"""发送配额守卫：每分钟滑动窗口 + 24 小时滚动总量 + 通知冷却 + 站点退避。

判定口径见 `docs/INTERFACES.md` §10 与 `docs/DESIGN_DECISIONS.md` D-1 / D-2：

- 750 只约束 `kind="reply"`；790 是三种 kind 的合计上限；
- 每分钟是滑动窗口（SQLite 发送时间 + 在途预留），不是内存令牌桶，重启后仍然准确；
- `kind` 是三值枚举（`"reply"` / `"notice"` / `"notice_local"`）：
  `"notice"` 是**主动**通知（busy / failure / quota），占用「每频道 24 小时一条」名额
  并受 `notice_cooldown_seconds` 冷却约束；`"notice_local"` 是应答明确用户动作的
  本地回复，两条通知约束都不适用，但同样计入 24 小时总量与每分钟窗口。
  两者必须是**不同的 kind 值**，不能共用一个 kind 再用布尔参数区分（D-1）。

`reserve()` 内部用 `asyncio.Lock` 把「读计数 + 登记预留」做成一个原子步骤，
并发调用时不会互相看不到对方的在途预留。
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .config import BehaviorConfig
from .logging_setup import get_logger, log_event
from .store import Store

logger = get_logger("quota")

# 时间窗口（秒）。
MINUTE_SECONDS: float = 60.0
DAY_SECONDS: float = 86400.0

# 通知冷却只参照主动通知本身；本地回复（notice_local）不得消耗该名额（D-1）。
NOTICE_REFERENCE_KINDS: tuple[str, ...] = ("notice",)


class Decision(enum.Enum):
    """一次 reserve() 的判定结果。"""

    ALLOW = "allow"
    DENY_DAILY = "daily"
    DENY_NOTICE = "notice"
    DENY_MINUTE = "minute"
    DENY_BACKOFF = "backoff"


@dataclass(frozen=True)
class QuotaResult:
    """判定结果；`retry_after` 只在退避时给出（秒）。"""

    decision: Decision
    retry_after: float | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


class QuotaGuard:
    """发送配额守卫。时间源可注入，便于测试用可控时钟。"""

    def __init__(
        self,
        store: Store,
        cfg: BehaviorConfig,
        *,
        now: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._now = now
        self._mono = mono
        self._lock = asyncio.Lock()
        # 在途预留：(kind, channel_id) -> 笔数。
        self._pending: dict[tuple[str, str], int] = {}
        # 本进程的发送时刻（monotonic），用于每分钟窗口；与 SQLite 计数取较大者。
        self._recent: deque[float] = deque()
        # 站点 429 退避截止时刻（monotonic）。
        self._backoff_until: float = 0.0

    # --- 对外接口 -----------------------------------------------------------

    async def reserve(self, channel_id: str, kind: str) -> QuotaResult:  # MUT-A
        """原子地判定并登记一笔预留；通过时调用方最终必须 note_sent() 或 release()。

        `kind` 是三值枚举，行为完全由它决定（见模块 docstring 与 D-1）：

        - `"reply"`：模型回复，受 `daily_normal_limit` 约束；
        - `"notice"`：主动通知，另受「每频道 24 小时一条」+ 冷却约束；
        - `"notice_local"`：应答用户动作的本地回复，不受上述两条通知约束。
        """
        async with self._lock:
            # 1. 站点退避：任何 kind 都不放行。
            remaining = self.backoff_remaining
            if remaining > 0:
                return QuotaResult(Decision.DENY_BACKOFF, remaining)

            now = self._now()
            since_daily = now - DAY_SECONDS
            # 在途预留也要计入，否则并发时会各自看到「还没发出去」的旧计数。
            pending_total = sum(self._pending.values())
            total = await self._store.count_sends_since(since_daily) + pending_total

            # 2. 绝对上限：完全静默。
            if total >= self._cfg.daily_absolute_limit:
                return QuotaResult(Decision.DENY_DAILY)

            # 3. 正常回复额度：只约束模型回复。
            if kind == "reply" and total >= self._cfg.daily_normal_limit:
                return QuotaResult(Decision.DENY_DAILY)

            # 4. 主动通知的频道级约束：每频道 24 小时一条 + 冷却。
            #    只统计 kind == "notice"；notice_local 绝不能算进来（D-1）。
            if kind == "notice":
                notice_count = await self._store.count_channel_sends_since(
                    channel_id, since_daily, "notice"
                )
                if notice_count + self._pending_count("notice", channel_id) >= 1:
                    return QuotaResult(Decision.DENY_NOTICE)
                last_at = await self._store.last_notice_at(channel_id, NOTICE_REFERENCE_KINDS)
                if last_at is not None and now < last_at + self._cfg.notice_cooldown_seconds:
                    return QuotaResult(Decision.DENY_NOTICE)

            # 5. 每分钟滑动窗口。
            window_start = self._mono() - MINUTE_SECONDS
            while self._recent and self._recent[0] <= window_start:
                self._recent.popleft()
            memory_count = len(self._recent)
            sql_count = await self._store.count_sends_since(now - MINUTE_SECONDS)
            if max(memory_count, sql_count) + pending_total >= self._cfg.minute_attempt_limit:
                return QuotaResult(Decision.DENY_MINUTE)

            # 6. 全部通过：登记预留。
            key = (kind, channel_id)
            self._pending[key] = self._pending.get(key, 0) + 1
            return QuotaResult(Decision.ALLOW)

    async def note_sent(self, channel_id: str, reply_to: int | None, kind: str) -> None:
        """预留转正：写 send_attempts 一行、记一笔内存窗口、释放预留。

        调用方在**消息已经发出**之后才调用本方法，因此写库失败时不能向上抛：
        抛异常会把一次已成功的投递误报成失败。此时降级为「只记内存窗口 +
        记一条 error 日志」，并且**必须释放预留**，否则这笔预留会永久占用
        分钟额度（kind="notice" 时还会永久占掉该频道的通知名额）。
        """
        async with self._lock:
            await self._store.record_send_attempt(channel_id, reply_to, kind)
            self._recent.append(self._mono())
            self._consume_pending(channel_id, kind)

    async def release(self, channel_id: str, kind: str) -> None:
        """发送失败/放弃：只释放预留，不写 send_attempts。"""
        async with self._lock:
            self._consume_pending(channel_id, kind)

    def backoff(self, seconds: float) -> None:
        """站点 429 之后调用：在指定秒数内拒绝一切发送。"""
        self._backoff_until = self._mono() + max(0.0, float(seconds))

    @property
    def backoff_remaining(self) -> float:
        """退避剩余秒数；未在退避中返回 0。"""
        return max(0.0, self._backoff_until - self._mono())

    # --- 内部 ---------------------------------------------------------------

    def _pending_count(self, kind: str, channel_id: str) -> int:
        return self._pending.get((kind, channel_id), 0)

    def _consume_pending(self, channel_id: str, kind: str) -> None:
        """扣减一笔预留；没有对应预留时记一条日志，绝不让计数变成负数。"""
        key = (kind, channel_id)
        count = self._pending.get(key, 0)
        if count <= 0:
            log_event(
                logger,
                logging.WARNING,
                "quota.release_unmatched",
                channel_id=channel_id,
                kind=kind,
            )
            return
        if count == 1:
            del self._pending[key]
        else:
            self._pending[key] = count - 1
