"""Launcher 数据根解析：路径由 Launcher 决定，不受快捷方式 cwd 影响。

Windows 默认 ``%LOCALAPPDATA%\\RaricyBotLight``（LIGHT_EDITION_DESIGN §13.1）；
其他平台仅保留边界，未完成平台验收前不作为发行承诺。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_data_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "RaricyBotLight"
        return Path.home() / "AppData" / "Local" / "RaricyBotLight"
    return Path.home() / ".local" / "share" / "raricy-bot-light"
