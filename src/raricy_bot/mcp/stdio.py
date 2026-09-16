"""基于官方 MCP Python SDK 的 stdio Provider。"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack
from typing import Any, TextIO

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import McpServerConfig
from ..redact import Redactor
from .session import MissingEnvironmentError, SessionMcpProvider

# 保留旧路径的导入名：调用点与测试按 `mcp.stdio` 写。
__all__ = ["MissingEnvironmentError", "StdioMcpProvider"]

_SAFE_INHERITED_ENV = ("PATH", "SYSTEMROOT", "HOME", "LANG", "TMP", "TEMP", "TMPDIR")


class StdioMcpProvider(SessionMcpProvider):
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
        super().__init__(
            config,
            connect_timeout_seconds=connect_timeout_seconds,
            call_timeout_seconds=call_timeout_seconds,
        )
        self._host_env = dict(os.environ if host_env is None else host_env)
        self._redactor = redactor
        self._stderr = stderr
        self._stderr_handle: TextIO | None = None

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

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        environment = self.resolve_environment()
        stderr = self._stderr
        if stderr is None:
            self._stderr_handle = open(os.devnull, "w", encoding="utf-8")
            stderr = self._stderr_handle
        params = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=environment,
        )
        return await stack.enter_async_context(stdio_client(params, errlog=stderr))

    def _cleanup_transport(self) -> None:
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
