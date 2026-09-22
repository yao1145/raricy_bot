"""Light 发行包的确定性 staging 与构建原型（LIGHT_EDITION_DESIGN §4.4、§15.1）。

staging 规则：

- `raricy_launcher` 整包复制；
- `raricy_bot` 只复制显式白名单模块（共享核心的最小闭包，见 `CORE_WHITELIST`）；
- 静态检查 staged 源码：无 MCP 导入、无白名单外的核心模块引用；
- `manifest.json` 记录排序后的文件 sha256，构建结果可核对；
- 仓库不长期维护第二份源码，staging 每次构建从当前源码重新生成。

用法::

    python tools/build_light.py [--staging DIR] [--pyinstaller]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = REPO_ROOT / "packaging" / "light"
DEFAULT_STAGING = REPO_ROOT / "build" / "light-staging"

# 共享核心的最小闭包：Launcher 的诊断出口只需要这两个模块。
# 扩充时同步 packaging/light/README.md 的白名单说明。
CORE_WHITELIST: tuple[str, ...] = ("__init__.py", "logging_setup.py", "redact.py")

# staged 源码中禁止出现的导入：MCP SDK 与核心 MCP 子系统（§4.2）。
FORBIDDEN_IMPORT_ROOTS: tuple[str, ...] = ("mcp", "raricy_bot.mcp")

_PACKAGING_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "entry_light.py",
    "light.spec",
)


class StagingError(Exception):
    """staging 静态检查失败的固定错误。"""


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _check_imports(path: Path, *, is_core: bool) -> None:
    """单文件导入检查；is_core 时本地模块引用必须在白名单内。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names = [node.module]
            elif node.level == 1 and is_core:
                # 核心白名单模块间的相对导入（如 from .redact import ...）；
                # from . import app 的目标在别名里，同样钉在白名单上。
                targets = [node.module] if node.module else [a.name for a in node.names]
                for target in targets:
                    if f"{target}.py" not in CORE_WHITELIST:
                        raise StagingError(
                            f"{path.name}: relative import outside whitelist: {target}"
                        )
        for name in names:
            if any(name == root or name.startswith(f"{root}.") for root in FORBIDDEN_IMPORT_ROOTS):
                raise StagingError(f"{path.name}: forbidden import: {name}")
            if is_core and name.startswith("raricy_bot"):
                remainder = name.removeprefix("raricy_bot")
                if remainder and remainder not in (".logging_setup", ".redact"):
                    raise StagingError(f"{path.name}: core import outside whitelist: {name}")
                # from raricy_bot import config 的别名同样是子模块目标。
                if isinstance(node, ast.ImportFrom) and not remainder:
                    for alias in node.names:
                        if f"{alias.name}.py" not in CORE_WHITELIST:
                            raise StagingError(
                                f"{path.name}: core import outside whitelist: {alias.name}"
                            )


def stage(staging_dir: Path) -> dict[str, str]:
    """生成 staging 并返回 {相对路径: sha256} 清单；失败抛 StagingError。"""
    staging_dir = Path(staging_dir)
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    src_out = staging_dir / "src"
    (src_out / "raricy_bot").mkdir(parents=True)

    shutil.copytree(
        REPO_ROOT / "src" / "raricy_launcher",
        src_out / "raricy_launcher",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in CORE_WHITELIST:
        shutil.copy2(REPO_ROOT / "src" / "raricy_bot" / name, src_out / "raricy_bot" / name)
    for name in _PACKAGING_FILES:
        shutil.copy2(PACKAGING_DIR / name, staging_dir / name)

    for path in _iter_python_files(src_out / "raricy_bot"):
        _check_imports(path, is_core=True)
    for path in _iter_python_files(src_out / "raricy_launcher"):
        _check_imports(path, is_core=False)

    manifest = {
        str(path.relative_to(staging_dir)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(staging_dir.rglob("*"))
        if path.is_file()
    }
    (staging_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _run_pyinstaller(staging_dir: Path) -> int:
    return subprocess.call(
        [sys.executable, "-m", "PyInstaller", "--clean", "-y", "light.spec"],
        cwd=staging_dir,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, default=DEFAULT_STAGING, help="staging 目录")
    parser.add_argument(
        "--pyinstaller",
        action="store_true",
        help="staging 后在 staging 内运行 PyInstaller（需已安装）",
    )
    args = parser.parse_args(argv)
    try:
        manifest = stage(args.staging)
    except StagingError as exc:
        print(f"staging 检查失败：{exc}", file=sys.stderr)
        return 2
    print(f"staging 完成：{args.staging}（{len(manifest)} 个文件）")
    if args.pyinstaller:
        return _run_pyinstaller(args.staging)
    return 0


if __name__ == "__main__":
    sys.exit(main())
