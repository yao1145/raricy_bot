"""MCP 工具发现、命名空间和 feature 白名单。

适配器的键是 **(feature 名, 模型侧工具名)** 这一对（INTERFACES §21.2）：同一个上游工具
可以被两个 feature 绑定（`search` 与 `blog_write` 都用 `web_search_exa`），而两边的参数
上限、结果数、schema 与限流策略各不相同。按单独的模型侧工具名查表时，后装配的适配器会
覆盖前一个 —— 配置里写在哪一边就不再决定行为（D-110）。**不保留跨 feature 的回退查询。**
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from ..config import McpFeatureConfig
from ..logging_setup import get_logger, log_event
from ..text_utils import estimate_tokens
from .contracts import (
    STAGE_CALL,
    STAGE_DISCOVER,
    McpCallCancelled,
    McpCallTimeoutError,
    McpProvider,
    McpProviderUnavailable,
    ToolCall,
    ToolDefinition,
    ToolExecution,
    describe_error,
    elapsed_ms,
)
from .adapter_kit import CapabilityLimiter
from .contracts import McpNoResultsError

_MODEL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")

# 适配器表的键：feature 名 + 模型侧工具名。写在类型别名里，让注入点与查表点
# 不可能各自漂移成一个字符串键。
AdapterKey = tuple[str, str]

_logger = get_logger("mcp.registry")


def model_tool_name(server_name: str, tool_name: str) -> str:
    """将 MCP 原始名称转换为模型工具名称。"""
    return f"{_MODEL_NAME_RE.sub('_', server_name)}__{_MODEL_NAME_RE.sub('_', tool_name)}"


class InMemoryToolRegistry:
    """管理 Provider 快照，并在执行前重新检查 feature 白名单。"""

    def __init__(
        self,
        providers: Mapping[str, McpProvider],
        features: Mapping[str, McpFeatureConfig],
        adapters: Mapping[AdapterKey, Callable[[Any, str], ToolExecution]] | None = None,
        on_provider_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._features = dict(features)
        self._adapters = dict(adapters or {})
        self._on_provider_failure = on_provider_failure
        self._tools: dict[str, ToolDefinition] = {}
        self._conflicts: set[str] = set()
        self._last_refresh_failures: set[str] = set()

    async def refresh(self) -> bool:
        """从所有可用 Provider 原子重建工具快照；返回发现是否完整成功。"""
        discovered: dict[str, ToolDefinition] = {}
        conflicts: set[str] = set()
        failures: set[str] = set()
        for server_name, provider in self._providers.items():
            if not provider.available:
                log_event(
                    _logger,
                    logging.DEBUG,
                    "mcp.discovery_skipped",
                    server=server_name,
                    reason="provider_unavailable",
                )
                continue
            started = time.monotonic()
            try:
                definitions = await provider.list_tools()
            except Exception as exc:
                self._notify_provider_failure(server_name)
                failures.add(server_name)
                log_event(
                    _logger,
                    logging.WARNING,
                    "mcp.discovery_failed",
                    server=server_name,
                    stage=STAGE_DISCOVER,
                    duration_ms=elapsed_ms(started),
                    **describe_error(exc),
                )
                continue
            accepted = 0
            for definition in definitions:
                try:
                    tool_name = definition.tool_name
                    description = definition.description
                    input_schema = definition.input_schema
                except AttributeError:
                    continue
                if not isinstance(tool_name, str) or not tool_name.strip():
                    continue
                if not isinstance(input_schema, Mapping):
                    input_schema = {}
                model_name = model_tool_name(server_name, tool_name)
                normalized = ToolDefinition(
                    server_name=server_name,
                    tool_name=tool_name,
                    model_name=model_name,
                    description=description if isinstance(description, str) else "",
                    input_schema=dict(input_schema),
                )
                if model_name in discovered:
                    conflicts.add(model_name)
                else:
                    discovered[model_name] = normalized
                    accepted += 1
            # "连上了但一个工具都没发现"是事故现场最容易踩的坑之一：
            # 它让 feature 判为不可用，而此前日志里没有任何痕迹。
            log_event(
                _logger,
                logging.INFO,
                "mcp.discovered",
                server=server_name,
                count=accepted,
            )
        self._tools = discovered
        self._conflicts = conflicts
        self._last_refresh_failures = failures
        for conflict in sorted(conflicts):
            log_event(_logger, logging.WARNING, "mcp.tool_conflict", tool=conflict)
        return not failures

    @property
    def last_refresh_failures(self) -> frozenset[str]:
        """最近一次发现失败的服务器名；只供生命周期管理器安排重连。"""
        return frozenset(self._last_refresh_failures)

    def tools_for(self, feature_name: str) -> tuple[ToolDefinition, ...]:
        """返回 feature 已绑定且当前已发现的工具。"""
        feature = self._features.get(feature_name)
        if feature is None or not feature.enabled:
            return ()
        result: list[ToolDefinition] = []
        for binding in feature.bindings:
            name = model_tool_name(binding.server, binding.tool)
            definition = self._tools.get(name)
            if definition is None or name in self._conflicts:
                continue
            if definition.server_name == binding.server and definition.tool_name == binding.tool:
                # 双键查表：同一模型侧工具名在不同 feature 下是两份策略，绝不互相回退。
                adapter = self._adapters.get((feature_name, name))
                schema_provider = getattr(_adapter_target(adapter), "model_input_schema", None)
                if callable(schema_provider):
                    try:
                        definition = replace(
                            definition, input_schema=dict(schema_provider())
                        )
                    except Exception:
                        continue
                result.append(definition)
        return tuple(result)

    def feature_available(self, feature_name: str) -> bool:
        """判断 feature 是否启用且全部绑定工具可用。"""
        feature = self._features.get(feature_name)
        if feature is None or not feature.enabled or not feature.bindings:
            return False
        if any(
            not self._providers.get(binding.server, _UnavailableProvider()).available
            for binding in feature.bindings
        ):
            return False
        return len(self.tools_for(feature_name)) == len(feature.bindings)

    async def execute(
        self,
        feature_name: str,
        call: ToolCall,
        *,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> ToolExecution:
        """校验调用归属并执行；所有拒绝结果都不包含原始参数。"""
        definition = {item.model_name: item for item in self.tools_for(feature_name)}.get(
            call.model_name
        )
        if definition is None:
            # 这里刻意不记录 call.model_name：那是模型自报的字符串，
            # 可能包含它复述的用户正文，不属于可以进日志的字段。
            return self._decline(
                call, "tool_not_allowed", "tool_not_allowed", feature=feature_name
            )
        try:
            arguments = json.loads(call.arguments_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._decline(
                call, "invalid_arguments", "invalid_arguments", definition=definition,
                feature=feature_name, level=logging.INFO,
            )
        if not isinstance(arguments, dict):
            return self._decline(
                call, "invalid_arguments", "invalid_arguments", definition=definition,
                feature=feature_name, level=logging.INFO,
            )
        adapter = self._adapters.get((feature_name, definition.model_name))
        # 适配器可以在 Provider 边界实现产品级参数策略。绑定 Exa 时，
        # 这里只接受 query，并由适配器强制写入配置中的 numResults；未来工具
        # 没有该钩子时仍保持通用 MCP 参数透传。
        preparer = getattr(_adapter_target(adapter), "prepare_arguments", None)
        if callable(preparer):
            try:
                arguments = preparer(arguments, self._features[feature_name])
            except (KeyError, TypeError, ValueError):
                return self._decline(
                    call, "invalid_arguments", "invalid_arguments",
                    definition=definition, feature=feature_name, level=logging.INFO,
                )
            if not isinstance(arguments, dict):
                return self._decline(
                    call, "invalid_arguments", "invalid_arguments",
                    definition=definition, feature=feature_name, level=logging.INFO,
                )
        provider = self._providers.get(definition.server_name)
        if provider is None or not provider.available:
            self._notify_provider_failure(definition.server_name, provider)
            return self._decline(
                call, "tool_unavailable", "tool unavailable",
                definition=definition, feature=feature_name,
            )

        async def call_provider() -> Any:
            # 多 Key 池要在槽位之间重查代次；单进程 Provider 没有轮换点，忽略即可。
            # 只接受两个位置参数的旧替身仍按原样调用（与 app 探测 model_gate 同一手法）。
            if generation_is_current is not None and _accepts_should_run(provider):
                return await provider.call_tool(
                    definition.tool_name, arguments, should_run=generation_is_current
                )
            return await provider.call_tool(definition.tool_name, arguments)

        limiter = getattr(_adapter_target(adapter), "limiter", None)
        # 从这里到返回是**调用阶段**：耗时只统计真正打出去的那一段，
        # 不把前面的参数校验与白名单拒绝算进去（它们没发生网络等待）。
        started = time.monotonic()
        try:
            if limiter is not None:
                raw = await limiter.run(
                    call_provider, should_run=generation_is_current
                )
                if raw is CapabilityLimiter.SKIPPED:
                    # 生成已被 /reset 作废：正常竞态，不是故障。
                    return self._decline(
                        call, "generation_cancelled", "capability cancelled",
                        definition=definition, feature=feature_name, level=logging.DEBUG,
                        duration_ms=elapsed_ms(started),
                    )
            else:
                if generation_is_current is not None and not generation_is_current():
                    return self._decline(
                        call, "generation_cancelled", "capability cancelled",
                        definition=definition, feature=feature_name, level=logging.DEBUG,
                        duration_ms=elapsed_ms(started),
                    )
                raw = await call_provider()
        except McpCallCancelled:
            # 池在换槽位之前发现本轮已被 `/reset` 作废：正常竞态，不是故障，
            # 也不该触发「整个 Provider 不可用」的重连。
            return self._decline(
                call, "generation_cancelled", "capability cancelled",
                definition=definition, feature=feature_name, level=logging.DEBUG,
                duration_ms=elapsed_ms(started),
            )
        except McpProviderUnavailable:
            # 零个可用槽位：一次尝试都没发生，不是超时。这是池自己的瞬态（槽位都在
            # 冷却里），由池的后台恢复任务处理，因此**不**通知 Manager 重连整个
            # Provider——那会把还在冷却的槽位提前拉起，属于额外升级。
            return self._decline(
                call, "tool_unavailable", "tool unavailable",
                definition=definition, feature=feature_name,
                duration_ms=elapsed_ms(started),
            )
        except McpCallTimeoutError:
            self._notify_provider_failure(definition.server_name, provider)
            return self._decline(
                call, "tool_timeout", "tool timed out",
                definition=definition, feature=feature_name,
                duration_ms=elapsed_ms(started),
            )
        except Exception:
            self._notify_provider_failure(definition.server_name, provider)
            return self._decline(
                call, "tool_unavailable", "tool unavailable",
                definition=definition, feature=feature_name,
                duration_ms=elapsed_ms(started),
            )
        if _result_is_error(raw):
            return self._decline(
                call, "tool_unavailable", "tool unavailable",
                definition=definition, feature=feature_name,
                duration_ms=elapsed_ms(started),
            )
        if adapter is not None:
            try:
                execution = adapter(raw, call.call_id)
            except McpNoResultsError:
                return self._decline(
                    call, "no_results", "no results",
                    definition=definition, feature=feature_name,
                    duration_ms=elapsed_ms(started),
                )
            except ValueError:
                return self._decline(
                    call, "invalid_result", "invalid_result",
                    definition=definition, feature=feature_name,
                    duration_ms=elapsed_ms(started),
                )
            except Exception:
                return self._decline(
                    call, "invalid_result", "invalid_result",
                    definition=definition, feature=feature_name,
                    duration_ms=elapsed_ms(started),
                )
            if not execution.is_error:
                log_event(
                    _logger, logging.INFO, "mcp.tool_done",
                    tool=definition.model_name, server=definition.server_name,
                    stage=STAGE_CALL, duration_ms=elapsed_ms(started),
                )
            return execution
        if isinstance(raw, str):
            content = raw
        else:
            try:
                content = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                return self._decline(
                    call, "invalid_result", "invalid_result",
                    definition=definition, feature=feature_name,
                    duration_ms=elapsed_ms(started),
                )
        # 未提供专用适配器的未来工具也不能把任意 MCP 响应无界地交给模型。
        # 以该 feature 的结果预算作为保守上限；专用适配器（如 Exa）在此之前
        # 已按更细的逐条规则处理。
        feature = self._features.get(feature_name)
        if feature is None:
            return self._decline(
                call, "invalid_result", "invalid_result",
                definition=definition, feature=feature_name,
                duration_ms=elapsed_ms(started),
            )
        max_tokens = max(1, feature.result_count * feature.result_item_token_limit)
        if estimate_tokens(content) > max_tokens:
            content = _clip_tokens(content, max_tokens)
        log_event(
            _logger, logging.INFO, "mcp.tool_done",
            tool=definition.model_name, server=definition.server_name,
            stage=STAGE_CALL, duration_ms=elapsed_ms(started),
        )
        return ToolExecution(call.call_id, content, False)

    def _decline(
        self,
        call: ToolCall,
        error_kind: str,
        content: str,
        *,
        definition: ToolDefinition | None = None,
        feature: str = "",
        level: int = logging.WARNING,
        duration_ms: int | None = None,
    ) -> ToolExecution:
        """记录一次未成功的调用并返回稳定错误结果。

        日志里的 ``reason`` 与返回给模型的 ``error_kind`` 同名，便于按一条
        稳定字符串同时 grep 日志与推演行为。工具参数、模型自报的工具名与
        上游返回正文都不在这里出现。

        ``duration_ms`` 只由**调用阶段之后**的拒绝路径传入：那之前的拒绝
        （白名单、参数不合法）没有发生过等待，记一个接近 0 的耗时会误导。
        """
        fields: dict[str, object] = {"reason": error_kind}
        if feature:
            fields["feature"] = feature
        if definition is not None:
            fields["tool"] = definition.model_name
            fields["server"] = definition.server_name
        if duration_ms is not None:
            fields["stage"] = STAGE_CALL
            fields["duration_ms"] = duration_ms
        log_event(_logger, level, "mcp.tool_failed", **fields)
        return ToolExecution(call.call_id, content, True, error_kind)

    def _notify_provider_failure(
        self, server_name: str, provider: McpProvider | None = None
    ) -> None:
        """通知生命周期管理器安排重连；回调异常不得影响用户请求。

        带 ``manages_own_recovery`` 标记的 Provider（Exa 池，§22.3）自己负责槽位
        冷却与后台恢复，执行路径上的瞬态不可用（预检、全部槽位超时、全部槽位异常）
        不通知整台重连——外层 ``stop()`` + ``start()`` 会把冷却一并抹掉。工具发现
        失败是硬故障，走不带 provider 的调用路径，仍然照常通知。
        """
        if provider is not None and getattr(provider, "manages_own_recovery", False):
            return
        if self._on_provider_failure is None:
            return
        try:
            self._on_provider_failure(server_name)
        except Exception:
            return


class _UnavailableProvider:
    """仅用于避免缺失服务器配置时分支产生异常。"""

    available = False


def _result_is_error(value: Any) -> bool:
    """识别 MCP CallToolResult 的错误标记，不把上游错误正文交给模型。"""
    if isinstance(value, dict):
        return bool(value.get("isError", value.get("is_error", False)))
    return bool(getattr(value, "isError", getattr(value, "is_error", False)))


def _accepts_should_run(provider: McpProvider) -> bool:
    """判断 Provider 的 `call_tool` 是否接受 `should_run`。

    多 Key 池需要它来在尝试之间重查代次；只实现旧两参数签名的替身仍然可用。
    """
    try:
        signature = inspect.signature(provider.call_tool)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return True
    return "should_run" in parameters


def _adapter_target(adapter: Any) -> Any:
    """取得绑定方法背后的适配器，也兼容可调用适配器对象。"""
    if adapter is None:
        return None
    return getattr(adapter, "__self__", adapter)


def _clip_tokens(text: str, limit: int) -> str:
    """对未适配工具结果做确定性、有界的保守截断。"""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()
