"""平台抽象边界：单实例、激活通道、Job 回收。

业务模块只依赖这里的协议与错误类型，不直接 import pywin32 或其他系统
绑定。首版只有 Windows 实现；其他平台拿到的是明确的不支持错误，而不是
静默退化的行为（LIGHT_EDITION_DESIGN §1.2、§9）。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
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
class InstanceGuard(Protocol):
    """桌面单实例互斥体（LIGHT_EDITION_DESIGN §9.4）。

    互斥体只决定 Controller 所有权；激活动作走受 ACL 保护的命名管道，
    不是互斥体本身。
    """

    def owned(self) -> bool:
        """本进程是否取得所有权；False 表示已有实例在运行。"""
        ...

    def close(self) -> None:
        """释放所有权并关闭句柄；重复关闭是安全的。"""
        ...


@runtime_checkable
class ActivationListener(Protocol):
    """激活管道服务端：接受有限激活动作的字节级请求/响应。"""

    def start(self, handler: Callable[[bytes], bytes]) -> None:
        """后台线程接受连接；handler 抛异常时回固定错误响应。"""
        ...

    def close(self) -> None:
        """停止接受并唤醒阻塞中的等待；重复关闭是安全的。"""
        ...


@runtime_checkable
class LauncherPlatform(Protocol):
    """桌面入口依赖的平台能力集合；随 L0 阶段逐项补全。"""

    def create_worker_job(self) -> WorkerJob:
        """创建空 job；句柄不可继承，保证只有 Controller 持有。"""
        ...

    def acquire_instance_guard(self) -> InstanceGuard:
        """取得当前用户范围的单实例互斥体。"""
        ...

    def create_activation_listener(self) -> ActivationListener:
        """创建 ACL 保护、拒绝远程客户端的激活管道服务端。"""
        ...

    def request_activation(self, payload: bytes, *, timeout_ms: int) -> bytes:
        """向已有实例的激活管道发一次请求；不可用抛 PlatformError。"""
        ...


def get_platform() -> LauncherPlatform:
    """返回当前平台的实现；不支持的平台抛 PlatformError。"""
    if sys.platform == "win32":
        from .windows import WindowsPlatform

        return WindowsPlatform()
    raise PlatformError("unsupported_platform")
