"""本地 Markdown 知识库内核（INTERFACES §23）。

本地、只读、可重建的产品能力，**不属于 MCP**，不触网：扫描只读目录并做纯标准库的
词法检索，把带 `[KBn]` 标签的资料块交给调用方拼进当前轮的 user 消息。
"""

from __future__ import annotations

from .index import KnowledgeIndex
from .loader import build_snapshot
from .models import (
    BUILD_ERROR_REASONS,
    ROOT_CATEGORY,
    SKIP_REASONS,
    KnowledgeBuildError,
    KnowledgeChunk,
    KnowledgeHit,
    KnowledgeSnapshot,
)
from .service import (
    KB_HEADER,
    STATUS_DISABLED,
    STATUS_NO_RESULTS,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    KnowledgeResult,
    KnowledgeService,
    format_hits,
)

__all__ = [
    "BUILD_ERROR_REASONS",
    "KB_HEADER",
    "KnowledgeBuildError",
    "KnowledgeChunk",
    "KnowledgeHit",
    "KnowledgeIndex",
    "KnowledgeResult",
    "KnowledgeService",
    "KnowledgeSnapshot",
    "ROOT_CATEGORY",
    "SKIP_REASONS",
    "STATUS_DISABLED",
    "STATUS_NO_RESULTS",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "build_snapshot",
    "format_hits",
]
