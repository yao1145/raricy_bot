"""消息路由器：把 SSE 事件与 resync 消息判定为「入队 / 本地回复 / 忽略」。

职责边界（INTERFACES.md §12）：
- 只做判定与入队，**不发消息、不调模型、不写除 `Store` 之外的任何东西**；
- `handle_stream` 只做 kind 分派，真正的判定在 `handle_message`；
- `record_event` 只在通过候选过滤（第 1-5 步）之后调用（D-15），因此
  第 1-5 步的 `ignored` 不落库、不需要 `mark_handled`；第 6 步之后的
  `reply_now` / `busy` 由 app 标记，`queued` 由 worker 标记，路由器一律不标记。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .. import texts
from ..config import BehaviorConfig, StorageConfig
from ..logging_setup import get_logger, log_event
from ..site.models import LOBBY, ChatMessage, StreamEvent
from ..store import Store
from ..text_utils import (
    contains_bot_mention,
    has_media,
    is_help_command,
    is_reset_command,
    is_secret_probe,
    strip_bot_mention,
)
from .context import ContextManager, dm_session_key, lobby_thread_session_key

_logger = get_logger("core.router")

# SSE 中需要静默忽略的 kind，它们的 reason 与 kind 同名。
_PASSIVE_KINDS: tuple[str, ...] = ("typing", "read")

# 值得运维关注的「可行动」结果：会入队、会回本地文案、或推进水位。
# 其余（typing/read/unknown_event/malformed 及各类前置过滤）是大区高频背景，
# 降到 DEBUG，避免活跃站点上每个 SSE 帧刷一行 INFO（设计文档 §4 要求极简日志）。
_ACTIONABLE_REASONS: frozenset[str] = frozenset(
    {
        "queued",
        "busy",
        "resync",
        "recovered_sent",
        "help",
        "reset",
        "empty",
        "media_only",
        "too_long",
        "secret_probe",
    }
)


@dataclass(frozen=True)
class Request:
    """一条待模型处理的任务；由 worker 消费。"""

    event_id: int | None  # resync 路径为 None
    channel_id: str
    channel_kind: str  # "lobby" | "dm"
    session_key: str
    generation: int  # 创建时的会话代次；worker 用它判断请求是否已被 /reset 作废
    message: ChatMessage
    user_text: str  # 已剔除 @机器人 的正文
    reply_context: str | None  # message.reply 非删除时的正文，否则 None
    thread_root_id: int | None = None  # 大区共享链的根；**私聊恒为 None**（D-20）


@dataclass(frozen=True)
class RouteResult:
    """一次判定的结果；`reason` 是稳定短标识，仅用于日志。"""

    action: str  # "queued" | "reply_now" | "ignored" | "busy" | "resync"
    channel_id: str | None
    message_id: int | None
    reply_to: int | None
    text: str | None  # reply_now / busy 时的本地文案
    request: Request | None
    reason: str
    actor_id: str | None = None  # 触发者；主动通知按它计冷却（D-18）
    thread_root_id: int | None = None  # 同 Request；DM 恒为 None


class MessageRouter:
    """按 INTERFACES.md §12 的判定顺序处理消息。"""

    def __init__(
        self,
        *,
        self_user_id: str,
        bot_username: str,
        ctx: ContextManager,
        store: Store,
        queue: asyncio.Queue[Request],
        cfg: BehaviorConfig,
        storage: StorageConfig,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._self_user_id = self_user_id
        self._bot_username = bot_username
        self._ctx = ctx
        self._store = store
        self._queue = queue
        self._cfg = cfg
        self._storage = storage
        self._now = now
        self._logger = _logger

    # --- 事件分派 -----------------------------------------------------------

    async def handle_stream(self, event: StreamEvent) -> RouteResult:
        """SSE 帧的 5 步分派（§12）。"""
        if event.kind in _PASSIVE_KINDS:
            return self._emit(
                "ignored",
                event.kind,
                channel_id=event.channel_id,
                message_id=event.message_id,
                reply_to=None,
                event_id=event.event_id,
            )
        if event.kind == "resync":
            return self._emit(
                "resync",
                "resync",
                channel_id=event.channel_id,
                message_id=event.message_id,
                reply_to=None,
                event_id=event.event_id,
            )
        if event.kind != "message":
            return self._emit(
                "ignored",
                "unknown_event",
                channel_id=event.channel_id,
                message_id=event.message_id,
                reply_to=None,
                event_id=event.event_id,
            )
        if event.message is None:
            return self._emit(
                "ignored",
                "malformed",
                channel_id=event.channel_id,
                message_id=event.message_id,
                reply_to=None,
                event_id=event.event_id,
            )
        return await self.handle_message(
            event.channel_id or event.message.channel_id,
            event.message,
            event.event_id,
        )

    # --- 候选过滤与判定 -----------------------------------------------------

    async def handle_message(
        self, channel_id: str, message: ChatMessage, event_id: int | None
    ) -> RouteResult:
        """消息的判定顺序（§12）；`record_event` 在第 6 步才调用（D-15）。"""
        is_lobby = channel_id == LOBBY

        # 1. 私聊频道登记（大区不登记）。
        if not is_lobby:
            await self._store.upsert_dm_channel(channel_id, message.id)

        # 2-4. 三类无需落库的候选过滤。
        if message.author.id == self._self_user_id:
            # 大区里机器人自己的消息要先做一次「回显补登记」，再静默结束：
            # 站点成功信封没带消息对象、或本地映射写入失败时，这是唯一的兜底。
            if is_lobby and message.reply is not None and not message.reply.is_deleted:
                await self._attach_self_echo(message)
            return self._emit(
                "ignored",
                "self_message",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                event_id=event_id,
            )
        if message.is_deleted:
            return self._emit(
                "ignored",
                "deleted",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                event_id=event_id,
            )
        if message.pat is not None:
            return self._emit(
                "ignored",
                "pat",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                event_id=event_id,
            )

        # 5. 频道判定与正文提取。
        if is_lobby:
            if not contains_bot_mention(message.content, self._bot_username):
                return self._emit(
                    "ignored",
                    "no_mention",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    channel_kind="lobby",
                    event_id=event_id,
                )
            user_text = strip_bot_mention(message.content, self._bot_username)
            channel_kind = "lobby"
            # 大区的 session_key 由第 8 步解析出的链决定，这里还不能定。
            session_key = ""
        else:
            # 私聊同样剔除习惯性的 @机器人（D-6，用户常在此处也 @ 机器人）。
            user_text = strip_bot_mention(message.content, self._bot_username)
            session_key = dm_session_key(channel_id)
            channel_kind = "dm"

        # 6. 主去重键拦截：只有通过前面过滤的候选消息才落库（D-15）。
        if not await self._store.record_event(event_id, message.id, channel_id):
            # 该 message_id 已有记录。两种可能：
            #   (a) 本进程已入队的重复投递 —— 应当忽略；
            #   (b) **上一进程崩溃时遗留的未完成事件**（启动时被
            #       mark_orphans_recoverable() 标成 recover）—— 必须重新认领。
            # 没有 (b) 这条分支的话，崩溃后靠水位补发回来的消息会被当成 duplicate
            # 丢掉：消息永远不处理、该行永远 pending、水位永远卡在它之前。
            if not await self._store.reclaim_orphan(message.id):
                return self._emit(
                    "ignored",
                    "duplicate",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    channel_kind=channel_kind,
                    event_id=event_id,
                )
            # 认领成功。但旧进程可能**其实已经回复过**、只是没来得及标记完成，
            # 那就补一个完成标记即可，绝不能再回一遍。
            if await self._store.find_sent_for_reply(channel_id, message.id) is not None:
                await self._store.mark_handled(message.id, "done")
                return self._emit(
                    "ignored",
                    "recovered_sent",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    channel_kind=channel_kind,
                    event_id=event_id,
                )

        # 7. 引用上下文（已删除的引用视为无）。
        reply_context: str | None = None
        if message.reply is not None and not message.reply.is_deleted:
            reply_context = message.reply.content

        # 8. 大区解析共享链并登记本条消息（私聊跳过，thread_root_id 恒为 None）。
        thread_root_id: int | None = None
        if is_lobby:
            thread_root_id = await self._resolve_thread(
                channel_id, message, user_text, event_id
            )
            if thread_root_id is None:
                # 解析失败已经记过日志：不调模型、不回话，事件保持非终态等补发。
                return self._emit(
                    "ignored",
                    "thread_resolve_failed",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    channel_kind=channel_kind,
                    event_id=event_id,
                )
            session_key = lobby_thread_session_key(thread_root_id)

        # 9.1 空正文：有媒体给纯媒体提示，否则给用法提示。
        if not user_text:
            if has_media(message):
                return self._emit(
                    "reply_now",
                    "media_only",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.UNSUPPORTED_MEDIA_TEXT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
            return self._emit(
                "reply_now",
                "empty",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=texts.USAGE_HINT,
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )

        # 9.2 /help 本地应答，不触发模型。
        if is_help_command(user_text):
            return self._emit(
                "reply_now",
                "help",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=texts.HELP_TEXT,
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )

        # 9.3 /reset：大区只建新链（第 8 步已用 force_new 建好，旧链不动，D-21）；
        #     私聊仍是清空当前会话并递增代次（D-9）。
        if is_reset_command(user_text):
            if not is_lobby:
                self._ctx.reset(session_key)
            return self._emit(
                "reply_now",
                "reset",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=texts.RESET_DONE_TEXT,
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )

        # 9.4 有媒体但正文非空：忽略媒体，照常处理文本（无需额外分支）。

        # 9.5 超长输入本地拦截。
        if len(user_text) > self._cfg.max_input_chars:
            return self._emit(
                "reply_now",
                "too_long",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=texts.TOO_LONG_TEXT,
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )

        # 9.6 索取系统提示 / 密钥本地拒绝。
        if is_secret_probe(user_text):
            return self._emit(
                "reply_now",
                "secret_probe",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=texts.SECRET_REFUSAL_TEXT,
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )

        # 10. 入队交给 worker；队列满则回 busy。
        request = Request(
            event_id=event_id,
            channel_id=channel_id,
            channel_kind=channel_kind,
            session_key=session_key,
            # 记下创建时的代次；worker 会在调模型前与模型返回后各比一次，
            # 用来丢弃已被 /reset 作废的过期请求（§16）。
            generation=self._ctx.generation(session_key),
            message=message,
            user_text=user_text,
            reply_context=reply_context,
            thread_root_id=thread_root_id,
        )
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            return self._emit(
                "busy",
                "queue_full",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=texts.BUSY_NOTICE_TEXT,
                actor_id=message.author.id,
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )
        return self._emit(
            "queued",
            "queued",
            channel_id=channel_id,
            message_id=message.id,
            reply_to=message.id,
            request=request,
            channel_kind=channel_kind,
            thread_root_id=thread_root_id,
            event_id=event_id,
        )

    # --- 大区共享链 ---------------------------------------------------------

    async def _resolve_thread(
        self, channel_id: str, message: ChatMessage, user_text: str, event_id: int | None
    ) -> int | None:
        """解析这条大区消息属于哪条链，并登记它；失败返回 None 并记一条无正文错误。

        失败时调用方必须**不回话**：事件保持非终态，靠 SSE 补发或下次重启重来（D-16）。
        用消息 id 而不是引用正文或用户名决定归属（D-20）。
        """
        reply_id = (
            message.reply.id
            if message.reply is not None and not message.reply.is_deleted
            else None
        )
        try:
            return await self._store.resolve_lobby_thread(
                message.id,
                reply_id,
                # /reset 无论回复谁，都以自己为根建一条新链（D-21）。
                force_new=is_reset_command(user_text),
                now=self._now(),
                retention_seconds=self._storage.lobby_thread_retention_seconds,
            )
        except Exception as exc:  # 写不进去就不处理这条消息，绝不猜测归属
            log_event(
                self._logger,
                logging.ERROR,
                "router.thread_resolve_failed",
                channel_id=channel_id,
                message_id=message.id,
                error=type(exc).__name__,
            )
            return None

    async def _attach_self_echo(self, message: ChatMessage) -> None:
        """把机器人自己的大区消息补登记到它回复的那条链上。

        兜底两种情形：站点成功信封没带消息对象，或本地映射写入失败。
        失败只记一条无正文错误：这条回显本来就不该产生任何回复或循环。
        """
        reply = message.reply
        if reply is None:  # pragma: no cover - 调用方已判空
            return
        try:
            now = self._now()
            root = await self._store.find_active_lobby_thread(
                reply.id,
                now=now,
                retention_seconds=self._storage.lobby_thread_retention_seconds,
            )
            if root is None:
                return
            await self._store.attach_lobby_message(message.id, root, now=now)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "router.self_echo_attach_failed",
                channel_id=LOBBY,
                message_id=message.id,
                error=type(exc).__name__,
            )

    # --- 内部工具 -----------------------------------------------------------

    def _emit(
        self,
        action: str,
        reason: str,
        *,
        channel_id: str | None,
        message_id: int | None,
        reply_to: int | None,
        text: str | None = None,
        request: Request | None = None,
        actor_id: str | None = None,
        channel_kind: str | None = None,
        event_id: int | None = None,
        thread_root_id: int | None = None,
    ) -> RouteResult:
        """构造 RouteResult 并按白名单字段记一条日志。"""
        result = RouteResult(
            action=action,
            channel_id=channel_id,
            message_id=message_id,
            reply_to=reply_to,
            text=text,
            request=request,
            reason=reason,
            actor_id=actor_id,
            thread_root_id=thread_root_id,
        )
        # 只输出 LOG_FIELDS 白名单内的稳定字段，且不打印空值。
        fields: dict[str, object] = {"reason": reason}
        if channel_id is not None:
            fields["channel_id"] = channel_id
        if message_id is not None:
            fields["message_id"] = message_id
        if channel_kind is not None:
            fields["channel_kind"] = channel_kind
        if event_id is not None:
            fields["event_id"] = event_id
        # 被动/高频结果记 DEBUG，可行动结果记 INFO（设计文档 §4：极简日志）。
        level = logging.INFO if reason in _ACTIONABLE_REASONS else logging.DEBUG
        log_event(self._logger, level, "router.route", **fields)
        return result
