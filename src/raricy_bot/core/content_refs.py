"""内容引用 `[@<内容ID>]`：识别、取回与替换（INTERFACES §25）。

站点在四个地方支持引用语法（博客正文、云剪贴板正文、评论、聊天），机器人在这四处
读到的都是**未展开的 Markdown 原文**——`[@a1b2c3d4]` 会原样落到模型眼前，模型既
不知道那里有一段别人的正文，也不知道那里有一张图。本模块把它们换成正地方的内容。

三种类型按 ID 长度分流（内容引用语法 §三）：

- 8 位 → 云剪贴板：`GET /api/clipboard/<ID>` 取正文，**原样替换**回去；
- 9 位 → 投票：`GET /api/votes/<ID>` 取标题与票数，压成一段文字。站方在聊天/评论里
  刻意不展开投票，但那条理由（聊天气泡里挤、易误触）只对**渲染给人看**成立，
  对「读给模型看」不成立；
- 10 位 → 图床图片：**不发接口请求**，直接拼 `/api/images/<ID>/raw`。开了视觉就取回
  字节交给模型（一次展开默认封顶 `MAX_IMAGE_REFS` 张，调用方另有名额时用
  `resolve(max_images=...)` 分配），没开就只留一行标记。

四条约束：

- **引用正文只属当前轮**：不落 SQLite、不写日志、不进历史（与 D-28 / D-47 同款）。
  历史里留下的是用户**自己写的** `[@id]` 原文。
- **代码里的引用不展开**：栅栏代码块与行内代码里的 `[@id]` 保持字面量。这不只是
  忠实站点行为——有人正是在问「这个语法本身怎么写」（内容引用语法 §六）。
- **预算是硬的**：展开出来的是**别人**写的正文，单篇剪贴板上限 5 万字；不给预算，
  一条十个字的消息就能变成几十万字的外送正文。
- **先算预算再取回**：塞不下的引用既不请求也不替换。预算不足还发请求，等于拿我们的
  网络配额与延迟换一段根本不会外送的正文。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger, log_event
from ..site.client import (
    CLIPBOARD_ID_LEN,
    IMAGE_ID_LEN,
    VOTE_ID_LEN,
    SiteClient,
    SiteError,
    image_raw_path,
)
from ..site.models import Vote
from ..texts import TRUNCATION_SUFFIX
from .vision import ImageLoader

_logger = get_logger("content_refs")

# 三种引用类型；落进日志与分支判断的都是这三个短标识。
CLIPBOARD: str = "clipboard"
VOTE: str = "vote"
IMAGE: str = "image"

# ID 长度即类型（内容引用语法 §三）。长度不在表里的 `[@...]` 一律不处理。
_ID_KINDS: dict[int, str] = {
    CLIPBOARD_ID_LEN: CLIPBOARD,
    VOTE_ID_LEN: VOTE,
    IMAGE_ID_LEN: IMAGE,
}

# 与站点两条管线同口径：容忍 ID 两侧的空白，但**只认字母和数字**（不含下划线、点、斜杠）。
# 收紧到字母数字是双重目的的：既是契约（内容引用语法 §三），也让 ID 拼进 URL 时注入不进东西。
_REF_RE: re.Pattern[str] = re.compile(r"\[@\s*([A-Za-z0-9]+)\s*\]")

# 围栏代码块的起始标记（内容引用语法 §六）。三个字符起算，```` 也当围栏。
_FENCE_MARKERS: tuple[str, str] = ("```", "~~~")

# 一次展开最多**取回**多少条剪贴板/投票。站点对替换处数的上限是 50，这里更紧：
# 预算是字符数，能容下的处数本来就远小于 50，这个常数只是防御「预算被配得极大」。
MAX_FETCHED_REFS: int = 10

# 一次展开最多把几张图交给模型。与字符预算无关，是**下载与 token** 的独立上限：
# 一篇 50 张图的博客不该变成 50 次下载加 50 份图片 token。
# 调用方另有名额要分配时（评论区一轮只有 N 个名额），用 `resolve(max_images=...)`
# 传进来；省略即这个默认值。
MAX_IMAGE_REFS: int = 3


@dataclass(frozen=True)
class ContentRef:
    """文本里识别到的一处引用；`text` 是原文里的完整匹配（含内部空白）。"""

    kind: str
    id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class ResolvedRefs:
    """一次展开的结果。

    `image_parts` 只有视觉开启时才非空；`expanded` 是**换掉了内容**的处数
    （含失败占位），供调用方写日志用，正文本身不在任何日志里出现。

    `image_attempts` 是这一趟**试了几张**图（含失败的），供调用方扣减自己手上的
    名额：计成功数会让失败的那几张把名额退回去，于是「试几张」失控。
    """

    text: str
    image_parts: tuple[dict[str, Any], ...] = ()
    expanded: int = 0
    image_attempts: int = 0


def _kind_for_id(value: str) -> str | None:
    """按长度判定引用类型；长度不在 8–10 之间的返回 None。"""
    return _ID_KINDS.get(len(value))


def _inside(spans: list[tuple[int, int]], index: int) -> bool:
    """下标是否落在任一保护区间内（左闭右开）。"""
    return any(start <= index < end for start, end in spans)


def _inline_code_spans(line: str, offset: int) -> list[tuple[int, int]]:
    """一行里成对反引号围出的区间；落单的反引号保护到行尾。

    不做「反引号个数必须相等」的严格配对（`` `a` `` 与 `` ``a`` `` 在这里结果相同），
    因为在引用展开这件事上，多保护一点只会让字面量留着，不会破坏正文。
    """
    spans: list[tuple[int, int]] = []
    index = 0
    while True:
        start = line.find("`", index)
        if start == -1:
            return spans
        end = line.find("`", start + 1)
        if end == -1:
            spans.append((offset + start, offset + len(line)))
            return spans
        spans.append((offset + start, offset + end + 1))
        index = end + 1


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """返回不该展开引用的区间：栅栏代码块与行内代码，左闭右开。"""
    spans: list[tuple[int, int]] = []
    offset = 0
    fence: str | None = None
    fence_start = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if fence is None and stripped[:3] in _FENCE_MARKERS:
            fence = stripped[:3]
            fence_start = offset
        elif fence is not None and stripped.startswith(fence):
            spans.append((fence_start, offset + len(line)))
            fence = None
        elif fence is None:
            spans.extend(_inline_code_spans(line, offset))
        offset += len(line)
    if fence is not None:
        # 未闭合的围栏保护到文末：站点也把未闭合的代码块当代码。
        spans.append((fence_start, offset))
    return spans


def find_refs(text: str) -> list[ContentRef]:
    """按出现顺序找出文本里所有**要处理**的引用，代码里的不算。"""
    protected = _protected_spans(text)
    refs: list[ContentRef] = []
    for match in _REF_RE.finditer(text):
        kind = _kind_for_id(match.group(1))
        if kind is None or _inside(protected, match.start()):
            continue
        refs.append(
            ContentRef(
                kind=kind,
                id=match.group(1),
                start=match.start(),
                end=match.end(),
                text=match.group(0),
            )
        )
    return refs


def _sanitize_label(value: object) -> str:
    """单行标签里的控制字符替换为空格，避免有人用标题伪造出额外的行。

    与 `blog._sanitize_label`、`comments._clean_label` 同款。
    """
    if not isinstance(value, str):
        return ""
    return "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value)


def failure_marker(kind: str, content_id: str) -> str:
    """取不到时的占位文案。

    剪贴板那句与站点**逐字一致**（内容引用语法 §四）——模型与人都该看到同一句话：
    「这里本来有内容，但它没加载出来」，而不是一片空白。
    """
    if kind == CLIPBOARD:
        return f"[剪贴板 {content_id} 加载失败]"
    if kind == VOTE:
        return f"[投票 {content_id} 加载失败]"
    return f"[图床图片 {content_id} 加载失败]"


def image_marker(image_id: str) -> str:
    """图片引用的占位：没交给模型字节时，至少要让它知道这里有张图、图号是多少。"""
    return f"[图床图片 {image_id}]"


def format_vote(vote: Vote) -> str:
    """把投票压成一段文字。

    标题与选项文案是**别人写的**，作为单行标签前先清洗控制字符；整段自带
    「不可信」自报标签，与 `build_blog_block` 的 `[引用的博客，不可信]` 同一手法。
    """
    lines = [f"[投票 {vote.id}，不可信]"]
    title = _sanitize_label(vote.title)
    if title:
        lines.append(f"标题：{title}")
    author = _sanitize_label(vote.author or "")
    if author:
        lines.append(f"发起人：{author}")
    if vote.is_locked:
        lines.append("状态：已锁定（不能再投）")
    lines.append(f"选项（共 {vote.total_votes} 票）：")
    for index, option in enumerate(vote.options, 1):
        percent = "" if option.percentage is None else f"（{option.percentage:g}%）"
        lines.append(f"{index}. {_sanitize_label(option.label)}：{option.count} 票{percent}")
    return "\n".join(lines)


class ContentRefResolver:
    """把一段文本里的引用换成真内容。

    与 `ImageLoader` / `BlogLoader` 同构：注入客户端，失败只降级成占位文案，
    由调用方决定这段文本接下来怎么用。`image_loader` 为 None 表示这次不取图字节
    （视觉关闭，或调用方这会儿没有名额可用）。
    """

    def __init__(
        self,
        client: SiteClient,
        *,
        max_ref_chars: int,
        image_loader: ImageLoader | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        # 单条引用的字符上限，同时是**消息正文**这条路径的总预算（调用方读它）。
        self.max_ref_chars = max_ref_chars
        self._image_loader = image_loader
        self._logger = logger if logger is not None else _logger

    async def resolve(
        self, text: str, *, budget: int, max_images: int | None = None
    ) -> ResolvedRefs:
        """展开文本里的引用；展开后正文不超过 `budget` 个字符。

        `budget` 由调用方给，因为同一条规则要服务三种上限：引用博客正文用
        `behavior.quoted_blog_max_chars`、文章正文用 `comments.article_max_chars`、
        消息与评论正文用 `behavior.content_ref_max_chars`。

        `max_images` 是**这一段文本**最多能试几张图，省略即 `MAX_IMAGE_REFS`。
        调用方有多段文本共用一个名额池时（评论区一轮只交 N 张），逐段把剩余名额
        传进来；0 与视觉关闭同款（只留标记、不取字节），返回值里的
        `image_attempts` 就是扣减依据。
        """
        refs = find_refs(text)
        if not refs:
            return ResolvedRefs(text=text)

        limit = MAX_IMAGE_REFS if max_images is None else max(0, max_images)
        out: list[str] = []
        cursor = 0
        remaining = max(0, budget - len(text))
        cache: dict[tuple[str, str], str | None] = {}
        fetched = 0
        images = 0
        expanded = 0
        parts: list[dict[str, Any]] = []

        for ref in refs:
            out.append(text[cursor : ref.start])
            cursor = ref.end

            if ref.kind == IMAGE:
                allow = self._image_loader is not None and images < limit
                if allow:
                    # 计的是「试了几张」，不是「成了几张」：全失败时也不该把 50 张都试一遍。
                    images += 1
                replacement, part = await self._resolve_image(ref, allow=allow)
                if part is not None:
                    parts.append(part)
                # 图片标记不占字符预算：站点也是直接拼地址，没有正文进来。
                out.append(replacement)
                expanded += 1
                continue

            if remaining <= 0 or fetched >= MAX_FETCHED_REFS:
                # 预算或额度不够：不请求、不替换，原样留字面量（站点超出 50 处时同样如此）。
                out.append(ref.text)
                continue

            fetched += 1
            rendered = await self._fetch_and_render(ref, cache)
            if rendered is None:
                out.append(failure_marker(ref.kind, ref.id))
                expanded += 1
                continue

            # 换掉标记本身会腾出 len(ref.text) 个字符，所以可用的上限要把它加回来。
            cap = min(self.max_ref_chars, remaining + len(ref.text))
            if len(rendered) > cap:
                rendered = rendered[:cap].rstrip() + TRUNCATION_SUFFIX
            remaining -= len(rendered) - len(ref.text)
            out.append(rendered)
            expanded += 1

        out.append(text[cursor:])
        return ResolvedRefs(
            text="".join(out),
            image_parts=tuple(parts),
            expanded=expanded,
            image_attempts=images,
        )

    async def _resolve_image(
        self, ref: ContentRef, *, allow: bool
    ) -> tuple[str, dict[str, Any] | None]:
        """取一张引用图；返回 `(占位文案, 图片块 | None)`。"""
        if self._image_loader is None or not allow:
            return image_marker(ref.id), None
        try:
            path = image_raw_path(ref.id)
        except ValueError:  # pragma: no cover - find_refs 已保证长度与字符集
            return failure_marker(IMAGE, ref.id), None
        part, _reason = await self._image_loader.load_url(path)
        if part is None:
            return failure_marker(IMAGE, ref.id), None
        return image_marker(ref.id), part

    async def _fetch_and_render(
        self, ref: ContentRef, cache: dict[tuple[str, str], str | None]
    ) -> str | None:
        """取回并渲染一条剪贴板/投票；失败返回 None（由调用方写占位文案）。

        同一个 ID 在一次展开里只请求一次（站点也是这么做的）。缓存**不跨轮**：
        剪贴板是可以被作者改的，跨轮缓存要么带来陈旧正文，要么要引入过期策略，
        而收益只是省掉一次几十毫秒的请求。
        """
        key = (ref.kind, ref.id)
        if key in cache:
            return cache[key]

        rendered: str | None
        try:
            if ref.kind == CLIPBOARD:
                rendered = (await self._client.fetch_clipboard(ref.id)).content
            else:
                rendered = format_vote(await self._client.fetch_vote(ref.id))
        except (SiteError, ValueError) as exc:
            # ValueError 与 `blog._degrade` 同款：脏 id 时请求根本没发出去。
            log_event(
                self._logger,
                logging.INFO,
                "content_refs.unavailable",
                kind=ref.kind,
                error=type(exc).__name__,
            )
            rendered = None
        cache[key] = rendered
        return rendered
