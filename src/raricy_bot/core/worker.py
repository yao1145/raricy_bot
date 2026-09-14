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
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

import httpx
import openai

from ..config import ModelConfig
from ..logging_setup import get_logger, log_event
from ..mcp.contracts import (
    McpProvider,
    ToolCall,
    ToolCompletion,
    ToolDefinition,
    ToolExecution,
    ToolExecutor,
)
from ..redact import Redactor

logger = get_logger("worker")


class SessionRequest(Protocol):
    """工作器要求请求对象提供的最小协议。"""

    session_key: str


RequestT = TypeVar("RequestT", bound=SessionRequest)

# 可重试错误最多重试一次（总共两次调用），由本模块自己控制。
_MAX_ATTEMPTS: int = 2


class ToolGenerationCancelled(Exception):
    """请求在工具循环的异步边界已被 /reset 作废。"""


class ToolRegistry(Protocol):
    """功能绑定与工具白名单的最小协议。"""

    def tools_for(self, feature_name: str) -> tuple[ToolDefinition, ...]: ...

    def feature_available(self, feature_name: str) -> bool: ...

    async def execute(self, feature_name: str, call: ToolCall) -> ToolExecution: ...


@dataclass(frozen=True)
class _ToolRound:
    """OpenAI 一轮响应的内部提取结果。"""

    text: str
    tool_calls: tuple[ToolCall, ...]
    raw_message: dict[str, Any]


class ModelClient(Protocol):
    """模型客户端协议；只需实现 `complete`。"""

    async def complete(self, messages: list[dict[str, Any]]) -> str: ...


class ToolCapableModelClient(ModelClient, Protocol):
    """支持 Chat Completions function tools 的可选模型协议。"""

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: tuple[ToolDefinition, ...],
        execute: ToolExecutor,
        max_tool_calls: int,
        generation_is_current: Callable[[], bool],
        model_gate: Any | None = None,
    ) -> ToolCompletion: ...


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
        self._tools_unsupported = False
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

    async def complete(self, messages: list[dict[str, Any]]) -> str:
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

    @property
    def tools_unsupported(self) -> bool:
        """当前模型端点是否已确认不支持 tools；只缓存首轮 400/404。"""
        return self._tools_unsupported

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: tuple[ToolDefinition, ...],
        execute: ToolExecutor,
        max_tool_calls: int,
        generation_is_current: Callable[[], bool],
        model_gate: Any | None = None,
    ) -> ToolCompletion:
        """执行至多一次工具的两轮 Chat Completions 工具循环。

        第一轮让模型自动决定是否调用工具并关闭并行调用；工具结果随后以
        ``role=tool`` 回传，第二轮明确设置 ``tool_choice=none``。MCP 内容由
        executor 负责清洗，本方法只把不可信字符串作为工具消息传递。
        """
        if not tools:
            raise ModelError("tools_unavailable", False)
        if self._tools_unsupported:
            raise ModelError("tools_unsupported", False)
        if max_tool_calls < 1:
            raise ModelError("tools_unavailable", False)

        self._check_generation(generation_is_current)
        first = await self._create_tool_completion(
            messages,
            tools=tools,
            tool_choice="auto",
            parallel_tool_calls=False,
            model_gate=model_gate,
            detect_tools_unsupported=True,
        )
        self._check_generation(generation_is_current)
        if not first.tool_calls:
            if not first.text:
                raise ModelError("empty", True)
            return ToolCompletion(first.text, (), None)

        # 即使模型违反 parallel_tool_calls=False，也只执行第一个名称与参数均合法的
        # 绑定工具。未知名称不消耗搜索预算；已执行一个合法调用后，其余合法调用只
        # 生成预算耗尽错误，使第二轮请求仍满足 SDK 的消息配对合同。
        executions: list[ToolExecution] = []
        allowed_names = {tool.model_name for tool in tools}
        executed_tool_names: list[str] = []
        for call in first.tool_calls:
            self._check_generation(generation_is_current)
            if call.model_name not in allowed_names:
                execution = ToolExecution(
                    call_id=call.call_id,
                    content="tool not allowed",
                    is_error=True,
                    error_kind="tool_not_allowed",
                    history_context=None,
                )
            elif len(executed_tool_names) >= max_tool_calls:
                execution = ToolExecution(
                    call_id=call.call_id,
                    content="tool call budget exhausted",
                    is_error=True,
                    error_kind="tool_budget_exhausted",
                    history_context=None,
                )
            else:
                try:
                    execution = await execute(call)
                except ToolGenerationCancelled:
                    raise
                except Exception:
                    # executor 是外部边界；不把异常正文或参数带入模型。
                    execution = ToolExecution(
                        call_id=call.call_id,
                        content="tool unavailable",
                        is_error=True,
                        error_kind="tool_unavailable",
                        history_context=None,
                    )
                # Registry 的 invalid_arguments 表示该候选尚未实际执行，允许
                # 后续返回的合法候选竞争本轮唯一预算；其他结果都算已尝试。
                if execution.error_kind != "invalid_arguments":
                    executed_tool_names.append(call.model_name)
            executions.append(execution)
            self._check_generation(generation_is_current)

        assistant_message = first.raw_message
        tool_messages: list[dict[str, Any]] = [
            {
                "role": "tool",
                "tool_call_id": execution.call_id,
                "content": execution.content,
            }
            for execution in executions
        ]
        followup_messages = list(messages)
        followup_messages.append(assistant_message)
        followup_messages.extend(tool_messages)

        self._check_generation(generation_is_current)
        final = await self._create_tool_completion(
            followup_messages,
            # 第二轮只让模型生成正文；不再把任何可调用工具定义发回端点。
            tools=(),
            tool_choice="none",
            parallel_tool_calls=False,
            model_gate=model_gate,
            detect_tools_unsupported=False,
        )
        self._check_generation(generation_is_current)
        if not final.text:
            raise ModelError("empty", True)
        history_context = next(
            (
                execution.history_context
                for execution in executions
                if execution.history_context is not None
            ),
            None,
        )
        return ToolCompletion(
            final.text,
            tuple(executed_tool_names),
            history_context,
        )

    async def aclose(self) -> None:
        """关闭底层 SDK 与其 http 客户端。"""
        await self._client.close()

    async def _create_tool_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: tuple[ToolDefinition, ...],
        tool_choice: str,
        parallel_tool_calls: bool,
        model_gate: Any | None,
        detect_tools_unsupported: bool,
    ) -> "_ToolRound":
        """发起一轮带工具请求并复用现有错误重试映射。"""
        payload = tuple(self._tool_payload(tool) for tool in tools)
        attempt = 0
        while True:
            try:
                request_kwargs: dict[str, Any] = {
                    "model": self._cfg.model,
                    "messages": messages,
                    "temperature": self._cfg.temperature,
                    "max_tokens": self._cfg.max_output_tokens,
                    "tool_choice": tool_choice,
                }
                # 工具循环的最终轮不提供空工具列表；某些兼容端点把空数组
                # 误当成非法 tools 参数，但仍接受明确的 tool_choice=none。
                if payload:
                    request_kwargs["tools"] = list(payload)
                    request_kwargs["parallel_tool_calls"] = parallel_tool_calls
                if model_gate is None:
                    response = await self._client.chat.completions.create(
                        **request_kwargs,
                    )
                else:
                    async with model_gate:
                        response = await self._client.chat.completions.create(
                            **request_kwargs,
                        )
                result = self._extract_tool_round(response)
            except Exception as exc:
                error = self._map_error(exc)
                if (
                    detect_tools_unsupported
                    and isinstance(exc, openai.APIStatusError)
                    and getattr(exc, "status_code", None) in {400, 404}
                ):
                    self._tools_unsupported = True
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
            if not result.text and not result.tool_calls:
                error = ModelError("empty", True)
                if attempt + 1 >= _MAX_ATTEMPTS:
                    raise error
                attempt += 1
                log_event(
                    logger,
                    logging.WARNING,
                    "model.retry",
                    kind=error.kind,
                    attempt=attempt,
                )
                continue
            return result

    @staticmethod
    def _check_generation(predicate: Callable[[], bool]) -> None:
        """generation 失效时抛专用异常，App 不得发送失败通知。"""
        if not predicate():
            raise ToolGenerationCancelled()

    @staticmethod
    def _tool_payload(tool: ToolDefinition) -> dict[str, Any]:
        """把领域工具转换为 OpenAI function tool 定义。"""
        return {
            "type": "function",
            "function": {
                "name": tool.model_name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    @classmethod
    def _extract_tool_round(cls, response: Any) -> "_ToolRound":
        """从 SDK 对象或兼容的 dict 中提取正文、调用和原始 assistant 消息。"""
        choices = cls._field(response, "choices") or []
        if not choices:
            return _ToolRound("", (), {"role": "assistant", "content": None})
        message = cls._field(choices[0], "message")
        text = cls._field(message, "content")
        text = text.strip() if isinstance(text, str) else ""
        raw_calls = cls._field(message, "tool_calls") or []
        calls: list[ToolCall] = []
        serial_calls: list[dict[str, Any]] = []
        for item in raw_calls:
            function = cls._field(item, "function")
            call_id = cls._field(item, "id")
            name = cls._field(function, "name")
            arguments = cls._field(function, "arguments")
            if not isinstance(call_id, str) or not call_id:
                continue
            if not isinstance(name, str) or not name:
                name = ""
            if not isinstance(arguments, str):
                arguments = ""
            calls.append(ToolCall(call_id, name, arguments))
            serial_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
        assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
        if serial_calls:
            assistant["tool_calls"] = serial_calls
        return _ToolRound(text, tuple(calls), assistant)

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


class WorkerPool(Generic[RequestT]):
    """固定并发的工作器池；同一 `session_key` 的请求严格串行。"""

    def __init__(
        self,
        *,
        queue: asyncio.Queue[RequestT],
        handler: Callable[[RequestT], Awaitable[None]],
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
