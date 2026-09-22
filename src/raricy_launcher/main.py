"""桌面入口：启动或激活已有实例（LIGHT_EDITION_DESIGN §5.2、§9.4）。

分派顺序：``--worker`` → Worker 执行入口；否则先抢单实例互斥体——

- 未取得所有权：经激活管道请已有实例给出管理页 URL，打开浏览器后退出，
  不跟随主实例常驻；
- 取得所有权：成为 Controller。L0 原型尚无配置概念，启动后打开一次
  管理页；静默启动与凭据检查在 L2/L3 接入。
"""

from __future__ import annotations

import logging
import sys
import webbrowser

from raricy_bot.logging_setup import log_event

from . import activation, diagnostics, texts, worker_main
from .activation import ActivationError
from .controller import Controller
from .paths import default_data_root
from .platform import PlatformError, get_platform

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_UNSUPPORTED = 2


def _console_line(text: str) -> None:
    """无终端发行包不假设 stdout/stderr 存在；都不在就静默。"""
    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is not None:
        print(text, file=stream)


def _activate_existing(logger: logging.Logger) -> int:
    platform = get_platform()
    try:
        data = platform.request_activation(
            activation.encode_request("open_admin"), timeout_ms=2000
        )
        response = activation.decode_response(data)
    except (PlatformError, ActivationError) as exc:
        log_event(
            logger,
            logging.WARNING,
            "launcher.activate",
            status="failed",
            error=type(exc).__name__,
        )
        _console_line(texts.ENTRY_ACTIVATE_FAILED)
        return EXIT_RUNTIME
    if not response["ok"]:
        log_event(logger, logging.WARNING, "launcher.activate", status="rejected")
        _console_line(texts.ENTRY_ACTIVATE_FAILED)
        return EXIT_RUNTIME
    # 调用期才取 webbrowser.open：无浏览器环境的替代与测试注入都才有意义。
    webbrowser.open(response["url"])
    _console_line(texts.ENTRY_ACTIVATED)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--worker":
        return worker_main.main(arguments[1:])

    data_root = default_data_root()
    logger = diagnostics.install(data_root)
    try:
        platform = get_platform()
    except PlatformError:
        _console_line(texts.ENTRY_UNSUPPORTED_PLATFORM)
        return EXIT_UNSUPPORTED

    guard = platform.acquire_instance_guard()
    if not guard.owned():
        guard.close()
        return _activate_existing(logger)

    try:
        controller = Controller(
            platform=platform,
            guard=guard,
            data_root=data_root,
            logger=logger,
        )
        try:
            controller.run()
        except KeyboardInterrupt:
            controller.stop()
        return EXIT_OK
    except Exception as exc:
        log_event(
            logger,
            logging.CRITICAL,
            "launcher.start",
            status="failed",
            error=type(exc).__name__,
        )
        _console_line(texts.ENTRY_INTERNAL_ERROR)
        guard.close()
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
