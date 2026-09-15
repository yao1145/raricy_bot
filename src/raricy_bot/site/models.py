"""站点 DTO 与 SSE 事件模型。

解析一律容错：字段缺失取默认值、多余字段忽略、可选块（图片/博客/拍一拍/引用）
降级为 None。唯一的例外是顶层 `id`：解析失败必须抛 `ValueError`，
因为无法定位的消息不能静默放过。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# 大区频道 id 固定值（chat-bot.md §7、§8）。
LOBBY: str = "lobby"

# SSE 事件类型（chat-bot.md §6.2）；未知类型统一归为 unknown。
_MESSAGE: str = "message"
_RESYNC: str = "resync"
_TYPING: str = "typing"
_READ: str = "read"
_UNKNOWN: str = "unknown"
_KNOWN_KINDS: frozenset[str] = frozenset({_MESSAGE, _RESYNC, _TYPING, _READ})


def _as_str(value: object, default: str = "") -> str:
    """只接受字符串，其余类型（含 None）取默认值。"""
    return value if isinstance(value, str) else default


def _as_optional_str(value: object) -> str | None:
    """只接受字符串，其余类型（含 None）为 None。"""
    return value if isinstance(value, str) else None


def _as_bool(value: object, default: bool = False) -> bool:
    """只接受真正的布尔值，其余类型取默认值。"""
    return value if isinstance(value, bool) else default


def _coerce_int(value: object) -> int:
    """把 JSON 里的整数或数字字符串转成 int；其余一律抛 ValueError。"""
    if isinstance(value, bool) or value is None:
        raise ValueError("不是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("不是整数")
        return int(value)
    if isinstance(value, str):
        # 非数字字符串由 int() 抛出 ValueError。
        return int(value.strip())
    raise ValueError("不是整数")


def _coerce_optional_int(value: object) -> int | None:
    """尽量转 int，转不动则为 None（用于 read 帧的 message_id）。"""
    try:
        return _coerce_int(value)
    except ValueError:
        return None


def _parse_author(value: object) -> "Author":
    """解析作者；非映射或字段缺失都退化为空作者。"""
    if not isinstance(value, Mapping):
        return Author(id="", username="")
    return Author(
        id=_as_str(value.get("id")),
        username=_as_str(value.get("username")),
        avatar_url=_as_str(value.get("avatar_url")),
        is_admin=_as_bool(value.get("is_admin")),
    )


def _parse_image(value: object) -> "ImageRef | None":
    """解析图片引用；非映射（含 null）为 None。"""
    if not isinstance(value, Mapping):
        return None
    return ImageRef(
        id=_as_str(value.get("id")),
        url=_as_str(value.get("url")),
        mime_type=_as_str(value.get("mime_type")),
    )


def _parse_blog(value: object) -> "BlogRef | None":
    """解析博客引用；非映射（含 null）为 None。"""
    if not isinstance(value, Mapping):
        return None
    return BlogRef(
        id=_as_str(value.get("id")),
        title=_as_str(value.get("title")),
        description=_as_str(value.get("description")),
        author=_as_optional_str(value.get("author")),
        updated_at=_as_str(value.get("updated_at")),
    )


def _parse_pat(value: object) -> "PatRef | None":
    """解析拍一拍引用；非映射（含 null）为 None。"""
    if not isinstance(value, Mapping):
        return None
    return PatRef(
        target_id=_as_str(value.get("target_id")),
        target_name=_as_str(value.get("target_name")),
    )


def _parse_number(value: object) -> float | None:
    """尽量转 float；布尔、None 与非数字字符串一律为 None。

    投票的百分比是站点算好的展示值，缺失只影响那一行的显示，不影响判定。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_reply(value: object) -> "ReplyRef | None":
    """解析引用块；id 解析失败时整块降级为 None（比丢掉整条消息好）。"""
    if not isinstance(value, Mapping):
        return None
    try:
        reply_id = _coerce_int(value.get("id"))
    except ValueError:
        return None
    return ReplyRef(
        id=reply_id,
        content=_as_str(value.get("content")),
        author_name=_as_optional_str(value.get("author_name")),
        is_deleted=_as_bool(value.get("is_deleted")),
        image_url=_as_optional_str(value.get("image_url")),
    )


@dataclass(frozen=True)
class Author:
    """消息作者（chat-bot.md §11.1）。"""

    id: str
    username: str
    avatar_url: str = ""
    is_admin: bool = False


@dataclass(frozen=True)
class ImageRef:
    """消息引用的图床图片。"""

    id: str
    url: str
    mime_type: str


@dataclass(frozen=True)
class BlogRef:
    """消息引用的博客。"""

    id: str
    title: str
    description: str
    author: str | None
    updated_at: str


@dataclass(frozen=True)
class PatRef:
    """拍一拍消息的目标。"""

    target_id: str
    target_name: str


@dataclass(frozen=True)
class ReplyRef:
    """被引用的消息摘要。"""

    id: int
    content: str
    author_name: str | None
    is_deleted: bool
    image_url: str | None


@dataclass(frozen=True)
class Clipboard:
    """一篇云剪贴板（`[@8位ID]` 引用的目标）。

    正文是**另一名用户**写的 Markdown，属于不可信数据；它只用于当轮外送，
    不进历史、不落库、不写日志。
    """

    id: str
    title: str
    author: str | None
    content: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Clipboard | None":
        """从 `GET /api/clipboard/<id>` 的信封里解析；没有可用正文返回 None。"""
        clip = payload.get("clip")
        if not isinstance(clip, Mapping):
            return None
        content = clip.get("content")
        if not isinstance(content, str):
            return None
        return cls(
            id=_as_str(clip.get("id")),
            title=_as_str(clip.get("title")),
            author=_as_optional_str(clip.get("author_name")),
            content=content,
        )


@dataclass(frozen=True)
class VoteOption:
    """投票的一个选项；`percentage` 缺失时为 None。"""

    label: str
    count: int
    percentage: float | None = None


@dataclass(frozen=True)
class Vote:
    """一个投票（`[@9位ID]` 引用的目标）。标题与选项文案同样不可信。"""

    id: str
    title: str
    author: str | None
    total_votes: int
    is_locked: bool
    options: tuple[VoteOption, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Vote | None":
        """从 `GET /api/votes/<id>` 的信封里解析；载荷不可用时返回 None。"""
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return None
        raw_options = data.get("options")
        options: list[VoteOption] = []
        if isinstance(raw_options, list):
            for item in raw_options:
                if not isinstance(item, Mapping):
                    continue
                try:
                    count = _coerce_int(item.get("count"))
                except ValueError:
                    count = 0
                options.append(
                    VoteOption(
                        label=_as_str(item.get("label")),
                        count=count,
                        percentage=_parse_number(item.get("percentage")),
                    )
                )
        try:
            total = _coerce_int(data.get("total_votes"))
        except ValueError:
            total = 0
        return cls(
            id=_as_str(data.get("id")),
            title=_as_str(data.get("title")),
            author=_as_optional_str(data.get("author_name")),
            total_votes=total,
            is_locked=_as_bool(data.get("is_locked")),
            options=tuple(options),
        )


@dataclass(frozen=True)
class ChatMessage:
    """一条站内消息。"""

    id: int
    channel_id: str
    author: Author
    content: str
    image: ImageRef | None
    image_missing: bool
    blog: BlogRef | None
    blog_missing: bool
    pat: PatRef | None
    reply: ReplyRef | None
    is_deleted: bool
    created_at: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChatMessage":
        """从 DTO 解析；顶层 id 解析失败抛 ValueError，其余字段容错。"""
        if not isinstance(data, Mapping):
            raise ValueError("消息体不是映射")
        try:
            message_id = _coerce_int(data.get("id"))
        except ValueError as exc:
            raise ValueError("消息 id 缺失或非数字") from exc
        return cls(
            id=message_id,
            channel_id=_as_str(data.get("channel_id")),
            author=_parse_author(data.get("author")),
            content=_as_str(data.get("content")),
            image=_parse_image(data.get("image")),
            image_missing=_as_bool(data.get("image_missing")),
            blog=_parse_blog(data.get("blog")),
            blog_missing=_as_bool(data.get("blog_missing")),
            pat=_parse_pat(data.get("pat")),
            reply=_parse_reply(data.get("reply")),
            is_deleted=_as_bool(data.get("is_deleted")),
            created_at=_as_str(data.get("created_at")),
        )


@dataclass(frozen=True)
class StreamEvent:
    """一帧 SSE 事件；未用到的字段一律为 None。"""

    kind: str
    event_id: int | None
    channel_id: str | None
    message: ChatMessage | None
    user_id: str | None
    username: str | None
    message_id: int | None

    @classmethod
    def from_sse(cls, data: str, event_id: int | None) -> "StreamEvent | None":
        """解析 SSE 的 data 行；JSON 非法或顶层不是对象返回 None，永不抛异常。"""
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None

        raw_kind = payload.get("type")
        kind = raw_kind if isinstance(raw_kind, str) and raw_kind in _KNOWN_KINDS else _UNKNOWN
        channel_id: str | None = None
        user_id: str | None = None
        username: str | None = None
        message_id: int | None = None
        message: ChatMessage | None = None

        if kind == _MESSAGE:
            message = _parse_stream_message(payload)
            if message is None:
                # 消息体不可用时降级为 unknown，交给上层按未知事件忽略。
                kind = _UNKNOWN
            else:
                channel_id = _as_optional_str(payload.get("channel_id"))
        elif kind == _TYPING:
            channel_id = _as_optional_str(payload.get("channel_id"))
            user_id = _as_optional_str(payload.get("user_id"))
            username = _as_optional_str(payload.get("username"))
        elif kind == _READ:
            channel_id = _as_optional_str(payload.get("channel_id"))
            user_id = _as_optional_str(payload.get("user_id"))
            message_id = _coerce_optional_int(payload.get("message_id"))

        return cls(
            kind=kind,
            event_id=event_id,
            channel_id=channel_id,
            message=message,
            user_id=user_id,
            username=username,
            message_id=message_id,
        )


def _parse_stream_message(payload: Mapping[str, Any]) -> ChatMessage | None:
    """解析 message 帧的载荷；缺失、非映射或 id 非法都返回 None。"""
    raw_message = payload.get("message")
    if not isinstance(raw_message, Mapping):
        return None
    try:
        return ChatMessage.from_dict(raw_message)
    except ValueError:
        return None
