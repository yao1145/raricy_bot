"""草稿的解析与预校验（INTERFACES §53.6）。

稿库文件与模型输出是**同一种格式**：YAML front matter + Markdown 正文。解析器只有这一份 ——
人可以把一篇满意的生成稿直接存进稿库复用，Publisher 则完全不负责解析来源格式。

本模块无 I/O：读文件的活归 `drafts.py`，发请求的活归 `publisher.py`。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import yaml

from ..blog_records import (
    HASH_VERSION,
    REASON_DRAFT_EMPTY,
    REASON_DRAFT_INVALID,
    Draft,
    PreparedDraft,
)
from ..redact import Redactor

# 长度口径与上游 `.length` 一致：JavaScript 数的是 UTF-16 code unit，不是码点。
# 一个非 BMP 字符（emoji、部分生僻字）在这里算 2，而 Python 的 len() 算 1。
TITLE_MAX_UNITS: int = 30
DESCRIPTION_MAX_UNITS: int = 100
# 正文上限 250000。**超长按失败处理**：截断正文会毁掉一篇文章，标题与描述只是元数据。
CONTENT_MAX_UNITS: int = 250_000

# 结束分隔符的两种写法，都是 YAML front matter 的通行形式。
_FRONT_MATTER_ENDINGS: frozenset[str] = frozenset({"---", "..."})


class DraftError(Exception):
    """草稿不可用：格式不对、字段缺失、超长或全空。

    `reason` 是 `blog/models.py` 的稳定原因常量，可以直接进日志与 `blog_runs.reason`。
    **异常文本与 repr 都不含原文** —— 文章正文不能借回溯漏进日志。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def utf16_length(text: str) -> int:
    """按 UTF-16 code unit 数长度，与上游 JavaScript 的 `.length` 同口径。"""
    # utf-16-le 每 2 字节一个 code unit；BMP 字符一个，非 BMP 字符两个。
    return len(text.encode("utf-16-le")) // 2


def has_lone_surrogate(text: str) -> bool:
    """是否含孤立代理字符。

    这类字符在 Python 里是合法字符串，编码成 UTF-8 时才会炸 —— 与其在指纹或 HTTP 编码处
    半途失败，不如在预校验这里当作草稿不合法。
    """
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def truncate_utf16(text: str, limit: int) -> str:
    """按 UTF-16 预算截断，且不切开代理对。

    Python 的字符串以码点为单位，按码点切**不可能**切出半个代理对，也不会留下孤立代理字符；
    要小心的只有非 BMP 字符占 2 个 unit，所以逐个累加而不是先按长度切再修。
    """
    if utf16_length(text) <= limit:
        return text
    used = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > limit:
            return text[:index]
        used += width
    return text


def content_hash(title: str, content: str) -> str:
    """内容指纹：`SHA256(UTF8(JSON([title, content], ensure_ascii=False, separators=(",", ":"))))`。

    用 JSON 数组而不是直接拼接两串：拼接会让 `("ab", "c")` 与 `("a", "bc")` 撞成同一个指纹，
    也就是两篇不同的文章被当成同一篇，永远不会发出去。
    描述与栏目**不属于**内容身份：只改摘要或换栏目不该让同一篇文重发。
    """
    payload = json.dumps([title, content], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_draft(text: str) -> Draft:
    """把 `front matter + 正文` 解析成原始 `Draft`；不做脱敏、不截断、不改空白。

    这里只回答「结构对不对」：front matter 必须是映射，`title` 与 `description` 必须都是
    字符串。**禁止**把数字、列表或对象隐式转成标题 —— 模型的空想会变成一篇奇怪的文章，
    而加载期报错反而能让人看出是模型没按格式写。
    """
    if not isinstance(text, str):
        raise DraftError(REASON_DRAFT_INVALID)

    block, content = _split_front_matter(text)
    try:
        loaded: Any = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise DraftError(REASON_DRAFT_INVALID) from exc
    if not isinstance(loaded, Mapping):
        raise DraftError(REASON_DRAFT_INVALID)

    title = loaded.get("title")
    description = loaded.get("description")
    # `isinstance(True, int)` 为真，但这里要求的是 str，所以布尔、数字、列表、对象一律挡住。
    if not isinstance(title, str) or not isinstance(description, str):
        raise DraftError(REASON_DRAFT_INVALID)

    return Draft(title=title, description=description, content=content)


def prepare_draft(draft: Draft, *, redactor: Redactor) -> PreparedDraft:
    """预校验流水线：类型 → 脱敏 → 规范化 → 长度 → 指纹（设计 §10）。

    顺序不能变：脱敏会让文本**变长**（密钥被换成占位符），先量长度再用脱敏后的文本发送，
    就会发出一个超过站方上限的标题。反过来，指纹必须在最后算，且此后不再变换任何字符 ——
    最终发送、落库标题、搜索标题与指纹共用这一份结果。
    """
    if not isinstance(draft, Draft):
        raise DraftError(REASON_DRAFT_INVALID)
    for value in (draft.title, draft.description, draft.content):
        if not isinstance(value, str) or has_lone_surrogate(value):
            raise DraftError(REASON_DRAFT_INVALID)

    title = redactor.redact(draft.title).strip()
    description = redactor.redact(draft.description).strip()
    # 正文保留原始空白：它是 Markdown，前导空行与缩进都有意义。
    content = redactor.redact(draft.content)

    if not title or not description or not content.strip():
        raise DraftError(REASON_DRAFT_EMPTY)
    if has_lone_surrogate(title) or has_lone_surrogate(description) or has_lone_surrogate(content):
        raise DraftError(REASON_DRAFT_INVALID)

    if utf16_length(content) > CONTENT_MAX_UNITS:
        # 正文宁可判失败也不截断：截断会毁文，而「这次没发出去」是可以重试的。
        raise DraftError(REASON_DRAFT_INVALID)

    # 标题与描述在这里收口一次，之后**不再变换**：落库标题、搜索标题、请求体与指纹
    # 必须逐字一致，否则对账会拿一个已经变过的标题去搜。
    final_title = truncate_utf16(title, TITLE_MAX_UNITS)
    final_description = truncate_utf16(description, DESCRIPTION_MAX_UNITS)
    return PreparedDraft(
        title=final_title,
        description=final_description,
        content=content,
        content_hash=content_hash(final_title, content),
        hash_version=HASH_VERSION,
    )


def _split_front_matter(text: str) -> tuple[str, str]:
    """切出 front matter 块与正文；格式不对抛 `DraftError`。

    正文取结束分隔符那一行之后的**全部原始字符**（含紧随其后的空行），不做空白归一化。
    """
    first_newline = text.find("\n")
    if first_newline < 0:
        raise DraftError(REASON_DRAFT_INVALID)
    if text[:first_newline].strip() != "---":
        raise DraftError(REASON_DRAFT_INVALID)

    rest = text[first_newline + 1 :]
    lines = rest.split("\n")
    for index, line in enumerate(lines):
        if line.strip() in _FRONT_MATTER_ENDINGS:
            return "\n".join(lines[:index]), "\n".join(lines[index + 1 :])
    raise DraftError(REASON_DRAFT_INVALID)
