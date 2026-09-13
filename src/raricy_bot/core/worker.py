"""模型客户端与工作器池。

- `OpenAIModelClient` 封装 openai SDK，把各类异常统一映射为 `ModelError`，
  并且**只由本模块**控制重试（SDK 的 `max_retries=0`）；
- `WorkerPool` 起固定数量的 worker task 消费队列，**同一 `session_key` 严格串行**、
  不同会话按 `concurrency` 并发，handler 抛异常只记日志、不终止 worker。

日志只写稳定事件字段，绝不写模型请求体或响应正文（§19 红线）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

import httpx
import openai

from ..config import ModelConfig
from ..logging_setup import get_logger, log_event
from ..redact import Redactor

if TYPE_CHECKING:
    from .router import Request

logger = get_logger("worker")

# 可重试错误最多重试一次（总共两次调用），由本模块自己控制。
_MAX_ATTEMPTS: int = 2


class ModelClient(Protocol):
    """模型客户端协议；只需实现 `complete`。"""

    async def complete(self, messages: list[dict[str, str]]) -> str: ...


class ModelError(Exception):
    """模型调用失败；`retryable` 表示本模块是否已再试过（最终失败时抛出）。"""

    def __init__(self, kind: str, retryable: bool) -> None:
        super().__init__(kind)
        self.kind = kind
        self.retryable = retryable


class OpenAIModelClient:
    """基于 openai SDK 的模型客户端；异常映射与重试策略见模块 docstring。"""

    def __init__(
        self,
        cfg: ModelConfig,
        api_key: str,
        *,
        redactor: Redactor,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        # 密钥登记进脱敏器，确保任何出站文本与日志都不会泄露它。
        redactor.add_secret(api_key)
        kwargs: dict[str, Any] = {
            "base_url": cfg.base_url,
            "api_key": api_key,
            "timeout": cfg.timeout_seconds,
            "max_retries": 0,
        }
        if transport is not None:
            # 测试注入假传输；transport 为 None 时让 SDK 自建 http 客户端。
            kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        self._client = openai.AsyncOpenAI(**kwargs)

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """调用 chat.completions；可重试错误只重试一次，最终失败抛 `ModelError`。"""
        attempt = 0
        while True:
            try:
                response = await self._client.chat.completions.create(
                    model=self._cfg.model,
                    messages=messages,
                    temperature=self._cfg.temperature,
                    max_tokens=self._cfg.max_output_tokens,
                )
                text = self._extract_text(response)
            except Exception as exc:  # 任何异常都必须映射为 ModelError
                error = self._map_error(exc)
                if not error.retryable or attempt + 1 >= _MAX_ATTEMPTS:
                    raise error from exc
                attempt += 1
                log_event(
                    logger,
                    logging.WARNING,
                    "model.retry",
                    kind=error.kind,
                    attempt=attempt,
                )
                continue

            if not text:
                error = ModelError("empty", True)
                if attempt + 1 >= _MAX_ATTEMPTS:
                    raise error
                attempt += 1
                log_event(
                    logger, logging.WARNING, "model.retry", kind=error.kind, attempt=attempt
                )
                continue

            return text

    async def aclose(self) -> None:
        """关闭底层 SDK 与其 http 客户端。"""
        await self._client.close()

    # --- 内部实现 ---

    @staticmethod
    def _extract_text(response: Any) -> str:
        """取第一条 choice 的正文并 strip；结构异常时退化为空串（触发 empty 重试）。"""
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            return ""
        return content.strip()

    @staticmethod
    def _map_error(exc: Exception) -> ModelError:
        """按 openai SDK 的异常类型映射；顺序敏感（父类在后）。"""
        if isinstance(exc, ModelError):
            return exc
        if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException)):
            # APITimeoutError 是 APIConnectionError 的子类，必须先判。
            # 超时不重试（D-19）：这次调用已经等满整个超时预算，立即重试几乎必然再等满一次，
            # 只把用户看到的静默从 1 个超时周期拖成 2 个。
            return ModelError("timeout", False)
        if isinstance(exc, openai.APIConnectionError):
            return ModelError("network", True)
        if isinstance(exc, (openai.RateLimitError, openai.InternalServerError)):
            # 429 与 5xx：可重试。二者都是 APIStatusError 的子类。
            return ModelError("http", True)
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return ModelError("auth", False)
        if isinstance(exc, openai.APIStatusError) and getattr(exc, "status_code", None) == 408:
            # SDK 没有 408 分支，会把它归入通用 APIStatusError；必须在兜底之前显式判出，
            # 否则它会被下面的「其余 4xx」吃成 bad_request，日志里就看不出是超时了。
            # 归类为 timeout，因此同样不重试（D-19）。
            return ModelError("timeout", False)
        if isinstance(exc, openai.APIStatusError):
            # BadRequestError / NotFoundError 及其余确定性的 4xx 状态。
            return ModelError("bad_request", False)
        return ModelError("network", True)


class WorkerPool:
    """固定并发的工作器池；同一 `session_key` 的请求严格串行。"""

    def __init__(
        self,
        *,
        queue: asyncio.Queue[Request],
        handler: Callable[[Request], Awaitable[None]],
        concurrency: int,
    ) -> None:
        self._queue = queue
        self._handler = handler
        self._concurrency = concurrency
        self._tasks: list[asyncio.Task[None]] = []
        # 每个会话一把锁；单线程事件循环里并发访问字典本身是安全的。
        # `_lock_users` 记录每个会话当前「持有或等待」的 task 数，归零即淘汰锁，
        # 否则大区按 lobby:{user_id} 建键会让字典随历史用户数无限增长。
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}

    @property
    def alive(self) -> bool:
        """所有 worker task 都仍在运行时为 True（未启动或已停止为 False）。"""
        return bool(self._tasks) and all(not task.done() for task in self._tasks)

    async def start(self) -> None:
        """启动 `concurrency` 个 worker task；重复调用幂等。"""
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._run(), name=f"worker-{index}")
            for index in range(self._concurrency)
        ]

    async def stop(self) -> None:
        """取消全部 worker 并等待其结束；之后 `alive` 为 False。"""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- 内部实现 ---

    def _acquire_lock(self, session_key: str) -> asyncio.Lock:
        """取会话锁并登记一名使用者；不存在则新建。"""
        lock = self._locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_key] = lock
        self._lock_users[session_key] = self._lock_users.get(session_key, 0) + 1
        return lock

    def _release_lock(self, session_key: str) -> None:
        """注销一名使用者；没有任何持有者或等待者时淘汰锁，避免字典无界增长。"""
        remaining = self._lock_users.get(session_key, 0) - 1
        if remaining > 0:
            self._lock_users[session_key] = remaining
            return
        self._lock_users.pop(session_key, None)
        # 计数归零意味着此刻既无人持有也无人等待，可以安全移除。
        self._locks.pop(session_key, None)

    async def _run(self) -> None:
        """单个 worker 主循环；除取消外绝不退出。"""
        while True:
            request = await self._queue.get()
            # task_done 必须无条件下调，否则 queue.join() 会永远挂住；
            # 锁使用者的注销同样必须无条件执行，否则计数会失衡、锁无法淘汰。
            try:
                lock = self._acquire_lock(request.session_key)
                async with lock:
                    try:
                        await self._handler(request)
                    except Exception as exc:  # 单条请求失败不得终止 worker
                        log_event(
                            logger,
                            logging.ERROR,
                            "worker.handler_error",
                            channel_id=getattr(request, "channel_id", None),
                            error=type(exc).__name__,
                        )
            finally:
                self._release_lock(request.session_key)
                self._queue.task_done()
