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
from .config_service import ConfigService, ConfigServiceError
from .credential_store import CredentialStore, SessionMemoryStore, SystemKeyringStore
from .events import EventService
from .platform import InstanceGuard, LauncherPlatform
from .process_manager import (
    START_TIMEOUT_SECONDS,
    STOP_BUDGET_MS,
    WorkerManager,
    WorkerSpec,
    default_worker_spec,
)
from .session import SessionManager
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
        self._clock = clock
        self._instance_id = uuid4().hex[:12]
        self._started_at = clock()
        self._quit = threading.Event()
        self._ready = threading.Event()

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
        self._api: LocalApi | None = None
        self._server = None
        self._api_thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()
        self._api_socket: socket.socket | None = None
        self._port = 0
        self._listener = None

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
        """首次进入：配置可用且偏好开启时静默启动；否则打开向导/修复页（§5.2）。"""
        try:
            status = self._config.status()
        except ConfigServiceError:
            status = None
        configured = status is not None and status.state == "configured"
        if configured and self._config.start_bot_on_launch():
            self._manager.start(revision=status.revision)
            return
        self._open_url(self.entry_url())

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
            time.sleep(0.02)
        self._write_runtime_metadata()

    def _serve_api(self, api: LocalApi) -> None:
        import uvicorn

        config = uvicorn.Config(
            api.app,
            log_level="warning",
            access_log=False,
            server_header=False,
            date_header=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        try:
            self._server.run(sockets=[self._api_socket])
        except OSError:
            # 套接字被关掉（正常退出路径）不算错误。
            pass

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
