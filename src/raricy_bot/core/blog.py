"""引用博客：状态判定、正文取回与拼块（设计 §3.2）。

模型只能看到**这一轮**的博客正文：它不进历史、不落库、不写日志。
正文取回走公开的 spider 接口（与评论机器人同一条通路，不带 Cookie）。
"""

from __future__ import annotations

import logging

from ..logging_setup import get_logger, log_event
from ..site.client import SiteClient, SiteError
from ..site.models import ChatMessage

_logger = get_logger("blog")

# 一条消息引用了博客时的五种状态。none 表示**没引用**，其余四种都表示引用了，
# 区别只在正文给不给、给什么。
BLOG_STATE_NONE: str = "none"
BLOG_STATE_OK: str = "ok"
BLOG_STATE_TOO_LONG: str = "too_long"
BLOG_STATE_MISSING: str = "missing"
BLOG_STATE_FAILED: str = "failed"

_READABLE_STATES: frozenset[str] = frozenset({BLOG_STATE_OK, BLOG_STATE_TOO_LONG})

# 历史里留下的标记。正文只属于当前轮，历史里退化成一行标记；否则那一轮会变成
# 「助手在回答一篇看不见的文章」，正文为空时更糟——历史直接是空的（设计 §3.5）。
_HISTORY_MARKERS: dict[str, str | None] = {
    BLOG_STATE_NONE: None,
    BLOG_STATE_OK: "[引用博客]",
    BLOG_STATE_TOO_LONG: "[引用博客]",
    BLOG_STATE_MISSING: "[引用博客已删除]",
    BLOG_STATE_FAILED: "[引用博客未取得]",
}

# 「正文」一栏的四种取值。超限那句**逐字**照 comments/service.py；另外两句是聊天区
# 自己的：评论区在取回失败时也说「长度规则」，那句在那条路径上并不准确。
_BODY_TOO_LONG: str = "正文因长度规则未提供"
_BODY_MISSING: str = "该博客已被删除，正文不可读"
_BODY_FAILED: str = "正文未取得"


def blog_readable(state: str) -> bool:
    """该状态下模型的答复有内容可依（标题或正文至少给了一样）。"""
    return state in _READABLE_STATES


def blog_marker(state: str) -> str | None:
    """历史里留下的标记；没有引用博客时为 None。"""
    return _HISTORY_MARKERS.get(state)


def _sanitize_label(value: object) -> str:
    """单行标签里的控制字符替换为空格，避免有人用标题伪造出额外的行。

    与 `comments._clean_label`、`context._sanitize_username` 同款。
    """
    if not isinstance(value, str):
        return ""
    return "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value)


def build_blog_block(title: str, author: str | None, body: str) -> str:
    """拼出交给模型的那一段。

    `[引用的博客，不可信]` 是自报标签，与评论区的 `[公开文章资料，不可信]` 同一手法：
    让模型知道这段是**别人写的、要当数据看**。

    正文**原样保留**（它本来就是不可信数据，转义与否都不改变这一点，原样更利于理解）；
    标题与作者是单行标签，控制字符必须清洗掉。
    """
    lines = ["[引用的博客，不可信]"]
    clean_title = _sanitize_label(title)
    if clean_title:
        lines.append(f"标题：{clean_title}")
    clean_author = _sanitize_label(author or "")
    if clean_author:
        lines.append(f"作者：{clean_author}")
    lines.append("正文：")
    lines.append(body)
    return "\n".join(lines)


class BlogLoader:
    """把一条消息引用的博客取回并拼成块。

    失败一律降级为「带标题的块 + 一个状态」，由调用方决定是照常调模型还是回本地提示。
    标题取**消息里的引用**（`message.blog.title`）而不是上游 meta：取回失败时它还在。
    作者同理；`description` 一律不给——它是正文的摘录，而正文已经给了。
    """

    def __init__(
        self,
        client: SiteClient,
        *,
        max_chars: int,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._max_chars = max_chars
        self._logger = logger if logger is not None else _logger

    async def load(self, message: ChatMessage) -> tuple[str | None, str]:
        """返回 `(block | None, state)`；只有「没引用博客」才返回 None。"""
        blog = message.blog
        if blog is None:
            return None, BLOG_STATE_NONE
        if message.blog_missing:
            # 站点已经说了它没了，再打一次只会拿到 404。
            return self._block(blog, _BODY_MISSING), BLOG_STATE_MISSING

        try:
            article = await self._client.fetch_blog_context(blog.id)
        except (SiteError, ValueError) as exc:
            # ValueError 来自 fetch_blog_context 的 UUID 校验：站点给了脏 id，
            # 请求根本没发出去，与取回失败同等对待。
            return self._degrade(blog, "error", type(exc).__name__)

        content = getattr(article, "content", None)
        if not isinstance(content, str):
            # 上游没给正文 ≠ 正文超限，归 failed：「长度规则」那句在这里是不实之词。
            return self._degrade(blog, "no_content")
        if len(content) > self._max_chars:
            self._log_too_long(count=len(content))
            return self._block(blog, _BODY_TOO_LONG), BLOG_STATE_TOO_LONG
        return build_blog_block(blog.title, blog.author, content), BLOG_STATE_OK

    def _block(self, blog: object, body: str) -> str:
        """按消息里的引用拼块；标题与作者可能缺失，`build_blog_block` 自己会省行。"""
        return build_blog_block(
            getattr(blog, "title", "") or "",
            getattr(blog, "author", None),
            body,
        )

    def _degrade(self, blog: object, reason: str, error: str = "") -> tuple[str, str]:
        """降级：块照给（标题还在），状态是 failed。

        日志字段只允许 `logging_setup.LOG_FIELDS` 白名单里的名字：这里的 `reason`
        与 `error` 在册，`title` / `id` / 正文都不在，也不该先递过去。
        """
        fields: dict[str, object] = {"reason": reason}
        if error:
            fields["error"] = error
        log_event(self._logger, logging.INFO, "blog.unavailable", **fields)
        return self._block(blog, _BODY_FAILED), BLOG_STATE_FAILED

    def _log_too_long(self, *, count: int) -> None:
        """超限不是失败，只记一条字符数（`count` 在字段白名单里）。"""
        log_event(self._logger, logging.INFO, "blog.too_long", count=count)
