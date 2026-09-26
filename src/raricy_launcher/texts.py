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

# 登录启动（N4 §8、§59）：错误响应按既有 ApiError(message=...) 机制带这些固定文案。
STARTUP_COMMAND_TOO_LONG: str = (
    "启动项命令超过 260 个字符，Windows 的启动项容纳不下；"
    "请把应用目录移到更短的路径后重试。"
)
STARTUP_PATH_UNUSABLE: str = (
    "当前形态或程序路径不能用于登录启动（开发形态、路径不存在或不可用）；"
    "请使用安装版，并确认程序文件仍在原位置。"
)
STARTUP_REGISTRATION_CONFLICT: str = (
    "注册表里同名的启动项不是本程序写入的；为避免破坏其他应用，"
    "程序不会覆盖或删除它，请先在系统的「启动应用」列表里确认该项来源。"
)
STARTUP_APPLY_FAILED: str = (
    "启动项操作没有完成：可能是权限或系统策略拒绝，也可能暂时读不到注册表；"
    "桌面偏好保持不变，可稍后重试。"
)
DESKTOP_SETTING_MOVED: str = (
    "「打开程序时启动机器人」已移到「桌面」页的桌面设置；请在那一页修改。"
)
