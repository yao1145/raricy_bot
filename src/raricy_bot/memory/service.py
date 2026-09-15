"""长期记忆的存储服务：快照、原子写、幂等与刷新（INTERFACES §30、规划 §4.3 / §5.5）。

本模块是记忆功能的**唯一写入口**，也是这条链路里最容易丢数据的一层，因此三条不变量优先于一切：

- **软故障绝对**（D-60）：`start()` / `stop()` 绝不抛出，读取失败只返回空结果，写入失败只返回
  稳定状态；聊天、评论、`/livez`、`/readyz` 都不依赖这里的任何结果。
- **Markdown 是唯一真相源**（D-59）：业务幂等结果写在目标文件的 front matter 里；一次写入是
  同目录临时文件 + 独占创建 + 完整 bytes + flush/fsync + 摘要比对 + `os.replace`，任一步失败都
  保留原正式文件与旧快照并清理临时文件。内存快照用**一次引用替换**更新。
- **正文只落 Markdown**（§37）：日志只记稳定标识与计数，绝不记正文、key、user ID、存储键或路径。

读路径（`context_for`、四个只读辅助、`find_operation`）做同步文件 I/O，且**不**加写锁：快照替换是
一次属性赋值，读者看到的永远是替换前或替换后的一份完整文档。写路径由单个 `asyncio.Lock` 串行
（§30.3）。`enabled=False` 时所有方法都不碰文件系统：不建目录、不读文件、不启任务（§26.3、D-60）。

共同记忆的四个管理 mutation（`add_common_candidate` / `approve_candidate` / `reject_candidate` /
`delete_common`）各自独立复查 `access.is_admin(actor_id)`（§32.3、裁决 R14）。这是**纵深防御**，
不是 Controller 那次检查的副本：Controller 的 bug 或未来的旁路不能自行授权一次对共享记忆的写入。
检查在任何 I/O、任何幂等查询与任何写入之前完成，失败是稳定状态 `forbidden`，不是异常（D-60）。

契约留白在本文件里定的读法（逐条记在任务报告里）：同 key 的 `add` 按「替换原条目」处理（§29.3）；
只有 `ok` 会写文件，其余状态一律不落盘、因此也不进 `operations`（§27.4）；`operations` 超过
`max_operations` 时按插入顺序淘汰最旧的键；候选的目标条目用 `updated_at > created_at` 判「已被改动」；
外部版本合法但本次操作已不适用时，采纳外部版本并返回该操作自己的状态（not_found / full / conflict）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import IO, Any

from ..config import MemoryConfig
from ..core.context import SupplementalItem
from ..logging_setup import get_logger, log_event
from ..redact import Redactor
from .access import MemoryAccessPolicy
from .codec import (
    ID_DIGITS,
    PREFIX_ALL_USER,
    PREFIX_CANDIDATE,
    PREFIX_LOBBY,
    PREFIX_USER,
    CodecError,
    CommonDocument,
    PrivateDocument,
    parse_common,
    parse_private,
    render_common,
    render_private,
)
from .models import (
    STATUS_CONFLICT,
    STATUS_FORBIDDEN,
    STATUS_FULL,
    STATUS_INVALID_PROPOSAL,
    STATUS_NOT_FOUND,
    STATUS_NOOP,
    STATUS_OK,
    STATUS_SECRET_DETECTED,
    STATUS_UNAVAILABLE,
    MemoryCandidate,
    MemoryContext,
    MemoryEntry,
    MemoryProposal,
    MemoryScope,
    OperationResult,
    ProposalAction,
    user_storage_key,
)

__all__ = ["MemoryService", "PrivateSettings"]

_logger = get_logger("memory")

# 频道类型（§30.2）：只有 dm 会碰用户私有文件，lobby 与 comment 连读都不读。
_DM: str = "dm"
_LOBBY: str = "lobby"

# `SupplementalItem.group` 的三个稳定取值（§30.2、§33）。
_GROUP_ALL_USER: str = "memory_all_user"
_GROUP_LOBBY: str = "memory_lobby"
_GROUP_USER: str = "memory_user"

# `priority` 数值是实现细节（D-62）：只表达「DM 私有优先于 all_user，lobby 优先于 all_user」的
# 组间次序。`SupplementalItem.priority` 是一个整数（§33），组间次序只能靠**步长**表达，因此步长
# 在构造时按容量上限现算（见 `__init__`），不能写死——§26.2 只要求上限是正整数，不保证它小于
# 某个固定的步长。

# 用户快照 LRU 的容量：只影响内存，淘汰不删文件（§30.4）。
_USER_CACHE_SIZE: int = 64

# 临时文件：与目标文件同目录、独占创建；测试据此断言成功与失败路径都清理干净（§5.5 第 1、7 步）。
_TEMP_PREFIX: str = ".memory-tmp-"
_TEMP_SUFFIX: str = ".tmp"

# 幂等键与 ID 的 ASCII 十进制序号（与 codec 同一口径，`digit()` 会放过非 ASCII 数字）。
_ASCII_DIGITS = re.compile(r"[0-9]+")

# 四个 ID 前缀：`_canonical_object_id` 靠它把「前缀自带」的对象 ID 收敛成渲染形态（§29.1）。
_ID_PREFIXES: tuple[str, ...] = (PREFIX_ALL_USER, PREFIX_LOBBY, PREFIX_USER, PREFIX_CANDIDATE)

# 稳定失败原因里表示「文件系统错误」的那个（codec 的六个 reason 之外唯一允许进日志的值）。
_REASON_IO: str = "io"

# 共同文件与用户文件的解析结果。
_MemoryDocument = CommonDocument | PrivateDocument

# 纯函数形式的操作：给定基线文档，返回（新文档, 稳定状态, 对象 ID）；状态非 ok 时新文档为 None。
_ApplyFn = Callable[[Any], "tuple[Any | None, str, str | None]"]

# 密钥筛的取材函数：给定基线文档，返回本次将要写进 Markdown 的文本（正文与 key）。
_ScreenFn = Callable[[Any], "tuple[str, ...]"]


@dataclass(frozen=True)
class PrivateSettings:
    """用户的私有记忆设置（D-67）：`private_settings()` 与 `private_settings_cached()` 的返回类型。"""

    private_enabled: bool
    auto_capture: bool


@dataclass(frozen=True)
class _Snapshot:
    """一个 Markdown 文件的内存快照与最近一次检查结果。

    - `document`：**最后一份有效快照**。外部文件后来变得不合法时它**不**被丢掉（§30.4），
      读取继续用它，写入则因为摘要对不上而被拒（conflict）。冷启动就没有有效快照时为 None，
      此时 `available` 为 False，整个范围不可用。
    - `digest`：被采纳字节的摘要；None 表示被采纳的状态就是「文件不存在」（基线是空文档）。
    - `reason`：最近一次检查失败的原因；None 表示最近一次检查是好的。
    - `attempted`：最近一次检查过的字节摘要，含被拒绝的版本；用它避免对同一份坏文件重复记日志。
    """

    document: _MemoryDocument | None
    digest: bytes | None
    reason: str | None
    attempted: bytes | None
    checked_at: float

    @property
    def available(self) -> bool:
        """该范围是否还有可用的快照（冷启动遇到坏文件时为 False）。"""
        return self.document is not None

    @property
    def revision(self) -> int:
        """当前快照的修订号；没有可用快照时为 0。"""
        if self.document is None:
            return 0
        return int(self.document.revision)


@dataclass(frozen=True)
class _DocumentView:
    """把「共同文件」与「用户文件」的差异收在一处，供刷新与原子写复用。"""

    path: str
    parser: Callable[[bytes, MemoryConfig], Any]
    fresh: Callable[[], Any]
    scope: str


class MemoryService:
    """长期记忆的存储服务（INTERFACES §30、规划 §4.3）。

    生产装配必须传入 `BotApp` 自有的 `Redactor`（登记了密码、`LLM_API_KEY` 与会话 Cookie）：
    密钥筛在每个含正文的 mutation 入口执行，命中即整条拒绝。`redactor=None` 只允许出现在只读
    场景与测试里（§30.1、§37）。
    """

    def __init__(
        self,
        config: MemoryConfig,
        *,
        now: Callable[[], float] = time.time,
        replace: Callable[[str, str], None] = os.replace,
        redactor: Redactor | None = None,
    ) -> None:
        self._config = config
        self._now = now
        self._replace = replace
        self._redactor = redactor
        self._enabled: bool = bool(config.enabled)
        self._root: str = str(config.root_dir)
        self._common_path: str = os.path.join(self._root, "common.md")
        self._users_dir: str = os.path.join(self._root, "users")
        # 刷新周期同时是用户文件的缓存 TTL：到期后的**下一次访问**才检查摘要（§30.4）。
        self._refresh_seconds: float = float(config.refresh_seconds)
        # priority 的组间步长：严格大于任何一组在容量上限内可能出现的最大名次，否则一个把上限
        # 设到 1000 以上的部署会让某个作用域的尾部名次跨进另一组的区间，破坏 §30.2 的相对次序。
        self._priority_stride: int = 1 + max(
            1,
            int(config.max_common_entries_per_scope),
            int(config.max_private_entries_per_user),
        )
        self._write_lock = asyncio.Lock()
        self._common: _Snapshot | None = None
        self._users: "OrderedDict[str, _Snapshot]" = OrderedDict()
        self._refresh_task: asyncio.Task[None] | None = None

    # --- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        """创建或加载 `common.md` 并启动刷新任务；失败只降级，绝不抛出（§30.1、D-60）。"""
        if not self._enabled:
            # 关闭时不建目录、不读文件、不启任务：升级前的部署行为逐字节不变（§26.3）。
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            # 重复 start：不再起第二个刷新循环（stop 之后任务字段会被清掉，重启仍然可以）。
            return
        try:
            os.makedirs(self._users_dir, exist_ok=True)
            state = self._inspect(
                self._common_view(), self._common, ttl=None, event="memory.load_failed", force=True
            )
            if state.available and state.digest is None:
                # 首次启用：落一份空的 common.md，让目录布局与 §5.1 一致。
                status, _, written = self._write_document(
                    self._common_view(), state, lambda document: (document, STATUS_OK, None)
                )
                if status == STATUS_OK:
                    state = written
            self._common = state
            if state.available and state.document is not None:
                log_event(
                    _logger,
                    logging.INFO,
                    "memory.ready",
                    scope="common",
                    revision=state.document.revision,
                    entry_count=_common_entry_count(state.document),
                )
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        except Exception as exc:  # 目录不可写、任务建不出来：软故障，保留 unavailable。
            log_event(
                _logger,
                logging.WARNING,
                "memory.load_failed",
                scope="common",
                error=type(exc).__name__,
            )

    async def stop(self) -> None:
        """停掉刷新任务；未启用或未启动时什么都不做，绝不抛出（§30.1）。"""
        task = self._refresh_task
        self._refresh_task = None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except Exception as exc:  # 事件循环已关之类的极端情况：同样只降级。
            log_event(
                _logger,
                logging.WARNING,
                "memory.refresh_failed",
                scope="common",
                error=type(exc).__name__,
            )

    async def _refresh_loop(self) -> None:
        """按 `refresh_seconds` 检查 `common.md` 的外部编辑（§30.4）。被取消即退出。"""
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                self._refresh_common()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 兜底：刷新任务永远不能把异常带出协程。
                log_event(
                    _logger,
                    logging.WARNING,
                    "memory.refresh_failed",
                    scope="common",
                    error=type(exc).__name__,
                )

    def _refresh_common(self) -> None:
        """一次刷新：摘要没变就不解析、不记日志；变了就整份接手，坏了就保留最后一份有效快照。"""
        if not self._enabled:
            return
        self._common = self._inspect(
            self._common_view(), self._common, ttl=None, event="memory.refresh_failed", force=True
        )

    # --- 读路径 -----------------------------------------------------------

    async def context_for(
        self,
        *,
        user_id: str | None,
        channel_kind: str,
        access: MemoryAccessPolicy,
    ) -> MemoryContext:
        """按作用域取候选条目（§30.2）；任何失败都返回空 items，绝不抛出（D-60）。"""
        if not self._enabled:
            # 关闭时不读任何文件，也不记 context_omitted：这是正常路径，路径依赖里没有记忆。
            return MemoryContext(common_revision=0, private_revision=None, items=())
        items: list[SupplementalItem] = []
        common_revision = 0
        private_revision: int | None = None
        try:
            if access.permits_common(user_id):
                state = self._common_state()
                if state.available and isinstance(state.document, CommonDocument):
                    common_revision = state.document.revision
                    items.extend(self._scope_items(state.document, channel_kind))
                else:
                    self._log_omitted("common", state.reason)
            if channel_kind == _DM and user_id and access.permits_private(user_id, channel_kind):
                # 私有记忆只在 DM 且门禁通过时读取；其它频道连文件都不读（§30.2 的表）。
                state = self._user_state(user_id)
                if state.available and isinstance(state.document, PrivateDocument):
                    private_revision = state.document.revision
                    items.extend(
                        self._items(state.document.entries, _GROUP_USER, base=0, prefix=PREFIX_USER)
                    )
                else:
                    self._log_omitted("user", state.reason)
        except Exception as exc:  # 兜底：记忆是可选增强，读取失败不能外溢（D-60）。
            self._log_omitted("memory", "internal", error=type(exc).__name__)
            return MemoryContext(common_revision=0, private_revision=None, items=())
        # 按 priority 排序返回（§30.2）：取舍与最终版面仍由 ContextManager 决定（D-62）。
        items.sort(key=lambda item: item.priority)
        return MemoryContext(
            common_revision=common_revision,
            private_revision=private_revision,
            items=tuple(items),
        )

    def _scope_items(self, document: CommonDocument, channel_kind: str) -> list[SupplementalItem]:
        """按频道取共同记忆：DM 与评论只有 `all_user`，大区加 `lobby`（§30.2）。

        未知的 `channel_kind` 按评论处理：只给 `all_user`，绝不扩大到别的范围。
        """
        items = self._items(document.all_user, _GROUP_ALL_USER, base=1, prefix=PREFIX_ALL_USER)
        if channel_kind == _LOBBY:
            # 大区共同记忆优先于全站共同记忆；私有记忆与它作用域互斥，不会同时出现。
            items = self._items(document.lobby, _GROUP_LOBBY, base=0, prefix=PREFIX_LOBBY) + items
        return items

    def _items(
        self, entries: tuple[MemoryEntry, ...], group: str, *, base: int, prefix: str
    ) -> list[SupplementalItem]:
        """组内顺序：pinned 在前，再按 `updated_at` 新到旧；同刻保持文件序（私有文件即 ID 升序）。"""
        ordered = sorted(
            entries, key=lambda entry: (0 if entry.pinned else 1, _descending_stamp(entry.updated_at))
        )
        return [
            SupplementalItem(
                group=group,
                label=entry.memory_id,
                content=entry.content,
                priority=base * self._priority_stride + rank,
            )
            for rank, entry in enumerate(ordered)
        ]

    async def private_settings(self, user_id: str) -> PrivateSettings:
        """该用户的私有设置；文件不存在或不可用都按「未开启」返回，不创建任何文件。"""
        if not self._enabled or not user_id:
            return PrivateSettings(private_enabled=False, auto_capture=False)
        state = self._user_state(user_id)
        if not isinstance(state.document, PrivateDocument):
            return PrivateSettings(private_enabled=False, auto_capture=False)
        return PrivateSettings(
            private_enabled=bool(state.document.private_enabled),
            auto_capture=bool(state.document.auto_capture),
        )

    def private_settings_cached(self, user_id: str) -> PrivateSettings | None:
        """只读内存快照、不做任何 I/O（D-67）：快照没加载或用户未知时返回 None。

        这是 §34.1 的 `private_enabled` 回调的唯一数据源：`/help` 是本地命令，不能为一句措辞去读文件。
        """
        if not user_id:
            return None
        state = self._users.get(user_id)
        if state is None or not isinstance(state.document, PrivateDocument):
            return None
        return PrivateSettings(
            private_enabled=bool(state.document.private_enabled),
            auto_capture=bool(state.document.auto_capture),
        )

    def private_path(self, user_id: str) -> str:
        """该用户 Markdown 的路径（用 `user_storage_key` 命名；目录不存在时**不创建**）。

        这是唯一的路径访问入口（裁决 E / D-65）：测试与上层都从这里拿路径，不再自己拼文件名。
        `user_id` 含孤立代理项时也不抛出（`_storage_key` 兜底），因此**读路径**永远拿得到一条
        路径，只是那条路径上不可能有文件（写入路径在 `_mutate_private` 入口就被拒）。
        """
        key = _storage_key(user_id)
        if key is None:
            # 兜底路径：同形、稳定、不含原始 ID；这条路径上不会有文件，也永远不会被写入。
            key = _invalid_id_path_key(user_id)
        return os.path.join(self._users_dir, f"{key}.md")

    async def private_entries(self, user_id: str) -> tuple[MemoryEntry, ...]:
        """该用户已生效的私有条目；不可用时返回空元组。"""
        if not self._enabled or not user_id:
            return ()
        state = self._user_state(user_id)
        if not isinstance(state.document, PrivateDocument):
            return ()
        return tuple(state.document.entries)

    async def common_entries(self, scope: MemoryScope) -> tuple[MemoryEntry, ...]:
        """该作用域已生效的共同条目；`user` 作用域没有共同条目，返回空元组。"""
        if not self._enabled or scope not in (MemoryScope.ALL_USER, MemoryScope.LOBBY):
            return ()
        state = self._common_state()
        if not isinstance(state.document, CommonDocument):
            return ()
        return tuple(_common_bucket(state.document, scope))

    async def candidates(self) -> tuple[MemoryCandidate, ...]:
        """待批准的共同记忆候选；不可用时返回空元组。"""
        if not self._enabled:
            return ()
        state = self._common_state()
        if not isinstance(state.document, CommonDocument):
            return ()
        return tuple(state.document.candidates)

    async def find_operation(
        self, operation_id: str, *, user_id: str | None = None
    ) -> OperationResult | None:
        """幂等键查询：命中返回第一次的稳定结果，未命中返回 None。

        §32.3 要求「重复 SSE、resync 或崩溃重放先查 Markdown 的 `operations`；命中时不再调用 AI」，
        而 §30.1 的接口表没有给查询入口，本方法就是那个入口（补充裁决，已写进任务报告）。
        `user_id` 非空时先查该用户的私有快照，再查共同快照；为空只查共同快照。
        """
        if not self._enabled or not operation_id:
            return None
        if user_id:
            state = self._user_state(user_id)
            if isinstance(state.document, PrivateDocument):
                hit = state.document.operations.get(operation_id)
                if hit is not None:
                    return hit
        state = self._common_state()
        if isinstance(state.document, CommonDocument):
            return state.document.operations.get(operation_id)
        return None

    # --- mutation：私有 ----------------------------------------------------

    async def set_private_enabled(
        self, user_id: str, enabled: bool, *, operation_id: str
    ) -> OperationResult:
        """开关私有记忆读取；值没变时是 noop，不写文件（§30.2）。"""
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_private_setting(
                document, operation_id, "private_enabled", enabled
            ),
        )

    async def set_auto_capture(
        self, user_id: str, enabled: bool, *, operation_id: str
    ) -> OperationResult:
        """开关自动提取；值没变时是 noop，不写文件（§30.2）。"""
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_private_setting(
                document, operation_id, "auto_capture", enabled
            ),
        )

    async def apply_private_proposal(
        self, user_id: str, proposal: MemoryProposal, *, operation_id: str
    ) -> OperationResult:
        """把撰写器的提案落到该用户的私有文件（规划 §4.3 的写入路径）。"""
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_private_proposal(document, proposal, operation_id),
            screen=lambda _document: (proposal.key, proposal.content),
        )

    async def delete_private(
        self, user_id: str, memory_id: str, *, operation_id: str
    ) -> OperationResult:
        """删除该用户的一条私有条目；ID 前缀与所在作用域在入口再次校验。"""
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_private_delete(document, memory_id, operation_id),
        )

    async def clear_private(self, user_id: str, *, operation_id: str) -> OperationResult:
        """清空该用户的私有条目，但保留幂等元数据与设置，因此重放旧命令不会再次执行（D-59）。"""
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_private_clear(document, operation_id),
        )

    # --- mutation：共同（四个管理操作各自独立复查 admin，§32.3 / 裁决 R14） ------

    def _admin_gate(self, access: MemoryAccessPolicy, actor_id: str) -> OperationResult | None:
        """管理 mutation 的独立授权检查：放行返回 None，否则返回 `forbidden` 结果。

        **纵深防御**（§32.3、裁决 R14），不是 Controller 那次检查的副本：Controller 的 bug 或未来的
        旁路不能自行授权一次对共享记忆的写入。检查在任何 I/O、任何幂等查询与任何写入**之前**完成，
        因此被拒的调用方不产生任何可观察副作用；失败是稳定状态而不是异常（§27.4、D-60）。
        """
        if access.is_admin(actor_id):
            return None
        return OperationResult(status=STATUS_FORBIDDEN, object_id=None, revision=0)

    async def add_common_candidate(
        self,
        scope: MemoryScope,
        proposal: MemoryProposal,
        *,
        operation_id: str,
        access: MemoryAccessPolicy,
        actor_id: str,
    ) -> OperationResult:
        """把提案写成一条待批准候选；候选绝不进入任何普通模型请求（D-58）。"""
        denied = self._admin_gate(access, actor_id)
        if denied is not None:
            return denied
        return await self._mutate_common(
            operation_id,
            lambda document: self._apply_candidate_add(document, scope, proposal, operation_id),
            event="memory.candidate_updated",
            screen=lambda _document: (proposal.key, proposal.content),
        )

    async def approve_candidate(
        self,
        candidate_id: str,
        *,
        operation_id: str,
        access: MemoryAccessPolicy,
        actor_id: str,
    ) -> OperationResult:
        """把候选原子移入生效区；目标条目已被改动时返回 conflict，不覆盖（§32.3）。

        候选正文即将被写进 Markdown，因此这里同样过一遍密钥筛：人工写进候选里的密钥不会被
        批准动作搬进生效区——包括外部版本里的那一份（见 `_write_document` 的基线复查）。
        """
        denied = self._admin_gate(access, actor_id)
        if denied is not None:
            return denied
        return await self._mutate_common(
            operation_id,
            lambda document: self._apply_candidate_approve(document, candidate_id, operation_id),
            event="memory.candidate_updated",
            id_field="memory_id",
            screen=lambda document: _candidate_texts(document, candidate_id),
        )

    async def reject_candidate(
        self,
        candidate_id: str,
        *,
        operation_id: str,
        access: MemoryAccessPolicy,
        actor_id: str,
    ) -> OperationResult:
        """丢弃一条候选，不动任何已生效条目。"""
        denied = self._admin_gate(access, actor_id)
        if denied is not None:
            return denied
        return await self._mutate_common(
            operation_id,
            lambda document: self._apply_candidate_reject(document, candidate_id, operation_id),
            event="memory.candidate_updated",
        )

    async def delete_common(
        self,
        memory_id: str,
        *,
        operation_id: str,
        access: MemoryAccessPolicy,
        actor_id: str,
    ) -> OperationResult:
        """删除一条已生效的共同记忆；ID 前缀决定它属于哪个区。"""
        denied = self._admin_gate(access, actor_id)
        if denied is not None:
            return denied
        return await self._mutate_common(
            operation_id,
            lambda document: self._apply_common_delete(document, memory_id, operation_id),
            event="memory.updated",
            id_field="memory_id",
        )

    # --- mutation 的公共骨架 ----------------------------------------------

    async def _mutate_private(
        self,
        user_id: str,
        operation_id: str,
        apply_fn: _ApplyFn,
        *,
        screen: _ScreenFn | None = None,
    ) -> OperationResult:
        """私有文件的 mutation 骨架：门禁 → 幂等 → 密钥筛 → 原子写（§30.2）。

        `screen` 拿到基线文档、返回将要写进 Markdown 的文本；筛选发生在 render 与写入之前。
        """
        if not self._enabled or not user_id or _storage_key(user_id) is None:
            # 关闭时能力根本没被注入；真被调到说明调用方绕过了访问门（§28、§34.1）。
            # user_id 为空或含孤立代理项（编码不出存储键）说明调用方给的压根不是站点 ID：
            # 同样按 forbidden 处理，绝不为此建目录或落文件。
            return OperationResult(status=STATUS_FORBIDDEN, object_id=None, revision=0)
        async with self._write_lock:
            state = self._user_state(user_id)
            if not isinstance(state.document, PrivateDocument):
                return OperationResult(status=STATUS_UNAVAILABLE, object_id=None, revision=state.revision)
            hit = state.document.operations.get(operation_id)
            if hit is not None:
                # 幂等命中：返回第一次的稳定结果，不再改动文件，也不重新调 AI（§30.2、D-67）。
                return hit
            if screen is not None and self._contains_secret(screen(state.document)):
                return OperationResult(
                    status=STATUS_SECRET_DETECTED, object_id=None, revision=state.revision
                )
            status, object_id, written = self._write_document(
                self._private_view(user_id), state, apply_fn, screen=screen
            )
            if written is not state:
                self._store_user(user_id, written)
            if status == STATUS_OK:
                self._log_updated("memory.updated", scope="user", revision=written.revision, memory_id=object_id)
            return OperationResult(status=status, object_id=object_id, revision=written.revision)

    async def _mutate_common(
        self,
        operation_id: str,
        apply_fn: _ApplyFn,
        *,
        event: str,
        id_field: str = "candidate_id",
        screen: _ScreenFn | None = None,
    ) -> OperationResult:
        """共同文件的 mutation 骨架：幂等 → 密钥筛 → 原子写（§30.2）。

        `id_field` 决定成功日志把对象 ID 记在哪个白名单字段上（§37）：候选类操作记
        `candidate_id`；批准与删除留下的对象 ID 是**生效条目**（`GM-A-…` / `GM-L-…`），必须记
        `memory_id`——否则「按 `candidate_id=MC-…` 查批准」会静默漏掉批准事件。
        """
        if not self._enabled:
            return OperationResult(status=STATUS_FORBIDDEN, object_id=None, revision=0)
        async with self._write_lock:
            state = self._common_state()
            if not isinstance(state.document, CommonDocument):
                return OperationResult(status=STATUS_UNAVAILABLE, object_id=None, revision=state.revision)
            hit = state.document.operations.get(operation_id)
            if hit is not None:
                return hit
            if screen is not None and self._contains_secret(screen(state.document)):
                return OperationResult(
                    status=STATUS_SECRET_DETECTED, object_id=None, revision=state.revision
                )
            status, object_id, written = self._write_document(
                self._common_view(), state, apply_fn, screen=screen
            )
            if written is not state:
                self._common = written
            if status == STATUS_OK:
                if id_field == "memory_id":
                    # 已生效条目按 ID 前缀报出它所在的作用域（§29.1）——只有删除是这么来的；
                    # 批准仍在与候选同一个文件里发生，作用域按公共文件记。
                    scope = _scope_of(object_id) if event == "memory.updated" else "common"
                    self._log_updated(
                        event, scope=scope, revision=written.revision, memory_id=object_id
                    )
                else:
                    # 候选类的操作统一记公共文件（候选 ID 不带作用域）。
                    self._log_updated(
                        event,
                        scope="common",
                        revision=written.revision,
                        candidate_id=object_id,
                    )
            return OperationResult(status=status, object_id=object_id, revision=written.revision)

    def _contains_secret(self, texts: tuple[str, ...]) -> bool:
        """密钥筛（§30.2、§37）：脱敏前后不一致就是命中，整条拒绝。

        比对的是**将要写进 Markdown 的全部文本**：正文与 key 都算（key 也会落盘）。命中的具体
        字符串绝不进日志，因此这里只返回布尔值；不保存任何 `[redacted]` 版本，也不落盘。
        """
        redactor = self._redactor
        if redactor is None:
            return False
        for text in texts:
            if isinstance(text, str) and redactor.redact(text) != text:
                return True
        return False

    # --- 原子写（规划 §5.5 的七步） ----------------------------------------

    def _write_document(
        self,
        view: _DocumentView,
        snapshot: _Snapshot,
        apply_fn: _ApplyFn,
        *,
        screen: _ScreenFn | None = None,
    ) -> tuple[str, str | None, _Snapshot]:
        """一次原子写入；返回（状态, 对象 ID, 新快照）。

        任一步失败都保留原正式文件与旧快照并清理临时文件，失败一律映射为 `unavailable`
        （§30.3 第 7 步、D-60）。同一份 `apply_fn` 会被用两次：一次以内存快照为基线，
        一次以接手的**外部版本**为基线（§5.5 第 4 步）。

        `screen` 也要跑两次：一次是调用方对内存快照跑的那次（在 mutation 入口），这里再对
        **外部版本**跑一次。§30.2 的密钥筛覆盖「任何将要写进 Markdown 的正文」，而外部版本接手后
        写进文件的正文来自磁盘上那份文档（批准动作会把**基线里**的候选正文搬进生效区），
        快照上的那次筛选覆盖不到它；命中同样整条拒绝、不落盘（返回 `secret_detected`）。合法外部
        版本照常被采纳进快照（§30.4），只是本次操作不写。

        **ID 收敛也在这里收口**（§29.1 的 ID 义务）：render 只给条目与候选的**标题行**补零，
        候选的 `target_id` 与 `operations.object_id` 是逐字渲染的，而 applier 会把磁盘上人工改窄
        的 ID（`GM-L-7`）原样抄进新文档——只让窄 ID「幸存」的写入因此会让快照停在窄形态、文件却
        是 6 位补零。在 render 之前把整份文档的 ID 收敛成渲染形态，并存下**收敛后**的那一份，
        快照与文件字节就不会再在 ID 宽度上漂移；`_write_document` 是唯一 render 与落盘的地方，
        所以这条覆盖由构造保证，不靠每个 applier 各自记得规范化。已经是渲染形态的 ID 逐字不变。

        抓什么、为什么（按调用点区分，而不是按 reason 区分）：
        - 外部版本解析失败：由 `_external_baseline` 返回 None，映射成 `conflict`（§30.3 第 4 步）。
        - 渲染**本模块自己的**文档失败：`CodecError`（render 的校验）与 `UnicodeEncodeError`
          （正文带孤立代理项，UTF-8 编码不出来）都映射成 `unavailable`——提案本身已经过校验，
          失败在持久化这一步。
        - 文件系统失败：`OSError`（建目录、独占创建、写、fsync、`os.replace`）。
        - 兜底 `Exception`：§30.3 第 7 步说「任一步失败都映射为 unavailable」，而记忆的调用方是
          聊天 worker（D-60）。`BaseException` 与 `CancelledError` **不**在这里被吞掉。
        """
        document = snapshot.document
        if document is None:
            # 磁盘上有这份文件但读不回来：绝不覆盖，等人工修好或外部编辑再接手。
            return STATUS_UNAVAILABLE, None, snapshot
        temp_path: str | None = None
        try:
            built, status, object_id = apply_fn(document)
            if status != STATUS_OK or built is None:
                # full / not_found / conflict / noop / invalid_proposal：一个字节都不写（§27.4）。
                return status, None, snapshot
            built = _canonical_document(built)
            object_id = _canonical_object_id(object_id)
            data = self._render(built)
            temp_path = _write_temp_file(os.path.dirname(view.path), data)
            disk = _read_bytes(view.path, self._config.max_file_bytes + 1)
            digest = _digest(disk)
            if digest != snapshot.digest:
                # 外部编辑：先尝试接手合法的那一份，再以它为基线重放本次操作（§5.5 第 3、4 步）。
                baseline = self._external_baseline(view, disk)
                if baseline is None:
                    return STATUS_CONFLICT, None, snapshot
                adopted = _snapshot_of(baseline, digest, self._now())
                if screen is not None and self._contains_secret(screen(baseline)):
                    # 基线里那份正文即将被搬进生效区：与入口处的命中同一条路——整条拒绝、
                    # 不落盘、不记命中串（§30.2、§37）。
                    return STATUS_SECRET_DETECTED, None, adopted
                built, status, object_id = apply_fn(baseline)
                if status != STATUS_OK or built is None:
                    return status, None, adopted
                built = _canonical_document(built)
                object_id = _canonical_object_id(object_id)
                data = self._render(built)
                _overwrite_temp_file(temp_path, data)
            self._replace(temp_path, view.path)
            temp_path = None
            adopted = _digest(data)
            return STATUS_OK, object_id, _Snapshot(
                document=built,
                digest=adopted,
                reason=None,
                attempted=adopted,
                checked_at=self._now(),
            )
        except CodecError as exc:
            self._log_write_failed(view.scope, reason=exc.reason)
            return STATUS_UNAVAILABLE, None, snapshot
        except UnicodeEncodeError as exc:
            # 绝不记 str(exc)：UnicodeEncodeError.object 就是被编码的整份文档正文（§37）。
            self._log_write_failed(view.scope, error=type(exc).__name__)
            return STATUS_UNAVAILABLE, None, snapshot
        except OSError as exc:
            self._log_write_failed(view.scope, error=type(exc).__name__)
            return STATUS_UNAVAILABLE, None, snapshot
        except Exception as exc:  # 兜底见 docstring：任一步失败都降级，绝不外溢（D-60）。
            self._log_write_failed(view.scope, error=type(exc).__name__)
            return STATUS_UNAVAILABLE, None, snapshot
        finally:
            if temp_path is not None:
                _remove_quietly(temp_path)

    def _external_baseline(self, view: _DocumentView, disk: bytes | None) -> _MemoryDocument | None:
        """载入外部版本：文件被删掉按空文档处理；解析不出来返回 None（调用方映射成 conflict）。"""
        if disk is None:
            return view.fresh()
        try:
            return view.parser(disk, self._config)
        except CodecError:
            return None

    def _render(self, document: _MemoryDocument) -> bytes:
        if isinstance(document, PrivateDocument):
            return render_private(document)
        return render_common(document)

    # --- 快照、缓存与刷新 --------------------------------------------------

    def _common_view(self) -> _DocumentView:
        return _DocumentView(
            path=self._common_path, parser=parse_common, fresh=CommonDocument, scope="common"
        )

    def _private_view(self, user_id: str) -> _DocumentView:
        return _DocumentView(
            path=self.private_path(user_id), parser=parse_private, fresh=PrivateDocument, scope="user"
        )

    def _common_state(self) -> _Snapshot:
        """共同快照：惰性加载一次；之后的刷新由 `start()` 起的任务负责（§30.4）。"""
        if self._common is None:
            self._common = self._inspect(
                self._common_view(), None, ttl=None, event="memory.load_failed", force=True
            )
        return self._common

    def _user_state(self, user_id: str) -> _Snapshot:
        """用户快照：惰性加载 + 有界 LRU；TTL 到期后的下一次访问检查摘要（§30.4）。"""
        state = self._users.get(user_id)
        fresh = self._inspect(
            self._private_view(user_id),
            state,
            ttl=self._refresh_seconds,
            event="memory.refresh_failed" if state is not None else "memory.load_failed",
        )
        self._store_user(user_id, fresh)
        return fresh

    def _store_user(self, user_id: str, state: _Snapshot) -> None:
        """写入缓存并维持 LRU 上界；淘汰只释放内存，**不删文件**（§30.4）。"""
        self._users[user_id] = state
        self._users.move_to_end(user_id)
        while len(self._users) > _USER_CACHE_SIZE:
            self._users.popitem(last=False)

    def _inspect(
        self,
        view: _DocumentView,
        state: _Snapshot | None,
        *,
        ttl: float | None,
        event: str,
        force: bool = False,
    ) -> _Snapshot:
        """检查一次文件：内容没变就不重复解析、不重复记日志；坏了就保留最后一份有效快照（§30.4）。"""
        now = self._now()
        if not force and state is not None and ttl is not None and now - state.checked_at < ttl:
            return state
        try:
            data = _read_bytes(view.path, self._config.max_file_bytes + 1)
        except OSError as exc:
            failed = _failed_snapshot(state, _REASON_IO, None, now)
            self._log_failure_once(state, failed, event, view.scope, error=type(exc).__name__)
            return failed
        if data is None:
            # 文件不存在不是失败：基线就是空文档（记忆是惰性创建的）。
            return _snapshot_of(view.fresh(), None, now)
        digest = _digest(data)
        if state is not None and digest in (state.digest, state.attempted):
            # 内容与上次检查过的一模一样：不解析、不记日志，只刷新检查时刻。
            return replace(state, checked_at=now)
        try:
            document = view.parser(data, self._config)
        except CodecError as exc:
            failed = _failed_snapshot(state, exc.reason, digest, now)
            self._log_failure_once(state, failed, event, view.scope, reason=exc.reason)
            return failed
        return _snapshot_of(document, digest, now)

    def _log_failure_once(
        self,
        previous: _Snapshot | None,
        failed: _Snapshot,
        event: str,
        scope: str,
        **fields: object,
    ) -> None:
        """同一份坏内容只记一次；换了一份坏内容、或从正常转为失败时再记。"""
        repeated = (
            previous is not None
            and previous.reason is not None
            and previous.reason == failed.reason
            and previous.attempted == failed.attempted
        )
        if repeated:
            return
        log_event(_logger, logging.WARNING, event, scope=scope, **fields)

    # --- 日志 -------------------------------------------------------------

    def _log_write_failed(
        self, scope: str, *, reason: str | None = None, error: str | None = None
    ) -> None:
        """写入失败只记稳定 token：`reason` 是 codec 的六个 reason 之一，`error` 是异常类名。

        绝不记异常字符串：`OSError` 的字符串里有绝对路径，`UnicodeEncodeError` 的对象就是正文。
        """
        fields: dict[str, object] = {"scope": scope}
        if reason is not None:
            fields["reason"] = reason
        if error is not None:
            fields["error"] = error
        log_event(_logger, logging.WARNING, "memory.write_failed", **fields)

    def _log_updated(
        self,
        event: str,
        *,
        scope: str,
        revision: int,
        memory_id: str | None = None,
        candidate_id: str | None = None,
    ) -> None:
        fields: dict[str, object] = {"scope": scope, "revision": revision}
        if memory_id is not None:
            fields["memory_id"] = memory_id
        if candidate_id is not None:
            fields["candidate_id"] = candidate_id
        log_event(_logger, logging.INFO, event, **fields)

    def _log_omitted(self, scope: str, reason: str | None, **extra: object) -> None:
        log_event(
            _logger,
            logging.INFO,
            "memory.context_omitted",
            scope=scope,
            reason=reason if reason is not None else "unavailable",
            **extra,
        )

    # --- 提案与命令的纯函数实现 --------------------------------------------

    def _apply_private_setting(
        self, document: PrivateDocument, operation_id: str, name: str, value: bool
    ) -> tuple[PrivateDocument | None, str, str | None]:
        """`set_private_enabled` / `set_auto_capture`：值没变就是 noop，不写文件。"""
        if bool(getattr(document, name)) == bool(value):
            return None, STATUS_NOOP, None
        revision = document.revision + 1
        return (
            replace(
                document,
                revision=revision,
                operations=self._record(document.operations, operation_id, STATUS_OK, None, revision),
                **{name: bool(value)},
            ),
            STATUS_OK,
            None,
        )

    def _apply_private_proposal(
        self, document: PrivateDocument, proposal: MemoryProposal, operation_id: str
    ) -> tuple[PrivateDocument | None, str, str | None]:
        """私有提案：ADD 新增（同 key 视为替换）、UPDATE 改既有条目。"""
        if proposal.action == ProposalAction.NOOP:
            return None, STATUS_NOOP, None
        if not _valid_proposal_shape(proposal, PREFIX_USER):
            return None, STATUS_INVALID_PROPOSAL, None
        stamp = self._timestamp()
        entries = list(document.entries)
        if proposal.action == ProposalAction.ADD:
            existing = _find_by_key(entries, proposal.key, None)
            if existing is not None:
                # §29.3：同 key 只能有一条，更新即替换，ID 与 created_at 保持不变。
                return self._replace_private_entry(
                    document, entries, existing, proposal, operation_id, stamp
                )
            if len(entries) >= self._config.max_private_entries_per_user:
                return None, STATUS_FULL, None
            number = _next_free_number(document.next_id, entries)
            memory_id = _format_id(PREFIX_USER, number)
            entries.append(
                MemoryEntry(
                    memory_id=memory_id,
                    key=proposal.key,
                    content=proposal.content,
                    pinned=False,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            revision = document.revision + 1
            return (
                replace(
                    document,
                    revision=revision,
                    next_id=number + 1,
                    entries=tuple(entries),
                    operations=self._record(
                        document.operations, operation_id, STATUS_OK, memory_id, revision
                    ),
                ),
                STATUS_OK,
                memory_id,
            )
        target = _find_by_memory_id(entries, proposal.target_id, PREFIX_USER)
        if target is None:
            return None, STATUS_NOT_FOUND, None
        return self._replace_private_entry(document, entries, target, proposal, operation_id, stamp)

    def _replace_private_entry(
        self,
        document: PrivateDocument,
        entries: list[MemoryEntry],
        target: MemoryEntry,
        proposal: MemoryProposal,
        operation_id: str,
        stamp: str,
    ) -> tuple[PrivateDocument | None, str, str | None]:
        """就地替换一条私有条目（同 key 的 add 与 update 共用）。"""
        if _find_by_key(entries, proposal.key, target.memory_id) is not None:
            # 改 key 撞到另一条既有条目：文件格式容不下两个同 key 条目，拒绝覆盖。
            return None, STATUS_CONFLICT, None
        entry = replace(target, key=proposal.key, content=proposal.content, updated_at=stamp)
        entry = _canonical_entry(entry, PREFIX_USER)
        entries[entries.index(target)] = entry
        revision = document.revision + 1
        return (
            replace(
                document,
                revision=revision,
                entries=tuple(entries),
                operations=self._record(
                    document.operations, operation_id, STATUS_OK, entry.memory_id, revision
                ),
            ),
            STATUS_OK,
            entry.memory_id,
        )

    def _apply_private_delete(
        self, document: PrivateDocument, memory_id: str, operation_id: str
    ) -> tuple[PrivateDocument | None, str, str | None]:
        """删除一条私有条目；前缀不对或不存在都落 not_found。"""
        target = _find_by_memory_id(list(document.entries), memory_id, PREFIX_USER)
        if target is None:
            return None, STATUS_NOT_FOUND, None
        kept = tuple(entry for entry in document.entries if entry.memory_id != target.memory_id)
        revision = document.revision + 1
        return (
            replace(
                document,
                revision=revision,
                entries=kept,
                operations=self._record(
                    document.operations, operation_id, STATUS_OK, target.memory_id, revision
                ),
            ),
            STATUS_OK,
            target.memory_id,
        )

    def _apply_private_clear(
        self, document: PrivateDocument, operation_id: str
    ) -> tuple[PrivateDocument | None, str, str | None]:
        """清空条目，保留设置、计数器与幂等元数据（`next_id` 不回收，避免 ID 被复用）。"""
        revision = document.revision + 1
        return (
            replace(
                document,
                revision=revision,
                entries=(),
                operations=self._record(document.operations, operation_id, STATUS_OK, None, revision),
            ),
            STATUS_OK,
            None,
        )

    def _apply_candidate_add(
        self,
        document: CommonDocument,
        scope: MemoryScope,
        proposal: MemoryProposal,
        operation_id: str,
    ) -> tuple[CommonDocument | None, str, str | None]:
        """写入一条候选；作用域、动作与目标前缀都在这里再校验一次（§30.2）。"""
        if scope not in (MemoryScope.ALL_USER, MemoryScope.LOBBY):
            return None, STATUS_INVALID_PROPOSAL, None
        if proposal.action == ProposalAction.NOOP:
            return None, STATUS_NOOP, None
        prefix = _scope_prefix(scope)
        if not _valid_proposal_shape(proposal, prefix):
            return None, STATUS_INVALID_PROPOSAL, None
        if proposal.action == ProposalAction.UPDATE and (
            _find_by_memory_id(list(_common_bucket(document, scope)), proposal.target_id, prefix)
            is None
        ):
            # 候选引用一条不存在的条目：候选本身就没有意义（§32.3 的 approve 会再查一遍）。
            return None, STATUS_NOT_FOUND, None
        if len(document.candidates) >= self._config.max_candidates:
            return None, STATUS_FULL, None
        number = _next_free_number(document.next_candidate_id, list(document.candidates))
        candidate_id = _format_id(PREFIX_CANDIDATE, number)
        candidate = MemoryCandidate(
            candidate_id=candidate_id,
            scope=scope,
            action=proposal.action,
            target_id=None if proposal.action == ProposalAction.ADD else _canonical(proposal.target_id, prefix),
            key=proposal.key,
            content=proposal.content,
            created_at=self._timestamp(),
        )
        revision = document.revision + 1
        return (
            replace(
                document,
                revision=revision,
                next_candidate_id=number + 1,
                candidates=tuple(document.candidates) + (candidate,),
                operations=self._record(
                    document.operations, operation_id, STATUS_OK, candidate_id, revision
                ),
            ),
            STATUS_OK,
            candidate_id,
        )

    def _apply_candidate_approve(
        self, document: CommonDocument, candidate_id: str, operation_id: str
    ) -> tuple[CommonDocument | None, str, str | None]:
        """批准候选：一次原子替换里同时「移出候选」与「写入生效区」（D-58）。"""
        candidate = _find_candidate(document.candidates, candidate_id)
        if candidate is None:
            return None, STATUS_NOT_FOUND, None
        prefix = _scope_prefix(candidate.scope)
        entries = list(_common_bucket(document, candidate.scope))
        stamp = self._timestamp()
        same_key = _find_by_key(entries, candidate.key, None)
        if candidate.action == ProposalAction.UPDATE:
            referenced = _find_by_memory_id(entries, candidate.target_id, prefix)
            if referenced is None:
                # 候选指向的条目已经没了：那也是「被改动」，不把它偷偷变成新增。
                return None, STATUS_NOT_FOUND, None
            if _touched_after(referenced.updated_at, candidate.created_at):
                # 候选生成之后有人改过目标条目：拒绝覆盖，要求重新生成候选（§32.3）。
                return None, STATUS_CONFLICT, None
            if same_key is not None and same_key.memory_id != referenced.memory_id:
                return None, STATUS_CONFLICT, None
            entry = replace(
                referenced, key=candidate.key, content=candidate.content, updated_at=stamp
            )
            entry = _canonical_entry(entry, prefix)
            entries[entries.index(referenced)] = entry
            memory_id = entry.memory_id
        elif same_key is not None:
            # 同 key 的新增 = 替换那一条（§29.3），ID 与 created_at 保持不变。ID 同样要规范成
            # 渲染形态：人工改窄过的那一条不规范化，快照与 `common_entries()` 会一直报窄形态，
            # 而文件里写着 6 位——正是 ID 义务要防的漂移。
            entry = replace(same_key, key=candidate.key, content=candidate.content, updated_at=stamp)
            entry = _canonical_entry(entry, prefix)
            entries[entries.index(same_key)] = entry
            memory_id = entry.memory_id
        else:
            if len(entries) >= self._config.max_common_entries_per_scope:
                return None, STATUS_FULL, None
            start = (
                document.next_all_user_id
                if candidate.scope == MemoryScope.ALL_USER
                else document.next_lobby_id
            )
            number = _next_free_number(start, entries)
            memory_id = _format_id(prefix, number)
            entries.append(
                MemoryEntry(
                    memory_id=memory_id,
                    key=candidate.key,
                    content=candidate.content,
                    pinned=False,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
        kept = tuple(
            item for item in document.candidates if item.candidate_id != candidate.candidate_id
        )
        revision = document.revision + 1
        changes: dict[str, object] = {
            "revision": revision,
            "candidates": kept,
            "operations": self._record(
                document.operations, operation_id, STATUS_OK, memory_id, revision
            ),
        }
        if candidate.scope == MemoryScope.ALL_USER:
            changes["all_user"] = tuple(entries)
            changes["next_all_user_id"] = _advanced_counter(
                document.next_all_user_id, memory_id, PREFIX_ALL_USER
            )
        else:
            changes["lobby"] = tuple(entries)
            changes["next_lobby_id"] = _advanced_counter(
                document.next_lobby_id, memory_id, PREFIX_LOBBY
            )
        return replace(document, **changes), STATUS_OK, memory_id

    def _apply_candidate_reject(
        self, document: CommonDocument, candidate_id: str, operation_id: str
    ) -> tuple[CommonDocument | None, str, str | None]:
        """丢弃一条候选；ID 前缀与存在性在入口再次校验。"""
        candidate = _find_candidate(document.candidates, candidate_id)
        if candidate is None:
            return None, STATUS_NOT_FOUND, None
        kept = tuple(
            item for item in document.candidates if item.candidate_id != candidate.candidate_id
        )
        revision = document.revision + 1
        return (
            replace(
                document,
                revision=revision,
                candidates=kept,
                operations=self._record(
                    document.operations, operation_id, STATUS_OK, candidate.candidate_id, revision
                ),
            ),
            STATUS_OK,
            candidate.candidate_id,
        )

    def _apply_common_delete(
        self, document: CommonDocument, memory_id: str, operation_id: str
    ) -> tuple[CommonDocument | None, str, str | None]:
        """删除一条已生效共同记忆；前缀不对或不存在都落 not_found。"""
        for scope in (MemoryScope.ALL_USER, MemoryScope.LOBBY):
            prefix = _scope_prefix(scope)
            target = _find_by_memory_id(list(_common_bucket(document, scope)), memory_id, prefix)
            if target is None:
                continue
            kept = tuple(
                entry
                for entry in _common_bucket(document, scope)
                if entry.memory_id != target.memory_id
            )
            revision = document.revision + 1
            field_name = "all_user" if scope == MemoryScope.ALL_USER else "lobby"
            return (
                replace(
                    document,
                    revision=revision,
                    operations=self._record(
                        document.operations, operation_id, STATUS_OK, target.memory_id, revision
                    ),
                    **{field_name: kept},
                ),
                STATUS_OK,
                target.memory_id,
            )
        return None, STATUS_NOT_FOUND, None

    def _record(
        self,
        operations: Mapping[str, OperationResult],
        operation_id: str,
        status: str,
        object_id: str | None,
        revision: int,
    ) -> dict[str, OperationResult]:
        """记录幂等结果；超过 `max_operations` 时按插入顺序淘汰最旧的键。

        `operations` 没有时间戳，淘汰只能按顺序：进程内是插入顺序，重启后解析出的顺序退化为文件序
        （即按键排序）。不淘汰的话文件会超过 `max_operations`，codec 直接读不回来（§29.2 第 5 条）。
        """
        updated = dict(operations)
        updated.pop(operation_id, None)
        updated[operation_id] = OperationResult(status=status, object_id=object_id, revision=revision)
        while len(updated) > self._config.max_operations:
            oldest = next(iter(updated))
            if oldest == operation_id:
                break
            updated.pop(oldest)
        return updated

    def _timestamp(self) -> str:
        """当前时刻的 UTC RFC 3339 秒级文本（codec 只接受 UTC，§29.2 第 8 条）。"""
        return (
            datetime.fromtimestamp(self._now(), tz=timezone.utc)
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )


# --- 模块级纯函数 ---------------------------------------------------------


def _scope_prefix(scope: MemoryScope) -> str:
    return PREFIX_ALL_USER if scope == MemoryScope.ALL_USER else PREFIX_LOBBY


def _scope_of(object_id: str | None) -> str:
    """已生效共同条目的作用域由 ID 前缀决定（§29.1）。"""
    return "lobby" if object_id and object_id.startswith(PREFIX_LOBBY) else "all_user"


def _common_bucket(document: CommonDocument, scope: MemoryScope) -> tuple[MemoryEntry, ...]:
    return document.all_user if scope == MemoryScope.ALL_USER else document.lobby


def _common_entry_count(document: CommonDocument) -> int:
    return len(document.all_user) + len(document.lobby)


def _digest(data: bytes | None) -> bytes | None:
    """文件字节摘要；文件不存在时是 None（「不存在」也是一个可比较的基线）。"""
    if data is None:
        return None
    return hashlib.sha256(data).digest()


def _snapshot_of(document: _MemoryDocument, digest: bytes | None, now: float) -> _Snapshot:
    return _Snapshot(
        document=document,
        digest=digest,
        reason=None,
        attempted=digest,
        checked_at=now,
    )


def _failed_snapshot(
    previous: _Snapshot | None, reason: str, digest: bytes | None, now: float
) -> _Snapshot:
    """读不回来时的快照：**保留**最后一份有效文档（§30.4），只记下失败原因。

    冷启动本来就没有有效快照（`previous is None`）时，document 为 None，整个范围因此不可用。
    """
    return _Snapshot(
        document=previous.document if previous is not None else None,
        digest=previous.digest if previous is not None else None,
        reason=reason,
        attempted=digest if digest is not None else (previous.attempted if previous is not None else None),
        checked_at=now,
    )


def _read_bytes(path: str, limit: int) -> bytes | None:
    """读文件（最多 `limit` 字节）；不存在返回 None，其它读失败抛 OSError。

    `limit` 传 `max_file_bytes + 1`：多读一个字节就足以让 codec 判定 `too_large`（§29.2）。
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except FileNotFoundError:
        return None


def _write_temp_file(directory: str, data: bytes) -> str:
    """在目标同目录独占创建一个临时文件并写入完整 bytes（§5.5 第 1、2 步）。"""
    directory = directory or "."
    os.makedirs(directory, exist_ok=True)
    handle, path = tempfile.mkstemp(dir=directory, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
    with os.fdopen(handle, "wb") as stream:
        _write_synced(stream, data)
    return path


def _overwrite_temp_file(path: str, data: bytes) -> None:
    """外部版本接手后换掉临时文件里的内容（还是同一个临时文件，正式文件始终没动过）。"""
    with open(path, "wb") as stream:
        _write_synced(stream, data)


def _write_synced(stream: IO[bytes], data: bytes) -> None:
    stream.write(data)
    stream.flush()
    os.fsync(stream.fileno())


def _remove_quietly(path: str) -> None:
    """清理临时文件；清理本身失败绝不影响这次操作的结果（§5.5 第 7 步）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _canonical_entry(entry: MemoryEntry, prefix: str) -> MemoryEntry:
    """把条目的 ID 规范成渲染形态；形状不对时原样返回（§29.1）。

    人工改窄过宽度的文件在这里收敛：不收敛的话内存快照会一直停在 `GM-L-7`，而文件里写着
    `GM-L-000007`，快照与字节在 ID 宽度上逐次写入地漂移。
    """
    canonical = _canonical(entry.memory_id, prefix)
    if canonical is None:
        return entry
    return replace(entry, memory_id=canonical)


def _canonical_document(document: _MemoryDocument) -> _MemoryDocument:
    """把整份文档的 ID 收敛成渲染形态；写路径的公共收口（§29.1 的 ID 义务）。

    render 只给条目与候选的标题行补零，候选的 `target_id` 与 `operations.object_id` 是逐字渲染
    的，而 applier 会把磁盘上人工改窄的 ID（`GM-L-7`）原样抄进新文档——「只让窄 ID 幸存」的删除
    也会这样。只在这里收敛一次，所有写入路径就都被覆盖：`_write_document` 是唯一 render 与落盘的
    地方，快照存的也是这一份收敛后的文档，快照与文件字节不再在 ID 宽度上漂移。形状不认得的 ID
    原样保留（与 `_canonical_entry` 同口径），真正的拒绝由 codec 的校验负责。
    """
    if isinstance(document, PrivateDocument):
        return replace(
            document,
            entries=tuple(_canonical_entry(item, PREFIX_USER) for item in document.entries),
            operations=_canonical_operations(document.operations),
        )
    return replace(
        document,
        all_user=tuple(_canonical_entry(item, PREFIX_ALL_USER) for item in document.all_user),
        lobby=tuple(_canonical_entry(item, PREFIX_LOBBY) for item in document.lobby),
        candidates=tuple(_canonical_candidate(item) for item in document.candidates),
        operations=_canonical_operations(document.operations),
    )


def _canonical_candidate(candidate: MemoryCandidate) -> MemoryCandidate:
    """候选的编号与目标引用都收敛成渲染形态；形状不认得时原样保留（§29.1）。

    候选的作用域只能是 `all_user` / `lobby`（codec 的校验保证），因此目标引用按作用域前缀收敛。
    """
    prefix = _scope_prefix(candidate.scope)
    candidate_id = _canonical(candidate.candidate_id, PREFIX_CANDIDATE)
    target_id = _canonical(candidate.target_id, prefix)
    return replace(
        candidate,
        candidate_id=candidate_id if candidate_id is not None else candidate.candidate_id,
        target_id=target_id if target_id is not None else candidate.target_id,
    )


def _canonical_object_id(object_id: str | None) -> str | None:
    """操作结果里的对象 ID 收敛成渲染形态；不是 ID（或形状不认得）时原样返回（§29.1）。

    对象 ID 可以是生效条目、候选或私有条目，前缀由 ID 自己带，这里只能逐个试。它会被逐字渲染进
    `operations`，也会经 `find_operation` 回到调用方，所以同样属于「写前必须收敛」的范围。
    """
    if not isinstance(object_id, str):
        return object_id
    for prefix in _ID_PREFIXES:
        canonical = _canonical(object_id, prefix)
        if canonical is not None:
            return canonical
    return object_id


def _canonical_operations(
    operations: Mapping[str, OperationResult],
) -> Mapping[str, OperationResult]:
    """把 `operations` 记录的对象 ID 一并收敛；键的插入顺序原样保留（`_record` 按它淘汰最旧的键）。"""
    return {
        operation_id: replace(result, object_id=_canonical_object_id(result.object_id))
        for operation_id, result in operations.items()
    }


def _storage_key(user_id: str) -> str | None:
    """§27.3 的用户存储键；`user_id` 含孤立代理项、编码不进严格 UTF-8 时返回 None。

    `models.user_storage_key` 用严格 UTF-8 编码 `"raricy-memory-v1\\0" + user_id`，孤立代理项会抛
    `UnicodeEncodeError`；站点给的 `author.id` 不可能长这样，但读路径不能因此把异常抛给调用方
    （D-60）。返回 None 表示「这个 ID 没有可用的存储键」：`private_path` 退回同形的兜底路径，
    `_mutate_private` 则直接落 `forbidden`，绝不替一个不存在的 ID 建目录或落文件。
    """
    try:
        return user_storage_key(user_id)
    except UnicodeEncodeError:
        return None


def _invalid_id_path_key(user_id: str) -> str:
    """编码不出来的 `user_id` 的兜底存储键：`surrogatepass` 让这次编码永远成立。

    用**另一个**域前缀，因此与任何合法 ID 的存储键都不会互相覆盖（合法 ID 走 `user_storage_key`
    那条分支）。哈希只用于生成 ASCII 文件名与「这条路径上没有文件」的判定，同样不构成加密。
    """
    raw = f"raricy-memory-invalid-id\0{user_id}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def _canonical(memory_id: str | None, prefix: str) -> str | None:
    """把 ID 规范成渲染形态（前缀 + 6 位零填充）；形状不对返回 None（§29.1）。"""
    if not isinstance(memory_id, str) or not memory_id.startswith(prefix):
        return None
    digits = memory_id[len(prefix) :]
    if _ASCII_DIGITS.fullmatch(digits) is None:
        return None
    return f"{prefix}{int(digits):0{ID_DIGITS}d}"


def _format_id(prefix: str, number: int) -> str:
    """分配 ID 时就写成渲染形态：快照与文件因此不会在宽度上漂移（§29.1）。"""
    return f"{prefix}{number:0{ID_DIGITS}d}"


def _id_number(memory_id: str) -> int | None:
    digits = memory_id.rsplit("-", 1)[-1]
    if _ASCII_DIGITS.fullmatch(digits) is None:
        return None
    return int(digits)


def _next_free_number(start: int, items: Any) -> int:
    """从 `start` 起找第一个没被占用的序号；人工改小的计数器不会撞出重复 ID。"""
    taken = {
        _id_number(item.memory_id if isinstance(item, MemoryEntry) else item.candidate_id)
        for item in items
    }
    number = max(1, int(start))
    while number in taken:
        number += 1
    return number


def _advanced_counter(counter: int, memory_id: str, prefix: str) -> int:
    """分配后把计数器推到已用序号之后。"""
    if not memory_id.startswith(prefix):
        return counter
    number = _id_number(memory_id)
    if number is None:
        return counter
    return max(int(counter), number + 1)


def _find_by_memory_id(
    entries: list[MemoryEntry], memory_id: str | None, prefix: str
) -> MemoryEntry | None:
    """按**归一化后**的形态找条目：人工写成 `UM-6` 的文件仍能被 `UM-000006` 命中（§29.1）。"""
    wanted = _canonical(memory_id, prefix)
    if wanted is None:
        return None
    for entry in entries:
        if _canonical(entry.memory_id, prefix) == wanted:
            return entry
    return None


def _find_by_key(entries: list[MemoryEntry], key: str, exclude_id: str | None) -> MemoryEntry | None:
    for entry in entries:
        if entry.key == key and entry.memory_id != exclude_id:
            return entry
    return None


def _find_candidate(
    candidates: tuple[MemoryCandidate, ...], candidate_id: str
) -> MemoryCandidate | None:
    wanted = _canonical(candidate_id, PREFIX_CANDIDATE)
    if wanted is None:
        return None
    for candidate in candidates:
        if _canonical(candidate.candidate_id, PREFIX_CANDIDATE) == wanted:
            return candidate
    return None


def _candidate_texts(document: Any, candidate_id: str) -> tuple[str, ...]:
    """候选将要写进 Markdown 的文本；找不到候选时没有可筛的内容（随后落 not_found）。"""
    if not isinstance(document, CommonDocument):
        return ()
    found = _find_candidate(document.candidates, candidate_id)
    if found is None:
        return ()
    return (found.key, found.content)


def _valid_key(value: object) -> bool:
    """与 codec 的 `_check_key` 同口径：非空、无首尾空白、无控制字符与其它空白。

    服务侧自己先判一次，是为了让「key 不合法」落成 `invalid_proposal` 这个稳定状态，
    而不是让 render 抛 `CodecError`（那是编程错误，不该出现在用户可预期的失败里）。
    """
    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    for char in value:
        if ord(char) < 32 or ord(char) == 127 or (char.isspace() and char != " "):
            return False
    return True


def _valid_proposal_shape(proposal: MemoryProposal, prefix: str) -> bool:
    """提案的结构校验：key 必须是 codec 认得的单行 key，`add` 不带目标、`update` 带同前缀目标。

    真正的授权（谁能写哪个作用域）不在这里，而在 Router / Controller 的访问门（§28、D-55）。
    """
    if not _valid_key(proposal.key) or not isinstance(proposal.content, str):
        return False
    if proposal.action == ProposalAction.ADD:
        return proposal.target_id is None
    if proposal.action == ProposalAction.UPDATE:
        return _canonical(proposal.target_id, prefix) is not None
    return False


def _moment(text: str) -> datetime | None:
    """解析 UTC RFC 3339 时间戳；不合法返回 None（时间只用于排序与审阅，不参与授权）。"""
    if not isinstance(text, str):
        return None
    body = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(body)
    except ValueError:
        return None
    if moment.utcoffset() != timedelta(0):
        return None
    return moment


def _descending_stamp(text: str) -> float:
    """排序键：`updated_at` 越新越靠前，因此取负的时间戳。"""
    moment = _moment(text)
    if moment is None:
        return 0.0
    return -moment.timestamp()


def _touched_after(updated_at: str, created_at: str) -> bool:
    """目标条目在候选生成之后被改过：`updated_at > candidate.created_at`（秒级粒度）。"""
    target = _moment(updated_at)
    baseline = _moment(created_at)
    if target is None or baseline is None:
        # 时间不可比时按「没被改动」处理：时间不参与授权，宁可让后续的 key 冲突检查兜住。
        return False
    return target > baseline
