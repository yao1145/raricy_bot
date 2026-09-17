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

**会话参与者（`ConversationSubject`，§45.1）同样是通用元数据，不是记忆类型**：它只装一个
不可逆的 owner key 与一个展示用的用户名标签，由调用方算好后挂到 `Turn` 上。本模块因此仍然
不 import memory（D-61）—— 算 key 是调用方的事（`user_storage_key`），这里只保存与查询。
subject **不渲染进任何一条消息**，也不参与预算：它随历史淘汰、`reset()` 与 `invalidate()`
自然消失，不另建参与者表（公开设计 §14.1）。
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
    return f"[站点发言者：@{sanitize_username(username)}]\n---\n{text}"


def sanitize_username(username: str) -> str:
    """把用户名里的控制字符替换为空格。

    单独导出是因为大区近期消息（`core/lobby_context.py`）也要用**同一条**规则：
    那个块的每条消息前面同样有一个站点发言者标签，只是形状不由 `speaker_wrapper`
    决定（§5.2 的版面没有 `---` 分隔行）。清洗规则只能有一份实现 —— 两处一旦分叉，
    用户名就能在其中一个出口伪造出额外的行。
    """
    return "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in username)


@dataclass(frozen=True)
class ConversationSubject:
    """一个会话参与者的不可逆身份（§45.1、公开设计 §14.1）。

    `key` 是 `user_storage_key(user_id)`，即固定域前缀后的 SHA-256 摘要：它**不可逆**，
    原始作者 id 不藏在里面，也不能由它反推。`label` 是给展示用的站点用户名，构造方必须
    先用 `sanitize_username` 清洗控制字符 —— 同一份规则只能有一个实现（§38.1）。

    这是通用类型，不是记忆专属：本模块不认识它由谁产生、给谁使用。构造方负责算 key，
    本模块只负责随历史保存与按会话查询（`recent_subjects`）。
    """

    key: str
    label: str


@dataclass(frozen=True)
class Turn:
    """一条历史记录。"""

    role: str  # "user" | "assistant"
    content: str
    # 说话人的会话 subject（§45.1）；只有 user 那一半会被填，assistant 恒为 None。
    # 它不参与渲染、不参与预算：`build_messages` 除了历史正文什么也不看。
    subject: ConversationSubject | None = None


@dataclass(frozen=True)
class SupplementalItem:
    """一条随当前轮注入的补充资料；由调用方决定来源与优先级。"""

    group: str       # memory_all_user | memory_lobby | memory_user
    label: str       # GM-A-... / GM-L-... / UM-...
    content: str
    priority: int    # 越小越优先


@dataclass(frozen=True)
class SupplementalCap:
    """一组**共享同一 token 上限**的补充资料分组（§33）。

    上限按**渲染后的文本块**计（组标签行 + 各条目行），也就是这一组资料实际会占掉的外送空间，
    与整轮预算（`max_input_tokens`）用的是同一口径、同一套估算。`groups` 里的分组**合计**
    不得超过 `max_tokens`；不在任何 `SupplementalCap` 里的分组不受分组上限约束，只受整轮预算
    约束（补充资料是通用的可扩展类型，D-61：一个新来源不该因为没人给它配上限就整轮消失）。

    本类型不 import 任何配置或记忆类型（裁决 C / D-61）：上限由调用方按配置算好后传进来。
    """

    groups: tuple[str, ...]
    max_tokens: int


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


def _history_tokens(turns: Sequence[Turn]) -> int:
    """一组轮次正文的 token 估算之和（整对丢弃与整对回补共用同一口径）。"""
    return sum(estimate_tokens(turn.content) for turn in turns)


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


def _render_transient_block(header: str, items: Sequence[str]) -> str:
    """把选中的单轮临时条目渲染成注入用的文本块（LOBBY_RECENT_CONTEXT_DESIGN §7.1）。

    形如：块头行 + 空行 + 逐条正文（条与条之间一个空行）。块头是静态常量（D-24），
    条目本身由调用方渲染好，这里不再加工 —— 尤其不解析、不展开其中的任何标记。
    """
    return f"{header}{_BLOCK_SEPARATOR}{_BLOCK_SEPARATOR.join(items)}"


def _fit_transient_suffix(items: Sequence[str], header: str, budget: int) -> int:
    """从最新向旧试装**连续后缀**，返回装得下的**最长后缀条数**（0 = 一条都装不下）。

    规则与 `LOBBY_RECENT_CONTEXT_DESIGN` §7.2 的近期块逐条一致：从长度 1 的后缀开始向更旧
    扩展，装不下下一条更老的就停，**不跳洞**；每次整块重渲染，估算口径因此与最终交出去的
    文本永远一致。`budget` 是这个块自己可用的 token 额度（整轮上限减去已用额度，以及它
    后面那个连接符）。

    `_plan_turn` 与 `select_recent_suffix` **共用**本函数：两处必须是同一份连续后缀规则，
    各写一遍迟早分叉（§45.2 明写「估算口径必须与 `_plan_turn` 完全一致」）。
    """
    length = 0
    for start in range(len(items) - 1, -1, -1):
        candidate = _render_transient_block(header, items[start:])
        if estimate_tokens(candidate) > budget:
            break
        length = len(items) - start
    return length


def _exceeds_group_caps(
    items: Sequence[SupplementalItem], caps: tuple[SupplementalCap, ...]
) -> bool:
    """`items` 里有没有哪个受约束的分组（或共享池）已经超出自己的上限。

    每个上限都按 `_render_supplemental_block` 现算：**先渲染、再估算**，因此上限与最终交出去的
    文本永远同一个口径（组标签行与换行都算在内）。共享池里的多个分组一起渲染，正是它们在最终
    版面里的那一段（`_GROUP_ORDER` 把同池分组排在一起，中间只隔一个空行）。
    """
    for cap in caps:
        capped = [item for item in items if item.group in cap.groups]
        if not capped:
            continue
        if estimate_tokens(_render_supplemental_block(capped)) > cap.max_tokens:
            return True
    return False


class ContextManager:
    """内存会话历史；只做追加、清空、裁剪与消息拼装。"""

    def __init__(self, max_turns: int, max_input_tokens: int) -> None:
        self._max_turns = max_turns
        self._max_input_tokens = max_input_tokens
        self._sessions: dict[str, list[Turn]] = {}
        # 会话代次：reset / invalidate 时递增。用途见 reset() 的注释。
        self._generations: dict[str, int] = {}

    def append_exchange(
        self,
        session_key: str,
        user: str,
        assistant: str,
        *,
        subject: ConversationSubject | None = None,
    ) -> None:
        """提交一组**完整**的 (user, assistant) 轮次；这是唯一的历史写入方式。

        调用时机：模型回复**已经送达**之后。任何失败路径都不要调用它。

        `subject` 是当前说话人的会话身份（§45.1），默认 None 时与升级前逐字节一致：
        它只在成功送达、整轮提交时随历史一起保存。失败轮次（模型报错、额度拒绝、发送失败、
        代次失效）根本不写历史，也就不会留下一个用户从没被回答过的参与者。

        subject **不渲染进任何一条消息**，也不改变历史正文：用户名的可见标签仍由调用方
        包装好的 user 正文提供（大区的 `speaker_wrapper`、评论的 `_comment_body`）。
        """
        turns = self._sessions.setdefault(session_key, [])
        # 每轮固定两条记录，因此阈值是 max_turns * 2。
        if len(turns) >= self._max_turns * 2:
            del turns[:2]
        turns.append(Turn(role="user", content=user, subject=subject))
        turns.append(Turn(role="assistant", content=assistant))

    def recent_subjects(self, session_key: str) -> tuple[ConversationSubject, ...]:
        """会话里最近出现过的参与者，按**最近一次出现从新到旧**、按 `key` 去重（§45.1）。

        只读、同步、无 I/O：不改历史、不推进代次。同一个 owner key 只出现一次，
        `label` 取最近一次的值（用户可能改过名）。会话不存在时返回空元组。

        它**不另建参与者表**：参与者就挂在历史的 `Turn` 上，历史淘汰、`reset()` 与
        `invalidate()` 之后这里自然看不到已经离开的说话人（公开设计 §14.1）。
        """
        latest: dict[str, ConversationSubject] = {}
        for turn in reversed(self._sessions.get(session_key, [])):
            subject = turn.subject
            if subject is None or subject.key in latest:
                continue
            latest[subject.key] = subject
        return tuple(latest.values())

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
        supplemental_caps: tuple[SupplementalCap, ...] = (),
        transient_user_items: tuple[str, ...] = (),
        transient_user_header: str | None = None,
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

        `supplemental_caps` 是各分组自己的 token 上限（§33）：每条上限管住一组（或一组共享
        同一份预算的分组）的**渲染后**大小，超了就跳过该条目——条目仍然不可拆分，绝不截半句。
        它与整轮预算（`max_input_tokens`）是两道独立的门，都满足才选入；传空元组时行为与
        没有这个参数完全一致（`supplemental_items=()` 的逐字节一致不受影响）。

        `transient_user_items` / `transient_user_header` 是**只属于当前轮**的临时条目
        （大区近期公开消息，§38.3）：入参顺序已经是旧到新，调用方负责渲染逐条文本，
        本模块不认识它们的类型，也不 import `lobby_context.py`（通用上下文模块不反向依赖
        具体频道 DTO，与 D-61 同源）。两个参数必须同时为空或同时非空，否则抛 `ValueError`。
        选中的条目按**最新连续后缀**扩展（装不下下一条更老的就停，不跳洞），渲染成
        `块头 + 空行 + 条目...` 的一整块，拼在末尾那条 user 消息里、记忆块之后、
        当前正文之前。它们**不**触发 `MEMORY_SYSTEM_ADDENDUM`（近期公开消息不是记忆），
        也**绝不**写进 `_sessions`。
        """
        has_transient = bool(transient_user_items)
        if has_transient != bool(transient_user_header):
            # 只给块头不给条目（或反过来）是调用方写错了。静默吞掉会让「为什么模型没看到
            # 近期消息」变成一个查不出来的问题。
            raise ValueError(
                "transient_user_items 与 transient_user_header 必须同时为空或同时非空"
            )

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

        head = ""
        memory_selected = False
        if supplemental_items or has_transient:
            history, head, memory_selected = self._plan_turn(
                history,
                base_tokens,
                feature_context,
                supplemental_items,
                # 有本轮正文时资料块会与它拼成 `block + _BODY_SEPARATOR + pending_user`，
                # 分隔符也占预算；没有正文时资料自成一条 user 消息，不存在分隔符。
                has_pending_body=pending_user is not None,
                caps=supplemental_caps,
                transient_items=transient_user_items,
                transient_header=transient_user_header,
            )
        elif feature_context:
            # 硬上限：历史整对丢到一条不剩也要让本轮内容装进去（D-38）。
            while history and over_budget():
                del history[:2]
        else:
            # len(history) > 2 保证「最后一组」一定留下：整对丢弃到只剩最旧一轮为止。
            while len(history) > 2 and over_budget():
                del history[:2]

        if memory_selected:
            # 只有确实选入至少一条**记忆**时才追加：零条选中时 system 与改动前逐字节一致。
            # 近期块刻意不走这条路（§11.2）：大区公开消息不是记忆，给它挂这条说明是错的。
            # 追加方式与 system_addendum 相同，且与它一样单独计入预算（在 _plan_turn 里）。
            system = f"{system}\n\n{MEMORY_SYSTEM_ADDENDUM}"

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend({"role": turn.role, "content": turn.content} for turn in history)
        if pending_user is not None:
            content = f"{head}{_BODY_SEPARATOR}{pending_user}" if head else pending_user
            messages.append({"role": "user", "content": content})
        elif head:
            # 没有本轮正文可挂时（调用方只传了资料），资料自己成为末尾那条 user 消息：
            # 它必须留在 role="user" 里，又不能改写历史。
            messages.append({"role": "user", "content": head})
        return messages

    def _plan_turn(
        self,
        history: list[Turn],
        base_tokens: int,
        feature_context: bool,
        items: tuple[SupplementalItem, ...],
        *,
        has_pending_body: bool,
        caps: tuple[SupplementalCap, ...] = (),
        transient_items: tuple[str, ...] = (),
        transient_header: str | None = None,
    ) -> tuple[list[Turn], str, bool]:
        """按规划 §6.2 的次序决定「留哪些历史、选哪些块」，返回 (历史, 前置块, 有无记忆)。

        前置块的组装次序是「近期块 → 记忆块」，整个块再与 `pending_user` 用 `_BODY_SEPARATOR`
        连接成末尾那一条 user 消息（§38.3）。第三项只表示**记忆**是否入选：`system` 要不要追加
        `MEMORY_SYSTEM_ADDENDUM` 由它决定，近期块不参与。

        前提：`items` 与 `transient_items` 不同时为空。返回的前置块为空串表示两个块一条都没选入
        —— 此时调用方必须**不**追加 `MEMORY_SYSTEM_ADDENDUM`，输出与没有这两个参数时逐字节一致。

        预算的分配次序（§7.2 与 §11.3）：
        1. system、静态 addendum 与本轮 `pending_user` 已经算进 `base_tokens`，不会动它们
           —— 因此 `/kb` 与引用的博客正文（它们就在 `pending_user` 里）天然优先于全部资料。
        2. 普通聊天先锁定最近一组完整历史（规则 3）；能力轮次不锁定，历史可以被资料挤光
           （D-38 的硬上限不变）。
        3. 记忆资料按 `priority` 从小到大逐条尝试，装不下就跳过该条并继续试后面的（规则 4）；
           `caps` 里的分组上限用同一套取舍逻辑（先渲染再估）叠加在整轮预算之上。
        4. 近期消息在最后一组链历史**之后**、更早的历史对**之前**：从最新向旧选出连续后缀。
        5. 剩下的预算从新到旧补更早的完整历史对（规则 6）。

        `has_pending_body` 表示前置块会拼在本轮正文之前：组装体是
        `块 + _BODY_SEPARATOR + pending_user`，分隔符同样是外送内容，选中任一块后必须一并
        计入已用预算，否则后面的历史对会把它顶穿（D-38 的硬上限）。

        两个块都在 `pending_user` 之前，因此**永远挤不掉** system 与本轮正文 —— 挤不下的
        只会是块自己（近期块可以为零条）与更早的历史。
        """
        used = base_tokens
        keep_start = len(history)
        if not feature_context and history:
            # 规则 3：普通聊天「至少保留最近一组完整历史」—— 先把它计入已用预算，
            # 资料只能争剩下的。历史因此永远是一段连续的后缀。
            keep_start = max(0, len(history) - 2)
            used += _history_tokens(history[keep_start:])

        # priority 越小越优先；同优先级保持入参次序（sorted 是稳定排序）。
        selected: list[SupplementalItem] = []
        addendum_tokens = estimate_tokens(MEMORY_SYSTEM_ADDENDUM)
        for item in sorted(items, key=lambda entry: entry.priority):
            candidate = [*selected, item]
            # 选中一条就必然追加 addendum，因此它从第一条起就要参与这条资料的可行性判断：
            # 一条「只有不追加 addendum 才装得下」的资料必须被跳过，否则就会顶穿预算。
            cost = estimate_tokens(_render_supplemental_block(candidate)) + addendum_tokens
            if used + cost > self._max_input_tokens:
                continue
            if caps and _exceeds_group_caps(candidate, caps):
                # 该分组（或该分组所在的共享池）装不下这一条：跳过它继续试后面的条目，
                # 与整轮预算的取舍同款 —— 条目不可拆分，但后面的条目仍有机会。
                continue
            selected = candidate

        memory_block = ""
        if selected:
            memory_block = _render_supplemental_block(selected)
            used += estimate_tokens(memory_block) + addendum_tokens
            if has_pending_body:
                # 组装体是 `块 + _BODY_SEPARATOR + pending_user`：分隔符同样是外送内容。
                # 零条选中时不加这一笔，那一路要回退到改动前的输出（逐字节一致）。
                used += estimate_tokens(_BODY_SEPARATOR)

        # 规则 4：近期消息从最新向旧扩展**连续后缀**。装不下下一条更老的就停，不跳洞 ——
        # 模型看到的因此始终是真正的「最近一段」，不会出现时间线中间缺一条的伪上下文。
        # 规则本身在 `_fit_transient_suffix` 里，与 `select_recent_suffix` 共用一份。
        transient_block = ""
        if transient_items:
            # 选中至少一条时才会出现的那一个分隔符：后面接记忆块，或直接接当前正文。
            tail = (
                _BLOCK_SEPARATOR
                if memory_block
                else (_BODY_SEPARATOR if has_pending_body else "")
            )
            tail_tokens = estimate_tokens(tail)
            count = _fit_transient_suffix(
                transient_items,
                transient_header or "",
                self._max_input_tokens - used - tail_tokens,
            )
            if count:
                transient_block = _render_transient_block(
                    transient_header or "", transient_items[len(transient_items) - count :]
                )
                used += estimate_tokens(transient_block) + tail_tokens

        head = _BLOCK_SEPARATOR.join(
            part for part in (transient_block, memory_block) if part
        )

        # 规则 5：剩余预算从新到旧补更早的完整历史对。整对不可拆、也不跳着补，
        # 因此只要有一对装不下就停 —— 保留的历史始终是连续的一段后缀，与旧行为一致。
        while keep_start >= 2:
            pair = history[keep_start - 2 : keep_start]
            cost = _history_tokens(pair)
            if used + cost > self._max_input_tokens:
                break
            used += cost
            keep_start -= 2

        return history[keep_start:], head, bool(selected)

    def select_recent_suffix(
        self,
        session_key: str,
        system_prompt: str,
        *,
        pending_user: str | None = None,
        system_addendum: str | None = None,
        feature_context: bool = False,
        transient_user_items: tuple[str, ...] = (),
        transient_user_header: str | None = None,
    ) -> tuple[str, ...]:
        """算出**本轮候选的近期消息后缀 S1**，供装配层在解析 subject 之前调用（§45.2、R2）。

        返回 S1 的条目（入参顺序，旧到新）：只有落在 S1 里的大区近期消息才可以贡献公开
        记忆的 subject，调用方随后把 S1 原样交给 `build_messages`。参数与 `build_messages`
        的同名预算输入逐条对应，估算口径也完全一致 —— 两处一旦分叉，S1 就不再是
        「`_plan_turn` 最终选择的上界」。

        **不预留记忆块的额度**：计算时假定本轮既没有记忆资料块、也没有记忆的 system 说明。
        `_plan_turn` 里记忆块**先于**近期块取（lobby 设计 §11.3 的既有合同，不翻转），所以
        真正挑选近期消息时可用额度只会比这里更紧，它选中的条目必然是 S1 的**子集**：
        不在 S1 里的消息永不贡献 subject。

        已知的保守面（R2 的刻意取舍）：S1 内、随后被记忆块挤掉的那几条**仍会**贡献
        subject —— 它们在这一步是装得下的。精确解需要在记忆块与近期块之间求不动点，
        而两边互相挤压时不动点可能振荡，因此宁可放宽这一点。

        纯只读：不改历史、不推进代次、不写 `_sessions`；`_plan_turn` 与 `build_messages`
        的行为一个字都不变。`transient_user_items` 为空时返回空元组（这一轮没有近期消息，
        也就没有 S1）。
        """
        if not transient_user_items:
            return ()
        # 与 `build_messages` 同款：system 与静态 addendum **分别**估算（先拼接再估会少算
        # 非 CJK 分段的取整项），再计入本轮正文。
        used = estimate_tokens(system_prompt)
        if system_addendum:
            used += estimate_tokens(system_addendum)
        if pending_user is not None:
            used += estimate_tokens(pending_user)
        history = self._sessions.get(session_key, [])
        if not feature_context and history:
            # 规则 3 的同一笔预留：普通聊天先锁定最近一组完整历史，近期块只能争剩下的。
            # 能力轮次（feature_context）不锁定，历史可以被挤光（D-38）。
            used += _history_tokens(history[max(0, len(history) - 2) :])
        # 假定没有记忆块：块后面要么直接接本轮正文（`_BODY_SEPARATOR` 那一路），要么本轮
        # 没有正文、连分隔符都不存在。记忆块在场时 `_plan_turn` 的尾部连接符是更短的
        # `_BLOCK_SEPARATOR`，但它同时要付整块的额度，可用空间只会更小 —— 子集关系
        # 因此不依赖这一笔的取值。
        tail_tokens = estimate_tokens(_BODY_SEPARATOR) if pending_user is not None else 0
        count = _fit_transient_suffix(
            transient_user_items,
            transient_user_header or "",
            self._max_input_tokens - used - tail_tokens,
        )
        if not count:
            return ()
        return tuple(transient_user_items[len(transient_user_items) - count :])

    def session_count(self) -> int:
        """当前有历史的会话数。"""
        return len(self._sessions)

    def turn_count(self, session_key: str) -> int:
        """指定会话的记录条数（一轮对 = 2 条）。"""
        return len(self._sessions.get(session_key, []))
