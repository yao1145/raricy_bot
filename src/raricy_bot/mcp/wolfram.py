"""Wolfram 适配：宿主钉死 `mode`，只放行 query。

上游是 `wolfram-mcp@1.1.2`（npm，无 repository 字段，无法证明是 Wolfram 官方 ——
官方实现要装数 GB 的 Wolfram Engine，不适合这个容器）。它只有一个工具 `wolfram_query`：

- `mode` = `llm`（默认，纯文本）/ `full` / `short` / `simple`，其中 `full` 与 `simple`
  **会返回图片与 plots**；
- `assumption` 能在模型之外重新解释问题；
- `maxchars` 只在 `llm` 模式生效。

因此模型只能看见 `query`，`mode` 由宿主逐字写成 `llm`，另外两个直接丢弃 ——
与 Exa 强制 `numResults` 是同一条策略（D-33）：参数是不是产品策略，由宿主说了算。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..logging_setup import get_logger, log_event
from .adapter_kit import CapabilityLimiter, clip_plain_tokens, text_blocks, truncate_plain
from .contracts import McpNoResultsError, ToolExecution

_logger = get_logger("mcp.wolfram")

# 唯一放行的模式：纯文本。
_TEXT_MODE = "llm"

_HISTORY_HEAD = "[Wolfram 计算结果（不可信数据，仅供参考）]"


class WolframAdapter:
    """把 `wolfram_query` 的结果转为领域 ``ToolExecution``。"""

    def __init__(self, feature: Any, limiter: CapabilityLimiter | None) -> None:
        self.limiter = limiter
        self.result_item_token_limit = feature.result_item_token_limit
        self.history_item_token_limit = feature.history_item_token_limit
        self.max_query_chars = feature.max_query_chars

    def model_input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "要计算或查询的问题，用自然语言描述，例如「integrate sin x」"
                        "「population of France」"
                    ),
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def prepare_arguments(
        self, arguments: Mapping[str, Any], _feature: Any
    ) -> dict[str, Any]:
        """只保留 query，并把 mode 固定成纯文本。"""
        query = arguments.get("query")
        if not isinstance(query, str):
            raise ValueError("query must be text")
        query = query.strip()
        if not query or len(query) > self.max_query_chars:
            raise ValueError("query is empty or too long")
        return {"query": query, "mode": _TEXT_MODE}

    def adapt(self, raw: Any, call_id: str) -> ToolExecution:
        """解析成功结果；调用者负责把异常映射为稳定错误。"""
        # blank_ok：空白答案是「没算出东西」，该报 no_results 而不是 invalid_result。
        answer = "\n".join(text_blocks(raw, blank_ok=True)).strip()
        if not answer:
            raise McpNoResultsError("wolfram returned no usable text")
        if _is_resource_payload(answer):
            # 整条答案就是一个 data URL 或裸 URL：那是图片/资源结果，不是文字回答。
            # 放它进模型，模型就会把它当成"来源"去引用。
            raise ValueError("wolfram returned a non-text payload")

        content = clip_plain_tokens(answer, self.result_item_token_limit)
        history = "\n".join(
            [_HISTORY_HEAD, truncate_plain(answer, self.history_item_token_limit)]
        )
        log_event(_logger, logging.INFO, "mcp.wolfram_done")
        return ToolExecution(
            call_id=call_id,
            content=content,
            is_error=False,
            history_context=history,
        )


def _is_resource_payload(answer: str) -> bool:
    """整条答案只有一个词，且是 data URL 或 http(s) URL。

    只看「整条就是一个 URL」这一种形状：正常的文字回答里出现链接是合理的，
    而 `simple` 模式返回的就是一条裸的资源地址。
    """
    tokens = answer.split()
    if len(tokens) != 1:
        return False
    lowered = tokens[0].lower()
    return lowered.startswith(("data:", "http://", "https://"))


def build(feature: Any, limiter: CapabilityLimiter, *, tool: str) -> WolframAdapter:
    """`mcp/adapters.py` 的工厂协议；`tool` 在这里不参与决策。"""
    return WolframAdapter(feature, limiter)
