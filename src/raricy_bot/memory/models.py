"""长期记忆的数据模型（INTERFACES §27）。

纯类型底座：无 I/O、无网络、不 import `app.py`（D-61）。`MemoryContext.items` 用
`core/context.py` 的 `SupplementalItem`，依赖方向单向（memory → core），不构成循环。

用户可预期的失败一律映射成 `STATUS_*` 稳定状态返回（§27.4）；异常只用于编程错误与被取消，
例如 `MemoryTarget` 的作用域与 `owner_key` 不匹配。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

# 仅为让 `MemoryContext.items` 的字符串前向引用在运行期可解析；core/context.py 不依赖本包。
from ..core.context import SupplementalItem


class MemoryScope(StrEnum):
    """记忆作用域：全站共同、大区共同、用户私有。"""

    ALL_USER = "all_user"
    LOBBY = "lobby"
    USER = "user"


class ProposalAction(StrEnum):
    """撰写器可提出的动作（INTERFACES §27.1）。

    `delete` 不在其中：删除只由用户命令或管理员动作触发（D-57）。
    """

    ADD = "add"
    UPDATE = "update"
    NOOP = "noop"


# 稳定状态字符串（INTERFACES §27.4）：值逐字固定，不得新增、改写或按用途重命名。
# 用户可预期的失败一律映射成这里的某个值返回，不用异常传递。
STATUS_OK: str = "ok"
STATUS_NOOP: str = "noop"
STATUS_DUPLICATE: str = "duplicate"
STATUS_NOT_FOUND: str = "not_found"
STATUS_FORBIDDEN: str = "forbidden"
STATUS_UNAVAILABLE: str = "unavailable"
STATUS_INVALID_PROPOSAL: str = "invalid_proposal"
STATUS_CONFLICT: str = "conflict"
STATUS_FULL: str = "full"
STATUS_SECRET_DETECTED: str = "secret_detected"
# 第十一个稳定状态（INTERFACES §40.3）：AI 撰写或自动提取试图更新一条**仍然公开**的来源条目。
# 它与 `conflict`（磁盘摘要与内存快照不一致）不是一回事：静默改写会扩大用户批准过的授权范围。
STATUS_PUBLIC_CONFLICT: str = "public_conflict"


@dataclass(frozen=True)
class MemoryEntry:
    """一条已生效的记忆；`content` 是 Markdown 里的正文原文。"""

    memory_id: str
    key: str
    content: str
    pinned: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryCandidate:
    """一条待管理员批准的共同记忆候选；作用域只能是 `all_user` / `lobby`。

    候选处于未生效状态，绝不进入任何普通模型请求（D-58）。
    """

    candidate_id: str
    scope: MemoryScope
    action: ProposalAction  # add / update
    target_id: str | None
    key: str
    content: str
    created_at: str


@dataclass(frozen=True)
class MemoryProposal:
    """AI 撰写器给出的提案；作用域、owner 与路径都不在这里（D-57）。"""

    action: ProposalAction
    target_id: str | None
    key: str
    content: str
    confidence: float


@dataclass(frozen=True)
class MemoryProposalResult:
    """一次撰写的结果：状态 + 提案（失败时提案为 None）。"""

    status: str
    proposal: MemoryProposal | None


@dataclass(frozen=True)
class OperationResult:
    """一次存储操作的结果：状态、受影响对象 ID 与操作后的修订号。"""

    status: str
    object_id: str | None
    revision: int


@dataclass(frozen=True)
class MemoryContext:
    """一次上下文读取的结果：共同/私有快照修订号与选入条目。

    `private_revision` 为 None 表示本次没有读私有快照（大区、评论，或用户没有私有记忆）。
    """

    common_revision: int
    private_revision: int | None
    items: tuple["SupplementalItem", ...]


@dataclass(frozen=True)
class MemoryCaptureResult:
    """自动提取（§32.3 的 `auto_capture`）的结果（D-67）。

    只有确实写入成功时 `status == STATUS_OK`；未写入时 `action` 取 `.NOOP`。
    `content` 是成功写入的正文原文，其余情况为空串。
    """

    status: str
    memory_id: str | None
    content: str
    action: ProposalAction


@dataclass(frozen=True)
class AutoCaptureToken:
    """一次自动提取的**进程内**不透明令牌（修复计划 §2.3 第 1、5 条）。

    `MemoryService.begin_auto_capture` 在同一把写锁内一并取得用户的授权状态、条目快照与该
    用户的提取代次；随后调用方把模型调用放在锁**之外**，模型返回后再由
    `MemoryService.commit_auto_capture` 在写锁内复核同一份授权仍然成立，才应用提案。

    - `entries`：开始提取时的私有条目快照，只用于给撰写器提供上下文；提交仍以磁盘/快照的
      **最新**版本为基线（外部编辑照常被采纳，§30.4）。
    - `generation`：该用户当前的提取代次。隐私操作（关闭自动提取、关闭读取、清空、删除）
      推进它，使此前取得的所有令牌失效。
    - `epoch`：服务级纪元。`MemoryService.stop()` 递增它，使服务停止前发出的令牌不再被接受。
      代次只保存在进程内、不落任何持久字段：进程重启后没有旧模型调用会继续返回，因此不需要
      持久化它（计划 §2.3 第 5 条）。
    """

    user_id: str
    generation: int
    epoch: int
    entries: tuple[MemoryEntry, ...]


@dataclass(frozen=True)
class PublicMemoryEntry:
    """一条公开个人记忆条目（INTERFACES §40.1）。

    它是私人条目在**发布那一刻**的显式快照，不是动态引用：`source_created_at` /
    `source_updated_at` 原样复制来源条目的 `created_at` / `updated_at`，来源条目之后的变化不会
    反映到这里。`memory_id` 沿用来源的 `UM-` ID，重复公开同一条目保持原 ID 与原快照。
    """

    memory_id: str
    key: str
    content: str
    pinned: bool
    source_created_at: str
    source_updated_at: str
    published_at: str


@dataclass(frozen=True)
class PublicMemoryDocument:
    """一个 owner 的公开投影文件（INTERFACES §40.1）。

    `owner_username` 必须满足站点用户名合同（§40.2 第 2 条）；`operations` 与私有文件同款，
    键是宿主的 `operation_id`，值是 `OperationResult`。
    """

    schema_version: int = 1
    revision: int = 0
    owner_username: str = ""
    operations: Mapping[str, OperationResult] = field(default_factory=dict)
    entries: tuple[PublicMemoryEntry, ...] = ()


@dataclass(frozen=True)
class PublicMemorySubject:
    """本轮选入的一个公开投影所有者（INTERFACES §40.2）。

    `source_priority` 只表达**本轮的选择顺序**（越小越优先），不落盘、不进日志。
    只包含不可逆的 `owner_key` 与已满足站点用户名合同的 `username`：没有原始 user ID、没有正文、
    没有来源文本；它是**宿主计算**的结果，模型不参与身份决策。
    """

    owner_key: str
    username: str
    source_priority: int


@dataclass(frozen=True)
class MemoryTarget:
    """一次操作的作用域目标（INTERFACES §27.3）。

    `all_user` / `lobby` 的 `owner_key` 必须为 None；`user` 的必须由宿主用
    `user_storage_key(author.id)` 算好。AI 输出与用户命令都不能提供 `owner_key`（D-56 / D-57）。
    """

    scope: MemoryScope
    owner_key: str | None = None

    def __post_init__(self) -> None:
        # 作用域与 owner_key 不匹配是编程错误，不是用户可预期失败，因此抛异常而不是返回状态。
        if not isinstance(self.scope, MemoryScope):
            # 先验作用域本身：下游一律用 `== MemoryScope.X` 判定作用域，一个拼错的字符串
            # 会让「私有」落进共同作用域分支，把本该私有的内容按共享处理。
            # 只认枚举成员：值字符串虽然比较相等，但不是这里的合同类型。
            raise ValueError("MemoryTarget 的 scope 必须是 MemoryScope 成员")
        if self.scope == MemoryScope.USER:
            if not isinstance(self.owner_key, str) or not self.owner_key:
                raise ValueError("user 作用域的 MemoryTarget 必须带非空 owner_key")
        elif self.owner_key is not None:
            raise ValueError("all_user / lobby 作用域的 MemoryTarget 不能带 owner_key")


def user_storage_key(user_id: str) -> str:
    """用户存储键：固定域分隔前缀后的 SHA-256 十六进制摘要（INTERFACES §27.3）。

    `"raricy-memory-v1\\0"` 是**代码常量**，不得由配置改。该哈希只避免原始 ID 出现在文件名里，
    **不构成加密**：用户 Markdown 一律按敏感数据保护。
    """
    raw = f"raricy-memory-v1\0{user_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
