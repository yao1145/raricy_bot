"""内建运维端点：`/livez` 与 `/readyz`。

只暴露两个布尔探针，响应体是固定短文本，**不包含**任何内部状态细节
（频道、队列长度、错误信息等一律不出现在响应里）。监听地址与端口来自
`config.ops`（见 D-10）；Docker Compose 只 `expose` 不发布到宿主。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiohttp import web

from .logging_setup import get_logger, log_event

_logger = get_logger("ops")


class OpsServer:
    """基于 aiohttp 的运维 HTTP 服务；`stop()` 可重复调用。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        livez: Callable[[], bool],
        readyz: Callable[[], bool],
    ) -> None:
        self._host = host
        self._port = port
        self._livez = livez
        self._readyz = readyz
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """启动监听；端口被占用时异常向上抛出，由调用方决定是否致命。"""
        if self._runner is not None:
            return  # 幂等：已启动则不再重复绑定
        app = web.Application()
        app.router.add_get("/livez", self._handle_livez)
        app.router.add_get("/readyz", self._handle_readyz)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self._host, self._port)
            await site.start()
        except BaseException:
            # 绑定失败（如端口被占用）时回滚已经建立的 runner，避免资源泄漏。
            await runner.cleanup()
            raise
        self._runner = runner
        log_event(_logger, logging.INFO, "ops.started", status=self._port)

    async def stop(self) -> None:
        """关闭监听；重复调用安全。"""
        runner, self._runner = self._runner, None
        if runner is None:
            return
        await runner.cleanup()
        log_event(_logger, logging.INFO, "ops.stopped")

    # --- 路由处理 -----------------------------------------------------------

    async def _handle_livez(self, _request: web.Request) -> web.Response:
        """存活探针：事件循环与关键任务是否还在跑。"""
        if self._livez():
            return web.Response(status=200, text="ok")
        return web.Response(status=503, text="down")

    async def _handle_readyz(self, _request: web.Request) -> web.Response:
        """就绪探针：能否接单处理消息。"""
        if self._readyz():
            return web.Response(status=200, text="ready")
        return web.Response(status=503, text="not ready")
