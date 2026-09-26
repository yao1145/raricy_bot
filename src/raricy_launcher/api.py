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
import hashlib
import json
import queue
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
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
from .credential_lifecycle import (
    STATE_OWNED,
    STATE_PENDING_REMOVAL,
    STATE_REVOKED,
    CredentialLifecycle,
)
from .credential_store import CredentialStoreError
from .desktop_settings import (
    DESKTOP_SETTINGS_CONFLICT,
    INVALID_DESKTOP_SETTINGS,
    STARTUP_TARGET_UNKNOWN,
    DesktopSettingsConflict,
    DesktopSettingsError,
    DesktopSettingsService,
)
from .lifecycle_gate import LifecycleGate, Ticket
from .lifecycle_service import (
    CODE_CONFIG_NOT_READY,
    CODE_IDEMPOTENCY_KEY_REQUIRED,
    CODE_LIFECYCLE_BUSY,
    CODE_QUITTING,
    ERROR_CREDENTIAL_BACKEND_UNAVAILABLE,
    ERROR_STOP_UNCONFIRMED,
    KIND_CREDENTIALS_CLEAR,
    OP_STATE_FAILED,
    OP_STATE_FINISHED,
    OP_STATE_RUNNING,
    STAGE_CLEAR_CREDENTIALS,
    STAGE_COMMIT_CONFIG,
    STAGE_STOP,
    CredentialRef,
    LifecycleService,
)
from .profile_removal import REMOVAL_SCOPES, RemovalService
from .profile_service import (
    IDENTITY_VERIFIED,
    PROFILE_STATE_ACTIVE,
    PROFILE_STATE_DELETING,
    PROFILE_STATE_DETACHED,
    ProfileError,
)
from .session import CSRF_HEADER, SESSION_COOKIE, Session, SessionManager
from .startup_service import (
    RESULT_APPLY_FAILED,
    RESULT_COMMAND_TOO_LONG,
    RESULT_OK,
    RESULT_PATH_UNUSABLE,
    RESULT_READ_FAILED,
    RESULT_REGISTRATION_CONFLICT,
    StartupFacts,
    StartupService,
)
from .verification import VerificationStore

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

# 桌面设置写请求的严格白名单（§59）：`expected_settings_revision` 必填，三个开关可省。
_DESKTOP_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "expected_settings_revision",
        "launch_at_sign_in",
        "start_bot_on_launch",
        "startup_profile_id",
    }
)
# 修复请求只接受版本守卫，绝不接受任意执行命令（§59）。
_REPAIR_KEYS: frozenset[str] = frozenset({"expected_settings_revision"})

# 启动项结果码 → 控制面稳定码与固定文案。`read_failed` 没有专属码，归入操作失败
# （读不到等于这次操作无法确认），不假装成功。
_STARTUP_REJECTION_CODES: dict[str, str] = {
    RESULT_COMMAND_TOO_LONG: "startup_command_too_long",
    RESULT_PATH_UNUSABLE: "startup_path_unusable",
    RESULT_REGISTRATION_CONFLICT: "startup_registration_conflict",
    RESULT_APPLY_FAILED: "startup_apply_failed",
    RESULT_READ_FAILED: "startup_apply_failed",
}
_STARTUP_REJECTION_MESSAGES: dict[str, str] = {
    "startup_command_too_long": texts.STARTUP_COMMAND_TOO_LONG,
    "startup_path_unusable": texts.STARTUP_PATH_UNUSABLE,
    "startup_registration_conflict": texts.STARTUP_REGISTRATION_CONFLICT,
    "startup_apply_failed": texts.STARTUP_APPLY_FAILED,
}

# --- 账号 API 的固定值（§59 的确切值总表，各任务共用，不各写一套） -------------

# 凭据清除的结果码（总表「结果码」表）；`lifecycle_service` 只定义到 remove 为止，
# 这一组的唯一出口是本模块的清除命令。
RESULT_CLEARED: str = "cleared"
RESULT_CLEARED_PARTIAL: str = "cleared_partial"
RESULT_CLEAR_FAILED: str = "clear_failed"
# 清除命令的配置窄写失败：凭据已经清了，但新引用没能写进配置（停在 `pending`）。
ERROR_CONFIG_WRITE_FAILED: str = "config_write_failed"

# 凭据清除可以单独勾选的类别；与 `credential_lifecycle` 的两种凭据一致。
CREDENTIAL_KINDS: tuple[str, ...] = ("password", "llm_api_key")

# 幂等键形状（§59 总表）：与协调器同一套规则。创建档案不写恢复记录，幂等表因此留在
# 本模块；校验规则必须逐字一致，不能各写一套。
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")
# 创建档案的幂等表容量：与协调器的「最近 50 个键」同量级。
MAX_CREATE_KEYS: int = 50

# `ApiError.details` 允许并入错误信封的键（§59）。服务层不得借这个通道夹带
# 别的字段（路径、凭据引用、异常文本都不是这里该出现的东西）。
_DETAIL_KEYS: frozenset[str] = frozenset(
    {"existing_profile_id", "profile_id", "operation_id", "scope"}
)

# 账号 API 的严格字段白名单：未列出的键一律 400 `bad_request`（§59）。
_PROFILE_CREATE_KEYS: frozenset[str] = frozenset(
    {"display_name", "expected_catalog_revision", "idempotency_key"}
)
_PROFILE_DRAFT_KEYS: frozenset[str] = frozenset(
    {"expected_revision", "expected_profile_revision", "values"}
)
_PROFILE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "expected_revision",
        "expected_profile_revision",
        "verification_id",
        "values",
        "credentials",
        "account",
        "display_name",
    }
)
_VERIFY_KEYS: frozenset[str] = frozenset({"account", "password"})
_ACTIVATE_KEYS: frozenset[str] = frozenset(
    {
        "expected_catalog_revision",
        "expected_epoch",
        "target_revision",
        "start",
        "idempotency_key",
    }
)
_REMOVAL_PREVIEW_KEYS: frozenset[str] = frozenset({"scope"})
_REMOVE_KEYS: frozenset[str] = frozenset(
    {"scope", "confirmation_token", "idempotency_key"}
)
_CREDENTIALS_CLEAR_KEYS: frozenset[str] = frozenset({"kinds", "idempotency_key"})

# 单个显示名的字符上限：只是防呆，显示名不参与任何判定。
MAX_DISPLAY_NAME_CHARS: int = 120

# 账号 API 的稳定码 → 固定文案（`texts.py` 是唯一来源）。只列本阶段新增、且
# 「码本身不足以说明接下来做什么」的码；其余保持只有码的既有形状。
_ACCOUNT_CODE_MESSAGES: dict[str, str] = {
    "client_upgrade_required": texts.CLIENT_UPGRADE_REQUIRED,
    "verification_required": texts.VERIFICATION_REQUIRED,
    "verification_invalid": texts.VERIFICATION_INVALID,
    "verification_mismatch": texts.VERIFICATION_MISMATCH,
    "profile_identity_taken": texts.PROFILE_IDENTITY_TAKEN,
    "profile_identity_mismatch": texts.PROFILE_IDENTITY_MISMATCH,
    "profile_state_conflict": texts.PROFILE_STATE_CONFLICT,
    "profile_revision_conflict": texts.PROFILE_REVISION_CONFLICT,
    "target_not_ready": texts.TARGET_NOT_READY,
    "idempotency_key_required": texts.IDEMPOTENCY_KEY_REQUIRED,
    "idempotency_conflict": texts.IDEMPOTENCY_CONFLICT,
    "credential_scope_required": texts.CREDENTIAL_SCOPE_REQUIRED,
    "credentials_index_corrupt": texts.CREDENTIALS_INDEX_BROKEN,
    "credentials_index_unreadable": texts.CREDENTIALS_INDEX_BROKEN,
    "credentials_index_unsupported_version": texts.CREDENTIALS_INDEX_BROKEN,
}
# 档案 ID 的形态由 `paths.validate_profile_id()` 判定，这里不复制第二套规则。


class ApiError(Exception):
    """把服务层异常映射成 HTTP 状态与稳定码（§11）。

    `message` 只在稳定码本身不足以说明用户能做什么时附带，取值来自
    `texts.py` 的固定文案；它不是第二种信封，也不透传服务层异常文本。
    `details` 只承载少量**受控标识**（`_DETAIL_KEYS` 白名单）：页面据此指到
    具体是哪个档案/操作，其余键在构造时就拒绝，服务层不能借它夹带别的字段。
    """

    def __init__(
        self,
        status: int,
        code: str,
        *,
        field: str | None = None,
        message: str | None = None,
        details: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field
        self.message = message
        if details:
            extra = sorted(set(details) - _DETAIL_KEYS)
            if extra:
                raise ValueError(f"unsupported detail key: {extra[0]}")
        self.details: dict[str, str] = dict(details or {})

    def payload(self) -> dict:
        body: dict[str, Any] = {"ok": False, "code": self.code}
        if self.field:
            body["field"] = self.field
        if self.message:
            body["message"] = self.message
        for key, value in self.details.items():
            body[key] = value
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
        profile_service,
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
        lifecycle_gate: LifecycleGate | None = None,
        desktop_settings: DesktopSettingsService | None = None,
        startup_service: StartupService | None = None,
        lifecycle_service: LifecycleService | None = None,
        removal_service: RemovalService | None = None,
        credential_lifecycle: CredentialLifecycle | None = None,
        verification_store: VerificationStore | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._data_root = Path(data_root)
        self._config = config_service
        # 档案级入口（`ProfileService`）：只读解析活动档案、按 id 取绑定服务。
        # 一次请求只解析一次，之后所有读写都用同一个绑定实例（§4.1 末句）。
        self._profiles = profile_service
        self._manager = manager
        self._status = status_service
        # 站点测试与启停共用的生命周期门（§59、D-132）。控制器装配一个进程内
        # 单实例；缺省只用于不共享进程的调用方，互斥范围就是这个对象。
        self._lifecycle = lifecycle_gate if lifecycle_gate is not None else LifecycleGate()
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
        # 桌面偏好与登录启动项（§58、§59）：一律由控制器装配注入。没注入时
        # 桌面设置仍可用（数据根下就是唯一来源），但启动项端点宁可直接失败，
        # 也**不自行打开真实注册表** —— 离线测试与缺注入的调用方不该碰系统。
        self._desktop = (
            desktop_settings
            if desktop_settings is not None
            else DesktopSettingsService(self._data_root)
        )
        self._startup_service = startup_service
        # 生命周期协调器（N2 Task 1）：账号 API 的命令（激活/移除/清除与启停）都经它
        # 串行化。未装配时只有 `/api/bot/*` 退回管理局直调 —— 那是隔离测试的调用方，
        # 生产装配（Controller）始终注入；账号 API 的其余端点没有退回路径，宁可如实
        # 报「配置不可用」也不自己造一条绕过协调器的写路径。
        self._lifecycle_service = lifecycle_service
        # 移除服务（N2 Task 3）：预览令牌与六步删除都经它；与协调器共用同一个实例
        # （令牌只在内存里，两个入口必须看到同一张表）。
        self._removal = removal_service
        # 凭据归属索引（N2 Task 2）：卡片的清理待办与清除命令用它。
        self._credential_lifecycle = credential_lifecycle
        # 一次性验证票据（N2 Task 4、D-146）：进程内、只存内存。
        self._verification = (
            verification_store if verification_store is not None else VerificationStore()
        )
        # 创建档案的幂等表（协调器的幂等表只覆盖写恢复记录的三种操作）。
        self._creates = _CreateIdempotency()
        # 同一进程内的创建串行化：查幂等表与建立档案必须在同一段临界区里，
        # 否则两个同键请求会各建一个目录。
        self._creates_lock = threading.Lock()
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
        # 服务层稳定错误的应用级兜底：路由里漏接的 `ConfigServiceError`（含
        # `ConfigConflict` / `ConfigInvalid`）同样回项目 JSON 信封与稳定码，而不是
        # Starlette 的纯文本 500（恢复态下启动/重启与知识库导入会走到这条路）。
        # 注册在应用上，将来新增的调用点自动覆盖，不靠逐处包 try。
        app.add_exception_handler(ConfigServiceError, self._service_error_response)

        app.add_api_route("/api/session/exchange", self._exchange, methods=["POST"])
        app.add_api_route("/api/profiles", self._list_profiles, methods=["GET"])
        app.add_api_route("/api/profiles", self._create_profile, methods=["POST"])
        app.add_api_route(
            "/api/profiles/{profile_id}/draft", self._get_profile_draft, methods=["GET"]
        )
        app.add_api_route(
            "/api/profiles/{profile_id}/draft", self._put_profile_draft, methods=["PUT"]
        )
        app.add_api_route(
            "/api/profiles/{profile_id}/verify", self._verify_profile, methods=["POST"]
        )
        app.add_api_route(
            "/api/profiles/{profile_id}/config", self._put_profile_config, methods=["PUT"]
        )
        app.add_api_route(
            "/api/profiles/{profile_id}/activate", self._activate_profile, methods=["POST"]
        )
        app.add_api_route(
            "/api/profiles/{profile_id}/removal-preview",
            self._removal_preview,
            methods=["POST"],
        )
        app.add_api_route(
            "/api/profiles/{profile_id}/remove", self._remove_profile, methods=["POST"]
        )
        app.add_api_route(
            "/api/profiles/{profile_id}/credentials/clear",
            self._clear_profile_credentials,
            methods=["POST"],
        )
        app.add_api_route("/api/config", self._get_config, methods=["GET"])
        app.add_api_route("/api/config", self._put_config, methods=["PUT"])
        app.add_api_route("/api/config/validate", self._validate_config, methods=["POST"])
        app.add_api_route("/api/config/draft", self._get_draft, methods=["GET"])
        app.add_api_route("/api/config/draft", self._put_draft, methods=["PUT"])
        app.add_api_route("/api/status", self._get_status, methods=["GET"])
        app.add_api_route("/api/desktop-settings", self._get_desktop_settings, methods=["GET"])
        app.add_api_route("/api/desktop-settings", self._put_desktop_settings, methods=["PUT"])
        app.add_api_route(
            "/api/desktop/startup-status", self._get_startup_status, methods=["GET"]
        )
        app.add_api_route(
            "/api/desktop/startup-repair", self._startup_repair, methods=["POST"]
        )
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

    async def _json_body(self, request: Request, *, limit: int = MAX_JSON_BYTES) -> dict:
        """读并解析 JSON 请求体；上限在**读取过程中**生效（审查 I6）。"""
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError as exc:
                raise ApiError(400, "bad_request") from exc
            if declared_bytes > limit:
                raise ApiError(413, "request_too_large")
        # 边读边判上限：ASGI 服务器不限制请求体，先整体缓冲再检查等于没有上限（审查 I6）。
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > limit:
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
        """把服务层错误映射成状态码与稳定码（§11）。

        档案错误（`ProfileError`，继承 `ConfigServiceError`）默认仍是 409 + 稳定码；
        只有三类需要显式分支：未知档案是 404、请求体缺幂等键与范围类参数是 422
        （总表把它们的 HTTP 状态钉死在 422，服务层只负责给稳定码）。
        """
        if isinstance(exc, ApiError):
            return exc
        if isinstance(exc, ConfigConflict):
            return ApiError(409, "revision_conflict")
        if isinstance(exc, ConfigInvalid):
            return ApiError(422, exc.code, field=exc.field)
        if isinstance(exc, ProfileError):
            code = str(exc)
            if code == "not_found":
                return ApiError(404, code)
            if code == CODE_IDEMPOTENCY_KEY_REQUIRED:
                return self._account_error(422, code, field="idempotency_key")
            if code == "removal_scope_invalid":
                return self._account_error(422, code, field="scope")
            if code == "credential_scope_required":
                return self._account_error(422, code, field="kinds")
            return self._account_error(409, code)
        if isinstance(exc, ConfigServiceError):
            return self._account_error(409, str(exc))
        if isinstance(exc, CredentialStoreError):
            return ApiError(503, str(exc))
        if isinstance(exc, DesktopSettingsConflict):
            # revision 不符：页面拿的是过期意图，先刷新（§58）。
            return ApiError(409, DESKTOP_SETTINGS_CONFLICT)
        if isinstance(exc, DesktopSettingsError):
            # 参数非法是 422，其余（不可读、损坏、版本不认识、写盘失败）都是
            # 「当前状态不允许这次操作」的 409，且必须显式映射 —— 包括
            # `desktop_settings_write_failed`（一次性导入落盘失败也会走这里）。
            code = str(exc)
            status = 422 if code == INVALID_DESKTOP_SETTINGS else 409
            return ApiError(status, code)
        if isinstance(exc, ConfigError):
            return ApiError(422, exc.kind, field=exc.field)
        return ApiError(500, "internal_error")

    @staticmethod
    def _account_error(status: int, code: str, *, field: str | None = None) -> ApiError:
        """稳定码 + 该码的固定文案（没有专属文案就只有码，与既有机制一致）。"""
        return ApiError(status, code, field=field, message=_ACCOUNT_CODE_MESSAGES.get(code))

    def _service_error_response(self, _request: Request, exc: Exception) -> JSONResponse:
        """应用级兜底：与逐路由的 `_handle` 用同一套状态码、JSON 信封与稳定码。"""
        mapped = self._handle(exc)
        return self._json(mapped.status, mapped.payload())

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
                # F1：配置页与接口都只支持保持不变/替换，完整清除在 N2 交付。这里
                # 如实回「暂不支持」，而不是转成 CredentialUpdate.delete() 让提交在
                # 置空后撞上必填校验、把原因误报成 `credentials_required`
                # （用户会以为只是没填凭据，也看不出功能本来就不存在）。
                # 动作常量与该分支保留在 config_service，供后续阶段复用。
                raise ApiError(
                    409,
                    "credential_delete_unavailable",
                    field=name,
                    message=texts.CREDENTIAL_DELETE_UNAVAILABLE,
                )
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

    # --- 档案上下文 -------------------------------------------------------

    def _profile_id(self, *, create: bool = False) -> str:
        """解析本次请求的档案：只读解析，或经写路径建立首个档案（D-135）。

        `create=False`（查询与纯校验）经 `active_profile_id()` 只读解析，没有
        档案就抛 409 `no_active_profile`，绝不建立目录或指针；`create=True`
        只允许写路径使用，经 `ensure_first_profile()` 建立或复用首个档案。
        元数据故障仍由 N0 的应用级处理器映射成 409 + 四个既有码。
        """
        if create:
            return self._profiles.ensure_first_profile()
        profile_id = self._profiles.active_profile_id()
        if profile_id is None:
            raise ApiError(409, "no_active_profile")
        return profile_id

    def _bound(self, profile_id: str):
        """该档案的绑定 `ConfigService`：本次请求的读写都用这一个实例（§4.1 末句）。"""
        return self._profiles.config_service(profile_id)

    def _read_service(self, profile_id: str | None):
        """只读上下文的配置服务：有档案用绑定实例，没有就用数据根级实例。

        数据根级实例的读路径同样不创建（D-133），因此「还没有档案」的查询与
        向导临时校验不会在真正空的根目录上留下任何文件。
        """
        if profile_id is None:
            return self._profiles.base_config_service()
        return self._bound(profile_id)

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
        """配置视图：只用只读上下文，绝不建立首个档案（D-133、D-135）。"""
        profile_id = self._profiles.active_profile_id()
        service = self._read_service(profile_id)
        saved = service.load_saved()
        values: dict[str, Any] = {}
        if saved is not None:
            for key in sorted(EDITABLE_FIELDS):
                values[key] = _get_path(saved.mapping, key)
        status = service.status()
        return {
            "ok": True,
            # 视图属于哪个档案：没有档案时是 None，与 revision 的 None 各自表达
            # 「还没有档案」与「还没有配置」两件事。
            "profile_id": profile_id,
            "revision": saved.revision if saved is not None else None,
            "state": status.state,
            "account": saved.account if saved is not None else None,
            "values": values,
            # 向导的初始值来自 Launcher 基线（含默认 System Prompt 模板），
            # 只含非敏感字段，且与提交时的基线同源（§5.1 第 3 点）；无档案时
            # 基线省略档案内路径字段，只回这份模板，不建目录、不写指针。
            "defaults": {
                key: _get_path(light_base_mapping(service.profile_or_none()), key)
                for key in sorted(EDITABLE_FIELDS)
                if key == "system_prompt"
            },
            "editable": sorted(EDITABLE_FIELDS),
            "credentials": self._credential_view(saved),
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
            # 过渡入口的活动代次门（§59、D-146）：旧页面不带上下文时明确要求升级，
            # 绝不把一次「当时看着 A」的提交落到刚刚切过去的 B 上。门返回的门里
            # 核过的档案与代次必须一路带进写路径，不能在工作线程里重新解析指针。
            target_profile_id, epoch = self._transition_gate(body)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            revision = await asyncio.to_thread(
                self._commit_config, body, target_profile_id, epoch
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, {"ok": True, "revision": revision})

    def _commit_config(
        self, body: dict, target_profile_id: str | None, expected_epoch: int
    ) -> int:
        """提交到**门里核过的那个档案**，并在写前复核代次（§59、D-146）。

        `target_profile_id` 由 `_transition_gate()` 在事件循环里核过；工作线程绝不
        重新解析活动指针 —— 从门校验到真正落盘之间，协调器的 `commit_active_B`
        完全可能把指针切到别的账号，那时按指针解析就会把这次编辑写进另一个档案
        （用户看不见的串账号写入，含凭据替换）。因此这里只对钉住的档案写，
        并在写前复核代次：指针变过就如实回 409，而不是写到一个已经不该写的地方。
        """
        if "start_bot_on_launch" in body:
            # 桌面偏好已移出配置面（§58）：如实回稳定码与去向，而不是静默忽略
            # 或多写一份会与 desktop.json 打架的副本。
            raise ApiError(
                422,
                "desktop_setting_moved",
                field="start_bot_on_launch",
                message=texts.DESKTOP_SETTING_MOVED,
            )
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
        # 写前复核：门之后活动代次变过（切换提交、删除清指针、新建首个档案）就拒绝。
        catalog = self._profiles.catalog()
        if catalog.active_epoch != expected_epoch:
            raise ApiError(
                409, "revision_conflict", field="expected_profile_epoch"
            )
        if target_profile_id is None:
            # 首次设置：门核过「当时没有档案」，写路径才允许建立首个档案
            # （profile.json 一并补齐）；请求体本身的错误已经在上面拒绝。
            target_profile_id = self._profiles.ensure_first_profile()
        else:
            if catalog.active_profile_id == target_profile_id:
                # 指针在场但档案目录可能还没建（N1 的首次写入语义）：补齐记录，
                # 已存在时不覆盖现场。
                self._profiles.ensure_first_profile()
            # 通用配置入口同样要拦 `deleting`（§6.2 第 1 步）：门只核了指针与代次，
            # 不核档案状态；正在删除的账号不接受任何新配置写入。
            self._reject_deleting(self._require_record(target_profile_id))
        return self._bound(target_profile_id).commit(
            values,
            expected_revision=expected,
            password=updates.get("password", CredentialUpdate.keep()),
            llm_api_key=updates.get("llm_api_key", CredentialUpdate.keep()),
            account=account,
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
        """静态校验：不写文件、不建档案、不调外部网络（§11）。

        无档案时用数据根级实例，`validate_values()` 内部按
        `light_base_mapping(None)` 校验（D-133）：真正空的根目录上不产生文件。
        """
        values = body.get("values") or {}
        if not isinstance(values, dict):
            raise ApiError(400, "bad_request")
        self._read_service(self._profiles.active_profile_id()).validate_values(values)

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
            revision = await asyncio.to_thread(self._save_draft, values, expected)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, {"ok": True, "revision": revision})

    # --- 桌面设置与登录启动（§8、§58、§59） --------------------------------

    async def _get_desktop_settings(self, request: Request):
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        try:
            body = await asyncio.to_thread(self._desktop_view)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, body)

    def _desktop_view(self) -> dict:
        """桌面偏好的读视图：只有三个开关与它自己的 revision（§58）。"""
        settings = self._desktop.read()
        return {
            "ok": True,
            "settings_revision": settings.settings_revision,
            "launch_at_sign_in": settings.launch_at_sign_in,
            "start_bot_on_launch": settings.start_bot_on_launch,
            "startup_profile_id": settings.startup_profile_id,
        }

    async def _put_desktop_settings(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            payload = await asyncio.to_thread(self._update_desktop_settings, body)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, payload)

    def _update_desktop_settings(self, body: dict) -> dict:
        """写意图 → 按当前意图应用 → 回读并把事实一起回给页面（§59）。

        应用失败**不回滚意图**：`applied=false` 加事实如实说明系统侧没做到什么，
        由页面显示差异。开启登录启动前先做写前判定，做不到的偏好不落盘。
        """
        intent = _desktop_intent(body)
        startup = self._startup_service_or_error()
        if intent.get("launch_at_sign_in") is True:
            rejection = startup.precheck()
            if rejection is not None:
                raise _startup_rejection(rejection, field="launch_at_sign_in")
        expected = intent.pop("expected_settings_revision")
        revision = self._desktop.update(expected, **intent)
        facts = startup.apply()
        return {
            "ok": True,
            "settings_revision": revision,
            "applied": facts.last_apply_result == RESULT_OK,
            "startup": _facts_payload(facts),
        }

    async def _get_startup_status(self, request: Request):
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        try:
            body = await asyncio.to_thread(self._startup_view)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, body)

    def _startup_view(self) -> dict:
        """一次观测：只看不写。

        `status()` 的 `last_apply_result` 是**本次观测**的结论（读不到或同名值非本
        产品持有时是 `read_failed` / `registration_conflict`），不写回设置文件、
        也不得当作持久值回用（文件里保留上一次真实应用的结果）。
        """
        startup = self._startup_service_or_error()
        settings = self._desktop.read()
        return {
            "ok": True,
            "settings_revision": settings.settings_revision,
            **_facts_payload(startup.status()),
        }

    async def _startup_repair(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            payload = await asyncio.to_thread(self._repair_startup, body)
        except Exception as exc:
            # 两个来源都在这里汇合：过期 revision 由 `repair()` 抛
            # `DesktopSettingsConflict`（409 desktop_settings_conflict），
            # 登记冲突/权限失败则以事实返回，由 `_startup_rejection` 映射成 409。
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, payload)

    def _repair_startup(self, body: dict) -> dict:
        """按当前 EXE 路径重新生成命令并执行；不接受任何调用方给的命令。"""
        guard = _desktop_intent(body, keys=_REPAIR_KEYS)
        startup = self._startup_service_or_error()
        settings = self._desktop.read()
        facts = startup.repair(guard["expected_settings_revision"])
        if facts.last_apply_result != RESULT_OK:
            raise _startup_rejection(facts.last_apply_result, field=None)
        return {
            "ok": True,
            "settings_revision": settings.settings_revision,
            "applied": True,
            "startup": _facts_payload(facts),
        }

    def _startup_service_or_error(self) -> StartupService:
        """取装配注入的启动项服务；没有注入就没有可用的注册表通道。"""
        if self._startup_service is None:
            # 防御性分支：控制面只在 Windows 上装配（其他平台在入口就退出），
            # 这里绝不自行创建注册表适配器。
            raise ApiError(409, "startup_apply_failed", message=texts.STARTUP_APPLY_FAILED)
        return self._startup_service

    def _save_draft(self, values: dict, expected_revision: int) -> int:
        """草稿是写路径：无档案时在这里建立首个档案（D-133、D-135）。

        解析出档案之后再拦一次 `deleting`：首次设置分支刚落下的记录是 `active`，
        自然放行；已存在的 `deleting` 档案不接受草稿（§6.2 第 1 步）。
        """
        profile_id = self._profile_id(create=True)
        self._reject_deleting(self._require_record(profile_id))
        return self._bound(profile_id).save_draft(
            values, expected_revision=expected_revision
        )

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
            self._transition_gate(body)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            operation_id = await asyncio.to_thread(self._start_bot_command, body)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(202, {"ok": True, "operation_id": operation_id})

    async def _bot_stop(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            if self._lifecycle_service is not None:
                operation_id = await asyncio.to_thread(self._lifecycle_service.stop_bot)
            else:
                operation_id = await asyncio.to_thread(self._manager_stop)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(202, {"ok": True, "operation_id": operation_id})

    async def _bot_restart(self, request: Request):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
            self._transition_gate(body)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            operation_id = await asyncio.to_thread(self._restart_bot_command, body)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(202, {"ok": True, "operation_id": operation_id})

    def _start_bot_command(self, body: dict) -> str:
        """启动命令：经协调器派发（代次在门内比较）；未装配协调器时退回管理局直调。"""
        if self._lifecycle_service is not None:
            return self._lifecycle_service.start_bot(
                expected_epoch=body["expected_profile_epoch"]
            )
        return self._dispatch_legacy("start", body)

    def _restart_bot_command(self, body: dict) -> str:
        if self._lifecycle_service is not None:
            return self._lifecycle_service.restart_bot(
                expected_epoch=body["expected_profile_epoch"]
            )
        return self._dispatch_legacy("restart", body)

    def _dispatch_legacy(self, kind: str, body: dict) -> str:
        """不装配协调器的调用方（隔离测试）：沿用 N1 的直调，但仍要过租约与代次门。

        请求里的 `revision` 不再参与：启动绑定的版本一律取该档案当前已保存的那一版
        （协调器一侧 `_saved_revision()`），旧页面发来的版本没有任何作用。
        """
        try:
            profile_id = self._profile_id()
        except ApiError:
            # 还没有档案：与「没有可启动的配置」同一结果码（既有语义）。
            raise ConfigServiceError(CODE_CONFIG_NOT_READY) from None
        revision = self._target_revision({}, profile_id)
        if revision is None:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY)
        ticket = self._lifecycle.begin_operation(kind)
        if ticket is None:
            raise ConfigServiceError(CODE_LIFECYCLE_BUSY)
        try:
            if kind == "start":
                operation = self._manager.start(revision=revision, profile_id=profile_id)
            else:
                operation = self._manager.restart(revision=revision, profile_id=profile_id)
        finally:
            self._lifecycle.end(ticket)
        return str(operation.operation_id)

    def _manager_stop(self) -> str:
        ticket = self._lifecycle.begin_operation("stop")
        if ticket is None:
            raise ConfigServiceError(CODE_LIFECYCLE_BUSY)
        try:
            operation = self._manager.stop()
        finally:
            self._lifecycle.end(ticket)
        return str(operation.operation_id)

    def _transition_gate(self, body: dict) -> tuple[str | None, int]:
        """过渡入口的活动代次门（§59、D-146）；返回门里核过的 `(档案, 代次)`。

        两个字段都必须在场：缺失说明调用方还是 N2 之前的页面，回
        `client_upgrade_required`（明确要求升级，**不**透明转发到刚切过去的账号）；
        在场但与当前不符是「拿着过期上下文」，回 `revision_conflict` 并指出是哪个
        字段过期。首次设置（还没有任何档案）时正确的取值是 `profile_id: null`、
        `expected_profile_epoch: 0`。

        返回值不是装饰：写路径必须带着它落到**同一个**档案上（见 `_commit_config()`），
        否则门只是把竞态窗口挪了个位置。

        元数据故障（N0 的四个码）在**判上下文之前**如实抛出：恢复态下「页面该刷新
        配置」比「页面该升级」更接近事实，且四个码是既有契约。
        """
        catalog = self._profiles.catalog()
        if "profile_id" not in body or "expected_profile_epoch" not in body:
            raise self._account_error(409, "client_upgrade_required")
        if body["profile_id"] != catalog.active_profile_id:
            raise ApiError(409, "revision_conflict", field="profile_id")
        epoch = body["expected_profile_epoch"]
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch != catalog.active_epoch:
            raise ApiError(409, "revision_conflict", field="expected_profile_epoch")
        return catalog.active_profile_id, catalog.active_epoch

    def _target_revision(self, body: dict, profile_id: str) -> int | None:
        """启动/重启的目标版本：请求指定优先，否则用该档案已保存的版本（§6.5）。

        只读这一次解析出来的档案：活动指针在这之后变化也不影响本次启动
        （§5.2 的输入固定）。指定的版本必须是该档案当前已保存的那一版：别的
        版本没有可用的运行快照，应当立刻回 409，而不是先答应再异步失败（审查 M8）。
        """
        saved = self._bound(profile_id).load_saved()
        if saved is None:
            return None
        requested = body.get("revision")
        if isinstance(requested, int) and not isinstance(requested, bool):
            return requested if requested == saved.revision else None
        return saved.revision

    async def _get_operation(self, request: Request, operation_id: str):
        """操作查询：先查协调器（带 `stage`），再查管理局（既有形状 + `stage=null`）。"""
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        if self._lifecycle_service is not None:
            record = self._lifecycle_service.operation(operation_id)
            if record is not None:
                return self._json(
                    200, {"ok": True, "operation": record.as_operation_view()}
                )
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
                    # 单次启停不写恢复记录、没有事务阶段：如实回 null。
                    "stage": None,
                    "result": operation.result,
                    "revision": operation.target_revision,
                    # 操作属于哪个档案：同号 revision 换档案时页面据此区分结果。
                    "profile_id": operation.profile_id,
                    "finished": operation.finished_at is not None,
                },
            },
        )

    # --- 测试 -------------------------------------------------------------

    async def _test_site(self, request: Request):
        """站点测试：只在 Bot 停止时执行，整段执行期间持有生命周期门（§8.2、D-132）。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        credentials = None
        if body:
            username = body.get("username")
            password = body.get("password")
            if (
                set(body) != {"username", "password"}
                or not isinstance(username, str)
                or not username.strip()
                or len(username) > 256
                or not isinstance(password, str)
                or not password
                or len(password) > 4096
            ):
                return self._json(422, {"ok": False, "code": "invalid_test_input"})
            credentials = Secrets(username=username, password=password, llm_api_key="")
        ticket = self._lifecycle.begin_test()
        if ticket is None:
            # 另一个站点测试或一次启停操作正持有门：不排队，直接拒绝（§59）。
            return self._json(409, {"ok": False, "code": "lifecycle_busy"})
        # 状态检查与测试都交给工作线程、都在租约覆盖内完成：检查之后不会再有新的
        # 启动被派发。租约也只由该线程的 `finally` 释放——请求协程被取消时线程
        # 仍在跑，协程侧释放等于把门开在测试进行中；`shield` 保证取消不传导给
        # 这个任务，线程照常跑完并释放。
        try:
            result, status = await asyncio.shield(
                asyncio.to_thread(self._run_site_test_under_lease, ticket, credentials)
            )
        except asyncio.CancelledError:
            # 请求协程被取消：线程仍在跑站点测试，租约只能留给它自己释放。
            raise
        except BaseException:
            # 其余异常只在线程结束时才抛回来（或线程根本没被调度，例如事件循环
            # 已关闭）；end() 幂等，线程释放过时这次是无操作。
            self._lifecycle.end(ticket)
            raise
        return self._json(status, result)

    def _site_test_blocker(self) -> str | None:
        """站点测试与身份验证共用的判据；不满足时返回稳定码。

        只有 `state` 还不够：restart 的停止阶段会先把状态写回 stopped / failed
        （`process_manager._stop_synchronously`），之后才写 starting
        （`_do_restart`），中间那段空档里 `state` 是测试允许的取值，而组合操作尚未
        完成 —— 光看状态会放行测试，让它与随即启动的新 Worker 并行。在途操作存在
        就拒绝：宁可保守地多拒一次，也不让测试和启动并行。判据不等待、不排队。

        协调器的未完成操作（切换/删除/清除事务）是**第三**条判据：事务在两次取门
        之间有窗口（提交指针之前），只看管理器状态会把站点测试放进去，而事务随后
        「指针已提交、B 未启动」的收尾会被门外的等待超时写成 `failed/lifecycle_busy`
        （§5.2 故障表第 4 行要求 `finished/result=start_failed`）。与状态聚合的
        `pending_operation` 同源（`current_operation()`），不另存一份判据。
        """
        state = self._manager.state
        if state == "running":
            # 机器人确实在运行：保留既有语义与稳定码（验证身份由页面先停止）。
            return "bot_running"
        if state not in ("stopped", "failed"):
            # starting / stopping：生命周期操作在途，不是「正在运行」。
            return "lifecycle_busy"
        pending = self._manager.current_operation()
        if pending is not None and pending.finished_at is None:
            return "lifecycle_busy"
        if self._lifecycle_service is not None and (
            self._lifecycle_service.current_operation() is not None
        ):
            # 协调器事务在途：与在途管理器操作同一结果码。
            return "lifecycle_busy"
        return None

    def _run_site_test_under_lease(
        self, ticket: Ticket, credentials_override: Secrets | None = None
    ) -> tuple[dict, int]:
        """在租约覆盖内检查进程状态并执行站点测试；租约只在本线程释放（D-132）。

        调用 `manager.state` 与派发测试之间没有释放动作，所以「看到 stopped」
        之后不会再有新的启动溜进来。
        """
        try:
            blocker = self._site_test_blocker()
            if blocker is not None:
                return {"ok": False, "code": blocker}, 409
            return self._run_site_test(credentials_override)
        finally:
            self._lifecycle.end(ticket)

    def _run_site_test(self, credentials_override: Secrets | None = None) -> tuple[dict, int]:
        """执行站点测试；档案上下文整段只解析一次（§4.1 末句）。"""
        try:
            profile_id = self._profiles.active_profile_id()
            service = self._read_service(profile_id)
            if credentials_override is None:
                status = service.status()
                saved = service.load_saved()
                if saved is None or status.state != STATE_CONFIGURED:
                    # 配置无效时连测试都不做：地址规则与凭据都没通过校验（审查 I7）。
                    return {"ok": False, "code": "config_not_ready"}, 409
                credentials = service.credentials_for(saved.revision)
                mapping = saved.mapping
                revision = saved.revision
                profile = service.profile()
            else:
                # 向导测试仅在内存中构造一次性配置；站点 URL 仍来自 Light 固定基线。
                # 无档案时基线省略档案内路径字段，config_dir 落到数据根，全程不创建文件。
                profile = service.profile_or_none()
                mapping = light_base_mapping(profile)
                mapping["model"] = {
                    "base_url": "https://draft.invalid/v1",
                    "model": "draft-model",
                }
                credentials = credentials_override
                revision = None
            config = parse_config(
                mapping,
                config_dir=str(profile if profile is not None else self._data_root),
                secrets=credentials,
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return mapped.payload(), mapped.status
        started = self._clock()
        account_id = None
        if credentials_override is None:
            outcome, detail = asyncio.run(self._probe_site(config, credentials))
        else:
            outcome, detail, account_id = asyncio.run(
                self._probe_site_identity(config, credentials)
            )
        elapsed = int((self._clock() - started) * 1000)
        if revision is not None:
            # 结果带档案身份：A、B 同为 rev 1 时不能互相顶替（§5.1 第 6 条）。
            self._status.record_test(
                "site",
                ok=outcome,
                detail=detail,
                revision=revision,
                profile_id=profile_id,
            )
        result = {"ok": outcome, "detail": detail, "elapsed_ms": elapsed}
        if account_id is not None:
            # 前端自报的 ID 不落盘、也不回填档案记录（§4.2 第 3 条）。
            result["account_id"] = account_id
        return result, 200

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
            # 稳定类别码；登录成功但探测失败要与登录失败分开报（§8.2）。
            return False, _site_error_detail(exc)
        except Exception as exc:
            return False, type(exc).__name__
        finally:
            await client.aclose()

    async def _probe_site_identity(self, config, credentials: Secrets) -> tuple[bool, str, str | None]:
        """向导站点测试同时确认稳定 user.id；聊天权限失败也保留已验证 ID。"""
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
            try:
                user = await client.login()
            except SiteError as exc:
                return False, _site_error_detail(exc), None
            account_id = str(user.id)
            try:
                await client.probe_chat()
            except SiteError as exc:
                # 登录已经成功：账号 ID 必须保留，票据照发（§4.2）。
                return False, _site_error_detail(exc), account_id
            return True, "chat_ready", account_id
        except Exception as exc:
            return False, type(exc).__name__, None
        finally:
            await client.aclose()

    async def _test_model(self, request: Request):
        """模型测试：单次在途、固定样例、小输出，不返回生成内容（§8.2）。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        transient_values = None
        if body:
            base_url = body.get("base_url")
            model = body.get("model")
            api_key = body.get("api_key")
            if (
                set(body) != {"base_url", "model", "api_key"}
                or not isinstance(base_url, str)
                or not base_url.strip()
                or len(base_url) > 2048
                or not isinstance(model, str)
                or not model.strip()
                or len(model) > 256
                or not isinstance(api_key, str)
                or not api_key.strip()
                or len(api_key) > 4096
            ):
                return self._json(422, {"ok": False, "code": "invalid_test_input"})
            transient_values = {
                "base_url": base_url.strip(),
                "model": model.strip(),
                "api_key": api_key,
            }
        if not self._model_test_lock.acquire(blocking=False):
            return self._json(409, {"ok": False, "code": "test_in_progress"})
        try:
            result, status = await asyncio.to_thread(self._run_model_test, transient_values)
        finally:
            self._model_test_lock.release()
        return self._json(status, result)

    def _run_model_test(self, transient_values: dict[str, str] | None = None) -> tuple[dict, int]:
        """执行模型测试；档案上下文整段只解析一次（§4.1 末句）。"""
        try:
            profile_id = self._profiles.active_profile_id()
            service = self._read_service(profile_id)
            if transient_values is None:
                saved = service.load_saved()
                if saved is None:
                    return {"ok": False, "code": "config_not_ready"}, 409
                credentials = service.credentials_for(saved.revision)
                mapping = saved.mapping
                revision = saved.revision
                profile = service.profile()
            else:
                # 向导临时值只在内存里；无档案时 config_dir 落到数据根，不创建文件。
                profile = service.profile_or_none()
                mapping = light_base_mapping(profile)
                mapping["model"] = {
                    "base_url": transient_values["base_url"],
                    "model": transient_values["model"],
                }
                credentials = Secrets(
                    username="draft", password="", llm_api_key=transient_values["api_key"]
                )
                revision = None
        except Exception as exc:
            mapped = self._handle(exc)
            return mapped.payload(), mapped.status
        started = self._clock()
        outcome, detail = asyncio.run(
            self._probe_model_mapping(
                mapping, profile if profile is not None else self._data_root, credentials
            )
        )
        elapsed = int((self._clock() - started) * 1000)
        if revision is not None:
            self._status.record_test(
                "model",
                ok=outcome,
                detail=detail,
                revision=revision,
                profile_id=profile_id,
            )
        return {"ok": outcome, "detail": detail, "elapsed_ms": elapsed}, 200

    async def _probe_model_mapping(
        self, mapping: dict, config_dir: Path, credentials: Secrets
    ) -> tuple[bool, str]:
        """测试已保存或向导临时映射；两条路径共用同一限时模型探测。"""
        try:
            config = parse_config(
                mapping,
                config_dir=str(config_dir),
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

    # --- 账号 API（N2 Task 4、§59） ---------------------------------------

    async def _list_profiles(self, request: Request):
        """账号列表：只读，绝不创建（空根目录回 200 + 空数组，D-135、§59）。"""
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        try:
            body = await asyncio.to_thread(self._profiles_view)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, body)

    def _profiles_view(self) -> dict:
        """一次请求内每个档案只解析一次上下文（§4.1 末句）。"""
        catalog = self._profiles.catalog()
        running_profile_id = self._manager.status().get("running_profile_id")
        startup_profile_id = self._startup_target()
        cards = [
            self._profile_card(
                record,
                catalog=catalog,
                running_profile_id=running_profile_id,
                startup_profile_id=startup_profile_id,
            )
            for record in self._profiles.list_profiles()
        ]
        return {
            "ok": True,
            "catalog": {
                "active_profile_id": catalog.active_profile_id,
                "active_epoch": catalog.active_epoch,
                "catalog_revision": catalog.catalog_revision,
                "schema_version": catalog.schema_version,
            },
            "profiles": cards,
        }

    def _profile_card(
        self, record, *, catalog, running_profile_id: str | None, startup_profile_id: str | None
    ) -> dict:
        """一张账号卡片（§59 的 ProfileCard）：字段名固定，动作由服务端判定。"""
        service = self._profiles.config_service(record.profile_id)
        status = service.status()
        try:
            saved = service.load_saved()
        except ConfigServiceError:
            # 配置读不出来：`config.state` 已经如实报错，卡片不再重复一次并让整个
            # 列表请求失败（一个坏档案不该挡住别的账号）。
            saved = None
        is_active = catalog.active_profile_id == record.profile_id
        if startup_profile_id == STARTUP_TARGET_UNKNOWN:
            # 桌面设置读不出来：无法判定，如实回 null 而不是 false（§59）。
            is_startup_target: bool | None = None
        else:
            is_startup_target = startup_profile_id == record.profile_id
        return {
            "profile_id": record.profile_id,
            "display_name": record.display_name,
            "account": saved.account if saved is not None else None,
            "site_user_id": record.site_user_id,
            "identity_state": record.identity_state,
            "state": record.state,
            "profile_revision": record.profile_revision,
            "config": {
                "state": status.state,
                "revision": status.revision,
                "error": status.error,
            },
            "is_active": is_active,
            "is_running": running_profile_id == record.profile_id,
            "is_startup_target": is_startup_target,
            "credentials": self._credentials_summary(record.profile_id),
            "actions": _card_actions(
                record, is_active=is_active, config_state=status.state
            ),
        }

    def _credentials_summary(self, profile_id: str) -> dict:
        """卡片的凭据摘要：后端可用性、清理待办与受管历史引用数。"""
        backend = self._credentials.describe()
        summary = {
            "backend": {"name": backend.name, "available": backend.available},
            "cleanup_pending": False,
            "historical_managed": 0,
            "unknown_ownership": False,
        }
        if self._credential_lifecycle is None:
            return summary
        try:
            managed = self._credential_lifecycle.managed_refs(profile_id)
            pending = profile_id in self._credential_lifecycle.pending_profiles()
            unreadable = self._credential_lifecycle.unreadable_documents(profile_id)
        except ConfigServiceError:
            # 索引读不出来：归属不完整，必须显示成「有清理待办」，不能假装干净。
            summary["cleanup_pending"] = True
            summary["unknown_ownership"] = True
            return summary
        summary["cleanup_pending"] = pending
        summary["historical_managed"] = len(managed)
        summary["unknown_ownership"] = bool(unreadable)
        return summary

    def _startup_target(self) -> str | None:
        """桌面设置里的启动目标；读不出来回**可区分的**中性哨兵（列表不因一个坏文件失败）。

        `None` 只表示「设置里没有启动目标」；`DesktopSettingsError` 必须走
        `STARTUP_TARGET_UNKNOWN`，否则「读不出来」会被渲染成「不是启动目标」——
        删除预览是不可逆动作前的检查，不能把未知说成否。
        """
        try:
            return self._desktop.read().startup_profile_id
        except DesktopSettingsError:
            return STARTUP_TARGET_UNKNOWN

    async def _create_profile(self, request: Request):
        """建立**非活动**账号：经协调器的串行化口径（不排队），幂等键保证只建一次。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
            intent = self._create_intent(body)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            result = await asyncio.to_thread(self._run_create, intent)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, result)

    def _create_intent(self, body: dict) -> dict:
        _strict_keys(body, _PROFILE_CREATE_KEYS)
        expected = _require_int(
            body, "expected_catalog_revision", code="invalid_revision"
        )
        key = _require_idempotency_key(body)
        display_name = body.get("display_name", "")
        if not isinstance(display_name, str):
            raise ApiError(422, "invalid_value", field="display_name")
        display_name = display_name.strip()
        if len(display_name) > MAX_DISPLAY_NAME_CHARS:
            raise ApiError(422, "invalid_value", field="display_name")
        return {
            "display_name": display_name,
            "expected_catalog_revision": expected,
            "idempotency_key": key,
        }

    def _run_create(self, intent: dict) -> dict:
        """创建档案：串行化 + 幂等。返回响应信封（不含任何凭据材料）。"""
        self._require_not_quitting()
        ticket = self._lifecycle.begin_operation("create")
        if ticket is None:
            # 站点测试持有租约：与其它写入口同一口径，不排队。
            raise ConfigServiceError(CODE_LIFECYCLE_BUSY)
        try:
            return self._create_locked(intent)
        finally:
            self._lifecycle.end(ticket)

    def _require_not_quitting(self) -> None:
        """退出流程已开始（`request_quit()` 之后）不再接受新的写命令。

        与协调器 `_require_launch_context()` 的 `quitting` 判定同口径、同一个标志位；
        协调器没有公开这个只读状态（它只在启停与切换路径内部判），因此这里按属性读取
        同一个标志而不是另存一份状态 —— 两份状态迟早会不一致。缺属性（未装配协调器
        的隔离调用方、替身）按「未退出」处理。
        """
        service = self._lifecycle_service
        if service is not None and getattr(service, "_quitting", False):
            raise ConfigServiceError(CODE_QUITTING)

    def _create_locked(self, intent: dict) -> dict:
        with self._creates_lock:
            key = intent["idempotency_key"]
            digest = _request_digest(
                {
                    "display_name": intent["display_name"],
                    "expected_catalog_revision": intent["expected_catalog_revision"],
                }
            )
            replayed = self._creates.lookup(key, digest)
            if replayed is not None:
                return {
                    "ok": True,
                    "profile_id": replayed,
                    "catalog_revision": self._profiles.catalog().catalog_revision,
                }
            self._require_no_operation()
            catalog = self._profiles.catalog()
            if catalog.catalog_revision != intent["expected_catalog_revision"]:
                raise ProfileError("revision_conflict")
            profile_id = self._profiles.create_profile(
                display_name=intent["display_name"]
            )
            self._creates.remember(key, digest, profile_id)
            return {
                "ok": True,
                "profile_id": profile_id,
                "catalog_revision": self._profiles.catalog().catalog_revision,
            }

    def _require_no_operation(self) -> None:
        """协调器有未完成的切换/删除/清除时不接受新的写命令（不排队）。"""
        if self._lifecycle_service is None:
            return
        if self._lifecycle_service.current_operation() is not None:
            raise ConfigServiceError(CODE_LIFECYCLE_BUSY)

    # --- 账号草稿与配置 -----------------------------------------------------

    async def _get_profile_draft(self, request: Request, profile_id: str):
        session = self._session(request)
        if session is None:
            return self._json(401, {"ok": False, "code": "unauthenticated"})
        try:
            body = await asyncio.to_thread(self._profile_draft_view, profile_id)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, body)

    def _profile_draft_view(self, profile_id: str) -> dict:
        record = self._require_record(profile_id)
        self._reject_deleting(record)
        draft = self._profiles.config_service(profile_id).load_draft()
        if draft is None:
            return {"ok": True, "revision": 0, "values": {}}
        return {
            "ok": True,
            "revision": draft.revision,
            "values": {
                key: _get_path(draft.mapping, key) for key in sorted(EDITABLE_FIELDS)
            },
        }

    async def _put_profile_draft(self, request: Request, profile_id: str):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
            _strict_keys(body, _PROFILE_DRAFT_KEYS)
            expected = _require_int(body, "expected_revision", code="invalid_revision")
            values = body.get("values", {})
            if not isinstance(values, dict):
                raise ApiError(400, "bad_request", field="values")
            expected_profile_revision = _optional_int(
                body, "expected_profile_revision", code="invalid_revision"
            )
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            revision = await asyncio.to_thread(
                self._save_profile_draft,
                profile_id,
                values,
                expected,
                expected_profile_revision,
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, {"ok": True, "revision": revision})

    def _save_profile_draft(
        self,
        profile_id: str,
        values: dict,
        expected_revision: int,
        expected_profile_revision: int | None,
    ) -> int:
        record = self._require_record(profile_id)
        self._reject_deleting(record)
        if (
            expected_profile_revision is not None
            and record.profile_revision != expected_profile_revision
        ):
            raise ProfileError("revision_conflict")
        return self._profiles.config_service(profile_id).save_draft(
            values, expected_revision=expected_revision
        )

    async def _put_profile_config(self, request: Request, profile_id: str):
        """保存该档案的配置；首次绑定身份必须消费验证票据（§4.2、§59）。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            payload = await asyncio.to_thread(
                self._save_profile_config, profile_id, session.session_id, body
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(200, payload)

    def _save_profile_config(
        self, profile_id: str, session_id: str, body: dict
    ) -> dict:
        """提交配置并在成功之后写身份；票据在提交前消费，绝不落盘。

        顺序固定：字段与状态校验 → 消费票据（一次性，改输入即失效）→ 写配置
        （`commit()` 的原子事务）→ 写身份（`bind_identity()`）→ 需要时把 `detached`
        恢复成 `active`。`detached → active` 只在这条路径上发生：必须用**同一稳定 ID**
        的票据重新保存一次（§6.2、§59）。
        """
        _strict_keys(body, _PROFILE_CONFIG_KEYS)
        expected = _require_int(body, "expected_revision", code="invalid_revision")
        expected_profile_revision = _require_int(
            body, "expected_profile_revision", code="invalid_revision"
        )
        values = body.get("values", {})
        if not isinstance(values, dict):
            raise ApiError(400, "bad_request", field="values")
        record = self._require_record(profile_id)
        self._reject_deleting(record)
        if record.profile_revision != expected_profile_revision:
            raise ApiError(409, "profile_revision_conflict", field="expected_profile_revision")
        updates = self._credential_updates(body)
        account = body.get("account")
        if account is not None and not isinstance(account, str):
            raise ApiError(400, "bad_request", field="account")
        display_name = body.get("display_name")
        if display_name is not None and not isinstance(display_name, str):
            raise ApiError(400, "bad_request", field="display_name")

        verification_id = body.get("verification_id")
        ticket = None
        if verification_id is not None:
            if not isinstance(verification_id, str) or not verification_id:
                raise ApiError(422, "invalid_value", field="verification_id")
            password = updates.get("password")
            password_value = password.value if password is not None else None
            if password is not None and password.action != ACTION_REPLACE:
                password_value = None
            ticket = self._verification.consume(
                verification_id,
                session_id=session_id,
                profile_id=profile_id,
                account=account,
                password=password_value,
            )
            if record.site_user_id and record.site_user_id != ticket.site_user_id:
                # 票据绑的稳定 ID 不是这个档案已绑定的那一个：拒绝，不悄悄改绑。
                raise ProfileError("verification_mismatch")
            conflict = self._identity_conflict(profile_id, ticket.site_user_id)
            if conflict is not None:
                raise ApiError(
                    409,
                    "profile_identity_taken",
                    message=texts.PROFILE_IDENTITY_TAKEN,
                    details={"existing_profile_id": conflict},
                )
        elif record.identity_state != IDENTITY_VERIFIED:
            # 没有票据、身份又没验证过：这次保存只能改草稿式内容，不能落配置。
            raise ApiError(409, "verification_required")
        was_detached = record.state == PROFILE_STATE_DETACHED

        revision = self._profiles.config_service(profile_id).commit(
            values,
            expected_revision=expected,
            password=updates.get("password", CredentialUpdate.keep()),
            llm_api_key=updates.get("llm_api_key", CredentialUpdate.keep()),
            account=account,
        )
        profile_revision = record.profile_revision
        if ticket is not None:
            saved_record = self._profiles.bind_identity(
                profile_id,
                site_user_id=ticket.site_user_id,
                display_name=display_name,
            )
            profile_revision = saved_record.profile_revision
        elif display_name is not None and record.site_user_id:
            # 改名不需要票据（身份没变），但写的必须是存档里的那一个 `site_user_id`。
            saved_record = self._profiles.bind_identity(
                profile_id,
                site_user_id=record.site_user_id,
                display_name=display_name,
            )
            profile_revision = saved_record.profile_revision
        if ticket is not None and was_detached:
            saved_record = self._profiles.set_state(
                profile_id, state=PROFILE_STATE_ACTIVE
            )
            profile_revision = saved_record.profile_revision
        return {"ok": True, "revision": revision, "profile_revision": profile_revision}

    def _identity_conflict(self, profile_id: str, site_user_id: str) -> str | None:
        """另一个非 `detached` 档案是否已占用该稳定 ID（占用则返回它的 id）。"""
        for other in self._profiles.list_profiles():
            if other.profile_id == profile_id:
                continue
            if other.state == PROFILE_STATE_DETACHED:
                continue
            if other.site_user_id == site_user_id:
                return other.profile_id
        return None

    # --- 身份验证 -----------------------------------------------------------

    async def _verify_profile(self, request: Request, profile_id: str):
        """用一次性输入登录站点、确认稳定 ID 并签发票据；整段持有站点测试租约。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            _strict_keys(body, _VERIFY_KEYS)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        account = body.get("account")
        password = body.get("password")
        if (
            not isinstance(account, str)
            or not account.strip()
            or len(account) > 256
            or not isinstance(password, str)
            or not password
            or len(password) > 4096
        ):
            return self._json(422, {"ok": False, "code": "invalid_test_input"})
        ticket = self._lifecycle.begin_test()
        if ticket is None:
            return self._json(409, {"ok": False, "code": "lifecycle_busy"})
        try:
            result, status = await asyncio.shield(
                asyncio.to_thread(
                    self._run_verify_under_lease,
                    ticket,
                    profile_id,
                    session.session_id,
                    account,
                    password,
                )
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            # 与 `/api/test/site` 同一口径：线程仍在跑，租约留给它自己释放。
            self._lifecycle.end(ticket)
            raise
        return self._json(status, result)

    def _run_verify_under_lease(
        self,
        ticket: Ticket,
        profile_id: str,
        session_id: str,
        account: str,
        password: str,
    ) -> tuple[dict, int]:
        """在租约内判定进程状态并执行验证；租约只在本线程释放（D-132）。"""
        try:
            blocker = self._site_test_blocker()
            if blocker is not None:
                return {"ok": False, "code": blocker}, 409
            return self._verify_profile_identity(profile_id, session_id, account, password)
        finally:
            self._lifecycle.end(ticket)

    def _verify_profile_identity(
        self, profile_id: str, session_id: str, account: str, password: str
    ) -> tuple[dict, int]:
        """登录站点并签发/拒绝票据；不写档案、不落盘任何输入。"""
        try:
            record = self._require_record(profile_id)
            self._reject_deleting(record)
            service = self._profiles.config_service(profile_id)
            profile = service.profile_or_none()
            # 站点地址取 Light 固定基线（与向导测试同一条路径）：验证身份时页面还
            # 没有保存配置，能相信的只有站点合同本身；模型部分用一次性占位值。
            mapping = light_base_mapping(profile)
            mapping["model"] = {
                "base_url": "https://draft.invalid/v1",
                "model": "draft-model",
            }
            credentials = Secrets(username=account, password=password, llm_api_key="")
            config = parse_config(
                mapping,
                config_dir=str(profile if profile is not None else self._data_root),
                secrets=credentials,
            )
            outcome, detail, site_user_id = asyncio.run(
                self._probe_site_identity(config, credentials)
            )
            if site_user_id is None:
                # 登录都没成功（账号或密码错、网络失败）：与 `/api/test/site` 同形。
                return {"ok": False, "detail": detail}, 200
            conflict = self._identity_conflict(profile_id, site_user_id)
            if conflict is not None:
                raise ApiError(
                    409,
                    "profile_identity_taken",
                    message=texts.PROFILE_IDENTITY_TAKEN,
                    details={"existing_profile_id": conflict},
                )
            expected = self._profiles.expected_site_user_id(profile_id)
            if (
                record.state == PROFILE_STATE_DETACHED
                and expected is not None
                and expected != site_user_id
            ):
                # 已移除（保留数据）的档案只能重新绑定**同一个**账号：稳定 ID 不符时
                # 拒绝，避免把它的历史数据接到另一个账号上。
                raise ApiError(409, "profile_identity_mismatch")
            verification_id = self._verification.issue(
                session_id=session_id,
                profile_id=profile_id,
                account=account,
                password=password,
                site_user_id=site_user_id,
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return mapped.payload(), mapped.status
        return (
            {
                "ok": True,
                "verification_id": verification_id,
                "site_user_id": site_user_id,
                # 聊天探测失败不影响签发票据：身份以登录结果为准（§4.2）。
                "chat_ready": outcome,
                "expires_in": int(self._verification.ttl),
            },
            200,
        )

    # --- 切换、移除与清除 ---------------------------------------------------

    async def _activate_profile(self, request: Request, profile_id: str):
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
            intent = self._activate_intent(body)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        try:
            operation_id = await asyncio.to_thread(
                self._run_activate_command, profile_id, intent
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(202, {"ok": True, "operation_id": operation_id})

    def _activate_intent(self, body: dict) -> dict:
        _strict_keys(body, _ACTIVATE_KEYS)
        intent = {
            "expected_catalog_revision": _require_int(
                body, "expected_catalog_revision", code="invalid_revision"
            ),
            "expected_epoch": _require_int(body, "expected_epoch", code="invalid_revision"),
            "start": _optional_bool(body, "start", default=True),
            "target_revision": _optional_int(
                body, "target_revision", code="invalid_revision"
            ),
            "idempotency_key": _require_idempotency_key(body),
        }
        return intent

    def _run_activate_command(self, profile_id: str, intent: dict) -> str:
        if self._lifecycle_service is None:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY)
        target_revision = intent["target_revision"]
        if target_revision is None and intent["start"]:
            # 没指定版本时绑定该档案当前已保存的那一版（与启停同一口径）：指定/求得的
            # 版本必须与档案当下一致，否则协调器直接回 `target_not_ready`。
            saved = self._profiles.config_service(profile_id).load_saved()
            target_revision = saved.revision if saved is not None else None
        return self._lifecycle_service.activate(
            profile_id,
            expected_epoch=intent["expected_epoch"],
            expected_catalog_revision=intent["expected_catalog_revision"],
            target_revision=target_revision,
            start=intent["start"],
            idempotency_key=intent["idempotency_key"],
        )

    async def _removal_preview(self, request: Request, profile_id: str):
        """删除预览（只读）：签发只存内存的确认令牌，不做任何删除（§6.2、§59）。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
            _strict_keys(body, _REMOVAL_PREVIEW_KEYS)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        scope = body.get("scope")
        if not isinstance(scope, str) or scope not in REMOVAL_SCOPES:
            return self._json(422, {"ok": False, "code": "removal_scope_invalid", "field": "scope"})
        try:
            preview = await asyncio.to_thread(self._preview_removal, profile_id, scope, session.session_id)
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(
            200,
            {
                "ok": True,
                "preview": preview.to_document(),
                "confirmation_token": preview.confirmation_token,
                "expires_in": preview.expires_in,
            },
        )

    def _preview_removal(self, profile_id: str, scope: str, session_id: str):
        if self._removal is None:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY)
        return self._removal.preview(profile_id, scope=scope, session_id=session_id)

    async def _remove_profile(self, request: Request, profile_id: str):
        """按确认令牌发起删除；令牌在预留记录之前校验并消耗（§6.2、§59）。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
            _strict_keys(body, _REMOVE_KEYS)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        scope = body.get("scope")
        if not isinstance(scope, str) or scope not in REMOVAL_SCOPES:
            return self._json(422, {"ok": False, "code": "removal_scope_invalid", "field": "scope"})
        token = body.get("confirmation_token")
        if not isinstance(token, str) or not token:
            return self._json(422, {"ok": False, "code": "removal_token_invalid", "field": "confirmation_token"})
        key = body.get("idempotency_key")
        if not isinstance(key, str) or not key:
            return self._json(422, {"ok": False, "code": "idempotency_key_required", "field": "idempotency_key"})
        try:
            operation_id = await asyncio.to_thread(
                self._run_remove_command, profile_id, scope, token, session.session_id, key
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(202, {"ok": True, "operation_id": operation_id})

    def _run_remove_command(
        self, profile_id: str, scope: str, token: str, session_id: str, key: str
    ) -> str:
        if self._lifecycle_service is None:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY)
        return self._lifecycle_service.remove(
            profile_id,
            scope=scope,
            confirmation_token=token,
            session_id=session_id,
            idempotency_key=key,
        )

    async def _clear_profile_credentials(self, request: Request, profile_id: str):
        """清除该档案受管凭据的一个子集；破坏性操作走 202 + operation_id（§6.1）。"""
        try:
            session = self._require_session(request)
            self._require_write(request, session)
            body = await self._json_body(request)
            _strict_keys(body, _CREDENTIALS_CLEAR_KEYS)
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
        kinds = body.get("kinds")
        if (
            not isinstance(kinds, list)
            or not kinds
            or not all(isinstance(item, str) and item in CREDENTIAL_KINDS for item in kinds)
        ):
            return self._json(422, {"ok": False, "code": "credential_scope_required", "field": "kinds"})
        key = body.get("idempotency_key")
        if not isinstance(key, str) or not key:
            return self._json(422, {"ok": False, "code": "idempotency_key_required", "field": "idempotency_key"})
        try:
            operation_id = await asyncio.to_thread(
                self._submit_credentials_clear, profile_id, tuple(sorted(set(kinds))), key
            )
        except Exception as exc:
            mapped = self._handle(exc)
            return self._json(mapped.status, mapped.payload())
        return self._json(202, {"ok": True, "operation_id": operation_id})

    def _submit_credentials_clear(
        self, profile_id: str, kinds: tuple[str, ...], idempotency_key: str
    ) -> str:
        """经协调器预留并派发一次凭据清除（串行化、幂等键、写恢复记录）。

        命令体（`_run_credentials_clear`）住在 API 层：协调器只提供串行化、记录与
        阶段钩子，不定义清除动作本身；这与 Task 2 的交接一致（清除的范围、保留项与
        结果码由凭据生命周期服务给出）。
        """
        if self._lifecycle_service is None:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY)
        lifecycle = self._credential_lifecycle
        if lifecycle is None:
            raise ConfigServiceError(CODE_CONFIG_NOT_READY)
        request = {"profile_id": profile_id, "kinds": list(kinds)}

        def validate() -> None:
            # 破坏性操作拒绝推进：坏索引在预留之前就失败（§6.1）。
            lifecycle.ensure_readable()
            record = self._require_record(profile_id)
            self._reject_deleting(record)

        def body(context) -> None:
            self._run_credentials_clear(context, profile_id, kinds, lifecycle)

        return self._lifecycle_service.submit(
            kind=KIND_CREDENTIALS_CLEAR,
            profile_id=profile_id,
            idempotency_key=idempotency_key,
            request=request,
            body=body,
            validate=validate,
            to_profile_id=profile_id,
        )

    def _run_credentials_clear(self, context, profile_id, kinds, lifecycle) -> None:
        """清除命令体：`stop` → `clear_credentials` → `commit_config`（§59 阶段码）。

        只要 `clear()` 正常返回就继续写配置：`ok=False` 只影响结果码
        （`cleared_partial`）与卡片的清理待办，不表示什么都没清 —— 只有 `clear()`
        抛异常才是「凭据库这一侧完全没动」。
        """
        with context.stage(STAGE_STOP, state=OP_STATE_RUNNING):
            if context.running_profile_id() == profile_id:
                stop_operation = context.stop_worker()
                if not context.await_exit(stop_operation):
                    context.finish(
                        state=OP_STATE_FAILED,
                        result=RESULT_CLEAR_FAILED,
                        error=ERROR_STOP_UNCONFIRMED,
                    )
                    return
        service = self._profiles.config_service(profile_id)
        saved = service.load_saved()
        if saved is None:
            context.finish(
                state=OP_STATE_FAILED, result=RESULT_CLEAR_FAILED, error=CODE_CONFIG_NOT_READY
            )
            return
        with context.stage(STAGE_CLEAR_CREDENTIALS):
            # 关键副作用之前先落记录：哪些引用属于这个档案（只有不透明引用）。
            managed = lifecycle.managed_refs(profile_id)
            context.record_details(
                credentials=tuple(CredentialRef(ref, STATE_OWNED) for ref in managed)
            )
            try:
                result = lifecycle.clear(profile_id, kinds=kinds)
            except (CredentialStoreError, ConfigServiceError):
                # 凭据阶段整体失败（后端不可用、索引坏掉）：一件都没做。
                context.finish(
                    state=OP_STATE_FAILED,
                    result=RESULT_CLEAR_FAILED,
                    error=ERROR_CREDENTIAL_BACKEND_UNAVAILABLE,
                )
                return
            entries = [CredentialRef(ref, STATE_REVOKED) for ref in result.revoked]
            entries += [
                CredentialRef(ref, STATE_PENDING_REMOVAL) for ref in result.pending_refs
            ]
            if result.new_ref is not None:
                # 新引用要等配置窄写成功才由 `commit_credentials_clear()` 标成 owned，
                # 这里如实写 `pending`（没写上就是 `cleared_partial`）。
                entries.append(CredentialRef(result.new_ref, "pending"))
            context.record_details(credentials=tuple(entries))
        with context.stage(STAGE_COMMIT_CONFIG):
            try:
                service.commit_credentials_clear(
                    expected_revision=saved.revision,
                    credentials_ref=result.new_ref,
                    account=saved.account,
                )
            except (ConfigServiceError, KeyError):
                # 凭据清了，但这一版配置没写进去：部分完成，如实报。
                context.finish(
                    state=OP_STATE_FINISHED,
                    result=RESULT_CLEARED_PARTIAL,
                    error=ERROR_CONFIG_WRITE_FAILED,
                )
                return
        context.finish(
            state=OP_STATE_FINISHED,
            result=RESULT_CLEARED if result.ok else RESULT_CLEARED_PARTIAL,
        )

    def _require_record(self, profile_id: str):
        """按 id 取档案记录；未知、已删除（墓碑）一律 404 `not_found`。"""
        record = self._record_or_none(profile_id)
        if record is None:
            raise ApiError(404, "not_found")
        return record

    def _record_or_none(self, profile_id: str):
        """按 id 取档案记录；没有（含墓碑）返回 None。

        只给 N1 语义下允许「指针在场、档案目录/记录还没建」的入口用（KB 导入），
        其余写路径一律用 `_require_record()` 的 404 口径。
        """
        for record in self._profiles.list_profiles():
            if record.profile_id == profile_id:
                return record
        return None

    @staticmethod
    def _reject_deleting(record) -> None:
        """`deleting` 档案拒绝配置、草稿与知识库写入（§6.2 第 1 步）。

        档案级路由与**通用入口**（`PUT /api/config`、`PUT /api/config/draft`、
        `POST /api/kb/import`）都过这一条判据。
        """
        if record.state == PROFILE_STATE_DELETING:
            raise ApiError(409, "profile_state_conflict")

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
            # 只读解析：无档案时如实回 409 `no_active_profile`，不建目录（D-135）。
            # 元数据故障是另一类事实（ConfigServiceError），由应用级处理器回它
            # 自己的稳定码，不再被折成「没有档案」。
            profile = self._bound(self._profile_id()).profile()
        except ApiError as exc:
            return self._json(exc.status, exc.payload())
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
            # 导入走更宽的传输上限（JSON 包装另留余量）：单文件上限才是真正生效的
            # 那道门，而不是被通用请求体上限抢先拒绝（审查 M9）。
            body = await self._json_body(request, limit=MAX_IMPORT_BYTES + 16 * 1024)
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
        # 导入是写路径，但目标档案必须是**已有**的：没有档案时回 409
        # `no_active_profile`，不在这里顺手建一个（D-135）。删除中的档案同样拒绝：
        # 往正在删除的账号里写知识库文件违反 §6.2 第 1 步。指针在场但档案记录
        # 还没建（N1 的首次写入语义）仍照旧放行 —— 那不是「删除中」。
        profile_id = self._profile_id()
        record = self._record_or_none(profile_id)
        if record is not None:
            self._reject_deleting(record)
        profile = self._bound(profile_id).profile()
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


class _CreateIdempotency:
    """创建档案的幂等表：内存、有界、不落盘。

    协调器的幂等表只覆盖写恢复记录的三种操作（`activate` / `remove` /
    `credentials_clear`），创建档案不写恢复记录，因此这一张表留在 API 层。规则与
    协调器逐条一致：同键 + 同摘要永远回同一 `profile_id`；同键 + 不同摘要抛
    `idempotency_conflict`；只保留最近 `MAX_CREATE_KEYS` 个键。
    """

    def __init__(self, capacity: int = MAX_CREATE_KEYS) -> None:
        self._capacity = capacity
        self._entries: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        self._lock = threading.Lock()

    def lookup(self, key: str, digest: str) -> str | None:
        """幂等命中返回 `profile_id`；同键不同摘要抛 `idempotency_conflict`。"""
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        profile_id, stored = entry
        if stored != digest:
            raise ProfileError("idempotency_conflict")
        return profile_id

    def remember(self, key: str, digest: str, profile_id: str) -> None:
        with self._lock:
            self._entries[key] = (profile_id, digest)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)


def _strict_keys(body: dict, allowed: frozenset[str]) -> None:
    """严格字段白名单：未列出的键一律 400 `bad_request`（§59）。"""
    extra = sorted(set(body) - allowed)
    if extra:
        raise ApiError(400, "bad_request", field=extra[0])


def _site_error_detail(exc: SiteError) -> str:
    """站点错误的稳定类别码：有站点 code 回 `site_http_<code>`，网络层回 `site_network`。

    `SiteError` 只有 `status` / `message` / `retry_after`（没有 `reason`），且站方
    文本一律不进 `detail`（§8.2 的红线）—— 这里只回固定类别码。
    """
    return f"site_http_{exc.status}" if exc.status else "site_network"


def _require_int(body: dict, key: str, *, code: str) -> int:
    """必填的非负整数；缺失或类型不对 → 422 + 稳定码（含 `field`）。"""
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiError(422, code, field=key)
    return value


def _optional_int(body: dict, key: str, *, code: str) -> int | None:
    if key not in body or body[key] is None:
        return None
    return _require_int(body, key, code=code)


def _optional_bool(body: dict, key: str, *, default: bool) -> bool:
    if key not in body:
        return default
    value = body[key]
    if not isinstance(value, bool):
        raise ApiError(422, "invalid_value", field=key)
    return value


def _require_idempotency_key(body: dict) -> str:
    """幂等键：缺失或形状不符是一样的结果码与状态（§59 总表）。"""
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY_RE.match(key):
        raise ApiError(422, CODE_IDEMPOTENCY_KEY_REQUIRED, field="idempotency_key")
    return key


def _request_digest(fields: Mapping[str, Any]) -> str:
    """请求摘要：排序键、无秘密的 sha256（幂等比较只在内存里发生）。"""
    payload = json.dumps(dict(sorted(fields.items())), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _card_actions(record, *, is_active: bool, config_state: str) -> list[str]:
    """卡片的可行动作（服务端判定，页面不自己推断，§59 的 ProfileCard）。

    - `deleting`：删除事务没做完，只能重新预览并继续；
    - `detached`：已经移除（保留数据），只能彻底删除或用同一账号重新绑定；
    - 其余（`active`）：编辑、清除凭据、移除与验身份总是可用；非活动档案在
      身份已验证且配置就绪时才多出「选中」与「选中并启动」。
    """
    if record.state == PROFILE_STATE_DELETING:
        return ["remove", "purge"]
    if record.state == PROFILE_STATE_DETACHED:
        return ["purge", "rebind"]
    actions = ["edit", "clear_credentials", "remove", "purge", "verify"]
    if (
        not is_active
        and record.identity_state == IDENTITY_VERIFIED
        and config_state == STATE_CONFIGURED
    ):
        actions = ["activate", "activate_and_start", *actions]
    return actions


def _desktop_intent(
    body: dict, *, keys: frozenset[str] = _DESKTOP_SETTING_KEYS
) -> dict[str, Any]:
    """桌面设置写请求的解析与白名单（§59）：多余键、缺版本守卫、类型不对都 422。

    只有**出现过的**开关才进返回值：省略表示本次不改它，`startup_profile_id=None`
    才是显式清空（`DesktopSettingsService.update()` 的 `_UNSET` 语义）。档案 id 的
    形态校验交给设置服务（非法即 `invalid_desktop_settings`），目标是否真的存在
    属于档案服务的判定，本阶段尚未落地。
    """
    extra = sorted(set(body) - keys)
    if extra:
        raise ApiError(422, INVALID_DESKTOP_SETTINGS, field=extra[0])
    expected = body.get("expected_settings_revision")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise ApiError(422, INVALID_DESKTOP_SETTINGS, field="expected_settings_revision")
    intent: dict[str, Any] = {"expected_settings_revision": expected}
    for key in ("launch_at_sign_in", "start_bot_on_launch"):
        if key in body:
            if not isinstance(body[key], bool):
                raise ApiError(422, INVALID_DESKTOP_SETTINGS, field=key)
            intent[key] = body[key]
    if "startup_profile_id" in body:
        target = body["startup_profile_id"]
        if target is not None and not isinstance(target, str):
            raise ApiError(422, INVALID_DESKTOP_SETTINGS, field="startup_profile_id")
        intent["startup_profile_id"] = target
    return intent


def _facts_payload(facts: StartupFacts) -> dict:
    """启动项事实的显式响应形状（§59）：只给本程序算出的命令与布尔事实。

    不返回注册表里读到的原始命令内容 —— 那可能是别的应用写在同一值名下的命令行。
    """
    pending = facts.pending_apply
    return {
        "requested_enabled": facts.requested_enabled,
        "registration_present": facts.registration_present,
        "command_matches": facts.command_matches,
        "executable_exists": facts.executable_exists,
        "effective_state": facts.effective_state,
        "divergence": facts.divergence,
        "last_apply_result": facts.last_apply_result,
        "pending_apply": (
            None if pending is None else {"action": pending.action, "command": pending.command}
        ),
        "expected_command": facts.expected_command,
    }


def _startup_rejection(result: str, *, field: str | None) -> ApiError:
    """启动项结果码 → 409 与固定文案；没有专属码的结果归入 `startup_apply_failed`。"""
    code = _STARTUP_REJECTION_CODES.get(result, "startup_apply_failed")
    return ApiError(409, code, field=field, message=_STARTUP_REJECTION_MESSAGES[code])


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
    """事件帧：不带 `event:` 行，走 SSE 默认的 `message` 事件。

    事件名放在 data 里。具名 `event:` 只会派发给同名的 addEventListener，
    而事件名是开放的（每个 `log_event` 名都可能出现）—— 客户端不可能预先
    枚举，于是「近期事件」永远是空的（复审 F4）。
    """
    return (
        f"id: {event.event_id}\n"
        f"data: {json.dumps(event.as_dict(), ensure_ascii=False)}\n\n"
    )
