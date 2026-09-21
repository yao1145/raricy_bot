"""消息发送器：脱敏、截断、配额预留、发消息与「结果不确定」时的对账。

发送流程见 `docs/design/INTERFACES.md` §13。两条硬约束：

- **预留不泄漏**：每一笔 `reserve()` 返回 ALLOW 的调用，在所有出口
  （成功、确定失败、重发失败、异常、取消）都恰好转为 `note_sent()` 或 `release()` 一次；
- **已确认送达不被取消改判**：`POST` 成功返回后，本地发送记录与配额转正收敛为
  一次受取消保护的终结操作（`_commit_delivery`）；调用方在等待结算时被取消，
  只延迟取消传播，不会把这次已送达的发送降级成 release。等待是**有界**的
  （`_SETTLE_WAIT_TIMEOUT_SECONDS`）：超时后不再等待，但终结任务本身不被取消、
  仍持有强引用；因此关闭路径上超预算的病态阻塞会丢掉那一笔本地记账
  （预留的结清仍由 `quota.note_sent` 的取消保护兜住，写库却可能撞上已关闭的
  Store —— 这是刻意的取舍：宁可 `stop()` 能结束，也不要无限挂住）；
- **只有 `SiteError.status == 0` 才是结果不确定**，其余状态都是确定答复，
  不确定时才进入对账，且最多重发一次（D-8：对账窗口 `after=reply_to, limit=100`）。
  远端结果尚不确定时被取消，保留既有语义：释放预留，绝不盲目重投（计划 §4.2 第 7 条）。

日志只写白名单字段（频道、kind、reason、message_id），绝不写正文、Cookie 或密钥。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
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

# 受取消保护的终结最多等多久。两处等待各自封顶：传播取消前等待终结、关闭时汇合在途终结；
# 两次上限之和仍留在 App 的 10 秒关闭总预算内（正常情况只是两笔 SQLite 写，毫秒级结束）。
_SETTLE_WAIT_TIMEOUT_SECONDS: float = 3.0


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


@dataclass(frozen=True)
class _Delivery:
    """`_deliver` 的产出：结果 + 终结所需信息。

    所有权模型（计划 §4.2 第 1 条）：`_deliver` 只负责 `POST` 与对账，
    **不做任何结算**；是否转正、要写哪条本地记录，全部交回 `send`，
    由它调用恰好一次终结操作。这样「是否已确认送达」只有一个判定点，
    任何出口都只能结算一次。

    - `record`：已确认送达后要写入本地去重表的消息（可能为 None）。
    - `charge`：True → 预留转正（`note_sent`）；False → 释放预留（`release`）。
    """

    result: SendResult
    record: ChatMessage | None
    charge: bool


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
        # 受取消保护的终结任务强引用（计划 §4.2 第 3 条）：`asyncio` 只对 task
        # 持弱引用，调用方在 `await` 处被取消而提前退出时，必须由这里保住引用，
        # 记账才不会被 GC 掉、关闭时也才有一个统一的等待点。
        self._settle_tasks: set[asyncio.Task[None]] = set()

    async def send(
        self,
        channel_id: str,
        text: str,
        reply_to: int | None,
        *,
        kind: str = "reply",
        actor_id: str | None = None,
        thread_root_id: int | None = None,
    ) -> SendResult:
        """脱敏、截断后发送；`kind` 与 `actor_id` 原样透传给 `quota`（三值见 §10）。

        `actor_id` 是触发这条消息的用户，只有 `kind="notice"` 用得上：
        主动通知的冷却按 (频道, 触发者) 计（D-18）。

        `thread_root_id` 是大区共享链的根：非空时随 `record_sent` 一起写入映射，
        让这条出站消息成为后续加入该链的锚点（D-20）。私聊恒为 None。
        """
        # 第 1 步：先脱敏，再在自然段边界截断。
        redacted = self._redactor.redact(text)
        content = truncate_at_paragraph(redacted, self._cfg.max_output_chars)[0]
        if not content.strip():
            return SendResult(False, None, "failed")

        # 第 2 步：配额预留；三种拒绝都不写 send_attempts。
        reservation = await self._quota.reserve(channel_id, kind, actor_id=actor_id)
        if not reservation.allowed:
            result = SendResult(False, None, self._deny_reason(reservation.decision))
            self._log(result, channel_id, kind)
            return result

        # 从这里开始持有预留：下面每一个出口都必须恰好 note_sent / release 一次。
        # `_deliver` 只做 POST 与对账，不结算；结算统一在本方法里做，保证唯一所有权。
        try:
            delivery = await self._deliver(channel_id, content, reply_to)
        except BaseException:
            # 未预期的异常（含取消）也不能让预留泄漏；release 同样受取消保护，
            # 否则取消恰好落在 quota 锁等待上时，这笔预留会永久占额。
            await self._run_settlement(self._release_reservation(channel_id, kind, actor_id))
            raise

        if delivery.charge:
            # 已确认送达：本地发送记录 + 配额转正收敛为一次受取消保护的终结操作。
            # 调用方此时被取消只会延迟取消传播，不会把这次送达改判成未发送。
            await self._run_settlement(
                self._commit_delivery(
                    delivery.record, channel_id, reply_to, kind, actor_id, thread_root_id
                )
            )
        else:
            await self._run_settlement(self._release_reservation(channel_id, kind, actor_id))
        self._log(delivery.result, channel_id, kind)
        return delivery.result

    async def wait_settled(self) -> None:
        """等待在途终结任务结束；有截止时间，超时只记事件并返回。

        关闭路径在 `Store` 关闭前调用（计划 §4.2 第 6 条）：正常取消已经由
        `_run_settlement` 在传播前等待到位，但重复取消有可能让调用方提前退出，
        留下仍在写库的终结任务。这里给它们一个统一的汇合点，避免 Store 关掉后
        它们还在访问数据库。不新增后台任务，等待的是本来就有生命周期的终结任务。

        **等待是有界的**（`_SETTLE_WAIT_TIMEOUT_SECONDS`）：若在途终结因存储阻塞等
        病态原因超时未结，本方法只记一条 `sender.settle_timeout` 事件便返回，不再
        等待 —— 这也意味着 `Store` 可能在那笔终结写库完成前就被关闭，丢掉那笔本地
        记账。终结任务本身不被取消、仍被强引用，超预算时它仍会自行跑完
        （预留的结清由 `quota.note_sent` 的取消保护兜住）。这样 `App.stop()` 的关闭
        总预算才不会被一个永久阻塞的写库拖穿。
        """
        pending = tuple(self._settle_tasks)
        if not pending:
            return
        # 用 asyncio.wait 而不是 gather：等到超时或本方法被取消都不会连带取消终结任务。
        _, still_running = await asyncio.wait(pending, timeout=_SETTLE_WAIT_TIMEOUT_SECONDS)
        if still_running:
            log_event(
                self._logger,
                logging.WARNING,
                "sender.settle_timeout",
                count=len(still_running),
            )

    # --- 内部实现 ---

    async def _run_settlement(self, operation: Awaitable[None]) -> None:
        """把终结操作放进独立 task 执行，并在取消到来时**有界地**等它完成再传播。

        只套 `asyncio.shield()` 不够：调用方仍可能在 `await` 处被取消而提前退出，
        把还没跑完的记账丢给事件循环。这里额外做两件事（计划 §4.2 第 3、4 条）：

        - 用 `self._settle_tasks` 保存强引用，任务不会被 GC，关闭时也能统一等待；
        - 取消时先等内层 task 结束再原样传播，但等待有界（`_await_settlement`）。
          重复取消只是更早地传播，内层 task 本身不在取消作用域内，仍会跑完，
          因此不会二次 POST 或双重记账。
        """
        task: asyncio.Task[None] = asyncio.ensure_future(operation)
        self._settle_tasks.add(task)
        task.add_done_callback(self._settle_tasks.discard)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 等结算完成（有界）；期间再次取消不会中断内层 task（它没有被 cancel）。
            await self._await_settlement(task)
            raise

    async def _await_settlement(self, task: asyncio.Task[None]) -> None:
        """在取消传播前有界等待受保护的终结任务；超时只记事件，绝不取消它。

        内层终结任务（写 Store / 拿 quota 锁）可能因磁盘满、SQLite 卡死或锁被长期
        持有而一直阻塞：此时若无限等待，取消就永远传播不出去，连 `App.stop()` 都会
        被拖住。这里用一次算好的截止时刻封顶，重复取消不重置预算；超时只记一条事件
        便返回，任务本身仍被强引用、仍会跑完 —— 「已确认送达不被取消改判」的底线
        因此不变，代价是关闭路径上超预算的那笔本地记账会丢失。
        """
        # 用一次算好的截止时刻，而不是每次循环重置超时：否则重复取消会把预算无限续期。
        # 仍然用 asyncio.wait 而不是 gather —— gather 被取消会连带取消内层任务。
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SETTLE_WAIT_TIMEOUT_SECONDS
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "sender.settle_timeout",
                    count=len(self._settle_tasks),
                )
                return
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                # 重复取消不重置预算，也不打断内层任务（它没有被 cancel）。
                continue
        if not task.cancelled():
            # 取出可能的异常，避免“Task exception was never retrieved”。
            task.exception()

    async def _commit_delivery(
        self,
        message: ChatMessage | None,
        channel_id: str,
        reply_to: int | None,
        kind: str,
        actor_id: str | None,
        thread_root_id: int | None,
    ) -> None:
        """一次受取消保护的终结：先落本地发送记录，再把预留转正。

        本地去重表写失败是既有降级（只记 error 日志），绝不能因此跳过转正 ——
        消息确实已经发出，`finally` 保证预留一定结清；把 dispatch 的意外异常
        （含被直接 cancel 内层任务时抛出的 `CancelledError`）也挡在转正之前。
        """
        try:
            await self._record_sent(message, channel_id, reply_to, thread_root_id)
        finally:
            await self._quota.note_sent(channel_id, reply_to, kind, actor_id=actor_id)

    async def _release_reservation(
        self, channel_id: str, kind: str, actor_id: str | None
    ) -> None:
        """释放一笔预留（异常/确定失败/取消路径的终结操作）。"""
        await self._quota.release(channel_id, kind, actor_id=actor_id)

    async def _deliver(
        self,
        channel_id: str,
        content: str,
        reply_to: int | None,
    ) -> _Delivery:
        """执行一次 POST 并处理结果；不做结算，只回报是否已确认送达。"""
        try:
            message = await self._post_once(channel_id, content, reply_to)
        except SiteError as exc:
            return await self._on_error(channel_id, content, reply_to, exc)

        return _Delivery(
            SendResult(True, message.id if message is not None else None, "delivered"),
            message,
            True,
        )

    async def _post_once(
        self, channel_id: str, content: str, reply_to: int | None
    ) -> ChatMessage | None:
        """确保会话有效后发一次消息；失败抛 `SiteError`。"""
        await self._client.ensure_session()
        return await self._client.post_message(channel_id, content, reply_to=reply_to)

    async def _on_error(
        self,
        channel_id: str,
        content: str,
        reply_to: int | None,
        exc: SiteError,
    ) -> _Delivery:
        """把确定性的站点错误映射为发送结果；status == 0 才进入对账。"""
        self._note_site_error(exc)
        if exc.status == 429:
            return _Delivery(SendResult(False, None, "backoff"), None, False)
        if exc.status == 403:
            # 细分 CSRF（我方配置错误）与权限/禁言（可能恢复），两者都不重试。
            return _Delivery(SendResult(False, None, _forbidden_reason(exc.message)), None, False)
        if exc.status == 400 and reply_to is not None:
            return _Delivery(SendResult(False, None, "reply_target_gone"), None, False)
        if exc.status == 0:
            return await self._reconcile(channel_id, content, reply_to)
        return _Delivery(SendResult(False, None, "failed"), None, False)

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
        self,
        channel_id: str,
        content: str,
        reply_to: int | None,
    ) -> _Delivery:
        """结果不确定时的对账：先查本地记录，再拉 `after=reply_to` 的最新一页。

        远端结果尚不确定时被取消会直接从本方法抛出，由 `send` 的 `BaseException`
        分支释放预留（计划 §4.2 第 7 条）：只在重发确实拿到成功响应后，
        才把这次 «已确认送达» 记进返回的 `_Delivery`，绝不为了结清预留盲目重投。
        """
        if reply_to is None:
            return _Delivery(SendResult(False, None, "failed"), None, False)

        existing = await self._store.find_sent_for_reply(channel_id, reply_to)
        if existing is not None:
            # 此前那次发送已经记过账（note_sent），本次预留直接释放。
            return _Delivery(SendResult(True, existing, "deduped"), None, False)

        try:
            messages = await self._client.fetch_messages(
                channel_id, limit=100, after=reply_to
            )
        except SiteError:
            # 对账查询本身失败：无法确认是否已发出，宁可不重发，避免双发。
            return _Delivery(SendResult(False, None, "failed"), None, False)

        self_user = self._client.self_user
        self_id = self_user.id if self_user is not None else None
        for message in messages:
            if message.author.id != self_id:
                continue
            if message.reply is None or message.reply.id != reply_to:
                continue
            # 命中的消息确实由本机器人发出，只是此前没记账：补记并转正预留。
            # 补记同样是尽力而为，写库失败不得影响预算记账（由终结操作保证顺序）。
            return _Delivery(SendResult(True, message.id, "deduped"), message, True)

        # 仍未命中：允许一次重发，仅一次。重发必须完整走第 5 步的错误副作用，
        # 尤其是 429 时的退避，否则会把站点的每分钟硬限撞穿。
        try:
            resent = await self._post_once(channel_id, content, reply_to)
        except SiteError as exc:
            self._note_site_error(exc)
            return _Delivery(SendResult(False, None, "failed"), None, False)
        return _Delivery(
            SendResult(True, resent.id if resent is not None else None, "delivered"),
            resent,
            True,
        )

    async def _record_sent(
        self,
        message: ChatMessage | None,
        channel_id: str,
        reply_to: int | None,
        thread_root_id: int | None,
    ) -> None:
        """有消息体时记录已发回复。

        消息体缺失、或去重表写库失败，都是可接受的降级：消息**确实已经发出**，
        绝不能因此让异常逃逸（那会让预留被 release、24 小时预算少记一笔）。
        失败只记一条不含正文的 error 日志，调用方继续走 note_sent 转正。
        """
        if message is None:
            return
        try:
            await self._store.record_sent(
                message.id, channel_id, reply_to, thread_root_id=thread_root_id
            )
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
