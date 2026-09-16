"""MCP 与模型工具循环共享的领域合同。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolDefinition:
    """模型可见的工具定义。"""

    server_name: str
    tool_name: str
    model_name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """模型请求执行的工具调用。"""

    call_id: str
    model_name: str
    arguments_json: str


@dataclass(frozen=True)
class ToolExecution:
    """经过清洗、可安全交给模型的工具结果。"""

    call_id: str
    content: str
    is_error: bool
    error_kind: str | None = None
    history_context: str | None = None


@dataclass(frozen=True)
class ToolCompletion:
    """工具循环结束后的模型正文及可持久化上下文。"""

    text: str
    used_tools: tuple[str, ...] = ()
    history_context: str | None = None


class McpProvider(Protocol):
    """一种 MCP 传输的生命周期和工具调用接口。"""

    @property
    def available(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def list_tools(self) -> tuple[ToolDefinition, ...]: ...

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        should_run: Callable[[], bool] | None = None,
    ) -> Any: ...


class McpCallTimeoutError(TimeoutError):
    """MCP 工具调用超过配置时限；供 Registry 映射稳定错误码。"""


class McpProviderUnavailable(RuntimeError):
    """一次尝试都还没发生就没有可用 Provider（池里零个 ready 槽位）。

    它与「真的超时」是不同的事实：没有超时，也没有失败的上游调用，只是当前没有
    可以发起调用的槽位。因此它只带稳定类型、不带池结构或上游正文，也**不**触发
    Registry 的整台 Provider 重连——池自己的后台恢复任务会处理。
    """


class McpCallCancelled(Exception):
    """池或 Registry 在尝试前发现本轮已被作废；不得映射为故障。"""


class McpNoResultsError(ValueError):
    """上游返回了内容，但没有一条结果通过安全解析。

    继承 ``ValueError`` 是为了让「没有结果」与「结果格式不认识」在 Registry 里能分开映射：
    前者是 ``no_results``（上游确实没查到），后者是 ``invalid_result``（我们不敢用）。
    适配器各自的名字（如 ``ExaNoResultsError``）都是它的别名。
    """


ToolExecutor = Callable[[ToolCall], Awaitable[ToolExecution]]


# 异常细节进日志的上限（字符）。MCP 侧的异常正文要么是 SDK 的固定短语，
# 要么是上游返回的一小段 JSON-RPC 错误，几百字符足够；设上限是为了让
# 一条异常无法把日志行撑爆。
ERROR_DETAIL_CHARS = 300


def describe_error(exc: BaseException) -> str:
    """把异常压成一行可 grep 的细节，供 MCP 生命周期与发现失败使用。

    在此之前只记 ``type(exc).__name__``，于是 ``MCPError`` 的 code 与 message
    全部丢失 —— 而「连接被对端关闭」和「服务端回了 JSON-RPC 错误」在日志里
    长得一模一样。2026-09-16 排查 wolfram 时正是卡在这里：日志只说
    ``error=MCPError``，真正的原因（子进程模块解析失败、当场退出）在另一处。

    正文来自上游，可能含密钥；调用方不必在意 —— 日志层的 ``RedactingFilter``
    会在写出前统一抹掉。这里只负责把它压成单行并限长。
    """
    name = type(exc).__name__
    text = " ".join(str(exc).split())[:ERROR_DETAIL_CHARS]
    code = getattr(exc, "code", None)
    if not text:
        return f"{name}({code})" if code is not None else name
    if code is None:
        return f"{name}: {text}"
    return f"{name}({code}: {text})"
