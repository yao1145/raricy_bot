"""用户画像的数据底座：字段形状、默认值、严格校验与进程内令牌（设计 §5、§8、§9）。

画像的三份文档（私有画像、工作流、公开快照）在**内存与接口上都是纯 dict**：契约要求
`persona_get` / `persona_workflow` 返回 dict，`PersonaUpdateToken` 直接暴露 `document`，模型侧
（`writer.py`）也只产出 `{paragraph, features}`。「纯 dict」还避免了一层 dataclass 与 dict 之间的
双向转换 —— 那种转换里最容易出现「少抄一个字段」，而少抄的字段落盘后就是一次静默的数据丢失。

本模块只做三件事，不做 I/O、不 import `app.py`（D-61）：

- **默认值**：`default_persona` / `default_workflow` / `default_public_persona`。新文件、空文件与
  「文件被删掉」三种情形共用同一份基线，因此 revision=0 的含义在整条链路上一致。
- **严格校验**：字段集合、类型、上界与取值域。多一个字段、少一个字段、类型不对都拒绝，
  绝不做「忽略未知字段、采纳其余」的宽容解析 —— 宽容解析会把一次越权或一次笔误变成静默通过
  的事件（与 `memory` 的 D-57 同款）。校验失败抛 `PersonaValidationError`，异常串只有稳定
  reason，绝不带原始内容（正文一律按敏感数据保护）。
- **进程内令牌**：`PersonaUpdateToken`。它只存在于内存，绝不落盘；重启后没有旧的模型调用会
  继续返回，因此不需要持久字段（与记忆的自动提取令牌同一套理由）。

用户可见正文（短文、特征值、观察摘要、问题与回答）**只**允许出现在目标 Markdown、面向所属
用户的明确展示，以及允许的 `role="user"` 模型请求里；不进入日志、SQLite、system prompt 或文件名。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# 稳定状态 token：与记忆共用同一套取值（同一件事只有一种写法），另加两个 persona 专用值。
# 它们是对外承诺的字符串，逐字固定，不得新增同义写法或按用途重命名。
from ..memory.models import (
    STATUS_CONFLICT,
    STATUS_FORBIDDEN,
    STATUS_INVALID_PROPOSAL,
    STATUS_NOT_FOUND,
    STATUS_NOOP,
    STATUS_OK,
    STATUS_SECRET_DETECTED,
    STATUS_UNAVAILABLE,
)

__all__ = [
    "FEATURE_CONFIDENCES",
    "FEATURE_DIMENSIONS",
    "FEATURE_FIELDS",
    "FEATURE_ORIGINS",
    "MAX_EVIDENCE_REFS",
    "MAX_FEATURES",
    "MAX_FEATURE_VALUE_CHARS",
    "MAX_OBSERVATIONS",
    "MAX_OBSERVATION_CHARS",
    "MAX_PARAGRAPH_CHARS",
    "MAX_QUESTIONS",
    "MAX_SOURCE_SUMMARIES",
    "MAX_SUMMARY_CHARS",
    "PERSONA_FIELDS",
    "PERSONA_PUBLIC_GROUP",
    "PERSONA_SCHEMA_VERSION",
    "PERSONA_USER_GROUP",
    "PUBLIC_FIELDS",
    "PersonaUpdateToken",
    "PersonaValidationError",
    "STATUS_CONFLICT",
    "STATUS_EXPIRED",
    "STATUS_FORBIDDEN",
    "STATUS_INVALID_PROPOSAL",
    "STATUS_NOT_DELIVERED",
    "STATUS_NOT_FOUND",
    "STATUS_NOOP",
    "STATUS_OK",
    "STATUS_SECRET_DETECTED",
    "STATUS_UNAVAILABLE",
    "TERMINAL_STAGES",
    "VALIDATION_REASONS",
    "WORKFLOW_FIELDS",
    "WORKFLOW_KINDS",
    "WORKFLOW_STAGES",
    "cleared_workflow",
    "default_persona",
    "default_public_persona",
    "default_workflow",
    "is_terminal_stage",
    "locked_feature_keys",
    "payload_of",
    "validate_features",
    "validate_persona",
    "validate_public_persona",
    "validate_workflow",
    "workflow_is_empty",
]

# 画像 schema 版本：与记忆的 `schema_version=1` 各自独立命名，旧 schema 不直接升级覆盖（设计 §8）。
PERSONA_SCHEMA_VERSION: int = 1

# 两个 persona 补充资料分组名（`SupplementalItem.group`）：私有短文与公开快照各一个。
# 常量在这里定，是因为它同时是 `core/context.py` 的标签键与 C 组装私有条目时的取值 ——
# 三处硬编码同一个字符串迟早会分叉。
PERSONA_USER_GROUP: str = "persona_user"
PERSONA_PUBLIC_GROUP: str = "persona_public"

# 四个特征维度、三种来源与三档置信度（设计 §5 的表）。
FEATURE_DIMENSIONS: tuple[str, ...] = ("knowledge", "interests", "expression", "thinking")
FEATURE_ORIGINS: tuple[str, ...] = ("self_report", "inference", "user_revision")
FEATURE_CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")

# 特征 dict 的**精确**字段集合与顺序（契约 §「Feature dict EXACT fields」）：
# 顺序也是结构的一部分，codec 的渲染与解析都按它逐字比对。
FEATURE_FIELDS: tuple[str, ...] = (
    "dimension",
    "value",
    "origin",
    "confidence",
    "evidence_refs",
    "first_seen_at",
    "last_seen_at",
    "user_locked",
)

# 私有画像、工作流与公开快照的字段集合（codec 与校验共用）。
PERSONA_FIELDS: tuple[str, ...] = (
    "schema_version",
    "revision",
    "enabled",
    "migrated",
    "paragraph",
    "features",
    "username",
    "updated_at",
    "last_change",
    "observations",
    "update_day",
    "update_count",
    "last_update_at",
    "last_message_id",
    "pending_unpublish",
    "committed_draft_id",
    "operations",
)

WORKFLOW_FIELDS: tuple[str, ...] = (
    "schema_version",
    "revision",
    "workflow_id",
    "kind",
    "stage",
    "base_revision",
    # 生效画像在**流程开始时**的内容指纹（sha256 十六进制；空串表示「这一版之前的历史文件」）。
    # 由服务在保存一条新流程时按当时的私有画像快照自动写入，之后原样保留；控制器不设置它。
    "base_fingerprint",
    "watermark",
    "blog_count",
    "message_count",
    "source_summaries",
    "model_calls_used",
    "questions",
    "answers",
    "paragraph",
    "features",
    "draft_id",
    "draft_version",
    "revision_used",
    "delivered",
    "delivered_message_id",
    "questions_message_id",
    "expires_at",
    "enabled_on_commit",
    "correction",
    "operations",
)

PUBLIC_FIELDS: tuple[str, ...] = (
    "schema_version",
    "revision",
    "owner_username",
    "source_revision",
    "published_at",
    "paragraph",
    "operations",
)

# 四种工作流种类与九个阶段（设计 §9）。
WORKFLOW_KINDS: tuple[str, ...] = ("create", "edit", "public", "clear")
WORKFLOW_STAGES: tuple[str, ...] = (
    "collecting",
    "awaiting_answers",
    "draft_pending_delivery",
    "awaiting_review",
    "revising",
    "awaiting_final_review",
    "committed",
    "cancelled",
    "expired",
)
# 终态：到达之后流程不再前进，正文按墓碑清空（设计 §9）。
TERMINAL_STAGES: frozenset[str] = frozenset(("committed", "cancelled", "expired"))

# 上界一律是**代码常量**，不可由 YAML 改：它们保护的是文件与内存，而不是产品策略。
# 产品侧的段落上限（`max_paragraph_chars`，默认 500）由撰写器与控制器负责；这里只挡住
# 「一次写入把文件撑爆」的那条路。
MAX_PARAGRAPH_CHARS: int = 2000
MAX_FEATURE_VALUE_CHARS: int = 300
MAX_FEATURES: int = 32
MAX_EVIDENCE_REFS: int = 8
MAX_OBSERVATIONS: int = 64
MAX_OBSERVATION_CHARS: int = 500
MAX_SOURCE_SUMMARIES: int = 32
MAX_SUMMARY_CHARS: int = 500
MAX_QUESTIONS: int = 5
MAX_ANSWER_CHARS: int = 2000
MAX_CORRECTION_CHARS: int = 2000
MAX_IDENTIFIER_CHARS: int = 64

# 时间与日期的形状（与 `memory.codec` 的 `_TIMESTAMP` 同一口径）：秒级或带小数、必须带时区，
# 且只能是 UTC（下面再查一次 utcoffset）。
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# 内容指纹的形状：sha256 的十六进制摘要，固定小写、固定 64 位。
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")

# persona 专用状态 token（另两个取值见 `memory.models`）。
STATUS_EXPIRED: str = "expired"
STATUS_NOT_DELIVERED: str = "not_delivered"

# 校验失败的稳定 reason（与 codec 的六个 reason 分开：这边描述的是**语义**而不是字节结构）。
VALIDATION_REASONS: tuple[str, ...] = (
    "bad_schema",
    "malformed",
    "too_long",
    "unknown_value",
    "duplicate",
)


class PersonaValidationError(Exception):
    """画像文档不合法：`reason` 只取 `VALIDATION_REASONS` 之一，异常串就是 reason 本身。

    绝不携带原始内容：异常会被上层收敛成稳定状态并可能记进日志，而正文是敏感数据。
    """

    def __init__(self, reason: str) -> None:
        if reason not in VALIDATION_REASONS:
            raise ValueError(f"未知的校验 reason: {reason!r}")
        self.reason = reason
        super().__init__(reason)


def default_persona() -> dict[str, Any]:
    """一份空私有画像（新文件、空文件与「文件被删掉」共用同一基线）。

    `migrated=False` 表示该用户还没有完成过一次画像提交：旧个人记忆仍是他的现行个人资料，
    因此提交前必须先撤回旧公开记忆（设计 §11）。`enabled=False` 与 `pending_unpublish=False`
    是「未开启、没有待撤回」的初始状态。
    """
    return {
        "schema_version": PERSONA_SCHEMA_VERSION,
        "revision": 0,
        "enabled": False,
        "migrated": False,
        "paragraph": "",
        "features": [],
        "username": "",
        "updated_at": "",
        "last_change": "",
        "observations": [],
        "update_day": "",
        "update_count": 0,
        "last_update_at": 0.0,
        "last_message_id": 0,
        "pending_unpublish": False,
        "committed_draft_id": "",
        "operations": {},
    }


def default_workflow() -> dict[str, Any]:
    """一份空工作流。`workflow_id` 为空串表示「这个用户没有待办流程」（文件不存在同义）。"""
    return {
        "schema_version": PERSONA_SCHEMA_VERSION,
        "revision": 0,
        "workflow_id": "",
        "kind": "create",
        "stage": "collecting",
        "base_revision": 0,
        "base_fingerprint": "",
        "watermark": 0,
        "blog_count": 0,
        "message_count": 0,
        "source_summaries": [],
        "model_calls_used": 0,
        "questions": [],
        "answers": {},
        "paragraph": "",
        "features": [],
        "draft_id": "",
        "draft_version": 0,
        "revision_used": False,
        "delivered": False,
        "delivered_message_id": None,
        "questions_message_id": None,
        "expires_at": 0.0,
        "enabled_on_commit": False,
        "correction": "",
        "operations": {},
    }


def default_public_persona() -> dict[str, Any]:
    """一份空公开快照（撤回后的墓碑与「从未发布」共用同一基线）。

    空快照仍然是一份合法文档：不写正文、不进索引，只保住 `operations` 的跨重启幂等
    （与记忆公开投影的 R9 同一套理由）。
    """
    return {
        "schema_version": PERSONA_SCHEMA_VERSION,
        "revision": 0,
        "owner_username": "",
        "source_revision": 0,
        "published_at": "",
        "paragraph": "",
        "operations": {},
    }


# --- 校验 -----------------------------------------------------------------


def validate_persona(value: object) -> None:
    """私有画像的完整语义校验；不合法抛 `PersonaValidationError`。"""
    document = _require_mapping(value, PERSONA_FIELDS)
    _check_plain_int(document["revision"], minimum=0)
    _check_bool(document["enabled"])
    _check_bool(document["migrated"])
    _check_bool(document["pending_unpublish"])
    _check_text(document["paragraph"], limit=MAX_PARAGRAPH_CHARS, allow_empty=True)
    validate_features(document["features"])
    _check_username(document["username"], allow_empty=True)
    _check_timestamp(document["updated_at"], allow_empty=True)
    _check_line(document["last_change"], limit=MAX_FEATURE_VALUE_CHARS, allow_empty=True)
    _check_text_list(
        document["observations"],
        limit=MAX_OBSERVATION_CHARS,
        count=MAX_OBSERVATIONS,
        allow_empty_items=False,
        unique=True,
    )
    _check_day(document["update_day"])
    _check_plain_int(document["update_count"], minimum=0)
    _check_float(document["last_update_at"], minimum=0.0)
    _check_plain_int(document["last_message_id"], minimum=0)
    _check_identifier(document["committed_draft_id"], allow_empty=True)
    _check_operations(document["operations"])


def validate_workflow(value: object) -> None:
    """工作流的完整语义校验；不合法抛 `PersonaValidationError`。

    工作流里**不存来源正文**：`source_summaries` 与 `observations` 都只是压缩后的摘要，
    原始博客与私聊样本只在内存里处理（设计 §8）。
    """
    document = _require_mapping(value, WORKFLOW_FIELDS)
    _check_plain_int(document["revision"], minimum=0)
    _check_identifier(document["workflow_id"], allow_empty=True)
    _check_choice(document["kind"], WORKFLOW_KINDS)
    _check_choice(document["stage"], WORKFLOW_STAGES)
    _check_fingerprint(document["base_fingerprint"])
    for name in ("base_revision", "watermark", "blog_count", "message_count", "model_calls_used"):
        _check_plain_int(document[name], minimum=0)
    _check_text_list(
        document["source_summaries"],
        limit=MAX_SUMMARY_CHARS,
        count=MAX_SOURCE_SUMMARIES,
        allow_empty_items=False,
        unique=False,
    )
    _check_text_list(
        document["questions"],
        limit=MAX_SUMMARY_CHARS,
        count=MAX_QUESTIONS,
        allow_empty_items=False,
        unique=False,
    )
    _check_answers(document["answers"], document["questions"])
    _check_text(document["paragraph"], limit=MAX_PARAGRAPH_CHARS, allow_empty=True)
    validate_features(document["features"])
    _check_identifier(document["draft_id"], allow_empty=True)
    _check_plain_int(document["draft_version"], minimum=0)
    _check_bool(document["revision_used"])
    _check_bool(document["delivered"])
    _check_bool(document["enabled_on_commit"])
    for name in ("delivered_message_id", "questions_message_id"):
        value_or_none = document[name]
        if value_or_none is not None:
            _check_plain_int(value_or_none, minimum=0)
    _check_float(document["expires_at"], minimum=0.0)
    _check_text(document["correction"], limit=MAX_CORRECTION_CHARS, allow_empty=True)
    _check_operations(document["operations"])


def validate_public_persona(value: object) -> None:
    """公开快照的完整语义校验；空快照（撤回后的墓碑）同样合法。"""
    document = _require_mapping(value, PUBLIC_FIELDS)
    _check_plain_int(document["revision"], minimum=0)
    _check_plain_int(document["source_revision"], minimum=0)
    _check_username(document["owner_username"], allow_empty=True)
    _check_timestamp(document["published_at"], allow_empty=True)
    _check_text(document["paragraph"], limit=MAX_PARAGRAPH_CHARS, allow_empty=True)
    _check_operations(document["operations"])


def validate_features(features: object) -> None:
    """特征列表的校验：每项字段集合精确、取值域受限，且 `(dimension, value)` 不重复。"""
    if not isinstance(features, list):
        raise PersonaValidationError("malformed")
    if len(features) > MAX_FEATURES:
        raise PersonaValidationError("too_long")
    seen: set[tuple[str, str]] = set()
    for item in features:
        if not isinstance(item, dict):
            raise PersonaValidationError("malformed")
        if set(item) != set(FEATURE_FIELDS):
            # 多一个字段就整项拒绝：宽容解析会让模型多写的字段静默落盘（D-57 同款）。
            raise PersonaValidationError("malformed")
        dimension = item["dimension"]
        _check_choice(dimension, FEATURE_DIMENSIONS)
        value = item["value"]
        _check_line(value, limit=MAX_FEATURE_VALUE_CHARS, allow_empty=False)
        _check_choice(item["origin"], FEATURE_ORIGINS)
        _check_choice(item["confidence"], FEATURE_CONFIDENCES)
        refs = item["evidence_refs"]
        if not isinstance(refs, list):
            raise PersonaValidationError("malformed")
        if len(refs) > MAX_EVIDENCE_REFS:
            raise PersonaValidationError("too_long")
        for ref in refs:
            _check_reference(ref)
        _check_timestamp(item["first_seen_at"], allow_empty=True)
        _check_timestamp(item["last_seen_at"], allow_empty=True)
        _check_bool(item["user_locked"])
        key = (dimension, value)
        if key in seen:
            raise PersonaValidationError("duplicate")
        seen.add(key)


# --- 纯函数助手 -----------------------------------------------------------


def payload_of(value: Mapping[str, Any]) -> dict[str, Any]:
    """取出用于「值没变就是 noop」比较的载荷：去掉 `revision` 与 `operations`。

    两者都是宿主记账（修订号每次都涨、幂等表会记下这次的 operation_id），拿它们参与比较
    会让每一次保存都看起来「变了」，noop 就永远不会命中。
    """
    return {key: item for key, item in value.items() if key not in ("revision", "operations")}


def locked_feature_keys(features: object) -> tuple[tuple[str, str], ...]:
    """用户锁定特征的 `(dimension, value)` 序列：自动更新的比对与合并共用它。

    只比维度与取值：`value` 是用户看过的措辞，`last_seen_at` 之类的记账字段变化不该被当成
    「用户改过锁定项」，否则一次自动更新会把令牌判成过期并静默丢弃结果。
    """
    if not isinstance(features, Sequence):
        return ()
    keys: list[tuple[str, str]] = []
    for item in features:
        if not isinstance(item, Mapping):
            continue
        if item.get("user_locked") is not True:
            continue
        dimension = item.get("dimension")
        value = item.get("value")
        if isinstance(dimension, str) and isinstance(value, str):
            keys.append((dimension, value))
    return tuple(sorted(keys))


def is_terminal_stage(stage: object) -> bool:
    """该阶段是否已经终结（终结的流程不再推进，也不再阻塞新的建档/编辑/更新）。"""
    return isinstance(stage, str) and stage in TERMINAL_STAGES


def workflow_is_empty(value: object) -> bool:
    """这份工作流是不是「没有待办流程」：空文档与终态都算。

    终态墓碑（`committed` / `cancelled` / `expired`）保留 `workflow_id` 与 `operations` 供幂等
    与崩溃恢复使用，但对调用方而言不再是一个可以继续的流程。
    """
    if not isinstance(value, Mapping):
        return True
    if not value.get("workflow_id"):
        return True
    return is_terminal_stage(value.get("stage"))


def cleared_workflow(value: Mapping[str, Any]) -> dict[str, Any]:
    """清空敏感正文的墓碑：保留标识、阶段、计数与幂等表，只去掉问题/回答/草稿/摘要。

    设计 §8 的「流程成功、取消或清空时立即清理待办正文」与 §9 的过期清理都走这一份纯函数，
    因此过期与主动取消留下的文件形状必然一致。`draft_id` 一并清空是**有意的**：
    `persona_confirm` 靠它判定「用户确认的是不是当前已展示的那一稿」，清空后旧编号自然失效。
    """
    cleared = dict(value)
    cleared["source_summaries"] = []
    cleared["questions"] = []
    cleared["answers"] = {}
    cleared["paragraph"] = ""
    cleared["features"] = []
    cleared["correction"] = ""
    cleared["draft_id"] = ""
    cleared["delivered"] = False
    cleared["delivered_message_id"] = None
    cleared["questions_message_id"] = None
    return cleared


@dataclass(frozen=True)
class PersonaUpdateToken:
    """一次自动更新的**进程内**不透明令牌（设计 §6 的「更新前取得授权代次与画像版本」）。

    `persona_begin_update` 在同一把写锁内一次性取齐代次、服务纪元、画像修订号与文档快照；
    随后模型调用发生在锁**之外**，返回后再由 `persona_commit_update` 在写锁内复核同一份授权，
    才应用结果。两处检查各覆盖一段：入口那次免去注定失效的提交，提交那次在真正要写的那份
    基线上重新确认 —— 与记忆的自动提取令牌是同一套结构（§2.3 的修复计划）。

    - `generation`：该用户的画像代次。`/persona off`、清空、进入手动编辑、取消与显式
      `persona_invalidate` 都会推进它，使此前取得的所有令牌失效。
    - `epoch`：服务级纪元，`stop_persona()` 递增；停止之后不该再接受停止之前发出的令牌。
    - `revision`：发放令牌时的画像修订号；提交时做 CAS，任何一次并发写入都会让结果作废。
    - `state`：发放令牌时的控制状态（目前只可能是 `"enabled"`，因为禁用状态不发放令牌）。
      保留它是为了让「提交时复核的是**当时**那一份授权」这件事在类型上可见，而不是靠注释。
    - `locked_features`：发放令牌时的用户锁定项 `(dimension, value)` 序列。用户在访谈或
      编辑里改过锁定项时，这两份序列对不上，提交即被丢弃。
    - `document`：私有画像 dict 的**深拷贝**快照，供调用方读取段落与特征；提交永远以磁盘/快照
      的最新版本为基线，绝不回写这份副本。
    """

    user_id: str
    epoch: int
    generation: int
    revision: int
    state: str
    workflow_id: str = ""
    enabled: bool = True
    locked_features: tuple[tuple[str, str], ...] = ()
    document: Mapping[str, Any] = field(default_factory=default_persona)


# --- 单独字段的校验实现 ---------------------------------------------------


def _require_mapping(value: object, expected: Sequence[str]) -> Mapping[str, Any]:
    """字段集合必须与 `expected` 逐字相同：多、少、拼错都拒绝。"""
    if not isinstance(value, Mapping):
        raise PersonaValidationError("malformed")
    if value.get("schema_version") != PERSONA_SCHEMA_VERSION or isinstance(
        value.get("schema_version"), bool
    ):
        # 布尔不是整数：`true` 不能充当 schema_version。
        raise PersonaValidationError("bad_schema")
    if set(value) != set(expected):
        raise PersonaValidationError("malformed")
    return value


def _check_plain_int(value: object, *, minimum: int) -> None:
    """只认十进制整数（布尔不算），并给出下界。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise PersonaValidationError("malformed")
    try:
        str(value)
    except ValueError:  # CPython 对超长整数的文本转换有上限：那是「输入让解析失灵」，不是崩溃。
        raise PersonaValidationError("malformed") from None
    if value < minimum:
        raise PersonaValidationError("malformed")


def _check_float(value: object, *, minimum: float) -> None:
    """浮点时间戳：不允许 NaN 与无穷。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PersonaValidationError("malformed")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise PersonaValidationError("malformed")


def _check_bool(value: object) -> None:
    if not isinstance(value, bool):
        raise PersonaValidationError("malformed")


def _check_choice(value: object, allowed: Sequence[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise PersonaValidationError("unknown_value")


def _check_text(value: object, *, limit: int, allow_empty: bool) -> None:
    """文本字段：必须是 UTF-8 可编码的字符串，且不超过长度上界。

    孤立代理项（`"\\ud800"` 之类）在 Python 里是合法 `str` 却编码不出任何字节，落在文件里
    就会让渲染失败；在这里拒绝，渲染才保证「要么成功、要么给出稳定理由」。
    """
    if not isinstance(value, str):
        raise PersonaValidationError("malformed")
    if not allow_empty and not value:
        raise PersonaValidationError("malformed")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise PersonaValidationError("malformed") from None
    if len(value) > limit:
        raise PersonaValidationError("too_long")


def _check_line(value: object, *, limit: int, allow_empty: bool) -> None:
    """单行文本字段：在 `_check_text` 之上再禁止换行与控制字符。

    这些字段渲染成 `- 名称: 值` 的一行 YAML，一行装不下的取值会让渲染失败；与其让用户的
    一次保存以 `unavailable` 收场，不如在入口就把形状说清楚（短文与修改意见走引用块，
    仍然允许换行，因此不走这里）。
    """
    _check_text(value, limit=limit, allow_empty=allow_empty)
    assert isinstance(value, str)
    for char in value:
        code = ord(char)
        if code < 32 or code == 127 or 0x7F <= code <= 0x9F:
            raise PersonaValidationError("malformed")


def _check_text_list(
    value: object,
    *,
    limit: int,
    count: int,
    allow_empty_items: bool,
    unique: bool,
) -> None:
    if not isinstance(value, list):
        raise PersonaValidationError("malformed")
    if len(value) > count:
        raise PersonaValidationError("too_long")
    seen: set[str] = set()
    for item in value:
        _check_text(item, limit=limit, allow_empty=allow_empty_items)
        if unique:
            if item in seen:
                raise PersonaValidationError("duplicate")
            seen.add(item)


def _check_answers(value: object, questions: object) -> None:
    """回答表：键只能是 `"1"` … `"5"`，值是非空文本；题目编号不得超出本次问题数。

    键用字符串而不是整数：Markdown 里的映射键解析出来可能是整数，`"1"` 与 `1` 混用会让
    「第 1 题答了吗」出现两种答案。渲染侧一律加引号，解析侧只认字符串。
    """
    if not isinstance(value, Mapping):
        raise PersonaValidationError("malformed")
    allowed = {str(index) for index in range(1, MAX_QUESTIONS + 1)}
    count = len(questions) if isinstance(questions, list) else 0
    for key, answer in value.items():
        if not isinstance(key, str) or key not in allowed:
            raise PersonaValidationError("unknown_value")
        if count and int(key) > count:
            raise PersonaValidationError("unknown_value")
        _check_text(answer, limit=MAX_ANSWER_CHARS, allow_empty=False)


def _check_username(value: object, *, allow_empty: bool) -> None:
    """站点用户名合同（与 `memory.codec` 同一条）：3–20 字符、`[A-Za-z0-9_-]`、首尾非 `-`/`_`。

    私有画像里的 `username` 取站点 DTO 的用户名，公开快照的 `owner_username` 用它进公开索引；
    两处都不接受任意文本。空串表示「还没有站点用户名」（新画像、被清空的墓碑）。
    """
    if not isinstance(value, str):
        raise PersonaValidationError("malformed")
    if value == "":
        if allow_empty:
            return
        raise PersonaValidationError("malformed")
    if not 3 <= len(value) <= 20:
        raise PersonaValidationError("malformed")
    if value[0] in "-_" or value[-1] in "-_":
        raise PersonaValidationError("malformed")
    for char in value:
        if not (char.isascii() and (char.isalnum() or char in "_-")):
            raise PersonaValidationError("malformed")


def _check_timestamp(value: object, *, allow_empty: bool) -> None:
    """时间字段：UTC 的 RFC 3339 文本，或空串（表示「还没有过」）。"""
    if value == "" and allow_empty:
        return
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise PersonaValidationError("malformed")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise PersonaValidationError("malformed") from None
    if moment.utcoffset() != timedelta(0):
        # 只接受 UTC：本地时间会让「哪一天」在跨时区部署里变来变去。
        raise PersonaValidationError("malformed")


def _check_fingerprint(value: object) -> None:
    """内容指纹：64 位小写十六进制（sha256 的 `hexdigest`），或空串（历史文件没有这一项）。"""
    if value == "":
        return
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise PersonaValidationError("malformed")


def _check_day(value: object) -> None:
    """`update_day` 是 UTC 日期（`YYYY-MM-DD`）或空串；每日更新预算按它归零。"""
    if value == "":
        return
    if not isinstance(value, str) or _DAY.fullmatch(value) is None:
        raise PersonaValidationError("malformed")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise PersonaValidationError("malformed") from None


def _check_identifier(value: object, *, allow_empty: bool) -> None:
    """标识符 token：短、无空白、无控制字符（工作流编号、草稿编号、提交标记都走它）。"""
    if value == "" and allow_empty:
        return
    if not isinstance(value, str) or not value:
        raise PersonaValidationError("malformed")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise PersonaValidationError("too_long")
    for char in value:
        if char.isspace() or ord(char) < 33 or ord(char) == 127:
            raise PersonaValidationError("malformed")
        if not (char.isascii() and (char.isalnum() or char in "_.:@+-")):
            raise PersonaValidationError("unknown_value")


def _check_reference(value: object) -> None:
    """证据引用：短 token（如 `blog:12`、`msg:345`、`question:2`、`observation:3`、`correction`）。

    只做形状限制，取值集合由撰写器保证（它只接受本次请求里给过模型的来源标签）。
    这里放宽到「短 token」，是为了让宿主自己写进来源摘要里的标签也能作为引用保存下来。
    """
    if not isinstance(value, str) or not value:
        raise PersonaValidationError("malformed")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise PersonaValidationError("too_long")
    for char in value:
        if char.isspace() or ord(char) < 33 or ord(char) == 127:
            raise PersonaValidationError("malformed")


def _check_operations(value: object) -> None:
    """幂等表：键是宿主的 operation_id，值是 `{status, object_id, revision}` 三元组。

    只记元数据，绝不记正文（设计 §8）。上界由服务按 `max_operations` 保证，这里只校形状。
    """
    if not isinstance(value, Mapping):
        raise PersonaValidationError("malformed")
    for operation_id, result in value.items():
        if not isinstance(operation_id, str) or not operation_id:
            raise PersonaValidationError("malformed")
        for char in operation_id:
            if char.isspace() or ord(char) < 33 or ord(char) == 127:
                raise PersonaValidationError("malformed")
        if not isinstance(result, Mapping) or set(result) != {"status", "object_id", "revision"}:
            raise PersonaValidationError("malformed")
        status = result["status"]
        if not isinstance(status, str) or not status:
            raise PersonaValidationError("malformed")
        object_id = result["object_id"]
        if object_id is not None and not isinstance(object_id, str):
            raise PersonaValidationError("malformed")
        _check_plain_int(result["revision"], minimum=0)
