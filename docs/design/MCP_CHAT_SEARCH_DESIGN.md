# 聊天区 MCP 联网搜索设计与实现规划

> 状态：已按本文实现，部署前需完成目标环境验收
> 适用范围：Raricy 私聊与大厅聊天机器人
> 首版搜索提供方：Exa MCP Server 3.4.1
> 上游站点合同：`docs/materials/chat-bot.md`，本设计不改变站点 API

本文设计一项显式、单轮、可关闭的联网搜索能力。用户通过 `/search` 授权当前一轮问题使用
Exa 搜索；模型再根据问题是否需要时效性或外部资料，决定直接回答还是调用一次 MCP 工具。

本文自身不是当前内部接口合同。实现前必须把最终采用的类型、默认值和裁决同步到
`INTERFACES.md` 与 `DESIGN_DECISIONS.md`，再开始跨模块实现。

---

# 第一部分：总述与架构设计

## 1. 目标

### 1.1 产品目标

首版需要同时满足以下目标：

1. 用户必须用 `/search <问题>` 显式授权联网；普通聊天绝不自动联网。
2. `/search` 只在私聊和大厅聊天中生效；博客评论子系统不识别该命令，也不获得任何工具。
3. 模型看到搜索工具后自行判断是否调用；每轮最多调用一次。
4. 首版只搜索摘要，不抓取网页全文，不执行深度研究。
5. 一次搜索固定最多返回 5 条结果；每条进入当前模型上下文前最多 3000 个估算 token。
6. MCP、Exa、Node 或 API Key 故障只停用联网功能，不影响普通聊天、评论和健康检查。
7. MCP 接入层不依赖 Exa：后续接入其他服务器或工具时，不需要重写 Router 和模型工具循环。

### 1.2 非目标

首版明确不做：

- 普通消息的自动联网判断；
- 博客评论联网；
- `web_fetch_exa` 网页全文抓取；
- `web_search_advanced_exa`、`agent_run` 或任何已废弃 Exa 工具；
- 多次查询改写、并行搜索或搜索后继续抓取；
- 搜索结果、查询词或工具消息的 SQLite 持久化；
- 独立的持久化搜索次数配额；
- 对模型最终回答强制统一的引用格式；
- 运行时通过 `npx` 下载 npm 包；
- 首版的 Streamable HTTP、SSE 或远程 MCP 传输。

## 2. 已锁定的用户行为

### 2.1 命令语义

命令语法为：

```text
/search <问题>
```

匹配规则：

- 忽略 `/search` 本身的大小写；
- 命令必须位于去除机器人精确提及后的消息开头；
- 命令后必须是字符串结尾或空白字符；
- `/searching`、`/search-x` 和正文中间出现的 `/search` 均不是命令；
- 去掉命令及紧随其后的空白后，剩余文本才是交给现有安全检查和模型的 `user_text`；
- 命令只授权当前请求，后续普通消息不会继承搜索能力。

示例：

| 原始场景 | 结果 |
|---|---|
| 私聊 `/search 北京今天有什么新闻` | 当前轮启用 `search` feature |
| 大厅 `@bot /search 北京今天有什么新闻` | 当前轮启用 `search` feature |
| 大厅 `/search 北京今天有什么新闻` | 未精确提及机器人，沿用现有忽略规则 |
| 私聊 `/SEARCH query` | 当前轮启用 `search` feature |
| 私聊 `/search` | 本地返回用法提示 |
| 私聊 `/searching query` | 作为普通聊天正文 |
| 博客评论 `/search query` | 作为普通评论正文，不启用工具 |

`/search` 空参数属于用户主动请求的本地答复，发送类型为 `notice_local`；不调用模型、不启动
MCP 调用，也不消耗正常模型回复额度。

### 2.2 现有本地检查的顺序

Router 在确定消息属于私聊或已精确提及机器人的大厅消息后，按以下顺序处理正文：

1. 识别并剥离 `/search`；
2. 若剥离后为空，返回搜索用法提示；
3. 对剥离后的正文执行 `/help`、`/reset` 等本地命令判断；
4. 对剥离后的正文执行输入长度与密钥/隐藏配置探测；
5. 构造 `Request`，把 `enabled_features=frozenset({"search"})` 放入请求。

因此 `/search 请告诉我 API key` 仍会被现有密钥探测逻辑拦截，问题不会进入模型或 Exa。
`/search /help` 返回本地帮助，`/search /reset` 执行本地重置；本地命令优先于联网能力。

### 2.3 用户告知与授权

`/help` 和使用文档必须明确告知：

- 只有 `/search` 会启用联网；
- 命令后的问题可能被发送给第三方 Exa；
- 输入 `/search` 即代表用户同意当前一轮进行该处理；
- 搜索结果来自互联网，可能不准确，并可能包含恶意或误导性文字；
- 普通聊天和博客评论不会调用 MCP。

不再增加二次确认，否则会破坏单轮命令语义。

## 3. 总体架构

### 3.1 分层

```text
SSE chat message
       |
       v
Chat Router -- parse /search --> Request.enabled_features
       |                              |
       |                              v
       |                         App coordinator
       |                              |
       |                    +---------+---------+
       |                    |                   |
       |                    v                   v
       |             ordinary complete()   ToolOrchestrator
       |                                        |
       |                              model tool decision
       |                                        |
       |                                        v
       |                                  ToolRegistry
       |                                        |
       |                              FeatureBinding(search)
       |                                        |
       |                                        v
       |                                McpProvider(stdio)
       |                                        |
       |                                        v
       |                       exa-mcp-server / web_search_exa
       |                                        |
       +<--------------- final model answer ----+
                                |
                                v
                         Sender and Context
```

架构分为四个新增层次：

1. **`McpProvider`**：管理一种 MCP 传输、服务器生命周期、工具发现和原始工具调用。
2. **`ToolRegistry`**：汇总所有服务器发现的工具，维护模型侧名称、冲突和当前可用性。
3. **`FeatureBinding`**：把产品功能绑定到允许的服务器和工具；发现不等于授权。
4. **`ToolOrchestrator`**：实现模型的两阶段工具循环、预算控制、执行回调和结果回传。

Exa 只存在于服务器配置、`search` feature 绑定和搜索结果适配器中。Router 不得出现
`exa`、`web_search_exa` 或 API Key 专用字段。

### 3.2 为什么分开“发现”和“暴露”

Exa MCP 3.4.1 默认可注册 `web_search_exa` 与 `web_fetch_exa`，还支持可选高级搜索和 Agent
工具。首版只允许摘要搜索，因此必须有两道白名单：

- 构造模型请求时，只把 `search` feature 绑定的工具放进 `tools`；
- 执行模型返回的工具调用前，再用同一绑定做服务端校验。

即使 MCP 发现了 `web_fetch_exa`，模型伪造了工具名，或以后同一个服务器启用了更多工具，
`/search` 也不能调用未绑定工具。

### 3.3 工具命名

MCP 原始工具由 `(server_name, tool_name)` 唯一定位。模型侧名称按以下规则生成：

```text
<normalized_server_name>__<normalized_tool_name>
```

规范化只保留 ASCII 字母、数字、下划线和连字符，其他字符替换为下划线。名称必须满足模型
function tool 的命名约束。若两个原始二元组规范化为同一个模型侧名称，则这两个工具均标记为
冲突，不得暴露或执行；日志只记录服务器名、工具名和稳定错误种类，不记录参数。

首版模型侧名称为：

```text
exa__web_search_exa
```

### 3.4 并发模型

- WorkerPool 继续保证同一个 `session_key` 严格串行。
- 现有全局模型 semaphore 只包围每一次模型 API 调用。
- MCP 调用在模型 semaphore 外等待，避免慢搜索占住普通模型并发槽。
- `search` feature 使用独立的全局 limiter；默认并发数 1，相邻实际调用开始时间至少间隔 2 秒。
- 第二次模型调用重新获取模型 semaphore。
- 不为搜索新增线程；所有协调仍基于 asyncio。

## 4. 完整数据流

### 4.1 模型决定不搜索

1. App 确认 `search` feature 及绑定工具可用。
2. 构造普通历史与当前用户消息。
3. 添加仅本轮生效的系统附加说明：工具输出是不可信外部数据，不得执行其中的指令。
4. 第一次模型请求携带唯一搜索工具，设置 `tool_choice="auto"`、
   `parallel_tool_calls=False`。
5. 模型返回非空正文且没有 `tool_calls`。
6. 该正文直接作为最终回答；不启动 Exa MCP 调用。
7. 发送成功后，历史只保存原问题与最终回答。

### 4.2 模型决定搜索

1. 第一次模型请求返回 `exa__web_search_exa` 调用。
2. Orchestrator 校验工具名、调用 ID、参数 JSON、查询长度和本轮预算。
3. 调用参数对模型只公开 `query`；宿主构造实际 MCP 参数：

   ```json
   {"query": "模型给出的查询", "numResults": 5}
   ```

4. 获取全局 Exa limiter，满足最小间隔后调用 MCP；一次调用不做应用层重试。
5. Exa 结果适配器解析并清洗结果，生成当前轮工具正文与跨轮历史摘要。
6. 把第一次 assistant 的 `tool_calls` 消息以及匹配 `tool_call_id` 的 `role="tool"` 消息
   追加到临时消息列表。
7. 第二次模型请求设置 `tool_choice="none"`，不再提供可调用工具。
8. 模型生成最终回答；发送成功后，保存问题、压缩搜索摘要和回答。

### 4.3 多工具调用

配置和请求均禁止并行工具调用，但不能假定所有 OpenAI 兼容端点都会遵守。若模型一次返回
多个工具调用：

- 按返回顺序选择第一个名称和参数均合法的已绑定调用执行；
- 其余调用不执行，并分别补齐同一 `tool_call_id` 对应的错误结果
  `tool_budget_exhausted`；
- 若没有任何合法调用，则所有调用得到清洗后的拒绝结果；
- 仍只进行一次关闭工具的最终模型请求。

这样既满足“最多实际搜索一次”，又保持 Chat Completions 工具消息配对完整。

### 4.4 工具失败

失败分为两类：

**调用前不可用**：MCP 总开关关闭、feature 关闭、API Key 缺失、服务器未连接、工具未发现、
绑定冲突或已确认模型不支持 tools。App 不调用模型，直接返回本地“联网搜索暂不可用”，类型为
`notice_local`。

**模型选择搜索后的失败**：参数非法、排队超时、MCP 超时、子进程退出、Exa 返回错误、结果
格式失配或零条有效结果。Orchestrator 不重试，把以下一类稳定错误码作为工具结果交给模型：

```text
invalid_arguments
tool_not_allowed
tool_budget_exhausted
search_timeout
search_unavailable
invalid_result
no_results
```

错误结果不包含异常正文、API Key、命令、路径或上游响应。最终模型应明确说明本轮搜索失败，
可以基于已有知识回答，但不能声称已经查到实时资料。

### 4.5 generation 竞态

现有 `/reset` 可以在长耗时请求期间改变会话 generation。搜索路径必须在以下位置检查
`request.generation == context.current_generation(session_key)`：

1. 第一次模型调用前；
2. 第一次模型调用后、执行工具前；
3. MCP 调用返回后；
4. 第二次模型调用后；
5. Sender 调用前；
6. 保存历史前。

任何一次失配都立即丢弃后续工作和已有结果，不发送、不保存。已经发出的 Exa HTTP 请求无法
撤回，但返回内容仍必须丢弃。取消等待 limiter 的请求也不得推进全局“上次调用时间”。

## 5. Exa 搜索适配

### 5.1 固定提供方合同

首版固定：

- npm 包：`exa-mcp-server@3.4.1`；
- transport：stdio；
- 工具：`web_search_exa`；
- 子进程变量：`EXA_API_KEY`、`ENABLED_TOOLS=web_search_exa`、`DEBUG=false`；
- Node：22，且不得低于上游要求的 Node 20；
- 查询参数：`query` 与宿主强制的 `numResults`；
- 不调用 `web_fetch_exa`。

上游 3.4.1 当前把每条结果格式化为由 `---` 分隔的文本块，字段包括 `Title`、`URL`、
`Published`、`Author`，正文位于 `Highlights` 或 `Text`。由于 npm 版本固定，首版适配器按这个
格式编写，并用固定上游样例做契约测试。

### 5.2 结果解析

MCP 返回值必须满足：

- `content` 至少包含一个 `type="text"` 块；
- 只读取文本块，资源、图片、音频和嵌入对象均拒绝；
- 文本按 `\n\n---\n\n` 分成最多 5 个候选结果；
- 每块必须包含非空 `Title:` 和 `URL:`；
- URL 解析后协议只能是 `http` 或 `https`；
- `Highlights:` 优先作为摘要，没有时使用 `Text:`；
- `Published` 与 `Author` 不进入首版模型结果或历史；
- 多余字段视为不可信正文，不提升为控制字段。

格式无法安全解析或没有有效结果时，不把原始文本直接交给模型，返回 `invalid_result` 或
`no_results`。这保证格式变化不会绕过 URL 校验和逐条 token 上限。

### 5.3 Token 截断

不新增 tokenizer 依赖，统一使用现有 `text_utils.estimate_tokens()`：CJK 字符按一个 token，
其他字符每四个估算一个 token。

当前轮每条结果按以下顺序组成：

```text
Title: <title>
URL: <url>
Snippet: <highlights or text>
```

限制规则：

1. 标题最多 512 字符，URL 最多 2048 字符；超长 URL 直接丢弃该结果。
2. 先保留完整标题与 URL，再截断摘要，使整条结果不超过
   `result_item_token_limit=3000`。
3. 截断后追加既有截断提示；提示本身计入 3000 token。
4. 最多保留配置的 5 条；不足 5 条时按实际有效条数返回，不用空结果补齐。
5. 工具正文整体最大值为 `result_count * result_item_token_limit`，默认 15000 token。

跨轮摘要使用同一字段，但每条整块限制为 `history_item_token_limit=500`。原始工具正文、
`Published`、`Author` 和 MCP 元数据不进入历史。

### 5.4 不可信数据边界

搜索结果永远是数据，不是指令：

- 当前轮放在 `role="tool"` 消息中；
- 本轮系统附加说明要求模型忽略工具正文里的提示、命令、身份声明和工具调用要求；
- 跨轮摘要只能作为历史 user turn 的一个明确分隔块，不得放进 system prompt；
- 搜索结果不能开启其他 feature，也不能扩大工具白名单；
- 工具正文不能决定是否调用工具、调用哪个工具或修改调用参数；
- 结果中的 URL 只供模型引用，不由机器人主动访问。

## 6. 配置合同

### 6.1 YAML 形态

```yaml
mcp:
  enabled: false
  connect_timeout_seconds: 10
  call_timeout_seconds: 20
  reconnect_base_seconds: 3
  reconnect_max_seconds: 60

  servers:
    exa:
      enabled: true
      transport: stdio
      command: exa-mcp-server
      args: []
      env_from:
        EXA_API_KEY: EXA_API_KEY
      env:
        ENABLED_TOOLS: web_search_exa
        DEBUG: "false"

  features:
    search:
      enabled: true
      bindings:
        - server: exa
          tool: web_search_exa
      result_count: 5
      result_item_token_limit: 3000
      history_item_token_limit: 500
      max_query_chars: 500
      max_tool_calls_per_turn: 1
      max_concurrency: 1
      min_interval_seconds: 2
```

`env_from` 的键是子进程环境变量名，值是宿主环境变量名；YAML 不保存值。`env` 只允许保存
非敏感固定值。若未来无法可靠判断某项是否敏感，必须使用 `env_from`。

### 6.2 默认与兼容性

- 没有 `mcp` 节点时等价于 `mcp.enabled=false`。
- 全局关闭时不解析 servers/features 的运行时可用性，不启动任何子进程。
- `mcp.enabled=true` 但缺少 `EXA_API_KEY` 时，机器人仍可启动；只把 `exa` 服务器标记为
  `missing_env`，`/search` 本地返回不可用。
- 普通聊天、评论、站点登录和健康检查不依赖 MCP 初始化结果。
- 开启全局 MCP 不会自动开启未配置或未启用的 feature。

### 6.3 校验规则

配置加载阶段必须拒绝：

- 非 stdio transport；
- 空服务器名、空 command、重复绑定；
- 未知服务器引用；
- 非正数 timeout、token limit、result count 或 concurrency；
- `reconnect_base_seconds > reconnect_max_seconds`；
- `history_item_token_limit > result_item_token_limit`；
- `result_count > 5`；
- `max_tool_calls_per_turn != 1`；
- `search.max_concurrency != 1`；
- `env_from` 或 `env` 中非法的环境变量名；
- 同一个子进程变量同时出现在 `env_from` 与 `env`；
- 在 `env` 中直接出现 `EXA_API_KEY` 或名称含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、
  `COOKIE` 的疑似秘密字段。

首版把只能取 1 的字段保留在配置中，是为了让通用接口形态稳定；不允许运维配置突破已经
评审的产品安全边界。

## 7. 公开接口设计

以下为实施时需要同步到 `INTERFACES.md` 的目标合同。类型名可以按仓库模块布局放置，但签名
和语义不得在实现时自行改变。

### 7.1 Router 请求

```python
@dataclass(frozen=True)
class Request:
    # 保留全部现有字段
    enabled_features: frozenset[str] = frozenset()
```

普通聊天为 `frozenset()`；只有成功解析且通过本地检查的 `/search` 请求包含 `"search"`。

### 7.2 MCP 与工具类型

```python
@dataclass(frozen=True)
class ToolDefinition:
    server_name: str
    tool_name: str
    model_name: str
    description: str
    input_schema: dict[str, Any]

@dataclass(frozen=True)
class ToolCall:
    call_id: str
    model_name: str
    arguments_json: str

@dataclass(frozen=True)
class ToolExecution:
    call_id: str
    content: str
    is_error: bool
    error_kind: str | None
    history_context: str | None

@dataclass(frozen=True)
class ToolCompletion:
    text: str
    used_tools: tuple[str, ...]
    history_context: str | None

class McpProvider(Protocol):
    @property
    def available(self) -> bool: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def list_tools(self) -> tuple[ToolDefinition, ...]: ...
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...

class ToolRegistry(Protocol):
    def tools_for(self, feature_name: str) -> tuple[ToolDefinition, ...]: ...
    def feature_available(self, feature_name: str) -> bool: ...
    async def execute(self, feature_name: str, call: ToolCall) -> ToolExecution: ...
```

`Any` 只存在于 MCP 协议边界。离开 Provider 前必须转换成显式领域类型，不能让任意 MCP 内容
直接穿过 App、Context 或 Sender。

### 7.3 模型接口

保留现有普通接口：

```python
class ModelClient(Protocol):
    async def complete(self, messages: list[dict[str, Any]]) -> str: ...
```

增加可选工具接口：

```python
class ToolCapableModelClient(ModelClient, Protocol):
    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: tuple[ToolDefinition, ...],
        execute: Callable[[ToolCall], Awaitable[ToolExecution]],
        max_tool_calls: int,
        generation_is_current: Callable[[], bool],
    ) -> ToolCompletion: ...
```

普通聊天和评论继续只调用 `complete()`。App 只有在 Request 显式启用 feature 时才调用
`complete_with_tools()`。

### 7.4 模型能力缓存

不能假定任意配置的 OpenAI 兼容端点都支持 tools。首次工具请求若得到确定性的 400/404，且
普通 `complete()` 仍可用，则把当前 `OpenAIModelClient` 实例标记为 `tools_unsupported`，本进程
后续 `/search` 直接返回本地不可用。网络错误、429、5xx 和超时不得缓存为永久不支持。

缓存只在内存中，重启后重新探测；不在启动时额外调用模型做能力探针。

## 8. 生命周期、秘密与日志

### 8.1 API Key

新增运行秘密：

```text
EXA_API_KEY
```

它只能来自宿主环境变量，经 `env_from` 注入子进程。实现时必须：

- 在启动 Exa Provider 前把值登记进 `Redactor`；
- 不在 YAML、日志、SQLite、异常消息、健康检查或用户提示中出现该值；
- 不把 `RARICY_USERNAME`、`RARICY_PASSWORD`、`LLM_API_KEY` 或 Cookie 传给 MCP；
- API Key 缺失或空字符串时不启动 Exa 子进程；
- 用户索取 Exa key 时沿用现有密钥探测与拒绝逻辑。

### 8.2 子进程环境

不得用未经筛选的 `os.environ` 作为子进程环境。允许继承的运行环境只包含 Python MCP SDK
启动 stdio 子进程所必需的平台变量，例如 Windows 的 `PATH`、`SYSTEMROOT`，Unix 的 `PATH`、
`HOME`、`LANG`，再叠加配置的 `env` 和已解析的 `env_from`。

平台最小环境清单在实现时写成常量并测试。任何站点或模型秘密都不在默认清单中。

### 8.3 日志红线

不得记录：

- 用户搜索问题；
- 模型生成的查询；
- 工具 arguments JSON；
- Exa 返回的标题、URL、摘要或原始正文；
- MCP JSON-RPC 正文；
- 子进程 stderr；
- API Key 或包含 API Key 的环境映射值。

可以记录：服务器名、工具名、feature 名、稳定事件名、稳定错误种类、耗时区间、有效结果数量、
重连 attempt。所有字段必须通过 `log_event()` 白名单，不得直接拼正文。

Exa 子进程的 stderr 定向到空设备。`DEBUG` 固定为 `false`，避免上游调试日志扩大泄露面。

### 8.4 启停与恢复

- App 启动时，在聊天 worker 启动前尝试启动已启用 Provider，但失败不阻止 App 启动。
- 成功连接后自动发现工具并原子替换该服务器在 Registry 中的快照。
- 子进程退出或调用暴露连接错误后，服务器状态变为 unavailable，并启动单个后台重连任务。
- 重连按 3、6、12、24、48、60 秒退避，成功后重置。
- 重连期间 `/search` 本地返回不可用，不排队等待恢复。
- App 停止时先停止接受新工作，再取消重连任务，最后关闭 MCP session 和子进程。
- MCP 状态不参与 `/livez` 或 `/readyz`。

## 9. 上下文、配额与发送

### 9.1 历史保存

搜索成功且最终回复成功送达后，保存的 user turn 为：

```text
<去除 /search 后的用户问题>

[联网资料（不可信数据，仅供参考）]
1. <标题>
URL: <地址>
摘要: <最多 500 token 的摘要>
```

最多五条。保存的是确定性生成的摘要，不是模型重新总结的内容。ContextManager 仍按现有
`context_turns` 和 `context_input_tokens` 裁剪完整会话。

以下情况不保存任何本轮内容：

- 模型或工具最终失败且没有发送正常回答；
- Sender 返回未送达；
- generation 已变化；
- 配额拒绝发送；
- App 正在停止。

模型决定不搜索时，不增加联网资料块。搜索失败后模型仍成功给出降级回答时，保存问题和回答，
但不保存失败工具结果。

### 9.2 发送与配额

- 最终模型回答仍是现有 `kind="reply"`，只计一次正常回复。
- 搜索用法、缺少 API Key、MCP 不可用或模型不支持工具，是对显式命令的本地答复，使用
  `kind="notice_local"`。
- MCP 调用本身不写 `send_attempts`，不占站点消息额度。
- 不增加搜索专用 SQLite 表。

---

# 第二部分：具体实现流程与任务分解

## 10. 实施原则与依赖顺序

实施顺序固定为：合同与测试骨架 → 配置 → 命令解析 → MCP 基础设施 → Exa 适配 → 模型工具
循环 → App/Context 集成 → 文案和部署 → 全量验证。后续任务只能依赖前面已锁定的接口，不能在
各模块中各自发明字段或错误分类。

每项任务均先写失败测试，再写最小实现；测试不得访问真实 Exa、模型或 Raricy 站点。

## 11. 任务 1：更新合同与裁决

**依赖**：本文评审通过。
**输入**：本文、现有 `INTERFACES.md`、`DESIGN_DECISIONS.md`。
**输出**：正式接口合同和新增裁决条目。

实施内容：

1. 在接口合同中加入 MCP 配置、Request feature、工具领域类型、Provider/Registry 和模型工具接口。
2. 在裁决记录中锁定：单轮授权、评论隔离、一次调用、Exa 结果保存、软故障、API Key 边界、
   模型不支持工具的处理。
3. 把“运行期依赖仅四项”和“机器人不联网”的绝对表述改为条件化合同。
4. 上游 `chat-bot.md` 不得修改，因为联网是机器人内部模型能力，不是站点 API 变化。

失败处理：发现本文与现有锁定合同存在无法兼容的签名时停止实现，先回改设计和裁决，不允许在
代码里临时兼容两套未评审接口。

测试：文档交叉引用、字段名、默认值、错误种类和路径扫描一致。
完成标准：后续任务不需要再决定公开字段、默认值或失败语义。

## 12. 任务 2：配置与秘密加载

**依赖**：任务 1。
**输入**：锁定配置合同、宿主环境。
**输出**：冻结配置对象、校验器、环境解析结果和示例配置。

实施内容：

1. 增加 `McpConfig`、`McpServerConfig`、`McpFeatureConfig` 和 `ToolBindingConfig`。
2. 未提供 `mcp` 时构造默认关闭配置，保证旧 YAML 可加载。
3. 区分结构错误与运行环境缺失：前者抛 `ConfigError`，后者生成服务器 unavailable 状态。
4. 解析 `env_from` 时登记秘密，但不把值保存进可 repr 的 frozen dataclass。
5. 更新启动环境检查和 `Redactor` 注册清单，加入 `EXA_API_KEY`。

失败处理：YAML 内明文疑似秘密、非法绑定和越界数值使配置加载失败；缺失 `EXA_API_KEY` 只停用
Exa。

测试：默认兼容、所有边界值、对象 repr、不继承秘密、空 API Key、疑似明文秘密、未知绑定。
完成标准：加载示例配置所得默认值与本文逐项一致，任何测试输出均不出现秘密值。

## 13. 任务 3：`/search` 文本解析与 Router

**依赖**：任务 1。
**输入**：去除精确机器人提及后的正文。
**输出**：清洗后的 `user_text`、本地 Action 或带 feature 的 Request。

实施内容：

1. 在纯文本工具层增加无 I/O 的搜索命令解析函数。
2. Router 按第 2.2 节的顺序组合现有本地判断。
3. 只给聊天 Request 增加通用 feature；评论 Router 不导入搜索解析器。
4. 增加搜索用法和不可用文案常量，不在业务代码内散落字符串。

失败处理：任何解析歧义都按普通正文处理；不得因为看起来像命令而丢弃用户内容。

测试：私聊/大厅、大小写、空参数、相邻字符、前后空白、密钥探测、`/help`、`/reset`、评论隔离。
完成标准：只有明确的单轮 `/search` 能产生 `enabled_features={"search"}`。

## 14. 任务 4：通用 stdio MCP Provider

**依赖**：任务 1、2。
**输入**：服务器配置和解析后的最小子进程环境。
**输出**：可启动、发现、调用、关闭和重连的 Provider。

实施内容：

1. 增加官方 Python MCP SDK 依赖 `mcp>=2.2,<3`，不安装 CLI extras。
2. 使用 SDK 的 stdio client 和自动协议协商，连接固定命令。
3. 分别实现连接超时和单次工具调用超时。
4. stderr 指向空设备；stdout 只由 MCP SDK 消费。
5. Provider 内部用锁保护 session 状态；同一服务器只允许一个重连任务。
6. 工具发现成功后生成不可变快照；连接丢失时立即撤销可用快照。

失败处理：启动、发现或调用异常统一映射到稳定内部错误，不向上游传递异常正文。停止必须幂等，
即使初始化只完成一半也能清理子进程。

测试：本地假 stdio server、握手、发现、超时、异常退出、取消、重复 start/stop、重连退避、stderr
不进入 pytest 捕获输出。
完成标准：测试进程结束后没有遗留子进程、task 或未关闭资源警告。

## 15. 任务 5：ToolRegistry 与 feature 白名单

**依赖**：任务 4。
**输入**：一个或多个 Provider 的发现快照、feature bindings。
**输出**：模型可见工具列表和受控执行入口。

实施内容：

1. 登记每个服务器发现的全部工具，但不默认暴露。
2. 生成并校验模型侧名称，拒绝规范化冲突。
3. `tools_for("search")` 只返回已启用、已发现且无冲突的绑定工具。
4. `execute()` 再次验证 feature、模型名、原始二元组和当前 Provider 状态。
5. Registry 原子替换服务器快照，避免重连期间读取半更新状态。

失败处理：绑定工具未发现时 feature unavailable；额外发现工具不影响可用性，也不能执行。

测试：未知绑定、重名、上下线切换、额外 `web_fetch_exa`、伪造模型名、重连快照原子性。
完成标准：无论模型返回什么名称，`search` 只能到达 `exa/web_search_exa`。

## 16. 任务 6：Exa 搜索适配器与 limiter

**依赖**：任务 5。
**输入**：经白名单验证的 ToolCall。
**输出**：清洗后的当前轮工具正文和可选历史摘要。

实施内容：

1. 只接受 JSON object，且公开字段只有字符串 `query`。
2. 校验去空白后非空且不超过 500 字符。
3. 始终覆盖为 `numResults=config.result_count`，不转发模型的额外字段。
4. 用全局 asyncio lock、可注入 monotonic clock 和 sleep 实现串行及最小间隔。
5. 按第 5 节解析 Exa 3.4.1 格式、校验 URL、截断当前轮和历史结果。
6. 工具调用不重试；把 MCP 的 error content 统一映射为 `search_unavailable`。

失败处理：排队取消不计一次调用；超时后释放锁；解析失败不回传原始内容；零结果返回
`no_results`。

测试：强制 5 条、忽略额外参数、0/1/5/超过 5 条、畸形分隔符、非法 URL、超长文本、中文与
ASCII token、并发排序和两秒间隔。
完成标准：任何 ToolExecution 都不可能超过已配置的逐条和总 token 上限。

## 17. 任务 7：模型工具循环

**依赖**：任务 5、6。
**输入**：聊天消息、允许工具、执行回调、generation predicate。
**输出**：ToolCompletion。

实施内容：

1. 把当前 Chat Completions 调用与异常映射复用于普通和工具请求。
2. 第一轮使用 function tools、`tool_choice="auto"`、`parallel_tool_calls=False`。
3. 无 tool call 时接受非空正文并直接完成。
4. 有 tool call 时完整保存 assistant tool-call 结构，为每个 call ID 生成工具响应。
5. 最多执行一个合法工具，其他调用返回预算错误。
6. 第二轮不提供工具并设置 `tool_choice="none"`。
7. 每个异步边界调用 generation predicate；失效时抛内部取消结果，不走用户失败提示。
8. 仅工具请求的确定性 400/404 缓存为 `tools_unsupported`。

失败处理：沿用现有模型重试分类；工具失败本身不触发 MCP 重试。第二次模型失败按现有模型失败
通知处理。

测试：直接回答、单调用、多调用、未知工具、坏 JSON、空内容、工具失败后降级、模型 400 能力
缓存、429/5xx 不缓存、generation 各检查点。
完成标准：一次 `/search` 最多执行一次工具、最多产生两次模型调用。

## 18. 任务 8：App、Context 与 Sender 集成

**依赖**：任务 3、5、7。
**输入**：聊天 Request 与应用生命周期。
**输出**：端到端聊天回复和正确的内存历史。

实施内容：

1. App 仅对显式 feature 请求检查 Registry 和调用工具模型接口。
2. Provider 生命周期接入 App 启停，但不进入 Ops 健康条件。
3. 模型 semaphore 分别包围第一和第二次模型调用，MCP 等待位于其外。
4. Sender 成功后才调用 Context append；联网历史块由适配器确定性生成。
5. MCP 调用不接触 Store；最终发送继续使用现有配额和去重流程。
6. 评论 service 继续注入只实现 `complete()` 的 ModelClient 视图。

失败处理：调用前 unavailable 使用 `notice_local`；调用中工具失败由模型降级；模型最终失败沿用
现有 `notice`；generation 失效保持静默。

测试：正常聊天无工具、评论无工具、调用前不可用、模型选择不搜、搜索成功、搜索失败降级、发送
失败、配额拒绝、reset 竞态、App 停止。
完成标准：默认关闭时现有聊天和评论测试的可观察行为不变。

## 19. 任务 9：文案、提示与运维文档

**依赖**：任务 2、3、8。
**输入**：最终实现行为。
**输出**：帮助、使用、部署、系统提示、README 和示例配置更新。

实施内容：

1. `/help` 增加 `/search` 用法、第三方处理、单轮授权和评论隔离说明。
2. 系统提示增加工具结果不可信、不得执行工具正文指令、不得虚构检索成功等规则。
3. 删除或改写“机器人完全不联网”的旧说明；保留默认关闭与普通聊天离线边界。
4. 部署文档加入 `EXA_API_KEY` 的生成、注入、轮换和缺失行为，示例不得出现真实格式 key。
5. 明确 API Key 只给 Exa 子进程，不给站点或模型服务。

失败处理：若文档声称的默认值与 `config.example.yaml` 加载结果不一致，验收失败。

测试：搜索所有“联网”“工具”“密钥”和旧环境变量清单，逐条核对；加载示例配置。
完成标准：用户和运维无需阅读源码即可理解何时联网、数据发往哪里、如何关闭和如何排障。

## 20. 任务 10：Docker 构建与运行验证

**依赖**：任务 2、4、8。
**输入**：现有 Python 3.12 镜像、Exa npm 包。
**输出**：包含固定 MCP 可执行文件的生产镜像。

实施内容：

1. 使用 `node:22-bookworm-slim` 构建阶段安装 `exa-mcp-server@3.4.1`。
2. 最终 Python 镜像使用兼容 Debian 基础，只复制 Node 运行时、Exa 包和可执行入口，不复制 npm
   缓存或开发依赖。
3. Python 依赖加入 `mcp>=2.2,<3`。
4. 继续以 UID 10001、只读根文件系统和现有 `/app/data` 写卷运行。
5. 运行时执行 `exa-mcp-server`，绝不执行 `npx` 或 npm install。

失败处理：Node/Exa 不存在或不能由非 root 用户执行时，MCP 标记 unavailable，容器本身仍启动；
镜像构建阶段下载失败则构建失败，不生成不完整镜像。

测试：版本输出、非 root 执行、只读 rootfs、无 API Key 软失败、有假 key 能启动 stdio 握手、禁网
环境启动、健康检查和普通聊天回归。
完成标准：容器运行阶段不需要 npm 网络，且 MCP 故障不改变 `/livez`、`/readyz`。

## 21. 测试矩阵

### 21.1 单元测试

- 配置默认、边界和秘密字段；
- `/search` 解析及本地命令优先级；
- 工具名称规范化与冲突；
- feature 绑定双重校验；
- Exa 文本解析、URL 校验和 token 截断；
- limiter 并发与时间；
- 模型工具消息组装及错误分类；
- 历史摘要确定性。

### 21.2 组件测试

- 使用测试进程实现最小 stdio MCP server，验证真实 MCP SDK 通信；
- 使用 OpenAI SDK 的注入 transport 返回工具调用和最终回答；
- 使用假 Sender、Context 和 Store 验证成功后提交语义；
- 捕获日志和数据库，扫描查询、URL、工具参数与秘密哨兵值。

### 21.3 回归测试

- 默认关闭时全部现有聊天测试；
- 评论完整测试，证明没有 `complete_with_tools()` 调用；
- Router、WorkerPool、Context、Sender、Ops 生命周期测试；
- Docker 内完整 pytest，避免把 Windows 临时目录权限错误误判为产品失败。

### 21.4 不允许的测试捷径

- 不访问真实 Exa API；
- 不把真实 API Key 放进 fixture、命令行或 CI 日志；
- 不用 sleep 验证两秒间隔，必须注入时钟和 sleep；
- 不通过放宽日志白名单来观察正文；
- 不因依赖警告而关闭 `filterwarnings=error`。

## 22. 最终验收清单

实现只有同时满足以下条件才算完成：

- [ ] 无 `mcp` 配置时，机器人行为与当前版本一致。
- [ ] 私聊和精确提及的大厅消息可用 `/search`；评论不可用。
- [ ] `/search` 仅授权当前轮，普通消息绝不携带工具。
- [ ] 模型可以不搜索；选择搜索时最多实际调用一次 `web_search_exa`。
- [ ] 宿主强制 `numResults=5`，模型不能扩大数量或调用 `web_fetch_exa`。
- [ ] 每条当前轮结果不超过 3000 估算 token，历史每条不超过 500。
- [ ] 搜索资料只在最终回复送达后以压缩形式进入内存，不写 SQLite。
- [ ] `EXA_API_KEY` 仅来自环境变量，只传给 Exa 子进程，且已注册脱敏。
- [ ] 查询、URL、摘要、工具参数和 MCP 正文不进入日志。
- [ ] MCP/API Key/Node/Exa 故障不影响普通聊天、评论、`/livez` 或 `/readyz`。
- [ ] 搜索期间 `/reset` 后旧请求不发送、不保存。
- [ ] 普通模型并发不被 MCP 搜索等待占用。
- [ ] Docker 运行阶段不调用 npm 或访问 npm registry。
- [ ] 假 MCP、假模型、组件、回归和 Docker 测试全部通过且无 warning。

## 23. 外部合同依据

- Exa MCP 官方仓库与工具列表：<https://github.com/exa-labs/exa-mcp-server>
- Exa MCP 3.4.1 npm 包定义：
  <https://github.com/exa-labs/exa-mcp-server/blob/main/package.json>
- `web_search_exa` 参数和返回格式实现：
  <https://github.com/exa-labs/exa-mcp-server/blob/main/src/tools/webSearch.ts>
- Exa stdio 配置与 `EXA_API_KEY` 示例：
  <https://github.com/exa-labs/exa-mcp-server/blob/main/npm.readme.md>
- 官方 Python MCP SDK：<https://github.com/modelcontextprotocol/python-sdk>

这些外部事实以 `exa-mcp-server@3.4.1` 和 `mcp>=2.2,<3` 为实现基线。升级任一依赖前，必须重新
核对工具名、参数、返回格式、Node 要求、协议兼容性和日志行为，不能只修改版本号。
