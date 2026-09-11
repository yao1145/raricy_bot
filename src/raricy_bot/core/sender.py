"""消息发送器：脱敏、截断、配额预留、发消息与「结果不确定」时的对账。

发送流程见 `docs/INTERFACES.md` §13。两条硬约束：

- **预留不泄漏**：每一笔 `reserve()` 返回 ALLOW 的调用，在所有出口
  （成功、确定失败、重发失败、异常、取消）都恰好转为 `note_sent()` 或 `release()` 一次；
- **只有 `SiteError.status == 0` 才是结果不确定**，其余状态都是确定答复，
  不确定时才进入对账，且最多重发一次（D-8：对账窗口 `after=reply_to, limit=100`）。

日志只写白名单字段（频道、kind、reason、message_id），绝不写正文、Cookie 或密钥。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import BehaviorConfig
from ..logging_setup import get_logger, log_event
from ..quota import Decision, QuotaGuard
from ..redact import Redactor
from ..site.client import SiteClient, SiteError
from ..site.models import ChatMessage
from ..text_utils import truncate_at_paragraph
from ..store import Store

_LOGGER = get_logger("sender")

# 403 里判定「客户端配置错误」的标记（D-4）：命中说明是我方错误地设置了
# Origin / Referer，属于程序缺陷，不是账号权限问题，绝不该进入探测重试。
_CSRF_MARKERS: tuple[str, ...] = ("跨源", "CSRF")


def _forbidden_reason(message: str) -> str:
    """把 403 细分为 `csrf`（客户端配置错误）或 `forbidden`（权限/禁言）。"""
    if any(marker in message for marker in _CSRF_MARKERS):
        return "csrf"
    return "forbidden"


@dataclass(frozen=True)
class SendResult:
    """一次发送尝试的结果；`reason` 为稳定短标识，供日志与上层判定。"""

    delivered: bool
    message_id: int | None
    reason: str


class MessageSender:
    """站内消息发送器；配额、去重与对账逻辑都收敛在这里。"""

    def __init__(
        self,
        *,
        client: SiteClient,
        store: Store,
        quota: QuotaGuard,
        redactor: Redactor,
        cfg: BehaviorConfig,
        logger=None,
    ) -> None:
        self._client = client
        self._store = store
        self._quota = quota
        self._redactor = redactor
        self._cfg = cfg
        self._logger = logger if logger is not None else _LOGGER

    async def send(
        self,
        channel_id: str,
        text: str,
        reply_to: int | None,
        *,
        kind: str = "reply",
    ) -> SendResult:
        """脱敏、截断后发送；`kind` 原样透传给 `quota.reserve`（三值见 §10）。"""
        # 第 1 步：先脱敏，再在自然段边界截断。
        redacted = self._redactor.redact(text)
        content = truncate_at_paragraph(redacted, self._cfg.max_output_chars)[0]
        if not content.strip():
            return SendResult(False, None, "failed")

        # 第 2 步：配额预留；三种拒绝都不写 send_attempts。
        reservation = await self._quota.reserve(channel_id, kind)
        if not reservation.allowed:
            result = SendResult(False, None, self._deny_reason(reservation.decision))
            self._log(result, channel_id, kind)
            return result

        # 从这里开始持有预留：下面每一个出口都必须恰好 note_sent / release 一次。
        try:
            result, charge = await self._deliver(channel_id, content, reply_to)
        except BaseException:
            # 未预期的异常（含取消）也不能让预留泄漏。
            await self._quota.release(channel_id, kind)
            raise

        if charge:
            await self._quota.note_sent(channel_id, reply_to, kind)
        else:
            await self._quota.release(channel_id, kind)
        self._log(result, channel_id, kind)
        return result

    # --- 内部实现 ---

    async def _deliver(
        self, channel_id: str, content: str, reply_to: int | None
    ) -> tuple[SendResult, bool]:
        """执行一次 POST 并处理结果；返回 (结果, 是否应 note_sent)。"""
        try:
            message = await self._post_once(channel_id, content, reply_to)
        except SiteError as exc:
            return await self._on_error(channel_id, content, reply_to, exc)

        await self._record_sent(message, channel_id, reply_to)
        return SendResult(True, message.id if message is not None else None, "delivered"), True

    async def _post_once(
        self, channel_id: str, content: str, reply_to: int | None
    ) -> ChatMessage | None:
        """确保会话有效后发一次消息；失败抛 `SiteError`。"""
        await self._client.ensure_session()
        return await self._client.post_message(channel_id, content, reply_to=reply_to)

    async def _on_error(
        self, channel_id: str, content: str, reply_to: int | None, exc: SiteError
    ) -> tuple[SendResult, bool]:
        """把确定性的站点错误映射为发送结果；status == 0 才进入对账。"""
        self._note_site_error(exc)
        if exc.status == 429:
            return SendResult(False, None, "backoff"), False
        if exc.status == 403:
            # 细分 CSRF（我方配置错误）与权限/禁言（可能恢复），两者都不重试。
            return SendResult(False, None, _forbidden_reason(exc.message)), False
        if exc.status == 400 and reply_to is not None:
            return SendResult(False, None, "reply_target_gone"), False
        if exc.status == 0:
            return await self._reconcile(channel_id, content, reply_to)
        return SendResult(False, None, "failed"), False

    def _note_site_error(self, exc: SiteError) -> None:
        """站点错误的副作用：429 时登记退避（失败重发路径也必须走到）。"""
        if exc.status != 429:
            return
        wait = (
            exc.retry_after
            if exc.retry_after is not None
            else self._cfg.rate_limit_wait_seconds
        )
        self._quota.backoff(wait)

    async def _reconcile(
        self, channel_id: str, content: str, reply_to: int | None
    ) -> tuple[SendResult, bool]:
        """结果不确定时的对账：先查本地记录，再拉 `after=reply_to` 的最新一页。"""
        if reply_to is None:
            return SendResult(False, None, "failed"), False

        existing = await self._store.find_sent_for_reply(channel_id, reply_to)
        if existing is not None:
            # 此前那次发送已经记过账（note_sent），本次预留直接释放。
            return SendResult(True, existing, "deduped"), False

        try:
            messages = await self._client.fetch_messages(
                channel_id, limit=100, after=reply_to
            )
        except SiteError:
            # 对账查询本身失败：无法确认是否已发出，宁可不重发，避免双发。
            return SendResult(False, None, "failed"), False

        self_user = self._client.self_user
        self_id = self_user.id if self_user is not None else None
        for message in messages:
            if message.author.id != self_id:
                continue
            if message.reply is None or message.reply.id != reply_to:
                continue
            # 命中的消息确实由本机器人发出，只是此前没记账：补记并转正预留。
            # 补记同样是尽力而为，写库失败不得影响预算记账。
            await self._record_sent(message, channel_id, reply_to)
            return SendResult(True, message.id, "deduped"), True

        # 仍未命中：允许一次重发，仅一次。重发必须完整走第 5 步的错误副作用，
        # 尤其是 429 时的退避，否则会把站点的每分钟硬限撞穿。
        try:
            resent = await self._post_once(channel_id, content, reply_to)
        except SiteError as exc:
            self._note_site_error(exc)
            return SendResult(False, None, "failed"), False
        await self._record_sent(resent, channel_id, reply_to)
        return SendResult(True, resent.id if resent is not None else None, "delivered"), True

    async def _record_sent(
        self, message: ChatMessage | None, channel_id: str, reply_to: int | None
    ) -> None:
        """有消息体时记录已发回复。

        消息体缺失、或去重表写库失败，都是可接受的降级：消息**确实已经发出**，
        绝不能因此让异常逃逸（那会让预留被 release、24 小时预算少记一笔）。
        失败只记一条不含正文的 error 日志，调用方继续走 note_sent 转正。
        """
        if message is None:
            return
        try:
            await self._store.record_sent(message.id, channel_id, reply_to)
        except Exception as exc:  # record_sent 是尽力而为，不得影响预算记账
            log_event(
                self._logger,
                logging.ERROR,
                "sender.record_sent_failed",
                channel_id=channel_id,
                message_id=message.id,
                error=type(exc).__name__,
            )

    @staticmethod
    def _deny_reason(decision: Decision) -> str:
        """配额拒绝映射为 `SendResult.reason`。"""
        if decision is Decision.DENY_MINUTE:
            return "minute"
        if decision is Decision.DENY_BACKOFF:
            return "backoff"
        # DENY_DAILY 与 DENY_NOTICE 都归入 quota。
        return "quota"

    def _log(self, result: SendResult, channel_id: str, kind: str) -> None:
        """记录一条不含正文的发送事件。"""
        level = logging.INFO if result.delivered else logging.WARNING
        log_event(
            self._logger,
            level,
            "sender.send",
            channel_id=channel_id,
            kind=kind,
            reason=result.reason,
            message_id=result.message_id,
        )
