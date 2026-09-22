"""本机管理 API（LIGHT_EDITION_DESIGN §8、§11）。

安全边界（§8.1、§8.2）：

- 只监听回环；每个请求校验**精确 Host**（含实际端口），写请求另校验 Origin /
  Fetch Metadata，并必须带会话绑定的 CSRF 值与 JSON 内容类型；
- 静态页与会话兑换之外的所有路径都要已认证会话；
- 请求体有上限，字段白名单由配置服务把关，错误只返回字段路径与稳定码；
- 响应一律 `Cache-Control: no-store`，页面带 CSP，不返回配置取值以外的任何
  凭据材料，也不返回可用于读取凭据的内部定位信息。

本模块只做协议转换：校验、事务、进程与事件都在各自的服务里。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from raricy_bot.config import ConfigError, Secrets, parse_config
from raricy_bot.core.worker import ModelError, OpenAIModelClient
from raricy_bot.redact import Redactor
from raricy_bot.site.client import SiteClient, SiteError

from . import paths, texts
from .config_service import (
    ACTION_DELETE,
    light_base_mapping,
    ACTION_KEEP,
    ACTION_REPLACE,
    EDITABLE_FIELDS,
    STATE_CONFIGURED,
    ConfigConflict,
    ConfigInvalid,
    ConfigServiceError,
    CredentialUpdate,
)
from .credential_store import CredentialStoreError
from .session import CSRF_HEADER, SESSION_COOKIE, Session, SessionManager

# 请求体上限：配置表单很小；知识库导入文件另有自己的上限（§13.2）。
MAX_JSON_BYTES: int = 256 * 1024
MAX_IMPORT_BYTES: int = 1024 * 1024
MAX_IMPORT_NAME_CHARS: int = 120

# 模型测试：固定样例、短超时、小输出，不读取任何用户数据（§8.2）。
MODEL_TEST_PROMPT: str = "请回复：ok"
MODEL_TEST_MAX_TOKENS: int = 16
MODEL_TEST_TIMEOUT_SECONDS: float = 15.0

# SSE 心跳：没有新事件时也要让浏览器知道连接还活着（§12）。
SSE_HEARTBEAT_SECONDS: float = 15.0
# SSE 轮询间隔：订阅队列的轮询周期，取消要立刻生效。
SSE_POLL_SECONDS: float = 0.5

CSP_POLICY: str = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class ApiError(Exception):
    """把服务层异常映射成 HTTP 状态与稳定码（§11）。"""

    def __init__(self, status: int, code: str, *, field: str | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field

    def payload(self) -> dict:
        body: dict[str, Any] = {"ok": False, "code": self.code}
        if self.field:
            body["field"] = self.field
        return body


class _HostGuardMiddleware:
    """每个请求校验精确 Host，并给 API 响应补 `no-store`、给页面补 CSP（§8.1/§8.2）。"""

    def __init__(self, app, *, api: "LocalApi") -> None:
        self._app = app
        self._api = api

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        if not self._api._host_allowed(request):
            await self._api._json(403, {"ok": False, "code": "bad_host"})(scope, receive, send)
            return
        is_api = str(scope.get("path", "")).startswith("/api/")

        async def _send(message):
            if message["type"] == "http.response.start":
                if is_api:
                    _set_header(message, b"cache-control", "no-store")
                for key, value in message.get("headers", []):
                    if key.lower() == b"content-type" and value.startswith(b"text/html"):
                        _set_header(message, b"content-security-policy", CSP_POLICY)
                        _set_header(message, b"x-content-type-options", "nosniff")
                        break
            await send(message)

        await self._app(scope, receive, _send)


class LocalApi:
    """本机管理 API 的装配与实现。"""

    def __init__(
        self,
        *,
        instance_id: str,
        data_root: Path,
        config_service,
        manager,
        status_service,
        events,
        sessions: SessionManager,
        credential_store,
        static_dir: Path,
        port: int = 0,
        site_transport=None,
        model_transport=None,
        on_quit: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._instance_id = instance_id
        self._data_root = Path(data_root)
        self._config = config_service
        self._manager = manager
        self._status = status_service
        self._events = events
        self._sessions = sessions
        self._credentials = credential_store
        self._static_dir = Path(static_dir)
        self._port = port
        self._site_transport = site_transport
        self._model_transport = model_transport
        self._on_quit = on_quit
        self._clock = clock
        self._model_test_lock = threading.Lock()
        self.app = self._build()

    def set_port(self, port: int) -> None:
        """监听成功后由 Controller 告知实际端口（Host 校验要用它，§9.4）。"""
        self._port = port

    # --- 装配 -------------------------------------------------------------

    def _build(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        # 纯 ASGI 中间件而不是 BaseHTTPMiddleware：后者会把响应体缓冲一遍，
        # 无限的事件流因此永远发不出第一段（实测挂住）。
        app.add_middleware(_HostGuardMiddleware, api=self)

        app.add_api_route("/api/session/exchange", self._exchange, methods=["POST"])
        app.add_api_route("/api/config", self._get_config, methods=["GET"])
        app.add_api_route("/api/config", self._put_config, methods=["PUT"])
        app.add_api_route("/api/config/validate", self._validate_config, methods=["POST"])
        app.add_api_route("/api/config/draft", self._get_draft, methods=["GET"])
        app.add_api_route("/api/config/draft", self._put_draft, methods=["PUT"])
        app.add_api_route("/api/status", self._get_status, methods=["GET"])
        app.add_api_route("/api/bot/start", self._bot_start, methods=["POST"])
        app.add_api_route("/api/bot/stop", self._bot_stop, methods=["POST"])
        app.add_api_route("/api/bot/restart", self._bot_restart, methods=["POST"])
        app.add_api_route("/api/operations/{operation_id}", self._get_operation, methods=["GET"])
        app.add_api_route("/api/test/site", self._test_site, methods=["POST"])
        app.add_api_route("/api/test/model", self._test_model, methods=["POST"])
        app.add_api_route("/api/logs/stream", self._logs_stream, methods=["GET"])
        app.add_api_route("/api/kb/status", self._kb_status, methods=["GET"])
        app.add_api_route("/api/kb/import", self._kb_import, methods=["POST"])
        app.add_api_route("/api/launcher/quit", self._quit, methods=["POST"])
        app.add_api_route("/", self._index, methods=["GET"])
        app.add_api_route("/{asset:path}", self._asset, methods=["GET"])
        return app

    # --- 基础工具 ---------------------------------------------------------

    @staticmethod
    def _json(status: int, body: dict, *, headers: dict[str, str] | None = None) -> JSONResponse:
        return JSONResponse(status_code=status, content=body, headers=headers or {})

    def _host_allowed(self, request: Request) -> bool:
        """Host 必须是本机地址加实际端口（§8.1）。"""
        host = (request.headers.get("host") or "").strip().lower()
        if not host or self._port == 0:
            return False
        return host in {f"127.0.0.1:{self._port}", f"localhost:{self._port}"}

    def _origin_allowed(self, request: Request) -> bool:
        """写请求的来源校验：Origin / Sec-Fetch-Site 存在时必须同源（§8.1）。"""
        origin = request.headers.get("origin")
        if origin:
            allowed = {
                f"http://127.0.0.1:{self._port}",
                f"http://localhost:{self._port}",
            }
            if origin.rstrip("/").lower() not in allowed:
                return False
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            return False
        return True

    def _session(self, request: Request) -> Session | None:
        return self._sessions.get(request.cookies.get(SESSION_COOKIE))

    def _require_session(self, request: Request) -> Session:
        session = self._session(request)
        if session is None:
            raise ApiError(401, "unauthenticated")
        return session

    def _require_write(self, request: Request, session: Session) -> None:
        """写请求的门：来源、CSRF 与内容类型三件都要成立（§8.1）。"""
        if not self._origin_allowed(request):
            raise ApiError(403, "bad_origin")
        if not self._sessions.check_csrf(session, request.headers.get(CSRF_HEADER)):
            raise ApiError(403, "bad_csrf")
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            raise ApiError(415, "unsupported_media_type")

    async def _json_body(self, request: Request) -> dict:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError as exc:
                raise ApiError(400, "bad_request") from exc
            if declared_bytes > MAX_JSON_BYTES:
                raise ApiError(413, "request_too_large")
        # 边读边判上限：ASGI 服务器不限制请求体，先整体缓冲再检查等于没有上限（审查 I6）。
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > MAX_JSON_BYTES:
                raise ApiError(413, "request_too_large")
        raw = bytes(chunks)
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "bad_request") from exc
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request")
        return body

    def _handle(self, exc: Exception) -> ApiError:
        """把服务层错误映射成状态码与稳定码（§11）。"""
        if isinstance(exc, ApiError):
            return exc
        if isinstance(exc, ConfigConflict):
            return ApiError(409, "revision_conflict")
        if isinstance(exc, ConfigInvalid):
            return ApiError(422, exc.code, field=exc.field)
        if isinstance(exc, ConfigServiceError):
            return ApiError(409, str(exc))
        if isinstance(exc, CredentialStoreError):
            return ApiError(503, str(exc))
        if isinstance(exc, ConfigError):
            return ApiError(422, exc.kind, field=exc.field)
        return ApiError(500, "internal_error")

    def _credential_updates(self, body: dict) -> dict[str, CredentialUpdate]:
        updates: dict[str, CredentialUpdate] = {}
        raw = body.get("credentials") or {}
        if not isinstance(raw, dict):
            raise ApiError(400, "bad_request")
        for name in ("password", "llm_api_key"):
            entry = raw.get(name)
            if entry is None:
                continue
            if not isinstance(entry, dict):
                raise ApiError(400, "bad_request")
            action = entry.get("action", ACTION_KEEP)
            if action == ACTION_KEEP:
                updates[name] = CredentialUpdate.keep()
            elif action == ACTION_REPLACE:
                value = entry.get("value")
                if not isinstance(value, str) or not value.strip():
                    raise ApiError(422, "credential_value_required", field=name)
                updates[name] = CredentialUpdate.replace(value)
            elif action == ACTION_DELETE:
                updates[name] = CredentialUpdate.delete()
            else:
                raise ApiError(422, "invalid_credential_action", field=name)
        return updates

    # --- 会话 -------------------------------------------------------------

    async def _exchange(self, request: Request):
        """兑换一次性引导令牌（§8.1）：成功后写会话 Cookie 并返回 CSRF 值。"""
        if not self._origin_allowed(request):
            return self._json(403, {"ok": False, "code": "bad_origin"})
        if not (request.headers.get("content-type") or "").startswith("application/json"):
            return self._json(415, {"ok": False, "code": "unsupported_media_type"})
        try:
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        token = body.get("token")
        if not isinstance(token, str):
            return self._json(400, {"ok": False, "code": "bad_request"})
        session = self._sessions.exchange(token)
        if session is None:
            # 失败原因不细分：不给「猜对了但过期」之类的反馈（§8.1）。
            return self._json(401, {"ok": False, "code": "exchange_failed"})
        response = self._json(200, {"ok": True, "csrf": session.csrf_token})
        # 回环 HTTP 不假设 Secure 可用；SameSite=Strict 与 HttpOnly 都要有（§8.1）。
        response.set_cookie(
            SESSION_COOKIE,
            session.session_id,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    # --- 配置 -------------------------------------------------------------

    async def _get_config(self, request: Request):
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        try:
            body = await asyncio.to_thread(self._config_view)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, body)

    def _config_view(self) -> dict:
        saved = self._config.load_saved()
        values: dict[str, Any] = {}
        if saved is not None:
            for key in sorted(EDITABLE_FIELDS):
                values[key] = _get_path(saved.mapping, key)
        status = self._config.status()
        return {
            "ok": True,
            "revision": saved.revision if saved is not None else None,
            "state": status.state,
            "account": saved.account if saved is not None else None,
            "values": values,
            # 向导的初始值来自 Launcher 基线（含默认 System Prompt 模板），
            # 只含非敏感字段，且与提交时的基线同源（§5.1 第 3 点）。
            "defaults": {
                key: _get_path(light_base_mapping(self._config.profile()), key)
                for key in sorted(EDITABLE_FIELDS)
                if key == "system_prompt"
            },
            "editable": sorted(EDITABLE_FIELDS),
            "credentials": self._credential_view(saved),
            "start_bot_on_launch": bool(saved.start_bot_on_launch) if saved else False,
        }

    def _credential_view(self, saved) -> dict:
        """只报 configured / backend / available，绝不回显取值（§7）。"""
        backend = self._credentials.describe()
        configured = False
        if saved is not None and saved.credentials_ref:
            try:
                values = self._credentials.get(saved.credentials_ref)
            except CredentialStoreError:
                values = None
            configured = values is not None
        return {
            "password": {"configured": configured},
            "llm_api_key": {"configured": configured},
            "backend": {"name": backend.name, "available": backend.available},
        }

    async def _put_config(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            revision = await asyncio.to_thread(self._commit_config, body)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, {"ok": True, "revision": revision})

    def _commit_config(self, body: dict) -> int:
        expected = body.get("expected_revision")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ApiError(422, "invalid_revision", field="expected_revision")
        values = body.get("values") or {}
        if not isinstance(values, dict):
            raise ApiError(400, "bad_request")
        updates = self._credential_updates(body)
        account = body.get("account")
        if account is not None and not isinstance(account, str):
            raise ApiError(400, "bad_request")
        start_bot = body.get("start_bot_on_launch")
        if start_bot is not None and not isinstance(start_bot, bool):
            raise ApiError(400, "bad_request")
        return self._config.commit(
            values,
            expected_revision=expected,
            password=updates.get("password", CredentialUpdate.keep()),
            llm_api_key=updates.get("llm_api_key", CredentialUpdate.keep()),
            account=account,
            start_bot_on_launch=start_bot,
        )

    async def _validate_config(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            await asyncio.to_thread(self._validate_values, body)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, {"ok": True})

    def _validate_values(self, body: dict) -> None:
        """静态校验：不写文件、不调外部网络（§11）。"""
        values = body.get("values") or {}
        if not isinstance(values, dict):
            raise ApiError(400, "bad_request")
        self._config.validate_values(values)

    async def _get_draft(self, request: Request):
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        try:
            draft = await asyncio.to_thread(self._config.load_draft)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        if draft is None:
            return self._json(200, {"ok": True, "revision": 0, "values": {}})
        return self._json(
            200,
            {
                "ok": True,
                "revision": draft.revision,
                "values": {
                    key: _get_path(draft.mapping, key) for key in sorted(EDITABLE_FIELDS)
                },
            },
        )

    async def _put_draft(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        expected = body.get("expected_revision")
        values = body.get("values") or {}
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            return self._json(422, {"ok": False, "code": "invalid_revision", "field": "expected_revision"})
        if not isinstance(values, dict):
            return self._json(400, {"ok": False, "code": "bad_request"})
        try:
            revision = await asyncio.to_thread(
                self._config.save_draft, values, expected_revision=expected
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, {"ok": True, "revision": revision})

    # --- 状态与进程 -------------------------------------------------------

    async def _get_status(self, request: Request):
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        try:
            # 先把 Worker 已上报的帧折进管理器（最近快照的来源），再聚合状态。
            await asyncio.to_thread(self._manager.drain)
            snapshot = await asyncio.to_thread(self._status.snapshot)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, {"ok": True, "status": snapshot})

    async def _bot_start(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        revision = self._target_revision(body)
        if revision is None:
            return self._json(409, {"ok": False, "code": "config_not_ready"})
        operation = self._manager.start(revision=revision)
        return self._json(202, {"ok": True, "operation_id": operation.operation_id})

    async def _bot_stop(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        operation = self._manager.stop()
        return self._json(202, {"ok": True, "operation_id": operation.operation_id})

    async def _bot_restart(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        revision = self._target_revision(body)
        if revision is None:
            return self._json(409, {"ok": False, "code": "config_not_ready"})
        operation = self._manager.restart(revision=revision)
        return self._json(202, {"ok": True, "operation_id": operation.operation_id})

    def _target_revision(self, body: dict) -> int | None:
        """启动/重启的目标版本：请求指定优先，否则用当前已保存版本（§6.5）。

        指定的版本必须是当前已保存的那一版：别的版本没有可用的运行快照，
        应当立刻回 409，而不是先答应再异步失败（审查 M8）。
        """
        saved = self._config.load_saved()
        if saved is None:
            return None
        requested = body.get("revision")
        if isinstance(requested, int) and not isinstance(requested, bool):
            return requested if requested == saved.revision else None
        return saved.revision

    async def _get_operation(self, request: Request, operation_id: str):
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        operation = self._manager.operation(operation_id)
        if operation is None:
            return self._json(404, {"ok": False, "code": "not_found"})
        return self._json(
            200,
            {
                "ok": True,
                "operation": {
                    "id": operation.operation_id,
                    "kind": operation.kind,
                    "state": operation.state,
                    "result": operation.result,
                    "revision": operation.target_revision,
                    "finished": operation.finished_at is not None,
                },
            },
        )

    # --- 测试 -------------------------------------------------------------

    async def _test_site(self, request: Request):
        """站点测试：只在 Bot 停止时执行，避免与运行会话互相影响（§8.2）。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        if self._manager.state not in ("stopped", "failed"):
            return self._json(409, {"ok": False, "code": "bot_running"})
        result, status = await asyncio.to_thread(self._run_site_test)
        return self._json(status, result)

    def _run_site_test(self) -> tuple[dict, int]:
        try:
            status = self._config.status()
            saved = self._config.load_saved()
            if saved is None or status.state != STATE_CONFIGURED:
                # 配置无效时连测试都不做：地址规则与凭据都没通过校验（审查 I7）。
                return {"ok": False, "code": "config_not_ready"}, 409
            credentials = self._config.credentials_for(saved.revision)
            config = parse_config(
                saved.mapping,
                config_dir=str(self._config.profile()),
                secrets=credentials,
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return mapped.payload(), mapped.status
        started = self._clock()
        outcome, detail = asyncio.run(self._probe_site(config, credentials))
        elapsed = int((self._clock() - started) * 1000)
        self._status.record_test("site", ok=outcome, detail=detail, revision=saved.revision)
        return {"ok": outcome, "detail": detail, "elapsed_ms": elapsed}, 200

    async def _probe_site(self, config, credentials: Secrets) -> tuple[bool, str]:
        """登录并探测聊天权限；任何失败只回固定类别码（§8.2）。

        地址取自**公共校验后的** `config.site.base_url`：站点密码只会发往通过了
        HTTPS/userinfo/query 规则（D-113）的地址，而不是 YAML 里的原始字符串。
        """
        client = SiteClient(
            config.site.base_url,
            Redactor(),
            timeout=min(config.site.request_timeout_seconds, MODEL_TEST_TIMEOUT_SECONDS),
            username=credentials.username,
            password=credentials.password,
            transport=self._site_transport,
        )
        try:
            await client.start()
            await client.login()
            await client.probe_chat()
            return True, "chat_ready"
        except SiteError as exc:
            # reason 是稳定类别；登录成功但探测失败要与登录失败分开报。
            return False, exc.reason
        except Exception as exc:
            return False, type(exc).__name__
        finally:
            await client.aclose()

    async def _test_model(self, request: Request):
        """模型测试：单次在途、固定样例、小输出，不返回生成内容（§8.2）。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        if not self._model_test_lock.acquire(blocking=False):
            return self._json(409, {"ok": False, "code": "test_in_progress"})
        try:
            result, status = await asyncio.to_thread(self._run_model_test)
        finally:
            self._model_test_lock.release()
        return self._json(status, result)

    def _run_model_test(self) -> tuple[dict, int]:
        try:
            saved = self._config.load_saved()
            if saved is None:
                return {"ok": False, "code": "config_not_ready"}, 409
            credentials = self._config.credentials_for(saved.revision)
        except Exception as exc:
            mapped = self._handle(exc)
            return mapped.payload(), mapped.status
        started = self._clock()
        outcome, detail = asyncio.run(self._probe_model(saved, credentials))
        elapsed = int((self._clock() - started) * 1000)
        self._status.record_test("model", ok=outcome, detail=detail, revision=saved.revision)
        return {"ok": outcome, "detail": detail, "elapsed_ms": elapsed}, 200

    async def _probe_model(self, saved, credentials: Secrets) -> tuple[bool, str]:
        """固定样例、短超时、小输出；只回分类，不回生成内容（§8.2）。"""
        try:
            config = parse_config(
                saved.mapping,
                config_dir=str(self._config.profile()),
                secrets=credentials,
            )
        except ConfigError:
            return False, "config_invalid"
        # 上限与超时按「测试一次」收口：不沿用部署里的大输出与长超时。
        test_config = dataclasses.replace(
            config.model,
            max_output_tokens=MODEL_TEST_MAX_TOKENS,
            timeout_seconds=MODEL_TEST_TIMEOUT_SECONDS,
        )
        client = OpenAIModelClient(
            test_config,
            credentials.llm_api_key,
            redactor=Redactor(),
            transport=self._model_transport,
        )
        try:
            messages = [{"role": "user", "content": MODEL_TEST_PROMPT}]
            await asyncio.wait_for(
                client.complete(messages), timeout=MODEL_TEST_TIMEOUT_SECONDS
            )
            return True, "ok"
        except ModelError as exc:
            return False, exc.kind
        except asyncio.TimeoutError:
            return False, "timeout"
        except Exception as exc:
            return False, type(exc).__name__
        finally:
            await client.aclose()

    # --- 事件流 -----------------------------------------------------------

    async def _logs_stream(self, request: Request):
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            return self._json(403, {"ok": False, "code": "bad_origin"})
        if not self._origin_allowed(request):
            return self._json(403, {"ok": False, "code": "bad_origin"})
        after = request.headers.get("last-event-id")
        # 先订阅再回放：两步之间到达的事件不会丢；回放里已有的帧在下面的
        # 循环里按序号跳过，因此也不会重复投递（审查 M4）。
        subscriber = self._events.subscribe()
        if subscriber is None:
            return self._json(503, {"ok": False, "code": "too_many_subscribers"})
        replayed, gap = self._events.replay(after)
        replayed_seq = replayed[-1].seq if replayed else 0

        async def _stream():
            try:
                if gap != "none":
                    yield _sse("gap", {"reason": gap})
                for event in replayed:
                    yield _sse_frame(event)
                idle = 0.0
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = subscriber.get_nowait()
                    except queue.Empty:
                        # 轮询而不是把阻塞读取丢进线程池：取消要立刻生效，
                        # 否则关闭页面会留下等心跳的线程（§12 的慢消费者清理）。
                        await asyncio.sleep(SSE_POLL_SECONDS)
                        idle += SSE_POLL_SECONDS
                        if idle >= SSE_HEARTBEAT_SECONDS:
                            idle = 0.0
                            yield _sse("heartbeat", {})
                        continue
                    idle = 0.0
                    if event is None:
                        return
                    if event.seq <= replayed_seq:
                        continue  # 回放里已经发过
                    yield _sse_frame(event)
            finally:
                self._events.unsubscribe(subscriber)

        return StreamingResponse(_stream(), media_type="text/event-stream")

    # --- 知识库 -----------------------------------------------------------

    async def _kb_status(self, request: Request):
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        try:
            profile = self._config.profile()
        except ConfigServiceError:
            return self._json(409, {"ok": False, "code": "no_active_profile"})
        directory = paths.knowledge_dir(profile)
        files = 0
        if directory.is_dir():
            files = sum(1 for path in directory.rglob("*.md") if path.is_file())
        worker = self._manager.last_status or {}
        kb = worker.get("knowledge_base") if isinstance(worker, dict) else None
        return self._json(200, {"ok": True, "files": files, "worker": kb})

    async def _kb_import(self, request: Request):
        """导入一份 Markdown 到受管目录：先写临时文件再替换（§13.2）。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        name = body.get("name")
        content = body.get("content")
        if not isinstance(name, str) or not isinstance(content, str):
            return self._json(400, {"ok": False, "code": "bad_request"})
        try:
            target = await asyncio.to_thread(self._write_kb_file, name, content)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        except OSError:
            return self._json(500, {"ok": False, "code": "write_failed"})
        return self._json(200, {"ok": True, "name": target.name})

    def _write_kb_file(self, name: str, content: str) -> Path:
        if len(name) > MAX_IMPORT_NAME_CHARS or not name.lower().endswith(".md"):
            raise ApiError(422, "invalid_file_name", field="name")
        # 只取纯文件名：不解释路径、不允许目录穿越（§13.2）。
        safe = Path(name).name
        if safe != name or safe in {"", ".", ".."}:
            raise ApiError(422, "invalid_file_name", field="name")
        payload = content.encode("utf-8")
        if len(payload) > MAX_IMPORT_BYTES:
            raise ApiError(413, "file_too_large")
        profile = self._config.profile()
        directory = paths.knowledge_dir(profile)
        if not paths.is_within(profile, directory):
            raise ApiError(500, "path_outside_profile")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe
        temp = directory / f".{safe}.{uuid4().hex[:8]}.tmp"
        temp.write_bytes(payload)
        temp.replace(target)
        return target

    # --- 退出 -------------------------------------------------------------

    async def _quit(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        show = self._on_quit
        if show is not None:
            threading.Timer(0.2, show).start()
        return self._json(200, {"ok": True, "message": texts.QUIT_ACKNOWLEDGED})

    # --- 静态资源 ---------------------------------------------------------

    async def _index(self, request: Request):
        return self._static_file("index.html")

    async def _asset(self, request: Request, asset: str):
        return self._static_file(asset)

    def _static_file(self, name: str):
        if not name or name.endswith("/"):
            name = f"{name}index.html"
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in name:
            return self._json(404, {"ok": False, "code": "not_found"})
        target = self._static_dir / candidate
        if not target.is_file():
            return self._json(404, {"ok": False, "code": "not_found"})
        media_type = _media_type(target)
        response = StreamingResponse(iter([target.read_bytes()]), media_type=media_type)
        response.headers["Cache-Control"] = "no-store"
        return response


def _set_header(message: dict, name: bytes, value: str) -> None:
    """在 ASGI 响应的原始头列表上替换/追加一个头（不引入额外的框架类型）。"""
    headers = [pair for pair in message.get("headers", []) if pair[0].lower() != name]
    headers.append((name, value.encode("latin-1")))
    message["headers"] = headers


def _get_path(document: Any, key: str) -> Any:
    cursor = document
    for part in key.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def _media_type(path: Path) -> str:
    return {
        ".html": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }.get(path.suffix.lower(), "application/octet-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_frame(event) -> str:
    return (
        f"id: {event.event_id}\n"
        f"event: {event.name}\n"
        f"data: {json.dumps(event.as_dict(), ensure_ascii=False)}\n\n"
    )
