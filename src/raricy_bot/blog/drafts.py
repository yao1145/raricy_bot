"""稿库选稿（INTERFACES §53.7）。

人把写好的 Markdown 放进任务目录排队；到点从这里取一篇。选稿依据**不是**「文件还在不在」，
而是站方投递状态（设计 §7.4）—— 同一个指纹可能已经发过、正在等确认、或者确定失败了。

本模块不写回输入：文件是人维护的真相源，机器人只读。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from ..blog_records import (
    MAX_POST_ATTEMPTS,
    REASON_FILE_INVALID,
    STATUS_RETRY_WAIT,
    BlogPost,
    BlogScope,
    PreparedDraft,
)
from ..logging_setup import get_logger, log_event
from .codec import DraftError, parse_draft, prepare_draft

_logger = get_logger("blog.drafts")

# 认作 Markdown 的后缀。站点与仓库里的稿件两种写法都有，全部按大小写不敏感处理。
_MARKDOWN_SUFFIXES: tuple[str, ...] = (".md", ".markdown")

# 单个稿件文件的读取上限（1 MiB）。正文上限本身是 250000 个 UTF-16 code unit
# （最坏情况下 UTF-8 约 750 KiB），这里留出余量：再大的一定不是正文，而是放错了文件。
MAX_DRAFT_FILE_BYTES: int = 1024 * 1024


async def next_file_draft(
    task: Any,
    *,
    scope: BlogScope,
    store: Any,
    redactor: Any,
    day: str,
) -> PreparedDraft | None:
    """按文件名升序取第一篇**可投递**的稿件；没有就返回 None。

    坏稿不堵队首：解析或预校验失败的文件记一条稳定原因后继续看下一个。
    """
    drafts_dir = getattr(task, "drafts_dir", None)
    if not drafts_dir:
        return None

    names = await asyncio.to_thread(_list_draft_files, drafts_dir)
    for name in names:
        path = os.path.join(drafts_dir, name)
        text = await asyncio.to_thread(_read_draft_file, path)
        if text is None:
            _log_file_invalid(task, REASON_FILE_INVALID)
            continue
        try:
            draft = parse_draft(text)
            prepared = prepare_draft(draft, redactor=redactor)
        except DraftError as exc:
            # 带上更具体的那一个稳定原因（格式坏 / 全空），比一律 file_invalid 有用得多。
            _log_file_invalid(task, exc.reason)
            continue

        if await _is_selectable(prepared, task=task, scope=scope, store=store, day=day):
            return prepared
    return None


def _list_draft_files(drafts_dir: str) -> list[str]:
    """目录里的普通 Markdown 文件，按文件名升序。目录不存在等同队列空。"""
    try:
        entries = os.listdir(drafts_dir)
    except OSError:
        # 目录可以先空着，甚至还没建 —— 这两种都不是错误，只是没有稿子。
        return []
    names = sorted(
        entry
        for entry in entries
        if entry.lower().endswith(_MARKDOWN_SUFFIXES)
        and os.path.isfile(os.path.join(drafts_dir, entry))
    )
    return names


def _read_draft_file(path: str) -> str | None:
    """有界读取一个稿件；读不到或不是 UTF-8 文本返回 None。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_DRAFT_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_DRAFT_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _log_file_invalid(task: Any, reason: str) -> None:
    """坏稿只记任务名与稳定原因：文件路径与正文都不进日志（INTERFACES §53.13）。"""
    log_event(
        _logger,
        logging.WARNING,
        "blog.draft_invalid",
        task_name=getattr(task, "name", ""),
        reason=reason,
    )


async def _is_selectable(
    prepared: PreparedDraft,
    *,
    task: Any,
    scope: BlogScope,
    store: Any,
    day: str,
) -> bool:
    """设计 §7.4 的六种投递状态判定；`find_blog_post` 按账号隔离，不是「存在即已发布」。"""
    post: BlogPost | None = await store.find_blog_post(scope, prepared.content_hash)
    if post is None:
        return True
    if post.status == STATUS_RETRY_WAIT:
        return _retry_ready(post, task=task, day=day)
    # published / inflight / unconfirmed：已发出或结果未定，绝不重新发送；
    # rejected / abandoned：同指纹不自动再投，保留诊断记录。
    return False


def _retry_ready(post: BlogPost, *, task: Any, day: str) -> bool:
    """一份 `retry_wait` 行是否可以在**这一次**尝试里复用。

    四个条件缺一不可：同任务、栏目未变、已到重试日、尝试次数未到上限。
    任务改名或改栏目都不得静默把原稿换到别的地方去 —— 那等于用一篇旧文的额度发一篇新文。
    """
    if post.task_name != task.name:
        return False
    if post.category_id != task.category_id:
        return False
    if post.attempts >= MAX_POST_ATTEMPTS:
        return False
    if post.retry_after_day is None:
        return False
    # 两侧都是 `YYYY-MM-DD`，字典序即时间序。
    return day >= post.retry_after_day
