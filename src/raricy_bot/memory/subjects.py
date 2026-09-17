"""公开个人记忆的 subject 解析（INTERFACES §43；公开设计 §6、§13.2、§13.3、§19.3）。

这一层回答「本轮到底是谁的公开记忆与这场对话有关」。它是本功能的**隐私边界**，
所以默认答案是**谁都不解析**：只有明确允许扫描的输入、本地公开索引里有的精确 username、
以及（对纯文本命中）一次成功的站点精确校验，三者同时成立才会产生一个 subject。

三件事：

1. 从调用方明确允许的文本段里枚举**完整**的站点用户名 token，与本地公开索引（大小写敏感）
   对表得到候选；`@` 前缀只提升同一 username 的优先级，不改变匹配本身。
2. 候选按 `(来源优先级, 来源内出现位置)` 排成确定次序，同一 owner 取最高来源，
   去重后截到 `max_subjects`。
3. 只有**纯文本命中**（没有宿主算好的稳定 owner key）才走站点精确校验：查询、**大小写完全
   一致的唯一**命中、`user_storage_key(结果 id)` 等于索引里的 owner key，三者同时成立才采用。

纪律：

- 稳定来源（当前发言者、短期会话参与者、大区近期消息自带的 subject）**不查网络**；
  同一 owner 已由稳定来源确立时，文本命中直接合并、也不查（§43.4 第 1 条、R6）。
- 任何失败（查询超时、限频、响应形状异常、坏索引、缓存形状不对）都只**少一个可选 subject**，
  绝不抛出：记忆是软故障，缺一份可选资料不影响聊天（D-60）。
- 不扫描模型回答、搜索结果、知识库片段与 MCP 输出；不做前缀、子串、拼音、昵称、语义或
  编辑距离匹配 —— 提取器不是通用实体识别器（公开设计 §2、§21 第 6 条）。
- 缓存只保存 `username -> owner_key | negative` 与过期时间，**正文不落缓存**（§13.2、R7/R22）。
- 本模块**不 import** `site/client.py`（§43.2）：站点能力以协议注入，测试传 fake，
  记忆包因此不背站点层的重依赖。
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..core.context import ConversationSubject, sanitize_username
from ..core.lobby_context import LobbyRecentMessage
from .models import PublicMemorySubject, user_storage_key

if TYPE_CHECKING:  # pragma: no cover - 只为注解：`site.models` 是纯 DTO，仍然不 import 站点客户端
    from ..site.models import ChatUserSummary

# 身份校验的缓存与本地节流（R7、§43.4）：**代码常量**，不做成 YAML 配置 ——
# 它们是对上游 20 次/分钟限频的实现保护，不是产品语义。
IDENTITY_CACHE_POSITIVE_TTL_SECONDS: float = 600.0  # 正结果 10 分钟
IDENTITY_CACHE_NEGATIVE_TTL_SECONDS: float = 60.0  # 负结果 1 分钟
IDENTITY_CACHE_MAX_ENTRIES: int = 512  # 有界，满了淘汰最旧写入的一条
IDENTITY_QUERY_LIMIT_PER_MINUTE: int = 15  # 站点查询本地节流，低于上游的 20 次/分钟
IDENTITY_QUERY_WINDOW_SECONDS: float = 60.0  # 节流的滑动窗口长度

# 只认这两个场景（§43.1、R4 同款口径）：`"dm"` 与未知值直接解析不出任何人。
_CHANNEL_KINDS: frozenset[str] = frozenset({"lobby", "comment"})

# 来源优先级（R5、§43.3）：数值越小越优先。
SOURCE_CURRENT_SPEAKER: int = 0
SOURCE_MENTION: int = 1
SOURCE_TEXT: int = 2
SOURCE_REPLY: int = 3
SOURCE_PARTICIPANT: int = 4
SOURCE_BLOG: int = 5
SOURCE_EXPANDED: int = 6
SOURCE_LOBBY_RECENT: int = 7

# token 只由这几个字符组成；**极大连续段**保证 token 前后不紧邻同类字符（公开设计 §6.2）。
_TOKEN_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-]+")

# 站点用户名合同（`docs/materials/chat-bot.md` §2.1、Global Constraints 第 15 条）：
# 3–20 字符，仅 ASCII 字母、数字、`_`、`-`，首尾不得是 `-` 或 `_`。索引键、提取出的 token
# 与最终写进 `PublicMemorySubject` 的 username 都按这一条判定。
_USERNAME_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,18}[A-Za-z0-9]")

# owner key 的形状：`user_storage_key` 的输出，64 位小写十六进制（§40.2 第 2 条）。
_OWNER_KEY_RE: re.Pattern[str] = re.compile(r"[0-9a-f]{64}")


class ChatUserSearch(Protocol):
    """resolver 需要的站点能力，只有这一个方法（§43.2、§44）。

    生产实现是 `site/client.py` 的 `SiteClient`；测试传 fake。协议而不是基类：
    本模块不认识站点客户端的具体类型，也不为它引入任何运行期依赖。
    """

    async def search_chat_users(self, username: str) -> tuple[ChatUserSummary, ...]: ...


@dataclass(frozen=True)
class PublicMemoryInputs:
    """一轮里允许公开记忆扫描的全部文本与来源（§43.1，字段名与次序照抄公开设计 §15）。

    每个字段的边界都是合同：调用方只放**本轮真的会提供给模型**的东西 —— 被省略的正文
    （超限未提供的博客与文章正文、预算不足未展开的引用、不在 S1 里的大区近期消息）
    既不入参也不扫描（公开设计 §6.3、R2）。`reply_text` 是已经拼好的直接引用块，
    里面的 token 一律按优先级 3 处理。
    """

    channel_kind: str
    current_subject: ConversationSubject | None
    conversation_subjects: tuple[ConversationSubject, ...]
    current_text: str
    reply_text: str | None
    blog_text: str | None
    expanded_clipboard_texts: tuple[str, ...]
    lobby_recent: tuple[LobbyRecentMessage, ...]


@dataclass(frozen=True)
class _Candidate:
    """一个待采纳的候选。

    稳定来源（当前发言者、会话参与者、大区近期消息的发言者）自带宿主算好的 `owner_key`；
    纯文本来源只有一个待校验的 `username`，`owner_key` 为 None。
    """

    priority: int
    position: int
    username: str
    owner_key: str | None = None


class PublicMemorySubjectResolver:
    """把一轮的输入解析成有序、去重的公开记忆 owner 列表（§43.2）。

    `index_provider` 是同步、无 I/O 的可调用对象（装配层接
    `MemoryService.public_username_index`）；本类只在构造时接收它，**不缓存返回值**：
    索引是「整份替换」发布的，跨轮留着旧快照会让撤回过的条目继续命中。
    `now` 注入单调时钟，缓存 TTL 与本地节流都读它。
    """

    def __init__(
        self,
        index_provider: Callable[[], Mapping[str, tuple[str, ...]]],
        client: ChatUserSearch,
        *,
        now: Callable[[], float] = time.monotonic,
        max_subjects: int,
    ) -> None:
        if isinstance(max_subjects, bool) or not isinstance(max_subjects, int) or max_subjects < 1:
            raise ValueError("max_subjects 必须是正整数")
        self._index_provider = index_provider
        self._client = client
        self._now = now
        self._max_subjects = max_subjects
        # 缓存只存身份：username -> (过期时刻, owner_key | None)，None 是负结果（R22）。
        # OrderedDict 的次序就是**写入次序**，满了从最旧的一端淘汰。正文绝不进来。
        self._cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
        # 本地节流：最近一分钟内发出过的站点查询时刻（滑动窗口）。
        self._queries: deque[float] = deque()

    async def resolve(self, inputs: PublicMemoryInputs) -> tuple[PublicMemorySubject, ...]:
        """解析本轮相关的公开记忆 owner；任何失败都只是少一个 subject，绝不抛出。

        同步完成 token 提取与索引对表，只对**需要校验**的候选 `await` 站点查询。
        返回的 tuple 已按 `(来源优先级, 来源内出现位置)` 排好并按 owner key 去重。
        """
        if inputs.channel_kind not in _CHANNEL_KINDS:
            return ()
        index = self._index()
        candidates = _collect(inputs, index)
        # 本轮**已经**由稳定来源确立的 username -> owner key（§43.4 第 1 条、R6）。
        # 先算全、再逐条解析：这样处理次序就不会决定「算不算已确立」——
        # 一条优先级 2 的正文命中，不该因为排在优先级 4 的参与者前面就多发一次查询。
        established: dict[str, str] = {}
        for candidate in candidates:
            if candidate.owner_key is not None:
                established.setdefault(candidate.username, candidate.owner_key)
        adopted: dict[str, _Candidate] = {}
        for candidate in candidates:
            if len(adopted) >= self._max_subjects:
                # 达到上限后低优先级来源不再扩大集合（公开设计 §6.1 末段）。这里可以直接停：
                # 候选按优先级递增、来源内位置递增地遍历，剩下的候选就算采纳成功也只能
                # **合并**进已选中的 owner，而合并不改变已选中的 (优先级, 位置)，
                # 因此对结果没有任何影响 —— 停下来还省掉后面全部站点查询。
                break
            resolved = await self._resolve_candidate(candidate, established, index)
            if resolved is None:
                continue
            adopted.setdefault(resolved.owner_key, resolved)
        return tuple(
            PublicMemorySubject(
                owner_key=candidate.owner_key,
                username=candidate.username,
                source_priority=candidate.priority,
            )
            for candidate in adopted.values()
            if candidate.owner_key is not None
        )

    # --- 内部实现 ----------------------------------------------------------

    async def _resolve_candidate(
        self,
        candidate: _Candidate,
        established: Mapping[str, str],
        index: Mapping[str, tuple[str, ...]],
    ) -> _Candidate | None:
        """把一个候选变成可采纳的候选；不能确定身份的一律返回 None（fail-closed）。"""
        if candidate.owner_key is not None:
            # 稳定来源：owner key 由宿主用 `user_storage_key` 算好，不查网络（§43.4 第 1 条）。
            return candidate
        known = established.get(candidate.username)
        if known is not None:
            # 同一 owner 已经由稳定来源确立：直接合并、不发查询（R6）。
            # 合并**保留文本命中自己的优先级**：同一 owner 取最小优先级（§43.3），
            # 于是「合并」与「校验成功」的结果完全一致，区别只在于省掉那次查询。
            return _Candidate(candidate.priority, candidate.position, candidate.username, known)
        keys = _index_keys(index, candidate.username)
        if not keys:
            return None
        owner_key = await self._verify(candidate.username, keys)
        if owner_key is None:
            return None
        return _Candidate(candidate.priority, candidate.position, candidate.username, owner_key)

    async def _verify(self, username: str, keys: tuple[str, ...]) -> str | None:
        """纯文本命中的身份精确校验（公开设计 §6.4、R6）：能采用就返回 owner key。

        三步：查缓存 → 占本地查询名额 → 站点查询并核对。**每一步失败都只返回 None**，
        该候选本轮不采用；负结果落缓存，所以一次坏响应不会变成重试风暴（R22）。
        """
        cached = self._cache_lookup(username)
        if cached is not None:
            _hit, owner_key = cached
            if owner_key is not None and owner_key in keys:
                return owner_key
            # 负结果，或缓存的 key 已经不在索引里（撤回、改名）——都不采用，且**不重查**：
            # 重查只会把一次瞬时失败升级成查询风暴，而 TTL 一过自然会再试。
            return None
        if not self._take_query_slot():
            # 本地节流超限：直接省略需要查询的候选，不等、不排队（§43.4）。
            # 这**不写缓存**：节流说的是站点负载，不是这个 username 的身份结论。
            return None
        try:
            results = await self._client.search_chat_users(username)
            match = _exact_match(results, username)
            owner_key = _match_owner_key(match, keys)
        except Exception:
            # 查询超时、限频、响应形状异常……全部 fail-closed（D-60）。
            # `asyncio.CancelledError` 继承 BaseException，不在这里，会照常向上传播。
            owner_key = None
        self._cache_store(username, owner_key)
        return owner_key

    def _index(self) -> Mapping[str, tuple[str, ...]]:
        """取一次索引快照；装配层给了坏东西（抛异常或不是映射）就只当空索引（§43.2）。"""
        try:
            snapshot = self._index_provider()
        except Exception:
            return {}
        return snapshot if isinstance(snapshot, Mapping) else {}

    def _cache_lookup(self, username: str) -> tuple[bool, str | None] | None:
        """查缓存：没命中返回 None；命中返回 `(True, owner_key | None)`，None 是负结果。"""
        entry = self._cache.get(username)
        if entry is None:
            return None
        expires_at, owner_key = entry
        if expires_at <= self._now():
            del self._cache[username]
            return None
        return True, owner_key

    def _cache_store(self, username: str, owner_key: str | None) -> None:
        """写入正/负结果；容量满了从**最旧写入**的一端淘汰（R22）。

        重写同一个 username 会把它挪到最新的一端：TTL 是从最后一次确认起算的。
        """
        ttl = (
            IDENTITY_CACHE_POSITIVE_TTL_SECONDS
            if owner_key is not None
            else IDENTITY_CACHE_NEGATIVE_TTL_SECONDS
        )
        self._cache.pop(username, None)
        self._cache[username] = (self._now() + ttl, owner_key)
        while len(self._cache) > IDENTITY_CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    def _take_query_slot(self) -> bool:
        """占用一个站点查询名额：滑动一分钟窗口内最多 `IDENTITY_QUERY_LIMIT_PER_MINUTE` 次。

        超限返回 False。判定与记账都是同步的（没有 await），并发轮次之间不会交错，
        因此不会因为并发而超出上限。
        """
        moment = self._now()
        while self._queries and self._queries[0] <= moment - IDENTITY_QUERY_WINDOW_SECONDS:
            self._queries.popleft()
        if len(self._queries) >= IDENTITY_QUERY_LIMIT_PER_MINUTE:
            return False
        self._queries.append(moment)
        return True


def _collect(
    inputs: PublicMemoryInputs, index: Mapping[str, tuple[str, ...]]
) -> list[_Candidate]:
    """按 §43.3 收集候选并排成确定次序；**不发网络请求**（只做提取与本地索引对表）。

    位置是**来源内**的次序：文本来源按 token 出现次序，`conversation_subjects` 与
    `lobby_recent` 按入参次序。大区近期消息里的**发言者**只经记录自带的 subject 参与，
    它的 `author_name` 不作文本候选（否则每个没被索引命中的发言者都会触发一次站点查询）。
    """
    candidates: list[_Candidate] = []
    positions: dict[int, int] = {}

    def position(priority: int) -> int:
        value = positions.get(priority, 0)
        positions[priority] = value + 1
        return value

    def add_stable(priority: int, subject: ConversationSubject | None) -> None:
        """登记一个稳定来源；形状不合合同的直接跳过（§40.2 在采用点复查一遍）。"""
        if subject is None:
            return
        username = sanitize_username(subject.label)
        if _OWNER_KEY_RE.fullmatch(subject.key) is None:
            return
        if _USERNAME_RE.fullmatch(username) is None:
            # 清洗控制字符之后的 username 可能已经不像站点用户名（也可能本来就是空串），
            # 那种值不能进 `PublicMemorySubject`（§40.2 第 2 条）。
            return
        candidates.append(_Candidate(priority, position(priority), username, subject.key))

    def add_text(priority: int, username: str) -> None:
        """登记一个文本 token；只有本地公开索引里有的 username 才成为候选（§6.2）。"""
        if not _index_keys(index, username):
            return
        candidates.append(_Candidate(priority, position(priority), username))

    def scan(priority: int, text: str | None) -> None:
        """登记一段文本里的全部完整 token（同一来源、同一优先级）。"""
        if not text:
            return
        for token in _iter_usernames(text):
            add_text(priority, token.group(0))

    add_stable(SOURCE_CURRENT_SPEAKER, inputs.current_subject)
    if inputs.current_text:
        for token in _iter_usernames(inputs.current_text):
            # `@` 前缀只提升优先级：`@alice` 与普通 `alice` 是同一 owner 的两个候选来源。
            mention = token.start() > 0 and inputs.current_text[token.start() - 1] == "@"
            add_text(SOURCE_MENTION if mention else SOURCE_TEXT, token.group(0))
    scan(SOURCE_REPLY, inputs.reply_text)
    for subject in inputs.conversation_subjects:
        add_stable(SOURCE_PARTICIPANT, subject)
    scan(SOURCE_BLOG, inputs.blog_text)
    for text in inputs.expanded_clipboard_texts:
        scan(SOURCE_EXPANDED, text)
    for message in inputs.lobby_recent:
        add_stable(SOURCE_LOBBY_RECENT, message.subject)
        scan(SOURCE_LOBBY_RECENT, message.content)
    candidates.sort(key=lambda candidate: (candidate.priority, candidate.position))
    return candidates


def _iter_usernames(text: str) -> Iterator[re.Match[str]]:
    """枚举文本里的完整 username token（公开设计 §6.2）。

    先取 `[A-Za-z0-9_-]` 的**极大**连续段（极大段保证 token 前后不紧邻同类字符），
    再套站点用户名合同的整体形状：所以 `alice` 命中 `alice`，而 `alice2`、`myalice`
    与 `alice-2024` 里的 `alice` 都不是完整 token。
    """
    for match in _TOKEN_RE.finditer(text):
        if _USERNAME_RE.fullmatch(match.group(0)) is not None:
            yield match


def _index_keys(index: Mapping[str, tuple[str, ...]], username: str) -> tuple[str, ...]:
    """该 username 在索引里的 owner key 元组；形状不对的桶整体丢弃（fail-closed）。

    索引是发布方维护的（重复 username 保留**全部** owner key，§42.3），但这里不把它
    当成可信输入：只有 64 位小写十六进制的键才是能拼进 `public/` 路径的形状。
    """
    raw = index.get(username)
    if not isinstance(raw, (tuple, list)):
        return ()
    keys: list[str] = []
    for key in raw:
        if isinstance(key, str) and _OWNER_KEY_RE.fullmatch(key) is not None and key not in keys:
            keys.append(key)
    return tuple(keys)


def _exact_match(results: tuple[ChatUserSummary, ...], username: str) -> ChatUserSummary | None:
    """只接受**大小写完全一致**的**唯一**命中；0 个或 ≥2 个 exact 都不采用（§43.4）。

    结果是大小写不敏感搜索的产物：`Alice` 与 `alice` 都可能被返回，但只有逐字相同的那一个
    才算身份，出现两个同名命中说明查询本身不足以确定身份。
    """
    matches = [result for result in results if getattr(result, "username", None) == username]
    if len(matches) != 1:
        return None
    return matches[0]


def _match_owner_key(match: ChatUserSummary | None, keys: tuple[str, ...]) -> str | None:
    """`user_storage_key(结果 id)` 必须等于该 username 在公开索引里的一个 owner key（§6.4）。

    对不上就是改名、被重新注册或索引重复，一律不采用。
    """
    if match is None:
        return None
    try:
        owner_key = user_storage_key(match.id)
    except (AttributeError, TypeError, UnicodeError):
        # 结果形状异常（缺 id、id 不是字符串、含孤立代理项）只让这个候选不成立。
        return None
    return owner_key if owner_key in keys else None
