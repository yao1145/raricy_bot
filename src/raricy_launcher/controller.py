"""Light 桌面控制面：单实例、配置事务、进程管理、本地 API、托盘与退出。

职责边界（LIGHT_EDITION_DESIGN §3.2）：

- **不做业务**：站点、模型、数据库与记忆都在 Worker 子进程里；
- **只做控制**：本机会话与 API、配置事务（§6）、凭据（§7）、进程状态机与 IPC
  （§9、§10）、事件缓冲与状态聚合（§12）、托盘命令端口（§61.2）。
- 长等待都在后台线程或后台操作里，HTTP 请求只返回 `operation_id`（§9.2）。

控制服务只监听回环，端口由操作系统分配；监听成功之后才发布运行元数据与
打开管理页（§9.4）。退出时先停 Worker（经私有控制管道请求优雅停止），再关
HTTP 服务、激活管道与互斥体（§9.3）。

托盘的启停命令**不经过 HTTP**：`Controller` 自己实现 `DesktopCommands`，与
`/api/bot/*` 走同一把生命周期门、同一个管理器、同一套稳定错误码，启停本身经
生命周期协调器（`lifecycle_service.LifecycleService`）委派；窗口回调只把结构化
命令投进 `TrayCoordinator` 的队列，耗时动作在托盘协调器线程里执行（§7.2、§61.2）。

协调器还负责跨操作的串行化与退出意图：`start()` 在迁移对账之后、自动启动之前
调用一次它的 `recover()`（只对账，不重放）；`request_quit()` 通知它关闭启动
入口，让在途切换按「停止意图优先」收敛（§5.1、§5.2、D-143）。
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
from .credential_lifecycle import CredentialLifecycle
from .credential_store import CredentialStore, SessionMemoryStore, SystemKeyringStore
from .desktop_settings import DesktopSettings, DesktopSettingsError, DesktopSettingsService
from .events import EventService
from .lifecycle_gate import LifecycleGate
from .lifecycle_service import LifecycleService
from .migration import MigrationResult, MigrationService
from .platform import (
    InstanceGuard,
    LauncherPlatform,
    PlatformError,
    TrayError,
    TrayIcon,
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
from .profile_removal import RemovalService
from .profile_service import ProfileService
from .session import SessionManager
from .startup_service import StartupService
from .status_service import StatusService
from .tray_model import TrayView
from .tray_service import CODE_CONFIG_NOT_READY, CODE_LIFECYCLE_BUSY, CODE_QUITTING
from .tray_service import TrayCommandError, TrayCoordinator
from .verification import VerificationStore

_RUNTIME_FILE = "launcher-runtime.json"

# 协调器的稳定码 → 托盘端口的稳定码（§61.2）。表外的一律按「配置不可用」报告：
# 托盘的启停只经协调器抛出配置/目录类故障，这一侧与 N3 的既有口径一致。
_TRAY_COMMAND_CODES: dict[str, str] = {
    CODE_CONFIG_NOT_READY: CODE_CONFIG_NOT_READY,
    CODE_LIFECYCLE_BUSY: CODE_LIFECYCLE_BUSY,
    CODE_QUITTING: CODE_QUITTING,
}


def _default_open_path(path: Path) -> None:
    """用系统 shell 打开一个目录；不支持的平台抛 OSError（降级路径记类别码）。"""
    opener = getattr(os, "startfile", None)
    if opener is None:
        raise OSError("open_path_unsupported")
    opener(str(path))


class _TraySurface:
    """把协调器的渲染转给托盘图标；图标在创建后挂上（工厂需要协调器的 submit）。

    协调器先构造、图标后创建：`on_message` 与 `surface` 互为对方的构造输入，
    这里用一个可后挂的转发槽解开这个环。挂上之前不允许启动协调器，因此不会
    丢掉任何一帧。
    """

    def __init__(self) -> None:
        self._icon: TrayIcon | None = None

    def attach(self, icon: TrayIcon) -> None:
        self._icon = icon

    def present(self, view: TrayView) -> None:
        icon = self._icon
        if icon is not None:
            icon.present(view)


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
        open_path: Callable[[Path], None] | None = None,
        startup_launch: bool = False,
        use_tray: bool = True,
        tray_factory: Callable[[Callable[[str], None]], TrayIcon] | None = None,
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
        # 打开诊断目录：默认交给系统 shell；非 Windows 明确不可用而不是静默失败。
        self._open_path = open_path or _default_open_path
        # 登录自启动来源提示（INTERFACES §59）：本任务只保存，自动运行解析在 N4 Task 5。
        self._startup_launch = startup_launch
        # 托盘装配（§61）：`use_tray=False` 是 `--no-tray`；工厂只服务测试注入。
        self._use_tray = use_tray
        self._tray_factory = tray_factory
        self._tray: TrayIcon | None = None
        self._coordinator: TrayCoordinator | None = None
        # 本次退出是否由注销/关机触发：只影响 launcher.quit 事件的 status（§61）。
        self._session_end = False
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
        # 凭据引用归属索引（N2 Task 2、D-144）：提交、清除与卡片查询共用同一个实例；
        # 与配置服务分开的写锁，调用顺序固定为「配置锁 → 生命周期锁」。
        self._credential_lifecycle = CredentialLifecycle(
            self._data_root, store=self._credentials
        )
        self._config = ConfigService(
            self._data_root,
            credential_store=self._credentials,
            profile_id=profile_id,
            credential_lifecycle=self._credential_lifecycle,
        )
        # 桌面偏好与登录启动项（§58、§59）：装配一次，API 与后续的自动运行解析共用；
        # 注册表适配器在这里惰性取得，测试用替身注入 `LocalApi`，不碰真实注册表。
        self._desktop_settings = DesktopSettingsService(self._data_root)
        self._startup_service = self._build_startup_service()
        # 档案级入口：控制面按档案取绑定服务（一次请求只解析一次档案上下文），
        # 状态聚合也经它读活动档案与代次（§4.1、D-135）。
        self._profiles = ProfileService(
            self._data_root, base_config_service=self._config
        )
        # 移除服务（N2 Task 3、D-145）：预览令牌只存内存，HTTP 入口与协调器的删除
        # 命令必须看到同一张表，所以只装配一个实例、两边共用。
        self._removal = RemovalService(
            self._data_root,
            profiles=self._profiles,
            credentials=self._credential_lifecycle,
            desktop_settings=self._desktop_settings,
            # 判据与状态聚合一致：进程句柄在场才算「这个档案在跑」（延迟取值，
            # 协调器在它之后才装配）。
            running_profile=lambda: (
                self._lifecycle_service.running_profile_id()
                if self._lifecycle_service is not None
                else None
            ),
        )
        # 一次性验证票据（N2 Task 4、D-146）：只存内存，`stop()` 里全部作废。
        self._verification = VerificationStore()
        # v1 迁移（§10）：服务在装配期构造，真正的迁移在 `start()` 的第一步跑；
        # 结果留在这里供 `_auto_start()` 判断（迁移未完成不自动运行机器人）。
        self._migration = MigrationService(
            self._data_root, config_service=self._config
        )
        self._migration_result: MigrationResult | None = None
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
        # 站点测试与启停共用的生命周期门：进程内单实例，随控制器一起装配
        # （§59、D-132）。互斥范围就是这个对象，所以只能有一个。
        self._lifecycle = LifecycleGate()
        # 跨操作的串行化、取消代次与 A→B 事务（§5.1、§5.2、D-143）：与上面的单次
        # 操作短租约是**两层**，共用同一个门对象（协调器不重写门、也不绕过门）。
        # 托盘命令经它委派，`start()` 与 `request_quit()` 也在下面接上它。
        self._lifecycle_service = LifecycleService(
            data_root=self._data_root,
            profiles=self._profiles,
            manager=self._manager,
            gate=self._lifecycle,
            events=self._events,
            logger=self._logger,
            # 阶段事件带目标档案（§59）：钩子只拿到 operation_id 与阶段码，
            # 档案由控制器按记录补上。
            stage_hook=self._on_lifecycle_stage,
            # 删除命令经同一个移除服务（令牌表只有一张）。
            removal=self._removal,
        )
        self._status = StatusService(
            instance_id=self._instance_id,
            config_service=self._config,
            manager=self._manager,
            profile_service=self._profiles,
            # 未完成操作优先取协调器：切换事务的阶段只有它知道（§5.2、§59）。
            lifecycle_service=self._lifecycle_service,
        )
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
        # 第一步是 v1 迁移：它在任何对外接口起来之前把数据根接管完（§10.6）。
        self._run_migration()
        # 第二步是协调器的启动对账（§5.2 故障表第 8 行）：必须在 `_auto_start()`
        # 之前完成，页面从接口可用那一刻读到的操作状态就已是已对账的。
        self._recover_lifecycle()
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
        """启动、装配托盘、按设计 §5.2 决定是否启动 Bot，然后阻塞到退出。"""
        self.start()
        try:
            # 托盘对象由当前线程的 `_start_tray()` 工厂创建；窗口与图标在下面
            # `_run_message_loop()` 的 `tray.run()` 里才建立，消息循环因此属于当前线程（§61）。
            self._start_tray()
            self._auto_start()
            self._ready.set()
            self._run_message_loop()
        finally:
            self.stop()

    def _run_message_loop(self) -> None:
        """有托盘就占住当前线程跑消息循环，无托盘就退到退出事件上等。"""
        tray = self._tray
        if tray is None:
            self._quit.wait()
            return
        try:
            tray.run()
        except (PlatformError, TrayError, OSError) as exc:
            # 窗口/图标建不起来也要继续运行：管理页仍是可见入口，这就是降级路径。
            self._report_tray_failure(exc)
            try:
                tray.close()  # 幂等；窗口本身已由 run() 的 finally 释放
            except Exception:
                # 兜底清理失败不改变结论：托盘已经不可用，继续走无托盘路径。
                pass
            self._stop_coordinator()
            self._quit.wait()

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
        # 拆机路径也要关闭协调器的启动入口（`stop()` 可以直接被调用、不经过
        # `request_quit()`）：先提高取消代次、置 `_quitting`，再回收 Worker（§5.1 第 1 条）。
        self._lifecycle_service.request_quit()
        self._manager.shutdown()
        # 停托盘协调器（有界等待），再关图标：窗口与图标的真正释放在拥有它的
        # 线程上完成（Task 4），这里只登记关闭意图。
        self._stop_coordinator()
        tray, self._tray = self._tray, None
        if tray is not None:
            try:
                tray.close()
            except Exception:
                # 收尾路径不因托盘释放失败而中断：Job 与互斥体仍要按序关闭。
                pass
        watcher, self._auto_start_watcher = self._auto_start_watcher, None
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=2)
        self._sessions.revoke_all()
        # 验证票据同样作废：进程退出后必须重新登录站点验证身份（§59、D-146）。
        self._verification.revoke_all()
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
            # 注销/关机触发的退出必须如实记为 session_end，不能写成优雅完成（§61）。
            status="session_end" if self._session_end else "ok",
            trace_id=self._instance_id,
        )
        # 最后一步才置空：API 线程已经退出，重复 stop() 由此走幂等早退。
        self._api = None

    def request_quit(self) -> None:
        """请求退出；通知生命周期协调器，有托盘就同时请它关闭消息循环。

        顺序（§5.1 第 1 条）：先通知协调器再关托盘 —— 退出意图一落，排队中的启动/
        重启/切换后的启动就再也追不上它（`submit()` 与 `start_bot()` 回 `quitting`），
        在途切换会在提交指针前后分别落 `cancelled_by_stop` / `selected_only`，两种情况
        都不再启动 Worker。线程安全、幂等。
        """
        self._quit.set()
        self._lifecycle_service.request_quit()
        tray = self._tray
        if tray is not None:
            tray.request_close()

    def _run_migration(self) -> None:
        """v1 无损接管（§10）：迁移是启动的第一步，失败/阻塞只记日志、继续启动 UI。

        迁移完全离线：不登录站点、不启动 Worker、不请求数据档案锁；单实例互斥体
        已由 `main.py` 在构造 Controller 之前取得（§10.1「停稳 Worker」）。结果
        如实留在 `self._migration_result` 里：`_auto_start()` 按它阻止自动运行，
        恢复态由 `status()` 报出（四类元数据故障 → `recovery`）。
        """
        try:
            result = self._migration.migrate()
        except Exception as exc:
            # 未归类的失败不阻断启动：管理页仍要能打开，日志只记异常类名。
            self._migration_result = None
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.migration",
                status="failed",
                error=type(exc).__name__,
            )
            return
        self._migration_result = result
        log_event(
            self._logger,
            logging.INFO if result.ok else logging.WARNING,
            "launcher.migration",
            status=result.stage,
            error=result.metadata_fault or result.error,
        )

    def _recover_lifecycle(self) -> None:
        """协调器的启动对账：把在途的 `activate` / `remove` / `credentials_clear` 记录
        标成 `interrupted` / `error="controller_restart"`（§5.2 故障表第 8 行）。

        **只对账，不重放**：不启动 Worker、不改活动指针、不清除任何东西；要不要继续
        由用户在看到操作记录后显式发起。对账失败不阻断控制面启动（管理页与恢复入口
        仍要能打开），只记一条可区分的日志；记录本身留在 `operations/` 里不动。
        """
        try:
            summary = self._lifecycle_service.recover()
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.lifecycle_recover",
                status="failed",
                error=type(exc).__name__,
            )
            return
        log_event(
            self._logger,
            logging.INFO,
            "launcher.lifecycle_recover",
            status="ok",
            count=len(summary["interrupted"]),
        )

    def _auto_start(self) -> None:
        """首次进入：按桌面偏好解析自动运行并决定是否启动（§5.2、§8、D-150）。

        解析顺序（INTERFACES §59 的唯一实现）：

        1. 读 `desktop.json`（桌面偏好的**唯一来源**，顺带完成升级用户的一次性导入）；
        2. 校验启动目标 —— 目标档案已被移除就清空目标并关掉机器人自动启动偏好，
           保留 `launch_at_sign_in` 与注册项；目标暂时不完整则保留目标与偏好；
        3. 把选中指针设为启动目标（`_select_startup_profile()` 经 N1 的
           `ProfileService.activate()` 提交；失败即本次不启动）；
        4. 只有「偏好开 + 目标可用 + 选中成功」才启动机器人。

        `--startup` 只是来源提示：授权偏好、档案状态与恢复记录一概重新读取，启停
        仍走 `LifecycleGate` 与 `WorkerManager` 的同一条路（§8.1）。任何一步读不
        出来都不猜：不启动、不改写现场，只给一次可见提示。
        """
        # v1 迁移门（§10.6）：迁移失败、被阻塞或抛错时不把半迁移的根目录当作
        # 干净安装，不启动机器人，只打开管理页让用户看到恢复入口。
        result = self._migration_result
        if result is None or not result.ok:
            self._open_url(self.entry_url())
            return
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
            self._start_bot(status.revision, self._profiles.active_profile_id())
            return
        try:
            target_removed = not self._profile_exists(target)
        except (OSError, ValueError) as exc:
            # 档案目录**读不到**（权限、被占用、数据根暂时不可用、布局损坏）：
            # 这不是「已移除」，不能触发清空目标与偏好的破坏性清理；保留现场，
            # 本次不启动，下一次启动重新判定（与 D-130 同口径）。
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.auto_start_skipped",
                status="target_unreadable",
                error=type(exc).__name__,
            )
            self._open_entry_or_tray()
            return
        if target_removed:
            # 目标**真的**已被移除（不存在/已删除）：清空目标并关掉机器人自动启动偏好。
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
        self._start_bot(status.revision, target)

    def _start_bot(self, revision: int | None, profile_id: str | None = None) -> None:
        """取生命周期租约后派发一次启动，并挂上静默失败监视（§9.2、D-132）。

        `profile_id` 在派发前固定一次（§5.2 的输入固定）：启动线程与规格工厂都不再
        从可变的活动指针推导目录，页面显示的档案与后台运行的档案因此不会分叉。
        """
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
            operation = self._manager.start(revision=revision, profile_id=profile_id)
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
        浏览器、不重复弹窗；托盘不可用（`--no-tray` 或装配失败，见
        `_tray_available()`）时按降级路径最多打开一次管理页，让用户仍看得到
        提示与恢复入口。
        """
        if self._startup_launch and self._tray_available():
            return
        self._open_url(self.entry_url())

    def _select_startup_profile(self, profile_id: str) -> bool:
        """把选中指针切到启动目标档案并发布新上下文（§5.2）。

        本次没有运行中的 Worker —— 自动启动发生在启动机器人**之前**（`run()` 里
        `_start_tray()` 之后、任何 `WorkerManager.start()` 之前），所以切换事务
        退化为「校验目标 → 提交选中指针 → 启动」，没有停机步骤；目标校验由调用方
        完成。返回 True 表示已确认选中的就是目标，只有这时才允许自动启动 ——
        页面显示 A 而后台运行 B 是不允许的。

        提交经 N1 的 `ProfileService.activate()`（提交指针 + 发布新上下文），
        **控制器不自行写 `launcher.json`，也不在这里实现事务**（§5.2 的事务归
        档案服务）。epoch 冲突或其它档案故障一律不重试、不覆盖，记一条可区分的
        日志状态后保持 fail-closed：本次不启动。
        """
        try:
            self._profiles.activate(profile_id)
        except ConfigServiceError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "launcher.startup_profile_select_failed",
                status="activate_failed",
                error=type(exc).__name__,
            )
            return False
        return True

    def _tray_available(self) -> bool:
        """登录启动时是否已有可见控制入口（N3 的托盘）。

        如实报告 `_tray` 是否装配成功，不看平台也不猜：`--no-tray` 与托盘创建
        失败（`_report_tray_failure()` 已把它置空）都返回 False，登录启动按降级
        路径「最多打开一次管理页」。顺序前提：`run()` 先 `_start_tray()` 再
        `_auto_start()`，所以自动运行解析读到的就是本次运行真实装起来的结果。
        """
        return self._tray is not None

    def _profile_exists(self, profile_id: str) -> bool:
        """目标档案目录是否真的**不存在**（查询路径：不建目录、不写指针）。

        只有 `FileNotFoundError` / `NotADirectoryError` 才算「已移除」；其余
        `OSError`（权限、被占用、暂时不可用的数据根）一律向上抛，由调用方按
        「读不到」处理并**保留**目标。不用 `Path.is_dir()`：它会把 `OSError` 吞成
        `False`，于是「读不到」会被当成「不存在」，触发清空目标与偏好的破坏性清理
        （与 D-130「读不到、读到了但不能用、不存在」三者严格分开同口径）。
        """
        try:
            paths.profile_dir(self._data_root, profile_id).stat()
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

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
            profile_service=self._profiles,
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
            # 账号 API 的依赖（N2 Task 4、§59）：协调器、移除服务（与协调器同一个
            # 实例）、凭据归属索引与只存内存的验证票据。
            lifecycle_service=self._lifecycle_service,
            removal_service=self._removal,
            credential_lifecycle=self._credential_lifecycle,
            verification_store=self._verification,
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

    # --- 托盘命令端口（§61.2） --------------------------------------------

    def start_bot(self) -> str:
        """托盘入口：经协调器启动活动档案的已保存版本，返回管理器 operation_id。

        协调器内部仍**先取生命周期门**再调管理器（`_dispatch_single()`，租约只覆盖
        派发本身），取不到门或切换事务在途就是 `lifecycle_busy`；没有已保存配置是
        `config_not_ready`（D-132、§59）。
        """
        return self._tray_command(self._lifecycle_service.start_bot)

    def stop_bot(self) -> str:
        """托盘入口：经协调器停止 Worker；不要求已保存配置（与 `/api/bot/stop` 一致）。

        停止也不受「有未完成的协调器操作」阻挡：它只提高取消代次（停止意图优先）。
        """
        return self._tray_command(self._lifecycle_service.stop_bot)

    def restart_bot(self) -> str:
        """托盘入口：经协调器重启到活动档案的已保存版本，返回管理器 operation_id。"""
        return self._tray_command(self._lifecycle_service.restart_bot)

    def _tray_command(self, action: Callable[[], str]) -> str:
        """协调器的稳定码 → 托盘端口的稳定码（§61.2）；成功原样返回 operation_id。

        协调器抛的 `ConfigServiceError`（含档案的 `ProfileError`）消息就是稳定码：
        表内的三种原样转出；其余档案/目录故障在托盘这一侧与「配置不可用」同一结果
        （真实原因由状态聚合的 `config.state` 承载），异常原文不进日志或页面事件。
        """
        try:
            return action()
        except ConfigServiceError as exc:
            raise TrayCommandError(
                _TRAY_COMMAND_CODES.get(str(exc), CODE_CONFIG_NOT_READY)
            ) from None

    def status_snapshot(self) -> dict:
        """完整状态快照；可能阻塞（读凭据库），退出流程开始后抛 `quitting`。"""
        if self._quit.is_set():
            raise TrayCommandError(CODE_QUITTING)
        return self._status.snapshot()

    def process_view(self) -> dict:
        """廉价视图：只读管理器内存，无 I/O（托盘 tick 用）。"""
        return {"process": self._manager.status(), "worker": self._manager.last_status}

    def diagnostics_dir(self) -> Path:
        """诊断日志目录（与 `diagnostics.install()` 同一个落点，§11）。"""
        return paths.diagnostics_dir(self._data_root)

    def begin_session_end(self) -> None:
        """注销/关机：拒绝新启动、请求退出，并把本次退出记为 `session_end`。"""
        self._session_end = True
        self._manager.begin_quit()
        self.request_quit()

    # --- 托盘装配 ---------------------------------------------------------

    def _start_tray(self) -> None:
        """在当前线程创建托盘与协调器；失败降级为「无托盘但继续运行」（§61）。"""
        if not self._use_tray:
            # `--no-tray`：明确要求不建托盘，只记一条信息，不是失败。
            log_event(self._logger, logging.INFO, "launcher.tray_disabled", status="ok")
            return
        surface = _TraySurface()
        coordinator = TrayCoordinator(
            surface=surface,
            commands=self,
            logger=self._logger,
            open_url=self._open_url,
            open_path=self._open_path,
            events=self._events,
        )
        factory = self._tray_factory or self._create_platform_tray
        try:
            tray = factory(coordinator.submit)
        except (PlatformError, TrayError, OSError) as exc:
            # 托盘建不起来不影响控制面：管理页仍会按 §8.1 打开（本轮可见的修复入口）。
            self._report_tray_failure(exc)
            return
        surface.attach(tray)
        self._tray = tray
        self._coordinator = coordinator
        coordinator.start()

    def _create_platform_tray(self, on_message: Callable[[str], None]) -> TrayIcon:
        """默认托盘工厂：图标资源取包目录下的 assets/（与 static/ 同法，§60）。"""
        return self._platform.create_tray(
            icon_dir=Path(__file__).parent / "assets", on_message=on_message
        )

    def _report_tray_failure(self, exc: Exception) -> None:
        """托盘不可用：日志与页面事件用同一个稳定码，然后继续运行。"""
        message = str(exc)
        stable = isinstance(exc, (PlatformError, TrayError)) and bool(message)
        error = message if stable else type(exc).__name__
        self._tray = None
        log_event(
            self._logger,
            logging.WARNING,
            "launcher.tray_failed",
            status="failed",
            error=error,
        )
        self._events.publish("launcher.tray_failed", level="warning", error=error)

    def _stop_coordinator(self) -> None:
        """停掉协调器线程并丢弃引用；有界等待，幂等。"""
        coordinator, self._coordinator = self._coordinator, None
        if coordinator is not None:
            coordinator.stop(timeout=2)

    # --- Worker -----------------------------------------------------------

    def _build_spec(
        self, revision: int | None, run_id: str, profile_id: str | None
    ) -> WorkerSpec:
        """构造一次启动的完整输入：运行快照与凭据在配置锁内一次取得（§6.5）。

        `profile_id` 是派发时固定下来的档案（§5.2 的输入固定）：用它的绑定实例
        取快照、凭据与目录，**不再**在启动线程里重新解析可变的活动指针 —— 否则
        切换档案的瞬间会起出一个「配置属于 A、目录已经指向 B」的 Worker。
        身份校验的期望值同样在这里固定：取该档案已绑定的稳定 ID 注入 Worker 环境；
        未验证身份的档案（v1 迁移来的）取到 `None`，不注入也不校验（§10.4）。
        """
        service = (
            self._config
            if profile_id is None
            else self._profiles.config_service(profile_id)
        )
        saved = service.load_saved()
        if saved is None:
            raise ConfigServiceError("no_active_config")
        target = saved.revision if revision is None else revision
        launch = service.build_run_launch(target)
        expected = (
            None
            if profile_id is None
            else self._profiles.expected_site_user_id(profile_id)
        )
        return default_worker_spec(
            run_config=str(launch.config_path),
            config_dir=str(service.profile()),
            credentials=launch.credentials,
            instance_id=self._instance_id,
            run_id=run_id,
            expected_site_user_id=expected,
        )

    def _on_worker_event(self, name: str, fields: dict, level: str = "info") -> None:
        """把进程阶段与 Worker 上报折进事件缓冲（§12），并标上事件归属（§59）。

        帧本身由管理器消费（它负责最近状态快照）；控制器只把事件转发出去，
        不再自己抽帧 —— 两边都抽会让快照永远空着（审查 I3）。

        归属取管理器**在途操作**的目标档案（`Operation.profile_id`，派发时就固定）：
        全局事件（没有在途操作）保持 `None`。帧里同名的字段先拿掉 —— 归属是控制器
        的事实，不由上游帧自报，也避免与显式关键字参数撞名。
        """
        operation = self._manager.current_operation()
        tagged = {key: value for key, value in fields.items() if key != "profile_id"}
        self._events.publish(
            name,
            level=level,
            profile_id=operation.profile_id if operation is not None else None,
            **tagged,
        )

    def _on_lifecycle_stage(self, operation_id: str, stage: str) -> None:
        """协调器的阶段事件（§59）：带目标档案，页面据此只显示当前账号的进度。"""
        record = self._lifecycle_service.operation(operation_id)
        self._events.publish(
            "launcher.lifecycle_stage",
            profile_id=record.profile_id if record is not None else None,
            kind=record.kind if record is not None else None,
            stage=stage,
            status=record.state if record is not None else None,
        )

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
