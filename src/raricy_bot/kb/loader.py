"""Markdown 知识库的扫描、读取与分块（INTERFACES §23.3 / §23.4）。

`build_snapshot` 是纯同步、确定性的函数，由调用方放进 `asyncio.to_thread` 执行；
相同文件树重复构建必须得到逐字节相同的快照。失败只抛 `KnowledgeBuildError`。

路径安全：不跟随符号链接 / junction / reparse point；任何解析后逃出根目录的项跳过；
快照里只保留相对路径，任何位置都不出现宿主绝对路径。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..config import KnowledgeBaseConfig
from .models import (
    ROOT_CATEGORY,
    KnowledgeBuildError,
    KnowledgeChunk,
    KnowledgeSnapshot,
)

# 标题行：最多三个前导空格，1–6 个 #，后随空白与标题文本；文本前后的 # 会被去掉。
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")

# 代码围栏起始行：最多三个前导空格，三反引号或三波浪线。
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _is_link(entry: os.DirEntry[str]) -> bool:
    """判断目录项是否为符号链接、junction 或 reparse point。"""
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction())
    except OSError:
        return True


def _is_within_root(path: Path, root: Path) -> bool:
    """判断解析后的路径是否仍在根目录内（含根目录本身）。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _category(relative_path: str) -> str:
    """从相对路径取分类：一级目录名；根目录文件为 `_root`。"""
    head, sep, _ = relative_path.partition("/")
    return ROOT_CATEGORY if not sep else head


def _collect(
    root: Path, cfg: KnowledgeBaseConfig
) -> tuple[list[tuple[Path, str]], int, set[str]]:
    """递归枚举候选 Markdown 文件。

    返回 `(按相对路径排序的候选, 跳过项数量, 跳过原因集合)`。隐藏文件与隐藏目录
    静默跳过，非 `.md` 静默忽略，两者都不计入跳过数量。
    """
    candidates: list[tuple[Path, str]] = []
    skipped = 0
    reasons: set[str] = set()

    def visit(dir_path: Path, rel_parts: list[str]) -> None:
        nonlocal skipped
        try:
            scan = os.scandir(dir_path)
        except OSError:
            if not rel_parts:
                # 根目录本身不可读：交由调用方映射成 root_unreadable。
                raise
            # 子目录不可读时跳过整棵子树；没有对应的稳定原因，静默处理。
            return
        with scan:
            for entry in scan:
                name = entry.name
                if name.startswith("."):
                    continue
                if _is_link(entry):
                    skipped += 1
                    reasons.add("symlink")
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    skipped += 1
                    reasons.add("read_failed")
                    continue
                full = Path(entry.path)
                if is_dir:
                    try:
                        real = full.resolve()
                    except OSError:
                        skipped += 1
                        reasons.add("read_failed")
                        continue
                    if not _is_within_root(real, root):
                        skipped += 1
                        reasons.add("escaped_root")
                        continue
                    visit(full, rel_parts + [name])
                elif is_file:
                    if not name.lower().endswith(".md"):
                        continue
                    try:
                        real = full.resolve()
                    except OSError:
                        skipped += 1
                        reasons.add("read_failed")
                        continue
                    if not _is_within_root(real, root):
                        skipped += 1
                        reasons.add("escaped_root")
                        continue
                    candidates.append((full, "/".join(rel_parts + [name])))

    visit(root, [])
    candidates.sort(key=lambda item: item[1])
    return candidates, skipped, reasons


def _read_file(
    path: Path, cfg: KnowledgeBaseConfig
) -> tuple[str | None, str | None, int]:
    """读取并严格解码一个文件，返回 `(正文, 跳过原因, 字节数)`。

    打开前后各查一次大小（P1-10）：大小不一致视为文件被替换，跳过该文件。
    """
    try:
        before = path.stat()
    except OSError:
        return None, "read_failed", 0
    if before.st_size > cfg.max_file_bytes:
        return None, "too_large", 0
    try:
        data = path.read_bytes()
    except OSError:
        return None, "read_failed", 0
    try:
        after = path.stat()
    except OSError:
        return None, "read_failed", 0
    if after.st_size != before.st_size or len(data) != after.st_size:
        return None, "replaced", 0
    try:
        # utf-8-sig 严格解码：去掉 BOM，且不猜测本地编码。
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "not_utf8", 0
    return text, None, len(data)


def _strip_front_matter(text: str) -> str:
    """丢弃文件开头完整的 `---` front matter 块；不完整则原样保留。"""
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :])
    return text


def _heading_path(stack: list[tuple[int, str]]) -> str:
    """把标题栈拼成 "H1 > H2" 形式。"""
    return " > ".join(title for _, title in stack)


def _blocks(lines: list[str]) -> list[tuple[str, str]]:
    """把行切成块，返回 `(heading_path, 块文本)` 列表。

    标题行只更新标题栈、不产生正文块；空行是块边界；fenced code block 内部
    （含空行）整块收集，不被空行拆开。
    """
    blocks: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    total = len(lines)
    index = 0
    while index < total:
        line = lines[index]
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            char = marker[0]
            length = len(marker)
            closer = re.compile(r"^ {0,3}" + re.escape(char) + "{%d,}[ \t]*$" % length)
            end = index + 1
            while end < total and not closer.match(lines[end]):
                end += 1
            last = min(end, total - 1)
            blocks.append((_heading_path(stack), "\n".join(lines[index : last + 1])))
            index = last + 1
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            del stack[level - 1 :]
            stack.append((level, title))
            index += 1
            continue
        if not line.strip():
            index += 1
            continue
        end = index
        paragraph: list[str] = []
        while (
            end < total
            and lines[end].strip()
            and not _HEADING_RE.match(lines[end])
            and not _FENCE_RE.match(lines[end])
        ):
            paragraph.append(lines[end])
            end += 1
        blocks.append((_heading_path(stack), "\n".join(paragraph)))
        index = end
    return blocks


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """按字符硬切文本，相邻片段保留 `overlap` 个字符的重叠。"""
    if size <= 0 or len(text) <= size:
        return [text]
    step = size - overlap
    if step <= 0:
        step = size
    pieces: list[str] = []
    start = 0
    while start < len(text):
        pieces.append(text[start : start + size])
        if start + size >= len(text):
            break
        start += step
    return pieces


def _pack(
    blocks: list[tuple[str, str]], chunk_chars: int, overlap: int
) -> list[tuple[str, str]]:
    """把块装进 `(heading_path, content)` 分块列表。

    标题变化、空行边界与超长都会切块；单块超过 `chunk_chars` 时硬切并保留重叠。
    只含空白的块不产出。
    """
    out: list[tuple[str, str]] = []
    parts: list[str] = []
    current_heading: str = ""
    current_len = 0

    def flush() -> None:
        nonlocal parts, current_len, current_heading
        if parts:
            content = "\n\n".join(parts).strip()
            if content.strip():
                out.append((current_heading, content))
        parts = []
        current_len = 0
        current_heading = ""

    for heading, text in blocks:
        if len(text) > chunk_chars:
            flush()
            for piece in _hard_split(text, chunk_chars, overlap):
                if piece.strip():
                    out.append((heading, piece))
            continue
        if parts and heading != current_heading:
            flush()
        if parts and current_len + 2 + len(text) > chunk_chars:
            flush()
        if not parts:
            current_heading = heading
        parts.append(text)
        current_len += (2 if len(parts) > 1 else 0) + len(text)
    flush()
    return out


def _chunk_document(
    text: str, cfg: KnowledgeBaseConfig
) -> list[tuple[str, str]]:
    """把一份 Markdown 正文切成 `(heading_path, content)` 序列。"""
    cleaned = _strip_front_matter(text)
    if not cleaned.strip():
        return []
    return _pack(_blocks(cleaned.split("\n")), cfg.chunk_chars, cfg.chunk_overlap_chars)


def build_snapshot(
    root: str, cfg: KnowledgeBaseConfig, *, version: int
) -> KnowledgeSnapshot:
    """同步、确定性地扫描 `root` 并构建一份不可变快照。

    失败时抛 `KnowledgeBuildError`，`reason` 是合同列出的五个稳定原因之一。
    """
    try:
        resolved = Path(root).resolve()
        exists = resolved.exists()
    except OSError:
        raise KnowledgeBuildError("root_unreadable") from None
    if not exists:
        raise KnowledgeBuildError("root_missing")
    if not resolved.is_dir():
        raise KnowledgeBuildError("root_unreadable")
    try:
        candidates, skipped, reasons = _collect(resolved, cfg)
    except OSError:
        raise KnowledgeBuildError("root_unreadable") from None

    if len(candidates) > cfg.max_files:
        raise KnowledgeBuildError("too_many_files")

    chunks: list[KnowledgeChunk] = []
    document_count = 0
    total_bytes = 0
    for path, relative_path in candidates:
        text, reason, byte_count = _read_file(path, cfg)
        if reason is not None:
            skipped += 1
            reasons.add(reason)
            continue
        assert text is not None
        total_bytes += byte_count
        if total_bytes > cfg.max_total_bytes:
            raise KnowledgeBuildError("total_too_large")
        pieces = _chunk_document(text, cfg)
        if not pieces:
            continue
        document_count += 1
        category = _category(relative_path)
        for ordinal, (heading_path, content) in enumerate(pieces):
            chunks.append(
                KnowledgeChunk(
                    category=category,
                    relative_path=relative_path,
                    heading_path=heading_path,
                    ordinal=ordinal,
                    content=content,
                )
            )

    if not chunks:
        raise KnowledgeBuildError("empty")

    return KnowledgeSnapshot(
        version=version,
        chunks=tuple(chunks),
        document_count=document_count,
        total_bytes=total_bytes,
        skipped_files=skipped,
        skip_reasons=tuple(sorted(reasons)),
    )


__all__ = ["build_snapshot"]
