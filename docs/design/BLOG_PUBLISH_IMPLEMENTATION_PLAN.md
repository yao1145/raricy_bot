# 定时发文实施计划：并行分工版

| 项 | 值 |
|---|---|
| 日期 | 2026-09-19 |
| 状态 | 已执行完毕（T0…T7）；本文是当时的交接文件，保留备查。功能已实现且默认关闭 |
| 行为依据 | [BLOG_PUBLISH_DESIGN.md](BLOG_PUBLISH_DESIGN.md)，2026-09-19 审查修订版 |
| 执行规模 | 1 个主代理 M + 最多 3 个子代理 A/B/C，同时活跃不超过 4 个 |
| 测试策略 | 目标 18 个新增 pytest 可收集用例，参数化展开后计数；复用现有回归测试 |

## 1. 交付目标与范围

交付默认关闭的自动发文子域：按 UTC+8 调度，从 Markdown 稿库或模型取得文章，经过统一
脱敏/预校验后单次投递；SQLite 保存调度与投递元数据，结果不确定时只读对账并持续占额。
同时完成实现所需的配置、MCP 隔离、模型严格完成检查、运行文档和最低限度回归验证。

遵守仓库 AGENTS.md：使用 superpowers-lite，中文注释，无新增运行依赖，不修改上游材料，
不写真实密钥、不访问真实站点或模型进行测试。不在这次实施中部署、启用生产发文或提交远端。
不扩展为多步研究工具循环，不保存现写稿正文，不实现编辑文章、补发停机任务或多实例运行。

本计划是交接文件。当前只编写计划；进入实施后才执行任务、创建子代理或修改运行代码。
所有清单初始均未完成。下文给出待实现的接口，不声称仓库已经存在这些方法。

## 2. 并行组织与文件归属

每个文件在任一时刻只允许一个写入者。所有代理共享同一工作目录，不要求额外 worktree，
不通过相互 cherry-pick 合并。任何代理都不得回滚他人的修改、全局格式化或顺手整理无关模块。
调用方发现接口不够用时，向 M 提交具体变更；由 M 修改合同，再通知受影响所有者。

| 所有者 | 独占文件/职责 | 不得直接改动 |
|---|---|---|
| M 主代理 | `config.py`、`capabilities.py`、`texts.py`、`logging_setup.py`、`app.py`；`blog/__init__.py`、`blog/models.py`、`blog/codec.py`、`blog/drafts.py`、`blog/planner.py`；全部文档与示例配置 | A/B/C 的实现文件，除非已明确移交 |
| A 存储与服务 | `store.py`、新增 `blog/service.py`；`tests/test_blog_publish_store.py`、`tests/test_blog_publish_service.py`；必要的 `tests/test_store.py` 兼容调整 | `app.py`、`config.py`、投递与模型代码 |
| B 站点与投递 | `site/client.py`、新增 `site/blog_models.py`、`blog/publisher.py`；`tests/test_blog_publish_delivery.py`；必要的 `tests/test_client.py` 兼容调整 | `store.py`、稿库、配置和 MCP |
| C 模型与 MCP | `core/worker.py`、`mcp/adapters.py`、`mcp/registry.py`、`mcp/runtime.py`、`blog/writer.py`；下文列出的 MCP/模型测试 | `config.py`、`capabilities.py`、`texts.py`、App 和 Store |

上表的源码路径均相对 `src/raricy_bot/`。`McpManager` 实际位于 `mcp/runtime.py`，
不要新建 `mcp/manager.py`。现有 `tests/test_blog.py` 测的是聊天博客引用，不承担自动发文测试。

测试文件也按所有权分开：

- M：`tests/test_blog_publish_config.py`、`tests/test_blog_publish_sources.py`、
  `tests/test_config.py`、`tests/test_capabilities.py`、`tests/test_app.py`、
  `tests/test_texts.py`、`tests/test_logging_safety.py`。
- A/B：如上表。A 的服务测试不修改 M 的 App fixture。
- C：`tests/test_blog_publish_model.py`、`tests/test_worker.py`、`tests/test_mcp.py`、
  `tests/test_mcp_adapters.py`、`tests/test_mcp_config.py` 及实际需要调整双键注入的
  `tests/test_mcp_*.py`。这里是兼容维护范围，不要求逐文件新增用例。
- `tests/conftest.py` 只由 M 修改；优先在各自测试文件放小替身，不搭建通用测试框架。

## 3. 执行顺序与放行条件

```text
T0 M：冻结合同、基础类型、配置与能力声明
  ├─ T1 A：Store 事务、恢复与预算 ──────────────┐
  ├─ T2 B：SiteClient + Publisher ────────────┤
  ├─ T3 C：MCP 隔离 + 严格模型调用 + Writer ───┤
  └─ T4 M：Codec + Drafts + Planner ──────────┤
                                              ↓
                       T5 A：BlogService 实际装配与生命周期
                       T6 M：App 接入、配置示例与运行文档
                                              ↓
                       T7 M：一次集成验证与交付
```

T1/T2/T3/T4 同时进行。T2 可按冻结的 Store 合同开发，用局部替身完成 HTTP 分支测试，
不等待 A 写完。T3 同理按 Codec 合同开发 Writer，不自行复制解析器。
T5 在 T1、T2、T3、T4 的实际接口均交付后进入真实装配，不以假实现冒充集成通过。
T6 的文档和 App 接入框架可在 T5 期间并行，实际启动/关闭验证等 T5 完成。

等待上游交付时，优先完成自己范围内的错误处理和交接说明，不启动重复探索子代理。
T1 完成后复用 A 执行 T5，不另开第四个子代理。B/C 完成后待命，只有具体失败属于其范围
才唤醒修正，不给每个任务再套一轮独立审查。

### G0：允许并行开工

T0 已提供真实基础类型、配置解析和明确的签名；接口说明已写入 INTERFACES。
并行任务不会争写这些文件。允许 `IMPLEMENTED_FEATURES` 与工厂表在施工中短暂不一致，
但这不是可发布状态；不得放入假工厂骗过完备性检查，T3 交付时必须消除不一致。

### G1：允许集成

四条实现线分别返回：修改文件、实现的合同、运行过的检查及结果、已知限制。
没有占位 `pass`、无条件成功返回、隐藏未实现分支或绕过旧断言。M 检查调用形状与关键状态
转换，确认 T5 可以使用真实组件；此处不重复运行四套全量测试。

### G2：允许交付

18 个精简新增用例及受影响回归通过；最终全量测试执行一次。文档与代码一致，发文默认关闭，
没有真实发布请求、模型正文缓存或未处理的接口冲突。没有授权时不自动提交、合并或部署。

## 4. T0：主代理先冻结的合同

### 4.1 基础类型与依赖

M 创建空的 `blog/__init__.py` 和只含 DTO/常量的 `blog/models.py`。
该模块不 import config、Store、SiteClient、MCP 或 App，避免基础模块导入业务实现。
`site/blog_models.py` 由 B 创建，只含站点 DTO；其字段在 T0 先写入 INTERFACES，B 按约实现。

| 类型/配置 | 必须锁定的内容 |
|---|---|
| `BlogScope` | `site_base_url: str`、`self_user_id: str`；地址使用 Config 已规范化值 |
| `Draft` | 原始 `title/description/content: str` |
| `PreparedDraft` | 最终出站 `title/description/content: str`、`content_hash: str`、`hash_version=1`；仅内存流转 |
| `RunCandidate` | `task_name: str`、`scheduled_at: float`、`task_order: int`；不在 DTO 构造时掷骰 |
| `BlogRun` / `BlogPost` | 对应设计 §11 的记录快照；字段名与 schema 一致 |
| `BlogReservation` | `allowed: bool`、`post: BlogPost \| None`、稳定 `reason`；拒绝时不得持有额度 |
| `PublishOutcome` | `post_id: int \| None`、`status: str`、稳定 `reason`；不返回文章正文供落库 |
| `BlogTaskConfig` | name、tier、schedule、category_id、probability、prompt、drafts_dir；frozen，schedule 使用元组 |
| `BlogConfig` | `enabled=False`、`max_posts_per_day=2`、`tasks=()`；作为 Config 的默认字段，旧构造调用继续有效 |

`Draft` 和 `PreparedDraft` 必须区别，防止已计算指纹后再次脱敏或截断。
Store 方法只接收标题、指纹和元数据，不能接收整个 PreparedDraft 或正文参数。
模型完整输出不可出现在异常 repr、日志字段、SQL 或临时文件。

### 4.2 存储接口

以下均为 Store 的 async 方法，时间 `now` 显式传入，不在 SQL 内读真实时钟。M 在 T0
把下列输入、返回类型和稳定 reason 写入 INTERFACES；A 实现后不能随意给消费者另造别名。

| 方法 | 输入与返回 | 必须保证 |
|---|---|---|
| `get_blog_run` | scope、task_name、scheduled_at → BlogRun 或 None | 调度重放时先查，再决定是否掷骰 |
| `claim_blog_run` | scope、candidate、selected、now → 新 BlogRun 或 None | 唯一键原子领取；冲突不能覆盖已有随机决策 |
| `take_blog_run` | scope、now → BlogRun 或 None | 按 scheduled_at/task_order 取 queued；过期标 misfire；合法行原子转 running |
| `finish_blog_run` | scope、run_id、status、reason、now → None | 仅允许规定的执行终态；不得改投递占额 |
| `find_blog_post` | scope、content_hash → BlogPost 或 None | 按账号隔离；不是“存在即已发布” |
| `blog_budget_used` | scope、day → int | 设计 §9 的已确认区间 + 全部不确定占额 |
| `reserve_blog_post` | scope、run_id、title、content_hash、hash_version、source_kind、category_id、max_posts_per_day、now → BlogReservation | 一个事务内复查 run、指纹状态、429 次数/日期/原任务/栏目、预算，创建或复用行、attempts 加一并关联 run.post_id |
| `finalize_blog_post` | scope、post_id、status、site_blog_id、reason、now → BlogPost | 状态转换与额度更新同事务；retry_after_day/计费日期由 Store 按规则推导，不让调用方任填 |
| `recover_blog_state` | scope、now → 恢复数量摘要 | inflight → unconfirmed，queued/running → interrupted；保留额度，不调用模型/站点 |
| `blog_posts_to_reconcile` | scope、now、limit=10 → BlogPost 元组 | 排除已查 12 次的行，按最近查询时间公平轮转 |
| `note_blog_reconcile` | scope、post_id、reason、now → 更新后快照 | 查询失败/无匹配也增加次数并更新查询时间；不改变额度 |

`finalize_blog_post` 不得成为任意改状态的后门：只接受设计 §7.2 的迁移；published 重复确认
必须幂等，不能再次计费。unconfirmed 不能自动迁移到 rejected/retry_wait/abandoned。
所有复用/终结都复查 scope。reserve 拒绝原因至少区分 `budget_exhausted`、`already_published`、
`awaiting_confirmation`、`not_retryable`；写库失败抛异常让服务停发，不伪装成额度用尽。

### 4.3 站点和业务接口

| 接口 | 约定 |
|---|---|
| `SiteClient.publish_blog(*, title, description, content, category_id)` | 返回 `BlogPublishResult(outcome, blog_id, code, reason)`；outcome 为 published/rejected/rate_limited/unconfirmed；reason 为稳定原因，不带响应正文 |
| `SiteClient.search_blog_titles(title, *, page=1, per_page=50)` | 返回 `BlogSearchPage(items, has_next)`；每项提供合法 id、title、author_id；坏结构/无法完整解析必须失败，不能过滤后伪装为空结果 |
| `SiteClient.fetch_blog_context(blog_id)` | 复用现有读取，不改既有消费者；正文仅临时计算指纹 |
| `parse_draft(text)` | `blog/codec.py`：front matter + Markdown → Draft；无 I/O |
| `prepare_draft(draft, *, redactor)` | 同文件：类型校验、脱敏、UTF-16 边界、最终指纹 → PreparedDraft；失败不含原文 |
| `next_file_draft(task, *, scope, store, redactor, day)` | `blog/drafts.py`：异步扫描、解析并按状态选稿 → PreparedDraft 或 None；坏文件继续，不保存副本 |
| `due_runs(tasks, *, scan_start, now, startup)` | `blog/planner.py`：返回按时间/声明顺序排列的 RunCandidate；固定 UTC+8，限制 5 分钟窗口 |
| `select_run(task, *, random_value)` | 同文件纯函数：must 恒真，maybe 比较概率；服务先查执行键才调用注入随机源 |
| `BlogWriter.write(task)` | async → Draft；内部复用模型/Registry，外层 180 秒总超时 |
| `BlogPublisher.publish(run, task, prepared)` | async → PublishOutcome；确认会话账号、事务预留、单次 POST、终结元数据 |
| `BlogPublisher.reconcile_once()` | async → None；有界只读对账，写确认元数据，永不 POST |
| `BlogService.start()` / `stop()` | async、幂等；服务拥有扫描/消费/对账任务，不拥有共享客户端的 close 权限 |

补齐两个容易导致跨代理误解的边界：

- `_decode()` 会把非法信封变成带 HTTP 状态的 SiteError。B 不能把所有 `SiteError(400/403)`
  都交给 Publisher 当作确定拒绝；只有解析成功的业务信封才返回 rejected。传输错误、
  无效响应、缺少成功 id 均返回 unconfirmed。`CancelledError` 继续传播，预留行保持 inflight。
- 对账的正文指纹使用原始远端标题和正文按同一 JSON 算法计算，不重新经过会变化的 Redactor；
  远端返回不是待发布草稿，不需要再次截断。重新读取详情后也核对标题，防止两次 GET 间编辑。

### 4.4 T0 的具体操作与完成条件

1. M 阅读当前 dirty diff，只记录本任务范围，不覆盖既有改动。
2. 将本节合同、设计 schema、状态和默认值登记进 INTERFACES；在 DESIGN_DECISIONS
   使用当时下一个可用 D 编号记录接口例外、标题例外、保守恢复、跨日计费和 MCP 双键。
3. 实现基础 DTO、BlogConfig/BlogTaskConfig 及全部加载期校验；相对稿库路径以配置文件目录解析。
4. M 添加无命令 `blog_write` 能力与所需 texts 常量、日志字段声明；与 C 对齐工具白名单、
   result_count=1 和 query 长度规则，C 不回头修改 config/capabilities。
5. M 完成 §7 的 3 个配置用例，只运行自己的配置文件与受影响旧配置/能力测试。
   工厂尚未交付引发的预期完备性失败明确记录，不删断言；接口/默认配置其他失败必须先修好。
6. 广播 G0 合同及文件归属给 A/B/C，随后并行启动 T1/T2/T3，M 开始 T4。

## 5. 子代理任务卡

### T1 — A：持久状态、原子占额与崩溃恢复

**输入**：设计 §6、§7、§9、§11；T0 类型和 Store 合同；D-90。

**实现顺序**：

1. 在现有 `_SCHEMA` 追加两张表和索引，保持 `_connect()` 幂等，不迁移或删除旧聊天/评论数据。
2. 用 `_execute()` / `_run_locked()` 的线程锁实现领取与记录读取，所有多步骤写操作显式事务。
3. 完成 reserve 的联合判定与 run.post_id 关联；不确定行也计入预算，不只数 published。
4. 实现受约束的状态终结；429 原行保留、清空本次计费区间；published 反复补记不重复计费。
5. 实现启动恢复及对账公平轮转；不因查询次数耗尽清除未知结果，不挂进旧 `_prune()`。
6. 运行本任务 2 个新增状态场景以及既有 Store/评论 Store 回归，返回明确交接。

**交付条件**：B 无须拼 SQL 即可投递和对账；A 后续 Service 无须绕开 Store 维护第二套状态。
正常数据库重开只恢复元数据，不重新生成文章。

### T2 — B：HTTP 边界、单次发布与保守对账

**输入**：设计 §2、§7、§10；T0 的 Site DTO、Store 和 PreparedDraft 合同。

**实现顺序**：

1. 建立纯 `site/blog_models.py`，实现 publish_blog 的响应分类；不模仿聊天/评论 POST 的
   401 自动重登重投。Publisher 在预留前 ensure_session，真正发布只发一次。
2. 搜索 GET 复用有界响应读取，title 用 params 编码，解析分页、author_id 和 UUID。
   `_request_envelope` 本身不保证响应字节有界，不能为了“复用”丢掉现有 bounded 机制。
3. 实现 Publisher：先确认账号，调用 reserve，发出一次 POST，按 outcome 调用 finalize。
   提交失败或取消不能释放不确定额度；成功后落库失败使本子域停发。
4. 实现 reconcile_once：只取当前账号待查行；最多 10 行/轮、5 页/行、10 篇正文/行；
   author_id/标题/指纹唯一匹配才确认；空结果、失败、上限、多匹配均维持 unconfirmed。
5. 对账本轮应按去重后的 blog id 计候选，分页重复项不能假造多个匹配；任何不完整查询均
   不确认成功。记录一次查询尝试，不能每页算一次或靠重启重置 12 次上限。
6. 用 MockTransport 执行 §7 的 5 个投递用例；A 未完成时可用局部 Store 替身，G1 前
   将核心发布/对账场景接到实际 Store 验证，替换测试装配而不是复制另一组测试。

**交付条件**：没有隐式第二次 POST，搜索没有负向凭证语义；日志不暴露 title/query/body。
发布成功或失败只返回稳定结果，不在客户端里修改配额或稿库文件。

### T3 — C：MCP 隔离、模型约束与 Writer

**输入**：设计 §8；T0 的能力声明、Codec 与模型可选参数合同。

**实现顺序**：

1. 将适配器映射改为 `(feature_name, model_tool_name)`；同步 runtime、tools_for、execute
   的 schema/参数/limiter/结果查找及注入测试。不改模型侧工具名，也不复制 Provider/pool。
2. 添加 blog_write 工厂，按工具委托现有清洗器，绑定级 query 上限取原工具与发文配置的
   较小值；发文适配器出口再次校验整体 token 预算。不能以通用透传填空。
3. 扩展普通 complete 与 complete_with_tools 的 `require_complete=False`、
   `max_input_tokens=None`；在实际网络请求前检查完整输入，在最终输出提取时检查 finish_reason。
   默认参数保留聊天原行为；新的稳定失败不能被 `_map_error` 吞成可重试未知错误。
4. 实现 Writer：静态 system、任务 user、工具 tool；只有明确授权的 blog_write；
   工具不可用时按设计降级。纯文本调用在外层持模型 gate，工具协议逐次请求持 gate。
5. 总超时 180 秒，最多一次 writer 调用、至多一次工具执行；底层既有有界重试不扩展。
   通过 `parse_draft` 返回 Draft，不自行截断、落稿、选栏目或调用 Publisher。
6. 新增 3 个重点用例，调整已有 MCP/模型测试中的映射和替身即可；不逐工具复制既有解析测试。

**交付条件**：search 与 blog_write 共用同一 Exa 绑定、颠倒配置顺序均不串策略；
未传新参数的旧模型调用仍按原行为工作。缺少严格调用能力的发文替身/客户端明确失败，
不得静默吞掉 `require_complete` 继续发布。

## 6. 主代理本地任务与集成

### T4 — M：统一文本处理、稿库选择与纯调度

可与 T1/T2/T3 并行，独占 codec/drafts/planner。

1. 写 parse_draft 与 prepare_draft：safe_load，严格字段类型，正文保留原始换行；
   共享 Redactor 后校验 UTF-16 长度，标题/描述合法截断，正文超长拒绝，最终 JSON 指纹。
2. 稿库只读取任务目录内普通 Markdown 文件，按文件名排序；不复制或重命名输入。
   读取/解析错误记录稳定原因后继续，候选按 §7.4 查询状态。文件 I/O 不长期阻塞事件循环。
3. next_file_draft 返回已准备的对象；现写路径只在 writer 返回后 prepare 一次。
   Publisher 不能重做文本变换，Store 完全看不到正文。
4. Planner 固定 UTC+8、当前分钟启动、运行期 5 分钟枚举窗口，按时间与配置顺序输出。
   概率选择独立，Service 先查已有 run，才使用注入 random。
5. 写 §7 的 2 个来源/调度用例，交付真实 Codec 后通知 C；完成 T4 后通知 A。

### T5 — A：BlogService

T1 完成后复用 A。进入真实装配需要 T2/T3/T4 全部交付；等待期间可按已冻结合同准备服务代码。

1. 构造参数按关键字注入 config、scope、Store、Writer、Publisher、Redactor、clock、sleep、random。
   不由 Service 创建 SiteClient、模型或 Registry，不持有它们的关闭权限。
2. start 先 recover，再创建扫描/串行消费/只读对账任务。扫描不等待模型，避免后续分钟漏点。
3. 消费器按顺序：probability → 预算预检 → 取稿/生成 → prepare → Publisher；
   每个 run 无论跳过、失败或结束均有元数据终态。POST 后运行记录终态不能替代投递状态。
4. 无法恢复的现写执行只告警；429 稿库在新调度点复用旧 post，不循环重投同一 run。
5. stop 先停领取，再取消并等待后台任务；取消不吞为普通错误，也不清空 inflight。
   App 总关闭预算现为 10 秒，不能在 stop 里等待一个 180 秒 writer 自然结束。
6. 运行服务场景，向 M 提供准确的构造和启动/关闭方式，M 接 App；不自行编辑 app.py。

### T6 — M：App 接入与文档

1. `blog.enabled=false` 时不构造或启动 BlogService；既有 Store 初始化新增空表可以接受，
   但不能启动发文任务、调用模型或访问博客发布接口。
2. enabled 时在账号、Store、模型、MCP 可用后装配，共享 `_model_gate` 与 Registry。
   博客子域异常不影响聊天/评论和既有健康判定；失败信息保留稳定原因供运维处理。
3. App shutdown 先停止 BlogService，再关闭 MCP/模型/SiteClient/Store；启动中失败也能清理。
4. 核对日志字段白名单，只记录任务/状态/数量/id/稳定 reason；不放开任意 error 文本。
5. 更新 config.example.yaml（关闭为默认），在 USAGE 的维护者部分写启用方法、调度行为、
   生成 token 预算、永久不确定占额、429 与改名语义。聊天用户帮助不宣传一个不可输入的命令。
6. 补齐“人工核实”操作说明：先关闭 blog、备份 SQLite，按 scope/post id 查明站方事实；
   确认已发要校验作者/标题/正文指纹并补 site id，确认未发才可清理占额，核实不了就保留。
   任何维护状态更改都必须在事务中同时保持预算与执行记录一致；首版不新增自动解锁工具。
7. 对照实现更新 INTERFACES、DESIGN_DECISIONS、设计与索引，不把尚未执行的真实验收写成已完成。

### T7 — M：一次集成验证

1. 检查实际 diff 是否超出所有权和需求：重点查额外重试、正文落盘、两份指纹算法、跨 feature
   fallback、未受约束的 finalize、没有停止的后台任务；不要求逐模块再请审查代理。
2. 运行所有新增 `test_blog_publish_*.py`，集中使用真实 Store/真实业务组件与网络替身。
3. 最后执行一次 `python -m pytest tests -q`。失败交给对应文件所有者修复，先重跑失败选择；
   只有修改引入了新的跨模块风险才再次跑全量，不在每次交接后重复全量。
4. 交付包含实现文件、验证结果、默认关闭说明、已知保守恢复代价。授权范围仍不包含部署或发文。

## 7. 精简测试清单

目标为 **18 个新增可收集用例**，参数化展开也计入，不用一个大参数表暗中扩成几十例。
这是工作量预算，不是牺牲关键错误处理的硬上限；只有发现原清单无法覆盖的新实际缺陷时才
增加针对性用例并说明原因。已有测试为接口变更做兼容修改不计作“新增覆盖”。

| 编号 | 所有者 / 文件 | 数量 | 关键证据 |
|---|---|---:|---|
| K1 | M / `test_blog_publish_config.py` | 3 | 缺省关闭与合法配置；prompt/drafts_dir 互斥拒绝；日预算越界拒绝 |
| K2 | A / `test_blog_publish_store.py` | 2 | 同键重复领取/重开恢复不重做；原子占额、跨午夜未确认占额及确认不重复计费 |
| K3 | B / `test_blog_publish_delivery.py` | 3 | 参数化 timeout、非法 5xx 响应、200 缺 id：每种仅一次 POST，持久保留不确定占额 |
| K4 | B / 同上 | 1 | 同作者同标题异文先不确认、随后唯一指纹匹配才补记；两轮对账均不 POST |
| K5 | B / 同上 | 1 | 文件稿 429 释放额度，次日原行重试，成功后不再选中 |
| K6 | C / `test_blog_publish_model.py` | 1 | search/blog_write 同绑定策略隔离，发文结果受总预算限制 |
| K7 | C / 同上 | 2 | 严格模式拒绝 length；第二轮工具结果导致输入超预算时不发第二次模型请求 |
| K8 | M / `test_blog_publish_sources.py` | 2 | 实际稿件经解析/脱敏/UTF-16/指纹的出站一致性；注入时间/random 的概率、顺序与过期决策 |
| K9 | A / `test_blog_publish_service.py` | 2 | 模型取材到实际 Store/Publisher 的单篇闭环；POST 期间取消并重启只对账、不重生成 |
| K10 | M / `test_app.py` | 1 | 关闭 blog 时沿用旧 App 路径且无新增模型/发布调用 |
| 合计 | | 18 | |

每个测试围绕一个真实业务场景，可以检查该场景的关联副作用；不要为凑数量将无关失败塞进
一条超长测试。K3 同时断言日志与数据库不含假密钥/正文，避免另写重复隐私测试。
K4 第一轮的搜索结果可包含一篇不同作者文章和一篇同作者异文，用真实候选匹配覆盖误确认风险。
K5 必须用实际 Store 与稿库选择器，不能用一个永远返回“可选”的假 Store 掩盖原问题。

设计 §13 是完整风险检查清单；本次按用户要求减少新测试，并非逐条新增自动测试。
已由现有用例覆盖的 MCP 参数/解析、通用 Redactor、SQLite 线程锁等直接复用。
其余诸如更多配置类型、坏稿跳过、所有 HTTP 拒绝码、分页上限、多匹配、12 次查询停止、
三次 429 终止等，以实现者定向自查和集成 diff 核对完成首版检查，发现疑点再补聚焦用例。
不做全组合矩阵、快照文案测试、真实网络 E2E、性能基准或每个私有函数的孤立单测。

### 分阶段运行命令

命令从仓库根执行。新增文件尚未存在时不运行；M 的最终 glob 由 pytest 自己发现文件，
避免依赖 PowerShell 展开规则。

```bash
# A：Store 交付时
python -m pytest tests/test_blog_publish_store.py tests/test_store.py tests/test_comment_store.py -q

# B：投递交付时
python -m pytest tests/test_blog_publish_delivery.py tests/test_client.py -q

# C：模型/MCP 交付时
python -m pytest tests/test_blog_publish_model.py tests/test_worker.py tests/test_mcp.py tests/test_mcp_adapters.py tests/test_mcp_config.py -q

# M：只发现本功能新增测试；App 关闭态用例在最后全量中运行
python -m pytest tests -q --override-ini=python_files=test_blog_publish_*.py

# M：最终只跑一轮全量，无新增改动时不重复
python -m pytest tests -q
```

## 8. 迁移、回退与完成记录

Schema 采用追加式建表，旧表不变。首次启用前由部署者备份 SQLite；本轮实施只用临时测试库，
不对用户运行库做迁移演练。回退优先将 `blog.enabled=false` 并重启，保留 blog_runs/blog_posts，
不能删表“解除堵塞”。回退到旧代码前也需核查 MCP 双键和模型可选参数是成套版本，不能只
撤回一半文件；保留未知结果记录，之后重新启用仍按恢复规则处理。

任务结束时主代理填写以下清单和实际命令结果，不将尚未执行的项目勾选为完成：

- [x] T0 合同、配置与基础类型
- [x] T1 Store 与恢复
- [x] T2 站点发布与对账
- [x] T3 MCP、模型与 Writer
- [x] T4 Codec、稿库与 Planner
- [x] T5 BlogService
- [x] T6 App 与运行文档
- [x] T7 新增场景 + 最终全量验证

最终验证：`python -m pytest tests -q` → **2819 passed**，输出无警告
（`filterwarnings = ["error"]`，任何警告都会失败）。新增的 `test_blog_publish_*.py` 命中 20 例。

与计划的出入（都要点写出来，不藏在正文里）：

- **测试预算 18 → 21 例**（另有 1 例加在 `test_mcp_adapters.py`，属兼容维护范围不计入 K）。
  三处超出各有具体理由，都是**计划清单之后才发现**的真实缺陷的安全回归：
  T5 复审发现的 reconcile 账号检查缺口、终审 item 10 的 `**kwargs` 静默吞掉、终审 item 9 的
  `rejected` 分支（既释放额度又永久终结指纹）。计划 §7 明确允许这种增加。
- **`BlogWriter` 的 `clock` 参数没有落地**（合同已相应修订）：唯一计时需求是 180 秒上限，
  用 `sleep` + 可注入的 `timeout_seconds` 表达即可，多一个无使用点的参数是死参数。
- **终审给出 26 条 deferred + 5 条新增 Minor（N1–N5）**，其裁决与逐条处置见
  `.superpowers/sdd/BLOG_PUBLISH_IMPLEMENTATION_PLAN/progress.md`。终审结论：无 Critical、无 Major，
  「按现状发布」，并附三条应在合并前修的低成本硬化（已修）。
- **真实站点验收（§15 的八条）没有执行**：本仓库的测试全部使用替身，发文默认关闭。
  §15 要求的是部署者启用后的真机行为。此外有三项**只能**在真机上确认的假设，
  已写进 `docs/usage/USAGE.md` §2.4 的「启用前请做一次真实核对」。

**这份清单不代表功能已在真实站点验证过。** 它代表：代码已实现、合同已登记、自动化测试全绿、
五次独立复审（四条实现线各一次 + 终审）没有留下未处理的 Critical/Major。

## 9. 可直接分派的子代理说明

主代理先完成 T0，再把公共约束与对应任务卡一起发送，不要求子代理重新规划全项目。

公共约束：

> 你不是唯一在修改仓库的代理，不能回滚别人的改动。以当前 AGENTS.md、
> BLOG_PUBLISH_DESIGN.md 和主代理已冻结的 INTERFACES 为准。只写分配给你的文件，
> 遇到共享合同问题先向主代理发出具体建议，不自行改签名。不要部署、真实联网测试、
> 提交或新建更多子代理。新增测试限定在本任务的 K 编号，已有测试只做必要兼容调整。
> 完成后报告改动文件、接口、实际测试命令与结果、未解决问题，不把仅用替身的结果说成全链路通过。

给 A：

> 执行 T1，负责 store.py 与 test_blog_publish_store.py，必要时兼容 test_store.py。
> 核心是原子领取、预算与投递状态同事务、未知结果不释放、重启不重新生成。完成 T1 后交接，
> 等主代理通知组件齐备再执行 T5，负责 blog/service.py 与 test_blog_publish_service.py。
> 不修改 App、SiteClient、配置或模型文件。

给 B：

> 执行 T2，负责 site/client.py、site/blog_models.py、blog/publisher.py 与投递测试。
> 只对合法业务信封判断确定拒绝；网络/解析问题均保留未知结果。不得用搜索空结果触发 POST。
> Store 按冻结合同调用；可以先用局部替身，交付前 K4/K5 接实际 Store/稿库组件，
> 不复制测试。缺少接口向主代理请求合同修订，不编辑 store.py。

给 C：

> 执行 T3，负责 MCP 双键映射、发文适配器、core/worker.py 可选严格参数与 blog/writer.py。
> 同一上游绑定被多个 feature 使用时必须隔离策略，Provider 仍共享。配置、能力表和静态文案
> 由主代理提供，你不改这些文件。兼容旧调用默认行为，测试按 K6/K7 和既有 MCP/模型回归执行。
