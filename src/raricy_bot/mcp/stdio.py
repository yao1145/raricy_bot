"""基于官方 MCP Python SDK 的 stdio Provider。"""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import McpServerConfig
from ..redact import Redactor
from .contracts import McpCallTimeoutError, ToolDefinition

_SAFE_INHERITED_ENV = ("PATH", "SYSTEMROOT", "HOME", "LANG", "TMP", "TEMP", "TMPDIR")


class MissingEnvironmentError(RuntimeError):
    """stdio 子进程所需的环境变量缺失。"""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        super().__init__("missing MCP environment variables")


class StdioMcpProvider:
    """单个 MCP stdio 子进程的生命周期封装。"""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        host_env: dict[str, str] | None = None,
        redactor: Redactor | None = None,
        connect_timeout_seconds: float = 10.0,
        call_timeout_seconds: float = 20.0,
        stderr: TextIO | None = None,
    ) -> None:
        self.config = config
        self._host_env = dict(os.environ if host_env is None else host_env)
        self._redactor = redactor
        self._connect_timeout = connect_timeout_seconds
        self._call_timeout = call_timeout_seconds
        self._stderr = stderr
        self._stderr_handle: TextIO | None = None
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._available = False
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """当前 MCP session 是否可用。"""
        return self._available and self._session is not None

    def resolve_environment(self) -> dict[str, str]:
        """生成最小子进程环境，并登记显式注入的秘密。"""
        missing = tuple(
            host_name
            for host_name in self.config.env_from.values()
            if not self._host_env.get(host_name, "").strip()
        )
        if missing:
            raise MissingEnvironmentError(tuple(dict.fromkeys(missing)))
        environment = {
            key: self._host_env[key]
            for key in _SAFE_INHERITED_ENV
            if self._host_env.get(key)
        }
        environment.update(self.config.env)
        for child_name, host_name in self.config.env_from.items():
            value = self._host_env[host_name]
            environment[child_name] = value
            if self._redactor is not None:
                self._redactor.add_secret(value)
        return environment

    async def start(self) -> None:
        """启动并初始化 stdio session；失败时清理已创建资源后抛出。"""
        async with self._lock:
            if self.available:
                return
            if not self.config.enabled:
                raise RuntimeError("MCP server disabled")
            environment = self.resolve_environment()
            stack = AsyncExitStack()
            stderr = self._stderr
            if stderr is None:
                self._stderr_handle = open(os.devnull, "w", encoding="utf-8")
                stderr = self._stderr_handle
            try:
                params = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env=environment,
                )
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(params, errlog=stderr)
                )
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=self._call_timeout),
                    )
                )
                await asyncio.wait_for(session.initialize(), self._connect_timeout)
            except BaseException:
                try:
                    await stack.aclose()
                finally:
                    self._close_stderr()
                raise
            self._stack = stack
            self._session = session
            self._available = True

    async def stop(self) -> None:
        """关闭 MCP session 和子进程，重复调用安全。"""
        async with self._lock:
            stack, self._stack = self._stack, None
            self._session = None
            self._available = False
            if stack is not None:
                try:
                    await stack.aclose()
                finally:
                    self._close_stderr()
            else:
                self._close_stderr()

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
                input_schema=dict(tool.inputSchema),
            )
            for tool in result.tools
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
        """执行一个已发现的工具；异常交由 Registry 映射为稳定错误。"""
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

    def _require_session(self) -> ClientSession:
        if not self.available:
            raise RuntimeError("MCP provider unavailable")
        assert self._session is not None
        return self._session

    def _close_stderr(self) -> None:
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
