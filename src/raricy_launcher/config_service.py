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
from typing import Any

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

SCHEMA_VERSION: int = 1

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

# 凭据操作（§7 的三种语义）。
ACTION_KEEP: str = "keep"
ACTION_REPLACE: str = "replace"
ACTION_DELETE: str = "delete"


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


def light_base_mapping(profile: Path) -> dict[str, Any]:
    """Launcher 掌控的非敏感基线（§5.3 的不可编辑项）。

    - 站点地址固定，首版不开放任意站点地址；
    - 数据库、记忆、知识与永久归档目录都落在本档案内（§13.1）；
    - 运维探针只监听回环；端口由 Launcher 运行参数控制，不写进 YAML（§10.3）；
    - MCP 与定时发文在 Light 里不存在（§4.2）。
    """
    return {
        "site": {"base_url": LIGHT_SITE_BASE_URL},
        "storage": {"db_path": str(paths.data_dir(profile) / "bot.db")},
        "ops": {"host": "127.0.0.1"},
        "knowledge_base": {"root_dir": str(paths.knowledge_dir(profile))},
        "memory": {"root_dir": str(paths.data_dir(profile) / "memory")},
        "logging": {"archive": {"directory": str(paths.error_logs_dir(profile))}},
        "mcp": {"enabled": False},
        "blog": {"enabled": False},
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
    }


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
    ) -> None:
        self._root = Path(data_root)
        self._store = credential_store
        self._profile_id = profile_id
        self._lock = threading.Lock()

    # --- 档案指针 ---------------------------------------------------------

    def read_launcher_metadata(self) -> dict[str, Any]:
        """读取 Launcher 元数据；不存在返回空映射，读不到则报稳定错误。

        「不存在」与「读不到」必须分开：把权限/占用错误当成空元数据，会让管理页
        走进首次设置流程并在界面之外丢掉活动档案（§13.3）。
        """
        path = paths.launcher_json_path(self._root)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ConfigServiceError("metadata_unreadable") from exc
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}

    def active_profile(self) -> str | None:
        """活动档案 id；没有或不合格时为 None（不把损坏的指针当成有效档案）。"""
        value = self.read_launcher_metadata().get("active_profile")
        if not isinstance(value, str):
            return None
        try:
            return paths.validate_profile_id(value)
        except ValueError:
            return None

    def profile_id(self) -> str | None:
        return self._profile_id or self.active_profile()

    def require_profile(self) -> str:
        profile_id = self.profile_id()
        if profile_id is None:
            raise ConfigServiceError("no_active_profile")
        return profile_id

    def profile(self) -> Path:
        return paths.profile_dir(self._root, self.require_profile())

    def set_active_profile(self, profile_id: str) -> None:
        """原子切换活动档案指针（§13.3：切换前必须确认旧 Worker 已退出，由调用方保证）。"""
        paths.validate_profile_id(profile_id)
        with self._lock:
            metadata = self.read_launcher_metadata()
            metadata["schema_version"] = SCHEMA_VERSION
            metadata["active_profile"] = profile_id
            self._write_document(paths.launcher_json_path(self._root), metadata)

    def start_bot_on_launch(self) -> bool:
        """打开程序时是否启动 Bot（§5.2）；默认关闭，向导完成后由界面开启。"""
        saved = self.load_saved()
        return bool(saved.start_bot_on_launch) if saved is not None else False

    # --- 读取 -------------------------------------------------------------

    def load_saved(self) -> SavedConfig | None:
        """读正式配置；不存在返回 None（`needs_setup`）。"""
        document = self._read_formal()
        if document is None:
            return None
        return self._to_saved(document)

    def load_draft(self) -> DraftConfig | None:
        profile = self.profile()
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
        """配置就绪状态（§9.1）。凭据库可能阻塞，调用方应在工作线程里调用。"""
        try:
            saved = self.load_saved()
        except ConfigServiceError as exc:
            return ConfigStatus(state=STATE_INVALID, error=str(exc))
        if saved is None:
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
        try:
            core_config.parse_config(
                saved.mapping, config_dir=str(self.profile()), secrets=credentials
            )
            # 手工编辑过的配置也要过能力策略与路径包含检查（§9.1 的 invalid
            # 涵盖「Light 能力策略不符合要求」）。
            self._validate_policy(saved.mapping, self.profile())
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
            self._validate(merged, credentials=None, allow_missing_required=True)
            self._validate_policy(merged, profile)
            revision = current_revision + 1
            document = {
                LAUNCHER_SECTION: {
                    "schema_version": SCHEMA_VERSION,
                    "revision": revision,
                },
                **merged,
            }
            self._write_document(paths.draft_path(profile), document)
            return revision

    def validate_values(self, values: Mapping[str, Any]) -> None:
        """静态校验：只走公共字段规则与 Light 能力策略，不写盘、不碰凭据（§11）。

        与草稿同一口径：允许「还没填」，但**已填写项**必须合法。
        """
        with self._lock:
            profile = self.profile()
            base = light_base_mapping(profile)
            current = self._read_formal()
            if current is not None:
                saved = self._to_saved(current)
                base = _deep_merge(base, self._without_launcher_owned(saved.mapping))
            merged = _merge_editable(base, values)
            self._validate(merged, credentials=None, allow_missing_required=True)
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
        start_bot_on_launch: bool | None = None,
    ) -> int:
        """提交一份完整可用的正式配置，返回新 revision。

        顺序与失败语义严格按 §6.4：校验 → 登记脱敏 → 写新凭据并回读 → 写快照与
        原子替换。任一步失败都不动旧版本；新建但未被引用的凭据会被清理。
        """
        with self._lock:
            profile = self.profile()
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
            self._validate(merged, credentials=new_secrets, allow_missing_required=False)
            self._validate_policy(merged, profile)
            start_bot = (
                bool(start_bot_on_launch)
                if start_bot_on_launch is not None
                else bool(saved.start_bot_on_launch if saved else False)
            )

            # 4) 凭据：先登记脱敏（内存），再写库并回读确认；旧引用此时仍然有效。
            credentials_ref = saved.credentials_ref if saved is not None else None
            created_ref: str | None = None
            if password.action == ACTION_REPLACE or llm_api_key.action == ACTION_REPLACE:
                secret_registry().register(new_secrets.password)
                secret_registry().register(new_secrets.llm_api_key)
                created_ref = new_reference()
                self._store.put(created_ref, new_secrets)
                readback = self._store.get(created_ref)
                if readback != new_secrets:
                    # 回读不一致：不确定的新引用不复用，也不删除（§7 最后一段）。
                    raise CredentialStoreError("credential_readback_mismatch")
                credentials_ref = created_ref

            # 5) 落盘：先写回退快照，再原子替换正式配置。
            revision = current_revision + 1
            document = {
                LAUNCHER_SECTION: {
                    "schema_version": SCHEMA_VERSION,
                    "revision": revision,
                    "credentials_ref": credentials_ref,
                    "account": new_account,
                    "start_bot_on_launch": start_bot,
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
        """读正式配置的原始文档；不存在返回 None，损坏则报稳定错误。"""
        return self._read_document(paths.config_path(self.profile()), broken_code="config_unreadable")

    def _to_saved(self, document: Mapping[str, Any]) -> SavedConfig:
        launcher = document.get(LAUNCHER_SECTION)
        launcher = launcher if isinstance(launcher, dict) else {}
        schema_version = launcher.get("schema_version")
        if schema_version != SCHEMA_VERSION:
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
            schema_version=SCHEMA_VERSION,
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
    ) -> None:
        """统一走 `parse_config()`：GUI 与 CLI 因此只有一份字段规则（§6.1）。

        草稿允许「还没填」，但**不允许填错**。`parse_config` 只报第一个错误，所以
        不能按错误分类放行 —— 那会让「A 项没填 + B 项填错」整体过关。这里改为用
        合法占位值补齐尚未填写的必填项后整体校验：剩下的任何错误都只可能来自
        已填写的字段（类型、范围、跨字段、URL 安全策略），一律拒绝（§6.2）。
        正式提交还要求凭据齐备 —— 缺凭据不是字段没填，而是不能启动（§7）。
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
                document, config_dir=str(self.profile()), secrets=probe
            )
        except ConfigError as exc:
            raise ConfigInvalid.from_config_error(exc) from exc

    def _validate_policy(self, mapping: Mapping[str, Any], profile: Path) -> None:
        """Light 能力策略（§4.2、§9.5）：不能靠「界面没提供开关」来保证。"""
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
            if not isinstance(value, str) or not paths.is_within(profile, value):
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
        """清理本次新建、但没能被配置引用的凭据；清理失败只是留下垃圾，不影响结果。"""
        try:
            self._store.delete(reference)
        except CredentialStoreError:
            pass


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
