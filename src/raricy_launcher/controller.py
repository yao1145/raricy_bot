"""Light 桌面控制面：单实例、配置事务、进程管理、本地 API 与退出。

职责边界（LIGHT_EDITION_DESIGN §3.2）：

- **不做业务**：站点、模型、数据库与记忆都在 Worker 子进程里；
- **只做控制**：本机会话与 API、配置事务（§6）、凭据（§7）、进程状态机与 IPC
  （§9、§10）、事件缓冲与状态聚合（§12）。
- 长等待都在后台线程或后台操作里，HTTP 请求只返回 `operation_id`（§9.2）。

控制服务只监听回环，端口由操作系统分配；监听成功之后才发布运行元数据与
打开管理页（§9.4）。退出时先停 Worker（经私有控制管道请求优雅停止），再关
HTTP 服务、激活管道与互斥体（§9.3）。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from raricy_bot.logging_setup import log_event

from . import __version__, activation, paths, texts
from .activation import ActivationError
from .api import LocalApi
from .config_service import (
    STATE_CONFIGURED,
    ConfigService,
    ConfigServiceError,
    ConfigStatus,
)
from .credential_store import CredentialStore, SessionMemoryStore, SystemKeyringStore
from .desktop_settings import DesktopSettings, DesktopSettingsError, DesktopSettingsService
from .events import EventService
from .lifecycle_gate import LifecycleGate
from .platform import (
    InstanceGuard,
    LauncherPlatform,
    PlatformError,
    get_startup_registry,
)
from .process_manager import (
    START_TIMEOUT_SECONDS,
    STOP_BUDGET_MS,
    OP_FAILED,
    WorkerManager,
    WorkerSpec,
    default_worker_spec,
)
from .session import SessionManager
from .startup_service import StartupService
from .status_service import StatusService

_RUNTIME_FILE = "launcher-runtime.json"


class Controller:
    """桌面 Controller；所有长时间等待都不占用 HTTP 请求线程。"""

    def __init__(
        self,
        *,
        platform: LauncherPlatform,
        guard: InstanceGuard,
        data_root: Path,
        logger: logging.Logger,
        credential_store: CredentialStore | None = None,
        profile_id: str | None = None,
        open_url: Callable[[str], None] | None = None,
        startup_launch: bool = False,
        clock: Callable[[], float] = time.monotonic,
        start_timeout: float = START_TIMEOUT_SECONDS,
        stop_budget_ms: int = STOP_BUDGET_MS,
    ) -> None:
        if not guard.owned():
            raise ValueError("guard_not_owned")
        self._platform = platform
        self._guard = guard
        self._data_root = Path(data_root)
        self._logger = logger
        self._open_url = open_url or (lambda url: webbrowser.open(url))
        # 登录自启动来源提示（INTERFACES §59）：本任务只保存，自动运行解析在 N4 Task 5。
        self._startup_launch = startup_launch
        self._clock = clock
        self._instance_id = uuid4().hex[:12]
        self._started_at = clock()
        self._quit = threading.Event()
        self._ready = threading.Event()
        self._auto_start_watcher: threading.Thread | None = None

        # 没有显式注入时按 §7 选择后端：系统凭据库不可用就退到**会话内存**，
        # 并在日志里说明（界面另会显示后端名与可用性）。绝不落到明文文件。
        if credential_store is not None:
            self._credentials = credential_store
        else:
            system_store = SystemKeyringStore()
            if system_store.describe().available:
                self._credentials = system_store
            else:
                self._credentials = SessionMemoryStore()
                log_event(
                    logger,
                    logging.WARNING,
                    "launcher.credentials_fallback",
                    status="session_only",
                    error="SystemKeyringStore",
                )
        self._config = ConfigService(
            self._data_root, credential_store=self._credentials, profile_id=profile_id
        )
        # 桌面偏好与登录启动项（§58、§59）：装配一次，API 与后续的自动运行解析共用；
        # 注册表适配器在这里惰性取得，测试用替身注入 `LocalApi`，不碰真实注册表。
        self._desktop_settings = DesktopSettingsService(self._data_root)
        self._startup_service = self._build_startup_service()
        # 事件时间要与管理页显示的墙钟一致（同一处时钟时基缺陷，复审指出）。
        self._events = EventService(instance_id=self._instance_id, clock=time.time)
        self._sessions = SessionManager(instance_id=self._instance_id, clock=time.monotonic)
        self._manager = WorkerManager(
            platform,
            spec_factory=self._build_spec,
            instance_id=self._instance_id,
            clock=clock,
            start_timeout=start_timeout,
            stop_budget_ms=stop_budget_ms,
            on_event=self._on_worker_event,
        )
        self._status = StatusService(
            instance_id=self._instance_id,
            config_service=self._config,
            manager=self._manager,
        )
        # 站点测试与启停共用的生命周期门：进程内单实例，随控制器一起装配
        # （§59、D-132）。互斥范围就是这个对象，所以只能有一个。
        self._lifecycle = LifecycleGate()
        self._api: LocalApi | None = None
        self._server = None
        self._api_thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()
        self._api_socket: socket.socket | None = None
        self._port = 0
        self._listener = None

    def _build_startup_service(self) -> StartupService | None:
        """装配登录启动项服务；取不到本机注册表通道时返回 None（端点回稳定失败）。

        `executable` 只有冻结发行形态才给出：开发形态是 `python.exe` 加源码目录，
        登记进 Run 在登录时跑不起来，服务层也会拒绝（`path_unusable`）。
        """
        try:
            registry = get_startup_registry()
        except PlatformError:
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.startup_unavailable",
                error="PlatformError",
            )
            return None
        frozen = bool(getattr(sys, "frozen", False))
        return StartupService(
            self._desktop_settings,
            registry,
            executable=sys.executable if frozen else None,
            frozen=frozen,
        )

    # --- 查询 -------------------------------------------------------------

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def port(self) -> int:
        return self._port

    @property
    def admin_url(self) -> str:
        """管理页地址（不带引导令牌）；未启动时抛错。"""
        if not self._port:
            raise RuntimeError("not_started")
        return f"http://127.0.0.1:{self._port}/"

    def entry_url(self) -> str:
        """带一次性引导令牌的管理页地址（§8.1）：令牌只在 fragment 里。"""
        token = self._sessions.issue_bootstrap()
        return f"{self.admin_url}#token={token}"

    def wait_ready(self, timeout: float) -> bool:
        """等待 run() 完成启动；供调用方与测试对齐时序。"""
        return self._ready.wait(timeout)

    # --- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        """绑定回环端口、启动 API 与激活管道、发布运行元数据。"""
        self._start_api()
        self._listener = self._platform.create_activation_listener()
        self._listener.start(self._handle_activation)
        log_event(
            self._logger,
            logging.INFO,
            "launcher.start",
            status="ok",
            trace_id=self._instance_id,
        )

    def run(self) -> None:
        """启动并按设计 §5.2 决定是否启动 Bot、是否打开浏览器。"""
        self.start()
        try:
            self._auto_start()
            self._ready.set()
            self._quit.wait()
        finally:
            self.stop()

    def stop(self) -> None:
        """停止 Worker、API、激活管道与互斥体；幂等（§9.3）。

        关闭步骤在进程内锁里串行：并发的两个 stop() 里，后到的那个走幂等早退，
        不会把拆到一半的组件再拆一遍。
        """
        with self._stop_lock:
            if self._quit.is_set() and self._api is None and self._listener is None:
                return
            self._quit.set()
            self._stop_locked()

    def _stop_locked(self) -> None:
        """真正的关闭步骤；只在 `stop()` 的关闭锁里执行。"""
        self._manager.shutdown()
        watcher, self._auto_start_watcher = self._auto_start_watcher, None
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=2)
        self._sessions.revoke_all()
        self._events.close()
        if self._server is not None:
            self._server.should_exit = True
        if self._api_thread is not None:
            self._api_thread.join(timeout=5)
            self._api_thread = None
        if self._api_socket is not None:
            try:
                self._api_socket.close()
            except OSError:
                pass
            self._api_socket = None
        if self._listener is not None:
            listener, self._listener = self._listener, None
            listener.close()
        self._remove_runtime_metadata()
        guard, self._guard = self._guard, None
        if guard is not None:
            guard.close()
        log_event(
            self._logger,
            logging.INFO,
            "launcher.quit",
            status="ok",
            trace_id=self._instance_id,
        )
        # 最后一步才置空：API 线程已经退出，重复 stop() 由此走幂等早退。
        self._api = None

    def request_quit(self) -> None:
        self._quit.set()

    def _auto_start(self) -> None:
        """首次进入：按桌面偏好解析自动运行并决定是否启动（§5.2、§8、D-150）。

        解析顺序（INTERFACES §59 的唯一实现）：

        1. 读 `desktop.json`（桌面偏好的**唯一来源**，顺带完成升级用户的一次性导入）；
        2. 校验启动目标 —— 目标档案已被移除就清空目标并关掉机器人自动启动偏好，
           保留 `launch_at_sign_in` 与注册项；目标暂时不完整则保留目标与偏好；
        3. 把选中指针设为启动目标（`_select_startup_profile()`；N1/N2 的服务入口
           尚未并入，见该方法）；
        4. 只有「偏好开 + 目标可用 + 选中成功」才启动机器人。

        `--startup` 只是来源提示：授权偏好、档案状态与恢复记录一概重新读取，启停
        仍走 `LifecycleGate` 与 `WorkerManager` 的同一条路（§8.1）。任何一步读不
        出来都不猜：不启动、不改写现场，只给一次可见提示。
        """
        try:
            settings = self._desktop_settings.read()
        except DesktopSettingsError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.auto_start_preference_failed",
                error=type(exc).__name__,
            )
            self._open_entry_or_tray()
            return
        if not settings.start_bot_on_launch:
            log_event(
                self._logger,
                logging.INFO,
                "launcher.auto_start_skipped",
                status="preference_off",
            )
            self._open_entry_or_tray()
            return
        target = settings.startup_profile_id
        if target is None:
            if self._startup_launch:
                # 登录启动只使用明确的启动目标，没有目标就不猜启动哪个档案（§8.1）；
                # 手动启动沿用原行为，启动当前选中档案。
                log_event(
                    self._logger,
                    logging.INFO,
                    "launcher.auto_start_skipped",
                    status="no_target",
                )
                self._open_entry_or_tray()
                return
            status = self._profile_status(None)
            if status is None or status.state != STATE_CONFIGURED:
                self._open_entry_or_tray()
                return
            self._start_bot(status.revision)
            return
        if not self._profile_exists(target):
            # 目标已被移除（不存在/已删除）：清空目标并关掉机器人自动启动偏好。
            self._clear_startup_target(settings)
            self._open_entry_or_tray()
            return
        status = self._profile_status(target)
        if status is None or status.state != STATE_CONFIGURED:
            # 暂时不完整（缺凭据、配置非法、恢复态）：保留目标与偏好，不启动、
            # 不自动清除 —— 凭据可以再填，配置可以再修。
            log_event(
                self._logger,
                logging.INFO,
                "launcher.auto_start_skipped",
                status="target_incomplete",
            )
            self._open_entry_or_tray()
            return
        if not self._select_startup_profile(target):
            # 选中指针无法确认指向目标：宁可这次不启动，也不让页面显示的档案与
            # 后台自动运行的档案不一致（§5.2）。
            log_event(
                self._logger,
                logging.INFO,
                "launcher.auto_start_skipped",
                status="selection_unavailable",
            )
            self._open_entry_or_tray()
            return
        self._start_bot(status.revision)

    def _start_bot(self, revision: int | None) -> None:
        """取生命周期租约后派发一次启动，并挂上静默失败监视（§9.2、D-132）。"""
        ticket = self._lifecycle.begin_operation("start")
        if ticket is None:
            # 门被站点测试占着：不绕过并发门，也不排队（§5.1 第 3 条）。
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.auto_start_skipped",
                status="lifecycle_busy",
            )
            self._open_entry_or_tray()
            return
        try:
            operation = self._manager.start(revision=revision)
        finally:
            # 租约只覆盖派发本身，长等待不留在门内（与 §59 的启停入口同一口径）。
            self._lifecycle.end(ticket)
        log_event(
            self._logger,
            logging.INFO,
            "launcher.auto_start",
            status="ok",
            trace_id=self._instance_id,
        )
        watcher = threading.Thread(
            target=self._watch_auto_start,
            args=(operation.operation_id,),
            name="raricy-auto-start-watch",
            daemon=True,
        )
        self._auto_start_watcher = watcher
        watcher.start()

    def _open_entry_or_tray(self) -> None:
        """没有自动启动机器人时的一次可见提示（§8.1）。

        手动启动沿用原行为：打开向导/恢复/管理页。登录启动默认只进托盘、不打开
        浏览器、不重复弹窗；托盘不可用（N3 尚未并入，见 `_tray_available()`）时
        按降级路径最多打开一次管理页，让用户仍看得到提示与恢复入口。
        """
        if self._startup_launch and self._tray_available():
            return
        self._open_url(self.entry_url())

    def _select_startup_profile(self, profile_id: str) -> bool:
        """把选中指针切到启动目标档案并发布新上下文（§5.2）。

        本次没有运行中的 Worker，切换事务退化为「校验目标 → 提交选中指针 → 启动」；
        目标校验由调用方完成。返回 True 表示「可以确认选中的就是目标」，只有这时
        才允许自动启动 —— 页面显示 A 而后台运行 B 是不允许的。

        **N1/N2 的选中/切换服务入口尚未并入本分支**（`ProfileService.activate()`
        之类还不存在）：这里只留接缝、一律返回 False，于是「有目标但选不了」时本次
        不启动、只给提示，而不是拿当前选中的档案凑数。服务落地后在这里调用选中
        服务（提交指针 + 发布新上下文）；**不自行写 `launcher.json`，也不在这里
        实现事务**（§5.2 的事务归档案服务）。
        """
        # TODO(N1/N2)：调用选中服务（如 profiles.activate(profile_id)）提交指针并
        # 发布新上下文；失败或服务未落地时继续保持 False。
        return False

    def _tray_available(self) -> bool:
        """登录启动时是否已有可见控制入口（N3 的托盘）。

        N3 的托盘尚未并入本分支，因此这里恒为 False：登录启动按降级路径「最多
        打开一次管理页」。接线位置就是本方法 —— N3 落地后改成报告托盘可用性
        （初始化失败即 False），上层分支不必再改。
        """
        return False

    def _profile_exists(self, profile_id: str) -> bool:
        """目标档案目录是否还在（查询路径：不建目录、不写指针）。"""
        try:
            return paths.profile_dir(self._data_root, profile_id).is_dir()
        except (OSError, ValueError):
            return False

    def _profile_status(self, profile_id: str | None) -> ConfigStatus | None:
        """按档案读配置就绪状态；读不出来返回 None（查询路径，不建立档案）。

        `profile_id` 为 None 时读当前活动档案。启动目标必须按**目标档案自己**读：
        活动指针还没切过去时，拿活动档案的状态判断目标是否可用会答错人。
        """
        try:
            if profile_id is None:
                return self._config.status()
            service = ConfigService(
                self._data_root,
                credential_store=self._credentials,
                profile_id=profile_id,
            )
            return service.status()
        except ConfigServiceError:
            # 元数据故障等：不猜成「没有配置」，也不清任何东西。
            return None

    def _clear_startup_target(self, settings: DesktopSettings) -> None:
        """启动目标已被移除：清空目标并关掉机器人自动启动偏好（§8.1、D-142）。

        **保留** `launch_at_sign_in` 与它已建好的注册项 —— 用户自己的「登录时启动
        Light」选择不由某个档案的存亡决定。revision 冲突说明别的页面刚改过设置：
        不重试、不覆盖，本次只是不启动，下一次启动重新解析。
        """
        try:
            self._desktop_settings.update(
                settings.settings_revision,
                startup_profile_id=None,
                start_bot_on_launch=False,
            )
        except DesktopSettingsError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.startup_target_clear_failed",
                error=type(exc).__name__,
            )
            return
        log_event(
            self._logger,
            logging.INFO,
            "launcher.startup_target_cleared",
            status="ok",
        )

    def _watch_auto_start(self, operation_id: str) -> None:
        """静默自启动失败时给出一次可恢复入口（托盘可用时只进托盘）。"""
        while not self._quit.is_set():
            operation = self._manager.operation(operation_id)
            if operation is None:
                return
            if operation.state == OP_FAILED:
                if not self._quit.is_set():
                    self._open_entry_or_tray()
                return
            if operation.finished_at is not None:
                return
            self._quit.wait(0.1)

    # --- API --------------------------------------------------------------

    def _start_api(self) -> None:
        # 自己先绑定并 listen，再交给 uvicorn：端口在监听成功后才发布，
        # 不存在「先选端口、释放套接字、等它启动」的竞态（§9.4）。
        self._api_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._api_socket.bind(("127.0.0.1", 0))
        self._api_socket.listen(64)
        self._port = int(self._api_socket.getsockname()[1])
        self._api = LocalApi(
            instance_id=self._instance_id,
            data_root=self._data_root,
            config_service=self._config,
            manager=self._manager,
            status_service=self._status,
            events=self._events,
            sessions=self._sessions,
            credential_store=self._credentials,
            static_dir=Path(__file__).parent / "static",
            port=self._port,
            on_quit=self.request_quit,
            lifecycle_gate=self._lifecycle,
            desktop_settings=self._desktop_settings,
            startup_service=self._startup_service,
        )
        api = self._api  # 线程只认这个局部引用：stop() 会先把 self._api 置空
        self._api_thread = threading.Thread(
            target=lambda: self._serve_api(api), name="raricy-api", daemon=True
        )
        self._api_thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            server = self._server
            if server is not None and getattr(server, "started", False):
                break
            if not self._api_thread.is_alive():
                raise RuntimeError("api_server_failed")
            time.sleep(0.02)
        else:
            raise RuntimeError("api_server_timeout")
        self._write_runtime_metadata()

    def _serve_api(self, api: LocalApi) -> None:
        import uvicorn

        try:
            # PyInstaller windowed 入口没有 stdout/stderr。Uvicorn 的默认日志
            # 格式器会访问 sys.stdout.isatty()，从而在服务启动前直接抛错。
            config = uvicorn.Config(
                api.app,
                log_config=None,
                log_level="warning",
                access_log=False,
                server_header=False,
                date_header=False,
                lifespan="off",
            )
            self._server = uvicorn.Server(config)
            self._server.run(sockets=[self._api_socket])
        except OSError:
            # 套接字被关掉（正常退出路径）不算错误。
            if not self._quit.is_set():
                log_event(self._logger, logging.WARNING, "launcher.api_failed", error="OSError")
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.api_failed",
                error=type(exc).__name__,
            )

    # --- 激活管道 ---------------------------------------------------------

    def _handle_activation(self, data: bytes) -> bytes:
        try:
            command = activation.decode_request(data)
        except ActivationError as exc:
            return activation.encode_response(ok=False, error=str(exc))
        if command == "open_admin":
            log_event(
                self._logger,
                logging.INFO,
                "launcher.activate",
                status="ok",
                trace_id=self._instance_id,
            )
            return activation.encode_response(ok=True, url=self.entry_url())
        return activation.encode_response(ok=False, error="unknown_command")

    # --- Worker -----------------------------------------------------------

    def _build_spec(self, revision: int | None, run_id: str) -> WorkerSpec:
        """构造一次启动的完整输入：运行快照与凭据在配置锁内一次取得（§6.5）。"""
        saved = self._config.load_saved()
        if saved is None:
            raise ConfigServiceError("no_active_config")
        target = saved.revision if revision is None else revision
        launch = self._config.build_run_launch(target)
        return default_worker_spec(
            run_config=str(launch.config_path),
            config_dir=str(self._config.profile()),
            credentials=launch.credentials,
            instance_id=self._instance_id,
            run_id=run_id,
        )

    def _on_worker_event(self, name: str, fields: dict, level: str = "info") -> None:
        """把进程阶段与 Worker 上报折进事件缓冲（§12）。

        帧本身由管理器消费（它负责最近状态快照）；控制器只把事件转发出去，
        不再自己抽帧 —— 两边都抽会让快照永远空着（审查 I3）。
        """
        self._events.publish(name, level=level, **fields)

    # --- 元数据 -----------------------------------------------------------

    def _write_runtime_metadata(self) -> None:
        """发布运行元数据（端口已在监听）；它不是锁，只是信息（§9.4）。"""
        metadata = {
            "instance_id": self._instance_id,
            "pid": os.getpid(),
            "port": self._port,
            "protocol_version": activation.PROTOCOL_VERSION,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            directory = paths.runtime_dir(self._data_root)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / _RUNTIME_FILE).write_text(
                json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            # 元数据只是信息：写失败不影响控制面可用性。
            pass

    def _remove_runtime_metadata(self) -> None:
        try:
            (paths.runtime_dir(self._data_root) / _RUNTIME_FILE).unlink(missing_ok=True)
        except OSError:
            pass
