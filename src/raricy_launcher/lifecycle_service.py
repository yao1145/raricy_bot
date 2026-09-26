"""生命周期协调器：命令串行化、操作记录、取消代次与 A→B 事务
（LIGHT_EDITION_DESIGN §5.1、§5.2，INTERFACES §58/§59，D-143）。

本模块与 `lifecycle_gate.py` 是**两层**，不是一件事：

- `LifecycleGate` 是**单次操作**的非阻塞短租约，防的是站点测试与启停交错；
  本模块不重写它，也不绕过它 —— 触碰 `WorkerManager` 的那一小段仍要取它的租约，
  只是取不到时在**门外**做有界等待而不是立刻失败（§5.1 第 4 条）。
- 本模块负责**跨操作**的串行化与取消代次：任一时刻最多一个协调器操作
  （`activate` / `remove` / `credentials_clear`），`submit()` 取不到就回
  `lifecycle_busy`，**不排队、不等待**；`stop` / `quit` 不进这条队列，只提高取消代次。

不变量：

1. 持锁期间只记录操作租约与不可变输入（档案、revision、代次基线），绝不等 Worker、
   网络、keyring 或文件系统 —— 长等待一律在锁外，且由调用方/阶段钩子放在门外。
2. **关键副作用之前**先把阶段写进 `operations/<id>.json`；记录写不进去就立即失败
   （`record_write_failed`），绝不在没有记录的情况下继续做副作用。记录只有 ID、revision、
   固定阶段码、受管相对路径与凭据引用：没有聊天/System Prompt/KB/记忆正文、密码或原始异常。
3. 停止意图优先：`stop` / `quit` 提高取消代次后，本操作在提交前落 `cancelled_by_stop`、
   提交后落 `selected_only`，两种情况都不启动 Worker；`quit` 还永久关闭本次实例的启动入口。
4. 迟到的结果不覆盖当前状态：收尾只对**仍是未完成态**的记录生效，重复或过期的收尾被丢弃。
5. Worker 上报的身份键是 `(profile_id, config_revision, profile_epoch)`（Worker 再加
   `run_id`）：本模块派发时固定 `profile_id` 与目标 revision，判定归谁由状态层按该键完成，
   不匹配的旧结果属于旧操作。

记录读写只有本模块一个入口（`paths.operation_record_path()` 给路径），与配置提交同一手法
（同目录临时文件 + flush + fsync + `os.replace`）。单次启停**不写**恢复记录：它们由
`WorkerManager` 的操作承载（D-143）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from raricy_bot.logging_setup import log_event

from . import paths
from .config_service import STATE_CONFIGURED, ConfigServiceError
from .lifecycle_gate import Ticket
from .process_manager import OP_FAILED, STOP_BUDGET_MS
from .profile_service import PROFILE_STATE_ACTIVE, ProfileError

# --- 固定值（确切值总表；各任务共用，不另写一套） ---------------------------

OPERATION_SCHEMA_VERSION: int = 1

KIND_ACTIVATE: str = "activate"
KIND_REMOVE: str = "remove"
KIND_CREDENTIALS_CLEAR: str = "credentials_clear"
# 写恢复记录的三种操作；单次启停不在其中。
RECORDED_KINDS: frozenset[str] = frozenset(
    {KIND_ACTIVATE, KIND_REMOVE, KIND_CREDENTIALS_CLEAR}
)

OP_STATE_RESERVED: str = "reserved"
OP_STATE_RUNNING: str = "running"
OP_STATE_FINISHED: str = "finished"
OP_STATE_FAILED: str = "failed"
OP_STATE_CANCELLED: str = "cancelled"
OP_STATE_INTERRUPTED: str = "interrupted"
# 仍在进行（未落终态）的状态：`recover()` 只对账这些。
IN_FLIGHT_STATES: frozenset[str] = frozenset({OP_STATE_RESERVED, OP_STATE_RUNNING})
TERMINAL_STATES: frozenset[str] = frozenset(
    {OP_STATE_FINISHED, OP_STATE_FAILED, OP_STATE_CANCELLED, OP_STATE_INTERRUPTED}
)

# 阶段码。activate 的事务顺序：validate_target → reserve_operation → stop_A →
# confirm_A_exited → commit_active_B → invalidate_old_views → [start_B] → finished。
STAGE_VALIDATE_TARGET: str = "validate_target"
STAGE_RESERVE_OPERATION: str = "reserve_operation"
STAGE_STOP_A: str = "stop_A"
STAGE_CONFIRM_A_EXITED: str = "confirm_A_exited"
STAGE_COMMIT_ACTIVE_B: str = "commit_active_B"
STAGE_INVALIDATE_OLD_VIEWS: str = "invalidate_old_views"
STAGE_START_B: str = "start_B"
STAGE_FINISHED: str = "finished"
# remove 的阶段码（Task 3 使用，同一处定义）。
STAGE_PREVIEW: str = "preview"
STAGE_STOP: str = "stop"
STAGE_CLEAR_CREDENTIALS: str = "clear_credentials"
STAGE_DETACH: str = "detach"
STAGE_PURGE_DATA: str = "purge_data"
STAGE_FINALIZE: str = "finalize"
# credentials_clear 的阶段码（Task 2 使用）。
STAGE_COMMIT_CONFIG: str = "commit_config"

# activate 的结果码（写记录与 `/api/operations/{id}`，不是 HTTP 错误码）。
RESULT_STARTED: str = "started"
RESULT_SELECTED: str = "selected"
RESULT_SELECTED_ONLY: str = "selected_only"
RESULT_CANCELLED_BY_STOP: str = "cancelled_by_stop"
RESULT_START_FAILED: str = "start_failed"

# remove 的结果码（Task 3 使用，同一处定义）。
RESULT_REMOVED_DETACHED: str = "removed_detached"
RESULT_REMOVED_PURGED: str = "removed_purged"
RESULT_REMOVED_PARTIAL: str = "removed_partial"
RESULT_REMOVE_FAILED: str = "remove_failed"

# 记录里的错误码（三类操作共有 `record_write_failed` 与 `lifecycle_busy`）。
ERROR_STOP_UNCONFIRMED: str = "stop_unconfirmed"
ERROR_CATALOG_WRITE_FAILED: str = "catalog_write_failed"
ERROR_RESOLVE_FAILED: str = "resolve_failed"
ERROR_RECORD_WRITE_FAILED: str = "record_write_failed"
ERROR_LIFECYCLE_BUSY: str = "lifecycle_busy"
# remove 的错误码（Task 3 使用）：三者都表示「已清理的部分不回滚、档案留在
# `deleting` 可重试」（§6.2、D-145）。
ERROR_DATA_IN_USE: str = "data_in_use"
ERROR_REMOVAL_UNSAFE_PATH: str = "removal_unsafe_path"
ERROR_CREDENTIAL_BACKEND_UNAVAILABLE: str = "credential_backend_unavailable"
# 崩溃恢复给未完成记录的固定错误码。
ERROR_CONTROLLER_RESTART: str = "controller_restart"

# 对外抛出的稳定码（`api._handle` 按 `str(exc)` 给 409）。
CODE_TARGET_NOT_READY: str = "target_not_ready"
CODE_PROFILE_STATE_CONFLICT: str = "profile_state_conflict"
CODE_REVISION_CONFLICT: str = "revision_conflict"
CODE_IDEMPOTENCY_CONFLICT: str = "idempotency_conflict"
CODE_IDEMPOTENCY_KEY_REQUIRED: str = "idempotency_key_required"
# 下面三个与 §61 托盘端口的稳定码同值（同一套拒绝语义，两处各自成文）。
CODE_CONFIG_NOT_READY: str = "config_not_ready"
CODE_LIFECYCLE_BUSY: str = "lifecycle_busy"
CODE_QUITTING: str = "quitting"

# 「停止已确认」只认这三种管理器结果；`failed` 与在途都不算确认退出（§5.1 第 2 条）。
STOP_CONFIRMED_RESULTS: frozenset[str] = frozenset({"stopped", "cancelled", "forced_stop"})

# 管理器**拒绝派发**一次启动时给出的结果码：`quitting` 是退出流程已开始，
# `operation_in_progress` 是重启仍在途。两种都没有拉起 Worker，绝不能记成 `started`
# （否则页面会显示「已启动」而实际没有进程）。
START_REFUSED_RESULTS: frozenset[str] = frozenset({"quitting", "operation_in_progress"})

# 确认 A 的进程已回收的上限：2 × 停止预算 + 5 秒余量（＝ 45 秒）。构造参数可注入，
# 测试传小值，不做真实等待。
EXIT_CONFIRM_TIMEOUT_SECONDS: float = 2 * STOP_BUDGET_MS / 1000 + 5.0
# 退出确认的轮询间隔（只在锁外、无租约时使用）。
EXIT_CONFIRM_POLL_SECONDS: float = 0.1

# 门外有界等待：被站点测试占住时最多等 30 秒，每 0.2 秒重试一次，仍未取得才失败。
OPERATION_GATE_WAIT_SECONDS: float = 30.0
OPERATION_GATE_RETRY_SECONDS: float = 0.2

# 已完成记录（只清理 `finished`）的保留上限；`failed` / `interrupted` / `cancelled`
# 永不自动丢弃。
MAX_OPERATION_RECORDS: int = 50
# 幂等键与它的内存表同样有界。
IDEMPOTENCY_KEY_MIN_CHARS: int = 8
IDEMPOTENCY_KEY_MAX_CHARS: int = 64
_IDEMPOTENCY_KEY_RE = re.compile(
    rf"^[A-Za-z0-9_-]{{{IDEMPOTENCY_KEY_MIN_CHARS},{IDEMPOTENCY_KEY_MAX_CHARS}}}$"
)

# 协调器操作的执行线程名。
THREAD_NAME: str = "raricy-lifecycle"

# 身份键：结果的归属判据（Worker 上报再加 `run_id`）。
IdentityKey = tuple[str | None, int | None, int | None]


def new_operation_id() -> str:
    """一个新的操作 id；形状固定为 `op-` + 12 位小写十六进制（`paths` 按形状校验）。"""
    return f"op-{uuid4().hex[:12]}"


def _request_digest(kind: str, request: Mapping[str, Any]) -> str:
    """幂等摘要：规范化请求字段的 JSON 排序键 + sha256。

    输入只放非敏感字段（档案 id、revision、代次、布尔开关），摘要也只在内存里参与
    比较，既不落盘也不进日志。
    """
    payload = {"kind": kind, **dict(request)}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CredentialRef:
    """记录里的凭据引用：`ref` 是不透明标识，`state` 是清理状态（不含取值）。"""

    ref: str
    state: str

    def to_document(self) -> dict[str, str]:
        return {"ref": self.ref, "state": self.state}


@dataclass(frozen=True)
class OperationRecord:
    """`operations/<id>.json` 的只读快照（字段与文档一一对应）。

    记录**只**承载最小恢复信息：ID、revision、固定阶段码、受管相对路径与凭据引用。
    聊天、System Prompt、KB/记忆正文、密码、模型 Key 与原始异常都不在这里。
    """

    operation_id: str
    kind: str
    state: str
    stage: str
    profile_id: str | None = None
    from_profile_id: str | None = None
    to_profile_id: str | None = None
    target_revision: int | None = None
    target_epoch: int | None = None
    idempotency_key: str | None = None
    retry_of: str | None = None
    started_at: str = ""
    updated_at: str = ""
    finished_at: str | None = None
    error: str | None = None
    result: str | None = None
    credentials: tuple[CredentialRef, ...] = ()
    managed_paths: tuple[str, ...] = ()
    schema_version: int = OPERATION_SCHEMA_VERSION

    def to_document(self) -> dict[str, Any]:
        """键与顺序固定（§58 的形状）；值只出现固定码、id 与路径。"""
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "profile_id": self.profile_id,
            "from_profile_id": self.from_profile_id,
            "to_profile_id": self.to_profile_id,
            "target_revision": self.target_revision,
            "target_epoch": self.target_epoch,
            "idempotency_key": self.idempotency_key,
            "retry_of": self.retry_of,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "credentials": [item.to_document() for item in self.credentials],
            "managed_paths": list(self.managed_paths),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> OperationRecord:
        """从文档读回记录；读不出必要字段就抛 `ValueError`（调用方只跳过、不改现场）。"""
        if not isinstance(document, Mapping):
            raise ValueError("invalid_operation_record")
        operation_id = document.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("invalid_operation_record")

        def _text(key: str) -> str | None:
            value = document.get(key)
            return value if isinstance(value, str) and value else None

        def _count(key: str) -> int | None:
            value = document.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return value

        credentials: list[CredentialRef] = []
        raw_credentials = document.get("credentials")
        if isinstance(raw_credentials, list):
            for item in raw_credentials:
                if not isinstance(item, Mapping):
                    continue
                ref = item.get("ref")
                state = item.get("state")
                if isinstance(ref, str) and isinstance(state, str):
                    credentials.append(CredentialRef(ref=ref, state=state))
        managed_paths = tuple(
            item
            for item in document.get("managed_paths", [])
            if isinstance(item, str)
        ) if isinstance(document.get("managed_paths"), list) else ()
        schema_version = _count("schema_version")
        return cls(
            operation_id=operation_id,
            kind=_text("kind") or "unknown",
            state=_text("state") or OP_STATE_RUNNING,
            stage=_text("stage") or STAGE_RESERVE_OPERATION,
            profile_id=_text("profile_id"),
            from_profile_id=_text("from_profile_id"),
            to_profile_id=_text("to_profile_id"),
            target_revision=_count("target_revision"),
            target_epoch=_count("target_epoch"),
            idempotency_key=_text("idempotency_key"),
            retry_of=_text("retry_of"),
            started_at=_text("started_at") or "",
            updated_at=_text("updated_at") or "",
            finished_at=_text("finished_at"),
            error=_text("error"),
            result=_text("result"),
            credentials=tuple(credentials),
            managed_paths=managed_paths,
            schema_version=(
                schema_version if schema_version is not None else OPERATION_SCHEMA_VERSION
            ),
        )

    def identity_key(self) -> IdentityKey:
        """这次操作钉住的身份键：目标档案、目标 revision 与预留时的活动代次。"""
        return (self.profile_id, self.target_revision, self.target_epoch)

    def as_operation_view(self) -> dict[str, Any]:
        """`GET /api/operations/{id}` 用的最小视图（既有字段不动，另加 `stage`）。"""
        return {
            "id": self.operation_id,
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "result": self.result,
            "profile_id": self.profile_id,
            "revision": self.target_revision,
            "finished": self.state in TERMINAL_STATES,
        }


@dataclass(frozen=True)
class OperationContext:
    """一次协调器操作的执行体句柄：阶段、取消判定与收尾都只经它。

    `generation` 是**预留时**的取消代次；本操作自己派发的停止由 `stop_worker()`
    吸收（基线 +1），因此 `cancelled()` 只对外部 `stop` / `quit` 变真。
    """

    operation_id: str
    kind: str
    profile_id: str | None
    target_revision: int | None
    target_epoch: int | None
    generation: int
    _service: LifecycleService = field(repr=False, compare=False)

    def set_stage(self, stage: str, *, state: str | None = None) -> None:
        """记下当前阶段（落盘在阶段动作之前）；写盘失败抛 `record_write_failed`。"""
        self._service.set_operation_stage(self.operation_id, stage, state=state)

    def stage(self, stage: str, *, state: str | None = None):
        """阶段上下文：进入时落盘，退出时回调阶段钩子（异常路径也回调）。"""
        return self._service.stage_scope(self.operation_id, stage, state=state)

    def stop_worker(self) -> Any:
        """派发一次停止（在生命周期门租约内），并把这次代次变化计入自己的基线。"""
        return self._service.dispatch_stop(self.operation_id)

    def await_exit(self, stop_operation: Any) -> bool:
        """确认这次停止已回收进程（判据同 A→B 事务的 `confirm_A_exited`，§5.1 第 2 条）。"""
        return self._service.await_worker_exit(stop_operation)

    def running_profile_id(self) -> str | None:
        """当前运行中的档案 id（没有 Worker 或管理器尚未上报时 None，只读）。"""
        return self._service.running_profile_id()

    def record_details(
        self,
        *,
        credentials: tuple[CredentialRef, ...] | None = None,
        managed_paths: tuple[str, ...] | None = None,
    ) -> None:
        """把凭据引用与受管路径写进记录（**关键副作用之前**落盘，§6.2 第 4 步）。"""
        self._service.set_operation_details(
            self.operation_id, credentials=credentials, managed_paths=managed_paths
        )

    def cancelled(self) -> bool:
        """预留之后是否收到过外部的 `stop` / `quit`（自己的停止不算）。"""
        return self._service.operation_cancelled(self.operation_id)

    def finish(self, *, state: str, result: str | None = None, error: str | None = None) -> None:
        """落终态；只对仍是未完成态的记录生效（迟到的结果被丢弃，不覆盖当前状态）。"""
        self._service.finish_operation(
            self.operation_id, state=state, result=result, error=error
        )


class LifecycleService:
    """一个数据根上的生命周期协调器；所有公开方法线程安全。

    依赖都是注入的：`profiles`（`ProfileService` 兼容对象，除 N1 的只读查询外还要有
    `commit_activation()` 与 `set_state()`）、`manager`（`WorkerManager` 兼容对象）、
    `gate`（进程内唯一的 `LifecycleGate`）、`removal`（`RemovalService` 兼容对象，
    `remove` 命令用；缺省时该命令回 `config_not_ready`，仅供不装配移除的测试）。
    `clock` 是单调时钟（有界等待用），`wall_clock` 是记录时间戳用的墙钟；`sleep`
    可注入，测试因此不做真实等待。
    """

    def __init__(
        self,
        *,
        data_root: Path,
        profiles: Any,
        manager: Any,
        gate: Any,
        events: Any = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        exit_confirm_timeout: float = EXIT_CONFIRM_TIMEOUT_SECONDS,
        gate_wait_seconds: float = OPERATION_GATE_WAIT_SECONDS,
        stage_hook: Callable[[str, str], None] | None = None,
        removal: Any = None,
    ) -> None:
        self._root = Path(data_root)
        self._profiles = profiles
        self._manager = manager
        self._gate = gate
        self._events = events
        self._logger = logger
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._exit_confirm_timeout = exit_confirm_timeout
        self._gate_wait_seconds = gate_wait_seconds
        self._stage_hook = stage_hook
        self._removal = removal

        self._lock = threading.RLock()
        self._records: dict[str, OperationRecord] = {}
        # 当前未完成的协调器操作（操作租约）与它的代次基线。
        self._current: str | None = None
        self._current_generation = 0
        # 取消代次：`stop` / `quit` 各 +1；只增不减。
        self._generation = 0
        self._quitting = False
        # 幂等表：最近 50 个键 → (operation_id, 请求摘要)。
        self._idempotency: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._load_records()

    # --- 查询 -------------------------------------------------------------

    def operation(self, operation_id: str) -> OperationRecord | None:
        """按 id 取记录（内存视图）；未知 id 返回 None（由调用方回 404）。"""
        with self._lock:
            return self._records.get(operation_id)

    def current_operation(self) -> OperationRecord | None:
        """当前未完成的协调器操作；没有就是 None。

        状态聚合据此给出 `pending_operation`：协调器有未完成操作时它优先于管理器的
        在途操作，页面看到的阶段因此是切换事务自己的阶段。
        """
        with self._lock:
            if self._current is None:
                return None
            return self._records.get(self._current)

    def generation(self) -> int:
        """当前取消代次（诊断用；不参与判定）。"""
        with self._lock:
            return self._generation

    # --- 取消代次 ---------------------------------------------------------

    def request_stop(self) -> Any:
        """停止意图：**先**提高取消代次，再派发 `manager.stop()`，返回管理器操作。

        代次先落，排队中的启动/重启/切换后的启动就再也追不上这次停止（§5.1 第 1 条）。
        """
        with self._lock:
            self._generation += 1
        return self._manager.stop()

    def request_quit(self) -> None:
        """退出意图：提高取消代次并**永久关闭本次实例的启动入口**。

        这里不调 `manager.begin_quit()`：`WorkerManager.shutdown()` 已经会调，重复
        调用只会让「谁在拒绝启动」多一个来源（§5.1 第 1 条）。Worker 的收回由
        `shutdown()` 负责，本方法只承担协调器这一侧。
        """
        with self._lock:
            self._generation += 1
            self._quitting = True

    # --- 单次启停（托盘与 HTTP 的短命令端口） ------------------------------

    def start_bot(self, *, expected_epoch: int | None = None) -> str:
        """启动已保存版本，返回管理器 operation_id。

        `expected_epoch` 给定时必须是当前活动代次（旧页面不能启动新账号）；
        `None` 表示「用当前值」（托盘与本地调用）。配置不可用回 `config_not_ready`，
        有未完成的协调器操作或站点测试占门回 `lifecycle_busy`，退出流程中回 `quitting`。
        """
        profile_id = self._require_launch_context(expected_epoch)
        revision = self._saved_revision(profile_id)
        return self._dispatch_single("start", profile_id=profile_id, revision=revision)

    def stop_bot(self) -> str:
        """停止 Worker，返回管理器 operation_id；不要求已保存配置，也不排队等待。

        停止**不**受「有未完成的协调器操作」阻挡：停止意图优先，它的作用正是取消
        那些操作里尚未发生的启动。
        """
        ticket = self._gate.begin_operation("stop")
        if ticket is None:
            raise ConfigServiceError(CODE_LIFECYCLE_BUSY)
        try:
            operation = self.request_stop()
        finally:
            self._gate.end(ticket)
        return self._checked_operation_id(operation)

    def restart_bot(self, *, expected_epoch: int | None = None) -> str:
        """重启到已保存版本，返回管理器 operation_id（代次口径同 `start_bot`）。"""
        profile_id = self._require_launch_context(expected_epoch)
        revision = self._saved_revision(profile_id)
        return self._dispatch_single("restart", profile_id=profile_id, revision=revision)

    # --- A→B 事务 ---------------------------------------------------------

    def activate(
        self,
        profile_id: str,
        *,
        expected_epoch: int,
        expected_catalog_revision: int,
        target_revision: int | None = None,
        start: bool = True,
        idempotency_key: str,
    ) -> str:
        """选中或「停 A → 启动 B」的事务，返回 `operation_id`（异步，202）。

        顺序固定（§5.2）：`validate_target → reserve_operation → stop_A →
        confirm_A_exited → commit_active_B → invalidate_old_views → [start_B] → finished`。

        `validate_target` 在**预留记录之前、停 A 之前**同步完成：目标不是可用档案、
        代次/目录 revision 过期、`start=True` 时目标未配置、目标 revision 不是该档案
        已保存的 revision 或凭据不可解析，都直接抛稳定码，A 完全不动。
        `start=False` 是「只选中以修复」：跳过后一组检查，只写指针、不启动 Worker。
        """
        request = {
            "profile_id": profile_id,
            "expected_epoch": expected_epoch,
            "expected_catalog_revision": expected_catalog_revision,
            "target_revision": target_revision,
            "start": bool(start),
        }
        try:
            from_profile_id = self._profiles.catalog().active_profile_id
        except ConfigServiceError:
            # 元数据故障只在这里旁路：真正提交时 `validate()` 会读到同一次故障并如实抛出，
            # 重复提交（幂等命中）则不必再读目录。
            from_profile_id = None

        def validate() -> None:
            self._validate_activation(
                profile_id,
                expected_epoch=expected_epoch,
                expected_catalog_revision=expected_catalog_revision,
                target_revision=target_revision,
                start=start,
            )

        def body(context: OperationContext) -> None:
            self._run_activate(
                context,
                start=start,
                expected_epoch=expected_epoch,
                target_revision=target_revision,
            )

        return self.submit(
            kind=KIND_ACTIVATE,
            profile_id=profile_id,
            idempotency_key=idempotency_key,
            request=request,
            body=body,
            validate=validate,
            from_profile_id=from_profile_id,
            to_profile_id=profile_id,
            target_revision=target_revision,
            target_epoch=expected_epoch,
        )

    # --- 移除账号（Task 3 的删除事务） -------------------------------------

    def remove(
        self,
        profile_id: str,
        *,
        scope: str,
        confirmation_token: str,
        session_id: str,
        idempotency_key: str,
    ) -> str:
        """按确认令牌发起一次删除，返回 `operation_id`（异步，202）。

        令牌在**预留之前同步**校验并消耗：未知/过期/已用/会话或档案不符 →
        `removal_token_invalid`，scope 或两个 revision 与预览时不同 →
        `removal_preview_stale`。校验失败不留记录、没有任何副作用；校验通过后由
        `RemovalService.remove()` 在自己的线程里按 §6.2 的六步执行。重试要重新
        预览（新令牌）并换新的幂等键；幂等命中时不重跑校验，因此响应丢失后的
        重复提交不会产生第二次副作用。
        """
        removal = self._require_removal()
        digest = (
            hashlib.sha256(confirmation_token.encode("utf-8")).hexdigest()
            if isinstance(confirmation_token, str)
            else ""
        )
        request = {
            "profile_id": profile_id,
            "scope": scope,
            "session_id": session_id,
            # 令牌本身不进摘要输入：摘要在内存里参与幂等比较，不落盘。
            "token_digest": digest,
        }
        # 令牌校验 consume 之后才填：body 只在预留成功、validate 已经跑过时才执行。
        binding_box: dict[str, Any] = {}

        def validate() -> None:
            binding_box["binding"] = removal.consume_token(
                profile_id, scope=scope, token=confirmation_token, session_id=session_id
            )

        def body(context: OperationContext) -> None:
            removal.remove(
                context,
                profile_id=profile_id,
                scope=scope,
                binding=binding_box["binding"],
            )

        return self.submit(
            kind=KIND_REMOVE,
            profile_id=profile_id,
            idempotency_key=idempotency_key,
            request=request,
            body=body,
            validate=validate,
            to_profile_id=profile_id,
        )

    def _require_removal(self) -> Any:
        """移除服务（`RemovalService` 兼容对象）；没装配时按「配置不可用」如实拒绝。"""
        if self._removal is None:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY)
        return self._removal

    # --- 串行化与幂等（Task 2/3 的命令复用这一条） --------------------------

    def submit(
        self,
        *,
        kind: str,
        profile_id: str | None,
        idempotency_key: str,
        request: Mapping[str, Any],
        body: Callable[[OperationContext], None],
        validate: Callable[[], None] | None = None,
        from_profile_id: str | None = None,
        to_profile_id: str | None = None,
        target_revision: int | None = None,
        target_epoch: int | None = None,
    ) -> str:
        """预留并派发一次协调器操作，返回 `operation_id`。

        - **幂等**：同一键 + 同一请求摘要永远返回同一个 `operation_id`（含已失败/已取消
          的操作，响应丢失后的重复提交不会产生第二次副作用）；同键不同摘要抛
          `idempotency_conflict`。命中时连 `validate()` 也不再跑。
        - **串行化**：已有未完成的协调器操作就抛 `lifecycle_busy`，不排队、不等待。
        - `validate` 在预留之前、锁外执行（可能读 keyring）；`body` 在自己的守护线程里
          执行，只能经 `OperationContext` 记阶段与收尾。
        """
        key = self._validate_idempotency_key(idempotency_key)
        if kind not in RECORDED_KINDS:
            raise ValueError("invalid_operation_kind")
        digest = _request_digest(kind, request)
        replayed = self._replay(key, digest, kind=kind, profile_id=profile_id,
                                target_revision=target_revision, target_epoch=target_epoch)
        if replayed is not None:
            return replayed
        if validate is not None:
            validate()
        with self._lock:
            # 复查一次：校验期间可能有别的提交落进来。
            replayed = self._replay(key, digest, kind=kind, profile_id=profile_id,
                                    target_revision=target_revision, target_epoch=target_epoch)
            if replayed is not None:
                return replayed
            if self._quitting or self._current is not None:
                raise ConfigServiceError(CODE_LIFECYCLE_BUSY)
            operation_id = new_operation_id()
            baseline = self._generation
            # 锁内只做内存登记：操作租约、代次基线与不可变输入。
            self._current = operation_id
            self._current_generation = baseline
            retry_of = self._recent_failure(profile_id=profile_id, kind=kind)
        now = self._timestamp()
        record = OperationRecord(
            operation_id=operation_id,
            kind=kind,
            state=OP_STATE_RESERVED,
            stage=STAGE_RESERVE_OPERATION,
            profile_id=profile_id,
            from_profile_id=from_profile_id,
            to_profile_id=to_profile_id,
            target_revision=target_revision,
            target_epoch=target_epoch,
            idempotency_key=key,
            retry_of=retry_of,
            started_at=now,
            updated_at=now,
        )
        try:
            self._write_record(record)
        except ConfigServiceError:
            # 记录写不进去：本次操作立即失败，且没有任何副作用已经发生。
            with self._lock:
                if self._current == operation_id:
                    self._current = None
            raise
        with self._lock:
            self._records[operation_id] = record
            self._idempotency[key] = (operation_id, digest)
            self._trim_idempotency()
        # 校验已经过去（成功才走到这里），按总表的阶段顺序先报它，再进预留阶段。
        self._notify_stage(operation_id, STAGE_VALIDATE_TARGET)
        context = OperationContext(
            operation_id=operation_id,
            kind=kind,
            profile_id=profile_id,
            target_revision=target_revision,
            target_epoch=target_epoch,
            generation=baseline,
            _service=self,
        )
        thread = threading.Thread(
            target=self._run_operation, args=(context, body), name=THREAD_NAME, daemon=True
        )
        thread.start()
        return operation_id

    # --- 崩溃恢复 ---------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """启动时对账：把 `reserved` / `running` 的记录改成 `interrupted`。

        只对账，**不重放**：不启动 Worker、不改活动指针、不清除任何东西（故障表第 8 行）。
        摘要里如实记下当时 `launcher.json` 的活动指针，以及哪些被中断的操作已经把指针
        提交到了自己的目标档案 —— 这决定恢复后页面该提示「已切到 B」还是「仍停在 A」。
        """
        records, unreadable = self._load_records()
        try:
            active_profile_id = self._profiles.catalog().active_profile_id
        except ConfigServiceError:
            active_profile_id = None
        interrupted: list[str] = []
        committed: list[str] = []
        write_failed: list[str] = []
        updated: list[OperationRecord] = []
        for record in records:
            if record.state not in IN_FLIGHT_STATES:
                continue
            finished = replace(
                record,
                state=OP_STATE_INTERRUPTED,
                error=ERROR_CONTROLLER_RESTART,
                updated_at=self._timestamp(),
                finished_at=record.finished_at or self._timestamp(),
            )
            try:
                self._write_record(finished)
            except ConfigServiceError:
                # 记录写不进去也要如实报告；内存里仍按 interrupted 记账，不掩盖。
                write_failed.append(record.operation_id)
            updated.append(finished)
            interrupted.append(record.operation_id)
            if (
                active_profile_id is not None
                and record.to_profile_id is not None
                and record.to_profile_id == active_profile_id
            ):
                committed.append(record.operation_id)
        with self._lock:
            for record in records:
                self._records[record.operation_id] = record
            for record in updated:
                self._records[record.operation_id] = record
        return {
            "examined": len(records),
            "interrupted": tuple(interrupted),
            "committed": tuple(committed),
            "write_failed": tuple(write_failed),
            "unreadable": tuple(unreadable),
            "active_profile_id": active_profile_id,
        }

    # --- 执行体用的入口（Task 2/3 的操作体复用同一套） ----------------------

    def set_operation_stage(
        self, operation_id: str, stage: str, *, state: str | None = None
    ) -> None:
        """记下阶段并落盘（在阶段动作之前）；写盘失败抛 `record_write_failed`。"""
        with self._lock:
            record = self._records.get(operation_id)
            if record is None:
                raise ConfigServiceError(ERROR_RECORD_WRITE_FAILED)
            updated = replace(
                record,
                stage=stage,
                state=state or record.state,
                updated_at=self._timestamp(),
            )
            self._records[operation_id] = updated
        self._write_record(updated)

    def stage_scope(self, operation_id: str, stage: str, *, state: str | None = None):
        """阶段上下文：进入时 `set_operation_stage`，退出时回调阶段钩子。"""

        @contextmanager
        def _scope() -> Iterator[None]:
            self.set_operation_stage(operation_id, stage, state=state)
            try:
                yield
            finally:
                self._notify_stage(operation_id, stage)

        return _scope()

    def set_operation_details(
        self,
        operation_id: str,
        *,
        credentials: tuple[CredentialRef, ...] | None = None,
        managed_paths: tuple[str, ...] | None = None,
    ) -> None:
        """把凭据引用与受管路径写进记录（**关键副作用之前**落盘，§6.2 第 4 步）。

        两个参数缺省表示「本次不改它」；记录里只出现不透明引用与受管相对路径，
        没有秘密、没有绝对路径。写盘失败抛 `record_write_failed`：调用方据此在动
        keyring 或删数据之前停下（记录写不进去就不能继续做副作用）。
        """
        with self._lock:
            record = self._records.get(operation_id)
            if record is None:
                raise ConfigServiceError(ERROR_RECORD_WRITE_FAILED)
            updated = replace(
                record,
                credentials=record.credentials if credentials is None else credentials,
                managed_paths=record.managed_paths if managed_paths is None else managed_paths,
                updated_at=self._timestamp(),
            )
            self._records[operation_id] = updated
        self._write_record(updated)

    def await_worker_exit(self, stop_operation: Any) -> bool:
        """确认一次停止已经回收进程（判据同 A→B 事务的 `confirm_A_exited`）。"""
        return self._await_exit(stop_operation)

    def running_profile_id(self) -> str | None:
        """当前运行中的档案 id：没有 Worker 或管理器尚未上报时为 None（只读）。

        判据与状态聚合一致（`manager.status()["running_profile_id"]`），并额外要求
        进程句柄在场 —— 句柄没了就是没有 Worker 在跑，旧的运行档案 id 不作数。
        """
        if self._manager.worker is None:
            return None
        value = self._manager.status().get("running_profile_id")
        return value if isinstance(value, str) and value else None

    def dispatch_stop(self, operation_id: str) -> Any:
        """在门租约内派发一次停止，并把这次代次变化计入该操作的基线。

        **只有预留之后没有别的停止到达时才吸收自己的 `+1`**：从「记下基线」到「派发
        自己的停止」之间有落盘（fsync）与线程启动，托盘「停止」、`request_quit()` 或
        `begin_session_end()` 完全可能落在这段里；无条件吸收会把那次外部停止抹掉，
        事务随后照常提交指针并启动 B —— 与故障表第 5/6 行相反。判据放在锁内一次取齐：
        当前代次已经不等于预留基线，就说明来过了，本次不吸收（`operation_cancelled()`
        因此保持为真，事务在提交前落 `cancelled_by_stop`）。
        """
        with self._lock:
            reserved = self._current == operation_id
            baseline = self._current_generation if reserved else self._generation
            before = self._generation
            absorb = reserved and before == baseline
        ticket = self._acquire_gate(KIND_ACTIVATE)
        if ticket is None:
            raise ConfigServiceError(ERROR_LIFECYCLE_BUSY)
        try:
            operation = self.request_stop()
        finally:
            self._gate.end(ticket)
        if absorb:
            with self._lock:
                if self._current == operation_id:
                    self._current_generation = before + 1
        return operation

    def operation_cancelled(self, operation_id: str) -> bool:
        """该操作预留之后是否收到过外部 `stop` / `quit`。"""
        with self._lock:
            if self._current != operation_id:
                # 已经不是当前操作：按「已过期」处理，绝不让它继续做副作用。
                return True
            return self._generation != self._current_generation

    def finish_operation(
        self,
        operation_id: str,
        *,
        state: str,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        """落终态并释放操作租约；重复或过期的收尾直接丢弃。

        阶段钩子（`finished`）在终态发布**之前**回调完毕：观测方一旦看到终态，就知道
        阶段序列已经完整交付，不会读到少一格序列。
        """
        with self._lock:
            record = self._records.get(operation_id)
            if record is None or record.state in TERMINAL_STATES:
                return
        self._notify_stage(operation_id, STAGE_FINISHED)
        with self._lock:
            record = self._records.get(operation_id)
            if record is None or record.state in TERMINAL_STATES:
                return
            now = self._timestamp()
            updated = replace(
                record,
                state=state,
                # 成功走到终点的记录停在与总表一致的 `finished`；失败/取消保留
                # 出事时的阶段，页面据此知道停在哪一步（不在收尾时抹掉现场）。
                stage=STAGE_FINISHED if state == OP_STATE_FINISHED else record.stage,
                result=result,
                error=error,
                updated_at=now,
                finished_at=now,
            )
            self._records[operation_id] = updated
            if self._current == operation_id:
                self._current = None
                self._current_generation = self._generation
        try:
            self._write_record(updated)
        except ConfigServiceError:
            # 终态落盘失败：内存视图仍如实，下次启动的 recover() 会按未完成态对账。
            self._log("launcher.lifecycle_record_failed", status="write_failed",
                      error=ERROR_RECORD_WRITE_FAILED)
        self._prune_records()
        self._log(
            "launcher.lifecycle",
            status=updated.result or updated.error or updated.state,
            kind=updated.kind,
            error=updated.error,
        )

    # --- 内部：A→B 事务 ---------------------------------------------------

    def _validate_activation(
        self,
        profile_id: str,
        *,
        expected_epoch: int,
        expected_catalog_revision: int,
        target_revision: int | None,
        start: bool,
    ) -> None:
        """停 A 之前的同步校验：目标必须是可激活且（要启动时）就绪的档案。"""
        catalog = self._profiles.catalog()
        if catalog.active_epoch != expected_epoch:
            raise ProfileError(CODE_REVISION_CONFLICT)
        if catalog.catalog_revision != expected_catalog_revision:
            raise ProfileError(CODE_REVISION_CONFLICT)
        record = self._find_profile(profile_id)
        if record is None or record.state != PROFILE_STATE_ACTIVE:
            raise ProfileError(CODE_PROFILE_STATE_CONFLICT)
        if not start:
            return
        service = self._profiles.config_service(profile_id)
        try:
            status = service.status()
        except ConfigServiceError as exc:
            raise ConfigServiceError(CODE_TARGET_NOT_READY) from exc
        if (
            status.state != STATE_CONFIGURED
            or status.revision is None
            or status.revision != target_revision
        ):
            raise ConfigServiceError(CODE_TARGET_NOT_READY)
        if not self._profiles.expected_site_user_id(profile_id):
            raise ConfigServiceError(CODE_TARGET_NOT_READY)
        try:
            service.credentials_for(target_revision)
        except (ConfigServiceError, KeyError) as exc:
            raise ConfigServiceError(CODE_TARGET_NOT_READY) from exc

    def _run_activate(
        self,
        context: OperationContext,
        *,
        start: bool,
        expected_epoch: int,
        target_revision: int | None,
    ) -> None:
        """A→B 事务的执行体；每个阶段的故障结果都按 §5.2 的故障表确定。"""
        target = context.profile_id
        with context.stage(STAGE_RESERVE_OPERATION, state=OP_STATE_RUNNING):
            # 记录在 submit() 里已落盘（reserved）；这里只把它翻成 running。
            pass
        active = self._from_profile_id(context)
        if target is not None and active is not None and target == active:
            # 目标就是当前活动档案：没有 A 要停、没有指针要提交，退化成一次普通启动。
            if not start:
                context.finish(state=OP_STATE_FINISHED, result=RESULT_SELECTED)
                return
            if context.cancelled():
                context.finish(state=OP_STATE_FINISHED, result=RESULT_CANCELLED_BY_STOP)
                return
            self._start_target(context, target, target_revision)
            return
        with context.stage(STAGE_STOP_A):
            stop_operation = context.stop_worker()
        with context.stage(STAGE_CONFIRM_A_EXITED):
            confirmed = self._await_exit(stop_operation)
        if not confirmed:
            # 故障表第 2 行：不切指针、不启动 B，保留可诊断的失败操作。
            context.finish(state=OP_STATE_FAILED, error=ERROR_STOP_UNCONFIRMED)
            return
        if context.cancelled():
            context.finish(state=OP_STATE_FINISHED, result=RESULT_CANCELLED_BY_STOP)
            return
        commit_failed = False
        try:
            with context.stage(STAGE_COMMIT_ACTIVE_B):
                self._profiles.commit_activation(target, expected_epoch=expected_epoch)
        except ConfigServiceError:
            commit_failed = True
        if commit_failed:
            # 故障表第 3 行：A 仍是活动档案但处于停止态，B 不启动。
            context.finish(state=OP_STATE_FAILED, error=ERROR_CATALOG_WRITE_FAILED)
            return
        with context.stage(STAGE_INVALIDATE_OLD_VIEWS):
            self._publish_activated(target, expected_epoch + 1)
        if context.cancelled():
            context.finish(state=OP_STATE_FINISHED, result=RESULT_SELECTED_ONLY)
            return
        if not start:
            context.finish(state=OP_STATE_FINISHED, result=RESULT_SELECTED)
            return
        self._start_target(context, target, target_revision)

    def _start_target(
        self, context: OperationContext, target: str | None, target_revision: int | None
    ) -> None:
        """start_B：在门租约内启动目标；失败不改指针、不回退到 A（故障表第 4 行）。

        管理器「拒绝派发」（退出流程、重启在途）与「启动失败」都记 `start_failed`：
        没有 Worker 被拉起时绝不写 `started`。
        """
        with context.stage(STAGE_START_B):
            ticket = self._acquire_gate(KIND_ACTIVATE)
            if ticket is None:
                raise ConfigServiceError(ERROR_LIFECYCLE_BUSY)
            try:
                operation = self._manager.start(
                    revision=target_revision, profile_id=target
                )
            finally:
                self._gate.end(ticket)
        # 只有「真的派发出去」才写 `started`：`state="failed"` 是启动失败，结果码
        # ∈ START_REFUSED_RESULTS（`quitting` / `operation_in_progress`）是管理器
        # **拒绝派发**（返回 OP_FINISHED 而不是失败）—— 两种都没有 Worker 被拉起。
        start_failed = (
            getattr(operation, "state", None) == OP_FAILED
            or getattr(operation, "result", None) in START_REFUSED_RESULTS
        )
        context.finish(
            state=OP_STATE_FINISHED,
            result=RESULT_START_FAILED if start_failed else RESULT_STARTED,
        )

    def _from_profile_id(self, context: OperationContext) -> str | None:
        with self._lock:
            record = self._records.get(context.operation_id)
        return record.from_profile_id if record is not None else None

    def _await_exit(self, stop_operation: Any) -> bool:
        """确认 A 的进程已回收：句柄层面（worker 与 pid 都没了）且停止结果已定。

        等待在锁外、租约外进行；超时或结果不在集合内都算未确认（§5.1 第 2 条）。
        """
        stop_id = getattr(stop_operation, "operation_id", None)
        deadline = self._clock() + self._exit_confirm_timeout
        while True:
            if self._exit_confirmed(stop_id):
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(EXIT_CONFIRM_POLL_SECONDS)

    def _exit_confirmed(self, stop_id: str | None) -> bool:
        if self._manager.worker is not None:
            return False
        if self._manager.status().get("pid") is not None:
            return False
        if stop_id is None:
            return False
        operation = self._manager.operation(stop_id)
        if operation is None or operation.finished_at is None:
            return False
        return operation.result in STOP_CONFIRMED_RESULTS

    def _publish_activated(self, profile_id: str | None, epoch: int) -> None:
        """发布新上下文：N1 的测试结果按身份键自然过期，旧视图不再冒充当前事实。"""
        if self._events is None or profile_id is None:
            return
        self._events.publish(
            "launcher.profile_activated",
            profile_id=profile_id,
            revision=epoch,
            status="ok",
        )

    # --- 内部：串行化、幂等、记录 ------------------------------------------

    def _run_operation(
        self, context: OperationContext, body: Callable[[OperationContext], None]
    ) -> None:
        """执行体线程：任何逃出来的异常都落成失败记录，不留在日志里自生自灭。"""
        try:
            body(context)
        except ConfigServiceError as exc:
            code = str(exc)
            if code not in _KNOWN_ERROR_CODES:
                code = self._stage_error(context.operation_id)
            context.finish(state=OP_STATE_FAILED, error=code)
        except Exception as exc:  # noqa: BLE001 - 兜底：失败必须落成可诊断的记录
            self._log(
                "launcher.lifecycle_failed",
                status="error",
                error=type(exc).__name__,
                kind=context.kind,
            )
            context.finish(
                state=OP_STATE_FAILED, error=self._stage_error(context.operation_id)
            )

    def _stage_error(self, operation_id: str) -> str:
        """按当时的阶段给出确定错误码（错误码集合不超出总表）。

        - activate 的停机阶段未确认 → `stop_unconfirmed`；
        - remove 的凭据阶段 → `credential_backend_unavailable`（停在那里、不进入数据
          清理，重试要重新预览，§6.2 第 4 步）；
        - remove 的数据阶段 → `removal_unsafe_path`（删除被拒绝，档案留在 `deleting`）；
        - 其余（写记录、写指针、写墓碑之前的落盘）→ `catalog_write_failed`。
        """
        record = self.operation(operation_id)
        stage = record.stage if record is not None else ""
        if stage in (STAGE_RESERVE_OPERATION, STAGE_STOP_A, STAGE_CONFIRM_A_EXITED):
            return ERROR_STOP_UNCONFIRMED
        if stage == STAGE_CLEAR_CREDENTIALS:
            return ERROR_CREDENTIAL_BACKEND_UNAVAILABLE
        if stage in (STAGE_DETACH, STAGE_PURGE_DATA, STAGE_FINALIZE):
            return ERROR_REMOVAL_UNSAFE_PATH
        return ERROR_CATALOG_WRITE_FAILED

    def _validate_idempotency_key(self, key: Any) -> str:
        """幂等键形状固定：`[A-Za-z0-9_-]{8,64}`；不合格按缺失处理（API 给 422）。

        用显式的 ASCII 字符集而不是 `str.isalnum()`：后者会放行中文与全角数字，
        而键会原样写进记录文件，形状必须与总表逐字一致。
        """
        if not isinstance(key, str) or not _IDEMPOTENCY_KEY_RE.match(key):
            raise ProfileError(CODE_IDEMPOTENCY_KEY_REQUIRED)
        return key

    def _replay(
        self,
        key: str,
        digest: str,
        *,
        kind: str,
        profile_id: str | None,
        target_revision: int | None,
        target_epoch: int | None,
    ) -> str | None:
        """幂等命中：同键同摘要回同一 operation_id，同键不同摘要抛冲突。"""
        with self._lock:
            entry = self._idempotency.get(key)
            if entry is not None:
                operation_id, stored = entry
                if stored != digest:
                    raise ProfileError(CODE_IDEMPOTENCY_CONFLICT)
                return operation_id
            record = self._recent_by_key(key)
            if record is None:
                return None
            if not self._record_matches(
                record,
                kind=kind,
                profile_id=profile_id,
                target_revision=target_revision,
                target_epoch=target_epoch,
            ):
                raise ProfileError(CODE_IDEMPOTENCY_CONFLICT)
            # 跨重启的重复提交：补登记后按同一操作返回，不产生第二次副作用。
            self._idempotency[key] = (record.operation_id, digest)
            self._trim_idempotency()
            return record.operation_id

    def _recent_by_key(self, key: str) -> OperationRecord | None:
        with self._lock:
            matches = [
                record
                for record in self._records.values()
                if record.idempotency_key == key
            ]
        if not matches:
            return None
        return max(matches, key=lambda record: (record.started_at, record.operation_id))

    @staticmethod
    def _record_matches(
        record: OperationRecord,
        *,
        kind: str,
        profile_id: str | None,
        target_revision: int | None,
        target_epoch: int | None,
    ) -> bool:
        """记录的字段摘要比较（重启后内存摘要表已空，只能按记录里的字段比）。"""
        return (
            record.kind == kind
            and (record.to_profile_id or record.profile_id) == profile_id
            and record.target_revision == target_revision
            and record.target_epoch == target_epoch
        )

    def _recent_failure(self, *, profile_id: str | None, kind: str) -> str | None:
        """同一档案同一 kind 的最近失败/取消/中断记录（纯诊断的 `retry_of`）。"""
        with self._lock:
            candidates = [
                record
                for record in self._records.values()
                if record.kind == kind
                and (record.to_profile_id or record.profile_id) == profile_id
                and record.state in (OP_STATE_FAILED, OP_STATE_CANCELLED, OP_STATE_INTERRUPTED)
            ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda record: (record.started_at, record.operation_id))
        return newest.operation_id

    def _trim_idempotency(self) -> None:
        while len(self._idempotency) > MAX_OPERATION_RECORDS:
            self._idempotency.popitem(last=False)

    def _prune_records(self) -> None:
        """有界清理：只删最旧的 `finished` 记录，未完成的记录永不自动丢弃。"""
        with self._lock:
            finished = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.state == OP_STATE_FINISHED
                ),
                key=lambda record: (record.finished_at or "", record.started_at,
                                    record.operation_id),
            )
            excess = len(finished) - MAX_OPERATION_RECORDS
            if excess <= 0:
                return
            removable = finished[:excess]
            for record in removable:
                self._records.pop(record.operation_id, None)
        for record in removable:
            try:
                paths.operation_record_path(self._root, record.operation_id).unlink(
                    missing_ok=True
                )
            except (OSError, ValueError):
                # 删不掉只是留下一个已完成记录，不影响任何判定。
                pass

    def _write_record(self, record: OperationRecord) -> None:
        """同目录临时文件 + flush + fsync + `os.replace`；失败抛 `record_write_failed`。"""
        path = paths.operation_record_path(self._root, record.operation_id)
        payload = (
            json.dumps(record.to_document(), ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        directory = path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
            )
        except OSError as exc:
            raise ConfigServiceError(ERROR_RECORD_WRITE_FAILED) from exc
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
            raise ConfigServiceError(ERROR_RECORD_WRITE_FAILED) from exc
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _load_records(self) -> tuple[list[OperationRecord], list[str]]:
        """读 `operations/` 下形状合法的记录；读不出来的**只记名字**，不改现场。"""
        records: list[OperationRecord] = []
        unreadable: list[str] = []
        directory = paths.operations_dir(self._root)
        try:
            entries = sorted(directory.iterdir())
        except FileNotFoundError:
            return records, unreadable
        except OSError:
            return records, unreadable
        for entry in entries:
            if not entry.is_file():
                continue
            try:
                paths.operation_record_path(self._root, entry.stem)
            except ValueError:
                continue
            try:
                document = json.loads(entry.read_text(encoding="utf-8"))
                records.append(OperationRecord.from_document(document))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                unreadable.append(entry.name)
        with self._lock:
            for record in records:
                self._records.setdefault(record.operation_id, record)
        return records, unreadable

    # --- 内部：单次启停 ---------------------------------------------------

    def _require_launch_context(self, expected_epoch: int | None) -> str:
        """启停命令的活动上下文：必须是当前活动档案，且代次（给了就）相符。"""
        catalog = self._profiles.catalog()
        if catalog.active_profile_id is None:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY)
        if expected_epoch is not None and catalog.active_epoch != expected_epoch:
            raise ProfileError(CODE_REVISION_CONFLICT)
        with self._lock:
            if self._quitting:
                raise ConfigServiceError(CODE_QUITTING)
            if self._current is not None:
                # 切换事务在途：不接受第二个启动/重启，停止不在这一条里（§5.1 第 1 条）。
                raise ConfigServiceError(CODE_LIFECYCLE_BUSY)
        return catalog.active_profile_id

    def _saved_revision(self, profile_id: str) -> int:
        """启停要绑定的目标版本：读不到已保存配置就是「配置不可用」。"""
        try:
            status = self._profiles.config_service(profile_id).status()
        except ConfigServiceError:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY) from None
        if status.state != STATE_CONFIGURED or status.revision is None:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY)
        return status.revision

    def _dispatch_single(self, kind: str, *, profile_id: str, revision: int) -> str:
        """短命令的派发：非阻塞取门、不排队（与 §59 的启停入口同一口径）。"""
        ticket = self._gate.begin_operation(kind)
        if ticket is None:
            raise ConfigServiceError(CODE_LIFECYCLE_BUSY)
        try:
            if kind == "start":
                operation = self._manager.start(revision=revision, profile_id=profile_id)
            else:
                operation = self._manager.restart(revision=revision, profile_id=profile_id)
        finally:
            self._gate.end(ticket)
        return self._checked_operation_id(operation)

    @staticmethod
    def _checked_operation_id(operation: Any) -> str:
        """管理器操作快照 → 对外 operation_id；退出与在途冲突映射成稳定码。"""
        if getattr(operation, "result", None) == "quitting":
            raise ConfigServiceError(CODE_QUITTING)
        if getattr(operation, "state", None) == OP_FAILED:
            raise ConfigServiceError(CODE_LIFECYCLE_BUSY)
        return str(operation.operation_id)

    # --- 内部：门、时钟与日志 ---------------------------------------------

    def _acquire_gate(self, kind: str) -> Ticket | None:
        """门外有界等待：被站点测试占住时最多等到 30 秒，仍取不到返回 None。

        等待在**门外**、锁外进行，租约只覆盖后面那一小段管理器调用。
        """
        deadline = self._clock() + self._gate_wait_seconds
        while True:
            ticket = self._gate.begin_operation(kind)
            if ticket is not None:
                return ticket
            if self._clock() >= deadline:
                return None
            self._sleep(OPERATION_GATE_RETRY_SECONDS)

    def _notify_stage(self, operation_id: str, stage: str) -> None:
        if self._stage_hook is not None:
            self._stage_hook(operation_id, stage)

    def _find_profile(self, profile_id: str):
        for record in self._profiles.list_profiles():
            if record.profile_id == profile_id:
                return record
        return None

    def _timestamp(self) -> str:
        return self._wall_clock().isoformat()

    def _log(self, name: str, **fields: Any) -> None:
        if self._logger is None:
            return
        log_event(self._logger, logging.INFO, name, **fields)


_KNOWN_ERROR_CODES: frozenset[str] = frozenset(
    {
        ERROR_STOP_UNCONFIRMED,
        ERROR_CATALOG_WRITE_FAILED,
        ERROR_RESOLVE_FAILED,
        ERROR_RECORD_WRITE_FAILED,
        ERROR_LIFECYCLE_BUSY,
        ERROR_DATA_IN_USE,
        ERROR_REMOVAL_UNSAFE_PATH,
        ERROR_CREDENTIAL_BACKEND_UNAVAILABLE,
    }
)
