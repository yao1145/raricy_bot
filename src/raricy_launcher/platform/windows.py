"""Windows 平台实现：Job Object、单实例互斥体与激活管道。

依据 LIGHT_EDITION_DESIGN §9.3 / §9.4：

- Job 设置 ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``：Controller 持有的最后一个
  job 句柄关闭（包括进程骤停由 OS 回收句柄）时，job 内进程被系统终止。
- ``CreateJobObject`` 不传安全属性，句柄默认**不可继承**：Worker 无法靠继承
  一个 job 句柄让自己在 Controller 死后存活。
- 调用方必须先创建挂起进程、``assign`` 成功后才放行主线程；assign 失败必须
  终止该进程而不是放行（「已运行但未纳管」的窗口由挂起创建消除）。
- 互斥体按当前用户 SID 命名，只决定 Controller 所有权；激活动作走命名管道。
- 管道 DACL 显式拒绝 NETWORK 主体、只放行当前用户与 SYSTEM，且句柄不可继承；
  管道上只有固定激活动作，不存在运行命令或读文件的通用 RPC。
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Sequence

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32job
import win32pipe
import win32process
import win32security
import winerror

from .. import activation
from . import (
    ActivationListener,
    ControlPipe,
    InstanceGuard,
    LauncherPlatform,
    PlatformError,
    SuspendedProcess,
    WorkerJob,
)

# 拒绝远程客户端的 NETWORK 主体与本地 SYSTEM 主体的公认 SID。
_SID_NETWORK = "S-1-5-2"
_SID_SYSTEM = "S-1-5-18"


def _current_user_sid() -> str:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
    return win32security.ConvertSidToStringSid(sid)


def _self_only_security_attributes():
    """构造 DACL：先拒绝 NETWORK，再放行当前用户与 SYSTEM；句柄不可继承。"""
    dacl = win32security.ACL()
    dacl.AddAccessDeniedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_ALL,
        win32security.ConvertStringSidToSid(_SID_NETWORK),
    )
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_ALL,
        win32security.ConvertStringSidToSid(_current_user_sid()),
    )
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_ALL,
        win32security.ConvertStringSidToSid(_SID_SYSTEM),
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    attrs = win32security.SECURITY_ATTRIBUTES()
    attrs.SECURITY_DESCRIPTOR = descriptor
    attrs.bInheritHandle = False
    return attrs


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


class _WinInstanceGuard:
    def __init__(self, handle, owned: bool) -> None:
        self._handle = handle
        self._owned = owned

    def owned(self) -> bool:
        return self._owned

    def close(self) -> None:
        handle, owned = self._handle, self._owned
        self._handle, self._owned = None, False
        if handle is None:
            return
        try:
            if owned:
                win32event.ReleaseMutex(handle)
            win32api.CloseHandle(handle)
        except pywintypes.error:
            pass


# 已连接客户端的请求等待上限：超过即丢弃该连接，沉默客户端不能占住服务线程。
_SERVE_DEADLINE_MS = 10_000

# 响应写出后等待客户端读走的窗口。激活客户端自身的等待上限是 2 秒
# （main._activate_existing 的 timeout_ms），交付窗口超过它只会让后续激活排队
# 等待一份已无人读取的响应；窗口内没被读走就丢弃该连接。
_RESPONSE_DELIVERY_MS = 2_000


class _WinActivationListener:
    """串行接受的激活管道服务端；一次只服务一个连接，队列由系统缓冲。

    accept、读取与响应交付都走 overlapped I/O：close() 置位取消事件即可让
    服务线程立刻退出，不依赖再建立一个唤醒连接（沉默客户端会把那种唤醒
    一起卡住）。
    """

    def __init__(self, pipe_name: str, attrs) -> None:
        self._pipe_name = pipe_name
        self._attrs = attrs
        self._stop = threading.Event()
        self._cancel = win32event.CreateEvent(None, True, False, None)
        self._thread: threading.Thread | None = None
        self._pending = None
        # accept 级故障的固定类别，供 Controller 在 close 后诊断。
        self.last_error: str | None = None

    def start(self, handler: Callable[[bytes], bytes]) -> None:
        if self._thread is not None:
            raise PlatformError("listener_started")
        # 首个管道实例同步创建：start 返回后名字即存在，客户端无需等待。
        self._pending = self._create_pipe()
        self._thread = threading.Thread(
            target=self._loop, args=(handler,), name="raricy-activate", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        # 直接取消在途的 accept/read 等待。
        cancel = self._cancel
        if cancel is not None:
            win32event.SetEvent(cancel)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                # 兜底：取消后仍未退出（如处理器自身阻塞）；句柄留给 OS 随进程回收。
                return
        if cancel is not None:
            self._cancel = None
            try:
                win32api.CloseHandle(cancel)
            except pywintypes.error:
                pass

    def _create_pipe(self):
        try:
            return win32pipe.CreateNamedPipe(
                self._pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED,
                win32pipe.PIPE_TYPE_MESSAGE
                | win32pipe.PIPE_READMODE_MESSAGE
                | win32pipe.PIPE_WAIT,
                4,  # 最大实例数：并发双击由队列消化，串行处理
                activation.MAX_MESSAGE_BYTES,
                activation.MAX_MESSAGE_BYTES,
                0,
                self._attrs,
            )
        except pywintypes.error as exc:
            raise PlatformError("pipe_create_failed") from exc

    def _await_io(self, pipe, overlapped, timeout_ms: int) -> bool:
        """等待 overlapped I/O 完成；取消事件或截止到达时取消操作并返回 False。"""
        result = win32event.WaitForMultipleObjects(
            (overlapped.hEvent, self._cancel), False, timeout_ms
        )
        if result == win32event.WAIT_OBJECT_0:
            return True
        # 取消在途操作并等它落地，之后由调用方丢弃该连接。本函数总在发起
        # I/O 的服务线程内调用，无 CancelIoEx 时 CancelIo 也只管本线程的操作。
        try:
            cancel = getattr(win32file, "CancelIoEx", None)
            if cancel is not None:
                cancel(pipe, overlapped)
            else:
                win32file.CancelIo(pipe)
            win32file.GetOverlappedResult(pipe, overlapped, True)
        except pywintypes.error:
            pass
        return False

    def _connect(self, pipe) -> bool:
        """接受一个连接（overlapped）；stop/取消或接受失败返回 False。"""
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            try:
                hr = win32pipe.ConnectNamedPipe(pipe, overlapped)
            except pywintypes.error as exc:
                hr = exc.winerror
            if hr is None or hr == winerror.ERROR_PIPE_CONNECTED:
                # 客户端在 Connect 之前已连上：也是成功。
                hr = 0
            if hr == winerror.ERROR_IO_PENDING:
                if not self._await_io(pipe, overlapped, win32event.INFINITE):
                    return False
                try:
                    win32file.GetOverlappedResult(pipe, overlapped, False)
                except pywintypes.error:
                    return False
            elif hr != 0:
                return False
            return not self._stop.is_set()
        finally:
            win32api.CloseHandle(overlapped.hEvent)

    def _read_request(self, pipe) -> bytes | None:
        """读取一条请求；沉默超过 _SERVE_DEADLINE_MS、取消或失败返回 None。"""
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            try:
                hr, data = win32file.ReadFile(pipe, activation.MAX_MESSAGE_BYTES, overlapped)
            except pywintypes.error:
                return None
            if hr == winerror.ERROR_IO_PENDING:
                if not self._await_io(pipe, overlapped, _SERVE_DEADLINE_MS):
                    return None
            elif hr != 0:
                return None
            # 同步完成与异步完成都统一取真实字节数：overlapped 句柄的
            # ReadFile 同步返回时不会截断缓冲。
            try:
                size = win32file.GetOverlappedResult(pipe, overlapped, False)
            except pywintypes.error:
                return None
            return bytes(data)[:size]
        finally:
            win32api.CloseHandle(overlapped.hEvent)

    def _write_response(self, pipe, response: bytes) -> bool:
        """overlapped 写回响应；取消或失败返回 False。"""
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            try:
                hr = win32file.WriteFile(pipe, response, overlapped)[0]
            except pywintypes.error as exc:
                hr = exc.winerror
            if hr is None:
                hr = 0
            if hr == winerror.ERROR_IO_PENDING:
                if not self._await_io(pipe, overlapped, _SERVE_DEADLINE_MS):
                    return False
                try:
                    win32file.GetOverlappedResult(pipe, overlapped, False)
                except pywintypes.error:
                    return False
                return True
            return hr == 0
        finally:
            win32api.CloseHandle(overlapped.hEvent)

    def _await_response_read(self, pipe) -> None:
        """等待客户端读走响应；等价于**有界、可取消**的 ``FlushFileBuffers``。

        命名管道的 ``FlushFileBuffers`` 是同步调用，不受取消事件或截止时间控制：
        客户端发来请求却不再读取响应时，它会永久阻塞服务线程，之后的激活请求
        全部排队。这里改用 overlapped 读等待客户端关闭连接 —— 正常客户端读完
        响应即关闭句柄，读立刻以 ``ERROR_BROKEN_PIPE`` 结束；一直不读的客户端由
        ``_RESPONSE_DELIVERY_MS`` 与取消事件界定，之后丢弃该连接（客户端本来
        也没在等这份响应）。同步完成（客户端已关闭）同样结束本次连接。
        """
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            try:
                hr = win32file.ReadFile(pipe, 1, overlapped)[0]
            except pywintypes.error as exc:
                hr = exc.winerror
            if hr is None:
                hr = 0
            if hr == winerror.ERROR_IO_PENDING:
                self._await_io(pipe, overlapped, _RESPONSE_DELIVERY_MS)
        finally:
            win32api.CloseHandle(overlapped.hEvent)

    def _loop(self, handler: Callable[[bytes], bytes]) -> None:
        pipe = self._pending
        self._pending = None
        while pipe is not None and not self._stop.is_set():
            if not self._connect(pipe):
                self._close_pipe(pipe)
                return
            # 先备好下一个实例再服务当前连接：实例数不降到 0，紧跟的下一个
            # 客户端才能用 WaitNamedPipe 等到可用实例。
            try:
                next_pipe = self._create_pipe()
            except PlatformError:
                self.last_error = "pipe_accept_failed"
                next_pipe = None
            self._serve_one(pipe, handler)
            self._close_pipe(pipe)
            pipe = next_pipe
        if pipe is not None:
            # 退出时释放尚未接受连接的待服务实例。
            self._close_pipe(pipe)

    @staticmethod
    def _close_pipe(pipe) -> None:
        try:
            pipe.Close()
        except pywintypes.error:
            pass

    def _serve_one(self, pipe, handler: Callable[[bytes], bytes]) -> None:
        try:
            data = self._read_request(pipe)
            if data is None:
                return
            try:
                response = handler(data)
            except Exception:
                response = activation.encode_response(ok=False, error="internal_error")
            if not self._write_response(pipe, response):
                return
            self._await_response_read(pipe)
        except pywintypes.error:
            # 客户端在读写间隙消失：丢弃这次连接，不影响后续接受。
            pass
        finally:
            try:
                win32pipe.DisconnectNamedPipe(pipe)
            except pywintypes.error:
                pass


class _WinControlPipe:
    """匿名管道控制通道：子端只读可继承，父端写入不可继承。"""

    def __init__(self, read_handle, write_handle) -> None:
        self._read_handle = read_handle
        self._write_handle = write_handle
        self._child_handle = int(read_handle)

    @property
    def child_handle(self) -> int:
        return self._child_handle

    def detach_child_end(self) -> None:
        handle, self._read_handle = self._read_handle, None
        if handle is not None:
            try:
                win32api.CloseHandle(handle)
            except pywintypes.error:
                pass

    def send(self, data: bytes) -> None:
        if self._write_handle is None:
            return
        try:
            win32file.WriteFile(self._write_handle, data)
        except pywintypes.error:
            # Worker 已退出导致管道断裂：退出结果以 wait 为准，这里不抛出。
            pass

    def close(self) -> None:
        self.detach_child_end()
        handle, self._write_handle = self._write_handle, None
        if handle is not None:
            try:
                win32api.CloseHandle(handle)
            except pywintypes.error:
                pass


class _WinSuspendedProcess:
    def __init__(self, process_handle, thread_handle, pid: int) -> None:
        self._process_handle = process_handle
        self._thread_handle = thread_handle
        self._pid = pid

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def process_handle(self) -> int:
        return int(self._process_handle)

    def resume(self) -> None:
        try:
            win32process.ResumeThread(self._thread_handle)
        except pywintypes.error as exc:
            raise PlatformError("process_resume_failed") from exc

    def wait(self, timeout_ms: int) -> int | None:
        result = win32event.WaitForSingleObject(self._process_handle, timeout_ms)
        if result == win32event.WAIT_TIMEOUT:
            return None
        return win32process.GetExitCodeProcess(self._process_handle)

    def terminate(self) -> None:
        try:
            win32process.TerminateProcess(self._process_handle, 1)
        except pywintypes.error:
            # 进程已退出：终止是幂等意图，不抛出。
            pass

    def close_handles(self) -> None:
        for attr in ("_process_handle", "_thread_handle"):
            handle = getattr(self, attr)
            setattr(self, attr, None)
            if handle is not None:
                try:
                    win32api.CloseHandle(handle)
                except pywintypes.error:
                    pass


class WindowsPlatform:
    """Windows 上的 LauncherPlatform 实现。"""

    def __init__(self) -> None:
        self._user_sid = _current_user_sid()
        self._pipe_name = rf"\\.\pipe\RaricyBotLight.Activate.{self._user_sid}"
        self._mutex_name = rf"Local\RaricyBotLight.Controller.{self._user_sid}"

    @property
    def pipe_name(self) -> str:
        """激活管道名；仅用于诊断与测试校验 ACL。"""
        return self._pipe_name

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

    def acquire_instance_guard(self) -> InstanceGuard:
        try:
            handle = win32event.CreateMutex(None, True, self._mutex_name)
            owned = win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS
        except pywintypes.error as exc:
            raise PlatformError("mutex_failed") from exc
        return _WinInstanceGuard(handle, owned)

    def create_activation_listener(self) -> ActivationListener:
        return _WinActivationListener(self._pipe_name, _self_only_security_attributes())

    def request_activation(self, payload: bytes, *, timeout_ms: int) -> bytes:
        if len(payload) > activation.MAX_REQUEST_BYTES:
            raise PlatformError("request_too_large")
        try:
            win32pipe.WaitNamedPipe(self._pipe_name, timeout_ms)
            data = win32pipe.CallNamedPipe(
                self._pipe_name, payload, activation.MAX_MESSAGE_BYTES, timeout_ms
            )
        except pywintypes.error as exc:
            raise PlatformError("activation_unavailable") from exc
        return bytes(data)

    def create_control_pipe(self) -> ControlPipe:
        try:
            attrs = win32security.SECURITY_ATTRIBUTES()
            attrs.bInheritHandle = True
            read_handle, write_handle = win32pipe.CreatePipe(attrs, 0)
            # 父端不可继承：子进程只拿到只读端；父端关闭即 EOF。
            win32api.SetHandleInformation(write_handle, win32con.HANDLE_FLAG_INHERIT, 0)
        except pywintypes.error as exc:
            raise PlatformError("control_pipe_failed") from exc
        return _WinControlPipe(read_handle, write_handle)

    def spawn_suspended(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str],
        cwd: str,
    ) -> SuspendedProcess:
        # bInheritHandles=True 只继承显式标记为可继承的句柄（PEP 446 下
        # Python 自己的文件/套接字默认不可继承），控制管道子端由此进入
        # 子进程并保持同一柄值。CREATE_NO_WINDOW 保证无终端窗口。
        try:
            startup = win32process.STARTUPINFO()
            process_handle, thread_handle, pid, _tid = win32process.CreateProcess(
                None,
                subprocess.list2cmdline(list(argv)),
                None,
                None,
                True,
                win32process.CREATE_SUSPENDED | win32process.CREATE_NO_WINDOW,
                env,
                cwd,
                startup,
            )
        except pywintypes.error as exc:
            raise PlatformError("process_create_failed") from exc
        return _WinSuspendedProcess(process_handle, thread_handle, pid)

