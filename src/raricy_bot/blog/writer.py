"""定时发文的生成器（INTERFACES §53.9）。

一次 `write()` 就是**一次**两轮工具协议：system 是静态写作规则
（`texts.BLOG_WRITE_SYSTEM_PROMPT`），user 是任务提示词，工具结果以 `role="tool"` 回传并
就地标明不可信（`texts.BLOG_WRITE_TOOL_UNTRUSTED_PREFIX`）。首轮直接给出文章同样合法；
至多执行一次合法工具调用，第二轮 `tool_choice="none"`。**不做多步研究循环**，也不在模型
客户端的既有有界重试之外重试（`max_retries=0` 不变）。

输出只经 `blog.codec.parse_draft` 变成 `Draft` 返回：不截断、不脱敏、不落稿、不选栏目、
不调用 Publisher —— 脱敏与预校验由 Service 在拿到 `Draft` 之后做**一次**（§53.6、§53.11）。
失败抛 `ModelError`（稳定 kind，来自 `core/worker.py`）或 `DraftError`；正文与提示词
都不进日志，日志里只有稳定 token 与计数。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Any

from .. import texts
from ..core.worker import (
    KIND_STRICT_UNSUPPORTED,
    KIND_TIMEOUT,
    ModelError,
)
from ..logging_setup import get_logger, log_event
from ..mcp.contracts import ToolCall, ToolDefinition, ToolExecution
from .codec import parse_draft
from .models import Draft

_logger = get_logger("blog.writer")

# 能力名：`mcp.features.blog_write`、`capabilities.CAPABILITY_BY_FEATURE` 与
# `mcp/adapters.py` 的工厂表用的是同一个字符串。
FEATURE_NAME = "blog_write"

# 整次 `write()` 的墙钟上限（秒，代码常量）：等 gate、工具执行与模型的全部时间都在内。
# 它不是配置项 —— 一个能把它调大的旋钮只会让关闭流程更久地卡在一个写手上。
WRITE_TIMEOUT_SECONDS: float = 180.0


class BlogWriter:
    """生成一篇待发布的文章；只返回 `Draft`，不碰任何持久状态。

    构造参数：

    - `model`：模型客户端（实现 `complete`；可选实现 `complete_with_tools`）；
    - `registry`：MCP 工具注册表（`McpManager.registry`），未启用 MCP 时为 None；
    - `feature`：`config.mcp.features` 里的 `blog_write` 段，工具预算从它取；
    - `mcp_enabled`：`config.mcp.enabled`；未启用时直接走无工具生成；
    - `max_input_tokens`：本轮请求的输入上限（装配时传 `behavior.context_input_tokens`）；
    - `model_gate`：App 共享的并发门，只在**每次模型 HTTP 请求**期间持有；
    - `sleep`：计时器注入点（默认 `asyncio.sleep`），用来施加 180 秒上限；
    - `timeout_seconds`：整次 `write()` 的上限，默认就是上面的常量。
    """

    def __init__(
        self,
        *,
        model: Any,
        registry: Any | None = None,
        feature: Any | None = None,
        mcp_enabled: bool = False,
        max_input_tokens: int | None = None,
        model_gate: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout_seconds: float = WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._model = model
        self._registry = registry
        self._feature = feature
        self._mcp_enabled = mcp_enabled
        self._max_input_tokens = max_input_tokens
        self._model_gate = model_gate
        self._sleep = sleep
        self._timeout_seconds = timeout_seconds
        # 工具预算**不新增配置项**：能力层是工具白名单与预算的唯一真值源（§53.9）。
        # 配置校验把它钉死为 1；这里的默认值只服务于手工构造的对象。
        self._max_tool_calls = int(
            getattr(feature, "max_tool_calls_per_turn", 1) or 1
        )

    async def write(self, task: Any) -> Draft:
        """生成一个 `Draft`；可能抛 `ModelError` / `DraftError`。

        每个调度点最多调用一次：失败就是失败，没有同点重试旋钮（§8.3）。
        """
        prompt = getattr(task, "prompt", None)
        if not isinstance(prompt, str) or not prompt.strip():
            # 只有现写任务会调用 write()，而配置层保证 prompt 非空。真走到这里说明调用方
            # 把稿库任务当成了现写任务 —— 这是编程错误，直接抛出去比拿空提示词写一篇文章好。
            raise ValueError("blog task without prompt")

        tools, declined = self._tool_plan()
        if tools is None:
            log_event(_logger, logging.DEBUG, "blog.write_no_tools", reason=declined)
            text = await self._within_deadline(self._generate_without_tools(prompt))
        else:
            text = await self._within_deadline(self._generate_with_tools(prompt, tools))
        return parse_draft(text)

    # --- 路径选择与降级 -----------------------------------------------------

    def _tool_plan(self) -> tuple[tuple[ToolDefinition, ...] | None, str]:
        """这一轮能不能走工具协议；不能时给出稳定原因（只进日志）。

        四类**调用前已知**的条件都在这里判定：MCP 未启用、没配 `blog_write`、工具不可用、
        客户端已缓存「不支持 tools」。任何一条成立都直接以无工具方式生成一次（§53.9）。
        """
        if not self._mcp_enabled:
            return None, "mcp_disabled"
        if self._feature is None:
            return None, "feature_unconfigured"
        if self._registry is None:
            return None, "registry_missing"
        if getattr(self._model, "tools_unsupported", False):
            # 端点已经用 400/404 明确拒绝过 tools：不必再撞一次，直接无工具生成。
            return None, "tools_unsupported"
        if not callable(getattr(self._model, "complete_with_tools", None)):
            return None, "client_without_tools"
        if not self._registry.feature_available(FEATURE_NAME):
            return None, "feature_unavailable"
        tools = tuple(self._registry.tools_for(FEATURE_NAME))
        if not tools:
            return None, "no_tools"
        return tools, ""

    # --- 两条生成路径 -------------------------------------------------------

    async def _generate_with_tools(
        self, prompt: str, tools: tuple[ToolDefinition, ...]
    ) -> str:
        """两轮工具协议；模型不调用工具时就是一次普通生成。"""
        complete_with_tools = getattr(self._model, "complete_with_tools")
        if not _accepts_strict_calls(complete_with_tools):
            # 拿不到严格完成检查就等于可能发布半篇正文：**明确失败**，不静默降级。
            raise ModelError(KIND_STRICT_UNSUPPORTED, False)
        try:
            completion = await complete_with_tools(
                self._messages(prompt),
                tools=tools,
                execute=self._execute,
                max_tool_calls=self._max_tool_calls,
                # 发文没有会话代次（聊天侧的 `/reset` 概念）——这一轮永远算「当前」。
                generation_is_current=_always_current,
                model_gate=self._model_gate,
                require_complete=True,
                max_input_tokens=self._max_input_tokens,
            )
        except ModelError as exc:
            if exc.kind == "bad_request" and getattr(
                self._model, "tools_unsupported", False
            ):
                # **首次调用**才发现端点不认 tools：结束本次生成，后续调度点再走无工具
                # 路径。同轮不降级、不重试、也不退回聊天的 `search` 授权（§53.9）。
                raise ModelError("tools_unsupported", False) from exc
            raise
        return completion.text

    async def _generate_without_tools(self, prompt: str) -> str:
        """无工具生成一次；严格完成检查与输入预算一样不能少。"""
        complete = getattr(self._model, "complete", None)
        if not callable(complete) or not _accepts_strict_calls(complete):
            raise ModelError(KIND_STRICT_UNSUPPORTED, False)
        messages = self._messages(prompt)
        async with self._gate():
            return await complete(
                messages,
                require_complete=True,
                max_input_tokens=self._max_input_tokens,
            )

    @staticmethod
    def _messages(prompt: str) -> list[dict[str, Any]]:
        """消息分工固定：静态写作规则进 system，任务提示词进 user。

        任务提示词与工具正文都**不得**拼进 system —— 它们是数据，不是规则。
        """
        return [
            {"role": "system", "content": texts.BLOG_WRITE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

    async def _execute(self, call: ToolCall) -> ToolExecution:
        """执行至多一次工具调用，并就地标明结果不可信（§53.9）。

        历史上下文一律丢弃：原始 MCP 内容不进 SQLite 也不进任何持久历史（§21.2）。
        """
        execution = await self._registry.execute(FEATURE_NAME, call)
        return ToolExecution(
            call_id=execution.call_id,
            content=f"{texts.BLOG_WRITE_TOOL_UNTRUSTED_PREFIX}\n{execution.content}",
            is_error=execution.is_error,
            error_kind=execution.error_kind,
            history_context=None,
        )

    def _gate(self) -> Any:
        """只在模型 HTTP 请求期间持有共享并发门；等 MCP 与工具执行都不占门（§53.9）。"""
        return self._model_gate if self._model_gate is not None else nullcontext()

    async def _within_deadline(self, operation: Awaitable[Any]) -> Any:
        """给整次 `write()` 施加墙钟上限（含等 gate、工具与模型）。

        计时用注入的 `sleep`（默认即 `asyncio.sleep`）：测试传一个立即返回的替身就能零耗时
        走完整条超时路径，而不必真等 180 秒。超时是稳定失败，不是未分类异常。
        """
        work = asyncio.ensure_future(operation)
        timer = asyncio.ensure_future(self._sleep(self._timeout_seconds))
        try:
            done, _pending = await asyncio.wait(
                {work, timer}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # 外部取消（Service.stop）必须原样传播；先收掉两个子任务再抛，不留悬挂任务。
            work.cancel()
            timer.cancel()
            await asyncio.gather(work, timer, return_exceptions=True)
            raise
        if work in done:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            return work.result()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        raise ModelError(KIND_TIMEOUT, False)


def _always_current() -> bool:
    """发文这一轮没有会话代次；恒真让 Registry 与池的取消检查保持原样。"""
    return True


def _accepts_strict_calls(callable_: Any) -> bool:
    """客户端是否接受 §53.9 的两个严格参数（与 app 探测 `model_gate` 同一手法）。

    两个参数都必须**显式声明**：只有完成检查而没有输入预算，或者反过来，都意味着有一条
    保证被静默吞掉。只靠 `**kwargs` 收下这两个参数的外壳同样不算 —— 它可以在内部把它们
    丢掉而调用照样成功，正是要挡住的那种静默吞掉，所以这里不认 `VAR_KEYWORD`。
    探不到签名（某些代理对象）时按**不支持**处理 —— 发文宁可明确失败，也不拿一个
    无法确认成稿完整性的客户端去发布文章。
    """
    try:
        signature = inspect.signature(callable_)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    return "require_complete" in parameters and "max_input_tokens" in parameters
