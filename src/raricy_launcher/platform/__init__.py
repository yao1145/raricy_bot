"""平台抽象边界：单实例、激活通道、Job 回收与父子管道。

业务模块只依赖这里的协议与错误类型，不直接 import pywin32 或其他系统
绑定。首版只有 Windows 实现；其他平台拿到的是明确的不支持错误，而不是
静默退化的行为（LIGHT_EDITION_DESIGN §1.2、§9）。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
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
class ControlPipe(Protocol):
    """父子控制通道：父端写入、子端只读；父端全部关闭即 EOF（§10.1、§9.3）。"""

    @property
    def child_handle(self) -> int:
        """子进程继承的只读端句柄值；经命令行参数传给 Worker。"""
        ...

    def detach_child_end(self) -> None:
        """子进程创建成功后关闭父进程持有的子端副本，保证 EOF 能传播。"""
        ...

    def send(self, data: bytes) -> None:
        """写控制指令；对端已消失时静默忽略（进程退出结果以 wait 为准）。"""
        ...

    def close(self) -> None:
        """关闭父端；重复关闭是安全的。关闭本身即构成 EOF 停止信号。"""
        ...


@runtime_checkable
class ReportPipe(Protocol):
    """Worker 上报通道：子端写入、父端只读；子端全部关闭即 EOF（§10.1）。

    与控制通道**分向**：日志与状态挤慢上报也不会挡住停止指令 —— 后者走
    另一个方向、另一根管道。
    """

    @property
    def child_handle(self) -> int:
        """子进程继承的只写端句柄值；经命令行参数传给 Worker。"""
        ...

    def detach_child_end(self) -> None:
        """子进程创建成功后关闭父进程持有的子端副本，保证 EOF 能传播。"""
        ...

    def receive(self, size: int) -> bytes:
        """读至多 `size` 字节；对端关闭返回空字节串（EOF）。"""
        ...

    def close(self) -> None:
        """关闭父端；重复关闭是安全的。"""
        ...


@runtime_checkable
class SuspendedProcess(Protocol):
    """以挂起主线程创建的子进程：assign Job 成功前不得 resume。"""

    @property
    def pid(self) -> int: ...

    @property
    def process_handle(self) -> int: ...

    def resume(self) -> None:
        """放行主线程；只应调用一次。"""
        ...

    def wait(self, timeout_ms: int) -> int | None:
        """等待退出；返回退出码，超时返回 None。"""
        ...

    def terminate(self) -> None:
        """强制终止（最后手段，不能代替优雅停止）。"""
        ...

    def close_handles(self) -> None:
        """关闭进程/线程句柄；重复关闭是安全的。"""
        ...


@runtime_checkable
class LauncherPlatform(Protocol):
    """桌面入口依赖的平台能力集合。"""

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

    def create_control_pipe(self) -> ControlPipe:
        """创建父子控制通道；子端可继承、父端不可继承。"""
        ...

    def create_report_pipe(self) -> ReportPipe:
        """创建 Worker 上报通道；子端可继承、父端不可继承。"""
        ...

    def spawn_suspended(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str],
        cwd: str,
        stdout_handle: int | None = None,
    ) -> SuspendedProcess:
        """以挂起主线程创建子进程；参数数组、明确 cwd、无控制台窗口。

        `stdout_handle` 非空时子进程的 stdout/stderr 都接到该句柄。
        """
        ...


def get_platform() -> LauncherPlatform:
    """返回当前平台的实现；不支持的平台抛 PlatformError。"""
    if sys.platform == "win32":
        from .windows import WindowsPlatform

        return WindowsPlatform()
    raise PlatformError("unsupported_platform")
