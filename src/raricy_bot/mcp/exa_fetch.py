"""Exa 网页读取适配：单 URL、5000 字符硬上限与压缩历史。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .. import texts
from .adapter_kit import CapabilityLimiter, clip_plain_tokens, text_blocks, valid_http_url
from .contracts import McpNoResultsError, ToolExecution

MAX_FETCH_CHARS = 5000


class ExaFetchAdapter:
    """将 web_fetch_exa 的 Markdown 文本转为有界工具结果。"""

    def __init__(self, feature: Any, limiter: CapabilityLimiter) -> None:
        self.max_query_chars = min(feature.max_query_chars, 2048)
        self.result_item_token_limit = feature.result_item_token_limit
        self.history_item_token_limit = feature.history_item_token_limit
        self.limiter = limiter

    def model_input_schema(self) -> dict[str, Any]:
        """模型只提供一个 URL，字符预算由宿主固定。"""
        return {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": self.max_query_chars},
                    "minItems": 1,
                    "maxItems": 1,
                    "description": "要读取的一个 HTTP(S) 网页 URL",
                }
            },
            "required": ["urls"],
            "additionalProperties": False,
        }

    def prepare_arguments(self, arguments: Mapping[str, Any], _feature: Any) -> dict[str, Any]:
        """拒绝批量、非 HTTP(S)、带凭据或过长 URL；不允许扩大字符预算。"""
        urls = arguments.get("urls")
        if not isinstance(urls, list) or len(urls) != 1:
            raise ValueError("exactly one URL is required")
        url = urls[0]
        if not isinstance(url, str) or len(url) > self.max_query_chars or not valid_http_url(url):
            raise ValueError("invalid URL")
        return {"urls": [url], "maxCharacters": MAX_FETCH_CHARS}

    def adapt(self, raw: Any, call_id: str) -> ToolExecution:
        """只接受上游标题、URL 和正文格式；正文仅用于当前轮。"""
        answer = "\n\n".join(text_blocks(raw, blank_ok=True)).strip()
        if not answer or answer in {"No content found.", "No content found for the provided URL(s)."}:
            raise McpNoResultsError("no fetched content")
        header, separator, body = answer.partition("\n\n")
        lines = header.splitlines()
        if len(lines) < 2 or not lines[0].startswith("# ") or not lines[1].startswith("URL: "):
            raise ValueError("invalid fetch result")
        url = lines[1][5:]
        if len(url) > 2048 or not valid_http_url(url):
            raise ValueError("invalid result URL")
        if not separator or not body.strip():
            raise McpNoResultsError("no fetched text")
        if len(answer) > MAX_FETCH_CHARS:
            answer = answer[:MAX_FETCH_CHARS - len(texts.TRUNCATION_SUFFIX)] + texts.TRUNCATION_SUFFIX
        content = clip_plain_tokens(answer, self.result_item_token_limit)
        history = clip_plain_tokens(
            texts.FETCH_HISTORY_HEAD + "\n" + content,
            self.history_item_token_limit,
        )
        return ToolExecution(call_id, content, False, history_context=history)


def build(feature: Any, limiter: CapabilityLimiter, *, tool: str) -> ExaFetchAdapter:
    """按 fetch 配置装配独立适配器。"""
    return ExaFetchAdapter(feature, limiter)
