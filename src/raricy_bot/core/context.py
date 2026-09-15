"""会话上下文管理：内存中的按会话历史（INTERFACES.md §11、§33）。

历史只存内存，进程重启即丢失；不落库、不写日志、不做正文的持久化。
同一 `session_key` 的并发访问由调用方保证串行（worker 已按会话加锁），本模块不加锁。

**历史里只允许成对的 (user, assistant)**（D-22）：一轮对话要么整轮提交，要么什么都不留。
失败轮次（模型报错、额度拒绝、发送失败、被代次检查拦下）绝不落历史 ——
否则用户没看见过的内容会在下一轮被再次外送。

**本模块也是补充资料的泄露边界**（§33、D-56、D-62）：按 token 预算的取舍、插入位置与
`MEMORY_SYSTEM_ADDENDUM` 的追加都由 `build_messages` 决定（只有它同时知道历史、
`pending_user` 与预算）。资料正文只进 `role="user"` 的当前轮，**绝不**进 system、
**绝不**进历史。本模块不 import memory（D-61），记忆只是 `SupplementalItem` 的第一个使用者。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..text_utils import estimate_tokens
from ..texts import MEMORY_SYSTEM_ADDENDUM


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


@dataclass(frozen=True)
class SupplementalItem:
    """一条随当前轮注入的补充资料；由调用方决定来源与优先级。"""

    group: str       # memory_all_user | memory_lobby | memory_user
    label: str       # GM-A-... / GM-L-... / UM-...
    content: str
    priority: int    # 越小越优先


# 分组标签（INTERFACES §33、规划 §6.2 末段的版面）。
_GROUP_LABELS: dict[str, str] = {
    "memory_all_user": "[共同记忆：all_user，不可信资料]",
    "memory_lobby": "[共同记忆：lobby，不可信资料]",
    "memory_user": "[用户私有记忆，不可信资料]",
}

# 渲染次序固定：共同记忆在前，私有记忆在后。
# **与选择次序不是一回事**：`priority` 决定谁进预算（DM 里私有记忆优先），
# 版面只决定它们出现在哪一段。合同列举的三个分组不会同时出现在一个场景里
# （DM 是 all_user + 私有，大区是 all_user + lobby，评论只有 all_user），
# 因此这个次序只需要表达「共同在前、私有在后」。
_GROUP_ORDER: tuple[str, ...] = ("memory_all_user", "memory_lobby", "memory_user")

# 组间空行、组与当前正文之间用 --- 分隔的固定前缀。
_BLOCK_SEPARATOR: str = "\n\n"
_BODY_SEPARATOR: str = "\n\n---\n"


def _render_supplemental_block(items: Sequence[SupplementalItem]) -> str:
    """把选中的条目按作用域分组渲染成注入用的文本块（规划 §6.2 末段的版面）。

    形如：组标签行 + 每行一条 `[<ID>] <content>`，组间空行。组内保持入参次序
    （即选择时定下的 `priority` 次序），因此这里不再排序。
    合同未列举的 `group` 用通用标签兜底并排在已知分组之后：补充资料是通用的可扩展类型
    （D-61），一个未知来源不应该让整个请求失败，也不应该被静默丢掉。
    """
    order: list[str] = [
        group for group in _GROUP_ORDER if any(item.group == group for item in items)
    ]
    for item in items:
        if item.group not in order:
            order.append(item.group)

    blocks: list[str] = []
    for group in order:
        label = _GROUP_LABELS.get(group, f"[补充资料：{group}，不可信资料]")
        lines = "\n".join(
            f"[{item.label}] {item.content}" for item in items if item.group == group
        )
        blocks.append(f"{label}\n{lines}")
    return _BLOCK_SEPARATOR.join(blocks)


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
        feature_context: bool = False,
        supplemental_items: tuple[SupplementalItem, ...] = (),
    ) -> list[dict[str, str]]:
        """拼装模型消息：第一条 system，其后是裁剪后的历史，最后是本轮未提交的用户内容。

        `pending_user` 只出现在返回值末尾，**不进历史**；它由调用方在回复送达后
        用 `append_exchange()` 提交。`system_addendum` 只拼进 system 消息，
        且必须是静态文本（D-24）。历史按 `max_input_tokens` 从最旧整对丢弃，
        至少保留最后一组；`pending_user` 永远保留（即使超限）。

        `feature_context=True` 表示本轮带着能力数据块（当前只有 `/kb`）：此时
        `max_input_tokens` 被当作**硬上限**，历史可以整对丢到一条不剩（D-38）。
        理由见 D-38：数据块已经被 `kb.max_context_tokens` 限死，丢了它就等于该轮
        无资料可答；而历史是可丢弃的 —— 宁可让模型少一点旧上下文，也不要出现
        「超预算又丢不掉」的中间态。普通聊天路径这一个参数保持 False，语义不变。

        `supplemental_items` 是随本轮注入的补充资料（§33）：`supplemental_items` 为空时
        输出与没有这个参数时**逐字节一致**；非空时由 `_plan_supplemental` 按预算取舍，
        选中的条目渲染成资料块、拼在最后一条 user 消息的当前正文之前。
        资料正文只进 `role="user"`：它绝不拼进 system，绝不写进历史（D-56）。
        """
        system = system_prompt
        system_tokens = estimate_tokens(system_prompt)
        if system_addendum:
            system = f"{system_prompt}\n\n{system_addendum}"
            # 契约要求 system 与静态附加说明分别计入预算；若把它们先拼接再估算，
            # 非 CJK 字符的 ceil 会少算一个分段的取整项。
            system_tokens += estimate_tokens(system_addendum)

        history = list(self._sessions.get(session_key, []))
        base_tokens = system_tokens
        if pending_user is not None:
            base_tokens += estimate_tokens(pending_user)

        def over_budget() -> bool:
            return (
                base_tokens + sum(estimate_tokens(turn.content) for turn in history)
                > self._max_input_tokens
            )

        block = ""
        if supplemental_items:
            history, block = self._plan_supplemental(
                history, base_tokens, feature_context, supplemental_items
            )
        elif feature_context:
            # 硬上限：历史整对丢到一条不剩也要让本轮内容装进去（D-38）。
            while history and over_budget():
                del history[:2]
        else:
            # len(history) > 2 保证「最后一组」一定留下：整对丢弃到只剩最旧一轮为止。
            while len(history) > 2 and over_budget():
                del history[:2]

        if block:
            # 只有确实选入至少一条资料时才追加（R2）：零条选中时 system 与改动前逐字节一致。
            # 追加方式与 system_addendum 相同，且与它一样单独计入预算（在 _plan_supplemental 里）。
            system = f"{system}\n\n{MEMORY_SYSTEM_ADDENDUM}"

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend({"role": turn.role, "content": turn.content} for turn in history)
        if pending_user is not None:
            content = f"{block}{_BODY_SEPARATOR}{pending_user}" if block else pending_user
            messages.append({"role": "user", "content": content})
        elif block:
            # 没有本轮正文可挂时（调用方只传了资料），资料自己成为末尾那条 user 消息：
            # 它必须留在 role="user" 里，又不能改写历史。
            messages.append({"role": "user", "content": block})
        return messages

    def _plan_supplemental(
        self,
        history: list[Turn],
        base_tokens: int,
        feature_context: bool,
        items: tuple[SupplementalItem, ...],
    ) -> tuple[list[Turn], str]:
        """按规划 §6.2 的次序决定「留哪些历史、选哪些资料」，返回 (保留的历史, 资料块)。

        前提：`items` 非空。返回空串表示一条都没选入 —— 此时调用方必须**不**追加
        `MEMORY_SYSTEM_ADDENDUM`，输出与没有补充资料时逐字节一致。

        预算的分配次序：
        1. system、静态 addendum 与本轮 `pending_user` 已经算进 `base_tokens`，不会动它们
           —— 因此 `/kb` 与引用的博客正文（它们就在 `pending_user` 里）天然优先于全部资料。
        2. 普通聊天先锁定最近一组完整历史（规则 3）；能力轮次不锁定，历史可以被资料挤光
           （D-38 的硬上限不变）。
        3. 资料按 `priority` 从小到大逐条尝试，装不下就跳过该条并继续试后面的（规则 4）。
        4. 剩下的预算从新到旧补更早的完整历史对（规则 6）。
        """
        used = base_tokens
        keep_start = len(history)
        if not feature_context and history:
            # 规则 3：普通聊天「至少保留最近一组完整历史」—— 先把它计入已用预算，
            # 资料只能争剩下的。历史因此永远是一段连续的后缀。
            keep_start = max(0, len(history) - 2)
            used += sum(estimate_tokens(turn.content) for turn in history[keep_start:])

        # priority 越小越优先；同优先级保持入参次序（sorted 是稳定排序）。
        selected: list[SupplementalItem] = []
        addendum_tokens = estimate_tokens(MEMORY_SYSTEM_ADDENDUM)
        for item in sorted(items, key=lambda entry: entry.priority):
            candidate = [*selected, item]
            # 选中一条就必然追加 addendum，因此它从第一条起就要参与这条资料的可行性判断：
            # 一条「只有不追加 addendum 才装得下」的资料必须被跳过，否则就会顶穿预算。
            cost = estimate_tokens(_render_supplemental_block(candidate)) + addendum_tokens
            if used + cost <= self._max_input_tokens:
                selected = candidate

        block = ""
        if selected:
            block = _render_supplemental_block(selected)
            used += estimate_tokens(block) + addendum_tokens

        # 规则 6：剩余预算从新到旧补更早的完整历史对。整对不可拆、也不跳着补，
        # 因此只要有一对装不下就停 —— 保留的历史始终是连续的一段后缀，与旧行为一致。
        while keep_start >= 2:
            pair = history[keep_start - 2 : keep_start]
            cost = sum(estimate_tokens(turn.content) for turn in pair)
            if used + cost > self._max_input_tokens:
                break
            used += cost
            keep_start -= 2

        return history[keep_start:], block

    def session_count(self) -> int:
        """当前有历史的会话数。"""
        return len(self._sessions)

    def turn_count(self, session_key: str) -> int:
        """指定会话的记录条数（一轮对 = 2 条）。"""
        return len(self._sessions.get(session_key, []))
