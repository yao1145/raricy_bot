"""Light Worker 执行入口：加载运行快照、持锁、启动 BotApp，经 IPC 交互。

流程（LIGHT_EDITION_DESIGN §3.3、§10.1）：

1. 读 `--run-config`：Controller 在配置锁内生成的**只含非敏感字段**的运行快照；
2. 凭据只从子进程环境取（§7），与快照一起交给 `parse_config()` 走公共校验；
3. 再验证发行策略：Light 里 MCP 与定时发文必须关闭（§4.2，不能只靠界面没有开关）；
4. 打开 Store/Memory/归档**之前**取得数据档案锁（§9.5），拿不到就以退出码 4 结束；
5. 以 Light 形态装配 `BotApp`（没有工厂 → 无工具实现，§56），把控制通道的 `stop`
   帧与父端 EOF 都转换成 `app.stop()`；
6. 上报 `ready` / `status` / `log` 帧；原始 stdout/stderr 由父进程排空丢弃（§12）。

退出码：0 正常，1 运行期致命，2 配置错误，4 数据目录不可用或被占用。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from raricy_bot.app import BotApp
from raricy_bot.config import (
    LLM_API_KEY_ENV,
    PASSWORD_ENV,
    USERNAME_ENV,
    Config,
    ConfigError,
    Secrets,
    parse_config,
    read_config_yaml,
)
from raricy_bot.data_lock import DataLockError, acquire_data_lock, data_lock_dir
from raricy_bot.logging_setup import event_payload, get_logger, log_event

from . import ipc
from .process_manager import INSTANCE_ID_ENV, RUN_ID_ENV

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_DATA_LOCKED = 4

# 状态帧周期：状态是给人看的近期快照，不必更密（§10.2）。
_STATUS_INTERVAL_SECONDS: float = 5.0


class _Reporter:
    """向上报通道写帧；写失败只标记一次，绝不抛出（§10.1）。

    上报通道断掉不是 Worker 的致命错误：进程退出结果由父端的 `wait()` 判定。
    """

    def __init__(self, fd: int, *, instance_id: str, run_id: str) -> None:
        self._fd = fd
        self._instance_id = instance_id
        self._run_id = run_id
        self._seq = 0
        self._lock = threading.Lock()
        self._failed = False

    def report(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self._failed:
                return
            self._seq += 1
            try:
                frame = ipc.encode_frame(
                    kind,
                    instance_id=self._instance_id,
                    run_id=self._run_id,
                    seq=self._seq,
                    payload=payload,
                )
                os.write(self._fd, frame)
            except (ipc.IpcError, OSError):
                self._failed = True


class _LogBridge(logging.Handler):
    """把**已清洗**的事件镜像成上报帧（§12）。

    只转发 `log_event` 产生的事件：字段已经过白名单与类型校验，原始 stderr、
    异常正文与第三方日志一律不进帧。
    """

    def __init__(self, reporter: _Reporter) -> None:
        super().__init__()
        self._reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        payload = event_payload(record)
        if payload is None:
            return
        self._reporter.report(
            "log",
            {
                "event": payload.name,
                "level": record.levelname,
                "at": round(record.created, 3),
                "fields": dict(payload.as_mapping()),
            },
        )


def _install_log_bridge(reporter: _Reporter) -> None:
    bridge = _LogBridge(reporter)
    logging.getLogger("raricy").addHandler(bridge)
    # 根 logger 的级别保持在 INFO：事件白名单已经决定了哪些内容可对外。
    logging.getLogger("raricy").setLevel(logging.INFO)


def _env_secret(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"缺少环境变量 {name}", field=name, kind="missing")
    return value


def _load_run_config(path: str, *, config_dir: str | None) -> Config:
    """按公共校验解析运行快照；凭据只来自环境变量（§7）。"""
    raw = read_config_yaml(path)
    base = config_dir or str(Path(path).resolve().parent)
    config = parse_config(
        raw,
        config_dir=base,
        secrets=Secrets(
            username=_env_secret(USERNAME_ENV),
            password=_env_secret(PASSWORD_ENV),
            llm_api_key=_env_secret(LLM_API_KEY_ENV),
        ),
    )
    # 发行策略在装配层再验证一次：配置、装配、打包同时收口（§4.2）。
    if config.mcp.enabled or config.blog.enabled:
        raise ConfigError("Light 不支持该能力", field="mcp.enabled")
    return config


def _open_handle_fd(handle: int, mode: int) -> int:
    """把继承来的 Windows 句柄转成 CRT 文件描述符。

    控制与上报两条通道都要转：`os.read`/`os.write` 只认 CRT fd，直接拿原始
    HANDLE 调用会以 EBADF 失败 —— 而上报侧是「静默失败」的写法，父端因此永远
    等不到 ready（审查 C1）。
    """
    if sys.platform == "win32":
        import msvcrt

        return msvcrt.open_osfhandle(handle, mode)
    return handle


class _ControlReader:
    """控制通道读取线程：`stop` 帧或 EOF 都请求停止（§9.3、§10.1）。"""

    def __init__(
        self,
        fd: int,
        *,
        instance_id: str,
        run_id: str,
        on_stop,
        on_status,
    ) -> None:
        self._fd = fd
        self._instance_id = instance_id
        self._run_id = run_id
        self._on_stop = on_stop
        self._on_status = on_status
        self._buffer = b""
        self._thread = threading.Thread(
            target=self._loop, name="raricy-worker-control", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            try:
                chunk = os.read(self._fd, max(size - len(self._buffer), 1))
            except OSError:
                return b""
            if not chunk:
                return b""
            self._buffer += chunk
            if len(self._buffer) > ipc.MAX_PENDING_BYTES:
                return b""
        piece, self._buffer = self._buffer[:size], self._buffer[size:]
        return piece

    def _loop(self) -> None:
        try:
            while True:
                try:
                    frame = ipc.read_frame(
                        self._read,
                        allowed=ipc.COMMANDS,
                        expect_instance=self._instance_id,
                        expect_run=self._run_id,
                    )
                except ipc.IpcError:
                    # 协议违例：不再解释这条通道，按停止处理。
                    break
                if frame is None:
                    break
                if frame["kind"] == "stop":
                    break
                if frame["kind"] == "status_request":
                    self._on_status()
        finally:
            self._on_stop()


async def _status_loop(app: BotApp, reporter: _Reporter) -> None:
    while True:
        await asyncio.sleep(_STATUS_INTERVAL_SECONDS)
        reporter.report("status", {"status": app.status_snapshot()})


async def _serve(app: BotApp, reporter: _Reporter, control_fd: int) -> int:
    loop = asyncio.get_running_loop()
    stop_once = threading.Event()

    def request_stop() -> None:
        if stop_once.is_set():
            return
        stop_once.set()
        asyncio.run_coroutine_threadsafe(app.stop(), loop)

    def report_status() -> None:
        async def _snapshot() -> None:
            reporter.report("status", {"status": app.status_snapshot()})

        asyncio.run_coroutine_threadsafe(_snapshot(), loop)

    reader = _ControlReader(
        control_fd,
        instance_id=os.environ.get(INSTANCE_ID_ENV, ""),
        run_id=os.environ.get(RUN_ID_ENV, ""),
        on_stop=request_stop,
        on_status=report_status,
    )
    reader.start()

    try:
        await app.start()
    except Exception as exc:
        # 启动失败只报**类型**：异常正文可能带站点/模型响应（§12）。
        log_event(
            get_logger("launcher.worker"),
            logging.ERROR,
            "worker.start_failed",
            error=type(exc).__name__,
        )
        return EXIT_RUNTIME

    reporter.report("ready", {"status": app.status_snapshot()})
    status_task = asyncio.create_task(_status_loop(app, reporter))
    try:
        await app.run_forever()
    finally:
        status_task.cancel()
        await asyncio.gather(status_task, return_exceptions=True)
        # `stop()` 幂等且自带 10 秒预算（§9.3）：正常停止时它已经在跑。
        await app.stop()
    reporter.report("stopped", {"status": app.status_snapshot()})
    return EXIT_OK


def _run(config: Config, reporter: _Reporter, *, control_fd: int) -> int:
    app = BotApp(config)  # Light 形态：无工厂 → 无工具实现（§56）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_serve(app, reporter, control_fd))
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="raricy_launcher.worker_main")
    parser.add_argument("--run-config", required=True, help="Controller 生成的运行快照")
    parser.add_argument("--config-dir", default=None, help="相对路径基准（默认取快照所在目录）")
    parser.add_argument("--control-handle", type=int, required=True, help="继承的控制管道只读端")
    parser.add_argument("--report-handle", type=int, required=True, help="继承的上报管道只写端")
    args = parser.parse_args(argv)

    try:
        report_fd = _open_handle_fd(args.report_handle, os.O_WRONLY)
    except OSError:
        # 上报通道都打不开时不再继续：父端收不到任何状态，继续跑只会让用户看到超时。
        return EXIT_RUNTIME
    reporter = _Reporter(
        report_fd,
        instance_id=os.environ.get(INSTANCE_ID_ENV, ""),
        run_id=os.environ.get(RUN_ID_ENV, ""),
    )
    _install_log_bridge(reporter)
    logger = get_logger("launcher.worker")

    try:
        config = _load_run_config(args.run_config, config_dir=args.config_dir)
    except ConfigError as exc:
        reporter.report(
            "log",
            {
                "event": "worker.config_invalid",
                "level": "ERROR",
                "fields": {"reason": exc.kind, "field": exc.field or "config"},
            },
        )
        return EXIT_CONFIG
    except OSError:
        reporter.report(
            "log",
            {"event": "worker.config_unreadable", "level": "ERROR", "fields": {}},
        )
        return EXIT_CONFIG

    try:
        lock = acquire_data_lock(data_lock_dir(config.storage.db_path))
    except DataLockError as exc:
        reporter.report(
            "log",
            {
                "event": "worker.data_locked",
                "level": "ERROR",
                "fields": {"reason": str(exc)},
            },
        )
        return EXIT_DATA_LOCKED

    try:
        fd = _open_handle_fd(args.control_handle, os.O_RDONLY)
        try:
            return _run(config, reporter, control_fd=fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    except Exception as exc:
        log_event(logger, logging.CRITICAL, "worker.fatal", error=type(exc).__name__)
        return EXIT_RUNTIME
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
