"""完整版的工具调用模型客户端（LIGHT_EDITION_DESIGN §4.3）。

`core/worker.py` 的 `OpenAIModelClient` 只保留普通调用与调度；Chat Completions 的
工具协议、工具响应解析与两轮调用实现集中在本模块的子类 `ToolCallingModelClient`。
工具能力只服务完整版，因此本模块随 `mcp/` 一起不进入 Light 发行闭包。

日志只写稳定事件字段，绝不写模型请求体、工具参数或响应正文（§19 红线）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import openai

from ..core.worker import (
    _FINISH_STOP_ONLY,
    STAGE_FIRST_ROUND,
    ModelClient,
    ModelError,
    OpenAIModelClient,
    ToolGenerationCancelled,
    _completion_kind,
    _status_code,
)
from ..diag import elapsed_ms
from ..logging_setup import get_logger, log_event
from .contracts import (
    ToolCall,
    ToolCompletion,
    ToolDefinition,
    ToolExecution,
    ToolExecutor,
)

logger = get_logger("tool_client")


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
    finish_reason: str | None = None


class ToolCapableModelClient(ModelClient, Protocol):
    """支持 Chat Completions function tools 的可选模型协议。

    `require_complete` / `max_input_tokens` 是 §53.9 的两个严格参数：定时发文显式传值，
    它会先探测客户端是否接受它们，不接受就稳定失败 —— 绝不静默吞掉严格检查继续发布。
    """

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: tuple[ToolDefinition, ...],
        execute: ToolExecutor,
        max_tool_calls: int,
        generation_is_current: Callable[[], bool],
        model_gate: Any | None = None,
        require_complete: bool = False,
        max_input_tokens: int | None = None,
    ) -> ToolCompletion: ...


# 工具循环后续轮的日志阶段名；首轮沿用 core.worker.STAGE_FIRST_ROUND。
STAGE_TOOL_FOLLOWUP = "tool_followup"

# 严格完成检查里首轮额外允许的完成原因：「模型要求工具」是协议内的中间态，
# 只有最终成稿才必须正常 stop。
_FINISH_STOP_OR_TOOL_CALLS: frozenset[str] = frozenset({"stop", "tool_calls"})

# 结构化「端点不支持 tools」判定用的白名单（计划 §3.2 第 3 条）。
# 只认 SDK 异常 `.body["error"]` 的结构化字段，**绝不**扫描 message 原始正文。
_TOOLS_PARAM_NAMES: frozenset[str] = frozenset(
    {"tools", "tool_choice", "parallel_tool_calls"}
)
"""错误信封里 `error.param` 精确点名工具相关参数时才算命中。"""

_TOOLS_UNSUPPORTED_CODES: frozenset[str] = frozenset(
    {
        "unsupported_parameter",
        "unknown_parameter",
        "unrecognized_parameter",
        "unsupported_value",
    }
)
"""`error.code` 表示「无法识别 / 不支持参数」的**明确**取值；这是唯一被接受的证据。

白名单之外的具体错误码（例如 `invalid_function_parameters`，它表示工具**参数值/形状**
有错，而不是端点不支持该参数）一律不命中。通用 `error.type`（如 `invalid_request_error`）
既不能覆盖一个具体的 `code`，也不能单独成立 —— `param=tools` + 该类型同样可能来自
「tools[0].function.name 类型不对」这类参数错误。
"""


def _is_tools_unsupported(exc: Exception) -> bool:
    """结构化判定：该异常是否**明确**表示「端点不接受 tools 参数」。

    判定依据与取舍（为什么这条窄判定是安全的）：

    1. 只看 openai SDK 异常对象 `.body["error"]` 里的结构化字段（`param` / `code`），
       **不读** `error["message"]` 或 `str(exc)` 的自由正文。报错文案里偶然出现 "tools"
       字样（例如「提示词过长」的文案恰好提到工具）不会触发。
    2. 只有 `param` 精确点名工具相关参数，**且** `code` 存在、是字符串、并命中
       `_TOOLS_UNSUPPORTED_CODES` 白名单时才返回 True。**只认明确的不支持错误码**：
       通用 `type` 不参与判定，它既不能覆盖一个具体的 `code`，也不能单独作为依据
       （`type=invalid_request_error` 是通用请求错误类别，`param=tools` 加上它并不能
       证明端点不支持 tools）。任一结构化字段缺失、取值对不上，或状态码不在 400/404
       内，都返回 False，交回 `_map_error` 的通用分类。
    3. 结果是**本次调用**的结论，不写回任何实例状态：一次请求的失败绝不能当作端点
       永久不支持工具的证据（真相见 INCIDENTS 事件一第六节第 3 条）。

    已知代价（刻意的取舍，不是「不会漏判」）：缺 `code` 的提供方 —— 只把
    「不支持工具」写进正文、或只给通用 `type` 而不给具体错误码 —— 会被漏判，退回
    通用 `bad_request`。之所以接受漏判：漏判只让一次调用按普通错误处理，误判却会把
    与「不支持」无关的具体错误（如 `invalid_function_parameters`）改写成「能力不可用」，
    二者不对称。
    """
    if not isinstance(exc, openai.APIStatusError):
        return False
    if _status_code(exc) not in {400, 404}:
        return False
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return False
    # openai SDK 会把信封里的 `error` 对象**解包**后放进 `.body`（body 即为错误对象本身）；
    # 兼容仍带一层 `{"error": {...}}` 的提供方，两种形状都接受。
    error = body.get("error")
    if not isinstance(error, dict):
        error = body
    param = error.get("param")
    if not isinstance(param, str) or param not in _TOOLS_PARAM_NAMES:
        return False
    # 只认明确的不支持错误码；通用 `type` 不参与判定（见 docstring 第 2、3 点）。
    code = error.get("code")
    return isinstance(code, str) and code in _TOOLS_UNSUPPORTED_CODES


class ToolCallingModelClient(OpenAIModelClient):
    """在普通客户端上增加至多一次工具的两轮 Chat Completions 工具循环。"""

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: tuple[ToolDefinition, ...],
        execute: ToolExecutor,
        max_tool_calls: int,
        generation_is_current: Callable[[], bool],
        model_gate: Any | None = None,
        require_complete: bool = False,
        max_input_tokens: int | None = None,
    ) -> ToolCompletion:
        """执行至多一次工具的两轮 Chat Completions 工具循环。

        第一轮让模型自动决定是否调用工具并关闭并行调用；工具结果随后以
        ``role=tool`` 回传，第二轮明确设置 ``tool_choice=none``。MCP 内容由
        executor 负责清洗，本方法只把不可信字符串作为工具消息传递。

        `require_complete` / `max_input_tokens` 与 `complete` 同义（§53.9），默认关闭：
        发文显式传 True / 预算值，聊天路径的行为逐字节不变。首轮合法的 `tool_calls`
        可以继续协议；只有**最终**一轮必须正常 `stop`。
        """
        if not tools:
            raise ModelError("tools_unavailable", False)
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
            # 首轮允许的另一个完成原因是「模型要求工具」：它是协议内的中间态，
            # 只有最终成稿才必须正常收尾。
            accepted_finish_reasons=(
                _FINISH_STOP_OR_TOOL_CALLS if require_complete else None
            ),
            max_input_tokens=max_input_tokens,
        )
        self._check_generation(generation_is_current)
        if not first.tool_calls:
            if require_complete and first.finish_reason != "stop":
                # `tool_calls` 的宽限只对**真的带回了合法调用**的那一轮成立；没有调用时，
                # 完成原因不是正常收尾就与截断、拒答同类：稳定失败，不产出半篇正文。
                raise ModelError(_completion_kind(first.finish_reason), False)
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
            accepted_finish_reasons=(
                _FINISH_STOP_ONLY if require_complete else None
            ),
            max_input_tokens=max_input_tokens,
            stage=STAGE_TOOL_FOLLOWUP,
        )
        self._check_generation(generation_is_current)
        if not final.text:
            raise ModelError("empty", True, stage=STAGE_TOOL_FOLLOWUP)
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

    async def _create_tool_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: tuple[ToolDefinition, ...],
        tool_choice: str,
        parallel_tool_calls: bool,
        model_gate: Any | None,
        detect_tools_unsupported: bool,
        accepted_finish_reasons: frozenset[str] | None = None,
        max_input_tokens: int | None = None,
        stage: str = STAGE_FIRST_ROUND,
    ) -> "_ToolRound":
        """发起一轮带工具请求并复用现有错误重试映射。

        `stage` 区分首轮与工具后续轮：第一轮失败多半是提示词/请求本身的问题，
        后续轮失败则与上一轮的工具结果大小直接相关，两者的处置完全不同。
        """
        payload = tuple(self._tool_payload(tool) for tool in tools)
        attempt = 0
        while True:
            # 检查发生在请求序列化完成之后、网络调用之前，且对**每一轮**都生效：
            # 第二轮带着 assistant 工具调用参数与 tool 消息，体积通常比第一轮大得多。
            self._check_input_budget(messages, payload, max_input_tokens)
            started = time.monotonic()
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
            except Exception as exc:  # 任何异常都必须映射为 ModelError
                if detect_tools_unsupported and _is_tools_unsupported(exc):
                    # 只有结构化、可识别的「端点不接受 tools」才归入该稳定 kind：
                    # 它是**本次调用**的结论，不写任何实例状态、不重试（与原语义一致）。
                    # 普通 400/404 走下面的通用映射（bad_request），只终结当前请求。
                    raise ModelError(
                        "tools_unsupported",
                        False,
                        http_status=_status_code(exc),
                        stage=stage,
                        duration_ms=elapsed_ms(started),
                    ) from exc
                error = self._map_error(
                    exc,
                    stage=stage,
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
            # 与 `complete` 同一条理由：严格完成检查在异常映射之外，稳定且不重试。
            self._ensure_complete(response, accepted_finish_reasons)
            if not result.text and not result.tool_calls:
                error = ModelError(
                    "empty",
                    True,
                    stage=stage,
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
            return _ToolRound("", (), {"role": "assistant", "content": None}, None)
        finish_reason = cls._field(choices[0], "finish_reason")
        if not isinstance(finish_reason, str) or not finish_reason:
            finish_reason = None
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
        return _ToolRound(text, tuple(calls), assistant, finish_reason)


# 可重试错误最多重试一次（总共两次调用），与 core.worker 的 `complete` 同一策略。
_MAX_ATTEMPTS: int = 2
