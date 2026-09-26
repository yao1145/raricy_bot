"""移除账号与彻底删除：预览令牌、安全删除与墓碑（N2 Task 3；设计 §6.2、§6.3，D-145）。

三种删除语义严格分开，各有自己的入口与后果（§6.1）：停止机器人、清除凭据、移除账号
不是一件事。本模块负责第三种，并且只有两种模式：

1. **移除账号（保留本地数据，`keep_data`）**：撤销该档案的全部受管凭据引用，把档案
   转成 `detached`，配置、数据库、知识库、记忆与日志都留在磁盘上（可以重新绑定）。
2. **彻底删除（`purge_data`）**：在 1 的基础上删掉业务数据，只留最小墓碑
   `removed.json` 与 `data/`（含锁文件），供后续维护与共享入口拒绝。

**不做撤销**：两种模式都没有「恢复」；墓碑只说明「这里的数据已按用户要求删除」。

不变量：

1. **只接受档案 ID**：内部一律 `paths.profile_dir()` 解析目标，绝不接受调用方给的
   路径；目标必须严格位于受管档案根内且不等于根。
2. **逐层拒绝重解析点**：档案根链与遍历到的每个条目都 `os.lstat`（绝不跟随），
   带 `FILE_ATTRIBUTE_REPARSE_POINT` 或 `S_ISLNK` 立即停止推进；解析失败（`OSError`）
   同样拒绝 —— **不退回字符串路径**，也不把「读不出来」当成「没有」（D-130 同口径）。
3. **不跨卷递归**：每个目录的 `st_dev` 必须与档案根相同；每层进入前记下
   `(st_dev, st_ino)`，删除目录前重新 `lstat` 比对，不一致即停（防检查后换链）。
4. **可恢复顺序**（§6.2 六步）：记录先落盘 → `deleting` → 停 Worker 并确认退出 →
   取数据排他锁 → 清启动目标/活动指针 → 清凭据 → 按范围保留或删除 → 收尾。
   已清理的部分**不回滚**，中断后可以从头幂等地续做。
5. **墓碑最后落盘**：只有业务数据全部删除成功才写 `removed.json`；`profile.json`
   在墓碑之后才删 —— 崩溃在任何一步都不会留下「没有记录也没有墓碑」的僵尸档案。
6. 预览**只读**：不改文件、不删凭据、不写记录；确认令牌只在内存里，不落盘、不写日志。
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from raricy_bot.data_lock import DATA_LOCK_FILE, DataLockError, acquire_data_lock

from . import paths
from .config_service import ConfigServiceError
from .credential_lifecycle import (
    CREDENTIAL_KINDS,
    STATE_OWNED,
    STATE_PENDING_REMOVAL,
    STATE_REVOKED,
)
from .credential_store import CredentialStoreError
from .desktop_settings import DesktopSettingsError
from .lifecycle_service import (
    ERROR_CATALOG_WRITE_FAILED,
    ERROR_CREDENTIAL_BACKEND_UNAVAILABLE,
    ERROR_DATA_IN_USE,
    ERROR_REMOVAL_UNSAFE_PATH,
    ERROR_STOP_UNCONFIRMED,
    OP_STATE_FAILED,
    OP_STATE_FINISHED,
    OP_STATE_RUNNING,
    RESULT_REMOVE_FAILED,
    RESULT_REMOVED_DETACHED,
    RESULT_REMOVED_PARTIAL,
    RESULT_REMOVED_PURGED,
    STAGE_CLEAR_CREDENTIALS,
    STAGE_DETACH,
    STAGE_FINALIZE,
    STAGE_PREVIEW,
    STAGE_PURGE_DATA,
    STAGE_STOP,
    CredentialRef,
    OperationContext,
)
from .profile_service import (
    PROFILE_STATE_DETACHED,
    PROFILE_STATE_DELETING,
    ProfileError,
)

# 墓碑文档版本（确切值总表：`removed.json` 的 `schema_version = 1`）。
TOMBSTONE_SCHEMA_VERSION: int = 1

# 两种删除范围（确切值固定）。
SCOPE_KEEP_DATA: str = "keep_data"
SCOPE_PURGE_DATA: str = "purge_data"
REMOVAL_SCOPES: tuple[str, str] = (SCOPE_KEEP_DATA, SCOPE_PURGE_DATA)

# 数据类别的 key（确切值总表；顺序就是预览里的展示顺序）。
CATEGORY_CONFIG: str = "config"
CATEGORY_REVISIONS: str = "revisions"
CATEGORY_RUNTIME: str = "runtime"
CATEGORY_DATABASE: str = "database"
CATEGORY_MEMORY: str = "memory"
CATEGORY_KNOWLEDGE: str = "knowledge"
CATEGORY_LOGS: str = "logs"
CATEGORY_CREDENTIALS: str = "credentials"
CATEGORY_KEYS: tuple[str, ...] = (
    CATEGORY_CONFIG,
    CATEGORY_REVISIONS,
    CATEGORY_RUNTIME,
    CATEGORY_DATABASE,
    CATEGORY_MEMORY,
    CATEGORY_KNOWLEDGE,
    CATEGORY_LOGS,
    CATEGORY_CREDENTIALS,
)

# 预览的大小扫描有界：最多统计 20000 个条目，超出时 `size_complete=false`。
MAX_SCAN_ENTRIES: int = 20000

# 确认令牌有效期（秒，确切值总表：300）。
CONFIRMATION_TOKEN_TTL_SECONDS: float = 300.0

# 对外稳定码（`str(exc)` 就是它们；`removal_unsafe_path` 同时是记录里的错误码，
# 取值来自 `lifecycle_service` 的同一份定义，不另写一套）。
REMOVAL_SCOPE_INVALID: str = "removal_scope_invalid"
REMOVAL_TOKEN_INVALID: str = "removal_token_invalid"
REMOVAL_PREVIEW_STALE: str = "removal_preview_stale"
NOT_FOUND: str = "not_found"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RemovalBinding:
    """确认令牌的绑定内容（只存内存）：会话、档案、范围与两个 revision。"""

    profile_id: str
    scope: str
    session_id: str
    profile_revision: int
    catalog_revision: int


@dataclass(frozen=True)
class RemovalPreview:
    """`removal-preview` 的只读结果 + 只存内存的确认令牌（形状见 INTERFACES §59）。"""

    profile_id: str
    display_name: str
    site_user_id: str | None
    state: str
    is_active: bool
    running: bool
    is_startup_target: bool
    scope: str
    allowed_scopes: tuple[str, ...]
    categories: tuple[Mapping[str, Any], ...]
    size_bytes: int
    size_complete: bool
    credentials: Mapping[str, Any]
    profile_revision: int
    catalog_revision: int
    confirmation_token: str
    expires_in: int

    def to_document(self) -> dict[str, Any]:
        """预览对象本体（**不含**确认令牌，令牌由调用方单独放进响应信封）。"""
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "site_user_id": self.site_user_id,
            "state": self.state,
            "is_active": self.is_active,
            "running": self.running,
            "is_startup_target": self.is_startup_target,
            "scope": self.scope,
            "allowed_scopes": list(self.allowed_scopes),
            "categories": [dict(item) for item in self.categories],
            "size_bytes": self.size_bytes,
            "size_complete": self.size_complete,
            "credentials": dict(self.credentials),
            "profile_revision": self.profile_revision,
            "catalog_revision": self.catalog_revision,
        }


@dataclass(frozen=True)
class _TokenEntry:
    """一条内存令牌：绑定内容、到期时刻与「已用过」标记。"""

    binding: RemovalBinding
    expires_at: float
    used: bool = False


@dataclass(frozen=True)
class _TreeOutcome:
    """一次安全删除的结果：是否删完、拒绝原因与已删除的受管相对路径。"""

    complete: bool
    error: str | None
    removed: tuple[str, ...]


@dataclass(frozen=True)
class _ClearOutcome:
    """凭据清除的结果摘要：是否完全清干净（还有失败项或读不出来的历史就不是）。"""

    complete: bool
    revoked: tuple[str, ...]
    pending_refs: tuple[str, ...]
    new_ref: str | None


def _is_reparse(st: os.stat_result) -> bool:
    """条目是不是链接/重解析点：Windows 看属性位，POSIX 看 `S_ISLNK`。"""
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _identity(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


def _lstat_directory(path: Path) -> os.stat_result | None:
    """`lstat` 一个目录；不是目录、是链接/重解析点或读不出来都返回 None。"""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if _is_reparse(st) or not stat.S_ISDIR(st.st_mode):
        return None
    return st


def _prune_directory(
    directory: Path,
    *,
    root_dev: int,
    keep: frozenset[str],
    delete_self: bool,
    relative: str,
    removed: list[str],
) -> str | None:
    """递归删掉目录内容（保留 `keep` 里的名字）；完成返回 None，否则返回拒绝原因码。

    每一层都 `os.lstat`（绝不跟随链接）、比较 `st_dev`、并在删除目录前重取身份比对：
    任何异常、换链或跨卷都立即停止推进，**不退回字符串路径**（§6.2 末段）。
    """
    identity_st = _lstat_directory(directory)
    if identity_st is None:
        return ERROR_REMOVAL_UNSAFE_PATH
    if identity_st.st_dev != root_dev:
        return ERROR_REMOVAL_UNSAFE_PATH
    try:
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError:
        return ERROR_REMOVAL_UNSAFE_PATH
    for name in names:
        if name in keep:
            continue
        path = directory / name
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            # 条目在列目录之后消失（并发或上一次中断的续做）：没有东西可删。
            continue
        except OSError:
            return ERROR_REMOVAL_UNSAFE_PATH
        if _is_reparse(st):
            return ERROR_REMOVAL_UNSAFE_PATH
        child_relative = f"{relative}/{name}"
        if stat.S_ISDIR(st.st_mode):
            if st.st_dev != root_dev:
                # 不跨卷递归：换卷的目录一律拒绝（可能就是挂载点）。
                return ERROR_REMOVAL_UNSAFE_PATH
            reason = _prune_directory(
                path,
                root_dev=root_dev,
                keep=frozenset(),
                delete_self=True,
                relative=child_relative,
                removed=removed,
            )
            if reason is not None:
                return reason
        elif stat.S_ISREG(st.st_mode):
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError:
                return ERROR_REMOVAL_UNSAFE_PATH
            removed.append(child_relative)
        else:
            # 本程序不会创建设备、FIFO、socket 之类；不认识的类型一律不删。
            return ERROR_REMOVAL_UNSAFE_PATH
    if not delete_self:
        return None
    current = _lstat_directory(directory)
    if current is None or _identity(current) != _identity(identity_st):
        # 进入之后目录被换掉（检查后换链）：不删它。
        return ERROR_REMOVAL_UNSAFE_PATH
    try:
        os.rmdir(directory)
    except OSError:
        return ERROR_REMOVAL_UNSAFE_PATH
    removed.append(relative)
    return None


def _remove_tree(profile: Path, *, data_dir: Path) -> _TreeOutcome:
    """按「叶子文件 → 目录」删除档案内的业务数据，保留墓碑、`data/` 与锁文件。"""
    top = _lstat_directory(profile)
    if top is None:
        return _TreeOutcome(complete=False, error=ERROR_REMOVAL_UNSAFE_PATH, removed=())
    removed: list[str] = []
    relative = f"{paths.PROFILES_DIR}/{profile.name}"
    reason = _prune_directory(
        profile,
        root_dev=top.st_dev,
        # 墓碑、data/ 与 profile.json 在这里都不动：前两者保留，profile.json 在
        # 墓碑落盘之后才删（崩溃不会留下没有记录的僵尸档案）。
        keep=frozenset({paths.REMOVED_FILE, paths.DATA_DIR, paths.PROFILE_FILE}),
        delete_self=False,
        relative=relative,
        removed=removed,
    )
    if reason is not None:
        return _TreeOutcome(complete=False, error=reason, removed=tuple(removed))
    if _lstat_directory(data_dir) is not None:
        reason = _prune_directory(
            data_dir,
            root_dev=top.st_dev,
            keep=frozenset({DATA_LOCK_FILE}),
            delete_self=False,
            relative=f"{relative}/{paths.DATA_DIR}",
            removed=removed,
        )
        if reason is not None:
            return _TreeOutcome(complete=False, error=reason, removed=tuple(removed))
    return _TreeOutcome(complete=True, error=None, removed=tuple(removed))


def _unlink_leaf(path: Path) -> str | None:
    """删掉一个普通文件；缺失算完成，链接/重解析点与异常一律拒绝。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return ERROR_REMOVAL_UNSAFE_PATH
    if _is_reparse(st) or not stat.S_ISREG(st.st_mode):
        return ERROR_REMOVAL_UNSAFE_PATH
    try:
        os.unlink(path)
    except OSError:
        return ERROR_REMOVAL_UNSAFE_PATH
    return None


class RemovalService:
    """一个数据根上的移除入口：预览（只读）、令牌校验与六步删除事务。

    依赖都是注入的：`profiles`（`ProfileService`）、`credentials`
    （`CredentialLifecycle`）、可选 `desktop_settings`（`DesktopSettingsService`，
    用于清启动目标，缺省跳过）、可选 `running_profile`（返回当前运行档案 id 的只读
    回调，预览用）。`clock` 是单调时钟（令牌有效期），`wall_clock` 是墓碑时间戳用的
    墙钟。删除事务由 `lifecycle_service.LifecycleService.remove()` 在自己的线程里
    调用，阶段、记录与收尾都经 `OperationContext`（`context`），本模块不写记录。
    """

    def __init__(
        self,
        data_root: Path,
        *,
        profiles: Any,
        credentials: Any,
        desktop_settings: Any = None,
        running_profile: Callable[[], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = Path(data_root)
        self._profiles = profiles
        self._credentials = credentials
        self._desktop = desktop_settings
        self._running_profile = running_profile
        self._clock = clock
        self._wall_clock = wall_clock or _utc_now
        self._lock = threading.RLock()
        self._tokens: dict[str, _TokenEntry] = {}

    # --- 预览（只读） -----------------------------------------------------

    def preview(self, profile_id: str, *, scope: str, session_id: str) -> RemovalPreview:
        """生成删除预览并签发确认令牌；**不改任何文件、不删凭据、不写记录**。

        - 范围非法、或 `detached` 档案按 `keep_data` 预览 → `removal_scope_invalid`；
        - 未知档案、已删除（墓碑）档案 → `not_found`；
        - 目标路径不在受管档案根内、等于根或链上有重解析点 → `removal_unsafe_path`；
        - 令牌绑定 `session_id + profile_id + scope + profile_revision +
          catalog_revision`，有效期 300 秒，只存内存；同一会话对同一档案重新预览会
          让旧令牌失效。
        """
        if scope not in REMOVAL_SCOPES:
            raise ProfileError(REMOVAL_SCOPE_INVALID)
        if not isinstance(session_id, str) or not session_id:
            raise ProfileError(REMOVAL_TOKEN_INVALID)
        profile, record = self._assert_removable(profile_id)
        allowed = self._allowed_scopes(record)
        if scope not in allowed:
            raise ProfileError(REMOVAL_SCOPE_INVALID)
        catalog = self._profiles.catalog()
        categories, size_bytes, size_complete = self._scan(profile, scope)
        binding = RemovalBinding(
            profile_id=profile_id,
            scope=scope,
            session_id=session_id,
            profile_revision=record.profile_revision,
            catalog_revision=catalog.catalog_revision,
        )
        token = self._issue_token(binding)
        return RemovalPreview(
            profile_id=profile_id,
            display_name=record.display_name,
            site_user_id=record.site_user_id,
            state=record.state,
            is_active=catalog.active_profile_id == profile_id,
            running=self._running(profile_id),
            is_startup_target=self._startup_target() == profile_id,
            scope=scope,
            allowed_scopes=allowed,
            categories=categories,
            size_bytes=size_bytes,
            size_complete=size_complete,
            credentials=self._credential_summary(profile_id),
            profile_revision=record.profile_revision,
            catalog_revision=catalog.catalog_revision,
            confirmation_token=token,
            expires_in=int(CONFIRMATION_TOKEN_TTL_SECONDS),
        )

    def consume_token(
        self, profile_id: str, *, scope: str, token: str, session_id: str
    ) -> RemovalBinding:
        """校验并**消耗**确认令牌，返回它的绑定内容。

        未知、已过期、已经用过、会话或档案不符 → `removal_token_invalid`；
        scope 或两个 revision 与预览时不同 → `removal_preview_stale`。校验通过的
        令牌立即标记为已用：同一次预览只能发起一次删除（重试要重新预览）。
        """
        if not isinstance(token, str) or not token:
            raise ProfileError(REMOVAL_TOKEN_INVALID)
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None or entry.used:
                raise ProfileError(REMOVAL_TOKEN_INVALID)
            if self._clock() >= entry.expires_at:
                self._tokens.pop(token, None)
                raise ProfileError(REMOVAL_TOKEN_INVALID)
            binding = entry.binding
            if binding.session_id != session_id or binding.profile_id != profile_id:
                raise ProfileError(REMOVAL_TOKEN_INVALID)
            self._tokens[token] = replace(entry, used=True)
        if binding.scope != scope:
            raise ProfileError(REMOVAL_PREVIEW_STALE)
        if self._current_revisions(profile_id) != (
            binding.profile_revision,
            binding.catalog_revision,
        ):
            # 预览之后档案或目录变过：页面看到的类别与大小可能已经不准。
            raise ProfileError(REMOVAL_PREVIEW_STALE)
        return binding

    # --- 六步删除事务（§6.2） ---------------------------------------------

    def remove(
        self,
        context: OperationContext,
        *,
        profile_id: str,
        scope: str,
        binding: RemovalBinding,
    ) -> None:
        """执行删除事务；阶段、记录与终态都经 `context`（协调器负责串行化与恢复）。

        顺序固定：`preview`（落 `deleting`）→ `stop`（停 Worker、取数据锁、清启动
        目标与活动指针）→ `clear_credentials` → `detach` / `purge_data` → `finalize`。
        这个顺序从任何一步中断都可以重跑：`deleting` 档案允许继续预览；数据删除与
        凭据撤销都幂等；已清理的部分不回滚，结果按完成程度落 `removed_detached` /
        `removed_purged` / `removed_partial`。
        """
        try:
            profile, record = self._assert_removable(profile_id)
        except ProfileError as exc:
            # 目标在预览之后被删掉或被换成链：什么都不动，如实收场。
            context.finish(
                state=OP_STATE_FINISHED, result=RESULT_REMOVE_FAILED, error=str(exc)
            )
            return
        if scope not in self._allowed_scopes(record):
            context.finish(
                state=OP_STATE_FINISHED, result=RESULT_REMOVE_FAILED, error=REMOVAL_SCOPE_INVALID
            )
            return

        lock = None
        try:
            with context.stage(STAGE_PREVIEW, state=OP_STATE_RUNNING):
                # 第 1 步：记录（由 submit 落盘）之后立刻转 `deleting` —— 此后该档案
                # 拒绝启动与配置/草稿写入；受管路径在动数据之前先写进记录。
                context.record_details(managed_paths=self._managed_paths(profile, scope))
                self._profiles.set_state(profile_id, state=PROFILE_STATE_DELETING)
            with context.stage(STAGE_STOP):
                # 第 2 步：先停 Worker 并确认退出，再取数据排他锁（非阻塞）。
                if context.running_profile_id() == profile_id:
                    stop_operation = context.stop_worker()
                    if not context.await_exit(stop_operation):
                        context.finish(state=OP_STATE_FAILED, error=ERROR_STOP_UNCONFIRMED)
                        return
                try:
                    lock = acquire_data_lock(paths.data_dir(profile))
                except DataLockError:
                    # 数据被别的写者占用：停止推进，档案留在 `deleting` 可重试。
                    context.finish(state=OP_STATE_FAILED, error=ERROR_DATA_IN_USE)
                    return
                # 第 3 步：清启动目标与活动指针（**不得**自动选中并启动其他账号）。
                try:
                    self._clear_activation(profile_id)
                except ConfigServiceError:
                    # 指针写不进去：还没动数据，可重试。
                    context.finish(state=OP_STATE_FAILED, error=ERROR_CATALOG_WRITE_FAILED)
                    return
            with context.stage(STAGE_CLEAR_CREDENTIALS):
                # 第 4 步：先落记录（引用与归属），再动凭据库。
                outcome = self._clear_credentials(context, profile_id)
            if outcome is None:
                return
            # 第 5 步：按范围保留或删除；第 6 步：如实收尾。
            result, error = self._finish_scope(
                context,
                profile=profile,
                profile_id=profile_id,
                scope=scope,
                binding=binding,
                outcome=outcome,
            )
            with context.stage(STAGE_FINALIZE):
                context.record_details(managed_paths=self._remaining_paths(profile, scope))
            context.finish(state=OP_STATE_FINISHED, result=result, error=error)
        finally:
            if lock is not None:
                lock.release()

    # --- 内部：第 5 步（按范围保留或删除） --------------------------------

    def _finish_scope(
        self,
        context: OperationContext,
        *,
        profile: Path,
        profile_id: str,
        scope: str,
        binding: RemovalBinding,
        outcome: _ClearOutcome,
    ) -> tuple[str, str | None]:
        """执行范围对应的动作，返回 (结果码, 错误码)；不在这里落终态。"""
        if scope == SCOPE_KEEP_DATA:
            with context.stage(STAGE_DETACH):
                self._profiles.set_state(profile_id, state=PROFILE_STATE_DETACHED)
            return (
                RESULT_REMOVED_DETACHED if outcome.complete else RESULT_REMOVED_PARTIAL,
                None,
            )
        with context.stage(STAGE_PURGE_DATA):
            tree = _remove_tree(profile, data_dir=paths.data_dir(profile))
            if tree.complete:
                # 墓碑最后落盘：数据全删干净了才写；`profile.json` 在它之后才删。
                tree = self._write_tombstone(profile, profile_id, scope, binding, tree)
        if not tree.complete:
            return RESULT_REMOVED_PARTIAL, tree.error
        if not outcome.complete:
            return RESULT_REMOVED_PARTIAL, None
        return RESULT_REMOVED_PURGED, None

    def _write_tombstone(
        self,
        profile: Path,
        profile_id: str,
        scope: str,
        binding: RemovalBinding,
        tree: _TreeOutcome,
    ) -> _TreeOutcome:
        """写最小墓碑（不含正文与秘密），随后删掉 `profile.json`。"""
        document = {
            "schema_version": TOMBSTONE_SCHEMA_VERSION,
            "profile_id": profile_id,
            "state": "deleted",
            "removed_at": self._wall_clock().isoformat(),
            "scope": scope,
            "catalog_revision": binding.catalog_revision,
        }
        payload = (
            json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        )
        path = paths.removed_json_path(profile)
        try:
            handle_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
        except OSError:
            return _TreeOutcome(False, ERROR_REMOVAL_UNSAFE_PATH, tree.removed)
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return _TreeOutcome(False, ERROR_REMOVAL_UNSAFE_PATH, tree.removed)
        reason = _unlink_leaf(paths.profile_json_path(profile))
        if reason is not None:
            # 墓碑已经在场：档案对界面就是「已删除」；这一个文件删不掉只影响磁盘整洁。
            return _TreeOutcome(False, reason, tree.removed)
        return _TreeOutcome(True, None, tree.removed)

    def _clear_credentials(
        self, context: OperationContext, profile_id: str
    ) -> _ClearOutcome | None:
        """第 4 步：记录归属 → 清凭据 → 记录结果；失败返回 None（已落 failed）。"""
        managed = self._credentials.managed_refs(profile_id)
        # 关键副作用之前先落记录：哪些引用属于这个档案（不含取值，只有不透明引用）。
        context.record_details(
            credentials=tuple(CredentialRef(ref, STATE_OWNED) for ref in managed)
        )
        try:
            clear_result = self._credentials.clear(profile_id, kinds=CREDENTIAL_KINDS)
        except (CredentialStoreError, ConfigServiceError):
            # 凭据阶段整体失败（后端不可用、索引坏掉、回读不一致）：**不进入数据清理**
            # —— 以后就再也说不清哪个引用属于谁；已清掉的部分不回滚（一件都没做）。
            context.finish(state=OP_STATE_FAILED, error=ERROR_CREDENTIAL_BACKEND_UNAVAILABLE)
            return None
        entries = [CredentialRef(ref, STATE_REVOKED) for ref in clear_result.revoked]
        entries += [
            CredentialRef(ref, STATE_PENDING_REMOVAL) for ref in clear_result.pending_refs
        ]
        if clear_result.new_ref is not None:
            # 结构上不该出现（移除清的是全部受管类别）；真出现就如实记录，别把它
            # 当成没有，也**不**在这里重跑一次 clear()（那会把保留项也删掉）。
            entries.append(CredentialRef(clear_result.new_ref, "pending"))
        context.record_details(credentials=tuple(entries))
        return _ClearOutcome(
            complete=clear_result.ok and clear_result.new_ref is None,
            revoked=tuple(clear_result.revoked),
            pending_refs=tuple(clear_result.pending_refs),
            new_ref=clear_result.new_ref,
        )

    # --- 内部：启动目标与活动指针 -----------------------------------------

    def _clear_activation(self, profile_id: str) -> None:
        """清启动目标（若是它）与活动指针（若是活动档案）；不选中任何其他档案。"""
        self._clear_startup_target(profile_id)
        if self._profiles.catalog().active_profile_id == profile_id:
            self._profiles.clear_activation()

    def _clear_startup_target(self, profile_id: str) -> None:
        """被删档案是启动目标时：清空目标并关掉「打开 Light 时启动机器人」（D-150）。

        `launch_at_sign_in` 是用户自己的机器级选择，必须保留。桌面文件读不出来或写不
        进去时跳过：`Controller._auto_start()` 在目标档案不存在时会自己清空目标
        （D-150 第 2 条），不让一个坏文件或一次写失败挡住删除。
        """
        if self._desktop is None:
            return
        try:
            settings = self._desktop.read()
            if settings.startup_profile_id != profile_id:
                return
            self._desktop.update(
                settings.settings_revision,
                start_bot_on_launch=False,
                startup_profile_id=None,
            )
        except DesktopSettingsError:
            return

    def _startup_target(self) -> str | None:
        """桌面设置里的启动目标；读不出来按 None（预览不因此失败，D-150 兜底）。"""
        if self._desktop is None:
            return None
        try:
            return self._desktop.read().startup_profile_id
        except DesktopSettingsError:
            return None

    def _running(self, profile_id: str) -> bool:
        if self._running_profile is None:
            return False
        return self._running_profile() == profile_id

    # --- 内部：目标解析与路径安全 -----------------------------------------

    def _record_or_none(self, profile_id: str):
        for record in self._profiles.list_profiles():
            if record.profile_id == profile_id:
                return record
        return None

    def _assert_removable(self, profile_id: str):
        """校验目标可被移除：严格位于受管根内、不等于根、链上没有重解析点。"""
        try:
            paths.validate_profile_id(profile_id)
        except ValueError as exc:
            raise ProfileError(NOT_FOUND) from exc
        record = self._record_or_none(profile_id)
        if record is None:
            # 未知档案、或已删除（墓碑被 `list_profiles()` 隐藏）。
            raise ProfileError(NOT_FOUND)
        root = paths.profiles_root(self._root)
        profile = paths.profile_dir(self._root, profile_id)
        if paths.normalize_path(profile) == paths.normalize_path(root):
            raise ProfileError(ERROR_REMOVAL_UNSAFE_PATH)
        if not paths.is_within(root, profile):
            raise ProfileError(ERROR_REMOVAL_UNSAFE_PATH)
        self._require_no_reparse_points(profile_id)
        if not profile.is_dir():
            raise ProfileError(NOT_FOUND)
        return profile, record

    def _require_no_reparse_points(self, profile_id: str) -> None:
        """档案根链上的每一段都不得是链接/重解析点（相对数据根判，不动根自身）。

        只规范化后再比较不够：`profiles/` 或 `profiles/<id>` 本身做成链接时，规范化
        会把两边都解析到同一处而放行。这里按**未规范化的名字**逐段 `lstat`。
        """
        root = paths.normalize_path(self._root)
        chain = [root / paths.PROFILES_DIR, root / paths.PROFILES_DIR / profile_id]
        for path in chain:
            try:
                st = os.lstat(path)
            except OSError:
                # 解析失败（不存在、权限、占用）都不猜：删除路径只接受能看清楚的现场。
                raise ProfileError(ERROR_REMOVAL_UNSAFE_PATH)
            if _is_reparse(st):
                raise ProfileError(ERROR_REMOVAL_UNSAFE_PATH)

    def _current_revisions(self, profile_id: str) -> tuple[int, int] | None:
        """当前的 (profile_revision, catalog_revision)；档案不在了返回 None。"""
        record = self._record_or_none(profile_id)
        if record is None:
            return None
        catalog = self._profiles.catalog()
        return record.profile_revision, catalog.catalog_revision

    def _allowed_scopes(self, record) -> tuple[str, ...]:
        """`detached` 档案只允许彻底删除（保留数据的移除已经做过一次了）。"""
        if record.state == PROFILE_STATE_DETACHED:
            return (SCOPE_PURGE_DATA,)
        return REMOVAL_SCOPES

    # --- 内部：预览的类别、大小与凭据摘要 ---------------------------------

    def _category_targets(self, profile: Path) -> dict[str, tuple[Path, ...]]:
        data = paths.data_dir(profile)
        return {
            CATEGORY_CONFIG: (paths.config_path(profile), paths.draft_path(profile)),
            CATEGORY_REVISIONS: (paths.revisions_dir(profile),),
            CATEGORY_RUNTIME: (paths.profile_runtime_dir(profile),),
            CATEGORY_DATABASE: (
                data / "bot.db",
                data / "bot.db-wal",
                data / "bot.db-shm",
            ),
            CATEGORY_MEMORY: (data / "memory",),
            CATEGORY_KNOWLEDGE: (paths.knowledge_dir(profile),),
            CATEGORY_LOGS: (profile / paths.LOGS_DIR,),
            CATEGORY_CREDENTIALS: (),
        }

    def _scan(
        self, profile: Path, scope: str
    ) -> tuple[tuple[Mapping[str, Any], ...], int, bool]:
        """类别与大小（有界扫描，最多 20000 个条目；只读、不跟随链接）。"""
        budget = [MAX_SCAN_ENTRIES]
        targets = self._category_targets(profile)
        categories: list[Mapping[str, Any]] = []
        total = 0
        for key in CATEGORY_KEYS:
            size = 0
            for target in targets[key]:
                size += self._measure(target, budget)
            total += size
            categories.append(
                {
                    "key": key,
                    "size_bytes": size,
                    # 数据类别只在彻底删除时移除；凭据总是要撤销的。
                    "removable": key == CATEGORY_CREDENTIALS or scope == SCOPE_PURGE_DATA,
                }
            )
        return tuple(categories), total, budget[0] > 0

    def _measure(self, target: Path, budget: list[int]) -> int:
        """统计一个文件或目录的字节数；每个条目消耗一格预算，链接不计入。"""
        try:
            st = os.lstat(target)
        except OSError:
            return 0
        if _is_reparse(st):
            return 0
        if stat.S_ISREG(st.st_mode):
            if budget[0] <= 0:
                return 0
            budget[0] -= 1
            return st.st_size
        if not stat.S_ISDIR(st.st_mode):
            return 0
        total = 0
        stack = [target]
        while stack and budget[0] > 0:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    children = list(entries)
            except OSError:
                continue
            for entry in children:
                if budget[0] <= 0:
                    break
                budget[0] -= 1
                try:
                    child = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _is_reparse(child):
                    continue
                if stat.S_ISDIR(child.st_mode):
                    stack.append(Path(entry.path))
                elif stat.S_ISREG(child.st_mode):
                    total += child.st_size
        return total

    def _credential_summary(self, profile_id: str) -> dict[str, Any]:
        """预览里的凭据归属摘要：受管引用数、清理待办与归属是否完整。"""
        managed = self._credentials.managed_refs(profile_id)
        unreadable = self._credentials.unreadable_documents(profile_id)
        return {
            "managed": len(managed),
            "cleanup_pending": profile_id in self._credentials.pending_profiles(),
            # 有读不出来的历史快照时，「还引用过哪些凭据」并不完整（D-144）。
            "unknown_ownership": bool(unreadable),
        }

    # --- 内部：受管相对路径 ------------------------------------------------

    def _managed_paths(self, profile: Path, scope: str) -> tuple[str, ...]:
        """本次操作可能触碰的受管相对路径（`keep_data` 不动数据，因此为空）。"""
        if scope != SCOPE_PURGE_DATA:
            return ()
        return tuple(
            self._relative(profile, target)
            for targets in self._category_targets(profile).values()
            for target in targets
        )

    def _remaining_paths(self, profile: Path, scope: str) -> tuple[str, ...]:
        """范围里仍然存在的受管相对路径（部分完成时如实列出未完成项）。"""
        if scope != SCOPE_PURGE_DATA:
            return ()
        return tuple(
            self._relative(profile, target)
            for targets in self._category_targets(profile).values()
            for target in targets
            if os.path.lexists(target)
        )

    @staticmethod
    def _relative(profile: Path, path: Path) -> str:
        try:
            tail = path.relative_to(profile).as_posix()
        except ValueError:
            tail = path.name
        return f"{paths.PROFILES_DIR}/{profile.name}/{tail}"

    # --- 内部：令牌表 ------------------------------------------------------

    def _issue_token(self, binding: RemovalBinding) -> str:
        """签发新令牌；同一会话对同一档案的旧令牌全部失效（只用最新预览）。"""
        token = _secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            self._tokens = {
                key: value
                for key, value in self._tokens.items()
                if not (
                    value.binding.session_id == binding.session_id
                    and value.binding.profile_id == binding.profile_id
                )
            }
            self._tokens[token] = _TokenEntry(
                binding=binding, expires_at=now + CONFIRMATION_TOKEN_TTL_SECONDS
            )
        return token
