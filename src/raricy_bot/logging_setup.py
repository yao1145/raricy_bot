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
        # 大区共享链与容量治理（D-20 / D-23）：只放 id 与字节数，绝不放用户名或正文。
        "thread_root_id",
        # 评论对象公开 UUID 仅用于诊断；正文、用户名与 actor id 不在白名单内。
        "comment_id",
        "blog_id",
        "notification_id",
        "conversation_id",
        "source",
        "size_bytes",
        "limit_bytes",
        # MCP：设计 §8.3 允许记录服务器名、工具名与 feature 名。三者都来自配置，
        # 不是用户数据；模型自己生成的工具名**不在**此处（拒绝路径只记 reason）。
        "server",
        "tool",
        "feature",
    }
)

# 日志行格式：单行输出，正文只保留稳定字段。
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s %(message)s"

# 必须压制的第三方 logger。
# 把根 logger 设成 DEBUG 会连带打开它们的 DEBUG 输出，而 `openai._base_client`
# 在 DEBUG 下会打印完整请求体，包含 system prompt 与用户正文：
#
#   openai._base_client DEBUG Request options: {... 'json_data': {'messages': [...]}}
#
# 这直接违反 §19.1「任何级别不得出现正文或模型请求体」。
# 这里用**定级**而不是「发现敏感串就过滤」：SDK 的日志格式随版本变化，
# 字符串过滤器很容易漏掉嵌套字段。降级后仍可诊断 —— 我们自己的
# `model.retry` / 错误类别 / HTTP 状态等走 `raricy.*` 命名空间，不受影响。
NOISY_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
    "httpcore._trace",
    "anyio",
)

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
    """配置根 logger：单行格式输出到 stderr，并挂上脱敏过滤器。

    同时把第三方库（模型 SDK、HTTP 栈）的级别压到 WARNING —— 否则根 logger 设成
    DEBUG 时会连带打开它们的 DEBUG 输出，把模型请求体写进 stderr。
    """
    global _handler
    root = logging.getLogger()
    root.setLevel(level.upper())

    # 放在 if 之外：本函数可能被重复调用，每次都要确保压制生效。
    for name in NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

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
