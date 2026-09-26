"""登录启动项服务：把「用户意图」变成登记动作，并如实报告事实（§8、§59、D-148）。

两件事：读 `desktop.json` 的 `launch_at_sign_in` 意图，操作 `HKCU\\...\\Run` 下本产品
自己的值名；然后把**实际读到的事实**报告出来。六条不变量：

1. **五项分离事实**：`requested_enabled`（意图）、`registration_present`（值在不在）、
   `command_matches`（内容是否等于本程序算出的命令）、`executable_exists`（EXE 还在不在
   原位置）、`last_apply_result`（上次应用结果码）。每一项都能独立为真/假，压成一个
   布尔就会在「值在但路径没了」「意图关但值没删掉」这些情况下撒谎。
2. **`unknown` 优先**：注册表读不到、同名值无法确认是本产品持有、或上次应用失败且
   未回读一致时，状态是 `unknown`。宁可说不知道，也不假报关闭成功；`enabled` 的
   语义只是「登记完整且路径有效」，**不表示「下次必定启动」**（不读 `StartupApproved`，
   系统侧的禁用决定无法通过受支持方式确认）。
3. **归属判定只用命令格式**：值形如 `"<绝对路径>" --startup`（引号包围 + 恰好一个固定
   参数）才算本产品持有；其他形态一律 `registration_conflict`，不覆盖、不删除，原值
   一个字节都不动。这是唯一允许的证据形式，没有别的启发式。
4. **拒绝在写之前**：命令超 260 字符或路径不可用（空、非绝对、不存在、非冻结形态）
   立即返回失败，**不写入、不截断**。
5. **待应用记录永不重放**：`pending_startup_apply` 只是「上次想写什么」的诊断；
   `repair()` 一律按**当前** EXE 路径重新生成命令，否则搬目录后会把旧路径写回去。
6. **不回显读到的原值**：对外只给本程序算出的 `expected_command` 与布尔事实，不把
   别的应用写在同名值里的命令行带回本机页面。

`registry` 是唯一读写注册表的通道（可注入替身）；`path_exists` 让离线流程不碰真实
文件系统。`STARTUP_FLAG` 与入口解析（`main.py`）里的那份必须保持同一字面量。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from .desktop_settings import (
    DEFAULT_LAST_APPLY_RESULT,
    DESKTOP_SETTINGS_CONFLICT,
    DesktopSettings,
    DesktopSettingsConflict,
    DesktopSettingsError,
    DesktopSettingsService,
    PendingStartupApply,
)
from .platform.startup_windows import StartupRegistry, StartupRegistryError

# 确切值总表：固定参数、值名与长度上限（各任务不得各写一套）。
STARTUP_FLAG: str = "--startup"
REGISTRY_VALUE_NAME: str = "RaricyBotLight"
# 按 `len(command)` 计（含引号与参数）；超限拒绝，不静默截断。
MAX_STARTUP_COMMAND_CHARS: int = 260

# `last_apply_result` 的取值（与 `desktop_settings.LAST_APPLY_RESULTS` 同一套）。
RESULT_OK: str = "ok"
RESULT_COMMAND_TOO_LONG: str = "command_too_long"
RESULT_PATH_UNUSABLE: str = "path_unusable"
RESULT_REGISTRATION_CONFLICT: str = "registration_conflict"
RESULT_APPLY_FAILED: str = "apply_failed"
RESULT_READ_FAILED: str = "read_failed"

# 这两个结果码表示「系统侧的结果没有回读确认」：与意图不一致时状态必须是 unknown。
_UNCONFIRMED_RESULTS: frozenset[str] = frozenset({RESULT_APPLY_FAILED, RESULT_READ_FAILED})

# `effective_state` 的四个取值（判定规则见 INTERFACES §59）。
STATE_ENABLED: str = "enabled"
STATE_DISABLED: str = "disabled"
STATE_NEEDS_REPAIR: str = "needs_repair"
STATE_UNKNOWN: str = "unknown"


@dataclass(frozen=True)
class StartupFacts:
    """一次观测的完整事实（确切值总表；调用方直接 `dataclasses.asdict` 即可进响应）。

    五项分离事实 + 判定结果：`effective_state`、`divergence`、本程序算出的
    `expected_command`，以及上次应用结果 `last_apply_result` 与待应用诊断
    `pending_apply`。**不含**注册表里读到的原始命令内容。
    """

    requested_enabled: bool
    registration_present: bool
    command_matches: bool
    executable_exists: bool
    effective_state: str
    divergence: bool
    expected_command: str
    last_apply_result: str
    pending_apply: PendingStartupApply | None


@dataclass(frozen=True)
class _Snapshot:
    """一次注册表读取的结果；`failed` 表示「读不到」，不是「没有值」。"""

    failed: bool
    present: bool
    raw: str | None


def build_startup_command(executable: str) -> str:
    """纯函数：`"<路径>" --startup`（引号包围路径、一个空格、恰好一个固定参数）。"""
    return f'"{executable}" {STARTUP_FLAG}'


def _is_owned_command(command: str | None) -> bool:
    """唯一的归属证据：`"<绝对路径>" --startup`（引号包围 + 恰好一个固定参数）。

    不满足就返回 False：调用方据此拒绝覆盖/删除，绝不猜测「看起来像本产品」。
    """
    if not command or not command.startswith('"'):
        return False
    end = command.find('"', 1)
    if end < 0:
        return False
    path = command[1:end]
    if command[end + 1 :] != f" {STARTUP_FLAG}":
        return False
    return bool(path) and os.path.isabs(path)


def _effective_state(
    *,
    read_ok: bool,
    owned: bool,
    requested: bool,
    present: bool,
    matches: bool,
    exists: bool,
    result: str,
) -> str:
    """`effective_state` 的唯一实现（确切值总表；N4 各任务不得各写一套）。"""
    if not read_ok or (present and not owned):
        # 登记事实取不到，或同名值无法确认是本产品持有：只能说不知道。
        return STATE_UNKNOWN
    consistent = (present and matches and exists) if requested else not present
    if result in _UNCONFIRMED_RESULTS and not consistent:
        # 上次应用失败且未回读一致（含「意图为关但值没删掉」）：不假报关闭成功。
        return STATE_UNKNOWN
    if requested:
        if present and matches and exists and result in (RESULT_OK, DEFAULT_LAST_APPLY_RESULT):
            return STATE_ENABLED
        # 意图为开但登记不完整（被删、搬目录、EXE 不在原位置），或上次结果是
        # 一个未回读确认的失败码：都落在「需要修复」这一档。
        return STATE_NEEDS_REPAIR
    return STATE_DISABLED if not present else STATE_UNKNOWN


class StartupService:
    """登录启动项服务：`status()` 只看、`apply()` 按当前意图执行、`repair()` 带版本守卫。

    `executable` 是当前进程真正要登记的可执行文件路径（未冻结形态可以为 None），
    `frozen` 表示发行形态，`path_exists` 默认 `os.path.isfile`（测试注入替身，
    不碰真实文件系统）。`registry` 是唯一注册表通道，自动化测试只注入内存替身。
    """

    def __init__(
        self,
        settings: DesktopSettingsService,
        registry: StartupRegistry,
        *,
        executable: str | None,
        frozen: bool,
        path_exists: Callable[[str], bool] = os.path.isfile,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._executable = executable
        self._frozen = frozen
        self._path_exists = path_exists

    # --- 对外接口 ---------------------------------------------------------

    def status(self) -> StartupFacts:
        """只看不写：读设置文件与注册表，报告事实。

        注册表读不到、或同名值非本产品持有时，返回的事实里 `last_apply_result`
        是**本次观测**的结论（`read_failed` / `registration_conflict`，连同对应的
        待应用诊断），因为这两件事正是界面要显示与提供修复的。但**不回写设置
        文件**：查询不修复、不创建、不覆盖（与 D-130 同口径），文件里保留上一次
        真实应用的结果。
        """
        return self._observe(apply=False)

    def apply(self) -> StartupFacts:
        """按当前意图登记或注销，然后把实际结果写回设置文件。

        写前判定拒绝条件，写后一定回读：命令发出去了不等于事实成立。应用失败
        **不回滚意图**（意图是用户要的，结果是系统给的，分开保存）。
        """
        return self._observe(apply=True)

    def repair(self, expected_revision: int) -> StartupFacts:
        """按当前意图与**当前** EXE 路径重新生成命令并执行。

        `expected_revision` 与当前 `settings_revision` 不符时抛
        `DesktopSettingsConflict`（页面拿着过期的意图来修复，应当先刷新）。
        本方法不重放 `pending_startup_apply`：那是诊断记录，重放会把搬目录前的
        旧路径又写回注册表。
        """
        return self._observe(apply=True, expected_revision=expected_revision)

    # --- 内部：一次观测 ---------------------------------------------------

    def _observe(self, *, apply: bool, expected_revision: int | None = None) -> StartupFacts:
        settings = self._settings.read()
        if expected_revision is not None and expected_revision != settings.settings_revision:
            raise DesktopSettingsConflict(DESKTOP_SETTINGS_CONFLICT)
        requested = settings.launch_at_sign_in
        expected = self._expected_command()
        result = settings.last_apply_result
        pending = settings.pending_startup_apply
        if apply and requested:
            rejection = self._rejection(expected)
            if rejection is not None:
                # 拒绝在写之前判定：立即返回失败，不写入、不截断（读一次只为如实
                # 报告登记事实，读本身不改任何东西）。
                snapshot = self._read_registry()
                return self._record(
                    settings,
                    rejection,
                    self._pending("register", expected),
                    snapshot,
                    apply=True,
                )
        snapshot = self._read_registry()
        if snapshot.failed:
            # 读不到就不能当成「没有登记」，也不能当成「已关闭」；待应用诊断照留，
            # 这样页面能看到「我们本想写什么」而不是只剩一个失败码。
            action = "register" if requested else "unregister"
            return self._record(
                settings,
                RESULT_READ_FAILED,
                self._pending(action, expected),
                snapshot,
                apply=apply,
            )
        if snapshot.present and not _is_owned_command(snapshot.raw):
            # 同名值不是本产品持有：不覆盖、不删除，也不把原值搬进设置文件。
            action = "register" if requested else "unregister"
            return self._record(
                settings,
                RESULT_REGISTRATION_CONFLICT,
                self._pending(action, expected),
                snapshot,
                apply=apply,
            )
        if apply:
            result, pending, snapshot = self._execute(requested, expected, snapshot)
        return self._record(settings, result, pending, snapshot, apply=apply)

    def _execute(
        self, requested: bool, expected: str, snapshot: _Snapshot
    ) -> tuple[str, PendingStartupApply | None, _Snapshot]:
        """执行一次登记/注销并回读；绝不凭「命令发出去了」宣告成功。"""
        operation_failed = False
        if requested:
            if snapshot.present and snapshot.raw == expected:
                # 已经是目标形态：不必再碰注册表（apply 可重复调用）。
                pass
            else:
                operation_failed = not self._write(expected)
        elif snapshot.present:
            operation_failed = not self._delete()
        confirmed = self._read_registry()
        if operation_failed:
            result = RESULT_APPLY_FAILED
        elif confirmed.failed:
            # 写过了但回读不了：结果不确定，不得报成功。
            result = RESULT_READ_FAILED
        elif requested and not (confirmed.present and confirmed.raw == expected):
            result = RESULT_APPLY_FAILED
        elif not requested and confirmed.present:
            result = RESULT_APPLY_FAILED
        else:
            result = RESULT_OK
        action = "register" if requested else "unregister"
        pending = self._pending(action, expected) if result != RESULT_OK else None
        return result, pending, confirmed

    def _record(
        self,
        settings: DesktopSettings,
        result: str,
        pending: PendingStartupApply | None,
        snapshot: _Snapshot,
        *,
        apply: bool,
    ) -> StartupFacts:
        """把结果写回设置文件（只在 `apply` 路径），并构造这一份事实快照。"""
        if apply:
            try:
                self._settings.record_apply_result(result, pending=pending)
            except DesktopSettingsError:
                # 结果记不进去（写盘失败）不改变本次的对外结论：注册表侧的动作已经
                # 发生，设置服务保证旧文件保持上一版；调用方拿到的仍是本次真实读数。
                pass
        return self._facts(settings, snapshot, result, pending)

    def _facts(
        self,
        settings: DesktopSettings,
        snapshot: _Snapshot,
        result: str,
        pending: PendingStartupApply | None,
    ) -> StartupFacts:
        requested = settings.launch_at_sign_in
        expected = self._expected_command()
        present = snapshot.present
        owned = bool(present and _is_owned_command(snapshot.raw))
        matches = bool(owned and snapshot.raw == expected)
        exists = self._executable_exists()
        return StartupFacts(
            requested_enabled=requested,
            registration_present=present,
            command_matches=matches,
            executable_exists=exists,
            effective_state=_effective_state(
                read_ok=not snapshot.failed,
                owned=owned,
                requested=requested,
                present=present,
                matches=matches,
                exists=exists,
                result=result,
            ),
            divergence=(
                requested != present
                or (requested and not matches)
                or result in _UNCONFIRMED_RESULTS
            ),
            expected_command=expected,
            last_apply_result=result,
            pending_apply=pending,
        )

    # --- 内部：意图、命令与判定 -------------------------------------------

    def _expected_command(self) -> str:
        """本程序当前要登记的命令；算不出来（没有 EXE 路径）时是空串。"""
        if not self._executable:
            return ""
        return build_startup_command(self._executable)

    def _rejection(self, command: str) -> str | None:
        """写之前的拒绝条件；返回稳定结果码或 None。不截断、不猜测。"""
        executable = self._executable
        if len(command) > MAX_STARTUP_COMMAND_CHARS:
            return RESULT_COMMAND_TOO_LONG
        if (
            not executable
            or not os.path.isabs(executable)
            # 路径里带引号会让我们自己生成的命令无法被格式判定还原成同一个路径。
            or '"' in executable
            # 未冻结的形态（python.exe + 源码目录）不得进 Run：登录时它跑不起来。
            or not self._frozen
            or not self._file_exists(executable)
        ):
            return RESULT_PATH_UNUSABLE
        return None

    def _pending(self, action: str, command: str) -> PendingStartupApply | None:
        """诊断记录：只有登记意图才留「本次想写入的完整命令」。

        注销没有待写入的命令，也不把读到的原值搬进设置文件（那可能是别的应用的
        命令行）；两种情况都留 None 比编一条假命令诚实。
        """
        if action != "register" or not command:
            return None
        return PendingStartupApply(action="register", command=command)

    def _file_exists(self, path: str) -> bool:
        try:
            return bool(self._path_exists(path))
        except OSError:
            # 路径判定本身失败（非法字符、权限）：按「不可用」处理，不猜。
            return False

    def _executable_exists(self) -> bool:
        if not self._executable:
            return False
        return self._file_exists(self._executable)

    # --- 内部：注册表（唯一通道；失败只回布尔，不抛给调用方） --------------

    def _read_registry(self) -> _Snapshot:
        try:
            present, raw = self._registry.read_value(REGISTRY_VALUE_NAME)
        except (StartupRegistryError, OSError):
            # 实现约定的失败类型 + 兜底 OSError：读不到绝不能退化成「没有登记」。
            return _Snapshot(failed=True, present=False, raw=None)
        return _Snapshot(failed=False, present=bool(present), raw=raw)

    def _write(self, command: str) -> bool:
        try:
            self._registry.write_value(REGISTRY_VALUE_NAME, command)
        except (StartupRegistryError, OSError):
            return False
        return True

    def _delete(self) -> bool:
        try:
            self._registry.delete_value(REGISTRY_VALUE_NAME)
        except (StartupRegistryError, OSError):
            return False
        return True
