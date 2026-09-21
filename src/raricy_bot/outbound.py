"""出站收口：脱敏、表情归一、敏感串复检与最终长度预算（设计 §4.1）。

聊天与评论两条链路共用同一个入口 ``prepare_outbound``，让「最终写出的字节」只由一处决定。
步骤顺序是承重结构，不能调换：

1. **脱敏** —— 隐私边界，最先执行；
2. **表情归一** —— ``render`` 可能让正文**变长**，必须在截断之前；功能关闭
   （``table is None``）时完全跳过，一个字节不动；
3. **逐 token 敏感串复检** —— 归一的去空白容错可能重新拼出第 1 步没拦住的敏感串，
   命中则**整枚丢弃**（不替换成 ``[redacted]``，那会留下一个非法 token）；
4. **截断** —— ``max_chars`` 是**含截断提示在内**的最终上限；``None`` 表示不做预算，
   第 4、5 步整段跳过（披露路径要在归一后、Sender 截断前拿回正文）；
5. **区间回退** —— 切点落在 token 或代码区内部时回退到区间起点，只依据区间表，不用正则猜归属。

模块是纯函数：无 IO、无网络，``redactor`` 与 ``table`` 都由调用方注入，因此可单测。
"""

from __future__ import annotations

from dataclasses import dataclass

from .redact import Redactor
from .stickers import StickerReport, StickerTable, render
from .text_utils import truncate_with_cut
from .texts import TRUNCATION_SUFFIX

# 区间表在收口内部流转的类型：左闭右开的 ``(start, end)``，按出现顺序。
_Span = tuple[int, int]


@dataclass(frozen=True)
class OutboundReport:
    """一次 ``prepare_outbound`` 的结果摘要。

    ``sticker`` 是这一趟 ``render`` 的计数；功能关闭（``table is None``）时为 None。
    ``dropped_for_secret`` 单独记「因归一后命中敏感串而被整枚丢弃」的数量，不计入
    ``sticker`` —— 那一份计数描述的是 ``render`` 自身的决策，两者各自完备。
    """

    truncated: bool
    sticker: StickerReport | None
    dropped_for_secret: int = 0


def _recheck_secrets(
    body: str,
    token_spans: tuple[_Span, ...],
    code_spans: tuple[_Span, ...],
    redactor: Redactor,
) -> tuple[str, tuple[_Span, ...], tuple[_Span, ...], int]:
    """对每个归一后的 token 再跑一遍脱敏器，命中则整枚丢弃并重建区间表。

    ``render`` 唯一会新建的文本就在 token 内部，所以这一步是完备的：每个 token 区间
    单独再过一遍 ``redact``，结果与原文不同即说明归一拼出了第 1 步没拦住的敏感串
    （典型场景：``[@14/上 班]`` 里的空格让「上班」逃过第 1 步，归一又把它拼回来）。

    丢弃会改变其后所有区间的偏移，因此这里重建正文，并把 ``token_spans`` /
    ``code_spans`` 一并按删除量平移；被丢弃的 token 不再出现在区间表里。
    """
    drops = tuple(
        span
        for span in token_spans
        if redactor.redact(body[span[0] : span[1]]) != body[span[0] : span[1]]
    )
    if not drops:
        return body, token_spans, code_spans, 0

    dropped = set(drops)
    pieces: list[str] = []
    cursor = 0
    for start, end in drops:
        pieces.append(body[cursor:start])
        cursor = end
    pieces.append(body[cursor:])
    new_body = "".join(pieces)

    def shift(position: int) -> int:
        """把旧正文里的偏移换算到删除后的新正文；``drops`` 有序且互不相交。"""
        removed = 0
        for start, end in drops:
            if end <= position:
                removed += end - start
            else:
                break
        return position - removed

    new_tokens = tuple(
        (shift(start), shift(end)) for start, end in token_spans if (start, end) not in dropped
    )
    new_codes = tuple((shift(start), shift(end)) for start, end in code_spans)
    return new_body, new_tokens, new_codes, len(drops)


def _truncate(
    body: str,
    token_spans: tuple[_Span, ...],
    code_spans: tuple[_Span, ...],
    max_chars: int,
) -> tuple[str, bool]:
    """按含截断提示在内的 ``max_chars`` 上限截断，必要时回退切点。

    返回 ``(最终正文, 是否截断)``。回退只减不增，只作用于区间表里已记录的区间，
    因此不会误伤无斜杠的内容引用 ``[@a1b2c3d4]``。
    """
    if len(body) <= max_chars:
        return body, False
    if max_chars < 1:
        return "", True
    if max_chars <= len(TRUNCATION_SUFFIX):
        # 提示本身不允许把正文顶出上限：放弃提示，硬切。
        return body[:max_chars], True

    limit = max_chars - len(TRUNCATION_SUFFIX)
    # ``len(body) > max_chars > limit``，故到此处必然发生截断。
    cut = truncate_with_cut(body, limit)[1]

    rollback = cut
    for start, end in (*token_spans, *code_spans):
        if start < cut < end:
            rollback = min(rollback, start)
    final = body[:rollback].rstrip() + TRUNCATION_SUFFIX
    # 硬契约：rollback <= cut <= limit，故 len(final) <= limit + len(SUFFIX) == max_chars。
    return final, final != body


def prepare_outbound(
    text: str,
    *,
    redactor: Redactor,
    table: StickerTable | None,
    max_chars: int | None,
) -> tuple[str, OutboundReport]:
    """出站正文的唯一收口：脱敏 → 归一 → 敏感串复检 → 截断 → 区间回退。

    ``max_chars`` 是**含截断提示在内**的最终上限，返回正文长度恒不超过它
    （``max_chars < 1`` 时返回空串）。``table is None`` 表示表情功能关闭：跳过归一，
    但脱敏与长度预算照常执行，正文其余部分一个字节不动。

    ``max_chars is None`` 表示**不做长度预算**：整段截断与区间回退被跳过，
    ``truncated`` 恒为 False、不追加截断提示、返回长度不设上界；脱敏、归一与逐 token
    敏感串复检照常执行。聊天侧的披露路径用它——那里必须在披露**之前**先归一
    （否则历史会留下未修正的坏 token），却**不能**同时截断，否则 Sender 会再截一次，
    正文末尾出现两个截断提示。

    幂等：``render`` 对已规范化的正文是恒等变换，因此对 ``prepare_outbound`` 自己的输出
    再跑一遍必须逐字节相同。这是 app.py 那套三段预留（披露 + 脱敏增长 +
    ``TRUNCATION_SUFFIX``）成立的前提。
    """
    body = redactor.redact(text)

    sticker_report: StickerReport | None = None
    token_spans: tuple[_Span, ...] = ()
    code_spans: tuple[_Span, ...] = ()
    dropped_for_secret = 0

    if table is not None:
        rendered = render(body, table)
        body = rendered.text
        sticker_report = rendered.report
        body, token_spans, code_spans, dropped_for_secret = _recheck_secrets(
            body, rendered.token_spans, rendered.code_spans, redactor
        )

    if max_chars is None:
        # 不做长度预算：跳过截断与区间回退，不追加提示，返回长度不设上界。
        return body, OutboundReport(
            truncated=False,
            sticker=sticker_report,
            dropped_for_secret=dropped_for_secret,
        )

    body, truncated = _truncate(body, token_spans, code_spans, max_chars)
    return body, OutboundReport(
        truncated=truncated,
        sticker=sticker_report,
        dropped_for_secret=dropped_for_secret,
    )
