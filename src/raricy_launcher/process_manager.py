"""受管 Worker 子进程：挂起创建 → Job 纳管 → 放行，以及双向 IPC 会话。

编排顺序钉死「已运行但未纳管」的窗口（LIGHT_EDITION_DESIGN §9.3）：

1. 创建控制管道、上报管道、排水管道与 Job；
2. 以挂起主线程创建子进程（参数数组、明确 cwd、``shell=False`` 等价语义）；
3. ``assign`` 成功才 ``resume``；assign 失败先终止再放行句柄，绝不放行
   一个不受 Job 约束的进程；
4. 优雅停止走控制通道的 ``stop`` 帧或父端 EOF；``terminate`` 只是停止
   超时后的最后手段，调用即标记 ``forced_stop``。

**凭据与实例身份走子进程环境**（§7）：账号、密码与模型 Key 由 Controller 注入，
MCP 凭据不在其中 —— Light 没有 MCP。原始 stdout/stderr 接到一根只读排水管道，
父进程持续排空并只保留字节计数（§12）。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from raricy_bot.config import LLM_API_KEY_ENV, PASSWORD_ENV, USERNAME_ENV, Secrets

from . import ipc
from .platform import LauncherPlatform, ReportPipe, SuspendedProcess, WorkerJob

# 子进程环境里的实例身份：Worker 用它回填 IPC 信封（§10.1）。
INSTANCE_ID_ENV: str = "RARICY_LIGHT_INSTANCE_ID"
RUN_ID_ENV: str = "RARICY_LIGHT_RUN_ID"

# 子进程环境的系统变量白名单（Windows）；凭据与 PYTHONPATH 走 extra。
_SYSTEM_ENV_KEYS: tuple[str, ...] = (
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "COMSPEC",
)

# 上报读取的单次块大小：帧头只有 4 字节，读得比它大不改变语义。
_REPORT_CHUNK_BYTES: int = 8 * 1024

# 排水管道的单次读取上限：只统计字节数，内容一律丢弃。
_DRAIN_CHUNK_BYTES: int = 4 * 1024


def _enqueue_report_frame(frames: queue.Queue, frame: dict) -> bool:
    """非阻塞保留最近状态；队列满时只淘汰可替代的帧。"""
    kind = frame.get("kind")
    with frames.mutex:
        items = frames.queue
        if len(items) < frames.maxsize:
            frames._put(frame)
            frames.unfinished_tasks += 1
            frames.not_empty.notify()
            return True

        # 状态快照只需要最新一份；事件日志可丢弃。ready/stopped 是生命周期
        # 帧，除替换同类重复帧外优先保留。
        if kind == "status":
            candidates = ("status", "log")
        elif kind == "log":
            candidates = ("log",)
        elif kind in {"ready", "stopped"}:
            candidates = (kind, "log", "status")
        else:
            candidates = ("log", "status")

        evict_index = None
        for candidate in candidates:
            for index, existing in enumerate(items):
                if isinstance(existing, dict) and existing.get("kind") == candidate:
                    evict_index = index
                    break
            if evict_index is not None:
                break
        if evict_index is None:
            return False

        del items[evict_index]
        frames.unfinished_tasks -= 1
        if frames.unfinished_tasks == 0:
            frames.all_tasks_done.notify_all()
        frames._put(frame)
        frames.unfinished_tasks += 1
        frames.not_empty.notify()
        return True


def build_worker_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """系统必需变量白名单 + 显式额外项；不透传完整宿主环境（§7）。"""
    env = {key: os.environ[key] for key in _SYSTEM_ENV_KEYS if key in os.environ}
    env.update(extra or {})
    return env


def worker_env(*, credentials: Secrets, instance_id: str, run_id: str) -> dict[str, str]:
    """注入子进程的凭据与身份变量：Core 仍只经这些环境变量取凭据（§7）。"""
    return {
        USERNAME_ENV: credentials.username,
        PASSWORD_ENV: credentials.password,
        LLM_API_KEY_ENV: credentials.llm_api_key,
        INSTANCE_ID_ENV: instance_id,
        RUN_ID_ENV: run_id,
    }


@dataclass(frozen=True)
class WorkerSpec:
    """一次 Worker 启动的固定输入：参数数组、环境、工作目录与运行快照。"""

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str
    run_config: str
    config_dir: str


def default_worker_spec(
    *,
    run_config: str,
    config_dir: str,
    credentials: Secrets,
    instance_id: str,
    run_id: str,
) -> WorkerSpec:
    """当前运行形态下的 Worker 启动规格。

    冻结发行用同一程序的 ``--worker`` 入口（首版选择，§10.1）；开发模式
    用独立模块入口。两者都是参数数组、明确 cwd，不拼接 shell 命令。
    """
    extra = worker_env(credentials=credentials, instance_id=instance_id, run_id=run_id)
    if getattr(sys, "frozen", False):
        return WorkerSpec(
            argv=(sys.executable, "--worker"),
            env=build_worker_env(extra),
            cwd=str(Path(sys.executable).resolve().parent),
            run_config=run_config,
            config_dir=config_dir,
        )
    src_root = Path(__file__).resolve().parents[1]
    return WorkerSpec(
        argv=(sys.executable, "-m", "raricy_launcher.worker_main"),
        env=build_worker_env({"PYTHONPATH": str(src_root), **extra}),
        cwd=str(src_root.parent),
        run_config=run_config,
        config_dir=config_dir,
    )


class WorkerError(Exception):
    """Worker 创建/纳管/会话失败的固定错误；消息是稳定类别码。"""


class WorkerProcess:
    """一个受 Job 约束、经 IPC 交互的 Worker 子进程。

    上报由独立线程读取并放进有界队列；控制命令由调用方在需要的线程里发。
    上报管道 EOF 只表示「对方不再说话」，进程是否退出仍以 ``wait()`` 为准。
    """

    def __init__(
        self, platform: LauncherPlatform, spec: WorkerSpec, *, instance_id: str, run_id: str
    ) -> None:
        self._platform = platform
        self._instance_id = instance_id
        self._run_id = run_id
        # 所有句柄属性**先**初始化再创建：创建失败时 close() 会读它们，
        # 否则清理路径自己抛 AttributeError，句柄照样泄漏、原因也被盖掉（复审 HIGH）。
        self._process: SuspendedProcess | None = None
        self._job: WorkerJob | None = None
        self._control = None
        self._report: ReportPipe | None = None
        self._drain: ReportPipe | None = None
        self._reader: threading.Thread | None = None
        self._drainer: threading.Thread | None = None
        try:
            # Job 与管道都在 try 内：任何一步失败都要走 close() 回收已建的句柄。
            self._job = platform.create_worker_job()
            self._control = platform.create_control_pipe()
            self._report = platform.create_report_pipe()
            self._drain = platform.create_report_pipe()
        except Exception:
            self.close()
            raise
        self._forced_stop = False
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._frames: queue.Queue[dict | None] = queue.Queue(maxsize=256)
        self.report_closed = threading.Event()
        self.drained_bytes = 0
        argv = [
            *spec.argv,
            "--run-config",
            spec.run_config,
            "--config-dir",
            spec.config_dir,
            "--control-handle",
            str(self._control.child_handle),
            "--report-handle",
            str(self._report.child_handle),
        ]
        try:
            self._process = platform.spawn_suspended(
                argv,
                env=spec.env,
                cwd=spec.cwd,
                stdout_handle=self._drain.child_handle,
            )
            try:
                self._job.assign(self._process.process_handle)
            except Exception as exc:
                # 纳管失败：先终止再清理，绝不放行不受 Job 约束的进程。
                self._process.terminate()
                raise WorkerError("job_assign_failed") from exc
            self._process.resume()
            self._control.detach_child_end()
            self._report.detach_child_end()
            self._drain.detach_child_end()
        except Exception:
            self.close()
            raise
        self._reader = threading.Thread(
            target=self._read_reports, name="raricy-worker-reports", daemon=True
        )
        self._reader.start()
        self._drainer = threading.Thread(
            target=self._drain_output, name="raricy-worker-drain", daemon=True
        )
        self._drainer.start()

    @property
    def pid(self) -> int:
        return self._process.pid if self._process is not None else -1

    @property
    def forced_stop(self) -> bool:
        return self._forced_stop

    @property
    def closed(self) -> bool:
        """是否已被回收（`close()` 之后）；监视线程据此结束等待。"""
        return self._process is None

    @property
    def job(self) -> WorkerJob | None:
        """纳管本进程的 Job；Controller 持有，崩溃模拟测试据此直接关闭。"""
        return self._job

    # --- 会话 -------------------------------------------------------------

    def send_command(self, kind: str, payload: dict | None = None) -> int:
        """发一条控制命令，返回本会话内递增的序号（§10.1）。

        已关闭的 Worker 上再发指令是空操作：停止流程、监视线程与用户操作会交叉，
        重复的「请停」不该变成异常。
        """
        if self._control is None:
            return 0
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        frame = ipc.encode_frame(
            kind,
            instance_id=self._instance_id,
            run_id=self._run_id,
            seq=seq,
            payload=payload,
        )
        self._control.send(frame)
        return seq

    def next_frame(self, timeout: float) -> dict | None:
        """取一条上报帧；超时返回 None。EOF 后队列会立刻排空并由 `report_closed` 标记。"""
        if self._report is None:
            return None
        try:
            frame = self._frames.get(timeout=timeout)
        except queue.Empty:
            return None
        if frame is None:
            self.report_closed.set()
            return None
        return frame

    def wait_ready(self, timeout: float) -> bool:
        """等到 Worker 报出 ready；超时或先关闭返回 False。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            frame = self.next_frame(remaining)
            if frame is None:
                if self.report_closed.is_set():
                    return False
                continue
            if frame["kind"] == "ready":
                self._ready_payload = frame.get("payload", {})
                return True

    @property
    def ready_payload(self) -> dict:
        """最近一次 ready 帧的载荷（只读）。"""
        return getattr(self, "_ready_payload", {})

    def request_stop(self) -> None:
        """经控制通道请求优雅停止；幂等，进程已退出或已关闭时不报错。"""
        self.send_command("stop")

    # --- 进程 -------------------------------------------------------------

    def wait(self, timeout_ms: int) -> int | None:
        """等待退出；返回退出码，超时或已回收返回 None。

        句柄可能被停止流程在同一时刻回收：这里先捕获局部引用，避免在检查与调用
        之间被换掉（那会让监视线程撞上 `None`）。
        """
        process = self._process
        if process is None:
            return None
        return process.wait(timeout_ms)

    def terminate(self) -> None:
        """停止超时后的强制终止；标记 forced_stop，不得声称优雅完成。"""
        self._forced_stop = True
        process, self._process = self._process, None
        if process is not None:
            process.terminate()
            self._process = process

    def close(self) -> None:
        """关闭管道、进程句柄与 Job；重复关闭是安全的。

        正常退出应先 request_stop + wait；直接 close 等于放弃纳管——Job
        句柄关闭会触发系统级回收（崩溃兜底路径）。
        """
        process, self._process = self._process, None
        pipe, self._control = self._control, None
        report, self._report = self._report, None
        drain, self._drain = self._drain, None
        job, self._job = self._job, None
        if pipe is not None:
            pipe.close()
        if report is not None:
            report.close()
        if drain is not None:
            drain.close()
        if process is not None:
            process.close_handles()
        if job is not None:
            job.close()
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2)
        drainer, self._drainer = self._drainer, None
        if drainer is not None and drainer is not threading.current_thread():
            drainer.join(timeout=2)

    def __enter__(self) -> WorkerProcess:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- 内部线程 ---------------------------------------------------------

    def _read_reports(self) -> None:
        """读上报帧直到 EOF；坏帧终止会话（固定类别，不尝试猜测语义）。"""
        report = self._report
        if report is None:
            return
        buffer = b""

        def read(size: int) -> bytes:
            nonlocal buffer
            while len(buffer) < size:
                chunk = report.receive(_REPORT_CHUNK_BYTES)
                if not chunk:
                    return b""
                buffer += chunk
                if len(buffer) > ipc.MAX_PENDING_BYTES:
                    raise ipc.IpcError("pending_too_large")
            piece, buffer = buffer[:size], buffer[size:]
            return piece

        try:
            while True:
                try:
                    frame = ipc.read_frame(
                        read,
                        allowed=ipc.REPORTS,
                        expect_instance=self._instance_id,
                        expect_run=self._run_id,
                    )
                except ipc.IpcError:
                    break
                if frame is None:
                    break
                _enqueue_report_frame(self._frames, frame)
        finally:
            try:
                self._frames.put_nowait(None)
            except queue.Full:
                pass
            self.report_closed.set()

    def _drain_output(self) -> None:
        """持续排空子进程原始输出并丢弃，只留下字节计数（§12）。"""
        drain = self._drain
        if drain is None:
            return
        while True:
            chunk = drain.receive(_DRAIN_CHUNK_BYTES)
            if not chunk:
                return
            self.drained_bytes += len(chunk)


# --- 启停状态机（§9.2） -------------------------------------------------------

STATE_STOPPED: str = "stopped"
STATE_STARTING: str = "starting"
STATE_RUNNING: str = "running"
STATE_STOPPING: str = "stopping"
STATE_FAILED: str = "failed"

OP_RUNNING: str = "running"
OP_FINISHED: str = "finished"
OP_FAILED: str = "failed"

# 启动等待上限：登录 + SSE 建立 + 首轮初始化都要走完，给足但不无限等（§9.2）。
START_TIMEOUT_SECONDS: float = 60.0

# 停止预算必须大于 Core 的 10 秒关闭预算，并给归档关闭与进程退出留余量（§9.3）。
STOP_BUDGET_MS: int = 20_000

# 启动期 stop 要等构造线程看到取消并回收子进程；至少覆盖 `_reap()` 的两轮等待。
START_CANCEL_JOIN_SECONDS: float = 5.0


@dataclass(frozen=True)
class Operation:
    """一次启停操作的结果快照；阶段与结果都是固定码（§9.2、§11）。"""

    operation_id: str
    kind: str
    state: str
    result: str | None
    target_revision: int | None
    started_at: float
    finished_at: float | None = None


class WorkerManager:
    """启停的串行决策者（§9.2）。

    - 长等待都在后台线程里，接口立刻返回 `Operation`（L3 的 API 回 202）；
    - `stop` 能抢占 `starting`：启动等待期间 `stop` 会置取消事件，启动操作随即
      终止并回收半初始化的子进程，而不是等它自己超时；
    - 只有确认旧进程退出并释放数据锁之后才允许创建下一个进程；
    - 意外退出（非本机发起的停止）进入 `failed`，首版不自动重启（§9.2）。
    """

    def __init__(
        self,
        platform: LauncherPlatform,
        *,
        spec_factory,
        instance_id: str,
        clock=time.monotonic,
        start_timeout: float = START_TIMEOUT_SECONDS,
        stop_budget_ms: int = STOP_BUDGET_MS,
        on_event=None,
    ) -> None:
        self._platform = platform
        self._spec_factory = spec_factory
        self._instance_id = instance_id
        self._clock = clock
        self._start_timeout = start_timeout
        self._stop_budget_ms = stop_budget_ms
        self._on_event = on_event or (lambda kind, fields, level='info': None)
        self._lock = threading.RLock()
        self._state = STATE_STOPPED
        self._worker: WorkerProcess | None = None
        self._operation: Operation | None = None
        self._operations: dict[str, Operation] = {}
        self._cancel_start = threading.Event()
        # 启动操作只由自己的线程清理；stop 在启动期只发取消信号并等待该操作收尾。
        self._start_complete = threading.Event()
        self._start_complete.set()
        self._active_start_operation_id: str | None = None
        self._restart_in_progress = False
        self._restart_operation_id: str | None = None
        # 停止请求的“代次”：restart 在停止阶段前记下它，停止后若变过就放弃启动阶段
        # —— 否则停止阶段的 stop 会被自己的清理动作抹掉（复审 MEDIUM）。
        self._stop_epoch = 0
        self._quitting = False
        self._forced_stop = False
        self._exit_reason: str | None = None
        self._monitor: threading.Thread | None = None
        self._last_status: dict | None = None
        # 正在运行的 Worker 是用哪个 revision 起的；停止后清空（§6.5 的 running_revision）。
        self._running_revision: int | None = None
        self._run_seq = 0

    # --- 查询 -------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def worker(self) -> WorkerProcess | None:
        with self._lock:
            return self._worker

    @property
    def last_status(self) -> dict | None:
        """Worker 最近一次上报的状态快照（§10.2）；没有就是 None，不猜。"""
        with self._lock:
            return self._last_status

    def current_operation(self) -> Operation | None:
        with self._lock:
            return self._operation

    def operation(self, operation_id: str) -> Operation | None:
        with self._lock:
            return self._operations.get(operation_id)

    def status(self) -> dict:
        with self._lock:
            worker = self._worker
            return {
                "state": self._state,
                "pid": worker.pid if worker is not None else None,
                "running_revision": self._running_revision,
                # 记住最后一次停止是否只能强制完成；Worker 已经回收时也要如实报告。
                "forced_stop": bool(
                    (worker is not None and worker.forced_stop) or self._forced_stop
                ),
                "exit_reason": self._exit_reason,
                "operation_id": self._operation.operation_id if self._operation else None,
            }

    # --- 操作 -------------------------------------------------------------

    def start(self, *, revision: int | None) -> Operation:
        """启动 Worker；处于 starting/running/stopping 时返回当前在途操作（幂等）。"""
        with self._lock:
            if self._quitting:
                return self._refused("start", revision, "quitting")
            if self._restart_in_progress:
                active = self._operations.get(self._restart_operation_id or "")
                if active is not None:
                    return active
            if self._state in (STATE_STARTING, STATE_RUNNING, STATE_STOPPING):
                in_flight = self._operation
                if in_flight is not None:
                    return in_flight
            self._cancel_start.clear()
            operation = self._new_operation("start", revision)
            self._state = STATE_STARTING
            self._start_complete.clear()
            self._active_start_operation_id = operation.operation_id
            self._spawn(self._run_start, operation)
            return operation

    def stop(self) -> Operation:
        """停止 Worker；已停止时返回一个立即完成的 finished 操作（幂等）。"""
        with self._lock:
            if self._restart_in_progress:
                self._cancel_start.set()
                self._stop_epoch += 1
                active = self._operations.get(self._restart_operation_id or "")
                if active is not None:
                    return active
            if self._state == STATE_STOPPED:
                # 停一个本来就停着的东西：不留「取消启动」意图，否则之后的
                # restart 会把它当成「刚被停止取消」而静默不动（审查 I1）。
                # 代次仍要推进：restart 的停止阶段里落下的这次 stop 必须让它放弃启动
                # （复审 N-4）。
                self._stop_epoch += 1
                self._cancel_start.clear()
                operation = self._new_operation("stop", None)
                return self._finish(operation, OP_FINISHED, "stopped")
            cancelled_start = self._state == STATE_STARTING
            self._cancel_start.set()  # 抢占 starting（§9.2）
            self._stop_epoch += 1
            if self._state == STATE_STOPPING and self._operation is not None:
                return self._operation
            operation = self._new_operation("stop", None)
            # 在返回前发布 stopping，避免 start/restart 趁清理线程尚未调度而并行启动。
            self._state = STATE_STOPPING
            self._spawn(self._do_stop, operation, cancelled_start)
            return operation

    def restart(self, *, revision: int | None) -> Operation:
        """先停后启；返回整个组合操作的快照（§9.2）。"""
        with self._lock:
            if self._quitting:
                return self._refused("restart", revision, "quitting")
            if self._restart_in_progress:
                active = self._operations.get(self._restart_operation_id or "")
                if active is not None:
                    return active
            if self._state in (STATE_STARTING, STATE_STOPPING):
                # 不和其他生命周期操作并行；调用方可在当前操作完成后再次重启。
                return self._refused("restart", revision, "operation_in_progress")
            self._restart_in_progress = True
            operation = self._new_operation("restart", revision)
            self._restart_operation_id = operation.operation_id
            epoch = self._stop_epoch
            self._state = STATE_STOPPING
            self._spawn(self._do_restart, operation, revision, epoch)
            return operation

    def begin_quit(self) -> None:
        """进入退出流程：此后拒绝一切启动（沿用 L0 的语义，扩到 restart）。"""
        with self._lock:
            self._quitting = True
            self._cancel_start.set()
            self._stop_epoch += 1

    def shutdown(self) -> None:
        """退出收尾：停掉 Worker 并等所有在途操作结束。"""
        self.begin_quit()
        # 启动中的线程拥有尚未挂接 Worker 的构造与清理权；等它因 quitting
        # 取消并回收后再做统一收尾，避免 shutdown 返回后留下新进程。
        self._start_complete.wait()
        self._stop_synchronously()
        monitors = []
        with self._lock:
            if self._monitor is not None:
                monitors.append(self._monitor)
                self._monitor = None
        for thread in monitors:
            thread.join(timeout=5)

    # --- 内部：操作实现 ---------------------------------------------------

    def _run_start(self, operation: Operation) -> None:
        """运行一次启动，并在所有取消/失败路径后通知等待中的 stop。"""
        try:
            self._do_start(operation)
        finally:
            with self._lock:
                if self._active_start_operation_id == operation.operation_id:
                    self._active_start_operation_id = None
                    self._start_complete.set()

    def _do_start(self, operation: Operation) -> None:
        revision = operation.target_revision
        self._on_event("worker.starting", {"revision": revision}, "info")
        with self._lock:
            if self._quitting or self._cancel_start.is_set():
                # 等待期间到达的退出/取消意图：不再创建新进程（§9.2、审查 I2）。
                self._mark_start_cancelled()
                self._finish(operation, OP_FINISHED, "cancelled")
                return
            self._run_seq += 1
            run_id = f"{self._instance_id}-{self._run_seq}"
        try:
            spec = self._spec_factory(revision, run_id)
            worker = WorkerProcess(
                self._platform, spec, instance_id=self._instance_id, run_id=run_id
            )
        except Exception as exc:
            if self._cancel_start.is_set() or self._quitting:
                self._mark_start_cancelled()
                self._finish(operation, OP_FINISHED, "cancelled")
                return
            self._finish(operation, OP_FAILED, _worker_failure_code(exc))
            with self._lock:
                self._state = STATE_FAILED
                self._exit_reason = _worker_failure_code(exc)
            self._on_event(
                "worker.start_failed", {"reason": _worker_failure_code(exc)}, "warning"
            )
            return
        with self._lock:
            abandoned = self._quitting or self._cancel_start.is_set()
            if not abandoned:
                self._worker = worker
        if abandoned:
            # 创建进程的这段时间里来了退出/取消：就地回收，不放进运行态。
            self.drain(worker)
            self._reap(worker)
            self._mark_start_cancelled()
            self._finish(operation, OP_FINISHED, "cancelled")
            return
        outcome = self._wait_ready(worker)
        if outcome != "ready":
            if outcome == "cancelled":
                reason = "cancelled"
            elif outcome == "exited":
                code = worker.wait(0)
                # 启动阶段就退出：把退出码说清楚，而不是一律报「超时」（审查 I4）。
                reason = f"exit_{code}" if code is not None else "exit_unknown"
            else:
                reason = "start_timeout"
            # 死掉的 Worker 已经留下了诊断帧（配置无效、数据被占用）：先排空再回收。
            self.drain(worker)
            with self._lock:
                if self._worker is worker:
                    self._worker = None
            self._reap(worker)
            if reason == "cancelled":
                self._mark_start_cancelled()
            else:
                with self._lock:
                    self._state = STATE_FAILED
                    self._exit_reason = reason
            self._finish(operation, OP_FINISHED if reason == "cancelled" else OP_FAILED, reason)
            if reason != "cancelled":
                self._on_event("worker.start_failed", {"reason": reason}, "warning")
            return
        self.drain(worker)
        with self._lock:
            cancelled = self._quitting or self._cancel_start.is_set()
            if not cancelled:
                self._state = STATE_RUNNING
                self._exit_reason = None
                self._forced_stop = False
                self._running_revision = revision
        if cancelled:
            self._reap(worker)
            with self._lock:
                self._worker = None
            self._mark_start_cancelled()
            self._finish(operation, OP_FINISHED, "cancelled")
            return
        self._start_monitor(worker)
        self._finish(operation, OP_FINISHED, "running")
        self._on_event("worker.started", {"revision": revision, "pid": worker.pid}, "info")

    def _mark_start_cancelled(self) -> None:
        """取消启动后先保留生命周期门槛，等 stop/restart 操作完成再开放新启动。"""
        with self._lock:
            self._state = STATE_STOPPED if self._quitting else STATE_STOPPING
            self._exit_reason = None

    def _wait_ready(self, worker: WorkerProcess) -> str:
        """等 ready；返回 `ready` / `cancelled` / `exited` / `timeout`（§9.2）。

        期间的每一帧都折进事件与最近状态：`status` 帧是快照的唯一来源，
        `log` 帧是启动失败时用户能看到的唯一诊断（审查 I3/I4）。
        """
        deadline = self._clock() + self._start_timeout
        while self._clock() < deadline:
            if self._cancel_start.is_set() or self._quitting:
                return "cancelled"
            frame = worker.next_frame(0.2)
            if frame is not None:
                self._consume(frame)
                if frame["kind"] == "ready":
                    return "ready"
            if worker.wait(0) is not None:
                return "exited"
        return "timeout"

    def _consume(self, frame: dict) -> None:
        """把一帧上报折进事件与最近状态。"""
        kind = frame.get("kind")
        payload = frame.get("payload") or {}
        if kind in ("ready", "status"):
            status = payload.get("status")
            if isinstance(status, dict):
                self._last_status = status
            if kind == "ready":
                self._on_event("worker.ready", {}, "info")
        elif kind == "log":
            level = str(payload.get("level", "info")).lower()
            fields = payload.get("fields")
            self._on_event(
                str(payload.get("event", "worker.log")),
                dict(fields) if isinstance(fields, dict) else {},
                level,
            )
        elif kind == "stopped":
            status = payload.get("status")
            if isinstance(status, dict):
                self._last_status = status
            self._on_event("worker.stopped", {}, "info")

    def drain(self, worker: WorkerProcess | None = None) -> None:
        """排空 Worker 已上报但还没消费的帧（状态查询与关闭路径都用它）。"""
        target = worker if worker is not None else self._worker
        if target is None:
            return
        while True:
            frame = target.next_frame(0)
            if frame is None:
                return
            self._consume(frame)

    def _do_stop(self, operation: Operation, cancelled_start: bool = False) -> None:
        if cancelled_start:
            # Worker 构造期间 `_worker` 尚未挂接。启动线程持有创建与回收所有权，
            # stop 只等它观察取消、回收半初始化进程，避免清除信号或双重 close。
            if not self._start_complete.wait(
                max(self._stop_budget_ms / 1000, START_CANCEL_JOIN_SECONDS)
            ):
                # 保持 stopping 状态，禁止并发启动；启动线程若稍后完成会自行回收并转 stopped。
                self._finish(operation, OP_FAILED, "start_cleanup_timeout")
                self._on_event(
                    "worker.stop_failed", {"reason": "start_cleanup_timeout"}, "warning"
                )
                return
            with self._lock:
                self._state = STATE_STOPPED
                self._exit_reason = None
                self._running_revision = None
            self._finish(operation, OP_FINISHED, "cancelled")
            self._on_event("worker.stopped", {"result": "cancelled"}, "info")
            return
        with self._lock:
            self._state = STATE_STOPPING
        result = self._stop_synchronously(cancelled_start=cancelled_start)
        self._finish(operation, OP_FINISHED, result)
        self._on_event("worker.stopped", {"result": result}, "info")

    def _do_restart(self, operation: Operation, revision: int | None, epoch: int) -> None:
        with self._lock:
            cancelled_start = False
            self._state = STATE_STOPPING
        try:
            self._stop_synchronously(cancelled_start=cancelled_start)
            with self._lock:
                superseded = self._quitting or self._stop_epoch != epoch
            if superseded:
                # 停止阶段里又来了一个 stop/退出：重启意图作废，不再起新进程。
                self._finish(operation, OP_FINISHED, "stopped")
                return
            with self._lock:
                self._cancel_start.clear()
                self._state = STATE_STARTING
                self._start_complete.clear()
                self._active_start_operation_id = operation.operation_id
            start_op = Operation(
                operation_id=operation.operation_id,
                kind="start",
                state=OP_RUNNING,
                result=None,
                target_revision=revision,
                started_at=operation.started_at,
            )
            self._run_start(start_op)
            with self._lock:
                finished = self._operations[operation.operation_id]
                if finished.result == "cancelled":
                    # 手动 stop 在 restart 的新 Worker 启动期间取消了它；门槛
                    # 直到组合操作完成前一直保持关闭，此处再落到 stopped。
                    self._state = STATE_STOPPED
                    self._exit_reason = None
                final = Operation(
                    operation_id=finished.operation_id,
                    kind="restart",
                    state=finished.state,
                    result=finished.result,
                    target_revision=revision,
                    started_at=operation.started_at,
                    finished_at=finished.finished_at,
                )
                self._operations[operation.operation_id] = final
                self._operation = final
        finally:
            with self._lock:
                if self._restart_operation_id == operation.operation_id:
                    self._restart_in_progress = False
                    self._restart_operation_id = None

    def _stop_synchronously(self, *, cancelled_start: bool = False) -> str:
        """停止并回收当前 Worker；返回固定结果码（stopped / cancelled / forced_stop / failed）。"""
        with self._lock:
            worker = self._worker
            self._worker = None
        if worker is None:
            with self._lock:
                self._state = STATE_STOPPED
            return "stopped"
        worker.request_stop()
        code = worker.wait(self._stop_budget_ms)
        forced = False
        if code is None:
            worker.terminate()
            forced = True
            code = worker.wait(self._stop_budget_ms)
        # 退出瞬间可能刚送到最后一帧日志/状态：关句柄前收干净（复审 N-5）。
        self.drain(worker)
        worker.close()
        if forced:
            self._forced_stop = True
        with self._lock:
            if cancelled_start:
                # 半初始化的进程被回收：没有在途工作，如实报「已取消」而不是失败。
                self._state = STATE_STOPPED
                self._exit_reason = None
                result = "cancelled"
            elif forced or code != 0:
                self._state = STATE_FAILED
                if forced:
                    self._exit_reason = "forced_stop"
                elif code is None:
                    self._exit_reason = "exit_unknown"
                else:
                    self._exit_reason = f"exit_{code}"
                result = "forced_stop" if forced else "failed"
            else:
                self._state = STATE_STOPPED
                self._exit_reason = None
                result = "stopped"
        self._last_status = None
        self._running_revision = None
        return result

    def _reap(self, worker: WorkerProcess) -> None:
        """回收一个没能进入 running 的 Worker：先请停，再按预算强制。"""
        worker.request_stop()
        if worker.wait(2000) is None:
            worker.terminate()
            worker.wait(2000)
        # 退出瞬间可能刚送到一帧诊断（配置无效、数据被占用）：关句柄前再收一次。
        self.drain(worker)
        worker.close()

    def _start_monitor(self, worker: WorkerProcess) -> None:
        """监视意外退出：不是本机发起的停止就进 failed，不自动重启（§9.2）。"""

        def _wait() -> None:
            while True:
                if worker.closed:
                    # 正常停止流程已经回收了它，不重复报「意外退出」。
                    return
                code = worker.wait(1000)
                if code is not None:
                    break
                with self._lock:
                    if self._worker is not worker:
                        # 正常的停止流程已经接管了这个进程，不重复报「意外退出」。
                        return
            with self._lock:
                if self._worker is not worker:
                    return
                self._state = STATE_FAILED
                self._exit_reason = f"exit_{code}"
                # 进程没了：快照与运行版本不能再冒充当前事实（复审 N-5）。
                self._worker = None
                self._last_status = None
                self._running_revision = None
            # 已退出的 Worker 不再有其它线程负责回收；不要让后续 start 覆盖掉最后
            # 一个引用而泄漏 Job、进程与管道句柄。
            self.drain(worker)
            worker.close()
            self._on_event("worker.exited", {"reason": f"exit_{code}"}, "warning")

        with self._lock:
            previous = self._monitor
            self._monitor = threading.Thread(
                target=_wait, name="raricy-worker-monitor", daemon=True
            )
            self._monitor.start()
        if previous is not None:
            previous.join(timeout=1)

    # --- 内部：操作记录 ---------------------------------------------------

    def _new_operation(self, kind: str, revision: int | None) -> Operation:
        operation = Operation(
            operation_id=uuid4().hex[:12],
            kind=kind,
            state=OP_RUNNING,
            result=None,
            target_revision=revision,
            started_at=self._clock(),
        )
        with self._lock:
            self._operations[operation.operation_id] = operation
            self._operation = operation
        return operation

    def _finish(self, operation: Operation, state: str, result: str) -> Operation:
        final = Operation(
            operation_id=operation.operation_id,
            kind=operation.kind,
            state=state,
            result=result,
            target_revision=operation.target_revision,
            started_at=operation.started_at,
            finished_at=self._clock(),
        )
        with self._lock:
            self._operations[final.operation_id] = final
            if self._operation is None or self._operation.operation_id == final.operation_id:
                self._operation = final
        return final

    def _refused(self, kind: str, revision: int | None, reason: str) -> Operation:
        """退出流程中的拒绝：立刻完成、结果码固定（§9.2）。"""
        operation = self._new_operation(kind, revision)
        return self._finish(operation, OP_FINISHED, reason)

    def _spawn(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()


def _worker_failure_code(exc: BaseException) -> str:
    """把启动失败归一成固定类别码：不把异常正文交出去（§12）。"""
    name = type(exc).__name__
    if isinstance(exc, WorkerError):
        return str(exc)
    if name == "DataLockError":
        code = str(exc)
        if code in {"account_in_use", "account_lock_unavailable", "account_lock_invalid_identity"}:
            return code
        return "data_in_use"
    if name == "ConfigServiceError" or name == "ConfigConflict":
        return "config_unavailable"
    if name in ("ConfigInvalid",):
        return "config_invalid"
    if isinstance(exc, OSError):
        return "config_unavailable"
    return "start_failed"
