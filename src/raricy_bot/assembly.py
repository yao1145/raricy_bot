"""装配接缝：App 与发行版专属子系统的交界，以及无工具默认实现。

完整版经 `BotApp(mcp_manager_factory=..., blog_service_factory=...)` 把 `mcp/`
与 `blog/` 的构造注入 App；Light 不打包这两个包，App 落到这里的**无工具**
实现 —— 行为与 `mcp.enabled=false` 的真实 Manager 一致：启动/停止是空操作，
Registry 的每个 feature 都不可用（设计 §4.2、§4.3）。

本模块保持中立：只用标准库类型，不认识 MCP SDK、工具 schema 或发文实现。
下面的协议只声明 App **真正用到**的成员，不复制完整版接口。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class AssemblyError(Exception):
    """装配接缝的固定错误；消息是稳定类别码。

    配置要求某个专属子域、却没有注入对应工厂时抛出。这类错位必须在**构造期**
    报错：落进 `start()` 的软故障兜底里被吞掉，等于把一个配置好的能力静默变没。
    """


class ToolRegistryLike(Protocol):
    """App 用到的 MCP 工具注册表视图。"""

    def feature_available(self, feature: str) -> bool: ...

    def tools_for(self, feature: str) -> tuple[Any, ...]: ...

    async def execute(
        self,
        feature: str,
        call: Any,
        *,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> Any: ...


class McpManagerLike(Protocol):
    """App 用到的 MCP Manager 视图：生命周期与注册表。"""

    registry: ToolRegistryLike

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class BlogServiceLike(Protocol):
    """App 用到的发文子域视图：只有生命周期，内部任务不外露。"""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class NoToolRegistry:
    """无工具注册表：任何 feature 都不可用，与 MCP 关闭时的真实注册表同形。"""

    def feature_available(self, feature: str) -> bool:
        return False

    def tools_for(self, feature: str) -> tuple[Any, ...]:
        return ()

    async def execute(
        self,
        feature: str,
        call: Any,
        *,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> Any:
        # 能力门在 feature_available 处已经判否；正常路径到不了这里。
        raise AssemblyError("no_tools")


class NoToolMcpManager:
    """无工具 Manager：生命周期为空操作，注册表恒不可用。"""

    def __init__(self) -> None:
        self.registry = NoToolRegistry()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None
