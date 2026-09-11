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
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx

from ..logging_setup import get_logger, log_event, register_secret
from ..redact import Redactor
from .models import LOBBY, Author, ChatMessage, _parse_author

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


def _messages_path(channel_id: str) -> str:
    """频道消息接口路径。"""
    return f"{_CHANNELS_PREFIX}/{channel_id}/messages"


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


class SiteError(Exception):
    """站点接口错误；`status` 为站点 code，0 表示网络层错误（结果不确定）。"""

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


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
    ) -> None:
        # username/password 是对 INTERFACES §7 构造签名的**追加**关键字参数：
        # 锁定签名里没有携带凭据的位置，而 login() 需要用它发登录请求。
        self._base_url = base_url.rstrip("/")
        self._redactor = redactor
        self._timeout = timeout
        self._transport = transport
        self._username = username
        self._password = password
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
