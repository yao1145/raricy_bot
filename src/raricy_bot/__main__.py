"""命令行入口：`python -m raricy_bot`。

流程（INTERFACES §17）：解析 `--config PATH` → 加载配置 → `setup_logging`
→ 打开永久归档（如启用）→ 构造 `BotApp` → 安装 SIGINT/SIGTERM 处理 → `run_forever()`。

配置错误以退出码 2 结束，归档已启用却打不开以退出码 3 结束，两者都只在 stderr
打印一行原因；**绝不**打印任何密钥。

另有只读的归档子命令 `archive read` / `archive verify`：它们不连接站点、不启动
机器人，只在本地读分片。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

from .app import BotApp
from .config import Config, ConfigError, load_config
from .error_archive import ArchiveError, ArchiveHandler, ErrorArchive, iter_entries, verify_segments
from .logging_setup import (
    get_logger,
    install_archive,
    install_asyncio_exception_handler,
    install_exception_hooks,
    log_event,
    setup_logging,
    shutdown_logging,
)

_logger = get_logger("main")

# 退出码：0 正常，1 运行期致命错误，2 配置错误，3 归档已启用却打不开。
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_ARCHIVE = 3


def main(argv: list[str] | None = None) -> int:
    """进程入口；返回退出码，便于测试直接调用。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "archive":
        return _archive_main(arguments[1:])

    parser = argparse.ArgumentParser(
        prog="raricy_bot",
        description="Raricy 站内聊天机器人",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="配置文件路径；缺省时读 BOT_CONFIG_PATH，再退回到 ./config.yaml",
    )
    args = parser.parse_args(arguments)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # 只打印原因本身；ConfigError 的文案里从不含密钥取值。
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(config.log_level)
    install_exception_hooks()

    archive = _open_archive(config)
    if archive is _ARCHIVE_FAILED:
        shutdown_logging()
        return EXIT_ARCHIVE

    try:
        asyncio.run(_serve(config, archive))
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as exc:  # 运行期致命错误：只报类型，不泄露任何取值
        print(f"运行失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        _close_archive(archive)
        shutdown_logging()
    return EXIT_OK


# `_open_archive` 的失败哨兵：与「归档关闭」的 None 必须区分开。
_ARCHIVE_FAILED = object()


def _open_archive(config: Config) -> ErrorArchive | object | None:
    """按配置打开归档；关闭返回 None，打不开返回哨兵。

    「已启用但打不开」是**致命**的：继续跑只会让所有人以为永久记录已经在工作，
    而那正是本功能唯一要保证的事。所以这里安全报错并退出，不降级、不假装。
    """
    if not config.log_archive.enabled:
        return None
    settings = config.log_archive
    archive = ErrorArchive(
        settings.directory,
        segment_max_bytes=settings.segment_max_bytes,
        fsync_interval_seconds=settings.fsync_interval_seconds,
        disk_warning_free_bytes=settings.disk_warning_free_bytes,
    )
    try:
        archive.open()
    except ArchiveError as exc:
        print(f"归档启动失败：{exc}", file=sys.stderr)
        return _ARCHIVE_FAILED
    install_archive(ArchiveHandler(archive))
    log_event(
        _logger,
        logging.INFO,
        "archive.enabled",
        segment=0,
        limit_bytes=settings.segment_max_bytes,
    )
    return archive


def _close_archive(archive: ErrorArchive | object | None) -> None:
    """关闭归档并做最后一次同步；正常关闭时不留待同步数据。"""
    if not isinstance(archive, ErrorArchive):
        return
    archive.close()


async def _serve(config: Config, archive: ErrorArchive | object | None) -> None:
    """构造应用、安装信号处理并阻塞运行，直到收到停止信号。"""
    app = BotApp(config, archive=archive if isinstance(archive, ErrorArchive) else None)
    loop = asyncio.get_running_loop()
    install_asyncio_exception_handler(loop)

    def _request_stop(*_args: object) -> None:
        # 信号处理函数运行在主线程；用 call_soon_threadsafe 把停止请求交给事件循环。
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(app.stop()))

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _request_stop)
        except (ValueError, OSError):
            # 非主线程或平台不支持该信号时忽略，进程仍可由 KeyboardInterrupt 退出。
            pass

    await app.run_forever()


# --- 只读归档工具 -----------------------------------------------------------


def _archive_main(argv: list[str]) -> int:
    """`archive read` / `archive verify`：只读，不启动机器人、不连站点。"""
    parser = argparse.ArgumentParser(
        prog="raricy_bot archive",
        description="永久错误归档的只读查询与校验",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read", help="按条件列出归档事件")
    read.add_argument("--directory", required=True, help="归档目录")
    read.add_argument("--event", default=None, help="只保留该事件名")
    read.add_argument("--level", default=None, help="只保留该级别，如 WARNING")
    read.add_argument("--since", default=None, help="ISO-8601 前缀，如 2026-09-21T00:00:00")
    read.add_argument("--limit", type=int, default=None, help="最多输出多少条")

    verify = sub.add_parser("verify", help="巡检分片完整性，不修改任何文件")
    verify.add_argument("--directory", required=True, help="归档目录")

    args = parser.parse_args(argv)
    if args.command == "read":
        for entry in iter_entries(
            args.directory,
            event=args.event,
            level=args.level,
            since=args.since,
            limit=args.limit,
        ):
            print(json.dumps(entry, ensure_ascii=False, sort_keys=True))
        return EXIT_OK

    reports = verify_segments(args.directory)
    if not reports:
        print("没有找到任何分片")
        return EXIT_OK
    damaged = 0
    for report in reports:
        mark = "ok" if report.complete_tail and not report.corrupt_lines else "DAMAGED"
        if mark != "ok":
            damaged += 1
        print(
            f"{mark}\t{report.path}\tentries={report.entries}"
            f"\tcorrupt={report.corrupt_lines}\tcomplete_tail={report.complete_tail}"
            f"\tlast_ts={report.last_ts}\tlast_seq={report.last_seq}"
        )
    # 损坏分片以非零退出码结束，让备份脚本可以直接据它告警。
    return EXIT_OK if damaged == 0 else EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
