"""档案记录、身份与目录（LIGHT_NEXT_GENERATION §4.1、§5.1）。

`profiles/<profile_id>/profile.json` 承载稳定身份、生命周期状态与
`profile_revision`（UTF-8 JSON，键固定）；`launcher.json` 继续只放活动指针与
目录字段。本模块是这两个文档的**档案级**入口：

- 查询（`catalog()`、`list_profiles()`、`expected_site_user_id()`）只读，不创建、
  不补写；`profile.json` 不存在是 v1 档案的正常形态，按降级默认返回；
- 创建只有两个入口：向导写路径的 `ensure_first_profile()` 与账号页的
  `create_profile()`（§4.2）；
- `activate()` 是低层激活，调用方负责先停稳 Worker 并确认退出（§5.1 第 2 条），
  N1 不把它暴露成 HTTP 路由；`commit_activation()` 是生命周期协调器在 A→B 事务的
  `commit_active_B` 阶段用的提交入口：与 `activate()` 同语义，另把 `catalog_revision`
  一起 +1（§58），使旧页面持有的目录 revision 自然过期；
- `bind_identity()` 是绑定站点稳定 ID 的唯一入口：一个稳定 ID 只属于一个可用档案；
- `set_state()` 是生命周期状态的唯一写入口（`active` / `detached` / `deleting`），
  `clear_activation()` 是删除活动档案时的清空指针入口；`list_profiles()` **跳过**
  带墓碑（`removed.json`）的已删除档案，目录本身与墓碑留给后续维护（§6.2、D-145）。

本模块不复制账号名：登录账号仍以 `config.yaml` 的 `_launcher.account` 为唯一来源，
把同一个事实写进 `profile.json` 只会制造第二份副本。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths
from .config_service import (
    LAUNCHER_SCHEMA_VERSION,
    ConfigInvalid,
    ConfigService,
    ConfigServiceError,
)

# `profile.json` 本次写入的 schema 版本；读到更大的值不猜格式，按不支持处理。
PROFILE_SCHEMA_VERSION: int = 1

# 身份状态：只有 `verified` 会被信任，其余（含未知值）一律当作未验证。
IDENTITY_UNVERIFIED: str = "unverified"
IDENTITY_VERIFIED: str = "verified"

# 生命周期状态：N1 只写 `active`；`detached` / `deleting` 由 N2 引入，
# 读到未知值原样保留、不报错，只有查重时 `detached` 不占用稳定 ID。
PROFILE_STATE_ACTIVE: str = "active"
# `detached`：保留本地数据的移除（凭据已清除、不再参与活动指针；查重时不算占用）。
PROFILE_STATE_DETACHED: str = "detached"
# `deleting`：删除事务已经落记录（拒绝启动与配置/草稿写入），可重试续做。
PROFILE_STATE_DELETING: str = "deleting"
PROFILE_STATES: frozenset[str] = frozenset(
    {PROFILE_STATE_ACTIVE, PROFILE_STATE_DETACHED, PROFILE_STATE_DELETING}
)

# 档案记录的读故障（稳定码，直接进 API 与状态）。
PROFILE_CORRUPT: str = "profile_corrupt"
PROFILE_UNREADABLE: str = "profile_unreadable"
PROFILE_UNSUPPORTED_VERSION: str = "profile_unsupported_version"

# 同一站点稳定 ID 已被另一个可用档案占用。
PROFILE_IDENTITY_TAKEN: str = "profile_identity_taken"


class ProfileError(ConfigServiceError):
    """档案记录的稳定错误；消息是稳定类别码（`api._handle` 因此自动给 409）。"""


@dataclass(frozen=True)
class CatalogView:
    """`launcher.json` 目录字段的只读快照（§4.1）。"""

    active_profile_id: str | None
    active_epoch: int
    catalog_revision: int
    schema_version: int


@dataclass(frozen=True)
class ProfileRecord:
    """`profile.json` 的只读快照，字段与文档一一对应。

    `profile_revision == 0` 且 `identity_state == "unverified"` 表示「还没有记录」
    （v1 档案目录）：这是降级默认，不是读故障。
    """

    profile_id: str
    display_name: str
    site_user_id: str | None
    identity_state: str
    state: str
    profile_revision: int
    created_at: str
    schema_version: int = PROFILE_SCHEMA_VERSION


def _counter(value: Any) -> int:
    """读目录里的非负计数；缺失或类型不对按 0 兜底（手工编辑不产生 ValueError）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


class ProfileService:
    """一个数据根上的档案记录与身份入口；所有方法线程安全。

    持有调用方注入的**同一个**未绑定 `ConfigService`（`base_config_service()` 每次
    返回该对象）：未绑定实例的 `_bootstrap_lock` 是每实例一把，只有 `for_profile()`
    的写锁被共享，所以同一个数据根上并发出现两个未绑定实例会让首建防重失效。
    `config_service(profile_id)` 按 id 缓存绑定实例，读写一次请求只解析一次档案上下文。

    本模块的读—改—写（`profile.json` 的 revision、目录计数）在自身可重入锁内串行；
    `launcher.json` 的落盘仍经 `ConfigService.update_catalog()`，复用它的共享写锁。
    """

    def __init__(
        self,
        data_root: Path,
        *,
        base_config_service: ConfigService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`data_root` 必须与 `base_config_service` 的数据根一致；`clock` 缺省为 UTC 墙钟。"""
        self._root = Path(data_root)
        self._base = base_config_service
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._services: dict[str, ConfigService] = {}

    # --- 目录（launcher.json） --------------------------------------------

    def catalog(self) -> CatalogView:
        """目录字段的只读快照；元数据故障按 N0 抛稳定错误，缺字段按 0/1 兜底。

        指针缺失或不是合法档案 ID 时 `active_profile_id` 是 `None`：这是视图能如实
        表达的「没有可用指针」；「文件存在但没有指针」属于元数据故障，由
        `ConfigService.profile_or_none()` 一侧报 `metadata_pointer_invalid`。
        """
        metadata = self._base.read_launcher_metadata()
        return CatalogView(
            active_profile_id=self._pointer(metadata.get("active_profile")),
            active_epoch=_counter(metadata.get("active_epoch")),
            catalog_revision=_counter(metadata.get("catalog_revision")),
            schema_version=self._schema_version(metadata.get("schema_version")),
        )

    def active_profile_id(self) -> str | None:
        """活动档案 id；只读，不创建（无档案时返回 None）。"""
        return self.catalog().active_profile_id

    @staticmethod
    def _pointer(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            return paths.validate_profile_id(value)
        except ValueError:
            return None

    @staticmethod
    def _schema_version(value: Any) -> int:
        # 缺字段是既有文件的正常形态（按 1 读）；更大的值已在元数据读取处拒绝。
        if isinstance(value, bool) or not isinstance(value, int):
            return 1
        return value

    def _new_document(
        self,
        profile_id: str,
        *,
        display_name: str = "",
        site_user_id: str | None = None,
    ) -> dict[str, Any]:
        """一份新记录：键与顺序固定，`profile_revision` 从 1 起。

        `site_user_id` 只是预占（防止重复添加同一账号），身份要经 `bind_identity()`
        在站点登录验证后才算 `verified`。
        """
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profile_id": profile_id,
            "display_name": display_name,
            "site_user_id": site_user_id,
            "identity_state": IDENTITY_UNVERIFIED,
            "state": PROFILE_STATE_ACTIVE,
            "profile_revision": 1,
            "created_at": self._clock().isoformat(),
        }

    # --- 创建 -------------------------------------------------------------

    def create_profile(
        self, *, display_name: str = "", site_user_id: str | None = None
    ) -> str:
        """新建**非活动**档案：新目录 + `profile.json`，`catalog_revision` +1（§4.2）。

        已有的活动指针不动。`site_user_id` 给定时先跨档案查重（`detached` 也算占用：
        重复添加要引导回已有档案，不复制 SQLite/记忆）。

        边界：`launcher.json` **不存在**的全新根目录上，本次创建就是「从无到有」，
        同一次写入里显式带上 `active_profile`、`active_epoch` 与 schema —— 否则会留下
        一个没有 `active_profile` 的文件，此后所有读都判 `metadata_pointer_invalid`，
        而 `ensure_first_profile()` 按 F2 拒绝修复它。文件存在时绝不改动指针。
        """
        if not isinstance(display_name, str):
            raise ConfigInvalid(
                "显示名必须是字符串", code="invalid_value", field="display_name"
            )
        if site_user_id is not None:
            self._require_site_user_id(site_user_id)
        with self._lock:
            # 先读目录：元数据故障在建立任何目录之前就抛出，不留下孤儿档案。
            catalog = self.catalog()
            fresh_catalog = not paths.launcher_json_path(self._root).exists()
            if site_user_id is not None and self.find_by_site_user_id(site_user_id):
                raise ProfileError(PROFILE_IDENTITY_TAKEN)
            profile_id = paths.new_profile_id()
            profile = paths.profile_dir(self._root, profile_id)
            self._write_document(
                profile,
                self._new_document(
                    profile_id, display_name=display_name, site_user_id=site_user_id
                ),
            )
            changes: dict[str, Any] = {
                "catalog_revision": catalog.catalog_revision + 1
            }
            if fresh_catalog:
                changes["active_profile"] = profile_id
                changes["active_epoch"] = catalog.active_epoch + 1
                # 文件是本模块新建的，直接写当前 schema；不是对既有文件的隐式升级。
                changes["schema_version"] = LAUNCHER_SCHEMA_VERSION
            self._base.update_catalog(changes)
            return profile_id

    def ensure_first_profile(self) -> str:
        """首个档案的唯一写入口：建立（无档案时）或补写缺失的 `profile.json`。

        建立条件与四类元数据故障的处理完全由 `ConfigService.ensure_first_profile()`
        决定（D-130）；本方法只负责让记录存在。已有记录**不覆盖**：读到损坏或读不了
        时原样抛出，修现场不是这里的职责。
        """
        with self._lock:
            profile_id = self._base.ensure_first_profile()
            profile = paths.profile_dir(self._root, profile_id)
            if self._read_document(profile) is None:
                self._write_document(profile, self._new_document(profile_id))
            return profile_id

    # --- 激活 -------------------------------------------------------------

    def activate(self, profile_id: str, *, expected_epoch: int | None = None) -> int:
        """低层激活：写活动指针并把 `active_epoch` +1，返回新代次。

        **前置条件由调用方保证：先停稳 Worker 并确认进程已退出**（§5.1 第 2 条）——
        活动指针一改，旧 Worker 的写入就归属到新档案。`expected_epoch` 与当前代次
        不符时抛 `revision_conflict`，不写任何东西。N1 不把它暴露成 HTTP 路由；
        本方法也不校验目标档案是否存在（调用方给的是解析出来的档案 id）。
        """
        return self._write_active_pointer(
            profile_id, expected_epoch=expected_epoch, bump_catalog_revision=False
        )

    def commit_activation(self, profile_id: str, *, expected_epoch: int | None = None) -> int:
        """协调器提交活动指针：与 `activate()` 同语义，另把 `catalog_revision` +1。

        `LifecycleService` 的 A→B 事务在 `commit_active_B` 阶段经它提交（§5.2、§58）。
        写锁内一次写入 `active_profile` / `active_epoch + 1` / `catalog_revision + 1`：
        要么三个字段一起生效，要么一个都不动（`update_catalog()` 的原子写）。目录
        revision 因此跟着前进，别的页面拿旧值再提交会被 `revision_conflict` 挡住。
        `expected_epoch` 与当前代次不符时抛 `revision_conflict`，不写任何东西；返回
        写入后的新代次（与 `activate()` 一致）。`activate()` 的既有行为不变。
        """
        return self._write_active_pointer(
            profile_id, expected_epoch=expected_epoch, bump_catalog_revision=True
        )

    def _write_active_pointer(
        self,
        profile_id: str | None,
        *,
        expected_epoch: int | None,
        bump_catalog_revision: bool,
    ) -> int:
        """激活入口共用的「读—校验代次—一次写入」；差异只有目录 revision。

        `profile_id=None` 是**清空**指针（删除活动档案，§6.2）：写出的
        `active_profile` 键仍在、值为 null，`config_service` 一侧据此报
        `no_selection` 而不是元数据故障（D-145）。
        """
        with self._lock:
            catalog = self.catalog()
            if expected_epoch is not None and catalog.active_epoch != expected_epoch:
                raise ProfileError("revision_conflict")
            epoch = catalog.active_epoch + 1
            changes: dict[str, Any] = {
                "active_profile": profile_id,
                "active_epoch": epoch,
            }
            if bump_catalog_revision:
                changes["catalog_revision"] = catalog.catalog_revision + 1
            self._base.update_catalog(changes)
            return epoch

    def clear_activation(self, *, expected_epoch: int | None = None) -> int:
        """清空活动指针：`active_profile=None`、`active_epoch` 与 `catalog_revision` 各 +1。

        删除活动档案时由移除流程调用（§6.2 第 3 步），**在 N1 基础上新增**：
        一次写入三个目录字段，绝不顺手选中别的档案（那是用户的决定，不是删除的
        副作用）。返回新代次；`expected_epoch` 不符抛 `revision_conflict`。
        """
        return self._write_active_pointer(
            None, expected_epoch=expected_epoch, bump_catalog_revision=True
        )

    # --- 生命周期状态（N2） -----------------------------------------------

    def set_state(
        self,
        profile_id: str,
        *,
        state: str,
        expected_profile_revision: int | None = None,
    ) -> ProfileRecord:
        """写档案的生命周期状态并把 `profile_revision` +1，返回写入后的记录。

        `state` 只允许 `active` / `detached` / `deleting`，其余值抛
        `invalid_profile_state`（同一套取值集合，不额外发明状态）。`deleting` 是
        删除事务的「已登记」标记：此后该档案不得启动、不得写配置与草稿（§6.2 第 1 步）。
        `expected_profile_revision` 给定时与当前记录不符抛 `revision_conflict`，
        且不写任何东西（页面并发编辑因此不会互相覆盖）。

        记录缺失（v1 档案目录）时按默认记录起写，与 `bind_identity()` 同口径；
        本方法不创建档案目录。
        """
        if state not in PROFILE_STATES:
            raise ProfileError("invalid_profile_state")
        with self._lock:
            profile = paths.profile_dir(self._root, profile_id)
            if not profile.is_dir():
                raise ProfileError(PROFILE_UNREADABLE)
            document = self._read_document(profile)
            if document is None:
                document = self._new_document(profile_id)
            revision = self._revision(document)
            if expected_profile_revision is not None and expected_profile_revision != revision:
                raise ProfileError("revision_conflict")
            document["state"] = state
            document["profile_revision"] = revision + 1
            self._write_document(profile, document)
            return self._to_record(profile_id, document)

    # --- 身份 -------------------------------------------------------------

    def bind_identity(
        self,
        profile_id: str,
        *,
        site_user_id: str,
        display_name: str | None = None,
    ) -> ProfileRecord:
        """把站点稳定 ID 绑到一个档案上，写 `identity_state="verified"`。

        这是绑定身份的唯一入口（N2 由一次性验证票据消费时调用）。跨档案查重：
        另一个 `state != "detached"` 的档案已占用同一 ID 时抛 `profile_identity_taken`，
        且在任何写入之前失败。目标目录必须已经存在：本方法不创建档案。
        """
        self._require_site_user_id(site_user_id)
        if display_name is not None and not isinstance(display_name, str):
            raise ConfigInvalid(
                "显示名必须是字符串", code="invalid_value", field="display_name"
            )
        with self._lock:
            profile = paths.profile_dir(self._root, profile_id)
            if not profile.is_dir():
                raise ProfileError(PROFILE_UNREADABLE)
            for record in self.list_profiles():
                if record.profile_id == profile_id:
                    continue
                if record.state == PROFILE_STATE_DETACHED:
                    continue
                if record.site_user_id == site_user_id:
                    raise ProfileError(PROFILE_IDENTITY_TAKEN)
            document = self._read_document(profile)
            if document is None:
                # 记录缺失（v1 档案）时按默认记录起写，不把空对象当成「没有」。
                document = self._new_document(profile_id)
            document["identity_state"] = IDENTITY_VERIFIED
            document["site_user_id"] = site_user_id
            if display_name is not None:
                document["display_name"] = display_name
            document["profile_revision"] = self._revision(document) + 1
            self._write_document(profile, document)
            return self._to_record(profile_id, document)

    def expected_site_user_id(self, profile_id: str) -> str | None:
        """该档案期望的站点稳定 ID；未验证或没有 ID 时返回 None（Task 4 用它决定是否注入校验）。

        只有 `identity_state == "verified"` 且 ID 非空的档案才产生期望值：预占的 ID
        （`create_profile(site_user_id=...)`）在验证之前不参与身份校验。
        """
        record = self._read_record(profile_id)
        if record.identity_state == IDENTITY_VERIFIED and record.site_user_id:
            return record.site_user_id
        return None

    def find_by_site_user_id(self, site_user_id: str) -> ProfileRecord | None:
        """按站点稳定 ID 找档案（N2 的「引导回已有档案」用）；按 id 排序取第一个。"""
        for record in self.list_profiles():
            if record.site_user_id == site_user_id:
                return record
        return None

    @staticmethod
    def _require_site_user_id(site_user_id: Any) -> None:
        if not isinstance(site_user_id, str) or not site_user_id.strip():
            raise ConfigInvalid(
                "站点用户 ID 不能为空", code="invalid_value", field="site_user_id"
            )

    # --- 读取 -------------------------------------------------------------

    def list_profiles(self) -> tuple[ProfileRecord, ...]:
        """`profiles/` 下每个档案目录一条记录，按 `profile_id` 排序。

        目录名非法或不是目录的条目跳过（不报错、不删除）；记录读不出来时如实抛
        `profile_corrupt` / `profile_unreadable` / `profile_unsupported_version`，
        绝不把「读不出来」当成「没有」。

        **已删除的档案不出现在这里**：目录里有墓碑（`removed.json`）即跳过（§6.2、
        D-145）。目录本身与墓碑保留给后续维护，本方法不报错、不删除、也不影响
        `catalog()`；`catalog` 里的活动指针是另一回事，由清空指针的写入者负责。
        """
        try:
            entries = list(paths.profiles_root(self._root).iterdir())
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise ProfileError(PROFILE_UNREADABLE) from exc
        records: list[ProfileRecord] = []
        for entry in entries:
            try:
                paths.validate_profile_id(entry.name)
            except ValueError:
                continue
            if not entry.is_dir():
                continue
            if paths.removed_json_path(entry).exists():
                continue
            records.append(self._to_record(entry.name, self._read_document(entry)))
        records.sort(key=lambda record: record.profile_id)
        return tuple(records)

    def _read_record(self, profile_id: str) -> ProfileRecord:
        profile = paths.profile_dir(self._root, profile_id)
        return self._to_record(profile_id, self._read_document(profile))

    def _to_record(
        self, profile_id: str, document: Mapping[str, Any] | None
    ) -> ProfileRecord:
        """把文档转成记录；缺字段按默认值兜底，未知 `state` 原样保留。"""
        if document is None:
            return ProfileRecord(
                profile_id=profile_id,
                display_name="",
                site_user_id=None,
                identity_state=IDENTITY_UNVERIFIED,
                state=PROFILE_STATE_ACTIVE,
                profile_revision=0,
                created_at="",
            )
        site_user_id = document.get("site_user_id")
        if not isinstance(site_user_id, str) or not site_user_id.strip():
            site_user_id = None
        identity_state = document.get("identity_state")
        state = document.get("state")
        display_name = document.get("display_name")
        created_at = document.get("created_at")
        return ProfileRecord(
            profile_id=profile_id,
            display_name=display_name if isinstance(display_name, str) else "",
            site_user_id=site_user_id,
            identity_state=(
                identity_state if isinstance(identity_state, str) else IDENTITY_UNVERIFIED
            ),
            state=state if isinstance(state, str) else PROFILE_STATE_ACTIVE,
            profile_revision=self._revision(document),
            created_at=created_at if isinstance(created_at, str) else "",
            schema_version=self._schema_version(document.get("schema_version")),
        )

    @staticmethod
    def _revision(document: Mapping[str, Any]) -> int:
        return _counter(document.get("profile_revision"))

    # --- 子服务 -----------------------------------------------------------

    def config_service(self, profile_id: str) -> ConfigService:
        """该档案的绑定 `ConfigService`（按 id 缓存；工厂保证共享写锁）。"""
        with self._lock:
            bound = self._services.get(profile_id)
            if bound is None:
                bound = self._base.for_profile(profile_id)
                self._services[profile_id] = bound
            return bound

    def base_config_service(self) -> ConfigService:
        """数据根级实例；「还没有档案」的查询路径用它（与 `for_profile()` 共享写锁）。"""
        return self._base

    # --- 内部：落盘 --------------------------------------------------------

    def _read_document(self, profile: Path) -> dict[str, Any] | None:
        """读 `profile.json`；不存在返回 None（v1 档案的正常形态，不补写）。

        存在但读不出来时抛三种稳定故障码之一，且**绝不覆盖现场**：非 UTF-8、非法
        JSON、顶层不是对象与版本字段类型不对都算 `profile_corrupt`；`OSError` 是
        `profile_unreadable`；版本更大是 `profile_unsupported_version`。
        """
        path = paths.profile_json_path(profile)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProfileError(PROFILE_UNREADABLE) from exc
        except UnicodeDecodeError as exc:
            # `UnicodeDecodeError` 不是 `OSError`，漏接会把解码错误漏成 500。
            raise ProfileError(PROFILE_CORRUPT) from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProfileError(PROFILE_CORRUPT) from exc
        if not isinstance(document, dict):
            raise ProfileError(PROFILE_CORRUPT)
        version = document.get("schema_version")
        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int):
                raise ProfileError(PROFILE_CORRUPT)
            if version > PROFILE_SCHEMA_VERSION:
                raise ProfileError(PROFILE_UNSUPPORTED_VERSION)
        return document

    def _write_document(self, profile: Path, document: Mapping[str, Any]) -> None:
        """同目录临时文件 + flush + fsync + 原子替换：要么旧版本、要么新版本。"""
        payload = (
            json.dumps(dict(document), ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        path = paths.profile_json_path(profile)
        directory = path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
            )
        except OSError as exc:
            raise ConfigServiceError("config_write_failed") from exc
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
            # 写盘失败只有一种对外语义：这一版没有生效（§6.4）。
            raise ConfigServiceError("config_write_failed") from exc
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
