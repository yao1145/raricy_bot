"""知识库的模型工具：普通聊天里由模型按需调用的一次本地检索（设计 §调用与授权、§预算）。

知识库是本地只读能力，**不是 MCP**（D-41）：这里不注册 feature、不扩大任何上游工具白名单，
也不联网。`KB_TOOL_DEFINITION` 是给模型的工具定义，`KnowledgeTool` 是它的执行器；工具名与查询串
上限取自 `capabilities.KB_MODEL_TOOL_NAME` / `KB_MODEL_MAX_QUERY_CHARS`，schema、参数校验与
装配共用同一份取值。

工具结果带分类、相对路径与标题，这些是**用户可见数据**：只经 `role="tool"` 交给模型，
一条都不写进日志、也不插值进 system。结果里的动态文本只有两种去处：模型看到的 role="tool"
消息与模型自己写的回答。

**为什么这里另有两份结构定义**：本模块不能导入 `mcp.contracts`。`kb/` 属于 Light 闭包，
`tools/build_light.py` 会拒绝 staged 源码里的任何 MCP 导入，而 `app.py` 两种版本共用、本身
也不导入 MCP（工具能力靠 `getattr(model, "complete_with_tools", None)` 探测）。因此
`KBToolDefinition` / `KBToolExecution` 与 `mcp.contracts` 的两个类型字段同名同义，消费者一律按
属性读取；字段对照由 `tests/test_kb_tool.py` 钉住，任一侧改字段名都会当场失败。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..capabilities import KB_MODEL_MAX_QUERY_CHARS, KB_MODEL_TOOL_NAME
from ..core.worker import ToolGenerationCancelled
from ..logging_setup import get_logger, log_event
from ..text_utils import estimate_tokens
from ..texts import KB_TOOL_DESCRIPTION, TRUNCATION_SUFFIX
from .service import STATUS_NO_RESULTS, STATUS_OK

logger = get_logger("kb")

# 工具协议里的固定短句：只说明发生了什么，不回显查询、分类、路径、标题或命中原文。
# 它们进的是 role="tool" 消息，不是用户可见文案；与 `mcp/registry.py` 的同类短句一样就地定义。
NOT_ALLOWED_CONTENT: str = "tool not allowed"
DENIED_CONTENT: str = "knowledge search not allowed"
INVALID_ARGUMENTS_CONTENT: str = "invalid arguments"
UNAVAILABLE_CONTENT: str = "knowledge search unavailable"
BUDGET_CONTENT: str = "knowledge search budget exhausted"
NO_RESULTS_CONTENT: str = "no matching local knowledge base entries"

# 稳定的失败分类：日志的 `reason` 与返回给模型的 `error_kind` 逐字一致，便于按一条字符串
# 同时 grep 日志与推演行为（与 `mcp/registry.py` 同一约定）。`tool_budget_exhausted`
# 与模型工具循环自己的预算耗尽同名：含义相同，都是「本轮工具额度已用完，不要重试」。
ERROR_NOT_ALLOWED: str = "tool_not_allowed"
ERROR_DENIED: str = "kb_access_denied"
ERROR_INVALID_ARGUMENTS: str = "invalid_arguments"
ERROR_UNAVAILABLE: str = "kb_unavailable"
ERROR_BUDGET: str = "tool_budget_exhausted"


class ToolCallLike(Protocol):
    """一次工具调用在本模块需要的最小形状（与 `mcp.contracts.ToolCall` 结构兼容）。"""

    call_id: str
    model_name: str
    arguments_json: str


@dataclass(frozen=True)
class KBToolDefinition:
    """模型侧工具定义；字段与 `mcp.contracts.ToolDefinition` 同名同义。"""

    server_name: str
    tool_name: str
    model_name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class KBToolExecution:
    """一次工具执行的清洗结果；字段与 `mcp.contracts.ToolExecution` 同名同义。

    `history_context` 恒为 `None`：知识库命中原文只属当前轮，不进历史（D-43）。
    """

    call_id: str
    content: str
    is_error: bool
    error_kind: str | None = None
    history_context: str | None = None


# 工具定义。`server_name` 只是本地伪名（不参与任何 MCP 查表，D-41）；模型只看得见 `model_name`。
# `additionalProperties: false` 与执行期校验同口径：schema 是给模型的合同，执行期的严格解析
# 才是真正的边界。字段说明与 MCP 适配器（如 `mcp/exa.py` 的 `model_input_schema`）一样就地写在
# schema 里；工具本身的介绍文案在 texts 里。
KB_TOOL_DEFINITION: KBToolDefinition = KBToolDefinition(
    server_name="kb",
    tool_name=KB_MODEL_TOOL_NAME,
    model_name=KB_MODEL_TOOL_NAME,
    description=KB_TOOL_DESCRIPTION,
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要检索的本地资料问题或关键词",
                "minLength": 1,
                "maxLength": KB_MODEL_MAX_QUERY_CHARS,
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)


class KnowledgeTool:
    """把现有的 `KnowledgeService` 包成一次模型工具调用。

    只依赖 service 的 `permits` 与 `search`：注入替身（或另一版本的 `KnowledgeService`）
    没有 `available`、`status_snapshot` 也照样能用。「索引是否可用」由 app 在装配时判定 ——
    它只是读取本地快照的事实，不联网，也不该在这里被伪装成一次工具失败。

    `generation_is_current` 是**唯一**的取消入口，代次与到期都由它承载（app 的回调同时复查
    两者）：回调返回假即抛 `ToolGenerationCancelled`，由模型客户端的工具循环原样传播，
    本轮不发送、不留历史。`max_result_tokens` 是一次调用能交给模型的 token 上限，零预算
    仍然可以构造，但每一次调用都会被拒绝 —— 那表示本轮根本没有空间放工具结果。
    """

    def __init__(
        self,
        service: Any,
        *,
        channel_kind: str,
        user_id: str,
        generation_is_current: Callable[[], bool],
        max_result_tokens: int,
    ) -> None:
        if isinstance(max_result_tokens, bool) or not isinstance(max_result_tokens, int):
            raise ValueError("max_result_tokens 必须是非负整数")
        if max_result_tokens < 0:
            raise ValueError("max_result_tokens 必须是非负整数")
        self._service = service
        self._channel_kind = channel_kind
        self._user_id = user_id
        self._generation_is_current = generation_is_current
        self._max_result_tokens = max_result_tokens

    async def execute(self, call: ToolCallLike) -> KBToolExecution:
        """执行一次工具调用；业务上的拒绝一律返回固定文案，不抛出。

        唯一的异常是 `ToolGenerationCancelled`：它表示这一轮已被 `/reset` 或到期作废。
        拒绝路径都不泄露目录、分类与是否命中（D-44），也不把原始参数或异常文本交给模型。
        """
        if call.model_name != KB_MODEL_TOOL_NAME:
            return self._decline(call, ERROR_NOT_ALLOWED, NOT_ALLOWED_CONTENT)
        if self._max_result_tokens <= 0:
            return self._decline(call, ERROR_BUDGET, BUDGET_CONTENT)
        self._ensure_current()
        # 访问门在检索之前：拒绝时不读目录、不产生任何检索副作用（与 `/kb` 同序，D-44）。
        if not self._permits():
            return self._decline(call, ERROR_DENIED, DENIED_CONTENT)
        query = _parse_query(call.arguments_json)
        if query is None:
            # 参数非法等于这次候选没有真正执行（与 registry 同一分类），工具循环因此仍可
            # 让同一轮的另一个候选竞争本轮唯一的预算。
            return self._decline(
                call,
                ERROR_INVALID_ARGUMENTS,
                INVALID_ARGUMENTS_CONTENT,
                level=logging.INFO,
            )
        self._ensure_current()
        result = await self._service.search(query)
        # 检索是异步边界：期间可能发生 `/reset`、到期或权限撤销。结果本身是「这一次授权」
        # 的产物，因此必须在它回到模型之前复查取消与门禁。
        self._ensure_current()
        if not self._permits():
            return self._decline(call, ERROR_DENIED, DENIED_CONTENT)
        if result.status == STATUS_OK:
            content = self._bounded(result.text)
            if not content:
                # 预算连截断提示都装不下：宁可声明预算不足，也不超预算地塞进正文。
                return self._decline(call, ERROR_BUDGET, BUDGET_CONTENT)
            self._log_done(result.block_count, result.snapshot_version)
            return KBToolExecution(
                call_id=call.call_id,
                content=content,
                is_error=False,
                history_context=None,
            )
        if result.status == STATUS_NO_RESULTS:
            # 「没查到」是一次成功的工具调用，不是故障：如实告诉模型，让它自己说明资料不足。
            self._log_done(0, result.snapshot_version)
            return KBToolExecution(
                call_id=call.call_id,
                content=NO_RESULTS_CONTENT,
                is_error=False,
                history_context=None,
            )
        # 关闭或索引不可用：只给固定状态，不返回原异常或任何目录事实。
        return self._decline(call, ERROR_UNAVAILABLE, UNAVAILABLE_CONTENT)

    def _permits(self) -> bool:
        """访问门：频道类型与稳定用户 ID（D-44）；服务自身返回布尔。"""
        return bool(
            self._service.permits(
                channel_kind=self._channel_kind, user_id=self._user_id
            )
        )

    def _ensure_current(self) -> None:
        """复查代次与到期；作废即抛 `ToolGenerationCancelled`，不产出任何结果。"""
        if not self._generation_is_current():
            raise ToolGenerationCancelled()

    def _bounded(self, text: str) -> str:
        """把结果收口到本次工具预算内；必要时截断并补上静态提示。

        预算小到连截断提示都装不下时返回空串，由调用方按预算不足收口：宁可什么都不给，
        也不超预算。
        """
        if estimate_tokens(text) <= self._max_result_tokens:
            return text
        room = self._max_result_tokens - estimate_tokens(TRUNCATION_SUFFIX)
        if room <= 0:
            return ""
        return _clip_tokens(text, room) + TRUNCATION_SUFFIX

    def _log_done(self, count: int, snapshot_version: int) -> None:
        """成功事件只记条数与快照版本；分类、路径、标题与正文一个都不进日志。"""
        log_event(
            logger,
            logging.INFO,
            "kb.tool_done",
            channel_kind=self._channel_kind,
            count=count,
            snapshot_version=snapshot_version,
        )

    def _decline(
        self,
        call: ToolCallLike,
        error_kind: str,
        content: str,
        *,
        level: int = logging.WARNING,
    ) -> KBToolExecution:
        """记一条只带稳定原因的失败事件，并返回固定文案。

        `reason` 与返回给模型的 `error_kind` 同名；查询、路径、标题与命中原文在这里一样
        都不出现（模型自报的工具名与参数都是它自己拼的字符串，不属于可写日志的字段）。

        参数不合法用 INFO：那是模型写错了一次参数，不是故障，也不该刷成告警。
        """
        log_event(
            logger,
            level,
            "kb.tool_failed",
            reason=error_kind,
            channel_kind=self._channel_kind,
        )
        return KBToolExecution(call.call_id, content, True, error_kind)


def _parse_query(arguments_json: str) -> str | None:
    """严格解析工具参数：恰好一个 `query` 字符串，去首尾空白后 1..上限。

    多余字段、错误类型、非对象 JSON、异常 JSON、空串与超长一律返回 None：参数是模型
    自己拼的，宽松解析等于把「它写错了一个字段」变成一次真实的资料外送。
    """
    try:
        arguments = json.loads(arguments_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(arguments, dict) or set(arguments) != {"query"}:
        return None
    query = arguments["query"]
    if not isinstance(query, str):
        return None
    query = query.strip()
    if not 1 <= len(query) <= KB_MODEL_MAX_QUERY_CHARS:
        return None
    return query


def _clip_tokens(text: str, limit: int) -> str:
    """按 token 估算做确定性、有界的保守截断。

    与 `mcp/registry.py` 的同名私有函数同一口径，但**不能**从那里导入：`kb/` 在 Light 闭包内，
    源码里出现任何 MCP 导入都会被 `tools/build_light.py` 拒绝。
    """
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()


__all__ = [
    "BUDGET_CONTENT",
    "DENIED_CONTENT",
    "INVALID_ARGUMENTS_CONTENT",
    "KB_TOOL_DEFINITION",
    "KBToolDefinition",
    "KBToolExecution",
    "KnowledgeTool",
    "NO_RESULTS_CONTENT",
    "NOT_ALLOWED_CONTENT",
    "ToolCallLike",
    "UNAVAILABLE_CONTENT",
]
