"""纯调度决策（INTERFACES §53.8）：无 I/O、无随机源、无时钟读取。

时间语义固定为 **UTC+8**，不跟系统时区：站方的日限额按 UTC+8 零点切（`dayStart`），
本地若按服务器本地时区切，在 TZ=UTC 的机器上会把头 8 小时发的东西算进前一天。

本模块同时是本子域**唯一**的 UTC+8 日历实现：`store.py` 推导 `retry_after_day` 与计费日期时
import 这里的 `utc8_day` / `utc8_next_day`，不另写一份。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

from ..config import TIER_MAYBE, BlogTaskConfig
from .models import RunCandidate

# 固定东八区。不用 zoneinfo：那会依赖宿主机的时区数据库，而这里要的就是一个死数。
UTC8 = timezone(timedelta(hours=8))

# 枚举窗口的硬上限（秒）。积压超过这个跨度还没开始的点不再补发 ——
# 停机一整天后一次性生成几十篇文章，比漏发危险得多。
SCAN_WINDOW_SECONDS: float = 300.0

_SECONDS_PER_MINUTE: int = 60


def utc8_day(now: float) -> str:
    """把 epoch 秒转成 UTC+8 的 `YYYY-MM-DD`。"""
    return datetime.fromtimestamp(now, UTC8).strftime("%Y-%m-%d")


def utc8_next_day(day: str) -> str:
    """`YYYY-MM-DD` 的次日。"""
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def scheduled_epoch(day: date, hhmm: str) -> float:
    """某天的 `HH:MM`（UTC+8）对应的 epoch 秒。"""
    hour, minute = (int(part) for part in hhmm.split(":"))
    moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC8)
    return moment.timestamp()


def select_run(task: BlogTaskConfig, *, random_value: float) -> bool:
    """这一次调度到底发不发。

    **纯函数**：调用方先确认执行键不存在，再用注入的随机源取一次 `random_value` 传进来。
    掷骰的结果由调用方落库，因此「概率未中」不会在重启后被重新掷一次。

    `must` 恒为真；`maybe` 用 `<` 比较 —— 概率配成 0 在加载期就是错误，所以这里不必再挡。
    """
    if task.tier != TIER_MAYBE:
        return True
    probability = task.probability
    if probability is None:
        # 加载期已经保证 maybe 必带概率；真走到这里只能是配置对象被手搓过，宁可不发。
        return False
    return random_value < probability


def due_runs(
    tasks: Sequence[BlogTaskConfig],
    *,
    scan_start: float,
    now: float,
    startup: bool,
) -> tuple[RunCandidate, ...]:
    """枚举 `(scan_start, now]` 内到点的调度点，按时间与配置声明顺序排列。

    - 半开区间 `(scan_start, now]`：`scan_start` 是上一次扫描的时刻，用左开避免同一个点被扫两次。
    - 下界同时收紧到 `now - SCAN_WINDOW_SECONDS`：向前跳时钟或长时间停机之后不会形成
      无界的补发队列；更早的遗漏由调用方记一条聚合日志。
    - `startup=True` 时只处理**当前分钟**的点，不追补停机期间更早的点。实现上把下界抬到
      当前分钟开头再退 1 秒，于是「正好落在分钟边界上的那个点」仍然被包含进来。
    """
    if now < scan_start and not startup:
        # 时钟回拨：把下界压到 now，本次窗口为空。防重最终仍由执行键的唯一约束兜底。
        scan_start = now

    start = _window_start(scan_start=scan_start, now=now, startup=startup)

    today = datetime.fromtimestamp(now, UTC8).date()
    # 昨天也要看：跨午夜的那几分钟里，前一天的 23:5x 仍然落在窗口内。
    days = (today - timedelta(days=1), today)

    candidates: list[RunCandidate] = []
    for order, task in enumerate(tasks):
        for hhmm in task.schedule:
            for day in days:
                scheduled_at = scheduled_epoch(day, hhmm)
                if start < scheduled_at <= now:
                    candidates.append(
                        RunCandidate(
                            task_name=task.name,
                            scheduled_at=scheduled_at,
                            task_order=order,
                        )
                    )

    candidates.sort(key=lambda candidate: (candidate.scheduled_at, candidate.task_order))
    return tuple(candidates)


def _window_start(*, scan_start: float, now: float, startup: bool) -> float:
    """枚举窗口的左开下界。"""
    if startup:
        minute_start = (int(now) // _SECONDS_PER_MINUTE) * _SECONDS_PER_MINUTE
        # 退 1 秒：分钟边界上的那个点必须落在 `(start, now]` 里。
        return float(minute_start) - 1.0
    return max(scan_start, now - SCAN_WINDOW_SECONDS)
