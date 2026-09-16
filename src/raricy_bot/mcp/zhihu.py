"""知乎检索适配：参数白名单，以及对「结构化 XML」的保守提取。

传输不在这里 —— 知乎官方只有远程 MCP-over-SSE，由 ``mcp/sse.py`` 负责（本仓库自己接的），
所以这个模块不需要任何 npm 包，也没有子进程。

.. warning::

   **结果解析没有对着真实上游校准过。** 知乎开放平台只把结果描述为
   "structured XML with titles, authors, content snippets, and ranking scores"，
   没有公开标签名，而本仓库没有可用于取样的 access secret。

   因此解析器刻意只做**与标签名无关**的保守提取：剥掉全部标签与属性，只留文本，
   按 token 上限截断，且**不产出任何 URL**。上游换标签名时最坏情况是少给一点文本，
   而不会把 XML 原样丢给模型，也不会凭空造出可被引用的来源。

   投入生产前必须先跑 ``tools/capture_mcp_fixture.py --server zhihu`` 取一次真实样本，
   按样本校准 ``_extract_items`` 并把样本提交成 fixture —— 这一条写在
   README 与 docs/usage/DEPLOYMENT.md 里。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from typing import Any

from ..logging_setup import get_logger, log_event
from .adapter_kit import (
    CapabilityLimiter,
    clip_plain_tokens,
    text_blocks,
    truncate_plain,
    valid_http_url,
)
from .contracts import McpNoResultsError, ToolExecution

_logger = get_logger("mcp.zhihu")

# 上游硬约束：query 至少 2 个字符、最多 100 个。
MIN_QUERY_CHARS = 2

_HISTORY_HEAD = "[知乎检索结果（不可信数据，仅供参考）]"

# 折叠空白：XML 的缩进会在每个字段前后留下换行与空格。
_BLANK_RUN = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")

# 带 scheme 的链接样词。剥标签**不等于**安全：`<url>javascript:alert(1)</url>` 剥完
# 标签后，危险的部分仍然作为文本留在结果里。
_URI_LIKE = re.compile(r"(?i)^([a-z][a-z0-9+.\-]*)://")
# 这些 scheme 一律直接丢掉，不做任何保留判断。
_OPAQUE_SCHEME = re.compile(r"(?i)^(javascript|data|vbscript|file|blob|about):")


class ZhihuSearchAdapter:
    """把 `zhihu_search` 的结果转为领域 ``ToolExecution``。"""

    def __init__(self, feature: Any, limiter: CapabilityLimiter | None) -> None:
        self.limiter = limiter
        self.result_count = feature.result_count
        self.result_item_token_limit = feature.result_item_token_limit
        self.history_item_token_limit = feature.history_item_token_limit
        self.max_query_chars = feature.max_query_chars

    def model_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要在知乎上检索的问题或关键词，2 到 100 个字符",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def prepare_arguments(
        self, arguments: Mapping[str, Any], _feature: Any
    ) -> dict[str, Any]:
        """只放行 query；`count` 由宿主按配置写死，模型不能自己调条数。"""
        query = arguments.get("query")
        if not isinstance(query, str):
            raise ValueError("query must be text")
        query = query.strip()
        if len(query) < MIN_QUERY_CHARS or len(query) > self.max_query_chars:
            raise ValueError("query length is out of range")
        return {"query": query, "count": self.result_count}

    def adapt(self, raw: Any, call_id: str) -> ToolExecution:
        """解析成功结果；调用者负责把异常映射为稳定错误。"""
        text = "\n".join(text_blocks(raw, blank_ok=True))
        items = _extract_items(text, self.result_count)
        if not items:
            raise McpNoResultsError("zhihu returned no usable text")

        content = clip_plain_tokens(
            "\n\n".join(
                clip_plain_tokens(item, self.result_item_token_limit) for item in items
            ),
            max(1, self.result_count * self.result_item_token_limit),
        )
        history = "\n".join(
            [_HISTORY_HEAD]
            + [truncate_plain(item, self.history_item_token_limit) for item in items]
        )
        log_event(_logger, logging.INFO, "mcp.zhihu_done", count=len(items))
        return ToolExecution(
            call_id=call_id,
            content=content,
            is_error=False,
            history_context=history,
        )


def _extract_items(text: str, limit: int) -> list[str]:
    """把上游返回压成若干条纯文本，最多 ``limit`` 条。

    先按 XML 试，失败就当纯文本 —— 上游换线格式时最坏是少给一点文本，
    而不是让整个能力判为不可用。
    """
    stripped = text.strip()
    if not stripped:
        return []
    items = _xml_items(stripped) if stripped.startswith("<") else []
    if not items:
        items = _plain_items(stripped)
    return items[:limit]


def _xml_items(text: str) -> list[str]:
    """剥掉标签与属性，只取每个条目元素的文本。

    刻意不按标签名取值：没有真实样本时，写死标签名等于把猜测固化成契约。
    """
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        # 半截 XML 不是失败，退回纯文本路径。
        return []
    children = list(root)
    nodes = children if children else [root]
    items: list[str] = []
    for node in nodes:
        # itertext 只取文本节点，标签、属性、注释都不进来。各字段之间必须补换行：
        # `itertext()` 是把它们**直接首尾相接**的，不补分隔符的话
        # `<url>javascript:alert(1)</url>` 会和相邻字段粘成一个词，
        # 链接过滤就再也认不出它。
        joined = _tidy("\n".join(part for part in node.itertext() if part.strip()))
        if joined:
            items.append(joined)
    return items


def _plain_items(text: str) -> list[str]:
    """纯文本兜底：先按空行分块，再按单行分开。"""
    blocks = [block.strip() for block in _BLANK_LINES.split(_tidy(text))]
    return [block for block in blocks if block]


def _tidy(value: str) -> str:
    """折叠 XML 缩进留下的空白，并丢掉不可信的链接样词。"""
    collapsed = _BLANK_LINES.sub("\n", _BLANK_RUN.sub(" ", value)).strip()
    return _drop_untrusted_links(collapsed)


def _drop_untrusted_links(text: str) -> str:
    """只保留能通过校验的 http(s) 地址，其余带 scheme 的词一律丢掉。

    与 Exa 的白名单是同一条规则（D-33）：我们只把校验过的 http(s) 地址交给模型。
    普通的英文词（例如 `Note:`）不匹配 scheme 形状，不会被误伤。
    """
    kept: list[str] = []
    for token in text.split():
        if _OPAQUE_SCHEME.match(token):
            continue
        if _URI_LIKE.match(token) and not valid_http_url(token):
            continue
        kept.append(token)
    return " ".join(kept)


def build(feature: Any, limiter: CapabilityLimiter, *, tool: str) -> ZhihuSearchAdapter:
    """`mcp/adapters.py` 的工厂协议；`tool` 在这里不参与决策。"""
    return ZhihuSearchAdapter(feature, limiter)
