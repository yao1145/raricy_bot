"""一次性验证票据（N2 Task 4、§4.2、§59 的「令牌、绑定与有效期」总表、D-146）。

票据把「页面刚刚用**这一份**账号与密码登录成功、服务端拿到的稳定 ID 是 X」这件事
钉在内存里，供紧接着的配置保存消费：保存时输入只要有一个字符不同（会话、档案、
账号或密码），票据就不再匹配，身份不会被写上。

不变量：

- **只存内存**：票据内容（尤其密码）不落盘、不写日志、不进事件；进程退出或
  `revoke_all()` 之后必须重新走一次站点登录（D-146）。
- **一次性**：`consume()` 成功即作废；未知、已过期、已用过的票据一律
  `verification_invalid`，输入不符一律 `verification_mismatch`。
- **惰性清理**：过期项在每次访问时顺手清掉，不启动后台线程、不依赖外部时钟推进。
- **服务端取得的稳定 ID 才作数**：`site_user_id` 由 `issue()` 的调用方从站点登录
  结果里取（绝不采信前端自报的 `account_id`）。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .profile_service import ProfileError

# 票据有效期（秒）：与总表一致，页面据此倒计时。
VERIFICATION_TTL_SECONDS: float = 600.0

# 稳定码：未知/过期/已用是 `verification_invalid`，绑定内容不符是 `verification_mismatch`。
CODE_VERIFICATION_INVALID: str = "verification_invalid"
CODE_VERIFICATION_MISMATCH: str = "verification_mismatch"


@dataclass(frozen=True)
class Ticket:
    """一次已验证身份的绑定内容（只存内存）。

    `password` 用 `repr=False`：票据对象一旦被打印、记日志或塞进异常文本，也不会
    把密码带出去。它是明文，只在 `verify` 与 `config` 两个调用之间存活。
    """

    session_id: str
    profile_id: str
    account: str
    password: str = field(repr=False)
    site_user_id: str = ""
    expires_at: float = 0.0


@dataclass(frozen=True)
class _Entry:
    ticket: Ticket
    used: bool = False


class VerificationStore:
    """进程内的一次性验证票据表；`Controller` 装配一个，停止时 `revoke_all()`。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = VERIFICATION_TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._tickets: dict[str, _Entry] = {}

    @property
    def ttl(self) -> float:
        """有效期（秒）；`verify` 的响应用它给出 `expires_in`。"""
        return self._ttl

    def issue(
        self,
        *,
        session_id: str,
        profile_id: str,
        account: str,
        password: str,
        site_user_id: str,
    ) -> str:
        """签发一张票据，返回不透明 id（随机、不可猜；只有本模块持有它的含义）。"""
        verification_id = secrets.token_urlsafe(32)
        ticket = Ticket(
            session_id=session_id,
            profile_id=profile_id,
            account=account,
            password=password,
            site_user_id=site_user_id,
            expires_at=self._clock() + self._ttl,
        )
        with self._lock:
            self._purge_locked()
            self._tickets[verification_id] = _Entry(ticket=ticket)
        return verification_id

    def consume(
        self,
        verification_id: str,
        *,
        session_id: str,
        profile_id: str,
        account: str | None,
        password: str | None,
    ) -> Ticket:
        """校验并消耗票据，返回它的绑定内容。

        未知、已过期、已用过 → `verification_invalid`（不区分，避免给探测者反馈）；
        会话、档案、账号或密码与签发时不一致 → `verification_mismatch`，且**不**消耗
        （用户可能只是提交了另一份输入，重来一次即可，不需要重新验证）。
        """
        if not isinstance(verification_id, str) or not verification_id:
            raise ProfileError(CODE_VERIFICATION_INVALID)
        with self._lock:
            self._purge_locked()
            entry = self._tickets.get(verification_id)
            if entry is None or entry.used:
                raise ProfileError(CODE_VERIFICATION_INVALID)
            ticket = entry.ticket
            if (
                ticket.session_id != session_id
                or ticket.profile_id != profile_id
                or ticket.account != account
                or ticket.password != password
            ):
                raise ProfileError(CODE_VERIFICATION_MISMATCH)
            self._tickets[verification_id] = _Entry(ticket=ticket, used=True)
        return ticket

    def revoke_all(self) -> None:
        """作废全部票据（`Controller.stop()` 调用）：重启后必须重新验证。"""
        with self._lock:
            self._tickets.clear()

    # --- 内部 -------------------------------------------------------------

    def _purge_locked(self) -> None:
        """惰性清理过期项；持锁调用，绝不在这里等任何外部资源。"""
        now = self._clock()
        expired = [
            verification_id
            for verification_id, entry in self._tickets.items()
            if now >= entry.ticket.expires_at
        ]
        for verification_id in expired:
            self._tickets.pop(verification_id, None)
