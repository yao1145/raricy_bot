"""配置事务：档案指针、草稿、revision 与凭据引用的一致提交（设计 §6、§7）。

三条不变量，任何改动都必须保持：

1. **密钥不落 YAML**：正式配置、草稿、回退快照里只有非敏感字段与一个不透明引用；
   密码与模型 Key 只存在于凭据库（§6.1、§6.3）。
2. **提交要么完整、要么是旧版本**：先登记脱敏、写新凭据集合并回读确认，再写同目录
   临时 YAML、flush/fsync、原子替换 `config.yaml`（§6.4）。任何一步失败都不动旧版本，
   也不会声称保存成功。
3. **revision 单调**：每次成功提交递增；提交必须带 `expected_revision`，与当前不符
   即拒绝，绝不覆盖其他页面的修改（§6.4 第 1 条）。

Launcher 掌控的字段（站点地址、档案内路径、探针监听、MCP/发文关闭）由本模块的基线
映射提供，**不接受**客户端提交（§5.3 的不可编辑项）。表单可编辑的字段见
`EDITABLE_FIELDS`，校验统一走 `raricy_bot.config.parse_config()`（§6.1）。
"""

from __future__ import annotations

import copy
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from raricy_bot import config as core_config
from raricy_bot.config import KIND_MISSING, Config, ConfigError, Secrets
from raricy_bot.logging_setup import secret_registry

from . import paths
from .credential_store import (
    CredentialStore,
    CredentialStoreError,
    new_reference,
)

if TYPE_CHECKING:  # 只为注解：运行时由调用方注入实例，避免与 lifecycle 互相导入。
    from .credential_lifecycle import CredentialLifecycle

# 档案内 `config.yaml` / `draft.yaml` 的 `_launcher.schema_version`：取值与校验口径
# 与拆分前完全一致（`_to_saved()` 仍要求等于它）。
CONFIG_SCHEMA_VERSION: int = 1

# `launcher.json` 本次写入的版本（N1 起为 2）；读取侧仍接受 1，见 `read_launcher_metadata()`。
LAUNCHER_SCHEMA_VERSION: int = 2

# `config.yaml` / `draft.yaml` 里的 Launcher 专属小节：Core 不认识它，
# Launcher 解析后把纯 Core 映射交给公共校验（§6.3）。
LAUNCHER_SECTION: str = "_launcher"

# 首版固定目标站点：不向普通用户开放任意站点地址（§5.3）。
LIGHT_SITE_BASE_URL: str = "https://raricy.com"

# System Prompt 的默认模板：向导把它作为初始值提供，用户可改（§5.1 第 3 点）。
# 正文规范见 docs/design/SYSTEM_PROMPTS.md；这里只放最小可用的起点文案。
DEFAULT_SYSTEM_PROMPT: str = (
    "你是站内通用聊天机器人。明确自己的机器人身份，跟随用户语言，"
    "保持简洁友好，不代表站方作正式承诺。"
)

# 表单可编辑字段（严格白名单，§6.1）：不在表里的键一律拒绝 —— 静默忽略会让
# 用户以为改动生效，而"保留未暴露字段"指的是服务端自己不删，不是接受任意键。
EDITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "model.base_url",
        "model.model",
        "model.temperature",
        "model.timeout_seconds",
        "model.max_output_tokens",
        "model.vision_enabled",
        "site.request_timeout_seconds",
        "behavior.context_turns",
        "behavior.context_input_tokens",
        "behavior.max_input_chars",
        "behavior.max_output_chars",
        "behavior.concurrency",
        "behavior.queue_size",
        "comments.enabled",
        "comments.recent_poll_seconds",
        "comments.notification_poll_seconds",
        "comments.context_turns",
        "comments.context_input_tokens",
        "knowledge_base.enabled",
        # KB 的「使用范围」：§13.2 要求启用前必须选定授权范围，不能只给一个开关。
        "knowledge_base.access_mode",
        "knowledge_base.allowed_channel_kinds",
        "knowledge_base.allowed_user_ids",
        "memory.enabled",
        # 记忆的允许使用者与共同记忆管理员分开（§5.3、§13.2）。
        "memory.access_mode",
        "memory.allow_user_list",
        "memory.admin_user_list",
        "logging.level",
        "system_prompt",
    }
)

# Launcher 掌控的字段：每次提交都从基线重建，不继承历史值，也不接受提交（§5.3）。
LAUNCHER_OWNED_FIELDS: frozenset[str] = frozenset(
    {
        "site.base_url",
        "storage.db_path",
        "ops.host",
        "ops.port",
        "knowledge_base.root_dir",
        "memory.root_dir",
        "logging.archive.directory",
        "mcp.enabled",
        "blog.enabled",
    }
)

# 必须落在本档案目录内的路径字段（§9.5：数据根下不允许未检查的重解析点逃逸）。
_CONTAINED_PATH_FIELDS: tuple[str, ...] = (
    "storage.db_path",
    "knowledge_base.root_dir",
    "memory.root_dir",
    "logging.archive.directory",
)

# 配置状态（§9.1）：配置就绪状态与进程状态分开维护。
STATE_NEEDS_SETUP: str = "needs_setup"
STATE_NEEDS_CREDENTIALS: str = "needs_credentials"
STATE_CONFIGURED: str = "configured"
STATE_INVALID: str = "invalid"
# 元数据故障的稳定恢复状态（F2）：坏元数据不是「首次运行」，不进入向导。
STATE_RECOVERY: str = "recovery"
# 「有档案但一个都没选中」（N2、D-145）：`launcher.json` 的 `active_profile` 键存在且
# 值为 null 时的合法状态。它**不是** `needs_setup`（那会打开空的首次设置向导），
# 也**不是** `recovery`（没有故障需要修复）：界面据此进入账号页。
STATE_NO_SELECTION: str = "no_selection"

# 元数据故障码（`ConfigStatus.error`）：四种互相可区分，恢复面板按码给文案。
METADATA_CORRUPT: str = "metadata_corrupt"
METADATA_UNREADABLE: str = "metadata_unreadable"
METADATA_UNSUPPORTED_VERSION: str = "metadata_unsupported_version"
METADATA_POINTER_INVALID: str = "metadata_pointer_invalid"

_METADATA_FAULT_CODES: frozenset[str] = frozenset(
    {
        METADATA_CORRUPT,
        METADATA_UNREADABLE,
        METADATA_UNSUPPORTED_VERSION,
        METADATA_POINTER_INVALID,
    }
)

# 「键不在」的哨兵：把「`active_profile` 键缺失」与「键存在且值为 null」分开
# （N2、D-145）—— 前者是元数据故障，后者是合法的「没有选中档案」。
_MISSING = object()

# `update_catalog()` 的窄写白名单：目录字段只有这四个，其余一律拒绝。
_CATALOG_FIELDS: frozenset[str] = frozenset(
    {"active_profile", "active_epoch", "catalog_revision", "schema_version"}
)

# 凭据操作（§7 的三种语义）。
ACTION_KEEP: str = "keep"
ACTION_REPLACE: str = "replace"
ACTION_DELETE: str = "delete"

# 凭据载荷里可以单独清除的类别（§6.1 的三种清除范围）。这里是**唯一定义**：
# `CredentialLifecycle` 的 `clear(kinds=…)` 与归属索引条目的 `kinds` 都从这里取，
# 不在别处再写一份取值。账号名不在其中：清除只针对密码与模型 Key。
KIND_PASSWORD: str = "password"
KIND_LLM_API_KEY: str = "llm_api_key"
CREDENTIAL_KINDS: tuple[str, str] = (KIND_PASSWORD, KIND_LLM_API_KEY)


class ConfigServiceError(Exception):
    """配置事务的固定错误；消息是稳定类别码。"""


class ConfigConflict(ConfigServiceError):
    """`expected_revision` 与当前 revision 不符（L3 映射为 409）。"""


class ConfigInvalid(ConfigServiceError):
    """字段、跨字段或能力策略校验失败。

    `code` 是**稳定类别码**（API 与状态里只出现它），`field` 与 `kind` 来自配置层，
    `message` 是给人看的中文说明，三者不混用（§8.2 的结构化错误）。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_value",
        field: str | None = None,
        kind: str = core_config.KIND_INVALID,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.kind = kind

    @classmethod
    def from_config_error(cls, exc: ConfigError) -> ConfigInvalid:
        return cls(str(exc), code=exc.kind, field=exc.field, kind=exc.kind)


@dataclass(frozen=True)
class CredentialUpdate:
    """一次提交里对某个凭据槽位的操作：保持、替换或删除（§7）。

    动作取值与替换值在**构造时**校验：写错一个动作名不能悄悄变成「删除」，
    那会把一次误操作变成「凭据没了」。
    """

    action: str
    value: str | None = None

    def __post_init__(self) -> None:
        if self.action not in (ACTION_KEEP, ACTION_REPLACE, ACTION_DELETE):
            raise ValueError("invalid_credential_action")
        if self.action == ACTION_REPLACE and (
            not isinstance(self.value, str) or not self.value.strip()
        ):
            raise ValueError("credential_value_required")

    @classmethod
    def keep(cls) -> CredentialUpdate:
        return cls(ACTION_KEEP)

    @classmethod
    def replace(cls, value: str) -> CredentialUpdate:
        return cls(ACTION_REPLACE, value)

    @classmethod
    def delete(cls) -> CredentialUpdate:
        return cls(ACTION_DELETE)

    def apply(self, current: str | None) -> str | None:
        if self.action == ACTION_KEEP:
            return current
        if self.action == ACTION_REPLACE:
            return self.value
        return None


@dataclass(frozen=True)
class SavedConfig:
    """一份正式配置的非敏感快照。"""

    revision: int
    mapping: Mapping[str, Any]
    credentials_ref: str | None
    account: str | None
    start_bot_on_launch: bool
    schema_version: int


@dataclass(frozen=True)
class RunLaunch:
    """一次启动需要的全部输入：版本、运行快照路径与凭据（§6.5）。"""

    revision: int
    config_path: Path
    credentials: Secrets


@dataclass(frozen=True)
class DraftConfig:
    """一份向导草稿：只有非敏感映射与它自己的 revision（§6.2）。"""

    revision: int
    mapping: Mapping[str, Any]


@dataclass(frozen=True)
class ConfigStatus:
    """配置就绪状态（§9.1）；`error` 是稳定类别码，不是异常文案。"""

    state: str
    revision: int | None = None
    account: str | None = None
    error: str | None = None


def light_base_mapping(profile: Path | None = None) -> dict[str, Any]:
    """Launcher 掌控的非敏感基线（§5.3 的不可编辑项）。

    - 站点地址固定，首版不开放任意站点地址；
    - 数据库、记忆、知识与永久归档目录都落在本档案内（§13.1）；
    - 运维探针只监听回环；端口由 Launcher 运行参数控制，不写进 YAML（§10.3）；
    - MCP 与定时发文在 Light 里不存在（§4.2）。

    `profile is None` 表示「还没有档案」：省略四个档案内路径字段，交给调用方
    （无档案的查询与向导临时校验）在不创建目录的前提下继续；正式提交路径始终
    传真实档案目录，取值与拆分前逐字节一致。
    """
    mapping: dict[str, Any] = {"site": {"base_url": LIGHT_SITE_BASE_URL}}
    if profile is not None:
        mapping["storage"] = {"db_path": str(paths.data_dir(profile) / "bot.db")}
    mapping["ops"] = {"host": "127.0.0.1"}
    if profile is not None:
        mapping["knowledge_base"] = {"root_dir": str(paths.knowledge_dir(profile))}
        mapping["memory"] = {"root_dir": str(paths.data_dir(profile) / "memory")}
        mapping["logging"] = {"archive": {"directory": str(paths.error_logs_dir(profile))}}
    mapping["mcp"] = {"enabled": False}
    mapping["blog"] = {"enabled": False}
    mapping["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    return mapping


# 草稿校验用的占位取值：把「还没填」的必填项补成合法值，公共校验才能一路走到
# 已填项上。它们只存在于内存里，绝不落盘（§6.2）。
_PROBE_VALUES: dict[str, Any] = {
    "model": {"base_url": "https://draft.invalid/v1", "model": "draft-model"},
    "system_prompt": "draft",
}


def _without_blanks(value: Any) -> Any:
    """递归去掉空串与 None：空白字段等于「还没填」，不是「填了个空值」。"""
    if isinstance(value, Mapping):
        return {
            key: _without_blanks(item)
            for key, item in value.items()
            if item not in ("", None)
        }
    if isinstance(value, list):
        return [_without_blanks(item) for item in value]
    return value


def _parse_revision(launcher: Mapping[str, Any], *, broken_code: str) -> int:
    """读 `_launcher.revision`：类型不对时给稳定错误，而不是把 ValueError 漏给调用方。

    `status()` 只捕获服务级错误；手工编辑出的 `revision: "abc"` 若直接 `int()`，
    状态查询会以 `ValueError` 失败，而不是如实报告 `invalid`（审查 P2）。
    """
    raw = launcher.get("revision", 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ConfigServiceError(broken_code)
    return raw


def _catalog_counter(value: Any) -> int:
    """读 launcher.json 里的非负计数；缺失或类型不对按 0 处理。

    只用于「以既有值为起点自增」与版本比较：手工编辑出的坏值不让首次初始化或
    版本升级变成 `ValueError`，写回时也会被替换成合法值。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _set_path(document: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cursor = document
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def _get_path(document: Mapping[str, Any], key: str) -> Any:
    cursor: Any = document
    for part in key.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并：`overlay` 覆盖 `base`，映射相加、其余取覆盖值。"""
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class ConfigService:
    """一个 Launcher 数据根的配置事务入口。

    所有写操作都在进程内写锁下进行；跨进程互斥由 Launcher 的单实例互斥体
    （§9.4）与数据档案锁（`raricy_bot.data_lock`）保证，配置目录本身不面向
    多写者设计。凭据库操作可能阻塞（系统授权框），调用方应在工作线程里调用
    本服务的方法（§7）。
    """

    def __init__(
        self,
        data_root: Path,
        *,
        credential_store: CredentialStore,
        profile_id: str | None = None,
        lock: threading.RLock | None = None,
        credential_lifecycle: CredentialLifecycle | None = None,
    ) -> None:
        self._root = Path(data_root)
        self._store = credential_store
        self._profile_id = profile_id
        # 缺省不装配归属索引：不装配索引的孤立测试走原来的提交路径（N2 之前的语义）。
        self._credential_lifecycle = credential_lifecycle
        self._bootstrap_lock = threading.Lock()
        # 可重入：`ensure_first_profile()` 可能在已持锁的提交路径上建立第一个档案
        # （§5.1 第 1 步），普通 Lock 会在那里自锁死（复审 N-1）。`for_profile()`
        # 的绑定实例必须复用同一把锁：同一数据根只允许一把进程内写锁。
        self._lock = lock if lock is not None else threading.RLock()

    def for_profile(self, profile_id: str) -> ConfigService:
        """绑定到指定档案的新实例：共享数据根、凭据库与同一把进程内写锁（要求 4）。

        绑定实例的读路径由 `profile_id` 直接求档案目录，**完全不读 `launcher.json`**，
        因此活动指针在别处切换也不会串档案（§4.1「一次请求内不反复从可变活动指针
        推导目录」）。写锁共享是硬要求：分开的锁会让两个实例对同一数据根并发写。
        归属索引（`credential_lifecycle`）一并传给绑定实例：档案切换后提交仍登记到
        自己的档案上。
        """
        paths.validate_profile_id(profile_id)
        return ConfigService(
            self._root,
            credential_store=self._store,
            profile_id=profile_id,
            lock=self._lock,
            credential_lifecycle=self._credential_lifecycle,
        )

    # --- 档案指针 ---------------------------------------------------------

    def read_launcher_metadata(self) -> dict[str, Any]:
        """读取 Launcher 元数据；不存在返回空映射，读到了但不能用则报稳定错误。

        「不存在」「读不到」「读到了但不能用」必须分开：把权限/占用错误或损坏的
        YAML 当成空元数据，会让管理页走进首次设置流程并在界面之外覆盖活动档案指针
        （§13.3、F2）。四种故障各有一个稳定码，装载进 `status()` 的恢复状态。

        `schema_version` 缺失是既有文件的正常形态（按旧文件读）；读取侧接受 1 与
        `LAUNCHER_SCHEMA_VERSION`，大于它才报 `metadata_unsupported_version`。
        """
        path = paths.launcher_json_path(self._root)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ConfigServiceError(METADATA_UNREADABLE) from exc
        except UnicodeDecodeError as exc:
            # 字节不是 UTF-8：读到了但不能用，与 YAML 语法错误同一类。
            # `UnicodeDecodeError` 不是 `OSError`，漏接会让 `status()` 抛出（F2）。
            raise ConfigServiceError(METADATA_CORRUPT) from exc
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigServiceError(METADATA_CORRUPT) from exc
        if not isinstance(data, dict):
            # 顶层不是映射：合法 YAML 但读不出元数据，同样不能当成首次运行。
            raise ConfigServiceError(METADATA_CORRUPT)
        version = data.get("schema_version")
        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int):
                # 版本字段类型不对：无法比较，按损坏处理（缺字段才等于「旧文件」）。
                raise ConfigServiceError(METADATA_CORRUPT)
            if version > LAUNCHER_SCHEMA_VERSION:
                # 版本比本程序新就不猜：宁可停在恢复状态，也不按未知格式解释（§6.5）。
                raise ConfigServiceError(METADATA_UNSUPPORTED_VERSION)
        return data

    def active_profile(self) -> str | None:
        """活动档案 id；没有指针时为 None，元数据故障则抛稳定错误。

        「没有指针」与「指针读不出来」不能混为一谈：后者要停在恢复状态，
        不能悄悄退回 None 让调用方以为这是首次运行（F2）。
        """
        value = self.read_launcher_metadata().get("active_profile")
        if not isinstance(value, str):
            return None
        try:
            return paths.validate_profile_id(value)
        except ValueError:
            return None

    def profile_id(self) -> str | None:
        return self._profile_id or self.active_profile()

    def _resolved_profile_id(self) -> str:
        """写路径上的档案 id；调用前必须已经过 `profile()`（必要时建立首个档案）。

        绑定实例直接用构造时的 `profile_id`（不读 `launcher.json`）；未绑定实例读一次
        活动指针。指针仍不可解析时抛稳定错误：写路径不在这里创建档案（F2）。
        """
        resolved = self._profile_id or self.active_profile()
        if resolved is None:
            raise ConfigServiceError(METADATA_POINTER_INVALID)
        return resolved

    def _has_existing_profiles(self) -> bool:
        """`profiles/` 下是否已有档案目录；读不了就当作有（宁可拒绝初始化）。"""
        try:
            entries = list(paths.profiles_root(self._root).iterdir())
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return any(entry.is_dir() for entry in entries)

    def pointer_is_empty(self) -> bool:
        """`active_profile` 键**存在且值为 null**：合法的「当前没有选中档案」（N2）。

        与「键缺失或取值非法」分开：后两者仍是元数据故障，由 `_resolve_pointer()`
        报 `metadata_pointer_invalid`。读元数据失败按 False 处理（故障自会由其它
        路径如实报告，这里不把读不出来误报成「没有选中」）。
        """
        try:
            metadata = self.read_launcher_metadata()
        except ConfigServiceError:
            return False
        return metadata.get("active_profile", _MISSING) is None

    def _resolve_pointer(self) -> str | None:
        """解析当前档案指针；**不建立**任何档案（查询路径专用，F2）。

        三种「没有指针」严格分开（N2、D-145）：

        - **键不存在**（含整个文件不存在）：元数据文件存在或 `profiles/` 下已有档案
          目录时抛 `metadata_pointer_invalid`，不能退化成首次运行 —— 首次初始化只
          允许在「元数据文件不存在且 `profiles/` 下没有任何既有档案目录」时发生；
        - **键存在且值为 null**：合法的「当前没有选中档案」（删除活动档案后的状态），
          返回 `None` 且不抛错；
        - 键存在但取值不是合法档案 ID：仍是 `metadata_pointer_invalid`。
        """
        profile_id = self.profile_id()
        if profile_id is not None:
            return profile_id
        if self.pointer_is_empty():
            return None
        if paths.launcher_json_path(self._root).exists() or self._has_existing_profiles():
            raise ConfigServiceError(METADATA_POINTER_INVALID)
        return None

    def require_profile(self) -> str:
        """当前档案 id；保留既有语义，委托给 `ensure_first_profile()`（要求 3）。

        既有调用方（提交、运行快照、控制面）不改变行为：首次初始化仍会建立第一个
        档案，其余四类元数据故障仍抛稳定错误。
        """
        return self.ensure_first_profile()

    def ensure_first_profile(self) -> str:
        """唯一允许建立首个档案的写入口（§5.1 第 1 步，要求 3）。

        条件与 N0 的 `require_profile()` 完全一致：只有「元数据文件不存在 **且**
        `profiles/` 下没有任何既有档案目录」才建立；元数据损坏、不可读、版本不支持
        或指针非法/缺失时一律不创建、不写指针，向上抛稳定错误（F2：自动新建会把
        损坏现场当成首次运行并覆盖它）。

        建立动作在专用锁内**重新检查**一次指针：并发首读如果各建一个档案，指针
        只会认最后一个，先建立的那些档案里的写入就再也看不见了（复审 N-2）。
        指针、`active_epoch` 与 `catalog_revision` 在同一次写入里落盘，不产生
        「指针有了但目录字段没写」的中间态。

        `active_profile` 键存在且为 null（「当前没有选中档案」）时**不建立**新档案，
        抛 `no_active_profile`：写路径必须先有一个被选中的档案，自动建一个并选中
        它等于替用户选了账号（§6.2「不得自动选中其他账号」），而且刚删掉档案的
        数据根上再冒出一个空档案正是用户要避免的。
        """
        profile_id = self._resolve_pointer()
        if profile_id is not None:
            return profile_id
        if self.pointer_is_empty():
            raise ConfigServiceError("no_active_profile")
        with self._bootstrap_lock:
            profile_id = self._resolve_pointer()
            if profile_id is None:
                if self.pointer_is_empty():
                    raise ConfigServiceError("no_active_profile")
                profile_id = paths.new_profile_id()
                self._create_first_profile(profile_id)
        return profile_id

    def _create_first_profile(self, profile_id: str) -> None:
        """建立首个档案：一次写入元数据，再把档案目录建出来。

        先写元数据再建目录：反过来一旦写元数据失败，就会留下「有档案目录但没有
        指针」的现场，下次读取只能停在 `metadata_pointer_invalid` 恢复态。
        """
        with self._lock:
            metadata = self.read_launcher_metadata()
            metadata["schema_version"] = LAUNCHER_SCHEMA_VERSION
            metadata["active_profile"] = profile_id
            metadata["active_epoch"] = _catalog_counter(metadata.get("active_epoch")) + 1
            metadata["catalog_revision"] = (
                _catalog_counter(metadata.get("catalog_revision")) + 1
            )
            self._write_document(paths.launcher_json_path(self._root), metadata)
        paths.profile_dir(self._root, profile_id).mkdir(parents=True, exist_ok=True)

    def profile(self) -> Path:
        """当前档案目录；绑定实例直接由 `profile_id` 求目录，不读 `launcher.json`。

        未绑定实例在「首次初始化」时经 `ensure_first_profile()` 建立；查询路径
        必须用 `profile_or_none()`，不得调用本方法（要求 2、4）。
        """
        return paths.profile_dir(self._root, self.require_profile())

    def profile_or_none(self) -> Path | None:
        """查询路径用的档案目录；没有档案返回 None，绝不建立首个档案（要求 2）。

        绑定实例直接由 `profile_id` 求目录，完全不读 `launcher.json`。
        """
        profile_id = self._resolve_pointer()
        if profile_id is None:
            return None
        return paths.profile_dir(self._root, profile_id)

    def set_active_profile(self, profile_id: str) -> None:
        """原子切换活动档案指针（§13.3：切换前必须确认旧 Worker 已退出，由调用方保证）。

        先读后写：读失败（损坏/不可读/版本不支持）必须直接失败，绝不把覆盖当成
        「修复」，损坏现场保持字节不变（F2）。**只有文件不存在的新根目录**才写
        `LAUNCHER_SCHEMA_VERSION`；文件已存在时原样保留它已有的 `schema_version`
        —— 包括「没有这个字段」的旧文件（`read_launcher_metadata()` 按正常旧文件
        读取）：一次指针写入不会**隐式升级**，升级是迁移的职责，只经
        `update_catalog()` 的显式入口。
        """
        paths.validate_profile_id(profile_id)
        metadata_path = paths.launcher_json_path(self._root)
        with self._lock:
            metadata = self.read_launcher_metadata()
            if not metadata_path.exists():
                metadata["schema_version"] = LAUNCHER_SCHEMA_VERSION
            metadata["active_profile"] = profile_id
            self._write_document(metadata_path, metadata)

    def update_catalog(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """目录字段的窄写入口：写锁内「读—改—原子写 `launcher.json`」（要求 5）。

        先读后写：读取失败（含四种元数据故障）直接抛出，绝不覆盖现场（F2）。
        只接受 `active_profile`、`active_epoch`、`catalog_revision` 与
        `schema_version`，其余键抛 `invalid_catalog_change`；`schema_version` 只
        允许**升到** `LAUNCHER_SCHEMA_VERSION`（当前值必须更小），降级与同级同样
        拒绝 —— 版本迁移只从这个显式入口发生，不会藏在别的写路径里。
        `active_profile` 接受 `None`，表示**清空指针**（「当前没有选中档案」，
        §6.2 的删除活动档案；键仍然写出来，值为 null），其余取值仍走
        `validate_profile_id`。返回写入后的完整元数据映射。

        调用前提：`launcher.json` 已存在，或本次 `changes` 显式带上
        `active_profile`。文件不存在时调用会写出**没有指针**的元数据，此后所有读取
        都按 `metadata_pointer_invalid` 停在恢复态 —— 迁移的 catalog 步与 N2 的
        删除流程必须自己保证指针在场（本入口只写它被要求写的字段）。
        """
        with self._lock:
            metadata = self.read_launcher_metadata()
            for key, value in changes.items():
                if key not in _CATALOG_FIELDS:
                    raise ConfigServiceError("invalid_catalog_change")
                if key == "active_profile":
                    # `None` 是显式清空（删除活动档案）：键写出来、值为 null。
                    if value is not None:
                        if not isinstance(value, str):
                            raise ConfigServiceError("invalid_catalog_change")
                        try:
                            paths.validate_profile_id(value)
                        except ValueError as exc:
                            raise ConfigServiceError("invalid_catalog_change") from exc
                elif key in ("active_epoch", "catalog_revision"):
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ConfigServiceError("invalid_catalog_change")
                else:  # schema_version
                    if (
                        value != LAUNCHER_SCHEMA_VERSION
                        or _catalog_counter(metadata.get("schema_version"))
                        >= LAUNCHER_SCHEMA_VERSION
                    ):
                        raise ConfigServiceError("invalid_catalog_change")
                metadata[key] = value
            self._write_document(paths.launcher_json_path(self._root), metadata)
            return metadata

    def start_bot_on_launch(self) -> bool:
        """档案里的**旧**启动偏好（只读口径）；桌面偏好的唯一来源是 `desktop.json`。

        保留它是为了让升级用户的一次性导入与 N1 迁移读到当年实际生效的取值；
        新提交不再写这个字段，新安装也读不到它（恒为 False）。
        """
        saved = self.load_saved()
        return bool(saved.start_bot_on_launch) if saved is not None else False

    # --- 读取 -------------------------------------------------------------

    def load_saved(self) -> SavedConfig | None:
        """读正式配置；不存在返回 None（`needs_setup`）。

        读是查询路径：不建立首个档案，也不写指针（F2）。首个档案只在写路径上由
        `ensure_first_profile()` 建立。
        """
        document = self._read_formal()
        if document is None:
            return None
        return self._to_saved(document)

    def load_draft(self) -> DraftConfig | None:
        profile = self.profile_or_none()
        if profile is None:
            return None
        data = self._read_document(
            paths.draft_path(profile), broken_code="draft_unreadable"
        )
        if data is None:
            return None
        launcher = data.get(LAUNCHER_SECTION)
        launcher = launcher if isinstance(launcher, dict) else {}
        return DraftConfig(
            revision=_parse_revision(launcher, broken_code="draft_unreadable"),
            mapping={key: value for key, value in data.items() if key != LAUNCHER_SECTION},
        )

    def status(self) -> ConfigStatus:
        """配置就绪状态（§9.1）。凭据库可能阻塞，调用方应在工作线程里调用。

        元数据故障落进稳定恢复状态，而不是 `needs_setup`；查询只报告，不修复、
        不创建、不覆盖，也不抛异常（F2）。在绑定实例上只报告该档案自身的状态。

        `active_profile` 为显式 null 时是 `no_selection`：有档案但一个都没选中
        （删掉活动档案后的正常状态），既不是首次设置，也不是需要修复（N2、D-145）。
        """
        try:
            saved = self.load_saved()
        except ConfigServiceError as exc:
            code = str(exc)
            if code in _METADATA_FAULT_CODES:
                return ConfigStatus(state=STATE_RECOVERY, error=code)
            return ConfigStatus(state=STATE_INVALID, error=code)
        if saved is None:
            if self._profile_id is None and self.pointer_is_empty():
                return ConfigStatus(state=STATE_NO_SELECTION)
            return ConfigStatus(state=STATE_NEEDS_SETUP)
        profile = self.profile_or_none()
        if profile is None:
            # 有正式配置就一定解析得出档案目录；真出现矛盾也不在这里创建档案。
            return ConfigStatus(state=STATE_NEEDS_SETUP)
        if not saved.credentials_ref:
            return ConfigStatus(
                state=STATE_NEEDS_CREDENTIALS,
                revision=saved.revision,
                account=saved.account,
            )
        try:
            credentials = self._store.get(saved.credentials_ref)
        except CredentialStoreError as exc:
            return ConfigStatus(
                state=STATE_NEEDS_CREDENTIALS,
                revision=saved.revision,
                account=saved.account,
                error=str(exc),
            )
        if credentials is None:
            # 仅内存凭据在 Controller 重启后不可解析：如实报告，不假装已保存（§6.4）。
            return ConfigStatus(
                state=STATE_NEEDS_CREDENTIALS,
                revision=saved.revision,
                account=saved.account,
            )
        if not (credentials.username and credentials.password and credentials.llm_api_key):
            # 载荷在、但某一项被清空（N2 的单独清除）：结构与可启动分开判定 ——
            # 配置仍然合法（`invalid` 只描述结构），但缺了任何一项都不能启动。
            # 与「没有引用」同形（`error=None`），这是用户主动清除后的正常状态（D-144）。
            return ConfigStatus(
                state=STATE_NEEDS_CREDENTIALS,
                revision=saved.revision,
                account=saved.account,
            )
        try:
            core_config.parse_config(
                saved.mapping, config_dir=str(profile), secrets=credentials
            )
            # 手工编辑过的配置也要过能力策略与路径包含检查（§9.1 的 invalid
            # 涵盖「Light 能力策略不符合要求」）。
            self._validate_policy(saved.mapping, profile)
        except ConfigError as exc:
            return ConfigStatus(
                state=STATE_INVALID,
                revision=saved.revision,
                account=saved.account,
                error=exc.kind,
            )
        except ConfigInvalid as exc:
            return ConfigStatus(
                state=STATE_INVALID,
                revision=saved.revision,
                account=saved.account,
                error=exc.code,
            )
        return ConfigStatus(
            state=STATE_CONFIGURED,
            revision=saved.revision,
            account=saved.account,
        )

    # --- 草稿（§6.2） ------------------------------------------------------

    def save_draft(
        self, values: Mapping[str, Any], *, expected_revision: int
    ) -> int:
        """保存非敏感草稿；允许缺必填项，但**已填写项**仍走同一套校验。

        返回草稿自己的新 revision。草稿不改变正式配置，也不接触凭据库。
        """
        with self._lock:
            profile = self.profile()
            current = self.load_draft()
            current_revision = current.revision if current is not None else 0
            if expected_revision != current_revision:
                raise ConfigConflict("revision_conflict")
            base = light_base_mapping(profile)
            if current is not None:
                base = _deep_merge(base, self._without_launcher_owned(current.mapping))
            merged = _merge_editable(base, values)
            self._validate(
                merged,
                credentials=None,
                allow_missing_required=True,
                profile=profile,
            )
            self._validate_policy(merged, profile)
            revision = current_revision + 1
            document = {
                LAUNCHER_SECTION: {
                    "schema_version": CONFIG_SCHEMA_VERSION,
                    "revision": revision,
                },
                **merged,
            }
            self._write_document(paths.draft_path(profile), document)
            return revision

    def validate_values(self, values: Mapping[str, Any]) -> None:
        """静态校验：只走公共字段规则与 Light 能力策略，不写盘、不碰凭据（§11）。

        与草稿同一口径：允许「还没填」，但**已填写项**必须合法。无档案时以
        `light_base_mapping(None)` 为基线（不含档案内路径字段，`_validate_policy`
        对缺失的路径字段跳过包含检查），校验全程只读、不建目录、不写指针。
        """
        with self._lock:
            profile = self.profile_or_none()
            base = light_base_mapping(profile)
            current = self._read_formal()
            if current is not None:
                saved = self._to_saved(current)
                base = _deep_merge(base, self._without_launcher_owned(saved.mapping))
            merged = _merge_editable(base, values)
            self._validate(
                merged,
                credentials=None,
                allow_missing_required=True,
                profile=profile,
            )
            self._validate_policy(merged, profile)

    # --- 正式提交（§6.4） --------------------------------------------------

    def commit(
        self,
        values: Mapping[str, Any],
        *,
        expected_revision: int,
        password: CredentialUpdate = CredentialUpdate(ACTION_KEEP),
        llm_api_key: CredentialUpdate = CredentialUpdate(ACTION_KEEP),
        account: str | None = None,
    ) -> int:
        """提交一份完整可用的正式配置，返回新 revision。

        顺序与失败语义严格按 §6.4：校验 → 登记脱敏 → 写新凭据并回读 → 写快照与
        原子替换。任一步失败都不动旧版本；新建但未被引用的凭据会被清理。

        正式配置**不再**承载桌面偏好：`start_bot_on_launch` 的唯一来源是
        `desktop.json`（§58、D-142/D-149）。历史快照里的同名字段只读保留
        （`SavedConfig.start_bot_on_launch`、`start_bot_on_launch()`），供升级
        导入与迁移判定使用，新提交不再写入。
        """
        with self._lock:
            profile = self.profile()
            profile_id = self._resolved_profile_id()
            current = self._read_formal()
            saved = self._to_saved(current) if current is not None else None
            current_revision = saved.revision if saved is not None else 0
            if expected_revision != current_revision:
                raise ConfigConflict("revision_conflict")

            # 1) 合并：Launcher 基线 + 上一次的非 Launcher 字段 + 本次白名单字段。
            base = light_base_mapping(profile)
            if saved is not None:
                base = _deep_merge(base, self._without_launcher_owned(saved.mapping))
            merged = _merge_editable(base, values)

            # 2) 组合新凭据集合：keep 用旧值，replace 用新值，delete 置空。
            #    账号名在第一次提交后固定：档案绑定的是站点账号（§13.3），换账号要
            #    重新设置（新档案）。否则「界面显示的账号」与「凭据里的账号」会拆成
            #    两个事实，状态显示 B 而实际仍以 A 登录（审查 F1）。
            old_credentials = self._resolve_credentials(saved)
            current_account = (saved.account if saved is not None else None) or ""
            requested_account = (account or "").strip()
            if requested_account and current_account and requested_account != current_account:
                raise ConfigInvalid(
                    "账号只能在重新设置流程里更换",
                    code="account_locked",
                    field="account",
                )
            new_account = requested_account or current_account
            new_secrets = Secrets(
                username=new_account,
                password=password.apply(old_credentials.password if old_credentials else None) or "",
                llm_api_key=(
                    llm_api_key.apply(old_credentials.llm_api_key if old_credentials else None) or ""
                ),
            )

            # 3) 校验：字段与跨字段规则、Light 能力策略、必要凭据齐备。
            #    校验在写任何东西之前完成，因此失败不会留下半份新配置。
            self._validate(
                merged,
                credentials=new_secrets,
                allow_missing_required=False,
                profile=profile,
            )
            self._validate_policy(merged, profile)

            # 4) 凭据：先登记脱敏（内存），再写库并回读确认；旧引用此时仍然有效。
            #    装配了归属索引时，登记的时点提前到写库**之前**：索引里先有
            #    `pending` 条目，崩溃重启才能找回「写过但尚未引用」的条目（§6.3、D-144）。
            credentials_ref = saved.credentials_ref if saved is not None else None
            created_ref: str | None = None
            if password.action == ACTION_REPLACE or llm_api_key.action == ACTION_REPLACE:
                secret_registry().register(new_secrets.password)
                secret_registry().register(new_secrets.llm_api_key)
                if self._credential_lifecycle is not None:
                    created_ref = self._credential_lifecycle.reserve(
                        profile_id=profile_id,
                        kinds=_payload_kinds(new_secrets),
                    )
                else:
                    created_ref = new_reference()
                try:
                    self._store.put(created_ref, new_secrets)
                    readback = self._store.get(created_ref)
                except BaseException:
                    # 写库或回读失败：回滚刚登记的引用（删不掉就留 orphan），旧引用不动。
                    self._discard_unreferenced(created_ref)
                    raise
                if readback != new_secrets:
                    # 回读不一致：不确定的新引用不复用，也不删除（§7 最后一段）。
                    self._mark_uncertain(created_ref)
                    raise CredentialStoreError("credential_readback_mismatch")
                credentials_ref = created_ref

            # 5) 落盘：先写回退快照，再原子替换正式配置。
            revision = current_revision + 1
            document = {
                LAUNCHER_SECTION: {
                    "schema_version": CONFIG_SCHEMA_VERSION,
                    "revision": revision,
                    "credentials_ref": credentials_ref,
                    "account": new_account,
                },
                **merged,
            }
            snapshot_path = paths.revisions_dir(profile) / f"{revision}.yaml"
            try:
                self._write_document(snapshot_path, document)
                self._write_document(paths.config_path(profile), document)
            except ConfigServiceError:
                # 这一版没有生效：快照也一起收回，否则它会引用一个刚被清理的凭据
                # 引用（审查 F6）。清理失败只是留下垃圾。
                self._discard_snapshot(snapshot_path)
                if created_ref is not None:
                    self._discard_unreferenced(created_ref)
                raise
            if created_ref is not None and self._credential_lifecycle is not None:
                # 配置已经落盘，这一版引用了它：标成 owned 并记下 revision。
                try:
                    self._credential_lifecycle.confirm(
                        created_ref, profile_id=profile_id, revision=revision
                    )
                except ConfigServiceError:
                    # 归属确认失败不推翻已提交的配置：引用就在 YAML 里，`reconcile()`
                    # 下次会把它认领回来（先登记后写库留下的恢复路径）。
                    pass
            return revision

    # --- 凭据清除后的窄写（§6.1、D-144） -----------------------------------

    def commit_credentials_clear(
        self,
        *,
        expected_revision: int,
        credentials_ref: str | None,
        account: str | None,
    ) -> int:
        """把「清除凭据」的结果写成一版新配置，返回新 revision。

        只改 `_launcher.credentials_ref`（`account` 非空时一并写账号名，其余键原样
        保留）。校验用 `allow_missing_required=True`：清除后配置**结构仍然有效**，
        只是缺了必填凭据 —— 「结构有效」与「可启动」分开判定，能不能启动交给
        `status()`（缺任何一项即 `needs_credentials`）。`credentials_ref=None` 表示
        密码与模型 Key 都已清除。

        装配了归属索引时：先在写任何东西之前确认索引可读（坏索引 → 稳定码，
        不写文件），写成功后再把新引用标成 `owned`（`clear()` 登记的是 `pending`）。
        """
        with self._lock:
            profile = self.profile()
            profile_id = self._resolved_profile_id()
            if self._credential_lifecycle is not None:
                # 破坏性操作拒绝推进：坏索引必须在写任何东西之前失败（§6.1）。
                self._credential_lifecycle.ensure_readable()
            current = self._read_formal()
            if current is None:
                raise ConfigServiceError("no_active_config")
            saved = self._to_saved(current)
            if expected_revision != saved.revision:
                raise ConfigConflict("revision_conflict")

            raw_launcher = current.get(LAUNCHER_SECTION)
            launcher = dict(raw_launcher) if isinstance(raw_launcher, dict) else {}
            launcher["credentials_ref"] = credentials_ref
            if account is not None:
                launcher["account"] = account
            revision = saved.revision + 1
            launcher["revision"] = revision
            document: dict[str, Any] = {
                **{key: value for key, value in current.items() if key != LAUNCHER_SECTION},
                LAUNCHER_SECTION: launcher,
            }
            mapping = {
                key: value for key, value in document.items() if key != LAUNCHER_SECTION
            }
            self._validate(
                mapping,
                credentials=None,
                allow_missing_required=True,
                profile=profile,
            )
            self._validate_policy(mapping, profile)

            snapshot_path = paths.revisions_dir(profile) / f"{revision}.yaml"
            try:
                self._write_document(snapshot_path, document)
                self._write_document(paths.config_path(profile), document)
            except ConfigServiceError:
                # 这一版没有生效：没被引用的快照一并收回（与 `commit()` 同一手法）。
                self._discard_snapshot(snapshot_path)
                raise
            if credentials_ref is not None and self._credential_lifecycle is not None:
                try:
                    self._credential_lifecycle.confirm(
                        credentials_ref, profile_id=profile_id, revision=revision
                    )
                except ConfigServiceError:
                    # 与 `commit()` 同一口径：归属确认失败不推翻已经写上的配置，
                    # `reconcile()` 会从 YAML 里把这条引用认领回来。
                    pass
            return revision

    # --- 运行快照与凭据（§6.5） --------------------------------------------

    def build_run_launch(self, revision: int) -> RunLaunch:
        """在写锁内**一次**取到指定版本的运行快照与对应凭据（§6.5）。

        分开调用快照与凭据会有窗口：中途又提交了新版本时，就会拼出「旧模型地址 +
        新 Key」的组合。入口启动 Worker 时只走这一个方法。
        """
        with self._lock:
            saved = self._require_saved(revision)
            if not saved.credentials_ref:
                raise ConfigServiceError("credentials_unresolved")
            credentials = self._store.get(saved.credentials_ref)
            if credentials is None:
                raise ConfigServiceError("credentials_unresolved")
            path = self._write_run_config(saved)
            return RunLaunch(
                revision=saved.revision, config_path=path, credentials=credentials
            )

    def build_run_config(self, revision: int) -> Path:
        """生成只含非敏感字段的运行配置文件并返回路径（§6.5）。

        **必须指定 revision**：不提供「当前版本」的默认值，是为了让调用方不得不
        明确它启动的是哪一版（§6.5 的「重启绑定目标版本」）。快照写在档案自己的
        `runtime/` 下，换档案不会互相覆盖。
        """
        saved = self._require_saved(revision)
        return self._write_run_config(saved)

    def credentials_for(self, revision: int) -> Secrets:
        """取指定 revision 对应的凭据，供注入子进程环境（§7）。**必须指定 revision**。"""
        saved = self._require_saved(revision)
        credentials = self._resolve_credentials(saved)
        if credentials is None:
            raise ConfigServiceError("credentials_unresolved")
        return credentials

    def _require_saved(self, revision: int) -> SavedConfig:
        saved = self.load_saved()
        if saved is None:
            raise ConfigServiceError("no_active_config")
        if revision != saved.revision:
            raise ConfigConflict("revision_conflict")
        return saved

    def _write_run_config(self, saved: SavedConfig) -> Path:
        target = paths.profile_runtime_dir(self.profile()) / f"run-{saved.revision}.yaml"
        self._write_document(target, dict(saved.mapping))
        return target

    # --- 内部：读取与解析 --------------------------------------------------

    def _read_document(self, path: Path, *, broken_code: str) -> dict[str, Any] | None:
        """有界读取一份 Launcher 文档；不存在返回 None，超限或损坏报稳定错误。"""
        try:
            with path.open("rb") as handle:
                raw = handle.read(core_config.MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            # 读不到不等于没有：当成「没有配置」会绕过 revision 冲突判定（§6.4）。
            raise ConfigServiceError(broken_code) from exc
        if len(raw) > core_config.MAX_CONFIG_BYTES:
            # 与 Core 的 YAML 上限同一口径（§6.3）：手工编辑出巨型文件也不能读进来。
            raise ConfigServiceError("config_too_large")
        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ConfigServiceError(broken_code) from exc
        if not isinstance(data, dict):
            raise ConfigServiceError(broken_code)
        return data

    def _read_formal(self) -> dict[str, Any] | None:
        """读正式配置的原始文档；不存在返回 None，损坏则报稳定错误。

        查询路径不得建立首个档案：没有指针就没有正式配置，交给 `status()` 报
        `needs_setup`（F2 的「查询不创建」）。
        """
        profile = self.profile_or_none()
        if profile is None:
            return None
        return self._read_document(paths.config_path(profile), broken_code="config_unreadable")

    def _to_saved(self, document: Mapping[str, Any]) -> SavedConfig:
        launcher = document.get(LAUNCHER_SECTION)
        launcher = launcher if isinstance(launcher, dict) else {}
        schema_version = launcher.get("schema_version")
        if schema_version != CONFIG_SCHEMA_VERSION:
            # 版本不认识就不猜：宁可报告无效，也不按新格式解释旧文件（§6.5）。
            raise ConfigServiceError("unsupported_schema")
        reference = launcher.get("credentials_ref")
        account = launcher.get("account")
        return SavedConfig(
            revision=_parse_revision(launcher, broken_code="config_unreadable"),
            mapping={key: value for key, value in document.items() if key != LAUNCHER_SECTION},
            credentials_ref=reference if isinstance(reference, str) and reference else None,
            account=account if isinstance(account, str) and account else None,
            start_bot_on_launch=bool(launcher.get("start_bot_on_launch", False)),
            schema_version=CONFIG_SCHEMA_VERSION,
        )

    def _resolve_credentials(self, saved: SavedConfig | None) -> Secrets | None:
        if saved is None or not saved.credentials_ref:
            return None
        return self._store.get(saved.credentials_ref)

    def _without_launcher_owned(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        """去掉 Launcher 掌控的字段：它们每次提交都从基线重建（§5.3）。"""
        trimmed = copy.deepcopy(dict(mapping))
        for key in LAUNCHER_OWNED_FIELDS:
            parts = key.split(".")
            cursor: Any = trimmed
            for part in parts[:-1]:
                if not isinstance(cursor, Mapping) or part not in cursor:
                    cursor = None
                    break
                cursor = cursor[part]
            if isinstance(cursor, dict):
                cursor.pop(parts[-1], None)
        return trimmed

    # --- 内部：校验 --------------------------------------------------------

    def _validate(
        self,
        mapping: Mapping[str, Any],
        *,
        credentials: Secrets | None,
        allow_missing_required: bool,
        profile: Path | None,
    ) -> None:
        """统一走 `parse_config()`：GUI 与 CLI 因此只有一份字段规则（§6.1）。

        草稿允许「还没填」，但**不允许填错**。`parse_config` 只报第一个错误，所以
        不能按错误分类放行 —— 那会让「A 项没填 + B 项填错」整体过关。这里改为用
        合法占位值补齐尚未填写的必填项后整体校验：剩下的任何错误都只可能来自
        已填写的字段（类型、范围、跨字段、URL 安全策略），一律拒绝（§6.2）。
        正式提交还要求凭据齐备 —— 缺凭据不是字段没填，而是不能启动（§7）。

        `profile` 是相对路径的基准目录；无档案的查询路径传 None，此时用数据根，
        并由调用方保证映射里不含档案内路径字段（那样不会误判包含关系）。
        """
        if not allow_missing_required:
            if credentials is None or not (
                credentials.username and credentials.password and credentials.llm_api_key
            ):
                raise ConfigInvalid(
                    "credentials_required",
                    code="credentials_required",
                    kind=core_config.KIND_MISSING,
                )
            probe = credentials
            document: Mapping[str, Any] = mapping
        else:
            probe = Secrets(username="draft", password="draft", llm_api_key="draft")
            document = _deep_merge(_PROBE_VALUES, _without_blanks(mapping))
        try:
            core_config.parse_config(
                document,
                config_dir=str(profile if profile is not None else self._root),
                secrets=probe,
            )
        except ConfigError as exc:
            raise ConfigInvalid.from_config_error(exc) from exc

    def _validate_policy(self, mapping: Mapping[str, Any], profile: Path | None) -> None:
        """Light 能力策略（§4.2、§9.5）：不能靠「界面没提供开关」来保证。

        无档案时（`profile is None`）路径字段一定缺失（基线省略、提交也拒绝这些
        键），缺失即跳过包含检查；一旦出现路径字段又无档案锚点，仍然拒绝。
        """
        for key in ("mcp.enabled", "blog.enabled"):
            if _get_path(mapping, key):
                raise ConfigInvalid(
                    f"Light 不支持该能力：{key}",
                    code="unsupported_capability",
                    field=key,
                )
        for key in _CONTAINED_PATH_FIELDS:
            value = _get_path(mapping, key)
            if value is None:
                continue
            if (
                profile is None
                or not isinstance(value, str)
                or not paths.is_within(profile, value)
            ):
                raise ConfigInvalid(
                    f"配置 {key} 必须位于当前档案目录内",
                    code="path_outside_profile",
                    field=key,
                )

    # --- 内部：落盘 --------------------------------------------------------

    def _write_document(self, path: Path, document: Mapping[str, Any]) -> None:
        """同目录临时文件 + flush + fsync + 原子替换：要么旧版本、要么新版本。"""
        payload = yaml.safe_dump(
            dict(document), allow_unicode=True, sort_keys=False
        ).encode("utf-8")
        if len(payload) > core_config.MAX_CONFIG_BYTES:
            raise ConfigServiceError("config_too_large")
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            handle_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
            )
        except OSError as exc:
            raise ConfigServiceError("config_write_failed") from exc
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            # 写盘失败只有一种对外语义：这一版没有生效（§6.4）。OSError 不再外泄，
            # L3 因此只需要处理服务级错误。
            raise ConfigServiceError("config_write_failed") from exc
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _discard_snapshot(self, path: Path) -> None:
        """删除未生效版本的快照；失败只是留下垃圾文件，不影响提交结果。"""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _discard_unreferenced(self, reference: str) -> None:
        """清理本次新建、但没能被配置引用的凭据；清理失败只是留下垃圾，不影响结果。

        装配了归属索引时走 `abandon()`：删得掉标 `revoked`，删不掉留 `orphan` +
        `last_error`，不把失败吞成无声的垃圾（D-144）。
        """
        if self._credential_lifecycle is not None:
            try:
                self._credential_lifecycle.abandon(reference, profile_id=self._profile_id)
            except ConfigServiceError:
                # 索引本身写不动时不再抛：调用方正在处理更重要的失败（原始异常）。
                pass
            return
        try:
            self._store.delete(reference)
        except CredentialStoreError:
            pass

    def _mark_uncertain(self, reference: str) -> None:
        """回读不一致的引用：不复用也不删除，只在索引里记下原因（D-127、D-144）。"""
        if self._credential_lifecycle is None:
            return
        try:
            self._credential_lifecycle.mark_uncertain(
                reference, error="credential_readback_mismatch"
            )
        except ConfigServiceError:
            pass


def _payload_kinds(secrets: Secrets) -> tuple[str, ...]:
    """这次写入的载荷实际装了哪些类别（账号名不算：它不参与清除）。"""
    values = {KIND_PASSWORD: secrets.password, KIND_LLM_API_KEY: secrets.llm_api_key}
    return tuple(kind for kind in CREDENTIAL_KINDS if values[kind])


def _merge_editable(
    base: Mapping[str, Any], values: Mapping[str, Any]
) -> dict[str, Any]:
    """把白名单内的可编辑字段合并进基线；其余键一律拒绝（§6.1）。"""
    merged = copy.deepcopy(dict(base))
    for key, value in values.items():
        if not isinstance(key, str) or key not in EDITABLE_FIELDS:
            raise ConfigInvalid(
                f"字段不可编辑：{key}", code="field_not_editable", field=str(key)
            )
        _set_path(merged, key, copy.deepcopy(value))
    return merged
