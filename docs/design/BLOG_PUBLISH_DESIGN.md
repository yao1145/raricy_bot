# 定时发文设计

| 项 | 值 |
|---|---|
| 日期 | 2026-09-19 |
| 依据的上游版本 | `raricycms/raricy.com@5eace1299751176e5a612036751c0de78bb8f7a9` |
| 状态 | 已实现，默认关闭（`blog.enabled: false`）。**真实站点验收尚未执行**：设计 §15 的八条要在部署者启用后按 §15 逐条跑，测试套件全部使用替身 |
| 最近修订 | 2026-09-19：补齐调度领取、投递恢复、预算占额与 MCP 隔离；同日按实施计划完成 T0…T7，内部合同登记在 `INTERFACES.md` §53，越界与仲裁登记为 D-106…D-110 |

本文件是设计与规格。运行行为（怎么启用、日志、验收）见 [`../usage/USAGE.md`](../usage/USAGE.md)；
实现前先把本设计新增或修改的合同写入 [`INTERFACES.md`](INTERFACES.md)，越界与仲裁写
[`DESIGN_DECISIONS.md`](DESIGN_DECISIONS.md)。
详细分工、并行依赖和精简测试安排见 [BLOG_PUBLISH_IMPLEMENTATION_PLAN.md](BLOG_PUBLISH_IMPLEMENTATION_PLAN.md)。

## 1. 目标

让机器人以自己账号，按配置的定时点自动发布博客文章。内容两条来源：

- **稿库**：人事先写好的 Markdown 文件放在目录里排队；
- **现写**：到点由模型生成，允许调用 MCP 工具取材料。

两条来源产出的都是同一个 `Draft` 形状，投递路径只有一条。**两条都不经人工审稿**
（用户明确选择）。自动发布必须同时满足 §6 的调度去重、§7 的保守投递、§8 的工具边界、
§9 的预算与 §10 的输出脱敏。本设计不承诺 exactly-once：站方没有幂等键，
在无法判断远端结果时，接受漏发或等待人工核实，不能用重复发布换取成功率。

## 2. 站方接口事实

接口是 `POST /api/blogs`，上游 `src/app/api/blogs/route.ts`。以下全部读自该文件、
`src/lib/blog-service.ts`、`src/lib/format.ts`、`src/middleware.ts`、`src/lib/rate-limit.ts`。

| 项 | 值 |
|---|---|
| 认证 | 登录 + core+ + 未被禁言（禁言对管理员同样生效） |
| 请求体 | JSON `{title, description, content, category_id}` |
| 标题 | 去首尾空白后非空，≤ 30 字符 |
| 描述 | 去首尾空白后非空，≤ 100 字符 |
| 正文 | 非空，**不去空白**，≤ 250000 字符 |
| 栏目 | `category_id` 可空/null 即「未分类」；非空须为整数、存在且 `is_active`；栏目或其父栏目勾了「仅管理员可发」而无管理权则 403 |
| 日限 | 每作者 **20 篇/日**，按 **UTC+8 零点** 切 |
| 分钟级限频 | **无** —— 该处理器没有调用 `rateLimit()` |
| 成功响应 | `{code: 200, message: "上传成功", blog_id, redirect}` |
| 失败响应 | `apiErr(code, message)`：JSON 里 `code` 是错误码，且 **HTTP 状态码取同值** |

发布顺序是固定的：登录 → 禁言 → 核心用户 → 校验 → 日限额 → 栏目管理员专属 → 建文 → 通知。
本地预校验（§10）挡住文本形状与长度错误；栏目存在性、启用状态等远端事实仍以站方响应为准。

编辑走 `PUT /api/blogs/:id`，校验与发布**完全一致**；本设计不使用它。

### 2.1 CSRF

上游 `src/middleware.ts` 只对写方法做 Origin/Referer 同源校验，且对**同时缺失**这两头的
请求保守放行（依据是 SameSite=lax）。现有 `SiteClient` 的策略就是两个都不设，直接适用，
**不得**为了本次改动给它加上任何一个头。

### 2.2 栏目 id 拿不到

站上**没有公开的栏目列表接口**。`GET /api/blogs` 只回栏目名与完整路径，不回 id；
`/api/admin/categories` 需管理员权限。有子栏目的父栏目 id 会以 `id="category-<n>"` 出现在
公开的 `/blog` 页面 HTML 里，**叶子栏目不会**。

因此 `category_id` 由人手工填，或留空发为未分类。本设计**不做**「启动时抓页面解析栏目」的
便利层：它只覆盖父栏目、依赖 HTML 结构、站方改版即静默失效，而失效的表现是发错栏目 ——
比让人填一次数字糟得多。

### 2.3 搜索不是发布凭证

`GET /api/blogs?search=...&search_fields=title` 返回标题子串匹配结果，有分页，且不带
`category` 时会排除 `excludeFromAll` 栏目及其子栏目的文章。搜索空结果不能证明未发布。
列表含稳定的 `author_id`，对账不能用可变的作者名替代它。候选正文可通过现有
`SiteClient.fetch_blog_context()` 使用的 core+ 读取接口临时取回计算指纹，无须持久化正文。

以上依据为表头固定提交中的 `src/app/api/blogs/route.ts`、`src/lib/blog-service.ts`。

## 3. 契约位置：本功能不在站方机器人契约里

站方 `docs/bot/` 只有 `chat-bot.md`、`comment-bot.md`、`fish-bot.md`、`favorite-bot.md`，
其中 `/api/blogs` 只出现在评论机器人的读评论/发评论口径中。**发文没有站方背书的机器人契约**，
它是普通用户接口，网页表单走的就是它。

三件事必须写进 `DESIGN_DECISIONS.md`：

1. 本子域不受 `docs/materials/chat-bot.md` 保护 —— 站方改前端即可能失效；
2. 本仓库的既有约定是「只允许使用 chat-bot.md 里的接口」，这是一处**需要显式记录的例外**，
   不是默默越界；
3. 它是 core+ 功能，账号掉出核心用户即整体失效。

## 4. 配置

新增配置段 `blog`，与既有 12 个段同级。**不建 Web 界面**（用户明确选择）：
任务的数量与字段都在 YAML 里，改动走版本控制。

```yaml
blog:
  enabled: false
  # 本地日预算 1..5，默认 2；不确定投递持续占额。本地日按 UTC+8 零点切（§9）。
  max_posts_per_day: 2
  tasks:
    # 现写型：到点让模型写一篇。tier=must 必发起。
    - name: "晨间随笔"
      tier: must
      schedule: ["09:00"]
      category_id: 12
      prompt: |
        （人设与写作要求，多行）
    # 现写型：tier=maybe 到点按概率发起。
    - name: "偶发感想"
      tier: maybe
      probability: 0.3
      schedule: ["21:30"]
      category_id: 7
      prompt: |
        （人设与写作要求，多行）
    # 稿库型：到点从目录取一篇。
    - name: "连载"
      tier: must
      schedule: ["20:00"]
      category_id: 5
      drafts_dir: ./data/blog_drafts/serial
```

### 4.1 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 任务名，非空、全局唯一。同时是持久任务标识与日志字段；改名等同新任务 |
| `tier` | 是 | `must`（必写）或 `maybe`（选写） |
| `schedule` | 是 | `"HH:MM"` 列表，非空。按本地时区（UTC+8）解释 |
| `prompt` | 二选一 | 现写型的任务提示词 |
| `drafts_dir` | 二选一 | 稿库型：稿件目录 |
| `category_id` | 否 | 公开栏目。留空即未分类 |
| `probability` | 否 | 仅 `maybe` 允许且必填，(0, 1] |

### 4.2 加载期校验

照 `config.py` 既有风格，**启动阶段报错，不部分启动**：

1. `tier` 必须是 `must` 或 `maybe`；
2. `probability` 只能出现在 `maybe` 上，且 `0 < p <= 1`；出现在 `must` 上即配置错误；
3. `prompt` 与 `drafts_dir` **恰好出现一个** —— 两个都给或都不给都是配置错误；
4. `schedule` 非空，每项严格为 `00:00` 至 `23:59` 的 `HH:MM`，同任务不得重复；
5. `name` 非空且不重复；
6. `category_id` 若给出，必须是正整数；
7. `enabled: true` 时 `tasks` 非空；
8. `max_posts_per_day` 为非 bool 整数，范围 1..5，默认 2；
9. `prompt` 为非空字符串；`drafts_dir` 为非空路径字符串，相对路径以配置文件目录为基准；
10. `probability` 为有限数值且非 bool；`category_id` 不接受 bool；`enabled` 必须为 bool。

`drafts_dir` 不存在**不是**配置错误（目录可以先空着），运行期按「队列空」处理。

> 注意：示例里的 `./data/` 在 `.gitignore` 里。稿库是人写的、通常想进版本控制的内容 ——
> 真要入版本就换个路径（如 `./drafts/`），别默认丢进 `data/`。

## 5. 模块

```
src/raricy_bot/blog/
  planner.py    纯决策：调度时间窗口、任务表与随机源 → 调度候选（无 I/O）
  service.py    调度领取、串行消费、恢复与生命周期
  drafts.py     稿库：扫目录、解析 front matter、按投递状态选稿
  writer.py     生成：复用模型客户端的两轮工具调用 → 一个 Draft
  publisher.py  投递：预校验、幂等、对账、落库
```

### 5.1 关键收敛：一个 Draft 形状，一个解析器

`Draft` 是 `title` / `description` / `content` 三个字段，**不含栏目** —— 栏目归任务。

稿库文件与模型输出是**同一种格式**：YAML front matter + Markdown 正文。

```markdown
---
title: 标题
description: 摘要
---

正文。
```

好处有三：解析器只有一份；人可以把一篇满意的生成稿直接存进稿库复用；`publisher` 完全
不负责解析来源格式。服务层另传投递元数据（账号、任务、调度点、来源类型、栏目快照），
它们不混入 `Draft`。模型可能吐出不合法 front matter，按**生成失败**处理（§8）。
解析使用 `yaml.safe_load`，front matter 必须为映射，且 `title`、`description` 都是字符串；
禁止隐式把数字、列表或对象转换成标题。正文保留原始 Markdown，不做换行或空白归一化。

### 5.2 依赖方向

配置解析与校验放 `config.py`（任务表是配置事实），与 `mcp.features` 的校验同处。
`blog/__init__.py` **必须保持为空**，否则 `config.py` import 它时会成环 ——
`capabilities.py` 的模块注释记过这个坑（`mcp/__init__.py` 拉起 registry，registry 依赖 config）。

## 6. 触发与决策

### 6.1 调度点与持久领取

调度点按固定 UTC+8 转为 epoch 秒，执行键为 `(site_base_url, self_user_id, task_name,
scheduled_at)`。同一个键最多领取一次；内容指纹不能替代这个键，现写两次可能生成不同文章。
扫描先查询执行键，已存在直接跳过；只有新键才请求随机决策，数据库唯一约束再作兜底。
`blog_runs` 在任何读取稿件、模型调用或发布之前，以唯一约束原子插入 `queued` 行并保存
`selected`（must 恒为 1；maybe 掷骰一次得到 0/1）。插入冲突直接跳过，不能再次掷骰或生成。
只有成功提交的随机结果生效；插入前崩溃没有任何生成或发布副作用。

进程启动仅处理当前分钟的调度点，不追补停机期间更早的点；正常运行按
`(上次扫描时间, now]` 枚举调度点。独立扫描任务每秒检查一次，不等待文章生成。
枚举下界同时限制为 `now - 5 分钟`，更早的遗漏只记聚合 `misfire` 日志，不逐个插入历史行。
积压超过 5 分钟尚未开始的 `queued` 行转 `skipped`，reason=`misfire`；已经开始的任务允许
跨分钟完成。同一分钟的任务按配置声明顺序领取和消费。时钟回退靠唯一键防重，向前跳跃
也按 5 分钟窗口跳过过期点；不得形成无界补发队列。

单进程只有一个发布消费者，扫描与消费可并行，但两篇文章的生成/投递不并行。
不支持两个机器人进程共享同账号进行发文；部署必须保持单实例。

### 6.2 一次执行的顺序

1. 成功领取且 `selected=0` → `skipped`，reason=`probability_miss`；该规则同时适用于稿库和现写。
2. 日预算无余量 → `skipped`，不读取稿件、不调用模型；`must` 同样服从预算。
3. 稿库型按文件名升序选第一篇可投稿，状态规则见 §7.4。队列为空则 `skipped`；
   `must` 记录 WARNING。解析损坏的文件记稳定原因并继续下一文件，不让队首坏稿阻塞整个队列。
4. 现写型调用 writer。每个调度点最多一次生成尝试，失败结束本次执行，下一调度点再开始。
5. 预校验、指纹去重后，在同一 SQLite 事务里重新检查日预算、领取投递行并预留额度；
   此前的预算检查仅用于节省模型调用，不能代替这一步。成功才允许 POST。

`planner.py` 接收时间窗口、任务表和注入的随机源，只返回候选；Store 负责领取和状态事实。
时钟、sleep、随机都注入。SQLite 操作遵守现有 Store 线程锁与事务方式（D-90）。

### 6.3 生命周期

服务在登录取得账号身份、Store 与 MCP 启动完成后启动，后台异常记录稳定原因并停止本子域，
不结束聊天/评论服务，也不改变现有健康判定。关闭时先停止领取，再取消并等待消费者与对账任务，
最后才能关闭 MCP、模型客户端、SiteClient 与 Store。POST 期间取消留下 `inflight`，不得释放
其额度或当作未发送。关闭发文开关时不自动发布或对账，持久记录保留，重启启用后恢复。

## 7. 幂等与对账

### 7.1 指纹与投递边界

先执行 §10，指纹只针对最终出站文本计算：
`SHA256(UTF8(JSON([title, content], ensure_ascii=False, separators=(",", ":"))))`。
不能直接拼接两串，避免字段边界碰撞。指纹版本固定为 1；描述和栏目不属于内容身份，
仅改描述或栏目不会使同标题正文重新发布。唯一范围为 `(site_base_url, self_user_id, content_hash)`。

调用 `ensure_session()`、确认仍为领取时账号后，先提交 `blog_posts.inflight`、`attempts += 1`
与额度预留，再调用一次 POST；模型请求、SQLite 提交失败都不能触发 POST。
同一事务必须把当前 `blog_runs.post_id` 关联到该投递行；后续 `retry_wait` 重试也如此。
POST 方法不做传输层自动重试。账号变更时停止本子域，旧记录只能用原账号恢复。

### 7.2 完整状态转换

| 事件 | 投递状态 | 占额与后续动作 |
|---|---|---|
| POST 前事务提交 | `inflight` | 预留 1 篇；这是不确定边界，崩溃后不能假定未发送 |
| 合法成功信封，`code=200` 且 `blog_id` 为合法 UUID | `published` | 原预留转为已消费；记录站方 id，不再发送 |
| 超时、网络错误、5xx、非法/未识别信封、成功但缺少合法 id、POST 期间取消 | `unconfirmed`（取消可留 `inflight`） | 保留占额；只读对账，绝不自动重投 |
| 明确业务信封 400/403/401 | `rejected` | 释放预留；不重投，记录稳定错误分类并告警；403 可能是权限/禁言，不称为内容损坏 |
| 明确业务信封 429，稿库来源，尚未达到尝试上限 | `retry_wait` | 释放预留；保存 `retry_after_day` 为下一 UTC+8 日期，后续调度点重新选稿 |
| 明确业务信封 429，现写来源，或稿库尝试已达上限 | `abandoned` | 确定未发布，释放预留并告警；现写稿不缓存到次日 |
| 只读对账找到唯一精确匹配 | `published` | 同一笔额度转正，不重复计费 |
| 对账无结果、不完整、失败或多个匹配 | `unconfirmed` | 维持占额，不允许转 `retry_wait` 或释放额度 |

只把已识别的业务拒绝信封视为确定失败，不能仅凭 HTTP 状态判定。
收到成功后写 SQLite 失败时，不在内存中当作未发送；停发并告警，保留持久 `inflight` 供恢复。
生成次数上限为每调度点 1 次；稿库同指纹累计 POST 上限为 3 次（含首次），只允许确定的
429 进入后续尝试。读对账不计入 POST 次数。

### 7.3 只读对账

启动恢复后及运行期间每 5 分钟串行扫描当前账号的 `unconfirmed`。每次最多处理 10 条，
按 `last_reconciled_at`（NULL 优先）和 id 轮转；单条每次最多取 5 页，每页 50 条，最多读取
10 篇候选正文。到达任一上限视为查询不完整，保持不确定。已累计 12 次仍未确认则告警并
停止该行自动查询，仍保持 `unconfirmed` 和占额；人工核实通过维护流程处理，不由模型决定。

搜索使用 URL 参数编码后的最终标题与 `search_fields=title`。逐页筛选 `author_id` 与领取账号
完全一致、标题完全一致、id 合法的候选，再临时取回正文计算同款指纹。仅完整走完本轮搜索且
恰有一个精确匹配时补记；同标题不同正文、他人文章、多个精确匹配都不能认作成功。
单次正文读取也沿用 SiteClient 响应大小上限，超限算查询不完整。

标题搜索受栏目过滤，分页也可能随并发更新移动；因此即使完整搜索为空也不构成未发布证明。
本路径只用于找到正向凭证，永远不能授权再次 POST。指纹匹配不要求本地保存正文。

### 7.4 稿库选稿与恢复

| 指纹查询结果 | 选稿动作 |
|---|---|
| 无记录 | 可选；新建投递行 |
| `published` | 已发布，跳过 |
| `inflight` / `unconfirmed` | 等待确认，跳过；绝不重新发送 |
| `retry_wait` | 同原任务、栏目未变、到达 `retry_after_day` 且 attempts < 3 时可选，复用原行 |
| `rejected` / `abandoned` | 同指纹不自动再投，跳过并保留诊断记录 |

恢复 `retry_wait` 时重新读取人维护的稿库并执行 §10，只有最终指纹相同才复用记录；文件不存在
就继续保留等待，任务/栏目变化不得静默把原稿换栏目。修改标题或正文得到新指纹，视为新稿。
每次调度仍最多投一篇；429 后的旧执行结束为 `finished`，以后由新调度点关联原投递行。

启动时所有 `inflight` 降为 `unconfirmed`。此前 `queued` / `running` 的调度执行一律转
`interrupted`，不重新生成；已存在投递行按其自身状态恢复，与运行记录分开处理。
现写稿正文只在内存，进程崩溃后无法还原。若尚无投递行，该执行告警结束；若已有行，只能对账。
包括“先落 inflight、尚未 POST 即崩溃”的窗口也可能永久待确认，这是不保存正文且无站方幂等键
时主动接受的可用性代价，不能重新生成一篇文章冒充原稿重试。

## 8. 生成路径与 MCP

### 8.1 工具循环

`writer.py` 复用 `OpenAIModelClient.complete_with_tools()` 的两轮协议：首轮允许模型请求工具，
宿主至多执行一次合法调用，第二轮 `tool_choice=none`，输出 front matter + 正文。
首轮直接返回文章也合法。不实现多步研究循环，现有 `max_tool_calls_per_turn == 1` 校验保持不变。

**工具预算不新增配置项**，直接用 `mcp.features.blog_write.max_tool_calls_per_turn` ——
能力层已经是工具白名单与预算的唯一真值源，再开一个 `blog` 侧的旋钮就有了两个真相，
且它们会在配置校验里互相打架。整篇文章的生成就是这条路上的一「轮」，语义对得上。

工具执行走现有 Registry 与 Provider/pool，复用超时、故障转移与冷却。feature 的限流器隔离
方式见 §8.2。工具结果清洗不替代最终文章脱敏，所有来源都必须经过 §10。

调用前已知 MCP 未启用、未配置 `blog_write`、工具不可用，或客户端已缓存不支持 tools 时，
直接以无工具方式生成一次；首次调用才发现模型不支持 tools，则结束本次生成，后续调度点
再使用无工具路径。不得退回聊天的 `search` 授权或反复尝试模型。工具在执行中失败则回传
稳定错误供第二轮写作，
不追加一次独立生成。静态写作规则放 system；任务提示词放 user；工具结果放 tool，且明确为
不可信资料，不能改变工具白名单、栏目、发布权限或预算。

只在每次模型 HTTP 请求期间持有 App 共享的 `_model_gate`，MCP 等待不占 gate。
整次 writer 调用（含等待 gate、工具与模型）上限 180 秒；复用 `model.max_output_tokens`，
请求输入总量受 `behavior.context_input_tokens` 限制，超限失败，不丢弃写作要求。
模型客户端增加可选 `max_input_tokens` 参数，默认 None 保持旧行为；发文显式传入上限。
检查发生在每次请求序列化完成、网络调用之前，覆盖 system/user、工具定义、assistant
工具调用参数以及 tool 消息。具体对最终 `messages` 和 `tools` 的 JSON 序列化文本
（`ensure_ascii=False`）应用现有 `estimate_tokens`，连同结构开销一起估算；第二轮不能绕过
检查。这是与现有项目一致的估算预算，不声称精确等同模型商的 tokenizer。
`writer` 不在模型客户端的既有有界重试之外增加重试；SDK `max_retries=0` 保持不变。

### 8.2 要动的契约

`config.py` 对 `mcp.features.<name>` 的校验**强制要求**该 feature 已在
`capabilities.py` 的 `CAPABILITY_BY_FEATURE` 里声明（"不是已声明的 MCP 能力" 即报错），
且每个 feature 必须在 `mcp/adapters.py::_FACTORIES` 里有适配器。所以发文这条路要加：

1. `capabilities.py`：新增一个 `blog_write` 能力。它**没有命令字面量** —— 现有
   `Capability.command` 是必填的 `str`，需放宽为 `str | None`，
   并让 `CAPABILITY_COMMANDS` 过滤掉无命令的能力（否则路由器的命令表与帮助文案会多出一条
   谁也敲不出来的命令）；
2. `capabilities.py`：把 `blog_write` 加进 `IMPLEMENTED_FEATURES`；
3. `blog_write` 首版白名单为现有已审核的 6 个只读工具：`web_search_exa`、`zhihu_search`、
   `maps_geo`、`maps_text_search`、`maps_weather`、`wolfram_query`；`max_bindings=6`，
   不引入通用透传。适配器工厂按绑定工具委托现有 Exa/Zhihu/Amap/Wolfram 清洗器，
   参数长度与白名单仍受对应工具的既有约束；feature 的 `max_query_chars` 默认/上限为 500，
   各绑定实际取它与原能力上限的较小值；
4. `mcp/adapters.py` 和 `mcp/registry.py`：适配器键统一改为
   `(feature_name, model_tool_name)`。`tools_for` 的模型 schema、`execute` 的参数预处理、
   limiter 和结果清洗均使用这一键。模型看到的工具名仍为 `<server>__<tool>`，Provider 与
   pool 不复制。每个 feature 有独立 limiter，同 feature 的所有绑定共用该 limiter。
   不能保留跨 feature 回退查询，否则 `search` 与 `blog_write` 共用 Exa 时仍会覆盖策略；
5. `config.py`：`result_shape=SHAPE_LIST`，`blog_write.result_count` 的默认值及唯一允许值
   都为 1；已有能力默认值不变。它同时控制委托清洗器的结果数及单次结果 token 预算；
6. 发文适配器必须显式保证 `estimate_tokens(content) <= result_item_token_limit`，
   对清洗后的整体结果再次有界裁剪；不依赖 Registry 的通用回退裁剪，因为专用适配器在它之前
   已经返回。工具结果再加入模型请求后，仍须检查 §8.1 的整份请求预算。

这些改动涉及能力表、适配器映射、Registry 构造参数与所有注入替身，必须同步
`INTERFACES.md` §21/§22 和相关测试，不能只修改 `Capability.command`。命令相关测试只遍历
command 非空的能力；工厂完备性测试仍覆盖全部已实现 MCP 能力。

### 8.3 失败的样式

以下都算**生成失败**，不产出草稿、不消耗发文预算，但计入尝试上限：
模型调用失败、返回的 front matter 解析不出来、必填字段缺失、正文为空。
另包括模型明确返回因 token 上限截断、超时和输入超预算。模型客户端需把截断完成原因暴露为
稳定失败，不得仅凭 front matter 可解析便发布半篇正文；聊天调用保持原行为，发文通过可选
严格完成检查启用。参数名为 `require_complete`，默认 False；发文显式传 True，只有
正常 `stop` 的最终文章可接受，首轮合法的 `tool_calls` 可继续协议。`length`、拒答或未知
完成原因均产生稳定失败。它和 `max_input_tokens` 一起进入普通 complete、complete_with_tools
协议及相关测试替身。
每调度点最多一次 writer 调用，失败立即告警并结束，直到下个调度点；没有同点重试旋钮。

## 9. 预算

- 站方日限 20 篇/账号；本地 `max_posts_per_day` 范围 1..5，默认 2。只约束本机器人记录的
  投递，同账号网页手工发文也会占站方额度，所以仍必须处理远端 429。
- 本地「日」用 **UTC+8 零点**，与站方 `dayStart` 同口径，**不跟系统时区** ——
  服务器 TZ 若是 UTC，用本地零点会把头 8 小时发的东西算进前一天。
- 当天占用 = 当天计费区间覆盖的 `published` 行数 + 全部 `inflight/unconfirmed` 行数。
  一行只计一次；不确定行跨日持续占 1，不能零点释放。新投递的预留与状态切换在同一个事务，
  加入后不得超过上限。单纯的生成失败、确定拒绝、`retry_wait` 和 `abandoned` 不占额。
- `budget_from_day` 取 POST 前预留的 UTC+8 日期。收到成功或对账确认后，
  `charged_through_day` 取本地确认日（时钟回退时至少为起始日）；该投递在两者闭区间内每天
  各计 1。这样跨午夜或次日补记不会漏掉真实发布日，代价是可能多占几天的本地额度。
  不把对账时间冒充站方真实发布时间，字段 `confirmed_at` 仅代表本地确认时刻。
- 429 等确定未发布会清空本次计费区间；稿库下次尝试重新预留，以新尝试日期为起点。
- 预算耗尽只阻止生成/POST，不阻止只读对账；每个账号每个 UTC+8 日最多一条预算耗尽日志。
  降低配置上限后已有占额不释放，直到占用低于新上限才允许新投递。
- 人工解决不确定行需要核实站方事实，事务性更新状态与额度；首版不提供模型工具或聊天命令
  执行此操作，也不自动删除未知结果记录。

## 10. 本地预校验

两条来源都走同一条发布预处理：先验证字段类型，再对标题、描述、正文调用共享 Redactor，
然后规范化、校验与计算指纹，最后落库和发布。标题落库前也必须脱敏。不得只在 MCP 路径脱敏。

标题与描述去首尾空白；以下长度按 JavaScript 的 UTF-16 code unit 计算，与上游 `.length`
一致，不能直接使用 Python `len()`。截断不得切开代理对；拒绝孤立代理字符。

| 字段 | 上限 | 超长处置 |
|---|---|---|
| 标题 | 30 字符 | 截断 |
| 描述 | 100 字符 | 截断 |
| 正文 | 250000 字符 | **视为生成失败** |

标题与描述是元数据，截断可接受；**正文截断会毁文**，所以宁可判失败。
空值或纯空白一律按失败处理，正文只检查不修改其原始空白。最终发送、存储标题、搜索标题与
指纹必须使用同一份预处理结果；不得在计算指纹后再次变换正文。脱敏后的扩张也计入长度。
稿库校验失败继续下一文件；现写校验失败结束本次生成。栏目变更产生的远端 400 正常进入
`rejected`，不声称本地能预知站方数据库中的栏目状态。

## 11. 落库与隐私

新增两张表；以下为预期 schema，时间戳沿用 Store 的 REAL epoch 秒，日字段为 UTC+8 的
`YYYY-MM-DD`。`site_base_url` 使用配置中统一规范化后的站点地址，与稳定用户 id 一起隔离账号。

```sql
CREATE TABLE IF NOT EXISTS blog_posts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    site_base_url        TEXT NOT NULL,
    self_user_id         TEXT NOT NULL,
    task_name            TEXT NOT NULL,
    source_kind          TEXT NOT NULL CHECK (source_kind IN ('file', 'generated')),
    category_id          INTEGER,
    content_hash         TEXT NOT NULL,
    hash_version         INTEGER NOT NULL DEFAULT 1 CHECK (hash_version = 1),
    title                TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN
                         ('inflight', 'published', 'unconfirmed', 'retry_wait', 'rejected', 'abandoned')),
    site_blog_id         TEXT,
    attempts             INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    retry_after_day      TEXT,
    budget_from_day      TEXT,
    charged_through_day  TEXT,
    reconcile_attempts   INTEGER NOT NULL DEFAULT 0,
    last_reconciled_at   REAL,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    confirmed_at         REAL,
    reason               TEXT,
    UNIQUE (site_base_url, self_user_id, content_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_blog_posts_site_id
    ON blog_posts (site_base_url, self_user_id, site_blog_id) WHERE site_blog_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_blog_posts_status
    ON blog_posts (site_base_url, self_user_id, status, last_reconciled_at);

CREATE TABLE IF NOT EXISTS blog_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    site_base_url     TEXT NOT NULL,
    self_user_id      TEXT NOT NULL,
    task_name         TEXT NOT NULL,
    scheduled_at      REAL NOT NULL,
    task_order        INTEGER NOT NULL,
    selected          INTEGER NOT NULL CHECK (selected IN (0, 1)),
    status            TEXT NOT NULL CHECK (status IN
                      ('queued', 'running', 'skipped', 'finished', 'failed', 'interrupted')),
    post_id           INTEGER REFERENCES blog_posts(id),
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    reason            TEXT,
    UNIQUE (site_base_url, self_user_id, task_name, scheduled_at)
);
CREATE INDEX IF NOT EXISTS idx_blog_runs_pending
    ON blog_runs (site_base_url, self_user_id, status, scheduled_at, task_order);
```

`CLAUDE.md` 的既有不变量是「不得把消息正文写入日志或 SQLite」。本子域的处置，
需要单独记一条决策：

- **落**：脱敏后的待发布标题、指纹、调度/账号/栏目元数据、状态、计费区间、站方 id；
- **不落**：正文本身、模型请求体、模型响应体；
- 日志同样只出任务名、状态、字数、站方 id —— 正文片段一律不出。

标题是在 POST 前保存，不能以“已经公开”为理由。例外仅允许脱敏后的待发布标题用于对账，
正文、描述、任务提示词和模型请求/响应不落 SQLite、日志或临时草稿文件；人维护的稿库文件
是输入来源，不由机器人复制为持久生成缓存。正文虽不存储，仍通过指纹参与对账。

不新增稿库游标表，选稿依据 §7.4 的状态而非“指纹存在即已发布”。`blog_runs` 用来去重调度，
`blog_posts` 用来去重内容与计费，两者职责不能合并。首版不自动清理这两表，防止删除记录后
重新发布；将来若增加保留策略必须明确去重保证的变化。不确定记录不能按年龄删除。

## 12. 对既有代码的改动

| 位置 | 改动 |
|---|---|
| `config.py` | 新增 `blog` 段解析与校验（§4.2）；`Capability.command` 放宽的影响面 |
| `capabilities.py` | 新增无命令的 `blog_write`；`command` 放宽为可空；过滤 `CAPABILITY_COMMANDS` |
| `mcp/adapters.py`、`mcp/registry.py`、`mcp/runtime.py` | feature + 工具双键映射及全部装配/注入消费者；发文工厂委托既有清洗器并强制总预算 |
| `core/worker.py` 与模型协议 | 可选 `require_complete` / `max_input_tokens`；发文拒绝截断及超预算请求；复用模型 gate 与两轮工具协议 |
| `store.py` | 两表、原子调度领取、状态转换、原子预算预留、恢复和只读对账元数据 |
| `site/client.py` | 单次发布与有界分页搜索；复用正文读取，不对 POST 做隐式重试 |
| `app.py`、新增 `blog/service.py` | 装配、扫描与串行消费、取消顺序、隔离故障 |
| `texts.py` | 新增文案常量 |
| `logging_setup.py` | `log_event` 白名单新增本子域用到的字段名 |
| `config.example.yaml`、`docs/usage/USAGE.md` | 配置、错过调度点策略、不确定结果占额及人工核实说明 |
| `INTERFACES.md`、`DESIGN_DECISIONS.md` | 实现前登记全部新合同及对既有合同的修订，不改写上游材料 |

新增的日志字段必须逐项加进 `log_event` 白名单 —— 该函数**静默丢弃**不在白名单里的字段，
忘了加不会报错，只会让日志缺字段。

## 13. 测试

以下是风险检查清单，不要求逐条新增测试。按用户“测试少一点”的要求，本次实施的自动化
范围以 [实施计划 §7](BLOG_PUBLISH_IMPLEMENTATION_PLAN.md#7-精简测试清单) 为准：目标 18 个
新增可收集用例，复用现有回归，其余边界做定向自查；发现实际缺陷再增加聚焦测试。

照既有规矩，**不开真连接**：

- `planner/service`：同分钟多任务按序；第一篇跨分钟不漏掉已领取的第二篇；超过 5 分钟
  未开始的点跳过；重启同分钟、重复扫描、时钟回拨均不重复领取；停机期间不补发；
  must/maybe × 稿库/现写四种组合，概率未中后重启不重新掷骰；生成失败同点不重试。
- `drafts`：front matter 类型、缺字段、损坏文件不堵队首；按全部六种投递状态选稿；
  429 次日按原指纹复用同一行，栏目变化不重投；累计 3 次后放弃。
- `writer`：模型/MCP/时钟全注入；至多一次工具调用、第二轮无工具；工具关闭/不可用路径；
  模型 gate 在等待工具时释放；180 秒总超时；格式错误、正文超长、输入超预算和 token
  截断均不发布；不得把动态任务提示词或工具正文放进 system。
- `publisher`：`httpx.MockTransport` 模拟落库后超时、发出前失败、5xx、非法响应、缺 id、
  取消和成功后本地写库失败；全部保留不确定状态，不触发第二次 POST。
- `reconcile`：同作者同标题不同正文不命中；同名不同作者不命中；多个精确匹配不补记；
  唯一作者 id/标题/正文指纹匹配才补记；覆盖分页、栏目隐藏、候选超限、详情失败和空结果，
  均不得授权重投；12 次未确认后保持占额，轮转不饿死后面的记录。
- 崩溃恢复：领取后生成前、生成后插投递行前、插 inflight 后 POST 前、服务端提交后回应前，
  分别断开再恢复；现写稿不重新生成，inflight 降为 unconfirmed，文件稿 retry_wait 可恢复。
- 预算：上限 1 时第一篇不确定挡住第二篇；预留与投递状态事务原子性；UTC+8 午夜、
  延迟到次日确认、跨日不确定占额、429 释放与重试重新预留；耗尽仍允许只读对账。
- MCP 隔离：`search` 与 `blog_write` 绑定同一个 Exa，互换配置顺序仍使用各自参数、
  schema、结果数和 limiter；旧能力回归。专用发文适配器超长结果必须被预算拦住/裁剪。
- 配置校验覆盖 §4.2 全部规则及关闭态兼容；无命令能力不出现在命令解析或帮助中。
- 隐私与文本：稿库/模型两条来源的标题、描述、正文均注入假密钥验证脱敏；日志、SQLite、
  临时文件不出现密钥、正文或模型请求；UTF-16 边界、非 BMP 字符及脱敏增长后的长度。
- 生命周期：后台故障不结束聊天/评论；关闭顺序不会在客户端关闭后调用 POST/工具；
  `blog.enabled=false` 不创建后台任务、不调用模型、不改变既有聊天行为。

## 14. 明确不做

- 不做 Web 配置界面（用户选择配置段）。
- 不做稿件级 `publish_at`：任务调度已够，稿库按先进先出取。
- 不做稿件级栏目覆盖：栏目归任务。
- 不对不确定结果自动重发；只有确定未发布的稿库 429 可以在后续调度点有界重试（§7）。
- 不持久化现写稿正文，不承诺进程崩溃后恢复原稿；不补发停机期间错过的调度点。
- 不做多步研究工具循环、不允许模型选择写入工具或发布栏目。
- 不做栏目 id 的自动解析（§2.2）。
- 不使用 `PUT /api/blogs/:id`：发布后不再改文。

## 15. 验收

1. 配置一个 `must` 现写任务，把调度点设到一分钟后：到点自动成文并发出，
   `blog_posts` 里出现一行 `published` 且带 `site_blog_id`；
2. 模拟站方提交后连接中断：转 `unconfirmed`；可查到唯一精确匹配时补记，否则持续待确认，
   包括隐藏栏目与查询空结果，均没有第二次 POST；
3. 稿库放两篇：按文件名顺序发完，第三轮空队列时 must 告警；另模拟首篇 429，次日按原行
   重试，不能因“指纹已存在”永久跳过；
4. 上限为 1：第一篇不确定时，第二篇被本地拦住；跨 UTC+8 午夜仍占额，但对账继续；
5. 同一分钟发布后重启，不重新生成；maybe 稿库概率未中时也不发布、不重新掷骰；
6. 同时启用聊天搜索与发文、共用一个 Exa 绑定，聊天结果数和限流策略不受发文配置覆盖；
7. 生成中/投递中取消后重启，行为符合 §7.4，且任何持久介质均没有新增模型正文副本；
8. `pytest tests -q` 全绿，且输出无警告（`filterwarnings = ["error"]`）。自动测试全部使用替身，
   真实发布只在部署者明确启用后执行，不能把站点写操作作为测试套件的一部分。
