"""知识库的不可变数据类型。

字段、默认值与稳定字符串逐字对应 `docs/design/INTERFACES.md` §23.2。
本模块不做任何 I/O，也不引用宿主路径：快照里只保留 POSIX 风格相对路径。
"""

from __future__ import annotations

from dataclasses import dataclass

# 根目录直接放置的文件所属的保留分类名（D-42）。
ROOT_CATEGORY: str = "_root"

# 允许出现在 `KnowledgeSnapshot.skip_reasons` 里的稳定原因。
SKIP_REASONS: frozenset[str] = frozenset(
    {"not_utf8", "too_large", "read_failed", "symlink", "escaped_root", "replaced"}
)

# 整次构建失败时允许出现的稳定原因。
BUILD_ERROR_REASONS: frozenset[str] = frozenset(
    {"root_missing", "root_unreadable", "too_many_files", "total_too_large", "empty"}
)


@dataclass(frozen=True)
class KnowledgeChunk:
    """一个可检索的正文块；绝不包含宿主绝对路径。"""

    category: str          # 一级目录名；根目录文件为 "_root"
    relative_path: str     # POSIX 风格相对路径
    heading_path: str      # "H1 > H2"；无标题层级时为 ""
    ordinal: int           # 该文档内的块序号，从 0 开始
    content: str


@dataclass(frozen=True)
class KnowledgeHit:
    """一次检索的命中：块与分数。"""

    chunk: KnowledgeChunk
    score: float


@dataclass(frozen=True)
class KnowledgeSnapshot:
    """一次完整构建的不可变结果；版本号从 1 开始逐次递增。"""

    version: int
    chunks: tuple[KnowledgeChunk, ...]  # 按 (relative_path, ordinal) 稳定升序
    document_count: int
    total_bytes: int                    # 读取并解码成功的字节数
    skipped_files: int
    skip_reasons: tuple[str, ...]       # 去重排序的稳定原因，不含路径

    @property
    def chunk_count(self) -> int:
        """块总数。"""
        return len(self.chunks)

    @property
    def empty(self) -> bool:
        """没有任何块时为空。"""
        return not self.chunks


class KnowledgeBuildError(Exception):
    """整次构建失败；`reason` 只能是合同列出的稳定原因之一。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
