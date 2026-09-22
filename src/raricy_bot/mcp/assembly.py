"""完整版的 MCP 装配：McpManager 与各 feature 的适配器。

Light 发行包不包含本模块。App 不再知道 Provider/适配器怎么组装，只接收
`BotApp(mcp_manager_factory=build_mcp_manager)` 注入的结果（设计 §4.3）；
没有工厂时 App 使用 `raricy_bot.assembly.NoToolMcpManager`。
"""

from __future__ import annotations

from ..config import McpConfig
from ..redact import Redactor, SecretRegistry
from .adapters import build_adapters
from .runtime import McpManager


def build_mcp_manager(
    config: McpConfig, *, redactor: Redactor, registry: SecretRegistry
) -> McpManager:
    """装配各 feature 的适配器；Provider/Registry 本身保持通用。"""
    return McpManager(
        config,
        redactor=redactor,
        registry=registry,
        adapters=build_adapters(config),
    )
