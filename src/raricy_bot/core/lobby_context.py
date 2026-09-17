"""大区近期消息缓冲：机器人被唤起前正在发生的公开讨论（INTERFACES.md §38）。

定位见 `docs/design/LOBBY_RECENT_CONTEXT_DESIGN.md`：机器人原先只知道精确 @ 它的消息，
对「被唤起之前大区里在聊什么」一无所知。本模块在内存里滚动保留最近若干条**公开文字**，
在一次真正的模型请求入队时把「这条触发消息之前」的那一批交给调用方，随后立即丢弃。

四条边界，任何改动都不能越过：

- **只存内存**：不写 SQLite、不写日志、不发网络请求；进程退出即释放全部正文。
- **纯同步**：`observe` / `peek_before` / `discard_through` 里没有 `await`，
  调用方因此能依赖「取快照 → 构造请求 → 入队 → 丢弃」这一整段不被其它协程插入（§10.3）。
- **正文只在构造时读一次**：`LobbyRecentMessage.content` 已经是 `content[:500]`，
  后续代码不得再回头去读 DTO 的完整正文。
- **不做任何展开**：图片不下载、博客只要标题、`[@<内容ID>]` 保持字面量、`reply.content`
  不递归复制（设计 §2 的「明确不做的事情」）。

本模块不 import Store、不 import 网络层，也不加锁：它只被事件循环里的同步片段调用。
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass

from ..site.models import ChatMessage
from .context import ConversationSubject, sanitize_username

# 三组容量都是**固定合同**（设计 §1、§5.1）：本期不新增 YAML 开关。
MAX_RECENT_MESSAGES: int = 50
MAX_CONTENT_CHARS: int = 500
# 博客标题是独立的 DTO 字段，正常情况下站点不会给出这个量级的标题；这只是兜底，
# 免得一个异常载荷绕过「单条容量」这个控制。
MAX_BLOG_TITLE_CHARS: int = 200
# 去重窗口：resync 一次最多拉 100 条，512 足以覆盖「刚消费完就被重放」的重叠段。
MAX_SEEN_IDS: int = 512

# 带博客的消息在正文之后附这一行。它描述的是「这条消息引用了什么」，不是正文本身，
# 因此单独成行、且只在标题非空时出现。
BLOG_QUOTE_TEMPLATE: str = "[引用博客：{title}]"
# 近期块里每条消息的发言者标签。形状与 `speaker_wrapper` 有意不同：设计 §5.2 的
# 版面里标签与正文之间不插 `---`（那是块与块之间的分隔符，每条消息都来一条会糊成一片）。
SPEAKER_LABEL_TEMPLATE: str = "[站点发言者：@{name}]"


@dataclass(frozen=True)
class LobbyRecentMessage:
    """一条已被判定为「可交给文本模型的公开消息」。

    `content` 与 `blog_title` 都是截断后的结果，`author_name` 是原始用户名
    （渲染时才清洗控制字符）。除此之外的一切 —— 作者 id、图片 URL、博客描述与正文、
    拍一拍目标、`created_at` —— 都不保存：它们对这层上下文没有用处，留下只会扩大泄露面。

    `subject` 是**唯一**的例外（R1、§46）：它由调用方（`core/router.py`）算好，里面只有
    不可逆的 owner key 与清洗前的站点用户名，**没有原始作者 id**。本模块不 import 任何
    memory 模块（D-61），也不解释这个 key —— 它只是替调用方随条目搬运这段元数据。
    """

    sequence: int
    message_id: int
    author_name: str
    content: str
    blog_title: str | None
    subject: ConversationSubject | None = None


class LobbyRecentContextBuffer:
    """大区公开消息的滚动 FIFO 与消费边界（§38.1）。

    `_messages` 是真正的近期上下文（有界 FIFO，满了挤掉最旧的）；`_seen_ids` 只负责
    「同一条消息被 SSE 与 resync 各投递一次」的去重，与正文无关，因此可以独立淘汰。
    两者刻意分开：把已消费的 id 也留在去重表里，才不会被一次紧跟着的 resync 塞回来。
    """

    def __init__(
        self,
        *,
        capacity: int = MAX_RECENT_MESSAGES,
        seen_capacity: int = MAX_SEEN_IDS,
    ) -> None:
        self._messages: deque[LobbyRecentMessage] = deque(maxlen=capacity)
        self._seen_ids: OrderedDict[int, None] = OrderedDict()
        self._seen_capacity = seen_capacity
        self._next_sequence = 0

    def observe(
        self, message: ChatMessage, subject: ConversationSubject | None = None
    ) -> int:
        """观察一条大区消息，返回本次到达序号（同步、无 I/O）。

        **每条**消息都会推进序号，包括那些没有可保存文本的（图片、拍一拍、已删除、
        空正文）—— 它们进不了 FIFO，但仍然要能充当「这条触发消息之前」的边界，
        否则紧随其后的一条消息就会把它后面到达的旁观消息也一起吃掉。

        重复 `message.id` 只推进边界、不重复入队：SSE 与 resync 会看见同一条消息两次，
        而已经消费掉的 id 也留在去重表里，避免刚消费完就被一次 resync 原样塞回来。

        `subject` 由调用方算好后随条目一起保存（R1、§46）：它**不参与去重**（去重只看
        `message_id`）、不参与 `peek_before` / `discard_through` 的边界语义，也不改变本方法
        的返回值。默认 None 时与升级前逐字节一致；记忆路径未装配时调用方传 None，
        这里不做任何计算（本模块不 import memory，D-61）。
        """
        self._next_sequence += 1
        sequence = self._next_sequence

        if message.id in self._seen_ids:
            self._seen_ids.move_to_end(message.id)
            return sequence
        self._seen_ids[message.id] = None
        while len(self._seen_ids) > self._seen_capacity:
            self._seen_ids.popitem(last=False)

        stored = _to_recent_message(message, sequence, subject)
        if stored is not None:
            self._messages.append(stored)
        return sequence

    def peek_before(self, sequence: int) -> tuple[LobbyRecentMessage, ...]:
        """返回 `sequence` **之前**的不可变快照（旧到新），不删除任何东西。

        严格小于：触发消息自己不在里面 —— 它已经作为当前轮正文交给模型了（§6.2）。
        """
        return tuple(item for item in self._messages if item.sequence < sequence)

    def discard_through(self, sequence: int) -> None:
        """从队首删除所有序号 `<= sequence` 的条目（消费边界到此为止）。

        消费是**单向**的：调用方只有在请求真的进入模型工作队列之后才调它，
        之后无论模型失败、额度拒绝还是发送失败都不回滚（设计 §3.3）。
        """
        while self._messages and self._messages[0].sequence <= sequence:
            self._messages.popleft()

    def __len__(self) -> int:
        """当前可用文本条目数（正在等待被某次唤起取走的消息）。"""
        return len(self._messages)


def render_lobby_recent(message: LobbyRecentMessage) -> str:
    """把一条记录渲染成模型看到的那一小段文本（设计 §5.2 的版面）。

    形如 `[站点发言者：@bob]\\n正文\\n[引用博客：标题]`：正文与标题都原样保留
    （它们是不可信数据，转义与否都不改变这一点），只有用户名要清洗控制字符 ——
    否则一个带换行的用户名就能伪造出一行发言者标签。

    渲染是**逐条**做的，返回值由调用方拼成元组交给 `ContextManager`：
    通用上下文模块不认识 `LobbyRecentMessage`，也不该认识（§38.3）。

    `message.subject` **不参与渲染**（§46）：给模型看的永远只有清洗后的站点用户名与正文，
    owner key 一个字都不出现在这里。
    """
    body = message.content
    if message.blog_title:
        quote = BLOG_QUOTE_TEMPLATE.format(title=message.blog_title)
        body = f"{body}\n{quote}" if body else quote
    label = SPEAKER_LABEL_TEMPLATE.format(name=sanitize_username(message.author_name))
    return f"{label}\n{body}"


def _to_recent_message(
    message: ChatMessage,
    sequence: int,
    subject: ConversationSubject | None = None,
) -> LobbyRecentMessage | None:
    """按设计 §5.1 的规则表决定一条消息存不存、存什么；没有可存文本时返回 None。

    只有图片、拍一拍、已删除这几类**完全不看正文**；其余情况正文照存，
    图片部分直接忽略（文字带图的消息仍然是有信息量的）。

    `subject` 原样带给记录：它不影响「存不存」的判定，也不参与任何文本准入。
    """
    if message.is_deleted or message.pat is not None:
        return None
    content = message.content[:MAX_CONTENT_CHARS]
    blog_title = _blog_title(message)
    if not content and not blog_title:
        return None
    return LobbyRecentMessage(
        sequence=sequence,
        message_id=message.id,
        author_name=message.author.username,
        content=content,
        blog_title=blog_title,
        subject=subject,
    )


def _blog_title(message: ChatMessage) -> str | None:
    """博客标题（截断后）；没有博客、标题为空或只有空白时为 None。

    标题为空且正文也为空的消息不进 FIFO：`[引用博客：]` 这一行没有任何信息，
    而一个空标题的引用本来也不指向任何可读的东西。
    """
    blog = message.blog
    if blog is None:
        return None
    title = blog.title[:MAX_BLOG_TITLE_CHARS]
    return title if title.strip() else None
