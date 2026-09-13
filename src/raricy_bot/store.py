"""SQLite 运行状态持久化。

只保存运行元数据：事件与水位、私聊频道、已发回复、发送尝试时间、冷却状态。
**不保存**消息正文、模型输入输出、Cookie、密码或 API Key（§19.1 红线）。

实现方式：单条 `sqlite3` 连接（`check_same_thread=False`）+ `asyncio.Lock` 串行化，
具体语句在 `asyncio.to_thread` 里执行，避免阻塞事件循环。全部方法都是 async。
时间一律是 `time.time()` 的 epoch 秒（REAL），不使用 datetime 字符串。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

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
)


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
        self._lock = asyncio.Lock()

    # --- 生命周期 -----------------------------------------------------------

    async def open(self) -> None:
        """建立连接并建表；可重复调用（幂等）。"""
        async with self._lock:
            if self._conn is not None:
                return
            self._conn = await asyncio.to_thread(self._connect)

    async def close(self) -> None:
        """关闭连接；可重复调用（幂等）。"""
        async with self._lock:
            conn = self._conn
            self._conn = None
            if conn is None:
                return
            await asyncio.to_thread(conn.close)

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
        """在锁内把一次数据库操作丢进线程执行，保证单连接被串行使用。"""
        async with self._lock:
            conn = self._conn
            if conn is None:
                raise RuntimeError("Store 尚未 open()，无法执行操作")
            return await asyncio.to_thread(operation, conn)

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
