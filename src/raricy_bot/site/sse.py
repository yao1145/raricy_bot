"""SSE 长连接接收器：手写帧解析、自愈重连、断线补齐。

设计要点（INTERFACES §8、chat-bot.md §6）：

- 不引入 `httpx-sse`，自己解析帧，显式处理 `retry:` 与 `id:`；
- `run()` 只在被 cancel 或 `stop()` 后退出，任何连接/解析异常都转成重连；
- handler 抛异常只记日志并继续消费后续帧，绝不因此拆连接或触发重连，
  否则一条坏消息会造成无限重连；
- `id:` 只出现在 message 帧上；没有 `id:` 的帧不得清空水位。
"""

from __future__ import annotations

import asyncio
import logging
import random as random_module
from collections.abc import Awaitable, Callable

import httpx

from ..logging_setup import get_logger, log_event
from .client import SiteClient
from .models import StreamEvent

# 抖动上限：退避时长最多上浮 20%。
_JITTER_RATIO: float = 0.2


def _parse_int(value: str) -> int | None:
    """把字段值解析为 int；解析失败返回 None（用于忽略非法 id:/retry:）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SSEReceiver:
    """消费站点 SSE 流并把 `StreamEvent` 交给 handler；内部负责重连与补齐。"""

    def __init__(
        self,
        client: SiteClient,
        handler: Callable[[StreamEvent], Awaitable[None]],
        *,
        base_delay: float = 3.0,
        max_delay: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        random: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._handler = handler
        self._base_delay = base_delay
        self._max_delay = max_delay
        # 注入点：测试用固定 sleep/random 让退避可精确断言。
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._random = random if random is not None else random_module.random

        self._connected = False
        self._stopped = False
        self._last_event_id: int | None = None
        # 服务端 `retry:` 给出的毫秒值；给出后与 base_delay 取较大者。
        self._server_retry_ms: int | None = None
        self._attempt = 0
        self._logger = get_logger("sse")

    @property
    def connected(self) -> bool:
        """当前是否处于已建立且正在消费的流连接中。"""
        return self._connected

    @property
    def last_event_id(self) -> int | None:
        """已处理的最后一个 message 帧的 SSE 事件 id。"""
        return self._last_event_id

    def set_last_event_id(self, value: int | None) -> None:
        """由 app 在 resync/水位推进后设置下次重连要带的 Last-Event-ID。"""
        self._last_event_id = value

    async def run(self) -> None:
        """持续消费；连接失败或正常断开都走退避重连，直到 cancel 或 stop()。"""
        self._attempt = 0
        while not self._stopped:
            try:
                await self._connect_and_consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何异常都不得终止循环
                log_event(
                    self._logger,
                    logging.WARNING,
                    "sse.connect_failed",
                    error=type(exc).__name__,
                    attempt=self._attempt,
                )
            finally:
                self._connected = False

            if self._stopped:
                break

            delay = self._backoff_delay()
            self._attempt += 1
            log_event(
                self._logger,
                logging.INFO,
                "sse.reconnect",
                attempt=self._attempt,
                delay=round(delay, 3),
            )
            await self._sleep(delay)

    async def stop(self) -> None:
        """请求退出：run() 在下次检查点返回。"""
        self._stopped = True

    # --- 内部实现 ---

    async def _connect_and_consume(self) -> None:
        """建立一条连接并消费至流结束；异常向上抛给 run() 触发重连。"""
        await self._client.ensure_session()
        async with self._client.open_stream(self._last_event_id) as response:
            self._connected = True
            log_event(self._logger, logging.INFO, "sse.connected")
            try:
                await self._consume(response)
            finally:
                self._connected = False

    async def _consume(self, response: httpx.Response) -> None:
        """逐行解析 SSE：空行分帧、`: ` 注释忽略、多行 data 以换行拼接。"""
        data_lines: list[str] = []
        frame_id: int | None = None

        async for line in response.aiter_lines():
            if line == "":
                await self._dispatch(data_lines, frame_id)
                data_lines = []
                frame_id = None
                continue
            if line.startswith(":"):
                continue

            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                # SSE 规范：冒号后最多去掉一个前导空格。
                value = value[1:]

            if field == "data":
                data_lines.append(value)
            elif field == "id":
                parsed = _parse_int(value)
                if parsed is not None:
                    frame_id = parsed
            elif field == "retry":
                parsed = _parse_int(value)
                if parsed is not None:
                    self._server_retry_ms = parsed

    async def _dispatch(self, data_lines: list[str], frame_id: int | None) -> None:
        """派发一个已完成的帧；无 data 的帧只用于携带 retry。"""
        if not data_lines:
            return

        event = StreamEvent.from_sse("\n".join(data_lines), frame_id)
        if event is None:
            # JSON 非法：不是错误，记一条日志后跳过。
            log_event(self._logger, logging.WARNING, "sse.bad_frame")
            return

        # 成功解析出一帧即视为连接健康，清掉此前累积的退避。
        self._attempt = 0
        log_event(self._logger, logging.DEBUG, "sse.frame", kind=event.kind, event_id=event.event_id)

        try:
            await self._handler(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - handler 的异常不得拆连接
            log_event(
                self._logger,
                logging.WARNING,
                "sse.handler_error",
                kind=event.kind,
                event_id=event.event_id,
                error=type(exc).__name__,
            )

        if event.kind == "message" and event.event_id is not None:
            self._last_event_id = event.event_id

    def _backoff_delay(self) -> float:
        """计算本次重连等待：base * 2**attempt，封顶后叠加 [0, 0.2) 抖动。"""
        base = self._base_delay
        if self._server_retry_ms is not None:
            base = max(base, self._server_retry_ms / 1000.0)
        delay = min(self._max_delay, base * (2**self._attempt))
        return delay * (1.0 + self._random() * _JITTER_RATIO)
