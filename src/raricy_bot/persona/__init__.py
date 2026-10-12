"""用户画像子系统（设计 §8 的 `persona/` 编排模块）。

交互（命令解析、状态机、采集、撰写）与存储分属两层：`persona/commands.py`、
`persona/controller.py`、`persona/sources.py`、`persona/writer.py` 负责前者，持久化**统一**由
`MemoryService` 管理（不增加第二个写入者）—— 画像的三份 Markdown 与记忆共用同一把写锁、
同一套原子替换与快照机制。

本包在导入期只急切加载 `models` 与 `codec`（纯类型与纯函数，不碰 I/O，也不依赖
`memory.service`）。`commands` / `controller` / `sources` / `writer` **惰性**转发：
`memory.service` 需要用到画像 codec，而 `controller` 又需要 `MemoryService`；急切导入会让
这条链在某个导入顺序下变成环。惰性转发让「直接 `from raricy_bot.persona import PersonaController`」
照常可用，同时把导入顺序的自由度留给装配方（PEP 562）。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from . import codec, models
from .codec import (
    parse_persona,
    parse_public_persona,
    parse_workflow,
    render_persona,
    render_public_persona,
    render_workflow,
)
from .models import (
    FEATURE_CONFIDENCES,
    FEATURE_DIMENSIONS,
    FEATURE_FIELDS,
    FEATURE_ORIGINS,
    PERSONA_FIELDS,
    PERSONA_PUBLIC_GROUP,
    PERSONA_SCHEMA_VERSION,
    PERSONA_USER_GROUP,
    PUBLIC_FIELDS,
    TERMINAL_STAGES,
    WORKFLOW_FIELDS,
    WORKFLOW_KINDS,
    WORKFLOW_STAGES,
    PersonaUpdateToken,
    PersonaValidationError,
    cleared_workflow,
    default_persona,
    default_public_persona,
    default_workflow,
    is_terminal_stage,
    locked_feature_keys,
    payload_of,
    validate_features,
    validate_persona,
    validate_public_persona,
    validate_workflow,
    workflow_is_empty,
)

__all__ = [
    "FEATURE_CONFIDENCES",
    "FEATURE_DIMENSIONS",
    "FEATURE_FIELDS",
    "FEATURE_ORIGINS",
    "PERSONA_FIELDS",
    "PERSONA_PUBLIC_GROUP",
    "PERSONA_SCHEMA_VERSION",
    "PERSONA_USER_GROUP",
    "PUBLIC_FIELDS",
    "TERMINAL_STAGES",
    "WORKFLOW_FIELDS",
    "WORKFLOW_KINDS",
    "WORKFLOW_STAGES",
    "PersonaUpdateToken",
    "PersonaValidationError",
    "cleared_workflow",
    "codec",
    "default_persona",
    "default_public_persona",
    "default_workflow",
    "is_terminal_stage",
    "locked_feature_keys",
    "models",
    "parse_persona",
    "parse_public_persona",
    "parse_workflow",
    "payload_of",
    "render_persona",
    "render_public_persona",
    "render_workflow",
    "validate_features",
    "validate_persona",
    "validate_public_persona",
    "validate_workflow",
    "workflow_is_empty",
]

# 惰性转发的四个子模块：它们的导入会牵到 `memory.service`（或模型客户端），因此不能急切加载。
_LAZY_MODULES: tuple[str, ...] = ("commands", "controller", "sources", "writer")


def __getattr__(name: str) -> Any:
    """把 `persona.<名字>` 转发到四个子模块（PEP 562）。

    只在第一次访问时导入；导入成功且子模块确实提供该名字时把它记进包命名空间，后续访问
    不再走这里。子模块不存在（其他 owner 尚未落地）时抛 `AttributeError`，而不是让
    `ImportError` 从一次属性查找里冒出来——属性缺失与模块缺失是两回事。
    """
    for module_name in _LAZY_MODULES:
        target = f"{__name__}.{module_name}"
        try:
            module = import_module(target)
        except ModuleNotFoundError as exc:
            if exc.name != target:
                # 子模块存在、但它自己的某个依赖缺失：那是真实错误，不能当成「模块不存在」吞掉。
                raise
            continue
        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
