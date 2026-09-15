"""记忆命令的类型与解析（INTERFACES §32.1 / §32.2、规划 §7.2）。

解析是纯函数、无 I/O：它只回答「这条消息是不是记忆命令、是哪一个、参数是什么」，
不查权限、不碰文件、不调模型。判定顺序与 `text_utils` 里既有的能力命令一致：整个消息
**去首尾空白后**必须以命令词开头，命令词之后必须是空白或字符串结束，因此 `/memoryx`、
`/memoryfoo`、`/remembering` 与正文中间的 `/memory` 都不命中（返回 `None`，交给普通路径）。

三个失败口径是稳定合同（§32.2）：

- **不命中**（返回 `None`）：不是记忆命令，或出现未知子命令（`/memory foo`）。
- **解析成功但 `argument is None`**：命令名认得出来、参数不可用 —— 缺参数（`/memory forget`）、
  非法 ID（`/memory forget um-1`）、内容为空（`/memory suggest all_user`）。上层据此回固定用法：
  它**不是** `None`，绝不允许变成一条普通聊天消息。
- **用法哨兵**（`MemoryCommand(name=USAGE_COMMAND)`）：连命令名都定不下来 —— 只有一个 `/memory`、
  `/memory auto` 少了 on/off、`/memory list` 带了未知作用域、不给实参的命令却被塞了实参。
  同样只回固定用法，不猜、不执行。

`/memory forget <非法 ID>` 按 §32.2 在**解析阶段**就落成用法（`argument is None`），
非法 ID 因此不进 AI、不落任何文件。ID 前缀逐字用 `codec` 的四个常量，本模块不重新定义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .codec import PREFIX_ALL_USER, PREFIX_CANDIDATE, PREFIX_LOBBY, PREFIX_USER
from .models import MemoryScope

__all__ = [
    "USAGE_COMMAND",
    "MemoryCommand",
    "MemoryCommandRequest",
    "MemoryCommandResult",
    "parse_memory_command",
]

# 用法哨兵的 `name` 取值：既不是 D-67 里的十四个命令名，也不是空字符串以外的任何值。
# 取空串是因为它**不是**一个命令名，而是「没有可识别的子命令」这件事本身。
# 控制器把它与未知命令名一起回固定用法（`texts.MEMORY_USAGE_TEXT`）。
USAGE_COMMAND: str = ""

_MEMORY_COMMAND: str = "/memory"
_REMEMBER_COMMAND: str = "/remember"

# 作用域实参的取值 → 枚举（§32.2 的两种 `list` 与两种 `suggest`）。
_SCOPES: dict[str, MemoryScope] = {
    "all_user": MemoryScope.ALL_USER,
    "lobby": MemoryScope.LOBBY,
}

# 需要 ID 实参的命令 → 该命令接受的 ID 前缀（§29.1 / §32.2）。
_ID_PREFIXES: dict[str, tuple[str, ...]] = {
    "forget": (PREFIX_USER,),
    "approve": (PREFIX_CANDIDATE,),
    "reject": (PREFIX_CANDIDATE,),
    "delete": (PREFIX_ALL_USER, PREFIX_LOBBY),
}

# 不接收任何实参的命令（`argument` 与 `scope` 都必须为空）。
_BARE_COMMANDS: frozenset[str] = frozenset({"status", "on", "off", "clear", "candidates"})

# ID 序号只接受 ASCII 十进制（与 codec 同一口径，`str.isdigit()` 会放过全角数字）；
# 宽度不限，codec 解析接受任意 ≥1 位，人工改窄或改宽过的文件仍能读回（§29.1）。
_ASCII_DIGITS = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class MemoryCommand:
    """一条解析后的记忆命令（§32.1）；字段取值是稳定合同（§32.2 的表）。"""

    name: str
    argument: str | None = None
    scope: MemoryScope | None = None


@dataclass(frozen=True)
class MemoryCommandRequest:
    """一次记忆命令的执行请求（§32.1）；由 Router 构造并入队，`session_key` 用该 DM 的键。"""

    event_id: int | None
    message_id: int
    channel_id: str
    session_key: str
    user_id: str
    command: MemoryCommand


@dataclass(frozen=True)
class MemoryCommandResult:
    """一次记忆命令的结果（§32.1）：稳定状态 + 用户可见文案 + 受影响对象 ID。"""

    status: str
    text: str
    memory_id: str | None = None


def parse_memory_command(text: str) -> MemoryCommand | None:
    """解析开头的记忆命令；不是记忆命令时返回 `None`（纯函数，§32.2）。

    命令词大小写不敏感，实参原样保留（`/remember` 的正文尤其如此：用户写什么就交给
    撰写器什么）。ID 实参另见 `_is_valid_id`。
    """
    if not isinstance(text, str):
        # 调用方偶尔会把 None 之外的非法值递进来；解析器不做类型推断，一律按「不是命令」处理。
        return None
    stripped = text.strip()
    if _starts_with_command(stripped, _REMEMBER_COMMAND):
        content = stripped[len(_REMEMBER_COMMAND) :].strip()
        # 缺内容时**不**返回 None：那会让 `/remember` 变成一条普通聊天消息（§32.2）。
        return MemoryCommand(name="remember", argument=content or None)
    if not _starts_with_command(stripped, _MEMORY_COMMAND):
        return None
    return _parse_body(stripped[len(_MEMORY_COMMAND) :].strip())


def _parse_body(rest: str) -> MemoryCommand | None:
    """解析 `/memory` 之后的子命令；不认识的子命令返回 `None`（普通路径），形状错误返回用法哨兵。"""
    if not rest:
        # 只发一个 `/memory`：用户想知道有哪些命令，绝不是想把它当正文发给模型。
        return _usage()
    tokens = rest.split(None, 2)
    head = tokens[0].lower()
    tail = tokens[1:]

    if head in _BARE_COMMANDS:
        return MemoryCommand(name=head) if not tail else _usage()
    if head == "auto":
        if len(tail) != 1:
            return _usage()
        value = tail[0].lower()
        if value == "on":
            return MemoryCommand(name="auto_on")
        if value == "off":
            return MemoryCommand(name="auto_off")
        return _usage()
    if head == "list":
        return _parse_list(tail)
    if head in _ID_PREFIXES:
        return _parse_id_command(head, tail)
    if head == "suggest":
        return _parse_suggest(tail)
    return None


def _missing(name: str) -> MemoryCommand:
    """「命令认得出来、参数不可用」：解析成功但 `argument is None`，由上层回固定用法（§32.2）。

    与用法哨兵的区别只在**命令名是否确定**：`/memory forget` 与 `/memory forget UM-abc`
    都落在这里（名字是 `forget`），因此上层能给出更贴切的用法文案。
    """
    return MemoryCommand(name=name, argument=None)


def _parse_list(tail: list[str]) -> MemoryCommand:
    """`/memory list` 的两种形式（§32.3）：无参数看自己的私有条目，带作用域看已生效共同记忆。"""
    if not tail:
        return MemoryCommand(name="list")
    if len(tail) == 1 and tail[0].lower() in _SCOPES:
        return MemoryCommand(name="list", scope=_SCOPES[tail[0].lower()])
    # 未知作用域不猜：`/memory list foo` 并不是「列出我的私有条目」的请求。
    return _usage()


def _parse_suggest(tail: list[str]) -> MemoryCommand:
    """`/memory suggest <scope> <内容>`：作用域由命令固定，AI 只撰写候选（D-58）。

    缺作用域、缺内容或作用域不认识都落成「解析成功但参数为空」：命令名认得出来，参数不可用。
    """
    if len(tail) < 2:
        return _missing("suggest")
    scope = _SCOPES.get(tail[0].lower())
    content = tail[1].strip()
    if scope is None or not content:
        return _missing("suggest")
    return MemoryCommand(name="suggest", argument=content, scope=scope)


def _parse_id_command(name: str, tail: list[str]) -> MemoryCommand:
    """带 ID 实参的命令：恰好一个实参，且前缀属于该命令允许的集合（§32.2）。

    缺参数与非法 ID **都**落成 `argument is None`（§32.2 逐字如此）：不进 AI、
    不产生任何可观察副作用，上层一律回固定用法。
    """
    if len(tail) != 1:
        return _missing(name)
    value = tail[0]
    if not _is_valid_id(value, _ID_PREFIXES[name]):
        return _missing(name)
    return MemoryCommand(name=name, argument=value)


def _is_valid_id(value: str, prefixes: tuple[str, ...]) -> bool:
    """ID 前缀与序号形状校验；序号宽度不限，只要是非空 ASCII 十进制（与 codec 同口径）。

    前缀大小写敏感：站点与 Markdown 里只有大写形态，`um-1` 不是一条能被找到的条目，
    与其让 Service 回 `not_found`，不如在解析阶段就告诉用户用法。
    """
    for prefix in prefixes:
        if value.startswith(prefix):
            return _ASCII_DIGITS.fullmatch(value[len(prefix) :]) is not None
    return False


def _starts_with_command(text: str, command: str) -> bool:
    """`command` 是否正好占据开头；命令词之后必须是空白或字符串结束。

    因此 `/memoryx`、`/memoryfoo` 与 `/remembering` 都不命中；`command` 传入的始终是小写
    字面量，整词比较大小写不敏感。
    """
    length = len(command)
    if len(text) < length or text[:length].lower() != command:
        return False
    return len(text) == length or text[length].isspace()


def _usage() -> MemoryCommand:
    """用法哨兵：命中了 `/memory` 家族但形状不合法。"""
    return MemoryCommand(name=USAGE_COMMAND)
