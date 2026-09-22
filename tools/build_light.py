"""Light 发行包的确定性 staging 与构建原型（LIGHT_EDITION_DESIGN §4.4、§15.1）。

staging 规则：

- `raricy_launcher` 整包复制；
- `raricy_bot` 复制 **Light 闭包**：整包减去完整版专属的包与入口（见
  `LIGHT_EXCLUDED_PACKAGES` / `LIGHT_EXCLUDED_MODULES`）；
- 静态检查 staged 源码：无 MCP/发文导入，且任何导入都不越出闭包；
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

# Light 闭包 = raricy_bot − mcp/ − blog/ − 完整版 CLI 入口（设计 §4.2、§4.3）。
# `mcp/` 与 `blog/` 只服务完整版；`raricy_bot/__main__.py` 是完整版 CLI 入口
# （装配工具客户端与两个工厂），Light 的 Worker 入口是 `raricy_launcher.worker_main`。
LIGHT_EXCLUDED_PACKAGES: tuple[str, ...] = ("mcp", "blog")
LIGHT_EXCLUDED_MODULES: tuple[str, ...] = ("__main__.py",)

# staged 源码中禁止出现的导入：MCP SDK、核心 MCP 子系统与完整版发文子域（§4.2）。
FORBIDDEN_IMPORT_ROOTS: tuple[str, ...] = ("mcp", "raricy_bot.mcp", "raricy_bot.blog")

# staging 里的顶层包：只有指向它们的导入要做闭包判定。
_PACKAGE_ROOTS: tuple[str, ...] = ("raricy_bot", "raricy_launcher")

_PACKAGING_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "entry_light.py",
    "light.spec",
)

# staging 目录标记：只有带此文件的已存在目录才允许清理重建。
STAGING_MARKER_NAME = ".raricy-light-staging"

# 绝不允许作为 staging 目标的源码树。
_SOURCE_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "src",
    REPO_ROOT / "tools",
    REPO_ROOT / "packaging",
)


class StagingError(Exception):
    """staging 静态检查失败的固定错误。"""


def _resolve_staging_dir(staging_dir: Path) -> Path:
    """规范化 staging 路径并在必要时安全清理。

    拒绝仓库根、仓库祖先目录与源码树；已存在的非空目录必须带 staging
    标记才允许删除，防止 ``--staging .`` 之类的误用删掉用户文件。
    """
    resolved = Path(staging_dir).resolve()
    if resolved == REPO_ROOT or resolved in REPO_ROOT.parents:
        raise StagingError(f"refuse staging into repo root or ancestor: {resolved}")
    if any(resolved == source or source in resolved.parents for source in _SOURCE_DIRS):
        raise StagingError(f"refuse staging into source tree: {resolved}")
    if resolved.exists():
        if any(resolved.iterdir()) and not (resolved / STAGING_MARKER_NAME).is_file():
            raise StagingError(f"refuse to clean unmarked directory: {resolved}")
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)
    (resolved / STAGING_MARKER_NAME).write_text("raricy light staging\n", encoding="utf-8")
    return resolved


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _light_closure(core_root: Path) -> list[Path]:
    """Light 闭包内的源码文件（相对 `core_root`），按确定性顺序返回。"""
    closure: list[Path] = []
    for path in _iter_python_files(core_root):
        relative = path.relative_to(core_root)
        if relative.parts[0] in LIGHT_EXCLUDED_PACKAGES:
            continue
        if relative.as_posix() in LIGHT_EXCLUDED_MODULES:
            continue
        closure.append(relative)
    return closure


def _module_name(path: Path, *, source_root: Path) -> tuple[str, str]:
    """staged 文件的 `(模块名, 所属包)`；`__init__.py` 的包就是它自己。"""
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
        module = ".".join(parts)
        return module, module
    module = ".".join(parts)
    return module, ".".join(parts[:-1])


def _module_names(source_root: Path) -> frozenset[str]:
    """source_root 下存在的全部模块与包名。"""
    names: set[str] = set()
    for path in _iter_python_files(source_root):
        module, _package = _module_name(path, source_root=source_root)
        names.add(module)
    return frozenset(names)


def _import_targets(path: Path, *, source_root: Path) -> list[str]:
    """文件中出现的全部**模块路径候选**。

    `from X import a` 的 `a` 可能是符号，也可能是子模块 —— 静态层面分辨不出，
    所以两者都当候选列出；判定时用「源码树里存在、staging 里不存在」来区分
    「被排除的模块」与「符号名」。
    """
    _module, package = _module_name(path, source_root=source_root)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if not node.module:
                    continue
                targets.append(node.module)
                targets.extend(f"{node.module}.{alias.name}" for alias in node.names)
                continue
            # 相对导入：按 PEP 328 从当前包往上 level-1 层解析。
            base = package.split(".") if package else []
            climb = node.level - 1
            if climb:
                base = base[: len(base) - climb]
            prefix = base + node.module.split(".") if node.module else base
            targets.append(".".join(prefix))
            # `from . import texts` 的别名同样是候选（本仓库只有子模块这一种形态，
            # 但判定规则与上面一致，不靠这条假设）。
            targets.extend(".".join(prefix + [alias.name]) for alias in node.names)
    return targets


def _check_imports(
    path: Path,
    *,
    source_root: Path,
    staged_modules: frozenset[str],
    source_modules: frozenset[str],
) -> None:
    """单文件导入检查：禁止根不得出现，被排除的模块不得被导入。

    `source_modules` 是**整理份源码树**的模块全集：只有它才能区分「符号名」与
    「存在但被 Light 排除的模块」（如 `from raricy_bot import mcp`）。
    """
    for name in _import_targets(path, source_root=source_root):
        if any(name == root or name.startswith(f"{root}.") for root in FORBIDDEN_IMPORT_ROOTS):
            raise StagingError(f"{path.name}: forbidden import: {name}")
        if not name.startswith(_PACKAGE_ROOTS) or name in staged_modules:
            continue
        if name in source_modules:
            raise StagingError(f"{path.name}: import outside light closure: {name}")


def stage(staging_dir: Path) -> dict[str, str]:
    """生成 staging 并返回 {相对路径: sha256} 清单；失败抛 StagingError。"""
    staging_dir = _resolve_staging_dir(staging_dir)
    src_out = staging_dir / "src"
    core_out = src_out / "raricy_bot"
    core_out.mkdir(parents=True)
    core_root = REPO_ROOT / "src" / "raricy_bot"

    shutil.copytree(
        REPO_ROOT / "src" / "raricy_launcher",
        src_out / "raricy_launcher",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for relative in _light_closure(core_root):
        target = core_out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(core_root / relative, target)
    for name in _PACKAGING_FILES:
        shutil.copy2(PACKAGING_DIR / name, staging_dir / name)

    staged_modules = _module_names(src_out)
    source_modules = _module_names(REPO_ROOT / "src")
    for path in _iter_python_files(core_out):
        _check_imports(
            path,
            source_root=src_out,
            staged_modules=staged_modules,
            source_modules=source_modules,
        )
    for path in _iter_python_files(src_out / "raricy_launcher"):
        _check_imports(
            path,
            source_root=src_out,
            staged_modules=staged_modules,
            source_modules=source_modules,
        )

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
