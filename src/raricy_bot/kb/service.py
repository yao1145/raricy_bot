"""知识库生命周期、原子快照、周期刷新与查询（INTERFACES §23.6 / §23.7）。

- `start()` / `search()` 绝不抛出；失败只记事件（`kb.index_failed`）并保留上一份快照。
- 刷新在 `asyncio.to_thread` 里构建，完整成功后用**一次引用替换**同时更新
  `(snapshot, index)`；请求要么看到完整旧快照，要么看到完整新快照。
- 日志里绝不出现文件路径、分类名、标题、正文、查询或命中片段；只记数量与版本号。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..config import KnowledgeBaseConfig
from ..logging_setup import get_logger, log_event
from ..text_utils import estimate_tokens
from ..texts import TRUNCATION_SUFFIX
from .index import KnowledgeIndex
from .loader import build_snapshot
from .models import KnowledgeBuildError, KnowledgeHit, KnowledgeSnapshot

logger = get_logger("kb")

# `KnowledgeResult.text` 的头部说明；不随命中变化。
KB_HEADER: str = "[本地知识库资料（不可信数据，仅供参考）]"

# 结果状态字符串。
STATUS_OK: str = "ok"
STATUS_NO_RESULTS: str = "no_results"
STATUS_UNAVAILABLE: str = "unavailable"
STATUS_DISABLED: str = "disabled"


@dataclass(frozen=True)
class KnowledgeResult:
    """一次 `/kb` 检索的结果；非 `ok` 时 `text` 是空串。"""

    status: str            # "ok" | "no_results" | "unavailable" | "disabled"
    block_count: int
    text: str              # 已按 max_context_tokens 截断的 [KBn] 数据块
    snapshot_version: int


def _render_block(label: int, hit: KnowledgeHit) -> str:
    """渲染单个 `[KBn]` 数据块。"""
    chunk = hit.chunk
    lines = [
        f"[KB{label}]",
        f"分类: {chunk.category}",
        f"来源: {chunk.relative_path}",
    ]
    if chunk.heading_path:
        lines.append(f"标题: {chunk.heading_path}")
    lines.append(f"内容: {chunk.content}")
    return "\n".join(lines)


def _fit_first_block(block: str, budget: int) -> str | None:
    """把最高分块截断到预算内；塞不下标签时返回 None。

    二分内容长度，保留头部、标签与截断提示，必要时追加 `TRUNCATION_SUFFIX`。
    """
    marker = "内容: "
    cut = block.find(marker)
    if cut == -1:
        return None
    prefix = block[: cut + len(marker)]
    content = block[cut + len(marker) :]
    best: str | None = None
    low, high = 0, len(content)
    while low <= high:
        mid = (low + high) // 2
        candidate = prefix + content[:mid].rstrip() + TRUNCATION_SUFFIX
        if estimate_tokens(KB_HEADER + "\n" + candidate) <= budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def format_hits(
    hits: tuple[KnowledgeHit, ...], max_context_tokens: int
) -> tuple[str, int]:
    """把命中渲染成受 token 预算约束的文本，返回 `(text, 保留块数)`。

    头部、标签、分类、路径、标题、正文与截断提示全部计入预算；超预算时按块
    整块丢弃（保留分数最高的块），必要时对第一块追加 `TRUNCATION_SUFFIX`。
    """
    blocks = [_render_block(label, hit) for label, hit in enumerate(hits, start=1)]
    kept: list[str] = []
    for block in blocks:
        if kept:
            candidate = KB_HEADER + "\n" + "\n\n".join([*kept, block])
        else:
            candidate = KB_HEADER + "\n" + block
        if estimate_tokens(candidate) <= max_context_tokens:
            kept.append(block)
            continue
        if not kept:
            fitted = _fit_first_block(block, max_context_tokens)
            if fitted is not None:
                kept.append(fitted)
        break
    if not kept:
        return "", 0
    return KB_HEADER + "\n" + "\n\n".join(kept), len(kept)


class KnowledgeService:
    """知识库的对外门面：生命周期、访问策略与检索。"""

    def __init__(self, config: KnowledgeBaseConfig) -> None:
        self._config = config
        self._state: tuple[KnowledgeSnapshot, KnowledgeIndex] | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """是否已有成功构建的快照。"""
        return self._state is not None

    def permits(self, *, channel_kind: str, user_id: str) -> bool:
        """访问策略：先看频道类型，再按 access_mode 判断用户（D-44）。"""
        config = self._config
        if not config.enabled:
            return False
        if channel_kind not in config.allowed_channel_kinds:
            return False
        if config.access_mode == "all_chat":
            return True
        return user_id in config.allowed_user_ids

    async def start(self) -> None:
        """首次构建 + 启动周期刷新；失败只记事件，绝不抛出。"""
        if not self._config.enabled:
            return
        await self._refresh_once()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """取消刷新任务；重复调用安全，绝不抛出。"""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _refresh_loop(self) -> None:
        """按 `refresh_seconds` 周期刷新；`_refresh_once` 自身不抛业务异常。"""
        interval = max(1, self._config.refresh_seconds)
        while True:
            await asyncio.sleep(interval)
            await self._refresh_once()

    async def _refresh_once(self) -> None:
        """构建一次快照与索引；只在完整成功时用一次引用替换旧状态。"""
        async with self._lock:
            version = self._state[0].version + 1 if self._state else 1
            try:
                snapshot = await asyncio.to_thread(
                    build_snapshot,
                    self._config.root_dir,
                    self._config,
                    version=version,
                )
                index = await asyncio.to_thread(KnowledgeIndex.build, snapshot)
            except KnowledgeBuildError as exc:
                log_event(logger, logging.WARNING, "kb.index_failed", reason=exc.reason)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                # 异常文本可能带宿主绝对路径，绝不写进日志。
                log_event(logger, logging.WARNING, "kb.index_failed", reason="unexpected")
                return
            self._state = (snapshot, index)
            log_event(
                logger,
                logging.INFO,
                "kb.ready",
                snapshot_version=snapshot.version,
                chunk_count=snapshot.chunk_count,
                count=snapshot.document_count,
                size_bytes=snapshot.total_bytes,
            )

    async def search(self, query: str) -> KnowledgeResult:
        """检索知识库；绝不抛出，也不记录查询、标题或正文。"""
        config = self._config
        if not config.enabled:
            return KnowledgeResult(STATUS_DISABLED, 0, "", 0)
        state = self._state
        if state is None:
            return KnowledgeResult(STATUS_UNAVAILABLE, 0, "", 0)
        snapshot, index = state
        try:
            hits = await asyncio.to_thread(index.search, query, top_k=config.top_k)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(logger, logging.WARNING, "kb.search_failed", reason="unexpected")
            return KnowledgeResult(STATUS_UNAVAILABLE, 0, "", snapshot.version)
        if not hits:
            return KnowledgeResult(STATUS_NO_RESULTS, 0, "", snapshot.version)
        text, block_count = format_hits(hits, config.max_context_tokens)
        if block_count == 0:
            return KnowledgeResult(STATUS_NO_RESULTS, 0, "", snapshot.version)
        return KnowledgeResult(STATUS_OK, block_count, text, snapshot.version)


__all__ = [
    "KnowledgeResult",
    "KnowledgeService",
    "KB_HEADER",
    "format_hits",
    "STATUS_OK",
    "STATUS_NO_RESULTS",
    "STATUS_UNAVAILABLE",
    "STATUS_DISABLED",
]
