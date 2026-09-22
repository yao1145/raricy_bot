"""Raricy Bot Light 桌面发行版的 Launcher / Controller 包。

只承载本机控制面：单实例激活、配置事务、凭据管理、Worker 子进程控制与
受限状态事件。业务模块不 import 本包；本包只通过核心公开生命周期与受限
接缝使用 `raricy_bot`。见 docs/design/LIGHT_EDITION_DESIGN.md §3。
"""

from __future__ import annotations

__version__ = "0.1.0"
