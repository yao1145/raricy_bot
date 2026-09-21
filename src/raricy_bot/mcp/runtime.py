"""MCP Provider 生命周期、软故障隔离和后台重连。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..config import TRANSPORT_SSE, McpConfig
from ..logging_setup import get_logger, log_event, observe_task
from ..redact import Redactor, SecretRegistry
from .contracts import (
    STAGE_CLOSE,
    STAGE_CONNECT,
    McpProvider,
    describe_error,
)
from .pool import ExaPooledProvider
from .registry import InMemoryToolRegistry
from .sse import SseMcpProvider
from .stdio import MissingEnvironmentError, StdioMcpProvider

_logger = get_logger("mcp.runtime")

# 阶段停滞的判定与巡检周期（秒）。停滞只报警、不取消：可能有副作用的调用为了
# 一条日志被取消或重放，会制造重复副作用（计划 §4）。
_PHASE_STALL_SECONDS = 60.0
_PHASE_CHECK_SECONDS = 5.0


def _required_tools(config: McpConfig, server_name: str) -> tuple[str, ...]:
    """该服务器被 feature 绑定到的工具名；池用它校验每个槽位的发现结果一致。

    binding 是配置里的白名单，不是用户数据：把它交给池只为了让 schema 不一致或
    缺工具的子进程在启动阶段就被禁用，而不是等到模型调用时才失败。
    """
    return tuple(
        sorted(
            {
                binding.tool
                for feature in config.features.values()
                for binding in feature.bindings
                if binding.server == server_name
            }
        )
    )


def _provider_diagnostics_fields(provider: McpProvider) -> dict[str, object]:
    """Provider 能提供的额外诊断字段；没有可说的就返回空字典。

    读不到（替身没有该方法、或它自己抛错）时静默跳过：诊断字段永远不能让
    一条「记录失败」的日志反过来失败。值仍由 ``log_event`` 过滤，非白名单字段
    会被丢弃，所以这里不必自己判断该不该写。

    Provider 返回的是**结构化分类**（类别、模块名），不是 stderr 原文 ——
    正文一概不进日志，无论它是否"看起来已经脱敏"（计划 §3.3）。
    """
    reader = getattr(provider, "diagnostics", None)
    if not callable(reader):
        return {}
    try:
        detail = reader()
    except Exception:
        return {}
    if not isinstance(detail, Mapping):
        return {}
    return {key: value for key, value in detail.items() if isinstance(key, str)}


class _PhaseMonitor:
    """MCP 阶段的耗时观测。

    只负责**观测**：记录阶段何时开始、何时结束，并找出长时间没结束的阶段。
    它不持有取消权，也不参与重试决策。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._active: dict[int, tuple[str, str, float, bool]] = {}
        self._next_token = 0

    def begin(self, server: str, stage: str) -> int:
        """登记一个阶段的开始，返回用于 ``end()`` 的句柄。"""
        self._next_token += 1
        self._active[self._next_token] = (server, stage, self._clock(), False)
        return self._next_token

    def end(self, token: int) -> tuple[str, str, int, bool] | None:
        """结束一个阶段；返回 `(server, stage, 耗时毫秒, 是否报过停滞)`。"""
        entry = self._active.pop(token, None)
        if entry is None:
            return None
        server, stage, started, alerted = entry
        return server, stage, int((self._clock() - started) * 1000), alerted

    def stalled(self, threshold: float) -> list[tuple[str, str, int]]:
        """找出超过阈值仍未结束的阶段；每个阶段只报一次。"""
        found: list[tuple[str, str, int]] = []
        for token, (server, stage, started, alerted) in self._active.items():
            elapsed = self._clock() - started
            if alerted or elapsed < threshold:
                continue
            self._active[token] = (server, stage, started, True)
            found.append((server, stage, int(elapsed * 1000)))
        return found


class McpManager:
    """管理一组可选 Provider；任何单个 MCP 故障都不会抛出到主应用。"""

    def __init__(
        self,
        config: McpConfig,
        *,
        host_env: dict[str, str] | None = None,
        redactor: Redactor | None = None,
        registry: SecretRegistry | None = None,
        provider_factory: Callable[..., McpProvider] = StdioMcpProvider,
        pooled_provider_factory: Callable[..., McpProvider] = ExaPooledProvider,
        adapters: Mapping[tuple[str, str], Callable[[Any, str], Any]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        phase_stall_seconds: float = _PHASE_STALL_SECONDS,
        phase_check_seconds: float = _PHASE_CHECK_SECONDS,
    ) -> None:
        self.config = config
        self._sleep = sleep
        self._stopping = False
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._watch_task: asyncio.Task[None] | None = None
        self._phases = _PhaseMonitor(clock=clock)
        self._phase_stall_seconds = phase_stall_seconds
        self._phase_check_seconds = phase_check_seconds
        self._disabled_missing_environment: set[str] = set()
        self._refresh_lock = asyncio.Lock()
        # 服务器配了 account_pool 就用池，否则仍是单进程 Provider：对 Registry 与
        # 模型侧完全一样，只是幕后多了若干槽位（INTERFACES §22.4）。
        self.providers: dict[str, McpProvider] = {}
        for name, server in config.servers.items():
            if not server.enabled:
                continue
            common: dict[str, Any] = {
                "host_env": host_env,
                "redactor": redactor,
                "registry": registry,
                "connect_timeout_seconds": config.connect_timeout_seconds,
                "call_timeout_seconds": config.call_timeout_seconds,
            }
            if server.transport == TRANSPORT_SSE:
                # 远程服务器没有子进程：池（多 Key 轮换）与 `provider_factory` 都
                # 只对 stdio 有意义，这里不接受它们的注入。
                self.providers[name] = SseMcpProvider(server, **common)
            elif server.account_pool is None:
                self.providers[name] = provider_factory(server, **common)
            else:
                self.providers[name] = pooled_provider_factory(
                    server,
                    required_tools=_required_tools(config, name),
                    provider_factory=provider_factory,
                    sleep=sleep,
                    **common,
                )
        # Adapter 按 `(feature 名, 模型侧工具名)` **双键**注入（§21.2），避免 Runtime 层把
        # Exa 绑死；App 把每个 feature 的绑定映射到对应适配器的 adapt 方法，未来服务器只需
        # 增加对应适配器而不改 Provider/Registry 合同。同一个上游工具被两个 feature 绑定时
        # 两条记录并存、各自生效 —— 单键会让后写的覆盖先写的（D-110）。
        self.registry = InMemoryToolRegistry(
            self.providers,
            config.features,
            adapters=adapters,
            on_provider_failure=self.ensure_reconnect,
        )

    async def start(self) -> None:
        """最佳努力启动全部服务器；失败的服务器进入重连状态。"""
        self._stopping = False
        if not self.config.enabled:
            log_event(_logger, logging.INFO, "mcp.disabled")
            return
        self._ensure_watcher()
        for name, provider in self.providers.items():
            try:
                await self._run_phase(name, STAGE_CONNECT, provider.start())
            except MissingEnvironmentError:
                # 环境映射是进程级静态配置；缺失时只停用该服务器，
                # 不启动无意义的指数重连循环，也不把变量名/值写入日志。
                self._disabled_missing_environment.add(name)
                log_event(
                    _logger,
                    logging.WARNING,
                    "mcp.provider_disabled",
                    server=name,
                    reason="missing_env",
                )
            except Exception as exc:
                # 这里原来是完全静默的：启动失败只换来一个后台重连任务，
                # 用户侧只表现为"/search 说联网不可用"。
                #
                # 原先还带上过异常正文与子进程 stderr 末尾，理由是「排查只能靠复刻依赖树」；
                # 那个理由成立，做法不对 —— 正文来自上游，可能含密钥或用户内容，而精确
                # 字符串替换不承诺识别编码与截断边界。现在改记受控分类（计划 §3.3）。
                log_event(
                    _logger,
                    logging.WARNING,
                    "mcp.provider_start_failed",
                    server=name,
                    stage=STAGE_CONNECT,
                    **describe_error(exc),
                    **_provider_diagnostics_fields(provider),
                )
                self.ensure_reconnect(name)
            else:
                log_event(_logger, logging.INFO, "mcp.provider_started", server=name)
        await self._refresh_registry()
        # 发现阶段也可能使 session 失效（例如子进程启动后立即退出）；不要把
        # 这种故障误认为已连接，否则永远不会创建后台重连任务。
        for name, provider in self.providers.items():
            if (
                (not provider.available or name in self.registry.last_refresh_failures)
                and name not in self._disabled_missing_environment
            ):
                self.ensure_reconnect(name)

    async def stop(self) -> None:
        """先取消重连任务，再关闭所有 Provider。"""
        self._stopping = True
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        watcher, self._watch_task = self._watch_task, None
        if watcher is not None:
            watcher.cancel()
        joined = tasks if watcher is None else (*tasks, watcher)
        if joined:
            await asyncio.gather(*joined, return_exceptions=True)
        await self._run_phase_all(
            tuple(self.providers.items()), STAGE_CLOSE
        )

    def ensure_reconnect(self, name: str) -> None:
        """为不可用服务器创建唯一后台重连任务。"""
        if (
            self._stopping
            or name not in self.providers
            or name in self._tasks
            or name in self._disabled_missing_environment
        ):
            return
        task = asyncio.create_task(self._reconnect(name), name=f"mcp-reconnect-{name}")
        # 重连循环自己会记 provider_recovered / reconnect_failed；这一层兜的是
        # 「任务本身意外结束」——那时循环已经不再跑了，而外面看不出来。
        observe_task(task, f"mcp-reconnect-{name}", component="mcp.runtime")
        self._tasks[name] = task

    async def _reconnect(self, name: str) -> None:
        provider = self.providers[name]
        delay = self.config.reconnect_base_seconds
        attempt = 0
        try:
            while not self._stopping:
                await self._sleep(delay)
                if self._stopping:
                    return
                attempt += 1
                try:
                    await self._run_phase(name, STAGE_CLOSE, provider.stop())
                    await self._run_phase(name, STAGE_CONNECT, provider.start())
                    await self._refresh_registry()
                    if (
                        not provider.available
                        or name in self.registry.last_refresh_failures
                    ):
                        raise RuntimeError("MCP provider unavailable after discovery")
                    log_event(
                        _logger,
                        logging.INFO,
                        "mcp.provider_recovered",
                        server=name,
                        attempt=attempt,
                    )
                    return
                except MissingEnvironmentError:
                    self._disabled_missing_environment.add(name)
                    log_event(
                        _logger,
                        logging.WARNING,
                        "mcp.provider_disabled",
                        server=name,
                        reason="missing_env",
                    )
                    return
                except Exception as exc:
                    # 退避期间必须留痕：否则"重连一直在失败"和"压根没触发重连"
                    # 在日志里完全一样。原因以**受控分类**记录，不带上游正文。
                    log_event(
                        _logger,
                        logging.WARNING,
                        "mcp.reconnect_failed",
                        server=name,
                        attempt=attempt,
                        delay=delay,
                        **describe_error(exc),
                        **_provider_diagnostics_fields(provider),
                    )
                    delay = min(delay * 2, self.config.reconnect_max_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            self._tasks.pop(name, None)

    async def _refresh_registry(self) -> None:
        """串行更新工具快照，避免多个重连任务互相覆盖。"""
        async with self._refresh_lock:
            await self.registry.refresh()

    # --- 阶段观测 ---------------------------------------------------------

    async def _run_phase(self, server: str, stage: str, awaitable: Awaitable[Any]) -> Any:
        """跑一个阶段并记录耗时；阶段异常照常向上抛。"""
        token = self._phases.begin(server, stage)
        try:
            return await awaitable
        finally:
            self._report_phase(token)

    async def _run_phase_all(
        self, pairs: Any, stage: str
    ) -> None:
        """并发跑一组阶段，单个失败不影响其余（关闭路径用）。"""
        async def one(name: str, provider: McpProvider) -> None:
            try:
                await self._run_phase(name, stage, provider.stop())
            except asyncio.CancelledError:
                raise
            except Exception:
                return

        await asyncio.gather(*(one(name, provider) for name, provider in pairs), return_exceptions=True)

    def _report_phase(self, token: int) -> None:
        """阶段结束时只记录**曾经停滞过**的那些。

        正常完成的阶段（毫秒级工具发现、正常关停）在这里保持安静：它们的成功
        已经由 `mcp.provider_started` / `mcp.tool_done` 表达，逐阶段各记一行只会
        把日志淹掉。
        """
        entry = self._phases.end(token)
        if entry is None:
            return
        server, stage, duration_ms, alerted = entry
        if not alerted:
            return
        log_event(
            _logger,
            logging.INFO,
            "mcp.phase_finished",
            server=server,
            stage=stage,
            duration_ms=duration_ms,
        )

    def _ensure_watcher(self) -> None:
        """起一个巡检任务，对长时间未结束的阶段报警一次。"""
        if self._watch_task is not None and not self._watch_task.done():
            return
        self._watch_task = asyncio.create_task(self._watch_phases(), name="mcp-phase-watch")

    async def _watch_phases(self) -> None:
        while True:
            await self._sleep(self._phase_check_seconds)
            for server, stage, duration_ms in self._phases.stalled(self._phase_stall_seconds):
                # 只报警，不取消：见 _PHASE_STALL_SECONDS 的说明。
                log_event(
                    _logger,
                    logging.WARNING,
                    "mcp.phase_stalled",
                    server=server,
                    stage=stage,
                    duration_ms=duration_ms,
                )
