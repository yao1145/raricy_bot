"""投递与只读对账（INTERFACES §53.10；设计 §7.1–§7.3、D-109）。

顺序是判决书的一部分：探活 → 确认还是领取时那个账号 → 事务内预留额度并落 `inflight`
→ **只调一次** `publish_blog` → 按分类结果终结那一行。预留之前的任何失败都不 POST；
预留之后崩溃留下的 `inflight` 由下次启动的 `recover_blog_state` 降级，这里**不释放额度**。

两条铁律：

- **不确定不是「没发出去」**：超时、网络错误、5xx、非法信封、成功但缺合法 id、取消，
  一律保持占额并只做只读对账；空结果也不构成未发布证明，永远不授权第二次 POST（D-109）。
- **本模块不持有状态**：额度、状态机与恢复全在 Store 里，这里只按冻结合同调用它。

`PublishOutcome.status` 的取值分两类：投递行已经存在时取 `blog_posts` 的状态；
还没拿到投递行时（探活失败、预留被拒）取**运行级**状态（`RUN_FAILED` / `RUN_SKIPPED`），
因为这时没有任何投递状态可以描述。`post_id is None` 正是这个分岔的判据。
"""

from __future__ import annotations

import logging
from typing import Any

from ..logging_setup import get_logger, log_event
from ..site.blog_models import (
    OUTCOME_PUBLISHED,
    OUTCOME_RATE_LIMITED,
    OUTCOME_REJECTED,
    BlogPublishResult,
    BlogSearchItem,
)
from ..site.client import SiteError
from .codec import content_hash, utf16_length
from .models import (
    MAX_POST_ATTEMPTS,
    MAX_RECONCILE_ATTEMPTS,
    REASON_ABANDONED,
    REASON_RECONCILE_AMBIGUOUS,
    REASON_RECONCILE_EXHAUSTED,
    REASON_RECONCILE_INCOMPLETE,
    REASON_RECONCILE_MATCH,
    REASON_RECONCILE_NO_MATCH,
    REASON_PUBLISHED,
    REASON_RATE_LIMITED,
    REASON_REJECTED,
    REASON_UNCONFIRMED,
    RUN_FAILED,
    RUN_SKIPPED,
    SOURCE_FILE,
    SOURCE_GENERATED,
    STATUS_ABANDONED,
    STATUS_PUBLISHED,
    STATUS_RETRY_WAIT,
    STATUS_REJECTED,
    STATUS_UNCONFIRMED,
    BlogPost,
    BlogScope,
    PreparedDraft,
    PublishOutcome,
)

_logger = get_logger("blog.publisher")

# 只读对账的上限（§53.10 / 设计 §7.3）：每轮最多 10 条、单条最多 5 页、每页 50 条、
# 最多读取 10 篇候选正文。任何一个上限被顶到都意味着这一轮不完整，不确认任何东西。
RECONCILE_BATCH: int = 10
RECONCILE_MAX_PAGES: int = 5
RECONCILE_PAGE_SIZE: int = 50
RECONCILE_MAX_FETCHES: int = 10


class AccountChangedError(RuntimeError):
    """登录账号已不是领取时那个：本子域必须整体停止（旧记录只能用原账号恢复）。

    异常文本里没有用户名或用户 id：它可能进日志，而账号标识不是诊断所必需。
    """

    def __init__(self) -> None:
        super().__init__("blog account changed")


class BlogPublisher:
    """单篇投递与只读对账；不持有额度、不解析来源格式、不碰稿库文件。"""

    def __init__(
        self,
        *,
        config: Any,
        scope: BlogScope,
        store: Any,
        client: Any,
        clock: Any,
    ) -> None:
        self._config = config
        self._scope = scope
        self._store = store
        self._client = client
        self._clock = clock
        self._logger = _logger

    async def publish(self, run: Any, task: Any, prepared: PreparedDraft) -> PublishOutcome:
        """投递一篇已准备好的稿子；返回只带元数据的收尾结果。

        `prepared` 是**最终出站文本**：这里不再脱敏、不再截断、不重算指纹 ——
        落库标题、搜索标题、请求体与指纹必须是同一份结果。
        """
        # 1) 探活。失败就不 POST、不占额：本次执行按运行级失败收尾，下个调度点再说。
        try:
            user = await self._client.ensure_session()
        except SiteError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "blog.publish_no_session",
                run_id=run.id,
                status=RUN_FAILED,
                reason=REASON_UNCONFIRMED,
                error=type(exc).__name__,
            )
            return PublishOutcome(post_id=None, status=RUN_FAILED, reason=REASON_UNCONFIRMED)

        # 2) 仍是领取时那个账号吗？变了就停止本子域，绝不用另一个账号续发旧记录。
        if user.id != run.self_user_id or user.id != self._scope.self_user_id:
            log_event(
                self._logger,
                logging.ERROR,
                "blog.account_changed",
                run_id=run.id,
                status=RUN_FAILED,
                reason=REASON_UNCONFIRMED,
            )
            raise AccountChangedError()

        category_id = getattr(task, "category_id", None)
        source_kind = SOURCE_FILE if getattr(task, "drafts_dir", None) else SOURCE_GENERATED

        # 3) 一个事务里复查状态、创建或复用投递行、`attempts += 1` 并预留当天额度。
        #    这里抛异常（含 SQLite 提交失败）就让本次执行失败收尾 —— 绝不带着一个
        #    没落盘的「预留」去 POST，那样连对账的凭证都没有。
        reservation = await self._store.reserve_blog_post(
            self._scope,
            run.id,
            prepared.title,
            prepared.content_hash,
            prepared.hash_version,
            source_kind,
            category_id,
            self._config.blog.max_posts_per_day,
            self._now(),
        )
        if not reservation.allowed:
            # 预算用尽、已有同指纹的发布/待确认行、或已到重试上限：什么都不做。
            return PublishOutcome(post_id=None, status=RUN_SKIPPED, reason=reservation.reason)
        post = reservation.post
        if post is None:
            # Store 合同：允许时必须给出可投递的行。拿不到行就宁可不发 —— 裸发等于
            # 发一篇没有本地记录的文，之后既不能对账也不能计费。
            raise RuntimeError("blog reservation allowed without a post row")

        # 4) 只此一次 POST。`CancelledError` 继续向上传播，预留行留在 `inflight`，
        #    由下次启动的恢复降级为 `unconfirmed`（不释放额度、不当作未发送）。
        result = await self._client.publish_blog(
            title=prepared.title,
            description=prepared.description,
            content=prepared.content,
            category_id=category_id,
        )
        status, site_blog_id, reason = self._settle(result, post=post, source_kind=source_kind)

        # 5) 按分类结果终结那一行。写库失败**不**在内存里当作未发送（见 `_finalize`）。
        await self._finalize(post, status=status, site_blog_id=site_blog_id, reason=reason)
        fields: dict[str, object] = {
            "post_id": post.id,
            "run_id": run.id,
            "status": status,
            "reason": reason,
            # 只出标题的 UTF-16 长度：正文、描述、指纹一律不进日志（§53.13）。
            "chars": utf16_length(prepared.title),
            "task_name": getattr(task, "name", ""),
        }
        if site_blog_id is not None:
            fields["blog_id"] = site_blog_id
        # `rejected` 是**永久终结**该指纹的结果（同指纹不再自动重投）：运维当天就该看到，
        # 所以升到 WARNING；成功与其他分类仍是 INFO。
        level = logging.WARNING if status == STATUS_REJECTED else logging.INFO
        log_event(self._logger, level, "blog.publish", **fields)
        return PublishOutcome(post_id=post.id, status=status, reason=reason)

    async def reconcile_once(self) -> None:
        """只读对账一轮：找正向凭证，永不 POST（设计 §7.3、D-109）。

        预算耗尽**不**阻止它：这里不产生任何新投递。命中的那一行只是把本来就已经
        占着的额度转正，不重复计费。
        """
        try:
            user = await self._client.ensure_session()
        except SiteError as exc:
            # 会话探活失败时本轮什么也不做，而且**不记查询尝试** —— 一次登录/网络故障
            # 不该消耗 12 次查询预算，那会凭外部故障把行推向永久待确认。
            log_event(
                self._logger,
                logging.WARNING,
                "blog.reconcile_no_session",
                reason=REASON_RECONCILE_INCOMPLETE,
                error=type(exc).__name__,
            )
            return

        # 账号换人：与 `publish` 同一套判断、同一条日志。用另一个账号继续跑只读查询，
        # 同样违背「账号掉出核心用户即整体失效」的语义（D-106、§53.11），必须整体停下。
        # 这条路径在取行之前就返回，因此**不**记任何查询尝试：一轮根本没查过。
        if user.id != self._scope.self_user_id:
            log_event(
                self._logger,
                logging.ERROR,
                "blog.account_changed",
                status=RUN_FAILED,
                reason=REASON_UNCONFIRMED,
            )
            raise AccountChangedError()

        posts = await self._store.blog_posts_to_reconcile(
            self._scope, self._now(), limit=RECONCILE_BATCH
        )
        for post in posts:
            if post.reconcile_attempts >= MAX_RECONCILE_ATTEMPTS:
                # Store 本应把查满上限的行排除在外；这里再挡一道，省一次站方查询。
                continue
            matched: str | None = None
            reason = REASON_RECONCILE_INCOMPLETE
            try:
                matched, reason = await self._reconcile_post(post)
            except SiteError as exc:
                # 查询失败 = 不完整：保持 `unconfirmed` 与占额，只记一次查询尝试。
                log_event(
                    self._logger,
                    logging.WARNING,
                    "blog.reconcile_incomplete",
                    post_id=post.id,
                    status=STATUS_UNCONFIRMED,
                    reason=REASON_RECONCILE_INCOMPLETE,
                    error=type(exc).__name__,
                )
                matched, reason = None, REASON_RECONCILE_INCOMPLETE

            # 每条记录每轮**只记一次**查询尝试：分页翻多少页都算同一次。
            updated = await self._store.note_blog_reconcile(
                self._scope, post.id, reason, self._now()
            )
            if matched is not None:
                await self._store.finalize_blog_post(
                    self._scope,
                    post.id,
                    STATUS_PUBLISHED,
                    matched,
                    REASON_RECONCILE_MATCH,
                    self._now(),
                )
                log_event(
                    self._logger,
                    logging.INFO,
                    "blog.reconcile_confirmed",
                    post_id=post.id,
                    status=STATUS_PUBLISHED,
                    reason=REASON_RECONCILE_MATCH,
                    blog_id=matched,
                )
            elif updated.reconcile_attempts >= MAX_RECONCILE_ATTEMPTS:
                # 到达 12 次仍未确认：告警并停止自动查询，但**保持占额**（D-109）。
                log_event(
                    self._logger,
                    logging.WARNING,
                    "blog.reconcile_exhausted",
                    post_id=post.id,
                    status=updated.status,
                    reason=REASON_RECONCILE_EXHAUSTED,
                    count=MAX_RECONCILE_ATTEMPTS,
                )

    async def _reconcile_post(self, post: BlogPost) -> tuple[str | None, str]:
        """查一条待确认记录；返回 `(命中的站方 id 或 None, 稳定原因)`。

        只有**完整走完搜索**且恰有一个精确匹配（作者、标题、正文指纹三者全中）才返回
        一个 id；其余情况一律返回 None，由调用方保持 `unconfirmed`。
        """
        # 搜索用落库的**脱敏标题**：它就是出站时用的那一份（§10）。
        seen: dict[str, BlogSearchItem] = {}
        page = 1
        while page <= RECONCILE_MAX_PAGES:
            result = await self._client.search_blog_titles(
                post.title, page=page, per_page=RECONCILE_PAGE_SIZE
            )
            for item in result.items:
                # 候选按**去重后的 blog id** 计数：分页随并发更新移动时同一篇文可能
                # 在两页里各出现一次，不去重就会假造出「多个精确匹配」。
                seen.setdefault(item.blog_id, item)
            if not result.has_next:
                break
            page += 1
        else:
            # 页数顶到上限还没翻完：这一轮不完整，什么都不能确认。
            return None, REASON_RECONCILE_INCOMPLETE

        candidates = [
            item
            for item in seen.values()
            if item.author_id == self._scope.self_user_id and item.title == post.title
        ]
        if not candidates:
            return None, REASON_RECONCILE_NO_MATCH
        if len(candidates) > RECONCILE_MAX_FETCHES:
            # 候选超限：读不完候选就等于不知道有没有第二个精确匹配。
            return None, REASON_RECONCILE_INCOMPLETE

        matches: list[str] = []
        for item in candidates:
            context = await self._client.fetch_blog_context(item.blog_id)
            if context is None or context.content is None:
                # 取不回正文就无法算指纹：不完整，不是「没发出去」。
                return None, REASON_RECONCILE_INCOMPLETE
            if context.title != post.title:
                # 两次 GET 之间被编辑过：这一篇已经不是我们发出去的那一篇，不参与匹配。
                continue
            # 指纹用**原始远端标题与正文**重算：远端返回不是待发布草稿，不经过 Redactor、
            # 不再截断（§7.3）。指纹算法只有一份（`blog/codec.content_hash`）。
            if content_hash(context.title, context.content) == post.content_hash:
                matches.append(item.blog_id)

        if len(matches) == 1:
            return matches[0], REASON_RECONCILE_MATCH
        if matches:
            return None, REASON_RECONCILE_AMBIGUOUS
        return None, REASON_RECONCILE_NO_MATCH

    def _settle(
        self, result: BlogPublishResult, *, post: BlogPost, source_kind: str
    ) -> tuple[str, str | None, str]:
        """把站点分类结果映射成投递行的 `(status, site_blog_id, reason)`。

        429 是**唯一**允许有界重试的确定结果，而且只对稿库来源：现写稿正文只在内存里，
        明天重试等于换一篇新文（§7.2、§14）。重试上限按**含本次**的累计 POST 次数算。
        """
        if result.outcome == OUTCOME_PUBLISHED and result.blog_id:
            return STATUS_PUBLISHED, result.blog_id, REASON_PUBLISHED
        if result.outcome == OUTCOME_REJECTED:
            return STATUS_REJECTED, None, REASON_REJECTED
        if result.outcome == OUTCOME_RATE_LIMITED:
            if source_kind == SOURCE_FILE and post.attempts < MAX_POST_ATTEMPTS:
                return STATUS_RETRY_WAIT, None, REASON_RATE_LIMITED
            return STATUS_ABANDONED, None, REASON_ABANDONED
        # 其余（含「published 但没有合法 id」这种自相矛盾的分类）一律不确定：
        # 不确定行的代价是多占一份额度，而当成确定失败的代价是重复发布。
        return STATUS_UNCONFIRMED, None, REASON_UNCONFIRMED

    async def _finalize(
        self, post: BlogPost, *, status: str, site_blog_id: str | None, reason: str
    ) -> None:
        """按结果终结投递行；写库失败时告警后继续抛出，让服务停发。

        「POST 已经成功、本地写库失败」不能当作未发送：站方那篇文章可能已经存在，
        重投就是重复发布。持久行此时仍是 `inflight`（事务回滚），由下次启动恢复成
        `unconfirmed` 并保持占额，交人工核实（D-107、D-109）。
        `CancelledError` 不是 `Exception` 的子类，取消不会被这里吞掉。
        """
        try:
            await self._store.finalize_blog_post(
                self._scope, post.id, status, site_blog_id, reason, self._now()
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "blog.publish_finalize_failed",
                post_id=post.id,
                status=status,
                reason=reason,
                error=type(exc).__name__,
            )
            raise

    def _now(self) -> float:
        """当前 epoch 秒；时钟由调用方注入，Store 的 SQL 里不读真实时钟。"""
        return float(self._clock())
