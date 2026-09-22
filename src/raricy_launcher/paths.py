"""Launcher 数据根、数据档案布局与路径安全（LIGHT_EDITION_DESIGN §9.5、§13.1）。

Windows 默认 ``%LOCALAPPDATA%\\RaricyBotLight``（§13.1）；其他平台仅保留边界，
未完成平台验收前不作为发行承诺。

```text
RaricyBotLight/
  launcher.json      活动档案指针与 Launcher schema（非敏感）
  runtime/           非敏感实例元数据，不是互斥锁的替代品
  diagnostics/       有界 Launcher 诊断
  profiles/<id>/
    config.yaml      正式非敏感配置与凭据引用
    draft.yaml       非敏感草稿
    revisions/       受控的非敏感回退/运行快照
    data/            bot.db（含 -wal/-shm）与 memory/
    knowledge/       受管 Markdown 资料
    logs/runtime/    有界运行日志
    logs/errors/     可选永久错误归档，不自动清理
```

路径规范化（`normalize_path` / `is_within`）覆盖相对路径、大小写与可识别的
链接别名：数据根下不允许任何未经检查的重解析点逃逸（§9.5）。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from uuid import uuid4

LAUNCHER_FILE: str = "launcher.json"
RUNTIME_DIR: str = "runtime"
DIAGNOSTICS_DIR: str = "diagnostics"
PROFILES_DIR: str = "profiles"
CONFIG_FILE: str = "config.yaml"
DRAFT_FILE: str = "draft.yaml"
REVISIONS_DIR: str = "revisions"
DATA_DIR: str = "data"
KNOWLEDGE_DIR: str = "knowledge"
LOGS_DIR: str = "logs"
RUNTIME_LOGS_SUBDIR: str = "runtime"
ERROR_LOGS_SUBDIR: str = "errors"

# 档案 id 的字符集：随机生成，但 launcher.json 可能被手工编辑，读取时要再校验一次。
# 档案 id：**只允许小写**（Windows 目录名不区分大小写，大小写混写会让同一个目录
# 出现两个指针），并排除 Windows 保留设备名。
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESERVED_STEMS: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def default_data_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "RaricyBotLight"
        return Path.home() / "AppData" / "Local" / "RaricyBotLight"
    return Path.home() / ".local" / "share" / "raricy-bot-light"


def launcher_json_path(data_root: Path) -> Path:
    """活动档案指针与 Launcher schema 的位置（非敏感）。"""
    return Path(data_root) / LAUNCHER_FILE


def runtime_dir(data_root: Path) -> Path:
    return Path(data_root) / RUNTIME_DIR


def diagnostics_dir(data_root: Path) -> Path:
    return Path(data_root) / DIAGNOSTICS_DIR


def profiles_root(data_root: Path) -> Path:
    return Path(data_root) / PROFILES_DIR


def new_profile_id() -> str:
    """一个新的档案 id；随机、无账号材料、可安全用作目录名。"""
    return f"p-{uuid4().hex[:12]}"


def validate_profile_id(profile_id: str) -> str:
    """校验档案 id；不合格直接拒绝（不静默改写成一个「差不多」的目录名）。"""
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.match(profile_id):
        raise ValueError("invalid_profile_id")
    if profile_id in _RESERVED_STEMS:
        # CON/NUL/COM1 之类在 Windows 上不是普通目录名；launcher.json 可被手工编辑，
        # 因此这里再挡一次，让失败是 invalid_profile_id 而不是系统调用报错。
        raise ValueError("invalid_profile_id")
    return profile_id


def profile_dir(data_root: Path, profile_id: str) -> Path:
    return profiles_root(data_root) / validate_profile_id(profile_id)


def config_path(profile: Path) -> Path:
    return Path(profile) / CONFIG_FILE


def draft_path(profile: Path) -> Path:
    return Path(profile) / DRAFT_FILE


def revisions_dir(profile: Path) -> Path:
    return Path(profile) / REVISIONS_DIR


def data_dir(profile: Path) -> Path:
    return Path(profile) / DATA_DIR


def knowledge_dir(profile: Path) -> Path:
    return Path(profile) / KNOWLEDGE_DIR


def runtime_logs_dir(profile: Path) -> Path:
    return Path(profile) / LOGS_DIR / RUNTIME_LOGS_SUBDIR


def error_logs_dir(profile: Path) -> Path:
    return Path(profile) / LOGS_DIR / ERROR_LOGS_SUBDIR


def profile_runtime_dir(profile: Path) -> Path:
    """档案自己的运行快照目录（§6.5）：根 `runtime/` 只放实例元数据（§13.1）。"""
    return Path(profile) / RUNTIME_DIR


def normalize_path(path: str | Path) -> Path:
    """规范化路径：绝对化、解析链接与重解析点、统一大小写。

    不要求目标存在（`strict=False`）：档案目录、数据库文件都可能在检查时还没建。
    解析失败（如不可达的 UNC）时退回 `abspath`，至少保证绝对与大小写一致。
    """
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except OSError:
        resolved = Path(os.path.abspath(Path(path).expanduser()))
    return Path(os.path.normcase(str(resolved)))


def is_within(root: str | Path, candidate: str | Path) -> bool:
    """`candidate` 规范化后是否落在 `root` 之内（含相等）。

    两侧都先 `normalize_path`：大小写、相对路径与链接别名因此都不会绕过包含判定
    —— 数据根下禁止未经检查的重解析点逃逸（§9.5）。
    """
    root_parts = normalize_path(root).parts
    candidate_parts = normalize_path(candidate).parts
    return candidate_parts[: len(root_parts)] == root_parts
