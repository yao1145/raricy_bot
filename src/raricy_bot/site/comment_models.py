"""博客评论接口 DTO 与受限解析工具。"""

from __future__ import annotations

import datetime as _datetime
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any


def normalize_uuid(value: object) -> str | None:
    """验证并规范化站点对象 UUID；非法值返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def parse_comment_time(value: object) -> float | None:
    """解析站点的假 Z 时间：墙上时间按 UTC+8 解释后转换为 epoch。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        parsed = _datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone(_datetime.timedelta(hours=8)))
    return parsed.timestamp()


def _text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _bool(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def actor_fingerprint(actor_id: object) -> str | None:
    """以不可逆 SHA-256 指纹表示通知 actor。"""
    if not isinstance(actor_id, str) or not actor_id:
        return None
    import hashlib

    return hashlib.sha256(("comment-actor\0" + actor_id).encode("utf-8")).hexdigest()


def iter_comment_nodes(nodes: tuple["CommentNode", ...] | list["CommentNode"]):
    """用显式栈展平评论树，避免递归遍历。"""
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def count_comment_nodes(nodes: tuple["CommentNode", ...] | list["CommentNode"], limit: int) -> int:
    """统计评论树节点数，超过上限时立即抛出。"""
    count = 0
    for _ in iter_comment_nodes(nodes):
        count += 1
        if count > limit:
            raise CommentTreeTooLarge("comment_tree_too_large")
    return count


@dataclass(frozen=True)
class CommentAuthor:
    """评论作者最小 DTO。"""

    id: str | None = None
    username: str | None = None
    is_admin: bool = False

    @classmethod
    def from_dict(cls, data: object) -> "CommentAuthor":
        if not isinstance(data, Mapping):
            return cls()
        raw_id = data.get("id")
        return cls(
            id=raw_id if isinstance(raw_id, str) and raw_id else None,
            username=(
                data.get("username")
                if isinstance(data.get("username"), str)
                else None
            ),
            is_admin=_bool(data.get("is_admin")),
        )


class CommentTreeTooLarge(ValueError):
    """评论树超过节点上限，整棵树不可安全处理。"""


@dataclass(frozen=True)
class CommentNode:
    """评论节点；spider 接口返回的 content 固定为 None。"""

    id: str
    blog_id: str
    author: CommentAuthor
    parent_id: str | None = None
    root_id: str | None = None
    content: str | None = None
    content_html: str = ""
    status: str = ""
    is_deleted: bool = False
    created_at: float | None = None
    updated_at: float | None = None
    has_image: bool = False
    has_quoted_blog: bool = False
    children: tuple["CommentNode", ...] = ()

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, require_ids: bool = True, max_nodes: int = 10000
    ) -> "CommentNode":
        """迭代容错解析单个节点；避免深树触发 Python 递归上限。"""
        if not isinstance(data, Mapping):
            raise ValueError("评论节点不是映射")
        if max_nodes < 1:
            raise CommentTreeTooLarge("comment_tree_too_large")
        stack: list[tuple[Mapping[str, Any], bool]] = [(data, False)]
        built: dict[int, CommentNode] = {}
        count = 0
        while stack:
            current, expanded = stack.pop()
            marker = id(current)
            if not expanded:
                count += 1
                if count > max_nodes:
                    raise CommentTreeTooLarge("comment_tree_too_large")
                stack.append((current, True))
                children_raw = current.get("children")
                if isinstance(children_raw, list):
                    for child in reversed(children_raw):
                        if isinstance(child, Mapping):
                            stack.append((child, False))
                continue
            comment_id = normalize_uuid(current.get("id"))
            blog_id = normalize_uuid(current.get("blog_id"))
            if require_ids and (comment_id is None or blog_id is None):
                # 无法安全定位的节点不应进入树；它的父节点仍可继续解析。
                continue
            if comment_id is None:
                comment_id = _text(current.get("id"))
            if blog_id is None:
                blog_id = _text(current.get("blog_id"))
            child_values: list[CommentNode] = []
            children_raw = current.get("children")
            if isinstance(children_raw, list):
                for child in children_raw:
                    value = built.get(id(child)) if isinstance(child, Mapping) else None
                    if value is not None:
                        child_values.append(value)
            raw_image = current.get("image")
            raw_blog = current.get("blog")
            built[marker] = cls(
                id=comment_id,
                blog_id=blog_id,
                author=CommentAuthor.from_dict(current.get("author")),
                parent_id=normalize_uuid(current.get("parent_id")),
                root_id=normalize_uuid(current.get("root_id")),
                content=current.get("content") if isinstance(current.get("content"), str) else None,
                content_html=_text(current.get("content_html")),
                status=_text(current.get("status")),
                is_deleted=_bool(current.get("is_deleted")),
                created_at=parse_comment_time(current.get("created_at")),
                updated_at=parse_comment_time(current.get("updated_at")),
                has_image=(
                    _bool(current.get("has_image"))
                    or _bool(current.get("image_missing"))
                    or isinstance(raw_image, Mapping)
                ),
                has_quoted_blog=(
                    _bool(current.get("has_quoted_blog"))
                    or _bool(current.get("blog_missing"))
                    or isinstance(raw_blog, Mapping)
                ),
                children=tuple(child_values),
            )
        result = built.get(id(data))
        if result is None:
            raise ValueError("评论节点 id 或文章 id 非法")
        return result


def strip_comment_content(node: CommentNode) -> CommentNode:
    """迭代移除 spider 评论树中的 Markdown 正文，保留 HTML 粗筛字段。"""
    stack: list[tuple[CommentNode, bool]] = [(node, False)]
    rebuilt: dict[int, CommentNode] = {}
    while stack:
        current, expanded = stack.pop()
        marker = id(current)
        if not expanded:
            stack.append((current, True))
            for child in reversed(current.children):
                stack.append((child, False))
            continue
        rebuilt[marker] = replace(
            current,
            content=None,
            children=tuple(rebuilt[id(child)] for child in current.children),
        )
    return rebuilt[id(node)]


@dataclass(frozen=True)
class CommentNotification:
    """通知 DTO；仅保留匹配需要的字段。"""

    id: str
    action: str
    actor_id: str | None
    blog_id: str | None
    timestamp: float | None
    read: bool

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CommentNotification":
        if not isinstance(data, Mapping):
            raise ValueError("通知不是映射")
        notification_id = _text(data.get("id"))
        if not notification_id:
            raise ValueError("通知 id 缺失")
        actor = data.get("actor")
        actor_id = actor.get("id") if isinstance(actor, Mapping) else None
        if not isinstance(actor_id, str) or not actor_id:
            actor_id = None
        obj = data.get("object")
        blog_id = obj.get("id") if isinstance(obj, Mapping) and obj.get("type") == "blog" else None
        blog_id = normalize_uuid(blog_id)
        return cls(
            id=notification_id,
            action=_text(data.get("action")),
            actor_id=actor_id,
            blog_id=blog_id,
            timestamp=parse_comment_time(data.get("timestamp")),
            read=_bool(data.get("read")),
        )


@dataclass(frozen=True)
class NotificationPage:
    """通知分页 DTO。"""

    notifications: tuple[CommentNotification, ...]
    page: int = 1
    pages: int = 1
    has_next: bool = False
    unread_count: int = 0


@dataclass(frozen=True)
class BlogContext:
    """本轮模型调用使用的文章资料。"""

    id: str
    title: str = ""
    content: str | None = None
