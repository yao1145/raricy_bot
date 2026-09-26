# 内部接口与行为契约

本文件维护跨模块必须共同遵守的行为边界与代码入口。**签名、字段、常量和默认值以对应源码为准**，
不再手抄整份类型定义；改变公共接口仍须检查全部消费者、测试和本文件。理由见
[决策记录](DESIGN_DECISIONS.md)，操作说明见 [文档索引](../README.md)。

保留原 §0–§53 编号以兼容现有注释。旧版细分节号（如 §30.3）及逐条规则见
[2026-09-20 完整快照](../archive/2026-09-20/INTERFACES.md)；归档不入库，
Git 恢复方式见 [归档索引](../ARCHIVE.md)。旧稿不是另一份现行契约。

## 0. 工程约定

Python >=3.12，包根为 `src/raricy_bot/`。依赖和测试配置见 [pyproject.toml](../../pyproject.toml)。
异步编排；阻塞文件操作、SQLite 和 MCP stderr 的线程边界按各模块实现，不将阻塞 I/O 放进事件循环。
中文注释和 docstring、英文标识符；固定用户文案放 `texts.py`，日志走白名单。

## 1. `config.py`

入口：[配置类型与 load_config](../../src/raricy_bot/config.py)、[带注释的配置样例](../../config.example.yaml)。
配置对象冻结；字段默认值和校验规则集中在代码，不复制第二张全量字段表。

- 配置路径：`--config` > `BOT_CONFIG_PATH` > `./config.yaml`；文件上限 1 MiB。
- 密码、模型与 MCP 凭据只取环境变量；配置对象不得保存解析后的 MCP Key。
- 无效配置启动失败，错误不回显取值；未知普通键忽略，未知 MCP feature 拒绝。
- MCP、KB、记忆、评论和发文默认关闭；视觉由独立 `model.vision_enabled` 控制。
- 配额当前默认值：聊天 100 次/分钟、7950/8000 次滚动 24 小时；评论 20 次/分钟、
  7950/8000 次滚动 24 小时。普通回复阈值小于总阈值；评论总阈值校验上限 8000，
  聊天 8000 是默认值，不能误写成加载期硬上界。
- 部署细节查 [DEPLOYMENT.md](../usage/DEPLOYMENT.md)，不修改本地密钥文件来维护文档。

另有独立模块 [error_archive.py](../../src/raricy_bot/error_archive.py)（§2.1）。

加载拆成三个入口：`read_config_yaml()`（有界读取与 YAML 解析）、`parse_config(raw, *,
config_dir, secrets)`（唯一的字段/跨字段/URL 安全校验，GUI 与 CLI 共用）、以及组合凭据的
`load_config()`（CLI：路径优先级 + 环境变量）。`ConfigError` 带 `field` 与 `kind`
（`missing` = 还没填、`invalid` = 填错），供 Launcher 的草稿校验与结构化错误使用（D-124）。

## 2. `logging_setup.py`

入口：[日志与白名单](../../src/raricy_bot/logging_setup.py)。`LOG_FIELDS` 由
`FIELD_KINDS` 派生：字段名与取值类型一起登记，不在白名单里的字段、类型不合的取值
一律整条丢弃，**不截断也不转写**。四类取值：`TOKEN`（受控标识）、`NAME`（Python
标识符，如异常类名）、`LABEL`（配置短名称，如发文任务名）、`FRAMES`（`模块.函数:行号`
列表），另有 `MODULE`（npm 包路径）。新字段必须先登记再使用。

- `log_event` 先构造 `LogEvent`（已校验），再分别编码为控制台文本与归档 JSON。
- 控制台脱敏作用在 handler 的**最终输出**上（`RedactingFormatter`），覆盖消息、extra、
  `exc_text` 与 `stack_info`；不改写共享 `LogRecord`。`RedactingFilter` 只作为
  兼容入口保留，生产路径不用它。
- 非 `raricy.*` 命名空间的 WARNING 及以上不渲染原文：控制台与归档都换成受限的
  `third_party.failure` 事件（只留来源 logger 名）。低于 WARNING 的第三方行保持原样。
- 安全堆栈只留模块、函数与行号；`safe_stack` 对异常链与帧数设上限。未捕获异常、
  线程异常与 asyncio 未取回异常由 `install_exception_hooks` /
  `install_asyncio_exception_handler` 兜底，不转储异常 context。
- 后台任务用 `observe_task` 挂结束观察：`app.task_exit` 区分正常停止、主动取消、
  **逃逸取消**与异常退出。它只补观测，不吞 `CancelledError`、不重启任务。
- MCP 错误用结构化分类，不记录 stderr 原文或异常正文（D-111）。

## 2.1 `error_archive.py`

入口：[永久归档](../../src/raricy_bot/error_archive.py)。单写者 JSONL，按 UTC 日期
与大小分片（**跨 UTC 零点必须换片**，否则文件名里的日期就不再是内容的可靠上界）；
文件名含 `boot_id` 与递增序号，`os.O_EXCL` 独占创建，**旧分片只增不删**。
每条写入后 flush，ERROR/CRITICAL 额外 fsync，其余最迟每 `fsync_interval_seconds`
批量同步；关闭时同步。

三条硬约束：

- 归档与控制台使用同一凭据登记表；`_serialize` 先递归脱敏字符串值，再编码 JSON。
  不替换 JSON 语法、字段名或数值元数据，避免转义后的密钥漏匹配或数字密钥破坏结构。
  类型约束只保证字段形状合法，不能代替脱敏。
- 归档自己的状态事件在**锁外**发出。`Handler.handle()` 先拿 handler 锁再 `emit()`，
  而 `emit()` 要拿归档锁；在持锁时发日志会与写入路径构成 ABBA 死锁，连带卡住机器人。
- 自身状态事件只走 stderr，不回写文件（否则写失败会递归）；换片或写入失败后
  必须能重试打开并逐条累计缺口，不能静默停摆。部分写入、短写或 flush 失败后，
  不再向可能损坏的分片追加；保留原分片，后续事件使用新分片，恢复记录也遵守相同规则。

只接收已清洗事件：WARNING 及以上，加上 `ARCHIVE_INFO_EVENTS` 里那张 INFO 白名单。
归档门槛独立于控制台级别，未启用时 `/archivez` 返回 404。`iter_entries` /
`verify_segments` 是只读工具，遇到损坏末行跳过但不修复原文件。配置见 §1 的
`logging.archive`。

## 3. `redact.py`

入口：[Redactor 与 SecretRegistry](../../src/raricy_bot/redact.py)。登记密码、模型/MCP
Key、会话 Cookie；用户名不是密钥。

`SecretRegistry` 是进程级登记中心：日志层 Redactor 与出站 Redactor 都订阅它，**一次
登记两条通路同时生效**，后订阅者补上此前登记的凭据，旧值在轮换后仍保留（迟到返回的
旧请求还带着轮换前的密钥）。装配方通过 `logging_setup.secret_registry()` 取得它；
`register_secret()` 保留为兼容入口。测试用独立实例，不依赖进程级状态。

记忆命中密钥时整条拒绝，不保存替换后的版本。

## 4. `text_utils.py`

入口：[文本与命令判定](../../src/raricy_bot/text_utils.py)。精确 @ 匹配区分大小写且检查用户名边界；
命令前缀必须完整，不能把前缀相似词当命令。token 为本地估算值；文本截断按 Unicode 字符和段落，
博客发文的 UTF-16 长度规则另见 §53。`is_secret_probe` 只实现既定的本地拒绝。

## 5. `texts.py`

入口：[固定文案与帮助函数](../../src/raricy_bot/texts.py)、[提示词规范](SYSTEM_PROMPTS.md)。
系统附加说明必须静态，不插入用户名、资料或配置正文（system 里唯一的动态片段是时间片段，
见 §54）。帮助文案由配置事实与用户门禁决定，不得写死可配置的博客/文章长度。
修改固定提示词须同步规范正文和对应测试。

本模块还持有 system 末段时间片段的固定文案：前缀 `CURRENT_TIME_LABEL` 与星期名 `WEEKDAYS`
（下标对齐 `datetime.weekday()`，不得重排）；渲染与追加函数见 §54。

## 6. `site/models.py`

入口：[聊天 DTO 与 SSE 解析](../../src/raricy_bot/site/models.py)。
容忍缺失和未知字段，非法必要标识使对象不可用；删除、图片/博客缺失用明确状态表达。
`ChatMessage.id` 与 SSE `event_id` 是不同序列，不可互换。

## 7. `site/client.py`

入口：[SiteClient](../../src/raricy_bot/site/client.py)；上游
[聊天契约](../materials/chat-bot.md)、[评论契约](../materials/comment-bot.md) 优先。

- 信封成功判据为 `code == 200`；发消息的 `message` 可为消息对象。spider 裸 JSON 读口单独解析。
- 不设置 `Origin` / `Referer`，登录并保存的 Cookie 不得出现在日志或错误正文。
- `open_stream` 独立设置 300 秒读超时，不继承普通请求的 20 秒默认值。
- 博客、评论等原匿名读口现须带会话 Cookie（D-104）；辅助读取的 401 不触发额外重登录。
- 图片取回须同源、禁止跨源重定向带出 Cookie；流式字节上限、格式白名单见 §20。
- 上游补充接口仅限已有记录：内容引用 D-50、发文 D-106。新增端点先记录依据和边界。

## 8. `site/sse.py`

入口：[SSEReceiver](../../src/raricy_bot/site/sse.py)。
单连接，空行分帧、多行 data 拼接；只有带 ID 的 message 帧推进传输游标。
成功解析帧重置退避；延迟为封顶后的指数退避乘 `1 + random()*0.2`，尊重服务端 retry。
handler 普通异常不拆流，取消必须传播。传输游标与 Store 安全水位分开（D-16）。

## 9. `store.py`

入口：[Store、表结构与事务](../../src/raricy_bot/store.py)。

- 单条 SQLite 连接；工作线程持 `threading.Lock` 串行使用，生命周期另用异步锁（D-90）。
- 事件表以 message ID 去重，只记录候选消息。NULL event ID 不参与安全水位。
- 安全水位检查点单调；清理保留回滚锚点，不删非终态事件，不设数据库硬容量上限。
- 启动将旧非终态标为 `recover`，重放时原子认领，再查已发记录，避免重复回复（D-17）。
- 回复链映射、配额与幂等状态可持久化；消息正文不可。过期映射清理须同步作废内存会话。
- 发文另有独立状态表和事务占额，允许保存脱敏标题（§53 / D-107）。

## 10. `quota.py`

入口：[QuotaGuard](../../src/raricy_bot/quota.py)。
`reserve` 在锁内合并落库计数与在途预留，按站点退避、日总量、reply 阈值、
notice 冷却、分钟滑动窗口判定。**reply 阈值比较的是全部发送计数，不是只数 reply。**

`reply` 为模型回复，`notice` 为 busy/failure/quota 主动提示，`notice_local` 为本地应答。
只有 `notice` 按 `notice:{channel_id}:{actor_id or '-'}` 冷却，默认 300 秒，
没有每频道每天一次的名额。每笔成功预留恰好 `note_sent` 或 `release` 一次；冷却仅在发出后登记。

`note_sent` 用 `settled` 标志保证预留恰好消费一次，并在 `except asyncio.CancelledError` 里
先结清预留再原样传播（取消不是 `Exception` 子类）：等待配额锁或写库时被打断也不能让预留永久
占额（D-117）。

## 11. `core/context.py`

入口：[ContextManager](../../src/raricy_bot/core/context.py)。
历史只存内存，同一 session 由调用者串行。仅在送达且 generation 有效后用
`append_exchange` 提交完整 user/assistant 对；失败轮次不留孤立 user。
DM 按频道，公开链用 `lobby-thread:<root_id>`，重启保留归属但不保留正文。

`pending_user` 只在当前请求出现。历史从最旧整对裁剪；普通路径至少保留最后一对，
`feature_context=True` 允许清空历史，但不会截断 system 或当前输入，调用者仍须预算预检。
记忆、近期消息和公开 subject 分别见 §33、§38、§45。

`ContextManager.__init__` 另有 keyword-only 的 `now: Callable[[], float] = time.time`（时钟注入点）。
`build_messages` 在 system 全部组装完成后把当前时间片段追加为 system 的**最后一段**；
`select_recent_suffix` 必须用同一个私有 `_time_fragment` 计入同一口径 —— 两处一旦分叉，
§45 的 S1 就不再是 `build_messages` 选中项的上界。片段本身见 §54。

## 12. `core/router.py`

入口：[Request、RouteResult、MessageRouter](../../src/raricy_bot/core/router.py)。
只判定和入队，不调用模型、不发送回复。处理次序：

1. 观察大区近期消息、登记 DM；过滤自身消息、删除消息和拍一拍。
2. 大区必须精确 @；通过候选过滤后才记录事件、去重/认领恢复。
3. 构造直接引用、解析公开回复链；链归属依据消息 ID，不依据用户名或模型判断。
4. 已装配时识别记忆命令；再解析最多一个能力前缀，冲突本地拒绝。
5. 空正文先判博客、再判图片；随后处理 help/reset、超长与密钥探测。
6. 入队成功才消费近期批次。queued 由 worker 终结，reply_now/busy 由 App 终结；
   memory_queued 交记忆 worker，不当作普通聊天任务。

大区 reset 以命令 ID 建新链，不清旧链；DM reset 清历史并递增 generation（D-21）。

`queue` 与 `memory_queue` 的参数类型是 `EnqueueQueue`（`core/scheduler.py`）：只要求
`put_nowait`，容量满时抛 `asyncio.QueueFull`。生产注入 `SessionScheduler`，测试可直接注入
`asyncio.Queue`，两者都结构性地满足它；入队与 busy 判定口径不变（§14）。

## 13. `core/sender.py`

入口：[MessageSender](../../src/raricy_bot/core/sender.py)。
**出站准备已改为统一走 [`prepare_outbound`](../../src/raricy_bot/outbound.py)（§55）**：
原来的「脱敏 + 段落截断」两点，现为「脱敏 → 表情归一 → 逐 token 敏感串复检 → 截断 →
区间回退」五步管线，顺序与预算口径只由该处决定；其后才是去重与配额预留。回复引用触发消息，
返回实际发送正文供历史提交。
只有网络层 `SiteError.status == 0` 走聊天不确定对账：`after=reply_to, limit=100`，
未找到时最多重发一次，不能保证严格 exactly-once。**该策略不适用于博客发文**（D-109）。
所有取消和异常出口都须结清预留。

`_deliver` / `_on_error` / `_reconcile` 只做 POST 与对账、**不结算**，用
`_Delivery(result, record, charge)` 回报是否已确认送达；`send` 是唯一结算点，确认送达后由
`_commit_delivery` 把 `store.record_sent` 与 `quota.note_sent` 收敛成一次受取消保护的终结操作
（`_run_settlement`：`ensure_future` + `shield` + 强引用集合 `_settle_tasks`）。**新增公开方法
`MessageSender.wait_settled()`**：等待所有在途终结任务结束；等待是**有界**的
（`_SETTLE_WAIT_TIMEOUT_SECONDS`，3 秒），超时记一条 `sender.settle_timeout` 便返回，**不取消**
在途终结任务（强引用仍在、仍会跑完），供关闭路径在 `Store` 关闭前调用（§16）。取消只延迟传播，
不把已确认送达改判成 release（D-117）。

强制杀进程或断电时，不承诺远端发送与本地记账跨系统原子一致；即便在正常关闭路径，有界等待超时
（在途终结因存储阻塞等病态原因未结）也会放弃等待、让关闭继续，代价是丢掉那一笔本地记账
（预留的结清仍由 `quota.note_sent` 的取消保护兜住）。

## 14. `core/worker.py`

入口：[模型客户端与 WorkerPool](../../src/raricy_bot/core/worker.py)。
SDK `max_retries=0`，重试只由本层控制：网络错误、429、5xx 最多重试一次；
超时及 HTTP 408 不重试（D-19）。固定 worker 数、同 session 串行，handler 普通异常不杀 worker。
严格完成检查只由发文显式启用（§53）；不能把被截断或非法完成当作可发布稿件。

**工具能力没有永久负缓存**（D-116）：客户端不持有「不支持 tools」的共享可变标记；普通
400/404 只终结当前请求并归类 `bad_request`；`tools_unsupported` 只在提供方给出结构化错误时
产生（`_is_tools_unsupported`：`error.param` 精确为 `tools` / `tool_choice` /
`parallel_tool_calls`，且 `code` 命中明确的不支持错误码白名单，即 `_TOOLS_UNSUPPORTED_CODES`；
通用 `type` 不作依据），且只属**本次调用**。App 与
`blog/writer.py` 只依据 `ModelError.kind`，不再读模型客户端上的共享属性。

`WorkerPool` 消费的是 `core/scheduler.py::SessionScheduler`（`RunnableQueue` 协议），不再从
裸 `asyncio.Queue` 取请求：工作器只领取**可运行会话**的一条请求（`get_runnable`），本条结束
经 `task_done` 清 active、有积压则把会话放回可运行队列队尾。同会话严格串行且不占执行容量；
容量口径与关闭语义见 D-118。

## 15. `ops.py`

入口：[OpsServer](../../src/raricy_bot/ops.py) 与 [App 健康判定](../../src/raricy_bot/app.py)。
`/livez`、`/readyz` 仅返回固定短文本与 200/503，不暴露正文或内部状态。
评论关键后台任务意外退出影响 livez；临时评论失败不改变聊天 readyz。
MCP、KB、记忆、发文为软故障扩展，不纳入健康就绪条件。Compose 默认不发布探针端口到宿主。

## 16. `app.py`

入口：[BotApp](../../src/raricy_bot/app.py)。
装配生命周期与各模型路径；启动恢复孤儿、从安全水位播种 SSE，运行期不逐帧回拨游标。
resync 拉取按 message ID 去重，空 event ID 不抬水位。
403 中 CSRF 为客户端错误，其余权限/禁言进入不可用并定时探测。
仅在回复成功后提交历史；退出总预算 10 秒，先停止发文/记忆等消费者，再关闭共享模型与 MCP。

两条队列都是 `SessionScheduler`（§14），容量取 `behavior.queue_size` / `memory.queue_size`；
记忆 worker 并发固定为 1。关闭顺序里，`_shutdown` 在 `Store` 关闭**之前**调用
`MessageSender.wait_settled()`，等在途的受取消保护终结任务（`record_sent` + `note_sent`）
结束，再关 `Store`，避免它们撞上已关闭的连接。`wait_settled()` 有界（超时放弃等待、让关闭
继续，见 §13 / D-117）。

装配接缝（`app.py` 不 import `mcp/` 与 `blog/`，见 §56）：MCP Manager 与发文子域都经
`BotApp(..., mcp_manager_factory=, blog_service_factory=)` 注入；没有 MCP 工厂且
`mcp.enabled=false` 时使用无工具默认实现（生命周期空操作、任何 feature 不可用），
没有工厂而配置启用了对应子域则在**构造期**抛 `AssemblyError`（消息是稳定类别码），
不进 `start()` 的软故障兜底。显式传入的 `mcp_manager` 实例优先于工厂。

可选身份校验（设计 §4.2、§10.4）：`BotApp(..., expect_site_user_id=)` 默认 `None` 即
**不校验**（完整版 CLI 行为不变）。非 `None` 时，校验发生在登录成功**之后**、账号锁与
消费者装配（`acquire_account_lock`、`_start_after_login`）**之前**：真实 `user.id` 与
期望值不符就关闭 `SiteClient` 与 `Store`、抛 `SiteIdentityMismatch("account_identity_mismatch")`
（异常类型在 `assembly.py`，消息是稳定类别码）。此时 SSE、评论与记忆一条都还没起。

## 16.1 评论子系统

入口：[发现器](../../src/raricy_bot/comments/discovery.py)、[路由](../../src/raricy_bot/comments/router.py)、
[服务](../../src/raricy_bot/comments/service.py)、[发送器](../../src/raricy_bot/comments/sender.py)、
[配额](../../src/raricy_bot/comments/quota.py)、[DTO](../../src/raricy_bot/site/comment_models.py)。

最近评论发现首次精确 @；通知发现直接回复。两路通过 Store 原子 claim 去重；
独立队列、会话、配额和恢复，共享模型并发闸门。完整树受字节数和节点数上限约束，不能用截断树匹配。
通知标记已读失败仅重试标记，不重复应答；冷启动尾页未读尽时不能宣称基线完成（D-26/D-27）。
发送仅 reply/notice_local；忙碌、模型失败、额度耗尽静默。每条成功评论仍会真实通知被回复者。

## 17. `__main__.py`

入口：[启动与退出码](../../src/raricy_bot/__main__.py)。
解析配置路径、校验配置、初始化脱敏日志、安装未捕获异常兜底、按需打开永久归档，
再运行 App。完整版在此注入 `ToolCallingModelClient`、`build_mcp_manager` 与
`build_blog_service`（§56）；Light 走 launcher 入口，不经过本模块。
退出码：配置错误 2（含装配接缝错位的 `AssemblyError`，只在 stderr 打一行类别码），
归档已启用却打不开 3，**数据目录被占用或不可用 4**（公共数据档案锁，D-125），运行期致命错误 1，
其余 0；任何一条错误路径都不回显凭据。锁在打开归档与 Store **之前**取得，第二个写者不碰任何数据。
停止须等待资源关闭，不能留下 pending task 或未关闭客户端；归档在 `finally` 里同步并关闭。

「已启用但打不开」是致命的：继续跑只会让所有人以为永久记录正在工作。边界要说清楚 ——
**配置解析成功、归档初始化完成之前**的启动错误只能安全写 stderr，仍依赖宿主保存；
OS/OOM/断电等进程外故障同样依赖宿主监控。不能宣称所有启动失败都已入应用归档。

另有两个只读子命令，不启动机器人、不连站点：

```bash
python -m raricy_bot archive verify --directory /app/logs/errors
python -m raricy_bot archive read --directory /app/logs/errors --level WARNING --since 2026-09-21T00:00:00
```

`verify` 有损坏分片时退出码 1，便于备份脚本直接据它告警。

## 18. 测试约定

入口：`tests/test_<模块>.py`，统一 `python -m pytest tests -q`。
禁止真实站点、模型或 MCP 连接；使用 MockTransport、fake、临时目录和注入时钟。
warning 按错误处理。测试目录当前被 Git 忽略，不能假设新克隆已含测试。

## 19. 全局红线

1. 不增加未经依据核对的站点 API，不修改上游原始材料。
2. 动态用户/引用/记忆数据只进 user 消息；MCP 结果进 tool 消息，不进 system。
3. 密钥、正文、模型请求/响应不落日志或 SQLite；授权记忆 Markdown 与 D-107 的发文标题例外须明确区分。
   永久归档同样受这条约束：它只收 `logging_setup` 已清洗的事件，不解析也不复制
   原始日志，更不因为"已经过脱敏"就接收任意正文（D-111、D-112）。
4. 模型不能决定工具权限、记忆作用域、owner、文件路径或写入权限。
5. 取消不得吞成普通成功；持久状态、额度、发送和历史提交须保持各自的顺序与幂等边界。
6. 归档分片只增不删：不设 `retention_days` / `max_files`，不用会按 `backupCount`
   淘汰旧文件的轮转策略；损坏的分片保留原样，读工具跳过而不修复。
7. `site` 与 `model` 的 `base_url` 对非回环地址**无条件**要求 https，且不得含
   userinfo、查询串或片段。回环地址的 http 也需要显式打开 `allow_plain_http`
   （默认关闭）；该开关**只**对回环地址有意义，公网明文没有任何配置能放行。

## 20. `core/vision.py`

入口：[ImageLoader](../../src/raricy_bot/core/vision.py)。
同源取图、流式限制字节，按字节识别 PNG/JPEG/GIF/WEBP，不转发 SVG；
编码 data URL 后仅供当前轮，历史只留图片标记。
有正文时失败降级纯文本；纯图失败本地 notice_local，不调模型。
评论按附件→评论引用→文章引用共享图片尝试名额，失败也扣名额（D-53）；
聊天引用缩略图按站点 URL 原样读取（D-54）。

## 21. 聊天区 MCP 工具合同

入口：[能力表](../../src/raricy_bot/capabilities.py)、[共享类型](../../src/raricy_bot/mcp/contracts.py)、
[Registry](../../src/raricy_bot/mcp/registry.py)、[适配工厂](../../src/raricy_bot/mcp/adapters.py)、
[生命周期](../../src/raricy_bot/mcp/runtime.py)。

显式命令只授权当前轮，普通聊天和评论不调用工具。发现不等于允许调用，宿主再次校验白名单、
参数和预算；模型不能传入额外抓取或扩大返回量的参数。结果为不可信 tool 数据。
适配器按 `(feature_name, model_tool_name)` 查找，无跨 feature 回退；每个 feature 共享一个 limiter，
每个 binding 独立适配器。stdio 与远程 SSE 配置互斥，SSE 需 HTTPS、环境 Bearer 与独立读超时。
成功送达后才提交压缩摘要，原始工具消息不入历史；失败不影响普通聊天。

诊断走**结构化字段**，不走原始正文（D-111）：阶段（`connect` / `discover` / `call` /
`close`）、耗时、异常类型、JSON-RPC 数字错误码与子进程退出码；stdio 的 stderr 只按固定
规则提取失败类别与经校验的模块名，原文一概不留。阶段停滞由 `McpManager` 的巡检任务
报警一次（`mcp.phase_stalled`），阶段结束时若曾停滞则记 `mcp.phase_finished`。停滞
**只报警、不取消**：可能有副作用的调用为了日志被取消或重放会制造重复副作用。

## 22. Exa 授权密钥池（`mcp/pool.py`）

入口：[ExaPooledProvider](../../src/raricy_bot/mcp/pool.py)，配置/运维见
[EXA_POOL_AND_KB.md](../usage/EXA_POOL_AND_KB.md)。
多个已授权 Key 对外仍为一个 Provider；每逻辑调用每可用槽位至多尝试一次，成功立即结束。
状态只存内存；未知上游错误不猜测额度、不轮换。故障转移不增加模型调用预算，
但可能产生多个上游请求和费用（D-36–D-46）。

## 23. Markdown 知识库（`kb/`）

入口：[加载](../../src/raricy_bot/kb/loader.py)、[索引](../../src/raricy_bot/kb/index.py)、
[服务](../../src/raricy_bot/kb/service.py)、[模型](../../src/raricy_bot/kb/models.py)。
本地只读能力，不走 MCP；默认 DM + 稳定用户 ID allowlist。
一级目录为分类，根文件归 `_root`；扫描受文件/字节/分块上限约束，完整构建后原子替换索引。
命中块只入当前 user 消息，不留原文历史，不把检索片段或文件路径写日志；失败使用旧快照或降级。

## 24. `core/blog.py`（引用博客）

入口：[BlogLoader](../../src/raricy_bot/core/blog.py)。
聊天引用博客读取标题与可用正文；`behavior.quoted_blog_max_chars` 与评论文章上限独立。
超长正文不提供，失败与删除有各自标记；正文/图片仅属当前轮，历史留博客状态标记。
带资料块时允许清空历史，仍须先做当前输入的预算预检。读取带 Cookie（D-104）。

## 25. `core/content_refs.py`（内容引用）

入口：[ContentRefResolver](../../src/raricy_bot/core/content_refs.py)。
`[@id]` 按 8/9/10 位解析剪贴板/投票/图床图；聊天、引用博客、评论共用解析器。
先算预算再请求，装不下既不请求也不替换。每次解析去重，缓存不跨轮；
请求数与图片数有独立上限，视觉关闭不取图片字节。展开只属当前轮，历史保留用户原始引用文本。
不递归展开、不从任意正文 URL 抓取；实际展开文本的身份扫描入口见 §49。

## 26. `config.py`（长期记忆配置）

入口：[MemoryConfig 与校验](../../src/raricy_bot/config.py)。
默认关闭、allowlist；管理员名单在 allowlist 模式下须为接入名单子集。
部署级 `auto_capture_available` 与每用户开关分开；启用自动提取时加载期检查披露空间。
条目、文件、候选、幂等记录、写作与上下文预算分别限额。

## 27. `memory/models.py`（记忆数据模型）

入口：[类型、状态与 user_storage_key](../../src/raricy_bot/memory/models.py)。
作用域为 all_user/lobby/user；稳定用户 ID 派生存储键，不能用 username 作私有路径。
状态名和值一起构成合同，现含 `STATUS_PUBLIC_CONFLICT`；不能沿用“只能有十个状态”的旧限制。
通用 `SupplementalItem` 定义在 core/context，memory 可依赖 core，反向依赖禁止。

## 28. `memory/access.py`（Beta 接入策略）

入口：[MemoryAccessPolicy](../../src/raricy_bot/memory/access.py)。
allowlist 是全部记忆能力总闸；all 模式开放共同读取，但私有读写和命令仍要求非空稳定 ID 与 DM。
管理权还须 admin_user_list，不使用站点 DTO 的 is_admin。关闭时所有门禁为假。

## 29. `memory/codec.py`（Markdown 编解码）

入口：[解析、渲染与 CodecError](../../src/raricy_bot/memory/codec.py)。
严格结构与容量校验，正文使用连续引用行，防止伪造标题/条目。
解析错误使用稳定 CodecError；写入前拒绝不可 UTF-8 编码的正文。
Markdown 保存条目和 operations；不把业务幂等结果另存 SQLite。

## 30. `memory/service.py`（存储服务）

入口：[MemoryService](../../src/raricy_bot/memory/service.py)，记忆的唯一写入口。
单进程 mutation lock；同目录临时文件、flush/fsync、摘要比对、os.replace 后一次发布新快照。
外部编辑冲突不得静默覆盖，坏文件保留最后有效快照；错误映射稳定状态。

| 请求场景 | 可读取的生效内容 |
|---|---|
| DM | all_user；仅所属用户且 private_enabled 时读取 user |
| lobby | all_user + lobby；公开投影另走 §42 |
| comment | all_user；公开投影另走 §42 |

共同候选不进普通请求；管理 mutation 在 I/O 和幂等查询前复查 admin。
`private_path` 是私有路径只读入口；`private_settings_cached` 无 I/O。
`find_operation` 有 user_id 时依次查私有→公开→共同，无 user_id 只查共同；
clear 保留幂等元数据。私有读取停用不等于删除。

**自动提取的提交授权**（D-115）：`begin_auto_capture` 在写锁内一并取得授权、条目快照与
该用户提取代次，返回不透明 `AutoCaptureToken`（`memory/models.py`）；模型调用在锁外。
`commit_auto_capture` 在写锁内复核 token 的 epoch / `_enabled` / `document.auto_capture` /
generation，失效返回稳定 `noop`（不写、不产生披露）；并把令牌传给 applier
（`_apply_guarded_proposal` 的 `capture_token`），在**实际写入的基线**上复查授权 ——
`_write_document` 接手外部版本后会用该基线重放 applier，入口的 TTL 缓存快照看不见它，因此外部把
`auto_capture` 改成 `False`、或直接删掉私有文件时，迟到的提交同样落稳定 `noop`。失效入口是
`set_auto_capture(False)`、`set_private_enabled(False)`、`clear_private`、`delete_private`；
失效处理先于应用，因此值未变的 no-op 路径同样生效。代次与纪元都是进程内字段，不落盘。

## 31. `memory/writer.py`（AI 撰写器）

入口：[MemoryWriter](../../src/raricy_bot/memory/writer.py)。
无 scope/owner/path 参数。模型 JSON 恰含 action/target_id/key/content/confidence；
未知字段整份拒绝，add 的 target_id 为 null，update 必须属于本轮 existing，不允许 delete。
非 noop 的规范化正文须非空且可 UTF-8 编码，有效性检查先于低置信度降级。

总输入预算包含 system、骨架、来源与已有条目；来源不截断，条目装不下整条跳过。
先等共享 model_gate 再开始 timeout，取消原样传播；其它模型/解析失败为稳定 invalid_proposal。

## 32. `memory/commands.py` 与 `memory/controller.py`

入口：[命令解析](../../src/raricy_bot/memory/commands.py)、[MemoryController](../../src/raricy_bot/memory/controller.py)。
执行顺序：权限→查 operations 幂等→读取快照→AI→校验→原子写→固定回执；
重放返回首次结果，不重复调用 AI，也不改成 duplicate。
共同候选由管理员提出、审批；批准的是已展示版本，不再调模型，目标变动则 conflict。
私有开关、手动保存与自动提取分开；reset 不动长期记忆。公开命令和两阶段删除见 §52。
自动提取的授权规则不由控制器自建：控制器从 `begin_auto_capture` 取令牌，模型调用后经
`commit_auto_capture` 提交，失效判定全部落在服务层的写锁内（D-115）。

## 33. `core/context.py`（记忆预算）

Service 只筛作用域和排序；[ContextManager](../../src/raricy_bot/core/context.py) 负责整轮及分组预算。
条目为不可拆单位，装不下跳过；共同组共享上限，私有和公开个人组各自限额。
正文只进当前 user，至少选入一条记忆才追加静态说明并计入 token；无记忆时保持原行为。

## 34. `core/router.py` 与 `app.py`（记忆集成）

入口：[Router](../../src/raricy_bot/core/router.py)、[App](../../src/raricy_bot/app.py)。
Router 只授权、构造请求、入记忆队列；只有 worker 成功启动才注入队列，避免事件永久 pending。
记忆 worker finally 终结事件，不阻塞 SSE。关闭功能不读写目录、不启任务。
自动提取仅在满足 DM/用户/部署开关后，于回答生成后、发送前执行；成功写入后拼披露并预留截断空间。
披露装不下的已知例外必须保留 warning（D-77），不能宣称任何情况下都能告知。
关闭或清空（`/memory off`、`/memory auto off`、`/memory clear`，以及 `/memory forget`）成功后，
**此前已开始的提取不再写入**：提交走 `commit_auto_capture` 的写锁内复核，失效即 no-op（D-115）。

## 35. 评论记忆集成

入口：[CommentRouter](../../src/raricy_bot/comments/router.py)、[CommentService](../../src/raricy_bot/comments/service.py)。
Router 在有 author.id 时计算 memory_allowed；下游传递已做出的决定。
`_CommentMemoryAccess` 是共同记忆决策载体，不能换回用 None 复问 allowlist 的真实策略（D-76）。
评论不读 lobby 或私有文件；公开资料单独走 §48。

## 36. `texts.py`（帮助与记忆文案）

入口：[help_text、comment_help_text 与记忆回执](../../src/raricy_bot/texts.py)。
配置上限必须注入；不恢复已删除的 HELP_TEXT 常量（D-94）。
开启读取/自动记忆及隐式开启的保存回执说明保存、外送、范围、查看删除方式，不另存“已披露”标记。
help 只读缓存；快照未暖时可保守显示未开启（D-79）。

## 37. 记忆日志与安全

入口：[LOG_FIELDS](../../src/raricy_bot/logging_setup.py)、[MemoryService](../../src/raricy_bot/memory/service.py)。
只记白名单内的状态、revision、数量和条目 ID；不记正文、来源、命令参数、路径或用户身份。
正文仅能进入授权 Markdown、所属场景的 user 消息和明确展示回执。
可选记忆失败不得影响聊天/评论及健康端点。

## 38. `core/lobby_context.py`（大区近期消息）

入口：[LobbyRecentContextBuffer](../../src/raricy_bot/core/lobby_context.py)。
纯同步内存缓冲，最多 50 条，每条正文前 500 字；只留文本和有界博客标题，
忽略拍一拍/已删除消息，不下载图片、不展开引用、不复制 reply.content。
Router 在过滤前观察；peek→入队→discard 是无 await 的连续片段，成功入队才消费。
后续失败不回滚批次。外送取预算内的最新连续后缀，不跳过长消息去挑更旧短消息。
不落库、不入历史、不记正文日志；重启即失（D-95）。

## 39. `config.py`（公开个人记忆配置）

仍使用 [MemoryConfig](../../src/raricy_bot/config.py) 和既有总开关/门禁，没有第二套用户授权。
独立限制每 owner 的公开条目数、每轮 subjects 数与公开组 token，不扩大其它记忆预算。

## 40. `memory/models.py`（公开个人记忆模型）

入口：[公开条目、文档与 subject](../../src/raricy_bot/memory/models.py)。
公开个人记忆是独立投影，不新增 MemoryScope，不并入 all_user。
`STATUS_PUBLIC_CONFLICT` 区分公开授权冲突与文件冲突；来源 ID/版本与发布时间不可混用。

## 41. `memory/codec.py`（公开 Markdown）

复用 [codec](../../src/raricy_bot/memory/codec.py) 的严格边界。
front matter 校验键集合，不限制人工调整键序；渲染器固定键序。
正文用引用行，时间为 UTC RFC 3339，source 字段原样复制来源。
owner_username 非法为 malformed；空合法公开文档保留 operations。

## 42. `memory/service.py`（公开投影）

入口：[公开读写方法与索引](../../src/raricy_bot/memory/service.py)。
公开请求只读 public/，不打开 users/；public_context_for 仅接受 lobby/comment。
发布为用户主动确认的快照：重复且内容一致 noop，不一致 conflict，不静默覆盖。
公开状态不可确认时保守拒绝写入；已公开来源不能被 AI 更新或同 key add 替换（public_conflict）。

撤回只改公开文件；删除私有来源前必须先撤回，失败停止。空投影保留幂等记录。
索引只从合法公开文档生成，文件数硬上限 4096；索引访问器同步无 I/O，异常仅省略该份资料。

## 43. `memory/subjects.py`（公开 subject 解析）

入口：[PublicMemoryInputs 与 resolver](../../src/raricy_bot/memory/subjects.py)。
候选只来自本地公开 username 索引。按当前作者、精确 @、普通精确用户名、直接引用、
会话参与者、实际提供的博客/文章、实际展开的公开剪贴板、大区近期块依次选择并按 owner 去重。
不扫描模型回答、MCP/搜索结果或 KB；无模糊匹配，不因文本触发写入。

文本命中须站点精确校验唯一同名用户，且派生 owner key 匹配；超时、429、改名、
歧义或异常均不用。已知稳定 owner 的来源无需重复网络查询。
正/负缓存 600/60 秒、最多 512 项、本地 15 次/分钟；缓存不含正文。DM 直接返回空。

## 44. `site/models.py` / `site/client.py`（用户查询）

入口：[ChatUserSummary](../../src/raricy_bot/site/models.py)、[search_chat_users](../../src/raricy_bot/site/client.py)。
使用既有聊天用户搜索，limit=30、offset=0；解析异常返回空，不能拿模糊结果确认身份。
username、用户 ID 和查询正文不进日志，resolver 只依赖最小查询 Protocol。

## 45. `core/context.py`（subject 与公开预算）

入口：[ConversationSubject、recent_subjects、select_recent_suffix](../../src/raricy_bot/core/context.py)。
subject 只挂历史 user 轮作元数据，按最近出现和 key 去重，随历史淘汰/reset 消失，不渲染成正文。
先选近期候选后缀 S1 再解析公开身份；S1 未预留记忆空间，极限预算下最终未外送的几条
仍可能贡献 subject，这是已记录的保守取舍（D-103）。
公开组优先级晚于既有记忆组；选中后同时计入通用与公开专属静态说明的 token。

## 46. `core/lobby_context.py`（subject 通道）

入口：[LobbyRecentMessage](../../src/raricy_bot/core/lobby_context.py)。
subject 由 Router 计算后传入；缓冲器不 import memory，不做哈希和身份查询。
peek/discard 保持同一批次的正文与元数据，不另建长期参与者缓存。

## 47. `core/router.py` 与聊天装配（公开记忆）

`MemoryCommandRequest.username` 用于显式发布；`Request.public_memory_subject` 用于上下文，
不能混用。入口：[Router](../../src/raricy_bot/core/router.py)、[App](../../src/raricy_bot/app.py)。
未装配记忆不计算 subject；DM 不走公开 resolver。
图片、博客、内容引用、KB 和输入预算已确定后才组装公开输入，只扫描本轮允许的来源。
公开读取失败只少一份资料，不能让聊天失败。

## 48. 评论公开记忆路径

入口：[CommentMemoryInputs](../../src/raricy_bot/comments/service.py)、
[请求感知 provider](../../src/raricy_bot/comments/service.py)。
输入包括 memory_allowed、当前/会话 subjects 和允许扫描的文本；不扩成原始作者 ID 的旁路。
共同记忆与公开个人记忆分别获取；文章超限未提供时不扫描其省略正文，不额外抓取身份材料。

## 49. `core/content_refs.py`（实际展开文本）

[ResolvedRefs.expanded_texts](../../src/raricy_bot/core/content_refs.py) 是实际展开成功、允许参与
公开身份扫描的文本入口。聊天与评论复用它，不二次解析；未请求、失败、未提供的内容不参与。
该元数据与展开正文一样仅属当前轮。

## 50. `texts.py`（公开个人记忆文案）

入口：[公开回执与静态说明](../../src/raricy_bot/texts.py)、[提示词规范](SYSTEM_PROMPTS.md)。
区分发布成功、撤回、非法 username、public_conflict；help 说明公开范围、匹配来源及撤回方式。
明确 memory off 不撤回、reset 不删除；只有本轮选中公开条目才加专属说明，不插值用户内容。

## 51. `logging_setup.py`（公开个人记忆日志）

复用 [日志白名单](../../src/raricy_bot/logging_setup.py)，仅记录稳定原因和计数。
不记录 username、owner key、扫描文本、命中条目正文或文件路径；身份校验失败不能把上游响应倾倒到日志。

## 52. `memory/commands.py` 与 `memory/controller.py`（公开命令）

入口：[解析器](../../src/raricy_bot/memory/commands.py)、[Controller](../../src/raricy_bot/memory/controller.py)。
public/unpublic/list public 只在 DM，条目 ID 必须匹配 UM 前缀；发布/撤回不调用 AI。
forget/clear 先以 `cmd:<message_id>:unpublish` 撤回，再以 `cmd:<message_id>` 删除私有；
撤回失败不执行删除，重放从幂等结果继续。
memory off 只关私有读取与自动提取，公开副本不变；自动更新遇 public_conflict 静默跳过，
显式 remember 返回专用说明。

## 53. 定时发文（`blog/`）

已实现、默认关闭，**真实站点验收尚未完成**；启用、人工核实和验收见
[USAGE.md §2.4](../usage/USAGE.md)。设计与实施计划已归档，见 [归档索引](../ARCHIVE.md)。
使用普通用户 `POST /api/blogs`，属于显式例外（D-106），不使用编辑文章接口。

### 53.1 基础类型

[blog_records.py](../../src/raricy_bot/blog_records.py)（包外的中立模块，见 §56）定义 Draft、
PreparedDraft、BlogScope、运行状态与投递状态，两套状态不可混用。hash_version=1 与 SQLite
CHECK 一起维护，变更须处理旧行。

### 53.2 配置

[BlogConfig / BlogTaskConfig](../../src/raricy_bot/config.py)：日上限默认 2、可配 1–5；
任务 must/maybe，UTC+8 的 HH:MM 调度；prompt/drafts_dir 恰选一个，目录相对配置文件解析。
停机点不补发，不支持多实例共用账号。

### 53.3 能力

[capabilities.py](../../src/raricy_bot/capabilities.py) 的 blog_write 无用户命令。
工具预算从该 feature 读取；result_count 固定 1，适配器与聊天按双键隔离（D-110）。

### 53.4 持久状态与预算

[Store 发文事务](../../src/raricy_bot/store.py) 原子领取执行、预留额度并落 inflight 后才准 POST。
恢复将 inflight→unconfirmed、queued/running→interrupted，不重新生成。
不确定行持续占额；published 按 UTC+8 的预留日至本地确认日闭区间计费（D-108）。

### 53.5 站点发布与搜索

[site/blog_models.py](../../src/raricy_bot/site/blog_models.py)、[SiteClient](../../src/raricy_bot/site/client.py)。
只有合法业务信封可判明确拒绝；HTTP 状态本身不能证明未发布。
成功需合法 blog_id。标题搜索有界且可能隐藏栏目，空结果不能证明未发布。

### 53.6 解析与预校验

[blog/codec.py](../../src/raricy_bot/blog/codec.py) 统一解析稿库与模型输出的 YAML front matter + Markdown。
统一脱敏后按 UTF-16 code units 校验标题/描述/正文；不截断、不补写模型稿。
指纹使用最终出站稿，标题、请求体、指纹必须一致，Publisher 不再次改写。

### 53.7 稿库

[blog/drafts.py](../../src/raricy_bot/blog/drafts.py) 按文件名选稿，坏稿不堵队首；
已发布/待确认指纹不得重投；不复制输入文件为生成缓存。改稿产生新指纹，视作新内容。

### 53.8 调度

[blog/planner.py](../../src/raricy_bot/blog/planner.py) 为纯函数；UTC+8 日历（`utc8_day` /
`utc8_next_day` / `SCAN_WINDOW_SECONDS`）在 [blog_records.py](../../src/raricy_bot/blog_records.py)，
Store 与子域取同一份（§56）。
扫描领取与串行生成/发送分离；执行键持久化后不重掷概率，不因长生成漏领后续分钟点。

### 53.9 生成

[blog/writer.py](../../src/raricy_bot/blog/writer.py)、[严格模型调用](../../src/raricy_bot/core/worker.py)。
system 是静态写作规则加 system 末段的当前时间片段（§54）、任务提示词进 user、工具返回进 tool；一次两轮协议，至多执行一次合法工具，
第二轮 tool_choice=none。整份输入超限、截断或非法完成均失败，不发布半稿；
共享 model_gate，取消传播，不自行扩展研究循环。

### 53.10 投递与对账

[BlogPublisher](../../src/raricy_bot/blog/publisher.py)：确认会话与原账号→事务预留→一次 POST→分类终结。
不确定结果只对账，不自动重投；唯有明确 429 的稿库来源允许同指纹累计少于 3 次时重试。
对账须唯一精确标题、稳定作者 ID 与正文指纹吻合；分页/正文读取触顶则本轮不确认。
累计 12 次仍不确认就停止自动查询，保留 unconfirmed 与占额。

### 53.11 服务

[BlogService](../../src/raricy_bot/blog/service.py) 独立扫描、串行消费、只读对账三个循环；
启动恢复后先对账，随后每 5 分钟一次。预算耗尽不挡对账。
PublishOutcome.post_id 是否为 None 决定 status 是运行态还是投递态；有投递行的 run 终态为 finished。
生成失败只失败本次运行，账号变化停整个发文子域。

### 53.12 生命周期

[App](../../src/raricy_bot/app.py) 在账号、Store、模型和 MCP 就绪后装配发文，
关闭时先停发文再关共享依赖；10 秒总关闭预算内取消，不等待长生成自然结束。
关闭配置时不扫描稿库、不启后台任务；发文失败不影响聊天健康。

### 53.13 隐私

仅脱敏待发布标题、指纹与必要状态/调度元数据落 SQLite；描述、正文、提示词和模型请求/响应不落。
日志只记任务、稳定状态、标题长度和站方 ID，不记标题正文、搜索词或指纹（D-107）。

### 53.14 验证

对应 `tests/test_blog_publish_*.py`，全部使用替身。
上线验收待办留在使用手册；不能把文档归档、单元测试通过或本地实现完成称为真实站点验收通过。

## 54. 当前时间片段（`time_context.py`）

入口：[渲染与追加](../../src/raricy_bot/time_context.py)。它是送进模型的 system 里**唯一**的
动态片段：由代码从进程时钟生成，用户完全不可控，因此不违反「用户内容只进 role=user」
（D-114）。口径固定 UTC+8，到分为止，片段定长 24 字符（文案前缀与星期名的归属见 §5）。
它固定是 system 的**最后一段**，排在 `system_prompt`、所有静态附加说明与记忆说明之后；
此后任何新增的动态 system 内容都必须重走一次决策记录，不得援引本次例外。

`render_current_time(now: float) -> str` 渲染片段，`append_current_time(system: str, now: float) -> str`
把它追加到 system 末尾（system 为空时只返回片段）。依赖方向固定为 `texts ← time_context`：
只依赖标准库与 `texts`，不 import 任何业务模块，聊天、评论与 `blog/` 各调用点都能安全引用，
不引入依赖环。

## 55. 出站文本管线与表情规范化（`outbound.py` / `stickers.py`）

入口：[prepare_outbound 与 OutboundReport](../../src/raricy_bot/outbound.py)、
[StickerTable、render、StickerReport](../../src/raricy_bot/stickers.py)。**签名、字段与默认值
以源码为准**，本节只定顺序、接线与预算口径；理由与取舍见 D-119。

聊天与评论两条出站链路共用**同一个**收口：`core/sender.py::MessageSender.send`（§13）与
`comments/sender.py` 的 `_prepare_content` → `_normalize`（§16.1）都调用 `prepare_outbound`，
版本与顺序只由一处决定。查找表在装配期构造一次（§55.4），两条链路共用同一张表。

### 55.1 固定顺序（承重，不能调换）

1. **脱敏**（`redact`）—— 隐私边界，最先执行，**与表情功能开关无关**；
2. **表情归一**（`stickers.render`）—— 归一**可能让正文变长**，所以必须排在截断之前。
   `table is None`（配置 `stickers.enabled: false`）时整段跳过，正文除脱敏外一个字节不动；
3. **逐 token 敏感串复检** —— 归一的去空白容错可能把第 1 步没拦住的敏感串重新拼出来
   （如 `[@14/上 班]`），命中则**整枚丢弃**，不回填 `[redacted]`（那会留下一个非法 token）。
   对每个**归一后**的 token 独立跑一遍脱敏器，因此这一步是完备的：`render` 唯一会新建的
   文本就在 token 内部，token 之外的正文第 1 步已经处理过；
4. **截断**（`truncate_with_cut`，§4）；
5. **区间回退** —— 切点落在某个 token 或代码区区间内部时回退到该区间起点。只依据第 2 步
   产出的区间表，不按正则猜归属，因此不会误伤无斜杠的内容引用 `[@a1b2c3d4]`。

**幂等不变量（承重）**：`render` 对已规范化的正文是恒等变换。因此 `app.py` 的披露路径可以
先归一、按既有算法算披露预算，Sender 再跑一遍时不会增长，那套三段预留（披露 + 脱敏增长 +
`TRUNCATION_SUFFIX`）依然成立（D-63）。

### 55.2 `max_chars` 的语义

**`max_chars` 是「含截断提示在内」的最终上限**，返回正文长度恒不超过它：

- 未超出时不截断、不追加提示，原样返回。
- 需要截断时只传 `limit = max_chars - len(TRUNCATION_SUFFIX)`。`truncate_at_paragraph` 的既有
  契约是「`limit` **不含**提示」，最多返回 `limit + 12`；收口只能由调用方在 `prepare_outbound`
  里扣减，不能改 `truncate_at_paragraph`（那会推翻既有语义）。
- 极小预算：`max_chars <= len(TRUNCATION_SUFFIX)` 时放弃提示、直接硬切 `text[:max_chars]`；
  `max_chars < 1` 时返回空串。提示本身不允许把正文顶出上限。
- **`max_chars is None` 是「不做长度预算」模式**：跳过第 4、5 步，`truncated` 恒为 False、
  不追加提示、返回长度不设上界；脱敏、归一与逐 token 敏感串复检照常执行。

### 55.3 聊天披露路径：先归一、后披露，且**不**在此处截断

聊天发送路径（`app.py`）在 `_auto_capture_answer` **之前**做一次
`prepare_outbound(max_chars=None)`：

- 归一必须在披露**之前**：聊天短期历史提交的是模型原文，而披露绝不进历史，所以历史拿到的
  必须是「已归一、不含披露」的正文，否则会留下未修正的坏 token，下一轮模型模仿自己。
- 但**不能同时截断**：`_auto_capture_answer` 之后 Sender 还会按披露预算再截一次，两次截断
  会让正文末尾出现两份截断提示。长度预算在那里由既有的披露算术独占，本管线只贡献归一后的
  正文及其真实长度。
- **首次归一后显式判空**：`rendered.strip()` 为空则复查 generation，然后走既有的
  `_notify_failure()` 结束——不提取记忆、不发送、不提交历史。若放行，空正文会流进
  `_auto_capture_answer`，自动记忆写入成功时返回「空 body + 非空披露」，最终发出一条只含
  记忆披露的回复，并向历史提交一条空的 assistant 轮次。该判空条件与功能开关无关
  （D-119 的连带修正之二）。
- 历史提交用归一后的 `rendered`（不含披露）。评论侧历史跟随 Sender 实际发布的正文
  （`_published_text`），把归一挂进 `_prepare_content` 即自动修好（§16.1）。

### 55.4 表情提示词的来源与装配

`texts.sticker_system_addendum(*, collection, names)` 是文本构造函数（不是模块级常量），
正文口径见 [SYSTEM_PROMPTS.md §1.10](SYSTEM_PROMPTS.md)。装配期 `app.py` 用一次
`StickerTable.from_config` 同时喂给两个 Sender（`table=`）与 `CommentService`
（`sticker_addendum=`）；聊天 system 在 `system_addenda` 里追加，评论把 addendum 并入既有的
`addendum` 变量（含无 `ContextManager` 的回退分支）。它排在时间片段**之前**（D-114 第 2 条）。
它是继模块级常量与时钟片段之后的**第三类 system 来源**，D-114 的例外不得被援引到它
（SYSTEM_PROMPTS.md §1.6、D-119）。

### 55.5 日志

`sticker.render`：字段 `candidates` / `kept` / `fixed` / `dropped` / `dropped_for_secret`，
类型均为 `TOKEN`（整数，§2）。前四者互斥且完备地描述**一次 `render`**：
`candidates == kept + fixed + dropped`，只统计会被处理的候选（代码区内的候选不占名额）。
`dropped_for_secret` 属第 3 步（逐 token 敏感串复检），与前者**处于不同阶段、可以重叠**：
同一个 token 可能既被计为 `kept`，又被计为 `dropped_for_secret`，因此实际发出的 token 数是
`kept + fixed - dropped_for_secret`，不是 `candidates - dropped`。功能关闭（`table is None`）
时不记该事件；**不记正文，也不记具体名字**。

## 56. 装配接缝与 Light 闭包（`assembly.py`、`tools/build_light.py`）

入口：[装配接缝](../../src/raricy_bot/assembly.py)、[staging 构建](../../tools/build_light.py)。
完整版与 Light 共用一份 App：`app.py` 不 import `mcp/` 与 `blog/`，完整版的构造经
[mcp/assembly.py](../../src/raricy_bot/mcp/assembly.py) 的 `build_mcp_manager` 与
[blog/assembly.py](../../src/raricy_bot/blog/assembly.py) 的 `build_blog_service` 注入
（`__main__.py` 装配，见 §17）。没有 MCP 工厂时 App 用 `NoToolMcpManager`：生命周期空操作、
任何 feature 不可用，与 `mcp.enabled=false` 的真实 Manager 行为一致；配置启用却没有工厂是
装配错位，构造期抛 `AssemblyError`（`mcp_manager_factory_required` /
`blog_service_factory_required`），**不**降级、不被 `start()` 的软故障兜底吞掉（D-121）。
接缝类型只声明 App 真正使用的成员，不复制完整版接口。

Light 闭包 = `raricy_bot` 整包 − `mcp/` − `blog/` − 完整版 CLI 入口 `__main__.py`，
由 staging 构建现算：禁止出现 `mcp` / `raricy_bot.mcp` / `raricy_bot.blog` 导入，且任何
指向 `raricy_bot` 的导入都必须落在闭包内（源码树里存在、staging 里不存在 = 越界）。
共享持久层需要的发文记录与 UTC+8 日历放在包外的
[blog_records.py](../../src/raricy_bot/blog_records.py)（D-122），它不 import `blog/`。
验证：`tests` 的无 MCP 导入测试在屏蔽 `mcp` SDK 的子进程里导入闭包全部模块。

安装元数据见 [packaging/light/pyproject.toml](../../packaging/light/pyproject.toml)：
闭包用到的第三方发行版（`httpx`、`openai`、`PyYAML`、`aiohttp`，约束与根
pyproject 一致）加平台层绑定 `pywin32`，**不含 `mcp`**。清单与闭包导入由测试对照钉住 ——
新增第三方导入必须同时更新元数据，否则干净环境安装后会缺件。

## 57. `raricy_launcher` 原型（激活、进程回收）

入口：[激活协议](../../src/raricy_launcher/activation.py)、[Windows 平台层](../../src/raricy_launcher/platform/windows.py)、
[控制器](../../src/raricy_launcher/controller.py)。这是 L0 原型控制面：**没有**会话认证与
配置事务，§8/§9/§11 的正式实现（L3）整体替换它。

- 激活管道只承载固定动作（当前 `open_admin`）：单连接一条请求（客户端用 `CallNamedPipe`），
  请求 ≤512 B、响应 ≤4096 B，未知命令与多余键一律拒绝；管道 ACL 拒绝 NETWORK 主体。
- 响应写出后等待客户端读走再断开，这段等待**有界且可取消**（D-123）：窗口内客户端再发
  数据只说明连接还活着，读掉继续等；一直不读的客户端由窗口与取消事件界定。
  `close()` 必须能取消全部在途等待，不留下存活的服务线程。
- 启停：退出流程开始后（停止标志已置位）到达的 start 一律在生命周期锁内拒绝，避免
  `stop()` 返回后留下无人回收的 Worker；Worker 由 Job 对象兜底回收（D-120）。
  L0 的拒绝只在日志里留痕（`worker.spawn status=refused`），可区分的操作结果属 L3；
  L3 若按 §9.2 用 `stop → start` 组合出 restart，必须先清除停止标志，否则重启永远被拒。

## 58. `raricy_launcher` 的配置与凭据（L2）

入口：[档案布局](../../src/raricy_launcher/paths.py)、[配置事务](../../src/raricy_launcher/config_service.py)、
[v1 迁移](../../src/raricy_launcher/migration.py)、
[桌面设置](../../src/raricy_launcher/desktop_settings.py)、[凭据库](../../src/raricy_launcher/credential_store.py)、
[数据档案锁](../../src/raricy_bot/data_lock.py)。

- **档案布局**（§13.1）：`launcher.json` 只放活动档案指针与 schema；每个档案有
  `profile.json`、`config.yaml`、`draft.yaml`、`revisions/`、`data/`、`knowledge/`、
  `logs/{runtime,errors}/`。
  路径判定一律先规范化（绝对、解析链接/重解析点、大小写）再做包含检查：档案内的存储、
  记忆、知识与归档目录必须落在当前档案目录内（D-122 的路径口径见 §9.5）。
- **配置三个对象**（§6.1）：`EditableConfig` 是表单可编辑字段的白名单（`EDITABLE_FIELDS`），
  `CredentialUpdate` 是 keep/replace/delete 三种凭据操作（delete 目前只保留在服务层，
  API 入口会拒绝，见 §59 的「凭据删除」），`Config` 仍是 Core 的冻结运行配置。
  Launcher 掌控的字段（站点地址、档案内路径、探针监听、MCP/发文关闭）由基线映射提供，
  不接受提交；不在白名单里的键**拒绝**而不是静默忽略。
- **提交协议**（§6.4）：`expected_revision` 不符即 `ConfigConflict`；校验（字段 + Light 策略 +
  凭据齐备）全部在写盘之前；替换凭据时先登记脱敏、写库、回读确认；随后写 `revisions/<rev>.yaml`
  快照与同目录临时文件，fsync 后 `os.replace` 原子替换。任一步失败都保持旧版本，并清理
  **本次新建且未被引用**的凭据；旧凭据不自动回收（运行实例与回退版本仍可能引用它们）。
- **草稿**（§6.2）走同一套校验，只接受 `kind == missing` 类错误（"还没填"），
  `invalid` 一律拒绝；草稿有自己的 revision，不改变正式配置，也不接触凭据库。
- **凭据库**（§7）：引用是随机不透明标识，载荷 `{username, password, llm_api_key}` 只进后端；
  `SystemKeyringStore` 惰性导入 `keyring`，后端必须来自 `keyring.backends`（系统后端命名空间），
  名字里出现空/明文/必然失败的特征词一律拒绝，**链式后端逐环递归检查**（`ChainerBackend`
  可以把写入转交给明文后端）；不可用时由调用方降级到 `SessionMemoryStore` 并如实报告
  （重启后引用不可解析 → `needs_credentials`，不假装已保存）。
- **状态**（§9.1）：`needs_setup` / `needs_credentials` / `configured` / `invalid` / `recovery`；
  `invalid` 覆盖版本、字段、能力策略与路径包含，`recovery` 专指元数据故障（见下一条），
  `error` 一律是稳定类别码（不是中文文案）。
  账号名在档案内**不可变**（§13.3：换账号走重新设置），因此界面显示的账号与实际登录凭据
  不会拆成两个事实。
- **元数据状态与恢复**（§9.1、§13.3，D-130）：`read_launcher_metadata()` 区分「文件不存在」
  （返回空映射，首次运行要靠它）、`OSError`（`metadata_unreadable`，权限/占用错误不得当成
  空元数据）与「读到了但不能用」。后者再分三码：`metadata_corrupt`（非 UTF-8 字节、YAML
  语法错误、顶层不是映射、`schema_version` 类型不对）、`metadata_unsupported_version`
  （`schema_version` 大于本程序的 `LAUNCHER_SCHEMA_VERSION`）、`metadata_pointer_invalid`
  （文件可读且是映射，但 `active_profile` 缺失或不是合法档案 ID；元数据文件缺失但
  `profiles/` 下已有档案目录同样按此处理，因为指针无从解析）。缺 `schema_version` 是
  既有文件的正常形态，不算版本未知。四种码互相可区分，经 `status()` 映射为
  `recovery`（`error` 承载具体码，不抛异常），不是 `needs_setup`。**查询不修复、不创建、
  不覆盖**：只有「元数据文件不存在
  **且** `profiles/` 下没有任何既有档案目录」才允许 `ensure_first_profile()` 建立第一个
  档案（`require_profile()` 保留并委托给它，既有调用方行为不变）；
  读取路径（`status()`、`load_saved()`、`load_draft()`）不改文件、不建目录、不改指针；
  `set_active_profile()` 与提交在读元数据失败时直接失败，损坏现场字节不变。
- **固定档案绑定与查询不创建**（N1、D-133）：查询路径一律经只读的 `profile_or_none()`
  解析档案目录（无档案返回 `None`），`load_saved()`、`load_draft()`、`validate_values()`、
  `status()` 因此都不建目录、不写指针 —— 真正空的根目录上 `status()` 报 `needs_setup`
  且根目录不出现任何新文件。唯一创建入口是写路径的 `ensure_first_profile()`：条件与
  D-130 相同，且在同一次写入里落 `active_profile`、`active_epoch`（`(既有值 or 0) + 1`）
  与 `catalog_revision`（同式），不产生「指针有了但目录字段没写」的中间态。
  `ConfigService.for_profile(profile_id)` 返回绑定实例：共享数据根、凭据库与**同一把
  进程内写锁**（构造参数 `lock=` 可注入，缺省自建）；绑定实例的 `profile()` 直接由
  `profile_id` 求目录、完全不读 `launcher.json`，`status()` 只报该档案自身的状态。
  目录字段的窄写入口是 `update_catalog(changes)`：写锁内「读—改—原子写」且先读后写
  （读失败直接抛、绝不覆盖现场），只接受 `active_profile`（`validate_profile_id` 校验）、
  `active_epoch` / `catalog_revision`（非负整数，拒绝 `bool`）与 `schema_version`
  （只允许升到 `LAUNCHER_SCHEMA_VERSION`，当前值必须更小；降级与同级都报
  `invalid_catalog_change`）。**调用前提**：`launcher.json` 已存在，或本次 `changes`
  显式带上 `active_profile`；文件不存在时调用会写出没有指针的元数据，此后所有读取都按
  `metadata_pointer_invalid` 停在恢复态 —— 迁移的 catalog 步与 N2 的删除流程必须自己
  保证指针在场。
- **launcher schema 与档案内 schema 分开**（N1、D-133）：`CONFIG_SCHEMA_VERSION = 1` 仍是
  档案内 `config.yaml` / `draft.yaml` 的 `_launcher.schema_version`，取值与校验口径不变
  （`_to_saved()` 仍要求相等）；`LAUNCHER_SCHEMA_VERSION = 2` 是 `launcher.json` 本次写入的
  版本，读取侧接受 1 与 2（缺字段按旧文件）。**不得隐式升级**：`set_active_profile()`
  写指针时原样保留文件已有的 `schema_version`（缺字段的旧文件保持缺失），只有文件
  不存在的新根目录才写 `LAUNCHER_SCHEMA_VERSION`；升级只经 `update_catalog()` 的
  显式入口。
  `light_base_mapping(profile=None)` 省略四个档案内路径字段
  （`storage.db_path`、`knowledge_base.root_dir`、`memory.root_dir`、
  `logging.archive.directory`），其余取值不变，仅供「还没有档案」的查询与向导临时校验；
  正式提交路径始终传真实档案目录。
- **v1 迁移**（§10、D-137）：`migration.MigrationService` 把「单档案 + `launcher.json`
  （schema 1 或缺 `schema_version`）+ `profiles/<id>/config.yaml`」的既有安装接管到 schema 2。
  迁移在 `Controller.start()` 的**第一步**跑（§10.6），完全离线：不登录站点、不启动 Worker、
  不请求数据档案锁（单实例互斥体已由 `main.py` 在构造 Controller 之前取得）；失败、阻塞或抛错
  只记一条 `launcher.migration` 事件（字段 `status` 为阶段码、`error` 为稳定码或异常类名），
  UI 照常启动、恢复态由 `status()` 如实报告，`_auto_start()` 在结果 `ok` 不为真时不启动机器人、
  只打开管理页。
  `inspect()` 只读，阶段码固定（缺省无写入、不建目录）：`nothing_to_migrate`（数据根不存在，
  或既没有 `launcher.json` 也没有档案目录）、`already_migrated`（`schema_version == 2` 且迁移
  记录已完成）、`blocked_metadata_fault`（N0 的四码之一，携带该码）、
  `blocked_recovery_candidate`（没有可用指针：多个档案目录、指针损坏或根本没有指针；不选
  「最新修改目录」、不合并同名目录、不自动接管）、`migratable`。
  `migrate()` 先 `inspect()`，前四类直接返回（幂等、无写入）；`migratable` 时按固定顺序
  执行四步，每步在 `<数据根>/operations/migration-v1-to-v2.json` 里落 `prepared` / `done`：
  1. `backup`：复制 `launcher.json`、每个档案的 `config.yaml`、`draft.yaml` 与已有
     `profile.json` 到 `migration/backup-<UTC 紧凑时间戳>/`（如 `backup-20260926T141530Z`），
     保留相对目录结构；`manifest.json` **最后写**（`created_at`、`tool_version`、
     `files[{path, sha256}]`、`schema_from: 1`、`schema_to: 2`），半份备份因此可识别；
     同一时间戳已有完整清单则复用。
  2. `profile_record`：补写 `profiles/<id>/profile.json`（`identity_state="unverified"`、
     `site_user_id=null`、`state="active"`、`display_name` 取 `_launcher.account`，没有就空串；
     **已存在不覆盖**）。
  3. `catalog`：经 `update_catalog()` 写 `schema_version=2`、`catalog_revision=1`、
     `active_epoch=1`；`active_profile` 不动，已有值不覆盖、同值不重写（同值 `schema_version`
     会被该入口拒绝，见上一条）。
  4. `record`：`operations/migration-v1-to-v2.json` 置 `stage="completed"` 并写
     `completed_at`；失败保留已完成步骤码（`stage="in_progress"`），下次运行按阶段续跑，
     已完成的步骤不重做（清单完整的备份目录原样复用）。
  操作记录只保存 ID、revision、固定阶段码、受管相对路径与备份目录；备份与记录都只含非敏感
  文件，**不含**密码、模型 Key、任何凭据取值、System Prompt、聊天/知识库/记忆正文或原始异常。
  `identity_unverified` 的含义与解除路径：v1 档案没有可信的稳定站点 ID，身份校验因此不注入
  期望值（§4.2、D-136），首次受控登录验证前不假定身份；迁移绝不写 `identity_state="verified"`，
  解除只经 `bind_identity()`（N2 由一次性验证票据消费时调用）。旧程序读到 schema 2 会报
  `metadata_unsupported_version` 并停在恢复态，因此不会误写；回退必须用升级前的停机备份恢复
  整份数据目录（使用手册 §7）。
- **档案记录与身份**（§4.1，D-134）：`profiles/<id>/profile.json` 是档案级记录
  （UTF-8 JSON、键固定、`profile_revision` 每次写入 +1）：

  | 字段 | 含义 |
  |---|---|
  | `schema_version` | 记录版本，当前 `PROFILE_SCHEMA_VERSION = 1` |
  | `profile_id` | 随机不可变的档案 id（同目录名） |
  | `display_name` | 可改的本地标签；N2 才提供改名 |
  | `site_user_id` | 站点登录返回并验证过的稳定 ID；`null` 表示未验证 |
  | `identity_state` | `unverified` / `verified`，只有 `verified` 会被信任 |
  | `state` | 生命周期状态；N1 只写 `active`，`detached` / `deleting` 留给 N2，读到未知值原样保留 |
  | `profile_revision` | 记录版本（本模块管理，不等同 `config.yaml` 的 revision） |
  | `created_at` | 注入时钟的 ISO 时间，只用于显示与记录，不参与判定 |

  登录账号名**不**进 `profile.json`：仍以 `config.yaml` 的 `_launcher.account` 为唯一来源。
- **档案记录的读容错**（绝不把「读不出来」当成「没有」）：`profile.json` 不存在是
  v1 档案与尚未迁移根目录的正常形态，读记录返回降级默认值（`identity_state="unverified"`、
  `site_user_id=None`、`profile_revision=0`、`state="active"`）且**不补写文件**；存在但
  非 UTF-8、非法 JSON、顶层不是对象或版本字段类型不对 → `profile_corrupt`，`OSError` →
  `profile_unreadable`，`schema_version` 大于 1 → `profile_unsupported_version`；三种情况
  都不得覆盖现场。`ProfileError` 继承 `ConfigServiceError`，经 `api._handle` 自动得到
  409 + 稳定码。目录级的列表读（`list_profiles()`）跳过目录名非法或不是目录的条目，
  但**不**跳过读不出来的记录（那会掩盖故障）。写盘失败沿用既有 `config_write_failed`。
- **身份**（§4.2）：`unverified` 的档案跑起来**不注入**身份校验（Task 4 的接缝按
  `expected_site_user_id()` 决定），首次受控登录验证前不假定身份。`bind_identity()` 是
  绑定站点的唯一入口，写 `identity_state="verified"` 与 `site_user_id`；一个稳定 ID
  只属于一个可用档案：另一个 `state != "detached"` 的档案占用同一 ID 时抛
  `profile_identity_taken`（`detached` 不占用，供 N2 引导回已有档案）。
  `create_profile(site_user_id=…)` 只是预占，未验证前不参与校验。
- **创建与激活**：`ProfileService.ensure_first_profile()` 是首个档案的唯一入口
  （内部经 `ConfigService.ensure_first_profile()`，并补写缺失的 `profile.json`）；
  `create_profile()` 建立非活动档案并让 `catalog_revision` +1，已有指针不动 —— 只有
  `launcher.json` 尚不存在的全新根目录会在同一次写入里显式带上 `active_profile`、
  `active_epoch` 与 schema，避免留下没有指针的目录文件。
  `activate(profile_id, *, expected_epoch)` 是低层激活：**调用方负责先停稳 Worker、
  确认退出**（§5.1 第 2 条），`expected_epoch` 不符抛 `revision_conflict`；N1 不把它
  暴露成 HTTP 路由。`catalog()`、`active_profile_id()`、`list_profiles()`、
  `expected_site_user_id()` 只读，不创建、不补写。
- **运行快照**（§6.5）：`build_run_launch(revision)` 在写锁内**一次**取到「指定版本的运行
  快照 + 对应凭据」（分开调用会拼出旧配置配新 Key）；`build_run_config()` / `credentials_for()`
  都**必须显式给 revision**，快照写在档案自己的 `runtime/` 下，换档案不会互相覆盖。
- **档案路径**（§9.5）：`profile_dir()` 以规范化后的数据根为锚点，档案目录自身指向根外
  时直接拒绝 —— 只校验 id 会让后续以档案为根的包含检查跟着链接解析出去。
- **数据档案锁**（§9.5）：锁标识由规范化后的**存储目录**（数据库文件所在目录）派生，
  规范化要**先解析数据库文件自身的链接**再取父目录 —— 否则指向同一个数据库的两条路径
  会各拿一把锁。完整版 CLI 与 Light Worker 因此天然争用同一个标识；`acquire_data_lock()`
  立即取得，被占用时 CLI 以退出码 4 结束（§17）。锁文件里的 pid 只用于诊断，不是夺锁依据。
- **桌面设置**（§8、D-142）：数据根下与 `launcher.json` 同级的 `desktop.json` 是桌面偏好
  的唯一持久来源，`schema_version=1`；字段与默认值：`settings_revision=0`（文件不存在时）、
  `launch_at_sign_in=false`、`start_bot_on_launch=false`、`startup_profile_id=null`、
  `pending_startup_apply=null`、`last_apply_result="not_attempted"`。`settings_revision` 只随
  **用户意图**（前三个字段）改变而 +1，与档案配置的 revision **各自独立**；`last_apply_result`
  （`ok` / `not_attempted` / `command_too_long` / `path_unusable` / `registration_conflict` /
  `apply_failed` / `read_failed`）与 `pending_startup_apply`（`null` 或
  `{"action": "register"|"unregister", "command": "<命令>"}`，只是诊断、不参与重放）只能由
  `record_apply_result()` 写，**不改 revision** ——「意图 / 待应用 / 实际结果」三段式因此不会在
  同一次写入里互相覆盖。`update(expected_revision, ...)` 省略某个参数表示本次不改它，
  `startup_profile_id=None` 是显式清空（`_UNSET` 哨兵区分两者）；`expected_revision` 不符抛
  `DesktopSettingsConflict`。写盘与配置提交同一手法（同目录临时文件 + flush + fsync +
  `os.replace`），失败即「这一版没有生效」，旧文件不变。
- **桌面设置的唯一来源**（§8、D-142）：桌面偏好只认 `desktop.json`，本模块不读档案
  `config.yaml` 的 `start_bot_on_launch`（该字段只保留在历史快照与 N1 迁移的只读口径里）。
  唯一例外是升级用户的一次性导入：`desktop.json` 不存在而 `launcher.json` 存在时，`read()`
  读取当前档案的旧偏好并写入新文件（不递增 revision，紧随其后的 `update(expected_revision=0,
  ...)` 仍然成立），写完即不再回退到档案；**N1 的 `migration.py` 落地后，这条回退仍是升级
  用户的唯一导入路径**。查询不修复、不创建、不覆盖（与 D-130 同口径）：只有这一次导入会创建
  文件，损坏、版本不认识或不支持时只报码、不动现场；全新实例（没有 `launcher.json`）连默认值
  也不落盘。**配置面不再承载该偏好**（N4 Task 4）：`GET /api/config` 不再返回
  `start_bot_on_launch`；`PUT /api/config` 收到该键时在任何写入之前回 422
  `desktop_setting_moved`（`field=start_bot_on_launch`，`message=texts.DESKTOP_SETTING_MOVED`
  指向「桌面」页），不静默忽略、也不写第二份副本；`ConfigService.commit()` 不再接受该参数、
  不再写入 `_launcher.start_bot_on_launch`。档案里的同名字段只在**历史快照与迁移的只读口径**
  里保留（`SavedConfig.start_bot_on_launch`、`ConfigService.start_bot_on_launch()`、
  一次性导入），新提交不再产生它。
- **桌面设置的稳定码**（§8、§11）：`desktop_settings_conflict`（revision 不符）、
  `invalid_desktop_settings`（参数类型/取值非法）、`desktop_unreadable`（读不到：权限、占用）、
  `desktop_corrupt`（JSON 解析失败、顶层非映射、`settings_revision` 不是非负整数、布尔字段
  类型不对、`startup_profile_id` 不是合法档案 id、超过字节上限）、`desktop_unsupported_version`
  （`schema_version` 大于本程序）、`desktop_settings_write_failed`（写盘失败）。`str(exc)`
  就是码，不含路径、命令或异常原文。
- **操作记录**（`operations/<id>.json`、§5.2、N2、D-143）：协调器操作的**最小恢复记录**，
  形状固定，由 `lifecycle_service.OperationRecord.to_document()` 生成（键顺序固定）：

  | 字段 | 含义 |
  |---|---|
  | `schema_version` | 记录版本，当前 `OPERATION_SCHEMA_VERSION = 1` |
  | `operation_id` | `op-` + 12 位小写十六进制；同时是文件名（`paths.operation_record_path()` 按形状校验，`operations/` 里的迁移记录因此不会被认成操作记录） |
  | `kind` | `activate` / `remove` / `credentials_clear`；**单次启停不写恢复记录**（D-143） |
  | `state` | `reserved` / `running` / `finished` / `failed` / `cancelled` / `interrupted` |
  | `stage` | 固定阶段码（见 §59）；成功收尾停在 `finished`，失败/取消保留出事时的阶段 |
  | `profile_id` | 目标档案；`from_profile_id` / `to_profile_id` 是提交前后的指针（恢复时据此判断「已切到谁」） |
  | `target_revision` / `target_epoch` | 本次操作钉住的配置 revision，以及**预留时**断言的活动代次 |
  | `idempotency_key` / `retry_of` | 幂等键；`retry_of` 指向同一档案同一 `kind` 的最近失败/取消/中断记录（**纯诊断**，服务层的续做靠档案状态与阶段，不重放它） |
  | `started_at` / `updated_at` / `finished_at` | ISO 时间；`finished_at` 非空即终态 |
  | `error` / `result` | 固定错误码 / 结果码（取值见 §59）；`error` 与 `result` 不同时出现 |
  | `credentials` | `[{"ref": <不透明引用>, "state": <清理状态>}]`；**不含取值** |
  | `managed_paths` | 受管相对路径（移除/清除用），不含绝对路径 |

  写入与配置提交同一手法（同目录临时文件 + flush + fsync + `os.replace`），**关键副作用之前**
  先落盘；写不进去就让操作失败（`record_write_failed`），绝不在没有记录的情况下继续做副作用。
  记录里**不得**出现聊天、System Prompt、KB/记忆正文、密码、模型 Key 或原始异常文本。
  读记录只认 `operations/` 下形状合法的文件名：读不出来的**只记名字**，不改写、不删除。
  清理只对 `state == "finished"` 生效（最多 `MAX_OPERATION_RECORDS = 50` 条，先删最旧），
  `failed` / `interrupted` / `cancelled` 永不自动丢弃 —— 未完成的清理任务不能随日志轮转丢失。
- **协调器的两个档案写入口**（N2、D-143）：`LifecycleService` 经 `ProfileService` 的
  `commit_activation(profile_id, *, expected_epoch=None) -> int` 提交活动指针（写锁内「读—
  校验 `active_epoch`—一次写入 `active_profile` / `active_epoch + 1` / `catalog_revision + 1`」，
  不符抛 `revision_conflict`，返回新代次）；目录 revision 因此跟着前进，旧页面拿旧值再提交
  会被 `revision_conflict` 挡住。N1 的 `activate()` 保留原样（低层语义不变：只写指针与代次，
  **不** bump `catalog_revision`），两个入口共用同一段「读—校验—一次写入」，不产生第二份
  实现。`set_state(profile_id, *, state, expected_profile_revision=None)` 改档案生命周期
  状态（`state` ∈ `{active, detached, deleting}`，其他值抛 `invalid_profile_state`；写
  `profile_revision + 1`，带期望值时不符抛 `revision_conflict`），删除路径（N2 Task 3）
  已按这个目标形状落地（必须原子、走配置写锁），见本节的「档案状态与删除墓碑」条。

- **凭据引用归属索引**（N2、D-144）：`credentials-index.json` 与 `launcher.json` 同级
  （`paths.CREDENTIALS_INDEX_FILE` / `credentials_index_path()`），UTF-8 JSON，写入用
  同目录临时文件 + flush + fsync + `os.replace`。形状固定：顶层
  `{"schema_version": 1, "index_revision": 4, "entries": [...]}`，条目为
  `{"ref": "…", "profile_id": "p-…", "kinds": ["password", "llm_api_key"], "state": "owned",
  "revision": 7, "created_at": "…", "updated_at": "…", "last_error": null}`。
  `index_revision` 每次写入 +1；`kinds` 只取 `password` / `llm_api_key`（账号名不在其中）；
  `revision` 是引用它的配置 revision，未被引用时为 `null`；`last_error` 是稳定类别码。
  文档**不含**任何秘密取值（密码、模型 Key、账号名都不写）。五种 `state`：`pending`
  （已登记、凭据库写入未确认，或写入成功但还没有 revision 引用）、`owned`（已被某个
  revision 引用）、`revoked`（已从凭据库删除）、`pending_removal`（申请删除但后端失败，
  可重试）、`orphan`（创建失败且回滚删除也失败）。读取分级与 D-130 同口径：文件不存在 →
  空索引；`OSError` → `credentials_index_unreadable`；JSON 或结构非法 →
  `credentials_index_corrupt`（含超体积、顶层不是对象、版本类型不对、条目字段形状不对）；
  `schema_version` 大于本程序 → `credentials_index_unsupported_version`。三种故障都
  **不覆盖现场**，并让破坏性操作（`clear()` 与 Task 3 的移除）在写任何东西之前失败。
- **先登记后写库**（§6.3、D-144）：`CredentialLifecycle.reserve(profile_id=…, kinds=…)`
  先把 `pending` 条目落盘并返回新引用，调用方才写凭据库；提交成功后 `confirm(ref,
  profile_id=…, revision=…)` 标成 `owned`，提交失败则 `abandon(ref)` 尽力删除（删不掉
  留 `orphan` + `last_error`）。回读不一致仍抛 `credential_readback_mismatch`，且**不删除**
  不确定的引用（D-127），只把条目留在 `pending` 并记 `last_error`。`ConfigService.commit()`
  的凭据步骤因此是 reserve → put → 回读 → confirm/abandon；构造参数新增
  `credential_lifecycle=None`（缺省保持旧路径，仅供不装配索引的孤立测试），`for_profile()`
  的签名与共享语义不变，只把该对象一并传给绑定实例。
- **历史引用并集**（§6.1、D-144）：`reconcile(profile_id)` 读该档案 `config.yaml` 与
  `revisions/*.yaml` 里出现过的 `credentials_ref`，与索引并集：未见过的引用按 `owned`
  登记（`revision` 取引用它的最大版本），已经是 `pending` 但配置里确实引用了它的条目
  **认领回 `owned`**（确认前崩溃的恢复路径），并返回 `{"registered", "claimed",
  "unreadable"}` 计数。`reconcile_all(profile_ids)` 在 `Controller.start()` 的 `recover()`
  之后跑一次，只读 YAML + 写索引、**不碰凭据库**。`clear()` 撤销的是这个并集（当前版本
  与**能读到的**全部历史快照），不是当前那一条。**读不出来的历史快照不当成「没有引用」**
  （与 D-130 同口径）：它记录的引用既登记不了也撤销不了，因此照常推进能做的删除，但把这类文档
  记入 `ClearResult.unreadable_documents`（相对数据根的路径）并让 `ok=False`，
  `reconcile` / `reconcile_all` 的摘要带 `unreadable` 计数，只读的
  `unreadable_documents(profile_id)` 供卡片与移除预览判断 `unknown_ownership`；
  清除因此可能仍有残留，调用方必须显示 `credentials_cleanup_pending` 与人工核对提示，
  不得显示成「已清干净」。旧版本留下的、完全失去引用的 `RaricyBotLight` 条目**不枚举、
  不批量删除**（归属未知），只在页面与使用手册里提示到 Windows 凭据管理器人工清理。
- **清除范围与 `needs_credentials` 的新判据**（§6.1、D-144）：`clear(profile_id, kinds=…)`
  的 `kinds` 是 `{password, llm_api_key}` 的非空子集，空集或未知类别 →
  `credential_scope_required`。还有保留项时先把保留项复制到**新引用**（先登记后写库），
  再逐个撤销该档案的受管旧引用；两类都清时不建新引用（`credentials_ref=None`）。
  单条删除失败不中止其余：失败的条目标 `pending_removal` + `last_error` 并留在索引里。
  `ClearResult(new_ref, revoked, pending_refs, ok, unreadable_documents)`：`pending_refs`
  是删除失败、可重试的引用，`unreadable_documents` 是读不出来的历史文档（可能有残留），
  `ok` 只在两者都空时为真；`ok=False` 时调用方必须把档案留在不可启动态并显示
  `credentials_cleanup_pending`（不谎报已清除）。`retry_pending(profile_id)` 重试
  `pending_removal` / `orphan`（口径与 `clear()` 一致：重试成功也不等于已清干净）；
  `pending_profiles()` 供卡片与预览查询（含 `pending`：写过、还没有 revision 引用的
  条目也算清理待办）。
  `ConfigService.commit_credentials_clear(expected_revision=…, credentials_ref=…, account=…)`
  只改 `_launcher.credentials_ref`（`account` 非空时一并写账号名，其余键原样保留），写
  `revisions/<n>.yaml` + 原子替换（沿用 `_write_document`），字段与策略校验用
  `allow_missing_required=True`：清除后**配置结构仍然有效**，能否启动交给 `status()` ——
  `store.get()` 成功后 `username` / `password` / `llm_api_key` 任一为空即
  `needs_credentials`（`error=None`，与「没有引用」同形）。`_validate(allow_missing_required=
  False)` 的必填语义不变；`PUT /api/config` 的 `{"action":"delete"}` 仍回 409
  `credential_delete_unavailable`（D-131 不变，文案改成指向新的独立清除入口）。
- **档案状态与删除墓碑**（N2 Task 3、§6.2、D-145）：`profile.json` 的 `state` 取
  `active`（可用档案）/ `detached`（保留数据的移除：凭据已撤销、不再参与启动与查重，
  可以重新绑定同一账号）/ `deleting`（删除事务已登记：拒绝启动、拒绝配置与草稿写入，
  → `profile_state_conflict`，判定落在激活校验与账号页路由上）。
  `ProfileService.set_state(profile_id, state=…, expected_profile_revision=None)` 是状态
  的唯一写入口（非法值 → `invalid_profile_state`，期望值不符 → `revision_conflict`，
  写成功 `profile_revision` +1）；`clear_activation()` 是删除活动档案时的清空指针入口
  （一次写入 `active_profile=None`、`active_epoch` +1 与 `catalog_revision` +1，**不**
  自动选中别的档案）。**活动指针的显式 null 是合法状态**：`active_profile` 键存在且值
  为 `null` 表示「当前没有选中档案」，`_resolve_pointer()` 返回 `None` 而不抛错，
  `status()` 因此返回新增状态 **`no_selection`**（既不是 `needs_setup` 也不是
  `recovery`）；键缺失或取值非法仍是 `metadata_pointer_invalid`（F2、D-130 不变）。
  写路径（`ensure_first_profile()` / `require_profile()`）在没有选中档案时抛
  `no_active_profile`，**不**自动建立并选中一个新档案。`update_catalog()` 的
  `active_profile` 接受 `None`（清空），其余取值仍走 `validate_profile_id`。
  **墓碑**：彻底删除后在 `profiles/<id>/removed.json` 留一份最小记录
  `{"schema_version": 1, "profile_id": "p-…", "state": "deleted", "removed_at": "…",
  "scope": "purge_data", "catalog_revision": n}`（`catalog_revision` 是预览时确认过的
  目录 revision）—— 不含正文、账号或任何秘密；`data/`
  目录与锁文件一起保留。`list_profiles()` **跳过**含墓碑的目录（不报错、不删除、不影响
  `catalog`）；`raricy_bot.data_lock.refuse_removed_profile(profile_root)` 在共享入口
  **取得数据锁之后、打开 Store/归档之前**拒绝已删除档案（`DataLockError("profile_removed")`），
  两边的文件名常量 `paths.REMOVED_FILE` / `REMOVED_MARKER_FILE` 必须逐字一致。
  边界（D-145）：旧版本程序不认识墓碑，且手工把 `storage.db_path` 改到档案内更深层时
  `raricy_bot/__main__.py` 的检查会漏 —— 不宣称对任意旧 CLI 的保护。
  **墓碑判定只看 `lstat`**：`ENOENT` 才是「没有墓碑」，其余（权限、占用、无法解析、
  悬空链接）一律按有墓碑处理 —— `Path.exists()` 会把这些吞成 False，让读不出来的墓碑
  把已删除账号重新放回 `list_profiles()`。同理，档案内的类别目标（含 `data/`）**在
  预览、取数据锁与删除三处同一判定里拒绝链接/重解析点**：`acquire_data_lock()` 会
  `mkdir` 并写锁文件，操作系统会穿过 junction 解析，`data/` 被指到别处时锁会落在
  **外部目录**里；预览的大小统计对读不出来的类别把 `size_complete` 置假而不是报成
  0 字节的完整统计。移除的操作记录里，收尾阶段的记账失败**不改写结果码**
  （数据已删净/保留完成就是成功），只把 `record_write_failed` 记进记录的 `error`。

- **验证票据**（N2 Task 4、§4.2 第 3 条、D-146）：`VerificationStore`（
  [verification.py](../../src/raricy_launcher/verification.py)）只在**内存**里保存票据，
  有效期 600 秒；绑定 `session_id + profile_id + 精确账号 + 精确密码 + 服务端登录得到的
  `site_user_id`；`consume()` 一次性（成功即作废），未知/过期/已用 → `verification_invalid`，
  会话、档案、账号或密码不符 → `verification_mismatch`（不消耗，可改正后再提交）。
  票据对象只在 `verify` 与 `config` 两个调用之间持有密码，不落盘、不写日志、不进事件、不进
  操作记录；`Controller.stop()` 调 `revoke_all()`，进程重启后必须重新验证。
  `verify` 与 `/api/test/site` 共用两条判据（`manager.state ∈ {stopped, failed}` 且无未完成
  在途操作）与同一个站点登录路径；机器人运行时不代为停止，回 409 `bot_running`，由账号页先
  提示、再由用户调用 `/api/bot/stop`。**绝不采信前端自报的 `account_id`**：稳定 ID 一律取
  登录结果。
- **`detached → active` 的唯一路径**（N2 Task 4、§6.2、§59）：`detached`（保留数据的移除）
  档案不能用 `/activate` 回到可用列表 —— 唯一路径是 `PUT /api/profiles/{id}/config` 用**同一
  稳定 ID**的验证票据重新保存一次配置（登录得到的 ID 与存档不符时 `verify` 就回
  `profile_identity_mismatch`）。对**已绑定**档案改名不需要票据，写的是存档里的那一个
  `site_user_id`（`bind_identity(id, site_user_id=<存档 ID>, display_name=<新值>)`，
  `profile_revision` +1），不新增改名方法或端点。
## 59. Light 控制面（会话、API、进程与事件）

入口：[桌面入口](../../src/raricy_launcher/main.py)、[会话](../../src/raricy_launcher/session.py)、
[API](../../src/raricy_launcher/api.py)、
[进程管理](../../src/raricy_launcher/process_manager.py)、[IPC 协议](../../src/raricy_launcher/ipc.py)、
[事件](../../src/raricy_launcher/events.py)、[状态聚合](../../src/raricy_launcher/status_service.py)、
[生命周期门](../../src/raricy_launcher/lifecycle_gate.py)、
[生命周期协调器](../../src/raricy_launcher/lifecycle_service.py)、
[控制器](../../src/raricy_launcher/controller.py)、
[启动项服务](../../src/raricy_launcher/startup_service.py)、
[启动项适配层](../../src/raricy_launcher/platform/startup_windows.py)、
[桌面设置](../../src/raricy_launcher/desktop_settings.py)。

- **入口参数与来源提示**（§9.4）：`--worker` 仍优先按 Worker 分派，其后参数原样透传；
  否则按 Controller 入口解析，只识别字面量 `--startup` 与 `--no-tray`（都可出现在任意
  位置），其余参数照旧忽略。`--startup` 只是来源提示、**不是权限边界**（互斥体、生命周期门
  与授权偏好的判定都不放宽）；已有实例时静默去重退出并记一条 `launcher.startup_deduped`，
  不沿用 `open_admin` 激活分支、不打开浏览器，激活协议与命令集合不变。`--no-tray` 只关掉
  托盘装配（记一条 `launcher.tray_disabled`），控制面、激活与退出路径都不变。
- **托盘命令入口**（§61.2、D-140）：托盘的命令**不经过 HTTP** —— `Controller` 自己实现
  `tray_service.DesktopCommands`，与 `/api/bot/*` 共用同一把生命周期门、同一个管理器与
  同一套稳定码；窗口回调只投递结构化命令，耗时动作在协调器线程里执行（§7.2）。
- **自动运行解析与目标清理**（§5.2、§8.1、D-150）：`Controller._auto_start()` 每次进入只解析
  一次，固定顺序是**读偏好 → 校验目标 → 设选中指针 → 启动**。偏好只读 `desktop.json`
  （唯一来源，顺带完成升级用户的一次性导入），不再读档案里的旧 `start_bot_on_launch`：
  1. 偏好关：不启动机器人，只给入口（手动启动打开向导/管理页；登录启动按可见控制入口决定）。
  2. 目标档案**已被移除**（档案目录不存在）：经 `DesktopSettingsService.update()` 清空
     `startup_profile_id` 并把 `start_bot_on_launch` 置 false，**保留** `launch_at_sign_in`
     与其注册项，本次不启动；revision 冲突时不重试、不覆盖，只保证本次不启动。
     「已移除」只认 `FileNotFoundError` / `NotADirectoryError`：档案目录**读不到**（权限、
     被占用、数据根暂时不可用）**不等于**已移除 —— 读不到时保留目标与偏好、不启动
     （可区分状态 `target_unreadable`，且不做任何清理），下一次启动重新判定。判定不复用
     `Path.is_dir()`，正是因为它会把 `OSError` 吞成 `False`，把「读不到」说成「不存在」。
  3. 目标**暂时不完整**（`needs_credentials` / `invalid` / `recovery` / 读不出来）：保留目标与
     偏好，不启动、不自动清除（凭据可以再填、配置可以再修）。
  4. 目标可用：先把选中指针设为该目标（§5.2 切换事务的退化形态：本次没有运行中的 Worker
     —— 自动启动发生在启动机器人**之前**，所以没有停机步骤；事务就是「校验目标 → 提交选中
     指针 → 启动」）。这一步经 `ProfileService.activate()` 提交，**控制器不自行写
     `launcher.json`**；epoch 冲突或其它档案故障不重试、不覆盖，选中无法确认时本次不启动，
     不允许页面显示 A 而后台自动运行 B。
  5. 只有走到这里才启动一次，且仍先取 `LifecycleGate` 租约 —— `--startup` 不绕过并发门，
     派发返回后立刻释放（与 §9.2 的启停入口同一口径）。
  手动启动保持原行为：偏好开且当前档案可用就用**当前选中档案**启动，否则打开向导/恢复页；
  登录启动没有明确目标时不启动（不猜档案），默认不打开浏览器；托盘不可用（`--no-tray` 或
  装配失败，`Controller._tray_available()` 如实报告 `_tray` 是否装配成功）时降级为最多打开
  一次管理页。目标的状态按**目标档案自己**读（`_profile_status()`：带显式档案 id 的只读查询，
  不建立档案、不写指针）。
  接缝已接通（N1、N3 已并入）：`_select_startup_profile()` 经 `ProfileService.activate()`
  提交活动指针（`launcher.json` 归档案服务写，控制器不自行落盘）；epoch 冲突或其它档案故障
  不重试、不覆盖，本次不启动（fail-closed）。`run()` 先 `_start_tray()` 再 `_auto_start()`，
  托盘可用性判据因此读到的是本次运行真实装起来的结果。
  用户退出 Light 后本次会话不自动复活；已有实例的重复入口（含 `--startup`）在入口层静默去重。
- **登录启动项**（§8、D-148）：只碰当前用户 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
  下本产品自己的值名 `RaricyBotLight`（`REG_SZ`），值内容固定为
  `"<绝对 EXE 路径>" --startup`（引号包围路径、一个空格、恰好一个固定参数；
  `startup_service.build_startup_command()`）。命令按 `len(command)` 计上限 260 字符，
  超限拒绝、**不静默截断**；非冻结发行形态（防止把 `python.exe --startup` 写进 Run）、
  EXE 路径为空、非绝对或不存在也一律拒绝，且拒绝在**写入之前**判定。**归属判定只用命令
  格式**：只有形如上述格式的值才认为本产品持有、可以覆盖与修复；其他形态（别的应用写在
  同名值里的命令行）一律 `registration_conflict`，不覆盖、不删除，原值一个字节都不动。
  不枚举其他启动项（只按固定值名访问），也不读、不写 `StartupApproved` —— 系统侧
  （任务管理器、设置页、企业策略）的禁用决定没有受支持的读取方式，程序不覆盖它。
- **启动项读写边界**（§8）：`platform/startup_windows.py` 的 `StartupRegistry`
  （`runtime_checkable` Protocol，三个方法 `read_value(name) -> (bool, str | None)`、
  `write_value(name, command)`、`delete_value(name)`，删除幂等、键或值不存在不算错误）
  是唯一通道。`WinRegistryStartup` 是标准库 `winreg` 实现：根只用 `HKEY_CURRENT_USER`，
  读取用 `KEY_READ`、写入用 `KEY_SET_VALUE`（`CreateKeyEx` + `SetValueEx(REG_SZ)`）。
  `get_startup_registry()` 在 win32 下惰性导入实现，其他平台抛
  `PlatformError("unsupported_platform")`（与 `get_platform()` 同风格）。适配层错误
  `StartupRegistryError` 的消息是内部类别码（`startup_registry_read_failed` /
  `_write_failed` / `_delete_failed`），不透传 `WinError` 原文；服务层把它映射为
  `read_failed` / `apply_failed`，这几个内部码不出现在 API 响应里。**自动化测试只注入
  内存替身，不打开真实注册表**（N4 全局约束）。
- **启动项事实与状态**（§8、D-148）：`StartupService(settings, registry, *, executable,
  frozen, path_exists=os.path.isfile)` 的三个入口 —— `status()`（只看不写）、`apply()`
  （按当前意图登记/注销，写前判定、写后回读、把结果写回设置文件）、
  `repair(expected_revision)`（revision 不符抛 `DesktopSettingsConflict`）—— 都返回
  `StartupFacts`（frozen dataclass，可直接 `dataclasses.asdict`）。字段就是五项分离事实
  `requested_enabled`（`desktop.json` 的 `launch_at_sign_in`）、`registration_present`、
  `command_matches`（读到的内容与 `expected_command` 逐字相等）、`executable_exists`、
  `last_apply_result`，加上 `effective_state`、`divergence`、`expected_command`（本程序
  算出的命令，算不出来时是空串）与 `pending_apply`。**不回显注册表里读到的原始命令
  内容**：只给本程序算出的命令与布尔事实，别的应用写在同名值里的命令行不会被带回本机
  页面。`status()` 不回写设置文件；注册表读不到或同名值非本产品持有时，返回的事实里
  `last_apply_result` 是**本次观测**的结论（`read_failed` / `registration_conflict`，
  连同待应用诊断），文件里保留上一次真实应用的结果。
- **`effective_state` 判定**（确切值总表；判定只有一处实现，各任务不得各写一套）：
  `unknown` **优先** ——
  注册表读不到、同名值无法确认是本产品持有、或上次结果是 `apply_failed` / `read_failed`
  且未回读一致（含「意图为关但值未删掉」，**不假报关闭成功**）；`enabled` =
  `requested_enabled=true` 且三条事实全为真且 `last_apply_result ∈ {ok, not_attempted}`
  —— 语义只是「登记完整且路径有效」，**不表示「下次登录必定启动」**（界面文案不得出现
  这类承诺）；`needs_repair` = 意图为开但登记不完整（值被删、搬目录、EXE 不在原位置）；
  `disabled` = 意图为关且值已不在。`divergence` = 意图与登记事实不一致
  （`requested_enabled != registration_present`，或意图为开而 `command_matches=false`），
  或 `last_apply_result ∈ {apply_failed, read_failed}`。`apply()` 写完必须回读：命令发出
  去了不等于事实成立；应用失败**不回滚意图**（意图是用户要的、结果由 `record_apply_result`
  单独记录）。`repair()` 一律按**当前** EXE 路径重新生成命令，**永不重放**
  `pending_startup_apply`：它只是「上次想写什么」的诊断记录，重放会把搬目录前的旧路径
  写回注册表。
- **桌面设置与启动项端点**（§8、§58、D-149）：三个端点都要求已认证会话，写请求另走
  Origin / Fetch Metadata / CSRF / JSON 内容类型与请求体上限。
  - `GET /api/desktop-settings` → `{"ok": true, "settings_revision", "launch_at_sign_in",
    "start_bot_on_launch", "startup_profile_id"}`；失败按稳定码映射（`desktop_unreadable` /
    `desktop_corrupt` / `desktop_unsupported_version` / `desktop_settings_write_failed` 都是 409，
    **必须显式映射**，否则一次性导入落盘失败会变成 500）。
  - `PUT /api/desktop-settings`：body 严格白名单 `{"expected_settings_revision": int,
    "launch_at_sign_in"?: bool, "start_bot_on_launch"?: bool, "startup_profile_id"?: str|null}`。
    缺版本守卫、多余键、类型不对 → 422 `invalid_desktop_settings`（`field` 指到具体键）；
    revision 不符 → 409 `desktop_settings_conflict`；`startup_profile_id` 形态非法同样 422
    （目标是否真的存在由档案服务判定，本阶段尚未落地）。开启 `launch_at_sign_in` 之前先做
    写前判定：命令超 260 → 409 `startup_command_too_long`、路径不可用/非冻结形态 → 409
    `startup_path_unusable`（`field=launch_at_sign_in`，带 `texts.py` 的固定文案），**且不写意图**
    ——做不到的偏好不落盘。成功 → 200 `{"ok": true, "settings_revision", "applied": bool,
    "startup": {…事实…}}`：写意图（revision +1）→ 按意图应用 → 回读 → 记录结果。应用失败
    **不回滚意图**，`applied=false` 加事实由页面显示差异。
  - `GET /api/desktop/startup-status` → `{"ok": true, "settings_revision", "requested_enabled",
    "registration_present", "command_matches", "executable_exists", "effective_state",
    "divergence", "last_apply_result", "pending_apply": obj|null, "expected_command"}`。
    **只读自有值、不回显原始命令**：不返回注册表里读到的内容（可能是别的应用写的），只给
    本程序算出的 `expected_command` 与布尔事实；不枚举其他启动项。这里的 `last_apply_result`
    是**本次观测**的结论（读不到或同名值非本产品持有时是 `read_failed` /
    `registration_conflict`），不写回设置文件、也不得当作持久值回用 —— 文件里保留上一次
    真实应用的结果。查询路径同样不落盘。
  - `POST /api/desktop/startup-repair`：body 只接受 `expected_settings_revision`（任何
    `command` 之类的键 → 422 `invalid_desktop_settings`，**不接受任意执行命令**）。按**当前**
    EXE 路径重新生成命令并执行，成功 → 200 同 `PUT` 的成功形状。**两个冲突来源都要映射**：
    revision 过期由服务层抛 `DesktopSettingsConflict` → 409 `desktop_settings_conflict`；
    同名值非本产品持有**不抛异常**、以事实返回 → 409 `startup_registration_conflict`；
    `apply_failed` / 读不到（`read_failed`，没有专属码）→ 409 `startup_apply_failed`，
    `command_too_long` / `path_unusable` → 各自的稳定码，都带 `texts.py` 的固定文案。
    启动时**不重放** `pending_startup_apply`：系统偏好只由用户在页面上按修复时改写。
- **会话**（§8.1）：引导令牌单次、限时（120 秒），经 URL fragment 交付；兑换成功即发放
  HttpOnly + SameSite=Strict 的会话 Cookie，并回一个会话绑定的 CSRF 值。兑换按 60 秒窗口限次，
  窗口会滚动，本机他人刷满也不能把用户永久挡在门外。会话只存内存，Controller 重启即全部失效。
- **请求门**（§8.1、§8.2）：每个请求校验精确 Host（含实际端口）；写请求还要 Origin /
  Fetch Metadata、CSRF 头与 JSON 内容类型三件齐备。请求体上限 256 KiB **在读取过程中**生效
  （先缓冲再检查等于没有上限）；知识库导入另有一个更宽的传输上限，让「单文件 1 MiB」
  成为真正生效的那道门。响应一律 `Cache-Control: no-store`，HTML 带 CSP 与
  `X-Content-Type-Options`；静态路径拒绝穿越。
- **配置面**（§11）：`GET/PUT /api/config`、`POST /api/config/validate`、草稿读写。
  读接口显式构造响应：只含可编辑字段、revision、账号与「凭据已配置/后端可用」三态，
  不返回凭据取值，也不返回可用于读取凭据的引用。校验失败回 422（`field` + 稳定码），
  revision 冲突回 409。
- **查询与纯校验不创建**（N1、D-135）：`LocalApi` 注入 `ProfileService`，每个请求
  只解析一次档案上下文 —— `_profile_id(create=False)` 经 `active_profile_id()` 只读解析、
  `_bound(profile_id)` 取该档案的绑定 `ConfigService`，之后所有读写都用这一实例
  （§4.1 末句）。`GET /api/config` 因此不建立首个档案：无档案时 `profile_id` 为 `null`、
  `state` 仍是 `needs_setup`、`values` 为空、`defaults` 只回 System Prompt 默认模板
  （基线按 `light_base_mapping(None)` 省略四个档案内路径字段）；有档案时取值不变。
  `POST /api/config/validate` 无档案时用数据根级实例（`light_base_mapping(None)` 基线），
  合法输入仍回 `200 {"ok": true}`、字段错误仍回 422 + 稳定码。`POST /api/test/site` 与
  `/api/test/model` 的**带显式输入**分支同样用 `light_base_mapping(None)`、`config_dir`
  取数据根，响应形状不变（`account_id` 仍不落盘）。真正空的根目录上这些查询都不产生
  `launcher.json`、`profiles/` 或任何新文件。**`no_active_profile`（409）只由需要已有档案
  的入口回**：`GET /api/kb/status`、`POST /api/kb/import`；启动/重启无档案仍回既有的
  `config_not_ready`。写路径 `PUT /api/config`、`PUT /api/config/draft` 才经
  `_profile_id(create=True)` → `ProfileService.ensure_first_profile()` 建立首个档案
  （`profile.json` 同时补齐）。读取档案时的元数据故障仍由应用级处理器映射成 409 +
  四个既有码，不再被折成「没有档案」。
- **凭据删除**（§7，D-131）：提交只接受 `keep` / `replace`；凭据项写成
  `{"action": "delete"}` 时，在任何写入之前立即回 409 `credential_delete_unavailable`，
  `field` 是该凭据名，响应另带 `message`（`texts.py` 的固定文案）说明暂不可用与手工撤销
  步骤。删除入口也不在页面上。不得把该动作继续转成 `CredentialUpdate.delete()` 后让提交
  撞上必填校验 —— 那样用户看到的 `credentials_required` 把「功能未交付」说成了「凭据没填」。
  delete 动作本身只保留在服务层（`ACTION_DELETE`、`CredentialUpdate.delete()`、
  `commit()` 的置空分支），完整清除在 N2 交付（D-131）。
- **服务层错误边界**（§11）：`ConfigServiceError` 及其子类由**应用级处理器**兜底，任何
  路由（包括在 `except ApiError` 之外调用服务层的启动/重启与知识库导入）都回同一套 JSON
  信封 `{"ok": false, "code": <稳定码>}`：冲突与配置服务态 409、字段校验 422；个别稳定码
  另带 `message`（`texts.py` 的固定文案，如 `credential_delete_unavailable`）。不能落成
  Starlette 的纯文本 500，否则恢复态下用户看不到诊断码。新增调用点自动被覆盖，不靠逐处
  包 try。
- **进程面**（§9.2）：`POST /api/bot/{start,stop,restart}` 立刻返回 `operation_id`（202），
  长等待在后台操作里；`GET /api/operations/{id}` 查固定阶段与结果码。指定版本必须是当前
  已保存的版本，否则立刻 409。退出流程开始后拒绝一切启动。
- **启动固定档案**（N1、D-135，§5.2 的输入固定）：`_bot_start` / `_bot_restart` 先
  `_profile_id()` 解析一次档案，再用该档案绑定实例的 `load_saved()` 校验 revision
  （请求指定时仍必须等于该档案已保存的版本，否则 409 `config_not_ready`）；生命周期门
  （`lifecycle_busy`）与 202 + `operation_id` 的既有顺序不变。`profile_id` 一路传到
  `WorkerManager.start/restart(profile_id=...)`、`Operation.profile_id`、
  `GET /api/operations/{id}` 响应的 `operation["profile_id"]`，以及
  **`spec_factory(revision, run_id, profile_id)` 三参签名**（`WorkerManager` 的
  `_spec_factory` 调用点已改成三参）。`Controller._build_spec(revision, run_id, profile_id)`
  用 `self._profiles.config_service(profile_id)` 取 `load_saved()` / `build_run_launch()` /
  `profile()`，**不再**在启动线程里重新解析活动指针；`Controller._auto_start` 与
  `api._bot_start/_bot_restart` 都显式传它。`WorkerManager.status()` 新增
  `running_profile_id`：与 `running_revision` 同点设置（`state=running`）、同点清空
  （停止、失败、回收、`shutdown`）。
- **身份校验的注入与上报**（N1、D-136）：Controller 在 `_build_spec` 里取该档案的
  `expected_site_user_id()`（只有 `identity_state="verified"` 且 ID 非空的档案才有值），
  经 `default_worker_spec(..., expected_site_user_id=)` → `worker_env(...)` 写进子进程
  环境变量 **`RARICY_LIGHT_EXPECTED_SITE_USER_ID`**（`EXPECTED_USER_ID_ENV`）；未验证身份的
  档案取到 `None` 时**不注入该键**，Worker 因此不校验（§10.4 的 v1 档案口径）。Worker 侧
  `worker_main._expected_site_user_id()` 把缺省或空串读成 `None`，转交
  `BotApp(config, expect_site_user_id=...)`。登录后不符时 `app.start()` 抛
  `SiteIdentityMismatch`，Worker 上报**既有 `log` 帧**承载的事件
  **`worker.identity_mismatch`**（`level="ERROR"`、`fields.reason` 固定为
  **`account_identity_mismatch`**，异常正文不进帧），并以既有 `EXIT_RUNTIME = 1` 结束 ——
  **不新增退出码**，稳定码由事件承载。
- **生命周期门**（F3、D-132）：站点测试与启停共用一把进程内、非阻塞的租约门
  （`lifecycle_gate.py`；控制器装配一个实例，`LocalApi` 构造注入，互斥范围就是这个对象）。
  `POST /api/test/site` 在整段执行期间持有租约，`manager.state` 检查与派发都在租约覆盖内
  由**工作线程**完成、租约只在该线程的 `finally` 释放（请求协程被取消时线程仍在跑，协程侧
  释放等于把门开在测试进行中）。`POST /api/bot/{start,stop,restart}` 先取租约再调用管理器，
  取不到就 409 `lifecycle_busy` 且不派发，租约在管理器调用返回后立刻释放。测试入口在**持租约
  期间**按两条判据放行：进程处于 `stopped` / `failed`，且没有未完成的在途操作。在途操作这条
  不可省：`restart` 的停止阶段会先把状态写回 `stopped` / `failed`、之后才写 `starting`，只看
  `state` 会在那段空档放行测试。拒绝分两个稳定码：进程确实 `running` 时沿用 `bot_running`
  （既有语义），门被占用、处于 `starting` / `stopping`、或仍有未完成操作时回 `lifecycle_busy`。
  门内不等待 Worker、网络或 keyring；操作之间不互斥，`WorkerManager` 对启停的串行化与 `stop`
  抢占 `starting` 的语义不变 —— 门只解决跨入口（测试 vs 启停）的竞态。
- **IPC**（§10.1）：控制通道（父→子：`stop`、`status_request`）与上报通道（子→父：
  `ready`、`status`、`log`、`stopped`）分向；帧有长度前缀与上限，信封固定携带协议版本、
  实例 ID、运行 ID 与序号，身份不符即终止会话。日志帧只承载 `log_event` 的白名单事件。
- **状态与事件**（§10.2、§12）：Worker 的 `status` 帧是状态快照的唯一来源，带采样时间；
  过期或缺失如实报 `stale` / `unknown`，不从日志猜。显式测试结果绑定 revision，配置一变
  即标为过期。事件环形缓冲 500 条、订阅者有上限；游标过旧或来自别的实例时发 `gap` 提示；
  SSE 心跳 15 秒、慢消费者丢帧而不拖住发布方。
- **状态 DTO 的档案身份**（N1、D-135）：`StatusService.__init__` 新增 `profile_service=None`
  （缺省只供不装配档案视图的测试；正式装配由 Controller 注入 `ProfileService`）。
  `snapshot()` 顶层新增五个字段：`active_profile_id`、`running_profile_id`、
  `startup_profile_id`、`profile_epoch`、`pending_operation`。
  `active_profile_id` / `profile_epoch` 取自 `profile_service.catalog()`（活动指针与
  `active_epoch`）；`running_profile_id` 取自 `manager.status()`；`startup_profile_id` 是
  「活动档案且 `config.start_bot_on_launch()` 为真」的过渡口径（N4 换成 `desktop.json`
  的启动目标）；`pending_operation` 在 `manager.current_operation()` 未完成时给出
  `{"operation_id", "kind", "state", "profile_id", "revision"}`，否则 `null`。
  `catalog()` 或配置读取抛 `ConfigServiceError` 时这三个档案字段**降级为 `None`**，不抛：
  元数据损坏时 `/api/status` 必须仍是 200，原因由 `config.state == "recovery"` 与它的
  稳定码承载。`restart_required` 的判据扩为「已运行且（`running_revision != saved_revision`
  **或** `running_profile_id != active_profile_id`）」—— 同号 revision 换档案也要提示重启。
  `TestResult` 新增 `profile_id` / `profile_epoch`，`record_test(..., profile_id=,
  profile_epoch=)` 可选；`_test_view()` 的过期判定改为：档案 id 不同即 `stale`（两边都是
  `None` 时退回按数字 revision 比较），代次只在两边都记录了它时参与比较，数字 revision
  仍参与 —— 身份键是 `(profile_id, config_revision, profile_epoch)`（§5.1 第 6 条）。
- **生命周期协调器**（§5.1、§5.2，N2、D-143）：`lifecycle_service.LifecycleService` 管
  **跨操作**的串行化、取消代次与 A→B 事务，与 `lifecycle_gate` 的**单次操作短租约**是
  两层（协调器不重写门，也不绕过门：触碰 `WorkerManager` 的那一小段仍取租约，只是取不到时
  在门外有界等待 30 秒、每 0.2 秒重试一次，仍未取得就让操作 `failed` / `error="lifecycle_busy"`，
  可重试）。它注入 `ProfileService`、`WorkerManager`、进程内唯一的 `LifecycleGate`、
  可选 `EventService`；时钟、sleep 与 `exit_confirm_timeout` 可注入（测试不做真实等待）。
  - **串行化与不排队**：任一时刻最多一个协调器操作（`activate` / `remove` /
    `credentials_clear`）。`submit()` 在锁内检查「是否已有未完成操作」，有就抛
    `ConfigServiceError("lifecycle_busy")`（HTTP 409），**不排队、不等待**；持锁期间只记录
    操作租约与不可变输入（档案、revision、代次基线），绝不等 Worker、网络、keyring 或文件系统。
    `stop` / `quit` **不进**这条队列：它们只提高取消代次，因此停止意图永远追得上在途的切换。
  - **取消代次与停止意图优先**（§5.1 第 1 条）：`request_stop()` 先 `_generation += 1` 再
    `manager.stop()`；`request_quit()` `+= 1` 并**永久关闭本次实例的启动入口**（此后
    `submit()` 回 `lifecycle_busy`、`start_bot` / `restart_bot` 回 `quitting`），但它**不**调
    `manager.begin_quit()` —— `WorkerManager.shutdown()` 已经会调。每个操作在预留时记下代次
    基线，在**提交指针之前**与**启动 B 之前**各比较一次；本操作自己派发的停止会把基线推进
    一格，**但只在预留之后没有别的停止到达时才推进**（从预留到派停之间有落盘与线程启动，
    外部停止完全可能落在这一段；无条件推进会把那次意图抹掉，让事务照常提交并启动 B）。
    代次变了就按位置落
    `cancelled_by_stop`（提交前，保留 A）或 `selected_only`（提交后，保留 B），**两种情况都不
    启动 Worker**（§5.2 故障表第 5 行）。所有影响活动 Worker 的命令都带代次：
    `start_bot` / `restart_bot` 的 `expected_epoch` 与 `catalog().active_epoch` 不符时抛
    `revision_conflict`（`None` 表示「用当前值」，托盘与本地调用走这条）。
  - **A→B 六阶段与确定结果**（§5.2）：`validate_target → reserve_operation → stop_A →
    confirm_A_exited → commit_active_B → invalidate_old_views → [start_B] → finished`。
    `validate_target` 是**同步**的（在预留记录与停 A 之前）：目标必须是 `state="active"` 的
    档案（否则 `profile_state_conflict`），代次/目录 revision 过期是 `revision_conflict`，
    `start=True` 时还要求目标 `configured`、`expected_site_user_id()` 非空、`target_revision`
    等于该档案已保存的 revision 且凭据可解析（任一不满足 → `target_not_ready`，A 完全不动）；
    `start=False`（「只选中以修复」）跳过后一组检查。故障表八行的确定结果：

    | 故障/用户动作 | 确定结果 |
    |---|---|
    | B 不完整或凭据不可用 | 启动式切换在停 A 前 `target_not_ready`；`start=false` 可选中而不启动 |
    | A 无法确认退出 | `failed` / `error="stop_unconfirmed"`，阶段停在 `confirm_A_exited`，不切指针、不启动 B |
    | 写活动指针失败 | `failed` / `error="catalog_write_failed"`：A 仍是活动档案但已停止，B 不启动 |
    | 指针已提交、B 启动失败 | `finished` / `result="start_failed"`：B 保持选中，不回退到 A，失败原因由管理器操作承载 |
    | stop 在切换期间到达 | 提交前 `cancelled_by_stop`（保留 A）、提交后 `selected_only`（保留 B）；两种都不启动 |
    | quit 在任何阶段到达 | 同上一行（记录保留到足以恢复）；Worker 由 `manager.shutdown()` 收回，启动入口永久关闭 |
    | 响应丢失/重复提交 | 同键同摘要回同一 `operation_id`；同键不同摘要 409 `idempotency_conflict` |
    | Controller 崩溃 | `recover()` 把 `reserved`/`running` 改成 `interrupted` / `error="controller_restart"` 并补 `finished_at`；**只对账**，不重放、不启动、不改指针、不清除 |

    「A 已退出」的判据是**进程句柄层面**的回收：`manager.worker is None` 且
    `manager.status()["pid"] is None` 且停止结果 ∈ `{stopped, cancelled, forced_stop}`；
    超时（`EXIT_CONFIRM_TIMEOUT_SECONDS = 2 × STOP_BUDGET_MS + 5 秒 = 45`，可注入）或结果不在
    集合内都算未确认。目标与当前活动档案是同一个时跳过停与提交，退化成一次普通启动
    （不写指针、不 bump epoch，结果码仍用 `started` / `selected`）。`invalidate_old_views`
    发布事件 **`launcher.profile_activated`**（N2 Task 4 起 `profile_id` 已随
    `FIELD_KINDS` 登记进事件，见本节的「事件归属」条；代次以 `revision=epoch` 发布，
    `revision` 早已在白名单里；N1 的测试结果按身份键自然过期），不重写任何状态。
  - **幂等键**（§4.2、总表）：形状 `[A-Za-z0-9_-]{8,64}`（`IDEMPOTENCY_KEY_MIN_CHARS` /
    `_MAX_CHARS`），摘要 = 规范化请求字段的 JSON 排序键 + sha256，**输入不含秘密、摘要不落盘**。
    内存表保留最近 50 个键；服务重启后回落到记录级比较（同一键、同一 `kind`、同一目标档案、
    同一 revision 与代次）。**命中即返回，连 `validate()` 都不再跑**（响应丢失后的重复提交
    不产生第二次副作用，已失败/已取消的操作也回同一个 `operation_id`）；同键不同摘要回
    `idempotency_conflict`。重试**必须换新键**：发现同一档案同一 `kind` 有
    `failed`/`cancelled`/`interrupted` 记录时，新记录的 `retry_of` 指向最近一条（纯诊断）。
    键缺失、形状不合格由 API 层回 422 `idempotency_key_required`（`field="idempotency_key"`）。
  - **记录与恢复**（§58 的形状）：关键副作用之前先落盘；`state` / `stage` / `result` / `error`
    的取值就是 §58 与上面的表。`recover()` 在 `Controller.start()` 的迁移门之后、
    `_auto_start()` 之前调用，返回摘要 `{examined, interrupted, committed, write_failed,
    unreadable, active_profile_id}`：`committed` 是被中断的操作里指针**已经**落到它的目标档案
    上的那些（页面据此提示「已切到 B」还是「仍停在 A」），`unreadable` 只报文件名、不改现场。
  - **托盘端口要绑定的签名**（N3 已合并，本节冻结；接线只做委派，签名与
    `TrayCommandError` 稳定码不变）：`start_bot(*, expected_epoch: int | None = None) -> str`、
    `stop_bot() -> str`、`restart_bot(*, expected_epoch: int | None = None) -> str`，都返回
    **管理器的** `operation_id`。`stop_bot()` 走代次 + `manager.stop()`，**不受**「有未完成的
    协调器操作」阻挡（停止意图优先）；`start_bot` / `restart_bot` 在切换事务在途时回
    `lifecycle_busy`（不允许第二个启动与 A→B 并行）。三者都用**非阻塞**的短租约（与
    §9.2 的启停入口同一口径），门被站点测试占住时回 `lifecycle_busy`。
  - **状态与操作视图**：`current_operation()` 给 `StatusService` 的 `pending_operation`
    （协调器有未完成操作时优先于管理器的在途操作，形状为记录字段 + `stage`）；
    `operation(operation_id)` 给 `GET /api/operations/{id}`（先查协调器、再查管理器，两者都无
    才 404），视图字段见 `OperationRecord.as_operation_view()`。旧结果按身份键归属，**不匹配就
    丢弃**：协调器派发时固定 `profile_id` 与目标 revision，收尾只对仍是未完成态的记录生效，
    迟到或重复的收尾不覆盖当前状态。
  - **生产接线**（N2 Task 1b，接在 N3 之后）：`Controller` 装配进程内唯一的
    `LifecycleService`（注入同一个 `ProfileService`、`WorkerManager`、`LifecycleGate` 与
    `EventService`），并在 `start()` 的迁移之后、`_auto_start()` 之前调用一次 `recover()`
    （只对账；对账失败只记日志，不阻断控制面启动）。`request_quit()` 先通知协调器
    （关闭启动入口、提高取消代次）再关托盘消息循环；`stop()` 的拆机路径不变。
    托盘的 `start_bot` / `stop_bot` / `restart_bot` 就是委派协调器的三个同名方法：
    成功原样返回管理器的 `operation_id`，失败把稳定码转成 `TrayCommandError`
    （表内三种原样，其余档案/目录故障按 `config_not_ready` 报告）。**只换入口，不换门**：
    协调器内部对启停仍先 `begin_operation()` 取短租约再调管理器，租约不跨长等待，
    与 `/api/bot/*` 的 HTTP 路径完全同形；托盘路径自此也在派发时固定 `profile_id`
    与目标 revision（`_build_spec` 不再从可变活动指针推导）。

- **凭据清除的稳定码**（N2、§6.1、D-144）：清除是一次独立的协调器操作
  （`kind="credentials_clear"`），阶段固定 `stop` → `clear_credentials` → `commit_config`；
  结果码 `cleared` / `cleared_partial` / `clear_failed`。停止未确认 → `clear_failed` /
  `error="stop_unconfirmed"`，**什么都不清**（先停止机器人、清除凭据、移除账号是三件
  不同的事）。`cleared_partial` 覆盖两种情形：凭据撤了但配置没写上（档案不可启动、
  记录清理待办），配置写上了但 keyring 里有失败项。请求的 `kinds` 缺失或非法 → 422
  `credential_scope_required`。归属索引的三种故障（`credentials_index_corrupt` /
  `credentials_index_unreadable` / `credentials_index_unsupported_version`）→ 409，且
  这些故障下不写任何东西；清理待办的显示码是 `credentials_cleanup_pending`，页面必须
  如实展示并可重试，不谎报已清除。`PUT /api/config` 的 `{"action":"delete"}` 仍回 409
  `credential_delete_unavailable`（`message` 指向新的独立清除入口）。
- **移除服务与确认令牌**（N2 Task 3、§6.2、D-145）：`profile_removal.RemovalService`
  只接受**档案 ID**（内部一律 `paths.profile_dir()` 解析，绝不接受调用方给的路径），
  并把删除分成两种范围：`keep_data`（移除账号、保留本地数据 → 档案转 `detached`）与
  `purge_data`（彻底删除 → 删业务数据、留墓碑）。`detached` 档案只允许 `purge_data`，
  `deleting` 档案允许继续预览（重试）。
  - **预览**（`preview(profile_id, scope=…, session_id=…)`，只读）：返回类别与大小
    （`config` / `revisions` / `runtime` / `database` / `memory` / `knowledge` / `logs` /
    `credentials`，有界扫描最多 20000 个条目，超出时 `size_complete=false`）、
    `credentials` 归属摘要（`managed` / `cleanup_pending` / `unknown_ownership`，后者为真
    表示有读不出来的历史快照，归属不完整）、`is_active` / `running` / `is_startup_target`
    与两个 revision，并在内存里签发 `confirmation_token`（`expires_in = 300`）。
    预览**不改任何文件、不删凭据、不写记录**；未知或已删除（墓碑）档案 →
    `not_found`，范围非法 → `removal_scope_invalid`，路径有链接/重解析点 →
    `removal_unsafe_path`。
  - **令牌**：绑定 `session_id + profile_id + scope + profile_revision +
    catalog_revision`，只存内存、一次性、不落盘不写日志；同一会话对同一档案重新预览
    使旧令牌失效。未知/过期/已用/会话或档案不符 → `removal_token_invalid`；scope 或两个
    revision 与预览时不同 → `removal_preview_stale`。校验在**预留记录之前**同步完成，
    幂等命中（同键同摘要）不重跑校验，因此重复提交不会第二次消耗令牌。
  - **执行体**（协调器命令 `LifecycleService.remove(profile_id, scope=…,
    confirmation_token=…, session_id=…, idempotency_key=…)`，202）：阶段固定
    `preview`（落 `deleting`、写受管路径）→ `stop`（停运行中的该档案并确认退出、**非
    阻塞**取数据排他锁、清启动目标与活动指针）→ `clear_credentials`（先落引用与归属，
    再撤销全部受管引用）→ `detach` / `purge_data` → `finalize`。结果码
    `removed_detached` / `removed_purged` / `removed_partial` / `remove_failed`；
    错误码 `data_in_use`（数据被占用，停在 `stop`）/ `stop_unconfirmed` /
    `credential_backend_unavailable`（停在 `clear_credentials`，**不进入数据清理**）/
    `removal_unsafe_path`（删除被拒绝：重解析点、跨卷、身份不一致、解析失败或墓碑写不
    进去）。三种错误都表示「已清理的部分不回滚、档案留在 `deleting`、重新预览后可
    重试」；请求范围全部完成才算成功，部分完成要如实显示已清理/未完成类别。
  - **删除器**：逐层 `os.lstat`（绝不跟随），链接/重解析点、非普通文件、跨卷（`st_dev`
    不同）与身份变化（`(st_dev, st_ino)` 不符）一律停止推进；`OSError` 同样拒绝，**不
    退回字符串路径**。删除顺序是叶子文件 → 目录；`data/` 里只留 `.raricy-data.lock`，
  档案目录里只留 `removed.json` 与 `data/`。墓碑**先于** `profile.json` 落盘，崩溃后不
  会留下「没有记录也没有墓碑」的僵尸档案；`operations/<id>.json` 记录不随档案删除。
  两种模式都**不提供撤销**。
  - **记录里的受管路径**：开始阶段写本次范围（`purge_data` 的类别路径，`keep_data` 为
  空），收尾阶段改写为**仍未完成**的路径；`credentials` 数组在动 keyring 之前先写归属
  （`owned`），清完之后按结果写 `revoked` / `pending_removal`。
  - **共享入口拒绝**：`raricy_bot.data_lock.refuse_removed_profile()` 在 `worker_main`
  与完整版 CLI 取得数据锁之后、打开 Store/归档之前调用；失败沿用既有事件
  `worker.data_locked`（`reason="profile_removed"`）与退出码 `EXIT_DATA_LOCKED = 4`，
  不新增退出码。

- **账号 API**（N2 Task 4、§4.2、§9、[verification.py](../../src/raricy_launcher/verification.py)、D-146）：
  全部沿用既有会话（`_require_session`）、写请求校验（精确 Origin / Fetch Metadata / CSRF /
  JSON 内容类型 / 256 KiB 上限）与**严格字段白名单**：请求体出现未列出的键 → 400
  `bad_request`（`field` 指向第一个多余键），类型与长度错误 → 422 + 对应稳定码。
  成功信封一律 `{"ok": true, …}`，失败信封与既有形状相同（`ok`/`code`，可选 `field`、
  `message`、`details`）。

  | 方法与路径 | 请求字段（白名单） | 成功响应 | 失败（稳定码，HTTP） |
  |---|---|---|---|
  | `GET /api/profiles` | — | 200 `{"ok":true,"catalog":{active_profile_id,active_epoch,catalog_revision,schema_version},"profiles":[ProfileCard…]}` | 409 元数据故障码（N0） |
  | `POST /api/profiles` | `display_name?`、`expected_catalog_revision`、`idempotency_key` | 200 `{"ok":true,"profile_id":"p-…","catalog_revision":n}` | 409 `revision_conflict` / `idempotency_conflict` / `lifecycle_busy`；422 `invalid_value` / `invalid_revision` / `idempotency_key_required` |
  | `GET /api/profiles/{id}/draft` | — | 200 `{"ok":true,"revision":n,"values":{…}}` | 404 `not_found`；409 `profile_state_conflict` |
  | `PUT /api/profiles/{id}/draft` | `expected_revision`、`values`、`expected_profile_revision?` | 200 `{"ok":true,"revision":n}` | 404 `not_found`；409 `revision_conflict` / `profile_state_conflict`；422 |
  | `POST /api/profiles/{id}/verify` | `account`（1–256 字符）、`password`（1–4096 字符） | 200 `{"ok":true,"verification_id":"…","site_user_id":"…","chat_ready":bool,"expires_in":600}`；登录失败是 200 `{"ok":false,"detail":"…"}`（与 `/api/test/site` 同形，不签发票据） | 409 `bot_running` / `lifecycle_busy` / `profile_state_conflict` / `profile_identity_taken` / `profile_identity_mismatch`；422 `invalid_test_input` |
  | `PUT /api/profiles/{id}/config` | `expected_revision`、`expected_profile_revision`、`verification_id?`、`values`、`credentials`、`account?`、`display_name?` | 200 `{"ok":true,"revision":n,"profile_revision":n}` | 404 `not_found`；409 `verification_required` / `verification_invalid` / `verification_mismatch` / `profile_revision_conflict` / `revision_conflict` / `profile_state_conflict`；422 |
  | `POST /api/profiles/{id}/activate` | `expected_catalog_revision`、`expected_epoch`、`target_revision?`、`start`（默认 true）、`idempotency_key` | 202 `{"ok":true,"operation_id":"op-…"}` | 409 `revision_conflict` / `target_not_ready` / `profile_state_conflict` / `lifecycle_busy` / `idempotency_conflict`；422 `idempotency_key_required` |
  | `POST /api/profiles/{id}/removal-preview` | `scope` | 200 `{"ok":true,"preview":{…},"confirmation_token":"…","expires_in":300}` | 404 `not_found`；409 `removal_unsafe_path`；422 `removal_scope_invalid` |
  | `POST /api/profiles/{id}/remove` | `scope`、`confirmation_token`、`idempotency_key` | 202 `{"ok":true,"operation_id":"op-…"}` | 409 `removal_token_invalid` / `removal_preview_stale` / `removal_unsafe_path` / `profile_state_conflict` / `lifecycle_busy` / `idempotency_conflict`；422 |
  | `POST /api/profiles/{id}/credentials/clear` | `kinds`（`["password","llm_api_key"]` 的非空子集）、`idempotency_key` | 202 `{"ok":true,"operation_id":"op-…"}` | 409 `lifecycle_busy` / `idempotency_conflict` / 三种索引故障码 / `profile_state_conflict`；422 `credential_scope_required` / `idempotency_key_required` |
  | `GET /api/operations/{id}` | — | 200 `{"ok":true,"operation":{id,kind,state,stage,result,profile_id,revision,finished}}` | 404 `not_found`（协调器与管理局都没有） |

  - **202 语义**：破坏性与长操作（`activate` / `remove` / `credentials/clear`）只预留并派发，
    立刻回 `operation_id`；页面用 `GET /api/operations/{id}` 轮询，协调器记录带 `stage`
    （`reserve_operation` / `stop_A` / … / `finished` 等固定阶段码），管理局的单次启停 `stage` 为 `null`。
  - **404 / 422 的映射**：服务层对未知或已删除（墓碑）档案抛 `ProfileError("not_found")`，
    由 `api._handle()` 映射成 **404**；`idempotency_key_required` / `removal_scope_invalid` /
    `credential_scope_required` 映射成 **422**（各自带 `field`）。其余 `ProfileError` 与
    `ConfigServiceError` 仍是 **409** + 稳定码。
  - **`details` 白名单**：错误信封只并入 `existing_profile_id` / `profile_id` / `operation_id` /
    `scope` 四个键（`ApiError(details=…)` 在构造时拒绝其余键），服务层不能借它夹带路径、
    凭据引用或异常文本。
  - **只读语义**：`GET /api/profiles` 在空数据根回 200 + 空数组，**不创建**任何档案或指针；
    卡片的 `config` 取自该档案自己的 `ProfileService.config_service(id).status()`，一次请求
    内每个档案只解析一次上下文（§4.1）。`actions` 由服务端判定：`deleting` →
    `["remove","purge"]`；`detached` → `["purge","rebind"]`；其余 → `["edit","clear_credentials",
    "remove","purge","verify"]`，非活动档案在身份已验证且配置就绪时前面再加
    `["activate","activate_and_start"]`。页面不自己推断可行动作（§60）。
  - **创建与幂等**：`POST /api/profiles` 建立**非活动**档案（首个档案在全新数据根上会同时
    写入活动指针，与 N1 的 `create_profile()` 口径一致），并立即补齐 `profile.json`。
    串行化取生命周期门的短租约、并在协调器有未完成操作或退出流程已开始时分别回
    `lifecycle_busy` / `quitting`（与协调器 `_require_launch_context()` 同一个标志位）。
    幂等键的形状与比较规则和协调器一致（`[A-Za-z0-9_-]{8,64}`、锚点 `\A…\Z`；同键同摘要
    永远回同一 `profile_id`，同键不同摘要 → 409 `idempotency_conflict`），但**幂等表只在
    内存、没有记录级回退**：协调器的表覆盖的是写恢复记录的三种操作（`operations/<id>.json`
    在重启后仍能按按键+摘要复用），创建不写记录，因此**进程重启后同键同体重发会再建一个
    档案**。这是一处如实记录的缺口，不是「逐字一致」：多出来的档案是空的，可用常规移除
    流程删掉；进程内的重复提交不受影响。迁移方向见 D-146。
  - **清除命令的阶段与结果码**：`credentials/clear` 经协调器的 `submit()` 预留（记录写盘、
    串行化、幂等键），执行体阶段固定 `stop` → `clear_credentials` → `commit_config`；结果码
    `cleared` / `cleared_partial` / `clear_failed`，错误码沿用 `stop_unconfirmed` /
    `credential_backend_unavailable` 与 `config_write_failed`（凭据清了但配置窄写失败）。
    `clear()` 正常返回就照常 `commit_credentials_clear()`：`ok=false` 只影响结果码与卡片的
    清理待办，不表示什么都没清；只有 `clear()` 抛异常才是「凭据库这一侧完全没动」。
    **执行体目前住在 `api.py`**（不是计划里的 `LifecycleService.clear_credentials()`）：
    `KIND_CREDENTIALS_CLEAR` 与阶段码仍从 `lifecycle_service` 导入，结果码
    `cleared` / `cleared_partial` / `clear_failed` 与总表里没有的 `config_write_failed`
    由 `api.py` 定义 —— 这是本阶段唯一的定义处，迁移目标见 D-146。
  - **保存失败会消耗票据**：`PUT /api/profiles/{id}/config` 在 `commit()` **之前**消费
    票据（先钉死身份，再写配置），因此提交阶段的失败（例如 `expected_revision` 与已保存
    配置不符的 `revision_conflict`）也会让这张票据作废 —— 重试要先重新验证身份。
    改成「提交成功后再消费」会把「验证过的那份输入」与「写进配置的那份输入」重新分开，
    所以这是有意的取舍。输入不符（`verification_mismatch`）与状态/版本类前置拒绝
    （`verification_required`、`profile_revision_conflict`）发生在消费之前，不消耗票据。
- **过渡入口的活动代次门**（N2 Task 4、§9.4、D-146）：`PUT /api/config` 与
  `POST /api/bot/{start,restart}` 的请求体必须另带 `profile_id`（等于当前活动档案）与
  `expected_profile_epoch`（等于 `catalog().active_epoch`）：缺任一 → 409
  `client_upgrade_required`（明确要求页面升级，**不**把改动透明转发到刚切过去的账号）；
  在场但与当前不符 → 409 `revision_conflict`（`field` 指向 `profile_id` 或
  `expected_profile_epoch`）。首次设置（还没有任何档案）时正确取值是
  `profile_id: null`、`expected_profile_epoch: 0`。元数据故障（N0 的四个码）在判上下文之前
  如实抛出。`POST /api/bot/stop` 不变（停止不受代次门限制）。
  **门核过的档案必须一路带进写路径**：`PUT /api/config` 的工作线程只对门里核过的
  `profile_id` 写（不在线程里重新解析活动指针），并在写前复核 `active_epoch`：复核不符
  回 409 `revision_conflict`（`field="expected_profile_epoch"`）。复核与 `commit()` 不是
  同一原子步骤，最后一条缝里指针被切换/删除提交改走时这次写入会得 200；写入因此只保证
  「落在门里核过的那个档案上」，**绝不**落到另一个账号（含凭据替换）——这是尽力而为的
  检测，不是「指针被切走必然回 409」的承诺。只校验一次门、写路径再按指针解析，才是把
  竞态窗口挪了个位置 —— 两个档案 revision 同号时连 `expected_revision` 都挡不住。
  `POST /api/bot/{start,stop,restart}` 一律经协调器的 `start_bot(expected_epoch=…)` /
  `stop_bot()` / `restart_bot(expected_epoch=…)`：启停因此共用同一条串行化与取消代次
  （`stop` 会提高代次，在途切换据此收敛）。请求体里的 `revision` 不再参与判定 —— 启动绑定的
  版本一律取该档案当前已保存的那一版（`start=False` 的「只选中」不需要版本）。
- **事件归属**（N2 Task 4、§12、D-146）：`EventService.publish(name, …, profile_id=None, **fields)`
  把 `profile_id` 经 `logging_setup.build_event()` 的同一份白名单清洗（`FIELD_KINDS` 的
  `TOKEN` 类型），`Event.as_dict()` 顶层带 `"profile_id"`（全局事件为 `null`，不是缺键），
  SSE 帧因此天然携带。`Controller._on_worker_event()` 用
  `manager.current_operation().profile_id` 给进程与 Worker 事件打标；协调器的阶段事件
  （`launcher.lifecycle_stage`）与 `launcher.profile_activated` 带目标档案。**前端默认只展示
  `profile_id` 为空或等于当前活动档案的事件**（Task 5）；切换档案时清掉旧表单、密码输入、
  测试结果与待提交动作，服务端不代做这件事。

## 60. 管理页与发行（`frontend/`、`packaging/light/`）

入口：[前端工程](../../frontend/package.json)、[发行元数据](../../packaging/light/pyproject.toml)、
[冻结配置](../../packaging/light/light.spec)、[staging 与打包](../../tools/build_light.py)、
[冻结冒烟](../../tools/smoke_light.py)、[使用手册](../usage/LIGHT.md)。

- 前端是 Svelte + Vite 的单页应用，**资源全部本地**：构建产物直接落进
  `src/raricy_launcher/static/`（`index.html` + 带哈希的 `assets/`），由 Controller 同源提供，
  不依赖 CDN、远程字体或运行期 Node。改前端后必须重新 `npm run build` 再提交。
- 页面只用会话 Cookie 与内存里的 CSRF 值：引导令牌从 fragment 取出后立即清掉地址栏，
  密码与模型 Key 从不写进 LocalStorage / SessionStorage。
- 向导临时测试接口沿用会话 Cookie 与 CSRF：`POST /api/test/site` 可带恰好
  `{"username": string, "password": string}`，`POST /api/test/model` 可带恰好
  `{"base_url": string, "model": string, "api_key": string}`；空对象或无字段仍测试已保存配置。
  临时站点测试的 URL 取 Light 固定基线，不接受调用方传站点地址；站点登录成功时响应可含稳定
  `account_id`（即使后续聊天权限探测失败），仅此临时路径返回。响应只含 `ok`、固定类别
  `detail`、`elapsed_ms`，不返回生成内容或凭据。测试输入只在本次调用内使用，不写入正式/草稿配置，
  也不更新 revision 绑定的测试状态；已保存配置测试仍按原规则记录 revision 结果。模型测试仍受
  单次并发、固定样例、输出 token 上限和超时约束。
- **桌面页签与文案约定**（§8、D-149）：导航在既有三页之后新增「桌面」页签（不重排现有页面），
  由 `frontend/src/DesktopSettings.svelte` 承载三个开关（「登录 Windows 时启动 Light」「打开 Light
  时启动机器人」「启动目标档案」）、启动项事实、差异提示与「修复启动项」。机器码 → 固定中文
  文案映射放 `frontend/src/texts.ts`：`STARTUP_STATUS_NOTICES`（按 `effective_state`，与
  `RECOVERY_NOTICES` 同构）、`STARTUP_RESULT_NOTICES`（按 `last_apply_result`），
  `desktop_settings_conflict` 进既有 `CONFLICT_NOTICES`。`enabled` 的文案必须写明
  「Windows 可能延迟执行，或按你在系统设置里的选择跳过；本程序不修改该选择」，**不得**出现
  「下次登录必定启动」一类承诺。启动目标档案在档案列表接口（N2）交付前只显示当前值并说明
  由账号页管理，页面不自行列出档案。设置页移除原复选框并指向桌面页；向导保存成功后改用
  `PUT /api/desktop-settings` 写 `start_bot_on_launch=true` 与从 `GET /api/config` 读回的
  `profile_id`（写进 `startup_profile_id`；`profile_id` 为 null 时不写该字段、不猜），
  `expected_settings_revision` 取当前值。
- 发行构建：`tools/build_light.py` 生成 staging（Light 闭包 + 静态资源 + 构建信息），
  `--pyinstaller` 用 `light.spec` 冻结为 onedir/windowed 应用，`--zip` 打出 ZIP 与 `.sha256`。
  `build-info.json` 记录版本、协议版本、Python 版本、依赖清单与整包校验和。
- 托盘图标资源（N3）：`src/raricy_launcher/assets/` 下三个多尺寸 ICO —— `tray-normal.ico`
  （实心圆）、`tray-stopped.ico`（空心圆环）、`tray-attention.ico`（实心三角），尺寸集合固定
  `16/20/24/32/48/256`，背景透明，三个**形状本身**不同而不只靠颜色区分（无障碍要求）。
  资源由 `tools/make_tray_icons.py` 生成，该工具只用标准库、输出确定性（重复运行字节一致），
  并且**不进 staging、不进冻结包、运行时不导入**；不要手改 `.ico` 字节，改样式请改生成器再重跑。
  运行期按包目录下的 `assets/` 定位（与 `static/` 同法，PyInstaller 6.x onedir 下即
  `_internal/raricy_launcher/assets/`）；`packaging/light/light.spec` 的 `datas` 与
  `packaging/light/pyproject.toml` 的 `[tool.setuptools.package-data]` **必须同步**，
  缺一处就会有一种安装形态少图标。取舍理由见 [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) D-138。
- 首次启动路径（N1、D-133）：查询路径**不再创建**首个档案 —— 数据根还没有活动档案时，
  管理页读到 `needs_setup`、`GET /api/config` 只回默认 System Prompt 模板，根目录不出现
  任何新文件。首个档案只由写接口（`PUT /api/config`、`PUT /api/config/draft`）经
  `ProfileService.ensure_first_profile()` 建立（§5.1 第 1 步），向导因此仍可直接保存 ——
  把「没有档案」当错误会让首次启动失败，而把查询当写路径会让只读访问留下档案。
- 升级路径（N1、D-137）：v1 安装首次启动时由迁移先接管数据根（§58），此后数据根新增
  `profiles/<id>/profile.json`、`operations/` 与 `migration/backup-*/` 三处，都只含非敏感
  内容（档案记录、固定恢复码与受管相对路径、配置类文件的副本与 sha256），不含密码、
  模型 Key 或任何凭据取值。
- 验收边界：`tools/smoke_light.py` 覆盖「启动 → 激活 → 会话 → 状态 → 页面与构建产物 →
  凭据后端可用 → 退出 → 元数据清理」，运行时本机不能再有另一个 Light 实例（激活通道与
  互斥体按当前用户命名）。**干净 Windows 清单（§17.2）与真实站点/模型验收仍未执行**，
  见使用手册 §8。
- **账号页要用的字段**（N2 Task 4；服务端形状见 §59 的 ProfileCard）：页面只用
  `GET /api/profiles` 的 `catalog`（`active_epoch` 给 `activate` 的 `expected_epoch`、
  `catalog_revision` 给 `expected_catalog_revision`）与每张卡片的
  `profile_id` / `display_name` / `account` / `site_user_id` / `identity_state` / `state` /
  `profile_revision` / `config{state,revision,error}` / `is_active` / `is_running` /
  `is_startup_target` / `credentials{backend,cleanup_pending,historical_managed,
  unknown_ownership}` / `actions`。页面**不自己推断可行动作**，按 `actions` 呈现按钮：
  `activate`/`activate_and_start` 只用 `catalog.active_epoch` 与 `catalog_revision`、
  `edit` 走草稿与 `PUT …/config`、`verify` 先 `POST …/verify`（机器人运行时先提示并调用
  `/api/bot/stop`）、`clear_credentials` 走 202、`remove`/`purge` 必须先
  `POST …/removal-preview` 拿令牌、`rebind` 走 `PUT …/config` + 同一稳定 ID 的新票据。
  `credentials.cleanup_pending` 为真时必须显示清理待办与重试入口（§58）。
  切换账号后清空旧表单、密码输入、测试结果与待提交动作，并按事件归属过滤日志流（§59）。
  `PUT /api/config` 与 `POST /api/bot/{start,restart}` 的过渡门字段由页面从 `catalog` 读出后
  原样带上（首次设置是 `null` / `0`）。
  **文案**：账号页的稳定码文案与 `src/raricy_launcher/texts.py` 的新增常量一一对应
  （`client_upgrade_required` / `verification_*` / `profile_identity_*` /
  `profile_state_conflict` / `profile_revision_conflict` / `target_not_ready` /
  `idempotency_*` / `credential_scope_required`），页面不得另写一套说法。
- **账号页与四个主区**（N2 Task 5、§9、D-147）：页签顺序是「状态（概览）/ 设置（当前账号
  设置）/ 近期事件 / 桌面（桌面设置）/ 账号」——「账号」在既有页签之后新增，**不重排**
  已有页面；「近期事件」保留入口。概览页在「有档案但一个都没选中」（`no_selection`）时
  自动切到账号页；`needs_credentials` / `invalid` / 身份未验证各有独立提示面板与行动按钮，
  `recovery` 仍是只读面板（不加修复按钮）。首次设置向导只在 `GET /api/profiles` 为空且
  `config.state == needs_setup` 时出现，出现后锁定到用户保存或取消为止（保存过程中档案
  已经建立，不能把向导从用户脚下撤走）。向导保存时先经 `POST /api/profiles` 建立首个
  档案（空数据根上它同时成为活动档案），再用一次性票据走 `PUT /api/profiles/{id}/config`
  绑定身份 —— **N2 起身份只能经票据写入**，无票据的 `PUT /api/config` 路径不绑定身份，
  因此首次设置不能再只靠它完成。
- **账号卡片与动作渲染**（N2 Task 5）：账号页是 `frontend/src/Accounts.svelte`，卡片字段
  逐个来自 `GET /api/profiles` 的 `ProfileCard`（标签、账号、稳定 ID、身份/档案/配置三个
  徽标、选中/运行/启动目标标记、`credentials` 摘要）；**页面不自己推断可行动作**，按钮一律
  按服务端给的 `actions` 渲染，标签取自 `texts.ts` 的 `PROFILE_ACTION_LABELS`。动作语义：
  `activate` / `activate_and_start` 带 `catalog.active_epoch` 与 `catalog_revision`；
  `edit` 只对当前选中档案切到设置页（未选中时只提示先选中，避免改到别的账号）；
  `verify` / `rebind` 用账号+密码走 `POST …/verify` 再 `PUT …/config`（`values` 只提交
  与装载快照不同的字段；`rebind` 的账号字段锁定），运行中的机器人在获得用户确认后先
  `/api/bot/stop`、等状态变成 `stopped` / `failed` 再验证，**不自动重启**；
  `clear_credentials` 与 `remove` / `purge` 是独立入口（停止机器人 / 清除凭据 / 移除账号
  三件事不共用按钮）。`credentials.cleanup_pending` 与 `unknown_ownership` 为真时卡片如实
  显示待办与重试，不谎报已清理。
- **删除对话框的两步确认**（N2 Task 5、§6.2）：点「移除账号（保留数据）」或「彻底删除」→
  `POST …/removal-preview` 拿只读预览与一次性令牌 → 对话框列出类别与大小（`size_complete`
  为假时标注估计值）、是否运行 / 是否启动目标 / 是否当前选中、凭据归属提醒与「没有撤销、
  不影响站点已发内容」→ 勾选确认复选框（彻底删除用醒目文案）→ `POST …/remove` → 用
  `GET /api/operations/{id}` 轮询 `stage` 到收尾。**参数（scope）变化必须重新预览**：令牌
  绑定 scope 与两个 revision，`removal_preview_stale` / `removal_token_invalid` 一律要求
  重新生成预览，页面不自动重试。部分完成（`removed_partial` / `remove_failed`）时如实显示
  结果与停在的阶段，并给「重新预览」入口（已清理的类别不会回滚）。
- **切换档案时的页面状态**（N2 Task 5、§9 末段、D-146）：账号页、设置页与向导在活动档案
  变化时按 `{#key active_profile_id}` 重建组件，旧表单、密码输入、测试结果与待提交动作
  随之作废；「近期事件」默认只展示全局事件与当前活动档案的事件（`Event.profile_id`）。
  异步动作期间禁用重复提交，但**不做前端假同步**：202 + `operation_id` 是唯一依据，
  轮询没结束就如实显示「进行中」。
- **构建产物与页面目标**（N2 Task 5）：`npm run check` 与 `npm run build` 之后 `static/`
  的产物**必须重新构建并提交**（N2 的前端活只归 Task 5）。

## 61. Windows 托盘

入口：[视图模型](../../src/raricy_launcher/tray_model.py)、[协调器](../../src/raricy_launcher/tray_service.py)、
[文案](../../src/raricy_launcher/texts.py)、[状态聚合](../../src/raricy_launcher/status_service.py)、
[平台协议](../../src/raricy_launcher/platform/__init__.py)、[控制器](../../src/raricy_launcher/controller.py)。
本节的词表是这些实现共用的唯一来源，窗口层不认识别的字符串，未列入词表的命令与事件
一律忽略。窗口层（隐藏窗口、图标、系统事件）的实现与合同见 §61.4。

### 61.1 状态与图标

`tray_model.py` 是纯逻辑模块：不 import win32、不 import `platform/`、不做任何 I/O，
只依赖 `texts` 与 `status_service` 的常量/纯函数，因此全部映射可离线验证。

- **图标三态与判定顺序**（`icon_for()`，按顺序取第一条命中；`attention` 只表示
  「需要用户动作」）：
  1. 配置状态 ∈ `needs_setup` / `needs_credentials` / `recovery` / `invalid` → `attention`；
  2. 进程 `failed` → `attention`；
  3. 进程 `stopped` 且 `forced_stop` → `attention`；
  4. 电源 `awaiting_report`（睡眠恢复后无新上报）→ `attention`；
  5. `quitting`、进程 `stopped` / `stopping`、或电源 `suspended` → `stopped`；
  6. 其余（`starting` / `running`）→ `normal`。
  **运行中但上报过期/缺失不升级成 `attention`**：此刻没有可执行的动作，只改状态文案。
- **状态标签**（`status_label()`，同一套「第一条命中」顺序，文案全部来自 `texts.py`）：
  `quitting` → 正在退出；`recovery` / `invalid` / `needs_setup` / `needs_credentials` →
  对应配置文案；进程 `failed` → 启动失败；`stopped` 且 `forced_stop` → 上次运行被强制结束；
  电源 `suspended` → 睡眠中（未在线）；`awaiting_report` → 已恢复，等待新上报；
  `starting` / `stopping` → 启动中 / 正在停止；`running` 按 `worker_freshness` 取
  运行中 / 运行中（状态过期）/ 运行中（暂无上报）；其余 → 已停止。
- **新鲜度判据只有一处**：`worker_freshness` 必须来自
  `status_service.snapshot_freshness(snapshot, now=...)`，默认阈值复用
  `SNAPSHOT_STALE_SECONDS`（30 秒）；不得复制常量或另写判据。`StatusService.snapshot()`
  内部调用同一个函数，二者不产生第二个真相。
- **菜单**（`menu_for()`，顺序固定）：打开管理页（`open_admin`，始终可用）｜账号行｜状态行｜
  启动机器人（`start`）｜停止机器人（`stop`）｜重启机器人（`restart`）｜打开诊断目录
  （`open_diagnostics`，始终可用）｜退出 Light（`quit`，始终可用）。账号行为
  `TRAY_ACCOUNT_PREFIX + (account or TRAY_ACCOUNT_UNSET)`，状态行为 `TRAY_STATUS_PREFIX + 状态标签`；
  两条展示行 `command` 是空串且 `enabled is False`。分隔符在这张表里**不占独立菜单项**，而是
  挂在紧随其后的项上（`separator_before`）；窗口层渲染时要在**该项之前**补一条独立的
  `MF_SEPARATOR` 项，而不是把这一项自己变成分隔线（§61.4）。整个菜单 12 行 = 8 项 + 4 个
  分隔符。可用性：`start` = 配置 `configured` 且进程
  `stopped` / `failed` 且非 `quitting`；`stop` = 进程 `running` / `starting` 且非 `quitting`；
  `restart` = 配置 `configured` 且进程 `running` / `stopped` / `failed` 且非 `quitting`。
  禁用只是交互提示，服务端仍然自己判。N3 **不**放切换账号（等 N2 的页面）与登录启动设置
  （N4），也不放任何「未实现」占位项。
- **tooltip** 固定为 `TRAY_TOOLTIP_FORMAT.format(app=APP_NAME, status=状态标签)`，按
  `NOTIFYICONDATA.szTip` 上限截断到 127 字符，**不含**账号、pid、路径或原始错误。
- **词表**：命令常量 `open_admin` / `start` / `stop` / `restart` / `open_diagnostics` /
  `quit` 与事件常量 `taskbar_created` / `power_suspend` / `power_resume` / `session_query` /
  `session_end` 定义在 `tray_model.py`；配置与进程状态字面量（`configured`、`needs_setup`、
  `needs_credentials`、`recovery`、`invalid`；`stopped`、`starting`、`running`、`stopping`、
  `failed`）与该模块的本地常量必须与 ConfigService / WorkerManager（§59）逐字一致。

### 61.2 协调器与命令端口（`tray_service.py`）

- **线程归属**（§7.2）：`submit()` 只把词表里的字符串放进 `queue.SimpleQueue` —— 不阻塞、
  不做 I/O，窗口回调与 HTTP 线程都可安全调用；`start_bot` / `stop_bot` / `restart_bot` /
  `status_snapshot` / `process_view` / `entry_url` / `diagnostics_dir` / `open_url` /
  `open_path` / `begin_session_end` / `request_quit` **只允许在派发线程**（daemon，命名
  `raricy-tray`）里出现。窗口层方法（`run` / `present` / `request_close` / `close`）的
  线程归属见 §61.3 最后两条。
- **命令端口**：协调器只依赖 `DesktopCommands` 协议（`start_bot` / `stop_bot` /
  `restart_bot` / `status_snapshot` / `process_view` / `entry_url` / `diagnostics_dir` /
  `begin_session_end` / `request_quit`），`Controller` 自己实现它。命令 → 动作：
  `open_admin` → `open_url(entry_url())`（带一次性引导令牌，§8.1）；`start` / `stop` /
  `restart` → 对应 `Controller.*_bot`，命令完成后立即完整刷新并渲染；`open_diagnostics` →
  `open_path(diagnostics_dir())`；`quit` → `request_quit()`；`session_end` →
  `begin_session_end()`；`taskbar_created` / `session_query` 只重画一次（任务栏重建不重启
  任何东西；关机询问可能被取消，所以不声明正在退出）。未识别消息忽略并记
  `launcher.tray_message_ignored`（只有类别码，不带消息原文）。
- **与 HTTP 同门同码**：三个启停方法先 `begin_operation("start"|"stop"|"restart")`，取不到门
  抛 `TrayCommandError("lifecycle_busy")`，再调管理器，`finally` 里 `end(ticket)` —— 与
  §59 的 `/api/bot/*` 完全同形；租约只覆盖派发，不跨长等待。`start` / `restart` 要求
  `load_saved()` 非空并用它的 `revision` 作为目标版本，否则 `config_not_ready`；
  `stop` 不要求已保存配置。管理器回 `result == "quitting"` 或 `state == failed` 时，
  分别抛 `quitting` / `lifecycle_busy`。
- **稳定码**：`TrayCommandError.code` 只有 `config_not_ready` / `lifecycle_busy` /
  `quitting` / `tray_internal` 四种。被拒绝的命令只记类别码：日志与页面事件都用
  `launcher.tray_command_failed`（`status="rejected"`、`error=<码>`），原始异常正文不进
  日志、不进事件；**不弹任何对话框**。`status_snapshot()` 在退出流程开始后抛 `quitting`，
  协调器据此把图标切到「正在退出」并不再读凭据库。
- **刷新节流**（保护凭据库）：每个 tick（`TICK_SECONDS`，默认 1 秒）只用 `process_view()`
  重算进程状态与 `snapshot_freshness(...)`（无 I/O）；完整 `status_snapshot()` 只在四种
  时机调用 —— 协调器启动、任何命令执行完成、收到 `power_resume`、`FULL_REFRESH_SECONDS`
  （30 秒）边界到期。订阅了事件服务时，只有 `REFRESH_EVENTS` 那六个生命周期事件
  （`worker.starting` / `worker.started` / `worker.ready` / `worker.stopped` /
  `worker.exited` / `worker.start_failed`）额外触发完整刷新；聊天/日志类事件一律不触发，
  否则每条消息都会读一次凭据库。`events.subscribe()` 返回 `None`（订阅者已满）不算失败，
  `_run()` 的 `finally` 必然 `unsubscribe`。
- **电源与渲染**：`power_suspend` → 电源 `suspended`（睡眠前不声明在线）；`power_resume` →
  `awaiting_report` 并记下发时刻，之后每个 tick 用廉价视图等到一条
  `sampled_at >= 恢复时刻` 的上报才回到 `active`（Core 的重连能力不动，托盘只如实显示）。
  视图与上次相同就不重复 `present()`；`present()` 抛异常记 `launcher.tray_present_failed`
  （`error=<类型名>`）并继续，绝不退出派发线程。

### 61.3 装配、降级与退出顺序（`controller.py`）

- **装配**（`Controller.run()`）：`start()`（API 与激活管道，后台线程）→ `_start_tray()`
  （**当前线程**创建托盘与协调器）→ `_auto_start()` → `ready` → 有托盘跑 `tray.run()`
  消息循环，无托盘退回退出事件 → `finally: stop()`。默认托盘工厂用包目录下的
  `assets/` 建图标（与 `static/` 同法，§60）；`tray_factory` / `open_path` / `use_tray`
  是测试与 `--no-tray` 的注入缝。
- **降级**：托盘创建失败或消息循环启动失败（`PlatformError` / `TrayError` / `OSError`）时
  记 `launcher.tray_failed` 并发布同码（`error` 是稳定类别码），置空托盘后**继续运行** ——
  管理页仍会按 §8.1 打开，这是没有托盘时的可见入口。`--no-tray` 只记一条
  `launcher.tray_disabled`（info），不算失败。两条路径都不新建第二条控制路径。
- **退出顺序**：`request_quit()` 置退出事件后，若托盘在则再 `tray.request_close()`；
  `_stop_locked()` 的顺序是 `manager.shutdown()` → `coordinator.stop(timeout=2)` →
  `tray.close()` → 其余收尾（会话、事件、HTTP、激活管道、元数据、互斥体）不变。
  `coordinator.stop()` 有界等待派发线程，超时也继续收尾。`launcher.quit` 在本次退出
  由 `begin_session_end()`（注销/关机）触发时用 `status="session_end"`，不得写成优雅完成。
- **`present()` 的窗口未就绪语义**：首帧可能在消息循环开始前就呈现（`run()` 还在当前
  线程里排队），窗口层必须把视图**存下来**，并在图标加入通知区域后应用它，不能把这一帧
  当空操作丢掉。真正的窗口与图标释放在拥有窗口的线程上完成，`close()` 幂等。
  `TrayIcon` 协议与 `TrayError` 定义在 `platform/__init__.py`（非 Windows 平台仍由
  `get_platform()` 的既有 `PlatformError("unsupported_platform")` 拦住，不新增平台分支）。
- **`request_close()` 的窗口未就绪语义**：与首帧同构 —— 窗口**尚未创建**时不得把请求
  当空操作丢掉，而要记下「已请求关闭」，由 `run()` 在**建好窗口之后、进入消息循环之前**
  检查该标记，若已置位就直接走正常关闭路径（销毁窗口、`PostQuitMessage`）而不进循环。
  `run()` 建窗口发生在 `_start_tray()` 之后，而管理页与激活入口的退出走的正是
  `request_quit()` → `tray.request_close()`：先到的关闭请求被丢掉的话，`self._quit` 已置位
  却没人叫醒消息循环，进程会一直挂着不退出。窗口已创建时按常规投递 `WM_CLOSE`。

### 61.4 Windows 窗口层（`platform/tray_windows.py`）

[窗口层](../../src/raricy_launcher/platform/tray_windows.py)、[平台实现](../../src/raricy_launcher/platform/windows.py)、
[协议](../../src/raricy_launcher/platform/__init__.py)。`WindowsPlatform.create_tray()` **惰性**
import 本模块（`--no-tray` 与非 Windows 路径都不加载 `win32gui`），返回 `WinTrayIcon`；
窗口与图标在 `run()` 里才真正建立。

- **一个图标 = 一个隐藏的顶层窗口**：`RegisterClass` 注册 `RaricyBotLight.TrayWindow`，
  `CreateWindowEx` 用 `WS_OVERLAPPED`、**不带** `WS_VISIBLE`、`parent = 0` 建窗口，没有任何
  可见 UI。**禁止 `HWND_MESSAGE` 消息窗口**：`TaskbarCreated`、`WM_POWERBROADCAST`、
  `WM_QUERYENDSESSION`/`WM_ENDSESSION` 都只广播给顶层窗口，消息窗口收不到 —— 那样的代价是
  Explorer 重启后图标再也回不来、注销/关机也看不到。
- **图标资源**：`LoadImage(0, path, IMAGE_ICON, GetSystemMetrics(SM_CXSMICON),
  GetSystemMetrics(SM_CYSMICON), LR_LOADFROMFILE)` 加载 `assets/` 下三个文件（§60）；文件名
  与 `tray_model.ICON_*` 一一对应，词表外的图标名按停止态占位处理。加图标用
  `Shell_NotifyIcon(NIM_ADD, ...)`，随后 `NIM_SETVERSION` 声明 `NOTIFYICON_VERSION_4`（值 4）。
- **pywin32 的元组形状与本地常量**（本机 pywin32 build 312 实测；细节见模块 docstring）：
  `Shell_NotifyIcon(Message, nid)` 的 `nid` 是**元组** `(hwnd, uID, uFlags, uCallbackMessage,
  hIcon, szTip)`；`NIM_SETVERSION` 用**八元组**（第 7 位 `szInfo` 留空串、第 8 位是
  `uTimeout`/`uVersion` 联合槽放 4）。成功返回 `None`，**失败抛 `pywintypes.error`**。
  `NOTIFYICON_VERSION_4`、`NIN_SELECT`（`WM_USER + 0`）、`NIN_KEYSELECT`（`WM_USER + 1`）在
  pywin32 里**没有**，必须本地定义；`SM_CXSMICON`/`SM_CYSMICON` 在 `win32con`，只有
  `RegisterClass`（没有 `RegisterClassEx`）。`GetMessage(None, 0, 0)` 返回
  `[ret, (hwnd, msg, wParam, lParam, time, pt)]`，`WM_QUIT` 时 `ret == 0`。
- **消息与词表**：`WM_TRAY_CALLBACK = WM_APP + 1` 是 `uCallbackMessage`，
  `WM_TRAY_PRESENT = WM_APP + 2` 是状态更新投递（`present()` 从任意线程投，窗口线程收到后
  才 `NIM_MODIFY`）。窗口回调**只做三件事**：映射成 `tray_model` 词表常量交给 `on_message`、
  渲染、返回。回调内不做文件/注册表/网络/keyring/子进程/等待；异常在回调内被捕获并记
  `launcher.tray_callback_failed`（`error` 为稳定类别：`win32_error` 或异常类型名），
  **绝不抛回消息循环**。
- **v4 的等价关系**：`LOWORD(lParam) == NIN_SELECT` 或 `NIN_KEYSELECT`（左键单击/键盘选中）
  → `on_message("open_admin")`。v4 不再单独送 `WM_LBUTTONDBLCLK`：左键单击与双击合并成
  `NIN_SELECT`，与设计 §7.1「双击图标执行同一动作」等效 —— 打开管理页这个动作不需要区分
  单击还是双击。`NIN_BALLOON*` 一律忽略（N3 不做通知）。
- **右键菜单**：`LOWORD(lParam) == WM_CONTEXTMENU` 时 `SetForegroundWindow(hwnd)` 后用
  `TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY` 弹 `TrackPopupMenu`（坐标取 `wParam` 的
  `GET_X_LPARAM`/`GET_Y_LPARAM`，按有符号 16 位解释；多显示器在左侧时可能是负数）。菜单项
  id 是 1..n，按 `enabled` 挂 `MF_GRAYED`（可点项是 `MF_STRING`，`MF_STRING` 的值就是 0）；
  `TrayMenuItem.separator_before` 表示「**这一项前面**先补一条**独立**的分隔项」，不是「这一项
  是分隔符」—— 必须另起一次 `AppendMenu(MF_SEPARATOR, 0, "")`，而**不是**把 `MF_SEPARATOR`
  或到该项自己的标志位上。Win32 的 `MF_SEPARATOR` 语义是「画一条横线，`lpNewItem` 与
  `uIDNewItem` 都被忽略」（实测：`MF_STRING | MF_SEPARATOR` 的行 `GetMenuState` 带
  `MF_SEPARATOR` 位、文本被丢弃）：并到带命令的项上会让它变成一条不可选中的空线，菜单从
  12 行塌成 8 行 —— 账号行文案消失，`start`/`open_diagnostics`/`quit` 三条命令从托盘不可达
  （`TrackPopupMenu` 永远拿不到它们的 id）。**弹出前把视图拷成局部变量**，返回的 id 必须用
  同一份快照映射回命令 ——
  `TrackPopupMenu` 阻塞期间 `present()` 可能已经换掉视图，用新视图解释旧菜单的返回值会映射
  到错误命令。收尾 `PostMessage(hwnd, WM_NULL, 0, 0)`。空串（账号行/状态行）与 0（取消）
  都不投递。
- **`TaskbarCreated` 重加**（Explorer 重建任务栏）：`NIM_DELETE` → `NIM_ADD` →
  `NIM_SETVERSION` —— 菜单每次弹出都现场构建，不需要重建；重加后投递 `taskbar_created`
  让协调器重画一次。重加失败只记 `launcher.tray_icon_readd_failed`（`error` 是稳定码），
  不崩、不重启 Controller/Worker。
- **系统事件合同**（处理函数本身不做任何等待）：
  - `WM_QUERYENDSESSION` → **立即 `return True`**（同意注销/关机），并投递 `session_query`；
    不弹窗、不阻塞、不试图取消。
  - `WM_ENDSESSION` → `wParam != 0` 时投递 `session_end` 并 `return 0`；`wParam == 0`
    （关机被取消）什么都不做。
  - `WM_POWERBROADCAST` → `PBT_APMSUSPEND` 投 `power_suspend`；`PBT_APMRESUMESUSPEND` /
    `PBT_APMRESUMEAUTOMATIC` 投 `power_resume`；其余忽略；返回 `True`。
  - **有界收尾**：`session_end` 由协调器转成 `commands.begin_session_end()`（拒绝新启动 +
    请求退出），窗口线程**从不等待 Worker**；真正的停止仍走既有的 20 秒预算
    （`STOP_BUDGET_MS`）与 Job 的 `KILL_ON_JOB_CLOSE` 兜底。不新增「无限等待」路径、不改
    停止预算；系统提前终止进程时 `launcher.quit` 记 `status="session_end"`，不得写成优雅完成。
- **线程归属**：`run()` 必须在调用线程里建窗口并跑消息循环，**窗口因此属于那个线程**；
  `_owner_thread` 记下它，`NIM_DELETE` / `DestroyIcon` / `DestroyWindow` / `UnregisterClass`
  只在 `run()` 的 `finally` 里、仍在那个线程上执行（全部幂等，重复调用不抛）。
  `present()` / `request_close()` / `close()` 任意线程可调：只改锁保护的状态再投消息，窗口
  未创建或已销毁时不留异常；`close()` 在非拥有线程上等价于 `request_close()`，绝不跨线程
  销毁窗口。**两条「窗口还没准备好」的请求都不丢**：首帧视图先存下、`NIM_ADD` 之后套用；
  关闭请求先记下、`run()` 建窗口之后进循环之前兑现（见 §61.3）。
- **稳定码**：`TrayError` 只有 `tray_window_failed`（类注册/窗口创建/消息循环失败）、
  `tray_icon_missing`（图标文件缺失或加载不出来）、`tray_icon_failed`（加进通知区域失败）。
  `create_tray()` 把窗口层的一切失败（含惰性 import 失败与 `pywintypes.error`）归一成
  `TrayError`：`Controller._start_tray()` 只捕获 `(PlatformError, TrayError, OSError)`，而
  `pywintypes.error` **不是** `OSError` 的子类，逸出的原始异常会直接结束进程，让「托盘建不
  起来仍继续运行、管理页仍是入口」的降级路径落空（§61.3）。
