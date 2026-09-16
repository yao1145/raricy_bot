"""能力表：命令字面量、上游工具白名单与「一条消息最多一个能力」的唯一真值源。

这里集中三件以前散落各处的事：Router 认哪些命令、配置允许某个 feature 绑定哪些工具、
以及一个能力的本轮文案。下一个能力只加一行。

**本模块不能放进 `mcp/`**：`config.py` 要用这张表做加载期校验，而 `mcp/__init__.py` 会
拉起 `registry`，`registry` 又依赖 `config`，放进去就成环。依赖方向固定为
`texts ← capabilities ← {text_utils, config, core/router, app}`。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from . import texts

# source 取值。
SOURCE_MCP = "mcp"
SOURCE_LOCAL = "local"

# result_shape 取值：list = 本轮是多条条目；single = 本轮是整条答案，result_count 必须为 1。
SHAPE_LIST = "list"
SHAPE_SINGLE = "single"


@dataclass(frozen=True)
class Capability:
    """一个用户可见能力的全部静态事实。"""

    feature: str
    """写入 ``Request.enabled_features`` 的通用能力名，也是 ``mcp.features`` 的键。"""

    command: str
    """消息开头触发的命令字面量，统一小写；大小写不敏感由解析器负责。"""

    source: str
    """``mcp`` = 需要上游 MCP 服务器；``local`` = 机器人本地实现，不经过 MCP。"""

    usage_text: str
    """命令无参数时的本地用法提示（``notice_local``）。"""

    unavailable_text: str | None
    """能力本地门判否时的提示；``local`` 能力在自己的代码路径上另行收口。"""

    system_addendum: str | None
    """本轮追加到 system 的静态说明；``None`` 表示不需要。"""

    allowed_tools: frozenset[str]
    """该能力允许绑定的上游工具名；空集表示不适用（本地能力）。"""

    max_bindings: int
    """``mcp.features.<feature>.bindings`` 允许的最大条数。"""

    result_shape: str
    """``list`` 或 ``single``，决定 ``result_count`` 的语义与校验。"""

    max_query_chars: int
    """宿主对模型给出的查询串长度上限，同时是该 feature 配置项的上限；本地能力不适用。"""


# 声明顺序即 Router 的判定顺序与帮助文案的出现顺序。当前没有任何一条命令是另一条的前缀，
# 所以顺序不影响正确性 —— 解析器要求命令后必须是空白或正文结束（见 text_utils）。
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        feature="search",
        command="/search",
        source=SOURCE_MCP,
        usage_text=texts.SEARCH_USAGE_TEXT,
        unavailable_text=texts.SEARCH_UNAVAILABLE_TEXT,
        system_addendum=texts.MCP_TOOL_SYSTEM_ADDENDUM,
        allowed_tools=frozenset({"web_search_exa"}),
        max_bindings=1,
        result_shape=SHAPE_LIST,
        max_query_chars=500,
    ),
    Capability(
        feature="zhihu",
        command="/zhihu",
        source=SOURCE_MCP,
        usage_text=texts.ZHIHU_USAGE_TEXT,
        unavailable_text=texts.ZHIHU_UNAVAILABLE_TEXT,
        system_addendum=texts.MCP_TOOL_SYSTEM_ADDENDUM,
        # 上游 zhihu_search 的 query 是 2..100 字符，宿主按同一上限收口。
        allowed_tools=frozenset({"zhihu_search"}),
        max_bindings=1,
        result_shape=SHAPE_LIST,
        max_query_chars=100,
    ),
    Capability(
        feature="map",
        command="/map",
        source=SOURCE_MCP,
        usage_text=texts.MAP_USAGE_TEXT,
        unavailable_text=texts.MAP_UNAVAILABLE_TEXT,
        system_addendum=texts.MCP_TOOL_SYSTEM_ADDENDUM,
        # 只白名单单调用可完成的三个工具（D-85）：周边搜索/逆地理编码/距离测量都需要
        # 「经度,纬度」，POI 详情需要上一轮返回的 POI ID，而单轮预算固定为 1，模型拿不到这些输入。
        allowed_tools=frozenset({"maps_geo", "maps_text_search", "maps_weather"}),
        max_bindings=3,
        result_shape=SHAPE_LIST,
        max_query_chars=100,
    ),
    Capability(
        feature="wolfram",
        command="/wolfram",
        source=SOURCE_MCP,
        usage_text=texts.WOLFRAM_USAGE_TEXT,
        unavailable_text=texts.WOLFRAM_UNAVAILABLE_TEXT,
        system_addendum=texts.MCP_TOOL_SYSTEM_ADDENDUM,
        allowed_tools=frozenset({"wolfram_query"}),
        max_bindings=1,
        result_shape=SHAPE_SINGLE,
        max_query_chars=300,
    ),
    Capability(
        feature="kb",
        command="/kb",
        source=SOURCE_LOCAL,
        usage_text=texts.KB_USAGE_TEXT,
        # 本地能力的访问门、无结果与关闭提示在 app._prepare_kb 各自收口（三种原因三种文案）。
        unavailable_text=None,
        # /kb 的数据块自带 KB_SYSTEM_ADDENDUM，由 app 按既有分支追加。
        system_addendum=None,
        allowed_tools=frozenset(),
        max_bindings=0,
        result_shape=SHAPE_LIST,
        # 本地能力不经过 MCP，没有上游查询串上限。
        max_query_chars=0,
    ),
)

# 已经接了适配器的 MCP 能力。声明在这里而不是 `mcp/adapters.py`，是因为 `config.py`
# 要在**加载期**用它挡住「能力表里有、但还没实现」的能力 —— 而 config 不能 import mcp
# （`mcp/__init__.py` 会拉起 registry，registry 又依赖 config，成环）。
# 没有适配器时 Registry 会走通用透传路径，把上游原文（截断后）直接交给模型，
# 那既不是我们评审过的清洗，也不是我们想给用户的形态，所以宁可在启动时报错。
# `mcp/adapters.py` 的工厂表必须与本集合逐项一致，由 tests/test_mcp_adapters.py 钉住。
IMPLEMENTED_FEATURES: frozenset[str] = frozenset(
    {"search", "zhihu", "map", "wolfram"}
)


CAPABILITY_BY_FEATURE: Mapping[str, Capability] = MappingProxyType(
    {capability.feature: capability for capability in CAPABILITIES}
)

# 命令字面量 -> 能力名。_parse_capability_command 按此顺序判定，顺序即声明顺序。
CAPABILITY_COMMANDS: tuple[tuple[str, str], ...] = tuple(
    (capability.command, capability.feature) for capability in CAPABILITIES
)
