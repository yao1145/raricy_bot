# 事件档案：线上异常现象与复现取证

面向维护者的事件登记簿。与 `docs/archive/` 不同，本文件**正常入库**。用途只有一个：
同类现象再次出现时**先查这里**——已定案的直接处置，未定案的就按该节的取证清单
**在重启之前**把现场抓下来，回来把那一节补完。

规则：

1. 一次事件一节，只追加不覆盖。未定案的事件长期保留，标题带「未定案」。
2. **事实与假设分开写。**事实必须带日志原文或代码位置；假设必须写清「还需要什么证据才能定案」。
3. **重启会销毁现场。**出现疑似本档案里的现象时，先执行该节的取证清单，再重启。
4. 日志时间是容器内 UTC；北京时间 = UTC + 8。
5. 代码位置写成「函数名（文件:行号）」，行号取自登记时的 `feat/public-personal-memory`
   分支（提交 `b38c6ea`）；行号漂移后以函数名为准。

---

## 事件一：`/livez` 持续 503、机器人对所有人静默（2026-09-17，未定案）

### 一、症状签名（再次出现时先对这几条）

- 容器**活着**：`/livez` 有响应，日志里每 30 秒一条健康检查 `503`
  （`aiohttp.access`，User-Agent 是 `Python-urllib/3.12`，即 compose 里的健康检查命令）。
- 机器人**对所有人静默**：大区精确 @ 与私聊都不回；日志里不再出现任何 `sender.send`，
  也不再出现任何 `httpx2` 模型调用行。
- **没有别的日志**：没有 `app.stopped`、没有 `app.shutdown_timeout`、没有 `sse.*`、
  没有 `worker.handler_error`、没有 `mcp.*`。
- **进程没有重启**：健康检查节拍严格 30 秒无中断（重启会重置节拍并留下启动日志）。
- 人工重启后一切恢复正常，之后无法复现。

### 二、事实（带证据）

时间轴（UTC）：

| UTC | 事实 |
|---|---|
| 12:13:50.093 | `WARNING raricy.app event=app.model_failed channel_id=lobby error=ModelError`。当晚 20:13（北京）站点用户收到「抱歉，这次的回复没有生成成功。请稍后再试。」（`texts.FAILURE_NOTICE_TEXT`，全仓库只由 `_notify_failure` 发送）——两边对得上，该次模型失败**已被正常处理**（发通知、`mark_handled`）。 |
| ~12:14:5x | zhihu MCP 会话已死（由下一条反推：工具执行时 `provider.available` 为假）。 |
| 12:15:02.573 | `WARNING raricy.mcp.registry event=mcp.tool_failed reason=tool_unavailable feature=zhihu tool=zhihu__zhihu_search server=zhihu`。 |
| 12:15:05.320 | 模型调用 200（`httpx2 HTTP Request: POST .../chat/completions`）。 |
| 12:15:05.441 | `INFO raricy.sender event=sender.send ... kind=reply reason=delivered message_id=11334`（频道为 UUID，即私聊）。**这是机器人最后一次被日志证明的输出。** |
| 12:15:19.959 | 第一条 `/livez` 503；此后每约 30.5 秒一条：12:15:50.484、12:16:20.987、12:16:51.512、12:17:22.012、12:17:52.528、12:18:23.048、12:18:53.561。 |
| 12:18:53 后 | 维护者手动重启容器，现象消失。 |

12:15:05.441 到 12:18:53.561 的 3 分 48 秒里，日志**只有健康检查行**，没有其它任何一行。

`/livez` 503 的判据（`live`，app.py:562-575）——只有以下之一为真才会 503：

1. `_stopped`（已进入停止流程）或 `_started` 为假；
2. 站点 SSE task 为 None 或已 done（`_sse_task`）；
3. **主 worker 池里任意一个 task 已 done**（`WorkerPool.alive` 要求三个全活，
   concurrency=3，core/worker.py:492-495）；
4. 评论功能启用且评论后台 task 有已退出者。

### 三、已排除（不要重复排查）

- **模型失败本身**。/zhihu 的 `ModelError` 有完整处理路径，且 12:13:50 之后机器人还正常
  服务了约 90 秒（12:15:05 那条私聊回复）。两者共享同一段背景（zhihu 会话死亡），
  没有因果。
- **优雅关闭 / 进程崩溃重启**。前者会留 `app.shutdown_timeout`（10 秒后）或 `app.stopped`；
  后者会打断 30 秒健康检查节拍并留下启动日志。两者都没有。
- **「MCP 故障会打挂服务」**。用仓库真实的 `SseMcpProvider` 在本地复现过四种故障形态，
  全部是干净 `Exception`（会被上层兜住）：静默会话下的 `wait_for` 超时 → `TimeoutError`；
  调用中途连接断开 → `MCPError('Connection closed')`；跨任务 `stop()`/`start()` → 正常完成；
  调用进行中 `stop()` → 调用方拿到 `MCPError('Connection closed')`、`stop()` 正常返回。
- **「站点 SSE task 自己会死」**。代码上没找到路径：`SSEReceiver.run()`（site/sse.py:80-111）
  只在 `sse.stop()` 后退出，而 `sse.stop()` 只有 `_shutdown` 调用；全仓库 `task.cancel()`
  的调用点逐个核对过，没有第二个目标指向它。（这只说明「按现在的代码它死不了」，
  不排除某条路径被看漏。）

### 四、待证实的假设（按可能性排序）

**H1（主要）：一个 chat worker 静默死亡，其余组件正常。**

- 后果已用仓库真实类复现（附录 A）：`WorkerPool._run` 只兜 `except Exception`
  （core/worker.py:546），`CancelledError` 是 BaseException，一旦从 handler 逃出，该 worker
  task 以 cancelled 结束——**零日志**（pool 持有 task 引用，连 asyncio 的
  「exception was never retrieved」都不会出现）；`live` 又要求三个 worker 全活，
  于是死一个 = `/livez` 永久 503，而进程继续运行。
- 逃逸路径已构造复现（附录 B）：mcp/session.py:148 用
  `asyncio.wait_for(..., call_timeout)` 包 mcp SDK 调用，而 mcp/session.py:75 又把**同一个
  超时值**传给 SDK 自己的读超时——同一个请求两路取消源。`asyncio.timeout` 的转换条件是
  `uncancel() <= self._cancelling`（CPython `Lib/asyncio/timeouts.py`）：只要超时触发之后、
  `__aexit__` 执行之前落进第二路 `task.cancel()`（anyio 的 cancel scope 就是这样投递的；
  SDK 取消收尾里还有带屏蔽的 await 把窗口拉长），`CancelledError` 就会原样逃出，
  `except TimeoutError` 与 `except Exception` 全部错过。
- 时间吻合：12:15:02 前后 zhihu 会话正被拆除、重连任务刚启动，是取消面最活跃的时刻；
  `call_timeout_seconds` 默认 20 秒，与首条 503 出现在其后 14 秒内一致。
- **它还解释不了「对所有人静默」**，见下方「未闭合之处」。

**H2：「半开」的站点 SSE 连接（静默期内消息根本进不来）。**

- 站点不发送心跳（见 CLAUDE.md），读侧超时 `STREAM_READ_TIMEOUT_SECONDS = 300`。
  TCP 被静默断开（无 FIN）时客户端会一直停在读上最多 5 分钟：区间内收不到任何消息
  （对所有人静默），但 task 没死——**单凭它解释不了 503**，需要与 H1（或评论 task 死亡）叠加。
- 可检验的预测：静默开始 + 约 300 秒处应出现 `sse.connect_failed`（ReadTimeout）+
  `sse.reconnect`，随后恢复。本次因 12:18:53 手动重启，这段日志没有取到。

**H3：部署启用了评论，且某个评论后台 task 退出。**
评论启用时 `/livez` 会多看一项；评论 task 死亡会 503 但不影响聊天。本次部署的
`comments.enabled` 没有确认。

**未闭合之处**：H1 解释 503、H2 解释静默，叠加才完整；而「对所有人静默」也可能只是观感
（静默期内未必有人真的尝试触发；失败通知还有默认 300 秒/（频道,触发者）的冷却）。
这一点没有日志可以证实，只凭维护者回忆。

### 五、复现取证清单（**先做这些，再重启**）

```bash
# 0) 先别重启。抓全量日志（覆盖静默开始前 5 分钟到当下，之后静默期有没有恢复就看它）：
docker compose logs --since "<静默开始前 5 分钟>" --until "<当下>" bot > /tmp/incident-$(date +%s).log

# 1) 进程与健康状态（有没有重启、是不是被 OOM 重启过）：
docker inspect -f '{{.RestartCount}} {{.State.Status}} {{.State.StartedAt}} {{.State.Health.Status}}' <容器>

# 2) 就地抓所有 task 的调用栈（直接看到少了哪个 worker、其余卡在哪）：
docker run --rm -it --pid=container:<容器> --cap-add SYS_PTRACE \
  alpine:3.20 sh -c 'apk add -q py-spy && py-spy dump --pid 1'
```

判定表：

| 观察 | 结论 |
|---|---|
| 栈里少一个 `worker-<n>`（三个里只剩两个） | H1 成立：worker 静默死亡，转第 6 节的第 1、2 条修法 |
| 三个 worker 都在、都停在队列等待 | 死的是别的东西：查评论 task 与站点 SSE 读栈 |
| 私聊发 `/help` **有**回复 | 入站路径（SSE）活着，问题在 worker 侧 |
| 私聊发 `/help` **无**回复 | 入站路径断了——对照 H2，翻静默开始 +300 秒附近的 `sse.*` 日志 |
| 日志出现 `app.unavailable`（403） | 走 D-4 的既有判据，与本事件无关 |

日志侧顺带核对：静默期内有没有任何 `sender.send` / `httpx2` 行（有没有人在被服务）；
有没有 `mcp.*` 行（zhihu 侧重连是否卡死）；`grep -E 'event=(app|sse|comment)\.'` 有无增量。

### 六、本事件暴露出的独立缺陷（与本事件因果未定，但都值得单独修）

> **2026-09-21 补充：**下列第 1、5、6 条已经补上了**观测手段**，第 3 条已按计划**修复**
> （见该条）。第 2、4 条仍未改动。补观测、修一个独立缺陷都不等于故障已解决：本事件的结论
> 仍是**未定案**，不要因为现在多几行日志、少一个独立缺陷就把它当成已定位。新增的取证方式见
> 第九节。

1. **worker 死亡无日志、无兜底**（core/worker.py:546、:492-495）：`except Exception` 兜不住
   BaseException；worker 一死 `/livez` 就永久 503，而现场毫无痕迹。修法：兜 `BaseException`，
   区分「真关停」与「逃逸取消」，对后者记稳定事件并让 worker 继续跑。
   **2026-09-21**：已补上结束观察（`app.task_exit`，`reason=cancelled_escaped` 即逃逸取消），
   worker 本身的行为**未改** —— 它仍会以 cancelled 结束，只是不再零日志。
2. **MCP 调用的双路同值超时**（mcp/session.py:75 与 :148）：外层 `wait_for` 与 SDK 自己的
   读超时同值并存，是取消转换被破坏的窗口。修法：只留一路（去掉外层、依赖 SDK 的
   `MCPError(REQUEST_TIMEOUT)`，或让 SDK 的读超时严格大于外层）。
3. **`tools_unsupported` 永久污染**（core/worker.py:335-342，全仓库无重置点）：第一轮
   （带 tools）遇到任何 400/404 都会把共享模型客户端标记为「不支持 tools」，之后
   `/search` `/zhihu` `/map` `/wolfram` 恒回「暂不可用」直到进程重启。
   「提示词过长」的 400 落在第一轮就会触发。
   **2026-09-21**：已修复永久负缓存。客户端不再持有「不支持 tools」的共享可变标记；普通
   400/404 只终结当前请求并归类 `bad_request`；`tools_unsupported` 只在提供方返回**结构化**
   错误时产生（`core/worker.py::_is_tools_unsupported`：`error.param` 精确为 `tools` /
   `tool_choice` / `parallel_tool_calls`，且 `code` 命中明确的不支持错误码白名单；只认 `code`，
   通用 `type` 不作依据），且只属**本次调用**。
   App 与 `blog/writer.py` 只依据 `ModelError.kind`，不再读模型客户端的共享属性（D-116）。
   对应的离线回归用例在 `tests/test_tools_capability.py`，消费方回归在 `tests/test_app.py` 与
   `tests/test_blog_publish_model.py`（`tests/` 被 Git 忽略，不入库）。
   这是本事件暴露出的**独立缺陷**，改它的依据是一条无关错误不应持续到重启，而不是「已证明
   它造成了本事件」：事件一的因果仍未定案，本条不构成「工具能力污染导致 3 分 48 秒静默」的
   证据。
4. **工具回合第二轮不参与预算**（core/worker.py:263-276）：第二轮消息（含工具结果，
   zhihu/search 单轮上限 5×3000 估算 token）直接拼接发出；叠加 `estimate_tokens` 对非 CJK
   按 4 字符/token 低估（text_utils.py:107-113，XML/URL 实际约 2–3 字符/token）——
   这就是本事件 12:13:50 那条 `ModelError`（「提示词过长」）的成因。
5. **MCP 重连没有「无进展」日志**（mcp/runtime.py:185-240）：`SessionMcpProvider.stop()`
   （mcp/session.py:89-101）没有超时且持有 `self._lock`，拆除一旦挂住，后续每次重连都排队
   等锁，**一条日志都不会有**。本事件里 3 分 48 秒无任何 `mcp.*` 行、也没有任何指向 zhihu 的
   HTTP 行，而健康的重连周期是毫秒级且必留日志——与「卡在拆除」一致。
   **2026-09-21**：已补上阶段耗时观测。connect / discover / close 三个阶段各自登记开始与
   结束时刻，超过 60 秒未结束就记一条 `event=mcp.phase_stalled`（每个阶段只报一次），
   结束若曾停滞则记 `event=mcp.phase_finished`。停滞**只报警、不取消** —— 拆除本身仍可能
   挂住，但现场不再是一片空白。
6. **第三方 logger 静音名单过期**（logging_setup.py:87-94）：名单里是 `httpx`，而
   openai 2.54 与 mcp 2.2 都改用 `httpx2`，其 INFO 行会漏进应用日志。本次正是靠这些行证明
   「zhihu 侧一个 HTTP 都没发出」；如果决定静音它，请同时保留一个针对性的
   「多久没发出过 MCP HTTP」诊断。
   **2026-09-21**：`httpx2` 已加入静音名单（两个包名都列着）。它仍保留 INFO 级输出，
   只有 WARNING 及以上会被换成受限的 `third_party.failure` 事件——上面那句「zhihu 侧
   一个 HTTP 都没发出」的证据形式因此还在。针对性的"多久没发出过 MCP HTTP"诊断仍未实现。
7. 记录性事实：健康检查行末尾 `503 192` 里的 192 不是正文长度——aiohttp 的 `%b` 统计的是
   **含响应头**的整个响应字节数（aiohttp 3.14 源码注释：`Size of response in bytes,
   including HTTP headers`），对应 `web.Response(status=503, text="down")`（正文 4 字节）。
   不要把它当成「响应来自别的服务器」的证据。

### 七、附录：本地复现实验记录（2026-09-17）

- **A. 后果复现（仓库真实 `WorkerPool`）**：handler 抛 `CancelledError` → 该 worker task
  以 cancelled 结束、**零日志**、`alive` 变 False、其余 worker 继续处理后续消息——
  与线上症状完全同形。
- **B. 逃逸复现（构造）**：`asyncio.wait_for(inner(), T)`，`inner` 在取消收尾里还有 await；
  收尾期间投递第二路 `task.cancel()` → 5/5 次 `CancelledError` 原样逃出，不转换成
  `TimeoutError`。对应 CPython 的转换条件 `uncancel() <= self._cancelling`
  （`Lib/asyncio/timeouts.py`）。
- **C. 阴性对照（都是干净的）**：静默会话 + 超时 → `TimeoutError`；调用中连接断开 →
  `MCPError('Connection closed')`；跨任务 `stop()`/`start()` → 正常完成；调用进行中
  `stop()` → 调用方拿到 `MCPError('Connection closed')`，`stop()` 正常返回。

### 八、环境未知项（下次事发前补上）

- 部署配置里 `comments.enabled`、`memory.enabled`、`mcp.call_timeout_seconds` /
  `reconnect_base_seconds` / `reconnect_max_seconds` 的实际取值；
- 镜像构建自哪个 commit（本事件是否包含 2026-09-17 的 `lobby_recent` 提交 `86db8f1`）；
- 容器有没有内存上限（用于彻底排除 OOM，虽然健康检查节拍已基本排除重启）。

### 九、新的取证方式（2026-09-21 起可用）

这一节只记录**怎么看得更清楚**，不改本事件的结论。永久错误归档（
[D-112](../design/DESIGN_DECISIONS.md#d-112)）开启后，WARNING 及以上会落在
`/app/logs/errors` 的分片里，不再随容器重建消失。

```bash
# 巡检分片完整性：末行被写坏会标 DAMAGED 并以非零退出码结束
docker compose exec bot python -m raricy_bot archive verify --directory /app/logs/errors

# 按级别与时间前缀取事件；时间戳是定长 UTC 串，前缀比较就是时间比较
docker compose exec bot python -m raricy_bot archive read \
  --directory /app/logs/errors --level WARNING --since 2026-09-21T00:00:00
```

与本次事件直接相关的几条新线索：

| 观察 | 现在能看到什么 |
|---|---|
| worker 静默死亡（第 6 节第 1 条） | `event=app.task_exit task=worker-<n> reason=cancelled_escaped` —— **没有**取消请求却以 `CancelledError` 收尾，正是事件一的形态；`reason=failed` 时还带安全堆栈（模块、函数、行号） |
| 健康状态翻转 | `event=app.health_changed from_state=up to_state=down reason=<组件> queue_depth=<n> worker_count=<n>`。只在**变化时**记一条，跨过静默开始那一刻就有据可查 |
| MCP 重连停滞（第 6 节第 5 条） | `event=mcp.phase_stalled server=zhihu stage=close duration_ms=<n>` |
| 追一条消息的全过程 | 新增 `trace_id`：路由判定、模型失败与发送结果都带上它。它只由随机数生成，不含 message_id 或用户信息 |
| 归档自身 | `/archivez` 只回计数与布尔；`archive.write_failed` / `archive.recovered` 带 `gap_count`。**缺口补不回来**，它只说明丢了多少条 |

**仍然查不到的**：控制台与归档都不保存消息正文、模型请求/响应与工具参数 —— 这套改动
加的是**诊断信息**，不是现场回放。第 8 节列的三个环境未知项照旧要在下次事发前确认，
归档不会替它们作答。
