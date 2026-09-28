"""聊天消息创建时间解析与过期判定。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_CREATED_AT_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}"
)
_UTC8 = timezone(timedelta(hours=8))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# 站点字段只有秒精度；容忍最多 5 分钟的服务端与机器人时钟偏差。
FUTURE_CLOCK_SKEW_SECONDS: int = 300


def parse_created_at_epoch(created_at: object) -> float | None:
    """严格按站方 `YYYY-MM-DD HH:MM:SS` UTC+8 格式转为 epoch 秒。"""
    if (
        not isinstance(created_at, str)
        or _CREATED_AT_PATTERN.fullmatch(created_at) is None
    ):
        return None
    try:
        local_time = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_UTC8
        )
    except ValueError:
        return None
    return (local_time - _EPOCH).total_seconds()


def message_expiry(
    created_at: object, *, now: float, max_age_seconds: int
) -> tuple[float | None, bool]:
    """返回创建时间截止点与是否应跳过；无效时间没有可传递的截止点。"""
    created_at_epoch = parse_created_at_epoch(created_at)
    if created_at_epoch is None:
        return None, True

    expires_at = created_at_epoch + max_age_seconds
    expired = (
        expires_at <= now
        or created_at_epoch > now + FUTURE_CLOCK_SKEW_SECONDS
    )
    return expires_at, expired
