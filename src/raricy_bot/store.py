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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

_T = TypeVar("_T")

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
)


class Store:
    """SQLite 状态存储；全部方法 async，单连接 + 锁串行。"""

    def __init__(self, path: str) -> None:
        self._path = path
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
        """可安全提交给 Last-Event-ID 的水位（只读，不改任何状态）。

        只看 `event_id` 非 NULL 的行：有 pending 时取 `min(event_id) - 1`，
        否则取 `max(event_id)`，再与 `max(event_id)` 取小；无记录返回 0。
        resync 行（event_id 为 NULL）既不推进也不阻挡水位。
        """
        return await self._execute(self._compute_watermark)

    async def watermark(self) -> int:
        """同 `advance_watermark()`：只读水位，不推进任何状态。"""
        return await self._execute(self._compute_watermark)

    @staticmethod
    def _compute_watermark(conn: sqlite3.Connection) -> int:
        bounds = conn.execute(
            "SELECT MIN(event_id), MAX(event_id) FROM events WHERE event_id IS NOT NULL"
        ).fetchone()
        max_event_id = bounds[1] if bounds is not None else None
        if max_event_id is None:
            # 一条带 event_id 的记录都没有（空表或全是 resync 行）。
            return 0

        placeholders = ", ".join("?" for _ in HANDLED_STATUSES)
        pending = conn.execute(
            "SELECT MIN(event_id) FROM events WHERE event_id IS NOT NULL"
            f" AND status NOT IN ({placeholders})",
            HANDLED_STATUSES,
        ).fetchone()
        pending_min = pending[0] if pending is not None else None

        if pending_min is None:
            value = int(max_event_id)
        else:
            value = int(pending_min) - 1
        # 水位不得超过见过的最大 event_id，也不为负。
        return max(0, min(value, int(max_event_id)))

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

    # --- 已发回复 -----------------------------------------------------------

    async def record_sent(self, message_id: int, channel_id: str, reply_to: int | None) -> None:
        """记录一条已成功发出的站内消息，供「结果不确定」时对账去重。"""

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO sent_replies (message_id, channel_id, reply_to, sent_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(message_id) DO UPDATE SET"
                " channel_id = excluded.channel_id,"
                " reply_to = excluded.reply_to,"
                " sent_at = excluded.sent_at",
                (message_id, channel_id, reply_to, time.time()),
            )
            conn.commit()

        await self._execute(operation)

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

    async def count_channel_sends_since(
        self, channel_id: str, since: float, kind: str | None = None
    ) -> int:
        """统计指定频道内的发送尝试数；`kind` 为 None 时不过滤。"""

        def operation(conn: sqlite3.Connection) -> int:
            sql = (
                "SELECT COUNT(*) FROM send_attempts"
                " WHERE channel_id = ? AND attempted_at >= ?"
            )
            params: list[object] = [channel_id, since]
            if kind is not None:
                sql += " AND kind = ?"
                params.append(kind)
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row is not None else 0

        return await self._execute(operation)

    async def last_notice_at(self, channel_id: str, kinds: Sequence[str]) -> float | None:
        """该频道内 `kinds` 覆盖的最近一次发送时间；无记录返回 None。"""

        selected = tuple(kinds)

        def operation(conn: sqlite3.Connection) -> float | None:
            if not selected:
                return None
            placeholders = ", ".join("?" for _ in selected)
            row = conn.execute(
                "SELECT MAX(attempted_at) FROM send_attempts"
                f" WHERE channel_id = ? AND kind IN ({placeholders})",
                (channel_id, *selected),
            ).fetchone()
            return float(row[0]) if row is not None and row[0] is not None else None

        return await self._execute(operation)

    # --- 冷却 ---------------------------------------------------------------

    async def get_cooldown(self, key: str) -> float | None:
        """未过期时返回到期时间戳，否则返回 None（过期即视为无）。"""

        def operation(conn: sqlite3.Connection) -> float | None:
            row = conn.execute(
                "SELECT until FROM cooldowns WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            until = float(row[0])
            return until if until > time.time() else None

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
