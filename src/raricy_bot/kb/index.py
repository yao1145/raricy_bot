"""词法检索索引：纯标准库的 BM25 风格评分（INTERFACES §23.5）。

拉丁字母/数字连续串 `casefold()` 后作为词项；CJK 同时生成单字与相邻双字词项。
正文用 BM25（`k1=1.5`、`b=0.75`）；分类、相对路径、文档标题、`heading_path`
命中用固定小幅加权。不引入任何第三方依赖。
"""

from __future__ import annotations

import math
from collections import Counter

from .models import KnowledgeChunk, KnowledgeHit, KnowledgeSnapshot

# BM25 参数。
K1: float = 1.5
B: float = 0.75

# 元数据字段命中一次的固定小幅加权。
CATEGORY_BOOST: float = 1.5
PATH_BOOST: float = 1.0
TITLE_BOOST: float = 0.8
HEADING_BOOST: float = 0.6

# 最高分不超过该阈值即视为无命中，不把整库塞给模型。
MIN_SCORE: float = 0.5

# 按 1 个词项计数（单字 + 相邻双字）的区段。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
)


def _is_cjk(ch: str) -> bool:
    """判断单个字符是否按 CJK 处理。"""
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _tokenize(text: str) -> list[str]:
    """把文本切成检索词项序列（不去重，保留词频）。

    拉丁字母/数字连续串整体作为一个词项（`casefold`）；CJK 字符生成单字词项，
    并与其后的 CJK 字符组成相邻双字词项。其余字符只是分隔符。
    """
    lowered = text.casefold()
    terms: list[str] = []
    total = len(lowered)
    index = 0
    while index < total:
        ch = lowered[index]
        if _is_cjk(ch):
            terms.append(ch)
            if index + 1 < total and _is_cjk(lowered[index + 1]):
                terms.append(lowered[index : index + 2])
            index += 1
            continue
        if ch.isalnum():
            end = index
            while end < total and not _is_cjk(lowered[end]) and lowered[end].isalnum():
                end += 1
            terms.append(lowered[index:end])
            index = end
            continue
        index += 1
    return terms


def _stem(relative_path: str) -> str:
    """取相对路径最后一段并去掉 `.md` 扩展名。"""
    name = relative_path.rsplit("/", 1)[-1]
    if name.lower().endswith(".md"):
        return name[:-3]
    return name


def _document_titles(chunks: tuple[KnowledgeChunk, ...]) -> dict[str, str]:
    """按文档标题建规范化键：首个非空 `heading_path` 的第一段，否则文件名。

    `KnowledgeChunk` 不携带独立标题字段，标题信息因此在索引侧从 `heading_path`
    与文件名重建（INTERFACES §23.4：第一个 H1 是文档标题，没有 H1 时用文件名）。
    """
    titles: dict[str, str] = {}
    for chunk in chunks:
        if chunk.relative_path in titles or not chunk.heading_path:
            continue
        titles[chunk.relative_path] = chunk.heading_path.split(" > ", 1)[0]
    for chunk in chunks:
        titles.setdefault(chunk.relative_path, _stem(chunk.relative_path))
    return titles


class KnowledgeIndex:
    """`KnowledgeSnapshot` 上的不可变词法索引。"""

    def __init__(
        self,
        chunks: tuple[KnowledgeChunk, ...],
        doc_lengths: tuple[int, ...],
        avgdl: float,
        document_freq: dict[str, int],
        postings: dict[str, dict[int, int]],
        category_terms: dict[str, tuple[int, ...]],
        path_terms: dict[str, tuple[int, ...]],
        title_terms: dict[str, tuple[int, ...]],
        heading_terms: dict[str, tuple[int, ...]],
    ) -> None:
        self._chunks = chunks
        self._doc_lengths = doc_lengths
        self._avgdl = avgdl
        self._document_freq = document_freq
        self._postings = postings
        self._category_terms = category_terms
        self._path_terms = path_terms
        self._title_terms = title_terms
        self._heading_terms = heading_terms

    @classmethod
    def build(cls, snapshot: KnowledgeSnapshot) -> KnowledgeIndex:
        """从快照构建索引；空快照得到空索引。"""
        chunks = snapshot.chunks
        count = len(chunks)
        titles = _document_titles(chunks)
        document_freq: dict[str, int] = {}
        postings: dict[str, dict[int, int]] = {}
        doc_lengths: list[int] = []
        category_terms: dict[str, list[int]] = {}
        path_terms: dict[str, list[int]] = {}
        title_terms: dict[str, list[int]] = {}
        heading_terms: dict[str, list[int]] = {}

        def add_meta(bucket: dict[str, list[int]], text: str, position: int) -> None:
            for term in set(_tokenize(text)):
                bucket.setdefault(term, []).append(position)

        for position, chunk in enumerate(chunks):
            term_counts = Counter(_tokenize(chunk.content))
            doc_lengths.append(sum(term_counts.values()))
            for term, freq in term_counts.items():
                postings.setdefault(term, {})[position] = freq
                document_freq[term] = document_freq.get(term, 0) + 1
            add_meta(category_terms, chunk.category, position)
            add_meta(path_terms, chunk.relative_path, position)
            add_meta(title_terms, titles[chunk.relative_path], position)
            add_meta(heading_terms, chunk.heading_path, position)

        avgdl = (sum(doc_lengths) / count) if count else 0.0

        def freeze(bucket: dict[str, list[int]]) -> dict[str, tuple[int, ...]]:
            return {key: tuple(value) for key, value in bucket.items()}

        return cls(
            chunks=chunks,
            doc_lengths=tuple(doc_lengths),
            avgdl=avgdl,
            document_freq=document_freq,
            postings=postings,
            category_terms=freeze(category_terms),
            path_terms=freeze(path_terms),
            title_terms=freeze(title_terms),
            heading_terms=freeze(heading_terms),
        )

    def search(self, query: str, *, top_k: int) -> tuple[KnowledgeHit, ...]:
        """检索：返回不超过 `top_k` 个命中，同一文件最多 2 块。"""
        if top_k <= 0 or not self._chunks:
            return ()
        query_terms = _tokenize(query)
        if not query_terms:
            return ()
        unique: list[str] = []
        seen: set[str] = set()
        for term in query_terms:
            if term not in seen:
                seen.add(term)
                unique.append(term)

        count = len(self._chunks)
        scores: dict[int, float] = {}

        def add(position: int, value: float) -> None:
            scores[position] = scores.get(position, 0.0) + value

        for term in unique:
            postings = self._postings.get(term)
            if postings is not None and self._avgdl > 0:
                freq = self._document_freq[term]
                idf = math.log(1.0 + (count - freq + 0.5) / (freq + 0.5))
                for position, term_freq in postings.items():
                    length = self._doc_lengths[position]
                    denominator = term_freq + K1 * (
                        1.0 - B + B * length / self._avgdl
                    )
                    add(position, idf * term_freq * (K1 + 1.0) / denominator)
            for position in self._category_terms.get(term, ()):
                add(position, CATEGORY_BOOST)
            for position in self._path_terms.get(term, ()):
                add(position, PATH_BOOST)
            for position in self._title_terms.get(term, ()):
                add(position, TITLE_BOOST)
            for position in self._heading_terms.get(term, ()):
                add(position, HEADING_BOOST)

        if not scores or max(scores.values()) <= MIN_SCORE:
            return ()

        ranked = [
            (position, score) for position, score in scores.items() if score > MIN_SCORE
        ]
        ranked.sort(
            key=lambda item: (
                -item[1],
                self._chunks[item[0]].relative_path,
                self._chunks[item[0]].ordinal,
            )
        )

        hits: list[KnowledgeHit] = []
        per_file: Counter[str] = Counter()
        for position, score in ranked:
            chunk = self._chunks[position]
            if per_file[chunk.relative_path] >= 2:
                continue
            per_file[chunk.relative_path] += 1
            hits.append(KnowledgeHit(chunk=chunk, score=score))
            if len(hits) >= top_k:
                break
        return tuple(hits)


__all__ = [
    "KnowledgeIndex",
    "MIN_SCORE",
    "K1",
    "B",
    "CATEGORY_BOOST",
    "PATH_BOOST",
    "TITLE_BOOST",
    "HEADING_BOOST",
]
