"""Launcher 的固定文案。

与站内 Bot 的 `raricy_bot.texts` 分开归属：这里只放桌面入口、激活与
本地管理页要用的字符串。语气约定与核心一致：说明发生了什么、用户
接下来可以怎么做；不使用表情符号。
"""

from __future__ import annotations

APP_NAME: str = "Raricy Bot Light"

# 桌面入口分派（main.py）。
ENTRY_ACTIVATED: str = "检测到正在运行的实例，正在打开管理页。"
ENTRY_ACTIVATE_FAILED: str = "检测到已有实例在运行，但无法连接；请稍后再试。"
ENTRY_UNSUPPORTED_PLATFORM: str = "当前平台暂不支持运行 Light 桌面版。"
ENTRY_INTERNAL_ERROR: str = "启动失败，请查看诊断日志后重试。"

# 管理页原型（L0）的退出结果。
QUIT_ACKNOWLEDGED: str = "已退出，可以关闭本页。"

# 凭据删除（F1）：N2 交付完整清除前，配置页与接口都只支持保持不变/替换。
# 接口用这条固定文案说明「暂不支持」及当下的替代做法，避免把它读成「缺凭据」。
CREDENTIAL_DELETE_UNAVAILABLE: str = (
    "当前版本暂不支持删除已保存的凭据，清除功能将在后续版本提供；"
    "需要立即撤销时，请在 Windows「凭据管理器」中删除 RaricyBotLight 的条目。"
)

# 托盘菜单（N3）：顺序与可用性由 tray_model 固定，文案只在这里维护。
TRAY_MENU_OPEN_ADMIN: str = "打开管理页"
TRAY_MENU_START: str = "启动机器人"
TRAY_MENU_STOP: str = "停止机器人"
TRAY_MENU_RESTART: str = "重启机器人"
TRAY_MENU_OPEN_DIAGNOSTICS: str = "打开诊断目录"
TRAY_MENU_QUIT: str = "退出 Light"

# 托盘的账号行与状态行（展示行，不可点击）。
TRAY_ACCOUNT_PREFIX: str = "账号："
TRAY_ACCOUNT_UNSET: str = "未设置"
TRAY_STATUS_PREFIX: str = "状态："

# 托盘状态标签：同一时刻只显示一条，判定顺序见 tray_model.status_label()。
TRAY_STATUS_NEEDS_SETUP: str = "未设置账号"
TRAY_STATUS_NEEDS_CREDENTIALS: str = "需重新填写凭据"
TRAY_STATUS_RECOVERY: str = "配置需要修复"
TRAY_STATUS_INVALID: str = "配置不可用"
TRAY_STATUS_STOPPED: str = "已停止"
TRAY_STATUS_STARTING: str = "启动中"
TRAY_STATUS_RUNNING: str = "运行中"
TRAY_STATUS_STOPPING: str = "正在停止"
TRAY_STATUS_FAILED: str = "启动失败"
TRAY_STATUS_FORCED_STOP: str = "上次运行被强制结束"
TRAY_STATUS_RUNNING_STALE: str = "运行中（状态过期）"
TRAY_STATUS_RUNNING_UNKNOWN: str = "运行中（暂无上报）"
TRAY_STATUS_SUSPENDED: str = "睡眠中（未在线）"
TRAY_STATUS_AWAITING_REPORT: str = "已恢复，等待新上报"
TRAY_STATUS_QUITTING: str = "正在退出"

# tooltip 只由应用名与状态标签组成，不含账号、pid、路径或原始错误（§61）。
TRAY_TOOLTIP_FORMAT: str = "{app} - {status}"
