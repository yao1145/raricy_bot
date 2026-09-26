"""生成托盘三态图标（N3 Task 1）：纯标准库、确定性输出。

用法::

    python tools/make_tray_icons.py --out src/raricy_launcher/assets

约定：

- 只依赖标准库（argparse/math/struct/pathlib），不引入图像库或 GUI 依赖：图标只有
  三个几何形状，写死字节布局比多背一个发行依赖便宜（见 DESIGN_DECISIONS.md D-138）；
- 输出**确定性**：不写时间戳、不用随机数，同样的输入永远得到同样的字节，因此图标
  可以入库，`git status` / `git diff` 也能如实回答「图标到底改没改」；
- 本工具只服务资源准备：不进 staging、不进冻结包、运行时不导入（INTERFACES.md §60）。
- 三个状态用**形状本身**区分（实心圆 / 空心圆环 / 实心三角），不单靠颜色：色觉障碍
  用户看不了颜色差别，16px 下的三个色块也难分（无障碍要求）。
"""

from __future__ import annotations

import argparse
import math
import struct
from pathlib import Path

# 尺寸集合固定：多尺寸一图让 Windows 在 100%/125%/150%/200% DPI 与「大图标」视图下
# 都能直接取到合适位图，而不是拿一张大图硬缩（缩出来的小图边缘会糊）。
SIZES: tuple[int, ...] = (16, 20, 24, 32, 48, 256)

# 每个像素再按 SUPERSAMPLE × SUPERSAMPLE 细分求覆盖比例，得到抗锯齿的 alpha。
SUPERSAMPLE: int = 4

# 三个状态的固定色（sRGB）。颜色是辅助信息，形状才是主要区分手段。
COLOR_NORMAL: tuple[int, int, int] = (0x2E, 0x7D, 0x32)  # 绿：运行中
COLOR_STOPPED: tuple[int, int, int] = (0x6B, 0x6B, 0x6B)  # 灰：已停止
COLOR_ATTENTION: tuple[int, int, int] = (0xC7, 0x77, 0x00)  # 琥珀：需要处理

# 状态名与文件名一一对应；状态名就是 tray_model 的 ICON_* 常量取值。
ICON_FILES: tuple[tuple[str, str], ...] = (
    ("normal", "tray-normal.ico"),
    ("stopped", "tray-stopped.ico"),
    ("attention", "tray-attention.ico"),
)

# 形状几何按「相对图标边长的比例」表达，所有尺寸共用同一套判定。
# 圆盘与圆环刻意留出边距：贴边的图标在任务栏上会被裁掉一圈。
_DISC_RADIUS: float = 0.44
_RING_OUTER_RADIUS: float = 0.44
_RING_INNER_RADIUS: float = 0.26

# 三角形顶点（相对坐标）：上顶点居中，底边略微上收，避免视觉上重心过低。
_TRIANGLE_VERTICES: tuple[tuple[float, float], ...] = (
    (0.5, 0.075),
    (0.065, 0.885),
    (0.935, 0.885),
)

# 默认输出目录 = 仓库里 Launcher 包资源目录（运行期按 `__file__` 同法定位）。
DEFAULT_OUT: Path = Path(__file__).resolve().parents[1] / "src" / "raricy_launcher" / "assets"


def _inside_disc(x: float, y: float) -> bool:
    """实心圆：到中心的距离不超过半径。"""
    return math.hypot(x - 0.5, y - 0.5) <= _DISC_RADIUS


def _inside_ring(x: float, y: float) -> bool:
    """空心圆环：距离落在内半径与外半径之间。"""
    distance = math.hypot(x - 0.5, y - 0.5)
    return _RING_INNER_RADIUS <= distance <= _RING_OUTER_RADIUS


def _inside_triangle(x: float, y: float) -> bool:
    """实心三角：用三个叉积同号判定（顶点顺序固定，边界算在内）。"""
    signs = [
        (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        for (ax, ay), (bx, by) in zip(
            _TRIANGLE_VERTICES,
            _TRIANGLE_VERTICES[1:] + _TRIANGLE_VERTICES[:1],
        )
    ]
    return all(sign >= 0.0 for sign in signs) or all(sign <= 0.0 for sign in signs)


_SHAPES = {
    "normal": (COLOR_NORMAL, _inside_disc),
    "stopped": (COLOR_STOPPED, _inside_ring),
    "attention": (COLOR_ATTENTION, _inside_triangle),
}


def _coverage(inside, size: int, column: int, row: int) -> int:
    """一个像素的 alpha（0-255）：16 个超采样点里落在形状内的比例。"""
    hits = 0
    for sub_row in range(SUPERSAMPLE):
        for sub_column in range(SUPERSAMPLE):
            x = (column + (sub_column + 0.5) / SUPERSAMPLE) / size
            y = (row + (sub_row + 0.5) / SUPERSAMPLE) / size
            if inside(x, y):
                hits += 1
    return round(255 * hits / (SUPERSAMPLE * SUPERSAMPLE))


def _xor_bitmap(size: int, color: tuple[int, int, int], inside) -> bytes:
    """XOR 位图：BGRA、逐行**自下而上**（DIB 惯例）、行按 4 字节对齐。

    32bpp 的行宽天然是 4 的倍数；颜色通道按 alpha 预乘 —— 这是 Windows 对
    32bpp 图标的约定，不预乘的话半透明边缘会偏亮、在任务栏上出现白边。
    背景透明：形状外的像素 alpha 为 0，颜色通道也随之为 0。
    """
    red, green, blue = color
    stride = ((size * 4 + 3) // 4) * 4
    rows: list[bytes] = []
    for row in range(size - 1, -1, -1):
        line = bytearray()
        for column in range(size):
            alpha = _coverage(inside, size, column, row)
            line += bytes(
                (
                    blue * alpha // 255,
                    green * alpha // 255,
                    red * alpha // 255,
                    alpha,
                )
            )
        line += bytes(stride - len(line))
        rows.append(bytes(line))
    return b"".join(rows)


def _image_bytes(size: int, color: tuple[int, int, int], inside) -> bytes:
    """一张位图：BITMAPINFOHEADER（40 字节）+ XOR 位图 + AND 掩码。"""
    xor_bitmap = _xor_bitmap(size, color, inside)
    # AND 掩码按 1bpp、行 4 字节对齐；全 0 表示「不透明」，透明度交给 alpha 通道
    # （掩码只是给不支持 alpha 的老消费方的兜底，现代 shell 不看它）。
    mask_stride = ((size + 31) // 32) * 4
    and_mask = bytes(mask_stride * size)
    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        size,  # biWidth
        size * 2,  # biHeight 写 2×高：ICO 里 XOR 位图与 AND 掩码上下叠放
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression = BI_RGB
        len(xor_bitmap) + len(and_mask),  # biSizeImage
        0,  # biXPelsPerMeter
        0,  # biYPelsPerMeter
        0,  # biClrUsed
        0,  # biClrImportant
    )
    return header + xor_bitmap + and_mask


def build_icon(state: str) -> bytes:
    """按固定字节布局拼出整张 ICO：ICONDIR + N × ICONDIRENTRY + N 张位图。"""
    color, inside = _SHAPES[state]
    images = [_image_bytes(size, color, inside) for size in SIZES]

    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for size, image in zip(SIZES, images):
        # 宽/高各占一个字节，256 放不下，按规范写 0。
        directory += struct.pack(
            "<BBBBHHII",
            0 if size == 256 else size,
            0 if size == 256 else size,
            0,  # bColorCount：真彩色图标不用调色板
            0,  # bReserved
            1,  # wPlanes
            32,  # wBitCount
            len(image),
            offset,
        )
        offset += len(image)
    return bytes(directory) + b"".join(images)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成托盘三态图标（确定性输出，重复运行字节一致）"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="输出目录（默认 src/raricy_launcher/assets）",
    )
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    for state, name in ICON_FILES:
        icon = build_icon(state)
        (args.out / name).write_bytes(icon)
        print(f"{name}: {len(icon)} bytes, sizes={','.join(str(size) for size in SIZES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
