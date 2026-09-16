"""按 feature 配置装配适配器：一个 feature 一份限流器，一个绑定一个适配器对象。

两条约束决定了这里的形状：

1. ``InMemoryToolRegistry`` 按**模型侧工具名**查适配器，并且只把适配器对象交给
   ``prepare_arguments(arguments, feature)``（不带工具名），所以每个绑定必须有自己的对象 ——
   `/map` 的三个工具参数白名单各不相同，共用一个对象就分不出是谁在调。
2. 限流是 **feature 级**的（INTERFACES §22）：同一 feature 的全部绑定必须共用同一个
   ``CapabilityLimiter``，否则 `/map` 可以用三个工具绕过最小间隔。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..capabilities import CAPABILITY_BY_FEATURE
from ..config import McpConfig, McpFeatureConfig
from ..capabilities import IMPLEMENTED_FEATURES
from . import amap, exa, wolfram, zhihu
from .adapter_kit import CapabilityLimiter
from .registry import model_tool_name

# factory(feature, limiter, *, tool) -> 适配器对象。
AdapterFactory = Callable[..., Any]

# feature 名 -> 适配器工厂。必须与 `capabilities.IMPLEMENTED_FEATURES` 逐项一致
# （tests/test_mcp_adapters.py 钉住），后者是配置校验用的那份声明。
_FACTORIES: Mapping[str, AdapterFactory] = {
    "search": exa.build,
    "zhihu": zhihu.build,
    "map": amap.build,
    "wolfram": wolfram.build,
}


def implemented_features() -> frozenset[str]:
    """当前真的接了适配器的能力名。"""
    return frozenset(_FACTORIES)


def build_adapters(config: McpConfig) -> dict[str, Any]:
    """返回 ``{模型侧工具名: 适配器的 adapt 方法}``，供 ``McpManager`` 注入 Registry。

    适配器只对**能力表声明过**且**配置里启用**的 feature 装配；绑定里出现能力表不允许的工具
    时跳过（配置校验本应已经拒绝，这里是第二道）。
    """
    adapters: dict[str, Any] = {}
    for feature_name, feature in config.features.items():
        spec = CAPABILITY_BY_FEATURE.get(feature_name)
        if spec is None or spec.source != "mcp" or not feature.enabled:
            continue
        factory = _FACTORIES.get(feature_name)
        if factory is None:
            continue
        limiter = _limiter_for(feature)
        for binding in feature.bindings:
            if spec.allowed_tools and binding.tool not in spec.allowed_tools:
                continue
            adapter = factory(feature, limiter, tool=binding.tool)
            if adapter is None:
                continue
            adapters[model_tool_name(binding.server, binding.tool)] = adapter.adapt
    return adapters


def _limiter_for(feature: McpFeatureConfig) -> CapabilityLimiter:
    """一个 feature 一份限流器，注入它全部的绑定。"""
    return CapabilityLimiter(feature.min_interval_seconds)
