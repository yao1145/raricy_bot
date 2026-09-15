"""`common.md` 与用户私有文件的严格解析与确定性渲染（INTERFACES §29、规划 §5.2 … §5.4）。

纯同步、无文件 I/O、不 import `app.py`（裁决 G）。解析失败只抛 `CodecError`，`reason` 只取六个稳定
值之一；异常字符串、参数与日志**不含任何原始内容**——记忆正文是敏感数据（§29.2 第 7 条、§37）。

两条硬不变量：

- **render 与 parse 共用同一套校验**（`_check_common` / `_check_private`）：render 写出的文档 parse
  一定读得回，非法文档在 render 侧就抛 `CodecError`，不会写出半截文件。唯一的例外是容量上限
  （`cfg` 只在 parse 侧可得），由 §30 的服务在写之前自己保证不越界。
- **确定性**：同一份 document 重复渲染逐字节相同——front matter 字段顺序固定、`operations` 按键
  排序、ID 规范成 6 位零填充、三个区顺序固定、用户文件条目按 ID 升序。这是服务做幂等与外部编辑
  检测的基础（§29.3、D-59）。
- **往返只差两处有意的规范化**：ID 序号按 6 位零填充输出（`UM-7` 读回是 `UM-000007`），正文**行尾**
  的 `\r`（CRLF 的行尾写法）在读回时被吸收。两者一轮就收敛：先 render 再 parse、第二次 render 与
  第一次逐字节相同，所以幂等与外部编辑检测不受影响。

解析是严格的：字段集合、字段顺序、ID 前缀、时间、正文结构与列表边界不合法一律落成稳定 reason，
绝不静默修复、绝不部分应用（§29.2）。用户文件一律按敏感数据处理（§27.3）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import yaml

from ..config import MemoryConfig
from .models import (
    STATUS_CONFLICT,
    STATUS_DUPLICATE,
    STATUS_FORBIDDEN,
    STATUS_FULL,
    STATUS_INVALID_PROPOSAL,
    STATUS_NOT_FOUND,
    STATUS_NOOP,
    STATUS_OK,
    STATUS_SECRET_DETECTED,
    STATUS_UNAVAILABLE,
    MemoryCandidate,
    MemoryEntry,
    MemoryScope,
    OperationResult,
    ProposalAction,
)

__all__ = [
    "CODEC_REASONS",
    "CodecError",
    "CodecReason",
    "CommonDocument",
    "PrivateDocument",
    "parse_common",
    "parse_private",
    "render_common",
    "render_private",
]

# 六个稳定 reason（INTERFACES §29.2 第 7 条）。超出这个集合的失败是编程错误，不是用户可预期失败。
CodecReason = Literal[
    "not_utf8", "too_large", "bad_schema", "malformed", "duplicate_id", "duplicate_key"
]
CODEC_REASONS: tuple[str, ...] = (
    "not_utf8",
    "too_large",
    "bad_schema",
    "malformed",
    "duplicate_id",
    "duplicate_key",
)

# 正文结构常量（§29.1）。
TITLE_COMMON: str = "# 共同记忆"
TITLE_PRIVATE: str = "# 用户私有记忆"
SECTION_ALL_USER: str = "all_user"
SECTION_LOBBY: str = "lobby"
SECTION_CANDIDATES: str = "candidates"
SECTION_ORDER: tuple[str, ...] = (SECTION_ALL_USER, SECTION_LOBBY, SECTION_CANDIDATES)

# ID 形状（§29.1）：前缀与所在区一致，序号解析接受任意宽度，渲染固定 6 位零填充。
PREFIX_ALL_USER: str = "GM-A-"
PREFIX_LOBBY: str = "GM-L-"
PREFIX_CANDIDATE: str = "MC-"
PREFIX_USER: str = "UM-"
ID_DIGITS: int = 6

# 明细行字段的**顺序**也是结构的一部分：顺序不对就落 malformed，不做宽容解析。
ENTRY_FIELDS: tuple[str, ...] = ("key", "pinned", "created_at", "updated_at")
CANDIDATE_FIELDS: tuple[str, ...] = ("scope", "action", "target_id", "key", "created_at")
OPERATION_FIELDS: tuple[str, ...] = ("status", "object_id", "revision")
FRONT_COMMON: tuple[str, ...] = (
    "schema_version",
    "revision",
    "next_all_user_id",
    "next_lobby_id",
    "next_candidate_id",
    "operations",
)
FRONT_PRIVATE: tuple[str, ...] = (
    "schema_version",
    "revision",
    "private_enabled",
    "auto_capture",
    "next_id",
    "operations",
)

# 稳定状态集合（§27.4）：只有这十个值可以出现在 `operations` 的 status 里。
STATUS_VALUES: tuple[str, ...] = (
    STATUS_OK,
    STATUS_NOOP,
    STATUS_DUPLICATE,
    STATUS_NOT_FOUND,
    STATUS_FORBIDDEN,
    STATUS_UNAVAILABLE,
    STATUS_INVALID_PROPOSAL,
    STATUS_CONFLICT,
    STATUS_FULL,
    STATUS_SECRET_DETECTED,
)

# 映射深度上限：front matter 是「根 → operations → 单条结果」三层；更深的嵌套直接拒绝。
MAX_FRONT_DEPTH: int = 3

_DIGITS = re.compile(r"[0-9]+")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_FENCE = "---"
_SCALAR_WIDTH = 10**9


class CodecError(Exception):
    """解析或渲染失败：`reason` 只取 `CODEC_REASONS` 之一，异常字符串就是 reason 本身。

    绝不携带原始内容：异常会被上层记进日志，而正文与 key 都是敏感数据（§37）。
    """

    def __init__(self, reason: CodecReason) -> None:
        if reason not in CODEC_REASONS:
            raise ValueError(f"未知的 codec reason: {reason!r}")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CommonDocument:
    """`common.md` 的内存形态：front matter 加三个区（INTERFACES §29.1、规划 §5.2）。

    候选与已生效共同记忆同处一份文件，批准才能在一次原子替换里同时完成「移出候选 + 加入生效区」
    （D-58）。`operations` 的键是宿主的 `operation_id`（形状 `<来源>:<message_id>`），渲染时按键
    排序，因此它的插入顺序不影响输出字节。
    """

    schema_version: int = 1
    revision: int = 0
    next_all_user_id: int = 1
    next_lobby_id: int = 1
    next_candidate_id: int = 1
    operations: Mapping[str, OperationResult] = field(default_factory=dict)
    all_user: tuple[MemoryEntry, ...] = ()
    lobby: tuple[MemoryEntry, ...] = ()
    candidates: tuple[MemoryCandidate, ...] = ()


@dataclass(frozen=True)
class PrivateDocument:
    """用户私有文件的内存形态（INTERFACES §29.1、规划 §5.3）。

    用户文件不写原始 user ID、username、消息正文、模型回答或来源频道；文件本身按敏感数据保护，
    条目一律按 ID 升序渲染。
    """

    schema_version: int = 1
    revision: int = 0
    private_enabled: bool = False
    auto_capture: bool = False
    next_id: int = 1
    operations: Mapping[str, OperationResult] = field(default_factory=dict)
    entries: tuple[MemoryEntry, ...] = ()


# --- 公开接口 ---


def parse_common(data: bytes, cfg: MemoryConfig) -> CommonDocument:
    """解析 `common.md`；失败抛 `CodecError`（§29.2）。"""
    text = _decode(data, cfg)
    front_text, body = _split_front_matter(text)
    front = _load_front(front_text)
    _check_front(front, FRONT_COMMON)
    all_user, lobby, candidates = _parse_common_body(body)
    document = CommonDocument(
        schema_version=front["schema_version"],
        revision=front["revision"],
        next_all_user_id=front["next_all_user_id"],
        next_lobby_id=front["next_lobby_id"],
        next_candidate_id=front["next_candidate_id"],
        operations=_parse_operations(front["operations"], cfg),
        all_user=all_user,
        lobby=lobby,
        candidates=candidates,
    )
    _check_common(document, cfg)
    return document


def parse_private(data: bytes, cfg: MemoryConfig) -> PrivateDocument:
    """解析用户私有文件；失败抛 `CodecError`（§29.2）。"""
    text = _decode(data, cfg)
    front_text, body = _split_front_matter(text)
    front = _load_front(front_text)
    _check_front(front, FRONT_PRIVATE)
    document = PrivateDocument(
        schema_version=front["schema_version"],
        revision=front["revision"],
        private_enabled=front["private_enabled"],
        auto_capture=front["auto_capture"],
        next_id=front["next_id"],
        operations=_parse_operations(front["operations"], cfg),
        entries=_parse_private_body(body),
    )
    _check_private(document, cfg)
    return document


def render_common(document: CommonDocument) -> bytes:
    """确定性渲染 `common.md`（§29.3）：区顺序固定，`operations` 按键排序。"""
    _check_common(document, None)
    lines = _render_front_matter(
        (
            ("schema_version", document.schema_version),
            ("revision", document.revision),
            ("next_all_user_id", document.next_all_user_id),
            ("next_lobby_id", document.next_lobby_id),
            ("next_candidate_id", document.next_candidate_id),
        ),
        document.operations,
    )
    lines.append("")
    lines.append(TITLE_COMMON)
    for section, prefix, entries in (
        (SECTION_ALL_USER, PREFIX_ALL_USER, tuple(document.all_user)),
        (SECTION_LOBBY, PREFIX_LOBBY, tuple(document.lobby)),
    ):
        lines.append("")
        lines.append(f"## {section}")
        for item in entries:
            lines.extend(_render_entry(item, 3, prefix))
    lines.append("")
    lines.append(f"## {SECTION_CANDIDATES}")
    for candidate in tuple(document.candidates):
        lines.extend(_render_candidate(candidate))
    return _encode(lines)


def render_private(document: PrivateDocument) -> bytes:
    """确定性渲染用户文件（§29.3）：条目按 ID 升序，同一 document 重复渲染逐字节相同。"""
    _check_private(document, None)
    lines = _render_front_matter(
        (
            ("schema_version", document.schema_version),
            ("revision", document.revision),
            ("private_enabled", document.private_enabled),
            ("auto_capture", document.auto_capture),
            ("next_id", document.next_id),
        ),
        document.operations,
    )
    lines.append("")
    lines.append(TITLE_PRIVATE)
    for item in _sorted_entries(document.entries):
        lines.extend(_render_entry(item, 2, PREFIX_USER))
    return _encode(lines)


# --- 输入侧：字节与结构 ---


def _decode(data: bytes, cfg: MemoryConfig) -> str:
    """先判上限，再严格 UTF-8 解码（§29.2 第 1 条；调用方最多读 `max_file_bytes + 1` 字节）。"""
    if len(data) > cfg.max_file_bytes:
        raise CodecError("too_large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CodecError("not_utf8") from None


def _split_front_matter(text: str) -> tuple[str, list[str]]:
    """切出 front matter 文本与正文行；围栏缺失或未闭合落 malformed（§29.2 第 2 条）。

    行只按 `\\n` 切分（CRLF 允许，行尾的 `\\r` 由 `rstrip` 吸收），不做其它换行符的归一化。
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0].rstrip() != _FENCE:
        raise CodecError("malformed")
    for index in range(1, len(lines)):
        if lines[index].rstrip() == _FENCE:
            return "\n".join(lines[1:index]), lines[index + 1 :]
    raise CodecError("malformed")


class _StrictLoader(yaml.SafeLoader):
    """拒绝重复映射键的 SafeLoader：YAML 默认「后者覆盖前者」，那正是要避免的静默修复。"""

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        seen: list[Any] = []
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise CodecError("duplicate_key")
            seen.append(key)
        return super().construct_mapping(node, deep=deep)


def _load_yaml(text: str) -> object:
    """用严格 loader 解析一段 YAML；任何解析失败都归入 `malformed`。"""
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except (yaml.YAMLError, RecursionError, ValueError):
        # 极深嵌套会让 PyYAML 的递归解析器撞上栈上限；超长数字字面量会让它的整数构造撞上
        # CPython 的整数与字符串转换上限——两者都是「输入让解析器失灵」，不是崩溃。
        raise CodecError("malformed") from None


def _load_front(front_text: str) -> Mapping[str, object]:
    loaded = _load_yaml(front_text)
    if not isinstance(loaded, dict):
        raise CodecError("malformed")
    return loaded


def _check_front(front: Mapping[str, object], expected: tuple[str, ...]) -> None:
    """front matter 的门禁：先认 schema_version，再查键集合与映射深度（§29.2）。"""
    version = front.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise CodecError("bad_schema")
    _check_depth(front, MAX_FRONT_DEPTH)
    if set(front) != set(expected):
        raise CodecError("malformed")


def _check_depth(value: object, limit: int) -> None:
    """映射深度有界（§29.2 第 5 条）：超过 `limit` 层的映射或序列一律拒绝。"""
    if limit < 0:
        raise CodecError("malformed")
    if isinstance(value, Mapping):
        for item in value.values():
            _check_depth(item, limit - 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_depth(item, limit - 1)


def _skip_blank(lines: list[str], index: int) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def _read_fields(lines: list[str], index: int, expected: tuple[str, ...]) -> tuple[dict[str, object], int]:
    """读取条目头部的 `- <字段>: <值>` 块；字段集合与顺序都必须与 `expected` 逐字相同。"""
    index = _skip_blank(lines, index)
    block: list[str] = []
    while index < len(lines) and lines[index].rstrip().startswith("- "):
        block.append(lines[index].rstrip())
        index += 1
    if not block:
        raise CodecError("malformed")
    loaded = _load_yaml("\n".join(block))
    if not isinstance(loaded, list):
        raise CodecError("malformed")
    names: list[str] = []
    values: dict[str, object] = {}
    for item in loaded:
        if not isinstance(item, dict) or len(item) != 1:
            raise CodecError("malformed")
        ((name, value),) = item.items()
        if not isinstance(name, str):
            raise CodecError("malformed")
        names.append(name)
        values[name] = value
    if tuple(names) != expected:
        raise CodecError("malformed")
    return values, index


def _read_body(lines: list[str], index: int) -> tuple[str, int]:
    """读取连续的 Markdown 引用行（§29.2 第 6 条）。

    `> ## 标题`、`> ---`、`> - key: x` 之类都只是正文，不会生成新条目或新字段——正文的每一行都
    带引用前缀，因此正文无法伪造结构。行尾空白在这里**不**裁剪，正文逐字保留。
    """
    index = _skip_blank(lines, index)
    parts: list[str] = []
    while index < len(lines) and lines[index].startswith(">"):
        raw = lines[index]
        if raw.endswith("\r"):
            raw = raw[:-1]  # CRLF 只是行尾写法，不算正文内容
        text = raw[1:]
        if text.startswith(" "):
            text = text[1:]
        parts.append(text)
        index += 1
    return "\n".join(parts), index


def _heading_id(line: str) -> str:
    return line.rstrip().split(" ", 1)[1]


def _parse_entry(lines: list[str], index: int) -> tuple[MemoryEntry, int]:
    memory_id = _heading_id(lines[index])
    fields, index = _read_fields(lines, index + 1, ENTRY_FIELDS)
    content, index = _read_body(lines, index)
    result = MemoryEntry(
        memory_id=memory_id,
        key=fields["key"],
        content=content,
        pinned=fields["pinned"],
        created_at=fields["created_at"],
        updated_at=fields["updated_at"],
    )
    return result, index


def _to_scope(value: object) -> MemoryScope:
    if not isinstance(value, str):
        raise CodecError("malformed")
    try:
        return MemoryScope(value)
    except ValueError:
        raise CodecError("malformed") from None


def _to_action(value: object) -> ProposalAction:
    if not isinstance(value, str):
        raise CodecError("malformed")
    try:
        return ProposalAction(value)
    except ValueError:
        raise CodecError("malformed") from None


def _parse_candidate(lines: list[str], index: int) -> tuple[MemoryCandidate, int]:
    candidate_id = _heading_id(lines[index])
    fields, index = _read_fields(lines, index + 1, CANDIDATE_FIELDS)
    content, index = _read_body(lines, index)
    result = MemoryCandidate(
        candidate_id=candidate_id,
        scope=_to_scope(fields["scope"]),
        action=_to_action(fields["action"]),
        target_id=fields["target_id"],
        key=fields["key"],
        content=content,
        created_at=fields["created_at"],
    )
    return result, index


def _parse_private_body(lines: list[str]) -> tuple[MemoryEntry, ...]:
    entries: list[MemoryEntry] = []
    index = _skip_blank(lines, 0)
    if index >= len(lines) or lines[index].rstrip() != TITLE_PRIVATE:
        raise CodecError("malformed")
    index += 1
    while True:
        index = _skip_blank(lines, index)
        if index >= len(lines):
            return tuple(entries)
        if not lines[index].rstrip().startswith("## "):
            raise CodecError("malformed")
        item, index = _parse_entry(lines, index)
        entries.append(item)


def _parse_common_body(
    lines: list[str],
) -> tuple[tuple[MemoryEntry, ...], tuple[MemoryEntry, ...], tuple[MemoryCandidate, ...]]:
    all_user: list[MemoryEntry] = []
    lobby: list[MemoryEntry] = []
    candidates: list[MemoryCandidate] = []
    buckets: dict[str, list[Any]] = {
        SECTION_ALL_USER: all_user,
        SECTION_LOBBY: lobby,
        SECTION_CANDIDATES: candidates,
    }
    index = _skip_blank(lines, 0)
    if index >= len(lines) or lines[index].rstrip() != TITLE_COMMON:
        raise CodecError("malformed")
    index += 1
    seen = 0
    current: list[Any] | None = None
    while True:
        index = _skip_blank(lines, index)
        if index >= len(lines):
            break
        line = lines[index].rstrip()
        if line.startswith("### "):
            if current is None:
                raise CodecError("malformed")
            if current is candidates:
                candidate, index = _parse_candidate(lines, index)
                candidates.append(candidate)
            else:
                item, index = _parse_entry(lines, index)
                current.append(item)
            continue
        if line.startswith("## "):
            # 三个区必须各出现一次且顺序固定：多、少、乱序都是结构错误。
            if seen >= len(SECTION_ORDER) or line[3:] != SECTION_ORDER[seen]:
                raise CodecError("malformed")
            current = buckets[SECTION_ORDER[seen]]
            seen += 1
            index += 1
            continue
        raise CodecError("malformed")
    if seen != len(SECTION_ORDER):
        raise CodecError("malformed")
    return tuple(all_user), tuple(lobby), tuple(candidates)


def _parse_operations(value: object, cfg: MemoryConfig) -> dict[str, OperationResult]:
    """解析 front matter 的 `operations`：键是宿主的幂等键，值是 `{status, object_id, revision}`。"""
    if not isinstance(value, dict):
        raise CodecError("malformed")
    if len(value) > cfg.max_operations:
        raise CodecError("malformed")
    operations: dict[str, OperationResult] = {}
    for operation_id, raw in value.items():
        _check_operation_id(operation_id)
        if not isinstance(raw, dict) or set(raw) != set(OPERATION_FIELDS):
            raise CodecError("malformed")
        operations[operation_id] = OperationResult(
            status=raw["status"],
            object_id=raw["object_id"],
            revision=raw["revision"],
        )
    return operations


# --- 校验：parse 与 render 共用 ---


def _check_schema_version(value: object) -> None:
    """只认 `schema_version == 1`（整数，布尔不算整数），不猜测兼容（§29.2 第 2 条）。"""
    if not isinstance(value, int) or isinstance(value, bool) or value != 1:
        raise CodecError("bad_schema")


def _check_plain_int(value: object) -> None:
    """只认能渲染成十进制文本的整数：数千位的整数连 `str()` 都转不出来（CPython 默认上限 4300 位）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodecError("malformed")
    try:
        str(value)
    except ValueError:
        raise CodecError("malformed") from None


def _check_bool(value: object) -> None:
    if not isinstance(value, bool):
        raise CodecError("malformed")


def _check_key(value: object) -> None:
    """key 必须是单行、无首尾空白、不含控制字符的字符串；空 key 没有意义。"""
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CodecError("malformed")
    for char in value:
        if ord(char) < 32 or ord(char) == 127 or (char.isspace() and char != " "):
            raise CodecError("malformed")


def _check_operation_id(value: object) -> None:
    """幂等键是不含空白的非空字符串（形状 `<来源>:<message_id>`，但取值由宿主决定）。"""
    if not isinstance(value, str) or not value:
        raise CodecError("malformed")
    for char in value:
        if char.isspace() or ord(char) < 32 or ord(char) == 127:
            raise CodecError("malformed")


def _check_timestamp(value: object) -> None:
    """时间必须是 UTC 的 RFC 3339 字符串；只用于排序与审阅，不参与授权（§29.2 第 8 条）。"""
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise CodecError("malformed")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise CodecError("malformed") from None
    if moment.utcoffset() != timedelta(0):
        raise CodecError("malformed")


def _check_id(value: object, prefix: str) -> str:
    """校验 ID 前缀与十进制序号，返回数字部分（宽度任意，人工改宽过的文件仍能读回）。"""
    if not isinstance(value, str) or not value.startswith(prefix):
        raise CodecError("malformed")
    digits = value[len(prefix) :]
    if _DIGITS.fullmatch(digits) is None:
        raise CodecError("malformed")
    return digits


def _id_key(digits: str) -> str:
    """ID 数字部分的规范形式：`7`、`07`、`000007` 是同一个 ID。

    只去前导零（不做 `int` 转换，任意宽度都安全），判重与排序共用它——两者若各用一套规范化，
    宽度超过 6 位的 ID 就会出现「排序认为相等、判重认为不同」的裂缝。
    """
    return digits.lstrip("0") or "0"


def _check_content(value: object) -> None:
    """正文必须是能写进 UTF-8 文件的字符串。

    孤立代理项（如 `"\\ud800"`，JSON 转义可以产生，§31.2 的字符黑名单挡不住）在 Python 里是合法
    `str`，却编码不出任何字节；在这里拒绝，渲染才保证「要么成功、要么给出稳定理由」。只查正文：
    key 与 ID 由 YAML 转义成 `\\uD800` 写进行内，本来就能往返，不该被牵连。
    """
    if not isinstance(value, str):
        raise CodecError("malformed")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise CodecError("malformed") from None


def _check_entry(item: MemoryEntry, prefix: str) -> None:
    _check_id(item.memory_id, prefix)
    _check_key(item.key)
    _check_bool(item.pinned)
    _check_timestamp(item.created_at)
    _check_timestamp(item.updated_at)
    _check_content(item.content)


def _check_candidate(candidate: MemoryCandidate) -> None:
    """候选的附加规则：作用域只能是共同记忆，动作只能是 add/update，目标必须与作用域同前缀。

    `add` 不得带目标、`update` 必须带目标，与 AI 输出的严格 JSON 合同同口径（§31.2 第 5 条）。
    """
    _check_id(candidate.candidate_id, PREFIX_CANDIDATE)
    if candidate.scope not in (MemoryScope.ALL_USER, MemoryScope.LOBBY):
        raise CodecError("malformed")
    if candidate.action not in (ProposalAction.ADD, ProposalAction.UPDATE):
        raise CodecError("malformed")
    prefix = PREFIX_ALL_USER if candidate.scope == MemoryScope.ALL_USER else PREFIX_LOBBY
    if candidate.action == ProposalAction.ADD:
        if candidate.target_id is not None:
            raise CodecError("malformed")
    else:
        if candidate.target_id is None:
            raise CodecError("malformed")
        _check_id(candidate.target_id, prefix)
    _check_key(candidate.key)
    _check_timestamp(candidate.created_at)
    _check_content(candidate.content)


def _check_operations(operations: object, cfg: MemoryConfig | None) -> None:
    if not isinstance(operations, Mapping):
        raise CodecError("malformed")
    if cfg is not None and len(operations) > cfg.max_operations:
        raise CodecError("malformed")
    for operation_id, result in operations.items():
        _check_operation_id(operation_id)
        if not isinstance(result, OperationResult):
            raise CodecError("malformed")
        if result.status not in STATUS_VALUES:
            raise CodecError("malformed")
        if result.object_id is not None and not isinstance(result.object_id, str):
            raise CodecError("malformed")
        _check_plain_int(result.revision)


def _check_capacity(count: int, limit: int | None) -> None:
    if limit is not None and count > limit:
        raise CodecError("malformed")


def _check_unique(values: Sequence[str], reason: CodecReason) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise CodecError(reason)
        seen.add(value)


def _check_common(document: CommonDocument, cfg: MemoryConfig | None) -> None:
    """`common.md` 的全部语义校验；parse 与 render 都走这里。"""
    _check_schema_version(document.schema_version)
    for value in (
        document.revision,
        document.next_all_user_id,
        document.next_lobby_id,
        document.next_candidate_id,
    ):
        _check_plain_int(value)
    _check_operations(document.operations, cfg)
    all_user = tuple(document.all_user)
    lobby = tuple(document.lobby)
    candidates = tuple(document.candidates)
    _check_capacity(
        max(len(all_user), len(lobby)),
        cfg.max_common_entries_per_scope if cfg is not None else None,
    )
    _check_capacity(len(candidates), cfg.max_candidates if cfg is not None else None)
    for item in all_user:
        _check_entry(item, PREFIX_ALL_USER)
    for item in lobby:
        _check_entry(item, PREFIX_LOBBY)
    for candidate in candidates:
        _check_candidate(candidate)
    # ID 唯一按**归一化后**的形式比：`GM-A-7` 与 `GM-A-000007` 渲染出来是同一个 ID。
    # 归一化与 `_sorted_entries` 的排序键共用 `_id_key`，宽度超过 6 位时两者也不会各说各话。
    _check_unique(
        [PREFIX_ALL_USER + _id_key(_check_id(i.memory_id, PREFIX_ALL_USER)) for i in all_user]
        + [PREFIX_LOBBY + _id_key(_check_id(i.memory_id, PREFIX_LOBBY)) for i in lobby]
        + [
            PREFIX_CANDIDATE + _id_key(_check_id(c.candidate_id, PREFIX_CANDIDATE))
            for c in candidates
        ],
        "duplicate_id",
    )
    # key 在同一作用域内唯一：同 key 只能有一条，更新时替换而不是并存（§29.3）。
    # 候选与已生效条目同 key 是 update 的正常形态，不跨组比较。
    _check_unique([item.key for item in all_user], "duplicate_key")
    _check_unique([item.key for item in lobby], "duplicate_key")
    _check_unique([candidate.key for candidate in candidates], "duplicate_key")


def _check_private(document: PrivateDocument, cfg: MemoryConfig | None) -> None:
    """用户文件的全部语义校验；parse 与 render 都走这里。"""
    _check_schema_version(document.schema_version)
    _check_plain_int(document.revision)
    _check_plain_int(document.next_id)
    _check_bool(document.private_enabled)
    _check_bool(document.auto_capture)
    _check_operations(document.operations, cfg)
    entries = tuple(document.entries)
    _check_capacity(
        len(entries), cfg.max_private_entries_per_user if cfg is not None else None
    )
    for item in entries:
        _check_entry(item, PREFIX_USER)
    _check_unique(
        [PREFIX_USER + _id_key(_check_id(i.memory_id, PREFIX_USER)) for i in entries],
        "duplicate_id",
    )
    _check_unique([item.key for item in entries], "duplicate_key")


# --- 输出侧：渲染 ---


def _dump_scalar(value: object, style: str | None) -> str | None:
    """把标量 dump 成单行文本；折行或多行样式时返回 None（调用方再试双引号样式）。"""
    dumped = yaml.safe_dump(
        {"_": value},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=_SCALAR_WIDTH,
        default_style=style,
    )
    body = dumped[:-1] if dumped.endswith("\n") else dumped
    lines = body.split("\n")
    if len(lines) != 1:
        return None
    head, separator, tail = body.partition(": ")
    if not separator or head not in ("_", '"_"'):
        return None
    return tail


def _scalar_line(value: object, *, force_quote: bool = False) -> str:
    """把标量渲染成一行 YAML 文本。

    朴素样式优先（`key: site.weekly_topic`）；必须加引号时统一用双引号样式（
    `created_at: "2026-09-15T12:30:00Z"`，与规划 §5.2 示例一致），含换行的字符串也退到这里，
    双引号样式会把换行转义成 `\\n`，因此渲染结果永远是单行。
    """
    text = _dump_scalar(value, '"' if force_quote else None)
    if not force_quote and isinstance(value, str) and (text is None or text.startswith("'")):
        text = _dump_scalar(value, '"')
    if text is None:
        raise CodecError("malformed")
    return text


def _render_front_matter(
    scalars: Sequence[tuple[str, object]], operations: Mapping[str, OperationResult]
) -> list[str]:
    lines = [_FENCE]
    for name, value in scalars:
        lines.append(f"{name}: {_scalar_line(value)}")
    if operations:
        lines.append("operations:")
        for operation_id in sorted(operations):
            result = operations[operation_id]
            lines.append(f"  {_scalar_line(operation_id, force_quote=True)}:")
            lines.append(f"    status: {_scalar_line(result.status)}")
            lines.append(f"    object_id: {_scalar_line(result.object_id)}")
            lines.append(f"    revision: {_scalar_line(result.revision)}")
    else:
        lines.append("operations: {}")
    lines.append(_FENCE)
    return lines


def _body_lines(content: str) -> list[str]:
    """正文渲染成引用行；空行输出成光杆 `>`，不在文件里留行尾空白。"""
    if content == "":
        return []
    return [f"> {line}" if line else ">" for line in content.split("\n")]


def _render_entry(item: MemoryEntry, level: int, prefix: str) -> list[str]:
    lines = [
        "",
        f"{'#' * level} {prefix}{_check_id(item.memory_id, prefix).zfill(ID_DIGITS)}",
        "",
        f"- key: {_scalar_line(item.key)}",
        f"- pinned: {_scalar_line(item.pinned)}",
        f"- created_at: {_scalar_line(item.created_at)}",
        f"- updated_at: {_scalar_line(item.updated_at)}",
        "",
    ]
    lines.extend(_body_lines(item.content))
    return lines


def _render_candidate(candidate: MemoryCandidate) -> list[str]:
    lines = [
        "",
        f"### {PREFIX_CANDIDATE}{_check_id(candidate.candidate_id, PREFIX_CANDIDATE).zfill(ID_DIGITS)}",
        "",
        f"- scope: {_scalar_line(str(candidate.scope))}",
        f"- action: {_scalar_line(str(candidate.action))}",
        f"- target_id: {_scalar_line(candidate.target_id)}",
        f"- key: {_scalar_line(candidate.key)}",
        f"- created_at: {_scalar_line(candidate.created_at)}",
        "",
    ]
    lines.extend(_body_lines(candidate.content))
    return lines


def _sorted_entries(entries: Sequence[MemoryEntry]) -> tuple[MemoryEntry, ...]:
    """按 ID 升序排序：序号按数值比较，因此 `UM-9` 排在 `UM-10` 之前（§29.3）。

    排序键是 `_id_key` 的结果（先比长度再比字典序），与判重共用同一套规范化，任意宽度都成立。
    """

    def sort_key(item: MemoryEntry) -> tuple[int, str]:
        canonical = _id_key(_check_id(item.memory_id, PREFIX_USER))
        return (len(canonical), canonical)

    return tuple(sorted(entries, key=sort_key))


def _encode(lines: Sequence[str]) -> bytes:
    """整份文档拼成 UTF-8 字节；编码失败是兜底，正常路径由 `_check_content` 在校验边界挡下。"""
    try:
        return ("\n".join(lines) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise CodecError("malformed") from None
