"""日志初始化与结构化事件输出。

所有日志都经过一层脱敏过滤器，密钥类字符串不会落到任何 handler。
业务代码只通过 `log_event()` 输出结构化字段，白名单之外的字段静默丢弃。
"""

from __future__ import annotations

import logging
import sys

from .redact import Redactor

# 允许出现在日志里的字段名白名单。
LOG_FIELDS: frozenset[str] = frozenset(
    {
        "event",
        "component",
        "status",
        "error",
        "kind",
        "reason",
        "event_id",
        "message_id",
        "channel_id",
        "channel_kind",
        "count",
        "attempt",
        "delay",
    }
)

# 日志行格式：单行输出，正文只保留稳定字段。
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s %(message)s"

# 进程级脱敏单例，`register_secret()` 与过滤器共用。
_redactor = Redactor()

# 已安装的 stderr handler，保证 setup_logging 可重复调用而不重复叠加。
_handler: logging.Handler | None = None


class RedactingFilter(logging.Filter):
    """把最终消息文本脱敏后写回 record，任何 logger 的输出都过一遍。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = _redactor.redact(record.getMessage())
        record.msg = message
        record.args = ()
        return True


def setup_logging(level: str = "INFO") -> None:
    """配置根 logger：单行格式输出到 stderr，并挂上脱敏过滤器。"""
    global _handler
    root = logging.getLogger()
    root.setLevel(level.upper())
    if _handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
        _handler = handler


def get_logger(component: str) -> logging.Logger:
    """按组件名取 logger，统一挂在 raricy 命名空间下。"""
    return logging.getLogger(f"raricy.{component}")


def register_secret(value: str) -> None:
    """注册需要脱敏的运行时密钥（如会话 Cookie）。"""
    _redactor.add_secret(value)


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """输出一条结构化事件：`event=<名称> 白名单字段=值`。"""
    parts = [f"event={event}"]
    for key, value in fields.items():
        if key in LOG_FIELDS:
            parts.append(f"{key}={value}")
    logger.log(level, " ".join(parts))
