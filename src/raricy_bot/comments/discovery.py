"""评论区两个轮询发现器。

评论接口没有 SSE。最近评论轮询负责冷启动水位与首次精确提及，通知轮询负责定位
机器人评论的直接回复。两个发现器都只构造候选并调用 Store 的原子 claim，不直接
调用模型或发布评论。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..logging_setup import get_logger, log_event
from ..site.comment_models import (
    CommentNode,
    CommentNotification,
    CommentTreeTooLarge as SiteCommentTreeTooLarge,
    actor_fingerprint,
)
from ..site.client import SiteClient
from ..store import CommentClaim, CommentDiscoveryState, Store
from ..text_utils import is_reset_command, strip_bot_mention
from .matcher import CommentCandidate, CommentMatcher, CommentTreeTooLarge

_logger = get_logger("comments.discovery")
_RECENT_LIMIT = 100


class CommentClient(Protocol):
    async def fetch_recent_comments(self) -> list[CommentNode]: ...

    async def fetch_blog_comments(self, blog_id: str) -> list[CommentNode]: ...

    async def fetch_notifications(self, *, page: int, unread_only: bool = True) -> Any: ...

    async def mark_notification_read(self, notification_id: str) -> None: ...


CandidateCallback = Callable[[CommentCandidate, CommentClaim], Awaitable[None] | None]


@dataclass(frozen=True)
class DiscoveryReport:
    """一次最近评论轮询的无正文结果。"""

    fetched: int = 0
    considered: int = 0
    claimed: int = 0
    baseline: bool = False
    gap_possible: bool = False
    tree_failures: int = 0


@dataclass(frozen=True)
class NotificationReport:
    """一次通知轮询的无正文结果。"""

    fetched: int = 0
    considered: int = 0
    claimed: int = 0
    baseline: bool = False
    unmatched: int = 0
    marked_read: int = 0
    tree_failures: int = 0


@dataclass
class _MemoryDiscoveryState:
    initialized: bool = False
    newest_created_at: float | None = None
    boundary: set[str] = field(default_factory=set)


def _valid_comment(node: object) -> bool:
    """验证轮询 DTO 的最低结构；解析器已经负责 UUID 规范化。"""
    return (
        isinstance(node, CommentNode)
        and bool(node.id)
        and bool(node.blog_id)
        and node.created_at is not None
    )


def _sort_key(node: CommentNode) -> tuple[float, str]:
    return (node.created_at if node.created_at is not None else float("inf"), node.id)


async def _maybe_callback(
    callback: CandidateCallback | None, candidate: CommentCandidate, claim: CommentClaim
) -> None:
    if callback is None:
        return
    result = callback(candidate, claim)
    if inspect.isawaitable(result):
        await result


async def _call_optional(
    target: object,
    names: Iterable[str],
    /,
    **kwargs: object,
) -> object | None:
    """在过渡期兼容 Store 的窄适配层命名；首选名称仍是内部契约名称。"""
    for name in names:
        fn = getattr(target, name, None)
        if fn is None:
            continue
        try:
            value = fn(**kwargs)
        except TypeError:
            # Store 的少数窄方法把必填字段写成位置参数、now/status 写成
            # keyword-only。按签名拆分，避免把 keyword-only 字段误塞进位置参数。
            try:
                signature = inspect.signature(fn)
                positional: list[object] = []
                keyword_only: dict[str, object] = {}
                for parameter in signature.parameters.values():
                    if parameter.name not in kwargs:
                        continue
                    if parameter.kind in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    ):
                        positional.append(kwargs[parameter.name])
                    elif parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                        keyword_only[parameter.name] = kwargs[parameter.name]
                value = fn(*positional, **keyword_only)
            except (TypeError, ValueError):
                continue
        return await value if inspect.isawaitable(value) else value
    return None


class RecentCommentPoller:
    """每轮读取最近 100 条并识别新评论。

    同一轮对同一 blog 只读取一次评论树。树读取失败时不提交发现水位，下一轮可
    重新尝试；这样不会因为临时断网把候选永久推出 100 条窗口。
    """

    def __init__(
        self,
        client: CommentClient | SiteClient,
        store: Store,
        *,
        bot_username: str,
        bot_user_id: str | None = None,
        matcher: CommentMatcher | None = None,
        max_tree_nodes: int = 10_000,
        now: Callable[[], float] = time.time,
        on_candidate: CandidateCallback | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.bot_username = bot_username
        self.bot_user_id = bot_user_id
        self.matcher = matcher or CommentMatcher(
            bot_username=bot_username,
            bot_user_id=bot_user_id,
            max_nodes=max_tree_nodes,
        )
        self.max_tree_nodes = max_tree_nodes
        self.now = now
        self.on_candidate = on_candidate
        self.logger = logger or _logger
        self._memory_state = _MemoryDiscoveryState()
        self._poll_lock = asyncio.Lock()

    async def poll_once(self) -> DiscoveryReport:
        """执行一次轮询；并行触发时跳过，不叠加公开树请求。"""
        if self._poll_lock.locked():
            return DiscoveryReport()
        async with self._poll_lock:
            return await self._poll_once()

    async def _poll_once(self) -> DiscoveryReport:
        """执行一次最近评论轮询。网络错误向上抛给服务循环处理。"""
        recent = list(await self.client.fetch_recent_comments())
        # 上游契约固定为最近 100 条；对测试替身或代理返回的超长数组也保守截断，
        # 不能让一轮异常响应突破发现窗口的内存与缺口语义。
        recent = recent[:_RECENT_LIMIT]
        valid = sorted((node for node in recent if _valid_comment(node)), key=_sort_key)
        state = await self._get_state()
        if not state.initialized:
            await self._save_state(valid)
            return DiscoveryReport(fetched=len(recent), baseline=True)

        newest = state.newest_created_at
        eligible = [
            node
            for node in valid
            if newest is None
            or node.created_at > newest
            or (node.created_at == newest and node.id not in state.boundary)
        ]
        gap_possible = False
        if (
            len(recent) >= _RECENT_LIMIT
            and valid
            and newest is not None
            and valid[0].created_at > newest
        ):
            gap_possible = True
            log_event(
                self.logger,
                logging.WARNING,
                "comment.discovery_gap_possible",
                count=len(recent),
                reason="recent_window",
            )

        # spider DTO 只有 content_html；只有可能提及或父级已知是机器人评论时才
        # 拉整棵树，既控制公开读取量，也保证最终判定只使用 Markdown 原文。
        known = await self._known_bot_ids()
        rough = [
            node
            for node in eligible
            if self._rough_mention(node) or node.parent_id in known
        ]
        tree_failures = 0
        transient_tree_failures = 0
        candidates: list[CommentCandidate] = []
        grouped: defaultdict[str, list[CommentNode]] = defaultdict(list)
        for node in rough:
            grouped[node.blog_id].append(node)
        for blog_id, observed in grouped.items():
            try:
                roots = await self.client.fetch_blog_comments(blog_id)
                index = self.matcher.build_index(roots)
            except (CommentTreeTooLarge, SiteCommentTreeTooLarge):
                tree_failures += 1
                log_event(
                    self.logger,
                    logging.WARNING,
                    "comment.tree_too_large",
                    size_bytes=None,
                    limit_nodes=self.max_tree_nodes,
                )
                await self._skip_tree_candidates(observed, "skipped_tree_too_large")
                continue
            except Exception as exc:
                tree_failures += 1
                overflow = self._tree_overflow_status(exc)
                if overflow is None:
                    transient_tree_failures += 1
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "comment.tree_fetch_failed",
                        error=type(exc).__name__,
                    )
                else:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "comment.tree_too_large",
                        reason=overflow,
                    )
                    await self._skip_tree_candidates(observed, overflow)
                continue
            candidates.extend(
                self.matcher.recent_candidates(
                    observed,
                    index,
                    known_bot_comment_ids=known,
                )
            )

        # 不因一个候选的 claim 失败而中断本轮；每个候选均必须调用同一个原子入口。
        claimed = 0
        seen: set[str] = set()
        for candidate in sorted(candidates, key=lambda item: _sort_key(item.comment)):
            if candidate.comment_id in seen:
                continue
            seen.add(candidate.comment_id)
            claim = await self._claim(candidate)
            if claim.claimed:
                claimed += 1
                await _maybe_callback(self.on_candidate, candidate, claim)

        # 超限是候选的永久终态，可以安全推进水位；只有网络/HTTP 临时失败
        # 才保留旧水位，避免候选被推出最近 100 条窗口。
        if transient_tree_failures == 0:
            await self._save_state(valid)
        return DiscoveryReport(
            fetched=len(recent),
            considered=len(eligible),
            claimed=claimed,
            gap_possible=gap_possible,
            tree_failures=tree_failures,
        )

    async def run_once(self) -> DiscoveryReport:
        """兼容服务编排器使用的名称。"""
        return await self.poll_once()

    def _rough_mention(self, node: CommentNode) -> bool:
        html = node.content_html or ""
        return f"@{self.bot_username}" in html if self.bot_username else False

    @staticmethod
    def _tree_overflow_status(exc: BaseException) -> str | None:
        """把 SiteClient 的响应/树上限错误映射为永久事件终态。"""
        message = getattr(exc, "message", "") or str(exc)
        if message == "response_too_large":
            return "skipped_oversize"
        if message == "comment_tree_too_large":
            return "skipped_tree_too_large"
        return None

    async def _skip_tree_candidates(
        self, observed: Iterable[CommentNode], status: str
    ) -> None:
        """完整树不可安全读取时，逐个原子领取粗筛候选并写永久跳过。"""
        seen: set[str] = set()
        for node in observed:
            if node.id in seen or not _valid_comment(node):
                continue
            seen.add(node.id)
            # 粗筛只把可能 @ 或已知机器人父回复送到这里；不使用树的前缀
            # 猜正文，claim 后直接落上限终态，水位才能前进且不会调用模型。
            candidate = CommentCandidate(node, "recent", reason="tree_oversize")
            claim = await self._claim(candidate)
            if not claim.claimed:
                continue
            await _call_optional(
                self.store,
                ("set_comment_event_status", "mark_comment_handled"),
                comment_id=node.id,
                status=status,
                next_attempt_at=None,
                now=self.now(),
            )

    async def _get_state(self) -> _MemoryDiscoveryState:
        result = await _call_optional(
            self.store,
            ("get_comment_discovery_state", "comment_discovery_state"),
        )
        if result is None:
            return self._memory_state
        if isinstance(result, CommentDiscoveryState):
            self._memory_state = _MemoryDiscoveryState(
                initialized=result.initialized,
                newest_created_at=result.newest_created_at,
                boundary=set(result.boundary),
            )
            return self._memory_state
        initialized = bool(getattr(result, "initialized", False))
        newest = getattr(result, "newest_created_at", None)
        boundary = getattr(result, "boundary", ())
        self._memory_state = _MemoryDiscoveryState(
            initialized=initialized,
            newest_created_at=float(newest) if isinstance(newest, (int, float)) else None,
            boundary={str(value) for value in boundary},
        )
        return self._memory_state

    async def _save_state(self, current: list[CommentNode]) -> None:
        if not current:
            newest = self._memory_state.newest_created_at
            boundary = self._memory_state.boundary
        else:
            newest = max(node.created_at for node in current if node.created_at is not None)
            # 同一时间边界只保留可见 UUID；水位跨过后旧边界自然失效。
            boundary = {node.id for node in current if node.created_at == newest}
            if newest == self._memory_state.newest_created_at:
                boundary |= self._memory_state.boundary
        self._memory_state = _MemoryDiscoveryState(True, newest, boundary)
        payload = {
            "initialized": True,
            "newest_created_at": newest,
            "boundary": tuple(sorted(boundary)),
            "now": self.now(),
        }
        await _call_optional(
            self.store,
            ("set_comment_discovery_state", "update_comment_discovery_state"),
            **payload,
        )

    async def _known_bot_ids(self) -> set[str]:
        result = await _call_optional(
            self.store,
            ("known_bot_comment_ids", "comment_bot_comment_ids", "get_bot_comment_ids"),
        )
        if result is None:
            return set()
        if isinstance(result, Mapping):
            result = result.keys()
        try:
            return {str(value) for value in result if value}
        except TypeError:
            return set()

    async def _claim(self, candidate: CommentCandidate) -> CommentClaim:
        # `/reset` 必须在第一次原子领取前决定会话意图。否则 Store 会依据 parent_id
        # 把 reset 事件写进旧会话，之后再由路由层切换只能造成事件表与上下文分裂。
        content = candidate.comment.content
        user_text = (
            strip_bot_mention(content, self.bot_username)
            if isinstance(content, str)
            else ""
        )
        force_new = is_reset_command(user_text)
        kwargs = {
            "comment_id": candidate.comment.id,
            "blog_id": candidate.comment.blog_id,
            "parent_id": candidate.comment.parent_id,
            "source": candidate.source,
            # Store 在 force_new=true 时以 comment_id 建新链；同时把 ID 传入
            # requested 字段，兼容尚未采用 force_new 的窄测试替身/适配层。
            "requested_conversation_id": candidate.comment.id if force_new else None,
            "force_new_conversation": force_new,
            "observed_created_at": candidate.comment.created_at,
            "now": self.now(),
        }
        result = await _call_optional(self.store, ("claim_comment",), **kwargs)
        if isinstance(result, CommentClaim):
            return result
        # 过渡期的最小替身只需返回 bool；生产 Store 必须使用完整 CommentClaim。
        claimed = bool(result) if result is not None else candidate.comment.id not in getattr(
            self, "_claimed_ids", set()
        )
        claimed_ids = getattr(self, "_claimed_ids", set())
        if claimed:
            claimed_ids.add(candidate.comment.id)
            self._claimed_ids = claimed_ids
        return CommentClaim(
            claimed=claimed,
            comment_id=candidate.comment.id,
            conversation_id=None,
            status="queued" if claimed else "done",
        )


class NotificationPoller:
    """轮询未读通知并在评论树中定位全部真实候选。"""

    def __init__(
        self,
        client: CommentClient | SiteClient,
        store: Store,
        *,
        bot_username: str = "",
        bot_user_id: str | None = None,
        max_pages: int = 5,
        unmatched_attempt_limit: int = 5,
        max_tree_nodes: int = 10_000,
        now: Callable[[], float] = time.time,
        on_candidate: CandidateCallback | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.bot_username = bot_username
        self.bot_user_id = bot_user_id
        self.max_pages = max_pages
        self.unmatched_attempt_limit = unmatched_attempt_limit
        self.matcher = CommentMatcher(
            bot_username=bot_username,
            bot_user_id=bot_user_id,
            max_nodes=max_tree_nodes,
        )
        self.max_tree_nodes = max_tree_nodes
        self.now = now
        self.on_candidate = on_candidate
        self.logger = logger or _logger
        self._baseline_initialized = False
        # 基线跨越五页时持久化“首轮最旧通知”边界；新通知不会被误吞进
        # baseline_ignored，旧通知则在后续轮次继续沿未读窗口安全清空。
        self._baseline_cutoff: tuple[float, str] | None = None
        self._notification_states: dict[str, tuple[str, int]] = {}
        self._poll_lock = asyncio.Lock()

    async def poll_once(self) -> NotificationReport:
        """执行一次通知轮询；已有轮询未结束时跳过。"""
        if self._poll_lock.locked():
            return NotificationReport()
        async with self._poll_lock:
            return await self._poll_once()

    async def _poll_once(self) -> NotificationReport:
        """最多拉取 `max_pages` 页通知；网络失败不消耗 unmatched 次数。"""
        notifications, has_next = await self._fetch_pages_state()
        valid = [notification for notification in notifications if self._valid_notification(notification)]
        baseline_active = False
        if not await self._baseline_done():
            baseline_active = True
            cutoff = await self._get_baseline_cutoff()
            if cutoff is None:
                keys = [self._notification_key(item) for item in valid]
                # 这是冷启动快照的高水位：首轮最多五页中的所有项都属于旧
                # 通知，即便站点时间只有秒级、多个通知时间相同也不能只忽略
                # lexical 最小的一个。后续时间严格更晚的新通知走 live 路径。
                cutoff = max(keys) if keys else None
                if cutoff is not None:
                    await self._set_baseline_cutoff(cutoff)
            baseline_items = [
                item
                for item in valid
                # 站点通知时间常只有秒级；以 timestamp 而非 UUID 作为冷启动
                # 边界，避免第 6 页同一秒的历史通知被误判为 live。相同秒的
                # 新通知宁可等基线完成后再处理，优先保证不批量回复旧通知。
                if cutoff is None or self._notification_key(item)[0] <= cutoff[0]
            ]
            # 比首轮 cutoff 更新的通知属于 live 流量；即使旧基线尚未跨完
            # 五页，也必须继续走正常匹配，不能为防旧回复而丢新回复。
            valid = [item for item in valid if item not in baseline_items]
            baseline_complete = True
            for notification in baseline_items:
                await self._observe(notification, status="baseline_ignored")
                if not await self._mark_read(notification.id):
                    # 基线必须是可重试的持久化检查点。标记已读失败时不能把
                    # remaining notification 当成已忽略，否则重启后会误回复旧通知。
                    baseline_complete = False
            if baseline_complete and not has_next:
                await self._set_baseline_done()
            baseline_report = NotificationReport(
                fetched=len(notifications),
                considered=len(baseline_items),
                # 该轮确实执行了冷启动路径；完成标志另由持久化检查点表示。
                baseline=True,
                marked_read=sum(
                    1
                    for notification in baseline_items
                    if self._notification_states.get(notification.id, ("", 0))[0] == "done"
                ),
            )
            if not valid:
                return baseline_report

        for notification in valid:
            await self._observe(notification, status="observed")

        # 标记已读失败只允许重试标记动作，不得因为下一轮树里突然出现目标而重新
        # 生成回复。candidates_pending 同理等待已有候选进入终态。
        skip_ids: set[str] = set()
        retried_read_ids: set[str] = set()
        for notification in valid:
            state = await self._notification_state(notification.id)
            candidates = await self._notification_candidates(notification.id)
            if state is None:
                continue
            status, attempts = state
            if status in {"done", "baseline_ignored"}:
                # 远端标记已读成功后，即使一个窄测试替身仍把通知返回在
                # unread 窗口中，也不能再次消耗 unmatched 或 claim。
                skip_ids.add(notification.id)
            elif status == "unmatched":
                if await self._mark_read(notification.id):
                    retried_read_ids.add(notification.id)
                skip_ids.add(notification.id)
            elif status == "mark_read_pending":
                # 标记已读失败是独立的生命周期状态。无论通知对应的是候选终态还是
                # unmatched，都只重试远端标记动作，绝不能因评论树变化重新匹配/发送。
                can_mark = not candidates or await self._can_mark_read(notification.id)
                if can_mark and await self._mark_read(notification.id):
                    retried_read_ids.add(notification.id)
                skip_ids.add(notification.id)

        known = await self._known_bot_ids()
        grouped: defaultdict[str, list[CommentNotification]] = defaultdict(list)
        for notification in valid:
            if notification.id not in skip_ids and notification.blog_id is not None:
                grouped[notification.blog_id].append(notification)

        candidates_by_notification: defaultdict[str, list[CommentCandidate]] = defaultdict(list)
        tree_failures = 0
        tree_failed_blogs: set[str] = set()
        for blog_id, blog_notifications in grouped.items():
            try:
                roots = await self.client.fetch_blog_comments(blog_id)
                index = self.matcher.build_index(roots)
            except (CommentTreeTooLarge, SiteCommentTreeTooLarge):
                tree_failures += 1
                log_event(
                    self.logger,
                    logging.WARNING,
                    "comment.tree_too_large",
                    limit_bytes=self.max_tree_nodes,
                )
                continue
            except Exception as exc:
                tree_failures += 1
                if getattr(exc, "message", "") not in {
                    "comment_tree_too_large",
                    "response_too_large",
                }:
                    tree_failed_blogs.add(blog_id)
                log_event(
                    self.logger,
                    logging.WARNING,
                    "comment.tree_fetch_failed",
                    error=type(exc).__name__,
                )
                continue
            for notification in blog_notifications:
                matched = self.matcher.notification_candidates(
                    notification, index, known_bot_comment_ids=known
                )
                candidates_by_notification[notification.id].extend(matched)

        claimed = 0
        unmatched = 0
        marked_read = len(retried_read_ids)
        for notification in valid:
            if notification.id in skip_ids:
                continue
            unique: dict[str, CommentCandidate] = {
                item.comment_id: item
                for item in candidates_by_notification.get(notification.id, ())
            }
            if not unique:
                # 网络/HTTP 结构暂时失败不计 unmatched；已确认超过节点上限则按
                # 契约计一次完整匹配失败（避免超大树通知永远占据未读窗口）。
                if notification.blog_id in tree_failed_blogs:
                    # 树读取失败是临时错误，不计入五次匹配失败。
                    continue
                attempts = await self._increment_unmatched(notification.id)
                unmatched += 1
                if attempts >= self.unmatched_attempt_limit:
                    await self._set_notification_status(notification.id, "unmatched")
                    if await self._mark_read(notification.id):
                        marked_read += 1
                continue

            await self._set_notification_status(notification.id, "candidates_pending")
            for candidate in sorted(unique.values(), key=lambda item: _sort_key(item.comment)):
                await self._add_candidate(notification.id, candidate.comment_id)
                claim = await self._claim(candidate)
                if claim.claimed:
                    claimed += 1
                    await _maybe_callback(self.on_candidate, candidate, claim)
            # 只有候选事件均已终态时才可以标已读；通常由 waiting dispatcher
            # 在事件完成后再次调用 `_try_mark_read`，本轮先保留未读。
            if await self._can_mark_read(notification.id):
                if await self._mark_read(notification.id):
                    marked_read += 1

        return NotificationReport(
            fetched=len(notifications),
            considered=len(valid),
            claimed=claimed,
            baseline=baseline_active,
            unmatched=unmatched,
            marked_read=marked_read,
            tree_failures=tree_failures,
        )

    async def _notification_state(self, notification_id: str) -> tuple[str, int] | None:
        value = await _call_optional(
            self.store,
            ("comment_notification_state", "get_comment_notification_state"),
            notification_id=notification_id,
        )
        if value is None:
            return self._notification_states.get(notification_id)
        status = getattr(value, "status", None)
        attempts = getattr(value, "unmatched_attempts", None)
        if isinstance(value, tuple):
            status = value[0] if value else None
            attempts = value[1] if len(value) > 1 else 0
        if not isinstance(status, str):
            return self._notification_states.get(notification_id)
        return status, int(attempts) if isinstance(attempts, int) and not isinstance(attempts, bool) else 0

    async def _notification_candidates(self, notification_id: str) -> list[str]:
        value = await _call_optional(
            self.store,
            ("comment_notification_candidates", "get_comment_notification_candidates"),
            notification_id=notification_id,
        )
        if value is None:
            return []
        try:
            return [str(item) for item in value if item]
        except TypeError:
            return []

    async def run_once(self) -> NotificationReport:
        return await self.poll_once()

    async def _fetch_pages(self) -> list[CommentNotification]:
        result, _has_next = await self._fetch_pages_state()
        return result

    async def _fetch_pages_state(self) -> tuple[list[CommentNotification], bool]:
        """读取最多五页，并返回是否因页数上限截断了未读尾页。"""
        result: list[CommentNotification] = []
        page = 1
        has_next = False
        while page <= self.max_pages:
            payload = await self.client.fetch_notifications(page=page, unread_only=True)
            items = getattr(payload, "notifications", None)
            if not isinstance(items, tuple | list):
                items = ()
            result.extend(item for item in items if isinstance(item, CommentNotification))
            has_next = bool(getattr(payload, "has_next", False))
            pages = getattr(payload, "pages", page)
            if not has_next or not isinstance(pages, int) or page >= pages:
                break
            page += 1
        return result, has_next and page >= self.max_pages

    @staticmethod
    def _valid_notification(notification: CommentNotification) -> bool:
        return (
            bool(notification.id)
            and not notification.read
            and notification.action == "评论回复"
            and bool(notification.blog_id)
        )

    @staticmethod
    def _notification_key(notification: CommentNotification) -> tuple[float, str]:
        """通知时间边界；缺失时间按最旧处理，ID 保证同刻稳定。"""
        timestamp = notification.timestamp
        return (
            float(timestamp)
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool)
            else float("-inf"),
            notification.id,
        )

    async def _baseline_done(self) -> bool:
        if self._baseline_initialized:
            return True
        result = await _call_optional(
            self.store,
            (
                "comment_notification_baseline_done",
                "is_comment_notification_baseline_initialized",
            ),
        )
        self._baseline_initialized = bool(result) if result is not None else False
        return self._baseline_initialized

    async def _set_baseline_done(self) -> None:
        self._baseline_initialized = True
        await _call_optional(
            self.store,
            (
                "set_comment_notification_baseline_done",
                "mark_comment_notification_baseline",
            ),
            now=self.now(),
        )

    async def _get_baseline_cutoff(self) -> tuple[float, str] | None:
        if self._baseline_cutoff is not None:
            return self._baseline_cutoff
        value = await _call_optional(
            self.store,
            (
                "comment_notification_baseline_cutoff",
                "get_comment_notification_baseline_cutoff",
            ),
        )
        if isinstance(value, tuple) and len(value) >= 2:
            timestamp, ident = value[0], value[1]
            if isinstance(timestamp, str) and isinstance(ident, (int, float)):
                timestamp, ident = ident, timestamp
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and isinstance(ident, str):
                self._baseline_cutoff = (float(timestamp), ident)
        elif isinstance(value, Mapping):
            timestamp = value.get("timestamp")
            ident = value.get("id") or value.get("notification_id")
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and isinstance(ident, str):
                self._baseline_cutoff = (float(timestamp), ident)
        else:
            timestamp = getattr(value, "timestamp", None)
            ident = getattr(value, "notification_id", None)
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and isinstance(ident, str):
                self._baseline_cutoff = (float(timestamp), ident)
        return self._baseline_cutoff

    async def _set_baseline_cutoff(self, cutoff: tuple[float, str]) -> None:
        self._baseline_cutoff = cutoff
        await _call_optional(
            self.store,
            (
                "set_comment_notification_baseline_cutoff",
                "record_comment_notification_baseline_cutoff",
            ),
            timestamp=cutoff[0],
            notification_id=cutoff[1],
            cutoff=cutoff,
            now=self.now(),
        )

    async def _observe(self, notification: CommentNotification, *, status: str) -> None:
        previous = self._notification_states.get(notification.id)
        if not (
            status == "observed"
            and previous is not None
            and previous[0] in {"unmatched", "mark_read_pending", "done", "baseline_ignored"}
        ):
            self._notification_states[notification.id] = (status, 0)
        await _call_optional(
            self.store,
            (
                "upsert_comment_notification",
                "observe_comment_notification",
                "record_comment_notification",
            ),
            notification_id=notification.id,
            blog_id=notification.blog_id,
            actor_fingerprint=actor_fingerprint(notification.actor_id),
            status=status,
            now=self.now(),
        )

    async def _increment_unmatched(self, notification_id: str) -> int:
        value = await _call_optional(
            self.store,
            (
                "increment_comment_notification_unmatched",
                "increment_comment_unmatched",
                "increment_notification_unmatched",
            ),
            notification_id=notification_id,
            now=self.now(),
        )
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        status, attempts = self._notification_states.get(notification_id, ("observed", 0))
        attempts += 1
        self._notification_states[notification_id] = (status, attempts)
        return attempts

    async def _set_notification_status(self, notification_id: str, status: str) -> None:
        _, attempts = self._notification_states.get(notification_id, (status, 0))
        self._notification_states[notification_id] = (status, attempts)
        await _call_optional(
            self.store,
            ("set_comment_notification_status", "update_comment_notification_status"),
            notification_id=notification_id,
            status=status,
            now=self.now(),
        )

    async def _add_candidate(self, notification_id: str, comment_id: str) -> None:
        await _call_optional(
            self.store,
            (
                "add_comment_notification_candidate",
                "record_comment_notification_candidate",
            ),
            notification_id=notification_id,
            comment_id=comment_id,
        )

    async def _can_mark_read(self, notification_id: str) -> bool:
        result = await _call_optional(
            self.store,
            (
                "comment_notification_candidates_terminal",
                "notification_candidates_terminal",
                "can_mark_comment_notification_read",
            ),
            notification_id=notification_id,
        )
        # 未提供查询接口时保守地不标已读；这避免候选仍在 queue 时丢失通知。
        return bool(result) if result is not None else False

    async def _mark_read(self, notification_id: str) -> bool:
        try:
            await self.client.mark_notification_read(notification_id)
        except Exception as exc:
            await self._set_notification_status(notification_id, "mark_read_pending")
            log_event(
                self.logger,
                logging.WARNING,
                "comment.notification_mark_read_failed",
                error=type(exc).__name__,
            )
            return False
        await self._set_notification_status(notification_id, "done")
        return True

    async def _known_bot_ids(self) -> set[str]:
        result = await _call_optional(
            self.store,
            ("known_bot_comment_ids", "comment_bot_comment_ids", "get_bot_comment_ids"),
        )
        if result is None:
            return set()
        try:
            return {str(value) for value in result if value}
        except TypeError:
            return set()

    async def _claim(self, candidate: CommentCandidate) -> CommentClaim:
        # 同 RecentCommentPoller：会话意图必须和 claim 在同一次 Store 原子操作中
        # 提交，不能等 service/router 发现 reset 后再改已持久化事件。
        content = candidate.comment.content
        user_text = (
            strip_bot_mention(content, self.bot_username)
            if isinstance(content, str)
            else ""
        )
        force_new = is_reset_command(user_text)
        result = await _call_optional(
            self.store,
            ("claim_comment",),
            comment_id=candidate.comment.id,
            blog_id=candidate.comment.blog_id,
            parent_id=candidate.comment.parent_id,
            source=candidate.source,
            requested_conversation_id=candidate.comment.id if force_new else None,
            force_new_conversation=force_new,
            observed_created_at=candidate.comment.created_at,
            now=self.now(),
        )
        claimed = bool(result) if result is not None else candidate.comment.id not in getattr(
            self, "_claimed_ids", set()
        )
        if isinstance(result, CommentClaim):
            return result
        claimed_ids = getattr(self, "_claimed_ids", set())
        if claimed:
            claimed_ids.add(candidate.comment.id)
            self._claimed_ids = claimed_ids
        return CommentClaim(
            claimed=claimed,
            comment_id=candidate.comment.id,
            conversation_id=None,
            status="queued" if claimed else "done",
        )


# 文档和外部测试常用的简短名称。
CommentDiscovery = RecentCommentPoller
NotificationDiscovery = NotificationPoller


__all__ = [
    "CommentDiscovery",
    "CommentClient",
    "DiscoveryReport",
    "NotificationDiscovery",
    "NotificationPoller",
    "NotificationReport",
    "RecentCommentPoller",
]
