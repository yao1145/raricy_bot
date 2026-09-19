"""按 feature 配置装配适配器：一个 feature 一份限流器，一个绑定一个适配器对象。

三条约束决定了这里的形状：

1. 适配器的键是 **(feature 名, 上游工具名)** 这一对，而不是单独的模型侧工具名：同一个
   上游工具可以被两个 feature 绑定（`search` 与 `blog_write` 都用 `web_search_exa`），
   而两边的参数上限、结果数、schema 与限流策略各不相同。单键存放时后装配的会覆盖前一个，
   配置里写在哪一边就不再决定行为（D-110）。**不保留跨 feature 的回退查询。**
2. ``InMemoryToolRegistry`` 只把适配器对象交给 ``prepare_arguments(arguments, feature)``
   （不带工具名），所以每个绑定必须有自己的对象 —— ``/map`` 的三个工具参数白名单各不相同，
   共用一个对象就分不出是谁在调。
3. 限流是 **feature 级**的（INTERFACES §22）：同一 feature 的全部绑定必须共用同一个
   ``CapabilityLimiter``，否则 ``/map`` 可以用三个工具绕过最小间隔。

发文（`blog_write`）的六个绑定**委托**上面这些清洗器，不重写解析、也不做通用透传：
它额外收窄查询串上限（取发文配置与原能力的较小值），并在出口再保证一次整体 token 预算。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from ..capabilities import CAPABILITY_BY_FEATURE, CAPABILITIES
from ..config import McpConfig, McpFeatureConfig
from ..text_utils import estimate_tokens
from . import amap, exa, wolfram, zhihu
from .adapter_kit import CapabilityLimiter, clip_plain_tokens
from .contracts import ToolExecution
from .registry import AdapterKey, model_tool_name

# factory(feature, limiter, *, tool) -> 适配器对象；返回 None 表示该绑定不装配。
AdapterFactory = Callable[..., Any]

# 没有用户命令的定时发文能力：它自己发起生成，用户敲不出来（§53.3）。
BLOG_WRITE_FEATURE = "blog_write"


def _origin_capability(tool: str) -> Any:
    """该工具在**聊天能力**下的归属：发文委托的就是那一份清洗器与参数上限。

    除了发文自己，按能力表声明顺序取第一个声明了该工具的能力 —— `blog_write` 也声明了
    同样六个工具，不排除它就查不到源头。
    """
    for capability in CAPABILITIES:
        if capability.feature != BLOG_WRITE_FEATURE and tool in capability.allowed_tools:
            return capability
    return None


class _BoundedOutputAdapter:
    """发文绑定的出口边界：委托清洗器之后，再对整体结果做一次有界裁剪。

    能力表把发文的 ``result_count`` 钉死为 1，所以 ``result_item_token_limit`` 就是这一轮
    的整体预算；这里必须**自己**保证它 —— 依赖 Registry 的通用裁剪等于把这条不变量交给
    一条在这条路上不会走到的分支（专用适配器在它之前就已经返回）。
    """

    def __init__(self, inner: Any, *, token_limit: int) -> None:
        self._inner = inner
        self.limiter = getattr(inner, "limiter", None)
        self.token_limit = token_limit

    # --- 模型可见面：与委托的清洗器逐字一致 ---------------------------------

    def model_input_schema(self) -> dict[str, Any]:
        return self._inner.model_input_schema()

    def prepare_arguments(
        self, arguments: Mapping[str, Any], feature: Any
    ) -> dict[str, Any]:
        """转发给清洗器；上限已在构造时收窄进它读的那份 feature。"""
        return self._inner.prepare_arguments(arguments, feature)

    # --- 结果 ---------------------------------------------------------------

    def adapt(self, raw: Any, call_id: str) -> ToolExecution:
        """清洗器结果原样返回，只在超出整体预算时做一次有界裁剪。"""
        execution = self._inner.adapt(raw, call_id)
        if estimate_tokens(execution.content) <= self.token_limit:
            return execution
        return ToolExecution(
            call_id=execution.call_id,
            content=clip_plain_tokens(execution.content, self.token_limit),
            is_error=execution.is_error,
            error_kind=execution.error_kind,
            history_context=execution.history_context,
        )


def _build_blog_write(
    feature: McpFeatureConfig, limiter: CapabilityLimiter, *, tool: str
) -> Any:
    """发文的一个绑定：委托同工具的既有清洗器，并把参数上限收窄到两侧的较小值。

    同一台 Exa 服务被 `search` 与 `blog_write` 同时绑定时，两个 feature 各拿自己的
    适配器对象与限流器，共用的只是 Provider 与连接池（D-110）。
    """
    origin = _origin_capability(tool)
    if origin is None:
        # 没有可委托的清洗器就不装配：宁可不接，也不要一个行为不明的适配器。
        return None
    delegate = _FACTORIES.get((origin.feature, tool))
    if delegate is None:
        return None
    effective = replace(
        feature,
        # 各绑定实际取「发文配置的上限」与「原工具上限」的较小值：例如 zhihu_search
        # 仍是 100，发文侧写 500 也不会把它放宽。
        max_query_chars=min(feature.max_query_chars, origin.max_query_chars),
    )
    adapter = delegate(effective, limiter, tool=tool)
    if adapter is None:
        return None
    return _BoundedOutputAdapter(adapter, token_limit=effective.result_item_token_limit)


# (feature 名, 上游工具名) -> 适配器工厂。必须与 `capabilities.IMPLEMENTED_FEATURES`
# 逐项一致（tests/test_mcp_adapters.py 钉住），后者是配置校验用的那份声明。
_FACTORIES: Mapping[tuple[str, str], AdapterFactory] = {
    ("search", "web_search_exa"): exa.build,
    ("zhihu", "zhihu_search"): zhihu.build,
    ("map", "maps_geo"): amap.build,
    ("map", "maps_text_search"): amap.build,
    ("map", "maps_weather"): amap.build,
    ("wolfram", "wolfram_query"): wolfram.build,
    # 发文的六个绑定全部委托给同工具在聊天能力下的那一份清洗器。
    ("blog_write", "web_search_exa"): _build_blog_write,
    ("blog_write", "zhihu_search"): _build_blog_write,
    ("blog_write", "maps_geo"): _build_blog_write,
    ("blog_write", "maps_text_search"): _build_blog_write,
    ("blog_write", "maps_weather"): _build_blog_write,
    ("blog_write", "wolfram_query"): _build_blog_write,
}


def implemented_features() -> frozenset[str]:
    """当前真的接了适配器的能力名。"""
    return frozenset(feature for feature, _tool in _FACTORIES)


def build_adapters(config: McpConfig) -> dict[AdapterKey, Any]:
    """返回 ``{(feature 名, 模型侧工具名): 适配器的 adapt 方法}``，供 ``McpManager`` 注入 Registry。

    适配器只对**能力表声明过**且**配置里启用**的 feature 装配；绑定里出现能力表不允许的工具
    时跳过（配置校验本应已经拒绝，这里是第二道）。
    """
    adapters: dict[AdapterKey, Any] = {}
    for feature_name, feature in config.features.items():
        spec = CAPABILITY_BY_FEATURE.get(feature_name)
        if spec is None or spec.source != "mcp" or not feature.enabled:
            continue
        # 一个 feature 一份限流器，注入它**全部**的绑定：限流是 feature 级全局串行。
        limiter = _limiter_for(feature)
        for binding in feature.bindings:
            if spec.allowed_tools and binding.tool not in spec.allowed_tools:
                continue
            factory = _FACTORIES.get((feature_name, binding.tool))
            if factory is None:
                continue
            adapter = factory(feature, limiter, tool=binding.tool)
            if adapter is None:
                continue
            key: AdapterKey = (feature_name, model_tool_name(binding.server, binding.tool))
            adapters[key] = adapter.adapt
    return adapters


def _limiter_for(feature: McpFeatureConfig) -> CapabilityLimiter:
    """一个 feature 一份限流器，注入它全部的绑定。"""
    return CapabilityLimiter(feature.min_interval_seconds)
