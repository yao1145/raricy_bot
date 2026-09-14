# Exa 授权密钥池与 Markdown 知识库设计及实施计划

> 状态：设计草案，实施前必须关闭本文标为 P0 的问题
> 适用范围：Raricy 私聊与大厅聊天机器人；博客评论首版不接入
> 当前代码基线：单个 Exa stdio MCP Provider、显式单轮 `/search`、内存会话历史
> 上游站点合同：`docs/materials/chat-bot.md`；本文不增加或修改任何站点 API

本文为两项能力给出统一设计：

1. 在现有 Exa MCP 搜索后面增加一个授权 API Key 池，用轮询和有界故障转移提高可用性；
2. 从只读目录递归读取 Markdown 文件，以一级文件夹作为分类，通过 `/kb <问题>` 显式检索。

本文前半部分只讲架构、行为、边界与风险；后半部分才给出依赖顺序、文件级分工、测试和交付
门禁。本文本身不是已锁定接口。批准后必须先把采用的合同同步到 `INTERFACES.md`，把裁决追加到
`DESIGN_DECISIONS.md`，再开始实现。

---

# 第一部分：架构设计

## 1. 目标与非目标

### 1.1 产品目标

- `/search` 的用户语义保持不变：显式、单轮、只在聊天区可用，模型每轮最多发起一个逻辑搜索。
- 一个逻辑搜索可在多个**已获授权**的 Exa API Key 之间轮询；某个 Key 明确额度耗尽、失效、
  限流或其子进程故障时，可以有界地尝试下一个 Key。
- API Key 只来自环境变量；YAML、日志、SQLite、用户提示、异常消息和模型上下文都不得出现
  Key 值或可用于反查 Key 的指纹。
- `/kb <问题>` 只检索当前轮；普通聊天不自动读取知识库，评论区首版不读取知识库。
- 知识库只接受 UTF-8 Markdown，递归目录的一级子目录是分类；返回给模型的每段资料带稳定的
  当前轮引用标签、分类、相对路径和标题路径。
- Exa、Node、任一 Key、知识库目录或索引失败，都不得影响普通聊天、评论和站点健康端点。
- 不新增运行时依赖；知识库检索使用 Python 标准库和现有 token 估算器。

### 1.2 明确非目标

- 不承诺或实现绕过 Exa 的账户、调用量、免费额度或其他平台限制；授权与条款门禁见 §6.1。
- 不把多个 Exa Key 暴露成多个模型工具，不让模型选择具体账号。
- 不增加 Exa 搜索次数、余额或账单的本地推测；官方没有给出可靠余额时不得伪造。
- 不切换到 Exa REST API，不启用 `web_fetch_exa`、高级搜索或 Agent。
- 不让 `/search` 与 `/kb` 在同一条消息叠加；首版一次只允许一种显式能力。
- 不解析 PDF、Word、图片、网页、数据库或 Markdown 之外的文件。
- 不做向量嵌入、外部向量库、语义重排、自动摘要或联网补全。
- 不给不同知识库分类配置不同 ACL；首版整个知识库共用一套访问策略。
- 不把知识库正文、索引、搜索词或命中片段写进 SQLite 或日志。
- 不在文件变化时阻塞聊天请求；不保证修改落盘后立即可检索。

## 2. 当前代码事实与改动落点

以下事实来自当前实现，设计不得绕过它们：

| 当前事实 | 代码位置 | 对本设计的约束 |
|---|---|---|
| `mcp.features.search` 被校验为只绑定一个 `web_search_exa` | `config.py::_mcp_feature` | 池必须藏在一个逻辑 Provider 后面，不能堆多个 binding |
| 一个 `StdioMcpProvider` 对应一个子进程和一份环境 | `mcp/stdio.py` | 每个 Key 需要独立子进程，或切 Key 时重启；首版选择独立子进程 |
| `SearchLimiter` 已全局串行搜索并限制相邻开始时间 | `mcp/exa.py`、`mcp/registry.py` | 账号池不得另起绕过全局 limiter 的旁路 |
| Registry 当前把所有 `isError` 结果压成 `search_unavailable` | `mcp/registry.py` | 池必须在 Registry 之前识别可轮换的账号级错误 |
| `/search` 是 Router 中的专用前缀解析 | `text_utils.py`、`core/router.py` | 新增 `/kb` 时需定义冲突和本地命令优先级 |
| 用户可控内容只能进入 `role=user` | `AGENTS.md`、`core/context.py` | Markdown 正文、路径、标题和查询都不能拼进 system |
| `ContextManager` 只存送达后的完整问答对 | `core/context.py`、D-22 | KB 原文只用于当前轮，历史不保存检索块 |
| Docker 根文件系统只读，只挂载 `/app/data` 可写卷 | `Dockerfile`、`docker-compose.yml` | KB 需要独立的只读 bind mount，不应复制进镜像或写入数据卷 |
| `log_event` 丢弃非白名单字段 | `logging_setup.py` | 新增池槽位/索引统计字段时需显式评审白名单 |
| MCP 失败不参与 `/livez`、`/readyz` | D-34、`app.py` | Exa 池与 KB 索引都保持软故障扩展 |

## 3. 总体架构

```text
chat message
     |
     v
MessageRouter
  | parse /search ------------------------------+
  | parse /kb ------------------+                |
  | ordinary chat               |                |
  v                             v                v
Request.enabled_features     KnowledgeService   Tool loop
  |                             |                |
  |                     atomic snapshot          v
  |                             |          InMemoryToolRegistry
  |                             v                |
  |                       ranked KB chunks       v
  |                                              ExaPooledProvider
  |                                           /        |        \
  |                                      slot 0     slot 1     slot N
  |                                      stdio      stdio      stdio
  |                                         \         |         /
  +------------------ role=user context -----+--------+--------+
                             |
                             v
                       model and sender
```

两项能力共享三条原则：

1. Router 只声明当前请求获准使用什么能力，不做 I/O。
2. App 在 worker 内协调 I/O、代次检查、模型调用和送达后提交。
3. 所有外部或文件内容都是不可信数据；静态 system 附加说明只能描述边界，不包含动态值。

## 4. 命令与用户行为

### 4.1 `/kb` 语法

```text
/kb <问题>
```

匹配规则与 `/search` 对齐：命令忽略大小写，必须位于去除机器人精确提及后的消息开头；命令后
必须是结尾或空白。`/kbase`、`/kb-x`、正文中间的 `/kb` 都按普通正文处理。命令被剥离后，
剩余内容继续走现有空输入、本地命令、长度和秘密探测。

| 输入场景 | 结果 |
|---|---|
| 私聊 `/kb 电化学中迁移数是什么` | 当前轮启用 `kb`，检索全部分类 |
| 大厅 `@bot /kb 电化学中迁移数是什么` | 先满足精确提及，再启用 `kb` |
| 大厅 `/kb ...` | 沿用现有规则静默忽略 |
| `/kb` | 本地返回用法，`notice_local`，不调模型 |
| `/kb /help` | 返回帮助；本地命令优先 |
| `/kb /reset` | 执行现有 reset 语义 |
| 评论 `/kb ...` | 首版按普通评论正文，不检索文件 |

### 4.2 能力冲突

首版禁止能力嵌套。剥离一个能力前缀后，如果正文又以独立的 `/search` 或 `/kb` 开头，则本地
返回统一的能力冲突提示，不调用模型、Exa 或知识库。这样 `/search /kb ...` 不会在用户只理解
一套披露时同时把本地资料和查询发送到两个不同边界。

`/search /help`、`/search /reset`、`/kb /help`、`/kb /reset` 仍执行本地命令，以保持现有
“本地动作优先”合同。实现时应把两个解析器组合成通用的“最多一个能力”判定，避免继续堆叠
难以推演的特殊分支。

### 4.3 分类语义

- 知识库根目录本身不算分类。
- `root/<category>/.../*.md` 的 `<category>` 是该文档分类；更深层目录保留在相对路径中。
- 根目录直接放置的 `.md` 归入保留分类 `_root`。
- 分类匹配使用文件夹原名；对检索建立规范化键，但展示时保留原名。
- `/kb` 首版没有单独的分类参数，默认跨分类检索；用户可把分类名写进自然语言问题，路径和分类
  词在评分中获得提升。
- 分类名和相对路径可能展示给提问者并发送给模型，因此不能把机密写入文件夹或文件名。

## 5. Exa 授权密钥池

### 5.1 方案选择

候选方案：

| 方案 | 结论 | 原因 |
|---|---|---|
| 为每个 Key 配一个 MCP server 和 feature binding | 不采用 | 模型会看到多个等价工具，破坏当前单 binding 合同并泄露池结构 |
| 绕过 MCP，直接调用 Exa REST | 不采用 | 重复实现现有 Exa 适配边界，并改变已锁定的依赖与传输选择 |
| 每次请求前重启同一进程并替换环境 | 不采用 | 启动开销大，切换竞态多，失败会放大到整个逻辑 Provider |
| 一个 `ExaPooledProvider` 包装多个 stdio Provider | 采用 | 对 Registry 仍是一个 `exa`，可独立管理槽位状态并保留现有工具合同 |

### 5.2 配置形态

建议在现有 `mcp.servers.exa` 下增加可选池配置；没有 `account_pool` 时，旧的单 Key 配置逐字节
保持现状。

```yaml
mcp:
  servers:
    exa:
      enabled: true
      transport: stdio
      command: exa-mcp-server
      args: []
      env:
        ENABLED_TOOLS: web_search_exa
        DEBUG: "false"
      account_pool:
        child_env: EXA_API_KEY
        host_envs:
          - EXA_API_KEY_1
          - EXA_API_KEY_2
          - EXA_API_KEY_3
        strategy: round_robin
        rate_limit_cooldown_seconds: 60
        transient_cooldown_seconds: 30
        quota_cooldown_seconds: 21600
```

约束：

- `host_envs` 保存的是环境变量名，不是 Key 值；真实值仍只从宿主环境读取。
- `child_env` 首版必须为 `EXA_API_KEY`，`strategy` 首版必须为 `round_robin`。
- 池模式与 `env_from.EXA_API_KEY` 互斥，避免一个逻辑服务器同时出现两套凭证来源。
- 池大小建议 2–8，硬上限 16；空列表、重复环境变量名和超过上限在配置阶段拒绝。
- 启动时读取并注册全部非空 Key 到 `Redactor`，再按值去重；日志只报告总槽位数和可用数。
- 单个环境变量缺失只禁用对应槽位；全部缺失才使 `exa` feature 不可用。
- Docker Compose 必须逐个显式透传变量；不得把多个 Key 拼成一个可被意外打印的命令行参数。

### 5.3 Provider 合同与槽位状态

新增 `ExaPooledProvider`，继续实现既有 `McpProvider`：

```python
class ExaPooledProvider:
    @property
    def available(self) -> bool: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def list_tools(self) -> tuple[ToolDefinition, ...]: ...
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...
```

每个槽位只以进程内序号标识，状态如下：

| 状态 | 进入条件 | 恢复条件 |
|---|---|---|
| `ready` | 子进程初始化且发现目标工具 | 调用成功后保持 |
| `cooldown` | 429、5xx、超时、子进程退出等暂时错误 | 对应冷却到期后重启并探测 |
| `exhausted` | 明确 402 或 `NO_MORE_CREDITS` /预算耗尽 tag | `quota_cooldown_seconds` 到期后探测，或进程重启 |
| `invalid` | 明确 401 / `INVALID_API_KEY` | 本进程不再自动尝试；修正环境后重启 |
| `disabled` | 环境缺失、重复 Key、启动时合同不匹配 | 修正配置或环境后重启 |

状态只存内存，不写 Key、散列、环境变量名或账户标识到 SQLite。重启后重新探测是刻意选择：余额
和 Key 状态属于 Exa 的外部事实，本地持久化容易在充值、月度刷新或管理员调整后变成错误真相。

### 5.4 选择与有界故障转移

一次逻辑工具调用按以下顺序执行：

1. 先经过现有 `SearchLimiter`；池不得绕过全局最小间隔。
2. 从轮询游标之后选择下一个 `ready` 槽位；游标在“开始一次真实尝试”时推进。
3. 成功则立即返回原始 MCP 结果，后续解析仍由现有 `ExaSearchAdapter` 完成。
4. 明确账号级错误时更新该槽位状态并尝试下一个可用槽位。
5. 非账号级输入错误（400、422、工具名错误、参数错误）直接返回给 Registry，不轮换。
6. 每个槽位在同一逻辑调用内最多尝试一次；最多尝试当时可用槽位数，不无限循环。
7. generation 在每次尝试前重查；若 `/reset` 已作废请求，立即返回 `generation_cancelled`。
8. 全部槽位失败时，Registry 仍只得到一个稳定的 `search_unavailable` / `search_timeout`，模型和
   用户都看不到池大小、槽位序号或上游原文。

内部故障转移不增加模型工具调用次数：模型仍只调用一次 `exa__web_search_exa`。但它可能消耗多个
上游请求，必须在运维文档中明确这一成本语义。

### 5.5 错误分类边界

Exa 官方 API 目前用 401 表示无效 Key、402 表示余额或预算耗尽、429 表示限流，并提供
`INVALID_API_KEY`、`NO_MORE_CREDITS`、`API_KEY_BUDGET_EXCEEDED`、`TEAM_BUDGET_EXCEEDED`
等 tag。实现不能假设 stdio MCP 永远保留 HTTP 状态和 tag：固定版本的 MCP 套件可能把它们压成
纯文本 `isError`。

因此错误分类器必须：

- 优先读取结构化 `status`、`code`、`tag`；
- 只在 `isError=true` 的错误内容上做严格、大小写无关的固定词匹配；
- 不把错误正文写日志或传给模型；
- 无法可靠分类时只标为 `unknown_upstream`，不得凭一句普通网页内容判断额度耗尽；
- 用固定的 3.4.1 错误样例做契约测试；若拿不到样例，P0-02 未关闭，不得上线自动轮换。

### 5.6 并发、生命周期与工具发现

- `search.max_concurrency=1` 与 `SearchLimiter` 继续提供全局串行；池状态锁不需要跨网络等待长期
  持有，但要原子选择槽位和推进游标。
- App 启动时并行启动槽位可缩短启动时间，但必须设总启动上限；至少一个槽位可用即认为逻辑
  Provider 可用。
- 工具发现只向 Registry 返回一份定义。每个可用槽位都必须发现同名目标工具；schema 指纹不同的
  槽位标为 `disabled`，不能把不一致结果混在同一池里。
- 一个槽位失败由池自己重启，不触发 `McpManager` 把整个逻辑 Provider 停掉重连。
- 池内无可用槽位时逻辑 Provider 暂时 unavailable；后台只探测已到期的 cooldown/exhausted 槽位。
- App 停止时取消所有槽位恢复任务，再并发关闭子进程；重复 stop 安全。
- 池状态不改变 `/livez`、`/readyz`。

## 6. Exa 合规、额度和秘密边界

### 6.1 实施前合规门禁

Exa 当前条款要求 API 使用遵守技术文档、使用指南和调用量限制，并保留审计与终止权限；官方
团队文档还说明同一 Team 的成员共享该 Team 的限制。官方资料没有明确承诺“为叠加免费额度而
创建多个个人账号”是允许的。

因此本设计的实现对象必须表述为“多个已获授权的 API Key”，而不是“规避免费额度的账号”。
在下列任一证据成立前，P0-01 保持阻塞，账号池不得进入生产：

- Exa 书面确认该部署可轮询这些账号/Key；或
- 这些 Key 来自同一组织依法管理的独立预算，且当前合同明确允许；或
- 需求改为官方 Team、充值、教育/创业额度或其他官方支持的容量方案。

确认记录只保存批准日期、适用账号范围和批准渠道，不保存邮件正文中的 Key。

### 6.2 秘密处理

- Key 只经 `host_envs -> child_env` 注入对应子进程。
- 所有 Key 在任何子进程启动前注册进同一个 `Redactor`。
- 不记录环境变量名，因为名称可能包含邮箱、组织或用途。
- 不记录 Key 的前后缀、长度、哈希或稳定指纹；只记录不跨重启稳定的槽位序号。
- 子进程仍继承最小安全环境，stderr 仍定向空设备，`DEBUG=false`。
- Duplicate 检查只在内存比较原值，比较结果只形成 `duplicate_secret` 稳定原因。

## 7. Markdown 知识库

### 7.1 模块边界

知识库是本地、只读、可重建的产品能力，不属于 MCP。建议新增：

```text
src/raricy_bot/kb/
  __init__.py
  models.py       # 文档、分块、命中和不可变快照
  loader.py       # 路径边界、UTF-8 读取、Markdown 分块
  index.py        # 纯标准库词项构建、BM25 风格评分
  service.py      # 生命周期、原子快照、周期刷新与查询
```

这样 KB 失败不会进入 MCP 重连逻辑，Exa 代码也不会依赖文件系统检索。

### 7.2 配置建议

```yaml
knowledge_base:
  enabled: false
  root_dir: ./knowledge
  access_mode: allowlist
  allowed_channel_kinds:
    - dm
  allowed_user_ids: []
  refresh_seconds: 60
  max_files: 2000
  max_file_bytes: 1048576
  max_total_bytes: 67108864
  chunk_chars: 2400
  chunk_overlap_chars: 200
  top_k: 6
  max_context_tokens: 4000
```

校验规则：

- `enabled` 默认 false；缺少整个节点时行为不变。
- `root_dir` 必须为非空路径；运行时解析后只能读取根内普通文件。
- `access_mode` 首版只允许 `allowlist` 或显式的 `all_chat`。
- `allowlist` 下 `allowed_user_ids` 不能为空，且默认只允许 DM；否则启动时把 KB 标为不可用。
- `all_chat` 必须由配置显式写出，不能成为默认值。
- 所有数量、字节、刷新和 token 限制为正数并设置代码硬上限。
- `chunk_overlap_chars < chunk_chars`，`top_k <= 10`，`max_context_tokens` 不得超过
  `behavior.context_input_tokens`。

### 7.3 文件发现与路径安全

一次扫描遵守：

1. 解析 KB 根目录的绝对规范路径。
2. 递归枚举扩展名大小写无关的 `.md` 普通文件。
3. 不跟随文件或目录符号链接、junction 或 reparse point；任何解析后逃出根目录的项跳过。
4. 跳过隐藏目录、隐藏文件、临时文件和非 Markdown 文件。
5. 单文件、总字节数和文件数在读取前后都检查，防止扫描过程中替换文件绕过上限。
6. 以 `utf-8-sig` 严格解码；非法编码只跳过该文件并记录稳定原因，不猜测本地编码。
7. 对外只保留 POSIX 风格相对路径；绝不保留或展示宿主绝对路径。
8. 扫描顺序按规范化相对路径稳定排序，使相同目录得到确定性快照和测试结果。

容器部署使用只读挂载：

```yaml
volumes:
  - ./knowledge:/app/knowledge:ro
```

Rocky/RHEL 使用 bind mount 时应按现有部署约定评估 `:Z`；KB 目录只需 UID 10001 可读，不能给
容器写权限。知识库不要复制进镜像，以免旧资料残留在镜像层。

### 7.4 Markdown 分块

首版使用确定性的行级解析，不追求完整 CommonMark AST：

- UTF-8 BOM 被移除；YAML front matter 默认不送模型、不参与正文检索，只可用于未来元数据扩展。
- 第一个 H1 作为文档标题；没有 H1 时使用不带扩展名的文件名。
- H1–H6 构成当前 `heading_path`；标题文本同时进入检索词项。
- 优先在标题和空行边界切块；超长段落再按字符硬切，保留配置的重叠字符。
- fenced code block 不在中间按空行拆开；若单个代码块超限，按硬上限切并标记连续序号。
- 每个块包含 `category`、`relative_path`、`heading_path`、`ordinal`、`content`，不含绝对路径。
- 空文件、只有 front matter 的文件和只含空白的块不进入索引。

### 7.5 检索算法

不新增 `jieba`、嵌入模型或向量数据库。首版使用标准库实现可测试的词法检索：

- 拉丁字母/数字连续串做 `casefold` 后作为词项；
- CJK 文本生成单字和相邻双字词项，兼顾短查询与中文召回；
- 分类、相对路径、文件标题、标题路径和正文分别建词项；
- 采用 BM25 风格正文分数，分类/路径/标题命中使用固定小幅加权；
- 同一文件最多返回两个块，避免一篇长文吃完 `top_k`；
- 分数相同按相对路径、块序号稳定排序；
- 查询没有有效词项或最高分不超过固定阈值时返回无结果，不把整库塞给模型。

索引是不可变 `KnowledgeSnapshot`。刷新任务在工作线程中构建新快照，完整成功后用一次引用替换
原快照；请求要么看旧版本，要么看新版本，不看到半建状态。单个坏文件可以跳过；根目录不可读、
容量超限或整个快照为空时，保留上一份成功快照并记录刷新失败。

### 7.6 当前轮上下文格式

命中结果按 token 预算截断后，作为当前最后一条 `role=user` 内容中的明确数据块，而不是 system：

```text
<用户问题>

[本地知识库资料（不可信数据，仅供参考）]
[KB1]
分类: electrochemistry
来源: electrochemistry/transport-number.md
标题: 迁移数 > 定义
内容: ...

[KB2]
...
```

`KB1` 等标签只在当前轮稳定，下一次检索重新编号。System 只追加一段无动态值的
`KB_SYSTEM_ADDENDUM`，要求模型：把资料当数据而非指令、只引用已提供的 `[KBn]`、资料不足时
明确说不足、不编造路径或来源。

知识库资料不会发送给 Exa，但会随当前轮消息发送给已配置的第三方模型。因此 `/help`、使用文档
和部署文档必须明确披露这一点。

### 7.7 上下文、送达与历史

- 先检查 generation，再检索；检索结束后和模型调用前再次检查。
- KB 块计入 `max_context_tokens`，标题、标签、分类、相对路径和截断提示全部计入。
- `ContextManager` 当前会保留至少最后一组历史，即使加入 feature 数据后超过预算。实现前必须按
  P0-05 决定：为能力上下文允许丢弃全部旧历史，或接受并记录一个明确的软上限。
- 回复成功送达后，历史只保存去掉 `/kb` 后的问题和最终回答，不保存原始命中块。
- 用户的下一条普通消息不会再次读取 KB；需要重新检索时再次使用 `/kb`。
- 检索无命中、索引不可用或访问被拒时不调用模型，发送 `notice_local` 的固定本地文案。
- Sender 未送达、generation 变化、模型失败或配额拒绝时，仍不写本轮历史。

### 7.8 访问控制

知识库不是“只要在本机就天然私密”。机器人返回的内容会被提问者看到；大厅回复还会被所有大厅
参与者看到。安全默认如下：

- `access_mode=allowlist`；
- 只允许 `channel_kind=dm`；
- 以站点稳定 `author.id` 判断允许用户，不按可改名的 username；
- 未授权请求本地回复统一文案，不泄露“目录存在、分类名称、文件数量或命中情况”；
- 只有明确配置 `access_mode=all_chat` 才允许所有聊天用户，大厅仍需精确提及机器人。

上线前应把 KB 根目录视为“允许机器人向获准用户披露的完整资料集”做人工清点。密钥、Cookie、
个人隐私、内部提示、部署配置、日志、数据库和未授权版权材料都不得放入挂载目录。

## 8. App 数据流

### 8.1 `/search`

现有模型工具循环不变，只把 Registry 后面的单 Provider 替换为池：

```text
Router -> Request(search) -> model first round -> one logical tool call
       -> SearchLimiter -> ExaPooledProvider -> selected slot(s)
       -> ExaSearchAdapter -> model second round -> Sender -> Context
```

### 8.2 `/kb`

```text
Router -> Request(kb)
       -> access check
       -> generation check
       -> KnowledgeService.search(query)
       -> no hit/unavailable: notice_local, stop
       -> append KB data block to pending role=user
       -> static KB system addendum
       -> ordinary model.complete()
       -> generation check -> Sender -> Context without raw KB blocks
```

`/kb` 不使用 `complete_with_tools`，因此不依赖模型端点的 tools 能力，也不会占用 MCP limiter。文件
检索在模型 semaphore 外执行；真正调用模型时仍使用现有全局 `_model_gate`。

## 9. 日志与可观测性

### 9.1 永远不得记录

- 任何 Exa Key、环境变量名、Key 指纹、账户邮箱或团队名；
- 用户 `/search` 或 `/kb` 查询；
- 知识库分类名、文件名、相对/绝对路径、标题、正文、命中词和分数；
- Exa/MCP 原始错误正文、工具参数、返回标题、URL 或摘要；
- 发送给模型的请求体或模型正文。

### 9.2 可记录的稳定字段

建议只增加经评审的低敏字段：

| 事件 | 字段示例 |
|---|---|
| `exa.pool_started` | `count=<配置槽位数>`、`status=<可用槽位数>` |
| `exa.slot_state` | `slot=<本进程序号>`、`reason=<稳定分类>`、`delay=<秒>` |
| `exa.pool_exhausted` | `count=<本轮已尝试槽位数>`、`reason=<聚合原因>` |
| `kb.index_ready` | `count=<文件数>`、`status=<块数>`、`size_bytes=<总读取字节>` |
| `kb.index_failed` | `reason=<稳定分类>`、`count=<跳过文件数>` |
| `kb.query_done` | `count=<返回块数>`、`status=<快照版本序号>` |
| `app.kb_unavailable` | `reason=<disabled/access/no_results/index_unavailable>`、现有频道字段 |

若 `status` 同时承载数字和字符串会妨碍日志查询，实现时可新增更明确的 `available_count`、
`chunk_count`、`snapshot_version`，但每个字段都必须先加入 `LOG_FIELDS` 并做正文哨兵测试。

## 10. 故障降级

| 故障 | 用户行为 | 运维行为 |
|---|---|---|
| 一个 Exa 槽位 402/401/429/崩溃 | 有界尝试下一槽位 | 更新单槽位状态，不停整个 App |
| 全部 Exa 槽位不可用 | `/search` 返回现有不可用提示 | 后台按状态恢复，不影响普通聊天 |
| KB 根不存在或不可读 | `/kb` 返回 KB 不可用提示 | 保留旧快照或无快照，周期重试 |
| 单个 Markdown 非 UTF-8/过大 | 其余文件仍可检索 | 跳过并累计稳定原因，不记路径 |
| 全库超过硬上限 | 保留上一成功快照；首次启动则不可用 | 不构建部分新快照，要求运维收缩目录 |
| KB 无相关结果 | 本地返回“未找到相关资料” | 记录命中数 0，不调模型 |
| 模型失败或发送失败 | 沿用现有失败/配额策略 | 不提交 KB 原文或孤立历史 |
| `/reset` 与检索/轮换竞态 | 旧请求静默作废 | 不发消息、不写历史、不继续换槽位 |

## 11. 配额和成本语义

- 站点消息配额不变；一次最终模型回答仍只占一条 `reply`。
- KB 用法、无权限、无结果和不可用提示属于明确用户动作，使用 `notice_local`。
- 一次 `/search` 仍是一个模型工具调用，但故障转移可能产生多个 Exa 上游请求。
- 不因有多个 Key 而提高 `SearchLimiter` 的全局并发或最小间隔。
- 不在本地累加“剩余额度”；402 只表示该时刻该 Key 或 Team/预算不可用。
- 若多个 Key 属于同一 Team，共享预算可能使轮换完全无效；应在部署验收中验证，而不是从 Key 数量
  推断容量。

## 12. 安全与隐私边界汇总

1. `/search` 只把问题发给模型，并可能由模型把查询发给 Exa；Exa 结果再发给模型。
2. `/kb` 把问题和命中的本地 Markdown 片段发给模型，但不发给 Exa。
3. 普通聊天既不调 Exa，也不读 KB。
4. 动态数据只进入 `role=user` 或现有 `role=tool`；system 附加说明必须为静态常量。
5. KB 目录是显式数据发布边界；访问策略不能靠“别人不知道 `/kb`”实现。
6. Exa 池只接收经过授权的 Key；池本身不是绕过平台额度政策的许可。

## 13. 推荐裁决摘要

批准后建议把下列裁决追加为 D-36 起的正式记录：

- Exa 多 Key 作为一个逻辑 Provider，不改变模型侧工具名和单轮调用上限。
- Key 池只接受已获授权凭证；合规确认是生产门禁。
- 轮询与故障转移有界，账号级错误和请求级错误分开处理。
- `/kb` 是显式单轮本地检索，不是 MCP 工具，也不自动联网。
- KB 一级目录即分类，递归只读 `.md`，索引可重建且不写 SQLite。
- KB 正文作为不可信 `role=user` 数据，回复送达后历史不保存命中原文。
- KB 默认仅 DM + 用户 ID allowlist；公开模式必须显式配置。
- Exa 池和 KB 都是软故障扩展，不参与健康判定。

## 14. 架构问题日志

状态含义：`阻塞` 必须在实现或上线前关闭；`待验证` 有推荐方案但需要证据；`已建议` 可按本文
默认执行，若改动需补裁决。

| ID | 级别 | 状态 | 问题与提醒 | 关闭条件 |
|---|---|---|---|---|
| P0-01 | P0 | 阻塞 | 多个人账号叠加免费额度是否获 Exa 明确授权未知 | 保存书面许可，或改用官方 Team/付费/资助额度 |
| P0-02 | P0 | 阻塞 | `exa-mcp-server@3.4.1` 是否保留 401/402/429/tag 的可分类证据未知 | 收集脱敏固定样例并写契约测试；无法分类则只轮询、不自动按额度切换 |
| P0-03 | P0 | 阻塞 | KB 是公开资料还是私有资料未确认 | 确认 `allowlist+dm` 或显式 `all_chat`，并记录资料负责人 |
| P0-04 | P0 | 阻塞 | KB 中是否含个人隐私、密钥、内部提示或无权转交模型的材料未知 | 上线前完成目录清点与第三方模型处理授权确认 |
| P0-05 | P0 | 阻塞 | `context_input_tokens` 对 KB 附件应是硬上限还是现有软上限未裁决 | 锁定 Context 合同并覆盖最后一组历史超预算用例 |
| P1-01 | P1 | 待验证 | 多个 Key 可能同属一个 Team 并共享预算，轮换无容量收益 | 用非生产 Key 验证 402/预算归属，或由 Exa/管理员确认 |
| P1-02 | P1 | 待验证 | 429 可能按 Key、Team、IP 或网络出口计，换 Key 可能无效并加剧限流 | 记录不含正文的测试结果，限制每槽一次并保持全局 limiter |
| P1-03 | P1 | 待验证 | 多子进程的内存和文件描述符成本未知 | 在目标 Docker 环境测 1/4/8 槽位 RSS、FD 与停止耗时 |
| P1-04 | P1 | 待验证 | MCP 工具 schema 在所有槽位是否完全一致 | 启动发现时比较规范化 schema 指纹，不一致槽位禁用 |
| P1-05 | P1 | 已建议 | 402 的真实恢复时刻未知 | 内存 `exhausted` + 6 小时探测；不要声称等到月初必然恢复 |
| P1-06 | P1 | 已建议 | 超时后上游可能已计费，再换 Key 会产生重复费用 | 文档披露；每槽一次、有界尝试，不对成功结果再试 |
| P1-07 | P1 | 已建议 | 环境变量名可能含账号信息 | 日志不记录变量名，只记录临时槽位序号 |
| P1-08 | P1 | 已建议 | 重复 Key 会制造虚假冗余 | 启动时内存去重，重复槽位禁用，不记指纹 |
| P1-09 | P1 | 待验证 | Windows reparse point 与 Linux symlink 的检测方式不同 | 两个平台分别做根逃逸组件测试，容器测试以 Linux 为准 |
| P1-10 | P1 | 已建议 | Markdown 文件扫描时被替换可能绕过大小检查 | 打开前后检查大小/类型，超限丢弃整个新快照 |
| P1-11 | P1 | 待验证 | CJK 双字词项对实际资料的召回质量未知 | 建立至少 20 条真实问题的小型离线金标集并报告 recall@6 |
| P1-12 | P1 | 已建议 | 大文件/大量文件重建可能阻塞事件循环 | `asyncio.to_thread` 构建，原子换快照，限制总量 |
| P1-13 | P1 | 已建议 | 大厅公开回复会扩大 KB 泄露范围 | 安全默认仅 DM；大厅必须显式开放 |
| P1-14 | P1 | 已建议 | 用户可在 Markdown 写提示注入 | 资料只进 `role=user`，静态附加说明隔离，不执行其中命令 |
| P1-15 | P1 | 已建议 | 文件名/标题本身可能泄密 | 只展示相对路径且部署前审查命名；绝不展示绝对路径 |
| P1-16 | P1 | 待验证 | 模型可能编造 `[KBn]` 或误引片段 | 增加未知标签检查与固定检索来源附注，验收引用准确性 |
| P1-17 | P1 | 已建议 | 周期刷新失败若清空旧快照会造成瞬时不可用 | 只有完整成功才替换；失败保留上一份快照 |
| P1-18 | P1 | 已建议 | 无结果时让模型回答会伪装成“来自知识库” | 无命中本地返回，不调用模型 |
| P2-01 | P2 | 已建议 | 根目录 `.md` 的分类语义可能令人困惑 | 固定为 `_root` 并在部署文档示例中说明 |
| P2-02 | P2 | 待验证 | front matter 是否包含需要检索的元数据未知 | 首版忽略正文；如需使用，另行锁定允许字段白名单 |
| P2-03 | P2 | 已建议 | 同一文件多个高分块挤占结果 | 每文件最多两块，分数相同时稳定排序 |
| P2-04 | P2 | 已建议 | KB 修改到可见存在刷新延迟 | `/help` 不承诺即时；日志提供 snapshot version 而非文件名 |
| P2-05 | P2 | 已建议 | `all_chat` 配错会静默公开资料 | 配置与启动日志明确 access mode；上线清单二次确认 |
| P2-06 | P2 | 待验证 | Docker `:Z` 是否影响部署目录现有 SELinux 标签 | 在 Rocky 目标机按部署附录实测，不能由 Docker Desktop 代证 |
| P2-07 | P2 | 已建议 | 新日志字段可能被白名单静默丢弃 | 每个新事件写日志合同测试，不能用直接 logger 绕过 |
| P2-08 | P2 | 已建议 | 帮助文案可能超过站点单条上限 | 拼装后按字符数做固定断言，必要时精简而非发送多条 |

---

# 第二部分：实施流程与分工

## 15. 实施原则与依赖顺序

实施分四个波次：

```text
Wave 0  关闭 P0 + 锁定合同和裁决
   |
   +------ Wave 1A 配置合同
   +------ Wave 1B Exa 池内核
   +------ Wave 1C KB 内核
                    |
Wave 2  Router/App/Context/Docker 集成（单一集成人负责共享文件）
                    |
Wave 3  独立审查、目标环境验收、文档同步
```

Wave 1 的工作可在接口锁定后并行，但所有共享文件由 Wave 2 的集成人统一修改。不得让多个执行者
同时修改 `config.py`、`core/router.py`、`app.py`、`texts.py`、`INTERFACES.md` 或
`DESIGN_DECISIONS.md`。

## 16. 角色与文件所有权

| 角色 | 独占写入范围 | 主要交付 |
|---|---|---|
| 架构负责人 | `docs/design/INTERFACES.md`、`docs/design/DESIGN_DECISIONS.md` | 关闭 P0、锁定类型/状态/错误码/命令顺序 |
| A：配置与秘密 | `src/raricy_bot/config.py`、`tests/test_config.py`、`config.example.yaml` | Pool/KB frozen dataclass、默认、校验、兼容性 |
| B：Exa 池 | 新建 `src/raricy_bot/mcp/pool.py`、新建 `tests/test_mcp_pool.py` | 槽位生命周期、轮询、分类、有界故障转移 |
| C：KB 内核 | 新建 `src/raricy_bot/kb/**`、新建 `tests/test_kb.py` | 安全扫描、分块、索引、快照与检索 |
| D：集成人 | `text_utils.py`、`core/router.py`、`core/context.py`、`app.py`、`texts.py`、`logging_setup.py`、Docker/usage 文档及对应既有测试 | 两条端到端数据流和所有共享文件改动 |
| E：审查与验收 | 原则上只读；发现问题写审查日志，修复交回原 owner | 隐私、竞态、成本、容器、回归与金标报告 |

每位执行者都不是仓库中唯一工作者：只修改自己的范围，不回滚别人已有改动；遇到共享合同变化先
通知架构负责人和集成人，不在局部代码里自行发明新字段。

## 17. Wave 0：决策与合同冻结

### 17.1 关闭阻塞问题

- 关闭 P0-01：保存不含 Key 的 Exa 授权结论；未关闭则账号池只可在假 Provider 测试中实现，
  不得写生产启用示例。
- 关闭 P0-02：固定 3.4.1 成功、401、402、429、500、畸形错误样例。
- 关闭 P0-03：确定 KB 访问模式和用户 ID allowlist，由资料负责人签字确认可见范围。
- 关闭 P0-04：逐文件清点挂载目录，确认不含秘密、隐私或无权转交第三方模型的材料。
- 关闭 P0-05：确定 feature 上下文与 `context_input_tokens` 的硬/软预算合同。

### 17.2 更新正式合同

在 `INTERFACES.md` 中锁定：

- `McpAccountPoolConfig`、`KnowledgeBaseConfig` 的完整字段和默认值；
- `ExaPooledProvider` 与槽位状态/错误分类类型；
- `KnowledgeDocument`、`KnowledgeChunk`、`KnowledgeHit`、`KnowledgeSnapshot`、
  `KnowledgeService` 的签名；
- `/kb`、能力冲突、访问拒绝、无结果的 Router 顺序和稳定 reason；
- App 启停、generation 检查、角色边界和历史提交；
- 新日志事件/字段、配置硬上限和 Docker 挂载合同。

在 `DESIGN_DECISIONS.md` 追加 §13 的裁决。完成后由 B、C、D 分别做一次只读合同审查；三方确认
再进入 Wave 1。

## 18. Wave 1A：配置与秘密

### 所有者 A

1. 先写配置失败用例：无节点保持旧默认、合法池、单 Key 兼容、池/env_from 互斥、空/重复/超量
   host env 名、非法数字、KB access allowlist 为空、路径/块/token 上限。
2. 在 `config.py` 增加 frozen dataclass 和解析器；不得在 Config/Secrets 中保存实际 Exa Key。
3. 为旧单 Key 配置保留兼容路径；默认配置不得启动额外进程或扫描目录。
4. 更新 `config.example.yaml`，只出现环境变量名与示例路径，不出现任何看似真实的秘密。
5. 交付一份字段表给集成人；不修改 `app.py` 或 Docker 文件。

### 验收

- 单元测试覆盖每条拒绝规则与边界值。
- `repr(config)`、异常消息和 pytest 输出不含秘密哨兵。
- 旧配置 fixture 加载结果与改动前相同。

## 19. Wave 1B：Exa 池内核

### 所有者 B

1. 用假 `McpProvider` 写失败测试，覆盖 3 槽轮询、单槽成功、402 切换、401 永久禁用、429 冷却、
   超时、全部失败、generation 取消、stop 幂等和 schema 不一致。
2. 实现槽位模型与错误分类器；时钟、sleep、provider factory 全部可注入。
3. 实现 `ExaPooledProvider`；同一逻辑调用每槽最多一次，成功后不再尝试。
4. 验证至少一个槽位可用时 `available=True`；内部单槽故障不触发整池重连。
5. 使用固定脱敏 MCP 样例覆盖结构化状态/tag 和严格文本降级匹配。
6. 不修改 Registry、Runtime 或 App；需要集成变化时以接口说明交给 D。

### 验收

- 无真实网络、真实 Key 或真实 Node 进程。
- 所有并发/冷却测试使用注入时钟，不使用真实 sleep。
- 日志不含错误正文、环境变量名、Key、查询或 URL 哨兵。
- N 个可用槽位连续 N 次成功调用按 `0..N-1` 轮询；第 N+1 次回到 0。
- 一次逻辑调用的上游实际尝试数不超过当时可用槽位数。

## 20. Wave 1C：知识库内核

### 所有者 C

1. 建立临时目录 fixture，先覆盖分类、根文件 `_root`、大小写 `.md`、非 md、BOM、非法 UTF-8、
   空文件、超大文件、总量超限、symlink/junction 根逃逸。
2. 实现确定性 Markdown 分块，覆盖标题层级、长段落、代码围栏和重叠边界。
3. 实现词项生成和 BM25 风格评分，覆盖中文、拉丁、分类/路径/标题提升、每文件最多两块和稳定
   tie-break。
4. 实现不可变快照和原子刷新：单个坏文件跳过，整个刷新失败保留上一成功快照。
5. 实现 token 有界的 `[KBn]` 格式化输出；不返回绝对路径。
6. 建立一份仅含测试资料的 20 问金标集，报告 recall@6 和明显误召回，不为追指标引入未评审依赖。
7. 不修改 Router/App/Context；把查询接口和错误类型交给 D。

### 验收

- 相同文件树重复构建得到顺序和内容完全相同的快照。
- 路径逃逸用例在 Windows 与 Linux 容器至少各验证一次。
- 大小和 token 上限的标签、标题、路径、内容与截断提示全部计入。
- 日志正文扫描不出现测试文件名、分类、查询和内容哨兵。
- 刷新过程中查询始终只看到完整旧快照或完整新快照。

## 21. Wave 2：集中集成

### 21.1 Runtime 与 Provider 装配

所有者 D：

- `McpManager` 依据服务器配置选择单 Provider 或 `ExaPooledProvider`，Registry 仍只登记一个
  `server=exa`。
- 保持现有适配器、模型工具名、feature binding 和 `SearchLimiter` 不变。
- 为池提供单槽恢复调度；确认单槽失败不会让 Registry 删除仍可用的逻辑工具。
- App 停止顺序加入池任务，继续满足总关闭超时。

### 21.2 Router 与文本命令

- 在 `text_utils.py` 增加 `/kb` 解析和通用能力冲突判定。
- 在 `Request.enabled_features` 继续只传通用字符串 `search`/`kb`，不得加入 Exa Key 或路径。
- 固定顺序：候选过滤 -> 单一能力解析 -> 空参数/冲突 -> `/help`/`/reset` -> 媒体 -> 长度 ->
  秘密探测 -> 入队。
- 把 `kb_usage`、`capability_conflict`、`kb_access_denied` 加入稳定 reason 与测试。
- 评论 Router 不解析 `/kb`。

### 21.3 App 与 Context

- BotApp 构造 `KnowledgeService`，但只有 `knowledge_base.enabled=true` 才扫描。
- `start()` 在普通聊天可启动的前提下最佳努力启动 KB；KB 失败只记事件。
- `_handle_request` 为 `kb` 走 §8.2，检索前后复用 generation 检查。
- 动态 KB 块只追加到 pending user，不得进入 `system_addenda`。
- 静态 `KB_SYSTEM_ADDENDUM` 与 `MCP_SEARCH_SYSTEM_ADDENDUM` 互斥。
- 按 P0-05 的裁决调整 Context 预算；普通聊天和 `/search` 的既有裁剪语义不得暗改。
- 仅发送成功后保存原始问题和回答，不保存 KB 块。

### 21.4 文案、日志与部署

- `texts.py` 增加 KB 用法、无权限、无结果、不可用、能力冲突和静态 system 附加说明。
- `/help` 同时披露 `/search` 发往 Exa、`/kb` 从本地资料检索但命中片段会发往第三方模型。
- 更新 `LOG_FIELDS` 与日志安全测试；禁止为排障记录原始错误/文件名。
- Compose 显式透传 Key 变量并只读挂载 KB；单 Key 配置仍可使用原来的 `EXA_API_KEY`。
- 更新 `docs/usage/USAGE.md`、`docs/usage/DEPLOYMENT.md`、根 README 的相关命令说明；若根
  README 有用户未提交改动，必须先合并语义而不是覆盖。

## 22. Wave 3：审查与验证

### 22.1 自动测试矩阵

```text
tests/test_config.py
  pool/KB defaults, validation, backward compatibility, secret repr

tests/test_mcp_pool.py
  round robin, error classification, cooldown, bounded failover, lifecycle

tests/test_kb.py
  safe scan, markdown chunks, CJK/Latin ranking, atomic refresh, token limits

tests/test_text_utils.py + tests/test_router.py
  /kb grammar, capability conflict, local command priority, comment isolation

tests/test_app.py
  access gate, no-result/no-index local replies, generation races,
  role=user injection, delivered-only history, ordinary-chat isolation

tests/test_logging_safety.py
  all keys, env names, queries, file names, paths, snippets and upstream bodies absent
```

按顺序运行：

```bash
python -m pytest tests/test_config.py tests/test_mcp_pool.py tests/test_kb.py -q
python -m pytest tests/test_text_utils.py tests/test_router.py tests/test_app.py -q
python -m pytest tests/test_logging_safety.py tests/test_mcp.py tests/test_context.py -q
python -m pytest tests -q
```

`filterwarnings=error` 保持开启。Windows 若再次出现临时目录权限或 `spawn EPERM`，只能作为环境
问题单独记录，不能用非隔离共享状态的测试替代全量证据。

### 22.2 目标环境验收

使用专门的非生产测试 Key 和可公开测试 Markdown：

1. 构建固定版本镜像，确认运行期不访问 npm。
2. 1、4、8 槽分别记录容器 RSS、FD、启动和停止时间，不记录 Key 或变量名。
3. 用受控假 MCP 或代理返回 401/402/429/500，证明轮换、冷却和全部失败降级。
4. 使用一个明确合法的真实 Key 做一次最小 `/search` 冒烟；不得为测试消耗多账号免费额度。
5. 在容器内验证 KB 挂载只读、UID 10001 可读、非 `.md` 不加载、修改后一个刷新周期可见。
6. 在 Rocky 9 验证 SELinux 标签、只读挂载、重启后 Key/KB 行为；Docker Desktop 不能替代该证据。
7. 验证 `/livez`、`/readyz` 在 Exa 全挂和 KB 全挂时仍符合现有合同。
8. 审查所有容器日志、SQLite 表和内存历史测试快照，确认不存在秘密或资料正文。

### 22.3 独立审查门

E 需要逐条回答：

- 是否有任何路径让普通聊天、评论或嵌套命令获得 Exa/KB 能力？
- 是否可能在一次逻辑搜索中重复尝试同一槽位或无限轮换？
- 401/402/429/输入错误是否被错误归类？无法分类时是否安全失败？
- 是否有 Key、环境变量名、查询、KB 文件名/路径/正文进入日志或 SQLite？
- KB 动态内容是否只进入 user 消息，静态 system 文本是否无占位符？
- allowlist 是否按 `author.id`，大厅公开风险是否被默认禁止？
- 刷新失败是否保留旧快照，符号链接是否能逃逸根目录？
- `/reset` 是否在检索、轮换、模型和发送每个异步边界作废旧请求？
- 帮助文案是否准确、未超长，用户是否知道 KB 命中会发往第三方模型？

任何 P0/P1 发现先回交原 owner 修复，再由 E 复验；集成人不得在审查阶段顺手跨模块重写。

## 23. 交付清单

- [ ] P0-01 至 P0-05 全部有明确关闭证据。
- [ ] `INTERFACES.md` 和 `DESIGN_DECISIONS.md` 已先于实现更新并通过三方审查。
- [ ] 无新配置时全部现有行为不变，不启动 KB，不增加 Exa 子进程。
- [ ] 单 Key 配置继续工作；池模式对模型仍只有 `exa__web_search_exa`。
- [ ] 轮询、公平性、有界故障转移、状态恢复和 schema 一致性有确定性测试。
- [ ] `/kb` 只在获准聊天中单轮生效，评论、普通消息和能力嵌套不能启用。
- [ ] KB 只读递归 `.md`，一级目录分类，路径无法逃逸，索引刷新原子。
- [ ] KB 无命中不调模型，命中正文只进当前 `role=user`，历史不保存原文。
- [ ] Key、查询、错误正文、KB 路径/内容均不进日志或 SQLite。
- [ ] Exa 池与 KB 故障不影响普通聊天、评论、`/livez`、`/readyz`。
- [ ] 帮助、使用、部署、Compose、Rocky 说明与真实行为一致。
- [ ] focused、全量、Docker、Rocky 和金标检索验收均有无 warning 的新鲜证据。

## 24. 外部依据与版本提醒

- Exa 服务条款：<https://exa.ai/assets/Exa_Labs_Terms_of_Service.pdf>
- Exa 错误码与稳定 tag：<https://exa.ai/docs/reference/error-codes>
- Exa Team 和共享限制说明：<https://exa.ai/docs/reference/setting-up-team>
- Exa MCP 配置与工具：<https://exa.ai/docs/reference/exa-mcp>
- Exa 当前价格与额度入口：<https://exa.ai/pricing>
- Exa MCP 官方仓库：<https://github.com/exa-labs/exa-mcp-server>

外部事实核对日期为 2026-09-14。价格、免费额度、错误形态、Team 规则和条款都可能变化；实施和每次
升级 `exa-mcp-server` 前必须重新核对官方资料。任何历史报价或本地观察都不能当作当前授权证明。
