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

**公开投影**（第三类文件，§42）与私人/共同文件共用这一整套机制：同一个写锁、同一套七步原子写、
同一套快照缓存（TTL 与 LRU 的形状都对私有文件那一套逐条照搬），只是多了一份 username 索引。
两条结构性的边界在这里落地：

- **公开读路径只读 `public/`**：`public_context_for` 与 `public_entries` 只会走
  `public_path_from_owner_key` → `_public_state`，从不调用 `private_path` / `_user_state`
  （§3.1、Global Constraints 第 7 条）。不是「读了再过滤」，是结构上读不到。
- **AI 不能静默改写已公开的来源条目**（§42.6）：`apply_private_proposal` 在改动既有条目的两条
  路径（`update` / 同 key 的 `add` 替换）上先查公开投影，命中即 `public_conflict` 且两个文件都不写；
  公开状态无法确认时保守拒绝（`unavailable`）。删除不经这道门——它只由用户命令触发（§52）。
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
from typing import IO, Any, TypeVar

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
    parse_public,
    render_common,
    render_private,
    render_public,
)
from .models import (
    STATUS_CONFLICT,
    STATUS_FORBIDDEN,
    STATUS_FULL,
    STATUS_INVALID_PROPOSAL,
    STATUS_NOT_FOUND,
    STATUS_NOOP,
    STATUS_OK,
    STATUS_PUBLIC_CONFLICT,
    STATUS_SECRET_DETECTED,
    STATUS_UNAVAILABLE,
    AutoCaptureToken,
    MemoryCandidate,
    MemoryContext,
    MemoryEntry,
    MemoryProposal,
    MemoryScope,
    OperationResult,
    ProposalAction,
    PublicMemoryDocument,
    PublicMemoryEntry,
    PublicMemorySubject,
    user_storage_key,
)

__all__ = ["MemoryService", "PrivateSettings"]

_logger = get_logger("memory")

# 频道类型（§30.2）：只有 dm 会碰用户私有文件，lobby 与 comment 连读都不读。
_DM: str = "dm"
_LOBBY: str = "lobby"
_COMMENT: str = "comment"

# 公开读路径唯一接受的两种频道（§42.1、R4）：dm 与未知值一律空结果。私有文件的读取门是
# 「只有 dm」，公开投影的读取门正好相反——它只出现在大区与评论里。
_PUBLIC_CHANNELS: frozenset[str] = frozenset((_LOBBY, _COMMENT))

# `SupplementalItem.group` 的四个稳定取值（§30.2、§33、§45.3）。
_GROUP_ALL_USER: str = "memory_all_user"
_GROUP_LOBBY: str = "memory_lobby"
_GROUP_USER: str = "memory_user"
_GROUP_PUBLIC: str = "memory_public_personal"

# `priority` 数值是实现细节（D-62）：只表达「DM 私有优先于 all_user，lobby 优先于 all_user」的
# 组间次序。`SupplementalItem.priority` 是一个整数（§33），组间次序只能靠**步长**表达，因此步长
# 在构造时按容量上限现算（见 `__init__`），不能写死——§26.2 只要求上限是正整数，不保证它小于
# 某个固定的步长。

# 公开个人记忆的组间基数（§45.3）：它必须**整体晚于**既有三组（数值更大），因此取「已用基数的
# 下一个」。既有基数只有 0（lobby / 私有）与 1（all_user），最大名次是 `max_common - 1`，而步长
# 至少是 `max_common + 1`，所以 2 * 步长 > 1 * 步长 + max_common - 1 恒成立（见 `__init__` 的
# 现算注释）。公开组是最后一组，它自己的名次再大也不会撞进别的组。
_PUBLIC_PRIORITY_BASE: int = 2

# 用户快照 LRU 的容量：只影响内存，淘汰不删文件（§30.4）。
_USER_CACHE_SIZE: int = 64

# 公开快照 LRU 的容量（§42.2）：与用户快照同款的「惰性加载 + 有界 LRU」，但**独立一份**，
# 两类快照的容量因此可以各自演化。同样只影响内存，淘汰不删文件。
_PUBLIC_CACHE_SIZE: int = 64

# 公开索引扫描的文件数量硬上限（§42.2、R14）：**代码常量**，不可由 YAML 改，也不受
# `max_private_entries_per_user`（那是条目数上限）管辖。超出的文件不索引、只记一个稳定 reason：
# 一个被塞了几万个文件的目录不能拖垮进程，也不能让索引构建变成一次长时间占用。
MAX_PUBLIC_FILES: int = 4096

# 临时文件：与目标文件同目录、独占创建；测试据此断言成功与失败路径都清理干净（§5.5 第 1、7 步）。
_TEMP_PREFIX: str = ".memory-tmp-"
_TEMP_SUFFIX: str = ".tmp"

# 三类记忆文件的扩展名；索引扫描按它筛 `public/` 下的候选文件名。
_MD_SUFFIX: str = ".md"

# 幂等键与 ID 的 ASCII 十进制序号（与 codec 同一口径，`digit()` 会放过非 ASCII 数字）。
_ASCII_DIGITS = re.compile(r"[0-9]+")

# 四个 ID 前缀：`_canonical_object_id` 靠它把「前缀自带」的对象 ID 收敛成渲染形态（§29.1）。
_ID_PREFIXES: tuple[str, ...] = (PREFIX_ALL_USER, PREFIX_LOBBY, PREFIX_USER, PREFIX_CANDIDATE)

# 稳定失败原因里表示「文件系统错误」的那个（codec 的六个 reason 之外唯一允许进日志的值）。
_REASON_IO: str = "io"

# `public/` 目录里文件数超过 `MAX_PUBLIC_FILES` 时的稳定 reason（R14）：同样只记 token，
# 不记文件名、路径或数量之外的任何东西。
_REASON_TOO_MANY_FILES: str = "too_many_files"

# 三类文件的解析结果。
_MemoryDocument = CommonDocument | PrivateDocument | PublicMemoryDocument

# 纯函数形式的操作：给定基线文档，返回（新文档, 稳定状态, 对象 ID）；状态非 ok 时新文档为 None。
_ApplyFn = Callable[[Any], "tuple[Any | None, str, str | None]"]

# 密钥筛的取材函数：给定基线文档，返回本次将要写进 Markdown 的文本（正文与 key）。
_ScreenFn = Callable[[Any], "tuple[str, ...]"]

# 私有条目与公开条目共享的「带 memory_id 的条目」形状：ID 收敛对两者是同一套规则（§41.3）。
_EntryLike = TypeVar("_EntryLike", MemoryEntry, PublicMemoryEntry)

# owner key 的形状：`user_storage_key` 的输出，64 位小写十六进制（§10.1）。
_OWNER_KEY_LENGTH: int = 64
_OWNER_KEY_CHARS = frozenset("0123456789abcdef")


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
        # 公开投影目录：只在 `enabled=true` 时由 `start()` 创建，读路径永不创建（§42.2）。
        self._public_dir: str = os.path.join(self._root, "public")
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
        # 自动提取的进程内失效状态（修复计划 §2.3）：`_capture_epoch` 随服务停止递增，
        # `_capture_generation` 按用户记录提取代次。两者都不落盘 —— 进程重启后没有旧的模型调用
        # 会继续返回，因此不需要持久字段。它们与写锁一起保证「同一把锁内复核」。
        self._capture_epoch: int = 0
        self._capture_generation: dict[str, int] = {}
        self._common: _Snapshot | None = None
        self._users: "OrderedDict[str, _Snapshot]" = OrderedDict()
        # 公开快照按 owner key 索引（owner key 是不可逆的存储键，原始 user ID 不参与索引）。
        self._public: "OrderedDict[str, _Snapshot]" = OrderedDict()
        # username → owner key(s)：**一次引用替换**发布给读者（§42.2、§42.3）。它是不可变映射的
        # 语义（只整体替换，绝不就地改），因此读者要么看到旧的一整份、要么看到新的一整份。
        self._public_index: Mapping[str, tuple[str, ...]] = {}
        # 最近一次扫描记下的稳定 reason（超上限 / 目录不可读）：只在取值变化时记一条日志（§42.3）。
        self._public_scan_reason: str | None = None
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
        # 公开投影是**独立**的软故障面（§42.8）：`public/` 建不出来或扫不动都不影响上面已经就位的
        # common、刷新循环与聊天，因此它放在同一个 try 之外。
        self._start_public()

    async def stop(self) -> None:
        """停掉刷新任务；未启用或未启动时什么都不做，绝不抛出（§30.1）。"""
        # 停止即让所有未提交的自动提取令牌失效（修复计划 §2.3 第 5 条）：停止之后不该再接受
        # 停止之前发出的令牌。放在最前，未启用/未启动的服务同样走这一步。
        self._capture_epoch += 1
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
        """按 `refresh_seconds` 检查 `common.md` 与 `public/` 的外部编辑（§30.4、§42.3）。被取消即退出。"""
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                self._refresh_common()
                self._refresh_public()
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

    # --- 公开投影：目录、索引与扫描 -----------------------------------------

    def _start_public(self) -> None:
        """建 `public/` 并扫描一次索引（§42.2、§42.3）；失败只降级，绝不抛出。"""
        try:
            os.makedirs(self._public_dir, exist_ok=True)
        except OSError as exc:
            # 建不出来（路径被文件占着、权限不足）：公开投影整块不可用，其余照常。
            self._log_scan_failure(_REASON_IO, error=type(exc).__name__)
            return
        try:
            count = self._refresh_public()
        except Exception as exc:  # 扫描自身的兜底：索引构建永远不能把异常带出去。
            self._log_scan_failure(_REASON_IO, error=type(exc).__name__)
            return
        if count is None:
            return
        log_event(
            _logger, logging.INFO, "memory.ready", scope="public", public_entry_count=count
        )

    def _refresh_public(self) -> int | None:
        """重扫 `public/` 并**一次引用替换**索引；返回索引到的公开条目总数（§42.3）。

        目录不可读时返回 None（调用方据此不记 ready）。

        每个文件走与用户私有文件同款的快照助手（惰性加载 + 有界 LRU + 摘要比对），因此摘要没变
        的文件不重新解析。索引项的移除只发生在三种情形：文件消失/变坏、文档变成空文档、
        条目全部被撤回（§42.3）——因此入索引的判据是**这一次检查是好的**（`reason is None`）而非
        「有最后一份有效快照」：坏文件仍然可以被已有 subject 读到旧内容（§42.8），但它不该继续
        出现在 username 索引里。
        """
        if not self._enabled:
            return None
        keys = self._public_file_keys()
        if keys is None:
            # 目录不可读：保留最后一份索引与全部快照，只第一次记一条稳定 reason（§42.8）。
            return None
        index: dict[str, list[str]] = {}
        total = 0
        for owner_key in keys:
            state = self._public_state(owner_key, force=True)
            document = state.document
            if state.reason is not None or not isinstance(document, PublicMemoryDocument):
                continue
            if not document.entries:
                # 空公开文档不入索引（R9）：它只是幂等元数据的载体。
                continue
            index.setdefault(document.owner_username, []).append(owner_key)
            total += len(document.entries)
        self._publish_index(index)
        return total

    def _public_file_keys(self) -> tuple[str, ...] | None:
        """`public/` 下的 owner key 列表（按文件名排序）；目录不可读返回 None。

        非法的文件名（不是 `.md`、或 `.md` 的主干不是 64 位小写十六进制的 owner key 形状）直接跳过：
        那不是任何 owner 的公开文件，也就没有可归属的「坏文件」要记。文件数超过 `MAX_PUBLIC_FILES`
        时按排序截断，只记一个稳定 reason（R14）。
        """
        try:
            with os.scandir(self._public_dir) as scan:
                names = [entry.name for entry in scan]
        except FileNotFoundError:
            # 目录不存在（还没启用过、或被人删掉）：没有任何公开投影，不是失败。
            self._note_scan_reason(None)
            return ()
        except OSError as exc:
            self._log_scan_failure(_REASON_IO, error=type(exc).__name__)
            return None
        keys = sorted(
            name[: -len(_MD_SUFFIX)]
            for name in names
            if name.endswith(_MD_SUFFIX) and _valid_owner_key(name[: -len(_MD_SUFFIX)])
        )
        if len(keys) > MAX_PUBLIC_FILES:
            self._log_scan_failure(_REASON_TOO_MANY_FILES)
            keys = keys[:MAX_PUBLIC_FILES]
        else:
            self._note_scan_reason(None)
        return tuple(keys)

    def _publish_index(self, index: Mapping[str, list[str]]) -> None:
        """把刚建好的索引发布给读者：一次引用替换，读到索引的人不会看到半个（§42.2）。"""
        self._public_index = {name: tuple(keys) for name, keys in index.items()}

    def _log_scan_failure(self, reason: str, **fields: object) -> None:
        """扫描失败只记一条稳定 reason；同一份失败重复出现不再刷屏（§42.3、§51）。"""
        if self._public_scan_reason == reason:
            return
        self._public_scan_reason = reason
        log_event(
            _logger, logging.WARNING, "memory.load_failed", scope="public", reason=reason, **fields
        )

    def _note_scan_reason(self, reason: str | None) -> None:
        """扫描恢复正常时清掉上次的失败标记，让下一次失败还能被记下来。"""
        self._public_scan_reason = reason

    def _reindex_owner(self, owner_key: str, document: _MemoryDocument | None) -> None:
        """一次 mutation 之后把该 owner 的索引项换成文档的当前事实（§42.4 第 10 步、§42.5）。"""
        current = document if isinstance(document, PublicMemoryDocument) else None
        self._public_index = _reindex(self._public_index, owner_key, current)

    # --- 读路径 -----------------------------------------------------------

    async def context_for(
        self,
        *,
        user_id: str | None,
        channel_kind: str,
        access: MemoryAccessPolicy,
    ) -> MemoryContext:
        """按作用域取候选条目（§30.2）；任何失败都返回空 items，绝不抛出（D-60）。

        DM 行的判据有两个：门禁（`access.permits_private`）**与**用户自己的
        `private_enabled`（§30.2 的 DM 行、D-78）。后者是 `/memory off` 承诺的那件事——
        「之后的私聊里我不会再参考你的条目」——用户关掉读取后，即便文件就在旁边也一条不注入。
        判定放在这里而不是装配层，是因为开关与条目写在**同一份**用户文件里：读这份文件与
        「要不要把它交出去」是同一个决定，分开会多出一次读取，也会让将来的调用方漏掉这一道。
        """
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
                    # 快照照常载入并报修订号（开关就存在这份文件里，必须先读才知道），
                    # 但开关关闭时**一条都不选**：既不是 unavailable，也不是读取失败，
                    # 因此不记 context_omitted —— 与 enabled=false 同属正常路径。
                    private_revision = state.document.revision
                    if state.document.private_enabled:
                        items.extend(
                            self._items(
                                state.document.entries, _GROUP_USER, base=0, prefix=PREFIX_USER
                            )
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

    def public_path_from_owner_key(self, owner_key: str) -> str:
        """该 owner 公开投影的路径（`<root_dir>/public/<owner_key>.md`；目录不存在时**不创建**）。

        这是公开路径的唯一入口（§42.1，与 `private_path` 的 D-65 同款）：测试与上层都从这里拿
        路径，不再自己拼文件名。**同步、只读、绝不抛出**——`owner_key` 形状非法（含路径分隔符、
        不是 64 位小写十六进制等）时返回一条同形、稳定、不含原始取值且不可能有文件的兜底路径。
        因此公开读路径在结构上只可能落在 `public/` 里，一个伪造的 owner key 越不出去。
        """
        key = owner_key if _valid_owner_key(owner_key) else _invalid_owner_key_path_key(owner_key)
        return os.path.join(self._public_dir, f"{key}{_MD_SUFFIX}")

    def public_username_index(self) -> Mapping[str, tuple[str, ...]]:
        """当前 username 索引的一次引用快照（§42.1、§42.3）：`username -> tuple[owner_key, ...]`。

        **同步、只读、大小写敏感**。调用方不得跨轮缓存它，也不得修改返回值（整体替换是发布方式，
        就地修改会破坏「读者要么看到旧的一整份、要么看到新的一整份」这条保证）。
        """
        return self._public_index

    async def public_entries(self, user_id: str) -> tuple[PublicMemoryEntry, ...]:
        """该用户自己的公开条目（`/memory list public` 与发布确认的数据源）；不可用时返回空元组。

        只读 `public/`：它不碰私人文件，也不因为「这条私有条目发不发布得出去」去读 `users/`。
        """
        if not self._enabled or not user_id:
            return ()
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return ()
        state = self._public_state(owner_key)
        if not isinstance(state.document, PublicMemoryDocument):
            return ()
        return tuple(state.document.entries)

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
        `user_id` 非空时依次查三处：该用户的私有快照 → 该用户的**公开快照** → 共同快照（§42.1）；
        为空只查共同快照——公开快照只按 owner 索引，没有 `user_id` 就没有可查的键，因此不给匿名
        调用者留一个按 operation_id 探测公开文档形状的口子。
        """
        if not self._enabled or not operation_id:
            return None
        if user_id:
            state = self._user_state(user_id)
            if isinstance(state.document, PrivateDocument):
                hit = state.document.operations.get(operation_id)
                if hit is not None:
                    return hit
            owner_key = _storage_key(user_id)
            if owner_key is not None:
                state = self._public_state(owner_key)
                if isinstance(state.document, PublicMemoryDocument):
                    hit = state.document.operations.get(operation_id)
                    if hit is not None:
                        return hit
        state = self._common_state()
        if isinstance(state.document, CommonDocument):
            return state.document.operations.get(operation_id)
        return None

    async def public_context_for(
        self,
        *,
        subjects: tuple[PublicMemorySubject, ...],
        channel_kind: str,
    ) -> MemoryContext:
        """按已选定的 subject 取公开个人记忆条目（§42.7）；任何失败都返回空 items，绝不抛出。

        **只读 `public/`**：本方法在任何分支下都不会调用 `private_path` / `_user_state`，因此
        「未公开的私有条目永不进入大区或评论」是结构性的，不是靠过滤（§3.1、Global Constraints
        第 7 条）。viewer 的接入门在 Router/App 判定；这里复查的是频道与 subject 的形状。

        两个 revision 字段不承载公开语义：公开投影可能跨多个 owner，没有单一份修订号，两个字段
        只保留类型形状（R4）。预算取舍也不在这里（D-62）：本方法只按 subject 次序返回，
        分组上限与整轮预算由 `ContextManager.build_messages` 决定。
        """
        empty = MemoryContext(common_revision=0, private_revision=None, items=())
        if not self._enabled or channel_kind not in _PUBLIC_CHANNELS:
            # DM 与未知频道一律空结果（R4）：公开投影只出现在大区与评论里。
            return empty
        try:
            items = self._public_items(subjects)
        except Exception as exc:  # 兜底：公开读取是可选增强，绝不能外溢给聊天（D-60）。
            self._log_omitted("public", "internal", error=type(exc).__name__)
            return empty
        return MemoryContext(
            common_revision=0, private_revision=None, items=tuple(items)
        )

    def _public_items(self, subjects: tuple[PublicMemorySubject, ...]) -> list[SupplementalItem]:
        """按 subject 次序取条目并给出 `priority`（§42.7、§45.3 的组间次序）。

        - 形状复查：只接受形状合法的 subject（owner key 是 64 位小写十六进制、username 满足站点
          用户名合同、`source_priority` 是整数），形状不对的直接跳过——这是纵深防御，不是
          对调用方的信任（§42.1）。同一个 owner 只取第一次出现（调用方已经去过重，这里再兜一次）。
        - 组内次序：pinned 在前，再按 `published_at`、`source_updated_at` 新到旧（§7.3 第 5 条）。
        - `priority` 在**输出次序**上递增，整体晚于既有三组（`_PUBLIC_PRIORITY_BASE`）。
        """
        if not subjects:
            return []  # 一个候选都没有：连目录都不看（这条路径因此是零 I/O 的）。
        if not self._public_dir_readable():
            # 目录不可读：本轮无公开个人记忆，聊天照常（§42.8）。
            self._log_omitted("public", _REASON_IO)
            return []
        # 先按次序收集「(标签里的 username, 条目)」，再一次性翻成条目：priority 只跟最终名次有关。
        ordered: list[tuple[str, PublicMemoryEntry]] = []
        seen: set[str] = set()
        for subject in subjects:
            if not _valid_subject(subject) or subject.owner_key in seen:
                continue
            seen.add(subject.owner_key)
            state = self._public_state(subject.owner_key)
            if not state.available or not isinstance(state.document, PublicMemoryDocument):
                # 冷启动就没有有效快照：这个 owner 本轮省略（§42.8）。
                self._log_omitted("public", state.reason)
                continue
            entries = _ordered_public_entries(state.document.entries)
            ordered.extend((subject.username, entry) for entry in entries)
        base = _PUBLIC_PRIORITY_BASE * self._priority_stride
        return [
            SupplementalItem(
                group=_GROUP_PUBLIC,
                label=f"@{username} / {entry.memory_id}",
                content=entry.content,
                priority=base + rank,
            )
            for rank, (username, entry) in enumerate(ordered)
        ]

    def _public_dir_readable(self) -> bool:
        """`public/` 目录本身能不能列：不能就是「本轮无公开个人记忆」（§42.8）。

        目录**不存在**是正常路径而不是失败：那时一个公开文件也没有，逐 owner 读到的同样是空文档，
        结果一样是空的（也不记日志）。只有「存在但读不动」才降级成本轮无结果。
        """
        try:
            with os.scandir(self._public_dir):
                return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    # --- mutation：私有 ----------------------------------------------------

    async def set_private_enabled(
        self, user_id: str, enabled: bool, *, operation_id: str
    ) -> OperationResult:
        """开关私有记忆读取；值没变时是 noop，不写文件（§30.2）。

        关闭读取时一并失效在途的自动提取令牌（修复计划 §2.3 第 4 条）：`/memory off` 的第一步
        走这里，即便文件的 `private_enabled` 已经是 False（noop 不写文件），失效处理也必须执行。
        """
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_private_setting(
                document, operation_id, "private_enabled", enabled
            ),
            invalidate_capture=not enabled,
        )

    async def set_auto_capture(
        self, user_id: str, enabled: bool, *, operation_id: str
    ) -> OperationResult:
        """开关自动提取；值没变时是 noop，不写文件（§30.2）。

        关闭自动提取时一并失效在途令牌（修复计划 §2.3 第 4 条）：即便值本来就是 False
        （noop 不写文件），失效处理也必须执行 —— 不能只靠文档 revision 是否增加。
        """
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_private_setting(
                document, operation_id, "auto_capture", enabled
            ),
            invalidate_capture=not enabled,
        )

    async def apply_private_proposal(
        self, user_id: str, proposal: MemoryProposal, *, operation_id: str
    ) -> OperationResult:
        """把撰写器的提案落到该用户的私有文件（规划 §4.3 的写入路径）。

        会改动**已有条目**的两条路径（`update` 与同 key 的 `add` 替换）先过 §42.6 的公开保护：
        目标已经在公开投影里时返回 `public_conflict`，两个文件都不写。
        """
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_guarded_proposal(
                document, user_id, proposal, operation_id
            ),
            screen=lambda _document: (proposal.key, proposal.content),
        )

    async def delete_private(
        self, user_id: str, memory_id: str, *, operation_id: str
    ) -> OperationResult:
        """删除该用户的一条私有条目；ID 前缀与所在作用域在入口再次校验。

        删除同样失效在途的自动提取令牌（修复计划 §2.3 第 7 条）：否则一次基于删除前快照的
        迟到提取会在新基线上看到目标 key 已不存在，把被删内容当成新增重新写回。
        """
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_private_delete(document, memory_id, operation_id),
            invalidate_capture=True,
        )

    async def clear_private(self, user_id: str, *, operation_id: str) -> OperationResult:
        """清空该用户的私有条目，但保留幂等元数据与设置，因此重放旧命令不会再次执行（D-59）。

        清空一并失效在途令牌（修复计划 §2.3 第 4 条）：空记忆上的 clear 是「值未变」的路径，
        失效处理放在 mutation 入口，不依赖文档 revision 是否增加。
        """
        return await self._mutate_private(
            user_id,
            operation_id,
            lambda document: self._apply_private_clear(document, operation_id),
            invalidate_capture=True,
        )

    # --- 自动提取的授权令牌（修复计划 §2.3） --------------------------------

    async def begin_auto_capture(self, user_id: str) -> AutoCaptureToken | None:
        """开始一次自动提取：在同一把写锁内一并取授权、条目快照与该用户的提取代次。

        返回 None 表示当时不满足授权（服务关闭、用户 ID 不可用、私有快照不可用，或用户没有
        开启自动提取）—— 调用方据此静默跳过，不调用撰写器。取得令牌之后，模型调用发生在写锁
        **之外**，因此隐私命令无需等待模型返回（修复计划 §2.3 第 2 条）。
        """
        if not self._enabled or not user_id or _storage_key(user_id) is None:
            return None
        async with self._write_lock:
            state = self._user_state(user_id)
            if not isinstance(state.document, PrivateDocument):
                return None
            if not bool(state.document.auto_capture):
                # 用户没有开启自动提取（或文件被人手改过）：不发放令牌（§6.1 的用户授权）。
                return None
            return AutoCaptureToken(
                user_id=user_id,
                generation=self._capture_generation.get(user_id, 0),
                epoch=self._capture_epoch,
                entries=tuple(state.document.entries),
            )

    async def commit_auto_capture(
        self, token: AutoCaptureToken, proposal: MemoryProposal, *, operation_id: str
    ) -> OperationResult:
        """提交一次自动提取：在写锁内复核令牌仍然有效，再应用提案。

        令牌失效（提取代次被隐私操作推进、服务纪元变化、授权被关闭或不可确认）时返回稳定的
        `noop`：**不写入**，也不产生「记忆已保存」披露。授权规则的唯一实现留在本层，
        控制器不再自己重写一套（修复计划 §2.3 第 3 条）。

        令牌随 applier 一起传入（`capture_token=token`），因此授权会被检查**两次**，各管一段：
        `_mutate_private` 入口那次用 TTL 缓存快照，跑在 `screen` 之前，保住「令牌失效 → 稳定
        noop」的次序并免去注定 noop 的写入流程；applier 里那次在**每一份真正成为写入前提的
        基线**上重判——`_write_document` 接手外部版本后会用外部基线重放 applier，入口那次缓存
        快照看不见这份基线（例如用户在 TTL 窗口内直接改盘关掉了 `auto_capture`）。
        """
        return await self._mutate_private(
            token.user_id,
            operation_id,
            lambda document: self._apply_guarded_proposal(
                document, token.user_id, proposal, operation_id, capture_token=token
            ),
            screen=lambda _document: (proposal.key, proposal.content),
            capture_token=token,
        )

    # --- mutation：公开投影 -------------------------------------------------

    async def publish_private(
        self,
        user_id: str,
        username: str,
        memory_id: str,
        *,
        operation_id: str,
    ) -> OperationResult:
        """把一条私有条目**复制**成公开快照（§42.4 的固定顺序；全程不调用 `MemoryWriter`）。

        顺序：入口先校验 username（R8）→ 公开文档的幂等键 → 私人快照定位 UM-ID → 密钥筛 →
        以公开文档当前版本为基线原子添加。私人文件只读不写，公开文件只在成功时被替换。

        两处判定与 §42.4 的步骤编号有意不同，理由都写在代码里：

        - **username 校验放在最前**：R8 明说「`publish_private` 入口先…校验 username」，
          `invalid_proposal` 因此优先于 `not_found`；这条错误回的是「拿不到你的有效身份」，
          与被点名的条目存不存在无关。
        - **同 ID 的 noop / conflict 先于容量判定**：§3.2 要求「重复执行 `public` 对同一条目是
          幂等的」，而容量只该拦住**新增**。反过来（先判 full）会让一个已满员用户在重放同一条
          命令时收到「公开条目已达上限」，既不是幂等，也说不到点子上；R3 的 conflict 更会被
          一句 full 盖掉，用户看不出真正的原因。
        """
        if not self._enabled or not user_id:
            return OperationResult(STATUS_FORBIDDEN, None, 0)
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return OperationResult(STATUS_FORBIDDEN, None, 0)
        if not _valid_username(username):
            # 第一层身份校验（R8）：codec 的 `_check_public` 是第二层，两层都不接受任意文本。
            return OperationResult(STATUS_INVALID_PROPOSAL, None, 0)
        async with self._write_lock:
            # R24：以磁盘上这一刻的公开文档为基线（幂等键与条目判重都读它），不走 TTL 缓存。
            state = self._public_state(owner_key, force=True)
            if not isinstance(state.document, PublicMemoryDocument):
                return OperationResult(STATUS_UNAVAILABLE, None, state.revision)
            hit = state.document.operations.get(operation_id)
            if hit is not None:
                # 幂等命中：返回第一次的稳定结果，不再读私人文件，也不重写公开文件（§42.4 第 2 步）。
                return hit
            private = self._user_state(user_id, force=True)
            if not isinstance(private.document, PrivateDocument):
                # 私人文件读不回来：定位不了来源条目，保守拒绝（§42.8）。
                return OperationResult(STATUS_UNAVAILABLE, None, private.revision)
            target = _find_by_memory_id(list(private.document.entries), memory_id, PREFIX_USER)
            if target is None:
                return OperationResult(STATUS_NOT_FOUND, None, private.revision)
            if self._contains_secret((target.key, target.content)):
                # 第二道密钥筛（§42.4 第 5 步）：人工编辑过的私人 Markdown 也绕不过去。
                return OperationResult(STATUS_SECRET_DETECTED, None, state.revision)
            status, object_id, written = self._write_document(
                self._public_view(owner_key),
                state,
                lambda document: self._apply_publish(document, target, username, operation_id),
            )
            if written is not state:
                self._store_public(owner_key, written)
                # §42.4 第 10 步：成功（含接手了外部版本）之后更新内存索引。
                self._reindex_owner(owner_key, written.document)
            if status == STATUS_OK:
                self._log_updated(
                    "memory.updated",
                    scope="public",
                    revision=written.revision,
                    memory_id=object_id,
                )
            return OperationResult(status=status, object_id=object_id, revision=written.revision)

    async def unpublish_private(
        self, user_id: str, memory_id: str, *, operation_id: str
    ) -> OperationResult:
        """撤回一条公开条目（§42.5）：只改公开文档，**不改私人来源**。

        撤到零条之后保留一个合法的空公开文档（R9）：它不进索引、不影响模型可见行为，但保住
        `operations` 的跨重启幂等。**不做**「尽力删除」，也不回显已经撤下的正文（文案由 Task 4 决定）。
        """
        return await self._mutate_public(
            user_id,
            operation_id,
            lambda document: self._apply_unpublish(document, memory_id, operation_id),
        )

    async def unpublish_all(self, user_id: str, *, operation_id: str) -> OperationResult:
        """撤回该 owner 的全部公开条目并清索引（§42.5，`/memory clear` 的撤回步）。

        本来就一条都没有时是 `noop`：不写文件（不给从未发布过的用户凭空造一份空公开文档），
        对调用方则同样是「撤回步已经到位」——`/memory clear` 因此照常继续删私人条目。
        """
        return await self._mutate_public(
            user_id,
            operation_id,
            lambda document: self._apply_unpublish_all(document, operation_id),
        )

    async def _mutate_public(
        self,
        user_id: str,
        operation_id: str,
        apply_fn: _ApplyFn,
    ) -> OperationResult:
        """公开文件的 mutation 骨架：门禁 → 幂等 → 原子写（§42.2）。

        与 `_mutate_private` 同款，只是把「用户文件」换成「公开文件」：同一个 `asyncio.Lock`
        （**不新建第二把锁**），同一套 `_write_document` 七步原子写与同一套快照替换。
        """
        if not self._enabled or not user_id:
            return OperationResult(STATUS_FORBIDDEN, None, 0)
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return OperationResult(STATUS_FORBIDDEN, None, 0)
        async with self._write_lock:
            # R24：销毁路径强制读盘。撤回步的 `not_found` 是 R19 授权删除私人来源的前提，必须
            # 意味着「读过公开文件，里面确实没有这条」；TTL 缓存会把它退化成「我没看见」，
            # 于是 TTL 窗口内落到盘上的公开条目会被漏撤，变成「私人删掉、公开遗留」。
            state = self._public_state(owner_key, force=True)
            if not isinstance(state.document, PublicMemoryDocument):
                return OperationResult(STATUS_UNAVAILABLE, None, state.revision)
            hit = state.document.operations.get(operation_id)
            if hit is not None:
                return hit
            status, object_id, written = self._write_document(
                self._public_view(owner_key), state, apply_fn
            )
            if written is not state:
                self._store_public(owner_key, written)
                self._reindex_owner(owner_key, written.document)
            if status == STATUS_OK:
                self._log_updated(
                    "memory.updated",
                    scope="public",
                    revision=written.revision,
                    memory_id=object_id,
                )
            return OperationResult(status=status, object_id=object_id, revision=written.revision)

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
        capture_token: AutoCaptureToken | None = None,
        invalidate_capture: bool = False,
    ) -> OperationResult:
        """私有文件的 mutation 骨架：门禁 → 幂等 → 密钥筛 → 原子写（§30.2）。

        `screen` 拿到基线文档、返回将要写进 Markdown 的文本；筛选发生在 render 与写入之前。

        两个自动提取相关的开关都在写锁之内生效（修复计划 §2.3）：`invalidate_capture` 供隐私
        操作推进提取代次；`capture_token` 供自动提取提交时复核令牌。二者与 `begin_auto_capture`
        共用同一把锁，因此「开始提取」与「隐私操作生效」之间没有可观察的中间态。

        `capture_token` 在这里检查是**第一处**，用的是本入口拿到的（TTL 缓存）快照，且必须留在
        `screen` 之前：守住「令牌失效 → 稳定 noop」优先于「密钥命中 → secret_detected」的既有
        次序（§2.3 的强制顺序），也免去为一次注定 noop 的提交跑完 `_write_document` 的整套机制。
        **第二处**在 applier（`_apply_guarded_proposal`，`commit_auto_capture` 会把令牌传下去）：
        `_write_document` 接手外部版本后会用外部基线重放 applier，那份基线在缓存之外，必须在
        真正要写的那一份上重新确认授权。两处不是重复，各覆盖一段。
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
            if invalidate_capture:
                # 隐私操作与令牌失效用同一把锁（修复计划 §2.3 第 4 条）：放在幂等命中之后、应用
                # 之前，因此「值本来就是 noop」的路径（空记忆 clear、auto 本来为 False）同样会
                # 失效在途令牌，不依赖文档 revision 是否增加。
                self._invalidate_auto_capture(user_id)
            if capture_token is not None and not self._capture_token_valid(
                capture_token, state.document
            ):
                # 令牌已失效：稳定 no-op，不写入、不产生披露（修复计划 §2.3 第 3 条）。
                return OperationResult(status=STATUS_NOOP, object_id=None, revision=state.revision)
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

    def _invalidate_auto_capture(self, user_id: str) -> None:
        """推进该用户的提取代次，使此前取得的所有自动提取令牌失效。

        必须在写锁内调用（`_mutate_private` 已经持锁）：它与 `begin_auto_capture` /
        `commit_auto_capture` 共用同一把锁，这正是「同一把锁内复核」的实现（修复计划 §2.3）。
        """
        self._capture_generation[user_id] = self._capture_generation.get(user_id, 0) + 1

    def _capture_token_valid(self, token: AutoCaptureToken, document: PrivateDocument) -> bool:
        """令牌此刻是否仍可提交：服务纪元、授权开关与提取代次三者都要成立。"""
        if token.epoch != self._capture_epoch or not self._enabled:
            # 服务已停止（或本实例已被替换过）：停止前发出的令牌一律不再被接受。
            return False
        if not bool(document.auto_capture):
            # 授权被关闭（或文件被外部改成关闭）：重新校验授权，而不是只信令牌。
            return False
        return self._capture_generation.get(token.user_id, 0) == token.generation

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
        if isinstance(document, PublicMemoryDocument):
            return render_public(document)
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

    def _public_view(self, owner_key: str) -> _DocumentView:
        """公开文件的视图：与用户文件同款，只是换成公开解析器与 `public/` 下的路径（§42.2）。"""
        return _DocumentView(
            path=self.public_path_from_owner_key(owner_key),
            parser=parse_public,
            fresh=PublicMemoryDocument,
            scope="public",
        )

    def _common_state(self) -> _Snapshot:
        """共同快照：惰性加载一次；之后的刷新由 `start()` 起的任务负责（§30.4）。"""
        if self._common is None:
            self._common = self._inspect(
                self._common_view(), None, ttl=None, event="memory.load_failed", force=True
            )
        return self._common

    def _user_state(self, user_id: str, *, force: bool = False) -> _Snapshot:
        """用户快照：惰性加载 + 有界 LRU；TTL 到期后的下一次访问检查摘要（§30.4）。

        `force=True` 绕开 TTL 直接读盘：发布要用**这一刻**的私有条目做快照（§3.2 的「当时的
        私有条目」），人工改过的 Markdown 因此不会躲在 TTL 后面绕过密钥筛（§42.4 第 5 步）。
        """
        state = self._users.get(user_id)
        fresh = self._inspect(
            self._private_view(user_id),
            state,
            ttl=self._refresh_seconds,
            event="memory.refresh_failed" if state is not None else "memory.load_failed",
            force=force,
        )
        self._store_user(user_id, fresh)
        return fresh

    def _store_user(self, user_id: str, state: _Snapshot) -> None:
        """写入缓存并维持 LRU 上界；淘汰只释放内存，**不删文件**（§30.4）。"""
        self._users[user_id] = state
        self._users.move_to_end(user_id)
        while len(self._users) > _USER_CACHE_SIZE:
            self._users.popitem(last=False)

    def _public_state(self, owner_key: str, *, force: bool = False) -> _Snapshot:
        """公开快照：与用户私有文件**同一套**惰性加载 + 有界 LRU + TTL 摘要比对（§42.2）。

        **写入与销毁路径必须 `force=True`**（R24）：`publish_private` / `unpublish_private` /
        `unpublish_all` 与 §42.6 的公开保护都要以**磁盘上这一刻**的公开文档为基线。走 TTL 缓存会
        把「TTL 窗口内落到盘上的条目」变成看不见的东西，而 `unpublish_private` 的 `not_found`
        是 R19 授权删除私人来源的前提——它必须意味着「我读了公开文件，里面没有这条」，不能退化成
        「我碰巧没看见」。只读路径（`public_entries` / `public_context_for` / `find_operation`）
        保持缓存，读者不为此付代价；username 索引的整份重扫（`_refresh_public`）不走读者那一格，
        它自己也 `force=True` —— 它是**填**这份缓存的那条路径，不是它的读者。
        """
        state = self._public.get(owner_key)
        fresh = self._inspect(
            self._public_view(owner_key),
            state,
            ttl=self._refresh_seconds,
            event="memory.refresh_failed" if state is not None else "memory.load_failed",
            force=force,
        )
        self._store_public(owner_key, fresh)
        return fresh

    def _store_public(self, owner_key: str, state: _Snapshot) -> None:
        """公开快照的 LRU：与用户快照同款，淘汰只释放内存（§42.2）。"""
        self._public[owner_key] = state
        self._public.move_to_end(owner_key)
        while len(self._public) > _PUBLIC_CACHE_SIZE:
            self._public.popitem(last=False)

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

    def _apply_guarded_proposal(
        self,
        document: PrivateDocument,
        user_id: str,
        proposal: MemoryProposal,
        operation_id: str,
        *,
        capture_token: AutoCaptureToken | None = None,
    ) -> tuple[PrivateDocument | None, str, str | None]:
        """§42.6 的公开保护 + 自动提取的授权复查 + 既有提案逻辑。

        两道门都放在 applier 里而不是 `_mutate_private` 的入口：applier 会被 `_write_document`
        用两次——一次以内存快照为基线、一次以接手的**外部版本**为基线（§5.5 第 4 步）——
        所以「同 key 的 add 到底替不替换」与「授权此刻是否仍然成立」这两个问题都会在每一份
        真正成为前提的基线上重新判一次。

        `capture_token` 只有自动提取的提交路径会传（`apply_private_proposal` 是用户命令触发的
        路径，本就不该受自动提取令牌约束，因此不传）。入口那次检查（`_mutate_private`）与这次
        不是重复：入口那次跑在 `screen` 之前，保住「令牌失效 → 稳定 noop」优先于「密钥命中 →
        secret_detected」的既有次序（§2.3 的强制顺序），也避免为一次注定 noop 的提交走完
        `_write_document` 的机制；这里这次覆盖的则是入口检查够不到的「基线被接手」情形——
        入口用的是 TTL 缓存快照，而真正写盘的前提是接手后的外部版本。
        """
        if capture_token is not None and not self._capture_token_valid(capture_token, document):
            # 外部版本接手后基线可能已经不同：授权必须对**真正要写的**那一份基线重新确认。
            # 令牌失效时保持稳定 no-op：不写盘、不产生「记忆已保存」披露（§2.3）。
            return None, STATUS_NOOP, None
        blocked = self._public_guard(document, user_id, proposal)
        if blocked is not None:
            return None, blocked.status, None
        return self._apply_private_proposal(document, proposal, operation_id)

    def _public_guard(
        self, document: PrivateDocument, user_id: str, proposal: MemoryProposal
    ) -> OperationResult | None:
        """目标 UM-ID 是否已经在公开投影里（§42.6）：命中返回拒绝结果，否则 None。

        | 情况 | 返回 |
        |------|------|
        | 目标已公开 | `public_conflict`（私人与公开文件都不写） |
        | 公开状态无法确认 | `unavailable`（保守拒绝） |
        | 目标未公开 | None（沿用现有更新逻辑） |

        只有 `update` 与「同 key 的 `add` 替换」会走到查表：`add` 的新 key 不可能已经在公开投影里
        （§12.5），`noop` 什么都不改，删除与清空只由用户命令触发、不经这里（§52）。

        判据是「这次调用会不会改动**已有条目**」：私人文件里没有这条目标时就什么都不改，
        因此不查公开投影、由既有逻辑回 `not_found`（§42.6 的原话就是这个范围）。

        读公开基线时**强制读盘**（R24）：这道门是唯一拦住「AI 静默改写已公开条目」的地方，
        它的答案必须来自磁盘上这一刻的公开文档；TTL 窗口内落到盘上的条目（人工编辑、从备份恢复）
        用缓存看不见，那道门就会在用户以为内容还公开着的时候放行一次改写。
        """
        if proposal.action == ProposalAction.UPDATE:
            target = _find_by_memory_id(list(document.entries), proposal.target_id, PREFIX_USER)
            if target is None:
                return None
            target_id = target.memory_id
        elif proposal.action == ProposalAction.ADD:
            existing = _find_by_key(list(document.entries), proposal.key, None)
            if existing is None:
                return None  # 新增条目：新 ID 不可能已经被公开
            target_id = existing.memory_id
        else:
            return None
        if _canonical(target_id, PREFIX_USER) is None:
            # 目标形状不是 UM-ID：形状校验交给 `_apply_private_proposal`（落 invalid_proposal），
            # 这里没有可查的公开条目。
            return None
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return None
        state = self._public_state(owner_key, force=True)
        if not state.available or not isinstance(state.document, PublicMemoryDocument):
            # 公开状态无法确认：不给「也许它没公开」留任何猜测空间。
            return OperationResult(STATUS_UNAVAILABLE, None, state.revision)
        if _find_public_entry(state.document.entries, target_id) is not None:
            return OperationResult(STATUS_PUBLIC_CONFLICT, None, state.revision)
        return None

    # --- mutation：公开投影的纯函数实现 -------------------------------------

    def _apply_publish(
        self,
        document: PublicMemoryDocument,
        source: MemoryEntry,
        username: str,
        operation_id: str,
    ) -> tuple[PublicMemoryDocument | None, str, str | None]:
        """把一条私有条目加成公开快照（§42.4 第 8、9 步）。

        R3：同 ID 已存在且 key 与正文**完全一致** → `noop`（幂等）；不一致 → `conflict`——
        公开副本是发布那一刻的显式快照，`.md` 不是动态引用，绝不静默覆盖用户批准过的旧版本
        （§3.2）。判定先于容量：`noop` / `conflict` 都不新增条目，容量只该拦住新增。
        """
        published = _public_entry_of(source, self._timestamp())
        existing = _find_public_entry(document.entries, published.memory_id)
        if existing is not None:
            if existing.key == published.key and existing.content == published.content:
                return None, STATUS_NOOP, existing.memory_id
            return None, STATUS_CONFLICT, None
        if len(document.entries) >= self._config.max_public_entries_per_user:
            return None, STATUS_FULL, None
        revision = document.revision + 1
        entries = tuple(document.entries) + (published,)
        return (
            replace(
                document,
                revision=revision,
                owner_username=username,
                entries=entries,
                operations=self._record(
                    document.operations, operation_id, STATUS_OK, published.memory_id, revision
                ),
            ),
            STATUS_OK,
            published.memory_id,
        )

    def _apply_unpublish(
        self, document: PublicMemoryDocument, memory_id: str, operation_id: str
    ) -> tuple[PublicMemoryDocument | None, str, str | None]:
        """撤回一条公开条目；前缀不对或不存在都落 `not_found`（§42.5）。

        撤到零条时**保留**这个文件（R9）：它仍然是幂等元数据的载体，只是不进索引。
        """
        target = _find_public_entry(document.entries, memory_id)
        if target is None:
            return None, STATUS_NOT_FOUND, None
        kept = tuple(
            entry
            for entry in document.entries
            if _canonical(entry.memory_id, PREFIX_USER) != _canonical(target.memory_id, PREFIX_USER)
        )
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

    def _apply_unpublish_all(
        self, document: PublicMemoryDocument, operation_id: str
    ) -> tuple[PublicMemoryDocument | None, str, str | None]:
        """撤回全部公开条目（§42.5、`/memory clear` 的撤回步）。

        本来就是空的 → `noop` 且**不写文件**：不给从未发布过的用户凭空造一份空公开文档，
        对调用方则同样是「撤回步已经到位」（`_step_failure` 的既有口径把 `noop` 当成功）。
        """
        if not document.entries:
            return None, STATUS_NOOP, None
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


def _canonical_entry(entry: _EntryLike, prefix: str) -> _EntryLike:
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
    if isinstance(document, PublicMemoryDocument):
        # 公开条目的 ID 沿用来源的 `UM-` 序号，因此与私有条目同一套收敛规则（§41.3）。
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


def _valid_owner_key(value: object) -> bool:
    """owner key 是否是 `user_storage_key` 的形态：64 位小写十六进制（§10.1、D-103）。

    这是**路径安全**的第一道闸：非法取值（`../`、绝对路径、任意文本）都要落进兜底路径，
    绝不能被拼进 `public/` 下面的文件名。
    """
    if not isinstance(value, str) or len(value) != _OWNER_KEY_LENGTH:
        return False
    return all(char in _OWNER_KEY_CHARS for char in value)


def _invalid_owner_key_path_key(owner_key: object) -> str:
    """形状非法的 owner key 的兜底文件名：同形、稳定、不含原始取值。

    与 `_invalid_id_path_key` 同款但用**另一个**域前缀，因此与任何合法 owner key 的文件名都不会
    互相覆盖；`surrogatepass` 让这次编码永远成立（孤立代理项也编码得出来），读路径因此永不抛出。
    """
    text = owner_key if isinstance(owner_key, str) else repr(owner_key)
    raw = f"raricy-memory-invalid-owner-key\0{text}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def _valid_username(username: object) -> bool:
    """username 是否满足站点用户名合同（§40.2 第 2 条、R8 的第一层）。

    复用 codec 的渲染校验而不是再写一份判定：`_check_username` 是这份规则唯一的实现，
    服务侧复制一遍就会多出一处会漂移的地方（公开设计 §6.2 的宽松描述就是这么来的）。
    空文档 + 这个 username 渲染得出来，就说明 username 本身合法。
    """
    if not isinstance(username, str) or not username:
        return False
    try:
        render_public(PublicMemoryDocument(owner_username=username))
    except CodecError:
        return False
    return True


def _valid_subject(subject: object) -> bool:
    """subject 的形状复查（§42.1）：owner key、username 与优先级都必须是合同里的形状。

    形状不对的 subject 直接跳过——`PublicMemorySubject` 自己不做任何校验（§40.2 的两条规则
    由宿主保证），服务这一层因此独立复查一遍，绝不把「调用方已经筛过」当成前提。
    """
    if not isinstance(subject, PublicMemorySubject):
        return False
    if not _valid_owner_key(subject.owner_key) or not _valid_username(subject.username):
        return False
    return isinstance(subject.source_priority, int) and not isinstance(
        subject.source_priority, bool
    )


def _public_entry_of(source: MemoryEntry, published_at: str) -> PublicMemoryEntry:
    """把一条私有条目复制成公开快照（§3.2、§12.1）：来源之后的变化不反映到这里。"""
    return PublicMemoryEntry(
        memory_id=source.memory_id,
        key=source.key,
        content=source.content,
        pinned=source.pinned,
        source_created_at=source.created_at,
        source_updated_at=source.updated_at,
        published_at=published_at,
    )


def _find_public_entry(
    entries: tuple[PublicMemoryEntry, ...], memory_id: str | None
) -> PublicMemoryEntry | None:
    """按归一化后的 `UM-` ID 找公开条目：人工改成 `UM-6` 的文件仍能被 `UM-000006` 命中（§41.3）。"""
    wanted = _canonical(memory_id, PREFIX_USER)
    if wanted is None:
        return None
    for entry in entries:
        if _canonical(entry.memory_id, PREFIX_USER) == wanted:
            return entry
    return None


def _ordered_public_entries(
    entries: tuple[PublicMemoryEntry, ...],
) -> tuple[PublicMemoryEntry, ...]:
    """同一 owner 内的次序（§42.7、设计 §7.3 第 5 条）：pinned 在前，再按发布时间、来源更新时间新到旧。

    两个时间都参与（合同写的是「`published_at` **或** `source_updated_at` 新到旧」）：发布时间
    是主要判据，来源更新时间只做同刻的次级判据，因此两种读法给出的前半段一致。
    """
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0 if entry.pinned else 1,
                _descending_stamp(entry.published_at),
                _descending_stamp(entry.source_updated_at),
            ),
        )
    )


def _reindex(
    index: Mapping[str, tuple[str, ...]],
    owner_key: str,
    document: PublicMemoryDocument | None,
) -> dict[str, tuple[str, ...]]:
    """给出该 owner 在索引里的新位置：**整份重建**，一次引用替换（§42.2、§42.3）。

    只有「文档可用且至少一条有效条目」才在索引里（R9 的空公开文档不入索引）。重建而不是就地改，
    是为了让 `public_username_index()` 的读者永远看到完整的一份；桶内次序保持既有相对次序，
    新用户名追加在末尾。
    """
    updated: dict[str, list[str]] = {
        name: [key for key in keys if key != owner_key] for name, keys in index.items()
    }
    if document is not None and document.entries and document.owner_username:
        updated.setdefault(document.owner_username, []).append(owner_key)
    return {name: tuple(keys) for name, keys in updated.items() if keys}


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
