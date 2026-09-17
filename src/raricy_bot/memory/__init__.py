"""长期记忆内核（全局记忆 Beta，INTERFACES §26 … §37）。

长期记忆是**软故障**能力：读取、撰写与写入的任何失败都不影响聊天、评论与健康端点；
默认关闭，`enabled=false` 时不建目录、不读文件、不注入任何能力，行为与升级前逐字节一致（D-60）。

正文只落 Markdown：记忆内容只允许出现在目标 Markdown 文件、允许的 `role="user"` 模型请求，
以及面向所属用户的明确展示；绝不进入日志、SQLite、system prompt 或文件名（§37）。

本包不 import `app.py`，装配全部留在 `app.py`（D-61）。这里先导出类型底座与 Beta 门禁的公开名，
后续任务的模块在这里继续补。

公开个人记忆（第三类记忆，§39 … §45）在本包里的底座是 `codec` 的 `parse_public` / `render_public`
与 `models` 的 `PublicMemoryEntry` / `PublicMemoryDocument` / `PublicMemorySubject`。
"""

from __future__ import annotations

from .access import MemoryAccessPolicy
from .codec import (
    CODEC_REASONS,
    CodecError,
    CodecReason,
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
    STATUS_DUPLICATE,
    STATUS_FORBIDDEN,
    STATUS_FULL,
    STATUS_INVALID_PROPOSAL,
    STATUS_NOT_FOUND,
    STATUS_NOOP,
    STATUS_OK,
    STATUS_PUBLIC_CONFLICT,
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
    PublicMemoryDocument,
    PublicMemoryEntry,
    PublicMemorySubject,
    user_storage_key,
)
from .commands import (
    USAGE_COMMAND,
    MemoryCommand,
    MemoryCommandRequest,
    MemoryCommandResult,
    parse_memory_command,
)
from .controller import MemoryController
from .service import MemoryService, PrivateSettings
from .writer import MemoryModel, MemoryWriter

__all__ = [
    "CODEC_REASONS",
    "USAGE_COMMAND",
    "STATUS_CONFLICT",
    "STATUS_DUPLICATE",
    "STATUS_FORBIDDEN",
    "STATUS_FULL",
    "STATUS_INVALID_PROPOSAL",
    "STATUS_NOT_FOUND",
    "STATUS_NOOP",
    "STATUS_OK",
    "STATUS_PUBLIC_CONFLICT",
    "STATUS_SECRET_DETECTED",
    "STATUS_UNAVAILABLE",
    "CodecError",
    "CodecReason",
    "CommonDocument",
    "MemoryAccessPolicy",
    "MemoryCandidate",
    "MemoryCaptureResult",
    "MemoryCommand",
    "MemoryCommandRequest",
    "MemoryCommandResult",
    "MemoryContext",
    "MemoryController",
    "MemoryEntry",
    "MemoryModel",
    "MemoryProposal",
    "MemoryProposalResult",
    "MemoryScope",
    "MemoryService",
    "MemoryTarget",
    "MemoryWriter",
    "OperationResult",
    "PrivateDocument",
    "PrivateSettings",
    "ProposalAction",
    "PublicMemoryDocument",
    "PublicMemoryEntry",
    "PublicMemorySubject",
    "parse_common",
    "parse_memory_command",
    "parse_private",
    "parse_public",
    "render_common",
    "render_private",
    "render_public",
    "user_storage_key",
]
