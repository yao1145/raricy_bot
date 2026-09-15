"""长期记忆内核（全局记忆 Beta，INTERFACES §26 … §37）。

长期记忆是**软故障**能力：读取、撰写与写入的任何失败都不影响聊天、评论与健康端点；
默认关闭，`enabled=false` 时不建目录、不读文件、不注入任何能力，行为与升级前逐字节一致（D-60）。

正文只落 Markdown：记忆内容只允许出现在目标 Markdown 文件、允许的 `role="user"` 模型请求，
以及面向所属用户的明确展示；绝不进入日志、SQLite、system prompt 或文件名（§37）。

本包不 import `app.py`，装配全部留在 `app.py`（D-61）。这里先导出类型底座与 Beta 门禁的公开名，
后续任务的模块在这里继续补。
"""

from __future__ import annotations

from .access import MemoryAccessPolicy
from .models import (
    STATUS_CONFLICT,
    STATUS_DUPLICATE,
    STATUS_FORBIDDEN,
    STATUS_FULL,
    STATUS_INVALID_PROPOSAL,
    STATUS_NOT_FOUND,
    STATUS_NOOP,
    STATUS_OK,
    STATUS_SECRET_DETECTED,
    STATUS_UNAVAILABLE,
    MemoryCandidate,
    MemoryCaptureResult,
    MemoryContext,
    MemoryEntry,
    MemoryProposal,
    MemoryProposalResult,
    MemoryScope,
    MemoryTarget,
    OperationResult,
    ProposalAction,
    user_storage_key,
)

__all__ = [
    "STATUS_CONFLICT",
    "STATUS_DUPLICATE",
    "STATUS_FORBIDDEN",
    "STATUS_FULL",
    "STATUS_INVALID_PROPOSAL",
    "STATUS_NOT_FOUND",
    "STATUS_NOOP",
    "STATUS_OK",
    "STATUS_SECRET_DETECTED",
    "STATUS_UNAVAILABLE",
    "MemoryAccessPolicy",
    "MemoryCandidate",
    "MemoryCaptureResult",
    "MemoryContext",
    "MemoryEntry",
    "MemoryProposal",
    "MemoryProposalResult",
    "MemoryScope",
    "MemoryTarget",
    "OperationResult",
    "ProposalAction",
    "user_storage_key",
]
