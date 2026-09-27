"""生成托盘三态图标（N3 Task 1）：纯标准库、确定性输出。

用法::

    python tools/make_tray_icons.py --out src/raricy_launcher/assets

约定：

- 只依赖标准库（argparse/math/struct/pathlib），不引入图像库或 GUI 依赖：四个几何形状
  的字节布局可直接生成（见 DESIGN_DECISIONS.md D-138、D-151）；
- 输出**确定性**：不写时间戳、不用随机数，同样的输入永远得到同样的字节，因此图标
  可以入库，`git status` / `git diff` 也能如实回答「图标到底改没改」；
- 本工具只服务资源准备：不进 staging、不进冻结包、运行时不导入（INTERFACES.md §60）。
- 三态都保留 favicon 的大圆、小圆、方块、三角：正常态全填充，停止态全描边，
  需处理态只填充三角。状态不单靠颜色区分。
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

# 与前端四个品牌色保持一致；状态还通过填充方式区分。
COLOR_ORBIT: tuple[int, int, int] = (0x63, 0xB8, 0xF6)
COLOR_SATELLITE: tuple[int, int, int] = (0xFF, 0x78, 0x82)
COLOR_VECTOR: tuple[int, int, int] = (0xFF, 0xD6, 0x6B)
COLOR_SIGNAL: tuple[int, int, int] = (0x37, 0xCD, 0xBD)
COLOR_MUTED: tuple[int, int, int] = (0xB2, 0xC6, 0xD8)

# 状态名与文件名一一对应；状态名就是 tray_model 的 ICON_* 常量取值。
ICON_FILES: tuple[tuple[str, str], ...] = (
    ("normal", "tray-normal.ico"),
    ("stopped", "tray-stopped.ico"),
    ("attention", "tray-attention.ico"),
)

# 形状位置与前端启动标记相同：左上大圆、右上小圆、左下方块、右下三角。
# 所有坐标按图标边长归一化，并留出透明边距。
_ORBIT: tuple[float, float, float] = (0.27, 0.27, 0.245)
_SATELLITE: tuple[float, float, float] = (0.80, 0.16, 0.13)
_SIGNAL: tuple[float, float, float, float] = (0.15, 0.63, 0.49, 0.96)
_TRIANGLE_VERTICES: tuple[tuple[float, float], ...] = (
    (0.76, 0.42),
    (0.54, 0.92),
    (0.98, 0.92),
)
_TRIANGLE_CENTER: tuple[float, float] = (0.76, (0.42 + 0.92 + 0.92) / 3)
_TRIANGLE_INNER: tuple[tuple[float, float], ...] = tuple(
    (_TRIANGLE_CENTER[0] + (x - _TRIANGLE_CENTER[0]) * 0.60,
     _TRIANGLE_CENTER[1] + (y - _TRIANGLE_CENTER[1]) * 0.60)
    for x, y in _TRIANGLE_VERTICES
)

# 默认输出目录 = 仓库里 Launcher 包资源目录（运行期按 `__file__` 同法定位）。
DEFAULT_OUT: Path = Path(__file__).resolve().parents[1] / "src" / "raricy_launcher" / "assets"


def _inside_triangle(
    x: float, y: float, vertices: tuple[tuple[float, float], ...]
) -> bool:
    """三个叉积同号表示点在三角形中，边界也算在内。"""
    signs = [
        (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        for (ax, ay), (bx, by) in zip(
            vertices,
            vertices[1:] + vertices[:1],
        )
    ]
    return all(sign >= 0.0 for sign in signs) or all(sign <= 0.0 for sign in signs)


def _sample_color(state: str, x: float, y: float) -> tuple[int, int, int] | None:
    """返回子像素的颜色；描边态只保留各形状的外缘。"""
    for (cx, cy, radius), thickness, color in (
        (_ORBIT, 0.065, COLOR_ORBIT),
        (_SATELLITE, 0.055, COLOR_SATELLITE),
    ):
        distance = math.hypot(x - cx, y - cy)
        if distance <= radius:
            filled = state == "normal"
            if filled or distance >= radius - thickness:
                return color if filled else COLOR_MUTED
            return None

    left, top, right, bottom = _SIGNAL
    if left <= x <= right and top <= y <= bottom:
        filled = state == "normal"
        if filled or min(x - left, right - x, y - top, bottom - y) <= 0.06:
            return COLOR_SIGNAL if filled else COLOR_MUTED
        return None

    if _inside_triangle(x, y, _TRIANGLE_VERTICES):
        filled = state in ("normal", "attention")
        if filled or not _inside_triangle(x, y, _TRIANGLE_INNER):
            return COLOR_VECTOR if filled else COLOR_MUTED
    return None


def _xor_bitmap(size: int, state: str) -> bytes:
    """XOR 位图：BGRA、逐行**自下而上**（DIB 惯例）、行按 4 字节对齐。

    32bpp 的行宽天然是 4 的倍数；颜色通道按 alpha 预乘 —— 这是 Windows 对
    32bpp 图标的约定，不预乘的话半透明边缘会偏亮、在任务栏上出现白边。
    背景透明：形状外的像素 alpha 为 0，颜色通道也随之为 0。
    """
    stride = ((size * 4 + 3) // 4) * 4
    rows: list[bytes] = []
    for row in range(size - 1, -1, -1):
        line = bytearray()
        for column in range(size):
            red = green = blue = hits = 0
            for sub_row in range(SUPERSAMPLE):
                for sub_column in range(SUPERSAMPLE):
                    x = (column + (sub_column + 0.5) / SUPERSAMPLE) / size
                    y = (row + (sub_row + 0.5) / SUPERSAMPLE) / size
                    color = _sample_color(state, x, y)
                    if color is not None:
                        hits += 1
                        red += color[0]
                        green += color[1]
                        blue += color[2]
            samples = SUPERSAMPLE * SUPERSAMPLE
            line += bytes(
                (
                    round(blue / samples),
                    round(green / samples),
                    round(red / samples),
                    round(255 * hits / samples),
                )
            )
        line += bytes(stride - len(line))
        rows.append(bytes(line))
    return b"".join(rows)


def _image_bytes(size: int, state: str) -> bytes:
    """一张位图：BITMAPINFOHEADER（40 字节）+ XOR 位图 + AND 掩码。"""
    xor_bitmap = _xor_bitmap(size, state)
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
    if state not in {name for name, _filename in ICON_FILES}:
        raise ValueError(state)
    images = [_image_bytes(size, state) for size in SIZES]

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
