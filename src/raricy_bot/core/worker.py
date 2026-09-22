"""模型客户端与工作器池。

- `OpenAIModelClient` 封装 openai SDK 的**普通**调用，把各类异常统一映射为
  `ModelError`，并且**只由本模块**控制重试（SDK 的 `max_retries=0`）；完整版的
  工具协议与两轮调用在 `mcp/tool_client.py` 的子类里（LIGHT_EDITION_DESIGN §4.3），
  本模块不导入任何工具合同，因此可以进入无 MCP 的 Light 发行闭包；
- `WorkerPool` 起固定数量的 worker task 消费 `SessionScheduler`（`core/scheduler.py`），
  **同一 `session_key` 严格串行**、等待同一会话的请求不占用执行容量，不同会话按
  `concurrency` 并发，handler 抛异常只记日志、不终止 worker。

日志只写稳定事件字段，绝不写模型请求体或响应正文（§19 红线）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Generic, Protocol, TypeVar

import httpx
import openai

from ..config import ModelConfig
from ..diag import elapsed_ms
from ..logging_setup import get_logger, log_event, new_trace_id, observe_task, safe_stack
from ..redact import Redactor
from ..text_utils import estimate_tokens
from .scheduler import RunnableQueue

logger = get_logger("worker")

# --- 稳定失败 kind（定时发文用，§53.9）---------------------------------------
# 与既有的 `timeout` / `http` / `bad_request` 一样是稳定 token：只进日志与运行记录，
# 不带任何正文。定义在这里而不是 `blog/models.py`，是因为它们描述的是**模型调用**的
# 失败形状，模型的三个调用方（聊天、评论、发文）都可能看到它们。
KIND_TRUNCATED = "truncated"
"""最终回复被 `max_tokens` 截断（`finish_reason == "length"`）：半篇正文不得发布。"""

KIND_INVALID_COMPLETION = "invalid_completion"
"""最终回复的完成原因不可接受：拒答或未知/缺失的 `finish_reason`。"""

KIND_INPUT_TOO_LARGE = "input_too_large"
"""整份请求超出 `max_input_tokens`：在发出网络调用之前就被本地拦下，写作要求不裁剪。"""

KIND_STRICT_UNSUPPORTED = "strict_unsupported"
"""客户端不接受 `require_complete` / `max_input_tokens`：发文**明确失败**，不静默降级。"""

KIND_TIMEOUT = "timeout"
"""整次调用超出上限；与 `_map_error` 的既有 `timeout` 是同一个取值。"""

# 严格完成检查接受的完成原因。完整版工具循环的首轮会额外放行 `tool_calls`
# （`mcp/tool_client.py`），它意味着「继续工具协议」而不是可发布的成稿。
_FINISH_STOP_ONLY: frozenset[str] = frozenset({"stop"})

# JSON 序列化文本之外的请求框架开销（角色、分隔、工具定义外层包装）的粗估。
# 与项目其它 token 预算一样是**估算**：不声称等同模型商的 tokenizer。
_REQUEST_OVERHEAD_TOKENS: int = 16


def _completion_kind(finish_reason: str | None) -> str:
    """把不可接受的完成原因映射成稳定 kind：截断单独一档，其余归入 invalid_completion。"""
    return KIND_TRUNCATED if finish_reason == "length" else KIND_INVALID_COMPLETION


def _estimate_request_tokens(messages: Any, tools: Any) -> int:
    """估算整份请求的输入量。

    对**最终的** `messages` 与 `tools` 的 JSON 文本（`ensure_ascii=False`）套现有
    `estimate_tokens`，再加一档框架开销：工具定义、assistant 工具调用参数与 tool 消息
    都在序列化结果里，第二轮因此不可能绕过检查。
    """
    payload = json.dumps(
        {"messages": messages, "tools": list(tools)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return estimate_tokens(payload) + _REQUEST_OVERHEAD_TOKENS


class SessionRequest(Protocol):
    """工作器要求请求对象提供的最小协议。"""

    session_key: str


RequestT = TypeVar("RequestT", bound=SessionRequest)

# 可重试错误最多重试一次（总共两次调用），由本模块自己控制。
_MAX_ATTEMPTS: int = 2


class ToolGenerationCancelled(Exception):
    """请求在工具循环的异步边界已被 /reset 作废。"""


class ModelClient(Protocol):
    """模型客户端协议；只需实现 `complete`。"""

    async def complete(self, messages: list[dict[str, Any]]) -> str: ...


# 模型普通调用的日志阶段名；工具循环的后续轮阶段名在 mcp/tool_client.py。
STAGE_FIRST_ROUND = "first"


def _status_code(exc: BaseException) -> int | None:
    """取 SDK 异常上的 HTTP 状态码；没有或不是整数就返回 None。"""
    value = getattr(exc, "status_code", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class ModelError(Exception):
    """模型调用失败；`retryable` 表示本模块是否已再试过（最终失败时抛出）。

    三个可选元数据都是**结构化**的，不放异常正文：`http_status` 来自 SDK 的
    `status_code`，`stage` 区分首轮与工具后续轮，`duration_ms` 是这一次尝试的耗时。
    它们全部可以作为关键字参数省略，既有构造调用逐字不变。
    """

    def __init__(
        self,
        kind: str,
        retryable: bool,
        *,
        http_status: int | None = None,
        stage: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.retryable = retryable
        self.http_status = http_status
        self.stage = stage
        self.duration_ms = duration_ms

    def log_fields(self) -> dict[str, object]:
        """供 `log_event` 使用的安全字段；未设置的元数据不出现在结果里。

        日志字段名与 `ModelError` 的属性名刻意不同：`http_status` 对应日志里的
        `http_status`，而 `error` 用异常类名 —— 与其余模块的约定保持一致。
        """
        fields: dict[str, object] = {
            "kind": self.kind,
            "retryable": self.retryable,
            "error": type(self).__name__,
        }
        if self.http_status is not None:
            fields["http_status"] = self.http_status
        if self.stage is not None:
            fields["stage"] = self.stage
        if self.duration_ms is not None:
            fields["duration_ms"] = self.duration_ms
        return fields


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

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        require_complete: bool = False,
        max_input_tokens: int | None = None,
    ) -> str:
        """调用 chat.completions；可重试错误只重试一次，最终失败抛 `ModelError`。

        两个可选参数只服务于定时发文（§53.9），默认值让聊天路径逐字节不变：
        `require_complete=True` 时只有正常 `stop` 的回复可接受；`max_input_tokens`
        非 None 时在每次网络调用之前检查整份请求。
        """
        attempt = 0
        while True:
            self._check_input_budget(messages, (), max_input_tokens)
            started = time.monotonic()
            try:
                response = await self._client.chat.completions.create(
                    model=self._cfg.model,
                    messages=messages,
                    temperature=self._cfg.temperature,
                    max_tokens=self._cfg.max_output_tokens,
                )
            except Exception as exc:  # 任何异常都必须映射为 ModelError
                error = self._map_error(
                    exc,
                    stage=STAGE_FIRST_ROUND,
                    duration_ms=elapsed_ms(started),
                )
                if not error.retryable or attempt + 1 >= _MAX_ATTEMPTS:
                    raise error from exc
                attempt += 1
                log_event(
                    logger,
                    logging.WARNING,
                    "model.retry",
                    attempt=attempt,
                    **error.log_fields(),
                )
                continue

            # 严格完成检查刻意放在异常映射**之外**：它产生的是稳定失败，既不参与重试，
            # 也不会被 `_map_error` 改写成可重试的未知错误。
            self._ensure_complete(response, _FINISH_STOP_ONLY if require_complete else None)
            text = self._extract_text(response)

            if not text:
                error = ModelError(
                    "empty",
                    True,
                    stage=STAGE_FIRST_ROUND,
                    duration_ms=elapsed_ms(started),
                )
                if attempt + 1 >= _MAX_ATTEMPTS:
                    raise error
                attempt += 1
                log_event(
                    logger,
                    logging.WARNING,
                    "model.retry",
                    attempt=attempt,
                    **error.log_fields(),
                )
                continue

            return text

    async def aclose(self) -> None:
        """关闭底层 SDK 与其 http 客户端。"""
        await self._client.close()

    @staticmethod
    def _check_input_budget(
        messages: list[dict[str, Any]],
        tools: Any,
        max_input_tokens: int | None,
    ) -> None:
        """输入预算检查：超限是稳定失败，**不裁剪**写作要求（§53.9）。"""
        if max_input_tokens is None:
            return
        if _estimate_request_tokens(messages, tools) > max_input_tokens:
            raise ModelError(KIND_INPUT_TOO_LARGE, False)

    @classmethod
    def _ensure_complete(
        cls, response: Any, accepted_finish_reasons: frozenset[str] | None
    ) -> None:
        """严格完成检查（§53.9）；`accepted` 为 None 时不做任何检查（聊天路径）。

        只有正常 `stop`（首轮合法的 `tool_calls` 由调用方加进来）且没有明确拒答的回复
        才算成稿：`length` 是半篇正文，拒答与未知完成原因同样不可发布。
        """
        if accepted_finish_reasons is None:
            return
        reason = cls._finish_reason(response)
        if reason in accepted_finish_reasons and not cls._has_refusal(response):
            return
        raise ModelError(_completion_kind(reason), False)

    @classmethod
    def _finish_reason(cls, response: Any) -> str | None:
        """取第一条 choice 的完成原因；结构异常一律返回 None（= 未知）。"""
        choices = cls._field(response, "choices") or []
        if not choices:
            return None
        reason = cls._field(choices[0], "finish_reason")
        return reason if isinstance(reason, str) and reason else None

    @classmethod
    def _has_refusal(cls, response: Any) -> bool:
        """模型是否明确拒答（`message.refusal` 被填上，部分兼容端点会这么返回）。"""
        choices = cls._field(response, "choices") or []
        if not choices:
            return False
        refusal = cls._field(cls._field(choices[0], "message"), "refusal")
        return isinstance(refusal, str) and bool(refusal.strip())

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        """兼容 openai SDK 对象与测试用 dict；不递归暴露未知字段。"""
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)

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
    def _map_error(
        exc: Exception, *, stage: str | None = None, duration_ms: int | None = None
    ) -> ModelError:
        """按 openai SDK 的异常类型映射；顺序敏感（父类在后）。

        `http_status` 取自 SDK 的 `status_code`：401（凭据失效）、429（限流）、
        500（上游故障）在日志里必须能分开，而它们的 `kind` 可能只是笼统的
        `http` / `bad_request`（计划 §4 的「模型失败」一行）。
        """
        if isinstance(exc, ModelError):
            return exc
        status = _status_code(exc)
        if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException)):
            # APITimeoutError 是 APIConnectionError 的子类，必须先判。
            # 超时不重试（D-19）：这次调用已经等满整个超时预算，立即重试几乎必然再等满一次，
            # 只把用户看到的静默从 1 个超时周期拖成 2 个。
            return ModelError(
                KIND_TIMEOUT, False, http_status=status, stage=stage, duration_ms=duration_ms
            )
        if isinstance(exc, openai.APIConnectionError):
            return ModelError(
                "network", True, http_status=status, stage=stage, duration_ms=duration_ms
            )
        if isinstance(exc, (openai.RateLimitError, openai.InternalServerError)):
            # 429 与 5xx：可重试。二者都是 APIStatusError 的子类。
            return ModelError(
                "http", True, http_status=status, stage=stage, duration_ms=duration_ms
            )
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return ModelError(
                "auth", False, http_status=status, stage=stage, duration_ms=duration_ms
            )
        if isinstance(exc, openai.APIStatusError) and status == 408:
            # SDK 没有 408 分支，会把它归入通用 APIStatusError；必须在兜底之前显式判出，
            # 否则它会被下面的「其余 4xx」吃成 bad_request，日志里就看不出是超时了。
            # 归类为 timeout，因此同样不重试（D-19）。
            return ModelError(
                KIND_TIMEOUT, False, http_status=status, stage=stage, duration_ms=duration_ms
            )
        if isinstance(exc, openai.APIStatusError):
            # BadRequestError / NotFoundError 及其余确定性的 4xx 状态。
            return ModelError(
                "bad_request", False, http_status=status, stage=stage, duration_ms=duration_ms
            )
        return ModelError(
            "network", True, http_status=status, stage=stage, duration_ms=duration_ms
        )


class WorkerPool(Generic[RequestT]):
    """固定并发的工作器池；同一 `session_key` 的请求严格串行且不占用执行容量。

    队列不是裸 `asyncio.Queue`，而是 `SessionScheduler`（`core/scheduler.py`）。
    工作器只领取**可运行会话**的一条请求；同一会话的后续请求留在调度器里等待，
    不再出现「一个在处理、其余 worker 排队等会话锁」而把别的会话挡在全局队列里的情况。
    """

    def __init__(
        self,
        *,
        queue: RunnableQueue[RequestT],
        handler: Callable[[RequestT], Awaitable[None]],
        concurrency: int,
    ) -> None:
        self._queue = queue
        self._handler = handler
        self._concurrency = concurrency
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def alive(self) -> bool:
        """所有 worker task 都仍在运行时为 True（未启动或已停止为 False）。"""
        return bool(self._tasks) and all(not task.done() for task in self._tasks)

    @property
    def alive_count(self) -> int:
        """仍存活的 worker 数；健康变化事件用它（计划 §4）。"""
        return sum(1 for task in self._tasks if not task.done())

    async def start(self) -> None:
        """启动 `concurrency` 个 worker task；重复调用幂等。"""
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._run(), name=f"worker-{index}")
            for index in range(self._concurrency)
        ]
        # 只加观察，不改控制流：worker 死于异常 / 逃逸取消时至少会留下一条
        # `app.task_exit`。事件档案 §六.1 记的正是「worker 一死 /livez 永久 503
        # 而现场毫无痕迹」—— 这里补的就是那个痕迹（计划 §4）。
        for task in self._tasks:
            observe_task(task, task.get_name(), component="worker")

    async def stop(self) -> None:
        """取消全部 worker 并等待其结束；之后 `alive` 为 False。"""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- 内部实现 ---

    async def _run(self) -> None:
        """单个 worker 主循环；除取消外绝不退出。

        只向调度器领取**可运行会话**的一条请求（调度器同时把该会话标为 active），
        因此等待同一会话的其他请求留在调度器里，不占住这个 worker。
        handler 异常或取消都必须走 `finally` 的 `task_done`，否则活跃计数会失衡、
        `join()` 会永远挂住。取消发生在 `get_runnable()`（还没领到请求）时不会误标任何
        未处理请求为 done —— 那些请求仍留在调度器的等待队列里。
        """
        while True:
            request = await self._queue.get_runnable()
            # 队列里的请求由 Router 生成 trace_id；没有的（替身、直接入队的测试）
            # 这里补一个，保证「一次处理」在日志里总有一个可 grep 的标识。
            if not getattr(request, "trace_id", ""):
                try:
                    request.trace_id = new_trace_id()
                except AttributeError:
                    pass
            try:
                try:
                    await self._handler(request)
                except Exception as exc:  # 单条请求失败不得终止 worker
                    log_event(
                        logger,
                        logging.ERROR,
                        "worker.handler_error",
                        trace_id=getattr(request, "trace_id", None),
                        channel_id=getattr(request, "channel_id", None),
                        error=type(exc).__name__,
                        stack=safe_stack(exc),
                    )
            finally:
                self._queue.task_done(request)
