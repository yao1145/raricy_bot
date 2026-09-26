"""托盘协调器：命令队列、状态刷新节流与渲染（N3 Task 3，契约见 INTERFACES §61.2）。

窗口回调（Task 4）与 HTTP 线程只做一件事：`submit()` 投一条结构化消息。所有耗时动作
—— 启停、完整状态快照、取管理页地址、打开页面/目录、请求退出 —— 都只在**派发线程**里
执行（§7.2）。这条边界是承重的：`StatusService.snapshot()` 要读凭据库，可能弹系统授权框，
把它放进窗口消息回调里会让整个托盘（以及 Explorer 的这次通知）一起卡住。

刷新分两档，为的是保护凭据库：

- 每个 tick（`tick_seconds`）只用 `process_view()` 重算进程状态与
  `snapshot_freshness(...)`，廉价、无 I/O；
- 完整 `status_snapshot()` 只在启动、命令执行完成、`power_resume`、`FULL_REFRESH_SECONDS`
  边界到期时调用；订阅了事件服务时，只对 `REFRESH_EVENTS` 那六个生命周期事件额外刷新。
  聊天/日志类事件一律不刷新，否则每条消息都会读一次凭据库。

命令被拒绝时只记稳定类别码（`TrayCommandError.code`）：日志与页面事件用同一个码，异常
原文既不进日志也不进事件，更不弹任何模态对话框 —— 无人值守的启动不能被一个弹窗卡住。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from raricy_bot.logging_setup import log_event

from . import tray_model
from .status_service import UNKNOWN, snapshot_freshness
from .tray_model import TrayView

# 一个 tick 的间隔：只用廉价视图重算进程状态与新鲜度（无 I/O）。
TICK_SECONDS: float = 1.0

# 完整快照（可能阻塞读凭据库）的最短间隔；tick 到这个边界才做一次。
FULL_REFRESH_SECONDS: float = 30.0

# 订阅事件里只认这六个生命周期事件触发完整刷新；白名单外的一律不刷新。
REFRESH_EVENTS: frozenset[str] = frozenset(
    {
        "worker.starting",
        "worker.started",
        "worker.ready",
        "worker.stopped",
        "worker.exited",
        "worker.start_failed",
    }
)

# 稳定类别码：命令被拒绝时只有这四种，绝不携带原始异常正文。
CODE_CONFIG_NOT_READY: str = "config_not_ready"
CODE_LIFECYCLE_BUSY: str = "lifecycle_busy"
CODE_QUITTING: str = "quitting"
CODE_TRAY_INTERNAL: str = "tray_internal"

# 停止派发线程的默认等待上限（秒）；到点就返回，长等待不拖住退出。
STOP_TIMEOUT_SECONDS: float = 2.0


class TrayCommandError(Exception):
    """托盘命令被拒绝：`code` 是稳定类别码，异常本身不含原文、路径或账号。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@runtime_checkable
class TraySurface(Protocol):
    """托盘图标在协调器眼里的样子：只需要能呈现一个视图。

    Task 4 的真实实现负责窗口与消息循环；测试用记录替身。
    """

    def present(self, view: TrayView) -> None:
        """呈现视图；线程安全，窗口销毁后是空操作。"""
        ...


@runtime_checkable
class DesktopCommands(Protocol):
    """托盘命令要做的桌面动作；由 `Controller` 实现（与 HTTP 走同一套门与错误码）。"""

    def start_bot(self) -> str:
        """启动已保存版本，返回 operation_id；配置不可用抛 TrayCommandError。"""
        ...

    def stop_bot(self) -> str:
        """停止 Worker，返回 operation_id（不要求已保存配置）。"""
        ...

    def restart_bot(self) -> str:
        """重启到已保存版本，返回 operation_id。"""
        ...

    def status_snapshot(self) -> dict:
        """完整状态快照；可能阻塞（读凭据库），只允许派发线程调用。"""
        ...

    def process_view(self) -> dict:
        """廉价视图 `{"process": ..., "worker": ...}`；只读内存，无 I/O。"""
        ...

    def entry_url(self) -> str:
        """带一次性引导令牌的管理页地址（§8.1）。"""
        ...

    def diagnostics_dir(self) -> Path:
        """诊断日志目录。"""
        ...

    def begin_session_end(self) -> None:
        """注销/关机：拒绝新启动、请求退出，并把本次退出记为 session_end。"""
        ...

    def request_quit(self) -> None:
        """请求退出。"""
        ...


class TrayCoordinator:
    """托盘与 Controller 之间的唯一协调者：一条命令队列 + 一个派发线程。"""

    def __init__(
        self,
        *,
        surface: TraySurface,
        commands: DesktopCommands,
        logger: logging.Logger,
        open_url: Callable[[str], None],
        open_path: Callable[[Path], None],
        events=None,
        clock: Callable[[], float] = time.time,
        tick_seconds: float = TICK_SECONDS,
        full_refresh_seconds: float = FULL_REFRESH_SECONDS,
    ) -> None:
        self._surface = surface
        self._commands = commands
        self._logger = logger
        self._open_url = open_url
        self._open_path = open_path
        self._events = events
        self._clock = clock
        self._tick_seconds = tick_seconds
        self._full_refresh_seconds = full_refresh_seconds

        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscriber: queue.Queue | None = None

        # 完整刷新来源的状态；进程与 Worker 新鲜度由每次 tick 的廉价视图统一提供，
        # 不在两处各存一份真相（§61.1 的新鲜度判据只有一处）。
        self._config_state = tray_model.CONFIG_STATE_RECOVERY
        self._account: str | None = None
        self._process: dict = {}
        self._freshness = UNKNOWN
        self._power = tray_model.POWER_ACTIVE
        self._awaiting_since: float | None = None
        self._quitting = False
        self._last_view: TrayView | None = None
        # 完整刷新的下一个边界；首次刷新（启动时）会把它推到 30 秒之后。
        self._next_full_refresh = float("inf")

    # --- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        """起派发线程（daemon，命名 `raricy-tray`）；重复调用无操作。"""
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._run, name="raricy-tray", daemon=True)
        self._thread = thread
        thread.start()

    def submit(self, message: str) -> None:
        """投递一条命令或系统事件；只入队，不阻塞、不做 I/O，任何线程可调。"""
        self._queue.put_nowait(message)

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """请求停止并等派发线程退出；幂等，超时即返回（不无限拖住退出流程）。"""
        self._stop.set()
        # 哨兵只用来把阻塞在 `get()` 上的派发线程叫醒；停下之后不再处理剩余消息。
        self._queue.put_nowait(None)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # --- 派发线程 ---------------------------------------------------------

    def _run(self) -> None:
        """派发线程主体：订阅事件、完整刷新一次，然后循环处理消息与 tick。"""
        self._subscriber = self._subscribe()
        try:
            self._refresh_full()
            self._tick()
            while not self._stop.is_set():
                try:
                    message = self._queue.get(timeout=self._tick_seconds)
                except queue.Empty:
                    message = None
                if self._stop.is_set():
                    break
                self._step(message)
        finally:
            self._unsubscribe()

    def _step(self, message: str | None) -> None:
        """一次循环体：先折进系统事件，再处理一条消息，或按 tick 到期重算。"""
        try:
            self._pump_events()
            if message is None:
                self._tick()
            else:
                self._handle(message)
        except Exception:
            # 兜底：任何意外都不允许带走派发线程（托盘是主要的可见入口之一）。
            self._reject(CODE_TRAY_INTERNAL)

    def _handle(self, message: str) -> None:
        """处理一条消息；未识别的消息忽略并记事件，命令异常只记类别码。"""
        if message == tray_model.TRAY_COMMAND_OPEN_ADMIN:
            self._execute(lambda: self._open_url(self._commands.entry_url()))
        elif message == tray_model.TRAY_COMMAND_START:
            self._execute(lambda: self._commands.start_bot())
        elif message == tray_model.TRAY_COMMAND_STOP:
            self._execute(lambda: self._commands.stop_bot())
        elif message == tray_model.TRAY_COMMAND_RESTART:
            self._execute(lambda: self._commands.restart_bot())
        elif message == tray_model.TRAY_COMMAND_OPEN_DIAGNOSTICS:
            self._execute(lambda: self._open_path(self._commands.diagnostics_dir()))
        elif message == tray_model.TRAY_COMMAND_QUIT:
            self._set_quitting()
            self._execute(lambda: self._commands.request_quit())
        elif message == tray_model.TRAY_EVENT_SESSION_END:
            self._set_quitting()
            self._execute(lambda: self._commands.begin_session_end())
        elif message == tray_model.TRAY_EVENT_POWER_SUSPEND:
            # 睡眠前不再声明在线；恢复要等一条不早于恢复时刻的新上报。
            self._set_power(tray_model.POWER_SUSPENDED, awaiting_since=None)
            self._tick()
            return
        elif message == tray_model.TRAY_EVENT_POWER_RESUME:
            self._set_power(tray_model.POWER_AWAITING_REPORT, awaiting_since=self._clock())
            self._refresh_full()
            self._tick()
            return
        elif message in (
            tray_model.TRAY_EVENT_SESSION_QUERY,
            tray_model.TRAY_EVENT_TASKBAR_CREATED,
        ):
            # `session_query` 只是系统在问「能不能关机」（可能被取消），
            # `taskbar_created` 是 Explorer 重建任务栏：都只重画一次，不做别的。
            self._tick()
            return
        else:
            # 不记原文：未识别的消息可能来自任何回调，只留一个类别码。
            log_event(
                self._logger,
                logging.INFO,
                "launcher.tray_message_ignored",
                status="ignored",
                error="unknown_message",
            )
            return
        # 命令已经执行完成：刷新完整状态再渲染（§61 的完整刷新时机之一）。
        self._refresh_full()
        self._tick()

    def _execute(self, action: Callable[[], object]) -> None:
        """在派发线程里执行一个命令动作；失败只记类别码，绝不抛出。"""
        try:
            action()
        except TrayCommandError as exc:
            self._reject(exc.code)
        except Exception:
            self._reject(CODE_TRAY_INTERNAL)

    # --- 状态与刷新 -------------------------------------------------------

    def _tick(self) -> None:
        """一个 tick：廉价重算进程与新鲜度，处理电源恢复，到期做完整刷新，然后渲染。"""
        view = self._process_view()
        process = view.get("process")
        worker = view.get("worker")
        worker = worker if isinstance(worker, dict) else None
        now = self._clock()
        with self._lock:
            if isinstance(process, dict):
                self._process = process
            # 新鲜度判据只有一处：status_service.snapshot_freshness（§61.1）。
            self._freshness = snapshot_freshness(worker, now=now)
            self._resolve_power_locked(worker)
            due = now >= self._next_full_refresh
        if due:
            self._refresh_full()
        self._render()

    def _refresh_full(self) -> None:
        """完整快照（可能阻塞读凭据库）；只允许派发线程调用。"""
        with self._lock:
            if self._quitting:
                # 已经在退出：不再读凭据库，图标保持「正在退出」。
                return
            # 先把边界推远：刷新失败也等下一个边界再试，不每个 tick 重试。
            self._next_full_refresh = self._clock() + self._full_refresh_seconds
        try:
            snapshot = self._commands.status_snapshot()
        except TrayCommandError as exc:
            self._reject(exc.code)
            return
        except Exception:
            self._reject(CODE_TRAY_INTERNAL)
            return
        config = snapshot.get("config") if isinstance(snapshot, dict) else None
        config = config if isinstance(config, dict) else {}
        with self._lock:
            state = config.get("state")
            if isinstance(state, str):
                self._config_state = state
            account = config.get("account")
            self._account = account if isinstance(account, str) and account else None

    def _process_view(self) -> dict:
        """廉价视图；取不到时记一条类别码继续跑，绝不退出派发线程。"""
        try:
            view = self._commands.process_view()
        except Exception:
            self._reject(CODE_TRAY_INTERNAL)
            return {}
        return view if isinstance(view, dict) else {}

    def _pump_events(self) -> None:
        """把订阅到的事件折进状态；只有六个生命周期事件触发完整刷新。"""
        subscriber = self._subscriber
        if subscriber is None:
            return
        refresh = False
        while True:
            try:
                event = subscriber.get_nowait()
            except queue.Empty:
                break
            if event is None:
                # 退订/关闭的哨兵：不会再有事件了。
                break
            if event.name in REFRESH_EVENTS:
                refresh = True
        if refresh:
            self._refresh_full()

    def _resolve_power_locked(self, worker: dict | None) -> None:
        """有了一条不早于恢复时刻的上报，才从「已恢复，等待新上报」回到在线。

        Core 自己的重连能力不动：托盘只如实显示，不替它做任何重连动作。
        """
        if self._power != tray_model.POWER_AWAITING_REPORT or self._awaiting_since is None:
            return
        sampled = worker.get("sampled_at") if isinstance(worker, dict) else None
        if isinstance(sampled, (int, float)) and not isinstance(sampled, bool):
            if sampled >= self._awaiting_since:
                self._power = tray_model.POWER_ACTIVE
                self._awaiting_since = None

    def _set_power(self, power: str, *, awaiting_since: float | None) -> None:
        with self._lock:
            self._power = power
            self._awaiting_since = awaiting_since

    def _set_quitting(self) -> None:
        """退出已经开始：图标显示「正在退出」，也不再做完整刷新。"""
        with self._lock:
            self._quitting = True

    def _state_locked(self) -> tray_model.TrayState:
        return tray_model.TrayState(
            config_state=self._config_state,
            process_state=str(self._process.get("state", tray_model.PROCESS_STATE_STOPPED)),
            worker_freshness=self._freshness,
            account=self._account,
            forced_stop=bool(self._process.get("forced_stop")),
            power=self._power,
            quitting=self._quitting,
        )

    def _render(self) -> None:
        """与上次不同才呈现；呈现失败只记类型名并继续（派发线程不因渲染退出）。"""
        with self._lock:
            view = tray_model.build_view(self._state_locked())
            if view == self._last_view:
                return
            # 先记下「已经尝试呈现」的视图：失败不每个 tick 重复报错，等状态变了再试。
            self._last_view = view
        try:
            self._surface.present(view)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.tray_present_failed",
                status="failed",
                error=type(exc).__name__,
            )

    # --- 事件订阅 ---------------------------------------------------------

    def _subscribe(self) -> queue.Queue | None:
        """订阅事件；订阅者已满（返回 None）不算失败，只是不再随事件刷新。"""
        if self._events is None:
            return None
        return self._events.subscribe()

    def _unsubscribe(self) -> None:
        """退订；`_run()` 无论怎么结束都必须走到这里。"""
        subscriber, self._subscriber = self._subscriber, None
        if subscriber is not None and self._events is not None:
            self._events.unsubscribe(subscriber)

    # --- 错误 -------------------------------------------------------------

    def _reject(self, code: str) -> None:
        """只记类别码：日志与页面事件同码，异常原文不进任何一处，也不弹对话框。"""
        if code == CODE_QUITTING:
            with self._lock:
                self._quitting = True
        log_event(
            self._logger,
            logging.WARNING,
            "launcher.tray_command_failed",
            status="rejected",
            error=code,
        )
        if self._events is not None:
            self._events.publish("launcher.tray_command_failed", level="warning", error=code)
