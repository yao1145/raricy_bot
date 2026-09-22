"""L0 最小 Controller：单实例、静态页、假 Worker 启停、激活与退出。

这是平台原型的控制面，只证明 LIGHT_EDITION_DESIGN §16 L0 的四件事：
无终端入口可运行、二次启动激活已有实例、Job 回收有效、静态资源可打开。
正式的认证会话、配置事务、进程状态机与事件流在 L3 按 §8/§9/§11 重写；
本阶段的安全边界只有回环监听与激活管道 ACL，没有会话认证。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from raricy_bot.logging_setup import log_event

from . import __version__, activation, texts
from .activation import ActivationError
from .platform import InstanceGuard, LauncherPlatform
from .process_manager import WorkerProcess, default_worker_spec

# 原型停止预算：假 Worker 无清理工作，5 秒足够；L3 换成大于 Core 关闭预算的常量。
_STOP_BUDGET_MS = 5000

_RUNTIME_DIR = "runtime"
_RUNTIME_FILE = "launcher-runtime.json"


class Controller:
    """桌面 Controller 原型；所有长时间等待都不占用 HTTP 请求线程。"""

    def __init__(
        self,
        *,
        platform: LauncherPlatform,
        guard: InstanceGuard,
        data_root: Path,
        logger: logging.Logger,
        open_url: Callable[[str], None] | None = None,
    ) -> None:
        if not guard.owned():
            raise ValueError("guard_not_owned")
        self._platform = platform
        self._guard = guard
        self._data_root = Path(data_root)
        self._logger = logger
        self._open_url = open_url or (lambda url: webbrowser.open(url))
        self._instance_id = uuid4().hex[:12]
        self._started_at = time.monotonic()
        self._worker: WorkerProcess | None = None
        self._stopping: WorkerProcess | None = None
        self._worker_lock = threading.Lock()
        # 生命周期锁串行化 start/stop 整段操作；锁顺序固定为先生命周期后 worker。
        self._lifecycle_lock = threading.Lock()
        self._quit = threading.Event()
        self._ready = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._listener = None

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def admin_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("not_started")
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/"

    def wait_ready(self, timeout: float) -> bool:
        """等待 run() 完成启动与首开页面；供调用方与测试对齐时序。"""
        return self._ready.wait(timeout)

    # --- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        """绑定回环端口、发布运行元数据、启动激活管道与 HTTP 服务。"""
        static_index = (Path(__file__).parent / "static" / "index.html").read_bytes()
        handler = _make_handler(self, static_index)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        # 端口在监听成功后才发布（§9.4）；元数据不是锁，只是信息。
        self._write_runtime_metadata()
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever, name="raricy-http", daemon=True
        )
        self._http_thread.start()
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
        """启动并阻塞到退出请求；返回时一切已回收。"""
        self.start()
        try:
            self._open_url(self.admin_url)
            self._ready.set()
            self._quit.wait()
        finally:
            self.stop()

    def stop(self) -> None:
        """停止 Worker、HTTP 服务与激活管道，释放互斥体；幂等。"""
        self._quit.set()
        self._stop_worker()
        if self._httpd is not None:
            httpd, self._httpd = self._httpd, None
            httpd.shutdown()
            httpd.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=2)
            self._http_thread = None
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

    # --- 激活管道 -----------------------------------------------------------

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
            return activation.encode_response(ok=True, url=self.admin_url)
        return activation.encode_response(ok=False, error="unknown_command")

    # --- Worker 管理（原型状态：stopped/running/stopping/failed） --------------

    def _worker_state(self) -> str:
        with self._worker_lock:
            if self._stopping is not None:
                return "stopping"
            worker = self._worker
            if worker is None:
                return "stopped"
            exit_code = worker.wait(0)
            if exit_code is None:
                return "running"
            worker.close()
            self._worker = None
            log_event(
                self._logger,
                logging.INFO,
                "worker.exit",
                exit_code=str(exit_code),
                trace_id=self._instance_id,
            )
            # 原型简化：exit 0 视为已停止，其余为故障；L3 换完整状态机。
            return "stopped" if exit_code == 0 else "failed"

    def start_worker(self) -> str:
        # 与在途停止串行：确认旧进程退出并关闭后才允许创建新进程（§9.2）。
        with self._lifecycle_lock:
            # 退出标志必须在锁内判定：stop() 回收旧 Worker 后会释放生命周期锁，
            # 此时它还要关闭 HTTP 服务；该窗口内到达的 start 若放行，就会在
            # stop() 返回后留下一个无人回收的新进程。
            if self._quit.is_set():
                log_event(
                    self._logger,
                    logging.INFO,
                    "worker.spawn",
                    status="refused",
                    reason="quitting",
                    trace_id=self._instance_id,
                )
                return "stopped"
            with self._worker_lock:
                if self._worker is not None and self._worker.wait(0) is None:
                    return "running"  # 重复 start 返回当前状态，不并行创建
                if self._worker is not None:
                    self._worker.close()
                    self._worker = None
                self._worker = WorkerProcess(self._platform, default_worker_spec())
            log_event(
                self._logger,
                logging.INFO,
                "worker.spawn",
                status="ok",
                trace_id=self._instance_id,
            )
            return "running"

    def _stop_worker(self) -> None:
        with self._lifecycle_lock:
            with self._worker_lock:
                worker = self._worker
                if worker is None:
                    return
                # 排空期间保留引用并暴露 stopping：确认退出前既不向状态查询
                # 报告 stopped，也不放行新的 start。
                self._stopping = worker
            try:
                worker.request_stop()
                if worker.wait(_STOP_BUDGET_MS) is None:
                    worker.terminate()
                    worker.wait(_STOP_BUDGET_MS)
            finally:
                worker.close()
                with self._worker_lock:
                    self._stopping = None
                    self._worker = None

    def stop_worker(self) -> str:
        self._stop_worker()
        return "stopped"

    # --- 状态与元数据 ---------------------------------------------------------

    def status(self) -> dict:
        return {
            "instance_id": self._instance_id,
            "version": __version__,
            "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            "worker": {"state": self._worker_state()},
        }

    def request_quit(self) -> None:
        self._quit.set()

    def _write_runtime_metadata(self) -> None:
        metadata = {
            "instance_id": self._instance_id,
            "pid": os.getpid(),
            "port": self._httpd.server_address[1],
            "protocol_version": activation.PROTOCOL_VERSION,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            directory = self._data_root / _RUNTIME_DIR
            directory.mkdir(parents=True, exist_ok=True)
            (directory / _RUNTIME_FILE).write_text(
                json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            # 元数据只是信息：写失败不影响控制面可用性。
            pass

    def _remove_runtime_metadata(self) -> None:
        try:
            (self._data_root / _RUNTIME_DIR / _RUNTIME_FILE).unlink(missing_ok=True)
        except OSError:
            pass


# --- HTTP 原型面（L3 替换为认证 API） -----------------------------------------


def _make_handler(controller: Controller, static_index: bytes):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            # 访问日志禁记 query/body；原型阶段整体静默（§12）。
            return

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send_html(static_index)
            elif self.path == "/api/status":
                self._send_json(200, controller.status())
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 4096))
            if self.path == "/api/bot/start":
                state = controller.start_worker()
                self._send_json(200, {"ok": True, "worker": {"state": state}})
            elif self.path == "/api/bot/stop":
                state = controller.stop_worker()
                self._send_json(200, {"ok": True, "worker": {"state": state}})
            elif self.path == "/api/launcher/quit":
                self._send_json(200, {"ok": True, "message": texts.QUIT_ACKNOWLEDGED})
                controller.request_quit()
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

    return _Handler
