"""纯文本工具：用户名字符、@ 提及、token 估算、段落截断、探测词与命令判定。

本模块不做任何 I/O，便于逐条覆盖 INTERFACES.md §4 的边界用例。
"""

from __future__ import annotations

import math
import string
from typing import Any

from .texts import TRUNCATION_SUFFIX

# 用户名合法字符：字母、数字、下划线与连字符（对应 chat-bot.md §2.1）。
USERNAME_CHARS: frozenset[str] = frozenset(string.ascii_letters + string.digits + "_-")

# 截断时允许作为切点的字符：换行与句末标点。
_SENTENCE_ENDS: str = "\n。！？.!?"

# token 估算中按 1 token 计数的区段（中日韩文字与全角标点）。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
    (0x3000, 0x303F),
    (0xFF00, 0xFFEF),
)

# 索取系统提示、运行密钥或隐藏配置的探测词，全部小写；命中任一即视为探测。
# 只收录与「索取系统提示 / 密钥 / 隐藏配置」直接相关的词，不得收录会命中普通寒暄
# （例如询问机器人名字、打招呼）的模式。
SECRET_PROBE_PATTERNS: tuple[str, ...] = (
    "系统提示",
    "system prompt",
    "systemprompt",
    "提示词",
    "你的指令",
    "你的设定",
    "api key",
    "apikey",
    "密钥",
    "口令",
    "环境变量",
    "env",
    "配置文件",
    "config",
    "隐藏配置",
    "内部配置",
    "cookie",
    "token",
)


def is_username_char(ch: str) -> bool:
    """判断单个字符是否属于用户名合法字符。"""
    return ch.isascii() and (ch.isalnum() or ch in "_-")


def _is_cjk(ch: str) -> bool:
    """判断单个字符是否按 1 token 计数。"""
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _mention_starts(content: str, bot_username: str) -> list[int]:
    """返回所有命中的 "@用户名" 的起始下标。

    命中判据：左右紧邻字符都不是用户名合法字符（串首、串尾视为合法）。
    用户名为空串时不命中任何内容。
    """
    if not bot_username:
        return []
    needle = "@" + bot_username
    starts: list[int] = []
    cursor = content.find(needle)
    while cursor != -1:
        end = cursor + len(needle)
        left_ok = cursor == 0 or not is_username_char(content[cursor - 1])
        right_ok = end == len(content) or not is_username_char(content[end])
        if left_ok and right_ok:
            starts.append(cursor)
        cursor = content.find(needle, cursor + 1)
    return starts


def contains_bot_mention(content: str, bot_username: str) -> bool:
    """判断内容里是否存在对机器人的精确 @ 提及（区分大小写）。"""
    return bool(_mention_starts(content, bot_username))


def strip_bot_mention(content: str, bot_username: str) -> str:
    """删除内容里所有命中的 "@用户名" 片段，并去掉首尾空白。"""
    if not bot_username:
        return content.strip()
    needle = "@" + bot_username
    parts: list[str] = []
    cursor = 0
    for start in _mention_starts(content, bot_username):
        parts.append(content[cursor:start])
        cursor = start + len(needle)
    parts.append(content[cursor:])
    return "".join(parts).strip()


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中日韩字符按 1 计，其余字符按 4 个 1 token 计。"""
    if not text:
        return 0
    cjk_count = sum(1 for ch in text if _is_cjk(ch))
    other_count = len(text) - cjk_count
    return cjk_count + math.ceil(other_count / 4)


def truncate_at_paragraph(text: str, limit: int) -> tuple[str, bool]:
    """在自然段边界截断文本，返回 (截断结果, 是否发生截断)。

    切点取 limit 之前最后一个换行或句末标点（含该字符）；切点不超过 limit // 2 时
    退化为硬切 text[:limit]。截断结果末尾追加 TRUNCATION_SUFFIX。
    """
    if len(text) <= limit:
        return text, False
    cut = 0
    for index in range(min(limit, len(text)) - 1, -1, -1):
        if text[index] in _SENTENCE_ENDS:
            cut = index + 1
            break
    if cut <= limit // 2:
        cut = limit
    return text[:cut].rstrip() + TRUNCATION_SUFFIX, True


def is_secret_probe(text: str) -> bool:
    """判断用户是否在索取系统提示、密钥或内部配置（大小写不敏感）。"""
    normalized = text.strip().lower()
    return any(pattern in normalized for pattern in SECRET_PROBE_PATTERNS)


def is_help_command(text: str) -> bool:
    """判断是否为 /help 命令。"""
    return text.strip().lower() == "/help"


def is_reset_command(text: str) -> bool:
    """判断是否为 /reset 命令。"""
    return text.strip().lower() == "/reset"


def has_media(message: Any) -> bool:
    """判断消息是否附带图片或博客引用。"""
    return message.image is not None or message.blog is not None
