"""远程 MCP-over-SSE Provider（MCP 2024-11-05 的 HTTP+SSE 传输）。

知乎开放平台只提供远程 MCP，没有可固定的 stdio 发行包，所以这条传输由本仓库自己接。
协议本身不手写：SDK 的 ``mcp.client.sse.sse_client`` 实现的就是这套
「GET 拿 endpoint 事件 → 向该会话地址 POST」的传输，与 stdio 共用同一个
``ClientSession`` 生命周期（见 ``session.py``）。

与 stdio 的两处实质差别：

1. **没有子进程**，因此 `env_from` / `env` / `args` 都不适用；凭证是宿主环境变量里的
   一个 Bearer 令牌，只在构造连接头时读取，并立刻登记进 `Redactor`。
2. **连接是长连接**，读侧超时不能沿用普通请求超时——站点 SSE 的既有教训是那样会被
   安静期掐断。``stream_read_timeout_seconds`` 单独配置，默认 300 秒。
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack
from typing import Any

from mcp.client.sse import sse_client

from ..config import McpServerConfig
from ..redact import Redactor
from .session import MissingEnvironmentError, SessionMcpProvider


class SseMcpProvider(SessionMcpProvider):
    """单个远程 MCP-over-SSE 服务器的生命周期封装。"""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        host_env: dict[str, str] | None = None,
        redactor: Redactor | None = None,
        connect_timeout_seconds: float = 10.0,
        call_timeout_seconds: float = 20.0,
    ) -> None:
        super().__init__(
            config,
            connect_timeout_seconds=connect_timeout_seconds,
            call_timeout_seconds=call_timeout_seconds,
        )
        # ``None`` 表示「用本进程的环境」，与 stdio / pool 一致。这里原先是
        # ``dict(host_env or {})``，把 ``None`` 也当成了空环境 —— 而生产装配
        # （app.py 构造 McpManager）根本不传 host_env，于是 stdio 服务器照常读
        # ``os.environ`` 启动，只有 SSE 的 zhihu 永远判为缺令牌、被静默停用
        # （2026-09-16）。空字典仍然是「没有环境」，只有 None 才回落到 os.environ。
        self._host_env = dict(os.environ if host_env is None else host_env)
        self._redactor = redactor

    def resolve_headers(self) -> dict[str, str]:
        """把宿主环境里的 Bearer 令牌变成连接头，并登记脱敏。

        缺令牌必须在连接**之前**失败：一个空的 Authorization 头换来的 401，
        在日志里和「网络不通」长得一模一样，而两者该有的处置完全不同。
        """
        name = self.config.bearer_env
        value = self._host_env.get(name, "").strip()
        if not value:
            raise MissingEnvironmentError((name,))
        if self._redactor is not None:
            # 注册原值即可：Redactor 做子串替换，`Bearer <token>` 整段会被一起抹掉。
            self._redactor.add_secret(value)
        return {"Authorization": f"Bearer {value}"}

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        return await stack.enter_async_context(
            sse_client(
                self.config.url,
                headers=self.resolve_headers(),
                # 建连与「等到 endpoint 事件」的超时。
                timeout=self._connect_timeout,
                # 长连接的读侧超时：安静期比它长就重连，绝不能用调用超时。
                sse_read_timeout=self.config.stream_read_timeout_seconds,
            )
        )
