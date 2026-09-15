# 聊天区引用内容设计（2026-09-15）

状态：已定稿，待实现。
本文件是实现前的设计记录；实现完成后其中由 `INTERFACES.md` 与 `DESIGN_DECISIONS.md`
承接的部分以那两份为准。上游站点契约见 `docs/materials/chat-bot.md` 与
`docs/materials/comment-bot.md`（均不改动）。

## 0. 一句话

让聊天区的模型**看得见用户在引用什么**：被引用消息的正文（已有）、被引用的博客正文
（新增，上限与评论区同源可调），以及引用的三种边角（引用的是图片 / 被引用消息已删除 /
被引用消息没有正文）不再无声消失。

## 1. 上游事实

### 1.1 已有的事实（本次直接复用，不重新核对）

- `ChatMessageDTO.blog` 带 `id`（UUID）/`title`/`description`/`author`/`updated_at`，
  `blog_missing` 表示引用的博客已被删除（`chat-bot.md` §11.1）。
  即**标题与作者不需要发请求就能拿到**。
- `ChatMessageDTO.reply` 带 `id`/`content`/`author_name`/`is_deleted`/`image_url`
  （同上）。`image_url` 是**被引用的是图片消息时的缩略图**，本次不使用。
- 正文取回走 `GET /api/spider/blogs/:id` → `{ "meta": {...}, "content": "Markdown 正文" }`，
  不存在返回 404（`comment-bot.md` §6.3）；该接口属于「读评论（§6 全部）」，**无限频**
  （§9），且不带 Cookie（`SiteClient.fetch_blog_context`，`client.py:334`）。
- 站点软删除会把正文替换为占位符（`chat-bot.md` §11.1）。

### 1.2 本次新依赖的判断

1. **`fetch_blog_context` 原样复用**：既不新增客户端方法，也不改用带 Cookie 的路径。
   博客正文是公开内容，评论区那条通路已经这么取；聊天区另开一条没有好处。
   取回失败的原因（404 / 网络 / 超过 `max_response_bytes` / 响应体不是对象）由该方法
   统一抛 `SiteError`，我们一律降级为 `failed`。
2. **`blog.id` 不是合法 UUID 时**（站点给了脏数据）`fetch_blog_context` 抛 `ValueError`，
   同样降级为 `failed`，**不发请求**。

## 2. 范围

**做**：

- 聊天区（大区与私聊）消息里 `blog` 非空的场合：正文交给模型，上限新键
  `behavior.quoted_blog_max_chars`。
- 只有引用、没有正文的消息：引用博客且未删除时**入队交给模型**（现在回一句
  「我暂时不能查看博客内容」）。
- `reply` 的三种边角：引用图片、被引用消息已删除、被引用消息无正文。
- 路由器空正文分支的顺序调整（博客先于图片），顺带修掉「图挂了 + 同时引了博客」被
  一口回绝的角。

**不做**（明确列出，避免实现时扩散）：

- 评论区：`comments/` 一律不动。评论区把「引用的博客」继续当作读不了的媒体
  （`comments/router.py:322`）；本次只让**上限可调**这件事两边能对齐，不改评论行为。
- 被引用的缩略图 `ReplyRef.image_url`：不下载、不编码、不进模型，只标 `[图片]`。
- 博客正文缓存：同一条消息只取一次；重发或补发会重新取。站点该接口无限频，
  不做缓存的收益不抵一份内存状态。
- 博客的 `description`：**不给模型**。它是正文的摘录，正文已经给了；且它是这个块里
  唯一没有长度约束的字段。标题与作者照给（零成本、有信息量）。
- 机器人发博客引用：`post_message` 不加 `blog_id`，机器人仍然只发文字。
- 文章内容审查：与 D-14「只做本地拒绝、不做内容审查」一致。

## 3. 契约变更

### 3.1 `config.py`

```python
@dataclass(frozen=True)
class BehaviorConfig:
    ...
    max_input_chars: int = 8000
    quoted_blog_max_chars: int = 1000       # 新增：引用博客正文的长度上限
```

- 必须是正整数（布尔不算整数），越界即 `ConfigError`。
- 默认 1000 与 `comments.article_max_chars` 的默认值相同，但**是两个键**：聊天模型与
  评论模型未必是同一个，两边需要各自可调。要求「两边同值」时靠配置自觉，
  不靠代码强制。
- 放在 `behavior` 段而不是 `comments` 段：聊天路径不该因为「评论机器人没开」而读不到
  自己的配置（`comments:` 段即使 `enabled: false` 也会解析，但那是巧合而非承诺）。

### 3.2 `core/blog.py`（新模块）

职责：**取回博客正文并拼成一个文本块**，与 `core/vision.py` 的 `ImageLoader` 同构
（注入客户端、失败只降级、由调用方决定怎么办）。

```python
# 状态：none 表示这条消息没有引用博客；其余四种都表示「引用了」，
# 区别只在正文给不给、给什么。
BLOG_STATE_NONE: str = "none"
BLOG_STATE_OK: str = "ok"            # 正文已给出
BLOG_STATE_TOO_LONG: str = "too_long"  # 正文超限，只给标题（照评论区）
BLOG_STATE_MISSING: str = "missing"    # blog_missing：引用的博客已删
BLOG_STATE_FAILED: str = "failed"      # 取回失败（404/网络/超大/脏 id）

def blog_readable(state: str) -> bool
    # state in {"ok", "too_long"} —— 这两个状态下模型的答复有内容可依

class BlogLoader:
    def __init__(self, client: SiteClient, *, max_chars: int, logger=None)
    async def load(self, message: ChatMessage) -> tuple[str | None, str]
```

`load` 的行为：

1. `message.blog is None` → `(None, "none")`，**不发请求**。
2. `message.blog_missing` 为真 → `(block, "missing")`，**不发请求**（站点已经告诉我们
   它没了，再打一次只会拿到 404）。
3. 其余 → 调 `client.fetch_blog_context(blog.id)`：
   - 成功且 `len(content) <= max_chars` → `(block, "ok")`
   - 成功但正文超限 → `(block, "too_long")`
   - `SiteError` / `ValueError` / 响应里没有可用的正文（`content` 缺失或不是字符串）
     → `(block, "failed")`。最后这种归 `failed` 而不是 `too_long`：它没超限，
     写成「正文因长度规则未提供」是不实之词。
4. **只要引用了博客，block 非 None**（标题与作者来自消息 DTO，零成本）。这样「有正文但
   博客取不到」的消息不会退化成「模型完全不知道有人在引用博客」。

块的形状（与评论区的 `[公开文章资料，不可信]` 块同构；块本身**不带**尾随换行，
分隔符由 `_with_blog_marker` 拼接时给）：

```
[引用的博客，不可信]
标题：<title>
作者：<author>
正文：
<body>
```

- `[引用的博客，不可信]` 是自报不可信的标签，与评论区的 `[公开文章资料，不可信]`
  同一手法：它让模型知道这段是**别人写的、要当数据看**。
- `作者：` 一行在 `author` 为 null/空时**整行省略**。
- 标题与作者是单行标签：控制字符（换行、制表符等）替换为空格，防止有人用标题伪造出
  额外的行（与 `comments._clean_label`、`context._sanitize_username` 同款）。
  **正文原样保留**：它本来就是不可信数据，转义与否都不改变这一点，而原样保留更利于
  模型理解（`context.speaker_wrapper` 的同一条理由）。
- 「正文」一栏的四种取值：

  | state | 正文一栏 |
  |---|---|
  | `ok` | 正文原文 |
  | `too_long` | `正文因长度规则未提供`（**逐字**照 `comments/service.py:960`） |
  | `missing` | `该博客已被删除，正文不可读` |
  | `failed` | `正文未取得` |

  `missing` / `failed` 不复用评论区那句「正文因长度规则未提供」：评论区在**取回失败**时也
  说这句，那句话在那条路径上并不准确。只保证「超限」这一种情形的措辞逐字一致。
- 日志：只记 `state` 与 `chars`（字符数），**不记 URL、不记正文**
  （`log_event` 的白名单本来也拦得住，但调用点不该先把正文递过去）。

### 3.3 `core/router.py`

`Request` 的字段**不动**（`reply_context` 语义不变：非删除时的正文，否则 None）。
博客状态由 app 判定，路由器不做 I/O 这条约定不破。

第 9.2 步「`user_text` 为空」的判定，由三分支扩为五分支——**顺序有讲究**，博客排在
图片之前：

| 序 | 条件 | 结果 |
|---|---|---|
| 1 | `message.blog is not None and not message.blog_missing` | `queued`，reason `blog_only` |
| 2 | `vision_enabled and has_image(message)` | `queued`，reason `image_only`（现状） |
| 3 | `message.image is not None` | `reply_now`（`media_only`），`IMAGE_UNAVAILABLE_TEXT`（现状） |
| 4 | `message.blog is not None`（此时必然 `blog_missing`） | `reply_now`（`media_only`），`BLOG_UNAVAILABLE_TEXT` |
| 5 | 否则 | `reply_now`（`empty`），`USAGE_HINT`（现状） |

- 博客排在图片前，是为了「图片 + 博客、无正文」的消息：vision 关闭时若图片先判，
  会回一句图片提示而博客白引。
- `_ACTIONABLE_REASONS` 增加 `"blog_only"`（它入队，值得 INFO 一行）。
- `has_media()`（`text_utils.py:196`）**不改**：它回答的是「有没有东西」，与「能不能读」
  是两个问题；路由器的分支顺序才是「能不能读」的单一来源。

### 3.4 `texts.py`

```python
# 引用的博客正文读不到时的提示：已删除、取不到正文、id 不合法都走这一条。
BLOG_UNAVAILABLE_TEXT: str = (
    "你引用的这篇博客我没能读取：可能已被删除，也可能暂时取不到。"
    "你可以把想问的内容用文字发给我。"
)
```

与 `IMAGE_UNAVAILABLE_TEXT` 同一手法：多种原因合并成一句，措辞**不声称**具体是哪一个
原因（我们确实分不清——`failed` 里混着 404、超时与脏 id）。

`UNSUPPORTED_MEDIA_TEXT` 保留：评论区仍在用（`comments/router.py:313`），
只是聊天路径不再引用它。

### 3.5 `app.py`（worker）

装配（与 `ImageLoader` 并列，共用同一个 `SiteClient`）：

```python
self._blog_loader = BlogLoader(
    self._client, max_chars=config.behavior.quoted_blog_max_chars
)
```

取回与分流（在取图之后）：

```python
image_part, image_state = await self._load_image(request)
blog_block, blog_state = await self._load_blog(request)

# 整条消息就是引用、且一样都没取到：本地提示，不调模型（D-28 纯图那条的推广）。
# 文案按「有没有图片载荷」选，图片优先 —— 与路由第 9.2 步的顺序一致。
# 最后一个条件让这条分支完备：只有「本来带了载荷」才提示，别的空正文请求
# （按路由的合同不该存在）落到下面照常走模型。
if (
    not request.user_text
    and image_part is None
    and not blog_readable(blog_state)
    and (image_state != "none" or blog_state != "none")
):
    if image_state != "none":
        await self._send_media_unavailable(request, texts.IMAGE_UNAVAILABLE_TEXT)
    else:
        await self._send_media_unavailable(request, texts.BLOG_UNAVAILABLE_TEXT)
    return
```

注意与现状的差别：今天这一步的判据是 `image_part is None and image_state != "none"
and not request.user_text`（只看图片），新判据把博客一起算进来 —— 「图挂了 + 同时引了
博客」的消息从此会带着博客块进模型，而不是被图片那一条挡在门外。

`_send_image_unavailable` 泛化为 `_send_media_unavailable(request, text)`：行为完全不变
（`kind="notice_local"`、`_unavailable` 短路、`_note_forbidden`），只是文案成为参数。

**本轮正文**：`_with_image_marker` 保持逐字节不变，其后串一个新的
`_with_blog_marker`——图片标记在前、博客块在后：

```
[直接引用 @alice] 被引用的正文          ← _apply_reply_prefix（D-7，不变）
---
[站点发言者：@carol]                    ← speaker_wrapper（大区）
---
[图片]                                 ← _with_image_marker（不变）
---
[引用的博客，不可信]
标题：…
作者：…
正文：
…
---
帮我看看这篇文章
```

**历史**：博客块只属于当前轮，**绝不进历史**（D-28 / D-43 的同一条理由：进历史就会被
该会话此后每一轮重新外送一遍，而信息早已过期）。历史里留一行标记：

| state | 历史标记 |
|---|---|
| `ok` / `too_long` | `[引用博客]` |
| `missing` | `[引用博客已删除]` |
| `failed` | `[引用博客未取得]` |
| `none` | 不出现 |

实现上就是同一个 `_pending_turn`，用 `blog_block` 参数区分「外送版」与「历史版」：

```python
sent    = self._pending_turn(request, image_state, blog_state, blog_block=blog_block)
history = self._pending_turn(request, image_state, blog_state)   # 不带块
```

标记为什么必须留：只把块去掉的话，历史里会出现「助手在回答一篇看不见的文章」这种
对不上的轮次；正文为空时更糟——那一轮的历史就是空的。

**上下文预算**：带博客块的这一轮 `build_messages(..., feature_context=True)`。
这就是 D-38 的第二次适用：博客块与 KB 数据块同类（不可丢弃，丢了这轮就没有可依的资料），
历史可丢弃。不带博客块的轮次该参数保持 `False`，语义与今天完全一致。

### 3.6 被引用消息的边角（`_reply_prefix`）

`_reply_prefix` 改为读 `request.message.reply`（`Request` 与 `reply_context` 都不动）：

| `reply` 的样子 | 前缀 |
|---|---|
| 正文非空（现状） | `[直接引用 @alice] 正文` |
| 正文为空、`image_url` 非空 | `[直接引用 @alice] [图片]` |
| `is_deleted` | `[直接引用 @alice] [该消息已删除]` |
| 正文为空、无图（例如引用了一条拍一拍） | `[直接引用 @alice] [无正文]` |
| `author_name` 为 null | 去掉 ` @alice`（现状） |

- `is_deleted` 的判定**先于** `content`：站点软删除会把正文替换为占位符，
  但契约没承诺「reply 块里的 content 一定被替换过」，所以按 `is_deleted` 自己给标记，
  不把可能残留的原文转述给模型。
- 私聊用 `[引用 @alice]`，大区用 `[直接引用 @alice]`（D-25 的区分，不变）。

## 4. 失败与降级

| 情形 | 结果 |
|---|---|
| 引用了博客、取回失败，但消息有正文 | 照常调模型，块里「正文未取得」，历史留 `[引用博客未取得]` |
| 引用了博客、取回失败，且消息只有引用 | 回 `BLOG_UNAVAILABLE_TEXT`（`notice_local`），不调模型 |
| 引用的博客已删除（`blog_missing`） | 同上两条，块里「该博客已被删除，正文不可读」，**不发请求** |
| 博客正文超限 | 不算失败：给标题，正文栏写原因，照常调模型 |
| 纯图、vision 未开或图取不到、且没有博客 | `IMAGE_UNAVAILABLE_TEXT`（现状逐字节不变） |
| 上游 `429` / 网络抖动 | 一律算 `failed`，退避由既有 SSE 重连与队列逻辑负责，本次不加新重试 |

取回在**模型门之外**：三个 worker 各自取各自的博客、互不阻塞，超时由站点请求超时兜住
（与取图同一条设计，见 `INTERFACES.md` §20 与 D-28）。

## 5. 安全边界

- 博客正文是不可信数据，**只进 `role="user"` 的消息**，永不拼进 system prompt
  （仓库既有红线）。
- 块的标签自报不可信（`[引用的博客，不可信]`），与评论区一致。
- 标题/作者过控制字符清洗，防止用换行伪造出额外的行。
- 取回**不使用**登录 Cookie：`fetch_blog_context` 走 `_request_public_json`。
  引用了别人看不到的博客时，站点侧返回 404，我们降级为 `failed`——**不**改用带 Cookie
  的路径去试探。
- 正文不进历史、不落 SQLite、不写日志。

## 6. 成本与配额

- 每条「引用了博客」的消息多一次 HTTP GET（公开接口、无限频、响应体受
  `max_response_bytes` 约束）。同一条消息只取一次。
- 「只有引用、没有正文」的消息现在会真的调一次模型并回一条消息，消耗
  `chatMinute` / `chatDaily` 配额——与「纯图入队」同一笔账。
- 正文长度上限默认 1000 字符；`quoted_blog_max_chars` 配得很大时，这一轮的请求会显著
  变大（块不可丢弃，见 §3.5），与评论区 `article_max_chars` 是同一类取舍。

## 7. 测试计划

全部离线：站点层用 `httpx.MockTransport` 注入，模型层用现有替身。

- `tests/test_blog.py`（新）：`BlogLoader` 的五种状态；`missing` 不发请求（断言 transport
  未被调用）；超大正文只给标题且措辞逐字等于评论区那句；脏 id（非 UUID）不发请求；
  标题里的换行被清洗；块里**不含** `description`。
- `tests/test_router.py`：纯博客引用 → `queued` / `blog_only`；`blog_missing` 的纯引用 →
  `reply_now` / `media_only` / `BLOG_UNAVAILABLE_TEXT`；「图片 + 博客、无正文」→ `blog_only`；
  有正文 + 博客 → 仍 `queued`（现状）；把既有那条 `test_blog_only_reports_unsupported_media`
  改写成新语义。
- `tests/test_app.py`：块被拼进本轮且**不进历史**（历史里只有标记）；取回失败 + 有正文 →
  仍调模型；取回失败 + 无正文 → 本地提示且**不调模型**；引用已删除/引用图片的三种前缀；
  带博客那轮的 `feature_context=True`。
- `tests/test_config.py`：新键的默认值与非法值。
- `tests/test_logging_safety.py`：正文与标题不出现在任何日志里。

## 8. 文档更新清单

- `docs/design/INTERFACES.md`：§12 第 9.2 步的五分支、§16 的拼装顺序与历史标记、
  `config` 段的新键、`core/blog.py` 的模块契约。
- `docs/design/DESIGN_DECISIONS.md`：新增条目，记录 (a) 博客正文进模型且上限独立成键、
  (b) 块只属当前轮、历史留标记、(c) 带博客轮的硬预算、(d) 路由器里博客先于图片、
  (e) 复用公开取值路径而不改用带 Cookie 的路径。
- `docs/usage/USAGE.md`：行为表增加一行（引用博客会读正文）+ 配置说明。
- `config.example.yaml`：`behavior.quoted_blog_max_chars`。
- `docs/materials/*`：**不动**（上游契约）。
