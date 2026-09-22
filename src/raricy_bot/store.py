"""SQLite 运行状态持久化。

只保存运行元数据：事件与水位、私聊频道、已发回复、发送尝试时间、冷却状态、
定时发文的调度与投递元数据。
**不保存**消息正文、模型输入输出、Cookie、密码或 API Key（§19.1 红线）。

定时发文是本条红线唯一的例外，且例外边界写死在 D-107：只允许落**脱敏后的待发布标题**
与内容指纹，正文与描述（以及模型请求/响应体）仍然一个字都不落。

实现方式：单条 `sqlite3` 连接（`check_same_thread=False`），具体语句在
`asyncio.to_thread` 里执行，避免阻塞事件循环；串行化用一把**线程锁**，由真正在用
连接的那个工作线程持有（`_conn_lock`）。不用 `asyncio.Lock` 串行化连接是有原因的：
工作线程无法被取消，协程侧持锁时一次取消就会让锁在 worker 还停在 sqlite3 里时
释放，两个线程随即并发使用同一条连接（未定义行为）。全部方法都是 async。
时间一律是 `time.time()` 的 epoch 秒（REAL），不使用 datetime 字符串。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .blog_records import (
    MAX_POST_ATTEMPTS,
    MAX_RECONCILE_ATTEMPTS,
    POST_HOLDING_STATUSES,
    POST_STATUSES,
    REASON_ALREADY_PUBLISHED,
    REASON_AWAITING_CONFIRMATION,
    REASON_BUDGET_EXHAUSTED,
    REASON_INTERRUPTED,
    REASON_MISFIRE,
    REASON_NOT_RETRYABLE,
    REASON_RESERVED,
    RUN_INTERRUPTED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SKIPPED,
    RUN_TERMINAL_STATUSES,
    SCAN_WINDOW_SECONDS,
    STATUS_ABANDONED,
    STATUS_INFLIGHT,
    STATUS_PUBLISHED,
    STATUS_REJECTED,
    STATUS_RETRY_WAIT,
    STATUS_UNCONFIRMED,
    BlogPost,
    BlogRecoverySummary,
    BlogReservation,
    BlogRun,
    BlogScope,
    RunCandidate,
    utc8_day,
    utc8_next_day,
)
from .config import StorageConfig

_T = TypeVar("_T")

# 运行期元数据里安全水位检查点的键名（INTERFACES §9.2）。
_CHECKPOINT_KEY: str = "safe_event_watermark"
DEFAULT_COMMENT_CONVERSATION_RETENTION_SECONDS: int = 30 * 86400


def _read_checkpoint(conn: sqlite3.Connection) -> int:
    """读已提交的安全水位检查点；没有记录时为 0。"""
    row = conn.execute(
        "SELECT int_value FROM runtime_meta WHERE key = ?", (_CHECKPOINT_KEY,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _compute_candidate(conn: sqlite3.Connection, checkpoint: int) -> int:
    """按 INTERFACES §9.2 的规则算出候选水位。

    只看 `event_id > checkpoint` 的行（resync 行 `event_id IS NULL` 不参与）：
    有非终态行时取最小非终态 `event_id - 1`，否则取这些行里最大的 `event_id`；
    这类行为空时返回检查点本身，让水位停在原处而不是归零。
    """
    rows = conn.execute(
        "SELECT event_id, status FROM events WHERE event_id IS NOT NULL AND event_id > ?",
        (checkpoint,),
    ).fetchall()
    if not rows:
        return checkpoint

    pending = [int(row[0]) for row in rows if row[1] not in HANDLED_STATUSES]
    if pending:
        return min(pending) - 1
    return max(int(row[0]) for row in rows)


def _read_watermark(conn: sqlite3.Connection) -> int:
    """`watermark()` 的只读口径：检查点与现场计算值取较大者。"""
    checkpoint = _read_checkpoint(conn)
    return max(checkpoint, _compute_candidate(conn, checkpoint))


@dataclass(frozen=True)
class CleanupResult:
    """一次清理结果；过期评论会话 ID 供内存上下文同步失效。"""

    expired_thread_roots: tuple[int, ...]
    deleted_events: int
    deleted_sent_replies: int
    deleted_send_attempts: int
    deleted_cooldowns: int
    deleted_dm_channels: int
    safe_event_watermark: int
    db_logical_bytes: int
    db_physical_bytes: int
    deleted_comment_send_attempts: int = 0
    deleted_comment_conversations: int = 0
    deleted_comment_events: int = 0
    deleted_comment_sent_replies: int = 0
    deleted_comment_notifications: int = 0
    expired_comment_conversation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommentClaim:
    """评论候选的原子领取结果。"""

    claimed: bool
    comment_id: str
    conversation_id: str | None
    status: str


@dataclass(frozen=True)
class CommentDiscoveryState:
    """全站最近评论发现水位及初始化状态。"""

    initialized: bool
    newest_created_at: float | None
    boundary: tuple[str, ...]


@dataclass(frozen=True)
class CommentNotificationState:
    """通知匹配状态的无正文摘要。"""

    notification_id: str
    status: str
    unmatched_attempts: int


def _prune(
    conn: sqlite3.Connection,
    now: float,
    cfg: StorageConfig,
    *,
    comment_conversation_retention_seconds: int = DEFAULT_COMMENT_CONVERSATION_RETENTION_SECONDS,
    comment_dedupe_retention_seconds: int = 90 * 86400,
) -> CleanupResult:
    """在一个事务里完成清理；调用方负责提交与回滚。

    三条不得违反的规则（D-23）：非终态事件永不按时间删除；
    安全水位之上的终态事件不提前删除；回滚锚点（安全水位内 event_id 最大的终态行）必须保留。
    """
    retention = cfg.lobby_thread_retention_seconds
    cutoff = now - retention

    expired_roots = tuple(
        int(row[0])
        for row in conn.execute(
            "SELECT thread_root_id FROM lobby_threads WHERE updated_at <= ?"
            " ORDER BY thread_root_id",
            (cutoff,),
        )
    )
    conn.execute("DELETE FROM lobby_threads WHERE updated_at <= ?", (cutoff,))

    checkpoint = _read_watermark(conn)
    conn.execute(
        "INSERT INTO runtime_meta(key, int_value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET int_value = excluded.int_value",
        (_CHECKPOINT_KEY, checkpoint),
    )

    placeholders = ", ".join("?" for _ in HANDLED_STATUSES)
    anchor_row = conn.execute(
        "SELECT MAX(event_id) FROM events WHERE event_id IS NOT NULL"
        f" AND event_id <= ? AND status IN ({placeholders})",
        (checkpoint, *HANDLED_STATUSES),
    ).fetchone()
    anchor = anchor_row[0] if anchor_row is not None and anchor_row[0] is not None else -1
    deleted_events = conn.execute(
        "DELETE FROM events WHERE event_id IS NOT NULL AND event_id <= ?"
        f" AND status IN ({placeholders}) AND received_at <= ? AND event_id != ?",
        (checkpoint, *HANDLED_STATUSES, cutoff, anchor),
    ).rowcount

    deleted_sent_replies = conn.execute(
        "DELETE FROM sent_replies WHERE sent_at <= ?"
        " AND NOT EXISTS (SELECT 1 FROM events AS e"
        " WHERE e.message_id = sent_replies.reply_to"
        f" AND e.status NOT IN ({placeholders}))",
        (cutoff, *HANDLED_STATUSES),
    ).rowcount

    deleted_send_attempts = conn.execute(
        "DELETE FROM send_attempts WHERE attempted_at <= ?",
        (now - cfg.send_attempt_retention_seconds,),
    ).rowcount
    deleted_comment_send_attempts = conn.execute(
        "DELETE FROM comment_send_attempts WHERE attempted_at <= ?",
        (now - cfg.send_attempt_retention_seconds,),
    ).rowcount
    # 评论正文不在库中；会话和去重元数据分别按评论配置清理。
    # 非终态事件永不按时间删除。
    comment_conversation_cutoff = now - comment_conversation_retention_seconds
    # comment_events 的去重行通常保留 90 天，可能比会话映射多活 60 天。
    # 终态事件不再需要会话归属，先断开可过期映射；非终态事件仍保留归属，
    # 避免清理期间丢失恢复所需的活动会话。
    conn.execute(
        "UPDATE comment_events SET conversation_id=NULL "
        "WHERE conversation_id IN ("
        " SELECT conversation_id FROM comment_conversations WHERE updated_at <= ?"
        ") AND (status='done' OR status='baseline_ignored' OR status LIKE 'skipped_%')",
        (comment_conversation_cutoff,),
    )
    expired_comment_conversation_ids = tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT conversation_id FROM comment_conversations WHERE updated_at <= ? "
            "AND NOT EXISTS ("
            " SELECT 1 FROM comment_events "
            " WHERE comment_events.conversation_id=comment_conversations.conversation_id"
            ") ORDER BY conversation_id",
            (comment_conversation_cutoff,),
        ).fetchall()
    )
    deleted_comment_conversations = conn.execute(
        "DELETE FROM comment_conversations WHERE updated_at <= ? "
        "AND NOT EXISTS ("
        " SELECT 1 FROM comment_events "
        " WHERE comment_events.conversation_id=comment_conversations.conversation_id"
        ")",
        (comment_conversation_cutoff,),
    ).rowcount
    deleted_comment_events = conn.execute(
        "DELETE FROM comment_events WHERE (status='done' OR status='baseline_ignored'"
        " OR status LIKE 'skipped_%')"
        " AND updated_at <= ? AND NOT EXISTS ("
        " SELECT 1 FROM comment_notification_candidates AS nc "
        " JOIN comment_notifications AS n "
        " ON n.notification_id=nc.notification_id "
        " WHERE nc.comment_id=comment_events.comment_id "
        " AND n.status NOT IN ('done','unmatched','baseline_ignored')"
        ")",
        (now - comment_dedupe_retention_seconds,),
    ).rowcount
    deleted_comment_sent_replies = conn.execute(
        "DELETE FROM comment_sent_replies WHERE sent_at <= ?",
        (now - comment_dedupe_retention_seconds,),
    ).rowcount
    deleted_comment_notifications = conn.execute(
        "DELETE FROM comment_notifications WHERE status IN ('done','unmatched','baseline_ignored') AND updated_at <= ?",
        (now - comment_dedupe_retention_seconds,),
    ).rowcount

    deleted_cooldowns = conn.execute(
        "DELETE FROM cooldowns WHERE until <= ?", (now,)
    ).rowcount

    deleted_dm_channels = conn.execute(
        "DELETE FROM dm_channels WHERE channel_id NOT IN ("
        " SELECT channel_id FROM dm_channels"
        " ORDER BY updated_at DESC, channel_id DESC LIMIT ?)",
        (cfg.max_dm_channels,),
    ).rowcount

    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])

    return CleanupResult(
        expired_thread_roots=expired_roots,
        deleted_events=deleted_events,
        deleted_sent_replies=deleted_sent_replies,
        deleted_send_attempts=deleted_send_attempts,
        deleted_cooldowns=deleted_cooldowns,
        deleted_dm_channels=deleted_dm_channels,
        safe_event_watermark=checkpoint,
        db_logical_bytes=max(0, page_count - freelist) * page_size,
        db_physical_bytes=page_count * page_size,
        deleted_comment_send_attempts=deleted_comment_send_attempts,
        deleted_comment_conversations=deleted_comment_conversations,
        deleted_comment_events=deleted_comment_events,
        deleted_comment_sent_replies=deleted_comment_sent_replies,
        deleted_comment_notifications=deleted_comment_notifications,
        expired_comment_conversation_ids=expired_comment_conversation_ids,
    )

# events.status 的取值。
#   pending —— 本进程已接收并入队，尚未处理完。
#   recover —— **上一进程崩溃时遗留**的未完成行，启动时由 mark_orphans_recoverable()
#              标记出来，允许被重新认领（见 INTERFACES.md §9 §12 §16）。
#   done / skipped —— 终态。
STATUS_PENDING: str = "pending"
STATUS_RECOVER: str = "recover"
STATUS_DONE: str = "done"
STATUS_SKIPPED: str = "skipped"

# 视为「已处理」的状态集合；**其余状态一律是非终态**，会压住水位。
# 判断非终态时请用 `status NOT IN HANDLED_STATUSES`，不要写成 `status = 'pending'`：
# 那样会让 recover 行既不算 pending 又被水位忽略，等于把孤儿事件连同它的水位一起跳过。
HANDLED_STATUSES: tuple[str, ...] = (STATUS_DONE, STATUS_SKIPPED)


def _merge_comment_sources(old: str, new: str) -> str:
    """合并双来源诊断字段，顺序固定且幂等。"""
    if old == "both" or new == "both":
        return "both"
    values = {item for item in (old + "," + new).split(",") if item in {"recent", "notification"}}
    if values == {"recent", "notification"}:
        return "both"
    if "recent" in values:
        return "recent"
    if "notification" in values:
        return "notification"
    return old or new

# 建表语句（字段类型自定，语义与 INTERFACES.md §9 一致）。
# events 的主键是 message_id —— 去重键，event_id 可空，只供水位计算。
_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER,
        message_id INTEGER PRIMARY KEY,
        channel_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        received_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_status ON events (status)",
    "CREATE INDEX IF NOT EXISTS idx_events_event_id ON events (event_id)",
    """
    CREATE TABLE IF NOT EXISTS dm_channels (
        channel_id TEXT PRIMARY KEY,
        last_message_id INTEGER NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sent_replies (
        message_id INTEGER PRIMARY KEY,
        channel_id TEXT NOT NULL,
        reply_to INTEGER,
        sent_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS send_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT NOT NULL,
        reply_to INTEGER,
        kind TEXT NOT NULL,
        attempted_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cooldowns (
        key TEXT PRIMARY KEY,
        until REAL NOT NULL
    )
    """,
    # 大区共享链：只保存「消息 id -> 链根 id」的归属，链根就是开启该链那条消息的 id。
    # 不保存用户名、用户 id、参与者名单或任何正文（§19.7）。
    """
    CREATE TABLE IF NOT EXISTS lobby_threads (
        thread_root_id INTEGER PRIMARY KEY,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lobby_threads_updated_at ON lobby_threads (updated_at)",
    """
    CREATE TABLE IF NOT EXISTS lobby_thread_messages (
        message_id INTEGER PRIMARY KEY,
        thread_root_id INTEGER NOT NULL,
        mapped_at REAL NOT NULL,
        FOREIGN KEY(thread_root_id)
            REFERENCES lobby_threads(thread_root_id)
            ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lobby_thread_messages_root"
    " ON lobby_thread_messages (thread_root_id)",
    # 运行期元数据；目前只放单调的 safe_event_watermark。
    """
    CREATE TABLE IF NOT EXISTS runtime_meta (
        key TEXT PRIMARY KEY,
        int_value INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_discovery_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        initialized INTEGER NOT NULL DEFAULT 0,
        newest_created_at REAL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_discovery_boundary (
        comment_id TEXT PRIMARY KEY,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_conversations (
        conversation_id TEXT PRIMARY KEY,
        blog_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_comment_conversations_updated ON comment_conversations(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS comment_events (
        comment_id TEXT PRIMARY KEY,
        blog_id TEXT NOT NULL,
        parent_id TEXT,
        conversation_id TEXT,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        observed_created_at REAL,
        observed_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL,
        FOREIGN KEY(conversation_id)
            REFERENCES comment_conversations(conversation_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_comment_events_status_due ON comment_events(status, next_attempt_at)",
    """
    CREATE TABLE IF NOT EXISTS comment_messages (
        comment_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        parent_id TEXT,
        mapped_at REAL NOT NULL,
        FOREIGN KEY(conversation_id) REFERENCES comment_conversations(conversation_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_comment_messages_conversation ON comment_messages(conversation_id)",
    """
    CREATE TABLE IF NOT EXISTS comment_sent_replies (
        trigger_comment_id TEXT PRIMARY KEY,
        bot_comment_id TEXT NOT NULL UNIQUE,
        blog_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        sent_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_notifications (
        notification_id TEXT PRIMARY KEY,
        blog_id TEXT NOT NULL,
        actor_fingerprint TEXT,
        status TEXT NOT NULL,
        unmatched_attempts INTEGER NOT NULL DEFAULT 0,
        observed_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_notification_baseline_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        cutoff_timestamp REAL NOT NULL,
        cutoff_notification_id TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_notification_candidates (
        notification_id TEXT NOT NULL,
        comment_id TEXT NOT NULL,
        PRIMARY KEY(notification_id, comment_id),
        FOREIGN KEY(notification_id) REFERENCES comment_notifications(notification_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_send_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        blog_id TEXT NOT NULL,
        trigger_comment_id TEXT,
        kind TEXT NOT NULL,
        attempted_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_comment_send_attempts_time ON comment_send_attempts(attempted_at)",
    "CREATE INDEX IF NOT EXISTS idx_comment_send_attempts_blog_time ON comment_send_attempts(blog_id, attempted_at)",
    # 定时发文（设计 §11）：投递行与调度执行行职责分离 —— blog_posts 去重内容与计费，
    # blog_runs 去重调度点。两张表都是追加式建表，旧库打开后自动补上，旧数据不动。
    # 这两张表**不挂进** `_prune()`：首版不自动清理。删掉投递行会让同一篇文重新发布，
    # 不确定行更是只能人工核实（设计 §11 最后一段）。
    """
    CREATE TABLE IF NOT EXISTS blog_posts (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        site_base_url        TEXT NOT NULL,
        self_user_id         TEXT NOT NULL,
        task_name            TEXT NOT NULL,
        source_kind          TEXT NOT NULL CHECK (source_kind IN ('file', 'generated')),
        category_id          INTEGER,
        content_hash         TEXT NOT NULL,
        hash_version         INTEGER NOT NULL DEFAULT 1 CHECK (hash_version = 1),
        title                TEXT NOT NULL,
        status               TEXT NOT NULL CHECK (status IN
                             ('inflight', 'published', 'unconfirmed', 'retry_wait', 'rejected', 'abandoned')),
        site_blog_id         TEXT,
        attempts             INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
        retry_after_day      TEXT,
        budget_from_day      TEXT,
        charged_through_day  TEXT,
        reconcile_attempts   INTEGER NOT NULL DEFAULT 0,
        last_reconciled_at   REAL,
        created_at           REAL NOT NULL,
        updated_at           REAL NOT NULL,
        confirmed_at         REAL,
        reason               TEXT,
        UNIQUE (site_base_url, self_user_id, content_hash)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_blog_posts_site_id"
    " ON blog_posts (site_base_url, self_user_id, site_blog_id) WHERE site_blog_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_blog_posts_status"
    " ON blog_posts (site_base_url, self_user_id, status, last_reconciled_at)",
    """
    CREATE TABLE IF NOT EXISTS blog_runs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        site_base_url     TEXT NOT NULL,
        self_user_id      TEXT NOT NULL,
        task_name         TEXT NOT NULL,
        scheduled_at      REAL NOT NULL,
        task_order        INTEGER NOT NULL,
        selected          INTEGER NOT NULL CHECK (selected IN (0, 1)),
        status            TEXT NOT NULL CHECK (status IN
                          ('queued', 'running', 'skipped', 'finished', 'failed', 'interrupted')),
        post_id           INTEGER REFERENCES blog_posts(id),
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL,
        reason            TEXT,
        UNIQUE (site_base_url, self_user_id, task_name, scheduled_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_blog_runs_pending"
    " ON blog_runs (site_base_url, self_user_id, status, scheduled_at, task_order)",
)

# --- 定时发文的状态语义与行转换（INTERFACES §53.4）--------------------------
#
# SELECT 列顺序与 `BlogPost` / `BlogRun` 的字段顺序逐字对应，行可以直接解包成 DTO。
_BLOG_POST_COLUMNS: str = (
    "id, site_base_url, self_user_id, task_name, source_kind, category_id, content_hash,"
    " hash_version, title, status, created_at, updated_at, site_blog_id, attempts,"
    " retry_after_day, budget_from_day, charged_through_day, reconcile_attempts,"
    " last_reconciled_at, confirmed_at, reason"
)
_BLOG_RUN_COLUMNS: str = (
    "id, site_base_url, self_user_id, task_name, scheduled_at, task_order, selected,"
    " status, post_id, created_at, updated_at, reason"
)

# 占额但不允许同指纹重投的两种状态（`POST_HOLDING_STATUSES` 去掉 published）。
# 从常量派生而不是另抄一份字面量：两处一旦不同步，预算口径就会悄悄错。
_BLOG_PENDING_STATUSES: tuple[str, ...] = tuple(
    sorted(POST_HOLDING_STATUSES - {STATUS_PUBLISHED})
)

# 投递状态迁移表（设计 §7.2）。表里没有的迁移一律拒绝 —— `finalize_blog_post`
# 不得成为任意改状态的后门。三行的理由：
#   inflight   —— POST 在途，四种结果都由这里落定；
#   unconfirmed—— 只读对账找到唯一精确匹配才转 published；同状态调用只刷新查询元数据，
#                 绝不释放额度（unconfirmed 到 rejected/retry_wait/abandoned 是明令禁止的）；
#   published  —— 重复确认必须幂等，因此只允许「还是 published」这一种。
_BLOG_POST_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_INFLIGHT: frozenset(
        {STATUS_PUBLISHED, STATUS_UNCONFIRMED, STATUS_REJECTED, STATUS_RETRY_WAIT, STATUS_ABANDONED}
    ),
    STATUS_UNCONFIRMED: frozenset({STATUS_PUBLISHED, STATUS_UNCONFIRMED}),
    STATUS_PUBLISHED: frozenset({STATUS_PUBLISHED}),
}


def _row_to_blog_post(row: tuple[object, ...]) -> BlogPost:
    """把 `_BLOG_POST_COLUMNS` 的一行解包成 DTO。"""
    return BlogPost(*row)  # type: ignore[arg-type]


def _row_to_blog_run(row: tuple[object, ...]) -> BlogRun:
    """把 `_BLOG_RUN_COLUMNS` 的一行解包成 DTO。"""
    return BlogRun(*row)  # type: ignore[arg-type]


def _blog_budget_used(conn: sqlite3.Connection, scope: BlogScope, day: str) -> int:
    """当天已占用的发文额度（设计 §9、D-108）。

    当天占用 = **当天计费闭区间**覆盖的 published 行数 + **全部** inflight/unconfirmed 行数，
    一行只计一次（两种状态互斥，所以两个子查询不会重复计同一行）。
    `charged_through_day` 为 NULL 的 published 行没有已确认区间，不计入 —— 本实现里
    只有 `finalize_blog_post(published)` 会写这两个字段，它一定一起写。

    调用方必须处在同一事务里：预检（省模型调用）不能代替预留时的复查。
    """
    holding = ", ".join("?" for _ in _BLOG_PENDING_STATUSES)
    row = conn.execute(
        "SELECT ("
        " SELECT COUNT(*) FROM blog_posts"
        " WHERE site_base_url = ? AND self_user_id = ?"
        f" AND status IN ({holding})"
        ") + ("
        " SELECT COUNT(*) FROM blog_posts"
        " WHERE site_base_url = ? AND self_user_id = ? AND status = ?"
        " AND budget_from_day IS NOT NULL AND charged_through_day IS NOT NULL"
        " AND budget_from_day <= ? AND ? <= charged_through_day"
        ")",
        (
            scope.site_base_url,
            scope.self_user_id,
            *_BLOG_PENDING_STATUSES,
            scope.site_base_url,
            scope.self_user_id,
            STATUS_PUBLISHED,
            day,
            day,
        ),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _blog_refusal_reason(
    post: BlogPost, *, run: BlogRun, category_id: int | None, day: str
) -> str | None:
    """设计 §7.4 的六种指纹状态：返回 None 表示这一行可以被复用/新建，否则是拒绝原因。"""
    if post.status == STATUS_PUBLISHED:
        return REASON_ALREADY_PUBLISHED
    if post.status in _BLOG_PENDING_STATUSES:
        # 结果未定的行绝不重投：额度还占着，去重靠这一条，而不是靠运气。
        return REASON_AWAITING_CONFIRMATION
    if post.status == STATUS_RETRY_WAIT:
        # 429 之后的有界重试：同原任务、栏目未变、已到重试日、尝试次数未到上限，缺一不可。
        if post.task_name != run.task_name:
            return REASON_NOT_RETRYABLE
        if post.category_id != category_id:
            return REASON_NOT_RETRYABLE
        if post.retry_after_day is None or day < post.retry_after_day:
            return REASON_NOT_RETRYABLE
        if post.attempts >= MAX_POST_ATTEMPTS:
            return REASON_NOT_RETRYABLE
        return None
    # rejected / abandoned（以及任何未知取值）：同指纹不自动再投，只保留诊断记录。
    return REASON_NOT_RETRYABLE


class Store:
    """SQLite 状态存储；全部方法 async，单连接 + 锁串行。"""

    def __init__(
        self,
        path: str,
        *,
        wal_journal_limit_bytes: int = 16777216,
        conversation_retention_seconds: int = DEFAULT_COMMENT_CONVERSATION_RETENTION_SECONDS,
        dedupe_retention_seconds: int = 90 * 86400,
    ) -> None:
        if (
            isinstance(conversation_retention_seconds, bool)
            or not isinstance(conversation_retention_seconds, int)
            or conversation_retention_seconds < 1
        ):
            raise ValueError("conversation_retention_seconds 必须是正整数")
        if (
            isinstance(dedupe_retention_seconds, bool)
            or not isinstance(dedupe_retention_seconds, int)
            or dedupe_retention_seconds < conversation_retention_seconds
        ):
            raise ValueError("dedupe_retention_seconds 必须是不小于会话保留期的正整数")
        self._path = path
        self._wal_journal_limit_bytes = wal_journal_limit_bytes
        self._conversation_retention_seconds = conversation_retention_seconds
        self._dedupe_retention_seconds = dedupe_retention_seconds
        # 构造时不建连接：open() 里才落盘/建表。
        self._conn: sqlite3.Connection | None = None
        # 两把锁分工不同，缺一不可：
        # `_state_lock` 是 asyncio 锁，管 open/close 的**状态转换**。两者都跨 await，
        # 不串行化会出现「close 看到 None 提前返回、随后 open 又建了一条连接」的泄漏。
        # `_conn_lock` 是线程锁，管连接的**使用**：连接是在工作线程里用的，而工作线程
        # 无法被取消，所以这把锁必须由用它的那个线程持有到它结束 —— asyncio 锁做不到，
        # 取消 await 会让它在 worker 还停在 sqlite3 里时就放锁（见 _execute）。
        self._state_lock = asyncio.Lock()
        self._conn_lock = threading.Lock()

    # --- 生命周期 -----------------------------------------------------------

    async def open(self) -> None:
        """建立连接并建表；可重复调用（幂等）。"""
        async with self._state_lock:
            if self._conn is not None:
                return
            self._conn = await asyncio.to_thread(self._connect)

    async def close(self) -> None:
        """关闭连接；可重复调用（幂等）。"""
        async with self._state_lock:
            conn = self._conn
            self._conn = None
            if conn is None:
                return
            await asyncio.to_thread(self._close_conn, conn)

    def _close_conn(self, conn: sqlite3.Connection) -> None:
        """在工作线程里关连接。

        先取 `_conn_lock`：`self._conn` 已经在上面的协程里置空，所以排队中的操作
        会在拿到锁后看到「连接已经不是当前这条」并拒绝执行，而正在执行的操作
        会把这段临界区走完再放锁 —— close 因此永远不可能关上一条正在被使用的连接。
        """
        with self._conn_lock:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        """在工作线程里建连接、设 PRAGMA 并建表。"""
        # 默认配置是 ./data/bot.db，而新检出的仓库里没有 data/ 目录；
        # 不预先建目录的话 sqlite3.connect 会直接抛
        # `OperationalError: unable to open database file`。
        # 容器里 Dockerfile 已经建了 /app/data，所以这个问题只在本地直接运行时暴露。
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        # `:memory:` 不支持 WAL，此时 PRAGMA 返回 "memory"，不会报错。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # WAL 的目标上限：journal 超过它时 SQLite 会在下次检查点截断（D-23）。
        conn.execute(f"PRAGMA journal_size_limit={int(self._wal_journal_limit_bytes)}")
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.commit()
        return conn

    async def _execute(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        """把一次数据库操作丢进线程执行，保证单连接被串行使用。

        串行化发生在**工作线程**里（`_conn_lock`），不在协程里：连接的使用者是线程，
        而线程不能被取消。若在协程侧持锁，取消 await 会在 worker 还停在 sqlite3 里时
        就放锁，下一个操作随即在另一个线程上并发使用同一条连接 —— sqlite3 的未定义
        行为（实测抛 `InterfaceError: bad parameter or other API misuse`，在 Windows
        上表现为访问冲突，直接把进程打死）。
        """
        conn = self._conn
        if conn is None:
            raise RuntimeError("Store 尚未 open()，无法执行操作")
        return await asyncio.to_thread(self._run_locked, operation, conn)

    def _run_locked(
        self, operation: Callable[[sqlite3.Connection], _T], conn: sqlite3.Connection
    ) -> _T:
        """在工作线程里取锁并执行；锁由这个线程自己持有到操作结束。"""
        with self._conn_lock:
            if conn is not self._conn:
                # 排队期间被 close() 了：连接要么已经关掉，要么正等着这条锁。
                raise RuntimeError("Store 已关闭，无法执行操作")
            return operation(conn)

    # --- 事件与水位（去重主键是 message_id，不是 event_id）------------------

    async def record_event(self, event_id: int | None, message_id: int, channel_id: str) -> bool:
        """记录一条候选事件；返回 True 表示首次记录，False 表示该 message_id 已存在。

        `event_id` 为 None 表示消息来自 resync 拉取，没有 SSE 事件 id。
        """

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO events"
                " (event_id, message_id, channel_id, status, received_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (event_id, message_id, channel_id, STATUS_PENDING, time.time()),
            )
            conn.commit()
            return cursor.rowcount > 0

        return await self._execute(operation)

    async def mark_handled(self, message_id: int, status: str) -> None:
        """把事件标记为已处理，`status` 取 "done" 或 "skipped"。"""

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE events SET status = ? WHERE message_id = ?",
                (status, message_id),
            )
            conn.commit()

        await self._execute(operation)

    async def message_status(self, message_id: int) -> str | None:
        """返回事件状态；没有这条记录时返回 None。"""

        def operation(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT status FROM events WHERE message_id = ?", (message_id,)
            ).fetchone()
            return str(row[0]) if row is not None else None

        return await self._execute(operation)

    async def is_handled(self, message_id: int) -> bool:
        """status 为 done/skipped 时返回 True。"""

        def operation(conn: sqlite3.Connection) -> bool:
            placeholders = ", ".join("?" for _ in HANDLED_STATUSES)
            row = conn.execute(
                "SELECT 1 FROM events WHERE message_id = ?"
                f" AND status IN ({placeholders})",
                (message_id, *HANDLED_STATUSES),
            ).fetchone()
            return row is not None

        return await self._execute(operation)

    async def advance_watermark(self) -> int:
        """把安全水位检查点**单调推进**到当前可安全提交的值，返回新值。

        与只读的 `watermark()` 不同，本方法会写入 `runtime_meta`：清理删掉旧事件行之后，
        水位仍能从检查点恢复，不会倒退引发大范围重放（见 INTERFACES §9.2、D-23）。
        算法只看检查点之后的行；没有这类行时保持检查点不变。
        """
        return await self._execute(self._advance_checkpoint)

    async def watermark(self) -> int:
        """只读，不推进：返回 `max(检查点, 按同一规则现场算出的值)`。"""
        return await self._execute(_read_watermark)

    @staticmethod
    def _advance_checkpoint(conn: sqlite3.Connection) -> int:
        checkpoint = _read_checkpoint(conn)
        value = max(checkpoint, _compute_candidate(conn, checkpoint))
        if value != checkpoint:
            conn.execute(
                "INSERT INTO runtime_meta(key, int_value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET int_value = excluded.int_value",
                (_CHECKPOINT_KEY, value),
            )
            conn.commit()
        return value

    async def pending_messages(self) -> list[tuple[int | None, int, str]]:
        """按 message_id 升序返回**所有未完成**事件：(event_id, message_id, channel_id)。

        「未完成」= 状态不在 HANDLED_STATUSES 里，因此 pending 与 recover 都算。
        """

        def operation(conn: sqlite3.Connection) -> list[tuple[int | None, int, str]]:
            placeholders = ", ".join("?" for _ in HANDLED_STATUSES)
            rows = conn.execute(
                "SELECT event_id, message_id, channel_id FROM events"
                f" WHERE status NOT IN ({placeholders}) ORDER BY message_id",
                HANDLED_STATUSES,
            ).fetchall()
            return [(row[0], row[1], row[2]) for row in rows]

        return await self._execute(operation)

    async def mark_orphans_recoverable(self) -> int:
        """崩溃恢复第一步：把所有 pending 行改标为 recover，返回改动行数。

        由 `BotApp.start()` 在 `Store.open()` 之后、SSE 启动**之前**调用一次。
        此刻本进程尚未认领任何事件，因此任何非终态行必定属于已经死掉的旧进程。
        顺序不能反：在本进程入队之后再扫，会把我们自己的在途工作误标成孤儿。
        """

        def operation(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE events SET status = ? WHERE status = ?",
                (STATUS_RECOVER, STATUS_PENDING),
            )
            conn.commit()
            return int(cursor.rowcount)

        return await self._execute(operation)

    async def reclaim_orphan(self, message_id: int) -> bool:
        """原子地把一条 recover 行重新认领为 pending；True 表示本次认领成功。

        单条 UPDATE + rowcount 判定，因此同一孤儿事件即使被 SSE 补发与 resync
        并发投递，也只有一个调用方拿得到 True。
        本进程已入队的 pending 行、以及 done/skipped 的终态行都不可认领。
        """

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE events SET status = ? WHERE message_id = ? AND status = ?",
                (STATUS_PENDING, message_id, STATUS_RECOVER),
            )
            conn.commit()
            return cursor.rowcount > 0

        return await self._execute(operation)

    # --- 私聊频道 -----------------------------------------------------------

    async def upsert_dm_channel(self, channel_id: str, last_message_id: int) -> None:
        """登记/更新已知私聊频道及其最后见到的消息 id（幂等）。

        `last_message_id` 取历史最大值：resync 与实时流可能乱序到达，
        不能被一条迟到的旧值拉低。
        """

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO dm_channels (channel_id, last_message_id, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(channel_id) DO UPDATE SET"
                " last_message_id = MAX(excluded.last_message_id, dm_channels.last_message_id),"
                " updated_at = excluded.updated_at",
                (channel_id, last_message_id, time.time()),
            )
            conn.commit()

        await self._execute(operation)

    async def dm_channels(self) -> list[str]:
        """返回已登记的私聊频道 id（按 id 升序）。"""

        def operation(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT channel_id FROM dm_channels ORDER BY channel_id"
            ).fetchall()
            return [str(row[0]) for row in rows]

        return await self._execute(operation)

    # --- 博客评论发现与状态机 ----------------------------------------------

    async def get_comment_discovery_state(self) -> CommentDiscoveryState:
        """读取最近评论发现水位；尚未初始化时返回空基线。"""

        def operation(conn: sqlite3.Connection) -> CommentDiscoveryState:
            row = conn.execute(
                "SELECT initialized, newest_created_at FROM comment_discovery_state WHERE singleton=1"
            ).fetchone()
            if row is None:
                return CommentDiscoveryState(False, None, ())
            boundary_rows = conn.execute(
                "SELECT comment_id FROM comment_discovery_boundary WHERE created_at = ? ORDER BY comment_id",
                (row[1],),
            ).fetchall() if row[1] is not None else []
            return CommentDiscoveryState(bool(row[0]), row[1], tuple(str(x[0]) for x in boundary_rows))

        return await self._execute(operation)

    async def comment_discovery_state(self) -> CommentDiscoveryState:
        """`get_comment_discovery_state` 的简短别名。"""
        return await self.get_comment_discovery_state()

    async def comment_discovery_boundary(self) -> set[str]:
        """返回当前水位时间的 UUID 边界集合。"""
        state = await self.get_comment_discovery_state()
        return set(state.boundary)

    async def set_comment_discovery_state(
        self,
        *,
        initialized: bool,
        newest_created_at: float | None,
        boundary: tuple[str, ...] | list[str] = (),
        now: float | None = None,
    ) -> None:
        """原子更新发现水位和等时边界集合。"""
        timestamp = time.time() if now is None else now

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM comment_discovery_boundary")
            if newest_created_at is not None:
                conn.executemany(
                    "INSERT OR IGNORE INTO comment_discovery_boundary(comment_id, created_at) VALUES (?, ?)",
                    [(str(item), newest_created_at) for item in boundary],
                )
            conn.execute(
                "INSERT INTO comment_discovery_state(singleton, initialized, newest_created_at, updated_at) VALUES (1, ?, ?, ?)"
                " ON CONFLICT(singleton) DO UPDATE SET initialized=excluded.initialized, newest_created_at=excluded.newest_created_at, updated_at=excluded.updated_at",
                (1 if initialized else 0, newest_created_at, timestamp),
            )
            conn.commit()

        await self._execute(operation)

    async def initialize_comment_discovery(
        self, newest_created_at: float | None, boundary: tuple[str, ...] | list[str], *, now: float | None = None
    ) -> None:
        """写入冷启动基线；调用方负责保证只执行一次业务初始化。"""
        await self.set_comment_discovery_state(
            initialized=True, newest_created_at=newest_created_at, boundary=boundary, now=now
        )

    async def update_comment_discovery(
        self, newest_created_at: float | None, boundary: tuple[str, ...] | list[str], *, now: float | None = None
    ) -> None:
        """更新发现水位；水位只允许单调前进。"""
        timestamp = time.time() if now is None else now

        def operation(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT newest_created_at FROM comment_discovery_state WHERE singleton=1"
            ).fetchone()
            old = row[0] if row is not None else None
            if old is not None and newest_created_at is not None and newest_created_at < old:
                return
            conn.execute("DELETE FROM comment_discovery_boundary")
            if newest_created_at is not None:
                conn.executemany(
                    "INSERT OR IGNORE INTO comment_discovery_boundary(comment_id, created_at) VALUES (?, ?)",
                    [(str(item), newest_created_at) for item in boundary],
                )
            conn.execute(
                "INSERT INTO comment_discovery_state(singleton, initialized, newest_created_at, updated_at) VALUES (1, 1, ?, ?)"
                " ON CONFLICT(singleton) DO UPDATE SET initialized=1, newest_created_at=excluded.newest_created_at, updated_at=excluded.updated_at",
                (newest_created_at, timestamp),
            )
            conn.commit()

        await self._execute(operation)

    async def claim_comment(
        self,
        *,
        comment_id: str,
        blog_id: str,
        parent_id: str | None,
        source: str,
        requested_conversation_id: str | None,
        force_new_conversation: bool,
        observed_created_at: float | None,
        now: float,
        conversation_retention_seconds: int | None = None,
    ) -> CommentClaim:
        """在一个事务中完成评论 UUID 去重、会话归属和队列领取。"""

        retention = (
            self._conversation_retention_seconds
            if conversation_retention_seconds is None
            else conversation_retention_seconds
        )
        if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
            raise ValueError("conversation_retention_seconds 必须是正整数")

        def operation(conn: sqlite3.Connection) -> CommentClaim:
            existing = conn.execute(
                "SELECT conversation_id, status, source FROM comment_events WHERE comment_id = ?",
                (comment_id,),
            ).fetchone()
            if existing is not None:
                old_source = str(existing[2])
                merged = _merge_comment_sources(old_source, source)
                conn.execute(
                    "UPDATE comment_events SET source=?, updated_at=? WHERE comment_id=?",
                    (merged, now, comment_id),
                )
                if str(existing[1]) == "recover":
                    conn.execute(
                        "UPDATE comment_events SET status='queued', next_attempt_at=NULL, updated_at=? WHERE comment_id=? AND status='recover'",
                        (now, comment_id),
                    )
                    if existing[0] is not None:
                        conn.execute(
                            "UPDATE comment_conversations SET updated_at=? "
                            "WHERE conversation_id=?",
                            (now, existing[0]),
                        )
                    conn.commit()
                    return CommentClaim(True, comment_id, existing[0], "queued")
                conn.commit()
                return CommentClaim(False, comment_id, existing[0], str(existing[1]))

            conversation_id: str | None = None
            if not force_new_conversation and parent_id is not None:
                mapped = conn.execute(
                    "SELECT m.conversation_id FROM comment_messages AS m "
                    "JOIN comment_conversations AS c "
                    "ON c.conversation_id=m.conversation_id "
                    "WHERE m.comment_id=? AND m.role='bot' AND c.blog_id=? "
                    "AND c.updated_at > ?",
                    (parent_id, blog_id, now - retention),
                ).fetchone()
                if mapped is not None:
                    conversation_id = str(mapped[0])
            if conversation_id is None:
                conversation_id = (
                    comment_id if force_new_conversation else requested_conversation_id or comment_id
                )
            conn.execute(
                "INSERT INTO comment_conversations(conversation_id, blog_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
                "updated_at=excluded.updated_at",
                (conversation_id, blog_id, now, now),
            )
            conn.execute(
                "INSERT INTO comment_events(comment_id, blog_id, parent_id, conversation_id, source, status, observed_created_at, observed_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (comment_id, blog_id, parent_id, conversation_id, source, observed_created_at, now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO comment_messages(comment_id, conversation_id, role, parent_id, mapped_at) VALUES (?, ?, 'user', ?, ?)",
                (comment_id, conversation_id, parent_id, now),
            )
            conn.commit()
            return CommentClaim(True, comment_id, conversation_id, "queued")

        return await self._execute(operation)

    async def claim_comment_event(
        self,
        *,
        comment_id: str,
        blog_id: str,
        parent_id: str | None,
        source: str,
        requested_conversation_id: str | None,
        force_new_conversation: bool,
        observed_created_at: float | None,
        now: float,
        conversation_retention_seconds: int | None = None,
    ) -> CommentClaim:
        """兼容名称别名。"""
        return await self.claim_comment(
            comment_id=comment_id,
            blog_id=blog_id,
            parent_id=parent_id,
            source=source,
            requested_conversation_id=requested_conversation_id,
            force_new_conversation=force_new_conversation,
            observed_created_at=observed_created_at,
            now=now,
            conversation_retention_seconds=conversation_retention_seconds,
        )

    async def comment_event_status(self, comment_id: str) -> str | None:
        """读取评论事件状态。"""

        def operation(conn: sqlite3.Connection) -> str | None:
            row = conn.execute("SELECT status FROM comment_events WHERE comment_id=?", (comment_id,)).fetchone()
            return str(row[0]) if row is not None else None

        return await self._execute(operation)

    async def get_comment_event(self, comment_id: str) -> dict[str, object] | None:
        """读取评论事件的非敏感元数据。"""

        def operation(conn: sqlite3.Connection) -> dict[str, object] | None:
            row = conn.execute(
                "SELECT comment_id, blog_id, parent_id, conversation_id, source, status, observed_created_at, observed_at, updated_at, attempts, next_attempt_at FROM comment_events WHERE comment_id=?",
                (comment_id,),
            ).fetchone()
            if row is None:
                return None
            names = (
                "comment_id", "blog_id", "parent_id", "conversation_id", "source", "status",
                "observed_created_at", "observed_at", "updated_at", "attempts", "next_attempt_at",
            )
            return dict(zip(names, row))

        return await self._execute(operation)

    async def set_comment_event_status(
        self, comment_id: str, status: str, *, next_attempt_at: float | None = None, increment_attempt: bool = False, now: float | None = None
    ) -> None:
        """更新评论事件状态，不接触正文。"""
        timestamp = time.time() if now is None else now

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE comment_events SET status=?, next_attempt_at=?, updated_at=?, attempts=attempts+? WHERE comment_id=?",
                (status, next_attempt_at, timestamp, 1 if increment_attempt else 0, comment_id),
            )
            conn.commit()

        await self._execute(operation)

    async def mark_comment_handled(self, comment_id: str, status: str = "done", *, now: float | None = None) -> None:
        """评论事件状态更新的语义别名。"""
        await self.set_comment_event_status(comment_id, status, now=now)

    async def recover_comment_events(self) -> int:
        """把评论非终态任务统一转为 recover，供启动恢复使用。"""

        def operation(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE comment_events SET status='recover', updated_at=? WHERE status NOT IN ('done', 'baseline_ignored') AND status NOT LIKE 'skipped_%'",
                (time.time(),),
            )
            conn.commit()
            return int(cursor.rowcount)

        return await self._execute(operation)

    async def comment_events_due(self, *, now: float, limit: int = 50) -> list[tuple[str, str, str | None, str | None]]:
        """返回到期可重新入队的评论摘要。"""

        def operation(conn: sqlite3.Connection) -> list[tuple[str, str, str | None, str | None]]:
            rows = conn.execute(
                "SELECT comment_id, blog_id, parent_id, conversation_id FROM comment_events WHERE status IN ('waiting_queue','waiting_rate_limit','recover') AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY updated_at, comment_id LIMIT ?",
                (now, limit),
            ).fetchall()
            return [(str(a), str(b), c, d) for a, b, c, d in rows]

        return await self._execute(operation)

    async def comment_conversation_for_message(
        self,
        comment_id: str,
        *,
        now: float | None = None,
        retention_seconds: int | None = None,
    ) -> str | None:
        """查找活动评论消息归属；过期会话视为未命中。"""
        reference = time.time() if now is None else now
        retention = (
            self._conversation_retention_seconds
            if retention_seconds is None
            else retention_seconds
        )
        if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
            raise ValueError("retention_seconds 必须是正整数")

        def operation(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT m.conversation_id FROM comment_messages m JOIN comment_conversations c ON c.conversation_id=m.conversation_id WHERE m.comment_id=? AND c.updated_at > ?",
                (comment_id, reference - retention),
            ).fetchone()
            return str(row[0]) if row is not None else None

        return await self._execute(operation)

    async def find_comment_parent(
        self,
        comment_id: str,
        *,
        blog_id: str | None = None,
        now: float | None = None,
        retention_seconds: int | None = None,
    ) -> dict[str, str] | None:
        """返回父机器人评论的会话映射，不返回正文。"""
        reference = time.time() if now is None else now
        retention = (
            self._conversation_retention_seconds
            if retention_seconds is None
            else retention_seconds
        )
        if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
            raise ValueError("retention_seconds 必须是正整数")

        def operation(conn: sqlite3.Connection) -> dict[str, str] | None:
            sql = (
                "SELECT m.conversation_id, c.blog_id FROM comment_messages m "
                "JOIN comment_conversations c ON c.conversation_id=m.conversation_id "
                "WHERE m.comment_id=? AND m.role='bot' AND c.updated_at > ?"
            )
            params: list[object] = [comment_id, reference - retention]
            if blog_id is not None:
                sql += " AND c.blog_id=?"
                params.append(blog_id)
            row = conn.execute(sql, params).fetchone()
            return None if row is None else {"conversation_id": str(row[0]), "blog_id": str(row[1])}

        return await self._execute(operation)

    async def find_comment_conversation(
        self,
        comment_id: str,
        *,
        blog_id: str | None = None,
        now: float | None = None,
        retention_seconds: int | None = None,
    ) -> dict[str, str] | None:
        """父机器人评论映射别名。"""
        return await self.find_comment_parent(
            comment_id,
            blog_id=blog_id,
            now=now,
            retention_seconds=retention_seconds,
        )

    async def find_comment_mapping(
        self,
        comment_id: str,
        *,
        blog_id: str | None = None,
        now: float | None = None,
        retention_seconds: int | None = None,
    ) -> dict[str, str] | None:
        """父机器人评论映射别名。"""
        return await self.find_comment_parent(
            comment_id,
            blog_id=blog_id,
            now=now,
            retention_seconds=retention_seconds,
        )

    async def lookup_comment_message(
        self,
        comment_id: str,
        *,
        blog_id: str | None = None,
        now: float | None = None,
        retention_seconds: int | None = None,
    ) -> dict[str, str] | None:
        """父机器人评论映射别名。"""
        return await self.find_comment_parent(
            comment_id,
            blog_id=blog_id,
            now=now,
            retention_seconds=retention_seconds,
        )

    async def get_comment_message(
        self,
        comment_id: str,
        *,
        blog_id: str | None = None,
        now: float | None = None,
        retention_seconds: int | None = None,
    ) -> dict[str, str] | None:
        """父机器人评论映射别名。"""
        return await self.find_comment_parent(
            comment_id,
            blog_id=blog_id,
            now=now,
            retention_seconds=retention_seconds,
        )

    async def finalize_comment_send(
        self,
        trigger_comment_id: str,
        bot_comment_id: str,
        blog_id: str,
        conversation_id: str,
        *,
        kind: str = "reply",
        sent_at: float | None = None,
        reservation_token: str | None = None,
    ) -> None:
        """在同一事务中幂等写入 sent 映射、发送尝试和事件终态。"""
        del reservation_token
        timestamp = time.time() if sent_at is None else sent_at

        def operation(conn: sqlite3.Connection) -> None:
            try:
                write_finalize(conn)
            except BaseException:
                conn.rollback()
                raise

        def write_finalize(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO comment_conversations(conversation_id, blog_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, blog_id, timestamp, timestamp),
            )
            existing = conn.execute(
                "SELECT bot_comment_id, blog_id, conversation_id "
                "FROM comment_sent_replies WHERE trigger_comment_id=?",
                (trigger_comment_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT OR IGNORE INTO comment_sent_replies "
                    "(trigger_comment_id, bot_comment_id, blog_id, conversation_id, sent_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (trigger_comment_id, bot_comment_id, blog_id, conversation_id, timestamp),
                )
                existing = conn.execute(
                    "SELECT bot_comment_id, blog_id, conversation_id "
                    "FROM comment_sent_replies WHERE trigger_comment_id=?",
                    (trigger_comment_id,),
                ).fetchone()
                if existing is None:
                    raise sqlite3.IntegrityError("comment sent mapping conflict")
            effective_bot_id = str(existing[0])
            effective_blog_id = str(existing[1])
            effective_conversation_id = str(existing[2])
            conn.execute(
                "INSERT OR IGNORE INTO comment_conversations "
                "(conversation_id, blog_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (effective_conversation_id, effective_blog_id, timestamp, timestamp),
            )
            conn.execute(
                "INSERT OR IGNORE INTO comment_messages(comment_id, conversation_id, role, parent_id, mapped_at) VALUES (?, ?, 'bot', ?, ?)",
                (effective_bot_id, effective_conversation_id, trigger_comment_id, timestamp),
            )
            conn.execute(
                "UPDATE comment_conversations SET updated_at=? WHERE conversation_id=?",
                (timestamp, effective_conversation_id),
            )
            attempt = conn.execute(
                "SELECT id FROM comment_send_attempts "
                "WHERE trigger_comment_id=? AND kind=? ORDER BY id LIMIT 1",
                (trigger_comment_id, kind),
            ).fetchone()
            if attempt is None:
                conn.execute(
                    "INSERT INTO comment_send_attempts "
                    "(blog_id, trigger_comment_id, kind, attempted_at) VALUES (?, ?, ?, ?)",
                    (effective_blog_id, trigger_comment_id, kind, timestamp),
                )
            conn.execute(
                "UPDATE comment_events SET status='done', updated_at=? WHERE comment_id=?",
                (timestamp, trigger_comment_id),
            )
            conn.commit()

        await self._execute(operation)

    async def record_comment_sent(
        self,
        trigger_comment_id: str,
        bot_comment_id: str,
        blog_id: str,
        conversation_id: str,
        now: float | None = None,
        *,
        kind: str | None = None,
        sent_at: float | None = None,
        reservation_token: str | None = None,
    ) -> None:
        """兼容旧名称；转发到评论发送成功的原子终结接口。"""
        timestamp = sent_at if sent_at is not None else (time.time() if now is None else now)
        await self.finalize_comment_send(
            trigger_comment_id=trigger_comment_id,
            bot_comment_id=bot_comment_id,
            blog_id=blog_id,
            conversation_id=conversation_id,
            kind=kind or "reply",
            sent_at=timestamp,
            reservation_token=reservation_token,
        )

    async def find_comment_sent_for_trigger(self, trigger_comment_id: str) -> str | None:
        """按触发评论查机器人回复。"""

        def operation(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT bot_comment_id FROM comment_sent_replies WHERE trigger_comment_id=?",
                (trigger_comment_id,),
            ).fetchone()
            return str(row[0]) if row is not None else None

        return await self._execute(operation)

    async def find_comment_sent_reply(self, trigger_comment_id: str) -> str | None:
        """发送对账查询别名。"""
        return await self.find_comment_sent_for_trigger(trigger_comment_id)

    async def find_sent_comment_for_trigger(self, trigger_comment_id: str) -> str | None:
        """发送对账查询别名。"""
        return await self.find_comment_sent_for_trigger(trigger_comment_id)

    async def find_comment_reply(self, trigger_comment_id: str) -> str | None:
        """发送对账查询别名。"""
        return await self.find_comment_sent_for_trigger(trigger_comment_id)

    async def find_comment_conversation_for_bot_reply(self, bot_comment_id: str) -> tuple[str, str] | None:
        """按机器人评论查会话与文章。"""

        def operation(conn: sqlite3.Connection) -> tuple[str, str] | None:
            row = conn.execute(
                "SELECT conversation_id, blog_id FROM comment_sent_replies WHERE bot_comment_id=?",
                (bot_comment_id,),
            ).fetchone()
            return (str(row[0]), str(row[1])) if row is not None else None

        return await self._execute(operation)

    async def comment_bot_replies(self, blog_id: str) -> list[tuple[str, str, float]]:
        """返回文章内已知机器人评论的 id、会话和发送时刻。"""

        def operation(conn: sqlite3.Connection) -> list[tuple[str, str, float]]:
            rows = conn.execute(
                "SELECT bot_comment_id, conversation_id, sent_at FROM comment_sent_replies WHERE blog_id=? ORDER BY sent_at, bot_comment_id",
                (blog_id,),
            ).fetchall()
            return [(str(a), str(b), float(c)) for a, b, c in rows]

        return await self._execute(operation)

    async def known_bot_comment_ids(self, blog_id: str | None = None) -> set[str]:
        """返回本地已确认的机器人评论 UUID，供发现器粗筛。"""

        def operation(conn: sqlite3.Connection) -> set[str]:
            if blog_id is None:
                rows = conn.execute("SELECT bot_comment_id FROM comment_sent_replies").fetchall()
            else:
                rows = conn.execute(
                    "SELECT bot_comment_id FROM comment_sent_replies WHERE blog_id=?", (blog_id,)
                ).fetchall()
            return {str(row[0]) for row in rows}

        return await self._execute(operation)

    async def comment_bot_comment_ids(self, blog_id: str | None = None) -> set[str]:
        """机器人评论 id 查询别名。"""
        return await self.known_bot_comment_ids(blog_id)

    async def get_bot_comment_ids(self, blog_id: str | None = None) -> set[str]:
        """机器人评论 id 查询别名。"""
        return await self.known_bot_comment_ids(blog_id)

    async def comment_message_ids(self, conversation_id: str) -> list[str]:
        """返回会话映射中的评论 id；不返回正文。"""

        def operation(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT comment_id FROM comment_messages WHERE conversation_id=? ORDER BY mapped_at, comment_id",
                (conversation_id,),
            ).fetchall()
            return [str(row[0]) for row in rows]

        return await self._execute(operation)

    # --- 评论通知 ----------------------------------------------------------

    async def observe_comment_notification(
        self,
        notification_id: str,
        blog_id: str,
        actor_fingerprint: str | None,
        *,
        status: str = "observed",
        now: float | None = None,
    ) -> CommentNotificationState:
        """登记通知；不保存通知正文或原始 actor id。"""
        timestamp = time.time() if now is None else now

        def operation(conn: sqlite3.Connection) -> CommentNotificationState:
            conn.execute(
                "INSERT INTO comment_notifications(notification_id, blog_id, actor_fingerprint, status, observed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(notification_id) DO UPDATE SET blog_id=excluded.blog_id, actor_fingerprint=excluded.actor_fingerprint, updated_at=excluded.updated_at",
                (notification_id, blog_id, actor_fingerprint, status, timestamp, timestamp),
            )
            row = conn.execute(
                "SELECT status, unmatched_attempts FROM comment_notifications WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            conn.commit()
            return CommentNotificationState(notification_id, str(row[0]), int(row[1]))

        return await self._execute(operation)

    async def comment_notification_state(self, notification_id: str) -> CommentNotificationState | None:
        """读取通知状态摘要。"""

        def operation(conn: sqlite3.Connection) -> CommentNotificationState | None:
            row = conn.execute("SELECT status, unmatched_attempts FROM comment_notifications WHERE notification_id=?", (notification_id,)).fetchone()
            return None if row is None else CommentNotificationState(notification_id, str(row[0]), int(row[1]))

        return await self._execute(operation)

    async def add_comment_notification_candidate(self, notification_id: str, comment_id: str) -> None:
        """关联通知与候选评论 UUID。"""

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT OR IGNORE INTO comment_notification_candidates(notification_id, comment_id) VALUES (?, ?)", (notification_id, comment_id))
            conn.execute("UPDATE comment_notifications SET status='candidates_pending', updated_at=? WHERE notification_id=?", (time.time(), notification_id))
            conn.commit()

        await self._execute(operation)

    async def comment_notification_candidates(self, notification_id: str) -> list[str]:
        """返回通知关联的评论 UUID。"""

        def operation(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute("SELECT comment_id FROM comment_notification_candidates WHERE notification_id=? ORDER BY comment_id", (notification_id,)).fetchall()
            return [str(row[0]) for row in rows]

        return await self._execute(operation)

    async def increment_comment_unmatched(self, notification_id: str, *, now: float | None = None) -> int:
        """增加一次完整树匹配失败计数并返回新值。"""
        timestamp = time.time() if now is None else now

        def operation(conn: sqlite3.Connection) -> int:
            conn.execute("UPDATE comment_notifications SET unmatched_attempts=unmatched_attempts+1, status='matching', updated_at=? WHERE notification_id=?", (timestamp, notification_id))
            row = conn.execute("SELECT unmatched_attempts FROM comment_notifications WHERE notification_id=?", (notification_id,)).fetchone()
            conn.commit()
            return int(row[0]) if row is not None else 0

        return await self._execute(operation)

    async def set_comment_notification_status(self, notification_id: str, status: str, *, now: float | None = None) -> None:
        """更新通知生命周期状态。"""
        timestamp = time.time() if now is None else now

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE comment_notifications SET status=?, updated_at=? WHERE notification_id=?", (status, timestamp, notification_id))
            conn.commit()

        await self._execute(operation)

    async def record_comment_notification(
        self, notification_id: str, blog_id: str, actor_fingerprint: str | None, *, now: float | None = None
    ) -> CommentNotificationState:
        """通知登记的语义别名。"""
        return await self.observe_comment_notification(
            notification_id, blog_id, actor_fingerprint, now=now
        )

    async def mark_comment_notification(self, notification_id: str, status: str, *, now: float | None = None) -> None:
        """通知状态更新的语义别名。"""
        await self.set_comment_notification_status(notification_id, status, now=now)

    async def baseline_comment_notification(
        self, notification_id: str, blog_id: str, actor_fingerprint: str | None, *, now: float | None = None
    ) -> None:
        """登记冷启动时已存在的通知，不创建评论任务。"""
        await self.observe_comment_notification(notification_id, blog_id, actor_fingerprint, now=now)
        await self.set_comment_notification_status(notification_id, "baseline_ignored", now=now)

    async def comment_notification_baseline_done(self) -> bool:
        """读取通知冷启动基线是否已经完成。"""

        def operation(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT int_value FROM runtime_meta WHERE key='comment_notification_baseline'"
            ).fetchone()
            return bool(row and row[0])

        return await self._execute(operation)

    async def is_comment_notification_baseline_initialized(self) -> bool:
        """通知冷启动基线状态别名。"""
        return await self.comment_notification_baseline_done()

    async def set_comment_notification_baseline_done(self, *, now: float | None = None) -> None:
        """原子记录通知冷启动完成标志。"""

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO runtime_meta(key, int_value) VALUES ('comment_notification_baseline', 1) ON CONFLICT(key) DO UPDATE SET int_value=1"
            )
            conn.commit()

        await self._execute(operation)

    async def mark_comment_notification_baseline(self, *, now: float | None = None) -> None:
        """通知冷启动标记别名。"""
        await self.set_comment_notification_baseline_done(now=now)

    async def comment_notification_baseline_cutoff(self) -> tuple[float, str] | None:
        """读取通知冷启动快照边界；边界必须跨重启保留。"""

        def operation(conn: sqlite3.Connection) -> tuple[float, str] | None:
            row = conn.execute(
                "SELECT cutoff_timestamp, cutoff_notification_id "
                "FROM comment_notification_baseline_state WHERE singleton=1"
            ).fetchone()
            if row is None:
                return None
            return float(row[0]), str(row[1])

        return await self._execute(operation)

    async def get_comment_notification_baseline_cutoff(self) -> tuple[float, str] | None:
        """通知冷启动快照边界查询别名。"""
        return await self.comment_notification_baseline_cutoff()

    async def set_comment_notification_baseline_cutoff(
        self,
        *,
        timestamp: float,
        notification_id: str,
        cutoff: tuple[float, str] | None = None,
        now: float | None = None,
    ) -> None:
        """原子保存通知冷启动快照边界。"""
        del cutoff
        updated_at = time.time() if now is None else now

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO comment_notification_baseline_state "
                "(singleton, cutoff_timestamp, cutoff_notification_id, updated_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "cutoff_timestamp=excluded.cutoff_timestamp, "
                "cutoff_notification_id=excluded.cutoff_notification_id, "
                "updated_at=excluded.updated_at",
                (float(timestamp), notification_id, updated_at),
            )
            conn.commit()

        await self._execute(operation)

    async def record_comment_notification_baseline_cutoff(
        self,
        *,
        timestamp: float,
        notification_id: str,
        cutoff: tuple[float, str] | None = None,
        now: float | None = None,
    ) -> None:
        """通知冷启动快照边界写入别名。"""
        await self.set_comment_notification_baseline_cutoff(
            timestamp=timestamp,
            notification_id=notification_id,
            cutoff=cutoff,
            now=now,
        )

    async def comment_notification_candidates_terminal(self, notification_id: str) -> bool:
        """只有通知关联的所有候选进入终态时才允许标已读。"""

        def operation(conn: sqlite3.Connection) -> bool:
            rows = conn.execute(
                "SELECT e.status FROM comment_notification_candidates AS n "
                "LEFT JOIN comment_events AS e ON e.comment_id=n.comment_id "
                "WHERE n.notification_id=?",
                (notification_id,),
            ).fetchall()
            if not rows:
                return False
            return all(
                row[0] is not None
                and (str(row[0]) == "done" or str(row[0]).startswith("skipped_"))
                for row in rows
            )

        return await self._execute(operation)

    async def notification_candidates_terminal(self, notification_id: str) -> bool:
        """候选终态查询别名。"""
        return await self.comment_notification_candidates_terminal(notification_id)

    async def can_mark_comment_notification_read(self, notification_id: str) -> bool:
        """候选终态查询别名。"""
        return await self.comment_notification_candidates_terminal(notification_id)

    # --- 评论发送尝试与配额 -----------------------------------------------

    async def record_comment_send_attempt(self, blog_id: str, trigger_comment_id: str | None, kind: str, *, attempted_at: float | None = None) -> int:
        """记录评论发送尝试；同一触发评论只转正一次。"""
        timestamp = time.time() if attempted_at is None else attempted_at

        def operation(conn: sqlite3.Connection) -> int:
            # Store 的单连接锁使「查找并插入」保持原子；通知类发送没有触发评论，
            # 因而每次都应计入总配额。
            if trigger_comment_id is not None:
                existing = conn.execute(
                    "SELECT id FROM comment_send_attempts "
                    "WHERE blog_id=? AND trigger_comment_id=? AND kind=? "
                    "ORDER BY id LIMIT 1",
                    (blog_id, trigger_comment_id, kind),
                ).fetchone()
                if existing is not None:
                    return int(existing[0])
            cursor = conn.execute("INSERT INTO comment_send_attempts(blog_id, trigger_comment_id, kind, attempted_at) VALUES (?, ?, ?, ?)", (blog_id, trigger_comment_id, kind, timestamp))
            conn.commit()
            return int(cursor.lastrowid)

        return await self._execute(operation)

    async def comment_send_attempt_exists(
        self, blog_id: str, trigger_comment_id: str, kind: str
    ) -> bool:
        """查询触发评论是否已经转正；不返回正文或其他用户字段。"""

        def operation(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT 1 FROM comment_send_attempts "
                "WHERE blog_id=? AND trigger_comment_id=? AND kind=? LIMIT 1",
                (blog_id, trigger_comment_id, kind),
            ).fetchone()
            return row is not None

        return await self._execute(operation)

    async def count_comment_sends_since(self, since: float, *, kind: str | None = None, blog_id: str | None = None) -> int:
        """统计评论发送尝试，可按 kind 和文章过滤。"""

        def operation(conn: sqlite3.Connection) -> int:
            sql = "SELECT COUNT(*) FROM comment_send_attempts WHERE attempted_at >= ?"
            params: list[object] = [since]
            if kind is not None:
                sql += " AND kind=?"
                params.append(kind)
            if blog_id is not None:
                sql += " AND blog_id=?"
                params.append(blog_id)
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row else 0

        return await self._execute(operation)

    async def latest_comment_send_at(self, blog_id: str, *, since: float | None = None) -> float | None:
        """读取文章最近一次评论发送时间。"""

        def operation(conn: sqlite3.Connection) -> float | None:
            if since is None:
                row = conn.execute("SELECT MAX(attempted_at) FROM comment_send_attempts WHERE blog_id=?", (blog_id,)).fetchone()
            else:
                row = conn.execute("SELECT MAX(attempted_at) FROM comment_send_attempts WHERE blog_id=? AND attempted_at >= ?", (blog_id, since)).fetchone()
            return None if row is None or row[0] is None else float(row[0])

        return await self._execute(operation)

    # --- 已发回复 -----------------------------------------------------------

    async def record_sent(
        self,
        message_id: int,
        channel_id: str,
        reply_to: int | None,
        *,
        thread_root_id: int | None = None,
    ) -> None:
        """记录一条已成功发出的站内消息，供「结果不确定」时对账去重。

        `thread_root_id` 非空时，**同一个事务**里把这条出站消息登记进共享链并刷新链的
        活动时间（INTERFACES §9.4）：出站消息是后续加入该链的锚点，映射与去重记录必须
        一起成功或一起失败。
        """

        def operation(conn: sqlite3.Connection) -> None:
            sent_at = time.time()
            conn.execute(
                "INSERT INTO sent_replies (message_id, channel_id, reply_to, sent_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(message_id) DO UPDATE SET"
                " channel_id = excluded.channel_id,"
                " reply_to = excluded.reply_to,"
                " sent_at = excluded.sent_at",
                (message_id, channel_id, reply_to, sent_at),
            )
            if thread_root_id is not None:
                conn.execute(
                    "INSERT INTO lobby_thread_messages(message_id, thread_root_id, mapped_at)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(message_id) DO UPDATE SET"
                    " thread_root_id = excluded.thread_root_id,"
                    " mapped_at = excluded.mapped_at",
                    (message_id, thread_root_id, sent_at),
                )
                conn.execute(
                    "UPDATE lobby_threads SET updated_at = ? WHERE thread_root_id = ?",
                    (sent_at, thread_root_id),
                )
            conn.commit()

        await self._execute(operation)

    # --- 大区共享链 ---------------------------------------------------------

    async def resolve_lobby_thread(
        self,
        message_id: int,
        reply_to: int | None,
        *,
        force_new: bool,
        now: float,
        retention_seconds: int,
    ) -> int:
        """命中活动链或新建链，登记 `message_id`，返回 `thread_root_id`。

        整体在**一个事务**内完成「查目标链是否活动 → 建链或命中 → 登记本条 → 刷新时间」，
        两个并发回复因此不会各自建出一条链（INTERFACES §9.3）。
        活动判据是开区间：`updated_at > now - retention_seconds`。
        """

        def operation(conn: sqlite3.Connection) -> int:
            root = message_id
            if not force_new and reply_to is not None:
                row = conn.execute(
                    "SELECT thread_root_id FROM lobby_thread_messages WHERE message_id = ?",
                    (reply_to,),
                ).fetchone()
                if row is not None:
                    candidate = int(row[0])
                    active = conn.execute(
                        "SELECT 1 FROM lobby_threads"
                        " WHERE thread_root_id = ? AND updated_at > ?",
                        (candidate, now - retention_seconds),
                    ).fetchone()
                    if active is not None:
                        root = candidate

            conn.execute(
                "INSERT OR IGNORE INTO lobby_threads(thread_root_id, created_at, updated_at)"
                " VALUES (?, ?, ?)",
                (root, now, now),
            )
            conn.execute(
                "INSERT INTO lobby_thread_messages(message_id, thread_root_id, mapped_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(message_id) DO UPDATE SET"
                " thread_root_id = excluded.thread_root_id,"
                " mapped_at = excluded.mapped_at",
                (message_id, root, now),
            )
            conn.execute(
                "UPDATE lobby_threads SET updated_at = ? WHERE thread_root_id = ?",
                (now, root),
            )
            conn.commit()
            return root

        return await self._execute(operation)

    async def prune_runtime_state(self, *, now: float, cfg: StorageConfig) -> CleanupResult:
        """清理过期运行状态并推进安全水位；**整体是一个事务**（INTERFACES §9.4）。

        失败即整体回滚：宁可这一轮什么也没清，也不留下「删了一半」的库。
        提交后再做一次被动 WAL 检查点；运行期**不做 `VACUUM`**（会长时间独占库锁）。
        """

        def operation(conn: sqlite3.Connection) -> CleanupResult:
            try:
                result = _prune(
                    conn,
                    now,
                    cfg,
                    comment_conversation_retention_seconds=(
                        self._conversation_retention_seconds
                    ),
                    comment_dedupe_retention_seconds=self._dedupe_retention_seconds,
                )
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            return result

        return await self._execute(operation)

    async def find_active_lobby_thread(
        self, message_id: int, *, now: float, retention_seconds: int
    ) -> int | None:
        """只读：`message_id` 属于活动链时返回其根，否则 None（过期等同未命中）。"""

        def operation(conn: sqlite3.Connection) -> int | None:
            row = conn.execute(
                "SELECT m.thread_root_id FROM lobby_thread_messages AS m"
                " JOIN lobby_threads AS t ON t.thread_root_id = m.thread_root_id"
                " WHERE m.message_id = ? AND t.updated_at > ?",
                (message_id, now - retention_seconds),
            ).fetchone()
            return int(row[0]) if row is not None else None

        return await self._execute(operation)

    async def attach_lobby_message(
        self, message_id: int, thread_root_id: int, *, now: float
    ) -> bool:
        """把一条出站或回显消息登记进链并刷新活动时间；链不存在时返回 False。

        幂等：重复登记同一条消息只覆盖归属与时间，不报错。
        """

        def operation(conn: sqlite3.Connection) -> bool:
            exists = conn.execute(
                "SELECT 1 FROM lobby_threads WHERE thread_root_id = ?",
                (thread_root_id,),
            ).fetchone()
            if exists is None:
                return False
            conn.execute(
                "INSERT INTO lobby_thread_messages(message_id, thread_root_id, mapped_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(message_id) DO UPDATE SET"
                " thread_root_id = excluded.thread_root_id,"
                " mapped_at = excluded.mapped_at",
                (message_id, thread_root_id, now),
            )
            conn.execute(
                "UPDATE lobby_threads SET updated_at = ? WHERE thread_root_id = ?",
                (now, thread_root_id),
            )
            conn.commit()
            return True

        return await self._execute(operation)

    async def find_sent_for_reply(self, channel_id: str, reply_to: int) -> int | None:
        """查该频道里是否已有针对 `reply_to` 的已发消息；未命中返回 None。"""

        def operation(conn: sqlite3.Connection) -> int | None:
            row = conn.execute(
                "SELECT message_id FROM sent_replies"
                " WHERE channel_id = ? AND reply_to = ?"
                " ORDER BY sent_at DESC LIMIT 1",
                (channel_id, reply_to),
            ).fetchone()
            return int(row[0]) if row is not None else None

        return await self._execute(operation)

    # --- 发送尝试与配额 -----------------------------------------------------

    async def record_send_attempt(
        self, channel_id: str, reply_to: int | None, kind: str
    ) -> int:
        """写一条发送尝试（kind: "reply" | "notice" | "notice_local"），返回自增主键。"""

        def operation(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO send_attempts (channel_id, reply_to, kind, attempted_at)"
                " VALUES (?, ?, ?, ?)",
                (channel_id, reply_to, kind, time.time()),
            )
            conn.commit()
            return int(cursor.lastrowid)

        return await self._execute(operation)

    async def count_sends_since(self, since: float, kind: str | None = None) -> int:
        """统计 `attempted_at >= since` 的发送尝试数；`kind` 为 None 时不过滤。"""

        def operation(conn: sqlite3.Connection) -> int:
            sql = "SELECT COUNT(*) FROM send_attempts WHERE attempted_at >= ?"
            params: list[object] = [since]
            if kind is not None:
                sql += " AND kind = ?"
                params.append(kind)
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row is not None else 0

        return await self._execute(operation)

    # --- 冷却 ---------------------------------------------------------------

    async def get_cooldown(self, key: str, *, now: float | None = None) -> float | None:
        """未过期时返回到期时间戳，否则返回 None（过期即视为无）。

        `now` 供调用方注入可比对的时间（`quota` 用可注入时钟，测试里要与它同一时间轴）；
        省略时用真实时间，与 `set_cooldown` 的写入口径一致。
        """

        reference = time.time() if now is None else now

        def operation(conn: sqlite3.Connection) -> float | None:
            row = conn.execute(
                "SELECT until FROM cooldowns WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            until = float(row[0])
            return until if until > reference else None

        return await self._execute(operation)

    async def set_cooldown(self, key: str, until: float) -> None:
        """写入/覆盖一条冷却记录（幂等）。"""

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO cooldowns (key, until) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET until = excluded.until",
                (key, until),
            )
            conn.commit()

        await self._execute(operation)

    # --- 定时发文：调度执行与投递（INTERFACES §53.4）------------------------
    #
    # 这一组的 `now` 一律由调用方传入，SQL 里不读真实时钟；UTC+8 日期只从
    # `blog_records` 取，不在这里另写一份日历。所有多步骤写操作都是显式事务，
    # 整体在一次 `_execute()` 里跑完，因此天然串行、天然原子（D-90）。

    async def get_blog_run(
        self, scope: BlogScope, task_name: str, scheduled_at: float
    ) -> BlogRun | None:
        """按执行键 `(site_base_url, self_user_id, task_name, scheduled_at)` 读一次调度执行。

        扫描时先查这里：**命中就跳过，绝不重新掷骰**（设计 §6.1）。本方法不插入、不改状态。
        """

        def operation(conn: sqlite3.Connection) -> BlogRun | None:
            row = conn.execute(
                f"SELECT {_BLOG_RUN_COLUMNS} FROM blog_runs"
                " WHERE site_base_url = ? AND self_user_id = ?"
                " AND task_name = ? AND scheduled_at = ?",
                (scope.site_base_url, scope.self_user_id, task_name, scheduled_at),
            ).fetchone()
            return None if row is None else _row_to_blog_run(row)

        return await self._execute(operation)

    async def claim_blog_run(
        self,
        scope: BlogScope,
        candidate: RunCandidate,
        selected: int,
        now: float,
    ) -> BlogRun | None:
        """原子领取一个调度点；同键已存在时返回 None。

        `selected` 是本次掷骰的结果（must 恒为 1），由调用方在**确认执行键不存在之后**
        取一次随机值得到。唯一约束是最后一道防线：重复扫描、时钟回拨、两个进程同时启动
        都只能有一个调用方插入成功，且**不覆盖**已经落库的随机决策（设计 §6.1）。
        冲突时返回 None，而不是抛异常 —— 这不是错误，是正常的防重命中。
        """
        if selected not in (0, 1):
            raise ValueError("selected 必须是 0 或 1")

        key = (
            scope.site_base_url,
            scope.self_user_id,
            candidate.task_name,
            candidate.scheduled_at,
        )

        def operation(conn: sqlite3.Connection) -> BlogRun | None:
            try:
                conn.execute(
                    "INSERT INTO blog_runs (site_base_url, self_user_id, task_name,"
                    " scheduled_at, task_order, selected, status, post_id,"
                    " created_at, updated_at, reason)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)",
                    (*key, candidate.task_order, selected, RUN_QUEUED, now, now),
                )
            except sqlite3.IntegrityError:
                # 只有唯一键冲突才是「已领取」；别的完整性错误必须原样抛出，
                # 否则一个写坏的字段会被伪装成正常的防重命中。
                conn.rollback()
                existing = conn.execute(
                    f"SELECT {_BLOG_RUN_COLUMNS} FROM blog_runs"
                    " WHERE site_base_url = ? AND self_user_id = ?"
                    " AND task_name = ? AND scheduled_at = ?",
                    key,
                ).fetchone()
                if existing is None:
                    raise
                return None
            row = conn.execute(
                f"SELECT {_BLOG_RUN_COLUMNS} FROM blog_runs"
                " WHERE site_base_url = ? AND self_user_id = ?"
                " AND task_name = ? AND scheduled_at = ?",
                key,
            ).fetchone()
            conn.commit()
            if row is None:
                raise RuntimeError("blog_runs 插入成功却读不回这一行")
            return _row_to_blog_run(row)

        return await self._execute(operation)

    async def take_blog_run(self, scope: BlogScope, now: float) -> BlogRun | None:
        """领取本账号最早的一条 `queued` 执行，原子转 `running`；没有可执行的返回 None。

        积压超过 5 分钟（`SCAN_WINDOW_SECONDS`）还没开始的点在**同一事务**里转成
        `skipped` + reason=`misfire` 后继续看下一行：停机一整天后一次性补发几十篇文章，
        比漏发危险得多（设计 §6.1）。已经开始的任务允许跨分钟完成。
        没有可执行的行时提交这些 misfire 记账再返回 None。
        """

        def operation(conn: sqlite3.Connection) -> BlogRun | None:
            while True:
                row = conn.execute(
                    f"SELECT {_BLOG_RUN_COLUMNS} FROM blog_runs"
                    " WHERE site_base_url = ? AND self_user_id = ? AND status = ?"
                    " ORDER BY scheduled_at, task_order, id LIMIT 1",
                    (scope.site_base_url, scope.self_user_id, RUN_QUEUED),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                run = _row_to_blog_run(row)
                if now - run.scheduled_at > SCAN_WINDOW_SECONDS:
                    conn.execute(
                        "UPDATE blog_runs SET status = ?, reason = ?, updated_at = ?"
                        " WHERE id = ? AND status = ?",
                        (RUN_SKIPPED, REASON_MISFIRE, now, run.id, RUN_QUEUED),
                    )
                    continue
                cursor = conn.execute(
                    "UPDATE blog_runs SET status = ?, updated_at = ?"
                    " WHERE id = ? AND status = ?",
                    (RUN_RUNNING, now, run.id, RUN_QUEUED),
                )
                if cursor.rowcount != 1:
                    # 连接由 Store 串行持有，这只可能是有人绕过 Store 直接改库；
                    # 重新选一次即可，不必把整个服务拖停。
                    conn.rollback()
                    continue
                updated = conn.execute(
                    f"SELECT {_BLOG_RUN_COLUMNS} FROM blog_runs WHERE id = ?",
                    (run.id,),
                ).fetchone()
                conn.commit()
                if updated is None:
                    raise RuntimeError("blog_runs 更新成功却读不回这一行")
                return _row_to_blog_run(updated)

        return await self._execute(operation)

    async def finish_blog_run(
        self,
        scope: BlogScope,
        run_id: int,
        status: str,
        reason: str | None,
        now: float,
    ) -> None:
        """给一次执行落终态；只接受 `RUN_TERMINAL_STATUSES` 里的取值。

        **不得**碰投递状态与额度：POST 之后运行记录的终态不能替代投递状态（§53.11）。
        已经终结的行不再改写，因此重复调用（或账号/作用域不符）会抛 `KeyError` ——
        把调用方的状态机错误暴露出来，而不是静默吞掉。
        """
        if status not in RUN_TERMINAL_STATUSES:
            raise ValueError(f"不是合法的执行终态：{status}")
        placeholders = ", ".join("?" for _ in RUN_TERMINAL_STATUSES)

        def operation(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "UPDATE blog_runs SET status = ?, reason = ?, updated_at = ?"
                " WHERE id = ? AND site_base_url = ? AND self_user_id = ?"
                f" AND status NOT IN ({placeholders})",
                (status, reason, now, run_id, scope.site_base_url, scope.self_user_id,
                 *RUN_TERMINAL_STATUSES),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise KeyError(f"blog_run {run_id} 不存在、不属于当前账号或已是终态")
            conn.commit()

        await self._execute(operation)

    async def find_blog_post(self, scope: BlogScope, content_hash: str) -> BlogPost | None:
        """按账号 + 内容指纹查投递行；没有记录返回 None。

        没有记录只表示「这个指纹还没投过」，**不是**「已发布」（设计 §7.4）：
        记录可能是 inflight/unconfirmed/retry_wait/rejected/abandoned。
        """

        def operation(conn: sqlite3.Connection) -> BlogPost | None:
            row = conn.execute(
                f"SELECT {_BLOG_POST_COLUMNS} FROM blog_posts"
                " WHERE site_base_url = ? AND self_user_id = ? AND content_hash = ?",
                (scope.site_base_url, scope.self_user_id, content_hash),
            ).fetchone()
            return None if row is None else _row_to_blog_post(row)

        return await self._execute(operation)

    async def blog_budget_used(self, scope: BlogScope, day: str) -> int:
        """`day`（UTC+8 `YYYY-MM-DD`）当天已占用的发文额度（设计 §9、D-108）。

        当天占用 = 当天计费闭区间覆盖的 published 行数 + **全部** inflight/unconfirmed
        行数。不确定行跨日**持续占 1**，不能零点释放：零点一放额度，「结果未知」就变成了
        「从未发生」，而那正是重复发布的入口。429 等确定未发布的行已清空计费区间，不占。
        """
        return await self._execute(lambda conn: _blog_budget_used(conn, scope, day))

    async def reserve_blog_post(
        self,
        scope: BlogScope,
        run_id: int,
        title: str,
        content_hash: str,
        hash_version: int,
        source_kind: str,
        category_id: int | None,
        max_posts_per_day: int,
        now: float,
    ) -> BlogReservation:
        """在**一个事务里**复查并预留一篇的额度；成功才返回 allowed 与投递行。

        复查顺序（设计 §6.2 第 5 步、§7.4）：执行行仍属于本账号且处于 `running` → 指纹行的
        六种状态 → 429 重试的四项条件 → 当天预算。通过后创建或复用投递行、`attempts += 1`、
        并把 `blog_runs.post_id` 关联到它。**只有 `take_blog_run` 领过的执行**才允许走到这里：
        一次执行必须先被领取才产生投递副作用，`queued` 行能预留就等于绕开了「一次只有一篇在跑」
        的那道闸（§53.4）。

        拒绝时 `post` 必须是 None（不持有额度就不能顺手带出行快照）。**写库失败一律抛异常**，
        绝不伪装成 `budget_exhausted` —— 那会让服务以为「今天额度用完了」继续跑，
        而实际上一条记录都没落下。
        """
        day = utc8_day(now)

        def operation(conn: sqlite3.Connection) -> BlogReservation:
            row = conn.execute(
                f"SELECT {_BLOG_RUN_COLUMNS} FROM blog_runs"
                " WHERE id = ? AND site_base_url = ? AND self_user_id = ?",
                (run_id, scope.site_base_url, scope.self_user_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"blog_run {run_id} 不存在或不属于当前账号")
            run = _row_to_blog_run(row)
            if run.status != RUN_RUNNING:
                # 只有 take 过的 run 才能产生投递副作用：终态的执行不许补发，
                # queued 的执行不许绕过「一次只有一篇在跑」的那道闸（§53.4）。
                raise KeyError(
                    f"blog_run {run_id} 不是 running，不能预留：{run.status}"
                )

            existing = conn.execute(
                f"SELECT {_BLOG_POST_COLUMNS} FROM blog_posts"
                " WHERE site_base_url = ? AND self_user_id = ? AND content_hash = ?",
                (scope.site_base_url, scope.self_user_id, content_hash),
            ).fetchone()
            post = None if existing is None else _row_to_blog_post(existing)

            if post is not None:
                refused = _blog_refusal_reason(
                    post, run=run, category_id=category_id, day=day
                )
                if refused is not None:
                    return BlogReservation(allowed=False, post=None, reason=refused)

            if _blog_budget_used(conn, scope, day) >= max_posts_per_day:
                return BlogReservation(
                    allowed=False, post=None, reason=REASON_BUDGET_EXHAUSTED
                )

            if post is None:
                cursor = conn.execute(
                    "INSERT INTO blog_posts (site_base_url, self_user_id, task_name,"
                    " source_kind, category_id, content_hash, hash_version, title, status,"
                    " site_blog_id, attempts, retry_after_day, budget_from_day,"
                    " charged_through_day, reconcile_attempts, last_reconciled_at,"
                    " created_at, updated_at, confirmed_at, reason)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, NULL, ?, NULL, 0, NULL,"
                    " ?, ?, NULL, NULL)",
                    (
                        scope.site_base_url,
                        scope.self_user_id,
                        run.task_name,
                        source_kind,
                        category_id,
                        content_hash,
                        hash_version,
                        title,
                        STATUS_INFLIGHT,
                        day,
                        now,
                        now,
                    ),
                )
                post_id = int(cursor.lastrowid)
            else:
                # 复用 429 后的原行：以新尝试日期为计费起点，清空旧的计费区间与重试日
                # （D-108）。标题不重写：指纹相同意味着它必然与落库时一致。
                cursor = conn.execute(
                    "UPDATE blog_posts SET status = ?, attempts = attempts + 1,"
                    " budget_from_day = ?, charged_through_day = NULL,"
                    " retry_after_day = NULL, reason = NULL, updated_at = ?"
                    " WHERE id = ? AND status = ?",
                    (STATUS_INFLIGHT, day, now, post.id, STATUS_RETRY_WAIT),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise RuntimeError(f"blog_post {post.id} 的 retry_wait 复用失败")
                post_id = post.id

            conn.execute(
                "UPDATE blog_runs SET post_id = ?, updated_at = ? WHERE id = ?",
                (post_id, now, run.id),
            )
            reserved = conn.execute(
                f"SELECT {_BLOG_POST_COLUMNS} FROM blog_posts WHERE id = ?", (post_id,)
            ).fetchone()
            conn.commit()
            if reserved is None:
                raise RuntimeError("blog_posts 写入成功却读不回这一行")
            # 成功与拒绝都不留空串：`REASON_RESERVED` 是「拿到了额度」的稳定 token（§53.4）。
            return BlogReservation(
                allowed=True, post=_row_to_blog_post(reserved), reason=REASON_RESERVED
            )

        return await self._execute(operation)

    async def finalize_blog_post(
        self,
        scope: BlogScope,
        post_id: int,
        status: str,
        site_blog_id: str | None,
        reason: str | None,
        now: float,
    ) -> BlogPost:
        """按设计 §7.2 的迁移终结一次投递；状态与额度在**同一事务**里变更。

        `retry_after_day` 与两个计费日期由本方法按 `now` 自己推导，**不接受调用方填值**：
        时钟口径只能有一处，否则「跨日多占」这条保守设计会被调用方悄悄绕过。

        - `published`：`charged_through_day` 取本地确认日（时钟回退时至少为起始日），
          计费区间是 `[budget_from_day, charged_through_day]` 的闭区间（D-108）；
          重复确认幂等，**不重新计费、不延长区间**。
        - `unconfirmed`：维持占额与计费区间不动。
        - `rejected` / `retry_wait` / `abandoned`：确定未发布，清空本次计费区间；
          `retry_wait` 额外把 `retry_after_day` 记为下一个 UTC+8 日期。
        """
        if status not in POST_STATUSES:
            raise ValueError(f"不是合法的投递状态：{status}")
        if status == STATUS_PUBLISHED:
            if not isinstance(site_blog_id, str) or not site_blog_id:
                raise ValueError("published 必须带非空 site_blog_id")
        elif site_blog_id is not None:
            raise ValueError("只有 published 可以带 site_blog_id")

        def operation(conn: sqlite3.Connection) -> BlogPost:
            row = conn.execute(
                f"SELECT {_BLOG_POST_COLUMNS} FROM blog_posts"
                " WHERE id = ? AND site_base_url = ? AND self_user_id = ?",
                (post_id, scope.site_base_url, scope.self_user_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"blog_post {post_id} 不存在或不属于当前账号")
            post = _row_to_blog_post(row)
            allowed = _BLOG_POST_TRANSITIONS.get(post.status, frozenset())
            if status not in allowed:
                raise ValueError(
                    f"blog_post {post_id}：{post.status} -> {status} 不是合法迁移"
                )

            if status == STATUS_PUBLISHED and post.status == STATUS_PUBLISHED:
                if site_blog_id != post.site_blog_id:
                    raise ValueError(
                        f"blog_post {post_id} 重复确认的 site_blog_id 与已存记录不一致"
                    )
                # 幂等：连 updated_at 都不动，重复确认不产生任何计费副作用。
                return post

            day = utc8_day(now)
            if status == STATUS_PUBLISHED:
                charged_through = day
                if post.budget_from_day is not None and post.budget_from_day > day:
                    # 时钟回退：确认日不得早于起始日，否则区间会变成倒置的空集。
                    charged_through = post.budget_from_day
                conn.execute(
                    "UPDATE blog_posts SET status = ?, site_blog_id = ?,"
                    " charged_through_day = ?, confirmed_at = ?, reason = ?, updated_at = ?"
                    " WHERE id = ?",
                    (STATUS_PUBLISHED, site_blog_id, charged_through, now, reason, now, post_id),
                )
            elif status == STATUS_UNCONFIRMED:
                # 结果不确定：保留占额与计费区间，只更新诊断字段。
                conn.execute(
                    "UPDATE blog_posts SET status = ?, reason = ?, updated_at = ?"
                    " WHERE id = ?",
                    (STATUS_UNCONFIRMED, reason, now, post_id),
                )
            else:
                # 确定未发布：释放预留，清空本次计费区间；稿库下次尝试重新预留。
                retry_after = utc8_next_day(day) if status == STATUS_RETRY_WAIT else None
                conn.execute(
                    "UPDATE blog_posts SET status = ?, reason = ?, retry_after_day = ?,"
                    " budget_from_day = NULL, charged_through_day = NULL, updated_at = ?"
                    " WHERE id = ?",
                    (status, reason, retry_after, now, post_id),
                )

            updated = conn.execute(
                f"SELECT {_BLOG_POST_COLUMNS} FROM blog_posts WHERE id = ?", (post_id,)
            ).fetchone()
            conn.commit()
            if updated is None:
                raise RuntimeError("blog_posts 更新成功却读不回这一行")
            return _row_to_blog_post(updated)

        return await self._execute(operation)

    async def recover_blog_state(
        self, scope: BlogScope, now: float
    ) -> BlogRecoverySummary:
        """启动恢复：`inflight` → `unconfirmed`，`queued`/`running` → `interrupted`。

        **保留额度**：不确定行照旧占 1（D-109）。这里不调用模型、不访问站点、不重新生成
        任何东西 —— 上一进程 POST 到一半的窗口无法在本地消除，只能对账（D-107）。
        运行记录与投递行各自恢复，不在这里重建两者的关联。
        """

        def operation(conn: sqlite3.Connection) -> BlogRecoverySummary:
            unconfirmed = conn.execute(
                "UPDATE blog_posts SET status = ?, updated_at = ?"
                " WHERE site_base_url = ? AND self_user_id = ? AND status = ?",
                (STATUS_UNCONFIRMED, now, scope.site_base_url, scope.self_user_id, STATUS_INFLIGHT),
            ).rowcount
            interrupted = conn.execute(
                "UPDATE blog_runs SET status = ?, reason = ?, updated_at = ?"
                " WHERE site_base_url = ? AND self_user_id = ? AND status IN (?, ?)",
                (RUN_INTERRUPTED, REASON_INTERRUPTED, now, scope.site_base_url,
                 scope.self_user_id, RUN_QUEUED, RUN_RUNNING),
            ).rowcount
            conn.commit()
            return BlogRecoverySummary(
                unconfirmed=int(unconfirmed), interrupted=int(interrupted)
            )

        return await self._execute(operation)

    async def blog_posts_to_reconcile(
        self, scope: BlogScope, now: float, limit: int = 10
    ) -> tuple[BlogPost, ...]:
        """本轮只读对账要处理的行：本账号的 `unconfirmed`，按公平轮转取最多 `limit` 条。

        排除已查满 `MAX_RECONCILE_ATTEMPTS` 次的行 —— 它们**仍然占额**，只是不再自动查询
        （设计 §7.3）。排序是 `last_reconciled_at` NULL 优先、然后最久没查的、然后 id：
        每次查询会把 `last_reconciled_at` 推到现在，于是下一轮自然轮到后面的记录，
        不会饿死。`now` 只为与其余方法保持同一签名口径，筛选本身不读时钟。
        """

        def operation(conn: sqlite3.Connection) -> tuple[BlogPost, ...]:
            rows = conn.execute(
                f"SELECT {_BLOG_POST_COLUMNS} FROM blog_posts"
                " WHERE site_base_url = ? AND self_user_id = ? AND status = ?"
                " AND reconcile_attempts < ?"
                " ORDER BY (last_reconciled_at IS NOT NULL), last_reconciled_at, id"
                " LIMIT ?",
                (
                    scope.site_base_url,
                    scope.self_user_id,
                    STATUS_UNCONFIRMED,
                    MAX_RECONCILE_ATTEMPTS,
                    limit,
                ),
            ).fetchall()
            return tuple(_row_to_blog_post(row) for row in rows)

        return await self._execute(operation)

    async def note_blog_reconcile(
        self, scope: BlogScope, post_id: int, reason: str | None, now: float
    ) -> BlogPost:
        """记一次只读查询尝试：次数 +1、查询时间与原因更新；**不改额度**。

        查询失败、无匹配、不完整**都要**记 —— 否则一行可以无限次被查询，12 次上限永远到不了。
        次数不由重启重置（唯一会让它变小的路径是人工维护）。
        """

        def operation(conn: sqlite3.Connection) -> BlogPost:
            cursor = conn.execute(
                "UPDATE blog_posts SET reconcile_attempts = reconcile_attempts + 1,"
                " last_reconciled_at = ?, reason = ?, updated_at = ?"
                " WHERE id = ? AND site_base_url = ? AND self_user_id = ?",
                (now, reason, now, post_id, scope.site_base_url, scope.self_user_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise KeyError(f"blog_post {post_id} 不存在或不属于当前账号")
            row = conn.execute(
                f"SELECT {_BLOG_POST_COLUMNS} FROM blog_posts WHERE id = ?", (post_id,)
            ).fetchone()
            conn.commit()
            if row is None:
                raise RuntimeError("blog_posts 更新成功却读不回这一行")
            return _row_to_blog_post(row)

        return await self._execute(operation)
