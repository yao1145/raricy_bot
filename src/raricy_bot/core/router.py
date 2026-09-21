"""消息路由器：把 SSE 事件与 resync 消息判定为「入队 / 本地回复 / 忽略」。

职责边界（INTERFACES.md §12）：
- 只做判定与入队，**不发消息、不调模型、不写除 `Store` 之外的任何东西**；
- `handle_stream` 只做 kind 分派，真正的判定在 `handle_message`；
- `record_event` 只在通过候选过滤（第 1-5 步）之后调用（D-15），因此
  第 1-5 步的 `ignored` 不落库、不需要 `mark_handled`；第 6 步之后的
  `reply_now` / `busy` 由 app 标记，`queued` 由 worker 标记，路由器一律不标记。

记忆命令（INTERFACES.md §34.1）在这一层只做三件事：算授权、构造请求、入队 ——
不碰 Markdown、不调 AI、不让任何记忆正文流进 `Request` 或模型消息。三个记忆参数
（`memory_access` / `memory_queue` / `private_enabled`）都不注入时整条记忆路径不存在，
行为与升级前逐字节一致（D-60 的回退路径），此时 `Request.memory_allowed` 恒为 `False`。

大区近期消息（INTERFACES.md §38、D-95）是这一层的第二件新职责：**每条**大区消息
（包括机器人自己的公开回复与未 `@` 的普通消息）在进入任何过滤之前先交给缓冲器观察，
但只有真正构造出 `Request` 且成功入队时才消费那一批。本地回复、被忽略的消息与队列满
都不消费，因此不需要为它们各写一次清理。缓冲器不注入时这条路径整体不存在。

**会话 subject（INTERFACES.md §47.2/§47.3、R1）是这一层的第三件事，也是唯一一处把
「原始作者」变成「不可逆 owner key」的地方**：当前作者的 subject 落在
`Request.public_memory_subject`，旁观消息的 subject 随 `observe` 进缓冲器。两处都只在
记忆路径装配（`memory_access` 注入）时才计算，未装配时恒为 `None`、不做任何哈希；
`core/context.py` 与 `core/lobby_context.py` 则完全不认识 memory（D-61），
它们只是搬运这段元数据。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .. import texts
from ..capabilities import CAPABILITY_BY_FEATURE
from ..config import BehaviorConfig, StorageConfig
from ..logging_setup import get_logger, log_event, new_trace_id
from ..memory.access import MemoryAccessPolicy
from ..memory.commands import (
    MemoryCommand,
    MemoryCommandRequest,
    parse_memory_command,
)
from ..memory.models import user_storage_key
from ..site.models import LOBBY, Author, ChatMessage, StreamEvent
from ..store import Store
from ..text_utils import (
    contains_bot_mention,
    has_image,
    has_media,
    is_help_command,
    leading_capability_command,
    parse_capability_command,
    is_reset_command,
    is_secret_probe,
    strip_bot_mention,
)
from .context import (
    ContextManager,
    ConversationSubject,
    dm_session_key,
    lobby_thread_session_key,
    sanitize_username,
)
from .lobby_context import LobbyRecentContextBuffer, LobbyRecentMessage

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
        "image_only",
        "blog_only",
        "too_long",
        "secret_probe",
        "kb_usage",
        # 无参数的能力命令：reason 按能力名取，运维一眼看得出用户想用哪个能力。
        "search_usage",
        "zhihu_usage",
        "map_usage",
        "wolfram_usage",
        "capability_conflict",
        # 记忆命令的四个结果（§34.1）：都会入队或回一条本地文案，同样值得运维看见。
        # 它们只承载稳定短标识，不含命令名、命令参数或用户 ID（§37 的日志红线）。
        "memory_queued",
        "memory_dm_only",
        "memory_beta_denied",
        "memory_queue_full",
    }
)


def _conversation_subject(author: Author) -> ConversationSubject | None:
    """把站点作者映射成不可逆的会话 subject（§45.1、§47.2）；算不出来时返回 None。

    `key` 是 `user_storage_key(author.id)`（§27.3），`label` 用 `sanitize_username` 清洗过
    控制字符 —— 清洗规则只有 `core/context.py` 那一份实现。

    编码不出存储键的畸形 `id`（孤立代理项会让严格 UTF-8 编码抛 `UnicodeEncodeError`）
    按「拿不到身份」处理：`memory/service.py` 的 `_storage_key` 是同一口径，那里也认
    「这个 ID 没有可用的存储键」。记忆是软故障，一个怪 id 不能把一条本来能回的消息变成异常。
    """
    if not author.id:
        return None
    try:
        key = user_storage_key(author.id)
    except UnicodeEncodeError:
        return None
    return ConversationSubject(key=key, label=sanitize_username(author.username))


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
    enabled_features: frozenset[str] = frozenset()
    # 当前作者是否可用共同记忆（§34.1 第 2 条）：由 Router 用 `message.author.id` 算出。
    # 记忆未注入或门禁关闭时恒为 `False`，worker 据此决定要不要取记忆上下文（§34.3）。
    memory_allowed: bool = False
    # 当前作者的会话 subject（§47.2、公开设计 §14.2）：Router 在入队前用
    # `user_storage_key(message.author.id)` 与 `sanitize_username(username)` 算好。
    # 它**不是**作者 ID 的替身（原始 id 本来就在 `message` 里），而是一个不可逆的 owner key
    # 加一个展示标签，供 App 解析公开记忆、以及发送成功后提交历史时使用。
    # 记忆未装配或拿不到作者 id 时恒为 `None`，App 因此不做任何公开记忆解析（R1）。
    public_memory_subject: ConversationSubject | None = None
    # 这条消息被唤起之前积累的大区公开消息（§38.2），入队那一刻固化；**私聊恒为空元组**。
    # 用不可变 tuple 而不是引用：请求进了队列之后，缓冲器还可以继续被 SSE 改动。
    lobby_recent: tuple[LobbyRecentMessage, ...] = ()
    # 本地随机关联标识（计划 §4）：把同一条消息的路由判定、模型失败与发送结果串起来。
    # 它只由随机数生成，不编码 message_id / 用户 / 频道 —— 因此可以安全地进日志与归档。
    trace_id: str = ""


@dataclass(frozen=True)
class RouteResult:
    """一次判定的结果；`reason` 是稳定短标识，仅用于日志。"""

    # "queued" | "reply_now" | "ignored" | "busy" | "resync" | "memory_queued"。
    # `memory_queued` **不是** `queued`：app 不把它当聊天请求处理，终态由记忆 worker 负责（§34.1、§34.3）。
    action: str
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
        vision_enabled: bool = False,
        kb_enabled: bool = False,
        memory_access: MemoryAccessPolicy | None = None,
        memory_queue: asyncio.Queue[MemoryCommandRequest] | None = None,
        private_enabled: Callable[[str | None], bool] | None = None,
        capabilities: frozenset[str] = frozenset(),
        lobby_recent: LobbyRecentContextBuffer | None = None,
    ) -> None:
        self._self_user_id = self_user_id
        self._bot_username = bot_username
        self._ctx = ctx
        self._store = store
        self._queue = queue
        self._cfg = cfg
        self._storage = storage
        self._now = now
        self._vision_enabled = vision_enabled
        # 只影响 /help 说不说实话：知识库没开时不得宣传 /kb。判定本身不依赖它。
        self._kb_enabled = kb_enabled
        # 记忆是可选能力：注入与否就是总开关（policy 不暴露 enabled，§28）。
        # 两者都不注入时记忆命令不存在，`/remember` 只是普通正文，行为与升级前一致（D-60）。
        self._memory_access = memory_access
        self._memory_queue = memory_queue
        # 只影响 /help 的措辞：由 app 接到 `MemoryService.private_settings_cached`（D-67），
        # 必须同步、无 I/O；未注入或取不到时按未开启处理。
        self._private_enabled = private_enabled
        # 同样只影响 /help 说不说实话：能力没开时不得宣传对应命令。
        # **不参与命令解析** —— 关闭的能力仍会被识别成本地不可用提示，与 /search 的既有行为一致。
        self._capabilities = capabilities
        # 大区近期消息缓冲（§38）：注入与否就是这条路径的总开关，与记忆同款。
        # 它是**外部持有**的实例（app 装配），不是模块全局 —— 测试之间不共享状态。
        self._lobby_recent = lobby_recent
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

        # 0. 大区近期消息观察（§38.2）。**必须排在所有过滤之前**：机器人自己的公开回复
        #    也要被看见，未 @ 的普通消息也要被看见，而 observe() 自己负责文本准入
        #    （图片、拍一拍、已删除、空正文）。它是同步纯内存操作，放在最前面不会产生
        #    「哪条消息绕过去了」的空档。私聊不观察：那是一对一的私有频道。
        trigger_sequence: int | None = None
        if is_lobby:
            trigger_sequence = self._observe_lobby_message(message)

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

        # 9.0 记忆命令（§34.1、规划 §7.3）：**先于能力命令解析**识别，否则
        # `/search /remember x` 会在剥掉 `/search` 之后被当成记忆命令执行，绕过
        # 「一条消息最多一个能力」（D-39）。记忆未注入时整段不存在：那时 `/remember`
        # 不是命令，只是普通正文，行为必须与升级前逐字节一致（D-60）。
        if self._memory_path_active():
            memory_command = parse_memory_command(user_text)
            if memory_command is not None:
                return self._route_memory_command(
                    memory_command,
                    message=message,
                    channel_id=channel_id,
                    channel_kind=channel_kind,
                    session_key=session_key,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )

        # 9.1 解析单轮能力命令（D-39）。命令本身不是聊天正文，不进入模型；一条消息里
        # **最多剥离一个**前缀，剥离后若正文又以能力命令开头就本地拒绝 —— 用户只理解
        # 一套披露时，`/search /kb ...` 会同时把查询发给 Exa、把本地资料发给模型。
        # 命令集合与用法文案都取自能力表（capabilities.py），这里不再有 per-能力 分支。
        enabled_features: frozenset[str] = frozenset()
        capability: str | None = None
        parsed = parse_capability_command(user_text)
        if parsed is not None:
            capability, user_text = parsed
        if capability is not None:
            enabled_features = frozenset({capability})
            if not user_text:
                spec = CAPABILITY_BY_FEATURE[capability]
                return self._emit(
                    "reply_now",
                    f"{capability}_usage",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=spec.usage_text,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
            if (
                leading_capability_command(user_text) is not None
                or self._leads_with_memory_command(user_text)
            ):
                return self._emit(
                    "reply_now",
                    "capability_conflict",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.CAPABILITY_CONFLICT_TEXT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )

        # 9.2 空正文：能读的引用交给模型，读不到的给本地提示。
        # 空正文不会命中下面 9.3-9.7 的任何一个分支（命令判定与探测词都要求非空内容），
        # 因此 `queued_reason` 置位之后直落第 10 步入队是安全的。
        # 博客排在图片**之前**：图片 + 博客、无正文的消息若先判图片，vision 关闭时
        # 会回一句图片提示而博客白引（设计 §3.3）。
        queued_reason: str | None = None
        if not user_text:
            if message.blog is not None and not message.blog_missing:
                queued_reason = "blog_only"
            elif self._vision_enabled and has_image(message):
                # 纯图消息入队。取图与降级由 app 的 worker 负责（设计 §3.5）：
                # 路由器不做 I/O，也就无从知道这张图能不能取到。
                queued_reason = "image_only"
            elif message.image is not None:
                # 有图但读不到：图片输入未开启，或 image_missing。
                return self._emit(
                    "reply_now",
                    "media_only",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.IMAGE_UNAVAILABLE_TEXT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
            elif message.blog is not None:
                # 走到这里必然 blog_missing：站方已经告诉我们它没了。
                return self._emit(
                    "reply_now",
                    "media_only",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.BLOG_UNAVAILABLE_TEXT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
            else:
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

        # 9.3 /help 本地应答，不触发模型。
        if is_help_command(user_text):
            return self._emit(
                "reply_now",
                "help",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                # 能不能看图、有没有知识库都是配置决定的；记忆状态则取决于**当前作者**。
                # 帮助文案必须说实话（三个开关的默认值都是关闭）。
                text=self._help_text(channel_kind, message.author.id),
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )

        # 9.4 /reset：大区只建新链（第 8 步已用 force_new 建好，旧链不动，D-21）；
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

        # 9.5 有媒体但正文非空：忽略媒体，照常处理文本（无需额外分支）。

        # 9.6 超长输入本地拦截。
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

        # 9.7 索取系统提示 / 密钥本地拒绝。
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
        # 近期消息快照必须先取、再入队、入队成功后丢弃（§38.2）。三步之间没有 await，
        # 因此同一事件循环里不会被 SSE 回调或 resync 任务插进来 —— 快照与删除边界一致。
        lobby_recent = self._peek_lobby_recent(trigger_sequence)
        request = Request(
            trace_id=new_trace_id(),
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
            enabled_features=enabled_features,
            memory_allowed=self._memory_allowed(message.author.id),
            # 当前作者的 subject 在**入队前**算好（§47.2）：worker 与 App 都不再回头去碰
            # `message.author.id`，公开记忆路径拿到的只有这个不可逆的 key（R1）。
            public_memory_subject=self._public_memory_subject(message.author),
            lobby_recent=lobby_recent,
        )
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            # 队列满 = 没有模型请求入队 = 这一批不算消费：一条都不删，
            # 留给下一次真正跑起来的唤起（设计 §13.2）。
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
        # 请求已经躺在队列里了，这一刻才算消费：连同触发消息自己一起越过边界。
        # 之后模型失败、额度拒绝、发送失败、代次失效或进程退出都不回滚（D-95 第 2 条）。
        if trigger_sequence is not None and self._lobby_recent is not None:
            self._lobby_recent.discard_through(trigger_sequence)
        return self._emit(
            "queued",
            queued_reason if queued_reason is not None else "queued",
            channel_id=channel_id,
            message_id=message.id,
            reply_to=message.id,
            request=request,
            channel_kind=channel_kind,
            thread_root_id=thread_root_id,
            event_id=event_id,
        )

    # --- 大区近期消息（§38） -------------------------------------------------

    def _observe_lobby_message(self, message: ChatMessage) -> int | None:
        """把一条大区消息交给近期消息缓冲，返回它这一轮的消费边界序号。

        未注入缓冲器时返回 `None`：整条路径不存在，行为与升级前逐字节一致。
        缓冲器只保存文本副本，一次失败不该影响任何一条消息的判定，因此异常在这里收口 ——
        丢了这一条顶多少一点背景，绝不能让一条正常消息因为旁观上下文而出错。
        """
        buffer = self._lobby_recent
        if buffer is None:
            return None
        try:
            # subject 与条目一起进缓冲（R1、§46）：记忆路径未装配时传 None，**不做任何计算**，
            # 行为与升级前逐字节一致。`owner key` 只在这里算一次，缓冲器不认识 memory。
            subject = (
                _conversation_subject(message.author)
                if self._memory_access is not None
                else None
            )
            return buffer.observe(message, subject)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "router.lobby_observe_failed",
                channel_id=LOBBY,
                message_id=message.id,
                error=type(exc).__name__,
            )
            return None

    def _peek_lobby_recent(self, trigger_sequence: int | None) -> tuple[LobbyRecentMessage, ...]:
        """取触发消息之前的近期消息快照；未启用或取不到边界时返回空元组。

        与 `_observe_lobby_message` 同样吞异常：这里返回空元组只是这一轮少一点背景，
        而让异常穿过会连累一条本来能正常回复的消息。
        """
        buffer = self._lobby_recent
        if buffer is None or trigger_sequence is None:
            return ()
        try:
            return buffer.peek_before(trigger_sequence)
        except Exception as exc:  # pragma: no cover - 纯内存遍历，理论上不会失败
            log_event(
                self._logger,
                logging.WARNING,
                "router.lobby_peek_failed",
                channel_id=LOBBY,
                error=type(exc).__name__,
            )
            return ()

    @staticmethod
    def _inner_text(user_text: str) -> str:
        """剥掉开头最多一个能力命令，供第 8 步判定内层是不是 `/reset`。

        这里的剥离只为「要不要新建链」服务：第 9.1 步才真正决定这条消息的能力归属，
        而且一条消息里最多剥离一个前缀（D-39）。`/search /kb /reset` 会在 9.1 被
        判成能力冲突，这里也自然看不到 `/reset`。
        """
        parsed = parse_capability_command(user_text)
        return user_text if parsed is None else parsed[1]

    def _help_text(self, channel_kind: str, user_id: str | None) -> str:
        """帮助文案（§34.1 第 3 条）：能力开关 × 当前作者的记忆状态，不夸大能力。

        vision / kb 与引用博客的正文上限都是部署开关，记忆状态则按**当前消息的作者**求值。
        `blog_max_chars` 必须传：文案里那句正文上限曾经写死成 1000，对上限配成别的值的
        部署就是假话（`behavior.quoted_blog_max_chars` 是权威来源）。
        """
        memory_allowed = self._memory_allowed(user_id)
        # `private_enabled` 只在 memory_allowed 为真时影响措辞，但回调本身同步无 I/O
        # （D-67 的 `private_settings_cached` 只读内存快照），照常求值即可。
        return texts.help_text(
            channel_kind=channel_kind,
            vision_enabled=self._vision_enabled,
            kb_enabled=self._kb_enabled,
            memory_allowed=memory_allowed,
            private_enabled=self._private_enabled_for(user_id),
            blog_max_chars=self._cfg.quoted_blog_max_chars,
            capabilities=self._capabilities,
        )

    # --- 记忆命令（§34.1） ---------------------------------------------------

    def _memory_path_active(self) -> bool:
        """记忆命令路径是否可用：策略与队列**都**注入才算。

        policy 不暴露 `enabled`（§28），因此注入与否就是路由层唯一的总开关；
        `memory.enabled=false` 时 app 根本不注入（D-60 的回退路径）。
        """
        return self._memory_access is not None and self._memory_queue is not None

    def _memory_allowed(self, user_id: str | None) -> bool:
        """当前作者能否读取共同记忆（§34.1 第 2 条）；未注入或门禁关闭时恒 False。"""
        access = self._memory_access
        if access is None:
            return False
        return access.permits_common(user_id)

    def _public_memory_subject(self, author: Author) -> ConversationSubject | None:
        """当前作者的会话 subject（§47.2）；记忆未装配时**不做任何计算**（R1）。

        与 `_memory_allowed` 一样，注入与否就是这条路径的总开关：关掉记忆的部署里
        连一次哈希都不发生，`Request.public_memory_subject` 恒为 `None`。
        """
        if self._memory_access is None:
            return None
        return _conversation_subject(author)

    def _private_enabled_for(self, user_id: str | None) -> bool:
        """当前作者的私有记忆开关（§34.1 第 3 条）；未注入或取不到时按 False。

        回调只影响一句措辞，所以这里连异常都吞掉：`/help` 是本地命令，
        不得因为一次探针失败而整条回复失败（D-60 的软故障口径）。
        """
        callback = self._private_enabled
        if callback is None:
            return False
        try:
            return bool(callback(user_id))
        except Exception:
            return False

    def _leads_with_memory_command(self, text: str) -> bool:
        """剥离一个能力前缀之后，剩余正文是否又以记忆命令开头（§34.1）。

        记忆命令也是一种能力，`/search /remember x` 里两条命令并存：既不能按记忆命令执行
        （那样搜索被静默丢弃），也不该把 `/remember x` 原文当成搜索词发出去，因此与
        `/search /kb` 同样本地拒绝（D-39）。未注入记忆时恒为 False：那时 `/remember`
        不是命令，升级前怎么处理就怎么处理（D-60）。
        """
        return self._memory_path_active() and parse_memory_command(text) is not None

    def _route_memory_command(
        self,
        command: MemoryCommand,
        *,
        message: ChatMessage,
        channel_id: str,
        channel_kind: str,
        session_key: str,
        thread_root_id: int | None,
        event_id: int | None,
    ) -> RouteResult:
        """记忆命令的授权与入队（§34.1）；不调模型、不读命令以外的任何状态。

        顺序固定为「只在 DM → Beta 接入门 → 入队」，与 §34.1 逐条对应。
        """
        # 大区是公开对话：同一文本只回固定提示，不进入 `MemoryAccessPolicy`（§28 末条），
        # 也不入队 —— 连管理员也不能在大区里执行记忆命令。
        if channel_kind != "dm":
            return self._emit(
                "reply_now",
                "memory_dm_only",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=texts.MEMORY_DM_ONLY_TEXT,
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )
        access = self._memory_access
        queue = self._memory_queue
        user_id = message.author.id
        # `access` 与 `queue` 已由调用方保证注入，这里的判空只为类型收窄。
        # 拿不到稳定身份（`author.id` 为空）与未通过 Beta 接入门是同一处置：固定拒绝文案。
        # `permits_commands` 对空 `user_id` 本来就为假（§28），这里显式判一次是为了不构造
        # 一个 `user_id` 为空的请求，也让「无稳定身份」这件事在代码里可见。
        if (
            not user_id
            or access is None
            or queue is None
            or not access.permits_commands(user_id, channel_kind)
        ):
            return self._emit(
                "reply_now",
                "memory_beta_denied",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=texts.MEMORY_BETA_DENIED_TEXT,
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )
        request = MemoryCommandRequest(
            event_id=event_id,
            message_id=message.id,
            channel_id=channel_id,
            # 用该 DM 的 session key（规划 §7.3）：记忆 worker 与主 worker 共用同一类队列，
            # 会话键不能省，也不能借用大区链路键。
            session_key=session_key,
            user_id=user_id,
            command=command,
            # R12：站点用户名原样落进请求，它是命令路径**唯一**的用户名来源。
            # 不在这里清洗、也不额外查询：`/memory public` 据此写 `owner_username`，
            # 合法性由 `publish_private`（R8）与 codec 的 `_check_public` 两道校验把关。
            username=message.author.username,
        )
        try:
            queue.put_nowait(request)
        except asyncio.QueueFull:
            # 与主队列满同一套 busy 语义：文案与冷却由 app 决定（D-3、D-18）。
            return self._emit(
                "busy",
                "memory_queue_full",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=texts.BUSY_NOTICE_TEXT,
                actor_id=user_id,
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )
        # 事件此刻已完成去重登记，**终态由记忆 worker 负责**（`mark_handled`，§34.3）：
        # 这里不标记完成，也不构造聊天 Request —— app 不把 `memory_queued` 当聊天请求。
        return self._emit(
            "memory_queued",
            "memory_queued",
            channel_id=channel_id,
            message_id=message.id,
            reply_to=message.id,
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
            # `/search /reset`、`/kb /reset` 仍是本地 reset；虽然能力命令按整体路由顺序
            # 在登记回复链之后才正式剥离，这里必须用其内层正文决定是否新建链。
            return await self._store.resolve_lobby_thread(
                message.id,
                reply_id,
                # /reset 无论回复谁，都以自己为根建一条新链（D-21）。
                force_new=is_reset_command(self._inner_text(user_text)),
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
