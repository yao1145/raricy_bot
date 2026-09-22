"""能力表：命令字面量、上游工具白名单与「一条消息最多一个能力」的唯一真值源。

这里集中三件以前散落各处的事：Router 认哪些命令、配置允许某个 feature 绑定哪些工具、
以及一个能力的本轮文案。下一个能力只加一行。

**并非每个能力都有命令**：`blog_write` 是定时发文子域自己发起的一轮生成，
用户敲不出它，所以 `command` 可为 None，`CAPABILITY_COMMANDS` 会把它过滤掉 ——
否则路由器的命令表与 `/help` 文案里会多出一条谁也敲不出来的命令。

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

    command: str | None
    """消息开头触发的命令字面量，统一小写；大小写不敏感由解析器负责。

    ``None`` = 这条能力没有用户命令（如定时发文的 ``blog_write``）：它由子域自己发起，
    不出现在命令解析、帮助文案或任何用户可见路径上。无命令的能力必须不带给用户的文案。
    """

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

    fixed_result_count: int | None = None
    """非 None 时，该能力的 ``result_count`` 默认值与唯一合法值都是它。

    定时发文要求「一次生成最多带一条工具结果」，若沿用通用默认值 5，
    ``result_count × result_item_token_limit`` 这一个整体预算就会变成 5 倍。
    """

    @property
    def has_command(self) -> bool:
        """是否有用户可见的命令字面量。"""
        return self.command is not None


# 工具能力（完整版专属，Light 不注册）与本地能力（两个版本共享）分组声明，
# 完整版装配结果 CAPABILITIES 由两组拼接而成（设计 §4.3）。
# 声明顺序即 Router 的判定顺序与帮助文案的出现顺序。当前没有任何一条命令是另一条的前缀，
# 所以顺序不影响正确性 —— 解析器要求命令后必须是空白或正文结束（见 text_utils）。
TOOL_CAPABILITIES: tuple[Capability, ...] = (
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
        # 定时发文子域的一轮生成（设计 §8）。它**没有命令字面量**：用户敲不出来，
        # 由 BlogService 到点自己发起，因此 command=None，也就没有占用文案与 system 追加。
        # 它仍然是一条 MCP 能力，因为要复用 Registry 的白名单、限流、超时与故障转移。
        feature="blog_write",
        command=None,
        source=SOURCE_MCP,
        usage_text="",
        unavailable_text=None,
        system_addendum=None,
        # 首版白名单是现有已审核的 6 个只读工具，不引入通用透传（设计 §8.2 第 3 条）。
        allowed_tools=frozenset(
            {
                "web_search_exa",
                "zhihu_search",
                "maps_geo",
                "maps_text_search",
                "maps_weather",
                "wolfram_query",
            }
        ),
        max_bindings=6,
        result_shape=SHAPE_LIST,
        # 整篇文章只是这条路上的一「轮」，没有多步研究循环，查询串上限与通用值同档。
        max_query_chars=500,
        # 一篇文只需要一条工具结果；写死成 1 是为了让整体 token 预算不被暗中放大 5 倍。
        fixed_result_count=1,
    ),
)

# 本地能力：不经过 MCP，两个版本共享；Light 的能力装配只取这一组（设计 §4.2）。
LOCAL_CAPABILITIES: tuple[Capability, ...] = (
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

# 完整版装配结果。命令顺序保持 search/zhihu/map/wolfram/kb：blog_write 没有命令，
# 任何按 has_command 过滤的消费方拿到的相对顺序与拆分前逐字节一致。
CAPABILITIES: tuple[Capability, ...] = TOOL_CAPABILITIES + LOCAL_CAPABILITIES

# 已经接了适配器的 MCP 能力。声明在这里而不是 `mcp/adapters.py`，是因为 `config.py`
# 要在**加载期**用它挡住「能力表里有、但还没实现」的能力 —— 而 config 不能 import mcp
# （`mcp/__init__.py` 会拉起 registry，registry 又依赖 config，成环）。
# 没有适配器时 Registry 会走通用透传路径，把上游原文（截断后）直接交给模型，
# 那既不是我们评审过的清洗，也不是我们想给用户的形态，所以宁可在启动时报错。
# `mcp/adapters.py` 的工厂表必须与本集合逐项一致，由 tests/test_mcp_adapters.py 钉住。
IMPLEMENTED_FEATURES: frozenset[str] = frozenset(
    {"search", "zhihu", "map", "wolfram", "blog_write"}
)


CAPABILITY_BY_FEATURE: Mapping[str, Capability] = MappingProxyType(
    {capability.feature: capability for capability in CAPABILITIES}
)

# 命令字面量 -> 能力名。_parse_capability_command 按此顺序判定，顺序即声明顺序。
# 无命令的能力在这里被过滤掉：它们不从消息里解析，也不该出现在帮助文案里。
CAPABILITY_COMMANDS: tuple[tuple[str, str], ...] = tuple(
    (capability.command, capability.feature)
    for capability in CAPABILITIES
    if capability.command is not None
)
