"""Exa 授权密钥池：把多个 stdio Provider 包装成一个逻辑 Provider。

对 ``InMemoryToolRegistry`` 而言池仍然只是一个 ``exa``：模型侧工具名、feature 绑定和
``SearchLimiter`` 的全局串行都不变（INTERFACES §22，裁决 D-36 / D-37 / D-40 / D-46）。

三条贯穿全文件的硬约束：

- 池**只**以进程内序号标识槽位，永远不把 Key 值、宿主环境变量名、查询、URL、摘要或
  上游错误正文写进日志、异常消息或返回值；
- 状态只在内存里（D-37），重启后从零重新探测，不落 SQLite；
- 无法可靠分类的上游错误一律安全失败（D-46）：不轮换、不冷却，把原始结果交回 Registry。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..config import McpServerConfig
from ..logging_setup import get_logger, log_event
from ..redact import Redactor
from .contracts import (
    McpCallCancelled,
    McpCallTimeoutError,
    McpProvider,
    McpProviderUnavailable,
    ToolDefinition,
)
from .stdio import MissingEnvironmentError, StdioMcpProvider

_logger = get_logger("mcp.pool")

# 槽位状态（INTERFACES §22.2）。字符串是合同的一部分，测试与诊断都按它断言。
SLOT_READY = "ready"
SLOT_COOLDOWN = "cooldown"
SLOT_EXHAUSTED = "exhausted"
SLOT_INVALID = "invalid"
SLOT_DISABLED = "disabled"
# 内部态：已配好 Key、但还没 start() 的槽位。它**不是**契约里的五个状态之一，
# 只用于区分「稍后会启动」与「永远不会启动（disabled）」——后者不参与任何轮换或探测。
SLOT_PENDING = "pending"

# 上游错误分类结果。只有前四个会触发轮换；request / unknown_upstream 不轮换。
KIND_OK = "ok"
KIND_RATE_LIMIT = "rate_limit"
KIND_QUOTA = "quota"
KIND_INVALID_KEY = "invalid_key"
KIND_TRANSIENT = "transient"
KIND_REQUEST = "request"
KIND_UNKNOWN = "unknown_upstream"

# 稳定原因串：进日志、可用于诊断，但绝不包含环境变量名或上游正文。
REASON_MISSING_ENV = "missing_env"
REASON_DUPLICATE_SECRET = "duplicate_secret"
REASON_SCHEMA_MISMATCH = "schema_mismatch"
REASON_REQUIRED_TOOLS = "required_tools_missing"
REASON_START_FAILED = "start_failed"
REASON_STARTUP_TIMEOUT = "startup_timeout"
REASON_LIST_FAILED = "list_tools_failed"
REASON_TIMEOUT = "timeout"
REASON_PROVIDER_ERROR = "provider_error"
REASON_RECOVER_FAILED = "recover_failed"

# 固定词表：只在 isError 内容上做大小写无关的整词匹配（§22.2 / D-46）。
_INVALID_KEY_WORDS = ("INVALID_API_KEY",)
_QUOTA_WORDS = ("NO_MORE_CREDITS", "API_KEY_BUDGET_EXCEEDED", "TEAM_BUDGET_EXCEEDED")
_RATE_LIMIT_WORDS = ("RATE_LIMIT_EXCEEDED", "TOO_MANY_REQUESTS")
_REQUEST_WORDS = ("BAD_REQUEST", "INVALID_ARGUMENT", "UNPROCESSABLE")

_INVALID_KEY_STATUS = frozenset({401})
_QUOTA_STATUS = frozenset({402})
_RATE_LIMIT_STATUS = frozenset({429})
_REQUEST_STATUS = frozenset({400, 422})
_TRANSIENT_STATUS = frozenset({500, 502, 503, 504})

_STATUS_PATTERN = re.compile(r"(?<![0-9])(401|402|429|400|422|500|502|503|504)(?![0-9])")

# 结构化字段的候选名：dict 大小写不敏感，对象属性按这几个拼写试。
_STRUCTURED_KEYS = ("status", "code", "tag")
_STRUCTURED_CONTAINERS = ("structuredContent", "structured_content", "error")


@dataclass
class _Slot:
    """一个槽位的内存态；``index`` 是进程内序号，不写环境变量名与 Key 指纹。"""

    index: int
    host_env_name: str
    provider: McpProvider | None = None
    state: str = SLOT_PENDING
    reason: str | None = None
    # 冷却/配额恢复的到期时刻，基准是注入的 clock。
    wake_at: float | None = None
    task: asyncio.Task[None] | None = None


def _field(source: Any, name: str) -> Any:
    """大小写无关地读 dict 键或对象属性；读不到返回 None。"""
    if isinstance(source, Mapping):
        target = name.casefold()
        for key, value in source.items():
            if isinstance(key, str) and key.casefold() == target:
                return value
        return None
    for candidate in (name, name.lower(), name.upper()):
        if hasattr(source, candidate):
            value = getattr(source, candidate)
            if value is not None:
                return value
    return None


def _is_error_result(result: Any) -> bool:
    """判断 MCP 结果是否 isError（兼容 isError / is_error 两种拼写）。"""
    for key in ("isError", "is_error"):
        value = _field(result, key)
        if value is not None:
            return bool(value)
    return False


def _kind_from_status(value: Any) -> str | None:
    """把一个结构化字段值（HTTP 状态码或稳定 tag）映射成 KIND；认不出返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _kind_from_number(value)
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "ignore") if isinstance(value, bytes) else value
        token = text.strip()
        if not token:
            return None
        if token.isdigit():
            return _kind_from_number(int(token))
        return _kind_from_word(token.upper())
    return None


def _kind_from_number(number: int) -> str | None:
    if number in _INVALID_KEY_STATUS:
        return KIND_INVALID_KEY
    if number in _QUOTA_STATUS:
        return KIND_QUOTA
    if number in _RATE_LIMIT_STATUS:
        return KIND_RATE_LIMIT
    if number in _REQUEST_STATUS:
        return KIND_REQUEST
    if number in _TRANSIENT_STATUS:
        return KIND_TRANSIENT
    return None


def _kind_from_word(word: str) -> str | None:
    if word in _INVALID_KEY_WORDS:
        return KIND_INVALID_KEY
    if word in _QUOTA_WORDS:
        return KIND_QUOTA
    if word in _RATE_LIMIT_WORDS:
        return KIND_RATE_LIMIT
    if word in _REQUEST_WORDS:
        return KIND_REQUEST
    return None


def _structured_values(result: Any) -> list[Any]:
    """收集结果里的结构化状态字段值，不含任何自由正文。"""
    values: list[Any] = []

    def collect(source: Any) -> None:
        if source is None:
            return
        for key in _STRUCTURED_KEYS:
            value = _field(source, key)
            if value is not None:
                values.append(value)

    collect(result)
    for container in _STRUCTURED_CONTAINERS:
        candidate = _field(result, container)
        if isinstance(candidate, Mapping):
            collect(candidate)
        elif isinstance(candidate, str):
            # 错误字段本身就是 tag 的形态（例如 error="INVALID_API_KEY"）。整串匹配，
            # 不做子串猜测，避免把一句普通正文当成额度耗尽（D-46）。
            values.append(candidate)
    content = _field(result, "content")
    if isinstance(content, (list, tuple)):
        for item in content:
            if isinstance(item, (Mapping,)) or not isinstance(item, (str, bytes)):
                collect(item)
    return values


def _error_text(result: Any) -> str:
    """只取 isError 结果的 text 块；不读任何其它自由正文。"""
    content = _field(result, "content")
    pieces: list[str] = []
    if isinstance(content, str):
        pieces.append(content)
    elif isinstance(content, (list, tuple)):
        for item in content:
            text = _field(item, "text")
            if isinstance(text, str):
                pieces.append(text)
    return "\n".join(pieces)


def classify_exa_error(result: Any) -> str:
    """把一次 MCP 调用结果分类成 KIND_* 之一（INTERFACES §22.2）。

    优先级：结构化 ``status`` / ``code`` / ``tag`` → ``isError`` 文本上的固定词与
    状态码整词匹配 → 其余一律 ``unknown_upstream``。错误正文既不进日志也不进模型上下文。
    """
    if not _is_error_result(result):
        return KIND_OK
    for value in _structured_values(result):
        kind = _kind_from_status(value)
        if kind is not None:
            return kind
    text = _error_text(result)
    if text:
        upper = text.upper()
        for word in _INVALID_KEY_WORDS:
            if word in upper:
                return KIND_INVALID_KEY
        for word in _QUOTA_WORDS:
            if word in upper:
                return KIND_QUOTA
        for word in _RATE_LIMIT_WORDS:
            if word in upper:
                return KIND_RATE_LIMIT
        for word in _REQUEST_WORDS:
            if word in upper:
                return KIND_REQUEST
        match = _STATUS_PATTERN.search(text)
        if match is not None:
            kind = _kind_from_number(int(match.group(1)))
            if kind is not None:
                return kind
    return KIND_UNKNOWN


def _schema_fingerprint(definitions: tuple[ToolDefinition, ...]) -> str:
    """工具定义（名字 + 入参 schema）的规范化指纹，用于跨槽位一致性比对。

    先对每一项各自归一化，再按 `(名字, schema)` 排序后拼接：同一组工具无论上游以
    什么顺序报出来，指纹都相同。否则一次无关的列表换序会让第二个槽位被误判为
    `schema_mismatch` 并静默退化成单 Key。
    """
    entries = sorted(
        (
            item.tool_name,
            json.dumps(item.input_schema, sort_keys=True, default=str, ensure_ascii=True),
        )
        for item in definitions
    )
    return json.dumps(entries, ensure_ascii=True)


async def _quiet_stop(provider: McpProvider) -> None:
    """关闭一个 Provider，失败只吞掉——stop 路径不允许被单个槽位打断。"""
    try:
        await provider.stop()
    except asyncio.CancelledError:
        raise
    except Exception:
        return


class ExaPooledProvider:
    """多个已授权 Exa Key 的池，对外是**一个** McpProvider。"""

    # 池内槽位的冷却与恢复由池自己的后台恢复任务负责；外层若按「整个逻辑 Provider
    # 不可用」重连（stop() + start()），会把还在冷却里的槽位一并拉起，抹掉冷却语义
    # （例如刚因 429 冷却 60 秒、刚因额度耗尽冷却 6 小时的槽位会被立刻再打一次）。
    # Registry 执行路径见到这个标记就不再通知 McpManager 重连（INTERFACES §22.3）。
    manages_own_recovery = True

    def __init__(
        self,
        config: McpServerConfig,
        *,
        host_env: dict[str, str] | None = None,
        redactor: Redactor | None = None,
        connect_timeout_seconds: float = 10.0,
        call_timeout_seconds: float = 20.0,
        required_tools: tuple[str, ...] = (),
        provider_factory: Callable[..., McpProvider] = StdioMcpProvider,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        startup_timeout_seconds: float | None = None,
    ) -> None:
        pool = config.account_pool
        if pool is None:
            # mcp/runtime.py 保证不会发生：没有 account_pool 的服务器走单 Key 老路径。
            raise ValueError("ExaPooledProvider requires account_pool configuration")
        self.config = config
        self._pool = pool
        # 宿主环境只在构造时读一次；宿主里没有的变量在下面变成 disabled 槽位。
        self._host_env = dict(os.environ if host_env is None else host_env)
        self._redactor = redactor
        self._connect_timeout = connect_timeout_seconds
        self._call_timeout = call_timeout_seconds
        self._required_tools = tuple(required_tools)
        self._provider_factory = provider_factory
        self._clock = clock
        self._sleep = sleep
        self._startup_timeout = (
            connect_timeout_seconds * 2
            if startup_timeout_seconds is None
            else startup_timeout_seconds
        )
        self._select_lock = asyncio.Lock()
        self._stopping = False
        self._cursor = 0
        # 停用槽位的回收任务：取消其恢复任务并关闭子进程。集中跟踪以便 stop() 收尾。
        self._retire_tasks: set[asyncio.Task[None]] = set()
        self._slots = self._build_slots()

    # ---- 生命周期 -------------------------------------------------------

    @property
    def available(self) -> bool:
        """至少一个槽位 ready。"""
        return any(slot.state == SLOT_READY for slot in self._slots)

    @property
    def slot_states(self) -> tuple[str, ...]:
        """诊断与测试用；顺序即槽位序号。"""
        return tuple(slot.state for slot in self._slots)

    async def start(self) -> None:
        """并发启动全部可用槽位；全不可用时抛 MissingEnvironmentError。"""
        self._stopping = False
        # stop() 会把健康槽位也标成 disabled。重启时只把它们放回可启动态；
        # 因 schema 不一致、Key 重复或环境缺失而停用的槽位（reason 非空/无 provider）
        # 不在其中——那些必须靠修正配置或环境，不能靠重启复活。
        for slot in self._slots:
            if slot.state == SLOT_DISABLED and slot.provider is not None and slot.reason is None:
                slot.state = SLOT_PENDING
        startable = [
            slot
            for slot in self._slots
            if slot.provider is not None and slot.state != SLOT_DISABLED
        ]
        if not startable:
            missing = tuple(
                dict.fromkeys(
                    slot.host_env_name
                    for slot in self._slots
                    if slot.reason == REASON_MISSING_ENV
                )
            )
            raise MissingEnvironmentError(missing)
        loop = asyncio.get_running_loop()
        tasks = [loop.create_task(self._start_slot(slot)) for slot in startable]
        # asyncio.wait 不会在超时时取消任务，也不吞并异常：单槽失败只标该槽位。
        _done, pending = await asyncio.wait(tasks, timeout=self._startup_timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)
        for slot in startable:
            if slot.state == SLOT_READY:
                continue
            self._enter_cooldown(slot, REASON_STARTUP_TIMEOUT, self._pool.transient_cooldown_seconds)
        ready = sum(1 for slot in self._slots if slot.state == SLOT_READY)
        log_event(
            _logger,
            logging.INFO,
            "mcp.pool_started",
            server=self.config.name,
            count=ready,
            status=SLOT_READY if ready else "unavailable",
        )

    async def stop(self) -> None:
        """先取消全部恢复任务，再并发关闭子进程；重复调用安全。"""
        self._stopping = True
        tasks = [slot.task for slot in self._slots if slot.task is not None]
        for slot in self._slots:
            slot.task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        # 等停用槽位的回收任务收尾，避免它们的 stop() 与本方法的收尾交错。
        retire = [task for task in self._retire_tasks if not task.done()]
        if retire:
            await asyncio.gather(*retire, return_exceptions=True)
        providers = [slot.provider for slot in self._slots if slot.provider is not None]
        if providers:
            await asyncio.gather(*(_quiet_stop(provider) for provider in providers))
        for slot in self._slots:
            if slot.state != SLOT_DISABLED:
                slot.state = SLOT_DISABLED
                slot.reason = None
                slot.wake_at = None

    # ---- 工具发现 -------------------------------------------------------

    async def list_tools(self) -> tuple[ToolDefinition, ...]:
        """向 ready 槽位取定义；schema 指纹或 required_tools 不一致的槽位被停用。"""
        reference: tuple[ToolDefinition, ...] = ()
        reference_fingerprint: str | None = None
        have_reference = False
        for slot in list(self._slots):
            if slot.state != SLOT_READY or slot.provider is None:
                continue
            try:
                definitions = tuple(await slot.provider.list_tools())
            except asyncio.CancelledError:
                raise
            except Exception:
                self._enter_cooldown(
                    slot, REASON_LIST_FAILED, self._pool.transient_cooldown_seconds
                )
                continue
            names = {item.tool_name for item in definitions}
            if any(tool not in names for tool in self._required_tools):
                self._disable(slot, REASON_REQUIRED_TOOLS)
                continue
            fingerprint = _schema_fingerprint(definitions)
            if not have_reference:
                have_reference = True
                reference = definitions
                reference_fingerprint = fingerprint
                continue
            if fingerprint != reference_fingerprint:
                self._disable(slot, REASON_SCHEMA_MISMATCH)
        return reference

    # ---- 调用与有界故障转移 ---------------------------------------------

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        should_run: Callable[[], bool] | None = None,
    ) -> Any:
        """按轮询游标做有界故障转移；每个槽位在一次逻辑调用里最多试一次。"""
        tried: set[int] = set()
        last_result: Any = None
        last_error: BaseException | None = None
        while True:
            if should_run is not None and not should_run():
                raise McpCallCancelled()
            async with self._select_lock:
                slot = self._next_ready(tried)
                if slot is None:
                    break
                tried.add(slot.index)
                # 「选择 + 推进游标」必须原子，否则并发调用会挑中同一个槽位。
                self._cursor = (slot.index + 1) % len(self._slots)
            provider = slot.provider
            if provider is None:  # pragma: no cover - 构造期已保证 ready 槽位有 provider
                continue
            try:
                result = await provider.call_tool(tool_name, arguments)
            except asyncio.CancelledError:
                raise
            except McpCallTimeoutError as exc:
                last_result, last_error = None, exc
                self._enter_cooldown(slot, REASON_TIMEOUT, self._pool.transient_cooldown_seconds)
                continue
            except Exception as exc:
                # 子进程退出、连接失败等传输层错误始终可分类，因此始终参与轮换（D-46）。
                last_result, last_error = None, exc
                self._enter_cooldown(
                    slot, REASON_PROVIDER_ERROR, self._pool.transient_cooldown_seconds
                )
                continue
            kind = classify_exa_error(result)
            if kind == KIND_OK:
                slot.state = SLOT_READY
                slot.reason = None
                return result
            if kind == KIND_INVALID_KEY:
                self._mark_state(slot, SLOT_INVALID, "invalid_key")
            elif kind == KIND_QUOTA:
                self._mark_state(
                    slot, SLOT_EXHAUSTED, "quota", self._pool.quota_cooldown_seconds
                )
            elif kind == KIND_RATE_LIMIT:
                self._enter_cooldown(slot, "rate_limit", self._pool.rate_limit_cooldown_seconds)
            elif kind == KIND_TRANSIENT:
                self._enter_cooldown(slot, "transient", self._pool.transient_cooldown_seconds)
            else:
                # request / unknown_upstream：不轮换，把原始结果原样交回 Registry（D-46）。
                return result
            last_result, last_error = result, None
        # 一个 ready 槽位都没有：一次尝试都没发生，这不是超时。用不带池结构的稳定
        # 错误告诉 Registry 当前不可用，让它映射成 tool_unavailable（F1）。
        if not tried:
            log_event(
                _logger,
                logging.DEBUG,
                "mcp.pool_call_unavailable",
                server=self.config.name,
            )
            raise McpProviderUnavailable()
        # 全部槽位失败：只暴露稳定的错误类型，绝不带槽位数、槽位序号或上游原文。
        if last_error is not None:
            if isinstance(last_error, McpCallTimeoutError):
                raise McpCallTimeoutError()
            log_event(
                _logger,
                logging.DEBUG,
                "mcp.pool_call_failed",
                server=self.config.name,
                error=type(last_error).__name__,
            )
            raise RuntimeError("exa pool call failed")
        if last_result is not None:
            return last_result
        raise McpCallTimeoutError()

    # ---- 内部：槽位构造 --------------------------------------------------

    def _build_slots(self) -> list[_Slot]:
        """按 host_envs 顺序造槽位；重复 Key 与缺失环境在这里变成 disabled。"""
        slots: list[_Slot] = []
        seen: set[str] = set()
        for index, host_name in enumerate(self._pool.host_envs):
            slot = _Slot(index=index, host_env_name=host_name)
            value = str(self._host_env.get(host_name, "") or "").strip()
            if not value:
                slot.state = SLOT_DISABLED
                slot.reason = REASON_MISSING_ENV
            elif value in seen:
                # 只比较内存里的原值，比较结果只形成稳定原因串（设计 §6.2）。
                slot.state = SLOT_DISABLED
                slot.reason = REASON_DUPLICATE_SECRET
            else:
                seen.add(value)
                try:
                    slot.provider = self._make_provider(host_name, value)
                except MissingEnvironmentError:
                    slot.state = SLOT_DISABLED
                    slot.reason = REASON_MISSING_ENV
                except Exception:
                    slot.state = SLOT_DISABLED
                    slot.reason = REASON_START_FAILED
            slots.append(slot)
        return slots

    def _make_provider(self, host_name: str, value: str) -> McpProvider:
        """复用 StdioMcpProvider 的 env_from 注入与最小环境，换掉它读的宿主变量名。"""
        child_config = replace(self.config, env_from={self._pool.child_env: host_name})
        provider = self._provider_factory(
            child_config,
            host_env=self._host_env,
            redactor=self._redactor,
            connect_timeout_seconds=self._connect_timeout,
            call_timeout_seconds=self._call_timeout,
        )
        resolver = getattr(provider, "resolve_environment", None)
        if callable(resolver):
            # stdio Provider 在这里登记密钥；不读它的返回值，也就不会把明文带出这个作用域。
            resolver()
        if self._redactor is not None:
            # 兜底：替身没有 resolve_environment 时也要保证「任何子进程启动前已脱敏」。
            self._redactor.add_secret(value)
        return provider

    # ---- 内部：状态迁移 --------------------------------------------------

    def _next_ready(self, tried: set[int]) -> _Slot | None:
        """从游标处开始找下一个尚未尝试过的 ready 槽位；必须持 _select_lock 调用。"""
        count = len(self._slots)
        for offset in range(count):
            index = (self._cursor + offset) % count
            slot = self._slots[index]
            if slot.state == SLOT_READY and index not in tried:
                return slot
        return None

    def _enter_cooldown(self, slot: _Slot, reason: str, seconds: float) -> None:
        """进入 cooldown 并排队后台恢复。"""
        self._mark_state(slot, SLOT_COOLDOWN, reason, seconds)

    def _mark_state(
        self, slot: _Slot, state: str, reason: str, cooldown_seconds: float | None = None
    ) -> None:
        if slot.state == SLOT_DISABLED:
            return
        slot.state = state
        slot.reason = reason
        slot.wake_at = None if cooldown_seconds is None else self._clock() + cooldown_seconds
        log_event(
            _logger,
            logging.DEBUG,
            "mcp.pool_slot_state",
            server=self.config.name,
            slot=slot.index,
            status=state,
            reason=reason,
        )
        if cooldown_seconds is not None:
            self._schedule_recovery(slot)

    def _disable(self, slot: _Slot, reason: str) -> None:
        """停用一个槽位：状态转 disabled，并回收它的恢复任务与子进程。

        取消恢复任务与关闭子进程都交给一个独立小任务完成，绝不在这里同步取消当前
        任务——`_disable` 可能正跑在恢复任务自己的栈上（`_try_restart`），那样等于
        让任务取消自己，只能靠 `finally` 兜底。独立任务还会顺手 `provider.stop()`：
        槽位转 disabled 后它的子进程与 FD 必须立刻回收，而不是等整池 `stop()`。
        """
        if slot.state == SLOT_DISABLED:
            return
        slot.state = SLOT_DISABLED
        slot.reason = reason
        slot.wake_at = None
        task, slot.task = slot.task, None
        log_event(
            _logger,
            logging.WARNING,
            "mcp.pool_slot_disabled",
            server=self.config.name,
            slot=slot.index,
            reason=reason,
        )
        self._schedule_retire(slot, task)

    def _schedule_retire(
        self, slot: _Slot, task: asyncio.Task[None] | None
    ) -> None:
        """排一个独立的回收任务；没有事件循环时静默跳过（构造期不可能走到这里）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 所有调用点都在协程里
            return
        retire = loop.create_task(self._retire_slot(slot, task))
        self._retire_tasks.add(retire)
        retire.add_done_callback(self._retire_tasks.discard)

    async def _retire_slot(
        self, slot: _Slot, task: asyncio.Task[None] | None
    ) -> None:
        """回收一个已转 disabled 的槽位：先等恢复任务结束，再关闭子进程。"""
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # 被取消的恢复任务本身：这正是期望结果，不是本任务被取消。
                pass
            except Exception:
                # 恢复任务自身吞掉业务异常，这里只做兜底。
                pass
        if slot.provider is not None:
            await _quiet_stop(slot.provider)

    # ---- 内部：后台恢复 --------------------------------------------------

    def _schedule_recovery(self, slot: _Slot) -> None:
        if self._stopping:
            return
        if slot.task is not None and not slot.task.done():
            return
        slot.task = asyncio.get_running_loop().create_task(self._recover(slot))

    async def _start_slot(self, slot: _Slot) -> None:
        provider = slot.provider
        assert provider is not None
        try:
            await provider.start()
        except asyncio.CancelledError:
            await _quiet_stop(provider)
            raise
        except MissingEnvironmentError:
            self._disable(slot, REASON_MISSING_ENV)
            return
        except Exception:
            self._enter_cooldown(slot, REASON_START_FAILED, self._pool.transient_cooldown_seconds)
            return
        slot.state = SLOT_READY
        slot.reason = None
        slot.wake_at = None

    async def _recover(self, slot: _Slot) -> None:
        """等待冷却到期后重启并探测一个槽位；失败按 transient 冷却重新排队。"""
        try:
            while not self._stopping:
                wake_at = slot.wake_at if slot.wake_at is not None else 0.0
                remaining = wake_at - self._clock()
                if remaining > 0:
                    await self._sleep(remaining)
                    continue
                if await self._try_restart(slot):
                    return
                slot.state = SLOT_COOLDOWN
                slot.reason = REASON_RECOVER_FAILED
                slot.wake_at = self._clock() + self._pool.transient_cooldown_seconds
                log_event(
                    _logger,
                    logging.INFO,
                    "mcp.pool_slot_recover_wait",
                    server=self.config.name,
                    slot=slot.index,
                    delay=self._pool.transient_cooldown_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 恢复任务自身绝不向外抛：未取回的异常会在 stop/退出时变成噪音。
            log_event(
                _logger,
                logging.WARNING,
                "mcp.pool_recover_failed",
                server=self.config.name,
                error=type(exc).__name__,
            )
        finally:
            if slot.task is asyncio.current_task():
                slot.task = None

    async def _try_restart(self, slot: _Slot) -> bool:
        provider = slot.provider
        if provider is None:
            return False
        try:
            await _quiet_stop(provider)
            await provider.start()
        except asyncio.CancelledError:
            await _quiet_stop(provider)
            raise
        except MissingEnvironmentError:
            self._disable(slot, REASON_MISSING_ENV)
            return True
        except Exception as exc:
            log_event(
                _logger,
                logging.DEBUG,
                "mcp.pool_restart_failed",
                server=self.config.name,
                slot=slot.index,
                error=type(exc).__name__,
            )
            return False
        slot.state = SLOT_READY
        slot.reason = None
        slot.wake_at = None
        log_event(
            _logger,
            logging.INFO,
            "mcp.pool_slot_recovered",
            server=self.config.name,
            slot=slot.index,
            status=SLOT_READY,
        )
        return True
