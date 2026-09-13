"""会话上下文管理：内存中的按会话历史（INTERFACES.md §11）。

历史只存内存，进程重启即丢失；不落库、不写日志、不做正文的持久化。
同一 `session_key` 的并发访问由调用方保证串行（worker 已按会话加锁），本模块不加锁。

**历史里只允许成对的 (user, assistant)**（D-22）：一轮对话要么整轮提交，要么什么都不留。
失败轮次（模型报错、额度拒绝、发送失败、被代次检查拦下）绝不落历史 ——
否则用户没看见过的内容会在下一轮被再次外送。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..text_utils import estimate_tokens


def dm_session_key(channel_id: str) -> str:
    """私聊会话键：按频道隔离。"""
    return f"dm:{channel_id}"


def lobby_thread_session_key(thread_root_id: int) -> str:
    """大区共享会话键：按公开回复链的根消息 id 隔离（D-20）。"""
    return f"lobby-thread:{thread_root_id}"


def speaker_wrapper(username: str, text: str) -> str:
    """把大区用户正文包装成带站点发言者标签的一轮内容（D-20）。

    用户名里的控制字符（换行、制表符等）替换为空格，避免有人用用户名伪造出额外的行。
    正文**原样保留**：它本来就是不可信数据，转义与否都不改变这一点，
    而原样保留更利于模型理解上下文。
    """
    return f"[站点发言者：@{_sanitize_username(username)}]\n---\n{text}"


def _sanitize_username(username: str) -> str:
    """把控制字符替换为空格。"""
    return "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in username)


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
        # 会话代次：reset / invalidate 时递增。用途见 reset() 的注释。
        self._generations: dict[str, int] = {}

    def append_exchange(self, session_key: str, user: str, assistant: str) -> None:
        """提交一组**完整**的 (user, assistant) 轮次；这是唯一的历史写入方式。

        调用时机：模型回复**已经送达**之后。任何失败路径都不要调用它。
        """
        turns = self._sessions.setdefault(session_key, [])
        # 每轮固定两条记录，因此阈值是 max_turns * 2。
        if len(turns) >= self._max_turns * 2:
            del turns[:2]
        turns.append(Turn(role="user", content=user))
        turns.append(Turn(role="assistant", content=assistant))

    def reset(self, session_key: str) -> bool:
        """清空指定会话的历史并**递增其代次**；会话原本不存在时返回 False。

        代次的用途是隔离 `/reset` 与**在途**模型请求的竞态：
        一次模型调用可能持续几十秒，而 `/reset` 完全可能在它返回之前到达。
        若不作废，旧请求返回后会往刚清空的会话里写入回复，
        把一条用户没见过的轮次写进「新会话」，并在**下一轮**被再次外送给模型。

        **即使会话原本不存在也要递增**：一个刚建立、还没写进历史的会话里
        同样可能有请求正在跑，此时把代次留在 0 就作废不了它。
        """
        existed = session_key in self._sessions
        self.invalidate(session_key)
        return existed

    def invalidate(self, session_key: str) -> None:
        """清空历史并递增代次，不关心会话是否存在。

        线程过期（D-20）与 DM `/reset` 共用这一个动作：前者只关心「作废」，
        后者还要知道原会话是否存在，于是多一层 `reset()`。
        """
        self._sessions.pop(session_key, None)
        self._generations[session_key] = self._generations.get(session_key, 0) + 1

    def generation(self, session_key: str) -> int:
        """返回当前代次；从未 reset / invalidate 过的会话为 0。"""
        return self._generations.get(session_key, 0)

    def build_messages(
        self,
        session_key: str,
        system_prompt: str,
        *,
        pending_user: str | None = None,
        system_addendum: str | None = None,
    ) -> list[dict[str, str]]:
        """拼装模型消息：第一条 system，其后是裁剪后的历史，最后是本轮未提交的用户内容。

        `pending_user` 只出现在返回值末尾，**不进历史**；它由调用方在回复送达后
        用 `append_exchange()` 提交。`system_addendum` 只拼进 system 消息，
        且必须是静态文本（D-24）。历史按 `max_input_tokens` 从最旧整对丢弃，
        至少保留最后一组；`pending_user` 永远保留（即使超限）。
        """
        system = system_prompt
        if system_addendum:
            system = f"{system_prompt}\n\n{system_addendum}"

        history = list(self._sessions.get(session_key, []))
        base_tokens = estimate_tokens(system)
        if pending_user is not None:
            base_tokens += estimate_tokens(pending_user)
        # len(history) > 2 保证「最后一组」一定留下：整对丢弃到只剩最旧一轮为止。
        while (
            len(history) > 2
            and base_tokens + sum(estimate_tokens(turn.content) for turn in history)
            > self._max_input_tokens
        ):
            del history[:2]

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend({"role": turn.role, "content": turn.content} for turn in history)
        if pending_user is not None:
            messages.append({"role": "user", "content": pending_user})
        return messages

    def session_count(self) -> int:
        """当前有历史的会话数。"""
        return len(self._sessions)

    def turn_count(self, session_key: str) -> int:
        """指定会话的记录条数（一轮对 = 2 条）。"""
        return len(self._sessions.get(session_key, []))
