"""MCP Provider 生命周期、软故障隔离和后台重连。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..config import TRANSPORT_SSE, McpConfig
from ..logging_setup import get_logger, log_event
from ..redact import Redactor
from .contracts import McpProvider, describe_error
from .pool import ExaPooledProvider
from .registry import InMemoryToolRegistry
from .sse import SseMcpProvider
from .stdio import MissingEnvironmentError, StdioMcpProvider

_logger = get_logger("mcp.runtime")


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
    一条「记录失败」的日志反过来失败。值由 ``log_event`` 过滤，非白名单字段
    会被丢弃，所以这里不必自己判断该不该写。
    """
    reader = getattr(provider, "diagnostics", None)
    if not callable(reader):
        return {}
    try:
        detail = reader()
    except Exception:
        return {}
    if not detail:
        return {}
    return {"stderr": detail}


class McpManager:
    """管理一组可选 Provider；任何单个 MCP 故障都不会抛出到主应用。"""

    def __init__(
        self,
        config: McpConfig,
        *,
        host_env: dict[str, str] | None = None,
        redactor: Redactor | None = None,
        provider_factory: Callable[..., McpProvider] = StdioMcpProvider,
        pooled_provider_factory: Callable[..., McpProvider] = ExaPooledProvider,
        adapters: Mapping[tuple[str, str], Callable[[Any, str], Any]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self._sleep = sleep
        self._stopping = False
        self._tasks: dict[str, asyncio.Task[None]] = {}
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
        for name, provider in self.providers.items():
            try:
                await provider.start()
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
                # 异常正文与子进程 stderr 的末尾一并记下。原先不记正文的顾虑是
                # 「可能含密钥」，但日志层会在写出前统一脱敏；而丢掉正文的代价
                # 是 2026-09-16 那次排查只能靠外部复刻依赖树才反推出根因。
                log_event(
                    _logger,
                    logging.WARNING,
                    "mcp.provider_start_failed",
                    server=name,
                    error=describe_error(exc),
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
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(provider.stop() for provider in self.providers.values()), return_exceptions=True)

    def ensure_reconnect(self, name: str) -> None:
        """为不可用服务器创建唯一后台重连任务。"""
        if (
            self._stopping
            or name not in self.providers
            or name in self._tasks
            or name in self._disabled_missing_environment
        ):
            return
        self._tasks[name] = asyncio.create_task(self._reconnect(name))

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
                    await provider.stop()
                    await provider.start()
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
                    # 在日志里完全一样。正文与 stderr 末尾同样要带上：重试一直
                    # 失败却不说为什么，等于把排查推回到「复刻现场」。
                    log_event(
                        _logger,
                        logging.WARNING,
                        "mcp.reconnect_failed",
                        server=name,
                        attempt=attempt,
                        delay=delay,
                        error=describe_error(exc),
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
