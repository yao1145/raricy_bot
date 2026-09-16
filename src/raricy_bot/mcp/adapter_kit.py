"""适配器公共件：限流器、MCP 文本块读取、URL 校验与 token 截断。

四个适配器（Exa / 知乎 / 高德 / Wolfram）都遵守同一套边界，所以这些件必须只有一份 ——
四个各自实现一遍 URL 校验，就是四个将来会各自漂移的安全边界。

本模块不依赖任何具体上游，也不 import 具体适配器。
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import Any

from ..text_utils import estimate_tokens
from ..texts import TRUNCATION_SUFFIX


class CapabilityLimiter:
    """全局串行且带最小间隔的能力限流器。

    每个 feature 一个实例：限流是 feature 级的，同一 feature 的全部绑定共享它，
    因此 `/map` 的三个工具不会绕过最小间隔（INTERFACES §22）。
    """

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


def text_blocks(result: Any, *, blank_ok: bool = False) -> list[str]:
    """只接受 MCP content 中的 text 块，不把任意对象直传模型。

    `isError` 与非 text 块一律抛 ``ValueError``；调用方（registry）把它映射成稳定错误，
    正文既不进日志也不进模型。

    ``blank_ok``：全是空白文本块时返回空列表而不是抛错，由调用方决定「什么内容都没有」
    该算「没有结果」还是「格式不认」。这是个真实的产品区别 —— Wolfram 的空白答案意味着
    它没算出东西（``no_results``），而结构化结果里的空数组意味着上游换了格式。
    """
    if isinstance(result, dict):
        if result.get("isError", result.get("is_error", False)):
            raise ValueError("MCP returned an error")
    elif bool(getattr(result, "isError", getattr(result, "is_error", False))):
        raise ValueError("MCP returned an error")
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("invalid MCP content")
    blocks: list[str] = []
    for item in content:
        kind = getattr(item, "type", None)
        text = getattr(item, "text", None)
        if isinstance(item, dict):
            kind, text = item.get("type"), item.get("text")
        if kind != "text" or not isinstance(text, str):
            raise ValueError("non-text MCP content")
        if text.strip():
            blocks.append(text)
    if not blocks and not blank_ok:
        raise ValueError("empty MCP content")
    return blocks


def valid_http_url(value: str) -> bool:
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
        # 不能让第三方返回内容把整个轮次变成未分类异常。
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(hostname)
        and parsed.username is None
        and parsed.password is None
    )


def truncate_tokens(text: str, budget: int, total_limit: int, prefix: str) -> str:
    """按现有 token 估算截断正文，截断提示计入总上限。"""
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


def truncate_plain(text: str, limit: int) -> str:
    """按 token 上限截断一段正文并补截断提示。

    用于没有「标题/URL 前缀」可保留的整块正文（高德、Wolfram 的结果就是这样）。
    有前缀字段时用 :func:`truncate_tokens`，那样能保住前缀不被截掉。
    """
    return truncate_tokens(text, limit, limit, "")


def clip_plain_tokens(text: str, limit: int) -> str:
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
