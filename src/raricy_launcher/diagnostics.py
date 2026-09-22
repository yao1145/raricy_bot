"""Launcher 启动早期的受控诊断出口。

无终端发行包不能假设 ``sys.stdout`` / ``sys.stderr`` 存在（PyInstaller
windowed 模式下它们可能是 ``None``，直接 ``StreamHandler(sys.stderr)``
会让启动在写第一条日志前崩掉）。本模块在最早可行的时机安装一个**有界**
的诊断文件 handler：单文件字节上限 + 单个滚动备份，永不无限增长。

事件输出复用 `raricy_bot.logging_setup` 的白名单与「最终输出脱敏」格式器，
Launcher 不另造一套安全规则；可写字段因此天然受限。安装本身失败时只降级
到控制台（若存在），绝不向外抛异常让桌面入口无声死掉。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from raricy_bot.logging_setup import LOG_FORMAT, RedactingFormatter, get_logger

# 有界诊断：单文件 256 KiB、保留 1 个滚动备份（LIGHT_EDITION_DESIGN §12）。
LOG_MAX_BYTES: int = 256 * 1024
LOG_BACKUP_COUNT: int = 1

_LOGGER_NAME: str = "launcher"
_DIAGNOSTICS_DIR: str = "diagnostics"
_LOG_FILE: str = "launcher.log"

# 标记本模块安装的 handler：测试框架等外来 handler 不能触发「已安装」短路。
_OWN_ATTR: str = "_raricy_launcher_diagnostic"


def own_handlers(logger: logging.Logger) -> list[logging.Handler]:
    """logger 上由本模块安装的 handler（忽略测试框架挂上的外来 handler）。"""
    return [h for h in logger.handlers if getattr(h, _OWN_ATTR, False)]


def install(data_root: Path, *, console: bool = True) -> logging.Logger:
    """安装 Launcher 的诊断出口，返回组件 logger；可重复调用且不叠加 handler。

    ``data_root`` 不可写时退化为仅控制台；``console`` 只在 ``sys.stderr``
    真实存在时生效。任何 ``OSError`` 都被吞掉 —— 诊断出口的故障不能反过来
    杀死它要诊断的进程。
    """
    logger = get_logger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if own_handlers(logger):
        return logger

    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(RedactingFormatter(LOG_FORMAT))
        setattr(stream, _OWN_ATTR, True)
        logger.addHandler(stream)

    try:
        directory = Path(data_root) / _DIAGNOSTICS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / _LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(RedactingFormatter(LOG_FORMAT))
        setattr(file_handler, _OWN_ATTR, True)
        logger.addHandler(file_handler)
    except OSError:
        # 目录不可建 / 文件不可写：保留控制台出口（若有），不抛出。
        pass
    return logger
