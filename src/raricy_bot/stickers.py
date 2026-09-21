"""表情包 token：扫描、归一与出站重写（设计 §4.2，INTERFACES §55）。

站方的表情语法是**正文文本** `[@合集/表情]`，渲染期展开；**名字写错或格式写错都不报错**，
只是把原文原样显示给读者（`docs/materials/chat-bot.md` §7.4）。因此「格式与正确性」必须由
代码持有：模型只提供查找线索，出站字节一律由本模块生成。

四条边界决定了本模块的形状：

- **判别式是斜杠**。只有 `合集/名称` 结构的方括号记号才进入本流程。`[@a1b2c3d4]`（8 位）、
  `[@AbCdEf1234]`（10 位）、`[@vOtE12345]`（9 位）、`[@123456]`（6 位数字）这些内容引用
  都没有斜杠，**一个字节都不改**。
- **只归一候选内部**（宽度、候选内空白），**不对整条正文做 NFKC**。
- **不做模糊纠正**：只有宽度归一、空格容错、精确匹配与配置别名。
- **代码区内的候选一律不动**：模型既然把它放进代码块，意图就是展示这个语法。

模块是纯函数：无 IO、无网络，不依赖 `SiteClient` / `Store` / `Redactor`，因此可单测，
也能被聊天与评论两条链路共用。依赖方向固定为 `stickers ← config`，本模块**不 import
`config`**（会成环）；`StickerTable.from_config` 按鸭子类型从配置对象取字段。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

# 一条消息里最多生效多少个表情 token（超出部分站点原样显示，chat-bot.md §7.4）。
MAX_STICKERS: int = 30

# 合集名与表情名各自的字符上限（chat-bot.md §7.4）。
MAX_NAME_CHARS: int = 32

# 名字允许的集合外字符：`+`、`·`(U+00B7)、`-`。其余必须落在 Unicode 字母 / 数字里。
# 注意这条把契约里那个坑挡在启动时：名字含 `_` 在线上会静默变成字面量。
_EXTRA_NAME_CHARS: frozenset[str] = frozenset("+·-")

# 候选记号：方括号（半角或全角）+ 不含方括号与换行的内文。
# 内文不许含方括号，于是嵌套时自然落到最内层；不含换行，未闭合候选就不会跨段落去吞后文。
_CANDIDATE_RE: re.Pattern[str] = re.compile(r"[\[［]([^\[\]［］\n]*)[\]］]")

# 围栏代码块的开闭字符（chat-bot.md §7.4 的代码区示例与 CommonMark 同款）。
_FENCE_CHARS: str = "`~"


def normalize_clue(value: str) -> str:
    """线索归一：宽度归一到半角（NFKC），并去掉首尾与内部**全部**空白。

    只对候选内部与查表键使用。整条正文不做 NFKC——那会把代码区、引用标记与用户原文
    一起改写，超出本模块的授权范围。
    """
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(char for char in normalized if not char.isspace())


def validate_sticker_name(name: str) -> str | None:
    """校验一个表情名；合法返回 None，否则返回中文短句说明原因（不回显取值）。

    规则来自站方契约（chat-bot.md §7.4）：非空、≤32 字符、无空白、字符集 ⊆
    Unicode 字母 / 数字 / `+·-`。校验的是**原样**的字节：全角字符不在这里折算，
    因为折算后可能与另一个名字撞成同一个查表键，那属于配置层按同一归一规则做的碰撞
    检查（设计 §4.3），不在这里悄悄放行。
    """
    if not isinstance(name, str):
        return "名称必须是字符串"
    if not name:
        return "名称不能为空"
    if len(name) > MAX_NAME_CHARS:
        return f"名称超过 {MAX_NAME_CHARS} 字符"
    for char in name:
        if char.isspace():
            return "名称不能含空白"
        if char in _EXTRA_NAME_CHARS or char.isalpha() or char.isdigit():
            continue
        return "名称含不允许的字符"
    return None


@dataclass(frozen=True)
class StickerEntry:
    """一个表情条目：规范名与别名（别名只做精确匹配，不做子串或编辑距离匹配）。"""

    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class StickerReport:
    """一次 `render` 的计数。

    不变量：``candidates == kept + fixed + dropped``。四者互斥且完备地覆盖**每一个
    会被处理的候选**；代码区内的候选一个都不计入（见 `render`）。
    """

    candidates: int
    kept: int
    fixed: int
    dropped: int


@dataclass(frozen=True)
class RenderedText:
    """一次 `render` 的结果。

    `token_spans` 是**归一后正文里**每个表情候选占据的区间（含代码区内未处理的候选），
    `code_spans` 是归一后正文里每个代码区的区间；两者都是左闭右开、按出现顺序。
    被剥除的候选在正文里不占字节，因此没有区间。区间供 `outbound.prepare_outbound`
    在截断时回退切点使用（设计 §4.1）。
    """

    text: str
    token_spans: tuple[tuple[int, int], ...]
    code_spans: tuple[tuple[int, int], ...]
    report: StickerReport


@dataclass(frozen=True)
class StickerTable:
    """一张表情查找表：一个合集下的规范名、别名与只读查表。

    `names` 是有序规范名（供提示词平铺）；`entries` 保留别名。查表内部用
    `MappingProxyType` 包住——`frozen=True` 只冻结字段重新赋值，不冻结字段里的字典。
    `_lookup` 可以直接构造，也可以省略：省略时按 `entries` 现算。
    """

    collection: str
    names: tuple[str, ...]
    entries: tuple[StickerEntry, ...]
    # 派生字段：不参与比较与哈希，避免 MappingProxyType 不可哈希拖垮 frozen 语义。
    _lookup: Mapping[str, str] | None = field(
        default=None, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        if self._lookup is not None:
            return
        lookup: dict[str, str] = {}
        for entry in self.entries:
            for alias in entry.aliases:
                key = normalize_clue(alias)
                if key:
                    # 规范名优先于别名：别名与别名相撞时应由配置校验拦住，
                    # 这里只保证不会反过来覆盖一条真名。
                    lookup.setdefault(key, entry.name)
        for entry in self.entries:
            key = normalize_clue(entry.name)
            if key:
                lookup[key] = entry.name
        object.__setattr__(self, "_lookup", MappingProxyType(lookup))

    @classmethod
    def from_config(cls, cfg: Any) -> StickerTable:
        """从配置对象构建表（鸭子类型）。

        只需要 `cfg.collection` 与 `cfg.entries`，每项有 `name` / `aliases`。
        这样做是为了让依赖方向保持 `stickers ← config`：本模块不 import `config`，
        否则会成环。
        """
        entries = tuple(
            StickerEntry(
                name=str(item.name),
                aliases=tuple(str(alias) for alias in (getattr(item, "aliases", None) or ())),
            )
            for item in cfg.entries
        )
        return cls(
            collection=str(cfg.collection),
            names=tuple(entry.name for entry in entries),
            entries=entries,
        )

    def resolve(self, clue: str) -> str | None:
        """把一条**名称线索**解析成规范名；查不到返回 None。

        线索会再走一遍 `normalize_clue`（幂等），因此宽度或内部空白写错的线索也能命中。
        这里只认名称本身，不认 `合集/名称` 整体——合集由调用方（`render`）先分离，
        避免把 `[@14/14/上班]` 这类脏输入也救回来。
        """
        lookup = self._lookup
        if lookup is None:  # pragma: no cover - __post_init__ 必然填上
            return None
        return lookup.get(normalize_clue(clue))


@dataclass(frozen=True)
class _Candidate:
    """一次扫描出的候选（自用，不对外）。"""

    start: int
    end: int
    canonical: str | None
    # 完全落在代码区内：保留原文、不计入计数，但区间要进 token_spans。
    covered: bool
    # 与代码区仅部分重叠（夹着反引号的病态输入）：保留原文，也不进 token_spans，
    # 否则区间会与代码区交叉，破坏调用方的回退判定。
    overlaps: bool


def _fence_run(stripped: str) -> tuple[str, int] | None:
    """一行去掉缩进后是否以 ≥3 个反引号或波浪号开头；返回 (字符, 长度)。"""
    if not stripped or stripped[0] not in _FENCE_CHARS:
        return None
    char = stripped[0]
    length = 0
    while length < len(stripped) and stripped[length] == char:
        length += 1
    if length < 3:
        return None
    return char, length


def _backtick_run(line: str, index: int) -> int:
    """从 index 起连续反引号的个数。"""
    end = index
    while end < len(line) and line[end] == "`":
        end += 1
    return end - index


def _inline_code_spans(line: str, offset: int) -> list[tuple[int, int]]:
    """一行里由**等长**反引号 run 配对出的代码区间。

    未配对的反引号是普通文本——这条不能写反：把落单反引号当成代码区起点，会让其后
    整段正文被误判成代码区，正是本设计要治的「漏字面量」。
    """
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(line)
    while index < length:
        if line[index] != "`":
            index += 1
            continue
        run = _backtick_run(line, index)
        cursor = index + run
        closing = -1
        while cursor < length:
            if line[cursor] != "`":
                cursor += 1
                continue
            other = _backtick_run(line, cursor)
            if other == run:
                closing = cursor + other
                break
            cursor += other
        if closing == -1:
            index += run
            continue
        spans.append((offset + index, offset + closing))
        index = closing
    return spans


def _code_spans(text: str) -> list[tuple[int, int]]:
    """扫描代码区（围栏与行内），返回正文里左闭右开的区间。

    围栏：` ``` ` 或 `~~~`，≥3 个字符；闭合围栏须**同字符且不短于**开启围栏，
    且其后只剩空白。**未闭合围栏按 CommonMark 延伸到文本末尾**。

    歧义时偏向「不是代码」：行内反引号必须等长配对，落单的按普通文本处理（见
    `_inline_code_spans`）。
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    fence_char: str | None = None
    fence_len = 0
    fence_start = 0
    for line in text.splitlines(keepends=True):
        indent = len(line) - len(line.lstrip(" \t"))
        stripped = line[indent:]
        opener = _fence_run(stripped)
        if fence_char is not None:
            if (
                opener is not None
                and opener[0] == fence_char
                and opener[1] >= fence_len
                and not stripped[opener[1] :].strip()
            ):
                spans.append((fence_start, offset + len(line)))
                fence_char = None
        elif opener is not None:
            fence_char, fence_len = opener
            fence_start = offset + indent
        else:
            spans.extend(_inline_code_spans(line, offset))
        offset += len(line)
    if fence_char is not None:
        # 未闭合的围栏保护到文末：站点也把未闭合的代码块当代码。
        spans.append((fence_start, offset))
    return spans


def _scan_candidates(
    text: str, table: StickerTable, code_spans: list[tuple[int, int]]
) -> list[_Candidate]:
    """按出现顺序找出可靠判定为表情语法的候选（含代码区内的）。

    「可靠判定」的判据有两条，缺一不可：

    - 内文带 `@` 记号（`[@…]` 是表情与内容引用共用的记号，加上斜杠才唯一指向表情）；
    - 或内文的合集段与配置里的合集完全一致（容忍模型漏写 `@`）。

    两条都不满足的方括号记号保持原文：`[a/b]`、Markdown 链接这类文本不是表情语法，
    剥除它们属于越权。合集对不上的（例如 `[@别的合集/上班]`）算可靠，但解析不到
    规范名，按「未命中」处理。
    """
    candidates: list[_Candidate] = []
    for match in _CANDIDATE_RE.finditer(text):
        normalized = normalize_clue(match.group(1))
        if "/" not in normalized:
            continue
        has_marker = normalized.startswith("@")
        body = normalized[1:] if has_marker else normalized
        collection, _, name = body.partition("/")
        if not has_marker and collection != table.collection:
            continue
        start, end = match.span()
        covered = any(cs <= start and end <= ce for cs, ce in code_spans)
        overlaps = (not covered) and any(cs < end and start < ce for cs, ce in code_spans)
        canonical = table.resolve(name) if collection == table.collection else None
        candidates.append(
            _Candidate(
                start=start,
                end=end,
                canonical=canonical,
                covered=covered,
                overlaps=overlaps,
            )
        )
    return candidates


@dataclass(frozen=True)
class _Marker:
    """一处要在正文里落笔的位置：`output` 为空串表示整段剥除。"""

    start: int
    end: int
    output: str


def render(text: str, table: StickerTable) -> RenderedText:
    """把正文里的表情候选重写成规范 token，并给出归一后正文的区间表。

    处理规则（设计 §4.2）：

    - 命中 → 重写为 `[@合集/规范名]`，出站字节由代码生成；
    - 未命中 → 整段剥除（不替换成 `[redacted]`，那会留下一个非法 token）；
    - 超过 `MAX_STICKERS` 个 → 只保留前 30 个，其余剥除；
    - 代码区内的候选一律不动，也不计入 kept / fixed / dropped。

    对已规范化的正文是**恒等变换**（幂等）：`render` 的结果再跑一遍逐字节相同，
    这条是披露预算那套三段预留成立的前提（设计 §4.1）。
    """
    code_spans = _code_spans(text)
    candidates = _scan_candidates(text, table, code_spans)

    processed = 0
    kept = 0
    fixed = 0
    dropped = 0
    markers: list[_Marker] = []
    for candidate in candidates:
        if candidate.covered or candidate.overlaps:
            continue
        processed += 1
        if processed > MAX_STICKERS or candidate.canonical is None:
            dropped += 1
            markers.append(_Marker(candidate.start, candidate.end, ""))
            continue
        output = f"[@{table.collection}/{candidate.canonical}]"
        if output == text[candidate.start : candidate.end]:
            kept += 1
        else:
            fixed += 1
        markers.append(_Marker(candidate.start, candidate.end, output))

    pieces: list[str] = []
    token_spans: list[tuple[int, int]] = []
    out_code_spans: list[tuple[int, int]] = []
    out_len = 0
    cursor = 0
    marker_index = 0

    def emit_markers(upto: int) -> None:
        """落笔所有起点早于 `upto` 的标记，并记录它们在输出里的区间。"""
        nonlocal out_len, cursor, marker_index
        while marker_index < len(markers) and markers[marker_index].start < upto:
            marker = markers[marker_index]
            pieces.append(text[cursor : marker.start])
            out_len += marker.start - cursor
            if marker.output:
                pieces.append(marker.output)
                token_spans.append((out_len, out_len + len(marker.output)))
                out_len += len(marker.output)
            cursor = marker.end
            marker_index += 1

    for code_start, code_end in code_spans:
        emit_markers(code_start)
        pieces.append(text[cursor:code_start])
        out_len += code_start - cursor
        span_start = out_len
        pieces.append(text[code_start:code_end])
        out_len += code_end - code_start
        out_code_spans.append((span_start, out_len))
        for candidate in candidates:
            if candidate.covered and code_start <= candidate.start and candidate.end <= code_end:
                token_spans.append(
                    (
                        span_start + candidate.start - code_start,
                        span_start + candidate.end - code_start,
                    )
                )
        cursor = code_end
    emit_markers(len(text))
    pieces.append(text[cursor:])

    return RenderedText(
        text="".join(pieces),
        token_spans=tuple(token_spans),
        code_spans=tuple(out_code_spans),
        report=StickerReport(
            candidates=processed,
            kept=kept,
            fixed=fixed,
            dropped=dropped,
        ),
    )
