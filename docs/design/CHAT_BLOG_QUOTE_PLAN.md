# 聊天区引用内容实现计划

> **给执行者：** 按任务顺序做，每个任务自成一步测试闭环。设计口径见同目录
> `CHAT_BLOG_QUOTE_DESIGN.md`；上游契约见 `docs/materials/chat-bot.md` 与
> `comment-bot.md`（都不改）。

**目标：** 聊天区的模型能看到被引用消息的正文（已有）、被引用博客的正文（新增），
引用的三种边角（图片 / 已删除 / 无正文）有明确标记。

**做法：** 新增 `core/blog.py` 承担「取回 + 判定 + 拼块」，与 `core/vision.py` 同构；
路由器只多一个「纯博客引用入队」的分支（不做 I/O）；app 的 worker 负责取回、拼装，
并把博客正文挡在历史之外（只留标记）。

**技术栈：** Python 3.13 + asyncio + httpx + pytest（`pythonpath=["src"]`，无需安装）。

## Global Constraints

- 注释与文档字符串用中文，标识符用英文，**任何地方不得出现 emoji**。
- 运行期依赖仍然只有 `httpx`、`openai`、`PyYAML`、`aiohttp`：本次**不新增依赖**。
- 用户与站点的文本都是不可信数据，只进 `role="user"` 的消息，**永不拼进 system prompt**。
- Cookie、密码、API Key、消息正文、模型请求体**不得**写进日志或 SQLite。
- 测试**不得**发起真实连接：站点层用 `httpx.MockTransport` 注入，模型层用假客户端。
- `filterwarnings = ["error"]`：**测试输出必须是干净的**，任何依赖的 warning 都算失败。
- 成功判据一律是 JSON 信封里的 `code == 200`，不是 HTTP 状态码。
- 写请求**绝不**设置 `Origin` / `Referer`。
- `tests/` 被 `.gitignore` 忽略：**提交步骤只提交 `src/` 与 `docs/`**，
  测试文件仍然要写、要跑，只是不进 commit。
- 基线：改动前 `python -m pytest tests -q` 为 `1017 passed, 1 failed`，
  那一处失败是 `tests/test_mcp.py::test_stdio_provider_handshakes_and_discovers_tools`
  （本机 mcp 1.29.1 与 pyproject 钉的 2.x API 不兼容，**与本次无关**）。
  除此之外出现任何失败都是回归。

---

### Task 1: 配置键 `behavior.quoted_blog_max_chars`

**Files:**
- Modify: `src/raricy_bot/config.py`（`BehaviorConfig` 约 150-166 行、`_behavior` 约 468-500 行）
- Modify: `config.example.yaml`（`behavior:` 段）
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `BehaviorConfig.quoted_blog_max_chars: int`（默认 `1000`）。
  Task 4 用它构造 `BlogLoader(max_chars=...)`。

- [ ] **Step 1: 写失败的测试**

在 `tests/test_config.py` 的 `test_defaults_fill_missing_keys` 里 `max_input_chars` 那行后面加：

```python
    assert cfg.behavior.quoted_blog_max_chars == 1000
```

在 `test_dataclass_defaults_are_the_contract` 里加：

```python
    assert BehaviorConfig().quoted_blog_max_chars == 1000
```

把 `test_behavior_ints_must_be_at_least_one` 的 parametrize 列表加上一项
（放在 `"max_input_chars"` 后面）：

```python
        "quoted_blog_max_chars",
```

在 `test_yaml_values_override_defaults` 末尾追加与覆盖断言：

```python
    # 覆盖同一个键：它必须真的生效，而不只是「有默认值」。
    text += "behavior:\n  quoted_blog_max_chars: 250\n"
```

并在该测试的断言区加：

```python
    assert cfg.behavior.quoted_blog_max_chars == 250
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL —— `AttributeError: 'BehaviorConfig' object has no attribute 'quoted_blog_max_chars'`

- [ ] **Step 3: 实现**

`src/raricy_bot/config.py`，`BehaviorConfig` 里 `max_input_chars` 之后：

```python
    # 引用博客正文的长度上限。默认值与 comments.article_max_chars 相同，但**是两个键**：
    # 聊天模型与评论模型未必是同一个，两边各自可调。
    quoted_blog_max_chars: int = 1000
```

`_behavior()` 里 `max_input_chars` 那行之后：

```python
    quoted_blog_max_chars = _positive_int(
        container, "quoted_blog_max_chars", "behavior", 1000
    )
```

`BehaviorConfig(...)` 构造里 `max_input_chars=max_input_chars,` 之后：

```python
        quoted_blog_max_chars=quoted_blog_max_chars,
```

`config.example.yaml` 的 `behavior:` 段里，`max_input_chars` 附近加：

```yaml
  # 消息里引用的博客正文，交给模型的最多字符数（按 Unicode 字符数）。
  # 超限时只给标题并写明原因，与 comments.article_max_chars 的规则一致。
  quoted_blog_max_chars: 1000
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/config.py config.example.yaml
git commit -m "Add the quoted-blog length limit as a behavior key"
```

---

### Task 2: `core/blog.py` —— 取回、状态与拼块

**Files:**
- Create: `src/raricy_bot/core/blog.py`
- Test: `tests/test_blog.py`

**Interfaces:**
- Consumes: `SiteClient.fetch_blog_context(blog_id) -> BlogContext`（已有，
  `site/client.py:334`，失败抛 `SiteError`，id 非 UUID 抛 `ValueError`）；
  `ChatMessage.blog: BlogRef | None`、`ChatMessage.blog_missing: bool`（已有）。
- Produces:
  - `BLOG_STATE_NONE/OK/TOO_LONG/MISSING/FAILED: str`
  - `blog_readable(state: str) -> bool`
  - `blog_marker(state: str) -> str | None`
  - `build_blog_block(title: str, author: str | None, body: str) -> str`
  - `BlogLoader(client, *, max_chars: int, logger=None).load(message) -> tuple[str | None, str]`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_blog.py`：

```python
"""core/blog.py：引用博客的取回、状态判定与拼块。

全部离线：假客户端按预设返回文章或抛错，不做任何真实访问。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from raricy_bot.core.blog import (
    BLOG_STATE_FAILED,
    BLOG_STATE_MISSING,
    BLOG_STATE_NONE,
    BLOG_STATE_OK,
    BLOG_STATE_TOO_LONG,
    BlogLoader,
    blog_marker,
    blog_readable,
    build_blog_block,
)
from raricy_bot.logging_setup import get_logger
from raricy_bot.site.client import SiteError

BLOG_ID = "3a7e5c1e-1111-4111-8111-111111111111"
REF_TITLE = "消息里的标题"
UPSTREAM_TITLE = "上游 meta 里的标题"
BLOG_LOGGER = "raricy.test.blog"


class FakeBlogClient:
    """假站点客户端：返回一篇文章或抛预设的异常。"""

    def __init__(self, *, content: str | None = "正文", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[str] = []

    async def fetch_blog_context(self, blog_id: str) -> object:
        self.calls.append(blog_id)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=blog_id, title=UPSTREAM_TITLE, content=self.content)


def blog_ref(*, title: str = REF_TITLE, author: str | None = "bob") -> SimpleNamespace:
    """消息 DTO 里的博客引用；description 故意给一个记号串。"""
    return SimpleNamespace(
        id=BLOG_ID,
        title=title,
        description="摘要不该出现在块里",
        author=author,
        updated_at="2026-09-11 20:31:05",
    )


def message(*, blog: object = ..., blog_missing: bool = False) -> SimpleNamespace:
    """最小 ChatMessage；要构造「没有引用」必须显式传 blog=None。"""
    return SimpleNamespace(
        blog=blog_ref() if blog is ... else blog,
        blog_missing=blog_missing,
    )


def test_state_helpers_are_exhaustive() -> None:
    """readable 只认「有东西可依」的两种状态；none 没有历史标记。"""
    assert blog_readable(BLOG_STATE_OK) is True
    assert blog_readable(BLOG_STATE_TOO_LONG) is True
    assert blog_readable(BLOG_STATE_MISSING) is False
    assert blog_readable(BLOG_STATE_FAILED) is False
    assert blog_readable(BLOG_STATE_NONE) is False

    assert blog_marker(BLOG_STATE_NONE) is None
    assert blog_marker(BLOG_STATE_OK) == "[引用博客]"
    assert blog_marker(BLOG_STATE_TOO_LONG) == "[引用博客]"
    assert blog_marker(BLOG_STATE_MISSING) == "[引用博客已删除]"
    assert blog_marker(BLOG_STATE_FAILED) == "[引用博客未取得]"


def test_block_has_a_single_line_header_and_no_description() -> None:
    block = build_blog_block(REF_TITLE, "bob", "正文内容")

    assert block.startswith("[引用的博客，不可信]\n")
    assert "标题：消息里的标题\n" in block
    assert "作者：bob\n" in block
    assert block.endswith("正文：\n正文内容")
    assert "摘要" not in block


def test_block_omits_the_author_line_when_there_is_none() -> None:
    block = build_blog_block(REF_TITLE, None, "正文内容")

    assert "作者：" not in block


def test_block_sanitises_control_characters_in_the_title() -> None:
    """标题里带换行不能伪造出额外的行。"""
    block = build_blog_block("标题\n作者：冒充者", "bob", "正文")

    assert "标题：标题 作者：冒充者\n" in block
    assert block.count("作者：") == 1


async def test_loader_skips_messages_without_a_blog() -> None:
    client = FakeBlogClient()

    block, state = await BlogLoader(client, max_chars=100).load(message(blog=None))

    assert (block, state) == (None, BLOG_STATE_NONE)
    assert client.calls == []


async def test_loader_returns_the_block_for_a_normal_blog() -> None:
    client = FakeBlogClient(content="hello")

    block, state = await BlogLoader(client, max_chars=100).load(message())

    assert state == BLOG_STATE_OK
    assert block is not None
    # 标题取**消息里的引用**，不是上游 meta：取回失败时标题也必须还在。
    assert "标题：消息里的标题" in block
    assert block.endswith("正文：\nhello")
    assert client.calls == [BLOG_ID]


async def test_missing_blog_never_hits_the_network() -> None:
    """站点已经说了它没了，再打一次只会拿到 404。"""
    client = FakeBlogClient()

    block, state = await BlogLoader(client, max_chars=100).load(message(blog_missing=True))

    assert state == BLOG_STATE_MISSING
    assert block is not None and "该博客已被删除，正文不可读" in block
    assert client.calls == []


async def test_too_long_body_keeps_only_the_title() -> None:
    client = FakeBlogClient(content="x" * 11)

    block, state = await BlogLoader(client, max_chars=10).load(message())

    assert state == BLOG_STATE_TOO_LONG
    assert block is not None
    # 逐字照 comments/service.py 的那一句。
    assert "正文因长度规则未提供" in block
    assert "x" * 11 not in block


async def test_body_of_exactly_the_limit_is_accepted() -> None:
    client = FakeBlogClient(content="x" * 10)

    block, state = await BlogLoader(client, max_chars=10).load(message())

    assert state == BLOG_STATE_OK
    assert block is not None and block.endswith("正文：\n" + "x" * 10)


@pytest.mark.parametrize(
    "error",
    [SiteError(404, "http=404"), SiteError(0, "network"), ValueError("文章 id 不是 UUID")],
)
async def test_fetch_failure_degrades_without_losing_the_title(error: Exception) -> None:
    client = FakeBlogClient(error=error)

    block, state = await BlogLoader(client, max_chars=100).load(message())

    assert state == BLOG_STATE_FAILED
    assert block is not None and "正文未取得" in block
    assert "标题：消息里的标题" in block


async def test_response_without_a_usable_body_is_a_failure_not_a_length_rule() -> None:
    """上游没给正文 ≠ 正文超限：写成「长度规则」是不实之词。"""
    client = FakeBlogClient(content=None)

    block, state = await BlogLoader(client, max_chars=100).load(message())

    assert state == BLOG_STATE_FAILED
    assert block is not None and "正文因长度规则未提供" not in block


async def test_failure_logs_the_reason_but_never_the_content(caplog) -> None:
    client = FakeBlogClient(error=SiteError(404, "http=404"))
    logger = get_logger("test.blog")

    with caplog.at_level(logging.INFO, logger=BLOG_LOGGER):
        await BlogLoader(client, max_chars=100, logger=logger).load(message())

    # 字段名必须在 logging_setup.LOG_FIELDS 白名单里，否则会被静默丢掉。
    assert "event=blog.unavailable" in caplog.text
    assert "reason=error" in caplog.text
    assert REF_TITLE not in caplog.text
    assert BLOG_ID not in caplog.text
    assert "正文" not in caplog.text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_blog.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'raricy_bot.core.blog'`

- [ ] **Step 3: 实现**

新建 `src/raricy_bot/core/blog.py`：

```python
"""引用博客：状态判定、正文取回与拼块（设计 §3.2）。

模型只能看到「这一轮」的博客正文：它不进历史、不落库、不写日志。
正文取回走公开的 spider 接口（与评论机器人同一条通路，不带 Cookie）。
"""

from __future__ import annotations

import logging

from ..logging_setup import get_logger, log_event
from ..site.client import SiteClient, SiteError
from ..site.models import ChatMessage

_logger = get_logger("blog")

# 一条消息引用了博客时的五种状态。none 表示**没引用**，其余四种都表示引用了，
# 区别只在正文给不给、给什么。
BLOG_STATE_NONE: str = "none"
BLOG_STATE_OK: str = "ok"
BLOG_STATE_TOO_LONG: str = "too_long"
BLOG_STATE_MISSING: str = "missing"
BLOG_STATE_FAILED: str = "failed"

_READABLE_STATES: frozenset[str] = frozenset({BLOG_STATE_OK, BLOG_STATE_TOO_LONG})

# 历史里留下的标记。正文只属于当前轮，历史里退化成一行标记，否则那一轮会变成
# 「助手在回答一篇看不见的文章」，正文为空时更糟——历史直接是空的（设计 §3.5）。
_HISTORY_MARKERS: dict[str, str | None] = {
    BLOG_STATE_NONE: None,
    BLOG_STATE_OK: "[引用博客]",
    BLOG_STATE_TOO_LONG: "[引用博客]",
    BLOG_STATE_MISSING: "[引用博客已删除]",
    BLOG_STATE_FAILED: "[引用博客未取得]",
}

# 「正文」一栏的四种取值。超限那句**逐字**照 comments/service.py；另外两句是聊天区
# 自己的：评论区在取回失败时也说「长度规则」，那句在那条路径上并不准确。
_BODY_TOO_LONG: str = "正文因长度规则未提供"
_BODY_MISSING: str = "该博客已被删除，正文不可读"
_BODY_FAILED: str = "正文未取得"


def blog_readable(state: str) -> bool:
    """该状态下模型的答复有内容可依（标题或正文至少给了一样）。"""
    return state in _READABLE_STATES


def blog_marker(state: str) -> str | None:
    """历史里留下的标记；没有引用博客时为 None。"""
    return _HISTORY_MARKERS.get(state)


def _sanitize_label(value: object) -> str:
    """单行标签里的控制字符替换为空格，避免有人用标题伪造出额外的行。

    与 `comments._clean_label`、`context._sanitize_username` 同款。
    """
    if not isinstance(value, str):
        return ""
    return "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value)


def build_blog_block(title: str, author: str | None, body: str) -> str:
    """拼出交给模型的那一段。`[引用的博客，不可信]` 是自报标签，与评论区的
    `[公开文章资料，不可信]` 同一手法：让模型知道这段是**别人写的、要当数据看**。

    正文**原样保留**（它是不可信数据，转义与否都不改变这一点，原样更利于理解）；
    标题与作者是单行标签，控制字符必须清洗掉。
    """
    lines = ["[引用的博客，不可信]"]
    clean_title = _sanitize_label(title)
    if clean_title:
        lines.append(f"标题：{clean_title}")
    clean_author = _sanitize_label(author or "")
    if clean_author:
        lines.append(f"作者：{clean_author}")
    lines.append("正文：")
    lines.append(body)
    return "\n".join(lines)


class BlogLoader:
    """把一条消息引用的博客取回并拼成块。

    失败一律降级为「带标题的块 + 一个状态」，由调用方决定是照常调模型还是回本地提示。
    标题取**消息里的引用**（`message.blog.title`）而不是上游 meta：取回失败时它还在。
    """

    def __init__(
        self,
        client: SiteClient,
        *,
        max_chars: int,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._max_chars = max_chars
        self._logger = logger if logger is not None else _logger

    async def load(self, message: ChatMessage) -> tuple[str | None, str]:
        """返回 `(block | None, state)`；只有「没引用博客」才返回 None。"""
        blog = message.blog
        if blog is None:
            return None, BLOG_STATE_NONE
        if message.blog_missing:
            return self._block(blog, _BODY_MISSING), BLOG_STATE_MISSING

        try:
            article = await self._client.fetch_blog_context(blog.id)
        except (SiteError, ValueError) as exc:
            # ValueError 来自 fetch_blog_context 的 UUID 校验：站点给了脏 id，
            # 请求根本没发出去，与取回失败同等对待。
            return self._degrade(blog, "error", type(exc).__name__)

        content = getattr(article, "content", None)
        if not isinstance(content, str):
            # 上游没给正文 ≠ 正文超限，归 failed：「长度规则」那句话在这里是不实之词。
            return self._degrade(blog, "no_content")
        if len(content) > self._max_chars:
            self._log_too_long(count=len(content))
            return self._block(blog, _BODY_TOO_LONG), BLOG_STATE_TOO_LONG
        return build_blog_block(blog.title, blog.author, content), BLOG_STATE_OK

    def _block(self, blog: object, body: str) -> str:
        """按消息里的引用拼块；标题与作者可能缺失，`build_blog_block` 自己会省行。"""
        return build_blog_block(
            getattr(blog, "title", "") or "",
            getattr(blog, "author", None),
            body,
        )

    def _degrade(self, blog: object, reason: str, error: str = "") -> tuple[str, str]:
        """降级：块照给（标题还在），状态是 failed，日志只记原因不记内容。"""
        fields: dict[str, object] = {"reason": reason}
        if error:
            fields["error"] = error
        log_event(self._logger, logging.INFO, "blog.unavailable", **fields)
        return self._block(blog, _BODY_FAILED), BLOG_STATE_FAILED

    def _log_too_long(self, *, count: int) -> None:
        log_event(self._logger, logging.INFO, "blog.too_long", count=count)
```

`log_event` 的字段是**白名单制**（`logging_setup.LOG_FIELDS`）：`state` 与 `chars`
都不在白名单里，写了也会被静默丢掉。所以这里用 `reason`（为什么降级）与 `count`
（字符数）这两个既有字段，事件名本身承担「哪种状态」。**不新增白名单字段**：
这次没有需要记的新概念，而白名单每多一个字段就多一条泄漏面。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_blog.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/core/blog.py
git commit -m "Add the quoted-blog loader"
```

---

### Task 3: 路由器 —— 纯博客引用入队

**Files:**
- Modify: `src/raricy_bot/core/router.py`（`_ACTIONABLE_REASONS` 约 46-62 行、9.2 分支约 338-383 行、`queued` 的 emit 约 478-488 行）
- Modify: `src/raricy_bot/texts.py`（`UNSUPPORTED_MEDIA_TEXT` 之后）
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `texts.BLOG_UNAVAILABLE_TEXT`（本任务新增）；`ChatMessage.blog`、`ChatMessage.blog_missing`。
- Produces: 空正文的 `queued` 请求 reason 可能是 `"blog_only"`；`Request` 字段不变。

- [ ] **Step 1: 写失败的测试**

`tests/test_router.py` 的 `make_message` 增加 `blog_missing` 形参（现在它写死 `False`）：

```python
    blog_missing: bool = False,
```

并把它传给 `ChatMessage(..., blog_missing=blog_missing, ...)`。

把既有的 `test_blog_only_reports_unsupported_media` 整条替换为下面五条
（`handle_message` 的签名是 `(channel_id, message, event_id)`，三个位置参数）：

```python
async def test_blog_only_is_queued_when_the_blog_is_readable(store: Store) -> None:
    """引用了一篇没被删的博客、正文为空：进模型（reason=blog_only）。"""
    router, _, queue = build_router(store)
    message = make_message(
        41, channel_id=DM_CHANNEL, content="", blog=blog_ref()
    )

    result = await router.handle_message(DM_CHANNEL, message, 41)

    assert (result.action, result.reason) == ("queued", "blog_only")
    assert result.request is not None
    assert queue.qsize() == 1


async def test_deleted_blog_only_reports_a_local_notice(store: Store) -> None:
    """引用的博客已被删除：本地应答，不入队。"""
    router, _, queue = build_router(store)
    message = make_message(
        46, channel_id=DM_CHANNEL, content="", blog=blog_ref(), blog_missing=True
    )

    result = await router.handle_message(DM_CHANNEL, message, 46)

    assert (result.action, result.reason) == ("reply_now", "media_only")
    assert result.text == texts.BLOG_UNAVAILABLE_TEXT
    assert queue.qsize() == 0


async def test_blog_wins_over_an_unreadable_image(store: Store) -> None:
    """图片 + 博客、无正文、视觉未开：博客别被图片那条分支挡住。"""
    router, _, queue = build_router(store, vision_enabled=False)
    message = make_message(
        47, channel_id=DM_CHANNEL, content="", image=image_ref(), blog=blog_ref()
    )

    result = await router.handle_message(DM_CHANNEL, message, 47)

    assert (result.action, result.reason) == ("queued", "blog_only")
    assert queue.qsize() == 1


async def test_image_alone_still_reports_the_image_notice(store: Store) -> None:
    """只有图、没有博客：文案与改动前逐字一致。"""
    router, _, queue = build_router(store, vision_enabled=False)
    message = make_message(48, channel_id=DM_CHANNEL, content="", image=image_ref())

    result = await router.handle_message(DM_CHANNEL, message, 48)

    assert (result.action, result.reason) == ("reply_now", "media_only")
    assert result.text == texts.IMAGE_UNAVAILABLE_TEXT
    assert queue.qsize() == 0
```

同时在 `image_ref()` 之后加一个构造辅助：

```python
def blog_ref(*, title: str = "标题") -> BlogRef:
    """一篇没被删的博客引用。"""
    return BlogRef(id="b1", title=title, description="", author=None, updated_at="")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_router.py -q`
Expected: FAIL —— 三条新测试报 `AssertionError`（现在博客引用走的是 `reply_now` + `UNSUPPORTED_MEDIA_TEXT`），
`blog_missing` 形参不存在则报 `TypeError`。

- [ ] **Step 3: 实现**

`src/raricy_bot/texts.py`，在 `UNSUPPORTED_MEDIA_TEXT` 之后加：

```python
# 引用的博客正文读不到时的提示：已删除、取不到正文、id 不合法都走这一条。
# 与 IMAGE_UNAVAILABLE_TEXT 同一手法——多种原因合并成一句，措辞不声称具体是哪一个。
BLOG_UNAVAILABLE_TEXT: str = (
    "你引用的这篇博客我没能读取：可能已被删除，也可能暂时取不到。"
    "你可以把想问的内容用文字发给我。"
)
```

`src/raricy_bot/core/router.py` 的 `_ACTIONABLE_REASONS` 加一项：

```python
        "blog_only",
```

9.2 那一段整段替换（原文是「空正文：能看图就交给模型，否则给本地提示」到 `USAGE_HINT` 分支结束）：

```python
        # 9.2 空正文：能读的引用交给模型，读不到的给本地提示。
        # 博客排在图片**之前**：图片 + 博客、无正文的消息若先判图片，vision 关闭时
        # 会回一句图片提示而博客白引（设计 §3.3）。
        queued_reason: str | None = None
        if not user_text:
            if message.blog is not None and not message.blog_missing:
                queued_reason = "blog_only"
            elif self._vision_enabled and has_image(message):
                # 纯图消息入队。取图与降级由 app 的 worker 负责（设计 §3.5）：
                # 路由器不做 I/O，也就无从知道这张图能不能取到。
                queued_reason = "image_only"
            elif message.image is not None:
                # 有图但读不到：图片输入未开启，或 image_missing。
                return self._emit(
                    "reply_now",
                    "media_only",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.IMAGE_UNAVAILABLE_TEXT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
            elif message.blog is not None:
                # 走到这里必然 blog_missing：站方已经告诉我们它没了。
                return self._emit(
                    "reply_now",
                    "media_only",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.BLOG_UNAVAILABLE_TEXT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
            else:
                return self._emit(
                    "reply_now",
                    "empty",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.USAGE_HINT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
```

并把 9.2 上面那句旧注释

```
        # 9.2 空正文：能看图就交给模型，否则给本地提示。
        # 空正文不会命中下面 9.2-9.6 的任何一个分支（命令判定与探测词都要求非空内容），
        # 因此 `image_only` 置位之后直落第 10 步入队是安全的。
```

改成：

```
        # 空正文不会命中下面 9.3-9.7 的任何一个分支（命令判定与探测词都要求非空内容），
        # 因此 `queued_reason` 置位之后直落第 10 步入队是安全的。
```

最后一步的 emit 改一行：

```python
        return self._emit(
            "queued",
            queued_reason if queued_reason is not None else "queued",
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_router.py tests/test_text_utils.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/core/router.py src/raricy_bot/texts.py
git commit -m "Queue chat messages that only quote a blog"
```

---

### Task 4: app —— 取回、拼装与历史标记

**Files:**
- Modify: `src/raricy_bot/app.py`（装配约 133-136 行、worker 约 551-590 行与 669 行、
  `_send_image_unavailable` 约 839-855 行、`_with_image_marker` 约 857-870 行、
  `_pending_turn` 约 872-883 行）
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `BlogLoader`、`blog_readable`、`blog_marker`（Task 2）；
  `texts.BLOG_UNAVAILABLE_TEXT`（Task 3）；`BehaviorConfig.quoted_blog_max_chars`（Task 1）。
- Produces: 外送的本轮正文里含博客块、历史里只含标记。

- [ ] **Step 1: 写失败的测试**

`tests/test_app.py`：

1. `AppStub.__init__` 加两个可控字段（放在 `image_status` 之后）：

```python
        # 引用博客：默认回一篇文章；测试可改 blog_status / blog_content 制造降级。
        self.blog_content: str | None = "文章的正文"
        self.blog_status = 200
        self.blog_missing_payload = False
```

2. `AppStub._handle` 在图片分支之后加：

```python
        if request.method == "GET" and path.startswith("/api/spider/blogs/"):
            if self.blog_status != 200:
                return httpx.Response(self.blog_status, json={"code": self.blog_status})
            return httpx.Response(200, json={"meta": {"title": "文章标题"}, "content": self.blog_content})
```

3. `message_dto` 增加 `blog_id: str | None = None, blog_missing: bool = False` 两个形参，
   并把返回体里的 `"blog": None` / `"blog_missing": False` 换成：

```python
        "blog": (
            {
                "id": blog_id,
                "title": "文章标题",
                "description": "摘要",
                "author": "bob",
                "updated_at": "2026-09-11 20:31:05",
            }
            if blog_id is not None
            else None
        ),
        "blog_missing": blog_missing,
```

4. `message_frame` 同样透传这两个形参。

5. 新增测试：

```python
BLOG_UUID = "3a7e5c1e-2222-4222-8222-222222222222"
SPIDER_BLOGS = f"/api/spider/blogs/{BLOG_UUID}"


async def test_quoted_blog_body_reaches_the_model(make_app) -> None:
    """引用的博客正文随本轮交给模型，且自报不可信。"""
    env = await make_app()
    await wait_until(lambda: env.app.ready)

    env.stub.feed(message_frame(20, LOBBY, 200, "@mybot 你怎么看", blog_id=BLOG_UUID))
    await wait_until(lambda: env.model.calls >= 1)

    sent = env.model.inputs[0][-1]["content"]
    assert "[引用的博客，不可信]" in sent
    assert "标题：文章标题" in sent
    assert "正文：\n文章的正文" in sent
    assert env.stub.call_count("GET", SPIDER_BLOGS) == 1


async def test_quoted_blog_body_is_not_retained_in_history(make_app) -> None:
    """正文只属于当前轮：历史里只剩标记。"""
    env = await make_app()
    await wait_until(lambda: env.app.ready)

    env.stub.feed(message_frame(21, LOBBY, 201, "@mybot 你怎么看", blog_id=BLOG_UUID))
    await wait_for_status(env.app, 201)

    session = lobby_thread_session_key(201)
    history = env.app._ctx.build_messages(session, env.config.system_prompt)
    user_turns = [message["content"] for message in history if message["role"] == "user"]

    assert "[引用博客]" in user_turns[-1]
    assert "文章的正文" not in user_turns[-1], "正文不得进历史"


async def test_blog_only_message_reaches_the_model(make_app) -> None:
    """只有引用、没有正文的消息也会进模型。"""
    env = await make_app()
    await wait_until(lambda: env.app.ready)

    env.stub.feed(message_frame(22, LOBBY, 202, "@mybot", blog_id=BLOG_UUID))
    await wait_until(lambda: env.model.calls >= 1)

    sent = env.model.inputs[0][-1]["content"]
    assert "正文：\n文章的正文" in sent


async def test_blog_fetch_failure_with_text_degrades_to_a_marked_block(make_app) -> None:
    """取不到正文但有正文可答：照常调模型，块里写明没取到。"""
    env = await make_app()
    await wait_until(lambda: env.app.ready)
    env.stub.blog_status = 404

    env.stub.feed(message_frame(23, LOBBY, 203, "@mybot 你怎么看", blog_id=BLOG_UUID))
    await wait_until(lambda: env.model.calls >= 1)

    sent = env.model.inputs[0][-1]["content"]
    assert "正文未取得" in sent
    assert "标题：文章标题" in sent


async def test_blog_only_with_fetch_failure_gets_a_local_notice(make_app) -> None:
    """只有引用、又取不到正文：不调模型，发一条本地文案。"""
    env = await make_app()
    await wait_until(lambda: env.app.ready)
    env.stub.blog_status = 404

    env.stub.feed(message_frame(24, LOBBY, 204, "@mybot", blog_id=BLOG_UUID))
    await wait_until(lambda: env.stub.call_count("POST", LOBBY_MESSAGES) >= 1)

    assert env.model.calls == 0
    body = env.stub.bodies("POST", LOBBY_MESSAGES)[-1]
    assert body["content"] == BLOG_UNAVAILABLE_TEXT
    assert body["reply_to"] == 204


async def test_deleted_blog_is_never_requested(make_app) -> None:
    """站点已说博客没了：不产生任何 spider 请求，本地应答。"""
    env = await make_app()
    await wait_until(lambda: env.app.ready)

    env.stub.feed(
        message_frame(25, LOBBY, 205, "@mybot", blog_id=BLOG_UUID, blog_missing=True)
    )
    await wait_until(lambda: env.stub.call_count("POST", LOBBY_MESSAGES) >= 1)

    assert env.stub.call_count("GET", SPIDER_BLOGS) == 0
    assert env.model.calls == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_app.py -q -k blog`
Expected: FAIL —— 没有博客块（`[引用的博客，不可信]` 不在 prompt 里），
`blog_status` 一类的字段也还不存在。

- [ ] **Step 3: 实现**

导入（`app.py` 顶部 `from .core.vision import ...` 附近）：

```python
from .core.blog import BlogLoader, blog_marker, blog_readable
```

装配（`self._image_loader = ...` 之后）：

```python
        self._blog_loader = BlogLoader(
            self._client, max_chars=config.behavior.quoted_blog_max_chars
        )
```

worker 里，把取图那两行与紧随其后的「纯图但读不到」分支换成：

```python
            image_part, image_state = await self._load_image(request)
            # 引用博客同样在模型门之外：与取图并列，两边互不阻塞。
            blog_block, blog_state = await self._load_blog(request)
            if (
                not request.user_text
                and image_part is None
                and not blog_readable(blog_state)
                and (image_state != "none" or blog_state != "none")
            ):
                # 整条消息就是引用、且一样都没取到：必须给个交代，
                # 但不值得为它占一次模型调用。文案按消息带了什么来选：有图片载荷时
                # 仍走原来那句（与改动前逐字节一致）。
                await self._send_media_unavailable(
                    request,
                    texts.IMAGE_UNAVAILABLE_TEXT
                    if image_state != "none"
                    else texts.BLOG_UNAVAILABLE_TEXT,
                )
                return
```

本轮正文与硬预算：

```python
            pending = self._pending_turn(
                request, image_state, blog_state, blog_block=blog_block
            )
```

```python
            messages = self._ctx.build_messages(
                request.session_key,
                self._config.system_prompt,
                pending_user=pending,
                system_addendum="\n\n".join(system_addenda) or None,
                # 数据块不可丢弃，历史可以（D-38）：/kb 与引用博客同理（设计 §3.5）。
                feature_context=(
                    "kb" in request.enabled_features or blog_block is not None
                ),
            )
```

提交历史那一行：

```python
                    history_user = self._pending_turn(request, image_state, blog_state)
```

取博客的辅助方法（紧挨 `_load_image`）：

```python
    async def _load_blog(self, request: Request) -> tuple[str | None, str]:
        """取回本轮引用的博客；没有引用时完全不碰网络。"""
        return await self._blog_loader.load(request.message)
```

`_send_image_unavailable` 泛化为 `_send_media_unavailable(request, text)`：函数体不变，
把 `texts.IMAGE_UNAVAILABLE_TEXT` 换成参数 `text`，docstring 改成：

```python
    async def _send_media_unavailable(self, request: Request, text: str) -> None:
        """整条消息就是引用但读不到：本地提示，不调模型。

        kind 用 `notice_local` 而不是 `notice`：它与路由第 9.2 步的纯媒体提示同类，
        都是应答明确用户动作的本地回复（D-1）。用 notice 会占掉该用户 24 小时的
        主动通知名额，把一次「没读到」变成「今天别再提醒他」（D-30）。
        """
```

`_pending_turn` 增加博客标记：

```python
    @staticmethod
    def _pending_turn(
        request: Request,
        image_state: str,
        blog_state: str,
        *,
        blog_block: str | None = None,
    ) -> str:
        """构造本轮待提交的用户内容（不含直接引用，见 D-7）。

        `blog_block` 只有**外送那一份**才给：博客正文只属于当前轮，历史里只留标记
        （设计 §3.5）。其余两种情况形状逐字相同。

        - 大区：带上站点发言者标签，模型才分得清谁在说话（D-20），
          图片与博客标记在包装**内部**（它们都属于发言人这条消息）；
        - 私聊：就是正文本身。
        """
        text = BotApp._with_image_marker(request.user_text, image_state)
        text = BotApp._with_blog_marker(text, blog_state, blog_block)
        if request.channel_kind != "lobby":
            return text
        return speaker_wrapper(request.message.author.username, text)
```

`_with_image_marker` 之后加：

```python
    @staticmethod
    def _with_blog_marker(
        user_text: str, blog_state: str, blog_block: str | None = None
    ) -> str:
        """给本轮正文加上引用博客的块（外送版）或标记（历史版）。

        块与标记只差「正文给不给」这一处，两者都必须留痕：只把块去掉的话，历史里
        会出现「助手在回答一篇看不见的文章」这种对不上的轮次；正文为空时更糟，
        那一轮的历史会直接变成空的（D-28 的同一条理由）。
        """
        if blog_state == "none":
            return user_text
        head = blog_block if blog_block is not None else blog_marker(blog_state)
        if not head:
            return user_text
        if not user_text:
            return head
        return f"{head}\n---\n{user_text}"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_app.py -q`
Expected: PASS（含既有的纯图三条：`IMAGE_UNAVAILABLE_TEXT` 路径必须逐字节不变）

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/app.py
git commit -m "Feed the quoted blog body into the chat turn"
```

---

### Task 5: 被引用消息的三种边角

**Files:**
- Modify: `src/raricy_bot/app.py`（`_reply_prefix` 约 906-920 行）
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Request.message.reply: ReplyRef`（`id`/`content`/`author_name`/`is_deleted`/`image_url`）、
  `Request.reply_context: str | None`。**两者的语义都不改。**
- Produces: 引用前缀在三种边角下不再是 `None`。

- [ ] **Step 1: 写失败的测试**

`tests/test_app.py`，`message_dto` / `message_frame` 再透传两个形参
`reply_deleted: bool = False`、`reply_image_url: str | None = None`，
把它们写进 `reply` 字典的 `is_deleted` / `image_url`。

新增：

```python
async def test_deleted_reply_is_marked_for_the_model(make_app) -> None:
    env = await make_app()
    await wait_until(lambda: env.app.ready)

    env.stub.feed(
        message_frame(
            30, LOBBY, 300, "@mybot 这条你怎么看", reply_id=299, reply_deleted=True
        )
    )
    await wait_until(lambda: env.model.calls >= 1)

    sent = env.model.inputs[0][-1]["content"]
    assert "[直接引用 @someone] [该消息已删除]" in sent


async def test_image_reply_is_marked_for_the_model(make_app) -> None:
    env = await make_app()
    await wait_until(lambda: env.app.ready)

    env.stub.feed(
        message_frame(
            31,
            LOBBY,
            301,
            "@mybot 这张图呢",
            reply_id=300,
            reply_content="",
            reply_image_url="/api/images/img_9/raw",
        )
    )
    await wait_until(lambda: env.model.calls >= 1)

    sent = env.model.inputs[0][-1]["content"]
    assert "[直接引用 @someone] [图片]" in sent
    assert env.stub.call_count("GET", "/api/images/img_9/raw") == 0, "被引用的图不下载"


async def test_empty_reply_is_marked_for_the_model(make_app) -> None:
    env = await make_app()
    await wait_until(lambda: env.app.ready)

    env.stub.feed(
        message_frame(32, LOBBY, 302, "@mybot 这个呢", reply_id=301, reply_content="")
    )
    await wait_until(lambda: env.model.calls >= 1)

    sent = env.model.inputs[0][-1]["content"]
    assert "[直接引用 @someone] [无正文]" in sent
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_app.py -q -k reply`
Expected: FAIL —— 三种边角下前缀根本不存在（`_reply_prefix` 在 `not context` 时返回 `None`）。

- [ ] **Step 3: 实现**

`_reply_prefix` 整段替换：

```python
    @staticmethod
    def _reply_prefix(request: Request) -> str | None:
        """构造本轮的直接引用前缀；没有引用块时返回 None。

        大区与私聊用不同的标签（D-25）：只有大区是「直接引用」——
        它的历史里本来就有别的发言者，需要与发言者标签区分开。

        三种边角也要留痕，否则模型看到的就是一句没头没尾的话：被引用的是图片、
        被引用消息已删除、被引用消息没有正文（例如一条拍一拍）。
        `is_deleted` 的判定**先于** `content`：契约没承诺 reply 块里的正文一定被
        替换过，所以自己给标记，不把可能残留的原文转述给模型。
        """
        reply = request.message.reply
        if reply is None:
            return None
        if reply.is_deleted:
            body = _REPLY_DELETED_MARKER
        elif request.reply_context:
            body = request.reply_context
        elif reply.image_url:
            body = _REPLY_IMAGE_MARKER
        else:
            body = _REPLY_EMPTY_MARKER
        author = reply.author_name
        label = "直接引用" if request.channel_kind == "lobby" else "引用"
        header = f"[{label} @{author}]" if author else f"[{label}]"
        return f"{header} {body}"
```

文件顶部的 logger 附近加三个模块常量：

```python
# 被引用消息的三种边角标记。它们是**模型可见**的文本，不是给用户看的文案，
# 所以不进 texts.py（那里放的是用户可见的回复）。
_REPLY_IMAGE_MARKER: str = "[图片]"
_REPLY_DELETED_MARKER: str = "[该消息已删除]"
_REPLY_EMPTY_MARKER: str = "[无正文]"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_app.py -q`
Expected: PASS（含既有的 `test_reply_context_is_not_retained_in_history`）

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/app.py
git commit -m "Mark the edge cases of a quoted chat message"
```

---

### Task 6: 文档同步与全量回归

**Files:**
- Modify: `docs/design/INTERFACES.md`
- Modify: `docs/design/DESIGN_DECISIONS.md`
- Modify: `docs/README.md`
- Modify: `docs/usage/USAGE.md`
- Modify: `docs/design/CHAT_BLOG_QUOTE_DESIGN.md`（把「归档后的图片设计稿」引用改成现行口径）

- [ ] **Step 1: 更新 `INTERFACES.md`**

1. §1 的 `BehaviorConfig` 契约里加 `quoted_blog_max_chars: int = 1000`；
2. §5 `texts.py` 加 `BLOG_UNAVAILABLE_TEXT`；
3. 新增 `## 24. core/blog.py（引用博客）`：状态常量、`blog_readable`、`blog_marker`、
   `build_blog_block`、`BlogLoader.load` 的签名与四种状态的语义、块的形状、日志字段白名单；
4. §12 第 9.2 步换成五分支（顺序、reason、文案），并把「博客一律只当作有东西读不了，
   不给模型」那句删掉；
5. §16 的拼装顺序：`_with_image_marker` → `_with_blog_marker` → `speaker_wrapper`，
   `feature_context` 的置位条件加上「或本轮带博客块」，历史提交那一份不带块。

- [ ] **Step 2: 更新 `DESIGN_DECISIONS.md`**

新增条目（接在 D-46 之后），每条一段，写清「原文/问题 → 裁决 → 理由」：

- 引用博客的正文进模型，上限独立成键（为什么不复用 `comments.article_max_chars`）；
- 博客块只属当前轮、历史留标记（D-28 / D-43 的同一条理由，附「正文为空时历史会空」
  这个更硬的理由）；
- 带博客块的那一轮把 `context_input_tokens` 当硬上限（D-38 的第二次适用）；
- 路由器里博客先于图片（否则 vision 关闭时「图片 + 博客」会白引）；
- 只复用公开取值路径，不改用带 Cookie 的路径（引用了看不见的博客时，站点返回 404，
  我们降级，不去试探）；
- 被引用消息的三种边角标记；被引用的缩略图不下载。

- [ ] **Step 3: 更新 `docs/README.md`**

`design/` 表格里，`design/SITE_DOCS_KB_DESIGN.md` 那一行**之后**加：

```markdown
| `design/CHAT_BLOG_QUOTE_DESIGN.md` | 聊天区引用内容的实现前设计稿：被引用博客正文进模型、三种引用边角的标记 | 设计口径 |
| `design/CHAT_BLOG_QUOTE_PLAN.md` | 上一条的实现计划与任务清单 | 实施步骤；行为以 `design/` 与代码为准 |
```

- [ ] **Step 4: 更新 `docs/usage/USAGE.md`**

行为表里加一行（位置紧随「图片」那一行）：

```markdown
| 引用一篇博客时机器人会读正文（≤`behavior.quoted_blog_max_chars` 字，默认 1000） | 超限只给标题并写明原因 |
```

- [ ] **Step 5: 全量回归**

Run: `python -m pytest tests -q`
Expected: `1 failed, N passed`，唯一失败仍是 `tests/test_mcp.py::test_stdio_provider_handshakes_and_discovers_tools`

Run: `PYTHONPATH=src python -c "import raricy_bot.app, raricy_bot.core.blog, raricy_bot.core.router"`
Expected: 无输出、退出码 0

- [ ] **Step 6: 提交**

```bash
git add docs/design/INTERFACES.md docs/design/DESIGN_DECISIONS.md docs/design/CHAT_BLOG_QUOTE_DESIGN.md docs/README.md docs/usage/USAGE.md
git commit -m "Document the quoted-blog contract"
```

---

## 完成标准

1. `python -m pytest tests -q` 除那一条已知的 mcp 失败外全绿；
2. 聊天区里引用博客的消息，模型能看到标题与正文（超限时只有标题 + 原因）；
3. 博客正文**不出现**在任何历史轮次里，历史里只有 `[引用博客]` 一类的标记；
4. 只有引用、没有正文的消息会进模型；取不到正文时回本地提示且不调模型；
5. 被引用的图片 / 已删除消息 / 空消息三种边角在 prompt 里有明确标记；
6. `docs/` 里的四份文档与代码一致，且 `docs/README.md` 索引里有新文档。
