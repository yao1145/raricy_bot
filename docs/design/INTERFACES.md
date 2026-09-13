# 接口契约（锁定）

本文件是各模块之间**唯一**的接口口径。实现时签名、名称、返回值形状必须与本文件一致；
若认为某处不合理，先在报告里提出，不要自行改动后让下游跟着改。

上游契约见 `docs/materials/chat-bot.md`；设计依据见 `docs/archive/BACKGROUND.md`。
歧义裁决见 `docs/archive/DESIGN_DECISIONS.md`。

## 0. 工程约定

- Python `>=3.12`（开发机为 3.13）。包根目录 `src/raricy_bot/`，包名 `raricy_bot`。
- 运行期依赖仅：`httpx`、`openai`、`PyYAML`、`aiohttp`。SQLite 用标准库 `sqlite3`，
  **不引入 aiosqlite**。测试用 `pytest` + `pytest-asyncio`。
- 全异步（asyncio）。除 `Store` 外不得使用线程。
- 所有对外可见的字符串常量集中在 `texts.py`，不得散落在业务代码里。
- 注释与 docstring 用中文，标识符用英文。
- 日志一律走 `logging_setup.log_event()`，不得直接拼 f-string 写正文内容。

## 1. `config.py`

```python
class ConfigError(Exception): ...

@dataclass(frozen=True)
class SiteConfig:
    base_url: str                     # 去尾斜杠；必须 http/https
    request_timeout_seconds: float = 20.0

@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    temperature: float = 0.4
    timeout_seconds: float = 45.0
    max_output_tokens: int = 600

@dataclass(frozen=True)
class BehaviorConfig:
    context_turns: int = 10
    context_input_tokens: int = 8000
    max_input_chars: int = 8000
    max_output_chars: int = 4000
    concurrency: int = 3
    queue_size: int = 50
    minute_attempt_limit: int = 25
    daily_normal_limit: int = 750
    daily_absolute_limit: int = 790
    notice_cooldown_seconds: int = 300
    reconnect_base_seconds: float = 3.0
    reconnect_max_seconds: float = 60.0
    ready_probe_seconds: float = 300.0
    rate_limit_wait_seconds: float = 60.0

@dataclass(frozen=True)
class StorageConfig:
    db_path: str = "./data/bot.db"
    lobby_thread_retention_seconds: int = 604800     # 共享链映射保留 7 天
    cleanup_interval_seconds: int = 3600             # 运行期清理周期
    send_attempt_retention_seconds: int = 172800     # 发送尝试保留 48 小时
    max_dm_channels: int = 10000                     # 已知私聊频道上限
    sqlite_soft_limit_bytes: int = 134217728         # 128 MiB，软上限（只告警不截断）
    wal_journal_limit_bytes: int = 16777216          # 16 MiB，WAL journal_size_limit

@dataclass(frozen=True)
class OpsConfig:
    host: str = "0.0.0.0"
    port: int = 8080

@dataclass(frozen=True)
class CommentConfig:
    enabled: bool = False
    recent_poll_seconds: int = 30
    notification_poll_seconds: int = 15
    notification_max_pages: int = 5
    queue_size: int = 50
    concurrency: int = 1
    context_turns: int = 10
    context_input_tokens: int = 8000
    article_max_chars: int = 1000
    max_output_chars: int = 1800
    max_response_bytes: int = 8388608       # SiteClient 硬上限 8 MiB
    max_tree_nodes: int = 10000             # 显式栈硬上限 10000
    unmatched_attempt_limit: int = 5
    minute_attempt_limit: int = 20
    daily_reply_limit: int = 600
    daily_absolute_limit: int = 630
    article_cooldown_seconds: int = 5
    conversation_retention_seconds: int = 2592000
    dedupe_retention_seconds: int = 7776000
    retry_base_seconds: int = 5
    retry_max_seconds: int = 300
    server_backoff_seconds: int = 3600

@dataclass(frozen=True)
class Secrets:
    username: str
    password: str
    llm_api_key: str

@dataclass(frozen=True)
class Config:
    site: SiteConfig
    model: ModelConfig
    behavior: BehaviorConfig
    storage: StorageConfig
    ops: OpsConfig
    comments: CommentConfig
    log_level: str
    system_prompt: str
    system_prompt_sha256: str          # 便于日志核对，不含正文
    secrets: Secrets

    @property
    def db_path(self) -> str           # 兼容别名，保留一个版本；新代码用 config.storage.db_path
```

def default_config_path(env: Mapping[str, str] | None = None) -> str
    # BOT_CONFIG_PATH 优先，否则 "./config.yaml"

def load_config(path: str | None = None, env: Mapping[str, str] | None = None) -> Config
```

规则：

- `path` 为 None 时用 `default_config_path(env)`；文件不存在或 YAML 非法 → `ConfigError`。
- **配置文件大小上限 1 MiB**：`MAX_CONFIG_BYTES = 1048576` 是**代码常量**，不是 YAML 里可改的项
  （必须在解析配置之前就能判定）。读取时最多读 `MAX_CONFIG_BYTES + 1` 字节，
  超过 `MAX_CONFIG_BYTES` 即 `ConfigError`；UTF-8 解码失败与 YAML 非法同样映射为 `ConfigError`。
  超限配置必须让进程**在启动阶段失败**，不得部分启动。
- `storage.*` 全部为**正整数**（布尔值不算整数），且满足关系：
  `cleanup_interval_seconds <= lobby_thread_retention_seconds`、
  `send_attempt_retention_seconds >= 86400`、
  `wal_journal_limit_bytes < sqlite_soft_limit_bytes`；任一不满足 → `ConfigError`。
  `storage` 段缺失时全部取默认值。
- 密钥只从环境变量读取：`RARICY_USERNAME`、`RARICY_PASSWORD`、`LLM_API_KEY`；缺失或空 → `ConfigError`。
- YAML 顶层键：`site` / `model` / `behavior` / `ops` / `storage`（`db_path`）/ `logging`（`level`）/
  `comments` / `system_prompt`。`comments.enabled` 默认 `false`；关闭时不创建评论队列、
  poller 或 quota，但 SiteClient 仍接收 `comments.max_response_bytes` 与 `max_tree_nodes`
  默认上限（旧窄客户端替身可省略这两个关键字）。
  未知键**忽略**；缺失键用上表默认值（`site.base_url`、`model.base_url`、`model.model`、`system_prompt` 必填）。
- 校验：`base_url` 必须 http/https 且非空；`0 <= temperature <= 2`；`timeout_seconds > 0`；
  `max_output_tokens >= 1`；所有 `behavior` 整数字段 `>= 1`；`daily_normal_limit < daily_absolute_limit`；
  `minute_attempt_limit >= 1`；`ops.port` 在 1..65535。
- `system_prompt_sha256` = `hashlib.sha256(system_prompt.encode()).hexdigest()[:12]`。
- `Config` 内**不含** Cookie、不含消息正文。`repr(Config)` 不得泄露密钥。

## 2. `logging_setup.py`

```python
LOG_FIELDS: frozenset[str]   # 允许出现在日志里的字段名白名单

def setup_logging(level: str = "INFO") -> None
    # 单行格式：`%(asctime)s %(levelname)s %(name)s %(message)s`，输出到 stderr

def get_logger(component: str) -> logging.Logger      # 返回 logging.getLogger(f"raricy.{component}")

def register_secret(value: str) -> None               # 注册后所有日志里的该串被替换为 "[redacted]"

def log_event(logger, level: int, event: str, **fields) -> None
    # event 是稳定短标识（如 "sse.connect"）；**只**输出白名单字段，其余静默丢弃。
    # 输出形如：`event=sse.connect status=200 event_id=123`
```

`LOG_FIELDS` 至少包含：`event`, `component`, `status`, `error`, `kind`, `reason`,
`event_id`, `message_id`, `channel_id`, `channel_kind`, `count`, `attempt`, `delay`,
`thread_root_id`, `size_bytes`, `limit_bytes`。

清理相关日志的级别：新建链/加入链用 DEBUG；周期清理摘要用 INFO；
清理失败与容量超限用 ERROR（避免大区活跃时刷屏）。
禁止记录用户名、用户 id、消息正文、引用正文、模型正文与数据库路径。

任何级别的日志都不得出现：Cookie、密码、API Key、消息正文、模型请求体。

**必须压制第三方库的日志（否则上面这条守不住）**：把根 logger 设成 `DEBUG` 会连带打开
`openai` 与 `httpx` 的 DEBUG 输出，而 `openai._base_client` 在 DEBUG 下会打印
`Request options: {... 'json_data': {'messages': [...]}}` —— 整份 prompt 与用户正文直接落进
stderr（已实测复现）。因此 `setup_logging()` 必须显式给这些 logger 定级：
`openai`、`openai._base_client`、`httpx`、`httpcore`、`httpcore._trace`、`anyio` 一律
`>= WARNING`。**不要**用「发现敏感串就过滤」的办法补救：SDK 的日志格式随版本变化，
过滤器很容易漏掉嵌套字段。降级后仍要保留可诊断性（错误类别、HTTP 状态、重试次数）。

## 3. `redact.py`

```python
class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None
    def add_secret(self, value: str) -> None       # 空串/None 忽略；已存在忽略
    def redact(self, text: str) -> str             # 逐个替换为 "[redacted]"
    def redact_mapping(self, data: Mapping[str, Any]) -> dict[str, Any]
        # 递归处理 str/list/dict；其余类型原样返回
    @property
    def secrets(self) -> tuple[str, ...]           # 只读，测试用
```

- 替换按密钥长度**降序**进行，避免短密钥先命中破坏长密钥。
- 线程安全（内部用 `threading.Lock`）。
- **哪些值算机密**：只有 `RARICY_PASSWORD`、`LLM_API_KEY`、以及登录拿到的
  `raricy_session` cookie 值。**机器人用户名不是机密，不得注册**。
  注册了会连带把日志和出站文本里机器人自己的名字抹成 `[redacted]`，
  既降低日志可诊断性，也会让模型正常写出 `@机器人名` 时被改写。

## 4. `text_utils.py`

```python
USERNAME_CHARS: frozenset[str]     # 字母、数字、"_"、"-"（与 chat-bot.md §2.1 一致）

def is_username_char(ch: str) -> bool
def contains_bot_mention(content: str, bot_username: str) -> bool
def strip_bot_mention(content: str, bot_username: str) -> str
def estimate_tokens(text: str) -> int
def truncate_at_paragraph(text: str, limit: int) -> tuple[str, bool]
def is_secret_probe(text: str) -> bool
def is_help_command(text: str) -> bool
def is_reset_command(text: str) -> bool
def has_media(message: Any) -> bool     # message.image is not None or message.blog is not None
```

规则（逐条照实现，测试按此断言）：

- `is_username_char`：`ch.isascii() and (ch.isalnum() or ch in "_-")`。
- `contains_bot_mention`：区分大小写。对 `content` 中每一处 `"@" + bot_username` 的出现，
  检查其**左侧紧邻字符**（若是串首则视为合法）和**右侧紧邻字符**（若在串尾则视为合法）：
  两侧都不是 `is_username_char` 时才算命中。任一处命中即返回 True。
  例（bot 名 `mybot`）：`"@mybot 你好"` True；`"@Mybot"` False（大小写）；
  `"@mybotx"` False；`"x@mybot"` False；`"@mybot-2"` False；`"你好@mybot"` True。
- `strip_bot_mention`：删除**所有**命中的 `@bot_username` 片段，再 `strip()`。
- `estimate_tokens`：中日韩字符（`U+4E00..U+9FFF`、`U+3400..U+4DBF`、`U+3040..U+30FF`、
  `U+AC00..U+D7AF`、全角标点 `U+3000..U+303F`、`U+FF00..U+FFEF`）每个计 1 token；
  其余字符按 `ceil(count / 4)` 计。空串返回 0。
- `truncate_at_paragraph(text, limit)`：`len(text) <= limit` 时返回 `(text, False)`。
  否则从 `limit` 处向前找最后一个换行符（`"\n"`）或句末标点（`。！？.!?`）作为切点，
  切点必须 `> limit // 2`，否则退化为硬切 `text[:limit]`。
  返回 `(text[:cut].rstrip() + TRUNCATION_SUFFIX, True)`，`TRUNCATION_SUFFIX` 取自 `texts.py`。
- `is_secret_probe`：命中任一即 True（大小写不敏感，对去空白后的文本匹配）：
  `系统提示`、`system prompt`、`systemprompt`、`提示词`、`你的指令`、`你的设定`、
  `api key`、`apikey`、`密钥`、`口令`、`环境变量`、`env`、`配置文件`、`config`、
  `隐藏配置`、`内部配置`、`cookie`、`token`。
  实现为模块级常量 `SECRET_PROBE_PATTERNS: tuple[str, ...]`，测试直接遍历该常量。
  **不要**加入任何会命中普通寒暄的模式（例如询问机器人名字、打招呼）；
  这一层的目标是挡住索取系统提示与运行密钥的请求，不是审查闲聊。
- `is_help_command` / `is_reset_command`：`text.strip().lower()` 后等于 `"/help"` / `"/reset"`。

## 5. `texts.py`

只放字符串常量，无逻辑。必须至少导出：

```python
TRUNCATION_SUFFIX: str      # 追加在被截断输出末尾的省略提示
HELP_TEXT: str              # 能力、隐私、无联网/图片能力说明
USAGE_HINT: str             # 空白内容或只有 @bot 时的用法提示
UNSUPPORTED_MEDIA_TEXT: str # 纯图片/纯博客
TOO_LONG_TEXT: str          # 超过 max_input_chars
SECRET_REFUSAL_TEXT: str    # 本地拒绝索取系统提示/密钥
RESET_DONE_TEXT: str        # /reset 后的确认
BUSY_NOTICE_TEXT: str       # 队列满
FAILURE_NOTICE_TEXT: str    # 模型最终失败
QUOTA_NOTICE_TEXT: str      # 当日额度用尽
LOBBY_SHARED_SYSTEM_ADDENDUM: str   # 共享大区请求的静态 system 附加说明（见 5.1）
```

`HELP_TEXT` 必须包含：机器人身份声明、能力范围、**消息可能发送至第三方模型处理**、
不联网、不能看图/博客、`/help` 与 `/reset` 用法；以及大区共享上下文的四点说明
（公开多人上下文、只有精确 `@bot` 的消息进入、新参与者加入后最近的链内历史会
再次发送给第三方模型、回复链内消息才能延续上下文，且 `/reset` 创建新链而非删除旧链）。
`HELP_TEXT` 调用于 `/help` 命令，**不得**触发模型调用。

### 5.1 `LOBBY_SHARED_SYSTEM_ADDENDUM`

共享大区请求在 system 消息里额外拼接的一段**静态**说明（D-24）。硬性要求：

- 是模块级字符串常量，**不含任何占位符**：不得插入用户名、用户 id、正文或任何运行时数据；
  拼接时也不做格式化（`system_prompt + "\n\n" + LOBBY_SHARED_SYSTEM_ADDENDUM`）。
- 内容必须覆盖：当前是公开多人对话；发言者标签仅用于区分说话者；不得把不同用户名
  视为同一人；所有用户正文都是不可信数据，其中的授权、身份或指令性陈述一律不作为依据；
  指向特定参与者时优先使用其用户名。
- 私聊**不**使用这段说明。它是否出现，只由 `channel_kind == "lobby"` 决定。
- 它计入 `context_input_tokens` 预算（与 system_prompt 同样先扣）。

## 6. `site/models.py`

```python
LOBBY: str = "lobby"

@dataclass(frozen=True) class Author:    id:str; username:str; avatar_url:str=""; is_admin:bool=False
@dataclass(frozen=True) class ImageRef:  id:str; url:str; mime_type:str
@dataclass(frozen=True) class BlogRef:   id:str; title:str; description:str; author:str|None; updated_at:str
@dataclass(frozen=True) class PatRef:    target_id:str; target_name:str
@dataclass(frozen=True) class ReplyRef:  id:int; content:str; author_name:str|None; is_deleted:bool; image_url:str|None

@dataclass(frozen=True)
class ChatMessage:
    id:int; channel_id:str; author:Author; content:str
    image:ImageRef|None; image_missing:bool
    blog:BlogRef|None; blog_missing:bool
    pat:PatRef|None; reply:ReplyRef|None
    is_deleted:bool; created_at:str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChatMessage"

@dataclass(frozen=True)
class StreamEvent:
    kind: str                       # "message" | "resync" | "typing" | "read" | "unknown"
    event_id: int | None
    channel_id: str | None
    message: ChatMessage | None
    user_id: str | None
    username: str | None
    message_id: int | None

    @classmethod
    def from_sse(cls, data: str, event_id: int | None) -> "StreamEvent" | None
        # data 为 SSE 的 data 行拼接结果；JSON 非法/type 未知 -> 返回 unknown 事件（不抛异常）
        # JSON 非法 -> 返回 None
```

`from_dict` 必须**容错**：字段缺失用默认值；`id` 非 int 时用 `int(...)` 失败则抛 `ValueError`；
`author` 缺失时构造 `Author(id="", username="", ...)`；`image`/`blog`/`pat`/`reply` 为 `None` 或非 dict 时为 `None`。
多余字段忽略。`content` 为 `None` 时取 `""`。

## 7. `site/client.py`

```python
class SiteError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None
    status: int
    message: str            # 已脱敏
    retry_after: float | None

class SiteClient:
    def __init__(self, base_url: str, redactor: Redactor, *, timeout: float = 20.0,
                 transport: httpx.AsyncBaseTransport | None = None,
                 username: str = "", password: str = "") -> None
        # username/password 只供 login() 使用。**不得**出现在日志、异常文案或 __repr__ 中。
        # 装配方（§16 的 BotApp）负责传入 cfg.secrets 里的值。

    async def start(self) -> None            # 创建 httpx.AsyncClient
    async def aclose(self) -> None

    @property
    def self_user(self) -> Author | None     # 登录后可用
    @property
    def logged_in(self) -> bool
    @property
    def session_cookie(self) -> str | None   # 仅测试用；不得写日志

    async def login(self) -> Author
        # POST /api/auth/login {"username","password"}；成功 Set-Cookie raricy_session
        # 单飞：并发调用共享同一次登录（内部 asyncio.Lock + 复检）
        # 失败 -> SiteError(401, ...)；网络错误 -> SiteError(0, ...)
        # 成功后把 cookie 值 register_secret/redactor.add_secret

    async def ensure_session(self) -> Author
        # GET /api/auth/me；401 -> 重新登录一次；仍失败抛 SiteError

    async def fetch_messages(self, channel_id: str, *, limit: int = 50,
                             before: int | None = None, after: int | None = None) -> list[ChatMessage]
        # GET /api/chat/channels/{channel_id}/messages；code != 200 -> SiteError
        # 返回按 id 升序（服务端已保证，客户端再排一次也无妨）

    async def post_message(self, channel_id: str, content: str, *,
                           reply_to: int | None = None) -> ChatMessage | None
        # POST /api/chat/channels/{channel_id}/messages
        # 成功判据**只看 code == 200**；message 为对象时解析为 ChatMessage；
        # 若 code == 200 但 message 是字符串（或信封里找不到消息体）→ 返回 None，
        # 调用方必须容忍（见 §13 第 5 步）。code != 200 → SiteError。
        # 429 -> SiteError(429, ..., retry_after=解析 Retry-After 秒数或 None)
        # 401 -> 重新登录一次并重试一次；再失败抛 SiteError(401, ...)
        # 网络错误/超时 -> SiteError(0, ...)   ← 调用方据此判定「结果不确定」

    async def probe_chat(self) -> None
        # GET lobby messages?limit=1；用于探测 403 禁言/权限是否恢复；异常抛 SiteError

    @asynccontextmanager
    async def open_stream(self, last_event_id: int | None = None) -> AsyncIterator[httpx.Response]
        # GET /api/chat/stream，Accept: text/event-stream
        # 带 Cookie；last_event_id 非 None 时带 Last-Event-ID
        # **不设置** Origin / Referer
        # 401 -> 重新登录一次后重试一次
        # 非 200 -> SiteError
        # **读超时例外**：SSE 是长连接，绝不能沿用 20 秒的读超时，否则安静站点会每 20 秒
        # 空转重连一次。流请求用
        #   httpx.Timeout(connect=timeout, read=STREAM_READ_TIMEOUT_SECONDS,
        #                 write=timeout, pool=timeout)
        # 其中 STREAM_READ_TIMEOUT_SECONDS 是 client.py 的模块常量，默认 300.0：
        # 半开连接最多 5 分钟被发现，同时不会因为"没人说话"而反复重连。
```

通用要求：

- 所有请求 `Cache-Control: no-store` 由服务端给出，客户端不做缓存。
- 所有响应体解析走统一信封：`code` 缺失或非 int → `SiteError(status, "malformed envelope")`。
- 错误 `message` 出站前先过 `redactor.redact()`。
- **不得**开启 httpx 的 debug/body 日志（不注册 event hook 打印 body）。
- `timeout` 作用于所有站点请求，**唯一例外是 SSE 长连接的读超时**（见 `open_stream`）。
- 站点响应 `code` 与 HTTP 状态可能同时可用：以响应体 `code` 为准，HTTP 状态作为兜底。

## 8. `site/sse.py`

```python
class SSEReceiver:
    def __init__(self, client: SiteClient, handler: Callable[[StreamEvent], Awaitable[None]],
                 *, base_delay: float = 3.0, max_delay: float = 60.0,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 random: Callable[[], float] | None = None) -> None

    @property
    def connected(self) -> bool
    @property
    def last_event_id(self) -> int | None
    def set_last_event_id(self, value: int | None) -> None    # resync/水位推进后由 app 设置

    async def run(self) -> None      # 永不返回，直到被 cancel；内部自愈重连
    async def stop(self) -> None
```

行为：

- 帧解析遵循 SSE：空行分帧；`:` 开头为注释；`retry: <ms>` 记为基础重连间隔（与 `base_delay` 取较大值）；
  `id: <int>` 记为该帧事件 id；多行 `data:` 以 `\n` 拼接。无法解析为 int 的 `id:` 忽略。
- 每个 `message` 帧 → `StreamEvent.from_sse(data, event_id)` → `await handler(event)`。
  **handler 必须快速返回**；接收器不得在 handler 内做模型调用。
- `handler` 抛异常时**记日志后继续处理后续帧**，既不得让异常终止 `run()`，
  **也不得**把它当成断线去重连。
  理由是重连会把同一条消息再投一次（该事件没被标记完成，`Last-Event-ID` 水位没推进），
  于是 handler 再抛一次 —— 一条毒消息就能把机器人钉死在「重连 → 同一条消息 → 再重连」
  的循环里。断线重连只由**传输层**错误触发。
- 重连退避：`delay = min(max_delay, base * 2 ** attempt)`，再乘 `1 + jitter`，
  jitter 为 `random()` 给出的 `[0, 0.2)` 区间；成功收到任一帧后 `attempt` 归零。
- 重连时带 `last_event_id`（若已知）。
- `stop()` 后 `run()` 退出。

## 9. `store.py`

标准库 `sqlite3`，全部方法 `async`，内部用 `asyncio.to_thread` + 单连接 +
`asyncio.Lock` 串行化。`PRAGMA journal_mode=WAL`、`PRAGMA synchronous=NORMAL`。
**不得**存储消息正文、模型输入输出、Cookie、密码。

```python
class Store:
    def __init__(self, path: str, *, wal_journal_limit_bytes: int = 16777216) -> None
    async def open(self) -> None      # 建表；path 为 ":memory:" 时可用（测试）
    async def close(self) -> None

    # --- 事件与水位（去重主键是 message_id，不是 event_id）---
    async def record_event(self, event_id: int | None, message_id: int, channel_id: str) -> bool
        # INSERT OR IGNORE；返回 True = 首次记录，False = message_id 已存在（重复投递）
        # event_id 为 None 表示该消息来自 resync 拉取，没有 SSE 事件 id

    async def mark_orphans_recoverable(self) -> int
        # 崩溃恢复第一步：把**所有** status='pending' 的行改成 'recover'，返回改动行数。
        # `BotApp.start()` 在 `Store.open()` 之后、SSE 启动**之前**调用一次。
        # 依据：此刻本进程还没开始处理任何事件，所以任何非终态行必定属于已经死掉的旧进程。
        # 'recover' 与 'pending' 一样是非终态（照旧压住水位），区别只在于
        # 「可以被重新认领」。

    async def reclaim_orphan(self, message_id: int) -> bool
        # 原子地把一条 'recover' 行重新认领为 'pending'；返回 True 表示本次认领成功。
        # 实现必须是 `UPDATE ... SET status='pending' WHERE message_id=? AND status='recover'`
        # 并靠 rowcount 判定 —— 保证同一孤儿事件即使被并发投递也只有一个认领成功。
        # 'pending'（本进程已入队）与 'done'/'skipped' 都不可认领。
    async def mark_handled(self, message_id: int, status: str) -> None   # "done" | "skipped"
    async def message_status(self, message_id: int) -> str | None
    async def is_handled(self, message_id: int) -> bool     # status in ("done", "skipped")
    async def advance_watermark(self) -> int
        # 把「安全水位检查点」单调推进，返回新值（在同一事务内更新 runtime_meta）。
        # 算法（完整口径见 9.2）：读检查点 C（缺失为 0）→ 只看 event_id > C 的行 →
        # 有非终态行取 min(那些 event_id) - 1，否则取这些行中最大的 event_id（没有这类行则取 C）
        # → 新值 = max(C, 候选值)，**绝不下降**。resync 行（event_id 为 NULL）不参与。
    async def watermark(self) -> int
        # 只读，不推进；返回 max(检查点, 按同一规则在现场算出的值)。
        # 启动时用它给 SSE 播 Last-Event-ID。
    async def pending_messages(self) -> list[tuple[int | None, int, str]]   # (event_id, message_id, channel_id)

    # --- 私聊频道 ---
    async def upsert_dm_channel(self, channel_id: str, last_message_id: int) -> None
    async def dm_channels(self) -> list[str]

    # --- 大区共享链 ---
    async def resolve_lobby_thread(self, message_id: int, reply_to: int | None, *,
                                   force_new: bool, now: float,
                                   retention_seconds: int) -> int
        # 原子地命中活动链或新建链，并登记 message_id，返回 thread_root_id。
        # 五条分支（同一事务内判定，见 9.3）：force_new / 无 reply_to / 目标未知 /
        # 目标链已过期 / 命中活动链。新建时 root = message_id。
    async def find_active_lobby_thread(self, message_id: int, *, now: float,
                                       retention_seconds: int) -> int | None
        # 只读；message_id 属于活动链则返回其根，否则 None（过期链等同未命中）。
    async def attach_lobby_message(self, message_id: int, thread_root_id: int, *,
                                   now: float) -> bool
        # 登记出站或回显消息并刷新链的 updated_at；根不存在时返回 False。
        # 幂等：同一 message_id 重复登记不报错。

    async def prune_runtime_state(self, *, now: float, cfg: StorageConfig) -> CleanupResult
        # 在一个存储锁内完成：安全水位推进 + 各表清理 + 容量统计。规则见 9.4。

    # --- 已发回复 ---
    async def record_sent(self, message_id: int, channel_id: str, reply_to: int | None, *,
                          thread_root_id: int | None = None) -> None
        # 同一事务内：写 sent_replies；thread_root_id 非空时同时写 lobby_thread_messages
        # 并刷新 lobby_threads.updated_at。DM 传 None。
    async def find_sent_for_reply(self, channel_id: str, reply_to: int) -> int | None

    # --- 发送尝试与配额 ---
    async def record_send_attempt(self, channel_id: str, reply_to: int | None, kind: str) -> int
        # kind: "reply" | "notice" | "notice_local"；返回自增主键
    async def count_sends_since(self, since: float, kind: str | None = None) -> int

    # --- 冷却 ---
    async def get_cooldown(self, key: str, *, now: float | None = None) -> float | None
        # 未过期返回到期时间戳，否则 None；now 省略时用真实时间，
        # 传入时与调用方（quota 用可注入时钟）同轴比较
    async def set_cooldown(self, key: str, until: float) -> None
```

### 9.1 表结构

字段类型自定，语义必须一致：

- `events(event_id INTEGER, message_id INTEGER PRIMARY KEY, channel_id TEXT, status TEXT, received_at REAL)`
  —— `message_id` 是去重主键（设计文档 §3.2）；`event_id` 可空，供水位计算；两条索引：
  `(status)`、`(event_id)`。
- `dm_channels(channel_id TEXT PRIMARY KEY, last_message_id INTEGER, updated_at REAL)`
- `sent_replies(message_id INTEGER PRIMARY KEY, channel_id TEXT, reply_to INTEGER, sent_at REAL)`
- `send_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT, reply_to INTEGER, kind TEXT, attempted_at REAL)`
- `cooldowns(key TEXT PRIMARY KEY, until REAL)`
- `lobby_threads(thread_root_id INTEGER PRIMARY KEY, created_at REAL, updated_at REAL)`
  —— 索引 `(updated_at)`。`thread_root_id` 就是开启该链那条消息的全局消息 id。
- `lobby_thread_messages(message_id INTEGER PRIMARY KEY, thread_root_id INTEGER NOT NULL,
  mapped_at REAL, FOREIGN KEY(thread_root_id) REFERENCES lobby_threads(thread_root_id) ON DELETE CASCADE)`
  —— 索引 `(thread_root_id)`。
- `runtime_meta(key TEXT PRIMARY KEY, int_value INTEGER NOT NULL)` —— 目前只存 `safe_event_watermark`。

**新表禁止保存**：`author.id`、用户名、参与者名单、任何正文（消息、引用、模型输入输出）、
system prompt、Cookie、密码、API Key。链归属只需要消息 id 与根 id；
当前发言者一律从实时 `ChatMessage` DTO 取，不落库。

时间一律用 `time.time()` 的 epoch 秒（REAL）。

### 9.2 安全水位检查点

`runtime_meta['safe_event_watermark']` 是唯一可以被清理**改写**的水位依据，
它必须单调不减（D-23）：

1. 读检查点 `C`（缺失时为 0）；
2. 只看 `event_id > C` 的行（`event_id` 为 NULL 的 resync 行不参与）；
3. 这些行里存在非终态（`pending` / `recover`）时，候选值 = 最小非终态 `event_id - 1`；
4. 否则候选值 = 这些行中最大的 `event_id`；若这类行为空，候选值 = `C`；
5. 新水位 = `max(C, 候选值)`，**绝不下降**；
6. `advance_watermark()` 在同一事务内更新检查点；`watermark()` 只读，返回
   `max(检查点, 现场按同一规则算出的值)`。

**回滚锚点**：清理后必须保留**至少一条**具有最大安全 `event_id` 的真实终态事件行，
且安全水位之后的终态事件一律不得提前删除。这样回滚到不认识 `runtime_meta` 的旧版本时，
旧算法仍能从事件表算出不倒退（或至少不明显倒退）的水位。

### 9.3 线程解析语义

`resolve_lobby_thread` 必须在**同一个 SQLite 事务内**完成「检查目标活动时间、
创建或命中线程、登记当前消息、刷新 `updated_at`」——否则两个并发回复可能拿到不同根。

分支（顺序固定）：

| 情况 | 结果 |
|------|------|
| `force_new=True`（大区 `/reset`） | 无条件以 `message_id` 新建链 |
| `reply_to is None` | 以 `message_id` 新建链 |
| `reply_to` 无映射 | 以 `message_id` 新建链 |
| `reply_to` 的链 `updated_at <= now - retention_seconds` | 视为过期，以 `message_id` 新建链 |
| `reply_to` 的链活动 | 命中该根，并把当前消息登记进去 |

活动判据是**开区间**：`updated_at > now - retention_seconds` 为活动，
`== now - retention_seconds` 即过期（测试按此固定）。全部使用**调用方注入的 `now`**。
`Store` 是过期判断的唯一权威时钟，`ContextManager` 不另设 TTL。

### 9.4 清理规则

`prune_runtime_state` 启动时调一次，运行期每 `cleanup_interval_seconds` 调一次。

| 数据 | 保留规则 |
|------|----------|
| `lobby_threads` 与映射 | `updated_at` 超过 `lobby_thread_retention_seconds` 删除，映射级联删除 |
| `events` 终态行 | 超过 7 天**且** `event_id <= safe_event_watermark` 时删除，但保留 9.2 的回滚锚点 |
| `events` 非终态行 | **永不**按时间删除 |
| `sent_replies` | 超过 7 天删除；其 `reply_to` 对应非终态事件时保留 |
| `send_attempts` | 保留 `send_attempt_retention_seconds`（默认 48 小时） |
| `cooldowns` | `until <= now` 时删除 |
| `dm_channels` | 不按时间删除，只保留 `updated_at` 最新的 `max_dm_channels` 行；并列时以 `updated_at DESC, channel_id DESC` 保证确定性 |

返回值：

```python
@dataclass(frozen=True)
class CleanupResult:
    expired_thread_roots: tuple[int, ...]   # app 用它失效对应内存上下文
    deleted_events: int
    deleted_sent_replies: int
    deleted_send_attempts: int
    deleted_cooldowns: int
    deleted_dm_channels: int
    safe_event_watermark: int
    db_logical_bytes: int                   # page_count * page_size 减 freelist
    db_physical_bytes: int
```

清理失败**不得**终止机器人，也不得删除额外数据：整个事务回滚，记一条限字段的 error，
等下一周期重试。禁止以「满足大小上限」为由删除非终态事件、最近发送记录或水位锚点。

清理成功后执行 `PRAGMA wal_checkpoint(PASSIVE)`，并设置 `PRAGMA journal_size_limit`；
运行期**不执行 `VACUUM`**（它会长时间独占数据库锁）。

### 9.5 容量

- SQLite 主库用 `sqlite_soft_limit_bytes` 做**软上限**：清理后计算逻辑/物理大小，
  超过时每周期最多记一条 error。**不用** `max_page_count` 硬截断 ——
  硬上限会让去重、水位或配额写入突然失败，可能造成重复回复或消息丢失。
- SQLite 不使用内存数据库以外的特殊路径；`":memory:"` 在测试中必须继续可用。

## 10. `quota.py`

```python
class Decision(enum.Enum):
    ALLOW = "allow"
    DENY_DAILY = "daily"        # 已达 daily_normal_limit / daily_absolute_limit
    DENY_NOTICE = "notice"      # 该频道通知冷却未过或已达每频道上限
    DENY_MINUTE = "minute"      # 每分钟令牌桶耗尽
    DENY_BACKOFF = "backoff"    # 站点 429 退避中

@dataclass(frozen=True)
class QuotaResult:
    decision: Decision
    retry_after: float | None = None
    @property
    def allowed(self) -> bool

class QuotaGuard:
    def __init__(self, store: Store, cfg: BehaviorConfig, *,
                 now: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None

    async def reserve(self, channel_id: str, kind: str, *,
                      actor_id: str | None = None) -> QuotaResult
        # kind 是三值枚举（不再有 channel_cap 参数，行为由 kind 决定）：
        #   "reply"        模型回复
        #   "notice"       **主动**通知（busy / failure / quota），
        #                  受 `notice_cooldown_seconds` 冷却约束，冷却按 (频道, actor_id) 隔离（D-18）
        #   "notice_local" 应答明确用户动作的本地回复（/help、/reset、用法提示、
        #                  纯媒体提示、超长提示、拒绝索取密钥），**不**受通知冷却约束
        # 三种 kind 全部计入 24 小时总量（790 带），见 D-1。
        # actor_id 是触发这条通知的用户，只有 "notice" 用得上。
        # **原子**：内部 asyncio.Lock 串行，把「在途预留」与 SQLite 中的历史合并计数，
        # 避免并发超发。允许时登记一笔预留，调用方**必须**最终调用 note_sent() 或 release()。

    async def note_sent(self, channel_id: str, reply_to: int | None, kind: str, *,
                        actor_id: str | None = None) -> None
        # 预留转正：写 send_attempts 一行、释放预留；kind == "notice" 时
        # 额外落下 `notice_cooldown_key(频道, actor_id)` 冷却（唯一写点，只在送达后落）。

    async def release(self, channel_id: str, kind: str, *, actor_id: str | None = None) -> None
        # 发送失败/放弃：释放预留，不写 send_attempts，也不落通知冷却。

def notice_cooldown_key(channel_id: str, actor_id: str | None) -> str
    # 主动通知的冷却键 `notice:{channel_id}:{actor_id}`；actor_id 为空时退回频道级。

    def backoff(self, seconds: float) -> None       # 站点 429 后调用
    @property
    def backoff_remaining(self) -> float
```

判定规则（`docs/archive/DESIGN_DECISIONS.md` D-1/D-2 为准）：

- 每分钟：`minute_attempt_limit` 次滑动窗口（默认 25），窗口内 `已在途预留数 + 已发送数`
  达到上限 → `DENY_MINUTE`。
- 24 小时滚动总量 = `count_sends_since(now - 86400)`（**三种 kind 全部计入**）。
  - 总量 `>= daily_absolute_limit` → 一律 `DENY_DAILY`（完全静默）。
  - `kind == "reply"` 且总量 `>= daily_normal_limit` → `DENY_DAILY`。
  - `kind == "notice"` 时**额外**要求：`(channel_id, actor_id)` 没有生效中的通知冷却
    （键 `notice_cooldown_key(channel_id, actor_id)`，时长 `notice_cooldown_seconds`）
    且没有在途的同键预留，否则 `DENY_NOTICE`。
    **没有「每频道 24 小时一条」的总量名额**：大区是全局唯一频道，按频道计会让
    全站每天只有一个人收得到主动提示（那是已经修掉的缺陷，见 D-18）。
    按 (频道, 触发者) 计，等于「每个人每 24 小时最多一条」在私聊里的原义，
    在大区里则逐人独立。
    **冷却只由 `"notice"` 落、也只看 `"notice"`，绝不要把 `"notice_local"` 算进来。**
    若把本地回复计入，大区里任何人一次 `/help`、或一条「只 @ 了机器人没说话」的
    用法提示，就会压住其余人的主动通知（忙碌/失败/额度）。
    `kind == "notice_local"` 不受通知冷却约束（仍受总量与分钟窗口约束）。
- `backoff_remaining > 0` → `DENY_BACKOFF`（任何 kind）。

## 11. `core/context.py`

```python
def dm_session_key(channel_id: str) -> str              # f"dm:{channel_id}"
def lobby_thread_session_key(thread_root_id: int) -> str  # f"lobby-thread:{thread_root_id}"

def speaker_wrapper(username: str, text: str) -> str
    # 大区发言者包装，形状固定为：
    #   [站点发言者：@<username>]
    #   ---
    #   <text>
    # username 中的换行等控制字符替换为空格后使用；**不**做其它转义，
    # **不**记录、**不**持久化用户名。返回值整体作为 role="user" 的内容。

@dataclass(frozen=True)
class Turn:
    role: str        # "user" | "assistant"
    content: str

class ContextManager:
    def __init__(self, max_turns: int, max_input_tokens: int) -> None

    def append_exchange(self, session_key: str, user: str, assistant: str) -> None
        # **唯一的历史写入方式**（D-22）：一次性提交一组完整的 (user, assistant)。
        # 只有「模型成功且回复已送达」的轮次才允许调用（见 16 的提交时机）。
    def reset(self, session_key: str) -> bool        # 清空历史并**递增代次**；返回原会话是否存在
    def invalidate(self, session_key: str) -> None   # 同上但不返回是否存在；线程过期时用它
    def generation(self, session_key: str) -> int    # 当前代次；不存在的会话返回 0
    def build_messages(self, session_key: str, system_prompt: str, *,
                       pending_user: str | None = None,
                       system_addendum: str | None = None) -> list[dict[str, str]]
        # [{"role":"system",...}] + 裁剪后的历史 [+ 末尾一条未提交的 role="user"]
    def session_count(self) -> int
    def turn_count(self, session_key: str) -> int    # 已提交的记录条数（一轮 = 2 条）
```

- 历史**只存内存**，进程重启即丢失；`session_key` 只由上面两个构造函数产生。
- **完整轮次提交**：历史里永远只出现成对的 (user, assistant)。
  `append_exchange` 一次写入两条，因此不存在「只有 user 没有 assistant」的幽灵轮次。
  模型失败、发送失败、额度拒绝、被代次检查拦下的请求一律**不写**历史。
- `max_turns` 指最多保留最近 N 组已提交轮次，超出时从最旧**整对**丢弃。
- `pending_user` 是当前这一轮尚未提交的用户内容（DM 是正文，大区是发言者包装后的正文）。
  它只出现在返回的 messages 末尾，不进历史；`assistant` 回复送达后才由调用方
  `append_exchange` 提交。
- `build_messages` 按 `max_input_tokens` 裁剪：从最旧整对丢弃，直到
  `estimate_tokens(system_prompt) + estimate_tokens(system_addendum)
   + sum(estimate_tokens(t.content)) + estimate_tokens(pending_user) <= max_input_tokens`；
  **至少保留 `pending_user` 这一轮**（即使超限）。
- system 消息只有一条：`system_prompt` + （`system_addendum` 非空时）`"\n\n" + system_addendum`。
  用户内容**绝不**拼进 system 内容。
- `reset` 与 `invalidate` 都会清空历史并**递增代次**（即使会话原本不存在），
  用来作废在途请求；区别只是 `reset` 返回原会话是否存在。
  线程过期走 `invalidate`，DM `/reset` 走 `reset`。
- 同一 `session_key` 的并发访问由调用方保证串行（worker 已保证）。

**代次（generation）—— 用来隔离 `/reset` 与在途模型请求的竞态。**
每个 `session_key` 维护一个从 0 开始的整数代次，`reset()` **即使会话原本不存在也要递增**。
这样做的原因：一次模型调用可能要跑几十秒，而 `/reset` 可能在它返回之前到达。
若不隔离，旧请求返回后会往刚刚清空的会话里 `append_assistant()`，
于是「清空」之后的历史里多出一条谁也没见过的 assistant 轮，并在**下一轮**被再次外送。
代次让「reset 之前创建的请求」与「reset 之后的会话」可以被区分开。

## 12. `core/router.py`

```python
@dataclass(frozen=True)
class Request:
    event_id: int | None     # resync 路径为 None
    channel_id: str
    channel_kind: str        # "lobby" | "dm"
    session_key: str         # 大区 lobby-thread:<root>；私聊 dm:<channel_id>
    generation: int          # 创建时的会话代次；worker 用它判断请求是否已被 /reset 作废
    thread_root_id: int | None  # 大区共享链根；**私聊恒为 None**
    message: ChatMessage
    user_text: str           # 已剔除 @机器人 的正文
    reply_context: str | None  # message.reply 非删除时的正文，否则 None

@dataclass(frozen=True)
class RouteResult:
    action: str              # "queued" | "reply_now" | "ignored" | "busy" | "resync"
    channel_id: str | None
    message_id: int | None
    reply_to: int | None
    text: str | None         # reply_now / busy 时的本地文案
    request: Request | None
    reason: str              # 稳定短标识，用于日志，如 "self_message"
    actor_id: str | None = None      # 触发者（busy 时供 app 计通知冷却，见 D-18）
    thread_root_id: int | None = None  # 同 Request；DM 恒为 None

class MessageRouter:
    def __init__(self, *, self_user_id: str, bot_username: str,
                 ctx: ContextManager, store: Store,
                 queue: asyncio.Queue[Request], cfg: BehaviorConfig,
                 storage: StorageConfig, now: Callable[[], float] = time.time) -> None

    async def handle_stream(self, event: StreamEvent) -> RouteResult
    async def handle_message(self, channel_id: str, message: ChatMessage,
                             event_id: int | None) -> RouteResult
```

`handle_stream` 判定顺序（严格照此，`reason` 用括号内标识）：

1. `event.kind == "typing"` / `"read"` → `ignored`（`typing` / `read`）。
2. `event.kind == "resync"` → `resync`。
3. `event.kind != "message"` → `ignored`（`unknown_event`）。
4. `message` 为 None → `ignored`（`malformed`）。
5. 其余 → 委托 `handle_message(event.channel_id or message.channel_id, event.message, event.event_id)`。

`handle_message` 判定顺序（**`record_event` 只在通过候选过滤之后调用**，见 D-15）：

1. 非 lobby 频道 → `await store.upsert_dm_channel(channel_id, message.id)`。
2. **大区自身回显补登记**（`channel_id == LOBBY` 且 `author.id == self_user_id`）：
   若 `message.reply` 存在且未删除，用 `store.find_active_lobby_thread(message.reply.id, ...)`
   查活动链，命中则 `store.attach_lobby_message(message.id, root, ...)`。
   **无论命中与否**都继续按 `ignored`（`self_message`）结束：不写事件表、不入模型、不发送。
   补登记失败只记一条无正文错误日志，绝不因此产生回复或循环。私聊的自身消息直接 `ignored`。
3. `is_deleted` → `ignored`（`deleted`）。
4. `pat is not None` → `ignored`（`pat`）。
5. 频道判定：
   - lobby：`contains_bot_mention(content, bot_username)` 为 False → `ignored`（`no_mention`）；
     否则 `user_text = strip_bot_mention(...)`、`channel_kind="lobby"`。
     **此时还不知道 session_key**，它由第 6-7 步解析出的链决定。
   - 其他（私聊）：`user_text = strip_bot_mention(content, bot_username)`、
     `session_key = dm_session_key(channel_id)`、`thread_root_id = None`、`channel_kind="dm"`。
     私聊**也**剔除 `@机器人`（见 D-6）：用户在私聊里同样会习惯性带上 @，那不是正文的一部分；
     只有 `@bot` 一条消息时 `user_text` 为空，自然落到第 9 步的用法提示。
6. 认领事件（**设计文档 §3.2 的主去重键拦截，必须排在所有副作用之前**）：
   ```
   if await store.record_event(event_id, message.id, channel_id):
       pass                                   # 新事件，继续
   elif await store.reclaim_orphan(message.id):
       # 上一进程崩溃时留下的未完成事件（启动时已被 mark_orphans_recoverable 标记）。
       # 先确认当时是不是其实已经回复过了 —— 若已回复，补标记完成即可，绝不重复回复。
       if await store.find_sent_for_reply(channel_id, message.id) is not None:
           await store.mark_handled(message.id, "done")
           → ignored（schema 里的 `recovered_sent`）
   else:
       → ignored（`duplicate`）
   ```
   `reclaim_orphan` 靠 `UPDATE ... WHERE status='recover'` 的 rowcount 保证原子性，
   因此即使孤儿事件被 SSE 重放与 resync 同时投递，也只有一方能认领成功。
   第二个分支是崩溃恢复能真正跑通的关键：没有它，被重放的孤儿事件会被当成
   `duplicate` 丢掉，消息永远不处理、水位永远卡住（已实测复现）。
7. `reply_context`：`message.reply` 存在且 `not is_deleted` 时取 `reply.content`，否则 `None`。
8. **大区解析共享链**（私聊跳过本步）：
   ```
   force_new = is_reset_command(user_text)
   root = await store.resolve_lobby_thread(
       message.id, reply_id, force_new=force_new,
       now=now(), retention_seconds=storage.lobby_thread_retention_seconds)
   session_key = lobby_thread_session_key(root)
   ```
   `reply_id` 取未被删除的 `message.reply.id`，否则 `None`。
   **写失败**：记一条无正文 error、`ignored`（`thread_resolve_failed`），
   不调模型、不发消息，并且**不** `mark_handled` —— 该事件保持非终态，
   靠重连补发或下次重启恢复（与 D-16 的恢复机制一致）。
9. 本地判定（全部沿用现有顺序，文案见 §5）：
   - `user_text` 为空：有媒体 → `reply_now`（`media_only`）；无媒体 → `reply_now`（`empty`）。
   - `is_help_command` → `reply_now`（`help`），文案 `HELP_TEXT`。
   - `is_reset_command`：
     - **大区**：**不**调用 `ctx.reset`（原链不受影响，见 D-21），直接 `reply_now`（`reset`），
       文案 `RESET_DONE_TEXT`。第 8 步已经用 `force_new=True` 把本条命令建成了新链。
     - **私聊**：`ctx.reset(session_key)`（清空并递增代次）→ `reply_now`（`reset`）。
   - 有媒体且 `user_text` 非空 → 照常处理文本（媒体忽略），继续往下。
   - `len(user_text) > cfg.max_input_chars` → `reply_now`（`too_long`），文案 `TOO_LONG_TEXT`。
   - `is_secret_probe(user_text)` → `reply_now`（`secret_probe`），文案 `SECRET_REFUSAL_TEXT`。
10. 构造 `Request` 并入队：成功 → `queued`；`asyncio.QueueFull` → `busy`
    （文案 `BUSY_NOTICE_TEXT`，是否真发由 app 按配额与冷却决定，见 D-3）。

`queued` / `busy` / `reply_now` 都必须带上第 8 步解析出的 `thread_root_id`，
app 据此在发送成功后登记出站消息（DM 为 `None`）。

标记时机（**别弄反**）：

- 第 1-5 步返回的 `ignored` 没有记录过事件，**不需要** `mark_handled`。
- 第 6 步的 `duplicate` 也不标记（那行属于首次投递）。
- 第 8 步解析失败时**不标记**（见上）。
- 第 8 步之后：`reply_now` / `busy` 由 **app** 在发送尝试结束后
  `await store.mark_handled(message.id, "done")`；`queued` 由 **worker** 处理完后同样标记。

`reply_to` 一律为 `message.id`。`RouteResult.channel_id` 与 `message_id` 在所有分支都要回填
（`ignored` 也填，便于日志），确实无法确定时为 None。

**大区语义**：会话键是 `lobby-thread:<thread_root_id>`，同一条公开回复链上的所有人共享
同一份上下文；不同链、不同用户之间互不可见。判据只有站点消息 id 的映射，
不看引用正文、不看用户名、不由模型决定。私聊仍按 `channel_id` 隔离。
resync 路径由 app 直接调用 `handle_message(channel_id, message, None)`，
与实时流共用同一套判定与去重；resync 可能乱序补多条消息，第 8 步的解析是幂等的，
但**不能假设按 id 升序到达**——同时到达的两条消息由 `resolve_lobby_thread` 的事务原子决定根。

## 13. `core/sender.py`

```python
@dataclass(frozen=True)
class SendResult:
    delivered: bool
    message_id: int | None
    reason: str      # "delivered" | "deduped" | "quota" | "minute" | "backoff"
                     # | "forbidden" | "reply_target_gone" | "failed"

class MessageSender:
    def __init__(self, *, client: SiteClient, store: Store, quota: QuotaGuard,
                 redactor: Redactor, cfg: BehaviorConfig, logger=None) -> None

    async def send(self, channel_id: str, text: str, reply_to: int | None, *,
                   kind: str = "reply", actor_id: str | None = None,
                   thread_root_id: int | None = None) -> SendResult
        # kind 与 actor_id 直接透传给 quota.reserve，三值同 §10：
        #   "reply"（模型回复）/ "notice"（主动通知，按 (频道, 触发者) 冷却）/
        #   "notice_local"（应答用户动作）
        # actor_id 是触发这条消息的用户，只有 "notice" 用得上（D-18）。
        # thread_root_id 是大区共享链的根；非空时随 record_sent 一起写入映射，
        # 使这条出站消息成为后续加入该链的锚点。DM 恒为 None。
```

流程：

1. `text = redactor.redact(text)`；再 `truncate_at_paragraph(text, cfg.max_output_chars)`。
   空文本 → `SendResult(False, None, "failed")`。
2. `quota.reserve(channel_id, kind)`；不允许 → 返回对应 `reason`（`quota`/`minute`/`backoff`）。
3. `store.record_send_attempt(...)` 只在**成功或确定失败后**由 `quota.note_sent`/`release` 处理；
   发送前不写。
4. `await client.ensure_session()`。
5. `POST`：
   - 成功（`code == 200`）：`quota.note_sent`；若返回了消息体则
     `store.record_sent(msg.id, channel_id, reply_to, thread_root_id=thread_root_id)`，
     返回 `delivered`。消息体为 None 时跳过 `record_sent`（去重记录缺失是可接受的降级），
     `message_id` 记 None，仍返回 `delivered`；该出站消息的链映射改由**自身 SSE 回显**补登记
     （路由器第 2 步），因此 sender 不为此重发、不改判失败。
     `thread_root_id` 同时用于第 6 步对账的两条命中路径（本地命中与远端命中），
     保证「发送其实成功了、只是本地没记账」时映射仍会被补上。
   - `SiteError.status == 429`：`quota.backoff(retry_after or cfg.rate_limit_wait_seconds)`，
     `quota.release`，返回 `backoff`。
   - `SiteError.status == 403`：`quota.release`，**不重试**，并按 `SiteError.message` 细分成两种：
     - 含 `跨源` 或 `CSRF` → 返回 `csrf`。这是**客户端配置错误**（说明我们错误地设置了
       `Origin`/`Referer`），属于程序缺陷：既不该重试，也**不该**进「不可用 / 探测」状态，
       否则会用一个永远好不了的错误把机器人钉在不可就绪上、每 5 分钟白探一次，
       把真正的病因（我们发错了头）掩盖成一个看起来像权限问题的假象。
     - 其余（`需要核心用户权限` / `你已被禁言，暂时无法聊天`）→ 返回 `forbidden`，
       由 app 进入不可用状态并按 `ready_probe_seconds` 用 `probe_chat()` 探测（D-4）。
   - `SiteError.status == 400` 且 `reply_to is not None`：`quota.release`，返回 `reply_target_gone`。
   - `SiteError.status == 0`（网络/超时，**结果不确定**）：走第 6 步对账。
   - 其他 `status >= 400`：`quota.release`，返回 `failed`。
6. 不确定结果对账（设计文档 §3.2）：
   - `reply_to is None` → `quota.release`，返回 `failed`。
   - `await store.find_sent_for_reply(channel_id, reply_to)` 命中 →
     **`quota.release`**，返回 `delivered`（`deduped`）。
     这里必须是 `release` 而不是 `note_sent`：本地已有这条发送记录，说明那次投递成功时
     就已经计过费了，本次预留若再转正就会**重复计入 24 小时预算**。
   - 否则 `client.fetch_messages(channel_id, after=reply_to, limit=100)`，找
     `author.id == client.self_user.id` 且 `reply` 非 None 且 `reply.id == reply_to` 的消息：
     命中 → 补记 `store.record_sent` + **`quota.note_sent`**（本地没有这条记录，
     说明没计过费，这里要补上）→ `delivered`（`deduped`）。
   - 对账查询**本身失败**（`fetch_messages` 抛 `SiteError`）→ `quota.release`，返回 `failed`，
     **不重发**。「是否已送达」此时无从判断：重发的代价是用户看到两条一模一样的回复，
     不重发的代价是这一条回复丢失（`failed` 会让 app 走失败提示路径）。
     宁可少说一句，也不要说两遍。
   - 仍未命中 → 允许**一次**重发：重复第 5 步一次；再失败 → `quota.release`，返回 `failed`。
     重发必须**完整**走第 5 步的错误分支 —— 尤其是重发吃到 429 时同样要
     `quota.backoff(retry_after or cfg.rate_limit_wait_seconds)` 再返回 `failed`，
     不能把 429 当成普通失败吞掉：那会让后续发送不被节流，直接把站点的每分钟硬限撞穿。

**「已送出但记账失败」不是发送失败**：`record_sent`（含链映射）是尽力而为 ——
它抛异常时只记一条无正文 error，仍按 `delivered` 返回并正常 `note_sent` 计费。
反过来，绝不能因为本地记账失败而重发：那会让用户看到两条一模一样的回复。
缺失的链映射由自身 SSE 回显兜底补登（路由器第 2 步）。

## 14. `core/worker.py`

```python
class ModelClient(Protocol):
    async def complete(self, messages: list[dict[str, str]]) -> str: ...

class OpenAIModelClient:
    def __init__(self, cfg: ModelConfig, api_key: str, *, redactor: Redactor,
                 transport: httpx.AsyncBaseTransport | None = None) -> None
        # transport 非 None 时传给 httpx.AsyncClient(transport=...) 再交给
        # AsyncOpenAI(http_client=...)，使测试无需真实网络（§18）
    async def complete(self, messages: list[dict[str, str]]) -> str
    async def aclose(self) -> None

class ModelError(Exception):
    def __init__(self, kind: str, retryable: bool) -> None   # kind: "timeout"|"network"|"http"|"empty"
                                                             #      |"auth"|"bad_request"

class WorkerPool:
    def __init__(self, *, queue: asyncio.Queue[Request], handler: Callable[[Request], Awaitable[None]],
                 concurrency: int) -> None
    async def start(self) -> None
    async def stop(self) -> None
    @property
    def alive(self) -> bool
```

`OpenAIModelClient.complete`：

- 用 `openai.AsyncOpenAI(base_url=..., api_key=..., timeout=cfg.timeout_seconds, max_retries=0)`
  （重试由本模块自己控制）。`temperature=cfg.temperature`、`max_tokens=cfg.max_output_tokens`。
- 返回 `choices[0].message.content`，`strip()`；为空 → `ModelError("empty", retryable=True)`。
- 超时 → `ModelError("timeout", False)`；网络错误 → `ModelError("network", True)`；
  **408 → `ModelError("timeout", False)`**（`openai` SDK 不带 408 分支，会把它归到通用
  `APIStatusError`，因此必须用 `exc.status_code == 408` 显式判出来，否则它会被下面的
  「其余 4xx」吃成 `bad_request`，日志里就看不出是超时了）；
  429 或 5xx → `ModelError("http", True)`；401/403 → `ModelError("auth", False)`；
  其余 4xx → `ModelError("bad_request", False)`。
- 重试策略：`retryable` 且仅一次重试（共两次调用）。**超时不重试（D-19）**：
  超时意味着这次调用已经等满整个 `timeout_seconds`，立即重试几乎必然再等满一次，
  只把用户看到的静默从 1 个超时周期拖成 2 个（默认 45 秒 → 90 秒）。
  网络抖动、429、5xx 仍然重试一次。
- **不得**把请求体或响应正文写日志。

`WorkerPool`：

- 启动 `concurrency` 个 worker task，各自 `while True: request = await queue.get()`。
- **同一 `session_key` 严格串行**：池内维护 `dict[str, asyncio.Lock]`，取到请求后
  `async with lock_for(request.session_key): await handler(request)`。
- `handler` 抛异常 → 记日志吞掉，worker 存活。
- `stop()` 取消所有 task 并等待结束；`alive` 表示所有 task 都还在运行。

## 15. `ops.py`

```python
class OpsServer:
    def __init__(self, host: str, port: int, *, livez: Callable[[], bool],
                 readyz: Callable[[], bool]) -> None
    async def start(self) -> None
    async def stop(self) -> None
```

- aiohttp `web.Application`；`GET /livez` → 200 `text/plain` `"ok"` 或 503 `"down"`；
  `GET /readyz` → 200 `"ready"` 或 503 `"not ready"`。
- 响应体不包含任何内部状态细节。
- 端口被占用 → 抛异常由调用方决定是否致命。

## 16. `app.py`

```python
class BotApp:
    def __init__(self, config: Config, *, transport: httpx.AsyncBaseTransport | None = None,
                 model_client: ModelClient | None = None) -> None
    async def start(self) -> None
    async def run_forever(self) -> None
    async def stop(self) -> None
    @property
    def ready(self) -> bool
    @property
    def live(self) -> bool
```

- 组件装配顺序：`Store.open` → **`store.mark_orphans_recoverable()`（崩溃恢复第一步）** →
  **`store.prune_runtime_state()` 清理一次（启动时）** → `SiteClient.start` + `login` →
  `SSEReceiver` → **用 `store.watermark()` 给 SSE 播下初始 `Last-Event-ID`**（见下）→
  `WorkerPool.start` → 启动**周期清理 task**（每 `storage.cleanup_interval_seconds`）→
  `OpsServer.start` → `sse.run()` 作为后台 task。
- 周期清理 task 的每一步都要能被取消：`stop()` 里**先取消该 task 并等待它结束，再关闭 Store**，
  否则它可能在数据库关闭后继续访问。清理本身失败只记一条 error 并等下一周期，
  **不得**终止机器人，也不得成为 `ready` 的判据。
- 启动清理与周期清理的返回值里带 `expired_thread_roots`，对每个
  `lobby_thread_session_key(root)` 调 `ctx.invalidate(...)`：过期链的内存上下文必须同时失效，
  免得一个在途请求把正文写回已经过期的链。
- **崩溃恢复为什么只需要这两步**：`mark_orphans_recoverable()` 把上一进程遗留的未完成行
  标成 `recover`，`store.watermark()` 又因为该行非终态而停在它**之前**，
  于是服务端会从水位处把它补发回来；路由器在第 6 步用 `reclaim_orphan` 重新认领它。
  因此**不需要**在启动时主动去拉历史消息做恢复 —— 补发本身就是恢复机制，
  这也顺带绕开了「历史接口一次最多 100 条、找不到就丢」的问题：
  认领失败时该行仍是 `recover`（非终态），水位不动，下一次补发或下次重启会再试。
  两步都必须在 `sse.run()` **之前**完成，否则本进程可能先认领、再被当成孤儿扫掉。
- **启动播种（崩溃恢复的关键，别漏）**：构造 `SSEReceiver` 之后、启动它之前，必须
  `sse.set_last_event_id(await store.watermark())`。
  没有这一步，进程重启后 SSE 会以「从此刻起」重新订阅，设计文档 §3.1 承诺的
  「崩溃时未完成事件依靠旧水位重新补发」就不成立 —— 崩溃瞬间在处理中的消息会被永久丢掉。
  有了它，重启后服务端会从水位处补发，未完成的候选消息被重新路由并按 `message_id` 去重。
- **运行期不要把 `last_event_id` 每帧回拨到水位**。`SSEReceiver` 在**收到**帧时推进它，
  这是 `Last-Event-ID` 的传输层语义（记住见过的最后一个 id），保留即可。
  原因见 D-16：D-15 决定了一张只记候选消息的事件表，若每帧把 `last_event_id` 回拨到
  `advance_watermark()`，非候选消息造成的水位落差会让每次重连都重放一大段无关消息。
- `ready` = 已登录 and SSE `connected` and `not queue.full()` and 未处于
  「权限/禁言不可用」状态。
- RouteResult 分派：
  - `queued` → 无事（worker 会处理）。
  - `reply_now` → 用 kind=`"notice_local"` 发送本地文案（**不是** `"notice"`：
    这类回复是应答明确用户动作的，必须不落通知冷却，见 D-1）。
  - `busy` → 发 `BUSY_NOTICE_TEXT`（kind=`"notice"`，带 `result.actor_id`）；
    冷却由 quota 按 (频道, 触发者) 统一执行（D-18），app 侧只做一次同键预读省掉白跑的尝试。
  - 三条发送路径（`reply_now` / `busy` / worker 里的 `reply` 与 `notice`）都要把
    `result.thread_root_id` / `request.thread_root_id` 传给 `sender.send`，
    出站消息才会登记进链；DM 传 `None`。
  - `resync` → 后台任务（**不要阻塞 SSE 循环**）：拉 lobby 最新 100 条 +
    `store.dm_channels()` 各最新 100 条，按 `message.id` 升序合并，
    逐条 `await router.handle_message(channel_id, message, None)`，
    最后 `sse.set_last_event_id(await store.advance_watermark())`。
    resync 期间到达的实时帧照常处理（去重键相同，不会重复回复）。
- **本轮 user turn 的拼装（D-7 的新形状）**。历史里只存**不含直接引用**的用户内容；
  直接引用只属于当前这一轮。构造给模型的最后一条 `role="user"` 内容：

  | 频道 | 本轮内容 | 提交进历史的内容 |
  |------|----------|------------------|
  | 大区 | （有引用时）`[直接引用 @<作者>] <被引用正文>\n---\n` + `speaker_wrapper(author.username, user_text)` | `speaker_wrapper(author.username, user_text)` |
  | 私聊 | （有引用时）`[引用 @<作者>] <被引用正文>\n---\n` + `user_text` | `user_text` |

  私聊的标签维持 `[引用 @作者]` 不变（只有大区用 `[直接引用 @作者]`，见 D-25）。
  即使被引用正文已经在历史里，本轮仍然保留这份直接引用：有限的重复优于丢失当前指向。
- worker 的 handler（**两处代次检查不能省**）：
  1. 开始处理前先比对 `ctx.generation(request.session_key) != request.generation`
     → 该请求已被 `/reset` 或线程过期作废：**不调模型、不发消息**，
     `mark_handled(..., "done")` 后返回。
  2. 组装本轮 `pending`（上表左列），`messages = ctx.build_messages(
     request.session_key, cfg_system_prompt, pending_user=pending,
     system_addendum=LOBBY_SHARED_SYSTEM_ADDENDUM if channel_kind == "lobby" else None)`
     → 模型（含一次重试）→ **再次**比对代次：
     - 代次已变 → **不提交历史**、**不**发送这条过期回复，只记一条日志
       （`app.stale_generation`，白名单字段），最后同样 `mark_handled(..., "done")`。
  3. 代次未变 → `sender.send(..., kind="reply", thread_root_id=request.thread_root_id)`。
  4. **只有 `SendResult.delivered` 为真**，才 `ctx.append_exchange(
     session_key, pending, 模型输出)`。模型失败、额度拒绝（`reason == "quota"`）、
     发送确定失败、`failed`/`backoff` 一律**不提交**——否则用户没看见的内容会变成
     后续轮次里的幽灵历史。
  第二次代次检查是必需的：模型调用可能持续几十秒，`/reset` 或线程过期完全可能在它
  返回之前发生，而那时清空后的会话会被这条旧回复污染，并在**下一轮**被再次外送给模型。
  模型最终失败则发 `FAILURE_NOTICE_TEXT`（kind=`"notice"`，带
  `request.message.author.id` 作为触发者与 `request.thread_root_id` 作为链；
  冷却同 `busy`，见 D-18）。
  无论哪条路径，最后都 `await store.mark_handled(request.message.id, "done")`。
- `reply_now` / `busy`：发送尝试结束后 `await store.mark_handled(message_id, "done")`。
- 额度用尽（`quota` 拒 `reply`）→ 尝试发一次 `QUOTA_NOTICE_TEXT`（kind=`"notice"`），
  无论是否发出都 `mark_handled(message_id, "done")`。
- 403 按 `SendResult.reason` 分成两条路（D-4）：
  - `forbidden`（权限不足 / 被禁言）→ 进入不可用状态，每 `ready_probe_seconds` 用
    `client.probe_chat()` 探测，期间不刷日志、不发消息；恢复后重连 SSE。
  - `csrf`（跨源被拒）→ **不进不可用状态、不探测**，只记一条 error 级日志
    （`app.csrf_rejected`），因为这是我们自己发错了 `Origin`/`Referer`，
    重试与探测都无意义。机器人继续正常工作。
- 401 由 `SiteClient` 内部处理；SSE 重连由 `SSEReceiver` 内部处理。
- 优雅关闭：`stop()` 依次停 周期清理 task → SSE → WorkerPool → OpsServer → client → store，
  总超时 10 秒。清理 task 必须先于 Store 关闭被取消并等待结束。

## 16.1 评论子系统

评论功能只在 `config.comments.enabled` 为真时装配，且不得改变聊天队列、聊天
`ready` 判定或聊天配额。以下是评论层与 Store、路由、发送器之间的最小内部协议：

```python
class RecentCommentPoller:
    async def poll_once(self) -> DiscoveryReport: ...

class NotificationPoller:
    async def poll_once(self) -> NotificationReport: ...

class CommentService:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def enqueue(self, candidate: CommentCandidate, claim: CommentClaim) -> bool: ...
```

最近评论轮询每 30 秒读取最近 100 条，首次只写不可回复的水位基线；后续按
`(created_at, comment_id)`，严格检查同时间边界，并在返回满 100 条且最旧项仍新于水位
时记录 `comment.discovery_gap_possible`。完整树超过 10000 节点或 8 MiB 时，已粗筛候选
逐一 claim 后写 `skipped_tree_too_large`/`skipped_oversize` 并推进水位；只有树网络/HTTP
临时失败保留水位。通知轮询每
15 秒最多读取 5 页，只接受未读 `action == "评论回复"` 且有效 blog 的通知（actor 缺失仍进入
unmatched）；同一
轮按文章合并树读取，匹配全部直接回复本地机器人评论的候选。

通知 actor 缺失不是静默丢弃：它仍进入 unmatched 计数，成功读取评论树累计 5 次后
标记已读。冷启动基线保存持久 cutoff；最多 5 页仍有 `has_next` 时不写 baseline done，
后续轮次继续清理旧通知，cutoff 之后的新通知按正常路径处理。

两个来源都必须调用唯一的 `Store.claim_comment(...)` 原子领取；候选正文、文章正文和
actor 原值只在内存中使用。树展平用显式栈，节点上限 10,000；响应字节上限由 SiteClient
执行。通知树成功但未匹配才增加 unmatched，连续 5 次转 `unmatched`；标记已读失败只重试
标记，不重新匹配或发送。

`CommentService` 持有容量 50、并发 1 的独立队列、waiting dispatcher、recent poller、
notification poller 和 worker。它与聊天共享模型客户端及 semaphore gate，但评论模型调用
只能持有一个 gate 位。模型调用前 Service 先原子 reserve reply quota，将令牌传给 Sender；
模型/取消/发送失败 release，成功只 note 一次，且 Context 只提交 Sender 实际脱敏截断正文。
评论临时故障不影响 `readyz`，评论后台 task 意外退出使 `livez` 失败；handler 意外异常
统一将非终态 queued 事件转 `recover` 并退避。
启动时先恢复旧评论事件再启动评论任务，关闭时先等待四个评论 task，再关闭聊天 SSE、
worker、ops、模型、client、Store；每个取消动作必须 await。

## 17. `__main__.py`

`python -m raricy_bot`：加载配置 → `setup_logging` → 构造 `BotApp` →
安装 SIGINT/SIGTERM 处理 → `run_forever()`；配置错误以退出码 2 结束并打印一行原因
（不得打印密钥）。`--config PATH` 可覆盖配置路径。

## 18. 测试约定

- `tests/` 下按模块命名：`test_config.py`、`test_redact.py`、`test_text_utils.py`、
  `test_models.py`、`test_client.py`、`test_sse.py`、`test_store.py`、`test_quota.py`、
  `test_context.py`、`test_router.py`、`test_sender.py`、`test_worker.py`、
  `test_ops.py`、`test_app.py`、`test_recovery.py`、`test_logging_safety.py`。
- 网络层测试**不得**发起真实连接：用 `httpx.MockTransport` 注入 `SiteClient(transport=...)`。
- **时间一律注入**：`quota`、`router`、`store` 的新接口都接受调用方传入的 `now`
  或可注入时钟，测试不得依赖真实时间推移来构造 7 天过期、48 小时保留、冷却到期这些边界。
- 模型客户端测试用假 `ModelClient`（实现 `complete`），不 mock `openai` 内部。
- 异步测试用 `pytest-asyncio`，`asyncio_mode = "auto"`（写在 `pyproject.toml`）。
- 断言必须验证**行为**；不得只断言 mock 被调用。
- 测试输出必须干净（无 warning）。
- 需要断言「日志/数据库里没有正文、Cookie、密码、API Key」的用例，
  放在 `test_app.py` 与 `test_store.py` 中。

## 19. 全局红线

1. 不把 Cookie、密码、API Key、消息正文、模型请求体写入日志或 SQLite。
2. 用户内容只放在 `role="user"` 的消息里，绝不拼进 system prompt。
3. HTTP 客户端不设 `Origin` / `Referer`。
4. 成功判据只看 `code == 200`。
5. 不实现图片理解、博客理解、工具调用、联网、长期记忆（第一版明确不支持）。
6. 不调用站内管理接口，不执行代码，不访问服务器文件。
7. 大区共享链的持久化只存 `message_id -> thread_root_id`：**不存** `author.id`、用户名、
   参与者名单、任何正文；发言者一律从实时 DTO 取。日志里也不得出现用户名或用户 id。
8. 直接引用正文只进当前轮，绝不写进 `ContextManager` 历史（D-7 仍然有效）。
