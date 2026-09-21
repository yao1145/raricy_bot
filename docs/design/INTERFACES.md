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
出站脱敏、段落截断、去重、配额预留；回复引用触发消息，返回实际发送正文供历史提交。
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
再运行 App。退出码：配置错误 2，归档已启用却打不开 3，运行期致命错误 1，其余 0；
任何一条错误路径都不回显凭据。停止须等待资源关闭，不能留下 pending task 或未关闭
客户端；归档在 `finally` 里同步并关闭。

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

[blog/models.py](../../src/raricy_bot/blog/models.py) 定义 Draft、PreparedDraft、BlogScope、
运行状态与投递状态，两套状态不可混用。hash_version=1 与 SQLite CHECK 一起维护，变更须处理旧行。

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

[blog/planner.py](../../src/raricy_bot/blog/planner.py) 为纯函数与 UTC+8 日界线唯一实现。
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
