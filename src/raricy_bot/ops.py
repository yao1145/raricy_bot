"""内建运维端点：`/livez`、`/readyz` 与 `/archivez`。

`/livez` 与 `/readyz` 只暴露两个布尔探针，响应体是固定短文本，**不包含**任何内部
状态细节（频道、队列长度、错误信息等一律不出现在响应里）。监听地址与端口来自
`config.ops`（见 D-10）；Docker Compose 只 `expose` 不发布到宿主。

`/archivez` 是永久归档的**独立**健康检查（计划 §5.3），与业务存活/就绪刻意分开：
磁盘或文件系统出问题不该让 `/livez` 翻红，那样宿主会按「服务死了」反复重启一个
其实还在正常收发消息的进程。它只回计数与布尔（已写条数、未持久化条数、磁盘是否
偏低），不含路径、组件名或任何事件字段。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from aiohttp import web

from .logging_setup import get_logger, log_event

_logger = get_logger("ops")

ArchiveStatus = Callable[[], "dict[str, object] | None"]


class OpsServer:
    """基于 aiohttp 的运维 HTTP 服务；`stop()` 可重复调用。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        livez: Callable[[], bool],
        readyz: Callable[[], bool],
        archive_status: ArchiveStatus | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._livez = livez
        self._readyz = readyz
        self._archive_status = archive_status
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """启动监听；端口被占用时异常向上抛出，由调用方决定是否致命。"""
        if self._runner is not None:
            return  # 幂等：已启动则不再重复绑定
        app = web.Application()
        app.router.add_get("/livez", self._handle_livez)
        app.router.add_get("/readyz", self._handle_readyz)
        if self._archive_status is not None:
            app.router.add_get("/archivez", self._handle_archivez)
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

    async def _handle_archivez(self, _request: web.Request) -> web.Response:
        """归档健康检查：计数与布尔，无路径、无组件名、无事件字段。

        未启用归档时回 404：`archivez` 缺席本身就是「这台机器不承诺永久保留」
        这个事实的最简表达，比回一个 `{"enabled": false}` 更难被误读成"一切正常"。
        """
        assert self._archive_status is not None
        status = self._archive_status()
        if status is None:
            return web.Response(status=404, text="no archive")
        healthy = bool(status.get("healthy"))
        return web.Response(
            status=200 if healthy else 503,
            content_type="application/json",
            text=json.dumps(status, ensure_ascii=False, sort_keys=True),
        )
