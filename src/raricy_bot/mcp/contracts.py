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


class McpCallCancelled(Exception):
    """池或 Registry 在尝试前发现本轮已被作废；不得映射为故障。"""


ToolExecutor = Callable[[ToolCall], Awaitable[ToolExecution]]
