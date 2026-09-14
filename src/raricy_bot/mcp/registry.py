"""MCP 工具发现、命名空间和 feature 白名单。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from ..config import McpFeatureConfig
from ..text_utils import estimate_tokens
from .contracts import (
    McpCallTimeoutError,
    McpProvider,
    ToolCall,
    ToolDefinition,
    ToolExecution,
)
from .exa import ExaNoResultsError, SearchLimiter

_MODEL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def model_tool_name(server_name: str, tool_name: str) -> str:
    """将 MCP 原始名称转换为模型工具名称。"""
    return f"{_MODEL_NAME_RE.sub('_', server_name)}__{_MODEL_NAME_RE.sub('_', tool_name)}"


class InMemoryToolRegistry:
    """管理 Provider 快照，并在执行前重新检查 feature 白名单。"""

    def __init__(
        self,
        providers: Mapping[str, McpProvider],
        features: Mapping[str, McpFeatureConfig],
        adapters: Mapping[str, Callable[[Any, str], ToolExecution]] | None = None,
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
                continue
            try:
                definitions = await provider.list_tools()
            except Exception:
                self._notify_provider_failure(server_name)
                failures.add(server_name)
                continue
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
        self._tools = discovered
        self._conflicts = conflicts
        self._last_refresh_failures = failures
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
                adapter = self._adapters.get(name)
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
            return ToolExecution(call.call_id, "tool_not_allowed", True, "tool_not_allowed")
        try:
            arguments = json.loads(call.arguments_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ToolExecution(call.call_id, "invalid_arguments", True, "invalid_arguments")
        if not isinstance(arguments, dict):
            return ToolExecution(call.call_id, "invalid_arguments", True, "invalid_arguments")
        adapter = self._adapters.get(definition.model_name)
        # 适配器可以在 Provider 边界实现产品级参数策略。绑定 Exa 时，
        # 这里只接受 query，并由适配器强制写入配置中的 numResults；未来工具
        # 没有该钩子时仍保持通用 MCP 参数透传。
        preparer = getattr(_adapter_target(adapter), "prepare_arguments", None)
        if callable(preparer):
            try:
                arguments = preparer(arguments, self._features[feature_name])
            except (KeyError, TypeError, ValueError):
                return ToolExecution(call.call_id, "invalid_arguments", True, "invalid_arguments")
            if not isinstance(arguments, dict):
                return ToolExecution(call.call_id, "invalid_arguments", True, "invalid_arguments")
        provider = self._providers.get(definition.server_name)
        if provider is None or not provider.available:
            self._notify_provider_failure(definition.server_name)
            return ToolExecution(call.call_id, "tool_unavailable", True, "search_unavailable")

        async def call_provider() -> Any:
            return await provider.call_tool(definition.tool_name, arguments)

        limiter = getattr(_adapter_target(adapter), "limiter", None)
        try:
            if limiter is not None:
                raw = await limiter.run(
                    call_provider, should_run=generation_is_current
                )
                if raw is SearchLimiter.SKIPPED:
                    return ToolExecution(
                        call.call_id,
                        "search cancelled",
                        True,
                        "generation_cancelled",
                    )
            else:
                if generation_is_current is not None and not generation_is_current():
                    return ToolExecution(
                        call.call_id,
                        "search cancelled",
                        True,
                        "generation_cancelled",
                    )
                raw = await call_provider()
        except McpCallTimeoutError:
            self._notify_provider_failure(definition.server_name)
            return ToolExecution(call.call_id, "search timed out", True, "search_timeout")
        except Exception:
            self._notify_provider_failure(definition.server_name)
            return ToolExecution(call.call_id, "tool_unavailable", True, "search_unavailable")
        if _result_is_error(raw):
            return ToolExecution(call.call_id, "tool unavailable", True, "search_unavailable")
        if adapter is not None:
            try:
                return adapter(raw, call.call_id)
            except ExaNoResultsError:
                return ToolExecution(call.call_id, "no results", True, "no_results")
            except ValueError:
                return ToolExecution(call.call_id, "invalid_result", True, "invalid_result")
            except Exception:
                return ToolExecution(call.call_id, "invalid_result", True, "invalid_result")
        if isinstance(raw, str):
            content = raw
        else:
            try:
                content = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                return ToolExecution(call.call_id, "invalid_result", True, "invalid_result")
        # 未提供专用适配器的未来工具也不能把任意 MCP 响应无界地交给模型。
        # 以该 feature 的结果预算作为保守上限；专用适配器（如 Exa）在此之前
        # 已按更细的逐条规则处理。
        feature = self._features.get(feature_name)
        if feature is None:
            return ToolExecution(call.call_id, "invalid_result", True, "invalid_result")
        max_tokens = max(1, feature.result_count * feature.result_item_token_limit)
        if estimate_tokens(content) > max_tokens:
            content = _clip_tokens(content, max_tokens)
        return ToolExecution(call.call_id, content, False)

    def _notify_provider_failure(self, server_name: str) -> None:
        """通知生命周期管理器安排重连；回调异常不得影响用户请求。"""
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
