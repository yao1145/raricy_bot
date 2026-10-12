"""用户画像的编排器：门禁、状态机、幂等、撰写与持久化（设计 §2、§3、§6、§9）。

职责边界与记忆模块同款，三层各有各的答案：

- **Controller（本模块）**决定「谁可以做什么、现在处于哪一步、失败给用户看什么」；
- **MemoryService**决定「文件里最终是什么」：原子写、版本 CAS、投递复核、公开快照；
- **PersonaWriter / PersonaSources**只负责读样本、提问题、写短文与探测来源。

## 状态机

一次流程只有**一个**待办工作流，阶段取值逐字来自设计 §9：

```
collecting ──(问卷已送达)──> awaiting_answers ──(5 题齐)──> draft_pending_delivery
     ^                                                              │
     │                                              (草稿已送达)     v
     └── 采集失败可重试                                    awaiting_review
                                                              │      │
                                                       (确认) │      │ (修改一次)
                                                              v      v
                                                          committed  revising
                                                                      │
                                                        (v2 已送达)    v
                                                              awaiting_final_review
```

`public` 与 `clear` 各自是一个 `kind`，复用 `awaiting_review` 与带编号确认（设计 §9）。

## 三条不变量

1. **确认只接受已展示的当前草稿**（设计 §9）：`_confirm` 先比对工作流里的 `draft_id`、
   再要求 `delivered` 为真，最后才把草稿编号交给 `MemoryService.persona_confirm`；
   服务侧在写锁内再复核一遍。旧编号、未送达、过期与关闭后的确认全部拒绝。
2. **修改额度只在成功受理后固定**（设计 §3.6）：第一次 `revise` 受理即置 `revision_used`，
   模型失败时**保留同一份意见**，只允许用同样的输入重试；换一份意见会被拒。
3. **原文不落盘**：博客正文与私聊正文只在一次命令里存活；落进 Markdown 的只有观察摘要、
   5 个问题、用户自己的回答、用户自己的修改意见与待审短文（设计 §8）。

用户可见固定文案统一维护在 `texts.py`；
固定短句一律取自 `texts.PERSONA_*`。日志只记稳定状态、阶段、种类与计数。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from .. import texts
from ..config import PersonaConfig
from ..logging_setup import get_logger, log_event
from ..memory.access import MemoryAccessPolicy
from ..memory.models import user_storage_key
from .commands import (
    DELIVERY_DRAFT,
    DELIVERY_NOTICE,
    DELIVERY_PROMPT,
    KIND_CLEAR,
    KIND_CREATE,
    KIND_EDIT,
    KIND_PUBLIC,
    MAX_ANSWER_INDEX,
    MAX_INPUT_CHARS,
    SKIP_ANSWER,
    STAGE_AWAITING_ANSWERS,
    STAGE_AWAITING_FINAL_REVIEW,
    STAGE_AWAITING_REVIEW,
    STAGE_DRAFT_PENDING_DELIVERY,
    STAGE_REVISING,
    USAGE_COMMAND,
    PersonaCommandRequest,
    PersonaCommandResult,
    PersonaSessionHint,
    format_draft_id,
    parse_draft_id,
    parse_numbered_answers,
)

__all__ = ["PersonaController"]

_logger = get_logger("persona")

# 私有能力（命令、编辑、公开预览、自动更新）的频道口径：只有 DM（设计 §2）。
_DM_KIND: str = "dm"

# `collecting` 阶段（设计 §9 的状态图）：只在本模块内部使用，命令模块不认识它。
_STAGE_COLLECTING: str = "collecting"

# 取消流程时落的终态（设计 §9 的九个阶段之一）。
_STAGE_CANCELLED: str = "cancelled"

# 幂等键前缀：与记忆命令同形的 `<来源>:<message_id>:<步骤>`。
_COMMAND_SOURCE: str = "cmd"
_UPDATE_SOURCE: str = "update"

# 送达标记、采集、草稿、修改各自的操作后缀：同一条消息可能触发两步写入，
# 键必须不同，否则第二次会被第一次的 `operations` 命中而静默跳过（与记忆模块同款）。
_DELIVERED_SUFFIX: str = "delivered"
_COLLECT_SUFFIX: str = "collect"
_DRAFT_SUFFIX: str = "draft"
_REVISE_SUFFIX: str = "revise"
_LEGACY_REVOKE_SUFFIX: str = "legacy"
_METADATA_SUFFIX: str = "meta"

# 服务层返回的稳定状态串（contract §17-§29）；未知取值一律按 unavailable 处理。
_SERVICE_OK: str = "ok"
_SERVICE_NOOP: str = "noop"
_SERVICE_CONFLICT: str = "conflict"
_SERVICE_UNAVAILABLE: str = "unavailable"
_SERVICE_SECRET: str = "secret_detected"
_SERVICE_INVALID: str = "invalid_proposal"
_SERVICE_FORBIDDEN: str = "forbidden"
_SERVICE_STATUSES: frozenset[str] = frozenset(
    {
        _SERVICE_OK,
        _SERVICE_NOOP,
        _SERVICE_CONFLICT,
        _SERVICE_UNAVAILABLE,
        _SERVICE_SECRET,
        _SERVICE_INVALID,
        _SERVICE_FORBIDDEN,
    }
)

# 本模块自有的结果状态（`PersonaCommandResult.status`）：只用于日志与测试。
STATUS_OK: str = "ok"
STATUS_NOOP: str = "noop"
STATUS_USAGE: str = "usage"
STATUS_DENIED: str = "denied"
STATUS_UNAVAILABLE: str = "unavailable"
STATUS_CONFLICT: str = "conflict"
STATUS_REJECTED: str = "rejected"
STATUS_REVISION_USED: str = "revision_used"
STATUS_PENDING_REVOKE: str = "pending_revoke"
STATUS_DEFERRED: str = "deferred"
STATUS_DISABLED: str = "disabled"

# 需要写进日志的 INFO 结果；其余（重复展示、用法提示）降到 DEBUG，避免刷屏。
_ACTIONABLE_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_OK,
        STATUS_CONFLICT,
        STATUS_UNAVAILABLE,
        STATUS_REJECTED,
        STATUS_REVISION_USED,
        STATUS_PENDING_REVOKE,
    }
)

# 缓存的流程快照容量（纯内存，无 I/O）：超过就丢最旧的一条，不是长久索引。
_HINT_CACHE_SIZE: int = 256

# 画像文件里保留的观察摘要条数上限（与存储层的 `MAX_OBSERVATIONS` 一致）。
_MAX_OBSERVATIONS: int = 64

# **一次建档**能写进工作流的摘要条数上限（存储层 `MAX_SOURCE_SUMMARIES`）：超过就明确失败，
# 绝不静默丢弃尾部（设计 §4.3）。
_MAX_SOURCE_SUMMARIES: int = 32

# 自动更新保留观察摘要的时间窗口（天，设计 §6 的「近期兴趣 30 天观察窗口」）。
_RECENT_OBSERVATION_DAYS: int = 30

# 草稿展示里列出的来源标签上限（只用于「主要推测依据」，不是证据索引）。
_MAX_EVIDENCE_LABELS: int = 8

# `origin` 里表示「用户修订」的那一个（`persona/models.py` 的 `FEATURE_ORIGINS`）。
_ORIGIN_USER_REVISION: str = "user_revision"

# 观察摘要首部里的日期元数据（`[observation:1|refs=…|dates=2026-10-01,2026-10-02]`）。
_OBSERVATION_DATES = re.compile(r"\|dates=(?P<dates>[0-9,\-]{1,200})")

# 每日更新预算的时区偏移（秒）：站点与用户按 UTC+8 记「一天」（设计 §6 的「每人每天」）。
_UTC8_OFFSET_SECONDS: float = 8 * 3600.0

# 写入日志的失败原因 token 只允许这种形状。
_REASON_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,31}")

# --- 用户可见文案（父级会集中到 texts.py；此处只做占位与插值） -------------------------


# C 侧写入 `last_change` 的两个短语：与存储层自己的短语同形（都是给用户看的中文短句）。
# 提交、关闭与清空由存储层自己写，C 只在「重新开启」与「自动更新」两处补上。

# 采集器返回的**稳定提示 token** → 用户可见短句（`persona/sources.py` 的 `WARNING_*`）。
# 带 `=N` 计数后缀的形态由 `_warning_labels()` 处理；未知 token 只在形状安全时原样列出，
# 否则回一句笼统提示 —— 展示层不回显任意文本。
_WARNING_TOKEN = re.compile(r"(?P<code>[a-z][a-z0-9_]{0,31})(?:=(?P<count>[0-9]{1,6}))?\Z")

# `last_change` 展示的长度上限：超过它只可能是被手工改过的文件，按无记录处理。
_MAX_CHANGE_CHARS: int = 40

class PersonaWriterFailure(Exception):
    """撰写器失败的本地载体：只携带稳定 reason token（不携带异常正文）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PersonaController:
    """画像命令的编排器（设计 §8）；依赖由装配层注入，构造时不做任何 I/O。"""

    def __init__(
        self,
        *,
        service: Any,
        writer: Any,
        sources: Any,
        access: MemoryAccessPolicy,
        config: PersonaConfig,
        max_display_chars: int = 4000,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.service = service
        self.writer = writer
        self.sources = sources
        self.access = access
        self.config = config
        # 单条展示的字符上限（装配层传 `behavior.max_output_chars`）：超限的草稿不发出去，
        # 保留待办等下一次重试 —— 截断会把「确认 / 修改 / 取消」的说明一起切掉。
        self._max_display_chars = max(1, int(max_display_chars))
        self._now: Callable[[], float] = now or time.time
        self._hints: dict[str, PersonaSessionHint] = {}

    # --- 对外入口 ---------------------------------------------------------

    async def execute_command(
        self, request: PersonaCommandRequest
    ) -> PersonaCommandResult:
        """执行一条画像命令；任何用户可预期的失败都映射成稳定状态，不抛异常。"""
        try:
            result = await self._run(request)
        except asyncio.CancelledError:
            # 取消原样传播：上层靠它收尾（与记忆命令同款）。
            raise
        except Exception:
            # 软故障（D-60）：画像链路上的意外不允许变成「用户什么都没收到」。
            # 异常字符串可能带路径或正文，因此只记稳定状态。
            result = self._result(STATUS_UNAVAILABLE, texts.PERSONA_UNAVAILABLE_TEXT)
        self._log_command(request, result)
        return result

    async def mark_delivered(
        self,
        request: PersonaCommandRequest,
        *,
        kind: str,
        draft_id: str | None,
        message_id: int | None,
    ) -> PersonaCommandResult | None:
        """送达回调（设计 §3.1、§9）：记下真实送达的消息 id，必要时继续流程。

        返回非空表示「还有一条要发的文案」：目前只有一种情况 —— 使用说明确实送达之后
        才开始采集（说明发送失败时不开始采集）。
        """
        try:
            return await self._mark_delivered(
                request, kind=kind, draft_id=draft_id, message_id=message_id
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                _logger,
                logging.WARNING,
                "persona.delivery",
                status=STATUS_UNAVAILABLE,
                reason="internal",
            )
            return None

    async def warm_session(self, user_id: str) -> None:
        """为本人私聊恢复工作流快照并清理过期提示。"""
        await self._load_workflow(user_id)

    def cached_hint(self, user_id: str | None) -> PersonaSessionHint | None:
        """当前用户流程的同步快照（**无 I/O**）；没有缓存时返回 `None`。

        Router 用它判定「这条普通消息是不是访谈、修改或确认输入」。缓存由本控制器在自己
        每次读取/写入流程时刷新；冷启动后第一次访问之前是空的（那时普通消息走聊天路径，
        用户发送一次 `/persona` 即可重新装填）。

        存储层若额外提供同步的流程快照（`persona_workflow_cached`，见 C-notes.md 第 2.4 条），
        这里会拿它兜底，重启后的第一条回复消息也能被正确归属；没有那个方法时行为不变。
        """
        if not user_id:
            return None
        hint = self._hints.get(user_id)
        if hint is not None:
            return hint
        snapshot = getattr(self.service, "persona_workflow_cached", None)
        if not callable(snapshot):
            return None
        try:
            workflow = snapshot(user_id)
        except Exception:
            return None
        if not isinstance(workflow, dict) or not workflow.get("workflow_id"):
            return None
        self._remember(user_id, workflow)
        return self._hints.get(user_id)

    def invalidate(self, user_id: str | None) -> None:
        """丢掉某个用户的流程快照（流程结束、关闭或取消时调用）。"""
        if user_id:
            self._hints.pop(user_id, None)

    async def maybe_update(
        self, user_id: str, evidence: tuple[str, ...], *, message_id: int
    ) -> str:
        """送达后自动更新（设计 §6）：节流、**压缩新证据**、合并与版本 CAS 都在这里。

        `evidence` 是装配层缓冲区里的**带标签原始正文**（每条形如
        `[dm:<消息 id>|<UTC 日期>] 用户原文`），控制器先把它压成带日期的观察摘要，
        再与画像里保留的摘要合并，最后交给 `PersonaWriter.update`。

        压缩在这里而不在装配层，是为了让「原始对话」只活到这一次调用：落盘的只有摘要
        （设计 §6 末段、§8 末段），装配层不持有任何可持久化的正文。

        返回稳定状态：只有 `deferred` 表示「这批证据还留着，下一次再判断」。
        """
        try:
            return await self._maybe_update(
                user_id, tuple(evidence), message_id=message_id
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                _logger,
                logging.WARNING,
                "persona.update",
                status=STATUS_UNAVAILABLE,
                reason="internal",
            )
            return STATUS_UNAVAILABLE

    def control_received(self, user_id: str, control: str) -> None:
        """授权控制命令一到达就执行的部分（**同步、无 I/O**，由 Router 回调）。

        设计 §6 要求 `off`、清空、开始手动编辑与取消「立即使旧令牌失效」：如果等到命令
        排到队首再失效，用户明确要求停止之后在途的模型结果仍可能落盘。这里只做同步的
        两件事 —— 推进存储层的画像代次（令牌作废）与丢掉流程快照（后续普通消息不再被
        当成访谈输入）；真正的持久化仍由排队的命令完成。
        """
        self.invalidate(user_id)
        self._invalidate_tokens(user_id)

    # --- 命令分派 ---------------------------------------------------------

    async def _run(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        name = request.command.name
        if name == USAGE_COMMAND:
            return self._result(STATUS_USAGE, texts.PERSONA_USAGE_TEXT)
        if not request.user_id:
            # 拿不到稳定身份：没有可归属的私有文件，按不可用处理（与记忆命令同款）。
            return self._result(STATUS_DENIED, texts.PERSONA_UNAVAILABLE_TEXT)
        if request.channel_kind != _DM_KIND:
            return self._result(STATUS_DENIED, texts.PERSONA_DM_ONLY_TEXT)
        if not self.access.permits_commands(request.user_id, request.channel_kind):
            # 门禁不过与功能不可用同一句固定文案：不透露部署的接入名单。
            return self._result(STATUS_DENIED, texts.PERSONA_UNAVAILABLE_TEXT)
        if name == "view":
            return await self._view(request)
        if name == "on":
            return await self._on(request)
        if name == "off":
            return await self._off(request)
        if name == "edit":
            return await self._edit(request)
        if name == "answer":
            return await self._answer(request)
        if name == "answer_batch":
            # 隐式输入专用（Router 从「回复问卷消息」或编号回答推断而来）：解析器永远
            # 不会产出这个名字，用户手写 `/persona answer_batch` 只会拿到用法提示。
            return await self._answer_batch(request)
        if name == "revise":
            return await self._revise(request)
        if name == "confirm":
            return await self._confirm(request)
        if name == "cancel":
            return await self._cancel(request)
        if name == "public":
            return await self._public(request)
        if name == "private":
            return await self._private(request)
        if name == "clear":
            return await self._clear(request)
        # 解析器不认识的名字（将来新增子命令而这里忘了接线）走用法，绝不静默成功。
        return self._result(STATUS_USAGE, texts.PERSONA_USAGE_TEXT)

    # --- 查看 / 继续流程 ---------------------------------------------------

    async def _view(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona`：有待办流程就展示进度或草稿，否则显示画像，无画像则开始建档。"""
        user_id = request.user_id
        workflow = await self._load_workflow(user_id)
        if workflow is not None:
            return self._resume(workflow, request)
        persona = await self.service.persona_get(user_id)
        if not self._has_paragraph(persona):
            return await self._begin_create(request)
        return self._result(STATUS_NOOP, self._status_text(user_id, persona))

    def _resume(
        self, workflow: dict, request: PersonaCommandRequest
    ) -> PersonaCommandResult:
        """把已有流程重新展示给用户（纯渲染，不写盘、不调模型）。"""
        kind = self._kind(workflow)
        stage = self._stage(workflow)
        if stage == _STAGE_COLLECTING:
            # 使用说明已送达但采集还没跑完（发送失败或采集软故障）：重发说明，送达后再采集。
            return self._result(
                STATUS_NOOP, texts.PERSONA_DISCLOSURE_TEXT, delivery_kind=DELIVERY_NOTICE
            )
        if stage == STAGE_AWAITING_ANSWERS:
            return self._result(
                STATUS_NOOP,
                self._questions_text(workflow),
                draft_id=str(workflow.get("draft_id") or "") or None,
                delivery_kind=DELIVERY_PROMPT,
            )
        if stage == STAGE_REVISING:
            if kind == KIND_EDIT:
                return self._result(
                    STATUS_NOOP,
                    self._edit_prompt(workflow),
                    delivery_kind=DELIVERY_PROMPT,
                )
            # 建档的修改没有生成成功：意见被冻结，只能重试同一条（设计 §3.6）。
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_REVISION_FAILED_TEXT)
        if kind == KIND_PUBLIC:
            return self._result(
                STATUS_NOOP,
                self._public_preview(workflow),
                draft_id=str(workflow.get("draft_id")),
                delivery_kind=DELIVERY_PROMPT,
            )
        if kind == KIND_CLEAR:
            return self._result(
                STATUS_NOOP,
                self._clear_preview(workflow),
                draft_id=str(workflow.get("draft_id")),
                delivery_kind=DELIVERY_PROMPT,
            )
        draft = self._draft_text(workflow)
        if draft is None:
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        return draft

    def _status_text(self, user_id: str, persona: dict) -> str:
        """`/persona` 的画像视图：短文、开关、更新时间、公开状态（关闭状态也照样展示）。"""
        if persona.get("pending_unpublish"):
            public = texts.PERSONA_TEXT_PUBLIC_PENDING_TEXT
        elif self._is_published(user_id, persona):
            # 公开快照的版本号来自存储层的同步只读取值（拿不到时只报「已发布」）；
            # 设计 §7 的「私有 v8，公开 v5」因此可以显示，而不只有两个状态。
            revision = self._public_revision(user_id)
            public = (
                texts.PERSONA_TEXT_PUBLIC_PUBLISHED_VERSION_TEXT.format(revision=revision)
                if revision is not None
                else texts.PERSONA_TEXT_PUBLIC_PUBLISHED_TEXT
            )
        else:
            public = texts.PERSONA_TEXT_PUBLIC_ABSENT_TEXT
        header = texts.PERSONA_TEXT_STATUS_HEADER_TEXT.format(
            revision=int(persona.get("revision") or 0),
            state=texts.PERSONA_TEXT_STATE_ON_TEXT if persona.get("enabled") else texts.PERSONA_TEXT_STATE_OFF_TEXT,
        )
        meta = texts.PERSONA_TEXT_STATUS_META_TEXT.format(
            updated_at=str(persona.get("updated_at") or ""),
            last_change=self._last_change_label(persona.get("last_change")),
            public=public,
            auto=(
                texts.PERSONA_TEXT_AUTO_ON_TEXT
                if persona.get("enabled") and self.config.auto_update_available
                else texts.PERSONA_TEXT_AUTO_OFF_TEXT
            ),
        )
        return (
            f"{header}\n\n{persona.get('paragraph') or ''}\n\n"
            f"{meta}\n{texts.PERSONA_TEXT_STATUS_COMMANDS_TEXT}"
        )

    # --- 建档 -------------------------------------------------------------

    async def _begin_create(
        self, request: PersonaCommandRequest
    ) -> PersonaCommandResult:
        """开始首次建档：先发使用说明，说明送达后才采集（设计 §3.1）。"""
        workflow = self._new_workflow(
            KIND_CREATE,
            request,
            stage=_STAGE_COLLECTING,
            enabled_on_commit=True,
        )
        status = await self._save_workflow(
            request.user_id,
            workflow,
            request,
            "create",
        )
        if status not in (_SERVICE_OK, _SERVICE_NOOP):
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        self._remember(request.user_id, workflow)
        return self._result(
            STATUS_OK, texts.PERSONA_DISCLOSURE_TEXT, delivery_kind=DELIVERY_NOTICE
        )

    async def _collect_and_ask(
        self, request: PersonaCommandRequest, workflow: dict
    ) -> PersonaCommandResult | None:
        """采集样本 → 阅读 → 5 个问题（设计 §3.2/§3.3）；失败保留流程可重试。"""
        user_id = request.user_id
        budget = self._budget(workflow)
        try:
            bundle = await self.sources.collect(
                user_id,
                request.username,
                request.channel_id,
                int(workflow.get("watermark") or request.message_id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 身份或权限校验失败、站点异常都在这里收口：流程留在 collecting，
            # 用户再发 `/persona` 可以重试；原始样本一个字节都没有落盘。
            log_event(
                _logger,
                logging.WARNING,
                "persona.collect",
                status=STATUS_UNAVAILABLE,
                reason=_reason_token(getattr(exc, "reason", None)),
            )
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        source_texts = tuple(getattr(bundle, "texts", ()) or ())
        warnings = tuple(str(item) for item in (getattr(bundle, "warnings", ()) or ()))
        try:
            observations = await self._writer_call(
                self.writer.read_sources, source_texts, budget=budget
            )
            questions = await self._writer_call(
                self.writer.questions, tuple(observations), budget=budget
            )
            five = self._exactly_five(questions)
        except PersonaWriterFailure as failure:
            # 模型调用失败也算进额度：撰写器在每次调用前自增，失败不再重来（设计 §4.3）。
            await self._persist_counters(request, workflow, budget)
            log_event(
                _logger,
                logging.WARNING,
                "persona.collect",
                status=STATUS_UNAVAILABLE,
                reason=failure.reason,
            )
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        summaries = tuple(str(item) for item in observations)
        if len(summaries) > _MAX_SOURCE_SUMMARIES:
            # 摘要条数超过存储上界（32）：按「输入过大」明确失败，**不静默截断**，
            # 也不开始访谈 —— 丢掉尾部就等于宣称读过其实没读过的资料（设计 §4.3）。
            await self._persist_counters(request, workflow, budget)
            log_event(
                _logger,
                logging.WARNING,
                "persona.collect",
                status=STATUS_UNAVAILABLE,
                reason="summaries_too_many",
                count=len(summaries),
            )
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_COLLECT_TOO_LARGE_TEXT)
        workflow.update(
            {
                "stage": STAGE_AWAITING_ANSWERS,
                "blog_count": int(getattr(bundle, "blog_count", 0) or 0),
                "message_count": int(getattr(bundle, "message_count", 0) or 0),
                "source_summaries": list(summaries),
                "model_calls_used": int(budget["used"]),
                "questions": list(five),
                "questions_message_id": None,
            }
        )
        status = await self._save_workflow(
            user_id,
            workflow,
            request,
            _COLLECT_SUFFIX,
        )
        if status not in (_SERVICE_OK, _SERVICE_NOOP):
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        self._remember(user_id, workflow)
        return self._result(
            STATUS_OK,
            self._questions_text(workflow, warnings=warnings),
            delivery_kind=DELIVERY_PROMPT,
        )

    # --- 访谈问答 ---------------------------------------------------------

    async def _answer(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona answer <编号> <回答>`：逐题记录，5 题齐了才生成草稿。"""
        user_id = request.user_id
        workflow = await self._load_workflow(user_id)
        if workflow is None or self._kind(workflow) != KIND_CREATE:
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_WORKFLOW_TEXT)
        if self._stage(workflow) != STAGE_AWAITING_ANSWERS:
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_ANSWERS_CLOSED_TEXT)
        command = request.command
        if command.number is None or not command.argument:
            return self._result(STATUS_USAGE, texts.PERSONA_USAGE_TEXT)
        answers = dict(workflow.get("answers") or {})
        # `skip` 是「明确跳过」（设计 §2）：大小写与空白归一，落盘形态只有一种。
        answered = command.argument.strip()
        if self._too_long(answered):
            # 超限即拒绝：截断后的「已记录」与用户真正写下的内容不一致。
            return self._result(STATUS_REJECTED, texts.PERSONA_INPUT_TOO_LONG_TEXT)
        if answered.lower() == SKIP_ANSWER:
            answered = SKIP_ANSWER
        answers[str(command.number)] = answered
        workflow["answers"] = answers
        if not self._answers_complete(answers):
            status = await self._save_workflow(
                user_id,
                workflow,
                request,
                "answer",
            )
            if status not in (_SERVICE_OK, _SERVICE_NOOP):
                return self._result(STATUS_CONFLICT, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
            self._remember(user_id, workflow)
            return self._result(
                STATUS_OK,
                texts.PERSONA_TEXT_ANSWER_PROGRESS_TEXT.format(
                    number=command.number,
                    done=len(answers),
                    total=MAX_ANSWER_INDEX,
                )
                + "\n"
                + texts.PERSONA_TEXT_ANSWER_INSTRUCTIONS_TEXT,
            )
        return await self._generate_draft(request, workflow)

    async def _answer_batch(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """一次回答多题（回复问卷消息，或正文里带 `1. …` 之类的编号）。

        有编号就按编号落位；没有编号的整段文本按「下一道还没答的题」处理 —— 用户在问卷
        消息下直接回一段话，最自然的解释就是回答同一个问题（设计 §2 的隐式输入）。
        """
        user_id = request.user_id
        text = request.command.argument or ""
        workflow = await self._load_workflow(user_id)
        if workflow is None or self._kind(workflow) != KIND_CREATE:
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_WORKFLOW_TEXT)
        if self._stage(workflow) != STAGE_AWAITING_ANSWERS:
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_ANSWERS_CLOSED_TEXT)
        pairs = parse_numbered_answers(text) or self._next_answer(workflow, text)
        if not pairs:
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_ANSWER_INSTRUCTIONS_TEXT)
        # 逐条检查长度并**整条拒绝**：回复问卷同样不许截断（设计 §2 的隐式输入与显式
        # `/persona answer` 走同一条规则），确保「已记录」就是用户写下的全部内容。
        if any(self._too_long(value.strip()) for _, value in pairs):
            return self._result(STATUS_REJECTED, texts.PERSONA_INPUT_TOO_LONG_TEXT)
        answers = dict(workflow.get("answers") or {})
        for number, value in pairs:
            recorded = value.strip()
            if not recorded:
                continue
            answers[str(number)] = (
                SKIP_ANSWER if recorded.lower() == SKIP_ANSWER else recorded
            )
        workflow["answers"] = answers
        if not self._answers_complete(answers):
            status = await self._save_workflow(
                user_id,
                workflow,
                request,
                "answer_batch",
            )
            if status not in (_SERVICE_OK, _SERVICE_NOOP):
                return self._result(STATUS_CONFLICT, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
            self._remember(user_id, workflow)
            return self._result(
                STATUS_OK,
                texts.PERSONA_TEXT_ANSWER_PROGRESS_TEXT.format(
                    number=pairs[0][0],
                    done=len(answers),
                    total=MAX_ANSWER_INDEX,
                )
                + "\n"
                + texts.PERSONA_TEXT_ANSWER_INSTRUCTIONS_TEXT,
            )
        return await self._generate_draft(request, workflow)

    @staticmethod
    def _next_answer(
        workflow: dict, text: str
    ) -> tuple[tuple[int, str], ...]:
        """没有编号时的落位：整段文本算作**下一道还没答的题**的回答（不截断）。"""
        value = text.strip()
        if not value:
            return ()
        answers = workflow.get("answers") or {}
        for index in range(1, MAX_ANSWER_INDEX + 1):
            if str(index) not in answers:
                return ((index, value),)
        return ()

    @staticmethod
    def _too_long(value: str) -> bool:
        """单条回答 / 修改意见是否超过 `MAX_INPUT_CHARS`（超限即拒绝，不截断）。"""
        return len(value) > MAX_INPUT_CHARS

    @staticmethod
    def _answers_complete(answers: dict[str, str]) -> bool:
        """5 题是否都已给出回答或明确跳过（设计 §1：不增加第 6 个问题）。"""
        return all(
            str(index) in answers and bool(answers[str(index)])
            for index in range(1, MAX_ANSWER_INDEX + 1)
        )

    # --- 草稿生成与一次修改 -----------------------------------------------

    async def _generate_draft(
        self,
        request: PersonaCommandRequest,
        workflow: dict,
        *,
        existing: dict | None = None,
        correction: str = "",
    ) -> PersonaCommandResult:
        """生成并保存一份草稿（建档首版 / 一次修改 / 手动编辑），返回展示文案。"""
        user_id = request.user_id
        budget = self._budget(workflow)
        try:
            proposal = await self._writer_call(
                self.writer.draft,
                await self._draft_sources(user_id, workflow),
                list(workflow.get("questions") or ()),
                dict(workflow.get("answers") or {}),
                existing=existing,
                correction=correction,
                budget=budget,
            )
            paragraph, features = self._proposal(proposal)
        except PersonaWriterFailure as failure:
            # 失败也要写回已消耗的额度（设计 §4.3），并保留当前阶段：
            # 修改中的流程停在 revising，只允许用同一份意见重试。
            await self._persist_counters(request, workflow, budget)
            log_event(
                _logger,
                logging.WARNING,
                "persona.draft",
                status=STATUS_UNAVAILABLE,
                reason=failure.reason,
            )
            if self._stage(workflow) == STAGE_REVISING:
                return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_REVISION_FAILED_TEXT)
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        workflow["model_calls_used"] = int(budget["used"])
        workflow["paragraph"] = paragraph
        # 特征的时间戳与「用户手动改动即锁定」都由宿主补齐（设计 §5、§6）：
        # 写入工作流之前就定型，用户看到的这一稿与落盘的这一份逐字一致。
        workflow["features"] = self._stamp_features(
            features, base=existing, corrected=bool(correction)
        )
        version = int(workflow.get("draft_version") or 0) + 1
        workflow["draft_version"] = version
        workflow["draft_id"] = format_draft_id(self._draft_number(workflow, version))
        workflow["stage"] = STAGE_DRAFT_PENDING_DELIVERY
        workflow["delivered"] = False
        workflow["delivered_message_id"] = None
        status = await self._save_workflow(
            user_id,
            workflow,
            request,
            _REVISE_SUFFIX if correction else _DRAFT_SUFFIX,
        )
        if status not in (_SERVICE_OK, _SERVICE_NOOP):
            return self._result(STATUS_CONFLICT, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        self._remember(user_id, workflow)
        draft = self._draft_text(workflow)
        if draft is None:
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        return draft

    async def _draft_sources(self, user_id: str, workflow: dict) -> tuple[str, ...]:
        """草稿这一步的输入文本：建档用本次阅读摘要，手动编辑用画像里存的观察摘要。"""
        summaries = tuple(workflow.get("source_summaries") or ())
        if summaries or self._kind(workflow) != KIND_EDIT:
            return summaries
        persona = await self.service.persona_get(user_id)
        if persona is None:
            return ()
        return tuple(str(item) for item in (persona.get("observations") or ()))[
            :_MAX_OBSERVATIONS
        ]

    async def _revise(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona revise <修改意见>`：每次流程只受理一次，失败保留同一份意见。"""
        user_id = request.user_id
        text = request.command.argument or ""
        if not text:
            return self._result(STATUS_USAGE, texts.PERSONA_USAGE_TEXT)
        if self._too_long(text):
            # 修改意见超限：在冻结这份输入之前就拒绝，额度不受影响（用户没提交成功）。
            return self._result(STATUS_REJECTED, texts.PERSONA_INPUT_TOO_LONG_TEXT)
        workflow = await self._load_workflow(user_id)
        if workflow is None:
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_WORKFLOW_TEXT)
        kind = self._kind(workflow)
        if kind not in (KIND_CREATE, KIND_EDIT):
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_REVISION_REJECTED_TEXT)
        stage = self._stage(workflow)
        retrying = (
            stage == STAGE_REVISING
            and workflow.get("revision_used")
            and workflow.get("correction") == text
        )
        if not retrying:
            if stage == STAGE_AWAITING_FINAL_REVIEW:
                # v2 只允许确认或取消（设计 §3.6）。
                return self._result(
                    STATUS_REVISION_USED, texts.PERSONA_REVISION_USED_TEXT
                )
            if kind == KIND_EDIT:
                # 编辑流程刚展示当前画像（`revising`），等一次修改意见；改过一次之后拒绝。
                if stage != STAGE_REVISING or workflow.get("revision_used"):
                    return self._result(
                        STATUS_REVISION_USED, texts.PERSONA_REVISION_USED_TEXT
                    )
            else:
                if stage == STAGE_REVISING:
                    # 上一次修改没有生成成功：额度已经被那一份意见占住，只允许原样重试。
                    return self._result(
                        STATUS_REVISION_USED, texts.PERSONA_REVISION_USED_TEXT
                    )
                if stage != STAGE_AWAITING_REVIEW:
                    return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_REVISION_REJECTED_TEXT)
                if not workflow.get("delivered"):
                    return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_REJECTED_UNDELIVERED_TEXT)
                if workflow.get("revision_used"):
                    return self._result(
                        STATUS_REVISION_USED, texts.PERSONA_REVISION_USED_TEXT
                    )
            workflow["revision_used"] = True
            workflow["correction"] = text
            workflow["stage"] = STAGE_REVISING
            status = await self._save_workflow(
                user_id,
                workflow,
                request,
                "revise_accept",
            )
            if status not in (_SERVICE_OK, _SERVICE_NOOP):
                return self._result(STATUS_CONFLICT, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
            self._remember(user_id, workflow)
        existing = self._revision_base(workflow)
        if existing is None:
            return await self._begin_create(request)
        return await self._generate_draft(
            request, workflow, existing=existing, correction=text
        )

    @staticmethod
    def _revision_base(workflow: dict) -> dict | None:
        """修改的基线是**用户已经看到的**那一版短文（建档用草稿、编辑用当前画像）。"""
        paragraph = str(workflow.get("paragraph") or "")
        if not paragraph:
            return None
        return {
            "paragraph": paragraph,
            "features": list(workflow.get("features") or []),
        }

    # --- 确认 -------------------------------------------------------------

    async def _confirm(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona confirm <草稿编号>`：只接受已送达的当前草稿（设计 §9）。"""
        user_id = request.user_id
        workflow = await self.service.persona_workflow(user_id)
        self._remember(user_id, workflow)
        if workflow is None:
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_WORKFLOW_TEXT)
        number = request.command.number
        if parse_draft_id(str(workflow.get("draft_id") or "")) != number:
            # 旧编号、过期编号或别人猜的编号：一律拒绝，不做任何写入。
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_REJECTED_DRAFT_TEXT)
        kind = self._kind(workflow)
        stage = self._stage(workflow)
        if kind in (KIND_CREATE, KIND_EDIT):
            if stage not in (STAGE_AWAITING_REVIEW, STAGE_AWAITING_FINAL_REVIEW):
                return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_WORKFLOW_TEXT)
            if not workflow.get("delivered"):
                return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_REJECTED_UNDELIVERED_TEXT)
        elif kind in (KIND_PUBLIC, KIND_CLEAR):
            if stage != STAGE_AWAITING_REVIEW:
                return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_WORKFLOW_TEXT)
        else:
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_WORKFLOW_TEXT)
        summaries = tuple(str(item) for item in (workflow.get("source_summaries") or ()))
        if kind == KIND_CREATE:
            blocked = await self._revoke_legacy_public(user_id, request)
            if blocked is not None:
                return blocked
        outcome = _status_text(
            await self.service.persona_confirm(
                user_id,
                str(workflow.get("draft_id")),
                operation_id=self._operation(request, "confirm"),
            )
        )
        return await self._after_confirm(request, kind, outcome, summaries)

    async def _revoke_legacy_public(
        self, user_id: str, request: PersonaCommandRequest
    ) -> PersonaCommandResult | None:
        """首次切换前撤回旧公开记忆（设计 §11.4）；撤回不了就不提交切换。"""
        persona = await self.service.persona_get(user_id)
        if persona is not None and persona.get("migrated"):
            return None
        try:
            result = await self.service.unpublish_all(
                user_id,
                operation_id=self._operation(request, _LEGACY_REVOKE_SUFFIX),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                _logger,
                logging.WARNING,
                "persona.migrate",
                status=STATUS_UNAVAILABLE,
                reason="legacy_revoke",
            )
            return self._result(
                STATUS_PENDING_REVOKE, texts.PERSONA_PENDING_REVOKE_TEXT
            )
        status = _status_text(result)
        if status in (_SERVICE_OK, _SERVICE_NOOP, _SERVICE_FORBIDDEN):
            # `forbidden` = 旧记忆子系统未启用（`memory.enabled=false`）：那时根本不存在
            # 旧公开投影，切换没有要撤回的东西，照常继续。
            return None
        log_event(
            _logger,
            logging.WARNING,
            "persona.migrate",
            status=status,
            reason="legacy_revoke",
        )
        return self._result(STATUS_PENDING_REVOKE, texts.PERSONA_PENDING_REVOKE_TEXT)

    async def _after_confirm(
        self,
        request: PersonaCommandRequest,
        kind: str,
        status: str,
        summaries: tuple[str, ...],
    ) -> PersonaCommandResult:
        """把确认结果映射成用户文案，并补写 C 侧元数据（观察摘要、用户名、变更说明）。"""
        user_id = request.user_id
        if status in (_SERVICE_OK, _SERVICE_NOOP):
            self.invalidate(user_id)
            await self._persist_confirm_metadata(request, kind, summaries)
            return self._result(STATUS_OK, self._commit_text(kind))
        if status == _SERVICE_CONFLICT:
            return self._result(STATUS_CONFLICT, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        if status in (_SERVICE_UNAVAILABLE, _SERVICE_SECRET):
            if kind in (KIND_CREATE, KIND_CLEAR):
                # 提交没到位时公开副本可能仍在：如实回「待完成」，不报全部成功（设计 §7）。
                return self._result(
                    STATUS_PENDING_REVOKE, texts.PERSONA_PENDING_REVOKE_TEXT
                )
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_UNAVAILABLE_TEXT)
        # `invalid_proposal` 与其它未知状态：草稿已失效或形状不对，重新走一次流程即可。
        return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_REJECTED_DRAFT_TEXT)

    @staticmethod
    def _commit_text(kind: str) -> str:
        if kind == KIND_PUBLIC:
            return texts.PERSONA_TEXT_COMMIT_PUBLIC_TEXT
        if kind == KIND_CLEAR:
            return texts.PERSONA_TEXT_COMMIT_CLEAR_TEXT
        if kind == KIND_EDIT:
            return texts.PERSONA_TEXT_COMMIT_EDIT_TEXT
        return texts.PERSONA_TEXT_COMMIT_CREATE_TEXT

    async def _persist_confirm_metadata(
        self, request: PersonaCommandRequest, kind: str, summaries: tuple[str, ...]
    ) -> None:
        """提交成功后的 C 侧元数据：用户名（公开索引键）、观察摘要与最近变更说明。

        这一步失败只记一条稳定日志：正文与开关已经由服务原子写入，元数据是补充信息，
        不能让用户看到一次「其实成功了」的失败。
        """
        try:
            persona = await self.service.persona_get(request.user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                _logger,
                logging.WARNING,
                "persona.commit",
                status=STATUS_UNAVAILABLE,
                reason="metadata_read",
            )
            return
        if persona is None:
            return
        fields: dict[str, Any] = {
            # 公开快照的索引键：站点用户名只在这条命令里可得，必须随提交一起落盘
            # （`/persona public` 用它写 `owner_username`，设计 §7）。
            "username": request.username,
        }
        if kind == KIND_CLEAR:
            # 清空保留无正文的幂等元数据：观察摘要属于授权内容，一并丢弃。
            fields["observations"] = []
        elif kind == KIND_CREATE and summaries:
            # 「控制器持久化摘要，后续回答与重启后仍能用已知信息」（设计 §4.3）。
            fields["observations"] = list(summaries)[:_MAX_OBSERVATIONS]
        # `last_change` 不在这里写：提交、编辑与清空由存储层自己写（那几处它才有权威取值）。
        await self._save_persona_fields(
            request.user_id, persona, fields, request, _METADATA_SUFFIX
        )

    # --- 取消 / 开关 / 公开 -------------------------------------------------

    async def _cancel(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona cancel`：清掉待办正文并**把流程终结成墓碑**，保留已生效画像（设计 §9）。

        清空正文用的是部分保存（而不是 `value=None`）：`None` 只清正文、保留原阶段，用户
        取消之后流程仍会被当成「待办」继续展示；这里同时把阶段落成 `cancelled`，流程才真正
        结束（`workflow_is_empty` 为真，后续 `/persona` 回到显示画像）。
        """
        user_id = request.user_id
        workflow = await self._load_workflow(user_id)
        if workflow is None:
            return self._result(STATUS_NOOP, texts.PERSONA_TEXT_NO_WORKFLOW_TEXT)
        cleared = {
            "workflow_id": str(workflow.get("workflow_id") or ""),
            # 版本号必须带上：部分保存的 CAS 用的就是它（存储层写入时内部推进）。
            "revision": int(workflow.get("revision") or 0),
            "stage": _STAGE_CANCELLED,
            "source_summaries": [],
            "questions": [],
            "answers": {},
            "paragraph": "",
            "features": [],
            "correction": "",
            "draft_id": "",
            "delivered": False,
            "delivered_message_id": None,
            "questions_message_id": None,
        }
        status = await self._save_workflow(user_id, cleared, request, "cancel")
        self._invalidate_tokens(user_id)
        if status in (_SERVICE_OK, _SERVICE_NOOP):
            self.invalidate(user_id)
            return self._result(STATUS_OK, texts.PERSONA_TEXT_CANCEL_TEXT)
        return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)

    async def _on(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona on`：有画像就开启使用与自动更新；没有就转入建档（设计 §2）。"""
        user_id = request.user_id
        workflow = await self._load_workflow(user_id)
        if workflow is not None:
            return self._resume(workflow, request)
        persona = await self.service.persona_get(user_id)
        if not self._has_paragraph(persona):
            return await self._begin_create(request)
        if persona.get("pending_unpublish"):
            # 待撤回未完成时阻止重新开启（设计 §7）。
            return self._result(
                STATUS_PENDING_REVOKE, texts.PERSONA_PENDING_REVOKE_TEXT
            )
        if persona.get("enabled"):
            return self._result(STATUS_NOOP, texts.PERSONA_TEXT_ON_ALREADY_TEXT)
        status = await self._save_persona_fields(
            user_id,
            persona,
            {"enabled": True, "last_change": texts.PERSONA_TEXT_CHANGE_ON_TEXT},
            request,
            "on",
        )
        if status in (_SERVICE_OK, _SERVICE_NOOP):
            return self._result(STATUS_OK, texts.PERSONA_TEXT_ON_TEXT_TEXT)
        if status == _SERVICE_CONFLICT:
            return self._result(STATUS_CONFLICT, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        return self._result(STATUS_UNAVAILABLE, texts.PERSONA_UNAVAILABLE_TEXT)

    async def _off(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona off`：停用 + 撤回公开；撤回失败如实回「待完成」（设计 §7）。"""
        user_id = request.user_id
        status = _status_text(
            await self.service.persona_off(
                user_id, operation_id=self._operation(request, "off")
            )
        )
        self._invalidate_tokens(user_id)
        self.invalidate(user_id)
        if status in (_SERVICE_OK, _SERVICE_NOOP):
            return self._result(STATUS_OK, texts.PERSONA_OFF_TEXT)
        if status in (_SERVICE_UNAVAILABLE, _SERVICE_CONFLICT, _SERVICE_SECRET):
            return self._result(
                STATUS_PENDING_REVOKE, texts.PERSONA_PENDING_REVOKE_TEXT
            )
        return self._result(STATUS_UNAVAILABLE, texts.PERSONA_UNAVAILABLE_TEXT)

    async def _edit(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona edit`：展示当前画像并开始一次手动修改（不隐式重新开启）。"""
        user_id = request.user_id
        workflow = await self._load_workflow(user_id)
        if workflow is not None:
            return self._resume(workflow, request)
        persona = await self.service.persona_get(user_id)
        if not self._has_paragraph(persona):
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_PROFILE_EDIT_TEXT)
        if persona.get("pending_unpublish"):
            return self._result(
                STATUS_PENDING_REVOKE, texts.PERSONA_PENDING_REVOKE_TEXT
            )
        self._invalidate_tokens(user_id)
        workflow = self._new_workflow(
            KIND_EDIT,
            request,
            stage=STAGE_REVISING,
            enabled_on_commit=bool(persona.get("enabled")),
            base_revision=int(persona.get("revision") or 0),
        )
        workflow["paragraph"] = persona.get("paragraph") or ""
        workflow["features"] = list(persona.get("features") or [])
        status = await self._save_workflow(
            user_id,
            workflow,
            request,
            "edit",
        )
        if status not in (_SERVICE_OK, _SERVICE_NOOP):
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        self._remember(user_id, workflow)
        return self._result(
            STATUS_OK, self._edit_prompt(workflow), delivery_kind=DELIVERY_PROMPT
        )

    async def _public(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona public`：展示**当前私有短文**的独立副本并等待确认（设计 §7）。"""
        user_id = request.user_id
        workflow = await self._load_workflow(user_id)
        if workflow is not None:
            return self._resume(workflow, request)
        persona = await self.service.persona_get(user_id)
        if not self._has_paragraph(persona):
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_PROFILE_TEXT)
        if persona.get("pending_unpublish"):
            return self._result(
                STATUS_PENDING_REVOKE, texts.PERSONA_PENDING_REVOKE_TEXT
            )
        workflow = self._new_workflow(
            KIND_PUBLIC,
            request,
            stage=STAGE_AWAITING_REVIEW,
            enabled_on_commit=bool(persona.get("enabled")),
            base_revision=int(persona.get("revision") or 0),
        )
        # 公开是**独立快照**：预览里展示的就是即将发布的字节，之后的自动更新与手动编辑
        # 都不会改变它（设计 §7）。
        workflow["paragraph"] = persona.get("paragraph") or ""
        workflow["features"] = list(persona.get("features") or [])
        workflow["draft_version"] = int(persona.get("revision") or 0)
        workflow["draft_id"] = format_draft_id(self._draft_number(workflow, 1))
        status = await self._save_workflow(
            user_id,
            workflow,
            request,
            "public",
        )
        if status not in (_SERVICE_OK, _SERVICE_NOOP):
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        self._remember(user_id, workflow)
        return self._result(
            STATUS_OK,
            self._public_preview(workflow),
            draft_id=str(workflow.get("draft_id")),
            delivery_kind=DELIVERY_PROMPT,
        )

    async def _private(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona private`：撤回公开副本，继续私有使用（设计 §7）。"""
        user_id = request.user_id
        status = _status_text(
            await self.service.persona_unpublish(
                user_id, operation_id=self._operation(request, "private")
            )
        )
        if status in (_SERVICE_OK, _SERVICE_NOOP):
            return self._result(STATUS_OK, texts.PERSONA_TEXT_PRIVATE_REVOKED_TEXT)
        if status in (_SERVICE_UNAVAILABLE, _SERVICE_CONFLICT, _SERVICE_SECRET):
            # 私有使用不受影响，因此这里不用「已停止使用与更新」那句（那是 `off` 的口径）。
            return self._result(STATUS_PENDING_REVOKE, texts.PERSONA_TEXT_PRIVATE_PENDING_TEXT)
        return self._result(STATUS_UNAVAILABLE, texts.PERSONA_UNAVAILABLE_TEXT)

    async def _clear(self, request: PersonaCommandRequest) -> PersonaCommandResult:
        """`/persona clear`：先预览，再由带编号确认执行删除（设计 §2、§7）。"""
        user_id = request.user_id
        workflow = await self._load_workflow(user_id)
        if workflow is not None:
            return self._resume(workflow, request)
        persona = await self.service.persona_get(user_id)
        if persona is None:
            return self._result(STATUS_REJECTED, texts.PERSONA_TEXT_NO_PROFILE_TEXT)
        workflow = self._new_workflow(
            KIND_CLEAR,
            request,
            stage=STAGE_AWAITING_REVIEW,
            enabled_on_commit=False,
            base_revision=int(persona.get("revision") or 0),
        )
        workflow["draft_id"] = format_draft_id(self._draft_number(workflow, 1))
        status = await self._save_workflow(
            user_id,
            workflow,
            request,
            "clear",
        )
        if status not in (_SERVICE_OK, _SERVICE_NOOP):
            return self._result(STATUS_UNAVAILABLE, texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT)
        self._remember(user_id, workflow)
        return self._result(
            STATUS_OK,
            self._clear_preview(workflow),
            draft_id=str(workflow.get("draft_id")),
            delivery_kind=DELIVERY_PROMPT,
        )

    # --- 送达回调 ---------------------------------------------------------

    async def _mark_delivered(
        self,
        request: PersonaCommandRequest,
        *,
        kind: str,
        draft_id: str | None,
        message_id: int | None,
    ) -> PersonaCommandResult | None:
        user_id = request.user_id
        workflow = await self.service.persona_workflow(user_id)
        self._remember(user_id, workflow)
        if workflow is None:
            return None
        if kind == DELIVERY_NOTICE:
            # 使用说明确实送达了：现在才采集（设计 §3.1）。
            if self._stage(workflow) == _STAGE_COLLECTING:
                return await self._collect_and_ask(request, workflow)
            return None
        if kind == DELIVERY_PROMPT:
            if message_id is None:
                return None
            workflow["questions_message_id"] = int(message_id)
            if self._kind(workflow) in (KIND_PUBLIC, KIND_CLEAR):
                # 公开与清空的预览同样要求「已送达才能确认」（服务侧复核 `delivered`）。
                workflow["delivered"] = True
                workflow["delivered_message_id"] = int(message_id)
            await self._save_delivery(user_id, workflow, request)
            if self._stage(workflow) == _STAGE_COLLECTING:
                return await self._collect_and_ask(request, workflow)
            return None
        if kind != DELIVERY_DRAFT or message_id is None:
            return None
        if str(workflow.get("draft_id") or "") != str(draft_id or ""):
            return None
        stage = self._stage(workflow)
        workflow["delivered"] = True
        workflow["delivered_message_id"] = int(message_id)
        if stage in (STAGE_DRAFT_PENDING_DELIVERY, STAGE_REVISING):
            # 已经用过修改额度的这一稿是**终审稿**：只允许确认或取消（设计 §3.6）。
            workflow["stage"] = (
                STAGE_AWAITING_FINAL_REVIEW
                if workflow.get("revision_used")
                else STAGE_AWAITING_REVIEW
            )
        await self._save_delivery(user_id, workflow, request)
        return None

    async def _save_delivery(
        self, user_id: str, workflow: dict, request: PersonaCommandRequest
    ) -> None:
        """写回送达标记；失败只记一条稳定日志（展示已经发出，不能反过来打扰用户）。"""
        status = await self._save_workflow(
            user_id,
            workflow,
            request,
            _DELIVERED_SUFFIX,
        )
        self._remember(user_id, workflow)
        if status not in (_SERVICE_OK, _SERVICE_NOOP):
            log_event(
                _logger,
                logging.WARNING,
                "persona.delivery",
                status=status,
                reason="save",
            )

    # --- 自动更新 ---------------------------------------------------------

    async def _maybe_update(
        self, user_id: str, evidence: tuple[str, ...], *, message_id: int
    ) -> str:
        if not self.config.auto_update_available:
            return STATUS_DISABLED
        if not evidence:
            return STATUS_NOOP
        if await self._load_workflow(user_id) is not None:
            # 建档、编辑与公开预览期间暂停自动提交（设计 §6）。
            return STATUS_DEFERRED
        persona = await self.service.persona_get(user_id)
        if persona is None or not persona.get("migrated") or not persona.get("enabled"):
            return STATUS_DISABLED
        if persona.get("pending_unpublish"):
            return STATUS_DEFERRED
        now = self._now()
        if (
            now - float(persona.get("last_update_at") or 0.0)
            < self.config.min_update_seconds
        ):
            return STATUS_DEFERRED
        today = _utc_day(now)
        count = int(persona.get("update_count") or 0)
        if persona.get("update_day") != today:
            count = 0
        if count >= self.config.max_updates_per_day:
            return STATUS_DEFERRED
        token = await self.service.persona_begin_update(user_id)
        if token is None:
            return STATUS_DISABLED
        document = getattr(token, "document", None)
        if not isinstance(document, dict):
            document = persona
        existing = {
            "paragraph": document.get("paragraph") or "",
            "features": list(document.get("features") or []),
        }
        previous = tuple(str(item) for item in (document.get("observations") or ()))
        # 阅读与更新**共用**一份预算（设计 §4.3 的「一次流程共用额度」在自动更新上的对应）。
        budget = {"used": 0, "limit": int(self.config.max_model_calls)}
        try:
            fresh = await self._writer_call(
                self.writer.read_sources, tuple(evidence), budget=budget
            )
            merged = self._merge_observations(previous, fresh, now)
            proposal = await self._writer_call(
                self.writer.update, existing, tuple(merged), budget=budget
            )
            paragraph, features = self._proposal(proposal)
        except PersonaWriterFailure as failure:
            log_event(
                _logger,
                logging.WARNING,
                "persona.update",
                status=STATUS_UNAVAILABLE,
                reason=failure.reason,
            )
            return STATUS_UNAVAILABLE
        metadata = {
            "update_day": today,
            "update_count": count + 1,
            "last_update_at": now,
            "last_message_id": int(message_id),
            "last_change": texts.PERSONA_TEXT_CHANGE_AUTO_TEXT,
            # 只落盘压缩后的带日期摘要：原始对话一个字节都不进 Markdown（设计 §6、§8）。
            "observations": list(merged),
        }
        status = _status_text(
            await self.service.persona_commit_update(
                token,
                paragraph,
                features,
                operation_id=f"{_UPDATE_SOURCE}:{int(message_id)}",
                metadata=metadata,
            )
        )
        if status in (_SERVICE_OK, _SERVICE_NOOP):
            log_event(
                _logger,
                logging.INFO,
                "persona.update",
                status=STATUS_OK,
                count=count + 1,
            )
            return STATUS_OK
        if status == _SERVICE_CONFLICT:
            # 提交时发现开关、代次或版本已变：放弃这批过期结果（设计 §6）。
            return STATUS_DEFERRED
        return STATUS_UNAVAILABLE

    def _merge_observations(
        self, previous: tuple[str, ...], fresh: Any, now: float
    ) -> tuple[str, ...]:
        """合并「画像里保留的摘要」与「本次新压缩的摘要」，按宿主时钟做有界保留。

        - 去重保序（旧的在前、新的在后），最后按 `_MAX_OBSERVATIONS` 截掉最旧的；
        - 丢掉**日期全部早于 30 天窗口**的摘要：近期兴趣只看 30 天（设计 §6），而长期特征
          早已固化在画像的特征里（带 `first_seen_at` / `last_seen_at`），因此裁剪摘要池
          不会丢掉长期证据；没有日期元数据的摘要（例如建档期的观察）一律保留。
        """
        merged: list[str] = [str(item) for item in previous if str(item).strip()]
        for item in fresh or ():
            text = str(item)
            if text.startswith("[observation:"):
                label = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                text = re.sub(r"^\[observation:[^|\]]+", "[observation:" + label, text, count=1)
            if text.strip() and text not in merged:
                merged.append(text)
        horizon = _utc_day(now - _RECENT_OBSERVATION_DAYS * 86400)
        kept = [item for item in merged if not _observation_expired(item, horizon)]
        return tuple(kept[-_MAX_OBSERVATIONS:])

    # --- 内部工具 ---------------------------------------------------------

    def _new_workflow(
        self,
        kind: str,
        request: PersonaCommandRequest,
        *,
        stage: str,
        enabled_on_commit: bool,
        base_revision: int = 0,
    ) -> dict:
        """新建一份流程（字段逐字对应 contract §15；存储层负责补齐默认值）。"""
        now = self._now()
        return {
            "schema_version": 1,
            "revision": 0,
            "workflow_id": f"{kind}-{int(request.message_id)}",
            "kind": kind,
            "stage": stage,
            "base_revision": int(base_revision),
            "watermark": int(request.message_id),
            "blog_count": 0,
            "message_count": 0,
            "source_summaries": [],
            "model_calls_used": 0,
            "questions": [],
            "answers": {},
            "paragraph": "",
            "features": [],
            "draft_id": "",
            "draft_version": 0,
            "revision_used": False,
            "delivered": False,
            "delivered_message_id": None,
            "questions_message_id": None,
            "expires_at": now + float(self.config.workflow_ttl_seconds),
            "enabled_on_commit": bool(enabled_on_commit),
            "correction": "",
            "operations": {},
        }

    def _budget(self, workflow: dict) -> dict[str, int]:
        """本次命令的模型调用预算：从工作流已用次数续算（设计 §4.3）。"""
        return {
            "used": int(workflow.get("model_calls_used") or 0),
            "limit": int(self.config.max_model_calls),
        }

    async def _persist_counters(
        self,
        request: PersonaCommandRequest,
        workflow: dict,
        budget: dict[str, int],
    ) -> None:
        """失败路径也要把已消耗的模型调用次数写回（设计 §4.3）。"""
        workflow["model_calls_used"] = int(budget.get("used") or 0)
        status = await self._save_workflow(
            request.user_id,
            workflow,
            request,
            "budget",
        )
        if status in (_SERVICE_OK, _SERVICE_NOOP):
            self._remember(request.user_id, workflow)

    async def _writer_call(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        budget: dict[str, int],
        **kwargs: Any,
    ) -> Any:
        """调用撰写器：取消原样传播，其余异常收敛成只带稳定 reason 的本地失败。"""
        try:
            return await func(*args, **kwargs, budget=budget)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PersonaWriterFailure(
                _reason_token(getattr(exc, "reason", None))
            ) from None

    @staticmethod
    def _exactly_five(questions: Any) -> tuple[str, ...]:
        """恰好 5 个问题（设计 §3.3）；数量和形状都不对就按撰写失败处理。"""
        if isinstance(questions, (str, bytes)) or not isinstance(
            questions, (list, tuple)
        ):
            raise PersonaWriterFailure("invalid_questions")
        values = tuple(str(item) for item in questions)
        if len(values) != MAX_ANSWER_INDEX or any(not item.strip() for item in values):
            raise PersonaWriterFailure("invalid_questions")
        return values

    @staticmethod
    def _proposal(value: Any) -> tuple[str, list]:
        """校验撰写器返回的 `{paragraph, features}`（设计 §5 末段）。"""
        if not isinstance(value, dict):
            raise PersonaWriterFailure("invalid_proposal")
        paragraph = value.get("paragraph")
        features = value.get("features")
        if not isinstance(paragraph, str) or not paragraph.strip():
            raise PersonaWriterFailure("invalid_proposal")
        if not isinstance(features, list):
            raise PersonaWriterFailure("invalid_proposal")
        return paragraph, list(features)

    async def _save_persona_fields(
        self,
        user_id: str,
        persona: dict,
        fields: dict[str, Any],
        request: PersonaCommandRequest | None,
        suffix: str,
    ) -> str | None:
        """把 C 侧字段写回画像文件：以读到的整份文档为基线，只覆盖本次要改的字段。

        `revision` 与 `operations` 原样回传（存储层把它们当宿主记账，写入时内部推进），
        `schema_version` 与其它字段也都逐字保留 —— 存储层的 envelope 要求字段集合精确一致，
        少传一个都会被整份拒绝。

        `updated_at` 默认刷新为宿主时钟的 UTC 时刻（注入时钟，测试可控）：C 侧的这几次写入
        （重新开启、提交后的元数据）同样是画像的一次变更。
        """
        value = dict(persona)
        value["updated_at"] = _utc_stamp(self._now())
        value.update(fields)
        operation_id = (
            self._operation(request, suffix) if request is not None else suffix
        )
        try:
            result = await self.service.persona_save(
                user_id,
                value,
                expected_revision=int(persona.get("revision") or 0),
                operation_id=operation_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                _logger,
                logging.WARNING,
                "persona.save",
                status=STATUS_UNAVAILABLE,
                reason=suffix,
            )
            return _SERVICE_UNAVAILABLE
        status = _status_text(result)
        if status not in (_SERVICE_OK, _SERVICE_NOOP):
            log_event(
                _logger, logging.WARNING, "persona.save", status=status, reason=suffix
            )
        return status

    def _invalidate_tokens(self, user_id: str) -> None:
        """进入手动编辑、关闭或取消时让在途的自动更新失效（设计 §6）。"""
        try:
            self.service.persona_invalidate(user_id)
        except Exception:
            log_event(
                _logger,
                logging.WARNING,
                "persona.invalidate",
                status=STATUS_UNAVAILABLE,
                reason="internal",
            )

    async def _load_workflow(self, user_id: str) -> dict | None:
        """读当前工作流（服务侧在锁内清理过期）并刷新同步快照。"""
        workflow = await self.service.persona_workflow(user_id)
        self._remember(user_id, workflow)
        return workflow

    async def _save_workflow(
        self,
        user_id: str,
        workflow: dict,
        request: PersonaCommandRequest,
        suffix: str,
    ) -> str:
        """保存工作流并把**本地副本**的版本号同步推进。

        存储层在写入成功时内部把版本号加一，本地这一份如果不动，紧接着的第二次保存就会因为
        CAS 对不上而被拒 —— 一次命令里连续写两次（受理修改 → 保存草稿）正是这种情形。
        `noop` 表示盘上没有变化，版本号也不推进。
        """
        status = _status_text(
            await self.service.persona_save_workflow(
                user_id,
                workflow,
                expected_revision=int(workflow.get("revision") or 0),
                operation_id=self._operation(request, suffix),
            )
        )
        if status == _SERVICE_OK:
            workflow["revision"] = int(workflow.get("revision") or 0) + 1
        return status

    def _remember(self, user_id: str, workflow: dict | None) -> None:
        """刷新 Router 用的同步快照（有界缓存，无 I/O）。"""
        if not user_id:
            return
        if workflow is None:
            self._hints.pop(user_id, None)
            return
        if len(self._hints) >= _HINT_CACHE_SIZE and user_id not in self._hints:
            self._hints.pop(next(iter(self._hints)), None)
        self._hints[user_id] = PersonaSessionHint(
            stage=self._stage(workflow),
            kind=self._kind(workflow),
            questions_message_id=_optional_int(workflow.get("questions_message_id")),
            delivered_message_id=_optional_int(workflow.get("delivered_message_id")),
            delivered=bool(workflow.get("delivered")),
            draft_id=str(workflow.get("draft_id") or ""),
        )

    def _is_published(self, user_id: str, persona: dict) -> bool:
        """该用户的画像是否已有公开副本（只读内存索引，同步、无 I/O）。"""
        try:
            owner_key = user_storage_key(user_id)
            index = self.service.persona_public_username_index()
        except Exception:
            return False
        username = str(persona.get("username") or "")
        if not username or not owner_key:
            return False
        return owner_key in tuple(index.get(username, ()) or ())

    def _public_revision(self, user_id: str) -> int | None:
        """公开快照的来源版本号（同步、只读取值）；实现不提供时返回 None。"""
        reader = getattr(self.service, "persona_public_revision_cached", None)
        if not callable(reader):
            return None
        try:
            value = reader(user_id)
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _questions_text(self, workflow: dict, *, warnings: tuple[str, ...] = ()) -> str:
        questions = tuple(workflow.get("questions") or ())
        lines = [f"{index}. {question}" for index, question in enumerate(questions, 1)]
        summary = texts.PERSONA_TEXT_COLLECT_SUMMARY_TEXT.format(
            blog=int(workflow.get("blog_count") or 0),
            messages=int(workflow.get("message_count") or 0),
        )
        if warnings:
            summary = f"{summary}\n" + texts.PERSONA_TEXT_COLLECT_WARNINGS_TEXT.format(
                warnings="、".join(_warning_labels(warnings))
            )
        return (
            f"{summary}\n\n"
            f"{texts.PERSONA_TEXT_QUESTIONS_HEADER_TEXT.format(count=len(lines))}\n"
            + "\n".join(lines)
            + f"\n\n{texts.PERSONA_TEXT_ANSWER_INSTRUCTIONS_TEXT}"
        )

    def _draft_text(self, workflow: dict) -> PersonaCommandResult | None:
        paragraph = str(workflow.get("paragraph") or "")
        if not paragraph:
            return None
        draft_id = str(workflow.get("draft_id") or "")
        if not draft_id:
            return None
        stage = self._stage(workflow)
        instructions = (
            texts.PERSONA_TEXT_FINAL_REVIEW_INSTRUCTIONS_TEXT
            if stage == STAGE_AWAITING_FINAL_REVIEW
            else texts.PERSONA_TEXT_DRAFT_INSTRUCTIONS_TEXT
        )
        version = int(workflow.get("draft_version") or 1)
        text = (
            f"{texts.PERSONA_TEXT_DRAFT_HEADER_TEXT.format(draft_id=draft_id, version=version)}\n\n"
            f"{paragraph}\n\n"
            f"{texts.PERSONA_TEXT_DRAFT_EVIDENCE_TEXT.format(evidence=self._evidence_labels(workflow))}\n\n"
            f"{instructions.format(draft_id=draft_id)}"
        )
        return self._result(
            STATUS_OK, text, draft_id=draft_id, delivery_kind=DELIVERY_DRAFT
        )

    def _public_preview(self, workflow: dict) -> str:
        return texts.PERSONA_TEXT_PUBLIC_PREVIEW_TEXT.format(
            revision=int(workflow.get("draft_version") or 0),
            paragraph=str(workflow.get("paragraph") or ""),
            draft_id=str(workflow.get("draft_id") or ""),
        )

    def _clear_preview(self, workflow: dict) -> str:
        return texts.PERSONA_TEXT_CLEAR_PREVIEW_TEXT.format(draft_id=str(workflow.get("draft_id") or ""))

    def _edit_prompt(self, workflow: dict) -> str:
        return texts.PERSONA_TEXT_EDIT_PROMPT_TEXT.format(
            revision=int(workflow.get("base_revision") or 0),
            paragraph=str(workflow.get("paragraph") or texts.PERSONA_TEXT_NO_PROFILE_TEXT),
        )

    @staticmethod
    def _evidence_labels(workflow: dict) -> str:
        """草稿的主要推测依据：特征里出现过的来源标签（有界、去重、保序）。"""
        labels: list[str] = []
        for feature in workflow.get("features") or ():
            if not isinstance(feature, dict):
                continue
            for ref in feature.get("evidence_refs") or ():
                value = str(ref)
                if value and value not in labels:
                    labels.append(value)
                if len(labels) >= _MAX_EVIDENCE_LABELS:
                    return "、".join(labels)
        return "、".join(labels) if labels else "样本不足"

    @staticmethod
    def _kind(workflow: dict) -> str:
        return str(workflow.get("kind") or "")

    @staticmethod
    def _stage(workflow: dict) -> str:
        return str(workflow.get("stage") or "")

    @staticmethod
    def _has_paragraph(persona: dict | None) -> bool:
        return bool(persona and str(persona.get("paragraph") or "").strip())

    @staticmethod
    def _draft_number(workflow: dict, version: int) -> int:
        """草稿编号 = **起始命令消息 id × 100 + 草稿版本号**。

        每条流程的草稿编号必须在**同一 owner 的整个生命周期内**唯一：生效画像里的
        `committed_draft_id` 会跨流程保留，而存储层用「marker == draft_id」判定「这一稿是不是
        已经提交过」。若每条新流程都从 `PD-1` 重新开始，第二次流程的 `PD-1` 会被当成
        「早就提交过」而静默跳过提交（或让重启恢复把在途草稿当成已提交丢弃）。
        起始消息 id 来自工作流的 `watermark`（创建流程时就是那条命令消息的 id）。
        """
        base = int(workflow.get("watermark") or 0)
        return base * 100 + max(1, int(version))

    def _stamp_features(
        self, features: list, *, base: dict | None, corrected: bool
    ) -> list[dict]:
        """给草稿特征补时间戳，并把「用户手动改动过」的特征交给宿主锁定。

        规则（设计 §5、§6）：

        - `first_seen_at` 沿用同一 `(dimension, value)` 在基线里的取值，新特征用当前 UTC 时刻；
          `last_seen_at` 一律刷新为当前 UTC 时刻 —— 写入工作流之前就定型，用户看到的短文
          与落盘的特征不会因为两次读取而分叉。
        - 用户这次提交了修改意见时，**相对基线新增或换词**的特征、以及模型标为
          `user_revision` 的特征，都由宿主置 `user_locked=True`：自动更新不得覆盖用户
          已经否定或明确改过的内容。
        - 基线里已经锁定的特征继续锁定（宿主侧也保住锁，不依赖模型）。
        """
        now = _utc_stamp(self._now())
        base_map: dict[tuple[Any, Any], dict] = {}
        for item in (base or {}).get("features") or ():
            if not isinstance(item, dict):
                continue
            key = (item.get("dimension"), item.get("value"))
            if isinstance(key[0], str) and isinstance(key[1], str):
                base_map[key] = item
        stamped: list[dict] = []
        for feature in features:
            item = dict(feature)
            key = (item.get("dimension"), item.get("value"))
            previous = base_map.get(key)
            first_seen = str(previous.get("first_seen_at") or "") if previous else ""
            item["first_seen_at"] = first_seen or now
            item["last_seen_at"] = now
            locked = bool(item.get("user_locked"))
            if previous is not None:
                locked = locked or bool(previous.get("user_locked"))
            if item.get("origin") == _ORIGIN_USER_REVISION or (corrected and previous is None):
                locked = True
            item["user_locked"] = locked
            stamped.append(item)
        return stamped

    @staticmethod
    def _operation(request: PersonaCommandRequest, suffix: str) -> str:
        return f"{_COMMAND_SOURCE}:{int(request.message_id)}:{suffix}"

    @staticmethod
    def _last_change_label(value: Any) -> str:
        """`last_change` 是给用户看的短句（存储层与 C 各自写自己的那几处）。

        只做形状收敛：空值、过长、含控制字符或换行的取值一律按「无记录」展示 —— 文件可能被
        手工改过，展示层不该把任意文本回声给用户。
        """
        if not isinstance(value, str):
            return texts.PERSONA_TEXT_LAST_CHANGE_UNKNOWN_TEXT
        text = value.strip()
        if not text or len(text) > _MAX_CHANGE_CHARS:
            return texts.PERSONA_TEXT_LAST_CHANGE_UNKNOWN_TEXT
        if any(char.isspace() and char != " " for char in text):
            return texts.PERSONA_TEXT_LAST_CHANGE_UNKNOWN_TEXT
        return text

    def _result(
        self,
        status: str,
        text: str,
        *,
        draft_id: str | None = None,
        delivery_kind: str | None = None,
    ) -> PersonaCommandResult:
        """构造结果；过长的展示不发出去（保留待办，等下一次重试）。"""
        if delivery_kind is not None and len(text) > self._max_display_chars:
            log_event(
                _logger,
                logging.WARNING,
                "persona.display",
                status=STATUS_UNAVAILABLE,
                reason="too_long",
            )
            return PersonaCommandResult(
                status=STATUS_UNAVAILABLE, text=texts.PERSONA_TEXT_WORKFLOW_UNAVAILABLE_TEXT
            )
        return PersonaCommandResult(
            status=status, text=text, draft_id=draft_id, delivery_kind=delivery_kind
        )

    def _log_command(
        self, request: PersonaCommandRequest, result: PersonaCommandResult
    ) -> None:
        """只记稳定结果与命令名；不含身份、问题、回答或短文（设计 §8 末段）。"""
        level = logging.INFO if result.status in _ACTIONABLE_STATUSES else logging.DEBUG
        log_event(
            _logger,
            level,
            "persona.command",
            status=result.status,
            kind=request.command.name or "usage",
        )


def _status_text(value: Any) -> str:
    """取稳定状态串；未知取值一律按 unavailable（软故障，不抛）。"""
    candidate = value if isinstance(value, str) else getattr(value, "status", None)
    if isinstance(candidate, str) and candidate in _SERVICE_STATUSES:
        return candidate
    return _SERVICE_UNAVAILABLE


def _warning_labels(warnings: tuple[str, ...]) -> tuple[str, ...]:
    """把采集器的稳定提示 token 翻成用户可见短句（`code=N` 形态带计数）。

    只接受 `<token>` 或 `<token>=<数字>` 的形状：文件或将来新增的 token 如果形状不对，
    回一句笼统提示，绝不把任意文本原样展示给用户。
    """
    labels: list[str] = []
    for value in warnings:
        match = _WARNING_TOKEN.fullmatch(value.strip())
        if match is None:
            labels.append(texts.PERSONA_TEXT_WARNING_UNKNOWN_TEXT)
            continue
        code = match.group("code")
        count = match.group("count")
        phrase = texts.PERSONA_WARNING_LABELS_TEXT.get(code)
        if phrase is None:
            labels.append(texts.PERSONA_TEXT_WARNING_UNKNOWN_TEXT)
        elif count is not None:
            labels.append(phrase.format(count=count))
        else:
            labels.append(phrase)
    return tuple(labels)


def _optional_int(value: Any) -> int | None:
    """把工作流里的站点消息 id 收敛成 `int | None`（形状不对按没有处理）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reason_token(value: Any) -> str:
    """把异常上的 reason 收敛成稳定 token；形状不对时用固定兜底 token。"""
    if isinstance(value, str) and _REASON_TOKEN.fullmatch(value):
        return value
    return "writer_failed"


def _utc_day(timestamp: float) -> str:
    """每人每天的更新计数按**站点时区（UTC+8）**的自然日切分。

    设计 §6 的节流是给用户看的「每人每天最多 N 次」：按 UTC 切分会让上限在本地时间
    早上 8 点翻页，同一个自然日能用掉两份额度（而傍晚之后又完全没有）。偏移量是固定常数，
    不依赖进程所在时区（存储层只校验 `YYYY-MM-DD` 形状）。
    """
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp + _UTC8_OFFSET_SECONDS))


def _utc_stamp(timestamp: float) -> str:
    """当前时刻的 UTC RFC 3339 秒级文本（与存储层的 `_timestamp()` 同一形态）。"""
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _observation_expired(observation: str, horizon: str) -> bool:
    """这条观察摘要的日期是否**全部早于** `horizon`（YYYY-MM-DD）。

    只认宿主首部里的 `|dates=…` 元数据（撰写器压缩时保留）；一条日期都解析不出来时返回
    假 —— 建档期的观察与手工写入的摘要不该因为缺元数据被裁掉。
    """
    match = _OBSERVATION_DATES.search(observation)
    if match is None:
        return False
    dates = [item.strip() for item in match.group("dates").split(",") if item.strip()]
    if not dates:
        return False
    return max(dates) < horizon
