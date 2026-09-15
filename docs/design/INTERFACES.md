# 接口契约（锁定）

本文件是各模块之间**唯一**的接口口径。实现时签名、名称、返回值形状必须与本文件一致；
若认为某处不合理，先在报告里提出，不要自行改动后让下游跟着改。

上游契约见 `docs/materials/chat-bot.md`；设计依据见 `docs/archive/BACKGROUND.md`。
歧义裁决见 `docs/design/DESIGN_DECISIONS.md`。

## 0. 工程约定

- Python `>=3.12`（开发机为 3.13）。包根目录 `src/raricy_bot/`，包名 `raricy_bot`。
- 运行期依赖：`httpx`、`openai`、`PyYAML`、`aiohttp`、官方 Python MCP SDK（当前约束
  `mcp>=2.2,<3`）。SQLite 用标准库 `sqlite3`，**不引入 aiosqlite**。测试用 `pytest`+
  `pytest-asyncio`。
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
    vision_enabled: bool = False          # 图片输入，默认关闭（§20）
    max_image_bytes: int = 5242880        # 5 MiB，单图下载期硬上限

@dataclass(frozen=True)
class BehaviorConfig:
    context_turns: int = 10
    context_input_tokens: int = 8000
    max_input_chars: int = 8000
    quoted_blog_max_chars: int = 1000     # 引用博客的正文上限；与 comments 的键**互相独立**
    content_ref_max_chars: int = 2000     # 单条 `[@<ID>]` 展开的字符上限；见 §25
    max_output_chars: int = 5000
    concurrency: int = 3
    queue_size: int = 50
    minute_attempt_limit: int = 25
    daily_normal_limit: int = 1950
    daily_absolute_limit: int = 2000
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
    max_images_per_reply: int = 3           # 一轮评论回复的图片名额（§20）；0 = 评论侧不取图
    max_output_chars: int = 5000
    max_response_bytes: int = 8388608       # SiteClient 硬上限 8 MiB
    max_tree_nodes: int = 10000             # 显式栈硬上限 10000
    unmatched_attempt_limit: int = 5
    minute_attempt_limit: int = 20
    daily_reply_limit: int = 1950
    daily_absolute_limit: int = 2000
    article_cooldown_seconds: int = 5
    conversation_retention_seconds: int = 2592000
    dedupe_retention_seconds: int = 7776000
    retry_base_seconds: int = 5
    retry_max_seconds: int = 300
    server_backoff_seconds: int = 3600

@dataclass(frozen=True)
class McpBindingConfig:
    server: str
    tool: str

@dataclass(frozen=True)
class McpServerConfig:
    name: str
    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    env_from: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    account_pool: McpAccountPoolConfig | None = None   # §22；只存环境变量名，不存 Key

@dataclass(frozen=True)
class McpFeatureConfig:
    name: str
    enabled: bool = True
    bindings: tuple[McpBindingConfig, ...] = ()
    result_count: int = 5
    result_item_token_limit: int = 3000
    history_item_token_limit: int = 500
    max_query_chars: int = 500
    max_tool_calls_per_turn: int = 1
    max_concurrency: int = 1
    min_interval_seconds: float = 2.0

@dataclass(frozen=True)
class McpConfig:
    enabled: bool = False
    connect_timeout_seconds: float = 10.0
    call_timeout_seconds: float = 20.0
    reconnect_base_seconds: float = 3.0
    reconnect_max_seconds: float = 60.0
    servers: dict[str, McpServerConfig] = field(default_factory=dict)
    features: dict[str, McpFeatureConfig] = field(default_factory=dict)

@dataclass(frozen=True)
class Secrets:
    username: str
    password: str
    llm_api_key: str

# McpConfig、McpServerConfig、McpFeatureConfig 和 McpBindingConfig 的完整定义见 §21；
# McpAccountPoolConfig 见 §22.1，KnowledgeBaseConfig 见 §23.1，MemoryConfig 见 §26.1。

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
    mcp: McpConfig = field(default_factory=McpConfig)
    knowledge_base: KnowledgeBaseConfig = field(default_factory=KnowledgeBaseConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)   # 长期记忆 Beta，见 §26

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
  Exa 的 `EXA_API_KEY` 通过 `mcp.servers.exa.env_from` 映射给 stdio 子进程；缺失时只停用 Exa，
  不阻止普通聊天、评论或健康检查。
- YAML 顶层键：`site` / `model` / `behavior` / `ops` / `storage`（`db_path`）/ `logging`（`level`）/
  `comments` / `mcp` / `system_prompt`。`comments.enabled` 默认 `false`；关闭时不创建评论队列、
  poller 或 quota，但 SiteClient 仍接收 `comments.max_response_bytes` 与 `max_tree_nodes`
  默认上限（旧窄客户端替身可省略这两个关键字）。
  未知键**忽略**；缺失键用上表默认值（`site.base_url`、`model.base_url`、`model.model`、`system_prompt` 必填）。
- 校验：`base_url` 必须 http/https 且非空；`0 <= temperature <= 2`；`timeout_seconds > 0`；
  `max_output_tokens >= 1`；所有 `behavior` 整数字段 `>= 1`；`daily_normal_limit < daily_absolute_limit`；
  `minute_attempt_limit >= 1`；`ops.port` 在 1..65535。
- `model.vision_enabled` 必须是**布尔**（YAML 里写成 `"true"` 字符串即 `ConfigError`）；
  `model.max_image_bytes` 是正整数（布尔不算整数）且 `<= 10485760`（站点图床单图上限是
  上游常量，配更大只会掩盖意图）。两者都缺省：`false` / `5242880`。
- `behavior.quoted_blog_max_chars` 是正整数（布尔不算整数），缺省 1000。
  它与 `comments.article_max_chars` **默认值相同但彼此独立**：聊天模型与评论模型未必是
  同一个，两边各自可调；要求同值时靠配置自觉（见 `DESIGN_DECISIONS.md` D-47）。
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
`thread_root_id`, `size_bytes`, `limit_bytes`；Exa 池与知识库另加 §22 / §23 用到的
`slot`（进程内槽位序号）、`snapshot_version`、`chunk_count`、`available_count`。
这些字段都只承载数字或配置来源的稳定短标识，绝不放 Key、环境变量名、查询、路径或正文。

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
def parse_search_command(text: str) -> str | None
def parse_kb_command(text: str) -> str | None
def leading_capability_command(text: str) -> str | None
def has_media(message: Any) -> bool     # message.image is not None or message.blog is not None
def has_image(message: Any) -> bool     # message.image is not None and not message.image_missing
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
- `parse_search_command`：仅识别消息开头独立的 `/search`（大小写不敏感），返回去掉前缀后的正文；
  `/searching`、`/search-x` 与正文中间出现的 `/search` 均不命中。该能力只由聊天 Router
  写入 `Request.enabled_features`，评论路径不得调用它。
- `parse_kb_command`：与 `parse_search_command` **同一条规则**，命令名换成 `/kb`；
  `/kbase`、`/kb-x`、正文中间的 `/kb` 都不命中。同样只由聊天 Router 解析。
- `leading_capability_command`：正文开头若是一个独立的能力命令，返回 `"search"` 或 `"kb"`，
  否则 `None`。它只服务于 §12 第 9.1 步的「最多一个能力」判定：剥离一个能力前缀后，
  若剩余正文又以能力命令开头，就返回能力冲突提示，`/search /kb ...` 之类的嵌套
  永远拿不到两个能力（D-39）。
- `has_image` 与 `has_media` 回答的是两个不同的问题：前者是「这一轮能不能把图交给模型」
  （因此 `image_missing` 为真时不算），后者是「有没有我读不了的东西」（保持原义）。

## 5. `texts.py`

只放字符串常量，无逻辑。必须至少导出：

```python
TRUNCATION_SUFFIX: str      # 追加在被截断输出末尾的省略提示
HELP_TEXT: str              # 能力、隐私、默认离线与无图片输入说明（vision 关闭时）
HELP_TEXT_WITH_VISION: str  # 同上，但能力句声明可以查看用户发来的图片（vision 开启时）
USAGE_HINT: str             # 空白内容或只有 @bot 时的用法提示
UNSUPPORTED_MEDIA_TEXT: str # 只有附件、没有可读内容；聊天区已不再用，评论区仍用
IMAGE_UNAVAILABLE_TEXT: str # 有图但读不到（未启用图片输入 / 图已失效 / 取图失败）
BLOG_UNAVAILABLE_TEXT: str  # 引用的博客读不到（已删除 / 取不到正文 / id 不合法）
TOO_LONG_TEXT: str          # 超过 max_input_chars
SECRET_REFUSAL_TEXT: str    # 本地拒绝索取系统提示/密钥
RESET_DONE_TEXT: str        # /reset 后的确认
BUSY_NOTICE_TEXT: str       # 队列满
FAILURE_NOTICE_TEXT: str    # 模型最终失败
QUOTA_NOTICE_TEXT: str      # 当日额度用尽
SEARCH_USAGE_TEXT: str      # `/search` 无参数时的本地用法
SEARCH_UNAVAILABLE_TEXT: str # MCP/模型 tools 能力不可用时的本地提示
LOBBY_SHARED_SYSTEM_ADDENDUM: str   # 共享大区请求的静态 system 附加说明（见 5.1）
MCP_SEARCH_SYSTEM_ADDENDUM: str     # 搜索结果不可信边界的静态 system 附加说明
HELP_TEXT_WITH_KB: str              # 同上，但能力句声明 `/kb`（KB 开启、vision 关闭）
HELP_TEXT_WITH_VISION_AND_KB: str   # 同上，同时声明图片与 `/kb`
KB_USAGE_TEXT: str                  # `/kb` 无参数时的本地用法
KB_UNAVAILABLE_TEXT: str            # KB 未启用或没有可用索引
KB_ACCESS_DENIED_TEXT: str          # 当前用户/频道不在 KB 访问策略内
KB_NO_RESULTS_TEXT: str             # 检索无相关结果
CAPABILITY_CONFLICT_TEXT: str       # 一条消息里出现两个能力命令
KB_SYSTEM_ADDENDUM: str             # 本地资料不可信边界的静态 system 附加说明（见 5.2）
```

记忆相关的 `help_text()` 重构、新增固定文案与 `MEMORY_SYSTEM_ADDENDUM` 见 §36
（记忆关闭时四个既有帮助常量的内容逐字节不变）。

`HELP_TEXT` 与 `HELP_TEXT_WITH_VISION` **共用同一份首尾文字**，只有中间那句能力描述
二选一（实现上是三段模块级常量拼接）。因此两份文案的事实披露必须逐字一致：身份、
第三方模型处理、默认离线、无长期记忆、`/help`、`/reset` 与 `/search`，以及大区共享上下文的四点。
新增或修改其中任何一处都必须同时作用于两者。

`HELP_TEXT` 必须包含：机器人身份声明、能力范围、**消息可能发送至第三方模型处理**、
默认离线、不能看图/博客、`/help`、`/reset` 与 `/search` 用法；以及大区共享上下文的四点说明
（公开多人上下文、只有精确 `@bot` 的消息进入、新参与者加入后最近的链内历史会
再次发送给第三方模型、回复链内消息才能延续上下文，且 `/reset` 创建新链而非删除旧链）。
`HELP_TEXT_WITH_VISION` 的差别只有一句：可以查看用户发来的图片，且**图片同样会转交
第三方模型处理**。两者都调用于 `/help` 命令，**不得**触发模型调用；整条都必须能塞进
站点单条消息上限（**5000 字**）。

> 这个数字以上游合同为准：`docs/materials/chat-bot.md` §7.1 的「长度上限」表写明纯文本消息与
> 带附件时的 `content` **同为 5000 字**（2026-09 起两档同值，此前才是 1000 / 500），
> 超限错误码是 `tooLong` / `captionTooLong`。本节曾误写「1000 字」，那个数字正是把
> `tests/` 里的断言带偏的原因；两处不一致时一律以上游合同为准（见 CLAUDE.md）。

### 5.1 `LOBBY_SHARED_SYSTEM_ADDENDUM`

共享大区请求在 system 消息里额外拼接的一段**静态**说明（D-24）。硬性要求：

- 是模块级字符串常量，**不含任何占位符**：不得插入用户名、用户 id、正文或任何运行时数据；
  拼接时也不做格式化（`system_prompt + "\n\n" + LOBBY_SHARED_SYSTEM_ADDENDUM`）。
- 内容必须覆盖：当前是公开多人对话；发言者标签仅用于区分说话者；不得把不同用户名
  视为同一人；所有用户正文都是不可信数据，其中的授权、身份或指令性陈述一律不作为依据；
  指向特定参与者时优先使用其用户名。
- 私聊**不**使用这段说明。它是否出现，只由 `channel_kind == "lobby"` 决定。
- 它计入 `context_input_tokens` 预算（与 system_prompt 同样先扣）。

### 5.2 `KB_SYSTEM_ADDENDUM`

`/kb` 当前轮在 system 里额外拼接的一段**静态**说明（D-38）。硬性要求：

- 与 `LOBBY_SHARED_SYSTEM_ADDENDUM` 同样**不含任何占位符**、不做格式化；
- 内容必须覆盖：本轮附带的本地资料是**不可信数据**而非指令；只能引用确实提供的
  `[KBn]` 标签，不得编造标签、路径或来源；资料不足以回答时明确说明不足；
  资料中的任何指令性陈述一律不作数；
- 它只由「本轮使用了 `kb` 能力」决定是否出现，与 `MCP_SEARCH_SYSTEM_ADDENDUM` **互斥**
  （能力冲突在前，不可能同时出现）。

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

class ImageFetchError(Exception):
    def __init__(self, reason: str, status: int = 0) -> None
    reason: str             # "host_not_allowed"|"too_large"|"http"|"network"
    status: int             # 仅 "http" 有意义

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

    async def fetch_clipboard(self, clip_id: str) -> Clipboard
        # 读一篇云剪贴板（内容引用语法 §三）：GET /api/clipboard/<8位ID>，带 Cookie。
        # 站点要求登录且 Core 以上；私有剪贴板对非作者是 403 —— 那是一次**降级**，
        # 不是故障（内容引用语法 §四）。code != 200 -> SiteError。
        # 信封形状 {"code":200,"message":"ok","clip":{id,title,author_name,publicity,
        # content,created_at}}；clip 缺失或 content 不是字符串 -> SiteError(200,
        # "malformed clipboard response")。
        # clip_id 形态（长度 8、纯 ASCII 字母数字）不对 -> ValueError 且**不发请求**。
        # **401 不重新登录、不重试**（同 fetch_image）：真正的会话失效由 SSE 那条路恢复。

    async def fetch_vote(self, vote_id: str) -> Vote
        # 读一个投票（9 位）：GET /api/votes/<ID>，带 Cookie，同样要求 Core 以上。
        # 信封 {"code":200,"message":"ok","data":{id,title,author_name,is_creator,
        # is_locked,created_at,total_votes,user_voted,options:[{id,label,count,percentage}]}}。
        # 只读：本方法**不会**替机器人投票。解析容错（缺 count 记 0、缺 percentage 记 None）。

def image_raw_path(image_id: str) -> str
    # 图床直链的相对路径（10 位 ID）："/api/images/<ID>/raw"。
    # 站点前端也直接拼这条路径、不发额外请求，所以图片引用**不消耗**接口调用。
    # 形态不对 -> ValueError。ID 只含字母数字，拼进路径注入不进东西。

CLIPBOARD_ID_LEN: int = 8
VOTE_ID_LEN: int = 9
IMAGE_ID_LEN: int = 10
    # ID 长度即内容类型（内容引用语法 §三）。

    async def fetch_image(self, url: str, *, max_bytes: int) -> bytes
        # 取回一条消息附带的图片原始字节（§20）。
        # url 用 urljoin(base_url + "/", url) 解析：站点给的是相对路径
        # `/api/images/<id>/raw`（chat-bot.md §11.1 写成绝对 URL，与源码不符，
        # 按该文档 §0「以源码为准」）。
        # **同源硬约束**：解析结果必须是 http/https 且 scheme + host + 有效端口与
        # base_url 完全一致，否则 ImageFetchError("host_not_allowed") 且**不发出请求**。
        # 理由：一旦允许「照站点给的 url 带着 Cookie 去 GET」，任何能让站点返回任意 url
        # 的路径都会变成凭据外泄通道。同源请求照常带 Cookie 与 Accept: image/*。
        # 字节上限在**流式累计过程中**执行：超过 max_bytes 立即抛
        # ImageFetchError("too_large")，**不读完整个响应**。
        # 非 200 -> ImageFetchError("http", status)；零字节响应体同样算 http(200)；
        # 网络错误/超时 -> ImageFetchError("network")。
        # **401 不重新登录、不重试**：取图失败只是一次降级，不值得多一次登录。
        # **本方法不写任何日志**（URL 不得进日志），失败原因由调用方记稳定字段。
        # 返回原始字节，**不返回 Content-Type**：格式判定以字节嗅探为准（§20）。

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
        # 三种 kind 全部计入 24 小时总量（2000 带），见 D-1。
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
                       system_addendum: str | None = None,
                       feature_context: bool = False) -> list[dict[str, str]]
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
- `feature_context=True`（`/kb` 这一轮）把 `max_input_tokens` 当作**硬上限**：允许把历史
  整对丢到**一条不剩**，只保留 system 与 `pending_user`。这是 D-38 对 P0-05 的裁决：
  能力数据块已经被 `kb.max_context_tokens` 限死，宁可让模型看不到旧历史，
  也不能让一个超出预算、又无法丢弃的 `pending_user` 把整个请求顶穿。
  普通聊天的语义**不变**：那一条路径永远至少保留最后一组历史。
- system 消息只有一条：`system_prompt` + （`system_addendum` 非空时）`"\n\n" + system_addendum`。
  用户内容**绝不**拼进 system 内容。
- `reset` 与 `invalidate` 都会清空历史并**递增代次**（即使会话原本不存在），
  用来作废在途请求；区别只是 `reset` 返回原会话是否存在。
  线程过期走 `invalidate`，DM `/reset` 走 `reset`。
- 同一 `session_key` 的并发访问由调用方保证串行（worker 已保证）。
- 记忆补充资料（`SupplementalItem` 与 `build_messages` 的新参数 `supplemental_items`）的拼装与
  预算规则见 §33；`supplemental_items` 为空时输出必须与改动前逐字节一致。

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
    enabled_features: frozenset[str] = frozenset()  # 当前轮显式授权的通用能力

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
                 storage: StorageConfig, now: Callable[[], float] = time.time,
                 vision_enabled: bool = False, kb_enabled: bool = False) -> None

    async def handle_stream(self, event: StreamEvent) -> RouteResult
    async def handle_message(self, channel_id: str, message: ChatMessage,
                             event_id: int | None) -> RouteResult
```

Beta 记忆接入（`Request.memory_allowed`、`MessageRouter` 的 `memory_access` / `memory_queue` /
`private_enabled` **三个**参数与新增动作 `"memory_queued"`）见 §34；三个都不注入时行为与今天
逐字节一致。

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
   - 9.1 **单一能力解析（D-39）**：先 `parse_search_command(user_text)`，再
     `parse_kb_command(user_text)`；命中就把对应的通用能力名（`"search"` / `"kb"`）放进
     `enabled_features` 并把 `user_text` 换成剥离后的正文。两者都只在消息开头生效，
     一条消息里只会剥离**一个**前缀。
     - 剥离之后若 `user_text` 为空：`search` → `reply_now`（`empty`，文案
       `SEARCH_USAGE_TEXT`）；`kb` → `reply_now`（`kb_usage`，文案 `KB_USAGE_TEXT`）。
     - 否则若 `leading_capability_command(user_text)` 非空（`/search /kb ...`、
       `/kb /search ...`、同一条命令写两遍）→ `reply_now`（`capability_conflict`，
       文案 `CAPABILITY_CONFLICT_TEXT`）。**不**剥第二个前缀、**不**调模型、**不**检索。
     - `/search /help`、`/kb /help`、`/search /reset`、`/kb /reset` 不构成冲突
       （`/help` 与 `/reset` 不是能力命令），继续走下面的本地命令判定，保持
       「本地动作优先」的既有合同。
     - 能力名只写入 `Request.enabled_features`，评论路径完全不解析这两个命令。
   - `user_text` 为空，按顺序**五分支**（博客排在图片**之前**，理由见 D-47）：
     - `message.blog is not None and not message.blog_missing` → **不回复**，直落第 10 步入队，
       最终 `reason` 为 `blog_only`（取正文与降级由 worker 负责，路由器不做 I/O）；
     - 否则 `vision_enabled` 且 `has_image(message)` → 同样入队，最终 `reason` 为 `image_only`；
     - 否则 `message.image is not None` → `reply_now`（`media_only`），
       文案 `IMAGE_UNAVAILABLE_TEXT`（图片输入未开启，或 `image_missing`）；
     - 否则 `message.blog is not None`（此时必然 `blog_missing`）→ `reply_now`（`media_only`），
       文案 `BLOG_UNAVAILABLE_TEXT`；
     - 否则 → `reply_now`（`empty`），文案 `USAGE_HINT`。
     空正文不会命中 9.3-9.7 的任何一个分支（命令判定与探测词都要求非空内容），
     因此「不回复直接入队」不会误判。
     注意大区里纯图消息仍然必须**带 `@bot`**（第 5 步的 mention 过滤在前），
     即用户输入 `@bot` 并附图；私聊不需要；纯博客引用同理。
   - `/help` 的文案按 `vision_enabled` × `kb_enabled` 四选一：`HELP_TEXT_WITH_VISION_AND_KB`、
     `HELP_TEXT_WITH_KB`、`HELP_TEXT_WITH_VISION`、`HELP_TEXT`。帮助文案必须说实话：
     KB 关闭时不得宣传 `/kb`，开启时必须披露「从本地资料检索、命中片段会发给第三方模型」。
   - `is_help_command` → `reply_now`（`help`），文案 `HELP_TEXT`。
   - `is_reset_command`：
     - **大区**：**不**调用 `ctx.reset`（原链不受影响，见 D-21），直接 `reply_now`（`reset`），
       文案 `RESET_DONE_TEXT`。第 8 步已经用 `force_new=True` 把本条命令建成了新链。
     - **私聊**：`ctx.reset(session_key)`（清空并递增代次）→ `reply_now`（`reset`）。
   - 有媒体且 `user_text` 非空 → 照常处理文本，继续往下。图片是否随本轮外送由
     worker 决定（`vision_enabled` 且图可读时附上，见 §16），路由器在这一步不分流。
     引用的博客同理：正文由 worker 取（§24），路由器这一层不分流。
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

**`max_output_chars` 还有一条上游约束**：记忆的自动提取披露（§34.4、D-63）按
`max_output_chars - len(披露) - len(TRUNCATION_SUFFIX)` 预留空间，因此这条上限至少要能容下
「一条最长的披露 + 一个回答字符 + `TRUNCATION_SUFFIX`」；部署打开自动提取时由 §26.2 第 13 条在
启动期校验（`enabled=false` 或 `auto_capture_available=false` 时不施加）。本节的第 1 步是那条预留
的**兜底**：即使文本仍超上限，也只按自然段截断；披露本身装不下时由 §34.4 放弃披露（D-77），
不会退化成「整条回答变成一句截断提示」。

**「已送出但记账失败」不是发送失败**：`record_sent`（含链映射）是尽力而为 ——
它抛异常时只记一条无正文 error，仍按 `delivered` 返回并正常 `note_sent` 计费。
反过来，绝不能因为本地记账失败而重发：那会让用户看到两条一模一样的回复。
缺失的链映射由自身 SSE 回显兜底补登（路由器第 2 步）。

## 14. `core/worker.py`

```python
class ModelClient(Protocol):
    async def complete(self, messages: list[dict[str, Any]]) -> str: ...

class OpenAIModelClient:
    def __init__(self, cfg: ModelConfig, api_key: str, *, redactor: Redactor,
                 transport: httpx.AsyncBaseTransport | None = None) -> None
        # transport 非 None 时传给 httpx.AsyncClient(transport=...) 再交给
        # AsyncOpenAI(http_client=...)，使测试无需真实网络（§18）
    async def complete(self, messages: list[dict[str, Any]]) -> str
    async def complete_with_tools(self, messages: list[dict[str, Any]], *,
                                  tools: tuple[ToolDefinition, ...],
                                  execute: ToolExecutor,
                                  max_tool_calls: int,
                                  generation_is_current: Callable[[], bool],
                                  model_gate: Any | None = None) -> ToolCompletion
    async def aclose(self) -> None

class ModelError(Exception):
    def __init__(self, kind: str, retryable: bool) -> None   # kind: "timeout"|"network"|"http"|"empty"
                                                             #      |"auth"|"bad_request"
                                                             #      |"tools_unavailable"|"tools_unsupported"

class WorkerPool:
    def __init__(self, *, queue: asyncio.Queue[Request], handler: Callable[[Request], Awaitable[None]],
                 concurrency: int) -> None
    async def start(self) -> None
    async def stop(self) -> None
    @property
    def alive(self) -> bool
```

`OpenAIModelClient.complete`：

- 参数类型放宽为 `list[dict[str, Any]]`：只有**当前轮**那条 `role="user"` 消息的
  `content` 可能是内容块列表（`[{"type":"text",...}, {"type":"image_url",...}]`，§20），
  历史与 system 一律仍是字符串。
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
                 model_client: ModelClient | None = None,
                 mcp_manager: McpManager | None = None,
                 knowledge_service: KnowledgeService | None = None) -> None
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

  **图片标记（§20，D-28）**：本轮带图时，`user_text` 先加上一行标记再包装，
  三种形状与上一段共用同一个字符串（发给模型的就是提交进历史的）：

  | `image_state` | 有正文 | 无正文 |
  |---------------|--------|--------|
  | `"ok"` | `[图片]\n---\n正文` | `[图片]` |
  | 其余失败值 | `[图片未提供]\n---\n正文` | （不会到模型：纯图读不到走本地提示） |
  | `"none"` | 正文（不加标记） | — |

  **引用博客标记（§24，D-47）**：本轮引用了博客时，`_with_image_marker` 之后再加
  `_with_blog_marker`。外送的那一份带**整块**（标题 + 正文），提交进历史的那一份
  只剩一行标记 —— 块与标记只差「正文给不给」这一处：

  | `blog_state` | 外送 | 历史 |
  |--------------|------|------|
  | `"ok"` / `"too_long"` | 块（正文或「正文因长度规则未提供」） | `[引用博客]` |
  | `"missing"` | 块（正文栏「该博客已被删除，正文不可读」） | `[引用博客已删除]` |
  | `"failed"` | 块（正文栏「正文未取得」） | `[引用博客未取得]` |
  | `"none"` | 不加任何东西 | — |

  大区的顺序是「直接引用 → 发言者包装 → 图片标记 → 引用博客 → 正文」，即标记在
  `speaker_wrapper` **内部**：图与引用都属于发言人这条消息。
  `attach_image` 必须排在 `_apply_reply_prefix` **之后**，因为后者按字符串拼接 content。

  **直接引用前缀（D-7 / D-48 / D-54）**：被引用消息的三种边角各留一行标记，
  图片的标记与被引用正文并列——整条引用就是一张图时标记本身就是正文，
  正文之外还带图时标记补在正文**后面**：

  | `reply` 的样子 | 前缀正文 |
  |----------------|----------|
  | `is_deleted` | `[该消息已删除]`（判定**先于**正文，契约没承诺正文被替换过） |
  | 正文非空 | `<正文>`；带图时再补 ` [图片]`（取不到则 ` [图片未提供]`） |
  | 正文为空、有 `image_url` | `[图片]`（取不到则 `[图片未提供]`） |
  | 正文为空、无图（例如拍一拍） | `[无正文]` |

  `reply_image_state == "ok"` 时那张**缩略图**会作为内容块挂在最后一条 user 消息上；
  视觉关闭时一个字节都不取，标记仍是 `[图片]`——那个标记本来就只说明「被引用的是
  图片消息」。`is_deleted` 的引用从不取图。契约只给 URL（无 id、无 mime、是缩略图），
  因此只能按原样取回，同源与格式都由 `fetch_image` 与字节嗅探兜住。
- worker 的 handler（**两处代次检查不能省**）：
  0. `vision_enabled` 为假时**完全不碰** `ImageLoader`（`_load_image` 直接返回
     `(None, "none")`），因此关闭图片输入的进程里没有任何新增的图床请求。
  1. 开始处理前先比对 `ctx.generation(request.session_key) != request.generation`
     → 该请求已被 `/reset` 或线程过期作废：**不调模型、不发消息**，
     `mark_handled(..., "done")` 后返回。
  1.5 取图（`ImageLoader.load`，在模型门**之外**，超时由站点请求超时兜住）→
     `(image_part, image_state)`；紧跟其后取**被引用消息的缩略图**
     （`_load_reply_image`，同一条路、同样在模型门之外）→
     `(reply_image_part, reply_image_state)`；然后取引用的博客（`BlogLoader.load`，
     同样在模型门之外）→ `(blog_block, blog_state)`。整条消息只有引用、且一样都没取到时：
     - `image_part is None` 且 `not blog_readable(blog_state)` 且 `user_text` 为空
       且（`image_state != "none"` 或 `blog_state != "none"`）
       → 发一次本地文案（kind=`"notice_local"`，见 D-30），**不调模型**，
       然后走 finally 的 `mark_handled`。文案按消息带了什么选：有图片载荷时用
       `IMAGE_UNAVAILABLE_TEXT`（与改动前逐字节一致），否则 `BLOG_UNAVAILABLE_TEXT`；
     - 其余情况继续往下。
  2. 组装本轮 `pending`（上表左列，**加上图片与引用博客标记**），
     `messages = ctx.build_messages(
     request.session_key, cfg_system_prompt, pending_user=pending,
     system_addendum=LOBBY_SHARED_SYSTEM_ADDENDUM if channel_kind == "lobby" else None,
     feature_context=("kb" in request.enabled_features or blog_block is not None))`
     → `_apply_reply_prefix`（仍是字符串拼接，带上 `reply_image_state`）
     → `attach_image`（顺序：消息自己的图 → 被引用消息的缩略图 → 各处引用换出来的图）
     → 模型（含一次重试）→ **再次**比对代次：
     - 代次已变 → **不提交历史**、**不**发送这条过期回复，只记一条日志
       （`app.stale_generation`，白名单字段），最后同样 `mark_handled(..., "done")`。
  3. 代次未变 → `sender.send(..., kind="reply", thread_root_id=request.thread_root_id)`。
  4. **只有 `SendResult.delivered` 为真**，才 `ctx.append_exchange(
     session_key, pending, 模型输出)`；这里的 `pending` 是**不带博客块**的那一份
     （`_pending_turn(request, image_state, blog_state)`，只留标记）。模型失败、
     额度拒绝（`reason == "quota"`）、发送确定失败、`failed`/`backoff` 一律**不提交**
     ——否则用户没看见的内容会变成后续轮次里的幽灵历史。
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
- 全局记忆 Beta 的装配、启动顺序、关闭顺序与 `memory_queued` 分派见 §34；
  `memory.enabled=false` 时全部跳过，且不创建目录。

### 16.0 `/kb` 的数据流（D-38 … D-44）

`KnowledgeService` 只在 `knowledge_base.enabled=true` 时扫描目录；构造它本身不做 I/O。
`start()` 里最佳努力启动（失败只记 `kb.index_failed`），`stop()` 与之对称。
`livez` / `readyz` **不**因为 KB 不可用而失败。

`_handle_request` 里 `"kb" in request.enabled_features` 时走独立分支，**不**调用
`complete_with_tools`，也不占 `SearchLimiter`：

1. 派发前已有的一次代次检查照旧；KB 分支在**检索前**再查一次代次。
2. 访问门：`knowledge_base.enabled` 为假 → `kb_unavailable`（reason `disabled`）；
   `service.permits(channel_kind=request.channel_kind, user_id=request.message.author.id)`
   为假 → `kb_unavailable`（reason `access`）。两者都只发对应本地文案（`notice_local`），
   **不调模型**，也不泄露目录、分类或命中情况。授权判据只用站点稳定 `author.id`。
3. `result = await service.search(request.user_text)`；检索结束后、调模型前**再查一次代次**。
   - `status != "ok"` → `kb_unavailable`（reason：`unavailable` / `no_results` /
     `disabled`，其中 `no_results` 用 `KB_NO_RESULTS_TEXT`），只发 `notice_local`，不调模型。
4. 命中时：`pending = _pending_turn(request, image_state, blog_state, blog_block=blog_block)`，再拼上
   `"\n\n" + result.text`（KB 数据块永远在**本轮最后一条 `role="user"`** 里），
   随后照旧 `_apply_reply_prefix` → `attach_image`。
5. system 附加说明二选一：`kb` 轮加 `KB_SYSTEM_ADDENDUM`，`search` 轮加
   `MCP_SEARCH_SYSTEM_ADDENDUM`；大区的 `LOBBY_SHARED_SYSTEM_ADDENDUM` 依旧叠加。
   动态 KB 内容**绝不**进 system。
6. `build_messages(..., feature_context=True)`（D-38 的硬预算；带引用博客块时同样置位）。
7. 调模型走普通 `model.complete()`（与普通聊天共用 `_model_gate`），失败按既有
   `ModelError` / 通用异常路径处理（`FAILURE_NOTICE_TEXT`），不写历史。
8. 送达后历史里只提交**不带 KB 数据块与引用博客块**的 `pending`
   （即 `_pending_turn(request, image_state, blog_state)` 的原值）与最终回答，
   与 `/search` 的 `history_context` 处理同一个道理（D-35 / D-43）。
9. 发送、代次与 `mark_handled` 与普通轮次完全一致。

新增稳定 reason（`app.kb_unavailable` 的 `reason` 字段）：`disabled`、`access`、
`unavailable`、`no_results`。

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

评论侧的记忆集成（`CommentRequest.memory_allowed`、只读 `all_user`、绝不请求 `lobby` 或用户
私有文件）见 §35。

### 16.1.1 评论区的图片输入（§20）

评论区识图要两个开关同时成立：`model.vision_enabled` 与
`comments.max_images_per_reply > 0`（`BotApp._comment_vision`）。缺任一条时
`CommentService.image_loader` 与 `_comment_ref_resolver` 都拿不到图床，一根图床请求
都不会发出去，路由器的纯图分支也退回本地提示——行为与加这个功能之前逐字节相同。

名额（`comments.max_images_per_reply`，默认 3）由三处共用，按这个顺序花：

| 顺序 | 图源 | 理由 |
|------|------|------|
| 1 | 触发评论自带的附件（`CommentNode.image.url`） | 用户自己发来的那张，最该被看到 |
| 2 | 触发评论正文里的 `[@10位]` | 他特意引用进来的 |
| 3 | 文章正文里的 `[@10位]` | 背景资料；且会随这篇文章上的每一次回复重复外送 |

`CommentNode.image` 是评论树上解析出的图片附件（`ImageRef | None`）；`has_image` 仍是
「有没有附件」那个问题（图已不存在时为真，此时 `image` 为 None）。
`CommentRequest.image_url` 只带**可读**的地址：图已缺失或没带图都是 None。

路由（`CommentRouter`，构造参数 `vision_enabled`）：

- 有正文 + 可读附件 → 入队，`image_url` 随请求走；
- **纯图**（正文为空、只有一张可读的附件）且视觉开启 → 入队，`reason == "image_only"`。
  它只能以「直接回复机器人评论」的形态出现（@ 需要文字），因此不会扩大应答面；
- 纯图但视觉关闭 / 图已缺失 / 只引用了博客 → 仍是本地 `UNSUPPORTED_MEDIA_TEXT`。

服务（`CommentService`）：

- 取图在模型门之外，失败只降级：有正文时给正文加 `[图片未提供]`（`with_image_marker`），
  纯图且取不到时回一条本地 `IMAGE_UNAVAILABLE_TEXT`（`kind="notice_local"`）且**不调
  模型、不留历史**；
- 图片块挂在最后一条 user 消息上（`attach_image`），顺序为 附件 → 评论引用 → 文章引用；
- **历史拿的是用户自己写的 `[@id]` 原文加图片标记**，不是展开后的正文（D-49）——
  展开内容只属当前轮。

节点与引用语法的权威描述在 `docs/materials/comment-bot.md` §10.2 与 §7.2。

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
5. 不实现博客理解、通用自动联网、长期记忆或站内工具调用。**唯一例外是 §26…§37 的全局记忆
   Beta**：默认关闭、只保存少量稳定条目、正文只落 Markdown，边界由 D-55…D-65 钉住。
   聊天区显式的 `/search <问题>` 是唯一的单轮联网入口：仅在私聊和精确 @ 机器人的大厅消息中生效，
   由模型决定是否调用
   已绑定的 Exa `web_search_exa`，每轮最多一次；评论区永不获得该入口。**图片理解仅在
   `model.vision_enabled` 为真时提供**，且只把当前轮那一张图取回内存转交模型：不落
   SQLite、不写日志、不写文件、不进 `ContextManager` 历史。默认关闭。不转发 SVG。
6. 不调用站内管理接口，不执行代码，不访问服务器文件。
7. 大区共享链的持久化只存 `message_id -> thread_root_id`：**不存** `author.id`、用户名、
   参与者名单、任何正文；发言者一律从实时 DTO 取。日志里也不得出现用户名或用户 id。
8. 直接引用正文只进当前轮，绝不写进 `ContextManager` 历史（D-7 仍然有效）。

## 20. `core/vision.py`

图片输入的全部实现。**图片字节只存在于内存**：不落 SQLite、不写日志、不写文件、
不进 `ContextManager` 历史。`model.vision_enabled` 为假时本模块不被调用。

```python
IMAGE_MIME_ALLOWLIST: tuple[str, ...]   # ("image/png", "image/jpeg", "image/gif", "image/webp")

def sniff_image_mime(data: bytes) -> str | None
    # 只按 magic bytes 判定：PNG 89 50 4E 47 0D 0A 1A 0A；JPEG FF D8 FF；
    # GIF "GIF8"；WEBP data[0:4]==b"RIFF" and data[8:12]==b"WEBP"。
    # 其余（含 SVG、空串、截断字节）一律 None。
def build_data_url(data: bytes, mime: str) -> str          # "data:<mime>;base64,<...>"
def build_image_part(data_url: str) -> dict[str, Any]      # {"type":"image_url","image_url":{"url":...}}
def attach_image(messages: list[dict[str, Any]], part: dict[str, Any]) -> None
    # 就地改写**最后一条** role=="user" 的 content：str -> [{"type":"text",...}, part]；
    # 已是 list -> 追加；找不到 user 消息 -> 不动。必须在 _apply_reply_prefix 之后调用。

def with_image_marker(user_text: str, state: str) -> str
    # 给本轮正文加图片标记，聊天与评论共用一份措辞：state=="ok" 且有正文 ->
    # "[图片]\n---\n<正文>"；state=="ok" 且正文为空 -> "[图片]"；state 既不是 "ok"
    # 也不是 "none"（即本来有图但没取到）且有正文 -> "[图片未提供]\n---\n<正文>"；
    # 其余原样返回。纯图且没取到时返回空串：标记没有可依附的内容。

class ImageLoader:
    def __init__(self, client: SiteClient, *, max_bytes: int,
                 logger: logging.Logger | None = None) -> None
    async def load(self, message: ChatMessage) -> tuple[dict[str, Any] | None, str]
    async def load_url(self, url: str) -> tuple[dict[str, Any] | None, str]
```

`load_url` 按 URL 取图并编码，`load` 只是「判断有没有可读的图，然后转调它」。
评论附件的图（§16.1）与 `[@10位]` 引用（§25）走的都是同一条路：嗅探、白名单、
失败降级与日志口径只有一份实现。URL 的形态由调用方负责——`fetch_image` 只放行与
站点完全同源的地址。

`load` 的 `reason` 取值（稳定短标识）：

| reason | 含义 | 调用方行为 |
|--------|------|-----------|
| `"none"` | 没有可读的图（`image is None` 或 `image_missing`） | 按现状处理，不加标记、不记日志 |
| `"ok"` | 已取到并编码，第一个返回值是内容块 | 附到当前轮；历史记 `[图片]` |
| `"host_not_allowed"` | 目标与站点不同源 | 降级 |
| `"too_large"` | 超过 `model.max_image_bytes` | 降级 |
| `"http"` | 站点返回非 200（含 404 的私有图、零字节响应体） | 降级 |
| `"network"` | 传输层错误或超时 | 降级 |
| `"unsupported_type"` | 嗅探结果不在白名单（含 SVG、非图片字节） | 降级 |

硬性要求：

- **不信任 DTO 的 `mime_type`**：data URL 里的 mime 一律取自字节嗅探。让远端数据决定
  我们发给模型的内容类型，是把一个可伪造的字段当成事实。
- **不转发 SVG**：图床上传白名单里有 `image/svg+xml`，它是唯一带脚本能力的格式，
  且模型对它的 data URL 也没有有效理解。
- 降级时记一条 `vision.image_unavailable`，字段只有 `reason`、`size_bytes`
  （`too_large` 时补 `limit_bytes`）——三者都在 `LOG_FIELDS` 白名单内。
  **URL 绝不进日志**。`reason == "none"` 不是失败，不记。
- 不做图片缓存：同一条消息被处理一次取一次，重试或补发会重新下载。
- 图片 token **不计入** `context_input_tokens` 预算。聊天区每条消息最多一张消息图
  （另有引用图，各段各自封顶），评论区一轮最多 `comments.max_images_per_reply` 张，
  当前轮永不被裁剪，因此超支有界；这是一个已知且被接受的取舍，不是遗漏。
- 图片标记（`with_image_marker`）说的只有三件事：这一轮**确实带了图**（`[图片]`）、
  本来有图但**没取到**（`[图片未提供]`）、以及什么都没有（不加标记）。三处调用者
  （聊天区 `app._pending_turn`、评论区的本轮正文与历史正文）必须给出同一份措辞，
  否则模型看到的是两种互相矛盾的「这一轮有没有图」。

## 21. 聊天区 MCP 工具合同

MCP 是可选的运行时扩展。`Request.enabled_features: frozenset[str]` 是 Router 到 App
的唯一能力通道；普通聊天和评论保持原有 `ModelClient.complete(messages) -> str`。
`/search` 请求只有在 `mcp.enabled`、`search` feature 及其绑定的 `exa__web_search_exa`
均可用时，才调用可选的 `complete_with_tools(...)`：第一轮 `tool_choice="auto"`，模型不
调用即直接回答；调用时宿主只执行一个合法工具，随后以 `tool_choice="none"` 生成最终回答。
MCP 等待和执行不持有普通模型 semaphore；两次模型请求各自占一个 gate 位。

稳定数据契约位于 `raricy_bot.mcp.contracts`：`ToolDefinition`、`ToolCall`、`ToolExecution`、
`ToolCompletion` 与 `McpProvider`。工具名统一为 `<server>__<tool>`；发现到的工具必须经过
feature binding 白名单后才可见。Exa 适配器只向模型公开 `query`，宿主强制 `numResults`
和查询上限，并把结果清洗为 HTTP(S) 的 `title`、`url`、`snippet`。当前轮每条默认 3000
估算 token，跨轮摘要每条默认 500 token；原始 MCP 内容、assistant tool-call 消息和孤立
`role="tool"` 消息不得进入 SQLite 或内存历史。

多 Key 池见 §22：它包装在同一份 `McpProvider` 协议后面，对 Registry 仍然只是一个 `exa`，
不改变本节任何一条工具名、绑定或限流约束。

MCP Provider/Node/Exa/API Key 故障只将对应 feature 标为不可用；不得改变 `readyz`、`livez`
或普通对话。缺失 `env_from` 环境变量只停用对应服务器，日志仅可记录稳定错误类型，不可
记录变量名映射值、查询、URL、摘要、工具参数或模型正文。

## 22. Exa 授权密钥池（`mcp/pool.py`）

一个逻辑 Provider 包住多个已获授权的 stdio 子进程；对 Registry 仍然只是一个 `exa`，
模型侧工具名、feature 绑定和 `SearchLimiter` 全局串行都不变。设计依据见
`docs/archive/EXA_ACCOUNT_POOL_AND_KB_DESIGN_PLAN.md` §5（已归档，不在版本控制里），
裁决见 D-36 / D-37 / D-40。

### 22.1 配置

```python
POOL_MIN_SLOTS: int = 2      # 代码常量：池的最小槽位数
POOL_MAX_SLOTS: int = 16     # 代码常量：池的硬上限

@dataclass(frozen=True)
class McpAccountPoolConfig:
    child_env: str = "EXA_API_KEY"
    host_envs: tuple[str, ...] = ()
    strategy: str = "round_robin"
    rate_limit_cooldown_seconds: float = 60.0
    transient_cooldown_seconds: float = 30.0
    quota_cooldown_seconds: float = 21600.0
```

`McpServerConfig` 增加字段 `account_pool: McpAccountPoolConfig | None = None`。
**它只保存环境变量名，永远不保存 Key 值**；Key 只在 Provider 构造时从宿主环境读取。

校验（全部在 `config.py` 解析阶段完成，任一条不满足 → `ConfigError`）：

- `account_pool` 与同一服务器的 `env_from` **互斥**（两套凭证来源不得并存）；
- `host_envs` 长度在 `[POOL_MIN_SLOTS, POOL_MAX_SLOTS]`，每项都得是合法环境变量名；
- `host_envs` 内部重复即 `ConfigError`（重复会制造虚假冗余，P1-08）；
- `child_env` 首版必须逐字等于 `"EXA_API_KEY"`；`strategy` 首版必须逐字等于 `"round_robin"`；
- 三个冷却秒数都必须是正数；
- 没有 `account_pool` 时，单 Key 的旧配置解析结果逐字段不变。

### 22.2 槽位状态与错误分类

```python
SLOT_READY = "ready"; SLOT_COOLDOWN = "cooldown"; SLOT_EXHAUSTED = "exhausted"
SLOT_INVALID = "invalid"; SLOT_DISABLED = "disabled"

KIND_OK = "ok"; KIND_RATE_LIMIT = "rate_limit"; KIND_QUOTA = "quota"
KIND_INVALID_KEY = "invalid_key"; KIND_TRANSIENT = "transient"
KIND_REQUEST = "request"; KIND_UNKNOWN = "unknown_upstream"

def classify_exa_error(result: Any) -> str
    # 只读 MCP CallToolResult 的结构化字段与 isError 文本；返回上面七个 KIND 之一。
```

状态迁移（内存态，不落 SQLite，重启后重新探测，D-37）：

| 状态 | 进入条件 | 恢复条件 |
|---|---|---|
| `ready` | 子进程初始化且发现全部 `required_tools` | 调用成功后保持 |
| `cooldown` | `rate_limit` → `rate_limit_cooldown_seconds`；`transient`（5xx、超时、子进程退出）→ `transient_cooldown_seconds` | 冷却到期后由池自己的后台任务重启并探测 |
| `exhausted` | `quota`（402 / `NO_MORE_CREDITS` / `API_KEY_BUDGET_EXCEEDED` / `TEAM_BUDGET_EXCEEDED`） | `quota_cooldown_seconds` 到期后探测，或进程重启 |
| `invalid` | `invalid_key`（401 / `INVALID_API_KEY`） | 本进程不再自动尝试 |
| `disabled` | 环境缺失、Key 值与另一槽位重复、启动合同不匹配、schema 指纹不一致 | 修正配置或环境后重启 |

`classify_exa_error` 的硬性要求：

- 非 `isError` → `KIND_OK`；
- 优先读结构化 `status` / `code` / `tag`（dict 或对象属性，大小写不敏感）；
- 只在 `isError` 内容上做**大小写无关**的固定词匹配：`INVALID_API_KEY` → `invalid_key`；
  `NO_MORE_CREDITS` / `API_KEY_BUDGET_EXCEEDED` / `TEAM_BUDGET_EXCEEDED` → `quota`；
  `RATE_LIMIT_EXCEEDED` / `TOO_MANY_REQUESTS` 或 429 → `rate_limit`；
  `BAD_REQUEST` / `INVALID_ARGUMENT` / `UNPROCESSABLE` 或 400/422 → `request`；
  500/502/503/504 → `transient`；
- 其余一律 `unknown_upstream`：**不得**从普通正文里猜额度耗尽；
- 错误正文不进日志、不进模型上下文。

### 22.3 Provider 合同

```python
class ExaPooledProvider:
    def __init__(self, config: McpServerConfig, *, host_env: dict[str, str] | None = None,
                 redactor: Redactor | None = None, connect_timeout_seconds: float = 10.0,
                 call_timeout_seconds: float = 20.0, required_tools: tuple[str, ...] = (),
                 provider_factory: Callable[..., McpProvider] = StdioMcpProvider,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 startup_timeout_seconds: float | None = None) -> None

    @property
    def available(self) -> bool          # 至少一个槽位 ready
    @property
    def slot_states(self) -> tuple[str, ...]   # 诊断与测试用，顺序即槽位序号
        # `start()` 之前可能有内部态 `"pending"`（已配好 Key、尚未启动）；
        # 启动或停止之后只出现上面五个状态字符串之一。
    async def start(self) -> None
    async def stop(self) -> None
    async def list_tools(self) -> tuple[ToolDefinition, ...]
    async def call_tool(self, tool_name: str, arguments: dict[str, Any], *,
                        should_run: Callable[[], bool] | None = None) -> Any
```

- `config.account_pool` 为 None → 构造时 `ValueError`（由 `mcp/runtime.py` 保证不会发生）。
- 槽位按 `host_envs` 顺序编号 `0..N-1`；**只有进程内序号**，不写环境变量名、不写 Key 指纹。
- 环境变量缺失 → 该槽位 `disabled`（不启动子进程）；Key 值与更早槽位重复 → 该槽位
  `disabled`，稳定原因 `duplicate_secret`；**全部槽位都不可用时** `start()` 抛
  `MissingEnvironmentError`（让 `McpManager` 停用整台服务器而不做无意义重连）。
- Key 值在任何子进程启动前全部注册进 `Redactor`（经 `StdioMcpProvider.resolve_environment`）。
- `start()` 并发启动槽位（总时长受 `startup_timeout_seconds` 约束，缺省取
  `connect_timeout_seconds * 2`），单个槽位失败只标该槽位，不抛出。
- `list_tools()`：向每个 `ready` 槽位取工具定义，规范化（`json.dumps(sort_keys=True)`）
  后的 schema 指纹必须与首个槽位一致；不一致的槽位转 `disabled`；返回**一份**定义元组。
  每个 `ready` 槽位都必须发现全部 `required_tools`，否则该槽位转 `disabled`。
- `call_tool()` 的有界故障转移（D-36）：
  1. 持池内锁选择槽位，保证「选择 + 推进游标」原子；
  2. 从游标之后选下一个 `ready` 槽位，游标在**开始一次真实尝试**时推进；
  3. 每次尝试前重查 `should_run`，为假立即抛 `McpCallCancelled`；
  4. 成功 → 立即返回原始 MCP 结果；
  5. `rate_limit` / `quota` / `invalid_key` / `transient` → 更新该槽位状态，尝试下一个；
  6. `request` / `unknown_upstream` → **不轮换**，把原始结果原样交回 Registry；
  7. 同一逻辑调用里每个槽位最多尝试一次，尝试次数不超过当次可用槽位数；
  8. 全部槽位失败：最后一次是超时 → 抛 `McpCallTimeoutError`；最后一次是异常 → 抛
     `RuntimeError`；否则返回最后一次的错误结果（Registry 统一映射为
     `search_unavailable` / `search_timeout`）。
- 池不暴露槽位数、槽位序号或上游原文给模型；模型侧仍然只看到一次
  `exa__web_search_exa` 调用（但一次调用可能产生多个上游请求，见 D-40）。
- `stop()` 先取消全部恢复任务，再并发关闭子进程；重复调用安全。
- 单个槽位故障由池自己的后台恢复任务处理，**不触发** `McpManager` 重连整个逻辑 Provider；
  恢复任务失败按 `transient_cooldown_seconds` 重新排队（有界，不无退避重试）。
- 上一条的实现方式：池声明类属性 `manages_own_recovery = True`，`InMemoryToolRegistry`
  在执行路径上（调用前预检不可用、全部槽位超时、全部槽位异常）据此抑制
  `on_provider_failure` 通知；工具发现失败仍照常通知重连。
- **在执行路径上**，`available` 为假对池只是「当前没有 ready 槽位」的瞬态（例如全部在冷却里），
  因此不触发外层重启；这条只约束运行期调用，不改变 `McpManager.start()` 启动后按
  `not provider.available` 兜底安排重连的既有逻辑 —— 启动那一刻还没有任何上游调用，
  也就不存在需要保住的冷却。
- 池不参与 `livez` / `readyz`（D-34）。

`mcp/contracts.py` 增加：

```python
class McpCallCancelled(Exception):
    """池或 Registry 在尝试前发现本轮已被作废；不得映射为故障。"""

class McpProvider(Protocol):
    ...
    async def call_tool(self, tool_name: str, arguments: dict[str, Any], *,
                        should_run: Callable[[], bool] | None = None) -> Any: ...
```

`StdioMcpProvider.call_tool` 接受并忽略 `should_run`（单进程没有轮换点）。
`InMemoryToolRegistry` 调用 Provider 时传入 `should_run=generation_is_current`；
为兼容只接受两个位置参数的旧替身，Registry 必须先探测签名（与 `model_gate` 同一手法）。
Registry 捕获 `McpCallCancelled` → `generation_cancelled`（DEBUG 级，不发通知）。

### 22.4 装配

`McpManager.__init__` 增加 `pooled_provider_factory: Callable[..., McpProvider] = ExaPooledProvider`。
服务器配置里 `account_pool` 非空时用池工厂，否则用 `provider_factory`（默认
`StdioMcpProvider`）。池工厂额外收到 `required_tools`（该服务器全部 feature binding 的工具名
去重排序）与 `provider_factory`，不得修改 Registry 的绑定、工具名或 `SearchLimiter`。

## 23. Markdown 知识库（`kb/`）

本地、只读、可重建的产品能力，**不属于 MCP**，不触发任何 Exa 代码路径。
设计依据见 `docs/archive/EXA_ACCOUNT_POOL_AND_KB_DESIGN_PLAN.md` §7（已归档，
不在版本控制里），裁决见 D-38 … D-44。

### 23.1 配置

```python
KB_MAX_FILES: int = 20000                 # 代码常量硬上限
KB_MAX_FILE_BYTES: int = 8388608          # 8 MiB
KB_MAX_TOTAL_BYTES: int = 536870912       # 512 MiB
KB_MAX_CHUNK_CHARS: int = 20000
KB_MAX_TOP_K: int = 10
KB_MAX_CONTEXT_TOKENS: int = 32000

@dataclass(frozen=True)
class KnowledgeBaseConfig:
    enabled: bool = False                 # 默认关闭；缺少整个节点时行为逐字节不变
    root_dir: str = "./knowledge"
    access_mode: str = "allowlist"        # "allowlist" | "all_chat"
    allowed_channel_kinds: tuple[str, ...] = ("dm",)
    allowed_user_ids: tuple[str, ...] = ()
    refresh_seconds: int = 60
    max_files: int = 2000
    max_file_bytes: int = 1048576
    max_total_bytes: int = 67108864
    chunk_chars: int = 2400
    chunk_overlap_chars: int = 200
    top_k: int = 6
    max_context_tokens: int = 4000
```

`Config` 增加字段 `knowledge_base: KnowledgeBaseConfig = field(default_factory=KnowledgeBaseConfig)`。

校验：

- `enabled` 必须是布尔；`root_dir` 必须是非空字符串；
- `access_mode` ∈ {`"allowlist"`, `"all_chat"`}；`all_chat` **必须显式写出**，默认是 `allowlist`；
- `allowed_channel_kinds` 非空且是 `{"dm", "lobby"}` 的子集；`all_chat` 下同样受它限制；
- `enabled=true` 且 `access_mode="allowlist"` 时 `allowed_user_ids` 不能为空（否则 `ConfigError`）；
  `allowed_user_ids` 每项必须是非空字符串；
- 所有数量、字节、刷新秒数都是正整数（布尔不算整数）且不超过上面代码常量硬上限；
- `chunk_overlap_chars < chunk_chars`；`top_k <= KB_MAX_TOP_K`；
  `max_context_tokens <= behavior.context_input_tokens` 且 `<= KB_MAX_CONTEXT_TOKENS`。

### 23.2 模块与类型

```python
# kb/models.py
ROOT_CATEGORY: str = "_root"

@dataclass(frozen=True)
class KnowledgeChunk:
    category: str          # 一级目录名；根目录文件为 "_root"
    relative_path: str     # POSIX 风格相对路径；绝不含宿主绝对路径
    heading_path: str      # "H1 > H2"；无标题层级时为 ""
    ordinal: int           # 该文档内的块序号，从 0 开始
    content: str

@dataclass(frozen=True)
class KnowledgeHit:
    chunk: KnowledgeChunk
    score: float

@dataclass(frozen=True)
class KnowledgeSnapshot:
    version: int                       # 从 1 开始，每次成功构建 +1
    chunks: tuple[KnowledgeChunk, ...] # 按 (relative_path, ordinal) 稳定升序
    document_count: int
    total_bytes: int                   # 读取并解码成功的字节数
    skipped_files: int
    skip_reasons: tuple[str, ...]      # 去重排序的稳定原因，不含路径

    @property
    def chunk_count(self) -> int
    @property
    def empty(self) -> bool

class KnowledgeBuildError(Exception):
    def __init__(self, reason: str) -> None   # "root_missing" | "root_unreadable"
                                              # | "too_many_files" | "total_too_large" | "empty"
```

```python
# kb/loader.py
def build_snapshot(root: str, cfg: KnowledgeBaseConfig, *, version: int) -> KnowledgeSnapshot
    # 纯同步、确定性；由调用方放进 asyncio.to_thread。失败抛 KnowledgeBuildError。
```

稳定跳过原因（`skip_reasons` 里只出现这些字符串）：`not_utf8`、`too_large`、
`read_failed`、`symlink`、`escaped_root`、`replaced`。

```python
# kb/index.py
class KnowledgeIndex:
    @classmethod
    def build(cls, snapshot: KnowledgeSnapshot) -> KnowledgeIndex
    def search(self, query: str, *, top_k: int) -> tuple[KnowledgeHit, ...]
```

```python
# kb/service.py
@dataclass(frozen=True)
class KnowledgeResult:
    status: str            # "ok" | "no_results" | "unavailable" | "disabled"
    block_count: int
    text: str              # 已按 max_context_tokens 截断的 [KBn] 数据块；非 ok 时为 ""
    snapshot_version: int

class KnowledgeService:
    def __init__(self, config: KnowledgeBaseConfig) -> None
    async def start(self) -> None           # 首次构建 + 周期刷新；失败只记事件，绝不抛出
    async def stop(self) -> None            # 取消刷新任务；重复调用安全
    @property
    def available(self) -> bool             # 有成功快照
    def permits(self, *, channel_kind: str, user_id: str) -> bool
    async def search(self, query: str) -> KnowledgeResult
```

### 23.3 扫描与路径安全

1. 解析根目录绝对规范路径（`Path.resolve()`）；不存在或不是目录 → `root_missing` / `root_unreadable`。
2. 递归枚举扩展名大小写无关的 `.md` **普通文件**；跳过隐藏目录与隐藏文件
   （名字以 `.` 开头），非 `.md` 静默忽略（不计入 `skipped_files`）。
3. 不跟随符号链接、junction 或 reparse point：`Path.is_symlink()` 为真即跳过并计
   `symlink`；任何解析后逃出根目录的项跳过并计 `escaped_root`。
4. 单个文件在打开前后各查一次大小；大小超过 `max_file_bytes` → 计 `too_large`；
   打开前后大小不一致 → 计 `replaced`（P1-10）。
5. 以 `utf-8-sig` **严格**解码；`UnicodeDecodeError` → 计 `not_utf8`，不猜测本地编码。
6. 文件数超过 `max_files`、累计字节超过 `max_total_bytes` → 整次构建失败抛
   `KnowledgeBuildError("too_many_files")` / `("total_too_large")`，**不产出部分快照**。
7. 扫描顺序按规范化 POSIX 相对路径稳定排序，保证相同目录得到逐字节相同的快照。
8. 快照里只保留相对路径；任何位置都不得出现宿主绝对路径。

### 23.4 分块

确定性的行级解析（不实现完整 CommonMark AST）：

- UTF-8 BOM 由 `utf-8-sig` 去掉；文件开头完整的 `---` front matter 块被丢弃
  （不进正文、不进检索词项；P2-02 首版忽略）。
- 第一个 H1 是文档标题；没有 H1 时用不带扩展名的文件名。
- H1..H6 构成 `heading_path`（父在前、`" > "` 连接，不含 `#`），标题文本同时进入检索词项。
- 优先在标题行与空行边界切块；单块超过 `chunk_chars` 时按字符硬切，块间保留
  `chunk_overlap_chars` 个字符的重叠。
- fenced code block（三反引号或三波浪线）内部不按空行拆开；整块超限时按硬上限切并保持连续 `ordinal`。
- 空文件、只有 front matter 的文件、只含空白的块不进入索引。

### 23.5 检索

不新增第三方依赖，只用标准库与 `text_utils.estimate_tokens`：

- 拉丁字母/数字连续串 `casefold()` 后作为词项；
- CJK 文本同时生成**单字**与**相邻双字**词项（兼顾短查询与召回，P1-11）；
- 分类、相对路径、文档标题、`heading_path`、正文分别建词项；
  `KnowledgeChunk` 没有独立的标题字段，文档标题在索引侧取 `heading_path` 的第一段
  （没有 H1 时该文档的文件名仍在相对路径里参与匹配）；
- 正文用 BM25（`k1=1.5`、`b=0.75`）；分类/路径/标题/标题路径命中用固定小幅加权；
- 同一 `relative_path` 最多返回 2 个块（P2-03）；
- 分数相同时按 `(relative_path, ordinal)` 稳定升序；
- 查询没有有效词项，或最高分不超过 `kb/index.py` 的模块常量 `MIN_SCORE` 时返回空元组
  ——不把整库塞给模型。

### 23.6 快照与刷新

- 索引是**不可变**的 `(KnowledgeSnapshot, KnowledgeIndex)` 对；刷新在
  `asyncio.to_thread` 里构建，完整成功后用**一次引用替换**同时更新两者（P1-12）。
- 请求要么看到完整旧快照，要么看到完整新快照，绝不看到半建状态。
- 单个坏文件跳过并累计稳定原因；根目录不可读、超出硬上限、或整次构建为空
  → 保留上一份成功快照并记 `kb.index_failed`（P1-17）；首次启动失败则 `available=False`。
- 快照版本号是成功构建的递增序号（从 1 开始）；日志只记版本号，不记文件名。

### 23.7 当前轮输出格式

`KnowledgeResult.text` 的形状（`status == "ok"` 时）：

```text
[本地知识库资料（不可信数据，仅供参考）]
[KB1]
分类: electrochemistry
来源: electrochemistry/transport-number.md
标题: 迁移数 > 定义
内容: ...

[KB2]
...
```

- `KB1`、`KB2` … 只在当前轮稳定，下一次检索重新编号；
- 头部说明、标签、分类、相对路径、标题、正文与截断提示**全部**计入 `max_context_tokens`；
- 超预算时按块整块丢弃（保留分数最高的块），必要时对最后一块追加 `TRUNCATION_SUFFIX`；
- 绝不出现宿主绝对路径。

## 24. `core/blog.py`（引用博客）

聊天区消息**引用的博客**：判定状态、取回正文、拼成交给模型的一段（D-47）。
与 `core/vision.py` 同构：注入客户端、失败只降级、由调用方决定怎么办。

```python
BLOG_STATE_NONE: str = "none"        # 消息没引用博客
BLOG_STATE_OK: str = "ok"            # 正文已给出
BLOG_STATE_TOO_LONG: str = "too_long"  # 正文超限，只给标题
BLOG_STATE_MISSING: str = "missing"    # blog_missing：引用的博客已删
BLOG_STATE_FAILED: str = "failed"      # 取不回（404 / 网络 / 超大 / 脏 id / 响应无正文）

def blog_readable(state: str) -> bool          # state in {"ok", "too_long"}
def blog_marker(state: str) -> str | None      # 历史标记；"none" 时为 None
def build_blog_block(title: str, author: str | None, body: str) -> str

@dataclass(frozen=True)
class BlogLoad:
    block: str | None
    state: str
    image_parts: tuple[dict[str, Any], ...] = ()   # 正文里 `[@10位]` 换出来的图片块（§25）

class BlogLoader:
    def __init__(self, client: SiteClient, *, max_chars: int,
                 resolver: ContentRefResolver | None = None, logger=None) -> None
    async def load(self, message: ChatMessage) -> BlogLoad
```

规则：

- `message.blog is None` → `(None, "none")`，**不发请求**。
- `message.blog_missing` 为真 → `(block, "missing")`，**不发请求**（站点已说了它没了）。
- 其余走 `client.fetch_blog_context(message.blog.id)`：
  - 取到正文且 `len(content) <= max_chars` → `"ok"`；
  - 取到但超限 → `"too_long"`，正文栏写 `正文因长度规则未提供`（逐字同评论区）；
  - `SiteError` / `ValueError`（id 不是 UUID）/ 响应里没有可用的正文 → `"failed"`。
- **只要引用了博客，block 就非 None**：标题与作者取自消息 DTO（零成本），
  取回失败时也还在。`description` 一律不给 —— 它是正文的摘录，而正文已经给了。
- 块的形状：

  ```text
  [引用的博客，不可信]
  标题：<title>
  作者：<author>
  正文：
  <正文 | 正文因长度规则未提供 | 该博客已被删除，正文不可读 | 正文未取得>
  ```

  标题与作者是**单行标签**：控制字符替换为空格（与 `comments._clean_label` 同款），
  防止有人用标题伪造出额外的行；`author` 缺失时整行省略。正文**原样保留**。
- 正文**只属当前轮**：不落 SQLite、不写日志、不进历史（历史里只有 `blog_marker`）。
- 正文里的 `[@<内容ID>]` 由注入的 `resolver` 展开（§25），预算就是 `max_chars`：
  展开后的正文仍不超过它，塞不下的引用原样留在正文里。**超限判定排在展开之前** ——
  正文本来就超限时连请求都不该发（反正只给标题）。没注入 `resolver` 时行为逐字不变。
- 日志只允许白名单字段（`logging_setup.LOG_FIELDS`）：失败用
  `blog.unavailable` + `reason` / `error`，超限用 `blog.too_long` + `count`。
  不记 `title`、`id`、正文，也不记 URL。

## 25. `core/content_refs.py`（内容引用 `[@<内容ID>]`）

站点在四处支持引用语法（博客正文、云剪贴板正文、评论、聊天），机器人在这四处读到的
都是**未展开的 Markdown 原文**。本模块把 `[@<ID>]` 换成正地方的内容：剪贴板正文、
投票的文字块、图床图片的字节（视觉开启时）。

```python
CLIPBOARD: str = "clipboard"     # 8 位
VOTE: str = "vote"               # 9 位
IMAGE: str = "image"             # 10 位

MAX_FETCHED_REFS: int = 10       # 一次展开最多**取回**多少条剪贴板/投票
MAX_IMAGE_REFS: int = 3          # 一次展开最多把几张图交给模型

@dataclass(frozen=True)
class ContentRef:
    kind: str        # CLIPBOARD | VOTE | IMAGE
    id: str
    start: int       # 匹配区间（左闭右开），用于按区间切片替换
    end: int
    text: str        # 原文里的完整匹配（含内部空白），预算按它结算

@dataclass(frozen=True)
class ResolvedRefs:
    text: str
    image_parts: tuple[dict[str, Any], ...] = ()
    expanded: int = 0                 # 换掉内容的处数（含失败占位），只用于日志
    image_attempts: int = 0           # 这一趟**试了**几张图（含失败的），供调用方扣名额

def find_refs(text: str) -> list[ContentRef]
def failure_marker(kind: str, content_id: str) -> str
def image_marker(image_id: str) -> str
def format_vote(vote: Vote) -> str

class ContentRefResolver:
    def __init__(self, client: SiteClient, *, max_ref_chars: int,
                 image_loader: ImageLoader | None = None,
                 logger: logging.Logger | None = None) -> None
    max_ref_chars: int                # 单条引用的字符上限；消息正文这条路径拿它当总预算
    async def resolve(self, text: str, *, budget: int,
                      max_images: int | None = None) -> ResolvedRefs
    # max_images：这一段最多能试几张图，省略即 MAX_IMAGE_REFS（3）。多段文本共用
    # 一个名额池时（评论区一轮 N 张，§16.1），调用方逐段传剩余名额，并按返回的
    # image_attempts 扣减——计的是**试了几张**，失败的那张同样占名额。
    # 0 与 image_loader is None 同款：只留标记、一个字节都不取。
```

识别规则（`find_refs`，纯函数）：

- 正则 `\[@\s*([A-Za-z0-9]+)\s*\]`：容忍 ID 两侧空白，**只认 ASCII 字母数字**
  （不含下划线、点、斜杠——比站点博客那条 `\w` 更严，同时让 ID 拼进 URL 注入不进东西）。
- 类型按 ID 长度分流：8 位剪贴板、9 位投票、10 位图片；**长度不在 8–10 之间的不处理**。
- **代码里的引用不展开**：栅栏代码块（``` / ~~~，未闭合时保护到文末）与行内代码
  （成对反引号，落单的到行尾）里的 `[@id]` 保持字面量。站点四处都这样，而且有人
  正是在问「这个语法怎么写」。

`resolve` 的行为：

- **按区间切片替换**，不重扫整串：插入的剪贴板正文里若含同样的 `[@id]`，重扫会把它
  再展开一次（站点的聊天管线专门为这个坑写过注释）。
- 三种类型分别取回：剪贴板 `SiteClient.fetch_clipboard`、投票 `fetch_vote`、
  图片 `ImageLoader.load_url(image_raw_path(id))`。同一个 ID 在一次展开里**只请求一次**；
  缓存**不跨轮**（剪贴板可以被作者改，跨轮缓存要么陈旧要么要引入过期策略）。
- **图片标记不占字符预算**（站点也是直接拼地址、没有正文进来）；图片另受
  `max_images`（默认 `MAX_IMAGE_REFS`）限制，`image_loader is None`（视觉关闭）
  或名额为 0 时只写 `[图床图片 <ID>]`，**一个字节都不下载**。
- **预算决定要不要请求**：`budget` 是展开后正文的长度上限，由调用方给
  （引用博客正文 = `quoted_blog_max_chars`、文章正文 = `comments.article_max_chars`、
  消息与评论正文 = `behavior.content_ref_max_chars`）。预算是零或取回处数已达
  `MAX_FETCHED_REFS` 时，引用**既不请求也不替换**，原样留在文本里。
- 单条展开超过 `max_ref_chars`（或剩余的预算空间）时截断，并追加
  `texts.TRUNCATION_SUFFIX`。
- 失败降级成占位文案：剪贴板 `[剪贴板 <ID> 加载失败]`（**与站点逐字一致**）、
  投票 `[投票 <ID> 加载失败]`、图片 `[图床图片 <ID> 加载失败]`。取不回的引用仍然计入
  `expanded`，占位文案本身**不占预算**（它只有几十字节，且受 `MAX_FETCHED_REFS` 约束）。

硬性要求：

- **展开出来的正文只属当前轮**：不落 SQLite、不写日志、不进 `ContextManager` 历史
  （与 D-28 / D-43 / D-47 同款）。历史里留下的是用户**自己写的** `[@id]` 原文。
- 剪贴板正文是**别人写的**，照旧作为 `role="user"` 数据外送；投票块自带
  `[投票 <ID>，不可信]` 自报标签，标题与选项文案按单行标签清洗控制字符
  （与 `blog._sanitize_label` 同款）。
- 次数上限：一次展开最多取回 `MAX_FETCHED_REFS` 条、最多交出 `MAX_IMAGE_REFS` 张图。
  两者都是**取回与 token** 的独立上限，与字符预算无关。
- 日志只用白名单字段：失败记一条 `content_refs.unavailable`，字段是 `kind`
  （`clipboard` / `vote`）与 `error`（异常类名）。**不记 ID、不记正文、不记 URL**。
  图片的失败由 `ImageLoader` 自己记（`vision.image_unavailable`），这里不重复记。

装配（§16）：

- `BotApp` 构造**两个**实例：`_ref_resolver`（`model.vision_enabled` 为真时拿到
  `image_loader`）供聊天那条路用，`_comment_ref_resolver` 交给 `CommentService`。
  两个实例拿到的 `image_loader` 是同一个对象；差别只在评论侧还要求
  `comments.max_images_per_reply > 0`（§16.1 的 `_comment_vision`）。
- 展开在**模型门之外**执行，与取图、取博客并列；展开后的正文随 `_pending_turn`
  与 `_apply_reply_prefix` 一起进当前轮。同一 ID 出现在文章正文与评论正文里会各请求一次
  ——两次 `resolve` 调用各有各的缓存，这是已知且有界的行为。

## 26. `config.py`（长期记忆 `MemoryConfig`）

全局记忆 Beta 的配置合同。**默认关闭**，关闭时行为与升级前逐字节一致（§26.3、D-60）。
产品语义见 `docs/design/GLOBAL_MEMORY_DESIGN.md`（下称「设计」），技术规划见
`docs/design/GLOBAL_MEMORY_IMPLEMENTATION_PLAN.md`（下称「规划」）§3。

### 26.1 字段与默认值

```python
@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = False
    access_mode: str = "allowlist"       # "allowlist" | "all"
    allow_user_list: tuple[str, ...] = ()
    admin_user_list: tuple[str, ...] = ()
    root_dir: str = "./data/memory"
    refresh_seconds: int = 10
    queue_size: int = 20
    max_common_entries_per_scope: int = 64
    max_private_entries_per_user: int = 32
    max_candidates: int = 128
    max_entry_chars: int = 500
    max_file_bytes: int = 262144
    max_operations: int = 512
    common_context_tokens: int = 800
    private_context_tokens: int = 800
    writer_context_tokens: int = 2000
    writer_timeout_seconds: float = 15.0
    auto_capture_available: bool = False
```

`Config` 增加（与 `comments` / `mcp` / `knowledge_base` 同一区域、同一写法）：

```python
memory: MemoryConfig = field(default_factory=MemoryConfig)
```

- 上面每个字段都可由 YAML 覆盖：本节**没有「代码常量」**。本子系统里不可由 YAML 改的量只有
  三处，各在自己小节标注：存储键域前缀（§27.3）、`key` 的长度界与自动提取的置信度门槛（§31.2）。
- `config.example.yaml` 的 `memory:` 段**逐字**照抄规划 §3.1 的 YAML 示例（含其中文注释）。
- 访问判据一律是站点稳定的 `author.id`，不用 username（规划 §1.5）。

### 26.2 校验规则

逐条实现规划 §3.2：

1. `enabled` 与 `auto_capture_available` 必须是 YAML 布尔值；字符串 `"true"` 非法
   （复用现成的 `_bool_flag`）。
2. `access_mode` 只能是 `"allowlist"` 或 `"all"`。
3. 两个用户列表是字符串列表：空串、重复值与非字符串非法（复用现成的 `_text_tuple`）。
4. **仅 `enabled=true` 时**：`access_mode == "allowlist"` 时 `admin_user_list` 必须是
   `allow_user_list` 的子集，否则 `ConfigError`。管理员不能一边被 Beta 门禁拒绝，
   一边又掌握管理入口。
5. `access_mode == "all"` 时保留 `allow_user_list` 的值但**不使用**，便于随时切回灰度模式。
6. 所有计数、容量与 token 字段是正整数（布尔不算整数）；`writer_timeout_seconds` 是正数。
7. **仅 `enabled=true` 时**：`common_context_tokens + private_context_tokens
   <= behavior.context_input_tokens`。
8. **仅 `enabled=true` 时**：`common_context_tokens <= comments.context_input_tokens`。
9. **仅 `enabled=true` 时**：`max_entry_chars <= behavior.max_input_chars`。
10. **仅 `enabled=true` 时**，`root_dir` 的三条路径约束：不得等于 `storage.db_path`；
    不得位于 `knowledge_base.root_dir` 内；也不得包含 `knowledge_base.root_dir`
    ——否则记忆会被 `/kb` 再次扫描并外送。判定用 `os.path.abspath` + `os.path.commonpath`
    做**路径包含**，不用字符串前缀。违反任一 → `ConfigError`。
11. 关闭时只做类型校验与单字段范围校验；第 4、7–10、13 条的交叉约束只在启用时施加
    （把 `behavior.context_input_tokens` 调小，不得影响一个关闭了记忆的部署）。
12. 未知键继续被忽略（沿用 `_section` 的现有行为，**不要**额外加未知键检查）。
13. **仅 `enabled=true` 且 `auto_capture_available=true` 时**（本条不在规划 §3.2 里，是 Task 13
    的修复补入的）：自动提取的写入披露要与回答挤同一条输出消息（§34.4、D-63），因此必须先
    证明空间够用，否则 `ConfigError`：

    ```text
    len(memory_auto_capture_text(memory_id=最长可能的 UM-ID, content="", created=True))
    + max_entry_chars + len(TRUNCATION_SUFFIX)
    < behavior.max_output_chars
    ```

    最长 ID 取「`UM-` 加九位十进制」（本实现是 `UM-999999999`），比 §29 的六位规范多算三位，
    只是把校验卡得更早；真的涨过九位时由 §34.4 的运行期分支接住。

    - **门控与第 11 条同源**（`config.py:1012` 的 `if enabled:` 与它下面两句注释）：没打开自动
      提取的部署不该因为这个组合启动失败——用户侧的 `auto_capture` 是运行时状态，部署没允许时
      它根本打不开。
    - 这条校验的算术依赖两个今天成立、但站在**文案**那一侧的前提：`memory_auto_capture_text`
      的长度只随 `content` 线性增长（`texts.py:443-449`，拼接只有 `+`，没有截断或转义），
      以及「已新增」与「已更新」两种措辞**等长**（`texts.py:412-413`）。将来改这两处文案的人
      必须回头看本条：更长的动作词或任何对正文的加工都会让这里预留的空间变小，而失效的表现是
      **启动报错**，不是悄悄降级。
    - 它只保证「脱敏增长为零」时的余量：`max_entry_chars` 是 codec 对正文字符数的上限，
      看不到 §34.4 里「脱敏把正文变长」那一项。因此运行期仍必须有 `disclosure_no_room`
      分支兜底（D-77）。

`common_context_tokens` 与 `private_context_tokens` 的**唯一**去处是 §33 的分组上限：前者管
`all_user` 与 `lobby` 两个分组**合计**，后者管 `memory_user`；评论侧只用到前者（§35）。它们不改变
整轮预算（`behavior.context_input_tokens` / `comments.context_input_tokens`）的任何账目，只是先给
记忆自己的那一份封顶，因此调小它们只可能让记忆少占、历史多留。

### 26.3 关闭语义

- `enabled=false`（默认）：不创建目录、不读取文件、不构造记忆服务与刷新任务、不向
  `MessageRouter` 注入任何记忆能力；模型请求与帮助文案与升级前逐字节一致（D-60）。
- 记忆不引入任何新的环境变量；本子系统的密钥红线仍是 §3 注册的那三个。

## 27. `memory/models.py`（记忆数据模型）

`memory/` 包的纯类型底座：无 I/O、无网络、不 import `app.py`（裁决 G）。
`MemoryContext.items` 用 `core/context.py` 的 `SupplementalItem`，因此本模块可以
`from ..core.context import SupplementalItem` —— `core/context.py` 不依赖 memory，无循环（§33、D-61）。
字段与语义逐字对照规划 §4.1 / §4.2。

### 27.1 作用域与提案动作

```python
class MemoryScope(StrEnum):
    ALL_USER = "all_user"
    LOBBY = "lobby"
    USER = "user"


class ProposalAction(StrEnum):
    ADD = "add"
    UPDATE = "update"
    NOOP = "noop"
```

### 27.2 条目、候选与结果

```python
@dataclass(frozen=True)
class MemoryEntry:
    memory_id: str
    key: str
    content: str
    pinned: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryCandidate:
    candidate_id: str
    scope: MemoryScope             # 只能是 all_user / lobby
    action: ProposalAction         # add / update
    target_id: str | None
    key: str
    content: str
    created_at: str


@dataclass(frozen=True)
class MemoryProposal:
    action: ProposalAction
    target_id: str | None
    key: str
    content: str
    confidence: float


@dataclass(frozen=True)
class MemoryProposalResult:
    status: str
    proposal: MemoryProposal | None


@dataclass(frozen=True)
class OperationResult:
    status: str
    object_id: str | None
    revision: int


@dataclass(frozen=True)
class MemoryContext:
    common_revision: int
    private_revision: int | None     # 本次没读私有快照时为 None
    items: tuple["SupplementalItem", ...]
```

```python
@dataclass(frozen=True)
class MemoryCaptureResult:
    status: str              # 稳定状态；只有确实写入成功才是 "ok"
    memory_id: str | None    # 成功写入的条目 ID；其余情况 None
    content: str             # 成功写入的正文原文；其余情况空串
    action: ProposalAction   # 本次是新增还是更新；未写入时取 ProposalAction.NOOP
```

`MemoryCaptureResult` 是 §32.3 的 `auto_capture` 返回值：规划 §4.5 只给了类型名，字段在这里
钉死（D-67）。`action` 不是装饰——设计 §6.3 要求披露必须让用户看清「是新增还是更新」，只看
`status` 与 `memory_id` 表达不了（D-63）。

### 27.3 `MemoryTarget` 与 `user_storage_key()`

```python
@dataclass(frozen=True)
class MemoryTarget:
    scope: MemoryScope
    owner_key: str | None = None


def user_storage_key(user_id: str) -> str:
    raw = f"raricy-memory-v1\0{user_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
```

规则：

- `all_user` / `lobby` 的 `owner_key` 必须为 `None`；`user` 的必须存在。违反时抛 `ValueError`
  ——这是编程错误，不是用户可预期失败（后者一律映射成状态，见 27.4）。
- `owner_key` 由宿主根据当前 `author.id` 计算；AI 输出与用户命令都不能提供（D-56 / D-57）。
- `"raricy-memory-v1\0"` 是**代码常量**：固定域分隔前缀，不得由配置改。
- 该哈希只避免原始 ID 出现在文件名里，**不构成加密**。用户 Markdown 一律按敏感数据保护。

### 27.4 稳定状态字符串

下面十个字符串是**稳定合同**（值逐字固定，不得新增、改写或按用途重命名）：

```text
ok | noop | duplicate | not_found | forbidden | unavailable |
invalid_proposal | conflict | full | secret_detected
```

它们由 `memory/models.py` 以 `STATUS_*` 常量导出，**标识符名同样逐字固定**（裁决 R8）：

```python
STATUS_OK: str = "ok"
STATUS_NOOP: str = "noop"
STATUS_DUPLICATE: str = "duplicate"
STATUS_NOT_FOUND: str = "not_found"
STATUS_FORBIDDEN: str = "forbidden"
STATUS_UNAVAILABLE: str = "unavailable"
STATUS_INVALID_PROPOSAL: str = "invalid_proposal"
STATUS_CONFLICT: str = "conflict"
STATUS_FULL: str = "full"
STATUS_SECRET_DETECTED: str = "secret_detected"
```

- 十个常量各自标注 `: str`，与本仓库既有的状态常量写法一致（`core/blog.py:26` 的
  `BLOG_STATE_OK: str = "ok"`）。
- 值**和**名字都只有这一处来源：`memory/` 内外的模块与测试一律从 `memory.models` 导入这些
  常量，不得重新内联字面量、不得另起别名、不得增删第十一个。

| 状态 | 含义 |
|------|------|
| `ok` | 操作已生效 |
| `noop` | 无需变更（含自动提取的低置信度降级） |
| `duplicate` | **Beta 不产生**该状态：幂等重放返回的是第一次的稳定结果，不是 `duplicate`（D-67）。字符串仍留在稳定集合里，值不得改写或删除 |
| `not_found` | 目标条目或候选不存在 |
| `forbidden` | 访问门或 admin 判定拒绝 |
| `unavailable` | 文件不可用（载入失败、原子写失败） |
| `invalid_proposal` | AI 输出不合法（`MemoryProposalResult.status`） |
| `conflict` | 外部编辑或候选目标已被改动，拒绝覆盖 |
| `full` | 触发容量上限，**绝不静默删除**既有条目 |
| `secret_detected` | 脱敏前后不一致（命中已注册密钥），整条拒绝、不保存脱敏版本 |

- 用户可预期的失败一律映射成状态返回，不用异常传递；异常只用于编程错误与被取消。
- 幂等命中返回**第一次**的稳定结果，不改写成 `duplicate`（§30.2）。
- 变更类操作里只有 `ok` 表示确实写了文件；`noop` 与其余状态都不产生写入。

## 28. `memory/access.py`（Beta 接入策略）

```python
class MemoryAccessPolicy:
    def __init__(self, config: MemoryConfig) -> None: ...

    def permits_common(self, user_id: str | None) -> bool: ...
    def permits_private(self, user_id: str | None, channel_kind: str) -> bool: ...
    def permits_commands(self, user_id: str | None, channel_kind: str) -> bool: ...
    def is_admin(self, user_id: str | None) -> bool: ...
```

`enabled` × `access_mode` 的真值表（逐条实现规划 §3.3）：

| 方法 | `enabled=false` | `allowlist` | `all` |
|------|-----------------|-------------|-------|
| `permits_common(user_id)` | `False` | 非空 `user_id` ∈ `allow_user_list` | 恒 `True`（`user_id` 为 `None` 也为真） |
| `permits_private(user_id, kind)` | `False` | 接入门通过 且 `kind == "dm"` | 非空 `user_id` 且 `kind == "dm"` |
| `permits_commands(user_id, kind)` | `False` | 同 `permits_private` | 同 `permits_private` |
| `is_admin(user_id)` | `False` | 接入门通过 且 ∈ `admin_user_list` | 非空 `user_id` 且 ∈ `admin_user_list` |

规则：

- `allowlist` 限制**全部**记忆能力：共同记忆读取、私有记忆读写、记忆命令与自动提取（D-55）。
- `all` 模式对共同记忆允许所有消息作者，但私有记忆仍要求非空稳定 `user_id` **且**频道为 DM。
- `is_admin` 必须**同时**满足接入门与 `admin_user_list`；**绝不**看站点 DTO 的 `is_admin` 字段。
- 评论作者 ID 为空时：`allowlist` 不读任何记忆、`all` 可读 `all_user` 但永不读私有。这条由调用方
  按 `user_id is None` 走 `permits_common` 实现；本模块只需保证 `all` 对 `None` 返回 True、
  对私有一律 False。
- 记忆管理命令只在 DM 执行；大区里的同一文本由 Router 回固定提示，不进入本模块（§34.1）。
- 纯内存、无 I/O；`enabled=false` 时四个方法全 `False`，调用方据此走原有无记忆路径。

## 29. `memory/codec.py`（Markdown 编解码）

`common.md` 与用户文件的严格解析与确定性渲染。**纯同步、无文件 I/O**、不 import `app.py`
（裁决 G）。合同化规划 §5.2 … §5.4。

```python
def parse_common(data: bytes, cfg: MemoryConfig) -> CommonDocument: ...
def render_common(document: CommonDocument) -> bytes: ...
def parse_private(data: bytes, cfg: MemoryConfig) -> PrivateDocument: ...
def render_private(document: PrivateDocument) -> bytes: ...
```

`CommonDocument` / `PrivateDocument` 是新的冻结 dataclass，字段照规划 §5.2 / §5.3 的 front matter
与正文结构。解析失败只给出稳定 reason（建议 `CodecError(reason)`）；
异常字符串与日志**绝不**带原始内容。

### 29.1 front matter 与正文结构

| 文件 | 键 | 说明 |
|------|----|------|
| `common.md` | `schema_version` | 当前只认 `1` |
| | `revision` | 每次成功写入 +1 |
| | `next_all_user_id` / `next_lobby_id` / `next_candidate_id` | 各区 ID 计数器 |
| | `operations` | 幂等键 → `{status, object_id, revision}` |
| 用户文件 | `schema_version` / `revision` / `next_id` | 同上 |
| | `private_enabled` / `auto_capture` | 用户私有设置 |
| | `operations` | 同上 |

- 正文结构：`# 共同记忆` 下的 `## all_user` / `## lobby` / `## candidates` 三个区；用户文件是
  `# 用户私有记忆` 下的条目。条目体是 `- key:` / `- pinned:` / `- created_at:` / `- updated_at:`
  （候选为 `- scope:` / `- action:` / `- target_id:` / `- key:` / `- created_at:`），正文是连续的
  Markdown 引用行。完整示例见规划 §5.2 / §5.3。
- 候选与已生效共同记忆放在**同一个** `common.md`，使批准可以在一次原子文件替换里同时完成
  「移出候选」和「加入生效区」（D-58）。
- ID 形状：前缀 `GM-A-`（all_user）、`GM-L-`（lobby）、`MC-`（候选）、`UM-`（用户私有），后接
  `next_*` / `next_id` 分配的十进制序号；前缀必须与所在区一致。
  **渲染规则（钉死）**：序号按 **6 位零填充**输出（`GM-A-000007`、`GM-L-000004`、`UM-000006`、
  `MC-000010`）；**解析接受任意 ≥1 位宽度**的十进制序号，因此人工改窄或改宽过的文件仍能读回。
- 用户文件不写原始 user ID、username、消息正文、模型回答或来源频道；`operations` 只存最近的
  幂等键、结果 ID 与 revision，不存命令正文。
- `operations` 的键是宿主的 `operation_id`，形状 `<来源>:<message_id>`（示例 `"chat:348921"`；
  显式 `/remember` 用 `"remember:<message_id>"`，见 §32.3）；值是 `{status, object_id, revision}`。

### 29.2 解析

1. 先判 `len(data) > cfg.max_file_bytes` → `too_large`；再做严格 UTF-8 解码 → `not_utf8`。
2. front matter 用 `yaml.safe_load`；`schema_version` 不是 `1` → `bad_schema`，不猜测兼容。
3. 结构、字段类型、ID 前缀与列表边界不合法 → `malformed`；时间必须是 UTC RFC 3339。
4. ID 重复 → `duplicate_id`；key 重复 → `duplicate_key`。
5. 列表长度、映射深度与 `operations` 条数都有界（用 `cfg` 的容量字段与 `cfg.max_operations`）。
6. 正文只接受**连续的 Markdown 引用行**（`> `）；`> ## 标题`、`> ---` 之类仍是正文，
   **不得**被解析成新条目（防伪造，规划 §13.3）。
7. 失败只返回六个稳定 reason：`not_utf8`、`too_large`、`bad_schema`、`malformed`、
   `duplicate_id`、`duplicate_key`。
8. 时间只用于排序与审阅，**不参与授权**。

读取规则（调用方，即 §30 的 `MemoryService`）：每次最多读 `max_file_bytes + 1` 字节，
先判上限，再交给本模块解码。

### 29.3 渲染

- 顺序固定：`all_user` → `lobby` → `candidates`；用户文件内的条目按 ID 升序。
- 同一 document 重复渲染必须**逐字节相同**。
- 同 key 更新时**替换**原条目，不得追加出并存条目。
- `operations` 按稳定顺序输出。

## 30. `memory/service.py`（存储服务）

规划 §4.3 的存储语义：快照、原子写、幂等、刷新。签名逐字合同化，另加裁决 E 的
`private_path`。本模块**不**做 AI 撰写、不做命令解析、不 import `app.py`（裁决 G）。

### 30.1 接口

```python
@dataclass(frozen=True)
class PrivateSettings:
    private_enabled: bool
    auto_capture: bool


class MemoryService:
    def __init__(
        self,
        config: MemoryConfig,
        *,
        now: Callable[[], float] = time.time,
        replace: Callable[[str, str], None] = os.replace,
        redactor: Redactor | None = None,
    ) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def context_for(
        self,
        *,
        user_id: str | None,
        channel_kind: str,
        access: MemoryAccessPolicy,
    ) -> MemoryContext: ...

    async def private_settings(self, user_id: str) -> "PrivateSettings": ...
    def private_settings_cached(self, user_id: str) -> PrivateSettings | None: ...
    def private_path(self, user_id: str) -> str: ...

    async def set_private_enabled(
        self, user_id: str, enabled: bool, *, operation_id: str
    ) -> OperationResult: ...
    async def set_auto_capture(
        self, user_id: str, enabled: bool, *, operation_id: str
    ) -> OperationResult: ...

    async def apply_private_proposal(
        self, user_id: str, proposal: MemoryProposal, *, operation_id: str
    ) -> OperationResult: ...
    async def delete_private(
        self, user_id: str, memory_id: str, *, operation_id: str
    ) -> OperationResult: ...
    async def clear_private(
        self, user_id: str, *, operation_id: str
    ) -> OperationResult: ...

    async def add_common_candidate(
        self,
        scope: MemoryScope,
        proposal: MemoryProposal,
        *,
        operation_id: str,
        access: MemoryAccessPolicy,
        actor_id: str,
    ) -> OperationResult: ...
    async def approve_candidate(
        self, candidate_id: str, *, operation_id: str,
        access: MemoryAccessPolicy, actor_id: str,
    ) -> OperationResult: ...
    async def reject_candidate(
        self, candidate_id: str, *, operation_id: str,
        access: MemoryAccessPolicy, actor_id: str,
    ) -> OperationResult: ...
    async def delete_common(
        self, memory_id: str, *, operation_id: str,
        access: MemoryAccessPolicy, actor_id: str,
    ) -> OperationResult: ...
```

公开只读辅助（测试与上层都要用，命名照此）：

```python
    async def private_entries(self, user_id: str) -> tuple[MemoryEntry, ...]: ...
    async def common_entries(self, scope: MemoryScope) -> tuple[MemoryEntry, ...]: ...
    async def candidates(self) -> tuple[MemoryCandidate, ...]: ...

    async def find_operation(
        self, operation_id: str, *, user_id: str | None = None
    ) -> OperationResult | None: ...
```

- `private_path(user_id)` 是**同步**的只读方法，返回该用户 Markdown 的路径（用
  `user_storage_key` 命名；目录不存在时**不创建**）。它是唯一的路径访问入口（裁决 E / D-65）。
- `private_settings_cached(user_id)` 同样是**同步**的只读方法：**只读内存快照、不做任何 I/O**，
  该用户的快照还没加载或用户未知时返回 `None`（调用方按「未开启」处理）。它是 §34.1 的
  `private_enabled` 回调的唯一数据源（D-67）。
- `find_operation(operation_id, *, user_id=None)` 是幂等键的**公开查询入口**：命中返回那次操作
  第一次的稳定结果（通常是 `ok`，**不是** `duplicate`，D-67），未命中、`operation_id` 为空或
  `enabled=false` 时返回 `None`。`user_id` 非空时先查该用户的私有快照、再查共同快照；为空只查
  共同快照。它服务于 §32.3 的**先查后撰写**：Controller 必须先调它、命中就不许再调 AI
  （写入侧的顺序由本模块自己保证，撰写侧的顺序只能由调用方保证）。
- **目录布局**（规划 §5.1）：`<root_dir>/common.md` 与 `<root_dir>/users/<64位用户存储键>.md`。
  用户文件名是 §27.3 的 `user_storage_key`，原始 user ID 不出现在文件名里。
- `redactor`：**生产装配必须传入**。传 `BotApp` 自有的那个实例即同时覆盖三类机密——它在构造时
  登记密码与 `LLM_API_KEY`，登录成功后又被 `SiteClient` 追加会话 Cookie
  （`site/client.py` 的 `add_secret(cookie)`；注册到日志单例的是另一次调用、另一个实例）。
  它只用于 §30.2 的密钥筛。`None` 表示**不筛**，只允许出现在只读场景与测试里。
- `start()` / `stop()`：`enabled=false` 时**不做任何事**（不建目录、不读文件、不启任务）。
  启用时创建或加载 `common.md`，并启动 `refresh_seconds` 周期的刷新任务；失败只记
  `memory.load_failed` / `memory.refresh_failed` 并保留 unavailable 状态，**绝不抛出**。
- 共同记忆的四个管理 mutation（`add_common_candidate` / `approve_candidate` / `reject_candidate` /
  `delete_common`）额外接收 `access` 与 `actor_id`：每个方法**各自独立**先查
  `access.is_admin(actor_id)`，失败返回 `forbidden`。检查在任何 I/O、任何幂等查询与任何写入
  **之前**完成，因此被拒的调用方不产生任何可观察副作用；失败是稳定状态，不是异常（§27.4、D-60）。
  这是**纵深防御**（§32.3 要求 Controller 与 Service 两层都查；裁决 R14），不是 Controller 那次
  检查的副本——否则 Controller 的一个 bug 或未来的一条旁路就能自行授权一次对共享记忆的写入。
  `context_for` 已按调用传 `access`，这里沿用同一模式，策略因此不进构造函数。
- 批准（`approve_candidate`）的正文来自**当时磁盘上的候选**：外部版本接管后写进 Markdown 的仍是
  那份候选的 key 与正文，密钥筛因此要按同一份基线复查一次（§30.2 覆盖「任何将要写进 Markdown 的
  正文」），命中同样整条拒绝并返回 `secret_detected`。

### 30.2 作用域读取与幂等

规则：

- `context_for` 按作用域取候选条目（`channel_kind` ∈ `"dm"` / `"lobby"` / `"comment"`）：

  | 场景 | `all_user` | `lobby` | 当前用户私有 |
  |------|-----------|---------|-------------|
  | DM | 读 | 不读 | 读（且**同时**满足 `access.permits_private(user_id, "dm")` 与该用户 Markdown 的 `private_enabled`） |
  | lobby | 读 | 读 | **连文件都不读** |
  | comment | 读 | 不读 | **连文件都不读** |

  DM 行的第二个判据是**用户自己的开关**（D-78）：`/memory off` 的回复承诺「之后的私聊里我不会
  再参考你的条目」，所以开关关闭时 `context_for` 读到条目也一条不注入（正常路径，不记
  `memory.context_omitted`）。判定只此一处：开关与条目写在**同一份**用户文件上，「读它」与
  「要不要交出去」是同一个动作，调用方（`app.py` 的 provider）不得在装配层再判一次。

- 返回的 `SupplementalItem.group` 取 `memory_all_user` / `memory_lobby` / `memory_user`；
  `label` 是条目 ID（`GM-A-…` / `GM-L-…` / `UM-…`）；`priority` 由本模块给值（数值是实现细节），
  但必须让 `ContextManager` 能表达 group 相对次序与组内次序：DM 私有优先于 `all_user`，
  lobby 优先于 `all_user`，同组内 pinned 在前、再按 `updated_at` 新到旧。
- **按 token 预算取舍与插入位置不在这里**（裁决 B / D-62）：`context_for` 只按作用域筛选并按
  `priority` 排序返回，取舍由 `ContextManager.build_messages` 决定（§33）。
- 任何失败返回空 items 并记 `memory.context_omitted`，**不抛出**（D-60）。

全部 mutation 方法（`apply_private_proposal`、`set_private_enabled`、`set_auto_capture`、
`delete_private`、`clear_private`、`add_common_candidate`、`approve_candidate`、
`reject_candidate`、`delete_common`）的公共规则：

- 入口**再次**校验 target 与 ID 前缀，不依赖 Router 或 Controller 已经授权。
- 先查 `operations[operation_id]`：命中就返回第一次的稳定结果，**不再改动文件**。返回的是那次
  操作原本的状态（通常是 `ok`），**不是** `duplicate`（D-67）。
- 单进程内用一个 `asyncio.Lock` 串行所有写入（§30.3 的单写者约束）。
- 容量上限（`max_common_entries_per_scope`、`max_private_entries_per_user`、`max_candidates`）
  命中时返回 `full`，**绝不静默删除**已有条目。
- **密钥筛在 Service 的每个 mutation 入口做**（render 与写入之前；不放在 Controller，也不放在
  renderer 里）：拿构造注入的 `redactor` 与任何将要写进 Markdown 的正文对照，脱敏前后不一致时
  **整条拒绝**并返回 `secret_detected`，不保存 `[redacted]` 版本、不落盘、不把命中的字符串写进
  日志（§30.1 的装配要求、§37）。
- `/memory clear` 删除全部私有条目，但保留必要的幂等元数据，因此清空后重放旧命令不会再次执行
  （§32.3、D-59）。

### 30.3 原子写入

一次成功写入是规划 §5.5 的七步：

1. 在目标文件**同目录**创建唯一临时文件。
2. 以独占创建方式打开，写入完整 bytes，flush 后 fsync。
3. 写入前比较当前磁盘摘要与内存快照摘要；不一致时先尝试载入外部版本。
4. 外部版本合法则以它为新基线**重新应用**本次操作；非法则返回 `conflict`，不覆盖。
5. 用 `os.replace` 原子替换正式文件。
6. 完成后**一次引用替换**内存快照。
7. 任一步失败都保留原正式文件与旧快照，并清理临时文件（映射为 `unavailable`）。

- `replace` 由构造注入（默认 `os.replace`），测试注入失败版本。
- **单进程单写者**：一个进程内所有 Markdown 写入串行；Beta **不支持**多个进程同时写同一目录，
  部署文档必须写明单副本约束。

### 30.4 刷新、缓存与外部编辑

- `common.md` 按 `refresh_seconds` 检查外部编辑，完整解析成功后替换快照。
- 用户文件**惰性加载** + 有界 LRU 快照缓存；缓存淘汰只释放内存，**不删文件**。
- 用户文件在缓存 TTL 到期后的**下一次访问**检查摘要，不为所有用户起轮询任务。
- 外部文件非法时保留该进程最后一份有效快照；冷启动无有效快照时对应范围 unavailable。
- 日志只记 `scope`、`reason`、`revision` 与数量；**不记用户存储键、路径或正文**（§37）。

## 31. `memory/writer.py`（AI 撰写器）

规划 §4.4 的撰写合同：AI 只负责把来源内容整理成一条候选记忆。

### 31.1 接口

```python
class MemoryModel(Protocol):
    async def complete(self, messages: list[dict[str, str]]) -> str: ...


class MemoryWriter:
    def __init__(
        self,
        model: MemoryModel,
        *,
        model_gate: asyncio.Semaphore,
        timeout_seconds: float,
        max_context_tokens: int,
        max_entry_chars: int,
    ) -> None: ...

    async def propose_private(
        self,
        source_text: str,
        existing: tuple[MemoryEntry, ...],
        *,
        automatic: bool,
    ) -> MemoryProposalResult: ...

    async def propose_common(
        self,
        source_text: str,
        existing: tuple[MemoryEntry, ...],
    ) -> MemoryProposalResult: ...
```

规则：

- `propose_private` 与 `propose_common` 使用**两份不同的静态 system prompt**（中文、无任何插值），
  放在 `memory/prompts.py` 或本模块的模块级常量里。
- **动态来源与已有记忆一律放进 `role="user"`**；`MemoryWriter` **不接受** scope、owner 或路径
  参数——它无法替模型决定权限（D-57）。
- 已有记忆以 `[<ID>] <content>` 形式渲染进 user 消息，受 `max_context_tokens` 约束：
  塞不下的条目**整条丢弃**，不截半句。
- `automatic=True` 只允许 `add` / `update` / `noop`，且只有 `confidence >= 0.85` 才接受；
  低于门槛时返回 `status == "ok"` + `action == "noop"` 的提案（不是错误）。
  显式 `/remember` 不以置信度替代安全校验，但 AI 仍可对一次性内容或凭证返回 `noop`。
- `timeout_seconds` 用 `asyncio.timeout`；`model_gate` 包住模型调用；
  `asyncio.CancelledError` **必须原样传播**（不得吞成 `invalid_proposal`）。
- 超时、非法 JSON、拒答与模型异常统一返回 `invalid_proposal`；**不写正文进日志**。

### 31.2 严格 JSON 合同

AI 只返回一项 JSON，形状逐字如下：

```json
{
  "action": "add",
  "target_id": null,
  "key": "preference.python_version",
  "content": "在 Python 相关回答中，用户偏好使用 Python 3.12 的示例。",
  "confidence": 0.97
}
```

解析规则（逐条实现规划 §4.4）：

1. 可剥离最外层**一个** Markdown JSON 代码围栏；围栏之外还出现其他正文 → 拒绝。
2. 用标准库 `json.loads`，不新增依赖。
3. 必须**恰好**包含 `action`、`target_id`、`key`、`content`、`confidence` 五个字段；
   **未知字段整份拒绝**（`scope`、`owner` 等一律拒绝，不做「忽略未知字段」的宽容解析）。
4. `action ∈ {add, update, noop}`；`automatic=True` 与共同候选都**不允许** `delete`。
5. `target_id` 只能引用**传给模型的那批既有条目**（用 `existing` 的 ID 集合校验）；
   `action == "add"` 时必须是 `null`。
6. `key`：小写 ASCII 字母、数字、`.`、`_`、`-`，长度 **1..64**（**代码常量**，不可由 YAML 改）。
7. `content`：去首尾空白与控制字符、剥掉 Markdown 标题伪装（如开头的 `#`）后，长度不超过
   `max_entry_chars`。
8. `confidence` 是 0..1 的有限数字；**bool 不算数字**，`NaN` / 无穷一律拒绝。
9. `automatic=True` 且 `confidence < 0.85` → `ok` + `noop` 提案。**0.85 是代码常量**，
   不可由 YAML 改。
10. 超时、非法 JSON、拒答、模型异常 → `invalid_proposal`。
11. `noop` 提案的规范化（本节钉死）：`proposal.content` 一律为空串 `""`——不保留原文，
    免得未经保存的正文流进下游展示路径；`status` 仍是 `"ok"`；`key`、`target_id`、`confidence`
    保留模型输出的原值。

## 32. `memory/commands.py` 与 `memory/controller.py`（命令与编排）

### 32.1 命令类型

```python
@dataclass(frozen=True)
class MemoryCommand:
    name: str
    argument: str | None = None
    scope: MemoryScope | None = None


@dataclass(frozen=True)
class MemoryCommandRequest:
    event_id: int | None
    message_id: int
    channel_id: str
    session_key: str
    user_id: str
    command: MemoryCommand


@dataclass(frozen=True)
class MemoryCommandResult:
    status: str
    text: str
    memory_id: str | None = None


def parse_memory_command(text: str) -> MemoryCommand | None: ...
```

### 32.2 命令表与解析

全部命令只在 DM 执行：

```text
/memory status
/memory on
/memory off
/memory auto on
/memory auto off
/memory list
/memory list all_user
/memory list lobby
/memory forget <UM-ID>
/memory clear
/remember <内容>

# 仅 admin_user_list
/memory suggest all_user <内容>
/memory suggest lobby <内容>
/memory candidates
/memory approve <MC-ID>
/memory reject <MC-ID>
/memory delete <GM-A-ID | GM-L-ID>
```

`MemoryCommand` 的字段取值是稳定合同：

| 输入 | `name` | `argument` | `scope` |
|------|--------|-----------|---------|
| `/memory status` | `"status"` | `None` | `None` |
| `/memory on` / `/memory off` | `"on"` / `"off"` | `None` | `None` |
| `/memory auto on` / `/memory auto off` | `"auto_on"` / `"auto_off"` | `None` | `None` |
| `/memory list` | `"list"` | `None` | `None` |
| `/memory list all_user` / `lobby` | `"list"` | `None` | `MemoryScope.ALL_USER` / `LOBBY` |
| `/memory forget <UM-ID>` | `"forget"` | ID 原样 | `None` |
| `/memory clear` | `"clear"` | `None` | `None` |
| `/remember <内容>` | `"remember"` | 内容原文 | `None` |
| `/memory suggest all_user <内容>` | `"suggest"` | 内容原文 | `MemoryScope.ALL_USER` / `LOBBY` |
| `/memory candidates` | `"candidates"` | `None` | `None` |
| `/memory approve <MC-ID>` / `/memory reject <MC-ID>` | `"approve"` / `"reject"` | ID 原样 | `None` |
| `/memory delete <GM-A- 或 GM-L- ID>` | `"delete"` | ID 原样 | `None` |

`/memory list` 的两种形式语义不同（无参数是查看自己的私有条目，带 scope 是列已生效共同记忆），
见 §32.3。

解析规则（`parse_memory_command`，纯函数）：

- 只在**消息开头**生效，大小写不敏感；`/memoryx`、`/memoryfoo`、正文中间的 `/memory`、
  未知子命令都不命中（返回 `None`，交给后续普通路径）。
- 命令名与参数之间必须有空白。
- `/memory forget` 缺参数时返回一个**解析成功但 `argument is None`** 的命令（由上层回固定用法），
  **不要**返回 `None`（那会让它变成普通聊天）。
- ID 参数做前缀校验（`UM-` / `MC-` / `GM-A-` / `GM-L-`）：非法 ID 在解析阶段即按用法错误处理
  （`argument is None`），不进入 AI。空参数与非法 ID 都只回固定用法。

### 32.3 `MemoryController`

```python
class MemoryController:
    def __init__(self, *, service: MemoryService, writer: MemoryWriter,
                 access: MemoryAccessPolicy,
                 auto_capture_available: bool = False) -> None: ...

    async def execute_command(
        self, request: "MemoryCommandRequest"
    ) -> "MemoryCommandResult": ...

    async def auto_capture(
        self,
        *,
        user_id: str,
        message_id: int,
        source_text: str,
    ) -> "MemoryCaptureResult": ...
```

执行顺序（**固定**，规划 §4.5）：访问门 → 幂等命中 → 读取目标快照 → AI 撰写 → 宿主校验 →
原子写入 → 生成固定用户文案。

规则：

- **构造参数 `auto_capture_available`**（keyword-only，默认 `False`，取值来自部署级
  `MemoryConfig.auto_capture_available`，D-80）：`/memory auto on` 只在它为真时成功
  （`forbidden` + §36 的专用文案），自动提取也只在它为真时运行（§34.4 把它与 Beta 接入门
  并列为**必须**条件）。三个注入依赖都读不到这个配置，装配方（`app.py` 的 `_start_memory`）
  必须传真实取值；漏传时默认 `False` 的表现是**静默**地永远拒绝——没有报错、没有日志。
- 显式 `/remember` 的 `operation_id` 是 `"remember:<message_id>"`（稳定合同）。重复 SSE、
  resync 或崩溃重放先查 Markdown 的 `operations`；命中时**不再调用 AI**，直接返回第一次的
  稳定结果。
- `/memory suggest <scope> <内容>` 只允许 admin：Controller **与** Service 两层都查 `is_admin`；
  目标作用域由命令解析固定，AI 只撰写候选（D-58）。
- `/memory approve` **不再调用 AI**：直接把候选原子移入生效区；候选的目标条目已被改动时返回
  `conflict`，要求重新生成候选，不覆盖新内容。
- `/memory list`（`scope is None`）列出**调用者自己的私有条目**——这就是设计 §6.4 要求的查看入口；
  `/memory list all_user|lobby` 列出**已生效**共同记忆，任何通过 Beta 接入门的用户都能看。
  两种形式都只展示已生效内容；候选只对管理员显示（`/memory candidates`）。
- **首次开启的说明**（设计 §11、D-66）：`/memory on` 与 `/memory auto on` 的**成功回复**，以及
  一次「隐式打开读取」的 `/remember` 成功回复，都必须带上那段简明说明——保存（或将要保存）了
  什么、记忆可能随请求发送给第三方模型、私有记忆只在本私聊使用、如何查看与删除。
  **不加任何持久标记**（不记录「已经说过」）：说明就挂在开启动作的那条回复上。
- `auto_capture` 采用高精度策略：`automatic=True`，只有**成功变更**才返回 `ok`；它不接收模型
  回答、搜索结果、知识库片段、引用正文或图片描述（**签名里就没有这些参数**）。
  `MemoryCaptureResult.content` 是写入后的正文原文，供 §34.4 拼写入披露。
- 命令的分组语义（规划 §7.1）：`/memory on` 启用私有记忆读取（显式 `/remember` 保存成功时也
  自动打开）；`/memory off` 暂停读取并关闭自动提取，但保留已有条目；`/memory clear` 删除全部
  私有条目且保留幂等元数据；`/memory auto on` 只在部署允许时成功并隐含启用读取；
  `/reset` **不清理**任何长期记忆。
- 用户可预期的失败一律映射成稳定状态（§27.4），不抛异常；日志只记稳定状态与数量，
  **不记正文、key、命令参数或用户 ID**（§37）。
- 用户可见文案一律取自 `texts.py`（§36）；成功文案必须展示**实际保存的正文与条目 ID**
  （设计 §11、规划 §8.1），不得只回「已记住」。

## 33. `core/context.py`（记忆的预算拼装）

`SupplementalItem` 与 `build_messages` 的新参数合同化规划 §6.1 / §6.2。类型定义在
`core/context.py`；`memory/models.py` 可以 import 它，反向不成立（裁决 A / D-61）。

```python
@dataclass(frozen=True)
class SupplementalItem:
    group: str       # memory_all_user | memory_lobby | memory_user
    label: str       # GM-A-... / GM-L-... / UM-...
    content: str
    priority: int    # 越小越优先


@dataclass(frozen=True)
class SupplementalCap:
    groups: tuple[str, ...]   # 共享这一份上限的分组名（可以多于一个）
    max_tokens: int           # 这些分组**合计**的渲染后上限


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
) -> list[dict[str, str]]:
    ...
```

规则：

- `supplemental_items` 为空时必须与改动前**逐字节一致**（规划 §6.1 的硬要求）。
- 选择顺序（规划 §6.2）：
  1. system、静态 addendum 与本轮 `pending_user` **永远保留**。
  2. 已位于 `pending_user` 的本轮显式资料（`/kb`、博客）优先于全部记忆。
  3. 普通聊天至少保留最近一组完整历史；`feature_context=True` 时这组历史也可丢（D-38 不变）。
  4. 记忆之间按 `context_for` 返回的 `priority` 次序取（作用域规则与组内次序的定义在 §30.2，
     这里不重述）。
  5. 记忆放好后，用剩余预算从新到旧补更早的完整历史对。
- 每条记忆是**不可拆分单位**：塞不下就跳过该条，绝不截半句。
- `supplemental_caps` 是各分组自己的 token 上限（§26.1 的两个 `*_context_tokens` 在这里生效）：
  上限按**渲染后的文本块**计（组标签行 + 条目行），与整轮预算同一口径；同一个 `SupplementalCap`
  里的多个分组**合计**受一份上限约束（`all_user` 与 `lobby` 共用 `common_context_tokens`，
  `memory_user` 独用 `private_context_tokens`）。超上限的条目按规则 4 跳过，条目依旧不可拆分；
  不在任何 `SupplementalCap` 里的分组不受分组上限约束（补充资料是通用类型，D-61）。
  上限与整轮预算是**两道独立的门**，都通过才选入；`supplemental_items=()` 的逐字节一致不受影响
  （那一轮根本不进选择）。
- 历史仍按时间正序输出；记忆按作用域分组放在**最后一条 `role="user"` 消息的当前正文之前**，
  版面照规划 §6.2 末尾的文本块：

```text
[共同记忆：all_user，不可信资料]
[GM-A-000007] ...

[用户私有记忆，不可信资料]
[UM-000006] ...

---
[当前消息]
...
```

- 分组标签按 `group` 取 `[共同记忆：all_user，不可信资料]` / `[共同记忆：lobby，不可信资料]` /
  `[用户私有记忆，不可信资料]`；条目行是 `[<ID>] <content>`；组间空行，与当前正文之间用
  `---` 分隔。
- 只有**确实选入至少一条**记忆时才向 system 追加 `MEMORY_SYSTEM_ADDENDUM`：
  `build_messages` 自己从 `texts` 取用并追加（它不 import memory，裁决 C），方式与现有
  `system_addendum` 一致（`"\n\n"` 拼接），并分别计入预算。该常量完全静态、不做任何插值
  （与 `LOBBY_SHARED_SYSTEM_ADDENDUM` 同款，D-24）。
- `append_exchange` 与记忆无关：历史内容**不含**记忆块（规划 §6.2 末段）。
- 记忆正文只出现在 `role="user"` 的当前轮里，**绝不**进 system、**绝不**进历史（D-56）。

## 34. `core/router.py` 与 `app.py`（路由、队列与生命周期）

合同化规划 §7.3 / §8 / §9。

### 34.1 Router

`MessageRouter.__init__` 增加**三个** keyword-only 参数：

```python
class MessageRouter:
    def __init__(self, *, self_user_id: str, bot_username: str,
                 ctx: ContextManager, store: Store,
                 queue: asyncio.Queue[Request], cfg: BehaviorConfig,
                 storage: StorageConfig, now: Callable[[], float] = time.time,
                 vision_enabled: bool = False, kb_enabled: bool = False,
                 memory_access: MemoryAccessPolicy | None = None,
                 memory_queue: asyncio.Queue[MemoryCommandRequest] | None = None,
                 private_enabled: Callable[[str | None], bool] | None = None) -> None
```

- 记忆相关的三个参数（`memory_access` / `memory_queue` / `private_enabled`）都为 `None` 时行为与
  今天**逐字节一致**（未注入兼容）：`memory_allowed` 恒 `False`，`private_enabled` 恒 `False`。
- `Request` 增加 `memory_allowed: bool = False`，由 Router 用当前 `message.author.id` 计算；
  门禁关闭或未注入时恒为 `False`。
- `RouteResult.action` 增加 `"memory_queued"`，**不是** `"queued"`：app 不会把它当聊天请求
  处理，终态由记忆 worker 负责（34.3）。

判定顺序（记忆命令在能力命令解析**之前**识别，避免 `/search /remember …` 绕过单能力规则）：

1. `parse_memory_command(user_text)` 命中时（§32.2）：
   - 只在 **DM** 执行；大区里的同一文本返回「请在私聊中管理记忆」的固定本地提示
     （`reply_now`，kind `notice_local`），不调 AI、不入记忆队列。
   - 未通过 Beta 接入门（`access.permits_commands(user_id, channel_kind)`）→ 固定拒绝文案
     （`reply_now`）。
   - `user_id` 为空（拿不到稳定身份）时按未通过门禁处理。
   - 通过 → 构造 `MemoryCommandRequest`（`event_id`、`message_id`、`channel_id`、
     `session_key` 用该 DM 的 session key、`user_id` 用 `message.author.id`、`command`）
     并 `put_nowait` 入 `memory_queue`。
   - 队列满（`asyncio.QueueFull`）→ 与现有 busy 语义一致的 `RouteResult.action == "busy"`
     （文案 `BUSY_NOTICE_TEXT`）。
   - 成功入队 → `RouteResult.action == "memory_queued"`。事件此时已完成去重登记，
     终态由记忆 worker 负责（`mark_handled`，见 34.3）。
2. 普通聊天：`memory_allowed = memory_access.permits_common(message.author.id)`。
3. `/help` 的文案改为调用 `help_text(...)`（§36），参数取 `vision_enabled`、`kb_enabled`、
   `memory_allowed`（当前作者是否可用记忆）与 `private_enabled`（注入的回调，用当前消息的
   `author.id` 求值；回调**必须是同步、无 I/O 的**，未注入或取不到时按 `False`）。
   `BotApp` 把这个回调接到 `MemoryService.private_settings_cached`（§30.1、D-67）。
4. `/reset` 行为完全不变（规划 §9.2）。

### 34.2 App 装配与生命周期

`BotApp.__init__` 新增（规划 §9.1）：`MemoryAccessPolicy`、`MemoryService`、`MemoryWriter`
（主模型构造完成后绑定）、`MemoryController`、独立 `asyncio.Queue[MemoryCommandRequest]`
（容量 `memory.queue_size`）与 `WorkerPool[MemoryCommandRequest]`（**并发 1**）。
测试注入点：`memory_service`、`memory_writer`、`memory_controller` 均可选注入（fake 优先，
避免真实模型与磁盘 I/O）。

启动顺序（规划 §9.2）：在 Store 崩溃恢复与主模型构造**之后**、聊天 worker 与评论服务启动
**之前**：

1. `MemoryService.start()` 创建或加载共同记忆；失败只记稳定错误并保留 unavailable 状态；
2. 构造 `MemoryWriter` 与 `MemoryController`；
3. 启动记忆 worker（并发 1）；
4. 构造 Router 时注入 access policy、memory queue 与 `private_enabled` 回调
   （接到 `MemoryService.private_settings_cached`）；
5. 构造 CommentRouter / CommentService 时注入 memory access、只读 context provider 与
   `common_context_tokens` 上限（§26.1、§35）。

`memory.enabled=false` 时**全部跳过**，且不创建目录（D-60）。

第 1-3 步的软故障兜底（§34.2 与 D-60 要求记忆的失败只记账）与第 4-5 步的**注入决定**必须共享
同一个事实：**记忆 worker 真的在跑**。注入不能只看配置里的 `enabled`——worker 没起来时队列没有
消费者，记忆命令会既没有回复、又永远不落终态，把水位钉住（D-15）。因此实现里用一个
`_memory_armed` 标志（只在 `WorkerPool.start()` 成功之后置真）同时管住这两处；装配失败时 Router
与评论侧拿到的都是 `None`，与 `enabled=false` 逐字同形。这条规则是**硬要求**，不是实现细节。

关闭顺序（规划 §9.4；**本节是权威版本**，§16 / §16.1 里那些更细的既有动作并入本顺序）：

1. 停评论服务与 SSE，停止产生新请求；
2. 停主聊天 worker **和记忆 worker**；
3. 停 `MemoryService` 的刷新任务；
4. 再依次关闭 OpsServer、MCP、KB、模型客户端、SiteClient 与 Store。

**记忆 worker 必须早于模型客户端关闭**，否则在途 AI 撰写会访问已关闭的客户端。

### 34.3 聊天侧读取与记忆 worker

- `_handle_request` 在拼装消息时，若 `request.memory_allowed` 为真，调用
  `service.context_for(user_id=..., channel_kind=..., access=...)`，把返回的 items 传给
  `ctx.build_messages(..., supplemental_items=...)`；取失败时传空元组继续（软故障，D-60）。
- `_dispatch` 增加 `memory_queued` 分支：无事可做（worker 会处理），**不要**当 `queued` 或
  `reply_now` 处理。
- 记忆 worker 的 handler：
  - 调 `MemoryController.execute_command(request)`；
  - 用 `kind="notice_local"` 发送返回文案（**不占**主动通知名额，D-1）；
  - 传 `thread_root_id=None`（记忆命令只在 DM）；
  - 在 `finally` 里 `mark_handled(request.message_id, "done")`，避免水位卡住；
  - handler 抛异常不得终止 worker，但 `mark_handled` 必须在 `finally`。
- `/remember` 的数据流（规划 §8.1）：Router 记录事件并入队 → Controller 检查门与
  `operation_id` → 读当前用户私有条目 → `propose_private(automatic=False)` → 校验 →
  原子 add/update → `notice_local` 展示**实际保存的正文与 ID**。

### 34.4 自动提取（规划 §8.3）

只在**同时**满足以下条件时运行：`memory.enabled`；当前用户通过 Beta 接入门；频道是 DM；
部署配置 `auto_capture_available`；用户私有设置 `auto_capture`；当前消息不是本地命令、
`/search`、`/kb`，也不依赖图片、博客或内容引用资料；当前请求 generation 仍有效。

- 执行位置：**主模型已经生成回答之后、发送回答之前**。
- `MemoryWriter` 只接收用户自己写的**原始正文**与当前私有记忆；**不接收**模型回答、搜索结果、
  知识库片段、引用正文或图片描述。
- 成功变更后先原子提交私有记忆，再给即将发送的回答追加确定性说明：

  ```text
  （已更新私有记忆 UM-000006：在 Python 相关回答中优先使用 Python 3.12。）
  ```

- 发送前为说明**预留输出空间**（裁决 D / D-63）：回答部分最多可占
  `X = behavior.max_output_chars - len(disclosure)`。`truncate_at_paragraph` 会在结果末尾追加
  `TRUNCATION_SUFFIX`（返回值最长可达 `limit + len(TRUNCATION_SUFFIX)`），所以加给它的上限必须
  **再减去** `len(TRUNCATION_SUFFIX)`：
  `limit = X - len(TRUNCATION_SUFFIX)`，`body, _ = truncate_at_paragraph(answer, limit)`，
  再拼 `body + disclosure`
  （`text_utils.truncate_at_paragraph(text, limit) -> tuple[str, bool]`，取第一个返回值）。
  这样 Sender 的第二次截断（按 `max_output_chars`）不会切掉披露。
- 撰写失败、超时、`noop` 或写入失败：原回答照常发送，**不追加**成功说明。
- 记忆提交与站内回复不是一个分布式事务：记忆提交后发送失败时记忆仍然存在（规划 §8.3）；
  该取舍要写进代码注释。
- 披露的措辞必须区分**新增**与**更新**（`MemoryCaptureResult.action`，设计 §6.3 要求用户看清
  「是新增还是更新」），例如 `（已新增私有记忆 UM-000006：…）` / `（已更新私有记忆 UM-000006：…）`。
- 日志只用 `memory.auto_capture` 事件与白名单字段（`memory_id`、`scope`、`status`、`reason`）。
  该事件的 `reason` 只使用下面三个稳定短标识（新增取值必须先改本节，源码级测试会拦）：
  `controller_unavailable`（装配里没有控制器，本轮自动提取就地放弃）、
  `disclosure_no_room`（披露装不下，见下一条）、`internal`（兜底异常）。
  写入成功或撰写失败的那些轮次**不带** `reason`，只带 `status`（必要时带 `memory_id`）。
- **唯一一处「写进去了但用户没被告知」的路径：`disclosure_no_room`（D-77）。** 当
  `limit = max_output_chars - len(披露) - len(TRUNCATION_SUFFIX) - 脱敏增长` 落到 1 以下时，这一轮
  **放弃披露、原回答照常发送**：记忆已经提交（提交发生在这一步之前，无法回退），用户却看不到那句
  说明。此时只留一条 `reason=disclosure_no_room` 的稳定 WARNING；条目本身仍能在 `/memory list`
  里看到、可以删。§26.2 第 13 条的启动期校验只保证「脱敏增长为零」时的余量，所以这条运行期分支是
  **承重的**，不是理论兜底。

## 35. 评论集成（`comments/router.py`、`comments/service.py`）

合同化规划 §10。

- `CommentRequest` 增加 `memory_allowed: bool = False`；**不得**添加 author ID 字段
  （现状如此，合同不变）。
- `CommentRouter` 在仍持有 `CommentNode.author.id` 时算出 `memory_allowed`
  （`access.permits_common(comment.author.id)`），并注入它需要的最小依赖。作者 ID 为空时：
  `allowlist` 模式为 `False`；`all` 模式可以是 `True`（只读 `all_user`）。
- `CommentService._build_model_messages` 仅在 `memory_allowed` 时取 `all_user` 共同记忆，
  放进当前轮的 `pending_user`（与文章块、父评论块同一位置）；**绝不**请求 `lobby` 或任何用户
  私有文件。
- 评论侧的共同记忆同样受 `memory.common_context_tokens` 约束（§26.1、§26.2 第 8 条）：装配层
  把同一份上限交给 `CommentService`（构造参数 `memory_common_tokens`），由 `build_messages`
  在选题时按 `SupplementalCap(("memory_all_user",), …)` 执行；未注入时这一路与升级前逐字节
  相同（`SupplementalCap` 不传，选择行为不变）。**这是 §26.2 第 8 条存在的理由**：评论的整轮
  预算比聊天小，共同记忆不该把它吃光。
- 评论路径**没有** `/remember`、`/memory` 或自动提取。
- `all_user` 块**不写进**评论 `ContextManager` 历史。
- 共同记忆服务失败不改变评论 `alive`，也不改变聊天 `/livez`、`/readyz`（软故障，D-60）。
- 评论只可能使用 `all_user`；帮助文案按记忆是否注入二选一——未注入时与升级前逐字节相同，
  注入后才换成不承诺「没有长期记忆」的披露版（`texts.comment_help_text`，§36）。

## 36. `texts.py`（记忆文案）

为避免 vision × KB × memory 继续组合出更多常量，`/help` 重构为一个函数（规划 §11）：

```python
def help_text(
    *,
    channel_kind: str,
    vision_enabled: bool,
    kb_enabled: bool,
    memory_allowed: bool,
    private_enabled: bool,
) -> str: ...
```

行为：

- `memory_allowed=False`（记忆未启用，或用户未通过 Beta 门）时，**两种 `channel_kind` 的输出都与
  今天的四个常量逐字节相同**：大区段落留在 DM 文本里的原位置，`_HELP_TAIL` 的排布不因新参数而变。
  既有测试必须原样通过，不得改动（裁决 F / D-64）。四个常量可以保留为函数结果或兼容常量。
- `channel_kind` **只在 `memory_allowed=True` 时起作用**：`"lobby"` 变体声明「大区里不会使用
  任何人的私有记忆」，`"dm"` 变体声明「你的私有记忆只在本次私聊中使用」。
- 只有 `memory_allowed=True` 才需要把「没有长期记忆」那句换成披露，因此 `_HELP_TAIL` 仍要拆成
  可组合的两段（拆分只影响启用记忆时的输出，关闭时的输出逐字节不变）。
- `_HELP_TAIL` 里「我重启之后可能会忘记先前聊过什么，没有长期记忆。」按 `memory_allowed`
  条件化：允许时换成如实披露（设计 §11 的七条）：
  1. 机器人存在共同记忆；
  2. 用户可以选择使用私有记忆（`private_enabled` 决定措辞）；
  3. 普通聊天不会被完整保存；
  4. 记忆可能随相关请求发送给第三方模型；
  5. 私有记忆只在该用户私聊中使用；
  6. 用户可以查看、纠正和删除自己的私有记忆；
  7. `/reset` 不等于删除长期记忆。
- `private_enabled` 的两种取值产生**不同措辞**（已开启 / 未开启）。
- 帮助文案仍须能整条塞进站点单条消息上限（现状已满足，不要把长度翻倍）。
- 新增固定文案全部集中在 `texts.py`（标识符由实现决定，语义与触发点见下表），全部中文，
  **不回显宿主路径、原始 user ID 或模型错误正文**：

  | 文案 | 触发点 |
  |------|--------|
  | Beta 拒绝（未通过接入门） | Router 收到记忆命令但 `permits_commands` 为假（§34.1） |
  | 只允许 DM | 大区里出现记忆命令（§34.1） |
  | 用法提示（`/memory` 与 `/remember`） | 空参数或非法 ID（§32.2） |
  | 撰写失败 | `invalid_proposal` / 超时（§31） |
  | 文件不可用 | `unavailable`（§30） |
  | 候选冲突 | `conflict`（§32.3） |
  | 记忆已满 | `full`（§30） |
  | 操作成功 | `ok`，必须展示**实际保存的正文与条目 ID** |
  | 权限拒绝（非管理员） | 通过 Beta 门但不在 `admin_user_list` 的账号调用 `suggest` / `candidates` / `approve` / `reject` / `delete`（§32.2）；与 Beta 拒绝是两个触发点，文案不得合并 |
  | 自动提取未开放 | `/memory auto on` 而 `MemoryConfig.auto_capture_available` 为假（§32.3） |
  | 状态报告 | `/memory status` 的成功回复：私有记忆与自动记忆的开关、私有条目数；不得回 `字段=取值` |
  | 开关确认（四条） | `/memory on`、`/memory off`、`/memory auto on`、`/memory auto off` 的成功回复；两条开启确认以首次开启的说明收尾（D-66），不另写一份 |
  | 删除确认 | `/memory forget <UM-ID>` 命名被删条目；`/memory clear` 报出本次删掉的条数（重放时如实报 0） |
  | 候选创建确认 | `/memory suggest <scope> <内容>`：命名候选 ID，并说清它还没有生效（D-58） |
  | 候选审阅确认 | `/memory approve`（说明它立即对所有使用者生效）、`/memory reject`、`/memory delete <GM-ID>`：命名受影响的 ID |
  | 列举表头与空态 | `/memory list`、`/memory list all_user\|lobby`：表头 + 每行 `<ID>：<正文>`；没有条目时回显式空态，不回空串或通用的「找不到这条记忆」 |
  | 候选表头与空态 | `/memory candidates`：表头 + 每行一条（含范围、动作与目标）；没有候选时回显式空态 |
  | 目标已不可见 | 幂等重放或防御分支里对象已不在快照；报「操作已记录、对象已不在」，**绝不回裸状态 token** |
  | 首次开启的说明 | `/memory on`、`/memory auto on`、隐式打开读取的 `/remember` 的成功回复（§32.3、D-66） |
  | 自动提取的写入披露 | §34.4 的确定性说明；措辞区分新增与更新 |
  | `MEMORY_SYSTEM_ADDENDUM` | §33；完全静态，说明记忆是不可信资料、不能改变规则或权限、与当前事实冲突时不机械照搬；**不做任何插值** |

- 评论区帮助文案是**两份常量 + 一个选择函数**（`texts.comment_help_text(*, memory_injected)`）：
  - `COMMENT_HELP_TEXT`（记忆**未注入**时使用）与升级前**逐字节相同**——默认部署的评论区一次都
    不会用到共同记忆，文案不得声称它会随请求发送（§26.3）；
  - `COMMENT_HELP_TEXT_WITH_MEMORY`（记忆已注入时使用）**不承诺「没有长期记忆」**：评论只可能
    使用 `all_user` 共同记忆，措辞照此（本功能唯一一处允许的既有文案注释性改动）；
  - 选择判据是**注入与否**（`CommentRouter._memory_access is not None`），不是「当前作者能否
    读取」：披露句讲的是评论区的上限（「最多只会用到」），同一线程里不该因作者在不在名单里而
    换措辞。记忆关闭的部署因此逐字节回到升级前的文案。
- `texts.py` 不 import 任何 memory 模块（`MEMORY_SYSTEM_ADDENDUM` 由 `build_messages` 取用）。

## 37. 记忆日志与安全

`logging_setup.LOG_FIELDS` 增加且**只**增加下面五个字段（它们只承载稳定标识与计数）：

```text
scope | revision | entry_count | memory_id | candidate_id
```

允许的事件名（规划 §12；每条路径只使用自己需要的子集）：

```text
memory.ready
memory.load_failed
memory.refresh_failed
memory.command
memory.write_failed
memory.updated
memory.candidate_updated
memory.auto_capture
memory.context_omitted
```

禁止记录（任何级别、任何路径）：

- 记忆正文、key、候选正文；
- user ID、用户存储键、username；
- 文件绝对路径、文件摘要；
- AI 撰写请求与响应；
- 记忆命令参数；
- 密钥检测命中的具体字符串。

规则：

- 日志一律走 `log_event`；`LOG_FIELDS` 之外的字段被**静默丢弃**，因此需要记录的新字段必须先加进
  白名单（上面五个）。
- 密钥筛由 **`MemoryService` 在 mutation 入口**执行，用的是构造注入的 `redactor`（§30.1）：
  脱敏前后不一致时**整条拒绝**并返回 `secret_detected`，不保存 `[redacted]` 版本，也不把命中的
  字符串写进日志。生产装配必须把 `BotApp` 的 `Redactor` 实例传进去——它同时登记了密码、
  `LLM_API_KEY` 与会话 Cookie；`None` 只允许出现在只读场景与测试里。
- 记忆正文只允许出现在三处：目标 Markdown、允许的 `role="user"` 模型请求、面向所属用户的
  明确展示。
- 所有用户内容与记忆正文进入模型时都是 `role="user"`；静态 memory system addendum 不插值；
  模型输出不能授权自身调用文件、网络或 Store（设计 §7.6 的高风险信息一律不进入记忆）。
- 记忆是软故障：任何读取、撰写或写入失败都不得让聊天、评论、`/livez`、`/readyz` 失败（D-60）。
