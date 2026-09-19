"""定时发文子域的基础 DTO 与常量。

**本模块只含纯数据类型与常量**：不 import config、Store、SiteClient、MCP 或 App。
`blog/__init__.py` 必须保持为空，`config.py` 才能安全地 import 这里而不成环
（理由同 `capabilities.py` 顶上那段：`mcp/__init__.py` 会拉起 registry，registry 又依赖 config）。

两份「状态」常量与设计 §11 的 CHECK 约束是同一份取值，改动必须两边一起改。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 内容指纹版本。只有算法本身改变时才递增。
#
# **递增之前必须先想清楚旧行怎么办**：`blog_posts.hash_version` 有 `CHECK (hash_version = 1)`，
# 而且查表只按 `content_hash` 走（不按版本），所以把这里的值改成 2 会让每一条新插入都撞
# CHECK 而**立刻炸掉**（子域停摆、日志可见），而不是静默地拿新算法去重发旧内容。
# 也就是说：今天挡住重复发布的是那条 CHECK，不是版本字段 —— 版本字段本身还没有任何读取点。
# 真要换算法，得连同旧行的迁移（或人工结清）一起做，这是一次成对改动，不是改一个数字。
HASH_VERSION: int = 1

# 一次性扣除内容身份的重试上限（含首次 POST）。见设计 §7.2。
MAX_POST_ATTEMPTS: int = 3

# 只读对账的单行查询次数上限；到达后停止自动查询但**保持占额**（设计 §7.3）。
MAX_RECONCILE_ATTEMPTS: int = 12

# ---- 调度执行状态（blog_runs.status）----
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_SKIPPED = "skipped"
RUN_FINISHED = "finished"
RUN_FAILED = "failed"
RUN_INTERRUPTED = "interrupted"

RUN_STATUSES: frozenset[str] = frozenset(
    {RUN_QUEUED, RUN_RUNNING, RUN_SKIPPED, RUN_FINISHED, RUN_FAILED, RUN_INTERRUPTED}
)

# 一次执行已经落地、不会再变的终态。领取之后只能往这几个状态之一走。
RUN_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {RUN_SKIPPED, RUN_FINISHED, RUN_FAILED, RUN_INTERRUPTED}
)

# ---- 投递状态（blog_posts.status）----
STATUS_INFLIGHT = "inflight"
STATUS_PUBLISHED = "published"
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_REJECTED = "rejected"
STATUS_ABANDONED = "abandoned"

POST_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_INFLIGHT,
        STATUS_PUBLISHED,
        STATUS_UNCONFIRMED,
        STATUS_RETRY_WAIT,
        STATUS_REJECTED,
        STATUS_ABANDONED,
    }
)

# 仍然占着当天额度、且**不允许**被同指纹再次投递的状态（设计 §7.2、§7.4）。
POST_HOLDING_STATUSES: frozenset[str] = frozenset(
    {STATUS_INFLIGHT, STATUS_PUBLISHED, STATUS_UNCONFIRMED}
)

# 终局：同指纹不再自动重投（`retry_wait` 是唯一的例外，由 §7.4 单独判定）。
POST_SETTLED_STATUSES: frozenset[str] = frozenset(
    {STATUS_PUBLISHED, STATUS_REJECTED, STATUS_ABANDONED}
)

# ---- 内容来源（blog_posts.source_kind）----
SOURCE_FILE = "file"
SOURCE_GENERATED = "generated"

# ---- 稳定原因 ----
# 调度跳过。
REASON_PROBABILITY_MISS = "probability_miss"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_EMPTY_QUEUE = "empty_queue"
REASON_MISFIRE = "misfire"
REASON_GENERATION_FAILED = "generation_failed"
REASON_INTERRUPTED = "interrupted"
# 生成失败（设计 §8.3）的细分原因：都算「生成失败」，不产出草稿、不消耗发文预算。
REASON_MODEL_ERROR = "model_error"
REASON_TRUNCATED = "truncated"
REASON_INPUT_TOO_LARGE = "input_too_large"
REASON_TIMEOUT = "timeout"
REASON_DRAFT_INVALID = "draft_invalid"
REASON_DRAFT_EMPTY = "draft_empty"
# 稿库单文件不可用；坏稿不堵队首，记这个原因后继续下一个文件。
REASON_FILE_INVALID = "file_invalid"
# 预留结果。`REASON_RESERVED` 是**成功**那一支的取值：`reason` 字段任何一支都不留空，
# 否则日志里会出现 `reason=` 这种看不出是「没拒绝」还是「忘了填」的行。
REASON_RESERVED = "reserved"
# 预留拒绝（设计 §4.2 的 Store 合同：至少区分这四个）。
REASON_ALREADY_PUBLISHED = "already_published"
REASON_AWAITING_CONFIRMATION = "awaiting_confirmation"
REASON_NOT_RETRYABLE = "not_retryable"
# 投递结果（`PublishOutcome.reason` / `BlogPost.reason`）。
REASON_PUBLISHED = "published"
REASON_REJECTED = "rejected"
REASON_RATE_LIMITED = "rate_limited"
REASON_UNCONFIRMED = "unconfirmed"
REASON_ABANDONED = "abandoned"
# 只读对账。
REASON_RECONCILE_MATCH = "reconcile_match"
REASON_RECONCILE_NO_MATCH = "reconcile_no_match"
REASON_RECONCILE_INCOMPLETE = "reconcile_incomplete"
REASON_RECONCILE_AMBIGUOUS = "reconcile_ambiguous"
REASON_RECONCILE_EXHAUSTED = "reconcile_exhausted"


@dataclass(frozen=True)
class BlogScope:
    """一份记录的账号隔离域：站点地址 + 机器人自己的稳定用户 id。

    `site_base_url` 用 Config 已规范化的取值（去尾斜杠），不是运行期再从响应里读到的地址。
    """

    site_base_url: str
    self_user_id: str


@dataclass(frozen=True)
class Draft:
    """一条**尚未处理**的文章：稿库解析结果或模型输出，字段都是原始文本。

    正文与描述不进 repr：异常回溯里带出正文会让整篇文章漏进日志。
    """

    title: str
    description: str = field(repr=False)
    content: str = field(repr=False)


@dataclass(frozen=True)
class PreparedDraft:
    """已脱敏、已按 UTF-16 长度收口、指纹已算定的最终出站文本。

    与 `Draft` 分成两个类型是**故意的**：指纹算完之后再脱敏或再截断，
    落库的指纹就不再对应真正发出去的字节，对账会认错文章。
    """

    title: str
    description: str = field(repr=False)
    content: str = field(repr=False)
    content_hash: str
    hash_version: int = HASH_VERSION


@dataclass(frozen=True)
class RunCandidate:
    """一个到点的调度执行候选；是否真的执行由 Service 领取时决定。

    这里**不含**随机决策的结果：掷骰发生在确认执行键不存在之后（设计 §6.1）。
    """

    task_name: str
    scheduled_at: float
    task_order: int


@dataclass(frozen=True)
class BlogRun:
    """blog_runs 的一行快照；字段名与设计 §11 的 schema 一致。"""

    id: int
    site_base_url: str
    self_user_id: str
    task_name: str
    scheduled_at: float
    task_order: int
    selected: int
    status: str
    post_id: int | None
    created_at: float
    updated_at: float
    reason: str | None = None


@dataclass(frozen=True)
class BlogPost:
    """blog_posts 的一行快照。

    `title` 是**脱敏后**的待发布标题，也是本子域唯一允许落库的正文类字段（设计 §11）。
    正文本身永远不在这里，也不在任何其他持久介质里。
    """

    id: int
    site_base_url: str
    self_user_id: str
    task_name: str
    source_kind: str
    category_id: int | None
    content_hash: str
    hash_version: int
    title: str
    status: str
    created_at: float
    updated_at: float
    site_blog_id: str | None = None
    attempts: int = 0
    retry_after_day: str | None = None
    budget_from_day: str | None = None
    charged_through_day: str | None = None
    reconcile_attempts: int = 0
    last_reconciled_at: float | None = None
    confirmed_at: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class BlogReservation:
    """`reserve_blog_post` 的返回值：能不能发、发哪一行、为什么。

    拒绝时 `post` 必须为 None —— 不持有额度就不能顺手带出一个行快照，
    否则调用方很容易误以为拿到了可用的投递行。
    """

    allowed: bool
    post: BlogPost | None
    reason: str


@dataclass(frozen=True)
class PublishOutcome:
    """一次投递尝试的收尾结果；只带元数据，绝不把正文回传给任何调用方。"""

    post_id: int | None
    status: str
    reason: str


@dataclass(frozen=True)
class BlogRecoverySummary:
    """启动恢复的计数摘要（设计 §7.4 最后一段）。

    两类都是「旧进程留下的非终态行」：`inflight` → `unconfirmed`（结果未知，继续占额），
    `queued`/`running` 的执行 → `interrupted`（不重新生成）。计数只用于日志与运维判断，
    不是可以拿来做决策的状态。
    """

    unconfirmed: int = 0
    interrupted: int = 0
