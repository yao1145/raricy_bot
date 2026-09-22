"""Windows 平台实现：Job Object 回收。

依据 LIGHT_EDITION_DESIGN §9.3：

- Job 设置 ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``：Controller 持有的最后一个
  job 句柄关闭（包括进程骤停由 OS 回收句柄）时，job 内进程被系统终止。
- ``CreateJobObject`` 不传安全属性，句柄默认**不可继承**：Worker 无法靠继承
  一个 job 句柄让自己在 Controller 死后存活。
- 调用方必须先创建挂起进程、``assign`` 成功后才放行主线程；assign 失败必须
  终止该进程而不是放行（「已运行但未纳管」的窗口由挂起创建消除）。
"""

from __future__ import annotations

import win32api
import win32job
import pywintypes

from . import PlatformError, WorkerJob


class _WinWorkerJob:
    def __init__(self, handle) -> None:
        self._handle = handle

    def assign(self, process_handle: int) -> None:
        try:
            win32job.AssignProcessToJobObject(self._handle, process_handle)
        except pywintypes.error as exc:
            raise PlatformError("job_assign_failed") from exc

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            win32api.CloseHandle(handle)
        except pywintypes.error:
            # 句柄已失效（如 OS 已回收）：关闭路径不抛出。
            pass

    def __enter__(self) -> WorkerJob:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class WindowsPlatform:
    """Windows 上的 LauncherPlatform 实现。"""

    def create_worker_job(self) -> WorkerJob:
        try:
            handle = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(
                handle, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                handle, win32job.JobObjectExtendedLimitInformation, limits
            )
        except pywintypes.error as exc:
            raise PlatformError("job_create_failed") from exc
        return _WinWorkerJob(handle)
