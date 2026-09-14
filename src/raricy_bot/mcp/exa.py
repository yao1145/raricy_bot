"""Exa MCP 搜索结果适配、可信边界和全局限流。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger, log_event
from ..text_utils import estimate_tokens
from ..texts import TRUNCATION_SUFFIX

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


class ExaNoResultsError(ValueError):
    """Exa 返回了内容，但没有一个结果通过安全解析。"""


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


class SearchLimiter:
    """全局串行且带最小间隔的搜索限流器。"""

    SKIPPED = object()

    def __init__(self, min_interval_seconds: float = 2.0, *, clock=None, sleep=None) -> None:
        if min_interval_seconds <= 0:
            raise ValueError("min_interval_seconds must be positive")
        self.min_interval_seconds = float(min_interval_seconds)
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._last_start: float | None = None

    async def run(self, operation, *, should_run=None):
        """等待间隔后执行 operation；取消等待不会推进上次调用时间。"""
        async with self._lock:
            if should_run is not None and not should_run():
                return self.SKIPPED
            now = self._clock()
            if self._last_start is not None:
                delay = self.min_interval_seconds - (now - self._last_start)
                if delay > 0:
                    await self._sleep(delay)
            if should_run is not None and not should_run():
                return self.SKIPPED
            self._last_start = self._clock()
            return await operation()


def _text_blocks(result: Any) -> list[str]:
    """只接受 MCP content 中的 text 块，不把任意对象直传模型。"""
    if isinstance(result, dict):
        if result.get("isError", result.get("is_error", False)):
            raise ValueError("Exa MCP returned an error")
    elif bool(getattr(result, "isError", getattr(result, "is_error", False))):
        raise ValueError("Exa MCP returned an error")
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("invalid Exa MCP content")
    blocks: list[str] = []
    for item in content:
        kind = getattr(item, "type", None)
        text = getattr(item, "text", None)
        if isinstance(item, dict):
            kind, text = item.get("type"), item.get("text")
        if kind != "text" or not isinstance(text, str):
            raise ValueError("non-text Exa MCP content")
        if text.strip():
            blocks.append(text)
    if not blocks:
        raise ValueError("empty Exa MCP content")
    return blocks


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


def _valid_url(value: str) -> bool:
    """仅允许无控制字符的 HTTP(S) URL。"""
    if any(ord(ch) < 32 or ch.isspace() for ch in value):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        # 访问 port 会主动校验端口是否为整数且在合法范围；hostname 则排除
        # ``http://:`` 这类虽有 netloc、实际没有主机的伪 URL。
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        # urlsplit 对畸形 IPv6、端口等输入会直接抛出；这类结果应被丢弃，
        # 不能让第三方返回内容把整个搜索轮次变成未分类异常。
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(hostname)
        and parsed.username is None
        and parsed.password is None
    )


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


def _truncate_tokens(text: str, budget: int, total_limit: int, prefix: str) -> str:
    """按现有 token 估算截断摘要，截断提示计入总上限。"""
    if estimate_tokens(prefix + text) <= total_limit:
        return text
    suffix = TRUNCATION_SUFFIX.strip()
    available = max(0, budget - estimate_tokens(suffix))
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= available:
            low = middle
        else:
            high = middle - 1
    clipped = text[:low].rstrip()
    return f"{clipped}{TRUNCATION_SUFFIX}" if clipped else suffix[: max(1, available)]


def _clip_plain_tokens(text: str, limit: int) -> str:
    """为整体硬上限提供一个不依赖 tokenizer 的保守裁剪。"""
    if estimate_tokens(text) <= limit:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()
