"""基于单个 MCP ``ClientSession`` 的 Provider 骨架；传输方式由子类提供。

stdio 与 SSE 只差「怎么拿到读写流」这一件事：会话生命周期、工具发现、调用超时、
可用性翻转、以及 ``should_run`` 的调用约定都是同一套。放在这里是为了让第二条传输
不必复制一遍——复制出来的那份迟早会和第一份漂移。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession

from ..config import McpServerConfig
from .contracts import McpCallTimeoutError, ToolDefinition


class MissingEnvironmentError(RuntimeError):
    """连接所需的环境变量缺失。

    stdio 用它表示子进程需要的变量没配；SSE 用它表示 Bearer 令牌没配。
    两者都必须在**发起连接之前**失败：缺令牌的连接只会换来一个 401，
    而 401 在日志里和「网络不通」长得一模一样。
    """

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        super().__init__("missing MCP environment variables")


class SessionMcpProvider:
    """一个 MCP session 的生命周期封装。子类只实现 ``_open_transport``。"""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        connect_timeout_seconds: float = 10.0,
        call_timeout_seconds: float = 20.0,
    ) -> None:
        self.config = config
        self._connect_timeout = connect_timeout_seconds
        self._call_timeout = call_timeout_seconds
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._available = False
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """当前 MCP session 是否可用。"""
        return self._available and self._session is not None

    async def start(self) -> None:
        """建立并初始化 session；失败时清理已创建资源后抛出。"""
        async with self._lock:
            if self.available:
                return
            if not self.config.enabled:
                raise RuntimeError("MCP server disabled")
            stack = AsyncExitStack()
            try:
                read_stream, write_stream = await self._open_transport(stack)
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        # 单位是**秒**（float），不是 timedelta：SDK 把它原样交给
                        # anyio.fail_after，而 fail_after 内部做 current_time() + delay。
                        # mcp 1.x 期望 timedelta（自己调 .total_seconds()），2.x 才改成
                        # 秒数；本项目固定 mcp>=2.2，所以这里只能传数值。
                        read_timeout_seconds=self._call_timeout,
                    )
                )
                await asyncio.wait_for(session.initialize(), self._connect_timeout)
            except BaseException:
                try:
                    await stack.aclose()
                finally:
                    self._cleanup_transport()
                raise
            self._stack = stack
            self._session = session
            self._available = True

    async def stop(self) -> None:
        """关闭 session，重复调用安全。"""
        async with self._lock:
            stack, self._stack = self._stack, None
            self._session = None
            self._available = False
            if stack is not None:
                try:
                    await stack.aclose()
                finally:
                    self._cleanup_transport()
            else:
                self._cleanup_transport()

    def diagnostics(self) -> str | None:
        """最近一次失败的可诊断细节（如子进程 stderr 的末尾）；默认没有。

        生命周期管理器在记录 ``mcp.provider_start_failed`` / ``mcp.reconnect_failed``
        时取它。传输不同，能说的话也不同：stdio 有子进程，所以有 stderr 可看；
        远程传输没有，于是返回 None 而不是编一个字段出来。
        """
        return None

    async def list_tools(self) -> tuple[ToolDefinition, ...]:
        """发现服务器工具并转换为领域类型。"""
        session = self._require_session()
        try:
            result = await asyncio.wait_for(session.list_tools(), self._call_timeout)
        except asyncio.TimeoutError as exc:
            self._available = False
            raise McpCallTimeoutError() from exc
        except Exception:
            self._available = False
            raise
        return tuple(
            ToolDefinition(
                server_name=self.config.name,
                tool_name=tool.name,
                model_name="",
                description=tool.description or "",
                input_schema=dict(_tool_input_schema(tool)),
            )
            for tool in result.tools
        )

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        should_run: Callable[[], bool] | None = None,
    ) -> object:
        """执行一个已发现的工具；异常交由 Registry 映射为稳定错误。

        ``should_run`` 由多 Key 池在轮换点之间调用；单连接的 Provider 没有轮换点，
        因此接受后直接忽略——它让池与单连接实现共用同一份 ``McpProvider`` 调用约定。
        """
        session = self._require_session()
        try:
            return await asyncio.wait_for(
                session.call_tool(tool_name, arguments), self._call_timeout
            )
        except asyncio.TimeoutError as exc:
            self._available = False
            raise McpCallTimeoutError() from exc
        except Exception:
            self._available = False
            raise

    async def _open_transport(
        self, stack: AsyncExitStack
    ) -> tuple[Any, Any]:  # pragma: no cover - 抽象钩子
        """建立传输并返回 ``(read_stream, write_stream)``；由子类实现。"""
        raise NotImplementedError

    def _cleanup_transport(self) -> None:
        """释放传输自身的资源（子进程句柄等）；默认无事可做。"""

    def _require_session(self) -> ClientSession:
        if not self.available:
            raise RuntimeError("MCP provider unavailable")
        assert self._session is not None
        return self._session


def _tool_input_schema(tool: object) -> Mapping[str, Any]:
    """读取工具的入参 schema，兼容两个大版本的字段名。

    mcp 1.x 的 ``Tool`` 字段名与线格式同为 ``inputSchema``，2.x 改成了
    ``input_schema``。这里两种都试，避免把版本差异变成运行期的 AttributeError
    ——它会让整个 Provider 变成不可用，而用户只会看到"能力暂不可用"。
    """
    for field in ("input_schema", "inputSchema"):
        value = getattr(tool, field, None)
        if isinstance(value, Mapping):
            return value
        if value is not None:
            try:
                return dict(value)
            except (TypeError, ValueError):
                continue
    return {}
