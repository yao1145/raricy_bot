"""应用装配：把配置、站点客户端、SSE、路由、工作器、发送器与运维端点接起来。

组件本身的逻辑都在各自模块里，本模块只负责**编排**（INTERFACES §16）：

- 启动顺序：`Store.open` → `SiteClient.start` + `login` → 构造 `SSEReceiver`
  → **用 `store.watermark()` 播种 `Last-Event-ID`（D-16）** → `WorkerPool.start`
  → `OpsServer.start` → `sse.run()` 作为后台 task；
- 长期记忆的装配位置（§34.2）：在 Store 崩溃恢复与主模型构造**之后**、聊天 worker 与评论服务
  启动**之前**——`MemoryService.start` → 构造 `MemoryWriter` / `MemoryController` → 启动记忆
  worker → 构造 Router 时注入 access policy、memory queue 与 `private_enabled` 回调 →
  构造 CommentRouter / CommentService 时注入 access policy 与只读 context provider（第 5 步）；
  `memory.enabled=false` 时整段跳过，不建目录也不注入任何能力（D-60）；
  **注入的判据是记忆 worker 真的起来了（`_memory_armed`）**，不是配置里的 `enabled`：
  一个没有消费者的队列会把记忆命令卡在水位之外（§34.3、D-15）；
- 路由结果分派：`reply_now` 用 `notice_local`、`busy` 用 `notice` 并加 (频道, 触发者) 冷却；
  `memory_queued` 什么都不做（终态由记忆 worker 负责，§34.3）；
- 自动提取（§34.4）只在主模型给出回答之后、回答发出之前尝试一次：全部前置条件同时成立
  才交给 `MemoryController.auto_capture`，成功写入时把确定性披露拼在回答末尾（并为它预留
  输出空间，D-63），失败一律原样发出；披露绝不进历史；
- 公开个人记忆（§47.4）：记忆路径真的装好之后再单独装配 `PublicMemorySubjectResolver`；
  聊天轮在内容引用、博客、`/kb`、`pending_user` 与近期块的预算判定**全部确定之后**才解析
  subject，只用 S1 里的大区近期消息，公开候选与共同/私有候选合并成一组交给 `ContextManager`；
  DM 不解析、`memory_allowed=false` 不解析，任何失败都只让本轮少一份可选资料（§47.5）；
- 模型失败的 `failure` 通知同样用 `notice` + 触发者冷却；额度用尽补发一次 `quota` 通知；
- 403 进入不可用状态，按 `ready_probe_seconds` 探测恢复（D-4）；
- 定时发文的装配位置（§53.12）：在所有账号、Store、模型、MCP 都就绪**之后**，
  共享 `_model_gate` 与 Registry；`blog.enabled=false` 时整段跳过，不构造也不起后台任务，
  因此既有部署升级后行为逐字节不变。它的失败只停自己，不影响聊天/评论与健康判定；
- 优雅关闭总超时 10 秒，`stop()` 可重复调用；记忆 worker 必须早于模型客户端关闭（§34.2），
  而**发文子域最先停** —— 它同时用 MCP 与模型，晚停就会在已经关掉的客户端上发起投递。

日志只写白名单字段，绝不写正文、Cookie、密码或 API Key（§19 红线）；
记忆日志只允许 §37 的九个事件名与白名单字段。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from . import texts
from .assembly import (
    AssemblyError,
    BlogServiceLike,
    McpManagerLike,
    NoToolMcpManager,
)
from .capabilities import CAPABILITIES, Capability
from .comments.quota import CommentQuotaGuard
from .comments.router import CommentRouter
from .comments.sender import CommentSender
from .comments.service import CommentMemoryInputs, CommentService
from .config import Config
from .core.blog import BlogLoad, BlogLoader, blog_marker, blog_readable
from .core.content_refs import ContentRefResolver, find_refs
from .core.context import (
    ContextManager,
    SupplementalCap,
    SupplementalItem,
    lobby_thread_session_key,
    speaker_wrapper,
)
from .core.lobby_context import (
    LobbyRecentContextBuffer,
    LobbyRecentMessage,
    render_lobby_recent,
)
from .core.router import MessageRouter, Request, RouteResult
from .core.scheduler import SessionScheduler
from .core.sender import MessageSender, SendResult
from .core.vision import ImageLoader, attach_image, with_image_marker
from .core.worker import (
    ModelClient,
    ModelError,
    OpenAIModelClient,
    ToolGenerationCancelled,
    WorkerPool,
)
from .diag import describe_error
from .kb.service import KnowledgeService
from .logging_setup import (
    get_logger,
    log_event,
    observe_task,
    secret_registry,
)
from .memory.access import MemoryAccessPolicy
from .memory.commands import MemoryCommandRequest
from .memory.controller import MemoryController
from .memory.models import STATUS_OK, ProposalAction, PublicMemorySubject
from .memory.service import MemoryService
from .memory.subjects import PublicMemoryInputs, PublicMemorySubjectResolver
from .memory.writer import MemoryWriter
from .ops import OpsServer
from .outbound import prepare_outbound
from .quota import QuotaGuard, notice_cooldown_key
from .redact import Redactor
from .site.client import SiteClient, SiteError
from .site.models import LOBBY, ChatMessage
from .site.sse import SSEReceiver
from .stickers import StickerTable
from .store import Store
from .text_utils import has_media, truncate_at_paragraph

_logger = get_logger("app")

# 被引用消息的三种边角标记。它们是**模型可见**的文本，不是给用户看的文案，
# 所以不进 texts.py（那里放的是用户可见的回复）。
_REPLY_IMAGE_MARKER: str = "[图片]"
# 被引用消息本来有图但没取到：与 `with_image_marker` 的 `[图片未提供]` 同一句话。
_REPLY_IMAGE_NOT_PROVIDED: str = "[图片未提供]"
_REPLY_DELETED_MARKER: str = "[该消息已删除]"
_REPLY_EMPTY_MARKER: str = "[无正文]"

# 优雅关闭总超时（秒）；超时后不再等待，交由进程退出兜底。
_SHUTDOWN_TIMEOUT_SECONDS: float = 10.0


def _reply_image_url(request: Request) -> str | None:
    """本轮直接引用里那张可以取的缩略图 URL；没有、已删除或没带图时为 None（D-48）。

    这是「这一轮要不要去取被引用的图」的**唯一**定义：`_load_reply_image` 按它取图，
    自动提取的「不依赖资料」判定按它跳过（§34.4）。两处必须同时移动 —— 否则提取会开始
    对一轮真的取了图的回合动手。
    """
    reply = request.message.reply
    if reply is None or reply.is_deleted or not reply.image_url:
        return None
    return reply.image_url


def _ref_source_texts(request: Request) -> tuple[str, str | None]:
    """本轮会做 `[@<ID>]` 内容引用展开的两块正文（§25）：消息正文与直接引用的正文。

    展开器（`_resolve_refs`）与自动提取的「不依赖资料」判定共用这一处定义：哪两块文本算
    「本轮依赖的资料」只写一次，将来多展开一块时两边一起动。
    """
    if request.reply_context:
        return request.user_text, request.reply_context
    return request.user_text, None


class CapabilityUnavailable(Exception):
    """能力授权前的本地门判否，并携带可判别的稳定原因码。

    它不是 ``ModelError``：模型压根没被调用，且用户侧文案按能力取自能力表、只有原因不同。
    用独立的异常类型可以让"我们自己的门"和"模型端点说它不支持 tools"
    在日志里各记一行、互不重复。
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _comment_blog_text(inputs: CommentMemoryInputs) -> str | None:
    """评论侧的 `PublicMemoryInputs.blog_text`（§48.2）：标题 + **实际提供**的正文。

    超限被省略的正文不在 `article_text` 里（`None`），因此这里只剩标题；两种情况都没
    内容时返回 `None`，解析器于是也看不到任何文章文本（公开设计 §6.3、§15）。
    """
    title = inputs.article_title
    body = inputs.article_text
    if body:
        return f"{title}\n{body}" if title else body
    return title or None


class _CommentMemoryAccess:
    """评论侧共同记忆的门禁载体：只承载 Router 已经判过的那一个布尔（§35、D-56）。

    `CommentRouter` 在还持有 `CommentNode.author.id` 时用真实策略算完 `permits_common`，
    而请求对象**刻意不带作者 ID**，因此作者身份过不了路由器 —— 下游只剩
    `memory_allowed`。若在取用时拿 `None` 当作者去向 allowlist 策略复问一遍，结果恒为假，
    等于把一条已经批准过的读取再错判一次（§28 对空作者 ID 的规定说的是这种情况）。

    所以这里只回答「共同记忆可读」，并且：
    - 只有 `CommentService` 在 `memory_allowed` 为真时才调用它（唯一调用点）；
    - 私有一律 False（纵深防御：`context_for` 本来也只在 DM 才问私有）；
    - 频道由 provider 钉死为 `comment`，作用域因此永远只有 `all_user`（§30.2 的表）。
    """

    def permits_common(self, user_id: str | None) -> bool:
        return True

    def permits_private(self, user_id: str | None, channel_kind: str) -> bool:
        return False


class BotApp:
    """一个机器人实例的完整装配与生命周期。"""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        model_client: ModelClient | None = None,
        model_client_cls: type[OpenAIModelClient] | None = None,
        mcp_manager: McpManagerLike | None = None,
        mcp_manager_factory: Callable[..., McpManagerLike] | None = None,
        blog_service_factory: Callable[..., BlogServiceLike] | None = None,
        knowledge_service: KnowledgeService | None = None,
        memory_service: MemoryService | None = None,
        memory_writer: MemoryWriter | None = None,
        memory_controller: MemoryController | None = None,
        archive: Any | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        # 永久归档由 `__main__` 装配（它要在 BotApp 之前决定"打不开就退出"），
        # 这里只保留一个只读引用供本地健康检查用。
        self._archive = archive
        # 图片输入：默认关闭。关闭时 worker 完全不碰 ImageLoader，
        # 进程里也就没有任何新增的图床请求（设计 §3.5）。
        self._vision_enabled = config.model.vision_enabled

        # 只登记真正的机密：密码与模型 Key；机器人用户名不是机密（redact.py §3）。
        # 走上进程级登记中心而不是直接构造：出站 Redactor 订阅同一份凭据表后，
        # 站点 Cookie、MCP 各槽位 Key 与 SSE 令牌都只需登记一次就两侧同时生效
        # （计划 §3.1）。登记发生在网络客户端与 MCP 初始化**之前**。
        self._registry = secret_registry()
        self._redactor = Redactor()
        self._registry.attach(self._redactor)
        self._registry.register(config.secrets.password)
        self._registry.register(config.secrets.llm_api_key)

        self._store = Store(
            config.storage.db_path,
            wal_journal_limit_bytes=config.storage.wal_journal_limit_bytes,
            conversation_retention_seconds=(
                config.comments.conversation_retention_seconds
            ),
            dedupe_retention_seconds=config.comments.dedupe_retention_seconds,
        )
        client_kwargs: dict[str, object] = {
            "registry": self._registry,
            "timeout": config.site.request_timeout_seconds,
            "transport": transport,
            "username": config.secrets.username,
            "password": config.secrets.password,
            # 评论上限由 SiteClient 在流式响应/解析层执行；即使 comments
            # disabled 也传默认值，保证启用时不会依赖隐含客户端常量。
            "max_response_bytes": config.comments.max_response_bytes,
            "max_tree_nodes": config.comments.max_tree_nodes,
        }
        # A/B 组或外部测试可能暂时注入旧版 SiteClient 替身。真实客户端和
        # 支持 **kwargs 的替身接收上限；仅对明确没有该参数的窄替身过滤，避免
        # 评论关闭时也破坏聊天启动。
        try:
            signature = inspect.signature(SiteClient)
            parameters = signature.parameters
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if not accepts_kwargs:
                client_kwargs = {
                    key: value for key, value in client_kwargs.items() if key in parameters
                }
        except (TypeError, ValueError):
            pass
        self._client = SiteClient(config.site.base_url, self._redactor, **client_kwargs)
        self._image_loader = ImageLoader(
            self._client, max_bytes=config.model.max_image_bytes
        )
        # 内容引用解析器：三种引用各一处取回，聊天与评论共用同一份策略。
        # 视觉关闭时不给它 image_loader —— 那等于连图字节都不该下载（与 _load_image 同款）。
        self._ref_resolver = ContentRefResolver(
            self._client,
            max_ref_chars=config.behavior.content_ref_max_chars,
            image_loader=self._image_loader if config.model.vision_enabled else None,
        )
        self._blog_loader = BlogLoader(
            self._client,
            max_chars=config.behavior.quoted_blog_max_chars,
            resolver=self._ref_resolver,
        )
        # 评论区识图要两个开关同时成立：模型支持视觉，且这一轮还留着名额
        # （`comments.max_images_per_reply` 为 0 即评论侧不取图，聊天区不受影响）。
        self._comment_vision = (
            config.model.vision_enabled and config.comments.max_images_per_reply > 0
        )
        # 同一个客户端、同一份上限，只有「取不取字节」这一处不同。
        self._comment_ref_resolver = ContentRefResolver(
            self._client,
            max_ref_chars=config.behavior.content_ref_max_chars,
            image_loader=self._image_loader if self._comment_vision else None,
        )
        # 会话调度器（§14）：入队与容量判定沿用 Queue 形状（Router 只 `put_nowait`，
        # readiness / `queue_depth` 只读 `full()`/`qsize()`），但工作器按「可运行会话」
        # 领取请求 —— 等待同一会话的请求不再占用执行容量（计划 §5）。
        self._queue: SessionScheduler[Request] = SessionScheduler(
            maxsize=config.behavior.queue_size
        )
        self._ctx = ContextManager(
            config.behavior.context_turns, config.behavior.context_input_tokens
        )
        # 大区近期消息缓冲（§38.1）：纯内存对象，构造它不做任何 I/O，也没有开关 ——
        # 容量 50 / 正文 500 字是本功能的固定合同。全进程只有这一个实例，
        # `start()` 把它注入路由器；不做模块全局，测试之间因此不会串状态。
        self._lobby_recent = LobbyRecentContextBuffer()
        # 聊天和评论共用同一模型门；评论 worker 仍保持自身 concurrency=1。
        self._model_gate = asyncio.Semaphore(config.behavior.concurrency)
        self._quota = QuotaGuard(self._store, config.behavior)
        # 表情包出站规范化（设计 §4.3、§4.4）：装配期构造**一次**，两个 Sender 与聊天
        # system 共用同一份。`enabled=false` 时表为 None、提示词为空串，正文与 system
        # 一个字节都不变；提示词正文只在名字表非空时才非空（`sticker_system_addendum`
        # 对空 names 返回空串）。
        self._sticker_table = (
            StickerTable.from_config(config.stickers) if config.stickers.enabled else None
        )
        self._sticker_addendum = (
            texts.sticker_system_addendum(
                collection=config.stickers.collection, names=self._sticker_table.names
            )
            if self._sticker_table is not None
            else ""
        )
        self._sender = MessageSender(
            client=self._client,
            store=self._store,
            quota=self._quota,
            redactor=self._redactor,
            cfg=config.behavior,
            table=self._sticker_table,
        )

        # 注入的假模型不归本对象关闭；内部构造的才需要 aclose。
        self._model = model_client
        self._owns_model = model_client is None
        # 自建模型客户端时使用的实现类：完整版由入口注入带工具循环的
        # `ToolCallingModelClient`，Light 版没有工具协议，退回纯文本实现。
        self._model_client_cls = model_client_cls or OpenAIModelClient
        # 装配接缝（设计 §4.3）：MCP 与发文都从工厂进入。没有 MCP 工厂时落到
        # 无工具实现（Light 的正常形态）；发文没有无工具形态，配置启用却没有
        # 工厂就在构造期报错，绝不静默跳过。
        self._mcp_manager = self._resolve_mcp_manager(mcp_manager, mcp_manager_factory)
        self._blog_service_factory = blog_service_factory
        if config.blog.enabled and blog_service_factory is None:
            raise AssemblyError("blog_service_factory_required")
        # 知识库是本地只读能力：构造它不做任何 I/O，`enabled=false` 时连目录都不会被扫。
        self._kb = (
            knowledge_service
            if knowledge_service is not None
            else KnowledgeService(config.knowledge_base)
        )

        # 长期记忆（全局记忆 Beta，§34.2）。策略是纯内存对象，构造它不做任何 I/O；
        # 队列与 worker 池同理，只有 `start()` 里 `enabled=true` 时才真正启动（D-60）。
        # 三个可选注入点供测试用假件替换服务/撰写器/控制器：真实模型与磁盘 I/O 都不进来。
        #
        # 这一个布尔是整个记忆装配的总闸：构造 Router／构造评论侧／`_start_memory`／关闭
        # 四处都从它取值，`Config` 冻结让它们今天不会漂，但只有一处来源才能让它们将来也不漂。
        self._memory_enabled = config.memory.enabled
        # 「记忆能力**已经装好**」的唯一事实来源，由 `_start_memory` 在 worker 池真正跑起来
        # 之后置真。它必须与 `_memory_enabled`（部署要不要记忆）分开：装配里的软故障兜底
        # 会把 worker 启动失败吞成一条日志，而 Router 的注入决定是一次独立的配置读取 ——
        # 两者一旦分叉，记忆命令就会被排进一个**没有消费者**的队列：没有回复，`finally` 里的
        # `mark_handled` 也永远不跑，事件不是终态、水位被钉住（D-15 / §34.3）。
        # 未装好时一律不注入：Router 的三个记忆参数都为 None，回到 D-60 的回退路径。
        self._memory_armed = False
        self._memory_access = MemoryAccessPolicy(config.memory)
        # 评论侧的门禁载体（§35）：纯内存对象，与策略同一位置构造，不持有任何记忆服务。
        self._comment_memory_access = _CommentMemoryAccess()
        self._memory_service = memory_service
        self._memory_writer = memory_writer
        self._memory_controller = memory_controller
        # 公开 subject 的解析器（§47.4）：与 `_memory_armed` 同时装配，只在记忆路径真的
        # 跑起来之后才存在。为 None 时整个公开个人记忆路径不存在（`enabled=false`、
        # 装配失败或解析器构造失败），DM 与普通聊天一个字都不受影响（D-60）。
        self._public_memory_resolver: PublicMemorySubjectResolver | None = None
        self._memory_queue: SessionScheduler[MemoryCommandRequest] = SessionScheduler(
            maxsize=config.memory.queue_size
        )
        # 并发固定为 1：命令路径要串行；`SessionScheduler` 另保证同会话请求不占执行容量。
        self._memory_workers = WorkerPool(
            queue=self._memory_queue,
            handler=self._handle_memory_command,
            concurrency=1,
        )

        self._workers = WorkerPool(
            queue=self._queue,
            handler=self._handle_request,
            concurrency=config.behavior.concurrency,
        )
        # 健康变化只在**变化时**记一条（计划 §4）。判定点就放在探针上：
        # 宿主按固定周期询问，这正是"状态被观测到"的时刻，不需要另起一个定时任务。
        self._health_state: dict[str, bool] = {}
        self._ops = OpsServer(
            config.ops.host,
            config.ops.port,
            livez=lambda: self._observe_health("live", self.live),
            readyz=lambda: self._observe_health("ready", self.ready),
            archive_status=self._archive_status,
        )

        # 下面这些在 start() 里按装配顺序建立。
        self._router: MessageRouter | None = None
        self._sse: SSEReceiver | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._resync_task: asyncio.Task[None] | None = None
        self._probe_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        # 定时发文同样默认关闭；启用时由独立服务托管扫描/消费/对账三个后台任务（§53.12）。
        self._blog_service: BlogServiceLike | None = None
        # 评论功能默认关闭；启用时由独立服务托管自己的队列与轮询任务。
        self._comment_service: CommentService | None = None
        self._comment_router: CommentRouter | None = None
        self._comment_sender: CommentSender | None = None
        self._comment_quota: CommentQuotaGuard | None = None
        self._comment_ctx = ContextManager(
            config.comments.context_turns, config.comments.context_input_tokens
        )
        self._comment_router_queue: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, config.comments.queue_size)
        )

        self._started = False
        self._stopped = False
        self._unavailable = False  # 403 权限/禁言导致的不就绪状态（D-4）
        self._shutdown_event = asyncio.Event()

    def _resolve_mcp_manager(
        self,
        injected: McpManagerLike | None,
        factory: Callable[..., McpManagerLike] | None,
    ) -> McpManagerLike:
        """解析 MCP Manager：显式实例 > 注入工厂 > 无工具默认实现。

        配置启用 MCP 却没有工厂是装配错位（Light 不含 MCP 实现），必须显式报错；
        `mcp.enabled=false` 时无工具实现与真实 Manager 的行为一致：生命周期为空
        操作、任何 feature 都不可用（设计 §4.2）。
        """
        if injected is not None:
            return injected
        if factory is None:
            if self._config.mcp.enabled:
                raise AssemblyError("mcp_manager_factory_required")
            return NoToolMcpManager()
        return factory(
            config=self._config.mcp,
            redactor=self._redactor,
            registry=self._registry,
        )

    # --- 生命周期 -----------------------------------------------------------

    async def start(self) -> None:
        """按 INTERFACES §16 的顺序装配并启动全部组件。"""
        if self._started:
            return
        await self._store.open()
        # 崩溃恢复第一步（§16）：此刻本进程尚未认领任何事件，所以任何非终态行
        # 必定属于已经死掉的旧进程。标成 recover 之后，它们才能被重新认领。
        # **必须早于 sse.run()**，否则会把本进程刚入队的在途工作误标成孤儿。
        await self._store.mark_orphans_recoverable()
        # 评论事件使用独立状态表；同样必须在评论服务创建任务前恢复旧进程遗留项。
        if self._config.comments.enabled:
            recover_comments = getattr(self._store, "recover_comment_events", None)
            if recover_comments is not None:
                await recover_comments()
        # 启动清理一次：过期链、旧事件、到期冷却都在这里收掉（D-23）。
        # 它失败不得阻止启动 —— 记录后继续，下一个周期还会再来。
        await self._prune_once()
        await self._client.start()
        try:
            user = await self._client.login()
        except BaseException:
            # 启动阶段失败：把已开的资源收回，避免半初始化状态泄漏。
            await self._client.aclose()
            await self._store.close()
            raise

        # 主模型必须早于记忆撰写器构造：`MemoryWriter` 绑定的是这一个客户端（§34.2 第 2 步）。
        # 位置相对旧版上移了一格（原先在 Router 之后），构造失败时的行为不变：异常照常
        # 传播出 `start()`，同样不会留下比旧版更多的半初始化资源。
        if self._model is None:
            self._model = self._model_client_cls(
                self._config.model,
                self._config.secrets.llm_api_key,
                redactor=self._redactor,
                transport=self._transport,
            )
        # 记忆装配（§34.2 第 1-3 步）：必须在聊天 worker 与评论服务启动之前，
        # 且整段只在 `memory.enabled=true` 时执行（D-60）。整段成功跑完才会置 `_memory_armed`。
        await self._start_memory()

        # 记忆路径的三种参数要么都给，要么都不给：全都为 None 时 Router 的行为与
        # 升级前逐字节一致（§34.1）。判据是 `_memory_armed` 而不是配置里的 `enabled`：
        # 只有记忆 worker 真的在跑，记忆命令才有消费者（§34.3 的 `memory_queued` 终态由它落笔）。
        # 装配失败时这一段与 `memory.enabled=false` 完全同形，`enabled=false` 走的就是这条。
        memory_on = self._memory_armed
        self._router = MessageRouter(
            self_user_id=user.id,
            bot_username=self._config.secrets.username,
            ctx=self._ctx,
            store=self._store,
            queue=self._queue,
            cfg=self._config.behavior,
            storage=self._config.storage,
            vision_enabled=self._vision_enabled,
            kb_enabled=self._config.knowledge_base.enabled,
            memory_access=self._memory_access if memory_on else None,
            memory_queue=self._memory_queue if memory_on else None,
            private_enabled=self._memory_private_enabled if memory_on else None,
            lobby_recent=self._lobby_recent,
        )
        # MCP 是可选扩展：任何连接、发现或子进程错误只让对应 feature 不可用，
        # 不得阻止普通聊天、评论或健康端点启动。
        try:
            await self._mcp_manager.start()
        except Exception as exc:
            log_event(
                _logger,
                logging.WARNING,
                "app.mcp_start_failed",
                error=describe_error(exc),
            )
        # 知识库同样是软故障扩展：目录不可读、索引为空都只让 `/kb` 本地提示，
        # 不得阻止普通聊天、评论或健康端点启动（D-45）。
        try:
            await self._kb.start()
        except Exception as exc:
            log_event(_logger, logging.WARNING, "app.kb_start_failed", error=type(exc).__name__)

        self._sse = SSEReceiver(
            self._client,
            self._on_event,
            base_delay=self._config.behavior.reconnect_base_seconds,
            max_delay=self._config.behavior.reconnect_max_seconds,
        )
        # D-16 崩溃恢复第二步：构造之后、run() 之前，用存储层水位播种 Last-Event-ID。
        # 否则重启会从「此刻」重新订阅，崩溃瞬间在处理的消息永久丢失。
        #
        # 两步缺一不可：上面 mark_orphans_recoverable() 把旧进程遗留的行标成 recover，
        # 这里的水位因为该行非终态而停在它**之前**，于是服务端会把它补发回来，
        # 路由器再在第 6 步用 reclaim_orphan() 认领它。只做这一步（只有水位播种）
        # 是不够的：补发回来的消息会被当成 duplicate 丢掉，消息永远不处理、
        # 水位永远卡住 —— 这正是外部审查发现的问题。
        self._sse.set_last_event_id(await self._store.watermark())

        await self._workers.start()
        try:
            await self._ops.start()
        except BaseException:
            await self._workers.stop()
            await self._mcp_manager.stop()
            if self._owns_model and self._model is not None:
                await self._model.aclose()
            await self._client.aclose()
            await self._store.close()
            raise

        # 评论与聊天共用 SiteClient、Store 和模型客户端，但评论服务拥有独立队列。
        # 按阶段七生命周期，先让聊天 worker/ops 就位，再构造并启动评论后台任务。
        if self._config.comments.enabled:
            try:
                self._comment_quota = CommentQuotaGuard(self._store, self._config.comments)
                self._comment_sender = CommentSender(
                    client=self._client,
                    store=self._store,
                    quota=self._comment_quota,
                    redactor=self._redactor,
                    cfg=self._config.comments,
                    self_user_id=user.id,
                    # 与聊天侧共用同一张表：评论出站也走 `prepare_outbound`（设计 §4.5）。
                    table=self._sticker_table,
                )
                self._comment_router = CommentRouter(
                    self_user_id=user.id,
                    bot_username=self._config.secrets.username,
                    ctx=self._comment_ctx,
                    store=self._store,
                    queue=self._comment_router_queue,
                    cfg=self._config.comments,
                    now=time.time,
                    vision_enabled=self._comment_vision,
                    # §34.2 第 5 步：与聊天侧同一个总闸，关闭时**不注入**任何记忆能力，
                    # 评论路径因此一次都不碰记忆（D-60）。
                    memory_access=self._memory_access if memory_on else None,
                )
                self._comment_service = CommentService(
                    self._client,
                    self._store,
                    self._config.comments,
                    bot_username=self._config.secrets.username,
                    bot_user_id=user.id,
                    router=self._comment_router,
                    sender=self._comment_sender,
                    model_client=self._model,
                    model_gate=self._model_gate,
                    quota=self._comment_quota,
                    context_manager=self._comment_ctx,
                    system_prompt=self._config.system_prompt,
                    # 与聊天 system 同一份表情附加说明（设计 §4.5）；关闭时是空串，
                    # 评论侧的 system 与升级前逐字节一致。无 `ContextManager` 的回退分支
                    # 由评论子系统内部单独拼接（那一条不走 `build_messages`）。
                    sticker_addendum=self._sticker_addendum,
                    content_refs=self._comment_ref_resolver,
                    image_loader=(
                        self._image_loader if self._comment_vision else None
                    ),
                    # §34.2 第 5 步的只读 context provider：请求感知（§48.2）、只读，
                    # 共同记忆只可能返回 `all_user`、公开条目只来自 `public/`；关闭时
                    # 同样不注入，评论侧连一个方法都拿不到。
                    memory_context=(
                        self._comment_memory_items if memory_on else None
                    ),
                    # §26.1 的 `common_context_tokens` 同样管评论侧：评论的整轮预算更小
                    # （`comments.context_input_tokens`），共同记忆不该把它吃光（§26.2 第 8 条）。
                    # 关闭时与 provider 一起缺席：那时评论侧一次都不碰记忆。
                    memory_common_tokens=(
                        self._config.memory.common_context_tokens if memory_on else None
                    ),
                    # 公开个人记忆的分组上限（§45.3、§48.2）：与共同记忆那份互相独立，
                    # 公开条目装不下时整条跳过，先保住既有记忆（D-103 第 5 条）。
                    memory_public_tokens=(
                        self._config.memory.public_personal_context_tokens
                        if memory_on
                        else None
                    ),
                )
                await self._comment_service.start()
            except BaseException:
                # 评论初始化失败不能回滚聊天服务；live 会保持 false，日志只记稳定错误。
                log_event(_logger, logging.ERROR, "app.comment_start_failed")
                self._comment_service = None

        # 定时发文（§53.12）：与评论一样是与聊天共享客户端/Store/模型的可选子域。
        # `blog.enabled=false` 时**整段跳过**：不构造、不建后台任务、不调模型、
        # 不访问发布接口 —— 旧部署升级后行为逐字节不变。
        # 装配位置在账号、Store、模型与 MCP 都就绪之后，共享 `_model_gate` 与 Registry。
        if self._config.blog.enabled:
            try:
                self._blog_service = self._build_blog_service(user)
                await self._blog_service.start()
            except AssemblyError:
                # 装配错位不受软故障兜底约束：工厂内部（如 L2 的发行能力策略）
                # 抛出的装配失败必须让启动失败，不能记一条日志后静默置空子域。
                raise
            except Exception as exc:
                # 只用 `Exception`（不用 `BaseException`）：启动途中被取消时取消必须继续传播，
                # 吞掉它会让关闭流程以为一切正常。子域失败不停聊天/评论，也不改变健康判定。
                log_event(
                    _logger,
                    logging.ERROR,
                    "app.blog_start_failed",
                    error=type(exc).__name__,
                )
                self._blog_service = None

        self._sse_task = asyncio.create_task(self._sse.run(), name="bot-sse")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="bot-cleanup")
        # 只加观测：这三个任务此前一旦安静地结束，/livez 会翻红而日志里没有一行
        # 说明是哪一个、以什么方式结束的（计划 §4 的「后台任务退出」）。
        observe_task(self._sse_task, "sse")
        observe_task(self._cleanup_task, "cleanup")
        self._started = True
        log_event(_logger, logging.INFO, "app.started")

    def _build_blog_service(self, user) -> BlogServiceLike:
        """经注入的工厂构造发文子域（§53.11 / §53.12）。

        工厂（完整版的 `blog.assembly.build_blog_service`）共享 App 的客户端、
        Store、模型门与 MCP Registry，**不在这里创建或持有**它们的关闭权限 ——
        它们属于 App，子域只是借用。工具预算不新增配置项：`BlogWriter` 从
        `mcp.features.blog_write` 取 `max_tool_calls_per_turn`（§8.1、D-110）。
        """
        factory = self._blog_service_factory
        if factory is None:
            # 构造期已按 blog.enabled 挡过一次；这里保留显式失败（不用 assert：
            # `-O` 下会消失），模块被绕过构造顺序调用时也不能静默少一个子域。
            raise AssemblyError("blog_service_factory_required")
        return factory(
            config=self._config,
            user=user,
            store=self._store,
            client=self._client,
            model=self._model,
            mcp_registry=self._mcp_manager.registry,
            model_gate=self._model_gate,
            redactor=self._redactor,
        )

    def _observe_health(self, kind: str, healthy: bool) -> bool:
        """记录一次健康观测，状态**变化**时写一条 `app.health_changed`。

        首次观测只用来建立基线，不算变化：进程刚起来时 `/readyz` 本来就是
        down（还没登录、SSE 还没连上），把它记成一次"故障"是纯噪音。
        """
        previous = self._health_state.get(kind)
        self._health_state[kind] = healthy
        if previous is None or previous == healthy:
            return healthy
        fields: dict[str, object] = {
            "kind": kind,
            "from_state": "up" if previous else "down",
            "to_state": "up" if healthy else "down",
            "queue_depth": self._queue.qsize(),
            "worker_count": self._workers.alive_count,
        }
        if not healthy:
            fields["reason"] = self._unhealthy_component()
        log_event(
            _logger,
            logging.INFO if healthy else logging.WARNING,
            "app.health_changed",
            **fields,
        )
        return healthy

    def _unhealthy_component(self) -> str:
        """当前最可能让探针翻红的组件名；只用于日志，取值是固定短标识。"""
        if self._stopped or not self._started:
            return "app"
        sse_task = self._sse_task
        if sse_task is None or sse_task.done():
            return "sse"
        if not self._workers.alive:
            return "workers"
        if not self._client.logged_in:
            return "auth"
        if self._queue.full():
            return "queue"
        if self._config.comments.enabled:
            service = self._comment_service
            if service is None or not service.alive:
                return "comments"
        return "unknown"

    def _archive_status(self) -> dict[str, object] | None:
        """归档健康检查回调；未启用归档时返回 None（端点回 404）。"""
        if self._archive is None:
            return None
        status = getattr(self._archive, "status", None)
        return status() if callable(status) else None

    async def run_forever(self) -> None:
        """启动并阻塞，直到 `stop()` 被调用（或信号处理方调用 stop）。"""
        await self.start()
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """优雅关闭：SSE → WorkerPool → OpsServer → client → store；总超时 10 秒。"""
        if self._stopped:
            return  # 幂等：重复调用安全
        self._stopped = True
        try:
            await asyncio.wait_for(self._shutdown(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log_event(_logger, logging.WARNING, "app.shutdown_timeout")
        except Exception as exc:
            # 未启动即 stop（或某个组件在关闭时抛错）不得让 stop() 本身失败：
            # 各组件都已被 stop() 设计成幂等 no-op，这里只兜底记录。
            log_event(_logger, logging.WARNING, "app.shutdown_error", error=type(exc).__name__)
        finally:
            self._shutdown_event.set()
        log_event(_logger, logging.INFO, "app.stopped")

    @property
    def ready(self) -> bool:
        """是否已登录、SSE 已连接、队列未满且未处于权限不可用状态。"""
        if self._stopped or self._unavailable:
            return False
        if not self._client.logged_in:
            return False
        sse = self._sse
        if sse is None or not sse.connected:
            return False
        return not self._queue.full()

    @property
    def live(self) -> bool:
        """事件循环在跑且关键 task（SSE、worker pool）未死。"""
        if self._stopped or not self._started:
            return False
        sse_task = self._sse_task
        if sse_task is None or sse_task.done():
            return False
        if not self._workers.alive:
            return False
        if self._config.comments.enabled:
            service = self._comment_service
            if service is None or not service.alive:
                return False
        return True

    def status_snapshot(self) -> dict[str, object]:
        """只读状态快照（LIGHT_EDITION_DESIGN §10.2、§12），供 Worker 转成 IPC 上报。

        与探针同一口径：区分「配置意图」「实际初始化」与「最近结果」，缺证据就
        报 unknown / False，不从历史日志猜当前状态。字段只含计数与状态，不含
        Cookie、正文、查询或路径；站点的模型测试结果由 Controller 侧持有，
        这里只报告配置意图。
        """
        sse = self._sse
        comments = self._comment_service
        memory_service = self._memory_service
        return {
            "sampled_at": time.time(),
            "live": self.live,
            "ready": self.ready,
            "site": {
                "authenticated": self._client.logged_in,
                "sse_connected": bool(sse is not None and sse.connected),
                # 登录成功不等于可聊天：权限/禁言时 ready 为 False（D-4）。
                "chat_ready": self.ready,
                "unavailable": self._unavailable,
            },
            "model": {
                "configured": bool(
                    self._config.model.base_url and self._config.model.model
                ),
                "vision_enabled": self._config.model.vision_enabled,
            },
            "comments": {
                "enabled": self._config.comments.enabled,
                "running": bool(comments is not None and comments.alive),
            },
            "knowledge_base": self._kb.status_snapshot(),
            "memory": {
                "enabled": self._memory_enabled,
                "armed": self._memory_armed,
                "available": bool(memory_service is not None and memory_service.available),
            },
            "archive": self._archive_status() or {"enabled": False},
        }

    # --- 长期记忆（§34.2 / §34.3 / §30.1） -----------------------------------

    async def _start_memory(self) -> None:
        """记忆装配的第一段（§34.2 第 1-3 步）；`enabled=false` 时整体跳过（D-60）。

        关闭时连策略也不构造注入：Router 的三个记忆参数都为 `None`，`/remember` 仍是普通聊天，
        `/search /remember x` 仍是普通搜索请求，`/help` 也不出现长期记忆与记忆命令两段。
        """
        if not self._memory_enabled:
            return
        service = self._memory_service
        if service is None:
            service = MemoryService(
                self._config.memory,
                # §30.1 / §37：生产装配**必须**传入 BotApp 自有的这个 Redactor —— 它在构造时
                # 登记了密码与 LLM_API_KEY，登录成功后 `SiteClient` 又把会话 Cookie 追加到
                # **同一个实例**上（注册到日志单例的是另一次调用、另一个实例）。
                # 漏掉它就等于密钥筛完全失效：不报错、不打日志、悄悄放过任何含密钥的正文。
                redactor=self._redactor,
            )
            self._memory_service = service
        try:
            # 服务自己承诺绝不抛出（§30.1），这一层兜的是注入的替身与将来的回归：
            # 记忆起不来只记账，绝不阻止聊天、评论与健康端点启动（D-60）。
            await service.start()
        except Exception as exc:
            log_event(
                _logger,
                logging.WARNING,
                "memory.load_failed",
                scope="common",
                error=type(exc).__name__,
            )
        try:
            # 第 2-3 步（§34.2）与 MCP/KB 的邻居同款地包在软故障里：两个构造函数今天
            # 只是纯赋值，但将来的任何参数校验都会把一次记忆问题升级成启动失败 —— 那与
            # D-60 相反。记忆起不来只记账，聊天、评论与健康端点照常。
            writer = self._memory_writer
            if writer is None and self._model is not None:
                # 撰写器绑定的是上面那一个主模型客户端，与普通聊天共用同一道并发门。
                writer = MemoryWriter(
                    self._model,
                    model_gate=self._model_gate,
                    timeout_seconds=self._config.memory.writer_timeout_seconds,
                    max_context_tokens=self._config.memory.writer_context_tokens,
                    max_entry_chars=self._config.memory.max_entry_chars,
                )
                self._memory_writer = writer
            if self._memory_controller is None and writer is not None:
                self._memory_controller = MemoryController(
                    service=service,
                    writer=writer,
                    access=self._memory_access,
                    # §32.3 与 §34.4：`/memory auto on` 与自动提取都只在部署开放时成立。
                    # 漏掉这个参数则开关永远打不开，同样静默、同样没有日志。
                    auto_capture_available=self._config.memory.auto_capture_available,
                )
            await self._memory_workers.start()
            # 到这里队列才有了消费者：只有在这个点上置真，Router 才会拿到记忆能力。
            # 启动失败会掉进下面的 except（标志保持 False，Router 不注入），不会半开半闭。
            self._memory_armed = True
        except Exception as exc:
            log_event(
                _logger,
                logging.WARNING,
                "memory.load_failed",
                scope="memory",
                error=type(exc).__name__,
            )
            return
        # §47.4：公开 subject 的解析器在记忆路径**真的装好之后**单独装配 —— 它失败只该让
        # 「公开个人记忆」这一格缺席，不该把已经装好的共同/私有记忆一起关掉（D-60）。
        # 索引取自服务的内存快照（同步、无 I/O），站点查询复用同一个 `SiteClient`。
        try:
            self._public_memory_resolver = PublicMemorySubjectResolver(
                index_provider=service.public_username_index,
                client=self._client,
                max_subjects=self._config.memory.max_public_subjects_per_turn,
            )
        except Exception as exc:
            log_event(
                _logger,
                logging.WARNING,
                "memory.load_failed",
                scope="public",
                reason="resolver",
                error=type(exc).__name__,
            )

    def _memory_private_enabled(self, user_id: str | None) -> bool:
        """`/help` 的 `private_enabled` 回调（§34.1 第 3 条，D-67）。

        **同步、无 I/O**：只读 `MemoryService` 的内存快照。这里必须做形状适配 ——
        `private_settings_cached` 返回的是 `PrivateSettings | None`，直接把方法接给 Router
        会让 `bool(对象)` 恒为真，于是 `/help` 对每个取不到快照的人都宣称私有记忆已开启。

        快照还没加载（用户从未触发过一次读取）或用户未知时返回 `False`，按「未开启」处理。
        """
        service = self._memory_service
        if service is None or not user_id:
            return False
        settings = service.private_settings_cached(user_id)
        return bool(settings is not None and settings.private_enabled)

    def _supplemental_caps(self) -> tuple[SupplementalCap, ...]:
        """补充资料的分组 token 上限（§33、§26.1）：把 `memory.*_context_tokens` 接上。

        共同记忆（`all_user` 与 `lobby`）**合计**一份预算、私有记忆（`memory_user`）单独一份，
        与 `config.example.yaml` 里那句「all_user 与 lobby 合计使用这一份共同记忆预算」一致
        —— 传两个分组名进同一个上限，而不是各给一份。公开个人记忆有**自己**的一份
        （`public_personal_context_tokens`，§45.3、公开设计 §7.3）：DM 用「共同 + 私有」、
        大区用「共同 + 公开个人」，三份上限各自独立、与整轮预算是两道独立的门。

        取值只来自配置（上限按分组名传，`core/context.py` 不认识任何记忆类型，D-61）；
        上限只管**选哪些条目**，整轮预算与历史回补的账目一个字都不动。记忆关闭时这些上限
        也没有对象可用：那一轮的 `supplemental_items` 恒为空元组，选择根本不会发生。
        """
        memory = self._config.memory
        return (
            SupplementalCap(
                ("memory_all_user", "memory_lobby"), memory.common_context_tokens
            ),
            SupplementalCap(("memory_user",), memory.private_context_tokens),
            SupplementalCap(
                ("memory_public_personal",), memory.public_personal_context_tokens
            ),
        )

    async def _memory_context_items(self, request: Request) -> tuple[SupplementalItem, ...]:
        """取本轮的记忆候选条目（§34.3）；任何失败都返回空元组并继续（软故障，D-60）。

        只按作用域取候选：按 token 预算的取舍与插入位置由 `ContextManager.build_messages`
        决定（D-62）。这里不做任何会改变聊天结果的判断。
        """
        service = self._memory_service
        if service is None:
            return ()
        # 传给 `context_for` 的就是 Router 用的那一个策略实例：`access` 按调用传入（§30.2），
        # 这里不能另造一个 —— 两份策略会在门禁口径上悄悄漂移。
        access = self._memory_access
        try:
            context = await service.context_for(
                user_id=request.message.author.id,
                channel_kind=request.channel_kind,
                access=access,
            )
        except Exception as exc:
            # 正常情况下 `context_for` 自己就把一切失败吞成空结果（D-60）；这一层只兜
            # 注入的替身与将来的回归：记忆取不到绝不能把这一轮聊天一起拖掉。
            log_event(
                _logger,
                logging.WARNING,
                "memory.context_omitted",
                scope="memory",
                reason="internal",
                error=type(exc).__name__,
            )
            return ()
        return tuple(context.items)

    def _public_memory_active(self, request: Request) -> bool:
        """本轮的公开个人记忆路径是否生效（§47.4 第 2 条）；三个条件缺一不可。

        - 解析器已装配：`memory.enabled=false` 或记忆装配失败时它不存在，这一格整段跳过；
        - `request.memory_allowed`：作者没过 Beta 门时连本地公开索引都不该看；
        - 频道是 `"lobby"`：**DM 绝不加载任何人的公开个人记忆**（R4 的同一条口径），
          连一次站点查询都不发。
        """
        return (
            self._public_memory_resolver is not None
            and request.memory_allowed
            and request.channel_kind == "lobby"
        )

    async def _public_memory_items(
        self,
        request: Request,
        *,
        reply_text: str | None,
        blog_text: str | None,
        expanded_texts: tuple[str, ...],
        lobby_recent: tuple[LobbyRecentMessage, ...],
    ) -> tuple[SupplementalItem, ...]:
        """解析本轮公开记忆的 subject 并取回条目（§47.4、§47.5）；任何失败都返回空元组。

        输入只放**本轮真的会提供给模型**的文本（公开设计 §6.3、§15）：`reply_text` 是已经拼好的
        直接引用块，`blog_text` 是实际拼进 `pending_user` 的博客块（省略了正文的只有标题），
        `expanded_texts` 是 R10 的 `ResolvedRefs.expanded_texts`（预算不足没取回的引用不在其中），
        `lobby_recent` 是 S1。**不为记忆匹配额外抓取博客或剪贴板**：本方法不读博客、不读剪贴板、
        不取图片，唯一的网络请求是纯文本命中的身份校验（§6.4），它不改变本轮外送的正文。

        近期消息按原样传入：优先级 7 只扫 `content`，**不扫 `blog_title`**。公开设计 §6.1 第 8 条
        说的是「实际选入的发言者与**正文**用户名」，标题因此落在匹配面之外 —— 少解析只是少一份
        可选资料（欠解析的方向），而反过来去碰一段没有明确授权的文本会扩大读取面（§6.3 的口径）。

        身份校验会发站点查询（纯文本命中才发，稳定来源不发，§43.4），失败一律 fail-closed：
        一个候选都解析不出来就返回空元组，这一轮只是少一份可选资料，聊天照常、历史提交不变。
        """
        resolver = self._public_memory_resolver
        service = self._memory_service
        if resolver is None or service is None:
            return ()
        try:
            # §47.5 的软故障边界必须包住**输入构造与解析两件事**：解析器自己承诺不抛（§43.2），
            # 构造则要碰注入的 context manager —— 从这里抛出去会被 `WorkerPool._run` 记成
            # `worker.handler_error`，整轮回复一起消失，那正是这条边界要挡住的事。
            inputs = PublicMemoryInputs(
                channel_kind=request.channel_kind,
                # 当前发言者由 Router 在入队前算好（§47.2）：这里不回头去碰 `author.id`。
                current_subject=request.public_memory_subject,
                # 短期参与者直接取历史的 subject（§45.1）：只读、同步、无 I/O。
                conversation_subjects=self._ctx.recent_subjects(request.session_key),
                # 当前正文用**用户自己写的原文**（设计 §15 的 `request.user_text`）：展开过的引用
                # 走它们自己的优先级来源，在这里再扫一遍只会把同一段文本当成两个来源。
                current_text=request.user_text,
                reply_text=reply_text,
                blog_text=blog_text,
                expanded_clipboard_texts=expanded_texts,
                lobby_recent=lobby_recent,
            )
            subjects = await resolver.resolve(inputs)
        except Exception as exc:
            # 这一层兜的是注入的替身与将来的回归：任何意外都只该让这一轮少一份可选资料。
            log_event(
                _logger,
                logging.WARNING,
                "memory.context_omitted",
                scope="public",
                reason="subjects",
                error=type(exc).__name__,
            )
            return ()
        # 取回与软故障由 `_public_context_items` 统一处理：评论路径问的是同一件事，
        # 只有输入的构造方式不同。
        return await self._public_context_items(subjects, request.channel_kind)

    async def _public_context_items(
        self, subjects: tuple[PublicMemorySubject, ...], channel_kind: str
    ) -> tuple[SupplementalItem, ...]:
        """按已经解析出的 subject 取回公开个人条目（§42.7）；任何失败返回空元组（§47.5）。

        一个 owner 都没命中时不看目录、不问服务，直接空手返回（§43.3 末段）。
        与 `context_for` 职责分开：公开候选只从 `public/` 来，共同/私有候选仍由
        `_memory_context_items` / `_comment_memory_items` 出（§47.4 第 4 条、§48.2）。
        """
        service = self._memory_service
        if service is None or not subjects:
            return ()
        try:
            context = await service.public_context_for(
                subjects=subjects, channel_kind=channel_kind
            )
            return tuple(context.items)
        except Exception as exc:
            # 软故障（§47.5）：已经解析出来的 subject 一个都不落地，回答照常生成。
            # `subject_count` 是本轮解析出的 owner 数（§51 的白名单字段），不含任何身份。
            log_event(
                _logger,
                logging.WARNING,
                "memory.context_omitted",
                scope="public",
                reason="internal",
                subject_count=len(subjects),
                error=type(exc).__name__,
            )
            return ()

    def _auto_capture_eligible(self, request: Request) -> bool:
        """§34.4 的自动提取前置条件；**全部**同时成立才为真。

        这里只判装配层**本地、无 I/O** 就能判的那些：总开关、DM、Beta 接入门（Router 用它
        算出的 `memory_allowed`）、本轮是不是单轮能力命令、本轮有没有依赖外部资料、代次是否
        还有效。部署级 `auto_capture_available` 与用户自己的 `auto_capture` 设置**不在这里
        复查** —— `MemoryController.auto_capture` 的两个门就是它们（§32.3），在这一层再判一遍
        只会多出一份会漂的副本，还会用它去否决控制器本该接受的写入。

        本地命令（`/help`、`/reset`、用法提示、超长与探测词拒绝）在 Router 里就已经本地应答，
        根本不会进 worker，因此这里没有它们的形状可判：它们不可能到达调用点。真正可能带着
        资料走到这里的只有下面三种，都要排除——用户的问题是冲着那些资料去的，只拿他写下的
        十几个字去提炼「记忆」只会得到噪音。

        判「依赖资料」看的是**用户把手指到哪儿**，不是这一轮真的取回了多少字节：图片输入关闭
        （`_vision_enabled` 为假，图床一个请求都不发）或引用的博客没加载成功时一样跳过提取。
        这两个回合的问题同样是冲着那份资料去的，按「取到几个字节」分叉既难解释，也与
        `has_media` 的既有口径（`image_missing` / `blog_missing` 同样算数）对不上。

        代次也放在这里：自动提取是**当前这一轮**的副作用，被 `/reset` 或过期作废的那一轮
        不该再往记忆里写任何东西。调用点已经紧跟在代次检查之后，这里仍是独立的一道 ——
        派生的条件不该依赖调用顺序来成立。
        """
        if not self._memory_enabled:
            return False
        if request.channel_kind != "dm":
            return False
        # `memory_allowed` 由 Router 用当前作者算出：记忆未启用或没通过 Beta 门时恒为假。
        # 作者 ID 为空同样不提取：控制器需要一个稳定身份来定位私有文件（§28）。
        if not request.memory_allowed or not request.message.author.id:
            return False
        if request.enabled_features:
            # `/search` 与 `/kb`：搜索结果与知识库片段只属当前轮，不进记忆（设计 §3.2）。
            return False
        if self._ctx.generation(request.session_key) != request.generation:
            return False
        message = request.message
        if has_media(message):
            # 自己的图（含 `image_missing`）与引用的博客：正文之外还有资料。
            return False
        if _reply_image_url(request) is not None:
            # 被引用消息的缩略图（§20）：已删除的引用不取图，那种引用不算依赖图片。
            return False
        if any(find_refs(text) for text in _ref_source_texts(request) if text):
            # `[@<ID>]` 内容引用（§25）：被引用正文里的引用同样会随当前轮展开。
            return False
        return True

    async def _auto_capture_answer(self, request: Request, answer: str) -> str:
        """在回答发出前做一次自动提取；返回**即将发送**的文本（§34.4）。

        只把用户自己写的**原始正文**（`request.user_text`，不含 @机器人、不含展开过的引用、
        不含博客块与知识库块）交给撰写器：模型回答、搜索结果、知识库片段、引用正文与图片描述
        在 `MemoryController.auto_capture` 的签名里**没有参数可传**（§32.3、§34.4），本层也
        绝不另找路子把它们送进去。

        成功变更时先原子提交私有记忆（在控制器里完成），再拼确定性披露：新增与更新的措辞不同
        （`MemoryCaptureResult.action`）。披露要占输出空间，因此先按 D-63 的口径截断回答主体：
        `X = max_output_chars - len(disclosure)`，`limit = X - len(TRUNCATION_SUFFIX)`
        （`truncate_at_paragraph` 最多返回 `limit + len(TRUNCATION_SUFFIX)`），再拼
        `body + disclosure` —— 于是 Sender 的第二次截断（按 `max_output_chars`）不会切掉披露。
        预留还要再扣掉**脱敏可能带来的增长**：Sender 先脱敏、后截断，模型回显一个比
        `"[redacted]"` 短的密钥会让正文变长，那就轮到披露的尾巴被切。

        配置凑到几乎没有回答余地时（披露 + 内容上限顶满 `max_output_chars`）宁可**放弃披露**，
        也不让 `limit` 落到零以下、把用户的回答整条换成一句截断提示：D-60 的底线是回答优先。
        配置层已按 `memory.max_entry_chars` 交叉校验，这一道只兜住它算不到的运行时长度。

        失败、超时、`noop`、写入失败与幂等重放一律原样返回 `answer`（不追加任何说明）。
        控制器返回的**形状不对**（读不出 `status` 等字段）同样只算软故障：那时模型早就把回答
        生成好了，一个 `AttributeError` 不允许把这条回答一起带走。下面从调用到拼披露整段都在
        同一个 `try` 里，正是为了这一点（D-60）。

        **记忆提交与站内回复不是一个分布式事务**：这一步之后再发生的发送失败不会回滚记忆，
        用户仍然能在 `/memory list` 里看到那条条目（规划 §8.3 接受这个取舍）。理由是不能为了
        跨系统原子性，把原始消息或待提交正文写进 SQLite —— 那会立刻违反「正文只落 Markdown」
        的红线（§37），而用户已经显式开启了自动记忆，写入依据也只是他自己的原文。
        """
        if not self._auto_capture_eligible(request):
            return answer
        controller = self._memory_controller
        if controller is None:
            # 软故障也要**可见**（D-60）：没有控制器时自动提取只能就地放弃，
            # 但绝不能一声不吭 —— 否则「什么都没发生」与「功能坏了」在事后无法分辨。
            log_event(
                _logger,
                logging.WARNING,
                "memory.auto_capture",
                scope="user",
                reason="controller_unavailable",
            )
            return answer
        try:
            result = await controller.auto_capture(
                user_id=request.message.author.id,
                message_id=request.message.id,
                source_text=request.user_text,
            )
            if result.status != STATUS_OK or result.memory_id is None:
                # 只有**确实写入**才带披露（§27.2）：撰写失败、低置信度 noop、写入失败与
                # 幂等重放都落在这里，原回答照常发送。
                return answer
            disclosure = texts.memory_auto_capture_text(
                memory_id=result.memory_id,
                content=result.content,
                created=result.action is ProposalAction.ADD,
            )
            # 预留按**脱敏后**的长度算：Sender 先脱敏、后截断（core/sender.py 第 1 步）。
            # 聊天调用方现在已先把正文归一 + 脱敏（`prepare_outbound`，max_chars=None），
            # 因此这里的 `growth` 通常为 0；算术保持不变，既覆盖未经预处理的调用方，也让
            # 「披露 + 增长 + 截断提示」三段预留继续成立——否则拼好的文本会顶出上限，披露的
            # 尾巴被 Sender 的第二次截断切掉（D-63 要防的正是这个）。
            growth = max(len(self._redactor.redact(answer)) - len(answer), 0)
            limit = (
                self._config.behavior.max_output_chars
                - len(disclosure)
                - len(texts.TRUNCATION_SUFFIX)
                - growth
            )
            if limit < 1:
                # 退化配置：披露（加上脱敏的增长）把输出空间吃光，再截下去用户的回答就会整条
                # 变成一句截断提示。D-60 的底线是回答优先，因此这一轮放弃披露 —— 记忆已经写入，
                # 用户仍能在 /memory list 里看到它，软故障照例留一条稳定日志。
                log_event(
                    _logger,
                    logging.WARNING,
                    "memory.auto_capture",
                    scope="user",
                    reason="disclosure_no_room",
                )
                return answer
            body, _ = truncate_at_paragraph(answer, limit)
            return body + disclosure
        except asyncio.CancelledError:
            raise
        except Exception:
            # 软故障（D-60）：记忆链路上的意外不允许把这一轮回答一起拖掉 —— 控制器的返回值
            # 形状不对（读不出 `status` 等字段）也走这里，否则已经生成好的回答根本发不出去。
            # 只记稳定事件与白名单字段，绝不记正文、key 或用户 ID（§37）。
            log_event(
                _logger,
                logging.WARNING,
                "memory.auto_capture",
                scope="user",
                reason="internal",
            )
            return answer

    async def _comment_memory_items(
        self, inputs: CommentMemoryInputs
    ) -> tuple[SupplementalItem, ...]:
        """评论区的只读 context provider（§34.2 第 5 步、§35、§48.2）；失败返回空元组（D-60）。

        **请求感知**（§48.2、公开设计 §14.3）：参数是评论服务在本轮资料都定下来之后构造的
        `CommentMemoryInputs` —— 当前 subject、会话 subjects、已允许扫描的文本段与门禁布尔，
        没有任何私人正文。它拿不到原始作者 ID，「谁」只能以不可逆的 owner key 出现。

        两路各自独立取用、各自软失败，都只读 `public/` 或 `all_user`：

        1. **共同记忆**：`user_id=None` + `channel_kind="comment"` + `_CommentMemoryAccess`
           载体，按 §30.2 的表只可能解析出 `all_user`（§35、D-76）。作者 ID 不可得，门禁
           结论由 `CommentRouter` 用真实 ID 判过、由载体带过这一段。
        2. **公开个人记忆**：由 `public_context_for` 从公开投影里取（§42.7、§48.2）；
           它不经过载体，因此载体的 fail-open 构造扩大不了任何范围（D-76 的 2026-09-17 补充）。

        `memory_allowed` 为假时两路都不读：门禁由评论服务在调用前判过（§48.2），走到这里
        时它恒为真，这一层只是纵深防御 —— 将来哪个调用方改了门禁，也不会让读取面静默变宽。
        """
        if not inputs.memory_allowed:
            return ()
        items: tuple[SupplementalItem, ...] = ()
        service = self._memory_service
        if service is not None:
            try:
                context = await service.context_for(
                    user_id=None,
                    channel_kind="comment",
                    access=self._comment_memory_access,
                )
                items = tuple(context.items)
            except Exception as exc:
                # `context_for` 自己就把失败吞成空结果（D-60）；这一层只兜注入的替身与将来的
                # 回归：共同记忆取不到绝不能把这一轮评论一起拖掉，`alive` 与 `/livez` 也不受影响。
                log_event(
                    _logger,
                    logging.WARNING,
                    "memory.context_omitted",
                    scope="comment",
                    reason="internal",
                    error=type(exc).__name__,
                )
        # 两路互不牵连：共同记忆读失败不影响公开投影的取用，反之亦然（都是软故障）。
        return (*items, *await self._comment_public_memory_items(inputs))

    async def _comment_public_memory_items(
        self, inputs: CommentMemoryInputs
    ) -> tuple[SupplementalItem, ...]:
        """评论这一轮的公开个人条目（§48.2、公开设计 §16）；任何失败都返回空元组。

        与聊天侧 `_public_memory_items` 是同一条路径（同一个解析器、同一个
        `public_context_for`），差别只在输入的构造：评论侧把本轮**实际提供**的文本按
        §48.2 映射成 §43.1 的 `PublicMemoryInputs` ——

        - `blog_text` = 标题 + 实际提供的正文（超限时只有标题，被省略的正文不扫描）；
        - `reply_text=None`：评论侧没有「直接引用」这个文本段（设计 §16 的可扫描清单里
          没有父块），父评论块既不匹配也不扫描；
        - `lobby_recent=()`：评论没有大区近期块；
        - `expanded_clipboard_texts` = 评论正文与文章正文两处成功展开的文本（R10）。

        §48.2 把这条映射写在 `CommentService` 名下；解析器由装配层持有（§47.4），装配层
        又是唯一能拿到它的地方，所以映射规则在这一层落地（规则本身逐字照抄该条）。

        **不为记忆匹配额外抓取任何东西**：本方法不读博客、不读剪贴板、不取图片，唯一的
        网络请求是纯文本命中的身份校验（§6.4）。门禁由评论服务在调用前判过（§48.2）：
        `memory_allowed` 为假时这个方法一次都不会被叫到。
        """
        resolver = self._public_memory_resolver
        if resolver is None or self._memory_service is None:
            return ()
        try:
            # §47.5 的软故障边界包住输入构造与解析（见 `_public_memory_items`）：构造里
            # 的 `_comment_blog_text` 也要读输入，任何意外都只该让评论少一份可选资料。
            public_inputs = PublicMemoryInputs(
                # 频道由评论服务钉死为 "comment"（§48.2）；解析器只认 lobby / comment（R4）。
                channel_kind=inputs.channel_kind,
                current_subject=inputs.current_subject,
                conversation_subjects=inputs.conversation_subjects,
                current_text=inputs.current_text,
                reply_text=None,
                blog_text=_comment_blog_text(inputs),
                expanded_clipboard_texts=inputs.expanded_clipboard_texts,
                lobby_recent=(),
            )
            subjects = await resolver.resolve(public_inputs)
        except Exception as exc:
            # 这一层兜的是注入的替身与将来的回归。
            log_event(
                _logger,
                logging.WARNING,
                "memory.context_omitted",
                scope="public",
                reason="subjects",
                error=type(exc).__name__,
            )
            return ()
        return await self._public_context_items(subjects, inputs.channel_kind)

    async def _handle_memory_command(self, request: MemoryCommandRequest) -> None:
        """记忆 worker 的处理：执行命令 → 回一条本地文案 → 无论成败都标记终态（§34.3）。

        - 文案一律 `notice_local`：它是应答明确用户动作的本地回复，**不占**主动通知名额（D-1），
          否则大区里的一条记忆命令会按 (频道, 触发者) 冷却掉整站 24 小时的主动通知；
        - `thread_root_id=None`：记忆命令只在私聊（§34.1），私聊没有大区共享链；
        - `mark_handled` 在 `finally` 里：命令失败或控制器抛异常都不许把水位卡住
          （`WorkerPool` 会兜住异常，但兜不住一个没有终态的事件）。
        """
        try:
            controller = self._memory_controller
            if controller is None:
                # 软故障也要**可见**（D-60）：这条命令只能被就地放弃，终态照留在 finally 里，
                # 但绝不能一声不吭 —— 否则水位推进、用户什么都收不到，事后无从分辨。
                # 只记稳定事件与白名单字段，绝不记命令参数（§37）。
                log_event(
                    _logger,
                    logging.WARNING,
                    "memory.command",
                    reason="controller_unavailable",
                )
                return
            result = await controller.execute_command(request)
            if self._unavailable or not result.text:
                # D-4：不可用期间不发消息（与 `_send_local` 同一口径）；
                # 命令本身已经执行完，终态照常在 finally 里落。
                return
            outcome = await self._sender.send(
                request.channel_id,
                result.text,
                request.message_id,
                kind="notice_local",
                thread_root_id=None,
            )
            self._note_forbidden(outcome)
        finally:
            await self._store.mark_handled(request.message_id, "done")

    # --- SSE 事件分派 -------------------------------------------------------

    async def _on_event(self, event) -> None:
        """把一帧交给路由器，并按 RouteResult 分派副作用。"""
        router = self._router
        if router is None:
            return
        result = await router.handle_stream(event)
        await self._dispatch(result)

    async def _dispatch(self, result: RouteResult) -> None:
        """按 §16 分派路由结果；`ignored` / `queued` / `memory_queued` 无需在此做事。"""
        if result.action == "reply_now":
            await self._send_local(result)
        elif result.action == "busy":
            await self._send_busy(result)
        elif result.action == "resync":
            self._schedule_resync()
        elif result.action == "memory_queued":
            # 记忆命令已经躺在记忆队列里（§34.1）：这一帧没有 `Request`，也**不是** `queued`。
            # 唯一做事的角色是记忆 worker（`mark_handled` 也在它那边，§34.3），这里连
            # `mark_handled` 都不能调 —— 抢先把事件标成终态会让 worker 之外再没有终态写入点，
            # 命令失败时就永远卡住了。这个分支存在的意义只是「明确地什么都不做」。
            return

    async def _send_local(self, result: RouteResult) -> None:
        """应答明确用户动作的本地回复：kind=notice_local（D-1，不落通知冷却）。"""
        try:
            if not self._unavailable and result.channel_id is not None:
                await self._send_notice_text(
                    result,
                    result.text or "",
                    kind="notice_local",
                    actor_id=result.actor_id,
                    thread_root_id=result.thread_root_id,
                )
        finally:
            if result.message_id is not None:
                await self._store.mark_handled(result.message_id, "done")

    async def _send_busy(self, result: RouteResult) -> None:
        """队列满提示：kind=notice，按 (频道, 触发者) 冷却（D-3、D-18）。"""
        try:
            if result.channel_id is None:
                return
            if await self._notice_cooling_down(result.channel_id, result.actor_id):
                return
            if self._unavailable:
                return
            await self._send_notice_text(
                result,
                texts.BUSY_NOTICE_TEXT,
                kind="notice",
                actor_id=result.actor_id,
                thread_root_id=result.thread_root_id,
            )
        finally:
            if result.message_id is not None:
                await self._store.mark_handled(result.message_id, "done")

    async def _notify_failure(self, request: Request) -> None:
        """模型最终失败提示：kind=notice，按 (频道, 触发者) 冷却（D-3、D-18）。"""
        if self._unavailable:
            return
        actor_id = request.message.author.id
        if await self._notice_cooling_down(request.channel_id, actor_id):
            return
        outcome = await self._sender.send(
            request.channel_id,
            texts.FAILURE_NOTICE_TEXT,
            request.message.id,
            kind="notice",
            actor_id=actor_id,
            thread_root_id=request.thread_root_id,
        )
        self._note_forbidden(outcome)

    async def _notify_quota(self, request: Request) -> None:
        """额度用尽提示：尝试发一次 kind=notice 的本地通知（D-18 同冷却口径）。"""
        if self._unavailable:
            return
        actor_id = request.message.author.id
        if await self._notice_cooling_down(request.channel_id, actor_id):
            return
        outcome = await self._sender.send(
            request.channel_id,
            texts.QUOTA_NOTICE_TEXT,
            request.message.id,
            kind="notice",
            actor_id=actor_id,
            thread_root_id=request.thread_root_id,
        )
        self._note_forbidden(outcome)

    async def _notice_cooling_down(self, channel_id: str, actor_id: str | None) -> bool:
        """该 (频道, 触发者) 的通知冷却是否仍在生效。

        真正的闸门在 `quota.reserve`（唯一写点是 `quota.note_sent`，只在送达后落冷却）；
        这里只是同键的一次预读，省掉注定被拒的那次尝试及其日志。
        """
        return (
            await self._store.get_cooldown(notice_cooldown_key(channel_id, actor_id))
            is not None
        )

    async def _send_notice_text(
        self,
        result: RouteResult,
        text: str,
        *,
        kind: str,
        actor_id: str | None = None,
        thread_root_id: int | None = None,
    ) -> SendResult | None:
        """发送一条通知/本地回复；403 按 reason 分流（D-4）。"""
        if result.channel_id is None or not text:
            return None
        outcome = await self._sender.send(
            result.channel_id,
            text,
            result.reply_to,
            kind=kind,
            actor_id=actor_id,
            thread_root_id=thread_root_id,
        )
        self._note_forbidden(outcome)
        return outcome

    # --- worker 的请求处理 --------------------------------------------------

    async def _handle_request(self, request: Request) -> None:
        """worker 的实际处理：上下文 → 模型 → 发送；无论成败都标记 done。"""
        try:
            if self._unavailable:
                # D-4：不可用期间不发消息。这里刻意不逐条记日志，
                # 否则一条活跃私聊会把「期间不刷日志」变成每消息一行。
                return
            # 代次检查之一（§16）：请求入队后可能已被 /reset 作废。
            # 这种请求连模型都不必调 —— 省下这次调用，也不会往新会话里写任何东西。
            if self._ctx.generation(request.session_key) != request.generation:
                log_event(
                    _logger,
                    logging.DEBUG,
                    "app.stale_generation",
                    channel_id=request.channel_id,
                    kind="pre_model",
                )
                return
            # 取图在模型门之外：三个 worker 各自下载互不阻塞，
            # 超时由站点请求超时兜住（设计 §3.5）。
            image_part, image_state = await self._load_image(request)
            # 被引用消息的缩略图与它并列：同样是取图，同样在模型门之外。
            reply_image_part, reply_image_state = await self._load_reply_image(request)
            # 引用博客同样在模型门之外：与取图并列，两边互不阻塞。
            blog = await self._load_blog(request)
            blog_block, blog_state = blog.block, blog.state
            # 内容引用（`[@<ID>]`）也在这里展开：三种引用各一次请求，与取图/取博客并列。
            user_text, reply_text, ref_parts, expanded_texts = await self._resolve_refs(
                request
            )
            if (
                not request.user_text
                and image_part is None
                and not blog_readable(blog_state)
                and (image_state != "none" or blog_state != "none")
            ):
                # 整条消息就是引用、且一样都没取到：用户明确发来了东西，必须给个交代，
                # 但不值得为它占用一次模型调用。文案按消息带了什么来选：有图片载荷时
                # 仍走原来那句（与改动前逐字节一致）。
                #
                # 被引用消息的缩略图**不算**在这一条里：它问的是「这条消息自己带了什么」，
                # 而路由器本来就不会把一条空正文、无图无博客的消息放进来。缩略图取不到
                # 有它自己的记号（前缀里的 `[图片未提供]`），不必把整轮降级成本地提示。
                await self._send_media_unavailable(
                    request,
                    texts.IMAGE_UNAVAILABLE_TEXT
                    if image_state != "none"
                    else texts.BLOG_UNAVAILABLE_TEXT,
                )
                return
            # `/kb`：访问门 → 检索 → 数据块；任何本地拒绝都在这里收口，不进模型。
            kb_text: str | None = None
            if "kb" in request.enabled_features:
                kb_text = await self._prepare_kb(request)
                if kb_text is None:
                    return
            # D-22：本轮内容先**临时**拼给模型，只有回复真正送达才提交进历史。
            # KB 数据块只属于当前轮，历史里提交的是不带它的问题（D-43）；
            # 引用的博客正文同理，历史里只留一行标记（设计 §3.5）。
            pending = self._pending_turn(
                request,
                image_state,
                blog_state,
                blog_block=blog_block,
                user_text=user_text,
            )
            if kb_text is not None:
                pending = f"{pending}\n\n{kb_text}" if pending else kb_text
            # 直接引用（D-7）在**这里**就拼进本轮正文，而不是等 `build_messages` 之后再改
            # 最后一条消息（§11.1）：它同样是外送内容，必须参与这一轮的 token 预算，
            # 否则引用正文会把预算顶穿，而模型看到的顺序也会与规划的不一致。
            reply_prefix = self._reply_prefix(
                request, reply_body=reply_text, reply_image_state=reply_image_state
            )
            if reply_prefix:
                # 与改动前逐字节一致：引用块与当前正文之间仍是 `\n---\n`。
                pending = f"{reply_prefix}\n---\n{pending}"
            # 本轮唯一启用的 MCP 能力（D-39 保证至多一个）；本地能力（`/kb`）不走这条口子，
            # 它在上面已经收口，取不到模型也不用再判一次。
            capability = self._enabled_capability(request)
            system_addenda: list[str] = []
            if request.channel_kind == "lobby":
                system_addenda.append(texts.LOBBY_SHARED_SYSTEM_ADDENDUM)
            if "kb" in request.enabled_features:
                # 与 MCP 工具说明互斥：一条消息里最多一种能力（D-39）。
                system_addenda.append(texts.KB_SYSTEM_ADDENDUM)
            elif capability is not None:
                # 四个 MCP 能力共用同一份「工具输出不可信」说明；按能力表取，同样只出现一次。
                addendum = capability.system_addendum
                if addendum is not None:
                    system_addenda.append(addendum)
            # 表情包用法的静态附加说明（设计 §4.4/§4.5）：继模块级常量与时钟片段之后的
            # 第三类 system 来源，只从启动时已校验的部署配置渲染。它必须排在时间片段**之前**——
            # 时间片段由 `build_messages` 追加，天然在最后（D-114 第 2 条）。关闭时是空串，不追加。
            if self._sticker_addendum:
                system_addenda.append(self._sticker_addendum)
            # 记忆候选只在本轮作者可用时取（§34.3）；取失败传空元组继续，绝不打断聊天（D-60）。
            # 位置在 `/kb` 的本地收口之后：那些分支本来就不调模型，也就没有必要读记忆。
            supplemental: tuple[SupplementalItem, ...] = ()
            if request.memory_allowed:
                supplemental = await self._memory_context_items(request)
            # 大区近期公开消息（§38.3）在这里渲染成逐条字符串：`ContextManager` 不认识
            # `LobbyRecentMessage`，只负责预算与拼装。DM 的 `lobby_recent` 恒为空元组，
            # 两个参数保持「同时为空」，整条路径与升级前逐字节一致。
            recent_items = tuple(
                render_lobby_recent(item) for item in request.lobby_recent
            )
            recent_messages = request.lobby_recent
            feature_context = (
                "kb" in request.enabled_features or blog_block is not None
            )
            system_addendum = "\n\n".join(system_addenda) or None
            # 公开记忆本轮可能生效时**先**算 S1（R2、§47.3）：喂给模型的近期块必须是
            # 「将要解析 subject 的那一串」，否则缓冲里有、但装不进这一轮的近期消息也会
            # 贡献 subject —— 而那些正文模型根本没见过。位置在内容引用、博客、`/kb`、
            # `pending_user` 与 `system_addenda` **全部确定之后**，因此 S1 的估算口径与
            # `build_messages` 完全一致。路径未装配时整段跳过，`recent_items` 与升级前相同。
            if self._public_memory_active(request) and recent_items:
                recent_items = self._ctx.select_recent_suffix(
                    request.session_key,
                    self._config.system_prompt,
                    pending_user=pending,
                    system_addendum=system_addendum,
                    feature_context=feature_context,
                    transient_user_items=recent_items,
                    transient_user_header=texts.LOBBY_RECENT_CONTEXT_HEADER,
                )
                # S1 必然是入参的**后缀**（§45.2），按条数切回来就是它对应的那些记录。
                # 已知的保守面（R2 的刻意取舍）：S1 内随后被记忆块挤掉的那几条仍会贡献
                # subject —— 精确解要在记忆与近期之间求不动点，可能振荡，宁可放宽这一点。
                keep = len(request.lobby_recent) - len(recent_items)
                recent_messages = recent_messages[keep:]
            if self._public_memory_active(request):
                # 公开个人候选由 `public_context_for` 独立取回，与既有共同/私有候选合并成
                # **一组** `SupplementalItem`（§47.4 第 4 条）：取舍与位置仍由
                # `build_messages` 决定，公开条目的 priority 整体晚于既有记忆（D-103）。
                supplemental = (
                    *supplemental,
                    *await self._public_memory_items(
                        request,
                        reply_text=reply_prefix,
                        blog_text=blog_block,
                        expanded_texts=expanded_texts,
                        lobby_recent=recent_messages,
                    ),
                )
            messages = self._ctx.build_messages(
                request.session_key,
                self._config.system_prompt,
                pending_user=pending,
                system_addendum=system_addendum,
                # 数据块不可丢弃，历史可以（D-38）：/kb 与引用的博客正文同理。
                feature_context=feature_context,
                # 记忆正文只走这一条口子：`build_messages` 把它放进 role="user" 的当前轮，
                # 既不进 system，也不进历史（§33、D-56）。本方法之外不再碰 memory 文本。
                supplemental_items=supplemental,
                # §26.1 的三个 token 旋钮在这里生效：共同记忆、私有记忆与公开个人记忆各有
                # 一份上限，记忆因此不能把整轮预算吃光、把历史挤掉（取舍仍由 `build_messages`
                # 做，D-62）。
                supplemental_caps=self._supplemental_caps(),
                # 近期消息与块头要么都给、要么都不给：没有条目时不传表头，否则
                # `build_messages` 会按契约抛 ValueError。公开路径生效时这里给的是 **S1**，
                # 它同时就是模型真正看到的那一串（R2、§47.3）。
                transient_user_items=recent_items,
                transient_user_header=(
                    texts.LOBBY_RECENT_CONTEXT_HEADER if recent_items else None
                ),
            )
            if image_part is not None:
                attach_image(messages, image_part)
            # 被引用的那张缩略图紧随消息自己的图，然后是各处引用换出来的图：
            # 一张图一块，顺序只影响模型的阅读次序。
            # 博客正文换出来的图也在其中——它同样只属当前轮，取回来了就该交出去。
            if reply_image_part is not None:
                attach_image(messages, reply_image_part)
            for part in ref_parts:
                attach_image(messages, part)
            for part in blog.image_parts:
                attach_image(messages, part)
            model = self._model
            if model is None:
                if capability is not None:
                    await self._send_capability_unavailable(
                        request, capability, "model_missing"
                    )
                else:
                    await self._notify_failure(request)
                return
            history_context: str | None = None
            try:
                if capability is not None:
                    text, history_context = await self._complete_capability(
                        model, messages, request, capability
                    )
                else:
                    async with self._model_gate:
                        text = await model.complete(messages)
            except ToolGenerationCancelled:
                # /reset 在工具循环的任一异步边界作废了本轮；不发通知、不写历史。
                return
            except CapabilityUnavailable as exc:
                # 本地能力门判否；reason 已在 _complete_capability 的判定点确定。
                await self._send_capability_unavailable(request, capability, exc.reason)
                return
            except ModelError as exc:
                if capability is not None and exc.kind in {
                    "tools_unavailable",
                    "tools_unsupported",
                }:
                    await self._send_capability_unavailable(request, capability, exc.kind)
                    return
                # 只记稳定分类，不带异常正文：超时、401、429、5xx 与非法请求
                # 在日志里必须能分开，而它们此前都塌缩成 error=ModelError 一行
                # （计划 §4 的「模型失败」）。trace_id 用来把同一条消息的
                # 判定、模型失败与发送结果串起来。
                log_event(
                    _logger,
                    logging.WARNING,
                    "app.model_failed",
                    trace_id=request.trace_id,
                    channel_id=request.channel_id,
                    **exc.log_fields(),
                )
                await self._notify_failure(request)
                return
            except Exception as exc:  # 模型最终失败（含重试后仍失败）
                log_event(
                    _logger,
                    logging.WARNING,
                    "app.model_failed",
                    trace_id=request.trace_id,
                    channel_id=request.channel_id,
                    error=type(exc).__name__,
                )
                await self._notify_failure(request)
                return

            # 代次检查之二（§16）：模型调用可能持续几十秒，`/reset` 或线程过期完全
            # 可能在它返回之前发生。若已作废，就**不写历史、也不发这条过期回复** ——
            # 否则它会污染刚清空的会话，并在下一轮被再次外送给模型。
            if self._ctx.generation(request.session_key) != request.generation:
                log_event(
                    _logger,
                    logging.INFO,
                    "app.stale_generation",
                    channel_id=request.channel_id,
                    kind="post_model",
                )
                return

            # 表情归一必须排在自动提取**之前**（设计 §4.5）：短期历史提交的是模型原文，
            # 而披露绝不进历史，所以历史拿到的必须是「已归一、不含披露」的正文，否则会留下
            # 未修正的坏 token，下一轮模型模仿自己。这里传 `max_chars=None`：归一要做，
            # 但**不能**截断——否则 `_auto_capture_answer` 再截一次，正文末尾会出现两个提示。
            rendered, _report = prepare_outbound(
                text,
                redactor=self._redactor,
                table=self._sticker_table,
                max_chars=None,
            )
            if not rendered.strip():
                # 归一后判空：模型整条回复就是一个非法 token，或只剩空白。若放行，空正文会流进
                # `_auto_capture_answer`，自动记忆写入成功时返回「空 body + 非空披露」，于是发出
                # 一条只含记忆披露的回复，并向历史提交一条空的 assistant 轮次（设计 §4.5）。
                # 收口：复查代次后走既有的失败通知；不提取记忆、不发送、不写历史。
                if self._ctx.generation(request.session_key) == request.generation:
                    await self._notify_failure(request)
                return

            # 自动提取（§34.4）就在这一格：主模型已经给出回答、这条回答还没有发出。
            # 位置在代次检查之二**之后**：被 /reset 作废的那一轮连提取都不做（本方法自己
            # 还会再复查一次代次）。它换出来的是**要发出的文本**，历史提交用已归一的
            # `rendered`（不含披露）—— 披露里就是记忆正文，绝不进历史（§33 的红线）。
            # 披露预算按归一后的长度算，`growth` 那套算术不用改。
            send_text = await self._auto_capture_answer(request, rendered)

            # 发送器也是异步边界；/reset 在此期间到达时，旧请求不得再发送。
            # 自动提取本身也是一段异步边界，这个检查因此不只是形式：记忆已经落盘而回复不发的
            # 情况是允许的（见 `_auto_capture_answer` 的取舍说明）。
            if self._ctx.generation(request.session_key) != request.generation:
                return

            outcome = await self._sender.send(
                request.channel_id,
                send_text,
                request.message.id,
                kind="reply",
                thread_root_id=request.thread_root_id,
            )
            if outcome.delivered:
                # 只有用户真的看见了这一轮，才把它写进历史（D-22）。
                if self._ctx.generation(request.session_key) == request.generation:
                    # 历史里只留问题本身：搜索摘要有自己的压缩形式，KB 命中原文
                    # 则完全不保留（D-35 / D-43）——下一轮本来就没有读知识库的授权。
                    # 引用博客的正文同理，只留标记。
                    history_user = self._pending_turn(request, image_state, blog_state)
                    if history_context:
                        history_user = f"{history_user}\n\n{history_context}"
                    # 当前说话人的会话身份随这一轮一起提交（§45.1、§47.4 第 6 条）：**只有**
                    # 真正送达的这一条路径会走到这里 —— 模型失败、额度拒绝、发送失败与代次
                    # 失效都在上面就返回了，因此不会留下一个从没被回答过的参与者。
                    # 记忆未装配时它是 None，与升级前逐字节一致。
                    self._ctx.append_exchange(
                        request.session_key,
                        history_user,
                        rendered,
                        subject=request.public_memory_subject,
                    )
            if outcome.reason == "quota":
                await self._notify_quota(request)
            else:
                self._note_forbidden(outcome)
        finally:
            # §16：无论成功失败都必须标记 done，否则水位永远推进不了。
            await self._store.mark_handled(request.message.id, "done")

    async def _load_image(self, request: Request) -> tuple[dict[str, Any] | None, str]:
        """取回本轮图片并编码；关闭图片输入时**完全不碰图床**。"""
        if not self._vision_enabled:
            return None, "none"
        return await self._image_loader.load(request.message)

    async def _load_reply_image(self, request: Request) -> tuple[dict[str, Any] | None, str]:
        """取回**被引用消息**的缩略图并编码；关闭图片输入时完全不碰图床。

        契约给的只有 URL（没有 id、没有 mime，且是缩略图），因此只能按原样取：
        同源约束与格式嗅探都由 `ImageLoader.load_url` 兜住，取不到就降级成
        `[图片未提供]`（见 `_reply_image_marker`）。
        已删除的引用不取——那条消息的图不再属于任何人（D-48 的判定顺序）。
        """
        if not self._vision_enabled:
            return None, "none"
        url = _reply_image_url(request)
        if url is None:
            return None, "none"
        return await self._image_loader.load_url(url)

    async def _load_blog(self, request: Request) -> BlogLoad:
        """取回本轮引用的博客（正文里的内容引用已展开）；没有引用时完全不碰网络。"""
        return await self._blog_loader.load(request.message)

    async def _resolve_refs(
        self, request: Request
    ) -> tuple[str, str | None, tuple[dict[str, Any], ...], tuple[str, ...]]:
        """展开本轮消息正文与**直接引用**正文里的内容引用（§25）。

        返回 `(消息正文, 直接引用正文, 图片块, 展开成功的引用正文)`。

        - 两块正文各自套用同一个预算（`behavior.content_ref_max_chars`）：它们是
          同一条消息外送时相邻的两段，各自都不超过上限。
        - **历史拿到的仍是用户自己写的原文**（调用方不传这两份展开结果），
          与「引用的博客正文只属当前轮」同一条理由：换回来的是别人写的内容，
          留在历史里会在该会话后续每一轮被反复外送。
        - 第四项是两块正文里**真正取回并渲染成功**的剪贴板正文与投票文本（R10、§49），
          按展开顺序合并。公开记忆的优先级 6 只扫描这一份：预算不足、取回失败或本来就是
          图片的引用都不在里面，因此「没提供给模型的正文不参与匹配」是结构性的（§6.3）。
        """
        budget = self._ref_resolver.max_ref_chars
        user_text, reply_text = _ref_source_texts(request)
        resolved = await self._ref_resolver.resolve(user_text, budget=budget)
        parts = list(resolved.image_parts)
        expanded = list(resolved.expanded_texts)
        if reply_text is not None:
            reply = await self._ref_resolver.resolve(reply_text, budget=budget)
            reply_text = reply.text
            parts.extend(reply.image_parts)
            expanded.extend(reply.expanded_texts)
        return resolved.text, reply_text, tuple(parts), tuple(expanded)

    @staticmethod
    def _enabled_capability(request: Request) -> Capability | None:
        """本轮唯一启用的 MCP 能力；没有则 None。

        D-39 保证一条消息里最多一个能力命令，所以这里按能力表顺序取第一个命中的即可 ——
        顺序固定才让同一份请求在任何进程里都得到同一个答案。
        """
        for spec in CAPABILITIES:
            if spec.source == "mcp" and spec.feature in request.enabled_features:
                return spec
        return None

    async def _complete_capability(
        self,
        model: ModelClient,
        messages: list[dict[str, Any]],
        request: Request,
        capability: Capability,
    ) -> tuple[str, str | None]:
        """执行一轮显式能力授权；MCP 调用不占用模型并发门。"""
        feature_name = capability.feature
        # 每道门都必须给出可判别的 reason：用户看到的都是同一句本地文案，
        # 没有 reason 时"总开关关了 / Provider 没起来 / 没发现工具 / 模型不认 tools"
        # 在日志里完全一样 —— 2026-09-14 的线上排查正是卡在这里。
        if not self._config.mcp.enabled:
            raise CapabilityUnavailable("mcp_disabled")
        registry = self._mcp_manager.registry
        if not registry.feature_available(feature_name):
            raise CapabilityUnavailable("feature_unavailable")
        complete_with_tools = getattr(model, "complete_with_tools", None)
        if not callable(complete_with_tools):
            raise CapabilityUnavailable("model_without_tools")
        # 这里不再判 `feature is None`：上面那道门已经覆盖了它。`feature_available()`
        # 在 feature 不在配置里时直接返回 False（registry.py 的 `_features.get` 判空），
        # 所以「配置根本没写这个 feature 段」走的是 `feature_unavailable`；而「名字写错」
        # 在加载期就已经是 ConfigError（能力表是封闭的，D-81）。留着那条分支只会让后来的人
        # 以为存在一条实际上到不了的失败路径。
        feature = self._config.mcp.features[feature_name]
        tools = tuple(registry.tools_for(feature_name))
        if not tools:
            raise CapabilityUnavailable("no_tools")

        async def execute(call):
            return await registry.execute(
                feature_name,
                call,
                generation_is_current=lambda: (
                    self._ctx.generation(request.session_key) == request.generation
                ),
            )

        kwargs: dict[str, Any] = {
            "tools": tools,
            "execute": execute,
            "max_tool_calls": feature.max_tool_calls_per_turn,
            "generation_is_current": lambda: (
                self._ctx.generation(request.session_key) == request.generation
            ),
        }
        # 兼容测试替身或旧的可选客户端；正式 OpenAI 客户端支持 model_gate，
        # 只在两次模型请求周围占门，等待/执行 MCP 时不占普通聊天槽位。
        try:
            signature = inspect.signature(complete_with_tools)
            accepts_gate = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            ) or "model_gate" in signature.parameters
        except (TypeError, ValueError):
            # 某些代理对象没有可反射签名；它们采用旧兼容路径。
            accepts_gate = False
        if accepts_gate:
            kwargs["model_gate"] = self._model_gate

        if accepts_gate:
            completion = await complete_with_tools(messages, **kwargs)
        else:
            async with self._model_gate:
                completion = await complete_with_tools(messages, **kwargs)
        return completion.text, completion.history_context

    async def _prepare_kb(self, request: Request) -> str | None:
        """执行一轮 `/kb`：访问门 → 检索；返回数据块，本地拒绝时返回 None。

        返回 None 表示这一轮已经在本地收口（发了 `notice_local`，或者请求在检索
        期间被 `/reset` 作废）：调用方必须直接返回，既不调模型也不写历史。
        """
        cfg = self._config.knowledge_base
        if not cfg.enabled:
            await self._send_kb_local(request, "disabled", texts.KB_UNAVAILABLE_TEXT)
            return None
        # 授权判据只用站点稳定 id，不按可改名的用户名（D-44）。
        if not self._kb.permits(
            channel_kind=request.channel_kind, user_id=request.message.author.id
        ):
            await self._send_kb_local(request, "access", texts.KB_ACCESS_DENIED_TEXT)
            return None
        if self._ctx.generation(request.session_key) != request.generation:
            return None
        result = await self._kb.search(request.user_text)
        if result.status != "ok":
            if result.status == "no_results":
                await self._send_kb_local(request, "no_results", texts.KB_NO_RESULTS_TEXT)
            else:
                await self._send_kb_local(request, "unavailable", texts.KB_UNAVAILABLE_TEXT)
            return None
        # 检索结束到调模型之间是另一个异步边界：`/reset` 可能刚好落在这里。
        if self._ctx.generation(request.session_key) != request.generation:
            return None
        # 只记条数与快照版本，不记分类、路径、标题或正文（INTERFACES §23）。
        log_event(
            _logger,
            logging.INFO,
            "kb.query_done",
            count=result.block_count,
            snapshot_version=result.snapshot_version,
            channel_id=request.channel_id,
            channel_kind=request.channel_kind,
        )
        return result.text

    async def _send_kb_local(self, request: Request, reason: str, text: str) -> None:
        """`/kb` 的本地提示：kind=`notice_local`，不占主动通知冷却（D-1）。"""
        if self._ctx.generation(request.session_key) != request.generation:
            return
        log_event(
            _logger,
            logging.INFO,
            "app.kb_unavailable",
            reason=reason,
            channel_id=request.channel_id,
            channel_kind=request.channel_kind,
        )
        if self._unavailable:
            return
        outcome = await self._sender.send(
            request.channel_id,
            text,
            request.message.id,
            kind="notice_local",
            thread_root_id=request.thread_root_id,
        )
        self._note_forbidden(outcome)

    async def _send_capability_unavailable(
        self, request: Request, capability: Capability, reason: str
    ) -> None:
        """显式授权了能力但它不可用时只发本地提示，并记下可判别的 feature 与 reason。"""
        # 只有本地能力没有这句文案，而它们在自己的代码路径上收口（如 `/kb`），不会到这里。
        text = capability.unavailable_text
        if text is None:
            return
        if self._ctx.generation(request.session_key) != request.generation:
            return
        log_event(
            _logger,
            logging.INFO,
            "app.capability_unavailable",
            reason=reason,
            feature=capability.feature,
            channel_id=request.channel_id,
            channel_kind=request.channel_kind,
        )
        outcome = await self._sender.send(
            request.channel_id,
            text,
            request.message.id,
            kind="notice_local",
            thread_root_id=request.thread_root_id,
        )
        self._note_forbidden(outcome)

    async def _send_media_unavailable(self, request: Request, text: str) -> None:
        """整条消息就是引用但读不到：本地提示，不调模型。

        kind 用 `notice_local` 而不是 `notice`：它与路由第 9.2 步的纯媒体提示同类，
        都是应答明确用户动作的本地回复（D-1）。用 notice 会占掉该用户 24 小时的
        主动通知名额，把一次「没读到」变成「今天别再提醒他」（D-30）。
        """
        if self._unavailable:
            return
        outcome = await self._sender.send(
            request.channel_id,
            text,
            request.message.id,
            kind="notice_local",
            thread_root_id=request.thread_root_id,
        )
        self._note_forbidden(outcome)

    @staticmethod
    def _pending_turn(
        request: Request,
        image_state: str,
        blog_state: str,
        *,
        blog_block: str | None = None,
        user_text: str | None = None,
    ) -> str:
        """构造本轮待提交的用户内容（不含直接引用，见 D-7）。

        `blog_block` 只有**外送那一份**才给：引用博客的正文只属于当前轮，历史里只留
        标记（设计 §3.5）。`user_text` 同理——展开过内容引用的正文只属当前轮，
        省略它就退回用户自己写的原文（历史要走这条）。其余两种情况形状逐字相同。

        - 大区：带上站点发言者标签，模型才分得清谁在说话（D-20），
          图片与博客标记在包装**内部**（它们都属于发言人这条消息）；
        - 私聊：就是正文本身。
        """
        text = with_image_marker(
            request.user_text if user_text is None else user_text, image_state
        )
        text = BotApp._with_blog_marker(text, blog_state, blog_block)
        if request.channel_kind != "lobby":
            return text
        return speaker_wrapper(request.message.author.username, text)

    @staticmethod
    def _with_blog_marker(
        user_text: str, blog_state: str, blog_block: str | None = None
    ) -> str:
        """给本轮正文加上引用博客的块（外送版）或标记（历史版）。

        块与标记只差「正文给不给」这一处，两者都必须留痕：只把块去掉的话，历史里
        会出现「助手在回答一篇看不见的文章」这种对不上的轮次；正文为空时更糟 ——
        那一轮的历史会直接变成空的（D-28 的同一条理由）。
        """
        if blog_state == "none":
            return user_text
        head = blog_block if blog_block is not None else blog_marker(blog_state)
        if not head:
            return user_text
        if not user_text:
            return head
        return f"{head}\n---\n{user_text}"

    @staticmethod
    def _reply_image_marker(image_state: str) -> str:
        """被引用消息的图片标记：取到了 `[图片]`，本来有图但没取到 `[图片未提供]`。

        视觉关闭时状态是 `"none"`（我们连试都没试），此时仍是原来的 `[图片]`：
        那个标记本来就只说明「被引用的是图片消息」，改动前的形状逐字保留。
        """
        return (
            _REPLY_IMAGE_MARKER
            if image_state in {"none", "ok"}
            else _REPLY_IMAGE_NOT_PROVIDED
        )

    @staticmethod
    def _reply_prefix(
        request: Request,
        *,
        reply_body: str | None = None,
        reply_image_state: str = "none",
    ) -> str | None:
        """构造本轮的直接引用前缀；没有引用块时返回 None。

        这是一个**纯文本**步骤（§11.1）：调用方把它拼在 `pending_user` 之前，于是引用
        与当前正文一起参与这一轮的 token 预算。引用文本**绝不**写进 `ContextManager`
        历史，否则同一段引用会在该会话后续每一轮被反复外送；它只属于引用它的那一轮
        （设计文档 §2.2.4「当前 reply_to 文本」）。即使被引用正文已在历史里也仍然保留
        这份前缀：有限的重复优于丢失当前指向。

        大区与私聊用不同的标签（D-25）：只有大区是「直接引用」——
        它的历史里本来就有别的发言者，需要与发言者标签区分开。

        三种边角也要留痕，否则模型看到的就是一句没头没尾的话：被引用的是图片、
        被引用消息已删除、被引用消息没有正文（例如一条拍一拍）。
        `is_deleted` 的判定**先于** `content`：契约没承诺 reply 块里的正文一定被
        替换过，所以自己给标记，不把可能残留的原文转述给模型。

        图片标记与被引用消息的正文并列：整条引用就是一张图时标记本身就是正文，
        正文之外还带图时标记补在正文**后面**（D-48 的三种边角加上这一条组合）。
        """
        reply = request.message.reply
        if reply is None:
            return None
        context = request.reply_context if reply_body is None else reply_body
        marker = BotApp._reply_image_marker(reply_image_state)
        if reply.is_deleted:
            body = _REPLY_DELETED_MARKER
        elif context:
            body = f"{context} {marker}" if reply.image_url else context
        elif reply.image_url:
            body = marker
        else:
            body = _REPLY_EMPTY_MARKER
        author = reply.author_name
        label = "直接引用" if request.channel_kind == "lobby" else "引用"
        header = f"[{label} @{author}]" if author else f"[{label}]"
        return f"{header} {body}"

    # --- 运行期清理 ---------------------------------------------------------

    async def _prune_once(self) -> None:
        """清理一次过期运行状态；失败只记录并等下一周期（D-23）。

        过期链的内存上下文必须**同时失效**：否则一个在途请求会把正文写回
        已经过期的链，下一个人回复旧消息时又变成新链，上下文对不上。
        """
        try:
            result = await self._store.prune_runtime_state(
                now=time.time(), cfg=self._config.storage
            )
        except Exception as exc:
            log_event(_logger, logging.ERROR, "app.cleanup_failed", error=type(exc).__name__)
            return

        for root in result.expired_thread_roots:
            self._ctx.invalidate(lobby_thread_session_key(root))
        # 评论会话同样只有内存历史；Store 清理返回已过期 conversation id 后
        # 立即递增对应代次，作废仍在模型门/发送器中的旧请求，避免清理后幽灵
        # assistant 轮次重新写回新链。兼容 A 组暂未扩展该字段的旧结果。
        expired_comment_ids = getattr(result, "expired_comment_conversation_ids", ())
        if expired_comment_ids:
            if self._comment_service is not None:
                self._comment_service.invalidate_conversations(expired_comment_ids)
            else:
                for conversation_id in expired_comment_ids:
                    if isinstance(conversation_id, str) and conversation_id:
                        key = (
                            conversation_id
                            if conversation_id.startswith("comment:")
                            else f"comment:{conversation_id}"
                        )
                        self._comment_ctx.invalidate(key)

        deleted = (
            len(result.expired_thread_roots)
            + result.deleted_events
            + result.deleted_sent_replies
            + result.deleted_send_attempts
            + result.deleted_cooldowns
            + result.deleted_dm_channels
        )
        limit = self._config.storage.sqlite_soft_limit_bytes
        log_event(
            _logger,
            logging.INFO,
            "app.cleanup_done",
            count=deleted,
            thread_root_id=None,
            size_bytes=result.db_logical_bytes,
            limit_bytes=limit,
        )
        if result.db_logical_bytes > limit:
            # 软上限只告警：硬截断会让去重、水位或配额写入突然失败（D-23）。
            log_event(
                _logger,
                logging.ERROR,
                "app.cleanup_oversize",
                size_bytes=result.db_logical_bytes,
                limit_bytes=limit,
            )

    async def _cleanup_loop(self) -> None:
        """按 `storage.cleanup_interval_seconds` 周期清理，直到被取消。"""
        while True:
            await asyncio.sleep(self._config.storage.cleanup_interval_seconds)
            await self._prune_once()

    # --- resync 与不可用探测 ------------------------------------------------

    def _schedule_resync(self) -> None:
        """后台触发一次 resync，绝不阻塞 SSE 循环。"""
        if self._resync_task is not None and not self._resync_task.done():
            return  # 已有一次在跑，避免 resync 风暴
        self._resync_task = asyncio.create_task(self._resync(), name="bot-resync")
        observe_task(self._resync_task, "resync")

    async def _resync(self) -> None:
        """拉取大区与已知私聊的最新 100 条，合并去重后交给同一套路由。"""
        try:
            batches: list[tuple[str, list[ChatMessage]]] = [
                (LOBBY, await self._client.fetch_messages(LOBBY, limit=100))
            ]
            for channel_id in await self._store.dm_channels():
                batches.append(
                    (channel_id, await self._client.fetch_messages(channel_id, limit=100))
                )

            router = self._router
            for channel_id, message in self._merge_messages(batches):
                if router is None:
                    break
                # 与实时流共用同一套判定与去重；本地回复同样要分派出去，
                # 否则补发回来的 /help 之类会永远停在 pending 而无人应答。
                result = await router.handle_message(channel_id, message, None)
                await self._dispatch(result)

            # resync 之后把水位回写给 SSE，作为下一次重连的 Last-Event-ID。
            sse = self._sse
            if sse is not None:
                sse.set_last_event_id(await self._store.advance_watermark())
            log_event(_logger, logging.INFO, "app.resync_done")
        except SiteError as exc:
            log_event(_logger, logging.WARNING, "app.resync_failed", error=type(exc).__name__)
            if exc.status == 403:
                self._enter_unavailable()
        except Exception as exc:  # resync 失败不得影响实时流
            log_event(
                _logger, logging.WARNING, "app.resync_failed", error=type(exc).__name__
            )

    @staticmethod
    def _merge_messages(
        batches: Iterable[tuple[str, list[ChatMessage]]],
    ) -> list[tuple[str, ChatMessage]]:
        """按全局 message.id 升序合并各频道结果，并按 id 去重。"""
        seen: dict[int, tuple[str, ChatMessage]] = {}
        for channel_id, messages in batches:
            for message in messages:
                seen.setdefault(message.id, (channel_id, message))
        return [seen[message_id] for message_id in sorted(seen)]

    def _enter_unavailable(self) -> None:
        """进入 403 不可用状态，并确保探测 task 在跑（D-4）。"""
        if self._unavailable or self._stopped:
            return
        self._unavailable = True
        log_event(_logger, logging.ERROR, "app.unavailable", status=403)
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop(), name="bot-probe")
            observe_task(self._probe_task, "probe")

    async def _probe_loop(self) -> None:
        """每 `ready_probe_seconds` 探测一次；恢复后解除不可用标志。

        探测期间不刷日志：每个探测周期最多一行（失败一行 / 恢复一行）。
        """
        try:
            while not self._stopped and self._unavailable:
                await asyncio.sleep(self._config.behavior.ready_probe_seconds)
                if self._stopped or not self._unavailable:
                    return
                try:
                    await self._client.probe_chat()
                except Exception as exc:
                    log_event(
                        _logger,
                        logging.WARNING,
                        "app.probe_failed",
                        error=type(exc).__name__,
                    )
                    continue
                self._unavailable = False
                log_event(_logger, logging.INFO, "app.probe_ok")
                return
        except asyncio.CancelledError:
            raise

    def _note_forbidden(self, outcome: SendResult) -> None:
        """按 403 的细分 reason 分流（D-4）。

        - `forbidden`：权限不足/被禁言 → 进入不可用状态并周期探测；
        - `csrf`：跨源被拒，说明是我方错误设置了 `Origin`/`Referer`，属于程序缺陷。
          它永远不会自行恢复，进不可用并每 5 分钟空探一次只会把永久缺陷伪装成临时
          权限问题，因此这里只记一条 error 级日志，机器人继续正常工作。
        """
        if outcome.reason == "forbidden":
            self._enter_unavailable()
        elif outcome.reason == "csrf":
            log_event(_logger, logging.ERROR, "app.csrf_rejected")

    # --- 关闭实现 -----------------------------------------------------------

    async def _shutdown(self) -> None:
        """按 §34.2 的顺序停各组件；每一步都容忍组件尚未构造。

        顺序是权威版本（§34.2 覆盖 §16 / §16.1 里那些更细的既有动作）：
        评论服务与 SSE → 主聊天 worker 与记忆 worker → 记忆刷新任务 → OpsServer / MCP / KB /
        模型客户端 / SiteClient / Store。
        **记忆 worker 必须早于模型客户端关闭**：在途的 AI 撰写会访问那个已关闭的客户端。
        """
        # 发文子域**最先**停（§53.12）：它同时用 MCP 与模型客户端，而这两者都在后面几步关闭；
        # 晚停就会在已经关掉的客户端上发起投递。`stop()` 只取消、不等 writer 自然结束 ——
        # 本函数的 10 秒总预算装不下一个 180 秒的生成。
        blog_service, self._blog_service = self._blog_service, None
        if blog_service is not None:
            await blog_service.stop()
        # 评论 poller 先停，禁止在聊天组件关闭期间再产生新的候选任务。
        comment_service, self._comment_service = self._comment_service, None
        if comment_service is not None:
            await comment_service.stop()
        sse, self._sse = self._sse, None
        if sse is not None:
            await sse.stop()
        await self._cancel_task("_sse_task")
        await self._cancel_task("_resync_task")
        await self._cancel_task("_probe_task")
        # 清理 task 必须先结束：晚了它会在 Store 关闭后继续访问数据库。
        await self._cancel_task("_cleanup_task")

        await self._workers.stop()
        # 记忆 worker 与主聊天 worker 同一阶段停下；此时没有新的记忆命令会被领取。
        await self._memory_workers.stop()
        # 记忆刷新 task 在 worker 之后停（§34.2 第 3 步）：刷新的只是内存快照，
        # 但它要早于模型客户端与 Store，否则末次刷新会撞上已经关掉的东西。
        # 与启动同一条口径：关闭时连 stop() 都不必调（那时服务根本没被启动过，D-60）。
        if self._memory_enabled:
            service = self._memory_service
            if service is not None:
                await service.stop()

        await self._ops.stop()
        await self._mcp_manager.stop()
        await self._kb.stop()

        if self._owns_model and self._model is not None:
            await self._model.aclose()
        await self._client.aclose()
        # 发送器的受取消保护终结（store.record_sent + quota.note_sent）可能仍有在途任务：
        # 正常取消已由 send 在传播前等待到位，重复取消则可能让调用方提前退出，留下仍在
        # 写库的终结任务。Store 关闭前统一汇合，避免它们撞上已关闭的连接；等待是**有界**的
        # （sender 侧 `_SETTLE_WAIT_TIMEOUT_SECONDS`，正常只是最多两笔 SQLite 写），超时便放弃
        # 等待、让关闭继续，仍在 10 秒总预算内（计划 §4.2 第 6 条）。
        await self._sender.wait_settled()
        await self._store.close()

    async def _cancel_task(self, attribute: str) -> None:
        """取消并等待一个可选的后台 task。"""
        task: asyncio.Task[None] | None = getattr(self, attribute)
        setattr(self, attribute, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
