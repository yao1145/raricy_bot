"""凭据引用归属索引与清除（N2；设计 §6.1、§6.3、§7，D-144）。

`credentials-index.json` 是「哪个引用属于哪个档案、被哪个 revision 引用、现在处于
什么清理状态」的唯一归属记录，**只含引用与状态，不含密码、模型 Key 或任何秘密取值**。
它是「先登记后写库」的一半：`reserve()` 先把 `pending` 条目落盘，之后才写系统凭据库
—— 写库成功但提交失败时，崩溃重启仍能从索引找回「写过但尚未引用」的条目（§6.3）。

五种状态（确切值固定，见 INTERFACES §58）：

- `pending`：已登记，凭据库写入尚未确认（也用于「写入成功但还没有 revision 引用」）；
- `owned`：已被某个 revision 引用，`revision` 记下是哪一版；
- `revoked`：已从凭据库删除；
- `pending_removal`：申请删除但后端失败，可重试；
- `orphan`：创建失败且回滚删除也失败。

读取语义与 N0/D-130 同口径：文件不存在 → 空索引；`OSError` → `credentials_index_unreadable`；
JSON/结构非法 → `credentials_index_corrupt`；`schema_version` 过大 →
`credentials_index_unsupported_version`。三种故障都**不覆盖现场**，并让破坏性操作
（`clear()` 与 Task 3 的移除）拒绝推进。

**历史引用是并集**：`config.yaml`、`revisions/*.yaml` 里出现过的引用与索引并起来处理，
删除不能只处理当前那一条。**不枚举、不批量删除整个 `RaricyBotLight` 服务命名空间**：
旧版本可能留下完全失去引用的条目，那属于「历史凭据归属未知」，只在页面与使用手册里
提示用户到 Windows 凭据管理器人工清理。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from raricy_bot import config as core_config
from raricy_bot.config import Secrets

from . import paths
from .config_service import (
    CREDENTIAL_KINDS,
    KIND_LLM_API_KEY,
    KIND_PASSWORD,
    ConfigServiceError,
)
from .credential_store import CredentialStore, CredentialStoreError, new_reference

# 索引文档版本：本程序写 1；读到更大的版本一律拒绝解释（§6.5 的口径）。
INDEX_SCHEMA_VERSION: int = 1

# 条目状态（五种，取值固定）。
STATE_PENDING: str = "pending"
STATE_OWNED: str = "owned"
STATE_REVOKED: str = "revoked"
STATE_PENDING_REMOVAL: str = "pending_removal"
STATE_ORPHAN: str = "orphan"

ENTRY_STATES: frozenset[str] = frozenset(
    {STATE_PENDING, STATE_OWNED, STATE_REVOKED, STATE_PENDING_REMOVAL, STATE_ORPHAN}
)

# 「需要清理」的状态：`pending` 是写过、还没被任何 revision 引用的条目，崩溃后就留在这里，
# 卡片与预览据此显示清理待办；后两种是删除失败、可重试的条目。
_CLEANUP_STATES: frozenset[str] = frozenset(
    {STATE_PENDING, STATE_PENDING_REMOVAL, STATE_ORPHAN}
)
_RETRYABLE_STATES: frozenset[str] = frozenset({STATE_PENDING_REMOVAL, STATE_ORPHAN})

# `KIND_PASSWORD` / `KIND_LLM_API_KEY` / `CREDENTIAL_KINDS` 从 `config_service` 导入：
# `clear()` 的 `kinds` 与提交时登记的载荷类别共用同一组确切取值，只在那里定义一次。

# 索引故障的稳定码（读、写都归到这里；不含路径与异常原文）。
INDEX_UNREADABLE: str = "credentials_index_unreadable"
INDEX_CORRUPT: str = "credentials_index_corrupt"
INDEX_UNSUPPORTED_VERSION: str = "credentials_index_unsupported_version"
# 部分失败时调用方必须显示的稳定码：档案在清理完成前不可启动（§6.1）。
CLEANUP_PENDING: str = "credentials_cleanup_pending"


class CredentialLifecycleError(ConfigServiceError):
    """凭据索引与清除的固定错误；消息是稳定类别码，绝不含秘密取值。"""


@dataclass(frozen=True)
class IndexEntry:
    """索引里的一条引用归属记录；字段形状固定，见 INTERFACES §58。"""

    ref: str
    profile_id: str
    kinds: tuple[str, ...]
    state: str
    revision: int | None
    created_at: str | None
    updated_at: str | None
    last_error: str | None

    def to_document(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "profile_id": self.profile_id,
            "kinds": list(self.kinds),
            "state": self.state,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_document(cls, raw: Any) -> IndexEntry:
        """解析一条条目；任何形状问题都按 `credentials_index_corrupt` 拒绝解释。"""
        if not isinstance(raw, dict):
            raise CredentialLifecycleError(INDEX_CORRUPT)
        reference = raw.get("ref")
        profile_id = raw.get("profile_id")
        kinds = raw.get("kinds", [])
        state = raw.get("state")
        revision = raw.get("revision")
        if not isinstance(reference, str) or not reference:
            raise CredentialLifecycleError(INDEX_CORRUPT)
        if not isinstance(profile_id, str) or not profile_id:
            raise CredentialLifecycleError(INDEX_CORRUPT)
        if not isinstance(kinds, list) or any(
            not isinstance(kind, str) or kind not in CREDENTIAL_KINDS for kind in kinds
        ):
            raise CredentialLifecycleError(INDEX_CORRUPT)
        if state not in ENTRY_STATES:
            raise CredentialLifecycleError(INDEX_CORRUPT)
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
        ):
            raise CredentialLifecycleError(INDEX_CORRUPT)
        for key in ("created_at", "updated_at", "last_error"):
            value = raw.get(key)
            if value is not None and not isinstance(value, str):
                raise CredentialLifecycleError(INDEX_CORRUPT)
        return cls(
            ref=reference,
            profile_id=profile_id,
            kinds=tuple(kinds),
            state=state,
            revision=revision,
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            last_error=raw.get("last_error"),
        )


@dataclass(frozen=True)
class ClearResult:
    """一次清除的结果（§6.1）：新引用、已撤销的旧引用、仍待清理的引用与总体成败。

    - `pending_refs`：申请删除但后端失败、留在索引里可重试的引用；
    - `unreadable_documents`：读不出来或结构不可用的历史文档（相对数据根的路径）。
      它们记录的引用**既登记不了也撤销不了**，因此可能有残留；调用方必须据此显示
      `credentials_cleanup_pending` 与人工清理说明，不能把它读成「已清干净」。
    - `ok`：只有「没有待清理引用、也没有读不出来的文档」才是完全成功。
    """

    new_ref: str | None
    revoked: tuple[str, ...]
    pending_refs: tuple[str, ...]
    ok: bool
    unreadable_documents: tuple[str, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_kinds(kinds: Iterable[str], *, allow_empty: bool) -> tuple[str, ...]:
    """把类别集合规范化成固定顺序的元组；空集或未知类别按 422 的稳定码拒绝。

    未知类别与空集共用 `credential_scope_required`：对调用方都是「这次的清除范围
    不成立」，不另造一个总表之外的码。
    """
    selected = set()
    for kind in kinds:
        if not isinstance(kind, str) or kind not in CREDENTIAL_KINDS:
            raise CredentialLifecycleError("credential_scope_required")
        selected.add(kind)
    if not selected and not allow_empty:
        raise CredentialLifecycleError("credential_scope_required")
    return tuple(kind for kind in CREDENTIAL_KINDS if kind in selected)


class CredentialLifecycle:
    """一个数据根的凭据引用归属、清除与重试入口。

    所有索引写入都在进程内写锁下完成（与 `ConfigService` 的写锁各自独立，调用顺序固定
    为「配置锁 → 生命周期锁」，不会互相等待）。凭据库操作可能阻塞（系统授权框），
    调用方应在工作线程里调用本类的方法（§7）。
    """

    def __init__(
        self,
        data_root: Path,
        *,
        store: CredentialStore,
        clock: Callable[[], datetime] | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        self._root = Path(data_root)
        self._store = store
        self._clock = clock if clock is not None else _utc_now
        # 可重入：`clear()` 在一次持锁里做「登记 → 写库 → 逐条撤销」多步。
        self._lock = lock if lock is not None else threading.RLock()

    # --- 索引读取 ---------------------------------------------------------

    def _index_path(self) -> Path:
        return paths.credentials_index_path(self._root)

    def _read_unlocked(self) -> tuple[int, list[IndexEntry]]:
        """读索引：返回 (index_revision, entries)；文件不存在是空索引，故障按码抛出。"""
        path = self._index_path()
        try:
            with path.open("rb") as handle:
                raw = handle.read(core_config.MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return 0, []
        except OSError as exc:
            raise CredentialLifecycleError(INDEX_UNREADABLE) from exc
        if len(raw) > core_config.MAX_CONFIG_BYTES:
            # 与配置文档同一体积上限：手工编辑出的巨型文件不读进来。
            raise CredentialLifecycleError(INDEX_CORRUPT)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CredentialLifecycleError(INDEX_CORRUPT) from exc
        if not isinstance(data, dict):
            raise CredentialLifecycleError(INDEX_CORRUPT)
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise CredentialLifecycleError(INDEX_CORRUPT)
        if version > INDEX_SCHEMA_VERSION:
            # 版本比本程序新就不猜：宁可拒绝推进，也不按未知格式解释并覆盖（§6.5）。
            raise CredentialLifecycleError(INDEX_UNSUPPORTED_VERSION)
        index_revision = data.get("index_revision", 0)
        if (
            isinstance(index_revision, bool)
            or not isinstance(index_revision, int)
            or index_revision < 0
        ):
            raise CredentialLifecycleError(INDEX_CORRUPT)
        raw_entries = data.get("entries", [])
        if not isinstance(raw_entries, list):
            raise CredentialLifecycleError(INDEX_CORRUPT)
        return index_revision, [IndexEntry.from_document(item) for item in raw_entries]

    def _write_unlocked(self, revision: int, entries: Sequence[IndexEntry]) -> None:
        """同目录临时文件 + flush + fsync + 原子替换：要么旧索引、要么新索引。"""
        document = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "index_revision": revision + 1,
            "entries": [entry.to_document() for entry in entries],
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        path = self._index_path()
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            handle_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
            )
        except OSError as exc:
            raise CredentialLifecycleError(INDEX_UNREADABLE) from exc
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            # 写不进去与读不出来对外是同一件事：索引不可用，破坏性操作必须停。
            raise CredentialLifecycleError(INDEX_UNREADABLE) from exc
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _flush(self, revision: int, entries: list[IndexEntry]) -> int:
        self._write_unlocked(revision, entries)
        return revision + 1

    def ensure_readable(self) -> None:
        """读一次索引；坏索引按稳定码抛出（破坏性操作在写任何东西之前调用）。"""
        with self._lock:
            self._read_unlocked()

    def pending_profiles(self) -> tuple[str, ...]:
        """有清理待办的档案 id，供卡片与预览查询。

        口径是 `pending` / `pending_removal` / `orphan`：`pending` 是「写过、还没有
        revision 引用」的条目，崩在提交中途就会留在这里，所以它也算待办。
        """
        with self._lock:
            _revision, entries = self._read_unlocked()
        return tuple(
            sorted({entry.profile_id for entry in entries if entry.state in _CLEANUP_STATES})
        )

    def managed_refs(self, profile_id: str) -> tuple[str, ...]:
        """该档案的「历史引用并集」：索引里未撤销的条目 ∪ 历史 YAML 里出现过的引用。

        只读、只登记不删除；`clear()` 处理的就是这个集合。读不出来的历史文档不在
        结果里（它们的引用无从得知），需要时用 `unreadable_documents()` 如实提示。
        """
        paths.validate_profile_id(profile_id)
        with self._lock:
            _revision, entries = self._read_unlocked()
            history, _unreadable = self._historical_refs(profile_id)
        index_refs = {
            entry.ref
            for entry in entries
            if entry.profile_id == profile_id and entry.state != STATE_REVOKED
        }
        return tuple(sorted(index_refs | set(history)))

    # --- 先登记后写库（§6.3） ---------------------------------------------

    def reserve(self, *, profile_id: str, kinds: Sequence[str]) -> str:
        """登记一个新引用并把索引**落盘**，返回引用；调用方之后才写凭据库。"""
        paths.validate_profile_id(profile_id)
        normalized = _normalize_kinds(kinds, allow_empty=True)
        with self._lock:
            index_revision, entries = self._read_unlocked()
            reference = self._new_reference(entries)
            now = self._now()
            entries.append(
                IndexEntry(
                    ref=reference,
                    profile_id=profile_id,
                    kinds=normalized,
                    state=STATE_PENDING,
                    revision=None,
                    created_at=now,
                    updated_at=now,
                    last_error=None,
                )
            )
            self._flush(index_revision, entries)
        return reference

    def confirm(self, reference: str, *, profile_id: str, revision: int) -> None:
        """配置提交成功后把引用标成 `owned` 并记下 revision。

        索引里若已经没有这条记录（例如索引被手工删过），重新登记为 `owned`：配置
        已经落盘，不让一次索引丢失推翻已成功的提交。
        """
        paths.validate_profile_id(profile_id)
        with self._lock:
            index_revision, entries = self._read_unlocked()
            index = self._find_index(entries, reference, profile_id)
            now = self._now()
            if index is None:
                entries.append(
                    IndexEntry(
                        ref=reference,
                        profile_id=profile_id,
                        kinds=(),
                        state=STATE_OWNED,
                        revision=revision,
                        created_at=now,
                        updated_at=now,
                        last_error=None,
                    )
                )
            else:
                entries[index] = replace(
                    entries[index],
                    state=STATE_OWNED,
                    revision=revision,
                    updated_at=now,
                    last_error=None,
                )
            self._flush(index_revision, entries)

    def abandon(self, reference: str, *, profile_id: str | None = None) -> bool:
        """提交失败时尽力删掉刚写的凭据；删不掉就留 `orphan` + `last_error`。

        返回是否已从凭据库删除。「本来就不存在」在存储层与删除成功同义。
        """
        try:
            self._store.delete(reference)
            deleted = True
            failure: str | None = None
        except CredentialStoreError as exc:
            deleted = False
            failure = str(exc)
        with self._lock:
            index_revision, entries = self._read_unlocked()
            index = self._find_index(entries, reference, profile_id)
            if index is not None:
                entries[index] = replace(
                    entries[index],
                    state=STATE_REVOKED if deleted else STATE_ORPHAN,
                    updated_at=self._now(),
                    last_error=None if deleted else failure,
                )
                self._flush(index_revision, entries)
        return deleted

    def mark_uncertain(self, reference: str, *, error: str) -> None:
        """回读不一致：不确定的引用既不复用、也不删除，只记下原因等人工核对（D-127）。"""
        with self._lock:
            index_revision, entries = self._read_unlocked()
            index = self._find_index(entries, reference, None)
            if index is None:
                return
            entries[index] = replace(
                entries[index],
                state=STATE_PENDING,
                updated_at=self._now(),
                last_error=error,
            )
            self._flush(index_revision, entries)

    # --- 历史引用归属（§6.1 的迁移口径） ----------------------------------

    def reconcile(self, profile_id: str) -> dict[str, int]:
        """把历史引用并集里未登记的引用补成 `owned`，并认领崩溃留下的 `pending`。

        「认领」指这条 `pending` 引用已经出现在某份 revision 文档里：说明它是
        「写库成功、配置也写上了、只差确认」的现场，读到时按 `owned` 收编，不再
        让确认前的崩溃把一条真实引用留在待办里。只读 YAML + 写索引，**不碰凭据库**。

        返回 `{"registered": 新增登记数, "claimed": 认领数, "unreadable": 读不出来的
        文档数}` 供日志与事件使用；`unreadable` 不为零说明「该档案还引用过哪些凭据」
        并不完整，调用方要如实显示清理待办，不能当成没有引用（与 `clear()` 同一口径）。
        """
        paths.validate_profile_id(profile_id)
        with self._lock:
            index_revision, entries = self._read_unlocked()
            history, unreadable = self._historical_refs(profile_id)
            registered = 0
            claimed = 0
            changed = False
            for reference, revision in sorted(history.items()):
                index = self._find_index(entries, reference, profile_id)
                if index is None:
                    now = self._now()
                    entries.append(
                        IndexEntry(
                            ref=reference,
                            profile_id=profile_id,
                            kinds=(),
                            state=STATE_OWNED,
                            revision=revision or None,
                            created_at=now,
                            updated_at=now,
                            last_error=None,
                        )
                    )
                    registered += 1
                    changed = True
                elif entries[index].state == STATE_PENDING:
                    entries[index] = replace(
                        entries[index],
                        state=STATE_OWNED,
                        revision=revision or entries[index].revision,
                        updated_at=self._now(),
                        last_error=None,
                    )
                    claimed += 1
                    changed = True
            if changed:
                self._flush(index_revision, entries)
            return {
                "registered": registered,
                "claimed": claimed,
                "unreadable": len(unreadable),
            }

    def reconcile_all(self, profile_ids: Iterable[str]) -> dict[str, int]:
        """对每个档案各跑一次 `reconcile()`；`Controller.start()` 在 `recover()` 之后调用。

        索引故障按稳定码抛出，由调用方记录且不因此阻止启动；本方法不碰凭据库。
        """
        summary = {"profiles": 0, "registered": 0, "claimed": 0, "unreadable": 0}
        for profile_id in profile_ids:
            result = self.reconcile(profile_id)
            summary["profiles"] += 1
            summary["registered"] += result["registered"]
            summary["claimed"] += result["claimed"]
            summary["unreadable"] += result["unreadable"]
        return summary

    # --- 清除（§6.1 的三种清除范围） --------------------------------------

    def clear(self, profile_id: str, *, kinds: Sequence[str]) -> ClearResult:
        """清除该档案受管凭据里 `kinds` 指定的类别；`kinds` 是非空子集。

        - 索引坏掉时在写任何东西之前失败（破坏性操作拒绝推进）；
        - 保留的类别先复制到**新引用**（先登记后写库），再逐个撤销该档案的全部受管
          旧引用 —— 当前版本与历史快照的并集，清掉的秘密不会从旧快照复活；
        - 单个引用删除失败不中止其余：如实记 `pending_removal` + `last_error`，
          `ok=False` 时调用方必须把档案留在不可启动态并显示 `credentials_cleanup_pending`；
        - 读不出来的历史快照**不当成「没有引用」**：它们记录的引用无从得知，因此照常
          推进能做的删除，但在 `unreadable_documents` 里如实列出并让 `ok=False`
          —— 清除可能仍有残留，调用方必须提示人工核对，不能显示成已清干净；
        - 新引用在配置写成功前保持 `pending`，由 `commit_credentials_clear()` 标成
          `owned`；配置没写上时不谎报已归属（那是 `cleared_partial`）。

        保留项写库失败时不删除任何旧引用（保留的秘密还没有安全副本），按凭据库错误抛出。
        """
        paths.validate_profile_id(profile_id)
        normalized = _normalize_kinds(kinds, allow_empty=False)
        with self._lock:
            index_revision, entries = self._read_unlocked()
            current_ref, account = self._current(profile_id)
            current: Secrets | None = None
            if current_ref is not None:
                current = self._store.get(current_ref)
                if current is not None:
                    account = account or current.username

            # 1) 先把历史并集登记进索引，再动凭据库：崩在中间也能找回受管引用。
            history, unreadable = self._historical_refs(profile_id)
            for reference, revision in sorted(history.items()):
                if self._find_index(entries, reference, profile_id) is None:
                    now = self._now()
                    entries.append(
                        IndexEntry(
                            ref=reference,
                            profile_id=profile_id,
                            kinds=(),
                            state=STATE_OWNED,
                            revision=revision or None,
                            created_at=now,
                            updated_at=now,
                            last_error=None,
                        )
                    )
            if history:
                index_revision = self._flush(index_revision, entries)

            # 2) 保留项：构造新载荷，先登记新引用再写库。
            new_secrets = Secrets(
                username=account or (current.username if current else ""),
                password="" if KIND_PASSWORD in normalized else (current.password if current else ""),
                llm_api_key=(
                    ""
                    if KIND_LLM_API_KEY in normalized
                    else (current.llm_api_key if current else "")
                ),
            )
            kept = {"password": new_secrets.password, "llm_api_key": new_secrets.llm_api_key}
            keep_kinds = tuple(
                kind
                for kind in CREDENTIAL_KINDS
                if kind not in normalized and kept[kind]
            )
            new_ref: str | None = None
            if keep_kinds:
                new_ref = self._new_reference(entries)
                now = self._now()
                entries.append(
                    IndexEntry(
                        ref=new_ref,
                        profile_id=profile_id,
                        kinds=keep_kinds,
                        state=STATE_PENDING,
                        revision=None,
                        created_at=now,
                        updated_at=now,
                        last_error=None,
                    )
                )
                index_revision = self._flush(index_revision, entries)
                try:
                    self._store.put(new_ref, new_secrets)
                    readback = self._store.get(new_ref)
                except CredentialStoreError:
                    # 保留的类别还没有安全副本：回滚新引用，什么都不删。
                    self.abandon(new_ref, profile_id=profile_id)
                    raise
                if readback != new_secrets:
                    # D-127：不确定的新引用不复用、也不删除，只登记待人工核对。
                    self.mark_uncertain(new_ref, error="credential_readback_mismatch")
                    raise CredentialStoreError("credential_readback_mismatch")

            # 3) 逐个撤销旧引用（当前版本与历史快照的并集），单条失败不中止其余。
            revoked: list[str] = []
            pending: list[str] = []
            targets = sorted(
                {entry.ref for entry in entries if entry.profile_id == profile_id}
                | set(history)
            )
            for reference in targets:
                if reference == new_ref:
                    continue
                index = self._find_index(entries, reference, profile_id)
                if index is None or entries[index].state == STATE_REVOKED:
                    continue
                try:
                    self._store.delete(reference)
                except CredentialStoreError as exc:
                    entries[index] = replace(
                        entries[index],
                        state=STATE_PENDING_REMOVAL,
                        updated_at=self._now(),
                        last_error=str(exc),
                    )
                    pending.append(reference)
                else:
                    entries[index] = replace(
                        entries[index],
                        state=STATE_REVOKED,
                        updated_at=self._now(),
                        last_error=None,
                    )
                    revoked.append(reference)
                index_revision = self._flush(index_revision, entries)

            return ClearResult(
                new_ref=new_ref,
                revoked=tuple(revoked),
                pending_refs=tuple(pending),
                ok=not pending and not unreadable,
                unreadable_documents=unreadable,
            )

    def retry_pending(self, profile_id: str) -> ClearResult:
        """重试该档案的 `pending_removal` / `orphan` 条目；仍失败就如实保留。

        与 `clear()` 同一口径：`ok` 还要求没有读不出来的历史文档 —— 重试成功也不等于
        「这条档案已清干净」。
        """
        paths.validate_profile_id(profile_id)
        with self._lock:
            index_revision, entries = self._read_unlocked()
            _history, unreadable = self._historical_refs(profile_id)
            revoked: list[str] = []
            pending: list[str] = []
            for index, entry in enumerate(entries):
                if entry.profile_id != profile_id or entry.state not in _RETRYABLE_STATES:
                    continue
                try:
                    self._store.delete(entry.ref)
                except CredentialStoreError as exc:
                    entries[index] = replace(
                        entry, updated_at=self._now(), last_error=str(exc)
                    )
                    pending.append(entry.ref)
                else:
                    entries[index] = replace(
                        entry,
                        state=STATE_REVOKED,
                        updated_at=self._now(),
                        last_error=None,
                    )
                    revoked.append(entry.ref)
                index_revision = self._flush(index_revision, entries)
            return ClearResult(
                new_ref=None,
                revoked=tuple(revoked),
                pending_refs=tuple(pending),
                ok=not pending and not unreadable,
                unreadable_documents=unreadable,
            )

    # --- 内部：读取与解析 --------------------------------------------------

    def _now(self) -> str:
        return self._clock().isoformat()

    def _new_reference(self, entries: Sequence[IndexEntry]) -> str:
        existing = {entry.ref for entry in entries}
        reference = new_reference()
        while reference in existing:
            reference = new_reference()
        return reference

    @staticmethod
    def _find_index(
        entries: Sequence[IndexEntry], reference: str, profile_id: str | None
    ) -> int | None:
        for index, entry in enumerate(entries):
            if entry.ref != reference:
                continue
            if profile_id is not None and entry.profile_id != profile_id:
                continue
            return index
        return None

    def _current(self, profile_id: str) -> tuple[str | None, str | None]:
        """当前正式配置的 (引用, 账号)；读不出来按稳定码抛出，不当成「没有配置」。"""
        path = paths.config_path(paths.profile_dir(self._root, profile_id))
        try:
            with path.open("rb") as handle:
                raw = handle.read(core_config.MAX_CONFIG_BYTES + 1)
        except FileNotFoundError as exc:
            raise CredentialLifecycleError("no_active_config") from exc
        except OSError as exc:
            raise CredentialLifecycleError("config_unreadable") from exc
        if len(raw) > core_config.MAX_CONFIG_BYTES:
            raise CredentialLifecycleError("config_unreadable")
        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise CredentialLifecycleError("config_unreadable") from exc
        if not isinstance(data, dict):
            raise CredentialLifecycleError("config_unreadable")
        launcher = data.get("_launcher")
        if not isinstance(launcher, dict):
            raise CredentialLifecycleError("config_unreadable")
        reference = launcher.get("credentials_ref")
        account = launcher.get("account")
        return (
            reference if isinstance(reference, str) and reference else None,
            account if isinstance(account, str) and account else None,
        )

    def _historical_refs(
        self, profile_id: str
    ) -> tuple[dict[str, int], tuple[str, ...]]:
        """该档案正式配置与全部历史快照里出现过的引用 → 引用它的最大 revision。

        返回 `(引用表, 读不出来的文档相对路径)`。**读不出来的文档不等于没有引用**：
        一份坏快照不能阻止用户清除凭据（那会更不安全），但也绝不能把它当成「没有
        引用」—— 它记录过的引用既登记不了也撤销不了，调用方必须如实显示可能的残留
        （与 D-130「读不出来不当成没有」同口径）。跳过期间不删除、不覆盖任何现场。
        """
        profile = paths.profile_dir(self._root, profile_id)
        unreadable: list[str] = []
        candidates = [paths.config_path(profile), *self._revision_documents(profile, unreadable)]
        found: dict[str, int] = {}
        for path in candidates:
            reference, revision, usable = self._document_ref(path)
            if not usable:
                unreadable.append(self._relative_document(profile, path))
                continue
            if reference is None:
                continue
            previous = found.get(reference)
            if previous is None or (revision or 0) > previous:
                found[reference] = revision or 0
        return found, tuple(unreadable)

    def unreadable_documents(self, profile_id: str) -> tuple[str, ...]:
        """该档案读不出来或结构不可用的历史文档（相对数据根的路径，只读）。

        供卡片与移除预览判断 `unknown_ownership`：有这类文档时，「该档案还引用过
        哪些凭据」就不完整，需要人工核对（不枚举、不批量删除服务命名空间）。
        """
        paths.validate_profile_id(profile_id)
        with self._lock:
            _refs, unreadable = self._historical_refs(profile_id)
        return unreadable

    def _revision_documents(self, profile: Path, unreadable: list[str]) -> list[Path]:
        """列出 `revisions/*.yaml`；列不出来就记账，不能当成「没有历史」。

        不能用 `Path.glob()`：`pathlib` 在列目录失败时会把 `OSError` 吞掉（本机
        3.13 实测，且项目 `requires-python >=3.12`），于是一个列不出来的目录会
        静默变成「没有历史」—— 正是本次要修的 fail-open。这里显式 `os.scandir`
        并自己区分三种情形：

        - 目录不存在 → 还没有历史（不是故障）；
        - 目录存在但列不出来（权限、占用、被做成普通文件等）→ 整段历史读不出来，
          按受管相对路径记入未读文档；
        - 条目不是普通文件（同名目录、链接等）→ 它可能是被改坏的历史，同样记账。
        """
        revisions = paths.revisions_dir(profile)
        documents: list[Path] = []
        try:
            with os.scandir(revisions) as entries:
                for entry in entries:
                    if not entry.name.lower().endswith(".yaml"):
                        continue
                    path = Path(entry.path)
                    try:
                        if entry.is_file():
                            documents.append(path)
                            continue
                    except OSError:
                        pass
                    unreadable.append(self._relative_document(profile, path))
        except FileNotFoundError:
            # 还没有 revisions/：没有历史可读，也不是「读不出来」。
            return []
        except OSError:
            unreadable.append(self._relative_document(profile, revisions))
            return []
        return sorted(documents)

    @staticmethod
    def _relative_document(profile: Path, path: Path) -> str:
        """文档相对数据根的受管路径（如 `profiles/p-x/revisions/3.yaml`），不含绝对路径。"""
        try:
            return f"{paths.PROFILES_DIR}/{profile.name}/{path.relative_to(profile).as_posix()}"
        except ValueError:
            return path.name

    @staticmethod
    def _document_ref(path: Path) -> tuple[str | None, int | None, bool]:
        """读一份 Launcher 文档里的 `_launcher.credentials_ref` 与 `revision`。

        返回 `(引用, revision, 可用)`；`可用=False` 表示这份文档读不出来或结构不可用
        （权限/占用失败、超体积、解码或 YAML 解析失败、顶层/`_launcher` 不是映射、
        `credentials_ref` 不是字符串），调用方必须把它计入「可能有残留」。
        读得到但没有引用（`引用=None, 可用=True`）才是「这份文档没有引用」——
        **文档不存在也算这一种**：新建但还没保存过设置的档案没有 `config.yaml`，
        与 `_current()` 区分 `no_active_config` / `config_unreadable` 同口径。
        """
        try:
            with path.open("rb") as handle:
                raw = handle.read(core_config.MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return None, None, True
        except OSError:
            return None, None, False
        if len(raw) > core_config.MAX_CONFIG_BYTES:
            return None, None, False
        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            return None, None, False
        if not isinstance(data, dict):
            return None, None, False
        launcher = data.get("_launcher")
        if not isinstance(launcher, dict):
            return None, None, False
        reference = launcher.get("credentials_ref")
        revision = launcher.get("revision")
        if reference is not None and not isinstance(reference, str):
            # 字段在但取值不是字符串：无法据它归属任何引用，按不可用处理。
            return None, None, False
        if not reference:
            # 缺失或空串都是「这份文档没有引用」（与 `_to_saved()` 同口径）。
            reference = None
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            revision = None
        return reference, revision, True
