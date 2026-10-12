"""画像三类文件的严格解析与确定性渲染（设计 §8 的目录布局）。

三份文件都是「front matter + 固定结构的 Markdown 正文」：

```
personas/users/<owner_key>.md        # 私有画像：短文与特征
personas/workflows/<owner_key>.md    # 工作流：来源摘要、问题、回答、草稿、修改意见
personas/public/<owner_key>.md       # 公开快照：只有短文
```

正文里的问题、回答、草稿与摘要都是**按本次流程授权保存的私有内容**：它们只允许出现在目标
Markdown、面向所属用户的明确展示，以及撰写器请求的 `role="user"` 消息里，绝不进日志、SQLite
或 system prompt（设计 §8）。渲染与解析都是纯同步函数，不做文件 I/O，也不 import `app.py`。

两条硬不变量（与 `memory.codec` 同一套理由）：

- **render 与 parse 共用同一套语义校验**（`persona.models` 的 `validate_*`）：render 写出的文档
  parse 一定读得回，非法文档在 render 侧就抛 `CodecError`，不会写出半截文件。
- **确定性**：同一份 dict 重复渲染逐字节相同 —— front matter 字段顺序固定、`operations` 按键排序、
  特征块按列表顺序编号。这是服务做幂等与外部编辑检测的基础。

解析是严格的：字段集合与顺序、标量类型、时间形状、标题与分节顺序不合法一律落成稳定 reason，
绝不静默修复、绝不部分应用。失败复用 `memory.codec.CodecError`，因此 `MemoryService` 的原子写与
快照机制（它按 reason 记日志、按 reason 判断是否接手外部版本）无需为画像再加一套分支。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from ..config import MemoryConfig
from ..memory.codec import CodecError
from .models import (
    FEATURE_FIELDS,
    PERSONA_SCHEMA_VERSION,
    PersonaValidationError,
    validate_persona,
    validate_public_persona,
    validate_workflow,
)

__all__ = [
    "FRONT_PERSONA",
    "FRONT_PUBLIC_PERSONA",
    "FRONT_WORKFLOW",
    "SECTION_ANSWERS",
    "SECTION_CORRECTION",
    "SECTION_DRAFT",
    "SECTION_FEATURES",
    "SECTION_PARAGRAPH",
    "SECTION_QUESTIONS",
    "SECTION_SUMMARIES",
    "TITLE_PERSONA",
    "TITLE_PUBLIC_PERSONA",
    "TITLE_WORKFLOW",
    "parse_persona",
    "parse_public_persona",
    "parse_workflow",
    "render_persona",
    "render_public_persona",
    "render_workflow",
]

# 三份文档的标题与固定的二级小节。多行正文（短文、修改意见、问题、回答、摘要）一律走
# 「`### <编号>` + 引用块」的小节形式：引用前缀让正文无法伪造结构，也让回答与问题可以
# 自然换行 —— 用户的回答本来就是多行文本，单行字段会把它们挡在门外。
TITLE_PERSONA: str = "# 用户画像"
TITLE_WORKFLOW: str = "# 画像工作流"
TITLE_PUBLIC_PERSONA: str = "# 用户公开画像"
SECTION_PARAGRAPH: str = "短文"
SECTION_OBSERVATIONS: str = "观察"
SECTION_FEATURES: str = "特征"
SECTION_SUMMARIES: str = "来源摘要"
SECTION_QUESTIONS: str = "问题"
SECTION_ANSWERS: str = "回答"
SECTION_DRAFT: str = "草稿"
SECTION_CORRECTION: str = "修改意见"

# front matter 的字段顺序也是结构的一部分（渲染按它输出，解析只查键集合）。
FRONT_PERSONA: tuple[str, ...] = (
    "schema_version",
    "revision",
    "enabled",
    "migrated",
    "username",
    "updated_at",
    "last_change",
    "update_day",
    "update_count",
    "last_update_at",
    "last_message_id",
    "pending_unpublish",
    "committed_draft_id",
    "operations",
)
FRONT_WORKFLOW: tuple[str, ...] = (
    "schema_version",
    "revision",
    "workflow_id",
    "kind",
    "stage",
    "base_revision",
    "base_fingerprint",
    "watermark",
    "blog_count",
    "message_count",
    "model_calls_used",
    "draft_id",
    "draft_version",
    "revision_used",
    "delivered",
    "delivered_message_id",
    "questions_message_id",
    "expires_at",
    "enabled_on_commit",
    "operations",
)
FRONT_PUBLIC_PERSONA: tuple[str, ...] = (
    "schema_version",
    "revision",
    "owner_username",
    "source_revision",
    "published_at",
    "operations",
)

# 正文小节的固定顺序：多一节、少一节、换序都是结构错误。
_WORKFLOW_SECTIONS: tuple[str, ...] = (
    SECTION_SUMMARIES,
    SECTION_QUESTIONS,
    SECTION_ANSWERS,
    SECTION_DRAFT,
    SECTION_FEATURES,
    SECTION_CORRECTION,
)

_FENCE: str = "---"

# 需要**强制**加引号的 front matter 字段：指纹是纯十六进制 token，理论上可能整串都是数字，
# 那时 YAML 会把它读成整数（校验只认字符串），文件就读不回来了。加引号没有任何代价。
_FORCE_QUOTED_FRONT: frozenset[str] = frozenset(("base_fingerprint",))

# front matter 的映射深度上限（根 → operations → 单条结果）。
_MAX_DEPTH: int = 3
_SCALAR_WIDTH: int = 10**9

# 校验 reason → codec reason 的映射：codec 只认记忆那套六个稳定值，语义差异在 reason 里体现
# 已经足够（服务只把它们记进日志的 `reason` 字段，不做分支判断）。
_REASON_MAP: dict[str, str] = {
    "bad_schema": "bad_schema",
    "duplicate": "duplicate_key",
    "too_long": "malformed",
    "unknown_value": "malformed",
    "malformed": "malformed",
}


# --- 公开接口 -------------------------------------------------------------


def parse_persona(data: bytes, cfg: MemoryConfig) -> dict[str, Any]:
    """解析私有画像文件；失败抛 `CodecError`。"""
    text = _decode(data, cfg)
    front_text, lines = _split_front_matter(text)
    front = _load_front(front_text)
    _check_front(front, FRONT_PERSONA)
    paragraph, observations, features = _parse_persona_body(lines)
    document: dict[str, Any] = dict(front)
    document["paragraph"] = paragraph
    document["observations"] = observations
    document["features"] = features
    _validated(validate_persona, document)
    return document


def render_persona(document: Mapping[str, Any]) -> bytes:
    """确定性渲染私有画像文件。"""
    check = dict(document)
    _validated(validate_persona, check)
    lines = _render_front(FRONT_PERSONA, check)
    lines.append("")
    lines.append(TITLE_PERSONA)
    lines.append("")
    lines.append(f"## {SECTION_PARAGRAPH}")
    lines.extend(_body_lines(check["paragraph"]))
    lines.append("")
    lines.append(f"## {SECTION_OBSERVATIONS}")
    lines.extend(_block_lines(check["observations"], str_keys=False))
    lines.append("")
    lines.append(f"## {SECTION_FEATURES}")
    lines.extend(_render_features(check["features"]))
    return _encode(lines)


def parse_workflow(data: bytes, cfg: MemoryConfig) -> dict[str, Any]:
    """解析工作流文件；失败抛 `CodecError`。"""
    text = _decode(data, cfg)
    front_text, lines = _split_front_matter(text)
    front = _load_front(front_text)
    _check_front(front, FRONT_WORKFLOW)
    body = _parse_workflow_body(lines)
    document: dict[str, Any] = dict(front)
    document.update(body)
    _validated(validate_workflow, document)
    return document


def render_workflow(document: Mapping[str, Any]) -> bytes:
    """确定性渲染工作流文件。"""
    check = dict(document)
    _validated(validate_workflow, check)
    lines = _render_front(FRONT_WORKFLOW, check)
    lines.append("")
    lines.append(TITLE_WORKFLOW)
    lines.append("")
    lines.append(f"## {SECTION_SUMMARIES}")
    lines.extend(_block_lines(check["source_summaries"], str_keys=False))
    lines.append("")
    lines.append(f"## {SECTION_QUESTIONS}")
    lines.extend(_block_lines(check["questions"], str_keys=False))
    lines.append("")
    lines.append(f"## {SECTION_ANSWERS}")
    lines.extend(_block_lines(check["answers"], str_keys=True))
    lines.append("")
    lines.append(f"## {SECTION_DRAFT}")
    lines.extend(_body_lines(check["paragraph"]))
    lines.append("")
    lines.append(f"## {SECTION_FEATURES}")
    lines.extend(_render_features(check["features"]))
    lines.append("")
    lines.append(f"## {SECTION_CORRECTION}")
    lines.extend(_body_lines(check["correction"]))
    return _encode(lines)


def parse_public_persona(data: bytes, cfg: MemoryConfig) -> dict[str, Any]:
    """解析公开快照文件；失败抛 `CodecError`。空快照（撤回后的墓碑）同样合法。"""
    text = _decode(data, cfg)
    front_text, lines = _split_front_matter(text)
    front = _load_front(front_text)
    _check_front(front, FRONT_PUBLIC_PERSONA)
    paragraph = _parse_public_body(lines)
    document: dict[str, Any] = dict(front)
    document["paragraph"] = paragraph
    _validated(validate_public_persona, document)
    return document


def render_public_persona(document: Mapping[str, Any]) -> bytes:
    """确定性渲染公开快照文件。"""
    check = dict(document)
    _validated(validate_public_persona, check)
    lines = _render_front(FRONT_PUBLIC_PERSONA, check)
    lines.append("")
    lines.append(TITLE_PUBLIC_PERSONA)
    lines.extend(_body_lines(check["paragraph"]))
    return _encode(lines)


# --- 输入侧：字节与结构 ---------------------------------------------------


def _decode(data: bytes, cfg: MemoryConfig) -> str:
    """先判上限，再严格 UTF-8 解码（`cfg.max_file_bytes` 是唯一的字节上界）。"""
    if len(data) > cfg.max_file_bytes:
        raise CodecError("too_large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CodecError("not_utf8") from None


def _split_front_matter(text: str) -> tuple[str, list[str]]:
    """切出 front matter 与正文行；围栏缺失或未闭合落 `malformed`。"""
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
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except (yaml.YAMLError, RecursionError, ValueError):
        # 极深嵌套撞上递归上限、超长数字字面量撞上整数转换上限：两者都是「输入让解析器失灵」。
        raise CodecError("malformed") from None


def _load_front(front_text: str) -> Mapping[str, Any]:
    loaded = _load_yaml(front_text)
    if not isinstance(loaded, dict):
        raise CodecError("malformed")
    return loaded


def _check_front(front: Mapping[str, Any], expected: Sequence[str]) -> None:
    """front matter 的门禁：先认 schema_version，再查键集合与映射深度。"""
    version = front.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != PERSONA_SCHEMA_VERSION:
        raise CodecError("bad_schema")
    _check_depth(front, _MAX_DEPTH)
    if set(front) != set(expected):
        raise CodecError("malformed")


def _check_depth(value: object, limit: int, visited: dict[int, int] | None = None) -> None:
    """映射深度有界：YAML 别名可以让同一棵子树按出现次数被反复遍历（见 memory.codec 的分析）。

    `visited` 记下每个容器**已走完**的最小 `limit`，因此每个容器最多被遍历
    `_MAX_DEPTH + 1` 遍，总工作量与文档大小成线性；含环的锚点仍由 `limit < 0` 终止。
    """
    if limit < 0:
        raise CodecError("malformed")
    if not isinstance(value, (Mapping, list, tuple)):
        return
    if visited is None:
        visited = {}
    marker = id(value)
    bound = visited.get(marker)
    if bound is not None and bound <= limit:
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _check_depth(item, limit - 1, visited)
    else:
        for item in value:
            _check_depth(item, limit - 1, visited)
    visited[marker] = limit


def _skip_blank(lines: Sequence[str], index: int) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def _expect_line(lines: Sequence[str], index: int, expected: str) -> int:
    index = _skip_blank(lines, index)
    if index >= len(lines) or lines[index].rstrip() != expected:
        raise CodecError("malformed")
    return index + 1


def _expect_end(lines: Sequence[str], index: int) -> None:
    if _skip_blank(lines, index) != len(lines):
        raise CodecError("malformed")


def _read_quote(lines: Sequence[str], index: int) -> tuple[str, int]:
    """读取连续的 Markdown 引用行；正文的每一行都带引用前缀，因此正文无法伪造结构。"""
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


def _read_blocks(
    lines: Sequence[str], index: int, *, str_keys: bool
) -> tuple[list[tuple[int | str, str]], int]:
    """读取连续的 `### <键>` + 引用块小节。

    `str_keys=False` 时键必须是位置编号（1、2、…，从 1 起连续），用于「有顺序的文本列表」；
    `str_keys=True` 时键必须是字符串（渲染侧一律加引号，避免 `1` 与 `"1"` 两种键），
    用于回答表 —— 用户可以只回答其中几题，编号因此不要求连续。
    """
    blocks: list[tuple[int | str, str]] = []
    while True:
        index = _skip_blank(lines, index)
        if index >= len(lines) or not lines[index].rstrip().startswith("### "):
            return blocks, index
        key = _load_yaml(lines[index].rstrip()[4:])
        if str_keys:
            if not isinstance(key, str):
                raise CodecError("malformed")
        else:
            if isinstance(key, bool) or not isinstance(key, int) or key != len(blocks) + 1:
                raise CodecError("malformed")
        body, index = _read_quote(lines, index + 1)
        blocks.append((key, body))


def _read_field_block(lines: Sequence[str], index: int, expected: Sequence[str]) -> tuple[dict[str, Any], int]:
    """读取 `- 名称: 值` 字段块：字段集合与**顺序**都必须与 `expected` 逐字相同。"""
    index = _skip_blank(lines, index)
    names: list[str] = []
    values: dict[str, Any] = {}
    while index < len(lines) and lines[index].lstrip().startswith("- "):
        item = _load_yaml(lines[index].lstrip()[2:])
        if not isinstance(item, dict) or len(item) != 1:
            raise CodecError("malformed")
        ((name, value),) = item.items()
        if not isinstance(name, str):
            raise CodecError("malformed")
        names.append(name)
        values[name] = value
        index += 1
    if tuple(names) != tuple(expected):
        raise CodecError("malformed")
    return values, index


def _parse_persona_body(lines: Sequence[str]) -> tuple[str, list[str], list[dict[str, Any]]]:
    index = _expect_line(lines, 0, TITLE_PERSONA)
    index = _expect_line(lines, index, f"## {SECTION_PARAGRAPH}")
    paragraph, index = _read_quote(lines, index)
    index = _expect_line(lines, index, f"## {SECTION_OBSERVATIONS}")
    blocks, index = _read_blocks(lines, index, str_keys=False)
    observations = [body for _, body in blocks]
    index = _expect_line(lines, index, f"## {SECTION_FEATURES}")
    features, index = _read_features(lines, index)
    _expect_end(lines, index)
    return paragraph, observations, features


def _read_features(lines: Sequence[str], index: int) -> tuple[list[dict[str, Any]], int]:
    """读取连续的 `### <编号>` + `- 字段: 值` 特征块；编号必须从 1 起连续。"""
    features: list[dict[str, Any]] = []
    while True:
        index = _skip_blank(lines, index)
        if index >= len(lines) or not lines[index].rstrip().startswith("### "):
            return features, index
        if lines[index].rstrip() != f"### {len(features) + 1}":
            # 编号必须从 1 起连续：它是渲染侧写出来的顺序，人工改乱即结构错误。
            raise CodecError("malformed")
        fields, index = _read_field_block(lines, index + 1, FEATURE_FIELDS)
        features.append(dict(fields))


def _render_features(features: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = [""]
    for index, feature in enumerate(features, start=1):
        lines.append("")
        lines.append(f"### {index}")
        lines.append("")
        for name in FEATURE_FIELDS:
            lines.append(f"- {name}: {_scalar_line(feature[name])}")
    return lines


def _parse_workflow_body(lines: Sequence[str]) -> dict[str, Any]:
    index = _expect_line(lines, 0, TITLE_WORKFLOW)
    index = _expect_line(lines, index, f"## {_WORKFLOW_SECTIONS[0]}")
    summaries, index = _read_blocks(lines, index, str_keys=False)
    index = _expect_line(lines, index, f"## {_WORKFLOW_SECTIONS[1]}")
    questions, index = _read_blocks(lines, index, str_keys=False)
    index = _expect_line(lines, index, f"## {_WORKFLOW_SECTIONS[2]}")
    answer_blocks, index = _read_blocks(lines, index, str_keys=True)
    index = _expect_line(lines, index, f"## {_WORKFLOW_SECTIONS[3]}")
    paragraph, index = _read_quote(lines, index)
    index = _expect_line(lines, index, f"## {_WORKFLOW_SECTIONS[4]}")
    features, index = _read_features(lines, index)
    index = _expect_line(lines, index, f"## {_WORKFLOW_SECTIONS[5]}")
    correction, index = _read_quote(lines, index)
    _expect_end(lines, index)
    answers: dict[str, str] = {}
    for key, body in answer_blocks:
        assert isinstance(key, str)
        if key in answers:
            # 重复的题号：YAML 的重复键检查管不到跨小节的重复，只能在这里查。
            raise CodecError("duplicate_key")
        answers[key] = body
    return {
        "source_summaries": [body for _, body in summaries],
        "questions": [body for _, body in questions],
        "answers": answers,
        "paragraph": paragraph,
        "features": features,
        "correction": correction,
    }


def _parse_public_body(lines: Sequence[str]) -> str:
    index = _expect_line(lines, 0, TITLE_PUBLIC_PERSONA)
    paragraph, index = _read_quote(lines, index)
    _expect_end(lines, index)
    return paragraph


# --- 输出侧：渲染 ---------------------------------------------------------


def _dump_scalar(value: object, style: str | None) -> str | None:
    """把标量 dump 成单行文本；折行或多行样式时返回 None（调用方据此判失败）。"""
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

    空列表渲染成 `[]`（仍然单行）；必须加引号时统一走双引号样式；含换行的字符串会退回多行
    样式，这里直接判失败 —— 单行字段的合法性已由 `persona.models` 的校验保证，走到这里说明
    调用方绕过了校验，与其写出半截文件不如给出稳定 reason。
    """
    if isinstance(value, (list, tuple)):
        # 列表（目前只有证据引用）渲染成单行流式序列：`["blog:1", "question:2"]`。
        # 块状序列会让「一个字段占多行」的解析多出一层缩进状态，而这里只有一个列表字段。
        dumped = yaml.safe_dump(
            list(value), allow_unicode=True, default_flow_style=True, width=_SCALAR_WIDTH
        ).rstrip("\n")
        if "\n" in dumped:
            raise CodecError("malformed")
        return dumped
    text = _dump_scalar(value, '"' if force_quote else None)
    if not force_quote and isinstance(value, str) and (text is None or text.startswith("'")):
        text = _dump_scalar(value, '"')
    if text is None:
        raise CodecError("malformed")
    return text


def _render_front(expected: Sequence[str], document: Mapping[str, Any]) -> list[str]:
    lines = [_FENCE]
    for name in expected:
        if name == "operations":
            continue
        lines.append(
            f"{name}: {_scalar_line(document[name], force_quote=name in _FORCE_QUOTED_FRONT)}"
        )
    lines.extend(_render_operations(document["operations"]))
    lines.append(_FENCE)
    return lines


def _render_operations(operations: Mapping[str, Any]) -> list[str]:
    if not operations:
        return ["operations: {}"]
    lines = ["operations:"]
    for operation_id in sorted(operations):
        result = operations[operation_id]
        lines.append(f"  {_scalar_line(operation_id, force_quote=True)}:")
        lines.append(f"    status: {_scalar_line(result['status'])}")
        lines.append(f"    object_id: {_scalar_line(result['object_id'])}")
        lines.append(f"    revision: {_scalar_line(result['revision'])}")
    return lines


def _body_lines(content: str) -> list[str]:
    """正文渲染成引用行：先补一个空行（与标题分隔），空行输出成光杆 `>`。"""
    lines: list[str] = [""]
    for line in content.split("\n") if content else []:
        lines.append(f"> {line}" if line else ">")
    return lines


def _block_lines(items: object, *, str_keys: bool) -> list[str]:
    """渲染一组「`### <键>` + 引用块」小节。

    位置列表用整数键（`### 1`），回答表用字符串键（`### "1"`）；两者都由解析侧分别校验，
    因此渲染出来的文件一定读得回。
    """
    lines: list[str] = [""]
    if isinstance(items, Mapping):
        sources = [(key, items[key]) for key in sorted(items, key=_answer_key)]
    else:
        assert isinstance(items, Sequence)
        sources = [(index, item) for index, item in enumerate(items, start=1)]
    for key, body in sources:
        lines.append("")
        lines.append(f"### {key}" if not str_keys else f"### {_scalar_line(key, force_quote=True)}")
        lines.extend(_body_lines(body))
    return lines


def _answer_key(key: str) -> tuple[int, str]:
    """回答按键的数值序排序（`"2"` 排在 `"10"` 之前）；非数字键退化为字典序。"""
    return (0, f"{int(key):09d}") if key.isdigit() else (1, key)


def _encode(lines: Sequence[str]) -> bytes:
    try:
        return ("\n".join(lines) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        # 正文里的孤立代理项：语义校验本该拦下它，这里只是兜底。
        raise CodecError("malformed") from None


def _validated(check: Any, document: dict[str, Any]) -> None:
    """跑一遍语义校验，把 `PersonaValidationError` 收敛成 `CodecError`。"""
    try:
        check(document)
    except PersonaValidationError as exc:
        raise CodecError(_REASON_MAP.get(exc.reason, "malformed")) from None
