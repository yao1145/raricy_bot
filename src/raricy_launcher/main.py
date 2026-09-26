"""桌面入口：启动或激活已有实例（LIGHT_EDITION_DESIGN §5.2、§9.4）。

分派顺序：``--worker`` → Worker 执行入口；否则先抢单实例互斥体——

- 未取得所有权：默认经激活管道请已有实例给出管理页 URL，打开浏览器后退出，
  不跟随主实例常驻；入口带 ``--startup``（登录自启动）时改走静默去重，只记
  一条事件就退出，不请求激活、不弹页面（INTERFACES §59）；
- 取得所有权：成为 Controller。L0 原型尚无配置概念，启动后打开一次
  管理页；静默启动与凭据检查在 L2/L3 接入。

``--startup`` 只是来源提示，不是权限边界：互斥体、生命周期门与授权偏好的
判定都不因它而放宽。``--no-tray`` 只关掉托盘装配（诊断与无通知区域环境用），
其余行为与权限判定不变，管理页仍是入口。
"""

from __future__ import annotations

import logging
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

from raricy_bot.logging_setup import log_event

from . import activation, diagnostics, texts, worker_main
from .activation import ActivationError
from .controller import Controller
from .paths import default_data_root
from .platform import LauncherPlatform, PlatformError, get_platform

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_UNSUPPORTED = 2

# 登录自启动入口的固定参数（INTERFACES §59）：只是来源提示，不携带任何凭据或目标。
STARTUP_FLAG = "--startup"

# 显式关闭托盘（INTERFACES §59、§61）：不建托盘，控制面与管理页照常可用。
NO_TRAY_FLAG = "--no-tray"


@dataclass(frozen=True)
class EntryArgs:
    """一次入口调用的解析结果。

    `kind` 取 `worker` / `controller`；`startup` 只表示「由登录启动拉起」这一
    来源事实；`no_tray` 表示显式要求不建托盘（`--no-tray`，无托盘仍可用管理页）。
    `worker_argv` 仅在 `kind == "worker"` 时有内容，是 `--worker` 之后的原样参数。
    """

    kind: str
    startup: bool
    no_tray: bool = False
    worker_argv: tuple[str, ...] = ()


def parse_entry_args(argv: list[str]) -> EntryArgs:
    """纯函数：只看字面量，不读环境、不打印。

    首个参数为 `--worker` 时优先按 Worker 分派，其余参数原样透传（保持现状）；
    否则按 Controller 分派，`--startup` 与 `--no-tray` 可出现在任意位置，其余
    未知参数照旧忽略 —— 不为它们新增失败模式。
    """
    if argv and argv[0] == "--worker":
        return EntryArgs(kind="worker", startup=False, worker_argv=tuple(argv[1:]))
    return EntryArgs(
        kind="controller",
        startup=STARTUP_FLAG in argv,
        no_tray=NO_TRAY_FLAG in argv,
    )


def _console_line(text: str) -> None:
    """无终端发行包不假设 stdout/stderr 存在；都不在就静默。"""
    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is not None:
        print(text, file=stream)


def _activate_existing(
    logger: logging.Logger,
    platform: LauncherPlatform,
    open_url: Callable[[str], None],
) -> int:
    """已有实例：请它给出管理页地址，再交给浏览器打开。"""
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
    open_url(response["url"])
    _console_line(texts.ENTRY_ACTIVATED)
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    platform: LauncherPlatform | None = None,
    open_url: Callable[[str], None] | None = None,
) -> int:
    """入口分派。

    `platform` 与 `open_url` 只服务测试注入，缺省回落真实实现；真实运行不传它们。
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    entry = parse_entry_args(arguments)
    if entry.kind == "worker":
        return worker_main.main(list(entry.worker_argv))

    data_root = default_data_root()
    logger = diagnostics.install(data_root)
    if platform is None:
        try:
            platform = get_platform()
        except PlatformError:
            _console_line(texts.ENTRY_UNSUPPORTED_PLATFORM)
            return EXIT_UNSUPPORTED
    # 调用期才取 webbrowser.open：无浏览器环境的替代与测试注入都才有意义。
    activate_open = open_url if open_url is not None else webbrowser.open

    guard = platform.acquire_instance_guard()
    if not guard.owned():
        guard.close()
        if entry.startup:
            # 登录自启动撞上已有实例：静默去重，不沿用请求激活/弹页面那条分支。
            log_event(logger, logging.INFO, "launcher.startup_deduped", status="ok")
            return EXIT_OK
        return _activate_existing(logger, platform, activate_open)

    try:
        controller = Controller(
            platform=platform,
            guard=guard,
            data_root=data_root,
            logger=logger,
            open_url=open_url,
            startup_launch=entry.startup,
            # `--no-tray` 只影响托盘装配；控制面、激活与退出路径都不变（§61）。
            use_tray=not entry.no_tray,
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
