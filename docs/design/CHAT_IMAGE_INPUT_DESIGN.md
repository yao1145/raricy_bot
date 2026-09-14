# 聊天图片输入设计（2026-09-14）

状态：已定稿，待实现。
本文件是实现前的设计记录；实现完成后其中由 `INTERFACES.md` 与 `DESIGN_DECISIONS.md`
承接的部分以那两份为准。上游站点契约见 `docs/materials/chat-bot.md`（不改动）。

## 0. 一句话

给机器人加一条**可关闭**的识图通路：把用户消息里带的那张图从站点取回内存、编码为
base64 data URL，随**当前这一轮**一并交给模型。图片不进历史、不落库、不写日志。

## 1. 上游事实（核对自站点源码，非文档）

核对对象：`github.com/raricycms/raricy.com`，提交 `9bd397f`（2026-09-14）。
`chat-bot.md` §11.1 把 `image.url` 写成 `"https://..."`，与源码不符；按该文档 §0
「若与源码不符以源码为准」，以源码为准。

1. **`image.url` 是相对路径**（`src/lib/chat-service.ts:769`）：
   `{ id, url: "/api/images/<id>/raw", mime_type }`。
   因此取图必须用 `urljoin(site.base_url, url)` 解析；同时也顺带兼容了「将来改成绝对
   URL」的情形（`urljoin` 对绝对 URL 幂等）。
2. **图床 MIME 白名单含 SVG**（`src/lib/image-upload.ts:17`）：
   `image/png|jpeg|gif|webp|svg+xml`；单图上限 `MAX_IMAGE_SIZE = 10 MiB`。
   `GET /api/images/:id/raw` 对 SVG 下发 `Content-Disposition: attachment`。
   我们**不转发 SVG**：模型对 SVG 的 data URL 没有有效理解，而它是白名单里唯一带脚本
   能力的格式。
3. **raw 路由不限频**（`src/app/api/images/[id]/raw/route.ts` 内无 `rateLimit` 调用）。
   取图不消耗 `chatMinute` / `chatDaily` / `chatPoll`。公开图带
   `Cache-Control: public, max-age=31536000, immutable`，我们不做缓存。
4. **公开图无需登录，私有图对非作者返回 404**（同上文件）：
   `isPublic === false` 时仅作者/管理员可见，无权者被伪装成 `404 Not Found`；
   上传默认 `isPublic ?? true`（`src/lib/image-service.ts:58`），所以聊天里的图
   基本都是公开的。

## 2. 范围

**做**：大区与私聊消息（`ChatMessage.image`）。图片随当前轮交给模型。

**不做**（明确列出，避免实现时扩散）：

- 被引用消息的缩略图 `ReplyRef.image_url`：无 id、无 mime、且是缩略图而非原图。
- 博客评论侧的图片：`CommentNode` 只保留了 `has_image` 布尔，要支持得先扩解析；
  本次范围外。
- 图片缓存 / 去重：同一条消息只在被处理时取一次；重试或补发会重新下载。
- 多图：站点 DTO 每条消息最多一张图，接口按「一张」设计。
- 机器人发图：`POST /api/images` 不实现，机器人仍然只发文字。
- 图片内容审查、NSFW 判定：与 D-14「只做本地拒绝，不做内容审查」一致。

## 3. 契约变更

### 3.1 `config.py`

```python
@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    temperature: float = 0.4
    timeout_seconds: float = 45.0
    max_output_tokens: int = 600
    vision_enabled: bool = False        # 新增：默认关闭
    max_image_bytes: int = 5242880      # 新增：5 MiB，下载期硬上限
```

- `vision_enabled` 必须是布尔（YAML 里是 `true`/`false`，写成字符串即 `ConfigError`）。
- `max_image_bytes` 必须是正整数（布尔值不算整数）且 `<= 10485760`（站点单图 10 MiB
  是上游常量，配更大没有意义，只会掩盖意图）。
- 两者都在 `model` 段：它们描述的是「这个模型能吃什么」。
- 默认关闭的理由：默认配置的模型未必支持视觉；一旦不支持就是每个带图轮次一次
  `400 bad_request` 与一条失败提示。默认关闭让既有部署升级后行为**逐字节不变**。

### 3.2 `site/client.py`

```python
class ImageFetchError(Exception):
    """取图失败；reason 是稳定短标识（用于日志与降级判定）。"""
    def __init__(self, reason: str, status: int = 0) -> None:
        self.reason = reason    # "host_not_allowed" | "too_large" | "http" | "network"
        self.status = status    # 仅 "http" 有意义，其余为 0

class SiteClient:
    async def fetch_image(self, url: str, *, max_bytes: int) -> bytes
```

行为：

1. `target = urljoin(self._base_url, url)`；`target` 必须是 http/https，且
   **scheme + host + port 与 `base_url` 完全一致**，否则 `ImageFetchError("host_not_allowed")`
   且**不发出任何请求**。这条是硬性的：一旦允许「按站点给的 url 带 Cookie 去 GET」，
   任何能让站点返回任意 url 的路径都会变成凭据外泄通道。
2. 同源请求带 Cookie（`_cookie_headers()`）与 `Accept: image/*`，不设 `Origin`/`Referer`
   （§19 红线 3 不变）。
3. 流式累计字节，`total > max_bytes` 立即 `ImageFetchError("too_large")`——
   **不读完整个响应**，与 `_bounded_response_bytes` 同一手法。
4. 非 200 → `ImageFetchError("http", status)`；网络错误/超时 → `ImageFetchError("network")`；
   200 但零字节按 `ImageFetchError("http", 200)` 处理。
5. **401 不重新登录、不重试**：取图失败只是一次降级，不值得为它多一次登录。
6. 本方法**不写任何日志**（URL 不得进日志）。失败原因由调用方以稳定字段记录。
7. 返回原始字节。**不返回 Content-Type**：格式判定以字节嗅探为准，见 §3.3。

### 3.3 `core/vision.py`（新模块）

职责只有三件：取图、判格式、编码。图片字节只在内存里存在。

```python
IMAGE_MIME_ALLOWLIST: tuple[str, ...] = (
    "image/png", "image/jpeg", "image/gif", "image/webp",
)   # 不含 image/svg+xml

def sniff_image_mime(data: bytes) -> str | None
    # PNG : 89 50 4E 47 0D 0A 1A 0A
    # JPEG: FF D8 FF
    # GIF : "GIF8"
    # WEBP: data[0:4] == b"RIFF" and data[8:12] == b"WEBP"
    # 其余（含 SVG、空串、截断字节）→ None

def build_data_url(data: bytes, mime: str) -> str          # f"data:{mime};base64,{...}"
def build_image_part(data_url: str) -> dict[str, Any]      # {"type":"image_url","image_url":{"url":...}}

def attach_image(messages: list[dict[str, Any]], part: dict[str, Any]) -> None
    # 就地改写**最后一条** role=="user" 的 content：
    #   content 是 str  -> [{"type":"text","text":原文本}, part]
    #   content 已是 list -> 追加 part
    #   找不到 user 消息 -> 不做任何事（调用方保证不会发生）

class ImageLoader:
    def __init__(self, client: SiteClient, *, max_bytes: int,
                 logger: logging.Logger | None = None) -> None

    async def load(self, message: ChatMessage) -> tuple[dict[str, Any] | None, str]
        # 返回 (part | None, reason)。reason 取值：
        #   "none"              这条消息没有可用的图（image 为 None 或 image_missing）
        #   "ok"                已取到图并编码为 part
        #   "host_not_allowed"  目标不同源
        #   "too_large"         超过 max_bytes
        #   "http" / "network"  站点错误 / 网络错误
        #   "unsupported_type"  嗅探结果不在白名单（含 SVG、非图片字节）
```

- **不信任 DTO 的 `mime_type`**：data URL 里的 mime 一律来自字节嗅探。站点给的值可能
  过期，而且它一旦被用作 data URL 的前缀，就等于让远端数据决定我们发给模型的内容类型。
- `reason == "none"` 不记日志（那不是失败）。其余失败记一条
  `vision.image_unavailable`，字段只有 `reason` 与 `size_bytes`（`too_large` 时补
  `limit_bytes`）——三者都已在 `LOG_FIELDS` 白名单里，**不新增字段、不记 URL**。
- 级别 INFO：取图失败罕见，且它是运维需要看见的降级信号。

### 3.4 `core/router.py`

- 构造参数新增 `vision_enabled: bool = False`（同时也是选 `HELP_TEXT` 的依据）。
- 第 9.1 步（无正文）改为三分支：

  | 条件 | 结果 | reason | 文案 |
  |------|------|--------|------|
  | `vision_enabled` 且 `has_image(message)` | `queued` | `image_only` | — |
  | 其余且有 `has_media(message)` | `reply_now` | `media_only` | `UNSUPPORTED_MEDIA_TEXT` |
  | 其余 | `reply_now` | `empty` | `USAGE_HINT` |

  新增 `text_utils.has_image(message) -> bool`：`image is not None and not image_missing`。
  `has_media` 保持原义（图片或博客，含已失效的引用）不动——它服务的是
  「有没有东西是我读不了的」这个问题。
- `image_only` 加入 `_ACTIONABLE_REASONS`（它会入队、会消耗一次回复配额，运维要看得见）。
- 第 9.4 步（有图且有正文）**保持不变**：vision 关闭时照旧忽略图片。vision 开启时
  正文照常处理，图片由 worker 附加，路由器不需要在这一步做任何分支。
- `HELP_TEXT` 的选择：`texts.HELP_TEXT_WITH_VISION if self._vision_enabled else texts.HELP_TEXT`。

### 3.5 `app.py`（worker）

装配：`ImageLoader(client=self._client, max_bytes=config.model.max_image_bytes)`；
`MessageRouter(..., vision_enabled=config.model.vision_enabled)`。

`_handle_request` 的插入点（**在模型门之外**，取图由 worker 并发天然限制在
`behavior.concurrency` 之内；取图超时由 `site.request_timeout_seconds` 兜住）：

```
0. 构造时就判死：`vision_enabled` 为假时 worker **完全不碰** `ImageLoader`
   （`self._load_image(request)` 直接返回 `(None, "none")`），因此关闭视觉的进程里
   没有任何新增的站点请求。
1. 代次检查（不变）
2. part, reason = await self._load_image(request)       # (part | None, reason)
3. 纯图且取不到（user_text == "" and part is None and reason != "none"）：
     sender.send(..., IMAGE_UNAVAILABLE_TEXT, kind="notice_local", thread_root_id=...)
     然后 return（不调模型）；finally 里照常 mark_handled
4. pending = _pending_turn(request, reason)            # 见 §4
5. messages = ctx.build_messages(...)                  # 不变
6. _apply_reply_prefix(messages, request)              # 不变，仍是字符串拼接
7. attach_image(messages, part)                        # part 非 None 时执行
8. 模型 → 代次检查 → 发送 → 提交历史（全部不变）
```

顺序是硬性的：`attach_image` 必须排在 `_apply_reply_prefix` **之后**，因为后者按
`messages[index]["content"]` 当字符串拼接。

纯图取不到时用 `notice_local` 而不是 `notice`：它与 `UNSUPPORTED_MEDIA_TEXT`（第 9.1
步的纯媒体提示）是同一类「应答明确用户动作」的本地回复，D-1 的归类不变。用 `notice`
会占用该用户 24 小时的主动通知名额，把一次「图没读到」变成「今天别再提醒他」。

### 3.6 `texts.py`

- 新增 `IMAGE_UNAVAILABLE_TEXT`：「这张图片我没能读取，可能是格式不支持或者体积太大。
  你可以用文字描述一下想问的内容，或者稍后再试。」
- 改写 `UNSUPPORTED_MEDIA_TEXT`：不再笼统说「不能看图」，改为针对仍然读不了的东西
  （博客正文、已失效的图片引用）。vision 关闭时它仍是纯图消息的答复，因此措辞要能
  同时覆盖两种情形。
- `HELP_TEXT` 拆分：`_HELP_HEAD` + 能力句 + `_HELP_TAIL` 三段拼接出两个常量——
  `HELP_TEXT`（能力句声明不能查看图片）与 `HELP_TEXT_WITH_VISION`（能力句声明可以查看
  用户发来的图片，且图片会转交第三方模型处理）。**其余文字只有一份**，避免两份长文案
  各自漂移。两者的「大区共享上下文四点」必须一字不差地保留。
- `COMMENT_HELP_TEXT` 不动（评论侧不支持图片）。

## 4. 本轮 prompt 的形状与历史标记

**一个字符串，两处用途**（沿用 D-7 与 D-22：提交进历史的就是发给模型的那一份）。

| 情形 | pending（发给模型 + 提交历史） | 模型收到的 content |
|------|------------------------------|-------------------|
| 图取到了，有正文 | `[图片]\n---\n正文` | `[{text: pending}, {image_url: data:...}]` |
| 图取到了，无正文（纯图） | `[图片]` | 同上 |
| 取图失败，有正文 | `[图片未提供]\n---\n正文` | 纯字符串 |
| 无图（`reason == "none"`） | 同现状 | 同现状 |

- 大区的发言者包装在**外层**：`speaker_wrapper(username, 带标记的正文)`，即

  ```
  [站点发言者：@alice]
  ---
  [图片]
  ---
  这是什么
  ```

- 与直接引用的顺序：`_apply_reply_prefix` 在最前面拼接，最终是
  「直接引用 → 图片标记 → 正文」，与现有「引用在正文前」的约定一致。
- 历史里留下 `[图片]` / `[图片未提供]` 而不是空串：后续轮次能知道「当时有一张图」，
  比留下一个看起来像幽灵轮的空 user 记录诚实。
- 图片**不会**在后续轮次里再次外送（它不可能是历史的一部分），这正是 D-7 已经确立的
  原则：体积大、只对当下有意义的附件只属于当前轮。

## 5. 失败与降级

| 失败点 | 行为 |
|--------|------|
| `image` 为 None 或 `image_missing` | 等同没有图：纯媒体走 `UNSUPPORTED_MEDIA_TEXT`，有正文照常走文本（现有行为） |
| 不同源 / 超限 / 非 200 / 网络错误 / 格式不支持 | 有正文：降级为纯文本轮 + `[图片未提供]` 标记；纯图：`IMAGE_UNAVAILABLE_TEXT`（`notice_local`），不调模型 |
| 模型拒绝 image 块（400） | 走现有 `ModelError("bad_request")` → `FAILURE_NOTICE_TEXT`。**不**自动重试为纯文本：把配置错误伪装成偶发故障，比一次清晰的失败提示更糟 |
| 模型调用失败 / 发送失败 | 与现在完全一致；历史仍只在送达后提交 |

## 6. 安全边界

- 图片字节只在内存：不落 SQLite、不写日志、不写文件、不进 `ContextManager` 历史。
- Cookie 只发往与 `site.base_url` 完全同源的地址；跨源直接拒绝且不发请求。
- 不把站点 URL 交给模型商回源抓取（也就不会把内网可达的地址变成第三方可探测的目标，
  同时避免了图床鉴权、体积、可用性三件事都由别人决定）。
- data URL 的 mime 来自字节嗅探，不来自 DTO。
- 不转发 SVG。
- 红线 §19 第 5 条改写为：不实现博客理解、工具调用、联网、长期记忆；图片理解仅在
  `model.vision_enabled` 为真时提供，且只把当前轮那一张图取回内存转交模型。

## 7. 成本与配额

- 取图**不消耗**站点限频（§1.3）；但纯图消息从「一条本地提示」变成「一次模型调用 +
  一次回复配额」，这是本次变更唯一新增的用量面。
- 图片 token **不计入** `context_input_tokens` 预算：每条消息最多一张图，且当前轮永不
  被裁剪，因此超支有界。这一点写进契约，不留成隐含行为。
- 不做图片缓存：一条消息只在被处理时取一次；重试与补发会重新下载。
- 运维若不接受上述成本，把 `model.vision_enabled` 设为 `false` 即可完全回到旧行为。

## 8. 测试计划

| 文件 | 覆盖 |
|------|------|
| `tests/test_vision.py`（新） | 四种格式嗅探 + SVG/空/截断字节为 None；data URL 编码；`attach_image` 的 str→parts、已是 list、无 user 消息三种输入；`ImageLoader` 的 none/ok/unsupported/too_large/http/network/host_not_allowed 七个分支；失败日志含 reason 与 size_bytes 且**不含 URL** |
| `tests/test_client.py` | `fetch_image`：相对路径解析、绝对同源、跨源拒绝（断言**没有发出请求**）、流式超限（断言未读完，用会抛错的 transport 证明）、404、网络错误、成功路径（`MockTransport`，不发真实连接） |
| `tests/test_router.py` | vision 开/关 × 纯图/纯博客/空正文/图文四种组合的 action 与 reason；`image_only` 入队；vision 关闭时与现状逐字一致 |
| `tests/test_app.py` | 有图有文时最后一条 user 的 content 是 parts 且文本部分等于 pending；纯图成功；取图失败时 pending 带 `[图片未提供]` 且 content 仍是 str；纯图失败时发 `IMAGE_UNAVAILABLE_TEXT`（kind=notice_local）且**未调用模型**；提交历史的正是带标记的那份；大区标记在发言者包装内部 |
| `tests/test_logging_safety.py` | 整轮带图流程后，捕获的日志与 SQLite 中不含图片 URL、不含 base64 片段 |
| `tests/test_context.py` | **不改**：作为「`ContextManager` 未被本次变更改动」的回归证明 |

测试输出必须干净（`filterwarnings = ["error"]`）。

## 9. 文档更新清单

- `docs/design/INTERFACES.md`：§1（ModelConfig）、§4（`has_image`）、§5（三个文案常量）、
  §7（`fetch_image` / `ImageFetchError`）、§12（第 9.1 步与构造参数）、§14
  （`ModelClient.complete` 的注解放宽为 `list[dict[str, Any]]`）、§16（worker 流程）、
  §19 第 5 条，以及新增一节 `core/vision.py`。
- `docs/design/DESIGN_DECISIONS.md`：新增三条——
  D-28 图片只进当前轮、历史记标记；D-29 自己下载转 data URL、同源、不转发 SVG；
  D-30 取图失败降级为纯文本轮、纯图失败转本地提示且用 `notice_local`。
- `docs/design/SYSTEM_PROMPTS.md`：能力句与「不能看图」的表述。
- `docs/usage/USAGE.md`、`docs/usage/DEPLOYMENT.md`、`config.example.yaml`：新配置项与
  运维含义（默认关闭、开启前提是模型支持视觉）。
- `docs/materials/chat-bot.md`：**不改**（站方文档）。其中 §11.1 的 `image.url` 与源码
  不符一事记在 §1，实现用 `urljoin` 兼容两种形状。
