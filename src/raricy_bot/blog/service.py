"""定时发文服务（INTERFACES §53.11；设计 §6、§7.4、§9）。

三个后台任务，职责互不重叠：

- **扫描**：每秒一次，用 `planner.due_runs` 枚举到点的调度点并**原子领取**进 `blog_runs`
  （`queued`）。它**不等待**文章生成 —— 否则后续分钟的点会被一篇长文拖掉；
- **消费**：串行取出 `queued` 行并整篇跑完（概率 → 预算预检 → 取稿/生成 → 预校验 → 投递）。
  同一时刻只有一次生成/投递在跑，这是「一次只发一篇」的那道闸；
- **对账**：启动恢复后立刻一轮，之后每 5 分钟一轮；只读找正向凭证，永不 POST。

三条判决书级别的边界：

1. `PublishOutcome.post_id` 是**唯一**判据：为 `None` 表示这次没碰过投递表（`status` 是运行
   状态，原样落库）；非 `None` 表示投递行已经存在（`status` 是投递状态，本次执行终态
   **固定** `finished`）。两套取值域不混用。
2. `write()` 抛出的**任何**异常都只算「本次生成失败」（映射到稳定原因落 `failed`），
   **不得**因此停发文子域 —— 一次生成失败是运行期常态；只有账号变更
   （`blog.publisher.AccountChangedError`）才是停发条件，因为继续投递会把文章发到
   另一个账号名下。
3. `stop()` 只有 App 给的 10 秒预算：取消就是取消，绝不在这里等一个 180 秒的 `write()`
   自然结束。取消不吞成普通错误，也不清空 `inflight`。

日志只出 §53.13 的字段：正文、描述、任务提示词、搜索词与指纹一律不进日志。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import TIER_MUST
from ..core.worker import KIND_INPUT_TOO_LARGE, KIND_TIMEOUT, KIND_TRUNCATED, ModelError
from ..logging_setup import get_logger, log_event
from .codec import DraftError, prepare_draft
from .drafts import next_file_draft
from .models import (
    REASON_BUDGET_EXHAUSTED,
    REASON_EMPTY_QUEUE,
    REASON_GENERATION_FAILED,
    REASON_INPUT_TOO_LARGE,
    REASON_INTERRUPTED,
    REASON_MISFIRE,
    REASON_MODEL_ERROR,
    REASON_PROBABILITY_MISS,
    REASON_TIMEOUT,
    REASON_TRUNCATED,
    RUN_FAILED,
    RUN_FINISHED,
    RUN_SKIPPED,
    BlogRun,
    BlogScope,
    PreparedDraft,
    RunCandidate,
)
from .planner import SCAN_WINDOW_SECONDS, due_runs, select_run, utc8_day
from .publisher import AccountChangedError

_logger = get_logger("blog.service")

# 扫描节奏（设计 §6.1）：独立任务每秒看一次，领取与消费靠数据库里的 `queued` 行交接，
# 所以一篇长文不会挡住下一个调度点的领取。消费空闲时同样每秒探一次队列。
SCAN_INTERVAL_SECONDS: float = 1.0
CONSUME_IDLE_SECONDS: float = 1.0
# 只读对账的节奏（设计 §7.3）：启动恢复后立刻一轮，之后每 5 分钟一轮。
RECONCILE_INTERVAL_SECONDS: float = 300.0

# 分钟口径与 `planner._window_start` 一致：启动扫描的下界是「当前分钟开头退 1 秒」，
# 判断启动时有没有点被丢掉要按同一个口径算。
_SECONDS_PER_MINUTE: int = 60

# `write()` 的 `ModelError.kind` → 稳定原因的映射（§53.9 的表）。
# 表里没有的 kind（`invalid_completion` / `strict_unsupported` / 将来新增的）一律
# `REASON_MODEL_ERROR`：不认识的失败不能被当成「稍微不同的成功」。
_MODEL_FAILURE_REASONS: dict[str, str] = {
    KIND_TRUNCATED: REASON_TRUNCATED,
    KIND_INPUT_TOO_LARGE: REASON_INPUT_TOO_LARGE,
    KIND_TIMEOUT: REASON_TIMEOUT,
}

# 只进日志的原因：它们从不出现在 `blog_runs` / `blog_posts` 里，因此不需要是
# `blog/models.py` 的跨模块常量。
_REASON_ACCOUNT_CHANGED = "account_changed"
_REASON_SERVICE_FAILED = "service_failed"


class BlogService:
    """调度领取、串行消费与生命周期。

    构造参数一律关键字注入（§53.11）：本类**不创建** SiteClient、模型或 Registry，
    也**不持有**它们的关闭权限 —— 那些都由 App 装配与关闭。

    `config` 收**整个 `Config`**（与 `BlogPublisher` 同口径），任务表与日预算都从
    `config.blog` 读；`clock` 返回 epoch 秒，`sleep` 是可注入的计时器，`random` 是无参随机源
    （只在确认执行键不存在之后取一次）。
    """

    def __init__(
        self,
        *,
        config: Any,
        scope: BlogScope,
        store: Any,
        writer: Any,
        publisher: Any,
        redactor: Any,
        clock: Any,
        sleep: Any,
        random: Any,
    ) -> None:
        self._config = config
        self._scope = scope
        self._store = store
        self._writer = writer
        self._publisher = publisher
        self._redactor = redactor
        self._clock = clock
        self._sleep = sleep
        self._random = random

        self._blog_tasks = tuple(config.blog.tasks)
        self._tasks_by_name = {task.name: task for task in self._blog_tasks}

        self._background: list[asyncio.Task[None]] = []
        self._started = False
        # 账号变更或后台异常后置位：三个循环都会就此退出，持久记录一个不动。
        self._halted = False
        # 预算耗尽每个账号每个 UTC+8 日最多一条日志（设计 §9）。
        self._budget_logged_day: str | None = None
        self._logger = _logger

    # --- 生命周期 -----------------------------------------------------------

    async def start(self) -> None:
        """恢复上一进程的状态，然后启动扫描 / 消费 / 对账三个后台任务；幂等。

        恢复必须在任何领取之前：此刻本进程还没有 `queued` 行，任何非终态行都只可能属于
        已经死掉的旧进程（设计 §7.4）。
        """
        if self._started:
            return
        self._halted = False
        now = self._now()
        summary = await self._store.recover_blog_state(self._scope, now)
        if summary.unconfirmed:
            log_event(
                self._logger,
                logging.WARNING,
                "blog.recovered_unconfirmed",
                status="unconfirmed",
                count=summary.unconfirmed,
            )
        if summary.interrupted:
            log_event(
                self._logger,
                logging.INFO,
                "blog.recovered_interrupted",
                status="interrupted",
                count=summary.interrupted,
            )
        try:
            self._background = [
                asyncio.create_task(self._scan_loop(), name="blog-scan"),
                asyncio.create_task(self._consume_loop(), name="blog-consume"),
                asyncio.create_task(self._reconcile_loop(), name="blog-reconcile"),
            ]
        except BaseException:
            background, self._background = self._background, []
            for task in background:
                task.cancel()
            if background:
                await asyncio.gather(*background, return_exceptions=True)
            raise
        self._started = True
        log_event(self._logger, logging.INFO, "blog.service_started")

    async def stop(self) -> None:
        """停止领取，再取消并等待三个后台任务；幂等。

        **取消就是取消**：这里不等一个可能还要跑 180 秒的 `write()`（App 总关闭预算 10 秒）。
        取消不吞成普通错误，也不清空 `inflight` —— 中途被取消的投递行留给下次启动的
        `recover_blog_state` 降级。
        """
        if not self._started:
            return
        self._started = False
        self._halted = True
        background, self._background = self._background, []
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        log_event(self._logger, logging.INFO, "blog.service_stopped")

    # --- 扫描与领取 ---------------------------------------------------------

    async def _scan_loop(self) -> None:
        """每秒枚举一次到点的调度点并领取；不等待文章生成。"""
        scan_start = self._now()
        startup = True
        while not self._halted:
            now = self._now()
            try:
                await self._scan_once(scan_start=scan_start, now=now, startup=startup)
            except Exception as exc:  # noqa: BLE001 - 后台异常一律停本子域，不结束聊天/评论
                self._fail("blog.scan_failed", exc)
                return
            scan_start, startup = now, False
            await self._sleep(SCAN_INTERVAL_SECONDS)

    async def _scan_once(self, *, scan_start: float, now: float, startup: bool) -> None:
        if self._missed_a_schedule_point(scan_start=scan_start, now=now, startup=startup):
            # 更早的遗漏只记**一条**聚合日志，不逐个插入历史行（设计 §6.1、§53.8）：
            # 停机一整天后一次性补发几十篇文章，比漏发危险得多。
            log_event(
                self._logger,
                logging.INFO,
                "blog.misfire",
                reason=REASON_MISFIRE,
                day=utc8_day(now),
            )
        for candidate in due_runs(
            self._blog_tasks, scan_start=scan_start, now=now, startup=startup
        ):
            await self._claim_candidate(candidate, now)

    def _missed_a_schedule_point(
        self, *, scan_start: float, now: float, startup: bool
    ) -> bool:
        """本次扫描是否真的跳过了调度点；只有真跳过才记那条聚合日志（设计 §6.1）。

        运行期看扫描间隔有没有超出枚举窗口。启动那一支的下界是「当前分钟开头退 1 秒」
        （见 `planner._window_start`）：比它更早、又还在五分钟枚举窗口内的点才真的被丢掉，
        用一次非 startup 的 `due_runs` 问 planner 这段窗口里有没有点即可。
        每次启动都无条件记一条只会把运维训练成忽略这个事件。
        """
        if not startup:
            return now - scan_start > SCAN_WINDOW_SECONDS
        minute_start = float(int(now) // _SECONDS_PER_MINUTE * _SECONDS_PER_MINUTE)
        # 上界取 `minute_start - 1` 而不是 `minute_start`：正好排在当前分钟开头的那个点
        # 会被本次启动扫描领取，不算被跳过，否则每次「开机即到点」都会误报一条。
        return bool(
            due_runs(
                self._blog_tasks,
                scan_start=minute_start - SCAN_WINDOW_SECONDS,
                now=minute_start - 1.0,
                startup=False,
            )
        )

    async def _claim_candidate(self, candidate: RunCandidate, now: float) -> None:
        """确认执行键不存在之后再掷骰，然后原子领取（设计 §6.1）。"""
        existing = await self._store.get_blog_run(
            self._scope, candidate.task_name, candidate.scheduled_at
        )
        if existing is not None:
            # 已有执行键：绝不重新掷骰、绝不重新生成。
            return
        task = self._tasks_by_name.get(candidate.task_name)
        if task is None:
            return
        selected = 1 if select_run(task, random_value=float(self._random())) else 0
        run = await self._store.claim_blog_run(self._scope, candidate, selected, now)
        if run is None:
            # 唯一键兜底：重复扫描或时钟回拨都不会覆盖已经落库的随机决策。
            return
        log_event(
            self._logger,
            logging.INFO,
            "blog.run_claimed",
            task_name=run.task_name,
            run_id=run.id,
            status=run.status,
            day=utc8_day(now),
        )

    # --- 串行消费 -----------------------------------------------------------

    async def _consume_loop(self) -> None:
        """串行消费 `queued` 执行：同一时刻只有一次生成/投递在跑。"""
        while not self._halted:
            try:
                run = await self._store.take_blog_run(self._scope, self._now())
                if run is None:
                    await self._sleep(CONSUME_IDLE_SECONDS)
                    continue
                await self._execute_run(run)
            except AccountChangedError:
                # 账号已变：继续投递会把文章发到另一个账号名下，必须整域停下。
                self._halt("blog.account_changed", _REASON_ACCOUNT_CHANGED)
                return
            except Exception as exc:  # noqa: BLE001 - 见模块 docstring 第 2 条的反面
                # 只有 Store 级故障（含「写库失败必须停发」）会走到这里：单篇的生成失败
                # 在 `_execute_run` 内部就已经收尾成 `failed`，不会逃出来。
                self._fail("blog.consume_failed", exc)
                return

    async def _execute_run(self, run: BlogRun) -> None:
        """跑完一次执行；**无论哪条路径都写一个终态元数据**，不留 `running`。"""
        task = self._tasks_by_name.get(run.task_name)
        if task is None:
            # 任务已被改名或从配置里删掉：没有定义可以执行，按失败收尾（诊断记录保留）。
            log_event(
                self._logger,
                logging.WARNING,
                "blog.task_missing",
                task_name=run.task_name,
                run_id=run.id,
                status=RUN_FAILED,
                reason=REASON_INTERRUPTED,
            )
            await self._finish_run(run, RUN_FAILED, REASON_INTERRUPTED)
            return

        if run.selected == 0:
            # 概率未中：稿库与现写一样，什么都不读、什么都不调。
            log_event(
                self._logger,
                logging.INFO,
                "blog.run_skipped",
                task_name=run.task_name,
                run_id=run.id,
                status=RUN_SKIPPED,
                reason=REASON_PROBABILITY_MISS,
            )
            await self._finish_run(run, RUN_SKIPPED, REASON_PROBABILITY_MISS)
            return

        now = self._now()
        day = utc8_day(now)
        if (
            await self._store.blog_budget_used(self._scope, day)
            >= self._config.blog.max_posts_per_day
        ):
            # 预算预检只用来省下一次模型调用，**不能代替** Publisher 那侧的原子预留。
            self._log_budget_exhausted(day, run)
            await self._finish_run(run, RUN_SKIPPED, REASON_BUDGET_EXHAUSTED)
            return

        prepared = await self._prepare(run, task, day)
        if prepared is None:
            # 取稿/生成阶段的失败或空队列：已经收尾过了。
            return
        await self._publish(run, task, prepared)

    async def _prepare(self, run: BlogRun, task: Any, day: str) -> PreparedDraft | None:
        """取稿或现写并做一次预校验；失败即收尾并返回 None。"""
        if getattr(task, "drafts_dir", None):
            prepared = await next_file_draft(
                task, scope=self._scope, store=self._store, redactor=self._redactor, day=day
            )
            if prepared is None:
                if task.tier == TIER_MUST:
                    # `must` 的空稿库是需要人看见的运维事实；`maybe` 不到点就当没事。
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "blog.empty_queue",
                        task_name=task.name,
                        run_id=run.id,
                        status=RUN_SKIPPED,
                        reason=REASON_EMPTY_QUEUE,
                        day=day,
                    )
                else:
                    log_event(
                        self._logger,
                        logging.INFO,
                        "blog.run_skipped",
                        task_name=task.name,
                        run_id=run.id,
                        status=RUN_SKIPPED,
                        reason=REASON_EMPTY_QUEUE,
                    )
                await self._finish_run(run, RUN_SKIPPED, REASON_EMPTY_QUEUE)
                return None
            return prepared

        # 现写：一次生成 + 一次预校验。`write()` 的失败只算本次生成失败（§53.9 的表）。
        reason: str | None = None
        error_name: str | None = None
        try:
            draft = await self._writer.write(task)
            return prepare_draft(draft, redactor=self._redactor)
        except DraftError as exc:
            reason = exc.reason
        except ModelError as exc:
            reason = _MODEL_FAILURE_REASONS.get(exc.kind, REASON_MODEL_ERROR)
        except Exception as exc:  # noqa: BLE001 - 未分类异常也必须映射成稳定原因
            reason, error_name = REASON_GENERATION_FAILED, type(exc).__name__
        # 生成或预校验失败：立即告警并结束本次执行，同点不重试，**不停**发文子域。
        fields: dict[str, object] = {
            "task_name": task.name,
            "run_id": run.id,
            "status": RUN_FAILED,
            "reason": reason,
        }
        if error_name is not None:
            fields["error"] = error_name
        log_event(self._logger, logging.ERROR, "blog.generation_failed", **fields)
        await self._finish_run(run, RUN_FAILED, reason)
        return None

    async def _publish(self, run: BlogRun, task: Any, prepared: PreparedDraft) -> None:
        """投递并落运行终态；`post_id` 是两套取值域的唯一判据（§53.10）。"""
        outcome = await self._publisher.publish(run, task, prepared)
        if outcome.post_id is None:
            # 预留被拒或会话检查失败：这次**没有碰过投递表**，`status` 本身就是运行状态。
            status, reason = outcome.status, outcome.reason
        else:
            # 投递行已经存在：`status` 是投递状态，运行记录的终态固定 `finished`。
            # 两者各记各的 —— POST 之后运行终态不能替代投递状态。
            status, reason = RUN_FINISHED, outcome.reason
        fields: dict[str, object] = {
            "task_name": run.task_name,
            "run_id": run.id,
            "status": status,
            "reason": reason,
        }
        if outcome.post_id is not None:
            fields["post_id"] = outcome.post_id
        log_event(self._logger, logging.INFO, "blog.run_finished", **fields)
        await self._finish_run(run, status, reason)

    # --- 只读对账 -----------------------------------------------------------

    async def _reconcile_loop(self) -> None:
        """启动后立刻一轮，之后每 5 分钟一轮；只读，永不 POST。"""
        while not self._halted:
            try:
                await self._publisher.reconcile_once()
            except AccountChangedError:
                self._halt("blog.account_changed", _REASON_ACCOUNT_CHANGED)
                return
            except Exception as exc:  # noqa: BLE001 - 后台异常一律停本子域
                self._fail("blog.reconcile_failed", exc)
                return
            await self._sleep(RECONCILE_INTERVAL_SECONDS)

    # --- 内部工具 -----------------------------------------------------------

    async def _finish_run(self, run: BlogRun, status: str, reason: str) -> None:
        await self._store.finish_blog_run(self._scope, run.id, status, reason, self._now())

    def _log_budget_exhausted(self, day: str, run: BlogRun) -> None:
        """预算耗尽日志：每个账号每个 UTC+8 日最多一条（设计 §9）。

        去重只在内存里：跨重启可能多出一条，代价是一条重复日志，而不是多加一个持久字段。
        """
        if self._budget_logged_day == day:
            return
        self._budget_logged_day = day
        log_event(
            self._logger,
            logging.WARNING,
            "blog.budget_exhausted",
            task_name=run.task_name,
            run_id=run.id,
            status=RUN_SKIPPED,
            reason=REASON_BUDGET_EXHAUSTED,
            day=day,
        )

    def _halt(self, event: str, reason: str) -> None:
        """停掉整个发文子域：保留全部持久记录，不影响聊天/评论与既有健康判定。"""
        if self._halted:
            return
        self._halted = True
        log_event(self._logger, logging.ERROR, event, reason=reason)
        self._cancel_siblings()

    def _fail(self, event: str, exc: BaseException) -> None:
        """后台任务的未分类故障：记稳定原因与异常类型名后停本子域。"""
        if self._halted:
            return
        self._halted = True
        log_event(
            self._logger,
            logging.ERROR,
            event,
            reason=_REASON_SERVICE_FAILED,
            error=type(exc).__name__,
        )
        self._cancel_siblings()

    def _cancel_siblings(self) -> None:
        """把另外两个循环立刻叫停。

        只置 `self._halted` 不够：对账循环可能正停在一次 300 秒的间隔里，要到下一轮才会
        看见标志位。停发条件（账号变更）出现后，整个子域必须当场停 —— 一个**已经不属于
        本账号**的会话不应该再多跑一轮只读查询。
        """
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in self._background:
            if task is not current:
                task.cancel()

    def _now(self) -> float:
        """当前 epoch 秒；时钟由调用方注入，Store 的 SQL 里不读真实时钟。"""
        return float(self._clock())
