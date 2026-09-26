"""v1 无损接管：把「单档案 + schema 1」的既有安装迁到目录 schema 2（设计 §10）。

四条硬口径：

1. **完全离线**：不登录站点、不启动 Worker、不请求数据档案锁。单实例互斥体由
   `main.py` 在构造 Controller 之前取得，迁移因此满足 §10.1「停稳 Worker」；
2. **先备份后写**：动任何东西之前，把要改动的**非敏感**文件复制到
   `migration/backup-<UTC 紧凑时间戳>/`（保留相对目录结构），`manifest.json`
   最后写 —— 半份备份因此可识别；备份里绝不出现密码、模型 Key 或任何凭据取值
   （`config.yaml` / `draft.yaml` 只有非敏感字段与不透明引用，见 §6.1）；
3. **可重入**：每步在 `operations/migration-v1-to-v2.json` 里记准备/完成状态，
   已完成的步骤不重做；失败保留已完成步骤码，下次按阶段续跑；
4. **不猜**：没有可用指针（含 `profiles/` 下有多个档案目录）时停在
   `blocked_recovery_candidate`，不选「最新修改目录」、不合并同名目录、不自动接管；
   四类元数据故障一律不写任何东西。

迁移不写 `identity_state="verified"`：v1 档案的稳定站点 ID 无从得知，一律
`unverified`（§10.4），首次受控登录验证前不假定身份。操作记录只保存 ID、revision、
固定阶段码、受管相对路径与备份目录，不保存 System Prompt、聊天正文、知识库/记忆
正文、密码或原始异常。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import __version__, paths
from .config_service import (
    LAUNCHER_SCHEMA_VERSION,
    LAUNCHER_SECTION,
    METADATA_CORRUPT,
    METADATA_POINTER_INVALID,
    METADATA_UNREADABLE,
    METADATA_UNSUPPORTED_VERSION,
    ConfigService,
    ConfigServiceError,
)
from .profile_service import (
    IDENTITY_UNVERIFIED,
    PROFILE_SCHEMA_VERSION,
    PROFILE_STATE_ACTIVE,
)

# `inspect()` 的阶段码（稳定字符串，直接进日志与记录）。
STAGE_NOTHING_TO_MIGRATE: str = "nothing_to_migrate"
STAGE_ALREADY_MIGRATED: str = "already_migrated"
STAGE_MIGRATABLE: str = "migratable"
STAGE_BLOCKED_METADATA_FAULT: str = "blocked_metadata_fault"
STAGE_BLOCKED_RECOVERY_CANDIDATE: str = "blocked_recovery_candidate"
# `migrate()` 成功收尾的阶段码；记录文件里的 stage 用它。
STAGE_COMPLETED: str = "completed"
# 记录文件里的中间状态：半迁移的根目录靠它可识别（§10.6）。
RECORD_STAGE_IN_PROGRESS: str = "in_progress"
RECORD_STAGE_COMPLETED: str = STAGE_COMPLETED

# 四个迁移步骤的固定阶段码，顺序即执行顺序。
STEP_BACKUP: str = "backup"
STEP_PROFILE_RECORD: str = "profile_record"
STEP_CATALOG: str = "catalog"
STEP_RECORD: str = "record"
_STEP_CODES: tuple[str, ...] = (
    STEP_BACKUP,
    STEP_PROFILE_RECORD,
    STEP_CATALOG,
    STEP_RECORD,
)

# 每步的准备/完成状态（§10.6：「每步记录准备/完成状态」）。
STEP_PREPARED: str = "prepared"
STEP_DONE: str = "done"

# 备份目录与清单：`<数据根>/migration/backup-YYYYMMDDTHHMMSSZ/`。
BACKUP_PREFIX: str = "backup-"
MANIFEST_FILE: str = "manifest.json"
BACKUP_STAMP_FORMAT: str = "%Y%m%dT%H%M%SZ"

# 迁移记录：`<数据根>/operations/migration-v1-to-v2.json`（固定文件名，N2 起
# 同一目录还放切换/删除记录）。
MIGRATION_RECORD_FILE: str = "migration-v1-to-v2.json"

# N0 的四个元数据故障码：只放行它们，其余 `ConfigServiceError` 不属于「可判定的
# 元数据故障」，应由调用方按未知错误处理（不在这里吞掉）。
_METADATA_FAULT_CODES: frozenset[str] = frozenset(
    {
        METADATA_CORRUPT,
        METADATA_UNREADABLE,
        METADATA_UNSUPPORTED_VERSION,
        METADATA_POINTER_INVALID,
    }
)

# 迁移步骤失败的稳定码（不含路径与异常原文）：只报「这一次没有生效」。
MIGRATION_WRITE_FAILED: str = "migration_write_failed"


class MigrationError(ConfigServiceError):
    """迁移的稳定错误；消息是稳定类别码，绝不携带原始异常或路径。"""


@dataclass(frozen=True)
class MigrationStatus:
    """`inspect()` 的只读结论；任何取值都不代表发生过写入。"""

    stage: str
    profile_id: str | None = None
    metadata_fault: str | None = None
    backup_dir: str | None = None


@dataclass(frozen=True)
class MigrationResult:
    """一次 `migrate()` 的结果。

    `ok` 为真只表示「迁移这件事不阻塞后续启动」（没有可迁移的内容、已经迁过、
    或本次迁移成功）；`blocked_*` 与步骤失败都是假。`steps` 是已完成的步骤码，
    失败时因此能看出续跑该从哪里接上。
    """

    stage: str
    ok: bool
    profile_id: str | None = None
    metadata_fault: str | None = None
    backup_dir: str | None = None
    steps: tuple[str, ...] = ()
    error: str | None = None


def _counter(value: Any) -> int:
    """读目录里的非负计数；缺失或类型不对按 0 兜底（手工编辑不产生异常）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    """同目录临时文件 + flush + fsync + 原子替换：要么旧版本、要么新版本。

    与配置事务（§6.4）和档案记录同一手法：半写文件永远不会出现在目标名下，
    清单因此可以当作「这份备份完整」的判据。
    """
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
        )
    except OSError as exc:
        raise MigrationError(MIGRATION_WRITE_FAILED) from exc
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
        raise MigrationError(MIGRATION_WRITE_FAILED) from exc
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _dump_json(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(document), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )


class MigrationService:
    """一个数据根上的 v1 迁移入口；`inspect()` 只读，`migrate()` 幂等可重入。

    构造与 `inspect()` 都不创建目录、不写文件：真正空的根目录上跑一遍不会留下
    任何痕迹（§4.1「查询不得隐式创建」的迁移版本）。
    """

    def __init__(
        self,
        data_root: Path,
        *,
        config_service: ConfigService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`data_root` 必须与 `config_service` 的数据根一致；`clock` 缺省为 UTC 墙钟。

        时钟只用于备份目录名与记录里的时间戳：测试因此能构造确定的时间戳，
        不必依赖真实的「现在」。
        """
        self._root = Path(data_root)
        self._config = config_service
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    # --- 只读判定 ---------------------------------------------------------

    def inspect(self) -> MigrationStatus:
        """只读判定：返回阶段码与已有的档案 id、元数据故障码、备份目录。

        判定顺序与口径见模块 docstring 与 `migrate()`；本方法在任何分支上都不
        创建目录、不写文件，包括 `blocked_recovery_candidate` 与四类元数据故障。
        """
        record = self._read_record()
        backup_dir = self._recorded_backup_dir(record)
        metadata_path = paths.launcher_json_path(self._root)
        profile_dirs = self._profile_dirs()
        if not metadata_path.exists() and not profile_dirs:
            # 既没有元数据也没有档案目录：不是迁移对象，更不该留下新文件。
            return MigrationStatus(STAGE_NOTHING_TO_MIGRATE)
        try:
            metadata = self._config.read_launcher_metadata()
        except ConfigServiceError as exc:
            code = str(exc)
            if code not in _METADATA_FAULT_CODES:
                raise
            # 读到了但不能用：不猜、不修、不写（F2）。
            return MigrationStatus(STAGE_BLOCKED_METADATA_FAULT, metadata_fault=code)
        profile_id = self._usable_pointer(metadata)
        if (
            _counter(metadata.get("schema_version")) >= LAUNCHER_SCHEMA_VERSION
            and self._record_completed(record)
        ):
            return MigrationStatus(
                STAGE_ALREADY_MIGRATED,
                profile_id=profile_id or self._recorded_profile_id(record),
                backup_dir=backup_dir,
            )
        if profile_id is None:
            # 没有可用指针：多个档案目录、指针损坏或根本没有指针。不选「最新修改
            # 目录」、不合并同名目录 —— 让用户在恢复流程里明确选择。
            return MigrationStatus(STAGE_BLOCKED_RECOVERY_CANDIDATE, backup_dir=backup_dir)
        return MigrationStatus(
            STAGE_MIGRATABLE, profile_id=profile_id, backup_dir=backup_dir
        )

    def _profile_dirs(self) -> list[str]:
        """`profiles/` 下的档案目录名（按名排序）；目录名非法或不是目录的跳过。"""
        try:
            entries = list(paths.profiles_root(self._root).iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError:
            return []
        names: list[str] = []
        for entry in entries:
            try:
                paths.validate_profile_id(entry.name)
            except ValueError:
                continue
            if entry.is_dir():
                names.append(entry.name)
        names.sort()
        return names

    def _usable_pointer(self, metadata: Mapping[str, Any]) -> str | None:
        """可用的活动指针：合法档案 id **且**目录存在。

        「指针是合法 id 但目录不在」与「指针缺失/损坏」一样不可用：迁移要保持原
        `profile_id` 与数据目录，拿不到目录就没有可接管的东西。
        """
        value = metadata.get("active_profile")
        if not isinstance(value, str):
            return None
        try:
            profile_id = paths.validate_profile_id(value)
            profile = paths.profile_dir(self._root, profile_id)
        except ValueError:
            return None
        return profile_id if profile.is_dir() else None

    # --- 迁移 -------------------------------------------------------------

    def migrate(self) -> MigrationResult:
        """先 `inspect()`；不可迁移的阶段直接返回，可迁移时按四步执行。

        `nothing_to_migrate` / `already_migrated` / `blocked_*` 都是**无写入**的
        直接返回（幂等）。可迁移时按 `backup` → `profile_record` → `catalog` →
        `record` 执行，每步前后在记录文件里落准备/完成状态；步骤失败时返回
        `ok=False` 与已完成的步骤码，下次运行从这里续跑，已完成的步骤不重做。
        """
        with self._lock:
            status = self.inspect()
            if status.stage in (STAGE_NOTHING_TO_MIGRATE, STAGE_ALREADY_MIGRATED):
                return MigrationResult(
                    stage=status.stage,
                    ok=True,
                    profile_id=status.profile_id,
                    backup_dir=status.backup_dir,
                )
            if status.stage in (
                STAGE_BLOCKED_METADATA_FAULT,
                STAGE_BLOCKED_RECOVERY_CANDIDATE,
            ):
                # 元数据故障码原样上报；恢复候选没有单一故障码，stage 自己就是答案。
                return MigrationResult(
                    stage=status.stage,
                    ok=False,
                    metadata_fault=status.metadata_fault,
                    error=status.metadata_fault,
                )
            return self._migrate_v1(status)

    def _migrate_v1(self, status: MigrationStatus) -> MigrationResult:
        """可迁移根目录的四步迁移；返回结果而不是抛异常（调用方是启动路径）。"""
        profile_id = status.profile_id
        if profile_id is None:  # pragma: no cover - inspect 已保证
            raise MigrationError(STAGE_BLOCKED_RECOVERY_CANDIDATE)
        previous = self._read_record() or {}
        steps = self._step_states(previous)
        note = self._note(previous, profile_id)
        backup_dir = status.backup_dir
        # `record` 步的准备状态就是这份文件本身：迁移一开始它就落盘，半迁移的根目录
        # 因此可识别；完成状态是收尾时的 stage=completed。
        steps[STEP_RECORD] = STEP_PREPARED
        note["steps"] = _step_list(steps)
        note["backup_dir"] = backup_dir
        try:
            # 「先备份后写」是按文件成立的：迁移只**新建**恢复记录与备份目录，绝不改动
            # 任何既有文件 —— 第一个被改写的既有文件（`launcher.json`）一定在 `backup`
            # 步之后才动，`profile.json` 则是新建的。先落记录是为了让中断可诊断。
            self._write_record(note)
            if not (steps.get(STEP_BACKUP) == STEP_DONE and self._backup_complete(backup_dir)):
                steps[STEP_BACKUP] = STEP_PREPARED
                note["steps"] = _step_list(steps)
                self._write_record(note)
                backup_dir = self._run_backup(backup_dir)
                steps[STEP_BACKUP] = STEP_DONE
                note["backup_dir"] = backup_dir
                note["steps"] = _step_list(steps)
                self._write_record(note)

            profile = paths.profile_dir(self._root, profile_id)
            if not paths.profile_json_path(profile).exists():
                steps[STEP_PROFILE_RECORD] = STEP_PREPARED
                note["steps"] = _step_list(steps)
                self._write_record(note)
                self._write_profile_record(profile_id)
                steps[STEP_PROFILE_RECORD] = STEP_DONE
                note["steps"] = _step_list(steps)
                self._write_record(note)

            changes = self._catalog_changes(profile_id)
            if changes:
                steps[STEP_CATALOG] = STEP_PREPARED
                note["steps"] = _step_list(steps)
                self._write_record(note)
                self._config.update_catalog(changes)
                steps[STEP_CATALOG] = STEP_DONE
                note["steps"] = _step_list(steps)
                self._write_record(note)

            # 记录里的 revision 与代次是**实际落盘**的值，不是写死的目标值：根目录
            # 可能被手工编辑过，记录要能如实说明迁移之后的数据根长什么样。
            current = self._config.read_launcher_metadata()
            note["catalog_revision"] = _counter(current.get("catalog_revision"))
            note["active_epoch"] = _counter(current.get("active_epoch"))
            note["stage"] = RECORD_STAGE_COMPLETED
            note["completed_at"] = self._clock().isoformat()
            steps[STEP_RECORD] = STEP_DONE
            note["steps"] = _step_list(steps)
            self._write_record(note)
        except ConfigServiceError as exc:
            # 稳定码原样上报（`config_write_failed`、`invalid_catalog_change` 等），
            # 原始异常与路径不进结果。
            return self._failed(profile_id, backup_dir, steps, str(exc))
        except OSError:
            return self._failed(profile_id, backup_dir, steps, MIGRATION_WRITE_FAILED)
        return MigrationResult(
            stage=STAGE_COMPLETED,
            ok=True,
            profile_id=profile_id,
            backup_dir=backup_dir,
            steps=tuple(code for code in _STEP_CODES if steps.get(code) == STEP_DONE),
        )

    @staticmethod
    def _failed(
        profile_id: str, backup_dir: str | None, steps: Mapping[str, str], code: str
    ) -> MigrationResult:
        return MigrationResult(
            stage=STAGE_MIGRATABLE,
            ok=False,
            profile_id=profile_id,
            backup_dir=backup_dir,
            steps=tuple(
                step for step in _STEP_CODES if steps.get(step) == STEP_DONE
            ),
            error=code,
        )

    # --- 步骤：备份 -------------------------------------------------------

    def _run_backup(self, previous: str | None) -> str:
        """复制要改动的非敏感文件，最后写清单；返回备份目录的受管相对路径。"""
        target = self._backup_target(previous)
        if self._backup_complete(_relative(self._root, target)):
            # 同一位置已有完整清单（同一时间戳的上一轮，或记录里的目录）：复用。
            return _relative(self._root, target)
        entries: list[dict[str, str]] = []
        for source, relative in self._backup_files():
            destination = target / relative
            _write_bytes(destination, source.read_bytes())
            entries.append({"path": relative, "sha256": _sha256(destination)})
        manifest = {
            "created_at": self._clock().isoformat(),
            "tool_version": __version__,
            "files": entries,
            "schema_from": 1,
            "schema_to": LAUNCHER_SCHEMA_VERSION,
        }
        # 清单最后写：没有清单的备份目录就是半份备份，下次运行不会被当作完整备份。
        _write_bytes(target / MANIFEST_FILE, _dump_json(manifest))
        return _relative(self._root, target)

    def _backup_target(self, previous: str | None) -> Path:
        """备份目录：优先复用记录里的目录，否则按当前 UTC 时间戳建一个。"""
        if previous is not None:
            candidate = self._root / previous
            if paths.is_within(self._root, candidate) and candidate.is_dir():
                return candidate
        stamp = self._clock().astimezone(timezone.utc).strftime(BACKUP_STAMP_FORMAT)
        return paths.migration_dir(self._root) / f"{BACKUP_PREFIX}{stamp}"

    def _backup_files(self) -> list[tuple[Path, str]]:
        """要备份的文件与它们的受管相对路径：元数据 + 每个档案的配置类文件。

        只复制这几个**已知非敏感**文件；凭据取值在系统凭据库里，本模块不读它，
        也不存在任何「顺手把整个目录打包」的路径。
        """
        files: list[tuple[Path, str]] = []
        metadata = paths.launcher_json_path(self._root)
        if metadata.is_file():
            files.append((metadata, paths.LAUNCHER_FILE))
        for name in self._profile_dirs():
            profile = paths.profile_dir(self._root, name)
            for filename in (paths.CONFIG_FILE, paths.DRAFT_FILE, paths.PROFILE_FILE):
                path = profile / filename
                if path.is_file():
                    files.append(
                        (path, f"{paths.PROFILES_DIR}/{name}/{filename}")
                    )
        return files

    def _backup_complete(self, backup_dir: str | None) -> bool:
        """备份目录是否带一份完整清单（清单里的每个文件都在且 sha256 一致）。"""
        if not backup_dir:
            return False
        directory = self._root / backup_dir
        if not paths.is_within(self._root, directory) or not directory.is_dir():
            return False
        manifest = self._read_manifest(directory)
        if not manifest:
            # 没有清单、清单结构不对或一个文件都没列：都不是「完整备份」。
            return False
        for entry in manifest:
            path = directory / entry["path"]
            if not paths.is_within(directory, path) or not path.is_file():
                return False
            try:
                if _sha256(path) != entry["sha256"]:
                    return False
            except OSError:
                return False
        return True

    @staticmethod
    def _read_manifest(directory: Path) -> list[dict[str, str]] | None:
        """读清单的 `files` 列表；结构不对或没有文件列表一律当作「不完整」。"""
        try:
            document = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict) or not isinstance(document.get("files"), list):
            return None
        entries: list[dict[str, str]] = []
        for entry in document["files"]:
            if not isinstance(entry, dict):
                return None
            path = entry.get("path")
            digest = entry.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                return None
            entries.append({"path": path, "sha256": digest})
        return entries

    # --- 步骤：档案记录与目录字段 ------------------------------------------

    def _write_profile_record(self, profile_id: str) -> None:
        """为 v1 档案补写 `profile.json`：未验证身份、本地标签取登录账号名。

        只在文件不存在时调用（已存在不覆盖）。字段与顺序同
        `ProfileService._new_document()`：迁移是一次性导入，写的是同一份记录格式；
        身份一律 `unverified`，`site_user_id` 为 `None`（§10.4）。
        """
        profile = paths.profile_dir(self._root, profile_id)
        document = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profile_id": profile_id,
            "display_name": self._account_name(profile),
            "site_user_id": None,
            "identity_state": IDENTITY_UNVERIFIED,
            "state": PROFILE_STATE_ACTIVE,
            "profile_revision": 1,
            "created_at": self._clock().isoformat(),
        }
        _write_bytes(paths.profile_json_path(profile), _dump_json(document))

    @staticmethod
    def _account_name(profile: Path) -> str:
        """档案 `config.yaml` 的 `_launcher.account`；缺失或读不出来时空串。

        账号名只是本地标签，不是身份（D-134）；读不出来不阻断迁移，也不猜。
        """
        try:
            raw = paths.config_path(profile).read_bytes()
            document = yaml.safe_load(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return ""
        if not isinstance(document, Mapping):
            return ""
        launcher = document.get(LAUNCHER_SECTION)
        if not isinstance(launcher, Mapping):
            return ""
        account = launcher.get("account")
        return account if isinstance(account, str) else ""

    def _catalog_changes(self, profile_id: str) -> dict[str, Any]:
        """本次还需要写进 `launcher.json` 的目录字段；都已到位就是空映射。

        已到位的值**不重写**：同值 `schema_version` 会被 `update_catalog()` 拒绝
        （只升不降，同级也算拒绝），而且重写没有任何意义。`active_profile` 只在
        缺失或不可用时才补 —— 迁移绝不移动已有的指针（D-133 的调用前提）。
        """
        metadata = self._config.read_launcher_metadata()
        changes: dict[str, Any] = {}
        if _counter(metadata.get("schema_version")) < LAUNCHER_SCHEMA_VERSION:
            changes["schema_version"] = LAUNCHER_SCHEMA_VERSION
        if _counter(metadata.get("catalog_revision")) == 0:
            changes["catalog_revision"] = 1
        if _counter(metadata.get("active_epoch")) == 0:
            changes["active_epoch"] = 1
        if not self._usable_pointer(metadata):
            changes["active_profile"] = profile_id
        return changes

    # --- 迁移记录 ---------------------------------------------------------

    def _record_path(self) -> Path:
        return paths.operations_dir(self._root) / MIGRATION_RECORD_FILE

    def _note(self, previous: Mapping[str, Any], profile_id: str) -> dict[str, Any]:
        """新的记录文档：跨次续跑保留 `started_at`，其余字段由步骤推进填写。"""
        started_at = previous.get("started_at")
        if not isinstance(started_at, str) or not started_at:
            started_at = self._clock().isoformat()
        return {
            "from_schema": 1,
            "to_schema": LAUNCHER_SCHEMA_VERSION,
            "stage": RECORD_STAGE_IN_PROGRESS,
            "profile_id": profile_id,
            # revision 与代次在 `catalog` 步之后按实际落盘值填（见 `_migrate_v1`）。
            "catalog_revision": None,
            "active_epoch": None,
            "backup_dir": None,
            # 受管相对路径：这份记录说明迁移改动了哪些文件，不含任何正文与凭据。
            "managed_paths": [
                paths.LAUNCHER_FILE,
                f"{paths.PROFILES_DIR}/{profile_id}/{paths.PROFILE_FILE}",
                f"{paths.OPERATIONS_DIR}/{MIGRATION_RECORD_FILE}",
            ],
            "steps": [],
            "started_at": started_at,
            "completed_at": None,
        }

    def _write_record(self, note: Mapping[str, Any]) -> None:
        path = self._record_path()
        if not paths.is_within(self._root, path):
            # `operations/` 被做成指向数据根之外的重解析点时绝不写出去（§9.5）。
            raise MigrationError(MIGRATION_WRITE_FAILED)
        _write_bytes(path, _dump_json(note))

    def _read_record(self) -> dict[str, Any] | None:
        """读迁移记录；缺失或读不出来返回 None（记录只是恢复提示，不阻塞续跑）。"""
        try:
            raw = self._record_path().read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, OSError, UnicodeDecodeError):
            return None
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return document if isinstance(document, dict) else None

    @staticmethod
    def _step_states(record: Mapping[str, Any]) -> dict[str, str]:
        """从记录里取已知步骤状态；未知步骤码与未知状态一律丢弃。"""
        states: dict[str, str] = {}
        steps = record.get("steps")
        if not isinstance(steps, list):
            return states
        for entry in steps:
            if not isinstance(entry, Mapping):
                continue
            code = entry.get("code")
            state = entry.get("state")
            if code in _STEP_CODES and state in (STEP_PREPARED, STEP_DONE):
                states[code] = state
        return states

    @staticmethod
    def _record_completed(record: Mapping[str, Any] | None) -> bool:
        return bool(record) and record.get("stage") == RECORD_STAGE_COMPLETED

    @staticmethod
    def _recorded_profile_id(record: Mapping[str, Any] | None) -> str | None:
        if not record:
            return None
        value = record.get("profile_id")
        if not isinstance(value, str):
            return None
        try:
            return paths.validate_profile_id(value)
        except ValueError:
            return None

    def _recorded_backup_dir(self, record: Mapping[str, Any] | None) -> str | None:
        """记录里的备份目录（只接受数据根内已存在的受管相对路径）。"""
        if not record:
            return None
        value = record.get("backup_dir")
        if not isinstance(value, str) or not value:
            return None
        directory = self._root / value
        if not paths.is_within(self._root, directory) or not directory.is_dir():
            return None
        return value


def _relative(root: Path, path: Path) -> str:
    """受管相对路径（一律用 `/` 分隔，跨平台可读可比较）。"""
    return path.relative_to(root).as_posix()


def _step_list(states: Mapping[str, str]) -> list[dict[str, str]]:
    """固定顺序的步骤状态列表：只列已经走到过的步骤码。"""
    return [{"code": code, "state": states[code]} for code in _STEP_CODES if code in states]
