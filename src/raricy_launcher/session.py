"""本机会话：一次性引导令牌、会话 Cookie 与 CSRF（LIGHT_EDITION_DESIGN §8.1）。

- 桌面入口经激活通道取得**短期、单次**引导令牌；浏览器用 URL fragment 携带它，
  兑换成随机会话 Cookie（HttpOnly、SameSite=Strict，只在当前 Controller 生命周期内有效）。
- 令牌不放 query、不写磁盘、不写日志；兑换后立即作废，兑换接口有次数上限（防暴力猜）。
- 每个会话另有一个 CSRF 值：写请求必须同时带 Cookie 与它。
- Controller 重启让全部会话失效 —— 会话只存在内存里（§8.1）。
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from typing import Callable

SESSION_COOKIE: str = "raricy_light_session"
CSRF_HEADER: str = "X-Raricy-CSRF"

# 引导令牌存活时间：够用户点开浏览器即可，越短越好。
BOOTSTRAP_TTL_SECONDS: float = 120.0

# 会话空闲上限：管理页保持打开时会被刷新，长时间无交互即失效。
SESSION_IDLE_SECONDS: float = 1800.0

# 兑换接口的尝试上限：令牌本身就是高熵随机串，次数上限只挡住明显的暴力尝试。
# 按**时间窗**计而不是永久锁死：本机任何进程都能把窗口刷满，不能让它把用户
# 永久挡在自己的管理页之外（审查 M7）。
EXCHANGE_MAX_ATTEMPTS: int = 10
EXCHANGE_WINDOW_SECONDS: float = 60.0


@dataclass
class Session:
    """一个已建立的管理页会话。"""

    session_id: str
    csrf_token: str
    created_at: float
    last_seen_at: float


@dataclass
class _Bootstrap:
    token: str
    expires_at: float
    used: bool = False


class SessionManager:
    """内存会话表；进程退出即清空（§8.1）。"""

    def __init__(
        self,
        *,
        instance_id: str,
        clock: Callable[[], float],
        token_source: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._instance_id = instance_id
        self._clock = clock
        self._token_source = token_source
        self._bootstraps: list[_Bootstrap] = []
        self._sessions: dict[str, Session] = {}
        self._exchange_attempts = 0
        self._window_start: float | None = None

    # --- 引导令牌 ---------------------------------------------------------

    def issue_bootstrap(self) -> str:
        """签发一个新的引导令牌；旧令牌立即作废（同时只应有一个在途）。"""
        token = self._token_source(32)
        self._bootstraps = [
            _Bootstrap(token=token, expires_at=self._clock() + BOOTSTRAP_TTL_SECONDS)
        ]
        self._exchange_attempts = 0
        self._window_start = None
        return token

    def exchange(self, token: str) -> Session | None:
        """兑换引导令牌；失败返回 None（令牌不对、已用过、已过期或次数用尽）。"""
        now = self._clock()
        if self._window_start is None or now - self._window_start >= EXCHANGE_WINDOW_SECONDS:
            self._window_start = now
            self._exchange_attempts = 0
        self._exchange_attempts += 1
        if self._exchange_attempts > EXCHANGE_MAX_ATTEMPTS:
            return None
        for candidate in self._bootstraps:
            if candidate.used or candidate.expires_at <= now:
                continue
            if not hmac.compare_digest(candidate.token, token):
                continue
            candidate.used = True
            session = Session(
                session_id=self._token_source(32),
                csrf_token=self._token_source(32),
                created_at=now,
                last_seen_at=now,
            )
            self._sessions[session.session_id] = session
            return session
        return None

    # --- 会话 -------------------------------------------------------------

    def get(self, session_id: str | None) -> Session | None:
        """取会话并刷新空闲计时；过期或不存在时删除并返回 None。"""
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        now = self._clock()
        if now - session.last_seen_at > SESSION_IDLE_SECONDS:
            self._sessions.pop(session.session_id, None)
            return None
        session.last_seen_at = now
        return session

    def check_csrf(self, session: Session, token: str | None) -> bool:
        if not token:
            return False
        return hmac.compare_digest(session.csrf_token, token)

    def revoke_all(self) -> None:
        """使全部会话失效：退出或重启时调用（§8.1）。"""
        self._sessions.clear()
        self._bootstraps.clear()

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)
