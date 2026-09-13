"""评论子系统的生命周期、独立队列与后台轮询任务。

`CommentService` 不参与聊天队列。评论网络短暂失败只记录稳定错误并等待下一轮，
不会让任务退出；真正意外退出的后台任务则通过 `alive` 反馈给 `/livez`。停止时
所有任务都显式取消并等待，保证应用关闭后没有 pending task warning。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .. import texts
from ..core.context import ContextManager
from ..logging_setup import get_logger, log_event
from ..site.comment_models import CommentNode, CommentTreeTooLarge as SiteCommentTreeTooLarge
from ..site.client import SiteClient
from ..store import CommentClaim, Store
from ..text_utils import is_reset_command, strip_bot_mention, truncate_at_paragraph
from .discovery import (
    CandidateCallback,
    NotificationPoller,
    RecentCommentPoller,
    _call_optional,
)
from .matcher import CommentCandidate, CommentTreeIndex, CommentTreeTooLarge

_logger = get_logger("comments.service")


class CommentHandler(Protocol):
    async def __call__(self, candidate: CommentCandidate, claim: CommentClaim) -> Any: ...


class ModelCompleter(Protocol):
    async def complete(self, messages: list[dict[str, str]]) -> str: ...


class GatedModelClient:
    """为评论模型请求套用与聊天共享的 semaphore。"""

    def __init__(self, client: ModelCompleter, gate: asyncio.Semaphore | None) -> None:
        self.client = client
        self.gate = gate

    async def complete(self, messages: list[dict[str, str]]) -> str:
        if self.gate is None:
            return await self.client.complete(messages)
        async with self.gate:
            return await self.client.complete(messages)

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


@dataclass(frozen=True)
class CommentServiceStatus:
    """服务运行状态的无正文快照。"""

    started: bool
    alive: bool
    queue_size: int
    queue_capacity: int


@dataclass
class _ReplyReservation:
    """模型回复在途 quota 预留；只在 Service 兼容层内部流转。"""

    token: object | None
    owned_by_service: bool
    reason: str | None = None
    retry_after: float | None = None
    released: bool = False


def _cfg(config: object, key: str, default: Any) -> Any:
    """从 CommentConfig 或简单测试映射中读取配置。"""
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _clean_label(value: object) -> str:
    """把用户名或文章标题中的控制字符替换为空格。"""
    if not isinstance(value, str):
        return ""
    return "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value)


class CommentService:
    """独立评论服务：两个 poller、等待调度器与单并发 worker。"""

    def __init__(
        self,
        client: SiteClient,
        store: Store,
        config: object,
        *,
        bot_username: str = "",
        bot_user_id: str | None = None,
        handler: CommentHandler | None = None,
        router: object | None = None,
        sender: object | None = None,
        quota: object | None = None,
        model_client: ModelCompleter | None = None,
        model_gate: asyncio.Semaphore | None = None,
        context_manager: object | None = None,
        system_prompt: str = "",
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.config = config
        self.bot_username = bot_username
        self.bot_user_id = bot_user_id
        self.now = now
        self.sleep = sleep
        self.logger = logger or _logger
        self.router = router
        self.sender = sender
        # 评论 quota 与聊天 quota 独立。Service 在模型调用前取得预留；新 Sender
        # 接受 reservation_token 后由本层完成 note/release，旧 Sender 则走兼容
        # 路径并交回其自身的预留逻辑。
        self.quota = quota
        self.context_manager = context_manager or ContextManager(
            _cfg(config, "context_turns", 10),
            _cfg(config, "context_input_tokens", 8000),
        )
        self.system_prompt = system_prompt
        self.handler = handler
        self.model_client = (
            GatedModelClient(model_client, model_gate)
            if model_client is not None
            else None
        )

        capacity = _cfg(config, "queue_size", 50)
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("comments.queue_size 必须为正整数")
        concurrency = _cfg(config, "concurrency", 1)
        if concurrency != 1:
            raise ValueError("评论 worker 并发度必须为 1")
        self.queue: asyncio.Queue[tuple[CommentCandidate, CommentClaim]] = asyncio.Queue(
            maxsize=capacity
        )
        self._queue_capacity = capacity
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()
        self._started = False
        self._stopped = False

        callback: CandidateCallback = self._enqueue_candidate
        self.recent_poller = RecentCommentPoller(
            client,
            store,
            bot_username=bot_username,
            bot_user_id=bot_user_id,
            max_tree_nodes=_cfg(config, "max_tree_nodes", 10_000),
            now=now,
            on_candidate=callback,
            logger=self.logger,
        )
        self.notification_poller = NotificationPoller(
            client,
            store,
            bot_username=bot_username,
            bot_user_id=bot_user_id,
            max_pages=_cfg(config, "notification_max_pages", 5),
            unmatched_attempt_limit=_cfg(config, "unmatched_attempt_limit", 5),
            max_tree_nodes=_cfg(config, "max_tree_nodes", 10_000),
            now=now,
            on_candidate=callback,
            logger=self.logger,
        )

    @property
    def alive(self) -> bool:
        """所有评论后台任务仍存活时返回 True。"""
        return self._started and not self._stopped and bool(self._tasks) and all(
            not task.done() for task in self._tasks
        )

    @property
    def started(self) -> bool:
        return self._started and not self._stopped

    def invalidate_conversations(self, conversation_ids: object) -> None:
        """清理已过期的评论会话历史并递增 generation。"""
        invalidate = getattr(self.context_manager, "invalidate", None)
        if not callable(invalidate):
            return
        try:
            values = tuple(conversation_ids)  # type: ignore[arg-type]
        except TypeError:
            values = (conversation_ids,)
        for conversation_id in values:
            if not isinstance(conversation_id, str) or not conversation_id:
                continue
            key = (
                conversation_id
                if conversation_id.startswith("comment:")
                else f"comment:{conversation_id}"
            )
            invalidate(key)

    @property
    def status(self) -> CommentServiceStatus:
        return CommentServiceStatus(
            started=self.started,
            alive=self.alive,
            queue_size=self.queue.qsize(),
            queue_capacity=self._queue_capacity,
        )

    async def start(self) -> None:
        """启动独立评论 worker、等待调度器与两个轮询器。"""
        if self.started:
            return
        self._stopped = False
        self._stop_event.clear()
        try:
            self._tasks = [
                asyncio.create_task(self._worker_loop(), name="comment-worker"),
                asyncio.create_task(self._waiting_dispatcher(), name="comment-waiting-dispatcher"),
                asyncio.create_task(self._poll_loop(
                    self.recent_poller,
                    _cfg(self.config, "recent_poll_seconds", 30),
                    "recent",
                ), name="comment-recent-poller"),
                asyncio.create_task(self._poll_loop(
                    self.notification_poller,
                    _cfg(self.config, "notification_poll_seconds", 15),
                    "notification",
                ), name="comment-notification-poller"),
            ]
        except BaseException:
            tasks, self._tasks = self._tasks, []
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            raise
        self._started = True
        log_event(self.logger, logging.INFO, "comment.service_started")

    async def stop(self) -> None:
        """按停止顺序取消所有评论任务并等待其结束；可重复调用。"""
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._started = False
        log_event(self.logger, logging.INFO, "comment.service_stopped")

    async def enqueue(
        self, candidate: CommentCandidate, claim: CommentClaim
    ) -> bool:
        """把已 claim 的候选放入独立队列；队满转为 waiting_queue。"""
        return await self._enqueue_candidate(candidate, claim)

    async def _enqueue_candidate(
        self, candidate: CommentCandidate, claim: CommentClaim
    ) -> bool:
        try:
            self.queue.put_nowait((candidate, claim))
        except asyncio.QueueFull:
            await _call_optional(
                self.store,
                ("set_comment_event_status", "mark_comment_waiting_queue"),
                comment_id=candidate.comment_id,
                status="waiting_queue",
                next_attempt_at=self.now() + _cfg(self.config, "retry_base_seconds", 5),
                now=self.now(),
            )
            log_event(
                self.logger,
                logging.INFO,
                "comment.queue_full",
                count=1,
                reason="waiting_queue",
            )
            return False
        return True

    async def _poll_loop(self, poller: object, interval: int, name: str) -> None:
        """轮询失败保持任务存活，取消则交给 stop() 等待。"""
        retry_base = max(1, int(_cfg(self.config, "retry_base_seconds", 5)))
        retry_max = max(retry_base, int(_cfg(self.config, "retry_max_seconds", 300)))
        failure_delay = retry_base
        try:
            while True:
                try:
                    await poller.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "comment.poll_failed",
                        error=type(exc).__name__,
                        reason=name,
                    )
                    await self.sleep(failure_delay)
                    failure_delay = min(retry_max, failure_delay * 2)
                    continue
                failure_delay = retry_base
                await self.sleep(max(1, int(interval)))
        except asyncio.CancelledError:
            raise

    async def _worker_loop(self) -> None:
        """单并发消费评论候选；单候选错误不终止 worker。"""
        try:
            while True:
                candidate, claim = await self.queue.get()
                try:
                    await self._handle_candidate(candidate, claim)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_event(
                        self.logger,
                        logging.ERROR,
                        "comment.worker_error",
                        error=type(exc).__name__,
                    )
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _handle_candidate(
        self, candidate: CommentCandidate, claim: CommentClaim
    ) -> Any:
        try:
            handler = self.handler
            if handler is not None:
                return await handler(candidate, claim)
            router = self.router
            if router is None:
                return None
            # 新 Router 契约直接返回 request，不再把 request 复制进自己的 queue。
            # 这既避免 service 无条件 drain 私有队列，也让 direct reply 的 claim
            # 会话映射沿同一个对象传入，避免再查 Store 一次。
            result = None
            fn = getattr(router, "handle_candidate", None)
            if fn is not None:
                try:
                    result = fn(candidate, claim)
                except TypeError:
                    result = None
            if result is None:
                handle_comment = getattr(router, "handle_comment", None)
                if handle_comment is None:
                    handle_comment = getattr(router, "route", None) or getattr(
                        router, "handle", None
                    )
                if handle_comment is None:
                    return None
                parent_conversation_id = self._parent_conversation_id(candidate, claim)
                try:
                    result = handle_comment(
                        candidate.comment,
                        source=candidate.source,
                        parent_bot_text=candidate.parent_bot_text,
                        parent_conversation_id=parent_conversation_id,
                        claim_override=claim,
                        # Bounded service queue is the sole queue owner.
                        enqueue=False,
                    )
                except TypeError:
                    # 过渡期窄适配器可能没有 parent/enqueue 参数；仍优先传
                    # claim_override，确保不会发生第二次原子领取。
                    try:
                        result = handle_comment(
                            candidate.comment,
                            source=candidate.source,
                            parent_bot_text=candidate.parent_bot_text,
                            claim_override=claim,
                        )
                    except TypeError:
                        result = handle_comment(candidate.comment)
            result = await result if inspect.isawaitable(result) else result

            action = getattr(result, "action", None)
            request = getattr(result, "request", None)
            if action == "rebind_required":
                rebound = await self._rebind_reset(candidate, claim, result)
                if rebound is None:
                    await self._set_event_status(
                        getattr(candidate, "comment_id", None),
                        "recover",
                        next_attempt_at=self.now()
                        + _cfg(self.config, "retry_base_seconds", 5),
                    )
                    return None
                # Store 的 rebind 必须已经在事务中完成；仅将新的 claim 交回
                # Router，禁止再次通过旧私有队列排队。
                rebound_result = getattr(router, "handle_candidate", None)
                if rebound_result is None:
                    await self._set_event_status(
                        getattr(candidate, "comment_id", None),
                        "recover",
                        next_attempt_at=self.now()
                        + _cfg(self.config, "retry_base_seconds", 5),
                    )
                    return None
                rebound_result = rebound_result(candidate, rebound)
                rebound_result = (
                    await rebound_result
                    if inspect.isawaitable(rebound_result)
                    else rebound_result
                )
                if getattr(rebound_result, "action", None) == "rebind_required":
                    await self._set_event_status(
                        getattr(candidate, "comment_id", None),
                        "recover",
                        next_attempt_at=self.now()
                        + _cfg(self.config, "retry_base_seconds", 5),
                    )
                    return None
                result = rebound_result
                action = getattr(result, "action", None)
                request = getattr(result, "request", None)
            if request is None and action in {"reply_now", "queued"}:
                # 过渡期 Router 可能仍把 request 放进兼容队列；只取与当前
                # comment_id 精确关联的项，保留其他候选，绝不无条件 drain。
                request = self._take_router_request(router, candidate.comment_id)
            if action == "reply_now" and request is not None:
                return await self._send_local(result, request)
            if action == "queued" and request is not None:
                return await self._run_model_request(request)
            if action == "ignored":
                # discovery 已确认的直接回复若在 Router 侧临时查不到映射，必须
                # 保留可恢复状态；否则一次 Store 短暂失败会把事件永久标为忽略。
                if self._is_direct_reply(candidate) or getattr(result, "reason", "") in {
                    "parent_mapping_unavailable",
                    "parent_lookup_failed",
                    "parent_missing",
                }:
                    await self._set_event_status(
                        getattr(candidate, "comment_id", None),
                        "waiting_rate_limit",
                        next_attempt_at=self.now()
                        + _cfg(self.config, "retry_base_seconds", 5),
                    )
                    log_event(
                        self.logger,
                        logging.INFO,
                        "comment.parent_mapping_pending",
                        reason=str(getattr(result, "reason", "unavailable")),
                    )
                else:
                    await self._set_event_status(
                        getattr(candidate, "comment_id", None), "skipped_ignored"
                    )
            elif action in {"waiting", "recover", "retry"}:
                await self._set_event_status(
                    getattr(candidate, "comment_id", None),
                    "waiting_rate_limit",
                    next_attempt_at=self.now()
                    + _cfg(self.config, "retry_base_seconds", 5),
                )
            elif action == "busy":
                await self._set_event_status(
                    getattr(candidate, "comment_id", None),
                    "waiting_queue",
                    next_attempt_at=self.now() + _cfg(self.config, "retry_base_seconds", 5),
                )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 任意 handler/router/sender 意外异常都必须把非终态事件转 recover；
            # 只处理 direct reply 会让首次 @ 事件永久卡在 queued，调度器永远看不到。
            # 已经由外部标记为终态的窄适配器结果不再反向改写。
            status = str(getattr(claim, "status", "queued"))
            terminal = (
                status == "done"
                or status == "baseline_ignored"
                or status == "unmatched"
                or status.startswith("skipped_")
            )
            if not terminal:
                await self._set_event_status(
                    getattr(candidate, "comment_id", None),
                    "recover",
                    next_attempt_at=self.now()
                    + _cfg(self.config, "retry_base_seconds", 5),
                )
                log_event(
                    self.logger,
                    logging.WARNING,
                    "comment.worker_recover",
                    reason=type(exc).__name__,
                )
                return None
            raise
        finally:
            # 通知只有在其所有候选事件进入终态后才可标已读；发送器/路由器负责
            # 推进事件状态，这里在每轮 worker 完成后再检查一次。
            if candidate.notification_id:
                try:
                    if await self.notification_poller._can_mark_read(candidate.notification_id):
                        await self.notification_poller._mark_read(candidate.notification_id)
                except Exception as exc:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "comment.notification_lifecycle_failed",
                        error=type(exc).__name__,
                    )

    async def _rebind_reset(
        self,
        candidate: CommentCandidate,
        claim: CommentClaim,
        result: object,
    ) -> CommentClaim | None:
        """在 Store 事务中把旧 reset 事件重绑到评论自身会话。"""
        comment = candidate.comment
        intent = getattr(result, "claim_intent", None)
        requested = getattr(intent, "requested_conversation_id", None)
        if not isinstance(requested, str) or not requested:
            requested = comment.id
        rebound = await _call_optional(
            self.store,
            (
                "rebind_comment_conversation",
                "rebind_comment_event",
                "rebind_comment_claim",
            ),
            comment_id=comment.id,
            blog_id=comment.blog_id,
            parent_id=comment.parent_id,
            source=candidate.source,
            requested_conversation_id=requested,
            conversation_id=requested,
            new_conversation_id=requested,
            force_new_conversation=True,
            now=self.now(),
        )
        if isinstance(rebound, CommentClaim):
            return rebound if rebound.claimed else None
        if rebound is True:
            return CommentClaim(True, comment.id, requested, "queued")
        # A transitional Store may expose only claim_comment with an explicit
        # rebind flag. It is safe to use only when that flag is accepted; a normal
        # duplicate claim must never silently change the conversation.
        claim_fn = getattr(self.store, "claim_comment", None)
        if claim_fn is None:
            return None
        try:
            value = claim_fn(
                comment_id=comment.id,
                blog_id=comment.blog_id,
                parent_id=comment.parent_id,
                source=candidate.source,
                requested_conversation_id=requested,
                conversation_id=requested,
                force_new_conversation=True,
                rebind=True,
                now=self.now(),
            )
            value = await value if inspect.isawaitable(value) else value
        except TypeError:
            return None
        return value if isinstance(value, CommentClaim) and value.claimed else None

    def _is_direct_reply(self, candidate: object) -> bool:
        """判断发现器已确认的直接回复候选。"""
        return getattr(candidate, "reason", "") in {
            "direct_reply",
            "notification_reply",
            "waiting_retry",
            "recovery",
        } and bool(getattr(getattr(candidate, "comment", None), "parent_id", None))

    def _parent_conversation_id(
        self, candidate: CommentCandidate, claim: CommentClaim
    ) -> str | None:
        """沿 claim 把父会话传给 Router；reset 的新会话不是父会话。"""
        comment = getattr(candidate, "comment", None)
        content = getattr(comment, "content", None)
        if isinstance(content, str) and is_reset_command(
            strip_bot_mention(content, self.bot_username)
        ):
            return None
        value = getattr(candidate, "parent_conversation_id", None)
        if not isinstance(value, str) or not value:
            value = getattr(claim, "parent_conversation_id", None)
        if not isinstance(value, str) or not value:
            value = claim.conversation_id
        return value if self._is_direct_reply(candidate) and value else None

    @staticmethod
    def _take_router_request(router: object, comment_id: str) -> object | None:
        """按评论 UUID 关联旧 Router 队列中的 request（仅兼容迁移期）。"""
        queue = getattr(router, "_queue", None)
        pending = getattr(queue, "_queue", None)
        if pending is None:
            return None
        try:
            entries = tuple(pending)
        except TypeError:
            return None
        for index, item in enumerate(entries):
            if getattr(item, "comment_id", None) != comment_id:
                continue
            try:
                del pending[index]
            except (IndexError, TypeError):
                return None
            task_done = getattr(queue, "task_done", None)
            if callable(task_done):
                try:
                    task_done()
                except ValueError:
                    # 窄测试替身可能没有对应 unfinished_tasks 计数。
                    pass
            return item
        return None

    async def _send_local(self, result: object, request: object) -> Any:
        """发送路由器产生的本地评论回复；控制提示不经过模型。"""
        sender = self.sender
        text = getattr(result, "text", None)
        if sender is None or not isinstance(text, str) or not text:
            await self._set_event_status(getattr(request, "comment_id", None), "skipped_failed")
            return result
        send = getattr(sender, "send", None)
        if send is None:
            await self._set_event_status(getattr(request, "comment_id", None), "skipped_failed")
            return result
        outcome = send(request, text, kind="notice_local")
        outcome = await outcome if inspect.isawaitable(outcome) else outcome
        await self._update_after_send(request, outcome)
        return outcome

    async def _reserve_reply(self, request: object) -> _ReplyReservation:
        """在构造/调用模型前原子预留评论 reply quota。"""
        quota = self.quota
        reserve = getattr(quota, "reserve", None) if quota is not None else None
        if reserve is None:
            # 老的窄测试替身没有 quota；生产装配始终传入评论守卫。
            return _ReplyReservation(None, False)
        blog_id = getattr(request, "blog_id", None)
        comment_id = getattr(request, "comment_id", None)
        if not isinstance(blog_id, str) or not blog_id:
            return _ReplyReservation(None, False, "failed")
        try:
            try:
                result = reserve(
                    blog_id,
                    "reply",
                    trigger_comment_id=comment_id if isinstance(comment_id, str) else None,
                )
            except TypeError:
                result = reserve(blog_id, kind="reply")
            result = await result if inspect.isawaitable(result) else result
        except Exception as exc:
            # quota 读库故障是临时状态，不在模型前生成一条无法安全发布的回复。
            log_event(
                self.logger,
                logging.WARNING,
                "comment.quota_failed",
                error=type(exc).__name__,
            )
            return _ReplyReservation(None, False, "backoff")

        allowed = getattr(result, "allowed", None)
        if allowed is None:
            allowed = bool(result) if isinstance(result, bool) else False
        if not allowed:
            reason = getattr(result, "reason", None)
            if not isinstance(reason, str) or not reason:
                decision = getattr(result, "decision", None)
                reason = getattr(decision, "value", decision)
            reason = {
                "daily": "quota",
                "daily_reply": "quota",
                "quota": "quota",
                "allow": None,
            }.get(str(reason), str(reason) if reason else "backoff")
            retry_after = getattr(result, "retry_after", None)
            if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
                retry_after = None
            return _ReplyReservation(None, False, reason, float(retry_after) if retry_after is not None else None)

        token = getattr(result, "reservation_token", None)
        if token is None:
            token = getattr(result, "token", None)
        # Service 只有把令牌实际交给 Sender 才拥有最终化责任。当前旧 Sender
        # 没有该关键字，交由其内部 reserve/note/release 保持向后兼容。
        # 令牌在交给 Sender 之前始终由 Service 负责释放；旧 Sender 路径会在
        # `_send_reply` 中先释放再让其自己 reserve，避免模型失败时泄漏预留。
        return _ReplyReservation(token, token is not None)

    async def _release_reply(self, request: object, reservation: _ReplyReservation) -> None:
        """释放 Service 持有的预留；无 token 或旧 Sender 路径不重复释放。"""
        if (
            not reservation.owned_by_service
            or reservation.token is None
            or reservation.released
        ):
            return
        quota = self.quota
        release = getattr(quota, "release", None) if quota is not None else None
        if release is None:
            reservation.released = True
            return
        blog_id = getattr(request, "blog_id", None)
        comment_id = getattr(request, "comment_id", None)
        try:
            try:
                result = release(
                    blog_id,
                    "reply",
                    trigger_comment_id=comment_id,
                    reservation_token=reservation.token,
                )
            except TypeError:
                result = release(blog_id, "reply", reservation_token=reservation.token)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            reservation.released = True
        except asyncio.CancelledError:
            reservation.released = True
            raise
        except Exception as exc:
            reservation.released = True
            # 释放失败不能覆盖原始模型/发送故障，也不能让 worker 退出。
            log_event(
                self.logger,
                logging.ERROR,
                "comment.quota_release_failed",
                error=type(exc).__name__,
            )

    async def _note_reply(self, request: object, reservation: _ReplyReservation) -> None:
        """把成功送达的 Service 预留恰好转为一笔正式发送。"""
        if (
            not reservation.owned_by_service
            or reservation.token is None
            or reservation.released
        ):
            return
        quota = self.quota
        note = getattr(quota, "note_sent", None) if quota is not None else None
        if note is None:
            reservation.released = True
            return
        blog_id = getattr(request, "blog_id", None)
        comment_id = getattr(request, "comment_id", None)
        try:
            try:
                result = note(
                    blog_id,
                    comment_id,
                    "reply",
                    reservation_token=reservation.token,
                )
            except TypeError:
                result = note(blog_id, comment_id, reservation_token=reservation.token)
            if inspect.isawaitable(result):
                # 远端已经发布；本地配额必须完成转正后才传播取消，避免在
                # SQLite await 点遗留永久占位的预留令牌。
                task = asyncio.ensure_future(result)
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            reservation.released = True
        except asyncio.CancelledError:
            reservation.released = True
            raise
        except Exception as exc:
            reservation.released = True
            # 远端已经送达；quota 守卫应自行降级，不把成功改成失败。
            log_event(
                self.logger,
                logging.ERROR,
                "comment.quota_note_failed",
                error=type(exc).__name__,
            )

    @staticmethod
    def _sender_token_keyword(send: object) -> str | None:
        """返回 Sender 接受预留令牌的关键字；旧接口返回 None。"""
        try:
            parameters = inspect.signature(send).parameters.values()
        except (TypeError, ValueError):
            return None
        names = {parameter.name for parameter in parameters}
        for name in ("reservation_token", "quota_token", "token"):
            if name in names:
                return name
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            return "reservation_token"
        return None

    async def _send_reply(
        self, request: object, text: str, reservation: _ReplyReservation
    ) -> tuple[object, bool]:
        """把预留令牌传给新 Sender，或兼容旧 Sender 的自管 quota。"""
        send = getattr(self.sender, "send", None)
        if send is None:
            raise RuntimeError("comment sender unavailable")
        keyword = self._sender_token_keyword(send)
        if keyword is None or reservation.token is None:
            # 旧 Sender 会在发送层再次 reserve；先释放 Service 检查所得的预留，
            # 避免该旁路把自己的 pending 计数当成文章冷却而拒绝发送。
            await self._release_reply(request, reservation)
            value = send(request, text, kind="reply")
            return (
                await value if inspect.isawaitable(value) else value,
                False,
            )
        value = send(request, text, kind="reply", **{keyword: reservation.token})
        return await value if inspect.isawaitable(value) else value, True

    async def _run_model_request(self, request: object) -> Any:
        """构造评论 role=user 上下文，调用共享模型门并发送成功回复。"""
        comment_id = getattr(request, "comment_id", None)
        await self._set_event_status(comment_id, "processing")
        model = self.model_client
        sender = self.sender
        if model is None or sender is None:
            await self._set_event_status(comment_id, "skipped_failed")
            return None

        # 预留必须发生在任何模型调用之前（包括构造共享上下文本身）；否则
        # quota 耗尽时模型输出会成为永远无法发布的幽灵历史。
        reservation = await self._reserve_reply(request)
        if reservation.reason is not None:
            if reservation.reason == "quota":
                await self._set_event_status(comment_id, "skipped_quota")
            elif reservation.reason in {"minute", "article", "backoff"}:
                await self._set_event_status(
                    comment_id,
                    "waiting_rate_limit",
                    next_attempt_at=self.now()
                    + (reservation.retry_after or _cfg(self.config, "retry_base_seconds", 5)),
                )
            else:
                await self._set_event_status(comment_id, "skipped_failed")
            return None

        try:
            messages = await self._build_model_messages(request)
        except Exception as exc:
            await self._release_reply(request, reservation)
            # 文章资料读取失败属于临时故障，不生成模型请求，交给 waiting dispatcher。
            status = "skipped_article" if getattr(exc, "status", None) in (400, 404) else "waiting_rate_limit"
            await self._set_event_status(
                comment_id,
                status,
                next_attempt_at=(
                    None
                    if status.startswith("skipped_")
                    else self.now() + _cfg(self.config, "retry_base_seconds", 5)
                ),
            )
            log_event(
                self.logger,
                logging.WARNING,
                "comment.article_context_failed",
                error=type(exc).__name__,
            )
            return None

        try:
            text = await model.complete(messages)
        except asyncio.CancelledError:
            # 已捕获取消后直接等待释放，确保释放任务本身不会留在 event loop
            # 成为 stop() 之后的 pending task。
            await self._release_reply(request, reservation)
            raise
        except Exception as exc:
            await self._release_reply(request, reservation)
            await self._set_event_status(comment_id, "skipped_model")
            log_event(
                self.logger,
                logging.WARNING,
                "comment.model_failed",
                error=type(exc).__name__,
            )
            return None
        if not isinstance(text, str) or not text.strip():
            await self._release_reply(request, reservation)
            await self._set_event_status(comment_id, "skipped_model")
            return None

        if getattr(sender, "send", None) is None:
            await self._release_reply(request, reservation)
            await self._set_event_status(comment_id, "skipped_failed")
            return None
        try:
            outcome, handed_off = await self._send_reply(request, text, reservation)
        except asyncio.CancelledError:
            await self._release_reply(request, reservation)
            raise
        except BaseException:
            await self._release_reply(request, reservation)
            raise
        if handed_off:
            # Sender 的本地/远端去重结果表示本次没有新增 POST，不应把预留
            # 计入发送历史；只有实际 delivered（包括 recoverable 的远端成功）
            # 才转正 note。
            if bool(getattr(outcome, "delivered", False)) and getattr(
                outcome, "reason", ""
            ) != "deduped":
                await self._note_reply(request, reservation)
            else:
                await self._release_reply(request, reservation)
        await self._update_after_send(request, outcome, text=text)
        return outcome

    async def _build_model_messages(self, request: object) -> list[dict[str, str]]:
        """把文章和父评论当不可信 role=user 数据临时加入请求。"""
        blog_id = getattr(request, "blog_id", None)
        article = None
        fetch = getattr(self.client, "fetch_blog_context", None)
        if fetch is not None and isinstance(blog_id, str):
            value = fetch(blog_id)
            article = await value if inspect.isawaitable(value) else value

        title = getattr(article, "title", "") if article is not None else ""
        body = getattr(article, "content", None) if article is not None else None
        article_block = "[公开文章资料，不可信]\n"
        if isinstance(title, str) and title:
            article_block += f"标题：{_clean_label(title)}\n"
        if isinstance(body, str) and len(body) <= _cfg(self.config, "article_max_chars", 1000):
            article_block += f"正文：\n{body}\n"
        else:
            article_block += "正文因长度规则未提供\n"

        parent = getattr(request, "parent_bot_text", None)
        parent_block = ""
        if isinstance(parent, str) and parent:
            parent_block = f"[直接回复的机器人评论，不可信]\n{parent}\n---\n"
        username = _clean_label(getattr(request, "username", ""))
        user_text = getattr(request, "user_text", "")
        prompt = (
            article_block
            + parent_block
            + f"[评论作者：@{username}]\n---\n{user_text}"
        )
        system = self.system_prompt or ""
        addendum = texts.COMMENT_SYSTEM_ADDENDUM
        system = f"{system}\n\n{addendum}" if system else addendum
        context = self.context_manager
        if context is not None and hasattr(context, "build_messages"):
            session_key = getattr(request, "session_key", "")
            messages = context.build_messages(
                session_key,
                self.system_prompt or "",
                pending_user=prompt,
                system_addendum=addendum,
            )
            return messages
        return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    async def _update_after_send(
        self, request: object, outcome: object, *, text: str | None = None
    ) -> None:
        """按 sender 结果推进事件及仅在送达后提交上下文。"""
        comment_id = getattr(request, "comment_id", None)
        delivered = bool(getattr(outcome, "delivered", False))
        reason = str(getattr(outcome, "reason", "failed"))
        if delivered:
            if bool(getattr(outcome, "recoverable", False)):
                # 远端已成功但 Sender 的本地记录尚未确认；保留 recover 让对账
                # 调度器接管，不能先提交上下文后在同进程重试时重复追加一轮。
                await self._set_event_status(
                    comment_id,
                    "recover",
                    next_attempt_at=self.now()
                    + _cfg(self.config, "retry_base_seconds", 5),
                )
                return
            context = self.context_manager
            if context is not None and text is not None and hasattr(context, "append_exchange"):
                session_key = getattr(request, "session_key", "")
                username = _clean_label(getattr(request, "username", ""))
                user_text = getattr(request, "user_text", "")
                published = self._published_text(outcome, text)
                if published is not None:
                    context.append_exchange(
                        session_key,
                        f"[评论作者：@{username}]\n---\n{user_text}",
                        published,
                    )
            # CommentSender normally performs this write after a remote success.  Keep
            # the service-level transition too so a narrow sender adapter cannot leave
            # a successfully delivered event pending.
            await self._set_event_status(comment_id, "done")
            return
        if reason in {"minute", "backoff", "article"}:
            await self._set_event_status(
                comment_id,
                "waiting_rate_limit",
                next_attempt_at=self.now() + _cfg(self.config, "retry_base_seconds", 5),
            )
        elif reason == "recover":
            await self._set_event_status(
                comment_id,
                "recover",
                next_attempt_at=self.now() + _cfg(self.config, "retry_base_seconds", 5),
            )
        elif reason in {"quota", "reply_target_gone", "forbidden", "csrf"}:
            await self._set_event_status(comment_id, f"skipped_{reason}")
        else:
            await self._set_event_status(comment_id, "skipped_failed")

    def _published_text(self, outcome: object, original: str) -> str | None:
        """返回 Sender 实际发布的脱敏、截断正文，供上下文而非日志使用。"""
        # 新 Sender 返回实际发布正文；接受几个仅表示正文的兼容字段，不能依赖
        # 模型原文，因为发布层可能做了密钥脱敏和自然段截断。
        for name in ("published_text", "published_content", "content", "body"):
            if hasattr(outcome, name):
                value = getattr(outcome, name, None)
                # Sender 的 deduped/recoverable 结果明确携带 content=None，
                # 表示本次没有可安全取得的实际正文；禁止回退到模型原文。
                return value if isinstance(value, str) else None

        sender = self.sender
        redactor = getattr(sender, "_redactor", None)
        value = original
        if redactor is not None:
            redact = getattr(redactor, "redact", None)
            if callable(redact):
                try:
                    redacted = redact(value)
                except Exception:
                    redacted = value
                if isinstance(redacted, str):
                    value = redacted
        sender_cfg = getattr(sender, "_cfg", None)
        limit = getattr(sender_cfg, "max_output_chars", 5000)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            limit = 5000
        return truncate_at_paragraph(value, limit)[0]

    async def _set_event_status(
        self,
        comment_id: object,
        status: str,
        *,
        next_attempt_at: float | None = None,
    ) -> None:
        if not isinstance(comment_id, str):
            return
        await _call_optional(
            self.store,
            ("set_comment_event_status", "mark_comment_handled"),
            comment_id=comment_id,
            status=status,
            next_attempt_at=next_attempt_at,
            now=self.now(),
        )

    async def _waiting_dispatcher(self) -> None:
        """把到期 waiting_queue 事件重新投递；未知 Store 接口时安全空转。"""
        interval = min(5, max(1, int(_cfg(self.config, "retry_base_seconds", 5))))
        try:
            while True:
                await self._dispatch_waiting()
                await self.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def _dispatch_waiting(self) -> None:
        rows = await _call_optional(
            self.store,
            (
                "comment_events_due",
                "due_comment_events",
                "waiting_comment_events",
                "list_waiting_comment_events",
            ),
            now=self.now(),
        )
        if rows is None:
            return
        for item in rows:
            candidate = item if isinstance(item, CommentCandidate) else None
            claim = getattr(item, "claim", None)
            if candidate is None and isinstance(item, tuple) and len(item) >= 4:
                candidate, claim = await self._recover_tuple(item)
            if candidate is None or not isinstance(claim, CommentClaim):
                continue
            if await self._enqueue_candidate(candidate, claim):
                await _call_optional(
                    self.store,
                    ("set_comment_event_status", "mark_comment_queued"),
                    comment_id=candidate.comment_id,
                    status="queued",
                    next_attempt_at=None,
                    now=self.now(),
                )

    async def _recover_tuple(
        self, item: tuple[object, ...]
    ) -> tuple[CommentCandidate | None, CommentClaim | None]:
        """从 Store 的无正文 waiting 摘要重新读取当前评论节点。"""
        comment_id = item[0] if isinstance(item[0], str) else None
        blog_id = item[1] if isinstance(item[1], str) else None
        parent_id = item[2] if isinstance(item[2], str) else None
        conversation_id = item[3] if isinstance(item[3], str) else None
        if not comment_id or not blog_id:
            return None, None
        try:
            roots = await self.client.fetch_blog_comments(blog_id)
            index = CommentTreeIndex.from_roots(
                roots, max_nodes=_cfg(self.config, "max_tree_nodes", 10_000)
            )
        except (CommentTreeTooLarge, SiteCommentTreeTooLarge) as exc:
            await self._set_event_status(comment_id, "skipped_tree_too_large")
            log_event(
                self.logger,
                logging.WARNING,
                "comment.recovery_tree_too_large",
                error=type(exc).__name__,
            )
            return None, None
        except Exception as exc:
            # SiteClient wraps parser overflow as SiteError(200,
            # comment_tree_too_large); preserve the permanent classification even
            # when the caller has not imported the site exception type.
            if getattr(exc, "status", None) == 404:
                await self._set_event_status(comment_id, "skipped_deleted")
                return None, None
            overflow_message = getattr(exc, "message", "") or str(exc)
            if overflow_message in {
                "comment_tree_too_large",
                "response_too_large",
            }:
                await self._set_event_status(
                    comment_id,
                    "skipped_tree_too_large"
                    if overflow_message == "comment_tree_too_large"
                    else "skipped_oversize",
                )
                return None, None
            log_event(
                self.logger,
                logging.WARNING,
                "comment.recovery_tree_failed",
                error=type(exc).__name__,
            )
            return None, None
        node = index.by_id.get(comment_id)
        if node is None:
            # The node was deleted/withdrawn between the original event and
            # recovery. This is a permanent fact, not a retryable tree failure.
            await self._set_event_status(comment_id, "skipped_deleted")
            return None, None
        if node.is_deleted:
            await self._set_event_status(comment_id, "skipped_deleted")
            return None, None
        if node.status != "approved":
            await self._set_event_status(comment_id, "skipped_not_approved")
            return None, None
        if not node.author.id:
            await self._set_event_status(comment_id, "skipped_author_missing")
            return None, None
        if self.bot_user_id is not None and node.author.id == self.bot_user_id:
            await self._set_event_status(comment_id, "skipped_self")
            return None, None
        parent = index.by_id.get(parent_id) if parent_id else None
        parent_text = parent.content if parent is not None else None
        status = await _call_optional(
            self.store,
            ("comment_event_status", "get_comment_event_status"),
            comment_id=comment_id,
        )
        if status not in (None, "recover"):
            # waiting_queue / waiting_rate_limit 已经属于当前库中的任务；调度器
            # 只负责重新投递，不应再次 claim（claim 仅对 recover 原子认领）。
            return (
                CommentCandidate(
                    node,
                    "recovery",
                    reason="waiting_retry",
                    parent_bot_text=parent_text,
                ),
                CommentClaim(True, comment_id, conversation_id, "queued"),
            )
        result = await _call_optional(
            self.store,
            ("claim_comment", "claim_comment_event"),
            comment_id=comment_id,
            blog_id=blog_id,
            parent_id=parent_id,
            source="recovery",
            requested_conversation_id=conversation_id,
            force_new_conversation=False,
            observed_created_at=node.created_at,
            now=self.now(),
        )
        if isinstance(result, CommentClaim):
            return (
                CommentCandidate(
                    node,
                    "recovery",
                    reason="recovery",
                    parent_bot_text=parent_text,
                ),
                result,
            )
        if result is False:
            return None, None
        return (
            CommentCandidate(
                node,
                "recovery",
                reason="recovery",
                parent_bot_text=parent_text,
            ),
            CommentClaim(True, comment_id, conversation_id, "queued"),
        )


__all__ = ["CommentService", "CommentServiceStatus", "GatedModelClient"]
