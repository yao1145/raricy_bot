"""Exa MCP 搜索结果适配、可信边界和全局限流。"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger, log_event
from ..text_utils import estimate_tokens
from .adapter_kit import (
    CapabilityLimiter,
    clip_plain_tokens as _clip_plain_tokens,
    text_blocks as _text_blocks,
    truncate_tokens as _truncate_tokens,
    valid_http_url as _valid_url,
)
from .contracts import McpNoResultsError

_logger = get_logger("mcp.exa")


@dataclass(frozen=True)
class ExaSearchResult:
    """一个经过 URL 校验的 Exa 摘要结果。"""

    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class ExaSearchOutput:
    """当前轮工具正文、历史摘要和有效结果。"""

    content: str
    history_context: str
    results: tuple[ExaSearchResult, ...]


# 保留这个名字：调用点与测试按它写，而语义由共享的 McpNoResultsError 定义。
ExaNoResultsError = McpNoResultsError


def parse_exa_result(
    result: Any,
    *,
    result_count: int = 5,
    result_item_token_limit: int = 3000,
    history_item_token_limit: int = 500,
) -> ExaSearchOutput:
    """解析 Exa 3.4.1 文本块，拒绝无法安全验证的结果。"""
    texts = _text_blocks(result)
    candidates: list[ExaSearchResult] = []
    for block in re.split(r"\n\s*---\s*\n", "\n\n".join(texts)):
        parsed = _parse_block(block)
        if parsed is None or not _valid_url(parsed.url):
            continue
        candidates.append(parsed)
        if len(candidates) >= result_count:
            break
    if not candidates:
        raise ExaNoResultsError("no valid Exa search results")

    current: list[str] = []
    history: list[str] = ["[联网资料（不可信数据，仅供参考）]"]
    for index, item in enumerate(candidates, 1):
        current_item = _format_item(item, result_item_token_limit)
        current.append(current_item)
        history_item = _format_history_item(item, history_item_token_limit, index)
        history.append(history_item)
    content = "\n\n---\n\n".join(current)
    max_total = result_count * result_item_token_limit
    if estimate_tokens(content) > max_total:
        content = _clip_plain_tokens(content, max_total)
    return ExaSearchOutput(
        content=content,
        history_context="\n".join(history),
        results=tuple(candidates),
    )


def build(feature: Any, limiter: CapabilityLimiter, *, tool: str) -> ExaSearchAdapter:
    """按 feature 配置造一个 Exa 适配器（`mcp/adapters.py` 的工厂协议）。

    Exa 只有一个绑定工具，`tool` 参数在这里不参与决策；保留它是为了让工厂协议对所有
    provider 一致。
    """
    return ExaSearchAdapter(
        result_count=feature.result_count,
        result_item_token_limit=feature.result_item_token_limit,
        history_item_token_limit=feature.history_item_token_limit,
        max_query_chars=feature.max_query_chars,
        limiter=limiter,
    )


class ExaSearchAdapter:
    """将 Exa Provider 的结果转为领域 ToolExecution。"""

    def __init__(
        self,
        *,
        result_count: int = 5,
        result_item_token_limit: int = 3000,
        history_item_token_limit: int = 500,
        max_query_chars: int = 500,
        limiter: SearchLimiter | None = None,
    ) -> None:
        self.result_count = result_count
        self.result_item_token_limit = result_item_token_limit
        self.history_item_token_limit = history_item_token_limit
        self.max_query_chars = max_query_chars
        self.limiter = limiter

    def prepare_arguments(
        self, arguments: Mapping[str, Any], _feature: Any
    ) -> dict[str, Any]:
        """只把模型可见的 query 转为 Exa 参数，并由宿主固定结果数量。

        Exa MCP 还接受 ``numResults`` 等参数，但它们属于宿主策略，不能由模型
        提高结果数量或改变分页。因此这里故意丢弃除 query 以外的所有字段。
        """
        query = arguments.get("query")
        if not isinstance(query, str):
            raise ValueError("query must be text")
        query = query.strip()
        if not query or len(query) > self.max_query_chars:
            raise ValueError("query is empty or too long")
        return {"query": query, "numResults": self.result_count}

    def model_input_schema(self) -> dict[str, Any]:
        """返回搜索 feature 对模型公开的最小参数合同。"""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的问题或关键词",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def adapt(self, raw: Any, call_id: str):
        """解析成功结果；调用者负责把异常映射为稳定错误。"""
        from .contracts import ToolExecution

        output = parse_exa_result(
            raw,
            result_count=self.result_count,
            result_item_token_limit=self.result_item_token_limit,
            history_item_token_limit=self.history_item_token_limit,
        )
        # 只记条数：标题、URL 与摘要都不进日志（设计 §8.3）。
        log_event(_logger, logging.INFO, "mcp.search_done", count=len(output.results))
        return ToolExecution(
            call_id=call_id,
            content=output.content,
            is_error=False,
            history_context=output.history_context,
        )


# 沿用小写别名，既有调用点与测试不必改；实现与限流语义由 adapter_kit 定义。
SearchLimiter = CapabilityLimiter


_FIELD_RE = re.compile(r"(?im)^\s*(Title|URL|Highlights|Text|Published|Author)\s*:\s*(.*)$")


def _parse_block(block: str) -> ExaSearchResult | None:
    matches = list(_FIELD_RE.finditer(block))
    fields: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        value = match.group(2).strip()
        continuation = block[match.end() : end].strip()
        if continuation:
            value = f"{value}\n{continuation}" if value else continuation
        fields[match.group(1).lower()] = value
    title = fields.get("title", "").strip()
    url = fields.get("url", "").strip()
    snippet = (fields.get("highlights") or fields.get("text") or "").strip()
    if not title or not url or len(url) > 2048:
        return None
    return ExaSearchResult(title=title[:512], url=url, snippet=snippet)


def _format_item(item: ExaSearchResult, limit: int) -> str:
    prefix = f"Title: {item.title}\nURL: {item.url}\nSnippet: "
    if estimate_tokens(prefix) >= limit:
        return _clip_plain_tokens(prefix, limit)
    value = prefix + _truncate_tokens(
        item.snippet, max(1, limit - estimate_tokens(prefix)), limit, prefix
    )
    return _clip_plain_tokens(value, limit)


def _format_history_item(item: ExaSearchResult, limit: int, index: int) -> str:
    prefix = f"{index}. {item.title}\nURL: {item.url}\n摘要: "
    if estimate_tokens(prefix) >= limit:
        return _clip_plain_tokens(prefix, limit)
    value = prefix + _truncate_tokens(
        item.snippet, max(1, limit - estimate_tokens(prefix)), limit, prefix
    )
    return _clip_plain_tokens(value, limit)
