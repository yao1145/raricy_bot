"""博客评论独立配额与节流。"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from ..config import CommentConfig
from ..store import Store

_MINUTE_SECONDS = 60.0
_DAY_SECONDS = 86400.0
_BACKOFF_KEY = "comment:global_backoff"


class CommentQuotaDecision(enum.Enum):
    """评论发送预留判定。"""

    ALLOW = "allow"
    DENY_DAILY = "daily"
    DENY_DAILY_REPLY = "daily_reply"
    DENY_MINUTE = "minute"
    DENY_ARTICLE = "article"
    DENY_BACKOFF = "backoff"
    DENY_DAILY_NORMAL = "daily_reply"
    DENY_DAILY_TOTAL = "daily"


@dataclass(frozen=True)
class CommentQuotaResult:
    """预留结果；retry_at 只在可等待限制时给出。"""

    decision: CommentQuotaDecision
    retry_at: float | None = None
    reservation_token: str | None = None
    decision_at: float | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is CommentQuotaDecision.ALLOW

    @property
    def retry_after(self) -> float | None:
        # 结果的等待时间必须在判定时刻冻结；这里不能再次读取真实时钟，
        # 否则注入时钟的测试和调用方看到的值都会随读取时间漂移。
        if self.retry_at is None or self.decision_at is None:
            return None
        return max(0.0, self.retry_at - self.decision_at)

    @property
    def token(self) -> str | None:
        """reservation_token 的短别名，便于发送器适配不同版本接口。"""
        return self.reservation_token

    @property
    def reason(self) -> str:
        """返回发送器可直接使用的稳定原因。"""
        return {
            CommentQuotaDecision.DENY_DAILY: "quota",
            CommentQuotaDecision.DENY_DAILY_REPLY: "quota",
            CommentQuotaDecision.DENY_MINUTE: "minute",
            CommentQuotaDecision.DENY_ARTICLE: "article",
            CommentQuotaDecision.DENY_BACKOFF: "backoff",
            CommentQuotaDecision.ALLOW: "allow",
        }[self.decision]


class CommentQuotaGuard:
    """使用 SQLite 发送历史与进程内预留的评论配额守卫。"""

    def __init__(
        self,
        store: Store,
        cfg: CommentConfig,
        *,
        now: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._now = now
        self._mono = mono
        self._lock = asyncio.Lock()
        self._pending: dict[tuple[str, str], int] = {}
        self._recent: list[float] = []
        self._backoff_until_mono = 0.0
        self._reservations: dict[str, tuple[str, str, str | None]] = {}
        self._finalized_triggers: dict[tuple[str, str, str], float] = {}
        self._finalized_tokens: dict[str, float] = {}

    @property
    def backoff_remaining(self) -> float:
        """返回本进程内退避剩余秒数。"""
        return max(0.0, self._backoff_until_mono - self._mono())

    async def reserve(
        self,
        blog_id: str,
        kind: str = "reply",
        *,
        trigger_comment_id: str | None = None,
    ) -> CommentQuotaResult:
        """按固定顺序检查全局退避、日限、分钟限和文章冷却并登记预留。"""
        if kind not in {"reply", "notice_local"}:
            raise ValueError("评论 kind 必须是 reply 或 notice_local")
        async with self._lock:
            now = self._now()
            self._prune_finalized(now)
            persisted_backoff = await self._store.get_cooldown(_BACKOFF_KEY, now=now)
            if self.backoff_remaining > 0 or persisted_backoff is not None:
                until = max(now + self.backoff_remaining, persisted_backoff or now)
                return self._result(CommentQuotaDecision.DENY_BACKOFF, until, now)

            pending_total = sum(self._pending.values())
            day_total = await self._store.count_comment_sends_since(now - _DAY_SECONDS)
            if day_total + pending_total >= self._cfg.daily_absolute_limit:
                return self._result(CommentQuotaDecision.DENY_DAILY, now=now)
            if kind == "reply":
                day_reply = await self._store.count_comment_sends_since(
                    now - _DAY_SECONDS, kind="reply"
                )
                pending_replies = sum(
                    count for (pending_blog, pending_kind), count in self._pending.items()
                    if pending_kind == "reply"
                )
                if day_reply + pending_replies >= self._cfg.daily_reply_limit:
                    return self._result(CommentQuotaDecision.DENY_DAILY_REPLY, now=now)

            window_start = now - _MINUTE_SECONDS
            monotonic_now = self._mono()
            self._recent = [item for item in self._recent if item > monotonic_now - _MINUTE_SECONDS]
            minute_db = await self._store.count_comment_sends_since(window_start)
            if max(len(self._recent), minute_db) + pending_total >= self._cfg.minute_attempt_limit:
                return self._result(
                    CommentQuotaDecision.DENY_MINUTE, now + _MINUTE_SECONDS, now
                )

            # 同文章的在途预留也先挡住，避免并发任务同时穿过 5 秒间隔检查。
            if any(pending_blog == blog_id for pending_blog, _ in self._pending):
                return self._result(
                    CommentQuotaDecision.DENY_ARTICLE,
                    now + self._cfg.article_cooldown_seconds,
                    now,
                )
            latest = await self._latest_blog_attempt(blog_id, now)
            if latest is not None:
                ready_at = latest + self._cfg.article_cooldown_seconds
                if ready_at > now:
                    return self._result(CommentQuotaDecision.DENY_ARTICLE, ready_at, now)

            key = (blog_id, kind)
            self._pending[key] = self._pending.get(key, 0) + 1
            token = uuid.uuid4().hex
            self._reservations[token] = (blog_id, kind, trigger_comment_id)
            return self._result(
                CommentQuotaDecision.ALLOW, reservation_token=token, now=now
            )

    async def note_sent(
        self,
        blog_id: str,
        trigger_comment_id: str | None,
        kind: str = "reply",
        *,
        reservation_token: str | None = None,
    ) -> None:
        """远端确认送达后幂等转正并释放对应预留。"""
        async with self._lock:
            now = self._now()
            self._prune_finalized(now)
            reservation = (
                self._reservations.get(reservation_token)
                if reservation_token is not None
                else None
            )
            if reservation is not None:
                reserved_blog, reserved_kind, reserved_trigger = reservation
                if reserved_blog != blog_id or reserved_kind != kind:
                    # token 不能跨文章或发送类型使用；错误调用不应释放别人的预留。
                    return
                if reserved_trigger is not None:
                    if trigger_comment_id is not None and trigger_comment_id != reserved_trigger:
                        return
                    trigger_comment_id = reserved_trigger
            trigger_key = (
                (blog_id, trigger_comment_id, kind)
                if trigger_comment_id is not None
                else None
            )
            if (
                (reservation_token is not None and reservation_token in self._finalized_tokens)
                or (trigger_key is not None and trigger_key in self._finalized_triggers)
            ):
                self._consume(
                    blog_id,
                    kind,
                    trigger_comment_id=trigger_comment_id,
                    reservation_token=reservation_token,
                )
                return

            # 重启后 token 不在内存，但 Store 里的触发键仍是权威去重依据。
            # 查找与记录都在 Store 的单连接锁下执行，避免重复转正。
            already_recorded = False
            if trigger_comment_id is not None:
                exists = getattr(self._store, "comment_send_attempt_exists", None)
                if exists is not None:
                    try:
                        value = exists(blog_id, trigger_comment_id, kind)
                        already_recorded = (
                            await value if hasattr(value, "__await__") else bool(value)
                        )
                    except Exception:
                        # 查询失败时仍尽力记录；远端已经成功，不把它改报成失败。
                        already_recorded = False
            try:
                if not already_recorded:
                    await self._store.record_comment_send_attempt(
                        blog_id, trigger_comment_id, kind, attempted_at=now
                    )
            except Exception:
                # 远端已经送达时，本地写失败不能把成功误报成发送失败。
                pass
            finally:
                if not already_recorded:
                    self._recent.append(self._mono())
                if trigger_key is not None:
                    self._finalized_triggers[trigger_key] = now
                if reservation_token is not None:
                    self._finalized_tokens[reservation_token] = now
                self._consume(
                    blog_id,
                    kind,
                    trigger_comment_id=trigger_comment_id,
                    reservation_token=reservation_token,
                )

    async def release(
        self,
        blog_id: str,
        kind: str = "reply",
        *,
        trigger_comment_id: str | None = None,
        reservation_token: str | None = None,
    ) -> None:
        """发送失败或取消时释放预留，不写发送记录。"""
        async with self._lock:
            self._consume(
                blog_id,
                kind,
                trigger_comment_id=trigger_comment_id,
                reservation_token=reservation_token,
            )

    async def backoff(self, seconds: float) -> None:
        """设置评论服务器全局退避，并等待持久化完成。"""
        delay = max(0.0, float(seconds))
        self._backoff_until_mono = self._mono() + delay
        await self._store.set_cooldown(_BACKOFF_KEY, self._now() + delay)

    async def set_backoff(self, seconds: float) -> None:
        """异步版本的退避设置，测试和发送器可等待其完成。"""
        await self.backoff(seconds)

    async def async_backoff(self, seconds: float) -> None:
        """给发送器使用的显式异步退避接口。"""
        await self.backoff(seconds)

    async def _latest_blog_attempt(self, blog_id: str, now: float) -> float | None:
        """读取当前文章最近一次评论发送时刻。"""
        return await self._store.latest_comment_send_at(blog_id, since=now - _DAY_SECONDS)

    def _consume(
        self,
        blog_id: str,
        kind: str,
        *,
        trigger_comment_id: str | None = None,
        reservation_token: str | None = None,
    ) -> None:
        """只释放对应 token 的预留；旧调用没有 token 时按触发键回退。"""
        token = reservation_token
        reservation = self._reservations.get(token) if token is not None else None
        if reservation is None:
            for candidate, value in self._reservations.items():
                if value[0] != blog_id or value[1] != kind:
                    continue
                if trigger_comment_id is not None and value[2] not in {
                    None,
                    trigger_comment_id,
                }:
                    continue
                token, reservation = candidate, value
                break
        if reservation is None:
            return
        self._reservations.pop(token, None)
        key = (reservation[0], reservation[1])
        count = self._pending.get(key, 0)
        if count <= 1:
            self._pending.pop(key, None)
        else:
            self._pending[key] = count - 1

    def _result(
        self,
        decision: CommentQuotaDecision,
        retry_at: float | None = None,
        now: float | None = None,
        *,
        reservation_token: str | None = None,
    ) -> CommentQuotaResult:
        """构造带判定时刻的不可变结果。"""
        return CommentQuotaResult(
            decision=decision,
            retry_at=retry_at,
            reservation_token=reservation_token,
            decision_at=now,
        )

    def _prune_finalized(self, now: float) -> None:
        """限制进程内幂等索引的生命周期；Store 仍保留 90 天权威去重。"""
        cutoff = now - max(0, self._cfg.dedupe_retention_seconds)
        self._finalized_triggers = {
            key: seen_at
            for key, seen_at in self._finalized_triggers.items()
            if seen_at > cutoff
        }
        self._finalized_tokens = {
            key: seen_at
            for key, seen_at in self._finalized_tokens.items()
            if seen_at > cutoff
        }


# 兼容更短的导入名称。
QuotaResult = CommentQuotaResult
Decision = CommentQuotaDecision
CommentQuota = CommentQuotaGuard
