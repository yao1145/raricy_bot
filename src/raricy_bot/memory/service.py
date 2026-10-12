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

from .. import texts

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
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

# 画像的模型与 codec：**惰性加载**（首次用到时导入一次，之后走缓存）。
#
# 为什么不能写成本模块顶部的 `from ..persona import ...`：`raricy_bot.memory` 这个包在导入
# `memory.service` 之前会先导入 `memory.controller`，而 `persona.codec` 需要 `memory.codec`
# （导入它又会先执行 `memory/__init__`）。任一侧先被导入时，另一侧都会读到「还没执行完的包」，
# 于是出现「名字尚未绑定」的导入期错误 —— 与运行时无关，纯粹是导入顺序的偶然性。
# 把这条边推迟到调用时刻，两个方向就都成立：先导入 persona 或先导入 memory 都能正常工作。
_PERSONA_LAYER: tuple[Any, Any] | None = None


def _persona_layer() -> tuple[Any, Any]:
    """返回（`persona.models`, `persona.codec`）；首次调用时导入并缓存。"""
    global _PERSONA_LAYER
    if _PERSONA_LAYER is None:
        from ..persona import codec as persona_codec
        from ..persona import models as persona_models

        _PERSONA_LAYER = (persona_models, persona_codec)
    return _PERSONA_LAYER


class _LazyPersonaModule:
    """惰性模块代理：第一次取属性时才真正导入画像的模型 / codec 模块。

    模块级 `__getattr__`（PEP 562）只覆盖 `module.attr` 形式的访问，**管不到函数体里的全局
    名字查找**，所以这里放一个导入期就存在的代理对象：`persona_models.validate_persona(...)`
    在第一次执行到那一行时才触发导入（见 `_persona_layer` 的说明），之后直接转发给真模块。
    """

    __slots__ = ("_name", "_module")

    def __init__(self, name: str) -> None:
        self._name = name
        self._module: Any = None

    def __getattr__(self, name: str) -> Any:
        if self._module is None:
            persona_models, persona_codec = _persona_layer()
            self._module = persona_models if self._name == "models" else persona_codec
        return getattr(self._module, name)


# 画像相关方法体里直接使用的两个名字；导入期不加载任何 persona 代码。
persona_models = _LazyPersonaModule("models")
persona_codec = _LazyPersonaModule("codec")

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

# 三类文件的解析结果。画像的三类文档是纯 dict（契约要求接口返回 dict），因此并入同一个别名。
_MemoryDocument = CommonDocument | PrivateDocument | PublicMemoryDocument | dict[str, Any]

# 纯函数形式的操作：给定基线文档，返回（新文档, 稳定状态, 对象 ID）；状态非 ok 时新文档为 None。
_ApplyFn = Callable[[Any], "tuple[Any | None, str, str | None]"]

# 密钥筛的取材函数：给定基线文档，返回本次将要写进 Markdown 的文本（正文与 key）。
_ScreenFn = Callable[[Any], "tuple[str, ...]"]

# 私有条目与公开条目共享的「带 memory_id 的条目」形状：ID 收敛对两者是同一套规则（§41.3）。
_EntryLike = TypeVar("_EntryLike", MemoryEntry, PublicMemoryEntry)

# owner key 的形状：`user_storage_key` 的输出，64 位小写十六进制（§10.1）。
_OWNER_KEY_LENGTH: int = 64
_OWNER_KEY_CHARS = frozenset("0123456789abcdef")

# --- 用户画像（设计 §8）的常量 ---

# 工作流默认有效期（24 小时）：与 `PersonaConfig.workflow_ttl_seconds` 的默认值一致，
# 显式传参时以传参为准。
_PERSONA_DEFAULT_TTL: float = 86400.0

# 画像快照 LRU 的容量：与用户快照同款，淘汰只释放内存。
_PERSONA_CACHE_SIZE: int = 64

# 画像目录的单次扫描上界（与 `MAX_PUBLIC_FILES` 同一套理由：一个被塞了几万个文件的目录
# 不能拖垮启动或一次读取）。超出的文件这一轮不处理、只记一个稳定 reason。
MAX_PERSONA_FILES: int = 4096

# 启动恢复与关闭撤回共用的幂等键：它们不是用户操作，但同样要在目标文件里留下「做过」的记录，
# 这样一次重试不会把同一步重复算成两次新操作。
_PERSONA_RECOVER_OPERATION: str = "persona-recovery"

# 画像公开条目在补充资料里的组间基数（`_PUBLIC_PRIORITY_BASE` 之后的一档）：公开画像整体
# 晚于既有三组与旧公开个人记忆，DM 里根本不出现。
_PERSONA_PUBLIC_PRIORITY_BASE: int = 3

# 被隔离（suppressed）的 owner 上界：隔离是**进程内**的状态，只影响公开投影的可见性。
# 上界防止一次被滥用的控制路径把内存塞满；淘汰最旧的条目即可 —— 隔离本身会在下一次
# `/persona off`、启动恢复或撤回成功时重新建立（设计 §7 的待撤回语义）。
_PERSONA_SUPPRESSED_MAX: int = 4096

# 人工流程的基线指纹在 JSON 序列化时的两个固定项：`sort_keys` 保证同一份内容只有一种写法。
_FINGERPRINT_SEPARATORS: tuple[str, str] = (",", ":")

# 画像日志的 scope 取值（白名单 token）：三类文件各一个。
_SCOPE_PERSONA: str = "persona_user"
_SCOPE_PERSONA_FLOW: str = "persona_workflow"
_SCOPE_PERSONA_PUBLIC: str = "persona_public"

# `last_change` 的固定短语：`/persona` 用它显示「最近一次变更」。

# `persona_commit_update` 允许由控制器提供的记账字段白名单：越界的键一律整条拒绝。
_PERSONA_METADATA_FIELDS: frozenset[str] = frozenset(
    ("update_day", "update_count", "last_update_at", "last_message_id", "observations", "last_change")
)


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
        """当前快照的修订号；没有可用快照时为 0。

        记忆文档是 dataclass（`document.revision`），画像文档是纯 dict（`document["revision"]`）；
        两者的修订号语义相同，因此在这里统一读出。
        """
        document = self.document
        if document is None:
            return 0
        if isinstance(document, dict):
            return int(document.get("revision", 0))
        return int(document.revision)


@dataclass(frozen=True)
class _DocumentView:
    """把「共同文件」与「用户文件」的差异收在一处，供刷新与原子写复用。

    `render` 只在画像的三类文档上用得到：那三类是纯 dict，`self._render` 的 `isinstance`
    分派认不出它们属于哪一类，因此由各自的 view 自带渲染器；记忆三类文档继续走 `_render`。
    """

    path: str
    parser: Callable[[bytes, MemoryConfig], Any]
    fresh: Callable[[], Any]
    scope: str
    render: Callable[[Any], bytes] | None = None


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
        # 构造签名与 INTERFACES §30.1 逐字一致（`test_memory_service.py` 有一条契约测试钉住它），
        # 因此画像的配置**不**从这里进：待办流程的有效期由 `start_persona(workflow_ttl_seconds=…)`
        # 注入，装配层已经这么调（见 `app.py::_start_persona`）。
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

        # --- 用户画像（设计 §8）---
        # 画像的三份文件与记忆共用这一把写锁、这一套原子写与这一套快照机制，但**开关与缓存
        # 各自独立**：画像可以在 `memory.enabled=false` 的部署里单独启用（设计 §12），
        # 因此它有自己的 `_persona_enabled`、自己的纪元与代次、自己的三份快照缓存。
        self._persona_root: str = os.path.join(self._root, "personas")
        self._persona_users_dir: str = os.path.join(self._persona_root, "users")
        self._persona_flows_dir: str = os.path.join(self._persona_root, "workflows")
        self._persona_public_dir: str = os.path.join(self._persona_root, "public")
        self._persona_enabled: bool = False
        self._persona_started: bool = False
        # 工作流有效期：`start_persona(workflow_ttl_seconds=…)` 可覆盖，缺省 24 小时
        # （与 `PersonaConfig.workflow_ttl_seconds` 的默认值一致）。
        self._persona_ttl: float = _PERSONA_DEFAULT_TTL
        self._persona_epoch: int = 0
        self._persona_generation: dict[str, int] = {}
        self._persona_users: "OrderedDict[str, _Snapshot]" = OrderedDict()
        self._persona_flows: "OrderedDict[str, _Snapshot]" = OrderedDict()
        self._persona_public: "OrderedDict[str, _Snapshot]" = OrderedDict()
        # username → owner key(s)：与记忆公开索引同款的一次引用替换（§42.2）。
        self._persona_index: Mapping[str, tuple[str, ...]] = {}
        self._persona_scan_reason: str | None = None
        # 被隔离的 owner（只存不可逆 owner key）：`/persona off` 或 `private` 一受理就立刻生效，
        # 公开投影（画像的**与**旧的）在撤回真正落盘之前不再引用这些 owner。见 `persona_quarantine`。
        self._persona_suppressed: "OrderedDict[str, None]" = OrderedDict()

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
            if self._persona_suppressed_owner(owner_key):
                # 画像侧的「待撤回」在完成之前同样挡住**旧**公开投影：一次 `/persona off`
                # 承诺的是不再引用这个人的公开资料，旧公开副本不能成为例外（设计 §7、§11）。
                continue
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

    def _public_file_keys(self, directory: str | None = None) -> tuple[str, ...] | None:
        """`public/` 下的 owner key 列表（按文件名排序）；目录不可读返回 None。

        `directory` 默认是公开投影目录；画像的三类目录同样按 owner key 命名，因此共用这一个
        扫描助手（画像只借它的「过滤非法文件名 + 数量上界」这两条规则）。

        非法的文件名（不是 `.md`、或 `.md` 的主干不是 64 位小写十六进制的 owner key 形状）直接跳过：
        那不是任何 owner 的公开文件，也就没有可归属的「坏文件」要记。文件数超过 `MAX_PUBLIC_FILES`
        时按排序截断，只记一个稳定 reason（R14）。
        """
        try:
            with os.scandir(directory or self._public_dir) as scan:
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
        """一次 mutation 之后把该 owner 的索引项换成文档的当前事实（§42.4 第 10 步、§42.5）。

        隔离中的 owner 一律按「没有公开投影」处理：隔离跟着画像的待撤回走，比某一次旧公开
        写入更权威（`_refresh_public` 会在下一次整份重扫时给出同样的结论）。
        """
        current = document if isinstance(document, PublicMemoryDocument) else None
        if self._persona_suppressed_owner(owner_key):
            current = None
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
            if (
                channel_kind == _DM
                and user_id
                and access.permits_private(user_id, channel_kind)
                and not self._legacy_private_blocked(user_id)
            ):
                # 私有记忆只在 DM 且门禁通过时读取；其它频道连文件都不读（§30.2 的表）。
                # 已迁移用户也不读：旧个人条目不得与新画像重复参与推断（设计 §11.5）。
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
        """该用户的私有设置；文件不存在或不可用都按「未开启」返回，不创建任何文件。

        已迁移用户同样按「未开启」返回：旧个人记忆对该用户已经退役（设计 §11.5）。
        """
        if not self._enabled or not user_id or self._legacy_private_blocked(user_id):
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
        **已迁移**这一点在这里只能靠内存里的画像快照判断（`_persona_cached_migrated`）：这是提示，
        不是判据 —— 权威判据是异步的 `persona_is_migrated`（它会真的加载私有状态），命令路由
        用后者。缓存被淘汰时这里返回文件里的原值，绝不会因此放行旧写入：写入路径有
        `_mutate_private` 的迁移检查兜底。
        """
        if not user_id:
            return None
        if self._persona_cached_migrated(user_id):
            return PrivateSettings(private_enabled=False, auto_capture=False)
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
        """该用户已生效的私有条目；不可用或已迁移时返回空元组。"""
        if not self._enabled or not user_id or self._legacy_private_blocked(user_id):
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
            if self._persona_suppressed_owner(subject.owner_key):
                # 画像侧已受理但未完成的撤回同样挡住旧公开条目：这是**内存集合**的判定，
                # 公开路由因此永远不会去读私有文件（设计 §7、Global Constraints 第 7 条）。
                self._log_omitted("public", "suppressed")
                continue
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

    def _public_dir_readable(self, directory: str | None = None) -> bool:
        """`public/` 目录本身能不能列：不能就是「本轮无公开个人记忆」（§42.8）。

        目录**不存在**是正常路径而不是失败：那时一个公开文件也没有，逐 owner 读到的同样是空文档，
        结果一样是空的（也不记日志）。只有「存在但读不动」才降级成本轮无结果。
        """
        try:
            with os.scandir(directory or self._public_dir):
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

        返回 None 表示当时不满足授权（服务关闭、用户 ID 不可用、私有快照不可用、用户已迁移到
        画像，或用户没有开启自动提取）—— 调用方据此静默跳过，不调用撰写器。取得令牌之后，模型
        调用发生在写锁**之外**，因此隐私命令无需等待模型返回（修复计划 §2.3 第 2 条）。
        """
        if not self._enabled or not user_id or _storage_key(user_id) is None:
            return None
        if self._legacy_private_blocked(user_id):
            # 已迁移用户不再跑旧自动提取：证据只从画像路径来（设计 §11.5）。
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
        if self._legacy_private_blocked(user_id):
            # 已迁移用户不再发布**旧**个人条目：公开一律走画像的独立快照（设计 §11.7）。
            # 撤回路径（`unpublish_private` / `unpublish_all`）不设这道门：迁移本身要把旧公开
            # 副本撤下来，撤回永远是被允许的方向。
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

        `asyncio.Lock` 不可重入，因此真正的实现在 `_mutate_public_locked`：画像的
        `persona_confirm` / `persona_off` 已经持有写锁，需要在这段临界区里顺带撤回旧公开记忆
        （逐用户迁移与关闭的固定顺序），调用的是同一个锁**之内**的那份实现，而不是再进一次
        锁 —— 那会立刻自锁。
        """
        if not self._enabled or not user_id:
            return OperationResult(STATUS_FORBIDDEN, None, 0)
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return OperationResult(STATUS_FORBIDDEN, None, 0)
        async with self._write_lock:
            return self._mutate_public_locked(owner_key, operation_id, apply_fn)

    def _mutate_public_locked(
        self,
        owner_key: str,
        operation_id: str,
        apply_fn: _ApplyFn,
    ) -> OperationResult:
        """公开文件的 mutation 主体；**调用方必须已经持有写锁**（见 `_mutate_public`）。"""
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
        if self._legacy_private_blocked(user_id):
            # 已迁移用户：旧个人记忆的**全部**写入路径在这里统一关闭（设计 §11.2、§11.6）。
            # 收在一个入口是纵深防御，不是重复检查：任何一条私有 mutation（含将来新增的）
            # 都不可能绕过它，`forbidden` 也如实说明了「不是暂时不可用，而是这条路已经退役」。
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
        """令牌此刻是否仍可提交：服务纪元、授权开关、提取代次与迁移状态都要成立。"""
        if token.epoch != self._capture_epoch or not self._enabled:
            # 服务已停止（或本实例已被替换过）：停止前发出的令牌一律不再被接受。
            return False
        if self._legacy_private_blocked(token.user_id):
            # 用户在模型调用期间完成了迁移：这次基于旧基线的提取结果作废（设计 §11.5）。
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

    # --- 用户画像：生命周期（设计 §8、§12） --------------------------------

    @property
    def persona_available(self) -> bool:
        """画像存储是否已完成装配。"""
        return self._persona_enabled and self._persona_started

    async def start_persona(self, *, workflow_ttl_seconds: float | None = None) -> None:
        """启用画像存储：建目录、扫公开索引、清理过期与恢复中断的工作流；失败只降级，绝不抛出。

        **不启用旧记忆配置**是有意的：画像与旧记忆是两个独立的软故障面，任一子系统启用都能
        单独工作（设计 §12），因此这里只翻转画像自己的开关，不碰 `self._enabled`。旧个人记忆
        的读写门禁仍由它自己的配置决定，迁移期的行为因此不受影响。

        `workflow_ttl_seconds` 是可选的配置注入（装配层传 `persona.workflow_ttl_seconds`）；
        不传时用 24 小时的默认值。清理与恢复都在写锁内完成，因此启动期间的读路径不会看到
        半个恢复状态。
        """
        if self._persona_started:
            return
        try:
            os.makedirs(self._persona_users_dir, exist_ok=True)
            os.makedirs(self._persona_flows_dir, exist_ok=True)
            os.makedirs(self._persona_public_dir, exist_ok=True)
        except OSError as exc:
            # 目录建不出来（路径被文件占着、权限不足）：画像整块不可用，聊天照常（D-60）。
            log_event(
                _logger,
                logging.WARNING,
                "persona.load_failed",
                scope=_SCOPE_PERSONA,
                error=type(exc).__name__,
            )
            return
        if workflow_ttl_seconds is not None and float(workflow_ttl_seconds) > 0:
            self._persona_ttl = float(workflow_ttl_seconds)
        self._persona_enabled = True
        self._persona_started = True
        # 顺序是安全要求：恢复先跑，它会把「待撤回」的 owner 放进隔离集，之后才构建公开索引 ——
        # 反过来（先建索引再恢复）会让一次崩溃留下的待撤回 owner 在索引里露一小段头（设计 §7）。
        try:
            await self._recover_persona()
        except Exception as exc:  # 恢复失败只降级：下一次访问仍会各自清理自己的过期流程。
            log_event(
                _logger,
                logging.WARNING,
                "persona.load_failed",
                scope=_SCOPE_PERSONA_FLOW,
                error=type(exc).__name__,
            )
        try:
            count = self._refresh_persona_public()
            log_event(
                _logger, logging.INFO, "persona.ready", scope=_SCOPE_PERSONA_PUBLIC, count=count
            )
        except Exception as exc:  # 扫描自身的兜底：索引构建不能把异常带出去。
            log_event(
                _logger,
                logging.WARNING,
                "persona.load_failed",
                scope=_SCOPE_PERSONA_PUBLIC,
                error=type(exc).__name__,
            )

    async def stop_persona(self) -> None:
        """停用画像写入：推进纪元让在途令牌失效、清空全部缓存与索引；绝不抛出。

        令牌与隔离集都只活在进程内，停止之后不该再接受停止之前发出的令牌（设计 §9）——与
        `stop()` 里记忆那套 `_capture_epoch` 是同一个理由。下一次 `start_persona` 的恢复会按
        盘上的「待撤回」标记把隔离集重新建起来（待撤回本来就是持久状态）。
        """
        self._persona_epoch += 1
        self._persona_enabled = False
        self._persona_started = False
        self._persona_users.clear()
        self._persona_flows.clear()
        self._persona_public.clear()
        self._persona_index = {}
        self._persona_generation.clear()
        self._persona_suppressed.clear()

    async def _recover_persona(self) -> None:
        """启动恢复：过期流程清理、提交标记收尾、待撤回重试（设计 §9 的崩溃恢复）。

        三类中断都在这里收口，全部在写锁内完成：

        - **已提交但没来得及收尾**：生效画像里带着 `committed_draft_id`，工作流却还在。
          只终结流程，绝不重复写画像、也不重做开关变更（`persona_confirm` 的固定顺序）。
        - **已停用但流程还在**：`off` 的中间态。待办正文（问题、回答、草稿）立即清空，
          不让一次崩溃把它们留在盘上。
        - **待撤回**：`pending_unpublish` 为真时重试撤回公开副本（画像与旧记忆各一次），
          成功后才清标记 —— 这正是 `/persona off` 承诺的「重启后继续完成」。这些 owner 在
          重试成功之前一律先放进**隔离集**：公开投影不能一边说着「已关闭」一边继续引用
          崩溃前没撤掉的公开副本（画像的与旧的都算）。
        """
        async with self._write_lock:
            for owner_key in self._persona_file_keys(self._persona_flows_dir):
                state = self._persona_flow_state(owner_key, force=True)
                flow = state.document
                if not isinstance(flow, dict) or persona_models.workflow_is_empty(flow):
                    continue
                user_state = self._persona_user_state(owner_key, force=True)
                document = user_state.document
                if not isinstance(document, dict):
                    continue
                if bool(document.get("pending_unpublish")):
                    # `off` 走到「已停用 + 待撤回」就崩了：待办正文当场清空（设计 §7）。
                    # 判据是待撤回标记，**不是** `enabled`：关闭状态下照样可以开始一次建档或编辑
                    # （设计 §6 允许查看与编辑已保存画像），拿开关当判据会把这种在途流程误杀。
                    self._persona_suppress(owner_key)
                    self._clear_flow_locked(owner_key, stage="cancelled")
                    continue
                committed = document.get("committed_draft_id")
                if committed and committed == flow.get("draft_id"):
                    # 生效文件已经提交了同一稿，只差终结流程（设计 §9 的两步顺序）。
                    self._clear_flow_locked(owner_key, stage="committed")
                    continue
                self._expire_flow_locked(owner_key, state)
            for owner_key in self._persona_file_keys(self._persona_users_dir):
                state = self._persona_user_state(owner_key, force=True)
                document = state.document
                if not isinstance(document, dict) or not bool(document.get("pending_unpublish")):
                    continue
                self._persona_suppress(owner_key)
                if not self._retry_pending_revoke(owner_key, state):
                    log_event(
                        _logger,
                        logging.WARNING,
                        "persona.revoke_pending",
                        scope=_SCOPE_PERSONA_PUBLIC,
                        status=STATUS_UNAVAILABLE,
                    )

    def _retry_pending_revoke(self, owner_key: str, state: _Snapshot) -> bool:
        """重试一次待撤回：画像公开副本与旧公开记忆都到位才清标记（写锁内调用）。

        两个副本都确认撤回之后才解除隔离：隔离跟着「待撤回」走，不跟着这次的写入结果走 ——
        标记没落盘就重启，恢复会重新隔离。
        """
        document = state.document
        if not isinstance(document, dict):
            return False
        public_status = self._revoke_persona_public_locked(owner_key, _PERSONA_RECOVER_OPERATION)
        legacy_status = self._revoke_legacy_locked(owner_key, _PERSONA_RECOVER_OPERATION)
        if public_status not in (STATUS_OK, STATUS_NOOP) or legacy_status not in (
            STATUS_OK,
            STATUS_NOOP,
        ):
            return False
        built = dict(document)
        built["pending_unpublish"] = False
        built["revision"] = state.revision + 1
        status, _ = self._commit_persona(
            self._persona_users, owner_key, self._persona_user_view(owner_key), state, built
        )
        if status == STATUS_OK:
            self._persona_release(owner_key)
        return status == STATUS_OK

    # --- 用户画像：读路径 ---------------------------------------------------

    async def persona_get(self, user_id: str) -> dict[str, Any] | None:
        """该用户的私有画像（深拷贝）；存储未启用或不可用时返回 None。

        文件不存在时返回**默认画像**（`migrated=false`、`enabled=false`、空短文），而不是 None：
        「这个用户还没有画像」与「拿不到这个用户的画像」是两件事，前者要用默认值表达
        （设计 §11 的逐用户迁移就是靠 `migrated` 判定）。返回值是深拷贝，调用方改它不影响
        任何快照或文件。
        """
        if not self._persona_enabled or not user_id:
            return None
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return None
        state = self._persona_user_state(owner_key)
        document = state.document
        if state.reason is not None or not isinstance(document, dict):
            return None
        return copy.deepcopy(document)

    def persona_cached(self, user_id: str) -> dict[str, Any] | None:
        """只读内存快照、不做任何 I/O：快照没加载或用户未知时返回 None。

        与 `private_settings_cached` 同款（D-67）：命令路由需要「这个人有没有开启画像」时
        不能为一句措辞去读文件。
        """
        if not self._persona_enabled or not user_id:
            return None
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return None
        state = self._persona_users.get(owner_key)
        if state is None or not isinstance(state.document, dict):
            return None
        return copy.deepcopy(state.document)

    async def persona_is_migrated(self, user_id: str) -> bool:
        """该用户是否已经进入新画像路径（逐用户迁移的判据，设计 §11）。

        **必须实际加载私有状态**，不能只看 `persona_cached`：那份缓存只有 64 格，被淘汰之后
        「读不到」与「没迁移」会变成同一件事，于是已迁移用户又悄悄拿回旧个人记忆（设计
        §11.6 明确禁止）。读不回来时**失败关闭**（当作已迁移）：宁可少给一次旧记忆，
       也不能让一次黑盒 IO 失败把用户退回旧路径。

        私有文件被删掉时看工作流：墓碑里留着成功提交的 `object_id`，那就是这个用户迁移过的
        证据 —— 删除文件不能复活旧个人记忆。
        """
        try:
            return self._persona_migrated(user_id)
        except Exception:  # 兜底：任何异常都按「已迁移」处理（fail closed）。
            return bool(self._persona_enabled and user_id)

    async def persona_legacy_allowed(self, user_id: str) -> bool:
        """该用户是否还能使用**旧个人记忆**（私有读写与旧自动提取）。

        只有两种情形允许：画像功能整体未装配（旧记忆是唯一路径），或这个人确实有一份旧私有
        文件且**尚未迁移**。已迁移用户一律 False（设计 §11.2/§11.6）；拿不准时同样 False。
        """
        if not self._persona_enabled:
            return True
        try:
            if self._persona_migrated(user_id):
                return False
            return self._legacy_private_file_exists(user_id)
        except Exception:
            return False

    def persona_quarantine(self, user_id: str) -> None:
        """同步隔离：立刻失效在途令牌，并让该 owner 的**公开投影**立即不可见（无私有 IO）。

        装配层在用户明确要求停止的那一刻（`/persona off`、`private`）调用它，因为命令本身要
        排队：等命令跑到服务层再收手，中间那段时间里在途的自动更新仍可能提交、旧的公开副本
        仍可能被大区引用（设计 §6「off … 立即使旧令牌失效」）。这里做的事只有两件：
        推进该用户的代次（令牌作废），把这个不可逆 owner key 放进有界的隔离集。

        它**不读私有文件、不改私有状态**：`/persona private` 也要调用它，但那只该影响公开
        投影，私有使用照旧（私有开关由 `enabled` 决定，不受隔离集影响）。
        """
        if not user_id:
            return
        self.persona_invalidate(user_id)
        owner_key = _storage_key(user_id)
        if owner_key is not None:
            self._persona_suppress(owner_key)

    async def persona_workflow(self, user_id: str) -> dict[str, Any] | None:
        """该用户**待办**的工作流（深拷贝）；没有待办流程时返回 None。

        过期清理在写锁内完成：一次访问就把过期流程落成墓碑（正文清空、阶段 `expired`），
        因此下一次访问不会再读到它。终态流程与空文件一样返回 None —— 对调用方而言，
        「已经提交、取消或过期的流程」不再是可继续的流程（设计 §9）。
        """
        if not self._persona_enabled or not user_id:
            return None
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return None
        async with self._write_lock:
            flow = self._expire_flow_locked(owner_key, self._persona_flow_state(owner_key))
            if not isinstance(flow, dict) or persona_models.workflow_is_empty(flow):
                return None
            return copy.deepcopy(flow)

    def persona_public_username_index(self) -> Mapping[str, tuple[str, ...]]:
        """当前画像公开索引的一次引用快照：`username -> tuple[owner_key, ...]`（无 I/O）。

        与 `public_username_index` 同款、**独立一份**：调用方（C）把两份索引合并成公开
        subject 列表时自行处理重名，本方法不替它做冲突决策。调用方不得跨轮缓存返回值。
        """
        return self._persona_index

    def persona_public_revision_cached(self, user_id: str) -> int | None:
        """该用户**公开快照**的版本号；没发布过、已撤回或快照未加载时返回 None。

        **同步、只读、无 I/O**（与 `persona_cached` / `private_settings_cached` 同款，D-67）：
        `/persona` 要显示「私有 v8，公开 v5」时不能为一行字去读盘。这里读的是公开快照**自己的**
        版本号，不是私有画像的修订号 —— 两者独立演进，公开副本只在用户确认发布时才前进。
        快照没加载时返回 None，宁可少显示一个版本号，也不做一次意外读取（与 `persona_cached`
        同一口径）。任何异常都收敛成 None：这是展示用的增强，绝不影响命令本身。
        """
        if not self._persona_enabled or not user_id:
            return None
        try:
            owner_key = _storage_key(user_id)
            if owner_key is None:
                return None
            if self._persona_suppressed_owner(owner_key):
                # 隔离中的 owner 对外就是「没有公开副本」：索引里也查不到它。
                return None
            state = self._persona_public.get(owner_key)
            document = state.document if state is not None else None
            if not isinstance(document, dict) or not document.get("paragraph"):
                # 空快照（撤回后的墓碑）不算「有公开副本」：只报当前真正公开着的那一份。
                return None
            return int(document.get("revision", 0))
        except Exception:
            return None

    async def persona_public_context(
        self,
        *,
        subjects: tuple[PublicMemorySubject, ...],
        channel_kind: str,
    ) -> tuple[SupplementalItem, ...]:
        """按已选定的 subject 取公开画像短文（设计 §7）；任何失败都返回空元组，绝不抛出。

        **只读 `personas/public/`**：本方法在任何分支下都不会调用 `_persona_user_state`，因此
        「未公开的私有画像永不进入大区或评论」是结构性的，不是靠过滤。DM 与未知频道一律空结果：
        DM 里的私有画像由调用方另行注入，这里只服务公开场景（设计 §7 的「DM 不读第三方公开画像」）。
        """
        if not self._persona_enabled or channel_kind not in _PUBLIC_CHANNELS or not subjects:
            return ()
        try:
            items = self._persona_public_items(subjects)
        except Exception as exc:  # 兜底：公开读取是可选增强，绝不能外溢给聊天（D-60）。
            log_event(
                _logger,
                logging.INFO,
                "persona.context_omitted",
                scope=_SCOPE_PERSONA_PUBLIC,
                reason="internal",
                error=type(exc).__name__,
            )
            return ()
        return tuple(items)

    def persona_invalidate(self, user_id: str) -> None:
        """推进该用户的画像代次，使此前取得的所有更新令牌失效（无 I/O、不抛异常）。

        进入手动编辑、关闭、清空与取消都会调用它；即便当时没有在途令牌也照常推进 ——
        「值没变就不推」会让一次迟到的提交恰好落在同一个代次上。
        """
        if not user_id:
            return
        self._persona_generation[user_id] = self._persona_generation.get(user_id, 0) + 1

    # --- 用户画像：mutation -------------------------------------------------

    async def persona_save(
        self,
        user_id: str,
        value: dict[str, Any],
        *,
        expected_revision: int,
        operation_id: str,
    ) -> str:
        """整份保存私有画像：严格校验 + 版本 CAS + 原子写（设计 §8）。

        `value` 是调用方控制的字段集合：给全一份完整画像 dict 可以，只给要改的几个字段也可以
        （缺的字段沿用**现值**，文件还不存在时用 `default_persona()`）。这条「A 补默认值」的
        读法让宿主不必先读一遍再原样抄回来，也让「只打开开关」不会顺带覆盖短文。
        多出来的未知字段一律 `invalid_proposal`，不做宽容解析。

        修订号由这里内部推进（基线 + 1），`value` 里的 `revision` 与 `operations` 不参与写入 ——
        它们是宿主记账，允许调用方原样回传自己读到的旧值。载荷与现值相同返回 `noop`
        （不写文件、不涨修订号），因此重复保存不会制造无意义的版本。
        """
        if not self._persona_enabled or not user_id or not operation_id:
            return STATUS_UNAVAILABLE
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return STATUS_UNAVAILABLE
        if (
            not isinstance(value, dict)
            or isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            return STATUS_INVALID_PROPOSAL
        proposed = copy.deepcopy(value)
        async with self._write_lock:
            state = self._persona_user_state(owner_key)
            document = state.document
            if not isinstance(document, dict):
                return STATUS_UNAVAILABLE
            hit = document["operations"].get(operation_id)
            if hit is not None:
                # 幂等命中：返回第一次的稳定结果，不重复写、也不重新调模型（设计 §9）。
                return str(hit["status"])
            if state.revision != expected_revision:
                return STATUS_CONFLICT
            merged = _merge_fields(document, proposed, persona_models.PERSONA_FIELDS)
            if merged is None:
                return STATUS_INVALID_PROPOSAL
            if persona_models.payload_of(document) == persona_models.payload_of(merged):
                return STATUS_NOOP
            built = merged
            # 内容确实变了才刷新 `updated_at`（noop 已经在上面返回）：更新时间表示「画像内容
            # 最后一次变化」，而不是「最后一次调用存储」——否则 `/persona` 会一直显示刚刚。
            built["updated_at"] = self._timestamp()
            built["revision"] = state.revision + 1
            built["operations"] = self._record_operation(
                document["operations"], operation_id, STATUS_OK, None, built["revision"]
            )
            try:
                persona_models.validate_persona(built)
            except persona_models.PersonaValidationError:
                return STATUS_INVALID_PROPOSAL
            if self._contains_secret(_persona_texts(built)):
                return STATUS_SECRET_DETECTED
            status, written = self._commit_persona(
                self._persona_users,
                owner_key,
                self._persona_user_view(owner_key),
                state,
                built,
                screen=lambda _document: _persona_texts(built),
            )
            self._log_persona_write(_SCOPE_PERSONA, status, written.revision)
            return status

    async def persona_save_workflow(
        self,
        user_id: str,
        value: dict[str, Any] | None,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> str:
        """保存或清空该用户的工作流（设计 §8、§9）。

        `value` 非空时整份校验后写入并推进修订号；与 `persona_save` 同款，`value` 可以只给要改
        的字段，缺的沿用现值（新文件用 `default_workflow()`）。`value=None` 表示**清空敏感
        正文**：问题、回答、草稿、摘要、修改意见与草稿编号一并清掉，只留下标识、阶段、计数与
        幂等表，因此一次取消或超时之后盘上不会残留待审内容。

        `base_fingerprint` 由**这里**维护，调用方不设置它：开一条新流程时按当时的私有画像快照
        现算（`_persona_fingerprint`），继续同一条流程时原样保留。确认时据此复核「用户看到的那
        一稿，是不是还基于同一份生效画像」——跨越重启也成立，因为指纹随流程落盘。

        `value=None` 时 `expected_revision` **不参与判定**：清空是一个没有歧义的销毁动作，
        让它因为一次版本漂移而静默失败，会把「已经取消的访谈」留在盘上。
        """
        if not self._persona_enabled or not user_id or not operation_id:
            return STATUS_UNAVAILABLE
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return STATUS_UNAVAILABLE
        if value is None:
            async with self._write_lock:
                return self._clear_flow_locked(owner_key, stage=None, operation_id=operation_id)[1]
        if not isinstance(value, dict) or isinstance(expected_revision, bool) or not isinstance(
            expected_revision, int
        ):
            return STATUS_INVALID_PROPOSAL
        proposed = copy.deepcopy(value)
        async with self._write_lock:
            state = self._persona_flow_state(owner_key)
            document = state.document
            if not isinstance(document, dict):
                return STATUS_UNAVAILABLE
            hit = document["operations"].get(operation_id)
            if hit is not None:
                return str(hit["status"])
            if persona_models.workflow_is_empty(document):
                # 终态墓碑（已提交 / 已取消 / 已过期）与空文件对 CAS 而言是同一件事：调用方
                # **拿不到**墓碑的修订号（`persona_workflow` 只返回待办流程），因此必须允许
                # 它从 0 重新开始建一条新流程。幂等表保留，同一条消息的重放照旧命中。
                baseline: dict[str, Any] = persona_models.default_workflow()
                baseline["operations"] = dict(document["operations"])
                base_revision = 0
                new_flow = True
            else:
                incoming = proposed.get("workflow_id")
                if isinstance(incoming, str) and incoming and incoming != document.get("workflow_id"):
                    # 已有待办流程：重开必须先取消，不静默覆盖（设计 §9）。
                    return STATUS_CONFLICT
                baseline = document
                base_revision = state.revision
                new_flow = False
            if base_revision != expected_revision:
                return STATUS_CONFLICT
            if new_flow:
                # 新流程的基线指纹取**当下**的私有画像：这是「用户接下来看到的草稿基于哪一份
                # 画像」的凭证。读不回来就不开流程 —— 一条没有基线的流程无法在确认时复核。
                private = self._persona_user_state(owner_key, force=True)
                private_document = private.document
                if not isinstance(private_document, dict):
                    return STATUS_UNAVAILABLE
            merged = _merge_fields(baseline, proposed, persona_models.WORKFLOW_FIELDS)
            if merged is None:
                return STATUS_INVALID_PROPOSAL
            if persona_models.payload_of(document) == persona_models.payload_of(merged):
                # 载荷没变：不写盘、不涨修订号，因此也不动指纹（它本来就不会变）。
                return STATUS_NOOP
            built = merged
            built["base_fingerprint"] = (
                _persona_fingerprint(private_document)
                if new_flow
                else str(document.get("base_fingerprint") or "")
            )
            built["revision"] = base_revision + 1
            built["operations"] = self._record_operation(
                document["operations"], operation_id, STATUS_OK, None, built["revision"]
            )
            try:
                persona_models.validate_workflow(built)
            except persona_models.PersonaValidationError:
                return STATUS_INVALID_PROPOSAL
            if self._contains_secret(_workflow_texts(built)):
                return STATUS_SECRET_DETECTED
            status, written = self._commit_persona(
                self._persona_flows,
                owner_key,
                self._persona_flow_view(owner_key),
                state,
                built,
                screen=lambda _document: _workflow_texts(built),
            )
            self._log_persona_write(_SCOPE_PERSONA_FLOW, status, written.revision)
            return status

    async def persona_confirm(self, user_id: str, draft_id: str, *, operation_id: str) -> str:
        """确认并提交已展示的草稿（设计 §3、§9）。

        固定顺序：在写锁内复核送达、期限、草稿编号、基础版本与开关，然后**先写生效文件**
        （带 `committed_draft_id` 标记）**再终结工作流**。崩溃发生在两步之间时，重启恢复只做
        收尾（生效画像里的 `committed_draft_id` 与草稿编号相同即为证据），不重复写画像、
        也不重做开关变更。提交的正文永远是**用户看到的那一稿**（工作流里存的那份），
        不接受调用方再传一次文本。
        """
        if not self._persona_enabled or not user_id or not operation_id or not draft_id:
            return STATUS_UNAVAILABLE
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return STATUS_UNAVAILABLE
        async with self._write_lock:
            # 确认路径强制读盘：草稿编号、送达标记与基础版本是「能不能提交」的判据，TTL 缓存会把
            # 「刚刚落到盘上的那一稿」变成看不见的东西（与公开投影销毁路径的 R24 同款理由）。
            flow_state = self._persona_flow_state(owner_key, force=True)
            flow = flow_state.document
            if not isinstance(flow, dict) or not flow.get("workflow_id"):
                return STATUS_NOT_FOUND
            hit = flow["operations"].get(operation_id)
            if hit is not None and hit.get("object_id") == draft_id:
                # 幂等命中：同一条确认消息重放时返回第一次的结果（草稿编号必须一致）。
                return str(hit["status"])
            if persona_models.is_terminal_stage(flow.get("stage")):
                return STATUS_NOT_FOUND
            if self._flow_expired(flow):
                self._expire_flow_locked(owner_key, flow_state)
                return persona_models.STATUS_EXPIRED
            if flow.get("draft_id") != draft_id:
                # 旧草稿编号、被清空的草稿与「不是当前这一稿」都走这里：只接受已送达的当前稿。
                return STATUS_NOT_FOUND
            if not bool(flow.get("delivered")):
                # 用户没看见过的内容不能提交（设计 §9：确认只接受已送达的当前 draft_id）。
                return persona_models.STATUS_NOT_DELIVERED
            persona_state = self._persona_user_state(owner_key, force=True)
            document = persona_state.document
            if not isinstance(document, dict):
                return STATUS_UNAVAILABLE
            if bool(document.get("pending_unpublish")):
                return STATUS_UNAVAILABLE
            kind = flow.get("kind")
            if kind not in ("create", "edit", "public", "clear"):
                return STATUS_INVALID_PROPOSAL
            if kind in ("create", "edit", "clear") and document.get("committed_draft_id") == draft_id:
                # 崩溃恢复：生效文件早已提交并带上了这一稿的标记，只差收尾（设计 §9）。
                # 这条路径**跳过**下面的基线复核：它要做的只是收尾，不重复任何写入。
                self._clear_flow_locked(
                    owner_key, stage="committed", operation_id=operation_id, object_id=draft_id
                )
                return STATUS_OK
            # 基线复核：草稿必须仍基于**同一份**生效画像（见 `_confirm_baseline_matches`）。
            if not self._confirm_baseline_matches(kind, document, persona_state.revision, flow):
                return STATUS_CONFLICT
            if kind in ("create", "edit"):
                if kind == "create" and not bool(document.get("migrated")):
                    # 逐用户迁移：旧公开记忆必须先撤回，撤回失败就不提交切换（设计 §11）。
                    if self._revoke_legacy_locked(owner_key, f"{operation_id}:legacy") not in (
                        STATUS_OK,
                        STATUS_NOOP,
                    ):
                        return STATUS_UNAVAILABLE
                status = self._confirm_persona_locked(
                    owner_key, persona_state, flow, draft_id, operation_id
                )
            elif kind == "public":
                status = self._confirm_public_locked(owner_key, persona_state, flow, operation_id)
            else:
                status = self._confirm_clear_locked(owner_key, persona_state, flow, operation_id)
            if status != STATUS_OK:
                return status
            self._clear_flow_locked(
                owner_key, stage="committed", operation_id=operation_id, object_id=draft_id
            )
            # 提交会改画像/公开副本：在途的自动更新结果一律作废（设计 §6、§9）。
            self.persona_invalidate(user_id)
            return STATUS_OK

    async def persona_off(self, user_id: str, *, operation_id: str) -> str:
        """关闭画像：先落盘停用与待撤回，再撤回公开副本（设计 §7）。

        两步顺序是这条承诺的全部：第一次写入把「已停用 + 待撤回」落到盘上，**之后**才做撤回。
        撤回失败时返回 `unavailable` 并保留待撤回标记 —— 不报告「全部关闭成功」，也不让用户
        误以为公开副本已经收回；重启与下一次访问都会重试（`start_persona` 的恢复路径）。
        私有画像本身保留，便于重新开启。
        """
        if not self._persona_enabled or not user_id or not operation_id:
            return STATUS_UNAVAILABLE
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return STATUS_UNAVAILABLE
        async with self._write_lock:
            state = self._persona_user_state(owner_key, force=True)
            document = state.document
            if not isinstance(document, dict):
                return STATUS_UNAVAILABLE
            hit = document["operations"].get(operation_id)
            if hit is not None:
                return str(hit["status"])
            # 受理即隔离：公开投影从**这一行**起就不再引用该 owner，哪怕下面的写入或撤回失败。
            # 用户的诉求是「立刻停止」，不能等到两步落盘之间那段窗口结束才生效（设计 §7）。
            self._persona_suppress(owner_key)
            if not bool(document.get("enabled")) and not bool(document.get("pending_unpublish")):
                # 本来就是关闭、也没有待撤回：不再翻版本，只把遗留令牌与待办流程收干净。
                # 状态已经确认干净，隔离随即解除（盘上没有任何待撤的公开副本）。
                self.persona_invalidate(user_id)
                self._clear_flow_locked(owner_key, stage="cancelled")
                self._persona_release(owner_key)
                return STATUS_NOOP
            staged = dict(document)
            staged["enabled"] = False
            staged["pending_unpublish"] = True
            staged["revision"] = state.revision + 1
            staged["updated_at"] = self._timestamp()
            staged["last_change"] = texts.PERSONA_CHANGE_DISABLED_TEXT
            status, written = self._commit_persona(
                self._persona_users, owner_key, self._persona_user_view(owner_key), state, staged
            )
            self._log_persona_write(_SCOPE_PERSONA, status, written.revision)
            if status != STATUS_OK:
                return STATUS_UNAVAILABLE
            # 停用立即生效：在途令牌失效，待办流程的正文（问题、回答、草稿）当场清空。
            self.persona_invalidate(user_id)
            flow_ok = self._clear_flow_locked(owner_key, stage="cancelled")[0]
            public_status = self._revoke_persona_public_locked(owner_key, f"{operation_id}:public")
            legacy_status = self._revoke_legacy_locked(owner_key, f"{operation_id}:legacy")
            if (
                not flow_ok
                or public_status not in (STATUS_OK, STATUS_NOOP)
                or legacy_status not in (STATUS_OK, STATUS_NOOP)
            ):
                log_event(
                    _logger,
                    logging.WARNING,
                    "persona.revoke_pending",
                    scope=_SCOPE_PERSONA_PUBLIC,
                    status=STATUS_UNAVAILABLE,
                )
                return STATUS_UNAVAILABLE
            final_state = self._persona_user_state(owner_key, force=True)
            final = final_state.document
            if not isinstance(final, dict):
                return STATUS_UNAVAILABLE
            built = dict(final)
            built["pending_unpublish"] = False
            built["revision"] = final_state.revision + 1
            built["operations"] = self._record_operation(
                final["operations"], operation_id, STATUS_OK, None, built["revision"]
            )
            status, written = self._commit_persona(
                self._persona_users, owner_key, self._persona_user_view(owner_key), final_state, built
            )
            self._log_persona_write(_SCOPE_PERSONA, status, written.revision)
            if status != STATUS_OK:
                # 公开副本已经撤回、待撤回标记却没能落盘：保持隔离，等恢复/下一次 off 收尾。
                return STATUS_UNAVAILABLE
            self._persona_release(owner_key)
            return STATUS_OK

    async def persona_unpublish(self, user_id: str, *, operation_id: str) -> str:
        """撤回公开副本，继续私有使用（`/persona private`）：只改公开文件，不动开关。

        同样**受理即隔离**：撤回写失败时旧副本仍然在盘上，隔离必须留着，否则大区还会继续引用
        一个用户已经要求下架的副本。撤回成功（或本来就没有副本）才解除隔离 —— 私有使用全程
        不受影响，隔离集只管公开投影。
        """
        if not self._persona_enabled or not user_id or not operation_id:
            return STATUS_UNAVAILABLE
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return STATUS_UNAVAILABLE
        async with self._write_lock:
            self._persona_suppress(owner_key)
            status = self._revoke_persona_public_locked(owner_key, operation_id)
            if status in (STATUS_OK, STATUS_NOOP):
                self._persona_release(owner_key)
            return status

    # --- 用户画像：自动更新的授权令牌（设计 §6） ----------------------------

    async def persona_begin_update(self, user_id: str) -> persona_models.PersonaUpdateToken | None:
        """开始一次自动更新：在同一把写锁内取齐代次、版本与文档快照。

        返回 None 表示当时不满足授权：画像未启用、用户已关闭画像、有待撤回未完成、已经有一个
        待办流程（编辑期间暂停自动提交），或存储不可用。取得令牌之后模型调用发生在写锁
        **之外**，因此 `/persona off`、清空与手动编辑无需等待模型返回。
        """
        if not self._persona_enabled or not user_id:
            return None
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return None
        async with self._write_lock:
            state = self._persona_user_state(owner_key)
            document = state.document
            if not isinstance(document, dict):
                return None
            if not bool(document.get("enabled")) or bool(document.get("pending_unpublish")):
                return None
            flow = self._expire_flow_locked(owner_key, self._persona_flow_state(owner_key))
            if isinstance(flow, dict) and not persona_models.workflow_is_empty(flow):
                return None
            return persona_models.PersonaUpdateToken(
                user_id=user_id,
                epoch=self._persona_epoch,
                generation=self._persona_generation.get(user_id, 0),
                revision=state.revision,
                state="enabled",
                enabled=True,
                locked_features=persona_models.locked_feature_keys(document["features"]),
                document=copy.deepcopy(document),
            )

    async def persona_commit_update(
        self,
        token: persona_models.PersonaUpdateToken,
        paragraph: str,
        features: list[dict[str, Any]],
        *,
        operation_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """提交一次自动更新；令牌失效或锁定特征被改时返回 `noop`（丢弃这次结果，不写文件）。

        令牌复核与写入在同一把锁内，因此 `/persona off`、清空与手动编辑之间没有可观察的中间态
        （设计 §6）。**强制读盘**并与令牌里的整份快照逐字段比对（不只是修订号）：外部编辑可以
        同 revision 改内容，只比修订号会让一次迟到的自动更新把那次编辑盖掉。锁定维度在这里
        **由宿主保守冻结**（见 `_merge_auto_features`）：模型不得覆盖、重新推入或换词恢复用户
        已经否定的内容；它动过锁定维度时连短文一起保留上一版。

        `metadata` 是可选的白名单键（`update_day` / `update_count` / `last_update_at` /
        `last_message_id` / `observations` / `last_change`），由控制器传入自动更新的记账字段；
        越界的键一律 `invalid_proposal`，不做部分应用。
        """
        if not self._persona_enabled or not operation_id:
            return STATUS_NOOP
        if not isinstance(token, persona_models.PersonaUpdateToken):
            return STATUS_NOOP
        user_id = token.user_id
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return STATUS_NOOP
        if metadata is not None and (
            not isinstance(metadata, dict) or not set(metadata) <= _PERSONA_METADATA_FIELDS
        ):
            return STATUS_INVALID_PROPOSAL
        async with self._write_lock:
            if token.epoch != self._persona_epoch or self._persona_generation.get(user_id, 0) != (
                token.generation
            ):
                return STATUS_NOOP
            flow = self._expire_flow_locked(owner_key, self._persona_flow_state(owner_key))
            if isinstance(flow, dict) and not persona_models.workflow_is_empty(flow):
                return STATUS_NOOP
            # 强制读盘：模型调用发生在锁外，这期间磁盘可能被人工改过（含同 revision 的改动）。
            state = self._persona_user_state(owner_key, force=True)
            document = state.document
            if not isinstance(document, dict):
                return STATUS_NOOP
            if not bool(document.get("enabled")) or bool(document.get("pending_unpublish")):
                return STATUS_NOOP
            if state.revision != token.revision or document != token.document:
                # 版本 CAS：修订号与**整份内容**都要与发放令牌时一致。并发写入、人工编辑
                # （哪怕没动 revision）、以及外部的删除重建都让这次结果作废（设计 §6）。
                return STATUS_NOOP
            if persona_models.locked_feature_keys(document["features"]) != token.locked_features:
                # 用户刚刚改过锁定项：这次结果基于旧授权，丢弃。
                return STATUS_NOOP
            hit = document["operations"].get(operation_id)
            if hit is not None:
                return str(hit["status"])
            merged_result = _merge_auto_features(
                document["features"], features, now_stamp=self._timestamp()
            )
            if merged_result is None:
                return STATUS_INVALID_PROPOSAL
            merged, locked_touched = merged_result
            built = dict(document)
            built["revision"] = state.revision + 1
            # 模型动过锁定维度：它的新句子很可能正是在描述那几条被丢弃的改动，而宿主没有
            # 语义能力把句子改回去 —— 整段保留上一版短文（保守冻结）。
            built["paragraph"] = document["paragraph"] if locked_touched else paragraph
            built["features"] = merged
            built["updated_at"] = self._timestamp()
            built["last_change"] = texts.PERSONA_CHANGE_UPDATED_TEXT
            if metadata:
                for name, value in metadata.items():
                    built[name] = copy.deepcopy(value)
            built["operations"] = self._record_operation(
                document["operations"], operation_id, STATUS_OK, None, built["revision"]
            )
            try:
                persona_models.validate_persona(built)
            except persona_models.PersonaValidationError:
                return STATUS_INVALID_PROPOSAL
            if self._contains_secret(_persona_texts(built)):
                return STATUS_SECRET_DETECTED
            status, written = self._commit_persona(
                self._persona_users,
                owner_key,
                self._persona_user_view(owner_key),
                state,
                built,
                screen=lambda _document: _persona_texts(built),
            )
            self._log_persona_write(_SCOPE_PERSONA, status, written.revision)
            return status

    # --- 用户画像：迁移判据与隔离（设计 §11、§7） ---------------------------

    def _persona_migrated(self, user_id: str) -> bool:
        """`persona_is_migrated` 的实现：读私有状态（TTL 缓存），必要时回看工作流墓碑。"""
        if not self._persona_enabled or not user_id:
            return False
        owner_key = _storage_key(user_id)
        if owner_key is None:
            # 编不出存储键说明调用方给的压根不是站点 ID：没有可归属的私有文件。
            return False
        state = self._persona_user_state(owner_key)
        document = state.document
        if not isinstance(document, dict):
            # 文件存在但读不回来，而且没有一份可用的旧快照：拿不准 → 按已迁移处理。
            return True
        if bool(document.get("migrated")):
            return True
        if state.digest is None:
            # 私有文件不在了：只有工作流墓碑能证明这个人迁移过。
            return self._workflow_shows_commit(owner_key)
        return False

    def _workflow_shows_commit(self, owner_key: str) -> bool:
        """工作流里有没有「成功提交过画像」的记录（幂等表里的 ok + 非空 object_id）。

        `persona_save_workflow` 记的 `object_id` 永远是 None，因此这条判据不会被一次普通的
        流程保存误命中；只有 `persona_confirm` 的四类提交会写下草稿编号。读不回来时按
        「有提交」处理（fail closed）。
        """
        try:
            # 强制读盘：这条判据只在私有画像文件**不在**的时候跑（罕见），而它决定的是一件
            # 不可逆的事 —— 用户会不会重新拿回旧个人记忆。TTL 缓存里的一份旧流程文件足以
            # 让它答错，这里不值得省那一次读取。
            state = self._persona_flow_state(owner_key, force=True)
        except Exception:
            return True
        flow = state.document
        if not isinstance(flow, dict):
            return True
        for result in (flow.get("operations") or {}).values():
            if (
                isinstance(result, Mapping)
                and result.get("status") == STATUS_OK
                and result.get("object_id")
            ):
                return True
        return False

    def _legacy_private_file_exists(self, user_id: str) -> bool:
        """旧私有记忆文件在不在盘上：只看「有没有这个文件」，不解析、不读正文。"""
        try:
            return _read_bytes(self.private_path(user_id), 1) is not None
        except OSError:
            return False

    def _legacy_private_blocked(self, user_id: str | None) -> bool:
        """旧个人记忆的私有路径是否已被画像迁移关上（纵深防御，见 D-56 同款理由）。

        画像未装配时恒为 False：没有新路径就谈不上迁移，旧记忆照旧工作。读不回来时按
        「已迁移」处理，与 `persona_is_migrated` 同一套失败方向。
        """
        if not self._persona_enabled or not user_id:
            return False
        try:
            return self._persona_migrated(user_id)
        except Exception:
            return True

    def _persona_cached_migrated(self, user_id: str) -> bool:
        """只读内存快照判断「已迁移」（无 I/O）：给同步回调用的保守提示，不是权威判据。"""
        if not self._persona_enabled or not user_id:
            return False
        owner_key = _storage_key(user_id)
        if owner_key is None:
            return False
        state = self._persona_users.get(owner_key)
        if state is None or not isinstance(state.document, dict):
            return False
        return bool(state.document.get("migrated"))

    def _persona_suppress(self, owner_key: str) -> None:
        """把一个 owner 放进隔离集（有界 FIFO）：两份公开索引立刻都不再列出它。

        索引也要当场改：读取路径的过滤只在有人来取资料时生效，而 `public_username_index()` /
        `persona_public_username_index()` 返回的是**索引快照**，调用方（装配层）拿它去组
        subject 列表 —— 不改索引就等于「已下架」的 owner 还会被反复组进候选。
        """
        if not _valid_owner_key(owner_key):
            return
        self._persona_suppressed[owner_key] = None
        self._persona_suppressed.move_to_end(owner_key)
        while len(self._persona_suppressed) > _PERSONA_SUPPRESSED_MAX:
            self._persona_suppressed.popitem(last=False)
        self._public_index = _reindex(self._public_index, owner_key, None)
        self._persona_index = _reindex_persona(self._persona_index, owner_key, None)

    def _persona_release(self, owner_key: str) -> None:
        """解除隔离：撤回/发布**确认落盘**之后才调用（隔离跟着待撤回走）。"""
        self._persona_suppressed.pop(owner_key, None)

    def _persona_suppressed_owner(self, owner_key: str) -> bool:
        """该 owner 是否正被隔离；隔离集只装不可逆 owner key，写锁外读也是安全的。"""
        return owner_key in self._persona_suppressed

    # --- 用户画像：路径、视图与快照（与记忆同款，独立一份缓存） --------------

    def _persona_user_path(self, owner_key: str) -> str:
        """私有画像的路径（`personas/users/<owner_key>.md`；目录不存在时**不创建**）。"""
        return os.path.join(self._persona_users_dir, f"{_persona_key(owner_key)}{_MD_SUFFIX}")

    def _persona_flow_path(self, owner_key: str) -> str:
        """工作流的路径（`personas/workflows/<owner_key>.md`）。"""
        return os.path.join(self._persona_flows_dir, f"{_persona_key(owner_key)}{_MD_SUFFIX}")

    def _persona_public_path(self, owner_key: str) -> str:
        """公开快照的路径（`personas/public/<owner_key>.md`）。"""
        return os.path.join(self._persona_public_dir, f"{_persona_key(owner_key)}{_MD_SUFFIX}")

    def persona_user_path(self, user_id: str) -> str:
        """按原始 `user_id` 取私有画像路径；用于测试与诊断（写入仍只看 owner key）。"""
        return self._persona_user_path(_storage_key(user_id) or _invalid_id_path_key(user_id))

    def _persona_user_view(self, owner_key: str) -> _DocumentView:
        return _DocumentView(
            path=self._persona_user_path(owner_key),
            parser=persona_codec.parse_persona,
            fresh=persona_models.default_persona,
            scope=_SCOPE_PERSONA,
            render=persona_codec.render_persona,
        )

    def _persona_flow_view(self, owner_key: str) -> _DocumentView:
        return _DocumentView(
            path=self._persona_flow_path(owner_key),
            parser=persona_codec.parse_workflow,
            fresh=persona_models.default_workflow,
            scope=_SCOPE_PERSONA_FLOW,
            render=persona_codec.render_workflow,
        )

    def _persona_public_view(self, owner_key: str) -> _DocumentView:
        return _DocumentView(
            path=self._persona_public_path(owner_key),
            parser=persona_codec.parse_public_persona,
            fresh=persona_models.default_public_persona,
            scope=_SCOPE_PERSONA_PUBLIC,
            render=persona_codec.render_public_persona,
        )

    def _persona_user_state(self, owner_key: str, *, force: bool = False) -> _Snapshot:
        """私有画像快照：与用户记忆文件同款的惰性加载 + 有界 LRU + TTL 摘要比对。"""
        state = self._persona_users.get(owner_key)
        fresh = self._inspect(
            self._persona_user_view(owner_key),
            state,
            ttl=self._refresh_seconds,
            event="persona.refresh_failed" if state is not None else "persona.load_failed",
            force=force,
        )
        self._store_persona(self._persona_users, owner_key, fresh)
        return fresh

    def _persona_flow_state(self, owner_key: str, *, force: bool = False) -> _Snapshot:
        """工作流快照。**提交与终结路径都 `force=True`**：草稿编号、送达标记与基础版本是确认
        的前提，TTL 缓存会把「刚刚落到盘上的那一稿」变成看不见的东西。"""
        state = self._persona_flows.get(owner_key)
        fresh = self._inspect(
            self._persona_flow_view(owner_key),
            state,
            ttl=self._refresh_seconds,
            event="persona.refresh_failed" if state is not None else "persona.load_failed",
            force=force,
        )
        self._store_persona(self._persona_flows, owner_key, fresh)
        return fresh

    def _persona_public_state(self, owner_key: str, *, force: bool = False) -> _Snapshot:
        """公开画像快照：写入与撤回路径必须 `force=True`（私有公开投影的 R24 同款理由）。"""
        state = self._persona_public.get(owner_key)
        fresh = self._inspect(
            self._persona_public_view(owner_key),
            state,
            ttl=self._refresh_seconds,
            event="persona.refresh_failed" if state is not None else "persona.load_failed",
            force=force,
        )
        self._store_persona(self._persona_public, owner_key, fresh)
        return fresh

    def _store_persona(
        self, cache: "OrderedDict[str, _Snapshot]", owner_key: str, state: _Snapshot
    ) -> None:
        """写入快照缓存并维持 LRU 上界；淘汰只释放内存，**不删文件**。"""
        cache[owner_key] = state
        cache.move_to_end(owner_key)
        while len(cache) > _PERSONA_CACHE_SIZE:
            cache.popitem(last=False)

    def _commit_persona(
        self,
        cache: "OrderedDict[str, _Snapshot]",
        owner_key: str,
        view: _DocumentView,
        state: _Snapshot,
        built: dict[str, Any],
        *,
        screen: _ScreenFn | None = None,
    ) -> tuple[str, _Snapshot]:
        """以「基线快照逐字段未变」为前提整份替换一份画像文档；返回（状态, 新快照）。

        CAS 由 applier 在**真正要写的那个基线**上执行：`_write_document` 接手外部版本之后会用
        外部基线重放 applier，磁盘上人工改过的版本因此不会被这次写入静默覆盖（同 revision 的
        改动也算：比的是整份文档，不是修订号）。
        """
        status, _, written = self._write_document(
            view, state, _replace_applier(state.document, built), screen=screen
        )
        if written is not state:
            self._store_persona(cache, owner_key, written)
        return status, written

    def _record_operation(
        self,
        operations: Mapping[str, Any],
        operation_id: str,
        status: str,
        object_id: str | None,
        revision: int,
    ) -> dict[str, dict[str, Any]]:
        """画像文档的幂等记账：与记忆的 `_record` 同一套「按插入顺序淘汰最旧」规则。

        只记元数据（状态、对象 ID、修订号），绝不记正文（设计 §8）。
        """
        updated: dict[str, dict[str, Any]] = {key: value for key, value in operations.items()}
        updated.pop(operation_id, None)
        updated[operation_id] = {"status": status, "object_id": object_id, "revision": revision}
        while len(updated) > self._config.max_operations:
            oldest = next(iter(updated))
            if oldest == operation_id:
                break
            updated.pop(oldest)
        return updated

    # --- 用户画像：工作流与公开副本的内部步骤 -------------------------------

    def _flow_expired(self, flow: Mapping[str, Any]) -> bool:
        """工作流是否已过有效期（`expires_at` 为 0 表示没有设期限）。"""
        expires = flow.get("expires_at")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            return False
        return float(expires) > 0 and self._now() >= float(expires)

    def _expire_flow_locked(self, owner_key: str, state: _Snapshot) -> dict[str, Any] | None:
        """过期流程的清理：落成 `expired` 墓碑并清空待办正文（写锁内调用）。

        写入失败时同样返回清理后的内存版本：这次已经不可能继续这个流程，返回内容与语义一致，
        下一次访问会再试一次落盘。
        """
        document = state.document
        if not isinstance(document, dict):
            return None
        if persona_models.workflow_is_empty(document) or not self._flow_expired(document):
            return document
        built = persona_models.cleared_workflow(document)
        built["stage"] = "expired"
        built["revision"] = state.revision + 1
        status, written = self._commit_persona(
            self._persona_flows, owner_key, self._persona_flow_view(owner_key), state, built
        )
        self._log_persona_write(_SCOPE_PERSONA_FLOW, status, written.revision)
        if status == STATUS_OK and isinstance(written.document, dict):
            return written.document
        return built

    def _clear_flow_locked(
        self,
        owner_key: str,
        *,
        stage: str | None,
        operation_id: str = "",
        object_id: str | None = None,
    ) -> tuple[bool, str]:
        """把待办流程终结成墓碑：清空正文、按需换阶段、记下幂等结果（写锁内调用）。

        返回（是否写入成功, 稳定状态）。`stage=None` 表示保留现有阶段（`persona_save_workflow`
        的 `value=None`：调用方只要求清空正文，不要求改变流程状态）。
        """
        state = self._persona_flow_state(owner_key, force=True)
        document = state.document
        if not isinstance(document, dict):
            return False, STATUS_UNAVAILABLE
        if operation_id:
            hit = document["operations"].get(operation_id)
            if hit is not None:
                return True, str(hit["status"])
        if persona_models.workflow_is_empty(document):
            # 没有待办流程（从来没建过，或已经是终态墓碑）：nothing to clear。
            return True, STATUS_NOOP
        built = persona_models.cleared_workflow(document)
        if stage is not None:
            built["stage"] = stage
        if built == document:
            # 正文早就清空了（重复取消、重复清空）：不再涨修订号，也不重复记幂等结果。
            return True, STATUS_NOOP
        built["revision"] = state.revision + 1
        if operation_id:
            built["operations"] = self._record_operation(
                document["operations"], operation_id, STATUS_OK, object_id, built["revision"]
            )
        else:
            built["operations"] = dict(document["operations"])
        status, written = self._commit_persona(
            self._persona_flows, owner_key, self._persona_flow_view(owner_key), state, built
        )
        self._log_persona_write(_SCOPE_PERSONA_FLOW, status, written.revision)
        return status == STATUS_OK, status

    def _confirm_baseline_matches(
        self,
        kind: str,
        document: Mapping[str, Any],
        revision: int,
        flow: Mapping[str, Any],
    ) -> bool:
        """确认前的基线复核：修订号 + 内容指纹（写锁内调用）。

        - **修订号**：宿主记账，拦住「展示草稿之后画像又前进了一版」。
          唯一的例外是 `create` 且当前正文为空：建档的基线按定义就是一份**空的**生效画像
          （没有画像时才走建档），而清空之后的重新建档同样走这条路 —— 那时墓碑还带着旧修订号，
          把它算进基线会让用户永远无法重新建档。内容安全由指纹保证，这里放宽的只是记账。
        - **指纹**：草稿基于的内容本身（段落 + 特征），随流程落盘，因此跨重启成立；
          空指纹表示这条流程来自还没有指纹字段的版本，此时只核对修订号（向后兼容）。
        """
        if revision != flow.get("base_revision") and not (
            kind == "create" and not str(document.get("paragraph") or "")
        ):
            return False
        fingerprint = flow.get("base_fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            return _persona_fingerprint(document) == fingerprint
        return True

    def _confirm_persona_locked(
        self,
        owner_key: str,
        state: _Snapshot,
        flow: Mapping[str, Any],
        draft_id: str,
        operation_id: str,
    ) -> str:
        """建档与编辑的提交：把**已展示的**那一稿写进生效画像（设计 §3、§9）。

        只复制工作流里存的段落与特征 —— 用户确认的就是他看见的那一份，不再次调用模型润色。
        `create` 打开画像；`edit` 保留现有开关（关闭状态下的编辑不隐式重新开启，设计 §6）。
        """
        document = state.document
        if not isinstance(document, dict):
            return STATUS_UNAVAILABLE
        hit = document["operations"].get(operation_id)
        if hit is not None and hit.get("object_id") == draft_id:
            # 幂等：上一次确认已经把这一稿写进生效文件，只是还没终结流程。`object_id` 必须是
            # 同一份草稿 —— 同一个文件里的其它幂等记录（元数据保存之类）不能冒充一次提交。
            return str(hit["status"])
        built = dict(document)
        built["revision"] = state.revision + 1
        built["paragraph"] = flow["paragraph"]
        built["features"] = copy.deepcopy(flow["features"])
        built["committed_draft_id"] = draft_id
        built["migrated"] = True
        built["updated_at"] = self._timestamp()
        built["last_change"] = (
            texts.PERSONA_CHANGE_CREATED_TEXT if flow.get("kind") == "create" else texts.PERSONA_CHANGE_EDITED_TEXT
        )
        if flow.get("kind") == "create":
            built["enabled"] = True
        built["operations"] = self._record_operation(
            document["operations"], operation_id, STATUS_OK, draft_id, built["revision"]
        )
        try:
            persona_models.validate_persona(built)
        except persona_models.PersonaValidationError:
            return STATUS_INVALID_PROPOSAL
        if self._contains_secret(_persona_texts(built)):
            return STATUS_SECRET_DETECTED
        status, written = self._commit_persona(
            self._persona_users,
            owner_key,
            self._persona_user_view(owner_key),
            state,
            built,
            screen=lambda _document: _persona_texts(built),
        )
        self._log_persona_write(_SCOPE_PERSONA, status, written.revision)
        return status

    def _confirm_public_locked(
        self,
        owner_key: str,
        persona_state: _Snapshot,
        flow: Mapping[str, Any],
        operation_id: str,
    ) -> str:
        """公开发布的提交：写入**独立**快照，私有画像一个字节都不改（设计 §7）。

        公开副本只含短文与必要身份、版本元数据：访谈答案、证据索引、修改意见与置信度都不
        出现在这里。`source_revision` 记下快照的来源版本，`/persona` 因此能显示「私有 v8，
        公开 v5」。
        """
        document = persona_state.document
        if not isinstance(document, dict):
            return STATUS_UNAVAILABLE
        public_state = self._persona_public_state(owner_key, force=True)
        public = public_state.document
        if not isinstance(public, dict):
            return STATUS_UNAVAILABLE
        hit = public["operations"].get(operation_id)
        if hit is not None and hit.get("object_id") == flow.get("draft_id"):
            # 幂等：上一次确认已经发布了这一稿，只是还没终结流程。
            return str(hit["status"])
        built = dict(public)
        username = document.get("username")
        built["owner_username"] = username if isinstance(username, str) else ""
        built["paragraph"] = flow["paragraph"]
        # 来源版本取**草稿的基线**，不是当下的修订号：快照发布的是用户预览过的那一段文字，
        # 它的来源就是预览时的那一版（确认路径已经复核过 base_revision，两者必然一致；
        # 记基线是把这层语义写进文件，而不是让它取决于「读的时候恰好是几」）。
        built["source_revision"] = int(flow.get("base_revision") or 0)
        built["published_at"] = self._timestamp()
        built["revision"] = public_state.revision + 1
        built["operations"] = self._record_operation(
            public["operations"], operation_id, STATUS_OK, flow.get("draft_id"), built["revision"]
        )
        try:
            persona_models.validate_public_persona(built)
        except persona_models.PersonaValidationError:
            # 最可能的失败是拿不到合法站点用户名：没有身份就不发布（公开索引按它归属）。
            return STATUS_INVALID_PROPOSAL
        if self._contains_secret((built["paragraph"], built["owner_username"])):
            return STATUS_SECRET_DETECTED
        status, written = self._commit_persona(
            self._persona_public, owner_key, self._persona_public_view(owner_key), public_state, built
        )
        if status == STATUS_OK:
            # 新快照已经落盘并复核过：解除隔离，让这一版公开画像重新进入索引与大区引用。
            self._persona_release(owner_key)
            self._reindex_persona_public(owner_key, written.document)
        self._log_persona_write(_SCOPE_PERSONA_PUBLIC, status, written.revision)
        return status

    def _confirm_clear_locked(
        self,
        owner_key: str,
        persona_state: _Snapshot,
        flow: Mapping[str, Any],
        operation_id: str,
    ) -> str:
        """清空的提交：**先撤回、再删除**（设计 §7）。撤回失败时不删私有内容，保留以便继续处理。"""
        public_status = self._revoke_persona_public_locked(owner_key, f"{operation_id}:public")
        if public_status not in (STATUS_OK, STATUS_NOOP):
            return STATUS_UNAVAILABLE
        legacy_status = self._revoke_legacy_locked(owner_key, f"{operation_id}:legacy")
        if legacy_status not in (STATUS_OK, STATUS_NOOP):
            return STATUS_UNAVAILABLE
        document = persona_state.document
        if not isinstance(document, dict):
            return STATUS_UNAVAILABLE
        built = dict(document)
        built["revision"] = persona_state.revision + 1
        # 墓碑：保留身份与开关字段、清掉全部正文；`migrated=true` 因此不会退回旧个人记忆（设计 §11）。
        built["enabled"] = False
        built["migrated"] = True
        built["paragraph"] = ""
        built["features"] = []
        built["observations"] = []
        built["pending_unpublish"] = False
        # 提交标记照记：清空同样是「先写生效文件、再终结流程」的两步，崩溃后靠它收尾。
        built["committed_draft_id"] = flow.get("draft_id") or ""
        built["updated_at"] = self._timestamp()
        built["last_change"] = texts.PERSONA_CHANGE_CLEARED_TEXT
        built["operations"] = self._record_operation(
            document["operations"], operation_id, STATUS_OK, flow.get("draft_id"), built["revision"]
        )
        status, written = self._commit_persona(
            self._persona_users, owner_key, self._persona_user_view(owner_key), persona_state, built
        )
        self._log_persona_write(_SCOPE_PERSONA, status, written.revision)
        if status == STATUS_OK:
            # 公开副本（画像的与旧的）都已撤回：隔离的使命结束，解除即可（索引里本来就没有它）。
            self._persona_release(owner_key)
        return status

    def _revoke_persona_public_locked(self, owner_key: str, operation_id: str) -> str:
        """撤回画像公开副本：写一份空快照（保留 operations），并把它移出索引（写锁内调用）。

        本来就没有公开副本时是 `noop`：不写文件，也不给从未发布过的用户凭空造一份空文件
        （与记忆公开投影的 R9 同一套理由）。
        """
        state = self._persona_public_state(owner_key, force=True)
        document = state.document
        if not isinstance(document, dict):
            return STATUS_UNAVAILABLE
        hit = document["operations"].get(operation_id)
        if hit is not None:
            return str(hit["status"])
        if not document.get("paragraph") and not document.get("owner_username"):
            return STATUS_NOOP
        built = persona_models.default_public_persona()
        built["revision"] = state.revision + 1
        built["operations"] = self._record_operation(
            document["operations"], operation_id, STATUS_OK, None, built["revision"]
        )
        status, written = self._commit_persona(
            self._persona_public, owner_key, self._persona_public_view(owner_key), state, built
        )
        if status == STATUS_OK:
            self._reindex_persona_public(owner_key, written.document)
        self._log_persona_write(_SCOPE_PERSONA_PUBLIC, status, written.revision)
        return status

    def _revoke_legacy_locked(self, owner_key: str, operation_id: str) -> str:
        """撤回该用户的**旧**公开个人记忆（逐用户迁移与关闭共用，写锁内调用）。

        走记忆自己的 `_mutate_public_locked`（同一把锁、同一套原子写），因此不会自锁，也不会
        绕过密钥筛与幂等表。旧记忆未启用时没有可撤回的东西：稳定 `noop`。
        """
        if not self._enabled:
            return STATUS_NOOP
        result = self._mutate_public_locked(
            owner_key, operation_id, lambda document: self._apply_unpublish_all(document, operation_id)
        )
        if result.status in (STATUS_OK, STATUS_NOOP):
            return STATUS_OK
        return STATUS_UNAVAILABLE

    # --- 用户画像：公开索引与补充资料 ---------------------------------------

    def _refresh_persona_public(self) -> int:
        """重扫 `personas/public/` 并一次引用替换索引；返回索引到的公开画像条数。

        空快照（撤回后的墓碑）不入索引：它只是幂等元数据的载体（与记忆公开投影的 R9 同款）。
        """
        index: dict[str, list[str]] = {}
        total = 0
        for owner_key in self._persona_file_keys(self._persona_public_dir):
            if self._persona_suppressed_owner(owner_key):
                # 隔离中的 owner 不进索引：盘上可能还留着崩溃前没撤掉的公开副本。
                continue
            state = self._persona_public_state(owner_key, force=True)
            document = state.document
            if not isinstance(document, dict) or state.reason is not None:
                continue
            username = document.get("owner_username")
            if not isinstance(username, str) or not username or not document.get("paragraph"):
                continue
            index.setdefault(username, []).append(owner_key)
            total += 1
        self._persona_index = {name: tuple(items) for name, items in index.items()}
        return total

    def _reindex_persona_public(self, owner_key: str, document: Any) -> None:
        """一次 mutation 之后把该 owner 的索引项换成文档的当前事实。"""
        current = document if isinstance(document, dict) else None
        if isinstance(current, dict) and (not current.get("owner_username") or not current.get("paragraph")):
            current = None
        if self._persona_suppressed_owner(owner_key):
            current = None
        self._persona_index = _reindex_persona(self._persona_index, owner_key, current)

    def _persona_public_items(
        self, subjects: tuple[PublicMemorySubject, ...]
    ) -> list[SupplementalItem]:
        """按 subject 次序取公开画像短文并给出 `priority`（设计 §7）。

        形状复查与记忆公开投影同款：只接受形状合法的 subject，同一个 owner 只取第一次出现。
        组间基数排在既有分组之后（公开画像永远不是最优先的资料）。
        """
        if not self._public_dir_readable(self._persona_public_dir):
            self._log_persona_omitted(_SCOPE_PERSONA_PUBLIC, _REASON_IO)
            return []
        ordered: list[tuple[str, str]] = []
        seen: set[str] = set()
        for subject in subjects:
            if not _valid_subject(subject) or subject.owner_key in seen:
                continue
            seen.add(subject.owner_key)
            if self._persona_suppressed_owner(subject.owner_key):
                # 已受理但还没撤回完成的 owner：本轮一条都不引用（设计 §7）。
                self._log_persona_omitted(_SCOPE_PERSONA_PUBLIC, "suppressed")
                continue
            state = self._persona_public_state(subject.owner_key)
            document = state.document
            if not isinstance(document, dict) or not document.get("paragraph"):
                # 冷启动就没有有效快照、或已经撤回：这个 owner 本轮省略。
                self._log_persona_omitted(_SCOPE_PERSONA_PUBLIC, state.reason)
                continue
            paragraph = document.get("paragraph")
            if isinstance(paragraph, str) and paragraph:
                ordered.append((subject.username, paragraph))
        base = _PERSONA_PUBLIC_PRIORITY_BASE * self._priority_stride
        return [
            SupplementalItem(
                group=persona_models.PERSONA_PUBLIC_GROUP,
                label=f"@{username}",
                content=paragraph,
                priority=base + rank,
            )
            for rank, (username, paragraph) in enumerate(ordered)
        ]

    def _persona_file_keys(self, directory: str) -> tuple[str, ...]:
        """画像目录下的 owner key 列表（按文件名排序）；目录不可读返回空元组。

        与公开投影的扫描同款：非法的文件名直接跳过，超过 `MAX_PERSONA_FILES` 时按排序截断
        并只记一个稳定 reason。
        """
        try:
            with os.scandir(directory) as scan:
                names = [entry.name for entry in scan]
        except OSError as exc:
            self._log_persona_scan_failure(_REASON_IO, error=type(exc).__name__)
            return ()
        keys = sorted(
            name[: -len(_MD_SUFFIX)]
            for name in names
            if name.endswith(_MD_SUFFIX) and _valid_owner_key(name[: -len(_MD_SUFFIX)])
        )
        if len(keys) > MAX_PERSONA_FILES:
            self._log_persona_scan_failure(_REASON_TOO_MANY_FILES)
            keys = keys[:MAX_PERSONA_FILES]
        return tuple(keys)

    # --- 用户画像：日志 -----------------------------------------------------

    def _log_persona_write(self, scope: str, status: str, revision: int) -> None:
        """画像写入只记稳定状态与修订号；短文、问题、回答与来源摘要一律不进日志（设计 §8）。"""
        log_event(
            _logger,
            logging.INFO,
            "persona.updated",
            scope=scope,
            status=status,
            revision=revision,
        )

    def _log_persona_omitted(self, scope: str, reason: str | None) -> None:
        log_event(
            _logger,
            logging.INFO,
            "persona.context_omitted",
            scope=scope,
            reason=reason if reason is not None else "unavailable",
        )

    def _log_persona_scan_failure(self, reason: str, **fields: object) -> None:
        """扫描失败只记一条稳定 reason；同一份失败重复出现不再刷屏（与公开投影同款）。"""
        if self._persona_scan_reason == reason:
            return
        self._persona_scan_reason = reason
        log_event(
            _logger,
            logging.WARNING,
            "persona.load_failed",
            scope=_SCOPE_PERSONA,
            reason=reason,
            **fields,
        )

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
            data = self._render_document(view, built)
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
                data = self._render_document(view, built)
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

    def _render_document(self, view: _DocumentView, document: Any) -> bytes:
        """按 view 渲染：画像三类文档自带渲染器，记忆三类走 `_render` 的类型分派。"""
        if view.render is not None:
            return view.render(document)
        return self._render(document)

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
        画像三类文件的 scope 以 `persona_` 开头，事件名据此分到画像那一支，方便运维按子系统
        过滤（字段集合与安全规则完全一致）。
        """
        fields: dict[str, object] = {"scope": scope}
        if reason is not None:
            fields["reason"] = reason
        if error is not None:
            fields["error"] = error
        event = "persona.write_failed" if scope.startswith("persona") else "memory.write_failed"
        log_event(_logger, logging.WARNING, event, **fields)

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


def _persona_key(owner_key: str) -> str:
    """画像文件名里的 owner key：形状不合法时换成同形兜底键（越不出目录，也不含原始取值）。

    与 `public_path_from_owner_key` 同款：路径只由宿主算出的 owner key 派生，伪造的取值落不到
    任何真实文件上。
    """
    return owner_key if _valid_owner_key(owner_key) else _invalid_owner_key_path_key(owner_key)


def _replace_applier(expected: Any, built: dict[str, Any]) -> _ApplyFn:
    """构造一个「基线与预期快照**完全相同**才整份替换」的 applier（画像三类文档共用）。

    CAS 写在 applier 里而不是写在调用点：`_write_document` 接手外部版本之后会用**外部基线**
    重放同一个 applier，检查因此覆盖「磁盘上被人手改过」的那条路径（§5.5 第 4 步）。

    比的是**整份文档的深相等**，不是只比修订号：修订号是宿主自己的记账，一次人工编辑完全可以
    「改了正文、没动 revision」——只比修订号会让那次编辑被静默覆盖，而这正是外部编辑接手机制
    要防的事。深相等只在「磁盘摘要与快照不一致」时才跑到，正常路径一次都不会多花代价。
    """

    def apply(baseline: Any) -> tuple[Any | None, str, str | None]:
        if not isinstance(baseline, dict) or baseline != expected:
            return None, STATUS_CONFLICT, None
        return built, STATUS_OK, None

    return apply


def _merge_fields(
    current: Mapping[str, Any], value: Mapping[str, Any], fields: Sequence[str]
) -> dict[str, Any] | None:
    """把调用方给的字段并到现值上：未知字段返回 None，缺的字段沿用现值。

    「缺的沿用现值」是 A 侧对契约里「A supplies defaults」的落地：宿主只要给它在改的字段，
    不必先读一遍再把整份文档抄回来；文件还不存在时现值就是那份默认文档（基线 revision 0）。
    未知字段仍然整条拒绝 —— 宽容解析会把一次越权或一个拼错的字段名变成静默通过的事件。
    """
    if set(value) - set(fields):
        return None
    merged = dict(current)
    for key, item in value.items():
        merged[key] = copy.deepcopy(item)
    return merged


def _persona_texts(document: Mapping[str, Any]) -> tuple[str, ...]:
    """一份画像文档里**将要写进 Markdown**的全部用户可见文本：密钥筛的取材函数（§37）。

    短文、变更说明、用户名、观察摘要与特征取值都算；证据引用只是标签，也一并过筛 ——
    命中即整条拒绝，命中的具体字符串绝不进日志。
    """
    texts: list[str] = []
    for name in ("paragraph", "last_change", "username"):
        value = document.get(name)
        if isinstance(value, str):
            texts.append(value)
    for item in document.get("observations") or ():
        if isinstance(item, str):
            texts.append(item)
    for feature in document.get("features") or ():
        if isinstance(feature, Mapping):
            value = feature.get("value")
            if isinstance(value, str):
                texts.append(value)
            for ref in feature.get("evidence_refs") or ():
                if isinstance(ref, str):
                    texts.append(ref)
    return tuple(texts)


def _workflow_texts(document: Mapping[str, Any]) -> tuple[str, ...]:
    """一份工作流里将要写进 Markdown 的全部文本：问题、回答、草稿、摘要与修改意见。"""
    texts: list[str] = []
    for name in ("paragraph", "correction"):
        value = document.get(name)
        if isinstance(value, str):
            texts.append(value)
    for name in ("source_summaries", "questions"):
        for item in document.get(name) or ():
            if isinstance(item, str):
                texts.append(item)
    answers = document.get("answers")
    if isinstance(answers, Mapping):
        for item in answers.values():
            if isinstance(item, str):
                texts.append(item)
    for feature in document.get("features") or ():
        if isinstance(feature, Mapping):
            value = feature.get("value")
            if isinstance(value, str):
                texts.append(value)
    return tuple(texts)


def _merge_auto_features(
    current: Any, proposed: Any, *, now_stamp: str
) -> tuple[list[dict[str, Any]], bool] | None:
    """把模型给的新特征与现行文档里的**用户锁定维度**合并（设计 §5 的推断规则）。

    保守冻结是这里的核心：宿主无法判断模型换的说法是不是同一个意思（`_rewording_of` 那种
    字面包含只是启发式），因此只要某个维度里存在用户锁定项，那个维度的**全部**特征一律冻结成
    现行文档里的那几条 —— 不采纳、不合并、不换词：

    - 锁定维度：整维冻结，逐字保留现有特征（含它们自己的时间戳与 `user_locked`）。
    - 其它维度：采纳模型给的内容，但 `user_locked` **强制为假**（模型无权决定锁定标志，
      与「不输出 owner/path/flags」同一条约束），并由宿主盖时间戳：
      `last_seen_at` 一律取本次提交时刻；`first_seen_at` 只在同名 `(dimension, value)` 特征
      已经存在时沿用旧值 —— 否则「首次观察到」会被每次更新推后。
    - 返回值第二项表示**模型是否动过锁定维度**（提案里该维度的特征集合与现行不一致）。
      调用方据此保留上一段短文：模型的新句子可能正是在描述那几条被宿主丢弃的改动，
      宿主没有语义能力把它改回去，只能整段不采纳。
    - 形状不对（不是 dict、字段集合不符）时返回 None，调用方映射成 `invalid_proposal`。
    """
    if not isinstance(proposed, list):
        return None
    for item in proposed:
        if not isinstance(item, dict) or set(item) != set(persona_models.FEATURE_FIELDS):
            return None
    locked_dims = {
        item.get("dimension")
        for item in current or ()
        if isinstance(item, Mapping) and item.get("user_locked")
    }
    existing = {
        (item.get("dimension"), item.get("value")): item
        for item in current or ()
        if isinstance(item, Mapping)
    }
    touched_locked = False
    for dimension in locked_dims:
        proposed_keys = {
            (item["dimension"], item["value"]) for item in proposed if item["dimension"] == dimension
        }
        current_keys = {
            key for key in existing if key[0] == dimension
        }
        if proposed_keys != current_keys:
            touched_locked = True
            break
    merged: list[dict[str, Any]] = []
    for item in proposed:
        if item["dimension"] in locked_dims:
            continue
        prior = existing.get((item["dimension"], item["value"]))
        stamped = dict(item)
        stamped["user_locked"] = False
        # 同名项沿用它的**首次**观察时间；没有同名项、或旧项自己没记过时间时，一律盖提交时刻。
        prior_first = prior.get("first_seen_at") if isinstance(prior, Mapping) else ""
        stamped["first_seen_at"] = prior_first if isinstance(prior_first, str) and prior_first else now_stamp
        stamped["last_seen_at"] = now_stamp
        merged.append(stamped)
    for item in current or ():
        if isinstance(item, Mapping) and item.get("dimension") in locked_dims:
            merged.append(copy.deepcopy(dict(item)))
    return merged, touched_locked


def _persona_fingerprint(document: Mapping[str, Any]) -> str:
    """画像基线指纹，保护同修订号的外部字段修改。"""
    payload = {key: value for key, value in document.items() if key not in ("revision", "operations")}
    try:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=_FINGERPRINT_SEPARATORS,
        ).encode("utf-8")
    except (TypeError, ValueError):
        # 形状不可能通过校验，这里只是兜底：拿不到指纹就返回空串（等价于「没有指纹」）。
        return ""
    return hashlib.sha256(data).hexdigest()


def _reindex_persona(
    index: Mapping[str, tuple[str, ...]], owner_key: str, document: Mapping[str, Any] | None
) -> Mapping[str, tuple[str, ...]]:
    """一次 mutation 之后重建索引的一份新映射（一次引用替换，绝不就地改）。

    `document` 为 None 表示该 owner 现在没有公开画像：把它从所有用户名下摘掉，空掉的名字
    也随之消失（否则索引会留下一串永远取不到内容的用户名）。
    """
    rebuilt: dict[str, list[str]] = {name: list(keys) for name, keys in index.items()}
    for name, keys in rebuilt.items():
        if owner_key in keys:
            rebuilt[name] = [key for key in keys if key != owner_key]
    if document is not None:
        username = document.get("owner_username")
        if isinstance(username, str) and username:
            rebuilt.setdefault(username, []).append(owner_key)
    return {name: tuple(keys) for name, keys in rebuilt.items() if keys}


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
    if isinstance(document, dict):
        # 画像三类文档是纯 dict，没有需要收敛的 ID 宽度：原样返回（render 由各自的 view 提供）。
        return document
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
