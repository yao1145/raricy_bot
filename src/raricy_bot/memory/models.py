"""长期记忆的数据模型（INTERFACES §27）。

纯类型底座：无 I/O、无网络、不 import `app.py`（D-61）。`MemoryContext.items` 用
`core/context.py` 的 `SupplementalItem`，依赖方向单向（memory → core），不构成循环。

用户可预期的失败一律映射成 `STATUS_*` 稳定状态返回（§27.4）；异常只用于编程错误与被取消，
例如 `MemoryTarget` 的作用域与 `owner_key` 不匹配。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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
