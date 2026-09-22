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
