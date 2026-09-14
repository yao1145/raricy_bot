"""应用装配：把配置、站点客户端、SSE、路由、工作器、发送器与运维端点接起来。

组件本身的逻辑都在各自模块里，本模块只负责**编排**（INTERFACES §16）：

- 启动顺序：`Store.open` → `SiteClient.start` + `login` → 构造 `SSEReceiver`
  → **用 `store.watermark()` 播种 `Last-Event-ID`（D-16）** → `WorkerPool.start`
  → `OpsServer.start` → `sse.run()` 作为后台 task；
- 路由结果分派：`reply_now` 用 `notice_local`、`busy` 用 `notice` 并加 (频道, 触发者) 冷却；
- 模型失败的 `failure` 通知同样用 `notice` + 触发者冷却；额度用尽补发一次 `quota` 通知；
- 403 进入不可用状态，按 `ready_probe_seconds` 探测恢复（D-4）；
- 优雅关闭总超时 10 秒，`stop()` 可重复调用。

日志只写白名单字段，绝不写正文、Cookie、密码或 API Key（§19 红线）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Iterable
from typing import Any

import httpx

from . import texts
from .comments.quota import CommentQuotaGuard
from .comments.router import CommentRouter
from .comments.sender import CommentSender
from .comments.service import CommentService
from .config import Config
from .core.context import ContextManager, lobby_thread_session_key, speaker_wrapper
from .core.router import MessageRouter, Request, RouteResult
from .core.sender import MessageSender, SendResult
from .core.vision import ImageLoader, attach_image
from .core.worker import (
    ModelClient,
    ModelError,
    OpenAIModelClient,
    ToolGenerationCancelled,
    WorkerPool,
)
from .logging_setup import get_logger, log_event
from .mcp.exa import ExaSearchAdapter, SearchLimiter
from .mcp.registry import model_tool_name
from .mcp.runtime import McpManager
from .ops import OpsServer
from .quota import QuotaGuard, notice_cooldown_key
from .redact import Redactor
from .site.client import SiteClient, SiteError
from .site.models import LOBBY, ChatMessage
from .site.sse import SSEReceiver
from .store import Store

_logger = get_logger("app")

# 优雅关闭总超时（秒）；超时后不再等待，交由进程退出兜底。
_SHUTDOWN_TIMEOUT_SECONDS: float = 10.0


class SearchUnavailable(Exception):
    """搜索授权前的本地能力门判否，并携带可判别的稳定原因码。

    它不是 ``ModelError``：模型压根没被调用，且用户侧文案相同、只有原因不同。
    用独立的异常类型可以让"我们自己的门"和"模型端点说它不支持 tools"
    在日志里各记一行、互不重复。
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class BotApp:
    """一个机器人实例的完整装配与生命周期。"""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        model_client: ModelClient | None = None,
        mcp_manager: McpManager | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        # 图片输入：默认关闭。关闭时 worker 完全不碰 ImageLoader，
        # 进程里也就没有任何新增的图床请求（设计 §3.5）。
        self._vision_enabled = config.model.vision_enabled

        # 只登记真正的机密：密码与模型 Key；机器人用户名不是机密（redact.py §3）。
        self._redactor = Redactor(
            [config.secrets.password, config.secrets.llm_api_key]
        )

        self._store = Store(
            config.storage.db_path,
            wal_journal_limit_bytes=config.storage.wal_journal_limit_bytes,
            conversation_retention_seconds=(
                config.comments.conversation_retention_seconds
            ),
            dedupe_retention_seconds=config.comments.dedupe_retention_seconds,
        )
        client_kwargs: dict[str, object] = {
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
        self._queue: asyncio.Queue[Request] = asyncio.Queue(
            maxsize=config.behavior.queue_size
        )
        self._ctx = ContextManager(
            config.behavior.context_turns, config.behavior.context_input_tokens
        )
        # 聊天和评论共用同一模型门；评论 worker 仍保持自身 concurrency=1。
        self._model_gate = asyncio.Semaphore(config.behavior.concurrency)
        self._quota = QuotaGuard(self._store, config.behavior)
        self._sender = MessageSender(
            client=self._client,
            store=self._store,
            quota=self._quota,
            redactor=self._redactor,
            cfg=config.behavior,
        )

        # 注入的假模型不归本对象关闭；内部构造的才需要 aclose。
        self._model = model_client
        self._owns_model = model_client is None
        self._mcp_manager = (
            mcp_manager if mcp_manager is not None else self._build_mcp_manager()
        )

        self._workers = WorkerPool(
            queue=self._queue,
            handler=self._handle_request,
            concurrency=config.behavior.concurrency,
        )
        self._ops = OpsServer(
            config.ops.host,
            config.ops.port,
            livez=lambda: self.live,
            readyz=lambda: self.ready,
        )

        # 下面这些在 start() 里按装配顺序建立。
        self._router: MessageRouter | None = None
        self._sse: SSEReceiver | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._resync_task: asyncio.Task[None] | None = None
        self._probe_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
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

    def _build_mcp_manager(self) -> McpManager:
        """装配 Exa feature 适配器；Provider/Registry 本身保持通用。"""
        feature = self._config.mcp.features.get("search")
        adapters: dict[str, Any] = {}
        if feature is not None:
            limiter = SearchLimiter(feature.min_interval_seconds)
            adapter = ExaSearchAdapter(
                result_count=feature.result_count,
                result_item_token_limit=feature.result_item_token_limit,
                history_item_token_limit=feature.history_item_token_limit,
                max_query_chars=feature.max_query_chars,
                limiter=limiter,
            )
            for binding in feature.bindings:
                if binding.tool == "web_search_exa":
                    adapters[model_tool_name(binding.server, binding.tool)] = adapter.adapt
        return McpManager(
            self._config.mcp,
            redactor=self._redactor,
            adapters=adapters,
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

        self._router = MessageRouter(
            self_user_id=user.id,
            bot_username=self._config.secrets.username,
            ctx=self._ctx,
            store=self._store,
            queue=self._queue,
            cfg=self._config.behavior,
            storage=self._config.storage,
            vision_enabled=self._vision_enabled,
        )
        if self._model is None:
            self._model = OpenAIModelClient(
                self._config.model,
                self._config.secrets.llm_api_key,
                redactor=self._redactor,
                transport=self._transport,
            )
        # MCP 是可选扩展：任何连接、发现或子进程错误只让对应 feature 不可用，
        # 不得阻止普通聊天、评论或健康端点启动。
        try:
            await self._mcp_manager.start()
        except Exception as exc:
            log_event(_logger, logging.WARNING, "app.mcp_start_failed", error=type(exc).__name__)

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
                )
                self._comment_router = CommentRouter(
                    self_user_id=user.id,
                    bot_username=self._config.secrets.username,
                    ctx=self._comment_ctx,
                    store=self._store,
                    queue=self._comment_router_queue,
                    cfg=self._config.comments,
                    now=time.time,
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
                )
                await self._comment_service.start()
            except BaseException:
                # 评论初始化失败不能回滚聊天服务；live 会保持 false，日志只记稳定错误。
                log_event(_logger, logging.ERROR, "app.comment_start_failed")
                self._comment_service = None

        self._sse_task = asyncio.create_task(self._sse.run(), name="bot-sse")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="bot-cleanup")
        self._started = True
        log_event(_logger, logging.INFO, "app.started")

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

    # --- SSE 事件分派 -------------------------------------------------------

    async def _on_event(self, event) -> None:
        """把一帧交给路由器，并按 RouteResult 分派副作用。"""
        router = self._router
        if router is None:
            return
        result = await router.handle_stream(event)
        await self._dispatch(result)

    async def _dispatch(self, result: RouteResult) -> None:
        """按 §16 分派路由结果；`ignored` / `queued` 无需在此做事。"""
        if result.action == "reply_now":
            await self._send_local(result)
        elif result.action == "busy":
            await self._send_busy(result)
        elif result.action == "resync":
            self._schedule_resync()

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
            if image_part is None and image_state != "none" and not request.user_text:
                # 纯图但读不到：用户明确发来一张图，必须给个交代，
                # 但不值得为它占用一次模型调用。
                await self._send_image_unavailable(request)
                return
            # D-22：本轮内容先**临时**拼给模型，只有回复真正送达才提交进历史。
            pending = self._pending_turn(request, image_state)
            system_addenda: list[str] = []
            if request.channel_kind == "lobby":
                system_addenda.append(texts.LOBBY_SHARED_SYSTEM_ADDENDUM)
            if "search" in request.enabled_features:
                system_addenda.append(texts.MCP_SEARCH_SYSTEM_ADDENDUM)
            messages = self._ctx.build_messages(
                request.session_key,
                self._config.system_prompt,
                pending_user=pending,
                system_addendum="\n\n".join(system_addenda) or None,
            )
            self._apply_reply_prefix(messages, request)
            # 必须排在 _apply_reply_prefix 之后：那一步按字符串拼接 content。
            if image_part is not None:
                attach_image(messages, image_part)
            model = self._model
            if model is None:
                if "search" in request.enabled_features:
                    await self._send_search_unavailable(request, "model_missing")
                else:
                    await self._notify_failure(request)
                return
            history_context: str | None = None
            try:
                if "search" in request.enabled_features:
                    text, history_context = await self._complete_search(
                        model, messages, request
                    )
                else:
                    async with self._model_gate:
                        text = await model.complete(messages)
            except ToolGenerationCancelled:
                # /reset 在工具循环的任一异步边界作废了本轮；不发通知、不写历史。
                return
            except SearchUnavailable as exc:
                # 本地能力门判否；reason 已在 _complete_search 的判定点确定。
                await self._send_search_unavailable(request, exc.reason)
                return
            except ModelError as exc:
                if "search" in request.enabled_features and exc.kind in {
                    "tools_unavailable",
                    "tools_unsupported",
                }:
                    await self._send_search_unavailable(request, exc.kind)
                    return
                log_event(
                    _logger,
                    logging.WARNING,
                    "app.model_failed",
                    channel_id=request.channel_id,
                    error=type(exc).__name__,
                )
                await self._notify_failure(request)
                return
            except Exception as exc:  # 模型最终失败（含重试后仍失败）
                log_event(
                    _logger,
                    logging.WARNING,
                    "app.model_failed",
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

            # 发送器也是异步边界；/reset 在此期间到达时，旧请求不得再发送。
            if self._ctx.generation(request.session_key) != request.generation:
                return

            outcome = await self._sender.send(
                request.channel_id,
                text,
                request.message.id,
                kind="reply",
                thread_root_id=request.thread_root_id,
            )
            if outcome.delivered:
                # 只有用户真的看见了这一轮，才把它写进历史（D-22）。
                if self._ctx.generation(request.session_key) == request.generation:
                    history_user = pending
                    if history_context:
                        history_user = f"{pending}\n\n{history_context}"
                    self._ctx.append_exchange(request.session_key, history_user, text)
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

    async def _complete_search(
        self,
        model: ModelClient,
        messages: list[dict[str, Any]],
        request: Request,
    ) -> tuple[str, str | None]:
        """执行一轮显式搜索授权；MCP 调用不占用模型并发门。"""
        # 每道门都必须给出可判别的 reason：用户看到的都是同一句本地文案，
        # 没有 reason 时"总开关关了 / Provider 没起来 / 没发现工具 / 模型不认 tools"
        # 在日志里完全一样 —— 2026-09-14 的线上排查正是卡在这里。
        if not self._config.mcp.enabled:
            raise SearchUnavailable("mcp_disabled")
        registry = self._mcp_manager.registry
        if not registry.feature_available("search"):
            raise SearchUnavailable("feature_unavailable")
        complete_with_tools = getattr(model, "complete_with_tools", None)
        if not callable(complete_with_tools):
            raise SearchUnavailable("model_without_tools")
        feature = self._config.mcp.features.get("search")
        if feature is None:
            raise SearchUnavailable("feature_missing")
        tools = tuple(registry.tools_for("search"))
        if not tools:
            raise SearchUnavailable("no_tools")

        async def execute(call):
            return await registry.execute(
                "search",
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

        try:
            if accepts_gate:
                completion = await complete_with_tools(messages, **kwargs)
            else:
                async with self._model_gate:
                    completion = await complete_with_tools(messages, **kwargs)
        except ModelError as exc:
            if exc.kind == "bad_request" and getattr(model, "tools_unsupported", False):
                raise ModelError("tools_unsupported", False) from exc
            raise
        return completion.text, completion.history_context

    async def _send_search_unavailable(self, request: Request, reason: str) -> None:
        """搜索显式授权但能力不可用时只发本地提示，并记下可判别的 reason。"""
        if self._ctx.generation(request.session_key) != request.generation:
            return
        log_event(
            _logger,
            logging.INFO,
            "app.search_unavailable",
            reason=reason,
            channel_id=request.channel_id,
            channel_kind=request.channel_kind,
        )
        outcome = await self._sender.send(
            request.channel_id,
            texts.SEARCH_UNAVAILABLE_TEXT,
            request.message.id,
            kind="notice_local",
            thread_root_id=request.thread_root_id,
        )
        self._note_forbidden(outcome)

    async def _send_image_unavailable(self, request: Request) -> None:
        """纯图但读不到：本地提示，不调模型。

        kind 用 `notice_local` 而不是 `notice`：它与路由第 9.1 步的纯媒体提示同类，
        都是应答明确用户动作的本地回复（D-1）。用 notice 会占掉该用户 24 小时的
        主动通知名额，把一次「图没读到」变成「今天别再提醒他」（D-30）。
        """
        if self._unavailable:
            return
        outcome = await self._sender.send(
            request.channel_id,
            texts.IMAGE_UNAVAILABLE_TEXT,
            request.message.id,
            kind="notice_local",
            thread_root_id=request.thread_root_id,
        )
        self._note_forbidden(outcome)

    @staticmethod
    def _with_image_marker(user_text: str, image_state: str) -> str:
        """给本轮正文加上图片标记（设计 §4）。

        `[图片]` 表示这一轮确实带了图；`[图片未提供]` 表示本来有图但没取到 ——
        让模型知道自己没看到图，而不是以为用户什么都没发。
        """
        if image_state == "ok" and user_text:
            return f"[图片]\n---\n{user_text}"
        if image_state == "ok":
            return "[图片]"
        if image_state != "none" and user_text:
            return f"[图片未提供]\n---\n{user_text}"
        return user_text

    @staticmethod
    def _pending_turn(request: Request, image_state: str) -> str:
        """构造本轮待提交的用户内容（不含直接引用，见 D-7）。

        - 大区：带上站点发言者标签，模型才分得清谁在说话（D-20），
          图片标记在包装**内部**（图属于发言人这条消息）；
        - 私聊：就是正文本身。
        """
        text = BotApp._with_image_marker(request.user_text, image_state)
        if request.channel_kind != "lobby":
            return text
        return speaker_wrapper(request.message.author.username, text)

    def _apply_reply_prefix(
        self, messages: list[dict[str, str]], request: Request
    ) -> None:
        """把当前轮的**直接引用**拼到最后一条 user 消息上（D-7）。

        引用文本**绝不**写进 `ContextManager` 历史，否则同一段引用会在该会话后续
        每一轮被反复外送；它只属于引用它的那一轮（设计文档 §2.2.4「当前 reply_to 文本」）。
        即使被引用正文已在历史里也仍然保留这份前缀：有限的重复优于丢失当前指向。
        """
        prefix = self._reply_prefix(request)
        if not prefix:
            return
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "user":
                original = messages[index].get("content", "")
                messages[index] = {
                    "role": "user",
                    "content": f"{prefix}\n---\n{original}",
                }
                return

    @staticmethod
    def _reply_prefix(request: Request) -> str | None:
        """构造本轮的直接引用前缀；没有引用上下文时返回 None。

        大区与私聊用不同的标签（D-25）：只有大区是「直接引用」——
        它的历史里本来就有别的发言者，需要与发言者标签区分开。
        """
        context = request.reply_context
        if not context:
            return None
        reply = request.message.reply
        author = reply.author_name if reply is not None else None
        label = "直接引用" if request.channel_kind == "lobby" else "引用"
        header = f"[{label} @{author}]" if author else f"[{label}]"
        return f"{header} {context}"

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
        """按顺序停各组件；每一步都容忍组件尚未构造。"""
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
        await self._ops.stop()
        await self._mcp_manager.stop()

        if self._owns_model and self._model is not None:
            await self._model.aclose()
        await self._client.aclose()
        await self._store.close()

    async def _cancel_task(self, attribute: str) -> None:
        """取消并等待一个可选的后台 task。"""
        task: asyncio.Task[None] | None = getattr(self, attribute)
        setattr(self, attribute, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
