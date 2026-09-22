"""完整版的发文装配：BlogService 及其 scope/writer/publisher 的构造。

Light 发行包不包含本模块。App 只接收 `BotApp(blog_service_factory=...)` 注入
的结果；`blog.enabled=true` 而没有工厂时 App 在构造期报 `AssemblyError`
（设计 §4.2）—— 配置要求发文、装配却没有发文实现，静默跳过等于悄悄少了一个
用户配置过的功能。
"""

from __future__ import annotations

import asyncio
import random as random_module
import time
from collections.abc import Awaitable, Callable

from ..config import Config
from ..core.worker import ModelClient
from ..redact import Redactor
from ..site.client import SiteClient
from ..site.models import Author
from ..store import Store
from .models import BlogScope
from .publisher import BlogPublisher
from .service import BlogService
from .writer import BlogWriter


def build_blog_service(
    *,
    config: Config,
    user: Author,
    store: Store,
    client: SiteClient,
    model: ModelClient | None,
    mcp_registry: object,
    model_gate: asyncio.Semaphore,
    redactor: Redactor,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random: Callable[[], float] = random_module.random,
) -> BlogService:
    """构造发文子域（§53.11 / §53.12）：共享 App 的客户端、Store、模型门与 Registry。

    **不在这里创建或持有** SiteClient、模型客户端的关闭权限 —— 它们属于 App，
    子域只是借用。工具预算不新增配置项：`BlogWriter` 从 `mcp.features.blog_write`
    取 `max_tool_calls_per_turn`（§8.1、D-110）。
    """
    scope = BlogScope(site_base_url=config.site.base_url, self_user_id=user.id)
    writer = BlogWriter(
        model=model,
        registry=mcp_registry,
        feature=config.mcp.features.get("blog_write"),
        mcp_enabled=config.mcp.enabled,
        max_input_tokens=config.behavior.context_input_tokens,
        model_gate=model_gate,
    )
    publisher = BlogPublisher(
        config=config,
        scope=scope,
        store=store,
        client=client,
        clock=clock,
    )
    return BlogService(
        config=config,
        scope=scope,
        store=store,
        writer=writer,
        publisher=publisher,
        redactor=redactor,
        clock=clock,
        sleep=sleep,
        random=random,
    )
