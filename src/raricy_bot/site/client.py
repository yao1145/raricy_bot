"""站点 HTTP 客户端：登录、探活、读消息、发消息与 SSE 长连接。

两个硬约束（chat-bot.md §3.1、§6.5）：

- 会话 Cookie 用**显式请求头**管理，不依赖 httpx 的 cookie jar；
- **绝不**设置 `Origin` / `Referer`（伪造会被 CSRF 校验判为跨源而 403），
  也**不**自行设置 `Accept-Encoding`。

所有响应体都按统一信封解析，成功判据只看 `code == 200`（§4、§7.2）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import urllib.parse
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx

from ..logging_setup import get_logger, log_event, register_secret
from ..redact import Redactor
from .comment_models import (
    BlogContext,
    CommentNode,
    CommentNotification,
    CommentTreeTooLarge,
    NotificationPage,
    count_comment_nodes,
    normalize_uuid,
    strip_comment_content,
)
from .models import LOBBY, Author, ChatMessage, Clipboard, Vote, _parse_author

# 会话 Cookie 名（chat-bot.md §3）。
SESSION_COOKIE_NAME: str = "raricy_session"

# SSE 长连接的**读**超时单独放宽（连接/写入/池仍用普通 timeout）。
# 服务端只在建连时发一帧注释，之后安静时段可能几十分钟没有字节（chat-bot.md §6.1）；
# 沿用 20 秒读超时会把「没人说话」当成断线，形成纯粹由静默驱动的空转重连。
# 5 分钟只用于发现半开连接（对端已死），不会因为无人发言而重连。
STREAM_READ_TIMEOUT_SECONDS: float = 300.0

_LOGIN_PATH: str = "/api/auth/login"
_ME_PATH: str = "/api/auth/me"
_STREAM_PATH: str = "/api/chat/stream"
_CHANNELS_PREFIX: str = "/api/chat/channels"
_COMMENTS_PREFIX: str = "/api/blogs"
_SPIDER_COMMENTS_PATH: str = "/api/spider/comments"
_SPIDER_BLOGS_PREFIX: str = "/api/spider/blogs"
_NOTIFICATIONS_PATH: str = "/api/notifications"
_CLIPBOARD_PREFIX: str = "/api/clipboard"
_VOTES_PREFIX: str = "/api/votes"
_IMAGES_PREFIX: str = "/api/images"
_MAX_COMMENT_RESPONSE_BYTES: int = 8 * 1024 * 1024
_MAX_COMMENT_TREE_NODES: int = 10000

# 内容引用 ID 的长度即类型（内容引用语法 §三）：8 位剪贴板、9 位投票、10 位图床图片。
CLIPBOARD_ID_LEN: int = 8
VOTE_ID_LEN: int = 9
IMAGE_ID_LEN: int = 10


def _messages_path(channel_id: str) -> str:
    """频道消息接口路径。"""
    return f"{_CHANNELS_PREFIX}/{channel_id}/messages"


def _content_id(value: str, length: int, *, kind: str) -> str:
    """校验内容引用 ID：长度固定、只含 ASCII 字母与数字（区分大小写）。

    形态不对就抛 ValueError、**不发请求**——与 `normalize_uuid` 对脏 id 的处理同款。
    这道校验同时是 URL 安全的那道门：ID 会被拼进路径，收紧到「只可能是字母数字」
    就注入不进任何东西。
    """
    if (
        not isinstance(value, str)
        or len(value) != length
        or not value.isascii()
        or not value.isalnum()
    ):
        raise ValueError(f"{kind} id 形态不合法")
    return value


def image_raw_path(image_id: str) -> str:
    """图床直链的相对路径（内容引用语法 §三）。

    站点前端也是直接拼这条路径、不做额外请求，所以图片引用**不消耗**任何接口调用。
    """
    normalized = _content_id(image_id, IMAGE_ID_LEN, kind="image")
    return f"{_IMAGES_PREFIX}/{normalized}/raw"


def _parse_retry_after(response: httpx.Response) -> float | None:
    """解析 `Retry-After` 的秒数形式（整数或小数，非负且有限）。

    HTTP-date 等形式无法解析，返回 None，由调用方退回默认等待时长；
    负数与非有限值（`inf`/`nan`）同样视为不可用，避免把退避时长拉成无穷。
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _effective_port(parsed: urllib.parse.SplitResult) -> int | None:
    """解析有效端口；非法端口（`:abc`、越界值）返回 None，从而判为不同源。"""
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        return port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


class SiteError(Exception):
    """站点接口错误；`status` 为站点 code，0 表示网络层错误（结果不确定）。"""

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class ImageFetchError(Exception):
    """图片取回失败。

    `reason` 是稳定短标识，供调用方判定降级路径与写日志用：
    - `host_not_allowed`：目标与站点不同源（**未发出任何请求**）
    - `too_large`：字节数超过调用方给定的上限
    - `http`：站点返回非 200（`status` 为 HTTP 状态码；空响应体也算）
    - `network`：传输层错误或超时
    """

    def __init__(self, reason: str, status: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class SiteClient:
    """站点接口客户端；所有请求共用同一份显式 Cookie 与超时。"""

    def __init__(
        self,
        base_url: str,
        redactor: Redactor,
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        username: str = "",
        password: str = "",
        max_response_bytes: int = _MAX_COMMENT_RESPONSE_BYTES,
        max_tree_nodes: int = _MAX_COMMENT_TREE_NODES,
    ) -> None:
        # username/password 是对 INTERFACES §7 构造签名的**追加**关键字参数：
        # 锁定签名里没有携带凭据的位置，而 login() 需要用它发登录请求。
        self._base_url = base_url.rstrip("/")
        self._redactor = redactor
        self._timeout = timeout
        self._transport = transport
        self._username = username
        self._password = password
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
            raise ValueError("max_response_bytes 必须是整数")
        if not 1 <= max_response_bytes <= _MAX_COMMENT_RESPONSE_BYTES:
            raise ValueError("max_response_bytes 超出 8 MiB 硬上限")
        if isinstance(max_tree_nodes, bool) or not isinstance(max_tree_nodes, int):
            raise ValueError("max_tree_nodes 必须是整数")
        if not 1 <= max_tree_nodes <= _MAX_COMMENT_TREE_NODES:
            raise ValueError("max_tree_nodes 超出 10000 节点硬上限")
        self._max_response_bytes = max_response_bytes
        self._max_tree_nodes = max_tree_nodes
        # 只有密码算机密（INTERFACES §3）；用户名**不得**注册，否则日志与出站文本里
        # 机器人自己的名字会被抹成 [redacted]。登记密码是为了让服务端回显时也不进异常文案。
        self._redactor.add_secret(password)
        self._client: httpx.AsyncClient | None = None
        self._user: Author | None = None
        self._session_cookie: str | None = None
        self._login_lock = asyncio.Lock()
        self._logger = get_logger("site")

    async def start(self) -> None:
        """创建底层 httpx 客户端；重复调用不会重复创建。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            )

    async def aclose(self) -> None:
        """关闭底层客户端；未启动或重复调用都安全。"""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    @property
    def self_user(self) -> Author | None:
        """登录后的自身用户（未登录为 None）。"""
        return self._user

    @property
    def logged_in(self) -> bool:
        """是否已登录。"""
        return self._user is not None

    @property
    def session_cookie(self) -> str | None:
        """当前会话 Cookie 值；仅供测试与装配核对，不得写日志。"""
        return self._session_cookie

    async def login(self) -> Author:
        """登录；并发调用共享同一次登录请求（单飞）。"""
        async with self._login_lock:
            if self._user is not None:
                return self._user
            return await self._authenticate()

    async def ensure_session(self) -> Author:
        """探活会话；401 时重新登录一次后重试，仍失败则抛 SiteError。"""
        try:
            return await self._fetch_me()
        except SiteError as exc:
            if exc.status != 401:
                raise
        await self._relogin()
        return await self._fetch_me()

    async def fetch_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        before: int | None = None,
        after: int | None = None,
    ) -> list[ChatMessage]:
        """读频道历史消息，返回按 id 升序（服务端已保证，这里再排一次）。"""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after

        payload = await self._call("GET", _messages_path(channel_id), params=params)
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list):
            raw_messages = []

        messages: list[ChatMessage] = []
        skipped = 0
        for item in raw_messages:
            if not isinstance(item, Mapping):
                skipped += 1
                continue
            try:
                messages.append(ChatMessage.from_dict(item))
            except ValueError:
                skipped += 1
        if skipped:
            log_event(
                self._logger, logging.WARNING, "site.bad_message", channel_id=channel_id, count=skipped
            )
        messages.sort(key=lambda message: message.id)
        return messages

    async def post_message(
        self, channel_id: str, content: str, *, reply_to: int | None = None
    ) -> ChatMessage | None:
        """发消息；成功判据只看 `code == 200`，消息体不可用时返回 None。

        §7.2 的例外：本接口成功时的 `message` 是消息对象而非字符串，
        退化成字符串（或缺失）时返回 None，调用方必须容忍。
        """
        path = _messages_path(channel_id)
        body: dict[str, Any] = {"content": content}
        if reply_to is not None:
            body["reply_to"] = reply_to

        response, payload = await self._request_envelope("POST", path, json_body=body)
        if payload["code"] == 401:
            # 会话失效：重新登录一次并重试一次。
            await self._relogin()
            response, payload = await self._request_envelope("POST", path, json_body=body)
        if payload["code"] != 200:
            raise self._error_from_payload(response, payload)

        return self._parse_sent_message(payload, channel_id)

    async def probe_chat(self) -> None:
        """读一条大区消息，用于探测 403 禁言/权限是否恢复；异常时抛 SiteError。"""
        await self._call("GET", _messages_path(LOBBY), params={"limit": 1})

    # --- 博客评论接口 ------------------------------------------------------

    async def fetch_recent_comments(self) -> list[CommentNode]:
        """读取全站最近评论；该 spider 接口是裸数组且不带 Cookie。"""
        payload = await self._request_public_json(_SPIDER_COMMENTS_PATH)
        if not isinstance(payload, list):
            raise self._error(200, "malformed comments response")
        result: list[CommentNode] = []
        for item in payload[:100]:
            if not isinstance(item, Mapping):
                continue
            try:
                node = CommentNode.from_dict(item, max_nodes=self._max_tree_nodes)
                # recent spider 只用于发现和匹配；即便上游错误地返回正文，
                # 也不能让它进入评论机器人后续流程。
                result.append(strip_comment_content(node))
            except CommentTreeTooLarge as exc:
                raise self._error(200, "comment_tree_too_large") from exc
            except ValueError:
                continue
        return result

    async def fetch_blog_comments(self, blog_id: str) -> list[CommentNode]:
        """读取整篇文章评论树并迭代解析为顶层节点列表。"""
        normalized = normalize_uuid(blog_id)
        if normalized is None:
            raise ValueError("文章 id 不是 UUID")
        response, payload = await self._request_comment_envelope(
            "GET", f"{_COMMENTS_PREFIX}/{normalized}/comments", authenticated=False
        )
        if payload.get("code") != 200:
            raise self._error_from_payload(response, payload)
        raw = payload.get("comments")
        if not isinstance(raw, list):
            return []
        result: list[CommentNode] = []
        total_nodes = 0
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            try:
                remaining = self._max_tree_nodes - total_nodes
                if remaining < 1:
                    raise CommentTreeTooLarge("comment_tree_too_large")
                # 把全树剩余额度传给每个根，避免先完整构造超限的后续子树，
                # 再在聚合计数时才发现已经越过 10,000 节点。
                node = CommentNode.from_dict(item, max_nodes=remaining)
                total_nodes += count_comment_nodes((node,), remaining)
                result.append(node)
            except CommentTreeTooLarge as exc:
                raise self._error(200, "comment_tree_too_large") from exc
            except ValueError:
                continue
        return result

    async def fetch_blog_context(self, blog_id: str) -> BlogContext:
        """读取本轮文章资料；spider 博客接口是裸对象且不带 Cookie。"""
        normalized = normalize_uuid(blog_id)
        if normalized is None:
            raise ValueError("文章 id 不是 UUID")
        payload = await self._request_public_json(f"{_SPIDER_BLOGS_PREFIX}/{normalized}")
        if not isinstance(payload, Mapping):
            raise self._error(200, "malformed blog response")
        meta = payload.get("meta")
        title = meta.get("title") if isinstance(meta, Mapping) else ""
        return BlogContext(
            id=normalized,
            title=title if isinstance(title, str) else "",
            content=payload.get("content") if isinstance(payload.get("content"), str) else None,
        )

    async def fetch_clipboard(self, clip_id: str) -> Clipboard:
        """读取一篇云剪贴板的正文（内容引用语法 §三）。

        站点要求**登录且 Core 以上**：私有剪贴板只有作者本人（与站长）取得到，
        其他人拿到 403——这在评论区是常见情形而不是故障，由调用方降级成
        `[剪贴板 <ID> 加载失败]`。

        与 `fetch_image` 同款：401 不重新登录、不重试。引用取不回只是一次降级，
        不值得为它多一次登录；真正的会话失效由 SSE 那条路负责恢复。
        """
        normalized = _content_id(clip_id, CLIPBOARD_ID_LEN, kind="clipboard")
        payload = await self._call("GET", f"{_CLIPBOARD_PREFIX}/{normalized}")
        clip = Clipboard.from_payload(payload)
        if clip is None:
            raise self._error(200, "malformed clipboard response")
        return clip

    async def fetch_vote(self, vote_id: str) -> Vote:
        """读取一个投票的标题、选项与票数（内容引用语法 §三）。

        同样要求登录且 Core 以上。投票是**只读**的：这里只取数据，
        不会替机器人投票——站点把投票做成可交互组件，机器人没有那个身份。
        """
        normalized = _content_id(vote_id, VOTE_ID_LEN, kind="vote")
        payload = await self._call("GET", f"{_VOTES_PREFIX}/{normalized}")
        vote = Vote.from_payload(payload)
        if vote is None:
            raise self._error(200, "malformed vote response")
        return vote

    async def fetch_notifications(
        self, *, page: int, unread_only: bool = True
    ) -> NotificationPage:
        """读取登录用户的通知分页，并容错解析通知条目。"""
        await self.ensure_session()
        params = {"page": page, "unread_only": str(bool(unread_only)).lower()}
        response, payload = await self._request_comment_envelope(
            "GET", _NOTIFICATIONS_PATH, params=params, authenticated=True
        )
        if payload.get("code") == 401:
            await self._relogin()
            response, payload = await self._request_comment_envelope(
                "GET", _NOTIFICATIONS_PATH, params=params, authenticated=True
            )
        if payload.get("code") != 200:
            raise self._error_from_payload(response, payload)
        raw = payload.get("notifications")
        notifications: list[CommentNotification] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                try:
                    notifications.append(CommentNotification.from_dict(item))
                except ValueError:
                    continue
        raw_page = payload.get("page", page)
        raw_pages = payload.get("pages", 1)
        return NotificationPage(
            notifications=tuple(notifications),
            page=int(raw_page) if isinstance(raw_page, int) and not isinstance(raw_page, bool) else page,
            pages=int(raw_pages) if isinstance(raw_pages, int) and not isinstance(raw_pages, bool) else 1,
            has_next=bool(payload.get("hasNext", False)),
            unread_count=(
                int(payload.get("unreadCount"))
                if isinstance(payload.get("unreadCount"), int)
                and not isinstance(payload.get("unreadCount"), bool)
                else 0
            ),
        )

    async def mark_notification_read(self, notification_id: str) -> None:
        """标记单条通知已读；错误按站内信封传播。"""
        if not isinstance(notification_id, str) or not notification_id:
            raise ValueError("通知 id 缺失")
        await self.ensure_session()
        path = f"{_NOTIFICATIONS_PATH}/{notification_id}/read"
        response, payload = await self._request_comment_envelope(
            "POST", path, authenticated=True
        )
        if payload.get("code") == 401:
            await self._relogin()
            response, payload = await self._request_comment_envelope(
                "POST", path, authenticated=True
            )
        if payload.get("code") != 200:
            raise self._error_from_payload(response, payload)

    async def post_comment(
        self, blog_id: str, content: str, *, parent_id: str
    ) -> CommentNode | None:
        """发布评论回复；成功判据为信封 code=200，评论对象位于 comment。"""
        normalized_blog = normalize_uuid(blog_id)
        normalized_parent = normalize_uuid(parent_id)
        if normalized_blog is None or normalized_parent is None:
            raise ValueError("文章 id 或父评论 id 不是 UUID")
        await self.ensure_session()
        body = {"content": content, "parent_id": normalized_parent}
        path = f"{_COMMENTS_PREFIX}/{normalized_blog}/comments"
        response, payload = await self._request_comment_envelope(
            "POST", path, json_body=body, authenticated=True
        )
        if payload.get("code") == 401:
            await self._relogin()
            response, payload = await self._request_comment_envelope(
                "POST", path, json_body=body, authenticated=True
            )
        if payload.get("code") != 200:
            raise self._error_from_payload(response, payload)
        raw = payload.get("comment")
        if not isinstance(raw, Mapping):
            return None
        try:
            return CommentNode.from_dict(raw, max_nodes=self._max_tree_nodes)
        except CommentTreeTooLarge:
            raise self._error(200, "comment_tree_too_large")
        except ValueError:
            return None

    async def _request_public_json(self, path: str) -> object:
        """读取公开 spider 响应，严格施加原始字节上限。"""
        try:
            async with self._require_client().stream(
                "GET", path, headers={"Accept": "application/json"}
            ) as response:
                status = response.status_code
                raw = await self._bounded_response_bytes(response)
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from exc
        if status != 200:
            raise self._error(status, f"http={status}")
        try:
            import json

            return json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._error(status, "malformed response") from exc

    async def _request_comment_envelope(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: Any = None,
        authenticated: bool = False,
    ) -> tuple[httpx.Response, Mapping[str, Any]]:
        """评论站内接口的有界信封请求。"""
        headers = {"Accept": "application/json"}
        if authenticated:
            headers.update(self._cookie_headers())
        try:
            async with self._require_client().stream(
                method, path, headers=headers, params=params, json=json_body
            ) as response:
                raw = await self._bounded_response_bytes(response)
                status = response.status_code
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from exc
        try:
            import json

            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._error(status, "malformed envelope") from exc
        if not isinstance(payload, Mapping):
            raise self._error(status, "malformed envelope")
        code = payload.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise self._error(status, "malformed envelope")
        return response, payload

    async def _bounded_response_bytes(self, response: httpx.Response) -> bytes:
        """流式累计响应字节，超过上限立即抛出稳定 SiteError。"""
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise self._error(response.status_code, "response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    async def fetch_image(self, url: str, *, max_bytes: int) -> bytes:
        """取回一条消息附带的图片原始字节（INTERFACES §7）。

        只允许与 `base_url` **完全同源**（scheme + host + 有效端口）的地址。站点给的是
        相对路径 `/api/images/<id>/raw`，用 `urljoin` 解析；一旦允许「照站点给的 url
        带着 Cookie 去 GET」，任何能让站点返回任意 url 的路径都会变成凭据外泄通道。
        跨源直接拒绝，**不发出任何请求**。

        字节上限在**流式累计过程中**执行：超限立即放弃，不读完整个响应。
        本方法**不写任何日志**（URL 不得进日志），失败原因由调用方以稳定字段记录。
        401 不重新登录、不重试：取图失败只是一次降级，不值得为它多一次登录。
        """
        base = urllib.parse.urlsplit(self._base_url)
        target = urllib.parse.urljoin(self._base_url + "/", url)
        parsed = urllib.parse.urlsplit(target)
        if not self._same_origin(base, parsed):
            raise ImageFetchError("host_not_allowed")

        headers: dict[str, str] = {"Accept": "image/*"}
        headers.update(self._cookie_headers())
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._require_client().stream(
                "GET", target, headers=headers
            ) as response:
                if response.status_code != 200:
                    raise ImageFetchError("http", int(response.status_code))
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageFetchError("too_large")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ImageFetchError("network") from exc

        data = b"".join(chunks)
        if not data:
            raise ImageFetchError("http", 200)
        return data

    @staticmethod
    def _same_origin(
        base: urllib.parse.SplitResult, target: urllib.parse.SplitResult
    ) -> bool:
        """两者是否同源。非 http/https 或端口非法一律判为不同源。"""
        if target.scheme not in ("http", "https"):
            return False
        if target.scheme != base.scheme or target.hostname != base.hostname:
            return False
        return _effective_port(target) == _effective_port(base)

    @asynccontextmanager
    async def open_stream(self, last_event_id: int | None = None) -> AsyncIterator[httpx.Response]:
        """打开 SSE 长连接；401 时重新登录并重试一次，其他非 200 抛 SiteError。

        成功时把响应交给调用方逐行消费，退出上下文时关闭连接。
        """
        retried = False
        while True:
            stream = self._require_client().stream(
                "GET",
                _STREAM_PATH,
                headers=self._stream_headers(last_event_id),
                timeout=self._stream_timeout(),
            )
            try:
                response = await stream.__aenter__()
            except httpx.HTTPError as exc:
                raise self._network_error(exc) from exc
            try:
                if response.status_code == 200:
                    yield response
                    return
                error = await self._stream_error(response)
            finally:
                await stream.__aexit__(None, None, None)

            if error.status == 401 and not retried:
                retried = True
                await self._relogin()
                continue
            raise error

    # --- 内部实现 ---

    async def _authenticate(self) -> Author:
        """真正发出登录请求并保存 Cookie；调用方需持有登录锁。"""
        response, payload = await self._request_envelope(
            "POST",
            _LOGIN_PATH,
            json_body={"username": self._username, "password": self._password},
        )
        if payload["code"] != 200:
            raise self._error_from_payload(response, payload)
        self._store_session_cookie(response)
        self._user = _parse_author(payload.get("user"))
        log_event(self._logger, logging.INFO, "site.login", status=payload["code"])
        return self._user

    async def _relogin(self) -> Author:
        """会话失效后的强制重登：先丢弃本地会话，避免单飞复检直接返回旧用户。"""
        log_event(self._logger, logging.INFO, "site.session_expired", status=401)
        self._user = None
        self._session_cookie = None
        return await self.login()

    async def _fetch_me(self) -> Author:
        """探活一次；非 200 抛 SiteError（401 由调用方处理）。"""
        payload = await self._call("GET", _ME_PATH)
        raw_user = payload.get("user")
        if isinstance(raw_user, Mapping):
            self._user = _parse_author(raw_user)
        if self._user is None:
            raise self._error(payload["code"], "user missing in envelope")
        return self._user

    def _store_session_cookie(self, response: httpx.Response) -> None:
        """保存 Set-Cookie 里的会话值并登记脱敏；不依赖 httpx 的 cookie jar。"""
        cookie = response.cookies.get(SESSION_COOKIE_NAME)
        if not cookie:
            return
        self._session_cookie = cookie
        self._redactor.add_secret(cookie)
        register_secret(cookie)
        if self._client is not None:
            # Cookie 已由显式请求头携带，清空 jar 以免两处来源互相覆盖。
            self._client.cookies.clear()

    def _parse_sent_message(self, payload: Mapping[str, Any], channel_id: str) -> ChatMessage | None:
        """取发消息接口的返回消息体；不是对象或解析失败都降级为 None。"""
        raw_message = payload.get("message")
        if not isinstance(raw_message, Mapping):
            log_event(self._logger, logging.WARNING, "site.post_no_body", channel_id=channel_id)
            return None
        try:
            return ChatMessage.from_dict(raw_message)
        except ValueError:
            log_event(self._logger, logging.WARNING, "site.post_bad_body", channel_id=channel_id)
            return None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("SiteClient 尚未 start()")
        return self._client

    def _error(self, status: int, message: str, retry_after: float | None = None) -> SiteError:
        """构造 SiteError；错误文案出站前统一脱敏。"""
        return SiteError(status, self._redactor.redact(message), retry_after)

    def _network_error(self, exc: httpx.HTTPError) -> SiteError:
        """传输层异常（含超时）统一映射为 SiteError(0, ...)。"""
        log_event(self._logger, logging.WARNING, "site.network_error", error=type(exc).__name__)
        return self._error(0, f"{type(exc).__name__}: {exc}")

    def _decode(self, response: httpx.Response) -> Mapping[str, Any]:
        """解析统一信封；JSON 非法、非对象或 code 非 int 一律 malformed。"""
        status = int(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise self._error(status, "malformed envelope") from exc
        if not isinstance(payload, Mapping):
            raise self._error(status, "malformed envelope")
        code = payload.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise self._error(status, "malformed envelope")
        return payload

    def _error_from_payload(self, response: httpx.Response, payload: Mapping[str, Any]) -> SiteError:
        """按信封 code 构造错误；429 时附带 Retry-After 的秒数。"""
        code = payload["code"]
        message = payload.get("message")
        if not isinstance(message, str) or not message:
            message = f"code={code} http={int(response.status_code)}"
        retry_after = _parse_retry_after(response) if code == 429 else None
        return self._error(code, message, retry_after)

    async def _request_envelope(
        self, method: str, path: str, *, params: Any = None, json_body: Any = None
    ) -> tuple[httpx.Response, Mapping[str, Any]]:
        """发一次带 Cookie 的请求并解析信封；网络错误抛 SiteError(0, ...)。"""
        headers: dict[str, str] = {"Accept": "application/json"}
        headers.update(self._cookie_headers())
        try:
            response = await self._require_client().request(
                method, path, headers=headers, params=params, json=json_body
            )
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from exc
        return response, self._decode(response)

    async def _call(
        self, method: str, path: str, *, params: Any = None, json_body: Any = None
    ) -> Mapping[str, Any]:
        """请求并在 `code != 200` 时抛 SiteError，返回信封载荷。"""
        response, payload = await self._request_envelope(
            method, path, params=params, json_body=json_body
        )
        if payload["code"] != 200:
            raise self._error_from_payload(response, payload)
        return payload

    async def _stream_error(self, response: httpx.Response) -> SiteError:
        """非 200 的 SSE 响应：尽量按信封取 message，取不到就退回 HTTP 状态码。"""
        await response.aread()
        try:
            payload = self._decode(response)
        except SiteError as exc:
            return exc
        return self._error_from_payload(response, payload)

    def _cookie_headers(self) -> dict[str, str]:
        """显式 Cookie 头；未登录时为空。"""
        if not self._session_cookie:
            return {}
        return {"Cookie": f"{SESSION_COOKIE_NAME}={self._session_cookie}"}

    def _stream_timeout(self) -> httpx.Timeout:
        """SSE 专用超时：只有读侧放宽，连接/写入/池仍沿用普通 timeout。"""
        return httpx.Timeout(
            connect=self._timeout,
            read=STREAM_READ_TIMEOUT_SECONDS,
            write=self._timeout,
            pool=self._timeout,
        )

    def _stream_headers(self, last_event_id: int | None) -> dict[str, str]:
        """SSE 请求头：Accept 必须声明事件流，断线补齐靠 Last-Event-ID。"""
        headers: dict[str, str] = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        headers.update(self._cookie_headers())
        return headers
