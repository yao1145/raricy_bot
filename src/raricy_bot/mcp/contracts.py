"""MCP 与模型工具循环共享的领域合同。"""

from __future__ import annotations

import time
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

# `BaseExceptionGroup` 展开的深度上限：三层足够剥掉 SDK 的包装，再多说明结构异常。
_ERROR_UNWRAP_DEPTH = 3

# MCP 阶段名（INTERFACES §21）。进日志的是这几个字符串，不是自由文本；
# 放在 contracts 里是为了让 runtime 与 registry 引用同一份取值而不互相导入。
STAGE_CONNECT = "connect"
STAGE_DISCOVER = "discover"
STAGE_CALL = "call"
STAGE_CLOSE = "close"


def elapsed_ms(started: float) -> int:
    """`time.monotonic()` 起点到现在的毫秒数；负数（时钟被注入成回退值）截到 0。"""
    return max(0, int((time.monotonic() - started) * 1000))


def describe_error(exc: BaseException) -> dict[str, object]:
    """把异常压成一组**受限**的稳定字段，不再是正文。

    在此之前这里返回 ``"MCPError(REQUEST_TIMEOUT: ...)"`` 这样的自由文本，理由是
    「只有 ``error=MCPError`` 时各种失败长得一模一样」。那个理由是真的，做法不对：
    正文来自上游，可能带密钥或用户内容，而 ``RedactingFilter`` 的精确字符串替换
    并不承诺识别 URL 编码、JSON 转义或跨截断边界拆开的秘密（计划 §3.3）。

    可诊断的部分本来也不在正文里，而在**结构化**的信息上：异常类型、
    JSON-RPC 数字错误码、子进程退出码。这些全部保留，正文一个字都不留。
    调用点写成 ``**describe_error(exc)`` 即可。

    返回值经 ``log_event`` 的类型约束二次过滤，这里不必自己判断字段该不该写。
    """
    leaf = _leaf_exception(exc)
    fields: dict[str, object] = {"error": type(leaf).__name__}
    code = getattr(leaf, "code", None)
    if isinstance(code, (int, str)):
        fields["code"] = code
    exit_code = getattr(leaf, "exit_code", None)
    if isinstance(exit_code, int):
        fields["exit_code"] = exit_code
    return fields


def _leaf_exception(exc: BaseException, *, depth: int = 0) -> BaseException:
    """展开有界的 ``BaseExceptionGroup``，取第一条子异常。

    只走 ExceptionGroup 这一层：``ExceptionGroup`` 这个名字本身不携带任何信息，
    而子异常（``MCPError`` / ``FileNotFoundError`` …）才是真正的原因。深度设上限
    是为了让嵌套分组无法把展开变成一次无界的遍历。
    """
    if depth >= _ERROR_UNWRAP_DEPTH:
        return exc
    sub = getattr(exc, "exceptions", None)
    if isinstance(sub, tuple) and sub:
        first = sub[0]
        if isinstance(first, BaseException):
            return _leaf_exception(first, depth=depth + 1)
    return exc
