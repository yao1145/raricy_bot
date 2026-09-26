"""桌面设置：`desktop.json` 的读写与独立 settings revision（设计 §6、§8）。

文件放在数据根下、与 `launcher.json` 同级（§13.1），只放**桌面偏好**：登录自启动
意图、打开程序是否启动机器人、启动目标档案，以及启动项最近一次应用的诊断
（`last_apply_result`、`pending_startup_apply`）。三条不变量：

1. **唯一来源**：桌面偏好只认这一个文件。本模块不读档案 `config.yaml` 里的
   `start_bot_on_launch`，唯一例外是升级用户的一次性导入（见
   `DesktopSettingsService._import_legacy_preference`）：`desktop.json` 不存在而
   `launcher.json` 存在时，把当前档案的旧偏好写进新文件，写完即不再回退。
   N1 的 `migration.py` 落地后，这条回退仍是升级用户的唯一导入路径。
2. **独立 revision**：`settings_revision` 只在**用户意图**改变时 +1，与档案配置的
   revision 无关（写入者与冲突面都不同）。`last_apply_result` 与
   `pending_startup_apply` 由 `record_apply_result()` 单独记录，不改 revision ——
   它们说的是「系统侧实际发生了什么」，与意图分开保存。
3. **查询不修复、不创建、不覆盖**（与 D-130 同口径）：文件损坏或版本不认识时只报
   稳定错误码，不改写现场、不重建文件；写盘一律「同目录临时文件 + flush + fsync +
   `os.replace`」，要么旧版本、要么新版本。

线程安全：一把 `RLock` 覆盖「读-改-写」，持锁期间只做内存操作与单次文件替换，
不等待任何外部资源。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from raricy_bot import config as core_config

from . import paths

# 本程序认识的 `desktop.json` 版本（确切值总表：桌面设置文件 `schema_version = 1`）。
DESKTOP_SCHEMA_VERSION: int = 1

# 启动项的稳定结果码：`not_attempted` 是文件不存在时的默认值，其余由启动项服务
# （N4 Task 3）在应用/回读之后写入。
DEFAULT_LAST_APPLY_RESULT: str = "not_attempted"
LAST_APPLY_RESULTS: frozenset[str] = frozenset(
    {
        "ok",
        DEFAULT_LAST_APPLY_RESULT,
        "command_too_long",
        "path_unusable",
        "registration_conflict",
        "apply_failed",
        "read_failed",
    }
)

# 稳定错误码（确切值总表）：`str(exc)` 就是它，绝不含路径、命令或异常原文。
DESKTOP_SETTINGS_CONFLICT: str = "desktop_settings_conflict"
INVALID_DESKTOP_SETTINGS: str = "invalid_desktop_settings"
DESKTOP_UNREADABLE: str = "desktop_unreadable"
DESKTOP_CORRUPT: str = "desktop_corrupt"
DESKTOP_UNSUPPORTED_VERSION: str = "desktop_unsupported_version"
# 写盘失败只有一种对外语义：这一版没有生效，旧文件保持不变（与配置提交同口径）。
DESKTOP_SETTINGS_WRITE_FAILED: str = "desktop_settings_write_failed"

# `pending_startup_apply.action` 的取值（确切值总表）。
PENDING_ACTIONS: frozenset[str] = frozenset({"register", "unregister"})


class DesktopSettingsError(Exception):
    """桌面设置的固定错误；消息是稳定类别码（不是中文文案）。"""


class DesktopSettingsConflict(DesktopSettingsError):
    """`expected_revision` 与当前 `settings_revision` 不符（L3 映射为 409）。"""


class _Unset:
    """`_UNSET` 的类型；只用来区分「本次不改」与「显式置 null」。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "_UNSET"


# 默认参数哨兵：`startup_profile_id=None` 表示**清空**目标档案，与「不带这个参数」
# 是两件事，因此不能用 None 当默认值（确切值总表）。
_UNSET = _Unset()


@dataclass(frozen=True)
class PendingStartupApply:
    """「上次想写进注册表什么」的诊断记录；**不参与重放**（确切值总表）。

    修复一律按当前 EXE 路径重新生成命令，否则搬目录后会把旧路径又写回注册表。
    """

    action: str
    command: str

    def __post_init__(self) -> None:
        if self.action not in PENDING_ACTIONS:
            raise ValueError("invalid_startup_action")
        if not isinstance(self.command, str) or not self.command:
            raise ValueError("invalid_startup_command")


@dataclass(frozen=True)
class DesktopSettings:
    """`desktop.json` 的一份非敏感快照（确切值总表的六个字段）。"""

    settings_revision: int
    launch_at_sign_in: bool
    start_bot_on_launch: bool
    startup_profile_id: str | None
    pending_startup_apply: PendingStartupApply | None
    last_apply_result: str


def _default_settings() -> DesktopSettings:
    return DesktopSettings(
        settings_revision=0,
        launch_at_sign_in=False,
        start_bot_on_launch=False,
        startup_profile_id=None,
        pending_startup_apply=None,
        last_apply_result=DEFAULT_LAST_APPLY_RESULT,
    )


def _to_document(settings: DesktopSettings) -> dict[str, Any]:
    """整份文档：每个字段都写出来，缺字段只可能来自手工编辑。"""
    pending = settings.pending_startup_apply
    return {
        "schema_version": DESKTOP_SCHEMA_VERSION,
        "settings_revision": settings.settings_revision,
        "launch_at_sign_in": settings.launch_at_sign_in,
        "start_bot_on_launch": settings.start_bot_on_launch,
        "startup_profile_id": settings.startup_profile_id,
        "pending_startup_apply": (
            None if pending is None else {"action": pending.action, "command": pending.command}
        ),
        "last_apply_result": settings.last_apply_result,
    }


def _parse_bool(document: Mapping[str, Any], key: str) -> bool:
    """缺字段按默认 false；类型不对（含 0/1、"true"）视为损坏，不猜。"""
    value = document.get(key, False)
    if not isinstance(value, bool):
        raise DesktopSettingsError(DESKTOP_CORRUPT)
    return value


def _parse_document(document: Mapping[str, Any]) -> DesktopSettings:
    """解析并校验一份 `desktop.json`；任何看不懂的取值都报损坏。"""
    version = document.get("schema_version")
    if version is not None:
        if isinstance(version, bool) or not isinstance(version, int):
            raise DesktopSettingsError(DESKTOP_CORRUPT)
        if version > DESKTOP_SCHEMA_VERSION:
            # 版本比本程序新就不猜：宁可报告不支持，也不按未知格式解释（§6.5）。
            raise DesktopSettingsError(DESKTOP_UNSUPPORTED_VERSION)
    revision = document.get("settings_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise DesktopSettingsError(DESKTOP_CORRUPT)
    launch_at_sign_in = _parse_bool(document, "launch_at_sign_in")
    start_bot_on_launch = _parse_bool(document, "start_bot_on_launch")
    target = document.get("startup_profile_id")
    if target is not None:
        if not isinstance(target, str):
            raise DesktopSettingsError(DESKTOP_CORRUPT)
        try:
            paths.validate_profile_id(target)
        except ValueError as exc:
            # 非法档案 id 不能让界面把它当目标；与 launcher.json 的指针同一口径。
            raise DesktopSettingsError(DESKTOP_CORRUPT) from exc
    pending_value = document.get("pending_startup_apply")
    pending: PendingStartupApply | None = None
    if pending_value is not None:
        if not isinstance(pending_value, Mapping):
            raise DesktopSettingsError(DESKTOP_CORRUPT)
        action = pending_value.get("action")
        command = pending_value.get("command")
        if action not in PENDING_ACTIONS or not isinstance(command, str) or not command:
            raise DesktopSettingsError(DESKTOP_CORRUPT)
        pending = PendingStartupApply(action=action, command=command)
    result = document.get("last_apply_result", DEFAULT_LAST_APPLY_RESULT)
    if not isinstance(result, str) or result not in LAST_APPLY_RESULTS:
        raise DesktopSettingsError(DESKTOP_CORRUPT)
    return DesktopSettings(
        settings_revision=revision,
        launch_at_sign_in=launch_at_sign_in,
        start_bot_on_launch=start_bot_on_launch,
        startup_profile_id=target,
        pending_startup_apply=pending,
        last_apply_result=result,
    )


class DesktopSettingsService:
    """`desktop.json` 的唯一读写入口（§6、§8）。

    每一次公开调用都在同一把锁里完成「读当前 → 改内存 → 单次文件替换」；写盘失败
    抛 `desktop_settings_write_failed` 且旧文件不变。
    """

    def __init__(self, data_root: Path, lock: threading.RLock | None = None) -> None:
        self._root = Path(data_root)
        self._lock = lock if lock is not None else threading.RLock()

    # --- 对外接口 ---------------------------------------------------------

    def read(self) -> DesktopSettings:
        """当前桌面设置；文件不存在时读默认值。

        「文件不存在」与「读不到」「读到了但不能用」严格分开（D-130 同口径）：
        只有真的不存在才可能落到默认值或一次性导入，损坏现场一概不动。
        唯一可能写盘的路径是那一次导入：它必须落盘才算完成，写不进去就抛
        `desktop_settings_write_failed`（下次查询会再试），不返回一份只存在于内存的
        「已导入」。
        """
        with self._lock:
            settings, imported = self._load()
            if imported:
                # 「查询不创建」的唯一例外：升级用户只能靠这一次导入把旧偏好带过来，
                # 文件落盘后就不再回退到档案（见 `_import_legacy_preference`）。
                self._write(settings)
            return settings

    def update(
        self,
        expected_revision: int,
        *,
        launch_at_sign_in: bool | _Unset = _UNSET,
        start_bot_on_launch: bool | _Unset = _UNSET,
        startup_profile_id: str | None | _Unset = _UNSET,
    ) -> int:
        """写「新意图」并把 `settings_revision` 加一；返回新 revision。

        `expected_revision` 与当前不符即 `DesktopSettingsConflict`，绝不覆盖其他
        页面/进程的改动。`last_apply_result` 与 `pending_startup_apply` **保持不变**：
        它们只能由 `record_apply_result()` 改，意图与系统侧事实不在同一次写入里互相
        覆盖。省略某个参数表示本次不改它，`startup_profile_id=None` 表示显式清空。
        """
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise DesktopSettingsError(INVALID_DESKTOP_SETTINGS)
        with self._lock:
            current, _ = self._load()
            if expected_revision != current.settings_revision:
                raise DesktopSettingsConflict(DESKTOP_SETTINGS_CONFLICT)
            updated = DesktopSettings(
                settings_revision=current.settings_revision + 1,
                launch_at_sign_in=_intent_bool(
                    launch_at_sign_in, current.launch_at_sign_in
                ),
                start_bot_on_launch=_intent_bool(
                    start_bot_on_launch, current.start_bot_on_launch
                ),
                startup_profile_id=_intent_profile(
                    startup_profile_id, current.startup_profile_id
                ),
                pending_startup_apply=current.pending_startup_apply,
                last_apply_result=current.last_apply_result,
            )
            self._write(updated)
            return updated.settings_revision

    def record_apply_result(
        self, result: str, *, pending: PendingStartupApply | None = None
    ) -> DesktopSettings:
        """记录一次启动项应用的**实际结果**；`settings_revision` 不变。

        只改 `last_apply_result` 与 `pending_startup_apply`，因此一次应用成败不会让
        正在编辑的页面看到 revision 冲突。`pending=None` 表示没有待应用的诊断记录。
        """
        if not isinstance(result, str) or result not in LAST_APPLY_RESULTS:
            raise DesktopSettingsError(INVALID_DESKTOP_SETTINGS)
        if pending is not None and not isinstance(pending, PendingStartupApply):
            raise DesktopSettingsError(INVALID_DESKTOP_SETTINGS)
        with self._lock:
            current, _ = self._load()
            updated = replace(
                current, last_apply_result=result, pending_startup_apply=pending
            )
            self._write(updated)
            return updated

    # --- 内部：读取 -------------------------------------------------------

    def _path(self) -> Path:
        return paths.desktop_json_path(self._root)

    def _read_document(self) -> dict[str, Any] | None:
        """读原始文档；不存在返回 None，读不到或超限报稳定错误。

        与 `config_service._read_document()` 同一手法（按字节上限 +1 读、先判不存在
        再判 OSError）：读不到不等于没有，把权限/占用错误当成默认值会让界面把
        「读不出来」显示成「关闭」。
        """
        try:
            with self._path().open("rb") as handle:
                raw = handle.read(core_config.MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DesktopSettingsError(DESKTOP_UNREADABLE) from exc
        if len(raw) > core_config.MAX_CONFIG_BYTES:
            # 超过与 YAML 文档同一字节预算的文件不读进来，按损坏处理。
            raise DesktopSettingsError(DESKTOP_CORRUPT)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesktopSettingsError(DESKTOP_CORRUPT) from exc
        if not isinstance(data, dict):
            # 顶层不是映射：合法 JSON 也读不出设置，不能当成空文件。
            raise DesktopSettingsError(DESKTOP_CORRUPT)
        return data

    def _load(self) -> tuple[DesktopSettings, bool]:
        """当前设置；第二个返回值表示这份设置来自一次性导入、尚未落盘。

        导入只在「文件不存在」时发生，因此它天然只跑一次；此后一律以文件为唯一来源。
        """
        document = self._read_document()
        if document is not None:
            return _parse_document(document), False
        imported = self._import_legacy_preference()
        if imported is not None:
            return imported, True
        return _default_settings(), False

    def _import_legacy_preference(self) -> DesktopSettings | None:
        """一次性把当前档案的旧 `start_bot_on_launch` 带进新文件；判不出来就返回 None。

        条件是「`desktop.json` 不存在**且** `launcher.json` 存在」：后者不存在就是全新
        安装，没有旧偏好可导。launcher.json 读不出来/指针非法、或档案 config.yaml 读到了
        但解析不了时返回 None —— 只回默认值、**不落盘**，下次查询再试，绝不把「没读到」
        写成「没有开启」（D-130 同口径）。档案 config.yaml 不存在则等于旧偏好默认关闭
        （`ConfigService.start_bot_on_launch()` 在 `load_saved()` 为 None 时也回 false）。

        导入**不递增 revision**：它记录的是既有意图，不是一次新的用户改动，所以紧随
        其后的 `update(expected_revision=0, ...)` 仍然成立。
        """
        metadata_path = paths.launcher_json_path(self._root)
        if not metadata_path.exists():
            return None
        metadata = _read_yaml_mapping(metadata_path)
        if metadata is None:
            return None
        profile_id = metadata.get("active_profile")
        if not isinstance(profile_id, str):
            return None
        try:
            profile = paths.profile_dir(self._root, profile_id)
        except ValueError:
            return None
        config_path = paths.config_path(profile)
        if not config_path.exists():
            # 没有正式配置：旧接口在 `load_saved()` 为 None 时也回 false，因此这里按
            # 「默认关闭」完成导入（真的存在但读不出来才返回 None）。
            return _default_settings()
        document = _read_yaml_mapping(config_path)
        if document is None:
            return None
        # 复用旧接口的口径（`ConfigService.start_bot_on_launch()` 用 `bool(...)`）：
        # 导入的是用户当年实际得到的取值，不是重新解释一遍 YAML。
        legacy_section = document.get("_launcher")
        legacy = legacy_section if isinstance(legacy_section, Mapping) else {}
        start_bot_on_launch = bool(legacy.get("start_bot_on_launch", False))
        imported = _default_settings()
        return replace(imported, start_bot_on_launch=start_bot_on_launch)

    # --- 内部：写入 -------------------------------------------------------

    def _write(self, settings: DesktopSettings) -> None:
        """整份替换 `desktop.json`：同目录临时文件 + flush + fsync + `os.replace`。

        与 `config_service._write_document()` 同一手法，但这里复制而不是调用：那是
        `ConfigService` 的私有方法，序列化 YAML、字节限额与错误码都属于配置事务，
        直接复用会把桌面设置绑到配置服务的生命周期上（DRY 不为跨模块重构让路）。
        """
        payload = json.dumps(
            _to_document(settings), ensure_ascii=False, indent=2
        ).encode("utf-8")
        path = self._path()
        directory = path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DesktopSettingsError(DESKTOP_SETTINGS_WRITE_FAILED) from exc
        try:
            handle_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
            )
        except OSError as exc:
            raise DesktopSettingsError(DESKTOP_SETTINGS_WRITE_FAILED) from exc
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
            # OSError 不再外泄：调用方只需要处理服务级错误（与 §6.4 同口径）。
            raise DesktopSettingsError(DESKTOP_SETTINGS_WRITE_FAILED) from exc
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _read_yaml_mapping(path: Path) -> dict[str, Any] | None:
    """有界读取一份 YAML 映射；不存在、读不到或解析不了都返回 None。

    只用于一次性导入：这里判不出来就不导入，绝不抛给查询路径（导入是尽力而为，
    不能让它把 `read()` 变成故障点）。
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(core_config.MAX_CONFIG_BYTES + 1)
    except OSError:
        return None
    if len(raw) > core_config.MAX_CONFIG_BYTES:
        return None
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _intent_bool(value: bool | _Unset, fallback: bool) -> bool:
    """解释 `update()` 的一个布尔意图参数：省略则保持不变，类型不对即拒绝。"""
    if value is _UNSET:
        return fallback
    if not isinstance(value, bool):
        raise DesktopSettingsError(INVALID_DESKTOP_SETTINGS)
    return value


def _intent_profile(value: str | None | _Unset, fallback: str | None) -> str | None:
    """解释 `update()` 的启动目标档案：省略保持不变，None 是显式清空。"""
    if value is _UNSET:
        return fallback
    if value is None:
        return None
    if not isinstance(value, str):
        raise DesktopSettingsError(INVALID_DESKTOP_SETTINGS)
    try:
        # 只校验 id 形态：目标是否真的存在由档案服务判定（N4 Task 4 的 422 分支）。
        return paths.validate_profile_id(value)
    except ValueError as exc:
        raise DesktopSettingsError(INVALID_DESKTOP_SETTINGS) from exc
