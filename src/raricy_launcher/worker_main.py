"""Light Worker 执行入口（L0：受管假 Worker，不连站点、不加载核心）。

唯一职责是验证平台层的进程纳管：从继承的控制管道只读端读指令——

- 收到 ``stop\\n``：退出码 0 正常退出；
- 父端关闭（EOF）或管道断裂（父进程骤停）：同样退出，不成为孤儿；
- 管道流入无法识别的垃圾超过上限：退出码 2（协议违例）。

只依赖标准库，不 import pywin32 或核心模块：它是被 Job 回收的最小进程。
"""

from __future__ import annotations

import argparse
import os
import sys

# 控制报文缓冲上限：控制指令只有几个字，超出即视为对端在说另一种协议。
_MAX_PENDING_BYTES = 4096
_READ_CHUNK = 1024

EXIT_OK = 0
EXIT_PROTOCOL = 2


def _read_control_loop(fd: int) -> int:
    pending = b""
    while True:
        try:
            chunk = os.read(fd, _READ_CHUNK)
        except OSError:
            return EXIT_OK  # 管道断裂：父进程已死，跟随退出
        if not chunk:
            return EXIT_OK  # EOF：父端正常关闭控制通道
        pending += chunk
        if b"stop\n" in pending:
            return EXIT_OK
        if len(pending) > _MAX_PENDING_BYTES:
            return EXIT_PROTOCOL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="raricy_launcher.worker_main")
    parser.add_argument(
        "--control-handle",
        type=int,
        required=True,
        help="从父进程继承的控制管道只读端句柄值",
    )
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        import msvcrt

        fd = msvcrt.open_osfhandle(args.control_handle, os.O_RDONLY)
    else:  # 非 Windows 开发路径：句柄值即文件描述符
        fd = args.control_handle
    try:
        return _read_control_loop(fd)
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
