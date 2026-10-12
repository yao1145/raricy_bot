"""画像来源采集：本人博客核验、当前私聊回溯分页、脱敏与有界采样（设计 §4.1–§4.3）。

职责边界（合同 §B）：

- **只读**站点接口，不写任何东西；原始样本只留在内存，不落盘、不进日志、不建缓存。
- 只采**当前私聊里本人**的消息，以及 `author_id` 精确核验通过的本人博客。
  命令、删除、机器人、系统、纯图片、拍一拍消息都不是证据（设计 §4.2）。
- 每次采集都是**有界**的：博客列表最多 `blog_pages` 页、私聊最多 `message_pages` 页，
  页大小固定；达到上限仍不足时用稳定警告 token 报告，**不**宣称「用户总共只有这些」。
- 失败是软故障：某个来源读取失败只记警告并保留已取得的样本，不向调用方抛异常；
  身份缺失（拿不到 `user_id` 或频道）直接返回空样本。

每条资料都是**单个字符串**，首部是机器可解析的标签头 `[<标签>|<元信息>]`：

```text
[blog:<uuid>|2026-09-11 20:31:05] 标题

正文……
[dm:12345|2026-09-11 20:31:05] 用户正文
```

标签里带**站点对象 ID**（博客 UUID / 消息 ID），因此撰写器给出的 `evidence_refs`
可以直接回指到站点对象，用户能够自行核查（设计 §5）；日期放在元信息里，只用于展示。
私聊标签用全局自增的消息 ID，它本身就是全站唯一游标（chat-bot.md §11.1）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..redact import Redactor
from ..site.blog_models import PersonaBlogItem
from ..site.client import SiteClient, SiteError
from ..site.models import ChatMessage

# 采样目标（设计 §1 的产品规则）：最多 5 篇本人博客、最新 50 条本人私聊消息。
PERSONA_BLOG_TARGET: int = 5
PERSONA_MESSAGE_TARGET: int = 50

# 页大小：列表接口 per_page 上限 50（blog-bot.md §9），消息接口 limit 上限 100（§8）。
BLOG_PAGE_SIZE: int = 50
MESSAGE_PAGE_SIZE: int = 100

# 标签前缀与警告 token 都是**稳定标识**：控制器按它们选文案（文案由 parent 集中在 texts.py），
# 不得在别处硬编码中文错误串。
BLOG_LABEL: str = "blog"
MESSAGE_LABEL: str = "dm"

WARNING_IDENTITY_MISSING: str = "identity_missing"
WARNING_BLOG_USERNAME_MISSING: str = "blog_username_missing"
WARNING_BLOG_SEARCH_FAILED: str = "blog_search_failed"
WARNING_BLOG_DETAIL_FAILED: str = "blog_detail_failed"
WARNING_BLOG_SCAN_LIMIT: str = "blog_scan_limit"
WARNING_DM_FETCH_FAILED: str = "dm_fetch_failed"
WARNING_DM_SCAN_LIMIT: str = "dm_scan_limit"

# 资料来源的标签头：`[blog:<uuid>|2026-09-11 20:31:05]`；元信息可缺省。
_HEADER_PATTERN = re.compile(r"^\[(?P<label>[a-z]+:[^\]|\s]+)(?:\|(?P<meta>[^\]]*))?\]")

# 清洗规则（设计 §4.3）：移除脚本 / 样式块、隐藏 HTML 注释与图片加载。
_SCRIPT_BLOCK = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.DOTALL | re.IGNORECASE
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_IMG_TAG = re.compile(r"<\s*img\b[^>]*>", re.IGNORECASE)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BLANK_RUN = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class SourceBundle:
    """一次采集的结果。

    `texts` 是带标签头的资料行（博客在前、私聊按时间升序在后），`blog_count` /
    `message_count` 是**实际取得**的数量（不等于扫描量，也不代表用户总共只有这些）。
    `warnings` 是稳定 token 序列，可带 `=N` 计数后缀（如 `blog_detail_failed=2`），
    由控制器映射成用户可见文案；空样本是合法的，调用方据此提示资料不足。
    """

    texts: tuple[str, ...]
    blog_count: int
    message_count: int
    warnings: tuple[str, ...]


class PersonaSources:
    """画像来源采集器；只依赖注入的站点客户端与脱敏器。

    `blog_pages` / `message_pages` 是**扫描上限**（配置项 `persona.blog_pages` /
    `persona.message_pages`），不是采样目标：目标固定为 5 篇 / 50 条，扫描到目标就停，
    扫满上限还没到目标时用 `blog_scan_limit` / `dm_scan_limit` 报告（设计 §4.1、§4.2）。
    """

    def __init__(
        self,
        client: SiteClient,
        *,
        redactor: Redactor,
        blog_pages: int = 10,
        message_pages: int = 10,
    ) -> None:
        if isinstance(blog_pages, bool) or not isinstance(blog_pages, int) or blog_pages < 1:
            raise ValueError("blog_pages 必须是不小于 1 的整数")
        if (
            isinstance(message_pages, bool)
            or not isinstance(message_pages, int)
            or message_pages < 1
        ):
            raise ValueError("message_pages 必须是不小于 1 的整数")
        self._client = client
        self._redactor = redactor
        self._blog_pages = blog_pages
        self._message_pages = message_pages

    async def collect(
        self, user_id: str, username: str, channel_id: str, watermark: int
    ) -> SourceBundle:
        """采集本人博客与当前私聊历史；`watermark` 是命令消息 ID（不含）。

        博客与私聊相互独立：一边失败不影响另一边，只会各自留下警告 token。
        `watermark` 形状不对属于调用方的编程错误，在发请求前抛 `ValueError`；
        站点错误则在内部收敛成警告（软故障，设计 §4.3）。
        """
        if isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 1:
            raise ValueError("watermark 必须是大于 0 的消息 ID")
        if not user_id or not channel_id:
            # 拿不到稳定身份或频道就核验不了作者，任何读取都没有意义。
            return SourceBundle(
                texts=(),
                blog_count=0,
                message_count=0,
                warnings=(WARNING_IDENTITY_MISSING,),
            )

        blog_texts, blog_warnings = await self._collect_blogs(user_id, username)
        message_texts, message_warnings = await self._collect_messages(
            user_id, channel_id, watermark
        )
        return SourceBundle(
            texts=tuple(blog_texts + message_texts),
            blog_count=len(blog_texts),
            # 计数按**实际取得**的条数算；标签用的是站点对象 ID，不是序号。
            message_count=len(message_texts),
            warnings=tuple(blog_warnings + message_warnings),
        )

    # --- 博客（设计 §4.1）---------------------------------------------------

    async def _collect_blogs(self, user_id: str, username: str) -> tuple[list[str], list[str]]:
        """核验作者后取最多 5 篇本人博客；返回（资料行, 警告 token）。"""
        warnings: list[str] = []
        if not username:
            # 作者名来自站点 DTO；缺失时无法发起作者搜索，也不猜一个名字。
            return [], [WARNING_BLOG_USERNAME_MISSING]

        candidates, exhausted, search_failed = await self._scan_blog_candidates(
            user_id, username
        )
        if search_failed:
            warnings.append(WARNING_BLOG_SEARCH_FAILED)

        texts: list[str] = []
        detail_failed = 0
        for item in candidates:
            if len(texts) >= PERSONA_BLOG_TARGET:
                break
            try:
                detail = await self._client.fetch_persona_blog(item.blog_id)
            except SiteError:
                # 删除、无权限、正文不可用或形状不符：继续找后面的候选（设计 §4.1）。
                detail_failed += 1
                continue
            if detail.author_id != user_id:
                # 详情与列表不一致：不能当成本人文章，也不取它的正文。
                continue
            content = sanitize_source_text(detail.content or "", self._redactor)
            if not content:
                # 正文缺失，或清洗后没有留下可读内容（只有脚本或控制字符）：按不可用跳过。
                detail_failed += 1
                continue
            texts.append(
                _render_blog(
                    detail.blog_id,
                    detail.created_at,
                    sanitize_source_text(detail.title, self._redactor),
                    content,
                )
            )

        if detail_failed:
            warnings.append(_counted(WARNING_BLOG_DETAIL_FAILED, detail_failed))
        if len(texts) < PERSONA_BLOG_TARGET and not exhausted and not search_failed:
            # 扫满页数上限仍然不够：明确报告「读取范围内取得 N 篇」，不宣称用户只有 N 篇。
            warnings.append(WARNING_BLOG_SCAN_LIMIT)
        return texts, warnings

    async def _scan_blog_candidates(
        self, user_id: str, username: str
    ) -> tuple[list[PersonaBlogItem], bool, bool]:
        """分页扫描作者搜索结果，返回（本人候选, 是否已到列表末尾, 搜索是否失败）。

        列表按站点默认的发布时间排序（**不传** `sort`），取候选时不取正文；
        已经取到足够的候选就停止翻页，避免为多余的页付一次请求。
        """
        candidates: list[PersonaBlogItem] = []
        seen: set[str] = set()
        exhausted = False
        page = 1
        while page <= self._blog_pages and len(candidates) < PERSONA_BLOG_TARGET:
            try:
                result = await self._client.search_persona_blogs(
                    username, page=page, per_page=BLOG_PAGE_SIZE
                )
            except SiteError:
                return candidates, exhausted, True
            for item in result.items:
                if item.author_id != user_id or item.blog_id in seen:
                    continue
                seen.add(item.blog_id)
                candidates.append(item)
            if not result.has_next:
                exhausted = True
                break
            page += 1
        return candidates, exhausted, False

    # --- 私聊（设计 §4.2）---------------------------------------------------

    async def _collect_messages(
        self, user_id: str, channel_id: str, watermark: int
    ) -> tuple[list[str], list[str]]:
        """从命令消息向前回溯，取最新 50 条本人有效文字消息。"""
        warnings: list[str] = []
        collected: list[ChatMessage] = []
        seen: set[int] = set()
        exhausted = False
        before = watermark
        for _ in range(self._message_pages):
            if len(collected) >= PERSONA_MESSAGE_TARGET:
                break
            try:
                batch = await self._client.fetch_messages(
                    channel_id, limit=MESSAGE_PAGE_SIZE, before=before
                )
            except SiteError:
                warnings.append(WARNING_DM_FETCH_FAILED)
                return self._render_messages(collected), warnings
            fresh = [message for message in batch if message.id not in seen]
            if not fresh:
                # 没有新消息（或整页都是重复）说明已经回溯到头，不必再翻页。
                exhausted = True
                break
            for message in sorted(fresh, key=lambda item: item.id, reverse=True):
                # 从新到旧取样：跨页取满 50 条时留下的必须是最新的那一批。
                seen.add(message.id)
                if not _is_evidence(message, user_id, channel_id, watermark):
                    continue
                collected.append(message)
                if len(collected) >= PERSONA_MESSAGE_TARGET:
                    break
            before = min(message.id for message in fresh)
            if len(batch) < MESSAGE_PAGE_SIZE or before <= 1:
                # 一页装不满（或已经到了消息 ID 的起点）说明前面没有更多历史，
                # 不必再花一次请求去确认「回溯到头了」。
                exhausted = True
                break

        if len(collected) < PERSONA_MESSAGE_TARGET and not exhausted:
            warnings.append(WARNING_DM_SCAN_LIMIT)
        return self._render_messages(collected), warnings

    def _render_messages(self, messages: list[ChatMessage]) -> list[str]:
        """按时间升序渲染消息资料；编号从 1 起，与 `message_count` 一致。"""
        ordered = sorted(messages, key=lambda message: message.id)
        return [
            f"{_header(f'{MESSAGE_LABEL}:{message.id}', message.created_at)} "
            f"{sanitize_source_text(message.content, self._redactor)}"
            for message in ordered
        ]


def source_label(text: str) -> str | None:
    """取资料行首部标签（如 `blog:<uuid>`、`dm:12345`）；没有标签头返回 None。

    撰写器用它确定「本次请求给过模型的标签集合」，因此标签的解析规则只有这一份实现：
    首行必须是 `[<标签>]` 或 `[<标签>|<元信息>]`，正文里出现的方括号不算标签。
    """
    if not isinstance(text, str):
        return None
    matched = _HEADER_PATTERN.match(text)
    return matched.group("label") if matched is not None else None


def _counted(code: str, count: int) -> str:
    """带计数的警告 token：`blog_detail_failed=2`。"""
    return f"{code}={count}"


def _render_blog(
    blog_id: str, created_at: str, title: str, content: str
) -> str:
    """一篇博客资料：标签带站点文章 UUID，正文保留 Markdown 结构。"""
    header = _header(f"{BLOG_LABEL}:{blog_id}", created_at)
    body = content.strip()
    if title.strip():
        return f"{header} {title.strip()}\n\n{body}"
    return f"{header} {body}"


def _header(label: str, meta: str) -> str:
    """渲染标签头；元信息为空时省略竖线，保持 `[<标签>]` 的形状。"""
    cleaned = meta.strip()
    return f"[{label}|{cleaned}]" if cleaned else f"[{label}]"


def _is_evidence(
    message: ChatMessage, user_id: str, channel_id: str, watermark: int
) -> bool:
    """这条消息能不能作为画像证据（设计 §4.2 的排除表）。

    排除：命令、已删除、机器人（作者不是本人）、系统（没有作者 ID）、纯图片与拍一拍。
    频道字段缺失时按请求的频道处理：消息是从那个频道读回来的，这里是二次核对。
    消息携带的 `reply` 块不展开成额外资料——被引用的第三方文字不属于本人观点；
    本人正文里以 `>` 标出的第三方引用保持引用标记，由撰写器的规则排除在自述之外
    （设计 §4.2、§5）。
    """
    if message.author.id != user_id:
        return False
    if message.channel_id and message.channel_id != channel_id:
        return False
    if message.id >= watermark:
        # 命令之后的新消息不得混进这次历史采样（设计 §4.2）。
        return False
    if message.is_deleted or message.pat is not None:
        return False
    text = message.content.strip()
    if not text or text.startswith("/"):
        return False
    return True


def sanitize_source_text(text: str, redactor: Redactor) -> str:
    """清洗一段来源正文：先脱敏，再移除脚本、隐藏 HTML 与图片加载（设计 §4.3）。

    保留 Markdown 的段落与缩进结构（模型要看的是正文本身），只做三类清理：

    1. `<script>` / `<style>` 块、HTML 注释与 `<img>` 标签整体移除；
    2. Markdown 图片 `![alt](url)` 换成 `[图片]`——引用中的外链不该被当成可加载资源，
       也不该把外部 URL 原样交给模型；
    3. 控制字符（C0、DEL、C1）与孤立代理项移除，连续空行折叠为一段空行。

    本函数**不截断长度**：预算由撰写器按 token 分块处理，静默截断会让模型以为
    文章只有开头（设计 §4.3）。清洗后的文本仍属不可信数据，脱敏只是额外一道网。
    """
    value = redactor.redact(text) if text else ""
    value = _SCRIPT_BLOCK.sub("", value)
    value = _HTML_COMMENT.sub("", value)
    value = _IMG_TAG.sub("", value)
    value = _MARKDOWN_IMAGE.sub("[图片]", value)
    value = "".join(char for char in value if _is_visible(char))
    value = _BLANK_RUN.sub("\n\n", value)
    return value.strip()


def _is_visible(char: str) -> bool:
    """控制字符与孤立代理项一律丢弃；换行、制表与其余字符保留。"""
    if char in "\n\t":
        return True
    code = ord(char)
    if code < 0x20 or 0x7F <= code <= 0x9F:
        return False
    return not 0xD800 <= code <= 0xDFFF
