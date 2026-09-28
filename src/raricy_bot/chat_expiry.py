"""聊天消息创建时间解析与过期判定。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_WALL_TIME_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}"
)
_ISO_WALL_TIME_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z"
)
_UTC8 = timezone(timedelta(hours=8))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# 站点字段只有秒精度；容忍最多 5 分钟的服务端与机器人时钟偏差。
FUTURE_CLOCK_SKEW_SECONDS: int = 300


def parse_created_at_epoch(created_at: object) -> float | None:
    """把站方已观测到的两种 UTC+8 墙上时间格式转为 epoch 秒。"""
    if not isinstance(created_at, str):
        return None
    if _WALL_TIME_PATTERN.fullmatch(created_at) is not None:
        format_string = "%Y-%m-%d %H:%M:%S"
    elif _ISO_WALL_TIME_PATTERN.fullmatch(created_at) is not None:
        # 线上 2026-09-28 返回 ISO-Z，但数值比容器 UTC 快 8 小时；Z 在此不是 UTC。
        format_string = (
            "%Y-%m-%dT%H:%M:%S.%fZ"
            if "." in created_at
            else "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        return None
    try:
        local_time = datetime.strptime(created_at, format_string).replace(tzinfo=_UTC8)
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
