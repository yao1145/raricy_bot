"""会话上下文管理：内存中的按会话历史（INTERFACES.md §11）。

历史只存内存，进程重启即丢失；不落库、不写日志、不含正文外送的持久化。
同一 `session_key` 的并发访问由调用方保证串行（worker 已按会话加锁），本模块不加锁。

存储顺序固定为 `[user, assistant, user, assistant, ...]`，因此「一轮」= 相邻两
条记录；`max_turns` 指最多保留最近 N 组 (user, assistant) 对。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..text_utils import estimate_tokens


def dm_session_key(channel_id: str) -> str:
    """私聊会话键：按频道隔离。"""
    return f"dm:{channel_id}"


def lobby_session_key(user_id: str) -> str:
    """大区会话键：按作者隔离，不同用户互不可见。"""
    return f"lobby:{user_id}"


@dataclass(frozen=True)
class Turn:
    """一条历史记录。"""

    role: str  # "user" | "assistant"
    content: str


class ContextManager:
    """内存会话历史；只做追加、清空、裁剪与消息拼装。"""

    def __init__(self, max_turns: int, max_input_tokens: int) -> None:
        self._max_turns = max_turns
        self._max_input_tokens = max_input_tokens
        self._sessions: dict[str, list[Turn]] = {}

    def append_user(self, session_key: str, content: str) -> None:
        """追加一条用户轮次；超出轮次上限时从最旧整对丢弃。"""
        turns = self._sessions.setdefault(session_key, [])
        # 每轮固定两条记录（user + assistant），因此阈值是 max_turns * 2。
        if len(turns) >= self._max_turns * 2:
            del turns[:2]
        turns.append(Turn(role="user", content=content))

    def append_assistant(self, session_key: str, content: str) -> None:
        """追加一条助手轮次；允许在空会话上形成孤立的 assistant 轮次。"""
        self._sessions.setdefault(session_key, []).append(
            Turn(role="assistant", content=content)
        )

    def reset(self, session_key: str) -> bool:
        """清空指定会话的历史；该会话不存在时返回 False。"""
        if session_key not in self._sessions:
            return False
        del self._sessions[session_key]
        return True

    def build_messages(
        self, session_key: str, system_prompt: str
    ) -> list[dict[str, str]]:
        """拼装模型消息：第一条固定 system，其后是裁剪后的历史。

        system 消息只承载 `system_prompt`；用户内容绝不拼进 system（§19.2）。
        按 `max_input_tokens` 从最旧整对丢弃，至少保留最后一组（即使超限）。
        """
        history = list(self._sessions.get(session_key, []))
        base_tokens = estimate_tokens(system_prompt)
        # len(history) > 2 保证「最后一组」一定留下：整对丢弃到只剩最旧一轮为止。
        while (
            len(history) > 2
            and base_tokens + sum(estimate_tokens(turn.content) for turn in history)
            > self._max_input_tokens
        ):
            del history[:2]

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": turn.role, "content": turn.content} for turn in history)
        return messages

    def session_count(self) -> int:
        """当前有历史的会话数。"""
        return len(self._sessions)

    def turn_count(self, session_key: str) -> int:
        """指定会话的记录条数（一轮对 = 2 条）。"""
        return len(self._sessions.get(session_key, []))
