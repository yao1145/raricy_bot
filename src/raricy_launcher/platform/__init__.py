"""平台抽象边界：单实例、激活通道、Job 回收。

业务模块只依赖这里的协议与错误类型，不直接 import pywin32 或其他系统
绑定。首版只有 Windows 实现；其他平台拿到的是明确的不支持错误，而不是
静默退化的行为（LIGHT_EDITION_DESIGN §1.2、§9）。
"""

from __future__ import annotations

import sys
from typing import Protocol, runtime_checkable


class PlatformError(Exception):
    """平台能力不可用的固定错误；消息是稳定类别，不拼接系统原始错误文本。"""


@runtime_checkable
class WorkerJob(Protocol):
    """KILL_ON_JOB_CLOSE 语义的进程回收载体。

    Controller 持有唯一必要的 job 句柄；句柄全部关闭（含进程崩溃由 OS
    回收）时，job 内所有进程被系统终止。正常退出仍先走 Core 停止流程，
    Job 只是崩溃兜底（LIGHT_EDITION_DESIGN §9.3）。
    """

    def assign(self, process_handle: int) -> None:
        """把进程纳入 job；失败抛 PlatformError，调用方不得放行该进程。"""
        ...

    def close(self) -> None:
        """关闭本侧句柄；重复关闭是安全的。"""
        ...


@runtime_checkable
class LauncherPlatform(Protocol):
    """桌面入口依赖的平台能力集合；随 L0 阶段逐项补全。"""

    def create_worker_job(self) -> WorkerJob:
        """创建空 job；句柄不可继承，保证只有 Controller 持有。"""
        ...


def get_platform() -> LauncherPlatform:
    """返回当前平台的实现；不支持的平台抛 PlatformError。"""
    if sys.platform == "win32":
        from .windows import WindowsPlatform

        return WindowsPlatform()
    raise PlatformError("unsupported_platform")
