"""命令行入口：`python -m raricy_bot`。

流程（INTERFACES §17）：解析 `--config PATH` → 加载配置 → `setup_logging`
→ 构造 `BotApp` → 安装 SIGINT/SIGTERM 处理 → `run_forever()`。

配置错误以退出码 2 结束，只在 stderr 打印一行原因；**绝不**打印任何密钥。
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from .app import BotApp
from .config import Config, ConfigError, load_config
from .logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    """进程入口；返回退出码，便于测试直接调用。"""
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
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # 只打印原因本身；ConfigError 的文案里从不含密钥取值。
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_level)

    try:
        asyncio.run(_serve(config))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # 运行期致命错误：只报类型，不泄露任何取值
        print(f"运行失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


async def _serve(config: Config) -> None:
    """构造应用、安装信号处理并阻塞运行，直到收到停止信号。"""
    app = BotApp(config)
    loop = asyncio.get_running_loop()

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


if __name__ == "__main__":
    sys.exit(main())
