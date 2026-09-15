"""博客评论候选路由。

评论发现器只负责把完整的评论 DTO 交给本模块；本模块负责判定这条评论是否
明确指向机器人，并把可执行的本地回复或模型请求交给上层。正文只存在于本次
调用返回值和内存队列中，不写日志或 SQLite。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import texts
from ..config import CommentConfig
from ..core.context import ContextManager
from ..logging_setup import get_logger, log_event
from ..memory.access import MemoryAccessPolicy
from ..site.comment_models import CommentNode
from ..store import CommentClaim
from ..text_utils import (
    contains_bot_mention,
    is_help_command,
    is_reset_command,
    is_secret_probe,
    strip_bot_mention,
)

logger = get_logger("comments.router")
_LOGGER = logger


def comment_session_key(conversation_id: str) -> str:
    """返回评论模型使用的会话键。"""
    return f"comment:{conversation_id}"


@dataclass(frozen=True)
class CommentRequest:
    """等待评论 worker 处理的一轮请求。

    这里只保留当前轮需要的最小资料。尤其不含作者原始 id；身份判定在路由和
    领取前完成，模型只接收 username 标签。记忆门禁同理：判定必须在**仍持有**
    `CommentNode.author.id` 时完成，请求里只留下 `memory_allowed` 这一个布尔
    （§35、D-56）—— 因此**不得**给它补一个 author id 字段。
    """

    comment_id: str
    blog_id: str
    parent_id: str | None
    conversation_id: str
    session_key: str
    generation: int
    username: str
    user_text: str
    parent_bot_text: str | None
    has_image: bool
    has_quoted_blog: bool
    # 图片附件的地址（同源约束由 `fetch_image` 兜住）；图没了或本来就没带图时为 None。
    image_url: str | None = None
    # 当前评论作者能否使用共同记忆（§35）。未注入策略或门禁关闭时恒 False；
    # 为真也只意味着可以读 `all_user`，lobby 与用户私有文件在评论路径上永不读取。
    memory_allowed: bool = False


@dataclass(frozen=True)
class CommentRouteResult:
    """评论路由结果；正文只在本地回复或请求对象中短暂存在。"""

    action: str  # queued | reply_now | busy | ignored | waiting | rebind_required
    comment_id: str | None
    blog_id: str | None
    parent_id: str | None
    text: str | None
    request: CommentRequest | None
    reason: str
    claim_intent: "CommentClaimIntent | None" = None

    @property
    def message_id(self) -> str | None:
        """兼容聊天分派器使用的字段名。"""
        return self.comment_id

    @property
    def reply_to(self) -> str | None:
        """评论回复的父评论始终是触发评论。"""
        return self.parent_id


# 便于上层和测试沿用聊天路由器的命名。
RouteResult = CommentRouteResult


@dataclass(frozen=True)
class CommentClaimIntent:
    """claim 前的评论会话意图。

    discovery 必须在第一次调用 ``Store.claim_comment`` 前使用该意图；特别是
    ``force_new_conversation`` 为真时，Store 才能把 /reset 评论自身作为新链。
    """

    comment_id: str
    blog_id: str
    parent_id: str | None
    source: str
    requested_conversation_id: str | None
    force_new_conversation: bool
    observed_created_at: float | None

    def as_kwargs(self) -> dict[str, object]:
        """转换为 Store.claim_comment 的关键字参数。"""
        return {
            "comment_id": self.comment_id,
            "blog_id": self.blog_id,
            "parent_id": self.parent_id,
            "source": self.source,
            "requested_conversation_id": self.requested_conversation_id,
            "force_new_conversation": self.force_new_conversation,
            "observed_created_at": self.observed_created_at,
        }


def comment_claim_intent(
    comment: CommentNode,
    *,
    bot_username: str,
    source: str = "recent",
    parent_conversation_id: str | None = None,
) -> CommentClaimIntent:
    """在 claim 前计算评论会话意图，供 discovery 使用。

    该函数不访问 Store，也不做 claim；调用方应把 ``as_kwargs()`` 与自己的
    ``now`` 合并后一次性传给 Store。reset 命令的 ``force_new`` 判定在这里完成，
    从而不会等到事件已经插入后才尝试改变会话归属。
    """
    content = comment.content if isinstance(comment.content, str) else ""
    user_text = strip_bot_mention(content, bot_username)
    force_new = is_reset_command(user_text)
    return CommentClaimIntent(
        comment_id=comment.id,
        blog_id=comment.blog_id,
        parent_id=comment.parent_id,
        source=source,
        requested_conversation_id=(None if force_new else parent_conversation_id),
        force_new_conversation=force_new,
        observed_created_at=comment.created_at,
    )


@dataclass(frozen=True)
class _ParentLookupResult:
    """父机器人映射查询结果，同时区分未命中与临时故障。"""

    mapping: "_ParentMapping | None"
    failed: bool = False


@dataclass(frozen=True)
class _ParentMapping:
    """已知机器人评论的短期映射；正文只来自当前评论树。"""

    conversation_id: str
    blog_id: str | None = None
    content: str | None = None


class CommentRouter:
    """将完整评论 DTO 路由为本地应答或评论模型请求。"""

    def __init__(
        self,
        *,
        self_user_id: str,
        bot_username: str,
        ctx: ContextManager,
        store: Any,
        queue: asyncio.Queue[CommentRequest],
        cfg: CommentConfig,
        now: Callable[[], float] = time.time,
        parent_lookup: Callable[[str, str], Awaitable[Any] | Any] | None = None,
        logger_instance: logging.Logger | None = None,
        logger: logging.Logger | None = None,
        vision_enabled: bool = False,
        memory_access: MemoryAccessPolicy | None = None,
    ) -> None:
        self._self_user_id = self_user_id
        self._bot_username = bot_username
        self._ctx = ctx
        self._store = store
        self._queue = queue
        self._cfg = cfg
        self._now = now
        # 图片输入是否对评论区开启（`model.vision_enabled` 且名额大于 0）。
        # 路由器不做 I/O，因此这里只回答「该不该交给模型」，取图与降级在 service。
        self._vision_enabled = vision_enabled
        # 共同记忆的接入策略（§35）：这是路由器需要的**最小依赖**——只用来算一次
        # `permits_common`，不持有服务、不持有队列，因此它也读不到任何记忆正文。
        # `None` 表示未注入（记忆关闭或旧装配）：`memory_allowed` 恒 False（D-60）。
        self._memory_access = memory_access
        self._parent_lookup = parent_lookup
        self._logger = (
            logger_instance
            if logger_instance is not None
            else (logger if logger is not None else _LOGGER)
        )
        # 只在测试替身没有 claim_comment 时使用；生产存储必须提供原子领取。
        self._fallback_claimed: set[str] = set()

    async def handle_comment(
        self,
        comment: CommentNode,
        *,
        source: str = "recent",
        parent_bot_text: str | None = None,
        parent_conversation_id: str | None = None,
        claim_override: CommentClaim | None = None,
        enqueue: bool = True,
    ) -> CommentRouteResult:
        """处理一条完整评论。

        `parent_bot_text` 与 `parent_conversation_id` 是发现器已经从当前评论树确认
        的父机器人评论资料。若未传入，会尽力从 Store 的映射查询会话归属；查询不到
        时该评论仍可按首次精确 @ 规则处理，但不会把它误认成后续回复。
        """
        if not isinstance(comment, CommentNode):
            return self._ignored(None, None, None, "malformed")

        comment_id = comment.id
        blog_id = comment.blog_id
        parent_id = comment.parent_id

        # 这些过滤必须早于 claim，避免旁观评论污染 dedupe 表。
        if not comment.author.id:
            return self._ignored(comment_id, blog_id, parent_id, "author_missing")
        if comment.author.id == self._self_user_id:
            return self._ignored(comment_id, blog_id, parent_id, "self_comment")
        if comment.status != "approved":
            return self._ignored(comment_id, blog_id, parent_id, "status")
        if comment.is_deleted:
            return self._ignored(comment_id, blog_id, parent_id, "deleted")
        if not isinstance(comment.content, str):
            # spider DTO 只有 HTML，不能拿它直接调用模型。
            return self._ignored(comment_id, blog_id, parent_id, "content_missing")

        parent = None
        if parent_id is not None:
            parent_lookup = await self._lookup_parent(
                parent_id,
                blog_id,
                supplied_text=parent_bot_text,
                supplied_conversation_id=parent_conversation_id,
            )
            parent = parent_lookup.mapping
            if parent_lookup.failed:
                # 已经从 discovery claim 得到父会话时不应走到这里；没有该事实
                # 时的 Store 短暂故障必须等待重试，不能把直接回复误判为普通评论。
                return self._waiting(
                    comment_id,
                    blog_id,
                    parent_id,
                    "parent_mapping_unavailable",
                )
            # 映射明确属于另一篇文章时，绝不能把它当成后续会话。
            if parent is not None and parent.blog_id not in (None, blog_id):
                parent = None

        is_follow_up = parent is not None
        has_mention = contains_bot_mention(comment.content, self._bot_username)
        # 后续只认直接回复已知机器人评论；没有该映射时，必须再次精确 @。
        if not is_follow_up and not has_mention:
            return self._ignored(comment_id, blog_id, parent_id, "not_addressed")

        user_text = strip_bot_mention(comment.content, self._bot_username)
        force_new = is_reset_command(user_text)
        claim = claim_override
        if claim is None:
            claim = await self._claim(
                comment,
                parent_id=parent_id,
                source=source,
                requested_conversation_id=(
                    None if force_new else (parent.conversation_id if parent is not None else None)
                ),
                force_new=force_new,
            )
        else:
            # discovery 已经完成原子领取；但 reset 的会话归属由命令评论自身决定。
            # 不再次 claim（同一 UUID 会被 Store 判为重复），也不能只在内存请求里
            # 切换会话，否则事件表与评论映射会分裂。返回稳定 rebind 指令，交给
            # discovery/service 在事务边界内重新 claim。
            claim = self._coerce_claim(claim, comment.id)
            # discovery 先按 CommentClaimIntent 原子领取 reset 时，会话应已经是
            # 当前命令评论自身；只有旧装配把它错误归入父会话（或没有会话）时，
            # 才返回事务重绑指令。不能把正确的 pre-claim 也误判成需要重绑。
            if force_new and claim.claimed and claim.conversation_id != comment.id:
                return self._rebind_required(
                    comment,
                    source=source,
                    parent_conversation_id=(parent.conversation_id if parent else None),
                )
        if not claim.claimed:
            return self._ignored(comment_id, blog_id, parent_id, "duplicate")

        conversation_id = claim.conversation_id or comment_id
        session_key = comment_session_key(conversation_id)
        request = CommentRequest(
            comment_id=comment_id,
            blog_id=blog_id,
            parent_id=parent_id,
            conversation_id=conversation_id,
            session_key=session_key,
            generation=self._ctx.generation(session_key),
            username=comment.author.username or "",
            user_text=user_text,
            parent_bot_text=(
                parent_bot_text
                if parent_bot_text is not None
                else (parent.content if parent is not None else None)
            ),
            has_image=self._has_image(comment),
            has_quoted_blog=self._has_quoted_blog(comment),
            image_url=self._readable_image_url(comment),
            # 门禁判定必须在这里完成：`comment.author.id` 一旦离开路由器就不可得，
            # 模型侧只剩这个布尔（§35、D-56）。
            memory_allowed=self._memory_allowed(comment.author.id),
        )

        # 本地回复不调用模型。/reset 的新会话由 force_new claim 建立，旧会话完全不动。
        image_only = False
        if not user_text:
            # 纯图评论（只能是直接回复机器人评论的那种）在视觉开启时是一轮请求：
            # 有字节可取的图才入队，取图与降级由 service 负责（路由器不做 I/O）。
            image_only = self._vision_enabled and request.image_url is not None
            if not image_only:
                media = self._has_media(comment)
                text = texts.UNSUPPORTED_MEDIA_TEXT if media else texts.USAGE_HINT
                return self._local(request, text, "media_only" if media else "empty")
        # 纯图请求（`user_text` 为空）直落下面的入队：命令判定都要求非空正文，
        # 三个都必然为假，因此不需要为它单开一条分支。
        if is_help_command(user_text):
            return self._local(request, self._comment_help_text(), "help")
        if force_new:
            return self._local(request, texts.RESET_DONE_TEXT, "reset")
        if is_secret_probe(user_text):
            return self._local(request, texts.SECRET_REFUSAL_TEXT, "secret_probe")

        # 附件交给模型：视觉开启且附件可读时随本轮一起外送（§20），否则只有文字。
        if enqueue:
            try:
                self._queue.put_nowait(request)
            except asyncio.QueueFull:
                return self._result("busy", request, texts.BUSY_NOTICE_TEXT, "queue_full")
        return self._result("queued", request, None, "image_only" if image_only else "queued")

    async def handle_candidate(self, candidate: Any, claim: CommentClaim | None = None) -> CommentRouteResult:
        """消费 discovery 的 ``CommentCandidate``，复用其已经完成的原子领取。"""
        comment = getattr(candidate, "comment", candidate)
        if not isinstance(comment, CommentNode):
            return self._ignored(None, None, None, "malformed")
        source = getattr(candidate, "source", "recent")
        parent_text = getattr(candidate, "parent_bot_text", None)
        if not isinstance(parent_text, str):
            parent_node = getattr(candidate, "parent_comment", None)
            if parent_node is None:
                parent_node = getattr(candidate, "parent", None)
            parent_text = (
                parent_node.content
                if isinstance(parent_node, CommentNode) and isinstance(parent_node.content, str)
                else None
            )
        parent_conversation = getattr(candidate, "parent_conversation_id", None)
        if not isinstance(parent_conversation, str) and comment.parent_id is not None:
            # discovery 的 claim 已经在事务内依据 parent_id 证明了父机器人会话。
            # 只有 direct/notification/recovery 候选可以使用该事实；首次 @ 普通
            # 评论时 claim 的 conversation_id 是当前评论自身，不能伪装成父会话。
            candidate_reason = getattr(candidate, "reason", "")
            if candidate_reason in {
                "direct_reply",
                "notification_reply",
                "waiting_retry",
                "recovery",
            }:
                claimed = self._coerce_claim(claim, comment.id) if claim is not None else None
                value = claimed.conversation_id if claimed is not None else None
                if isinstance(value, str) and value:
                    parent_conversation = value
        return await self.handle_comment(
            comment,
            source=source if isinstance(source, str) else "recent",
            parent_bot_text=parent_text,
            parent_conversation_id=(
                parent_conversation if isinstance(parent_conversation, str) else None
            ),
            claim_override=claim,
            enqueue=False,
        )

    async def route(self, comment: Any, *args: Any, **kwargs: Any) -> CommentRouteResult:
        """`handle_comment` 的兼容入口，也接受 discovery candidate + claim。"""
        if hasattr(comment, "comment"):
            claim = args[0] if args else kwargs.pop("claim", None)
            return await self.handle_candidate(comment, claim)
        return await self.handle_comment(comment, **kwargs)

    def acknowledge_request(self, request: CommentRequest) -> bool:
        """从旧兼容队列中确认并移除指定请求。

        新的 discovery/service 路径由 ``handle_candidate`` 直接返回 request，不
        使用该队列。旧装配仍可能先把 request 放进 Router 队列，再由上层处理；
        此时只能按对象身份删除已确认的那一项，绝不能无条件 ``get_nowait`` 抢走
        其他会话的请求。该方法只做内存队列操作，不触及事件状态或正文持久化。
        """
        raw_queue = getattr(self._queue, "_queue", None)
        if raw_queue is None:
            return False
        for index, queued in enumerate(raw_queue):
            if queued is not request:
                continue
            del raw_queue[index]
            self._queue.task_done()
            return True
        return False

    # 过渡期适配器可能使用这些名称；它们都保留“只确认指定对象”的语义。
    consume_request = acknowledge_request
    take_queued_request = acknowledge_request

    @staticmethod
    def _has_image(comment: CommentNode) -> bool:
        """识别图片引用，包括已失效但仍代表附件的占位字段。"""
        return bool(comment.has_image or getattr(comment, "image_missing", False))

    @staticmethod
    def _readable_image_url(comment: CommentNode) -> str | None:
        """附件里**可以取回**的那张图的地址；没带图或图已不在时为 None。

        与 `_has_image` 的分工是 `image_missing`：那个字段表示「引用了图但图已不存在」，
        没有字节可交（与 `text_utils.has_image` 同款口径）——「有没有附件」与
        「有没有东西可看」是两个问题。
        """
        image = getattr(comment, "image", None)
        url = getattr(image, "url", "")
        return url if isinstance(url, str) and url else None

    @staticmethod
    def _has_quoted_blog(comment: CommentNode) -> bool:
        """识别博客引用，包括已失效的引用占位字段。"""
        return bool(comment.has_quoted_blog or getattr(comment, "blog_missing", False))

    @classmethod
    def _has_media(cls, comment: CommentNode) -> bool:
        return cls._has_image(comment) or cls._has_quoted_blog(comment)

    async def handle(self, comment: Any, *args: Any, **kwargs: Any) -> CommentRouteResult:
        """`handle_comment` 的兼容别名。"""
        return await self.route(comment, *args, **kwargs)

    async def _lookup_parent(
        self,
        parent_id: str,
        blog_id: str,
        *,
        supplied_text: str | None,
        supplied_conversation_id: str | None,
    ) -> _ParentLookupResult:
        """查询父机器人评论映射，并区分未命中与临时故障。"""
        if supplied_conversation_id:
            return _ParentLookupResult(
                _ParentMapping(supplied_conversation_id, blog_id, supplied_text)
            )

        value: Any = None
        if self._parent_lookup is not None:
            try:
                value = self._parent_lookup(parent_id, blog_id)
            except TypeError:
                # 简单的内存 matcher 通常只需要 parent_id；保留该窄适配形状。
                try:
                    value = self._parent_lookup(parent_id)  # type: ignore[call-arg]
                except Exception:
                    return _ParentLookupResult(None, True)
            except Exception:
                return _ParentLookupResult(None, True)
            if hasattr(value, "__await__"):
                try:
                    value = await value
                except Exception:
                    return _ParentLookupResult(None, True)
        else:
            # Store 的评论实现允许按窄接口演进；仅尝试不带正文的映射查询。
            attempted = False
            for name in (
                "find_comment_conversation_for_bot_reply",
                "find_comment_parent",
                "find_comment_conversation",
                "find_comment_mapping",
                "lookup_comment_message",
                "get_comment_message",
            ):
                method = getattr(self._store, name, None)
                if method is None:
                    continue
                attempted = True
                try:
                    if name == "find_comment_conversation_for_bot_reply":
                        value = method(parent_id)
                    else:
                        value = method(
                            parent_id,
                            blog_id=blog_id,
                            now=self._now(),
                            retention_seconds=self._cfg.conversation_retention_seconds,
                        )
                except TypeError:
                    try:
                        value = method(parent_id)
                    except TypeError:
                        return _ParentLookupResult(None, True)
                except Exception:
                    # 映射查询是路由的可选旁路；数据库短暂故障不能把普通评论
                    # 误发给模型，也不能终止发现 worker。
                    return _ParentLookupResult(None, True)
                if hasattr(value, "__await__"):
                    try:
                        value = await value
                    except Exception:
                        return _ParentLookupResult(None, True)
                if name == "find_comment_conversation_for_bot_reply":
                    if isinstance(value, tuple) and len(value) >= 2:
                        if isinstance(value[0], str) and isinstance(value[1], str):
                            return _ParentLookupResult(
                                _ParentMapping(value[0], value[1], supplied_text)
                            )
                    continue
                break
            if not attempted:
                return _ParentLookupResult(None)
        return _ParentLookupResult(self._coerce_parent(value, blog_id, supplied_text))

    def _memory_allowed(self, author_id: str | None) -> bool:
        """当前评论作者能否读取共同记忆（§35）；未注入策略时恒 False。

        判据与聊天区同款（`access.permits_common`）：`allowlist` 模式要求非空作者 ID
        在名单内，`all` 模式对所有作者（含拿不到 ID 的评论）为真 —— 但真也只意味着
        可读 `all_user`，评论路径永远不会读 `lobby` 或任何用户私有文件（§28、§30.2）。
        """
        access = self._memory_access
        if access is None:
            return False
        return access.permits_common(author_id)

    def _comment_help_text(self) -> str:
        """评论区 `/help` 的文案（§35、§36）：只按记忆**有没有被注入**二选一。

        未注入（默认部署，或 `memory.enabled=false`）时用的是升级前那份常量、逐字节相同：
        那时评论区一次都不会用到共同记忆，文案就不能声称它会随本轮请求发送（§26.3）。
        注入后才改用披露版（评论侧最多只用 `all_user`，绝不使用私有记忆）。
        判据不放宽到「当前作者能否读取」：披露句讲的是评论区的上限（「最多只会用到」），
        同一段评论线程里的两个人不该因为各自在不在名单里而看到两种措辞。
        """
        return texts.comment_help_text(memory_injected=self._memory_access is not None)

    @staticmethod
    def _coerce_claim(value: Any, comment_id: str) -> CommentClaim:
        """把发现器或测试替身返回的 claim 规整为内部 DTO。"""
        if isinstance(value, CommentClaim):
            return value
        if isinstance(value, Mapping):
            conversation = value.get("conversation_id")
            status = value.get("status", "queued")
            return CommentClaim(
                bool(value.get("claimed", False)),
                comment_id,
                conversation if isinstance(conversation, str) else None,
                status if isinstance(status, str) else "queued",
            )
        if isinstance(value, tuple) and value:
            return CommentClaim(
                bool(value[0]),
                comment_id,
                value[1] if len(value) > 1 and isinstance(value[1], str) else None,
                value[2] if len(value) > 2 and isinstance(value[2], str) else "queued",
            )
        conversation = getattr(value, "conversation_id", None)
        status = getattr(value, "status", "queued")
        return CommentClaim(
            bool(getattr(value, "claimed", False)),
            comment_id,
            conversation if isinstance(conversation, str) else None,
            status if isinstance(status, str) else "queued",
        )

    @staticmethod
    def _coerce_parent(value: Any, blog_id: str, text: str | None) -> _ParentMapping | None:
        if value is None or value is False:
            return None
        if isinstance(value, str):
            return _ParentMapping(value, blog_id, text)
        if isinstance(value, tuple) and value:
            conversation_id = value[0]
            if not isinstance(conversation_id, str) or not conversation_id:
                return None
            mapped_text = value[1] if len(value) > 1 and isinstance(value[1], str) else text
            mapped_blog = value[2] if len(value) > 2 and isinstance(value[2], str) else blog_id
            return _ParentMapping(conversation_id, mapped_blog, mapped_text)
        if isinstance(value, Mapping):
            conversation_id = value.get("conversation_id") or value.get("conversation")
            if not isinstance(conversation_id, str) or not conversation_id:
                return None
            mapped_blog = value.get("blog_id")
            return _ParentMapping(
                conversation_id,
                mapped_blog if isinstance(mapped_blog, str) else blog_id,
                value.get("content") if isinstance(value.get("content"), str) else text,
            )
        conversation_id = getattr(value, "conversation_id", None)
        if not isinstance(conversation_id, str) or not conversation_id:
            return None
        mapped_blog = getattr(value, "blog_id", blog_id)
        mapped_text = getattr(value, "content", None)
        return _ParentMapping(
            conversation_id,
            mapped_blog if isinstance(mapped_blog, str) else blog_id,
            mapped_text if isinstance(mapped_text, str) else text,
        )

    async def _claim(
        self,
        comment: CommentNode,
        *,
        parent_id: str | None,
        source: str,
        requested_conversation_id: str | None,
        force_new: bool,
    ) -> CommentClaim:
        """调用 Store 的原子领取接口；测试替身无该接口时用进程内去重。"""
        method = getattr(self._store, "claim_comment", None)
        if method is None:
            if comment.id in self._fallback_claimed:
                return CommentClaim(False, comment.id, requested_conversation_id, "done")
            self._fallback_claimed.add(comment.id)
            return CommentClaim(True, comment.id, requested_conversation_id or comment.id, "queued")
        try:
            value = method(
                comment_id=comment.id,
                blog_id=comment.blog_id,
                parent_id=parent_id,
                source=source,
                requested_conversation_id=requested_conversation_id,
                force_new_conversation=force_new,
                observed_created_at=comment.created_at,
                now=self._now(),
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "comment.claim_failed",
                blog_id=comment.blog_id,
                comment_id=comment.id,
                error=type(exc).__name__,
            )
            return CommentClaim(False, comment.id, None, "claim_failed")
        if hasattr(value, "__await__"):
            try:
                value = await value
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "comment.claim_failed",
                    blog_id=comment.blog_id,
                    comment_id=comment.id,
                    error=type(exc).__name__,
                )
                return CommentClaim(False, comment.id, None, "claim_failed")
        if isinstance(value, CommentClaim):
            return value
        if isinstance(value, Mapping):
            return CommentClaim(
                bool(value.get("claimed", False)),
                comment.id,
                value.get("conversation_id") if isinstance(value.get("conversation_id"), str) else None,
                value.get("status") if isinstance(value.get("status"), str) else "queued",
            )
        if isinstance(value, tuple) and value:
            return CommentClaim(
                bool(value[0]),
                comment.id,
                value[1] if len(value) > 1 and isinstance(value[1], str) else None,
                value[2] if len(value) > 2 and isinstance(value[2], str) else "queued",
            )
        claimed = bool(getattr(value, "claimed", False))
        conversation_id = getattr(value, "conversation_id", None)
        status = getattr(value, "status", "queued")
        return CommentClaim(claimed, comment.id, conversation_id, status)

    def _local(self, request: CommentRequest, text: str, reason: str) -> CommentRouteResult:
        return self._result("reply_now", request, text, reason)

    def _waiting(
        self,
        comment_id: str,
        blog_id: str,
        parent_id: str | None,
        reason: str,
    ) -> CommentRouteResult:
        """返回可由服务重新入队的稳定等待结果，不发布控制评论。"""
        log_event(
            self._logger,
            logging.WARNING,
            "comment.router",
            blog_id=blog_id,
            comment_id=comment_id,
            reason=reason,
        )
        return CommentRouteResult(
            action="waiting",
            comment_id=comment_id,
            blog_id=blog_id,
            parent_id=parent_id,
            text=None,
            request=None,
            reason=reason,
        )

    def _rebind_required(
        self,
        comment: CommentNode,
        *,
        source: str,
        parent_conversation_id: str | None,
    ) -> CommentRouteResult:
        """发现器已错误预 claim reset 时返回事务重绑指令。"""
        intent = comment_claim_intent(
            comment,
            bot_username=self._bot_username,
            source=source,
            parent_conversation_id=parent_conversation_id,
        )
        log_event(
            self._logger,
            logging.WARNING,
            "comment.router",
            blog_id=comment.blog_id,
            comment_id=comment.id,
            reason="reset_requires_preclaim",
        )
        return CommentRouteResult(
            action="rebind_required",
            comment_id=comment.id,
            blog_id=comment.blog_id,
            parent_id=comment.parent_id,
            text=None,
            request=None,
            reason="reset_requires_preclaim",
            claim_intent=intent,
        )

    def _result(
        self,
        action: str,
        request: CommentRequest,
        text: str | None,
        reason: str,
    ) -> CommentRouteResult:
        log_event(
            self._logger,
            logging.INFO if action != "ignored" else logging.DEBUG,
            "comment.router",
            blog_id=request.blog_id,
            comment_id=request.comment_id,
            reason=reason,
        )
        return CommentRouteResult(
            action=action,
            comment_id=request.comment_id,
            blog_id=request.blog_id,
            parent_id=request.comment_id,
            text=text,
            request=request,
            reason=reason,
        )

    def _ignored(
        self,
        comment_id: str | None,
        blog_id: str | None,
        parent_id: str | None,
        reason: str,
    ) -> CommentRouteResult:
        log_event(
            self._logger,
            logging.DEBUG,
            "comment.router",
            blog_id=blog_id,
            comment_id=comment_id,
            reason=reason,
        )
        return CommentRouteResult(
            action="ignored",
            comment_id=comment_id,
            blog_id=blog_id,
            parent_id=parent_id,
            text=None,
            request=None,
            reason=reason,
        )


# 更贴近聊天模块的类名，供上层装配迁移期间使用。
MessageRouter = CommentRouter

__all__ = [
    "CommentRequest",
    "CommentClaimIntent",
    "CommentRouteResult",
    "CommentRouter",
    "MessageRouter",
    "RouteResult",
    "comment_claim_intent",
    "comment_session_key",
]
