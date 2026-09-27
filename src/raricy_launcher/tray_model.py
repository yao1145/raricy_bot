"""托盘视图模型：把配置/进程/电源状态映射成图标、tooltip 与菜单（纯逻辑，§61）。

本模块是 Task 3 的协调器与 Task 4 的窗口层共用的**唯一**字符串来源：窗口层不认识
别的消息，未列入词表的命令与事件一律忽略。因此这里不 import win32、不 import
`platform/`、不做任何 I/O，只依赖 `texts` 与 `status_service` 的常量/纯函数，
全部判定都可以离线验证。

「需处理」（attention）只表示**需要用户动作**：配置要设置/修复，进程启动失败，
停止后确认是被强制结束，或睡眠恢复后还没有新上报。运行中但上报过期/缺失**不**算
需处理 —— 用户此刻无事可做，只能等 Worker 下一次上报，把图标变成警报只会制造假信号，
所以只改状态文案（运行中（状态过期）/ 运行中（暂无上报））。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import texts
from .status_service import FRESH, STALE, UNKNOWN

# 图标三态；与 `assets/` 下三个 ICO 文件名一一对应（§60）。
ICON_NORMAL: str = "normal"
ICON_STOPPED: str = "stopped"
ICON_ATTENTION: str = "attention"

# 菜单命令词表。窗口层只投递这些字符串，其余一律忽略。
TRAY_COMMAND_OPEN_ADMIN: str = "open_admin"
TRAY_COMMAND_START: str = "start"
TRAY_COMMAND_STOP: str = "stop"
TRAY_COMMAND_RESTART: str = "restart"
TRAY_COMMAND_OPEN_DIAGNOSTICS: str = "open_diagnostics"
TRAY_COMMAND_QUIT: str = "quit"

# 系统事件词表（Task 4 的窗口回调投递，Task 3 的协调器消费）。
TRAY_EVENT_TASKBAR_CREATED: str = "taskbar_created"
TRAY_EVENT_POWER_SUSPEND: str = "power_suspend"
TRAY_EVENT_POWER_RESUME: str = "power_resume"
TRAY_EVENT_SESSION_QUERY: str = "session_query"
TRAY_EVENT_SESSION_END: str = "session_end"

# 电源状态（协调器维护，不由本模块推算）。
POWER_ACTIVE: str = "active"
POWER_SUSPENDED: str = "suspended"
POWER_AWAITING_REPORT: str = "awaiting_report"

# 配置与进程状态字面量：与 ConfigService / WorkerManager 的稳定码一致（§59）。
# 本模块不 import 那两个模块（进程层带 win32，且要保证纯逻辑可离线测），只能复述；
# 改词表必须三处同步，见 §61。
CONFIG_STATE_NEEDS_SETUP: str = "needs_setup"
CONFIG_STATE_NEEDS_CREDENTIALS: str = "needs_credentials"
# N2 新增：有档案但一个都没选中（D-145）——需要用户去账号页选中一个。
CONFIG_STATE_NO_SELECTION: str = "no_selection"
CONFIG_STATE_CONFIGURED: str = "configured"
CONFIG_STATE_RECOVERY: str = "recovery"
CONFIG_STATE_INVALID: str = "invalid"

PROCESS_STATE_STOPPED: str = "stopped"
PROCESS_STATE_STARTING: str = "starting"
PROCESS_STATE_RUNNING: str = "running"
PROCESS_STATE_STOPPING: str = "stopping"
PROCESS_STATE_FAILED: str = "failed"

# 配置状态里需要用户动作的五种：图标升为 attention（§61 判定顺序第 1 条）。
_ATTENTION_CONFIG_STATES: frozenset[str] = frozenset(
    {
        CONFIG_STATE_NEEDS_SETUP,
        CONFIG_STATE_NEEDS_CREDENTIALS,
        CONFIG_STATE_NO_SELECTION,
        CONFIG_STATE_RECOVERY,
        CONFIG_STATE_INVALID,
    }
)

# tooltip 上限：`NOTIFYICONDATA.szTip` 是 128 个 UTF-16 字符，留一个给结尾的 NUL。
TOOLTIP_MAX_CHARS: int = 127


@dataclass(frozen=True)
class TrayState:
    """渲染托盘所需的全部输入；由协调器从稳定状态与系统事件组装。"""

    config_state: str  # ConfigService 的稳定状态字面量
    process_state: str  # WorkerManager 的稳定状态字面量
    worker_freshness: str  # snapshot_freshness() 的结果
    account: str | None
    forced_stop: bool
    power: str
    quitting: bool


@dataclass(frozen=True)
class TrayMenuItem:
    """一条菜单项。"""

    command: str
    label: str
    enabled: bool


@dataclass(frozen=True)
class TrayView:
    """一次要呈现给窗口层的完整视图。"""

    icon: str
    tooltip: str
    status_label: str
    menu: tuple[TrayMenuItem, ...]


def icon_for(state: TrayState) -> str:
    """图标判定：按 §61 的固定顺序取第一条命中，顺序即契约。"""
    if state.config_state in _ATTENTION_CONFIG_STATES:
        return ICON_ATTENTION
    if state.process_state == PROCESS_STATE_FAILED:
        return ICON_ATTENTION
    if state.process_state == PROCESS_STATE_STOPPED and state.forced_stop:
        return ICON_ATTENTION
    if state.power == POWER_AWAITING_REPORT:
        return ICON_ATTENTION
    if (
        state.quitting
        or state.process_state in (PROCESS_STATE_STOPPED, PROCESS_STATE_STOPPING)
        or state.power == POWER_SUSPENDED
    ):
        return ICON_STOPPED
    return ICON_NORMAL


def status_label(state: TrayState) -> str:
    """状态标签：同样按 §61 的固定顺序取第一条命中，文案全部来自 `texts`。"""
    if state.quitting:
        return texts.TRAY_STATUS_QUITTING
    if state.config_state == CONFIG_STATE_RECOVERY:
        return texts.TRAY_STATUS_RECOVERY
    if state.config_state == CONFIG_STATE_INVALID:
        return texts.TRAY_STATUS_INVALID
    if state.config_state == CONFIG_STATE_NEEDS_SETUP:
        return texts.TRAY_STATUS_NEEDS_SETUP
    if state.config_state == CONFIG_STATE_NEEDS_CREDENTIALS:
        return texts.TRAY_STATUS_NEEDS_CREDENTIALS
    if state.config_state == CONFIG_STATE_NO_SELECTION:
        return texts.TRAY_STATUS_NO_SELECTION
    if state.process_state == PROCESS_STATE_FAILED:
        return texts.TRAY_STATUS_FAILED
    if state.process_state == PROCESS_STATE_STOPPED and state.forced_stop:
        return texts.TRAY_STATUS_FORCED_STOP
    if state.power == POWER_SUSPENDED:
        return texts.TRAY_STATUS_SUSPENDED
    if state.power == POWER_AWAITING_REPORT:
        return texts.TRAY_STATUS_AWAITING_REPORT
    if state.process_state == PROCESS_STATE_STARTING:
        return texts.TRAY_STATUS_STARTING
    if state.process_state == PROCESS_STATE_STOPPING:
        return texts.TRAY_STATUS_STOPPING
    if state.process_state == PROCESS_STATE_RUNNING:
        if state.worker_freshness == STALE:
            return texts.TRAY_STATUS_RUNNING_STALE
        if state.worker_freshness == FRESH:
            return texts.TRAY_STATUS_RUNNING
        return texts.TRAY_STATUS_RUNNING_UNKNOWN
    return texts.TRAY_STATUS_STOPPED


def menu_for(state: TrayState) -> tuple[TrayMenuItem, ...]:
    """右键菜单只保留启动、重启、停止、退出（§61）。

    禁用只是交互提示，服务端仍然自己判：窗口层不因为菜单项禁用就跳过命令校验。
    """
    configured = state.config_state == CONFIG_STATE_CONFIGURED
    process = state.process_state
    can_start = (
        configured
        and process in (PROCESS_STATE_STOPPED, PROCESS_STATE_FAILED)
        and not state.quitting
    )
    can_stop = process in (PROCESS_STATE_RUNNING, PROCESS_STATE_STARTING) and not state.quitting
    can_restart = (
        configured
        and process in (PROCESS_STATE_RUNNING, PROCESS_STATE_STOPPED, PROCESS_STATE_FAILED)
        and not state.quitting
    )
    return (
        TrayMenuItem(TRAY_COMMAND_START, texts.TRAY_MENU_START, can_start),
        TrayMenuItem(TRAY_COMMAND_RESTART, texts.TRAY_MENU_RESTART, can_restart),
        TrayMenuItem(TRAY_COMMAND_STOP, texts.TRAY_MENU_STOP, can_stop),
        TrayMenuItem(TRAY_COMMAND_QUIT, texts.TRAY_MENU_QUIT, True),
    )


def build_view(state: TrayState) -> TrayView:
    """组装完整视图；tooltip 只含应用名与状态标签并截断到 127 字符。"""
    label = status_label(state)
    tooltip = texts.TRAY_TOOLTIP_FORMAT.format(app=texts.APP_NAME, status=label)
    return TrayView(
        icon=icon_for(state),
        tooltip=tooltip[:TOOLTIP_MAX_CHARS],
        status_label=label,
        menu=menu_for(state),
    )
