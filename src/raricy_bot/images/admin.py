"""服务器本地黑名单管理；列表只读，修改必须持有机器人公共数据锁。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path

from ..data_lock import DataLockError, acquire_data_lock, data_lock_dir, refuse_removed_profile
from ..store import Store


def _read(path: Path, scope: str | None = None) -> tuple[list[str], list[dict]]:
    """只读查询现有数据库；旧版本没有黑名单表时返回空列表。"""
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        scopes: set[str] = set()
        for table in ("image_jobs", "image_quota_clock", "image_blacklist"):
            if table in tables:
                scopes.update(row[0] for row in conn.execute(f"SELECT DISTINCT scope FROM {table}"))
        entries = []
        if "image_blacklist" in tables:
            sql = "SELECT scope,actor_id,blocked_at,reason,trigger_message_id FROM image_blacklist"
            params = ()
            if scope is not None:
                sql += " WHERE scope=?"
                params = (scope,)
            for row in conn.execute(sql + " ORDER BY blocked_at,scope,actor_id", params):
                entries.append(dict(zip(("scope", "user_id", "blocked_at", "reason", "message_id"), row)))
        return sorted(scopes), entries
    finally:
        conn.close()


async def _modify(path: Path, args: argparse.Namespace) -> dict:
    """不访问站点或模型，单写者锁内进行增删。"""
    with acquire_data_lock(data_lock_dir(path)):
        refuse_removed_profile(data_lock_dir(path))
        scopes, _ = _read(path)
        if args.scope is None and len(scopes) != 1:
            raise ValueError("image_scope_required")
        scope = args.scope if args.scope is not None else scopes[0]
        if not scope.strip() or not args.user_id.strip():
            raise ValueError("invalid_image_identity")
        store = Store(str(path))
        await store.open()
        try:
            if args.action == "block":
                await store.block_image_user(scope, args.user_id, reason="manual", now=time.time())
                return {"action": "block", "user_id": args.user_id, "blocked": True}
            removed = await store.unblock_image_user(scope, args.user_id)
            return {"action": "unblock", "user_id": args.user_id, "removed": removed}
        finally:
            await store.close()


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；稳定错误码输出，不打印原始数据库异常或凭据。"""
    parser = argparse.ArgumentParser(description="图像黑名单：修改前先停止机器人，list 可在线使用。")
    parser.add_argument("action", choices=("list", "block", "unblock"))
    parser.add_argument("--db", required=True, help="现有机器人数据库路径")
    parser.add_argument("--scope", help="站点 base_url|机器人用户 ID；唯一作用域时可省略")
    parser.add_argument("--user-id", help="可信站点用户 ID，禁止用频道 ID 或用户名替代")
    args = parser.parse_args(argv)
    if args.action != "list" and not args.user_id:
        parser.error("block/unblock 需要 --user-id")
    try:
        path = Path(args.db).expanduser().resolve()
        if not path.is_file():
            raise ValueError("image_database_missing")
        if args.action == "list":
            scopes, entries = _read(path, args.scope)
            result = {"scopes": scopes, "entries": entries}
        else:
            result = asyncio.run(_modify(path, args))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (DataLockError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    except (OSError, sqlite3.Error):
        print(json.dumps({"error": "image_database_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
