# Raricy 站内聊天机器人

以普通 core+ 账号登录 Raricy，用**和真人完全一样的公开接口**收发消息：在大区里只回应精确
`@机器人用户名` 的消息，在私聊里有问必答，回复始终引用触发它的那条消息。它不修改站点，
也不需要站点的任何特殊接口。**它不是真人，也不代表站方立场。**

- 大区是**公开多人对话**：一条回复链上的所有人共享最近十来轮上下文，精确 @ 后按回复链加入。
- 聊天默认**不联网、也不读本地资料**：要联网就发 `/search <问题>`，要查本地资料就发
  `/kb <问题>` —— 每条命令只授权它自己那一轮，下一条普通消息不会被这层授权牵连。
  另有 `/zhihu`、`/map`、`/wolfram` 三条同类命令，默认关闭。
- 上下文只存在内存里，进程重启即清空；去重、额度与回复链归属会保留。

## 在聊天里怎么用它

| 场合 | 怎么触发 |
|---|---|
| 大区 | 精确 `@机器人用户名`。区分大小写，`@机器人名x` 不算叫它 |
| 私聊 | 直接发消息，不用 @ |
| 博客评论（可选） | 首次精确 @，之后直接回复它的评论就能接着聊 |

| 命令 | 作用 |
|---|---|
| `/help` | 能力、隐私与限制说明（本地回复，不调用模型） |
| `/reset` | 开一段新对话，旧的还在 |
| `/search <问题>` | 授权当前这一轮由模型判断是否调用 Exa 联网搜索；最多一次、最多五条摘要 |
| `/zhihu <问题>` | 同上，但查知乎站内内容（需开启；见「可选能力」） |
| `/map <问题>` | 同上，但查高德地图的坐标、地点与天气（需开启） |
| `/wolfram <问题>` | 同上，但交给 Wolfram 做计算或事实查询（需开启） |
| `/kb <问题>` | 授权当前这一轮从本地 Markdown 资料里检索（需开启） |

这五条命令**不能写在同一条消息里**，任意两条叠加都会被本地拒绝。它们都只在私聊和大厅
生效，博客评论区不解析它们，也不调用搜索、地图或知识库。

**关于你的消息**：发给它的内容会被转交给第三方模型服务；`/search`、`/zhihu`、`/map`、
`/wolfram` 的查询在模型决定调用工具时可能转交给对应的上游服务，`/kb` 命中的本地资料片段
**不会**发给任何 MCP 上游。写给站内用户的完整说明（可直接发布到站点）
见 [`docs/usage/USAGE.md`](docs/usage/USAGE.md)。

## 快速开始

需要 Python 3.12+。

```bash
cp config.example.yaml config.yaml
# 至少改三处：site.base_url、model.base_url、model.model

export RARICY_USERNAME=机器人账号
export RARICY_PASSWORD=机器人密码
export LLM_API_KEY=模型服务Key

PYTHONPATH=src python -m raricy_bot --config config.yaml
```

或者安装后运行（不传 `--config` 时依次读 `BOT_CONFIG_PATH`、`./config.yaml`）：

```bash
pip install -e ".[dev]"
python -m raricy_bot
```

跑测试：

```bash
python -m pytest tests -q
```

测试不发起任何真实网络请求：站点层用 `httpx.MockTransport` 伪造，模型层注入假客户端。

## 配置与密钥

配置是只读 YAML，`config.example.yaml` 是一份可直接复制的样例（字段旁都有中文注释）。
顶层小节为 `site` / `model` / `behavior` / `mcp` / `knowledge_base` / `ops` / `storage` /
`logging` / `comments` / `memory` / `blog`，外加必填的 `system_prompt`。
完整字段、默认值与校验规则见 [config.py](src/raricy_bot/config.py)，跨模块约束见
[内部契约 §1](docs/design/INTERFACES.md)。

**密钥永远只从环境变量读取**，不写进 YAML、镜像或日志：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `RARICY_USERNAME` | 是 | 站点登录用户名 |
| `RARICY_PASSWORD` | 是 | 站点登录密码 |
| `LLM_API_KEY` | 是 | 模型服务 API Key |
| `EXA_API_KEY` | 否 | Exa MCP API Key；只在启用 `/search` 时使用 |
| `EXA_API_KEY_1..N` | 否 | 多 Key 池的各槽位 Key，名字由 `account_pool.host_envs` 指定 |
| `AMAP_MAPS_API_KEY` | 否 | 高德 Web 服务 Key；只在启用 `/map` 时使用 |
| `WOLFRAM_APP_ID` | 否 | Wolfram AppID；只在启用 `/wolfram` 时使用 |
| `ZHIHU_ACCESS_SECRET` | 否 | 知乎开放平台访问密钥；只在启用 `/zhihu` 时使用 |
| `BOT_CONFIG_PATH` | 否 | 配置文件路径，缺省 `./config.yaml` |

`site.base_url` 与 `model.base_url` 对非回环地址必须是 https，且不得含 userinfo、
查询串或片段；本机开发用 http 时需要显式打开 `allow_plain_http`（默认关闭，只对回环
地址有意义）。

配置有错时进程在启动阶段以退出码 2 结束，并在 stderr 打印一行原因（不打印取值）。
密钥缺失或为空同样如此；只有四个 MCP 密钥属于软故障，各自只停用它对应的那一个能力。
永久归档已启用却打不开目录时以退出码 3 结束 —— 这是刻意的，继续跑只会让人以为永久
记录正在工作。

## 可选能力（默认全部关闭）

以下能力都默认关闭，默认只跑普通聊天与私聊。不打开就不会有额外的子进程、目录扫描或站点请求。

### 联网搜索 `/search`

在 `config.yaml` 里设 `mcp.enabled: true`，并准备 `EXA_API_KEY`。搜索由构建期固定的
`exa-mcp-server@3.4.1` 提供，每轮最多一次、最多五条摘要，只读摘要不抓网页全文。

多个**已获授权的** Key 可以配成池（`mcp.servers.exa.account_pool`）：每个 Key 一个子进程，
一次搜索在其中做有界轮询与故障转移，对模型仍然只是一个工具。它提高的是可用性，**不是
容量绕过**——上线前必须确认这些 Key 的授权，见「上线前人工检查」。

### 知乎 `/zhihu`、高德 `/map`、Wolfram `/wolfram`

三个同类能力，共用 `mcp.enabled` 这个总开关，各自有 `mcp.features.<name>.enabled`。
它们都由构建期固定版本的 npm 包提供（`@amap/amap-maps-mcp-server@0.0.8`、`wolfram-mcp@1.1.2`），
版本与安装口径只写在 `mcp-tools.package.json` 里（含裁决 SDK 提升冲突的 `overrides`），
知乎是个例外 —— 它只有远程 MCP-over-SSE，没有子进程，所以不进镜像。

```yaml
mcp:
  enabled: true
  features:
    map:
      enabled: true      # 再把 amap 服务器的 env_from 配好，并注入 AMAP_MAPS_API_KEY
```

**这三个在 `config.example.yaml` 里默认 `enabled: false`，启用前必须先取样。** 三个上游的
npm 包都没有 `repository` 字段，工具名与参数可以逐个从 tarball 读出来并已核对，但**结果
格式只能靠一次真调用确认**。所以先跑一次：

```bash
PYTHONPATH=src python tools/capture_mcp_fixture.py \
    --server amap --tool maps_weather --args '{"city":"上海"}' \
    --out tests/fixtures/amap_weather.json
```

脚本只能写进 `tests/fixtures/`，写出的内容先过脱敏（高德的异常正文里带请求 URL，而 URL 里
带 `key=`），也不会回显宿主密钥。拿到样本后按它校准解析器；**知乎拿不到样本就不要发布
`/zhihu`**，解析器的细节与取样步骤见
[`docs/usage/DEPLOYMENT.md`](docs/usage/DEPLOYMENT.md) 的 §4.1.2。

各能力对模型只公开必要的参数，其余由宿主强制或丢弃：`/zhihu` 丢弃 `count`（条数由配置
决定）、`/map` 丢弃 `types`（上游 schema 与 handler 不一致，那个参数实际被忽略）与
`photos`（外部图片地址）、`/wolfram` 把 `mode` 钉死为纯文本。

### 本地知识库 `/kb`

在 `config.yaml` 里开启 `knowledge_base.enabled`，并把 Markdown 目录只读挂载进容器。
一级子目录就是分类，递归读取 `.md`；命中片段只随当前这一轮发给模型，不联网、不落库、
不进历史。没有命中时直接回一句本地提示，不会拿常识冒充资料。

默认只对**私聊 + 白名单用户**开放；要在大厅公开必须显式配置（大厅仍需精确 @）。检索是
标准库实现的词法匹配，不是语义检索；目录改动最长要等 `refresh_seconds` 才能被检索到。

仓库自带的 `knowledge/` 收录站方对外文档（`docs/guide/`）的问答化改写：功能怎么用、
有哪些限额、联机棋类怎么判胜负。它只含用户能看到的事实，源码实现与运维内容一概不收；
取材范围、锁定提交与复核步骤见 [`docs/materials/SITE_DOCS_SOURCE.md`](docs/materials/SITE_DOCS_SOURCE.md)。

### 图片理解

`model.vision_enabled: true` 且模型自身支持视觉时，用户当前这一轮发的图会随这一轮交给模型；
**回复（引用）一条带图的消息时，那张缩略图同样交给模型**（站点只给缩略图，没有原图 id）。
博客区同理，但另外受 `comments.max_images_per_reply`（默认 3）约束——一轮评论回复里，
评论附件、评论正文的 `[@10位]`、文章正文的 `[@10位]` 共用这一个名额池，按这个顺序花。
图片不落库、不进历史；关闭时只处理文本，并明确告诉用户图没读到。

### 博客评论

`comments.enabled: true` 后，机器人每 30 秒轮询一次全站最近评论，每 15 秒检查一次未读通知。
只需精确首次 `@机器人用户名` 或直接回复它的评论即可触发；评论与聊天使用独立队列和配额，
每条成功的评论回复都会真实通知被回复的人。评论轮询故障不影响聊天与健康端点。

## Docker

```bash
export RARICY_USERNAME=你的账号
export RARICY_PASSWORD=你的密码
export LLM_API_KEY=你的模型Key
# 启用 MCP 能力时再设置对应的那个；不要提交，也不要写进 docker-compose.yml
export EXA_API_KEY=你的ExaKey
# export AMAP_MAPS_API_KEY=你的高德Key
# export WOLFRAM_APP_ID=你的WolframAppID
# export ZHIHU_ACCESS_SECRET=你的知乎密钥

docker compose up -d
docker compose logs -f bot
```

几个要点：

- **固定单副本运行。** 去重、水位与配额都是「每个实例各自的内存 + 各自 SQLite」，
  跑两个副本会让同一条消息被回复两遍。
- `config.yaml` 与知识库目录都以**只读**方式挂载（`/app/config.yaml`、`/app/knowledge`）；
  SQLite 只写 `/app/data` 命名卷。密钥由宿主环境变量注入，Compose 文件里只有 `${VAR}` 引用。
- 上游 Node 与三个 stdio MCP 包（Exa、高德、Wolfram）在构建期固定安装，运行期不访问 npm。
- 容器根文件系统只读，运维端口只在容器内 `expose 8080`，不发布到宿主。

首次部署、升级、SELinux、备份与排障的完整步骤见
[`docs/usage/DEPLOYMENT.md`](docs/usage/DEPLOYMENT.md)。

## 运维

| 路径 | 含义 | 通过 | 不通过 |
| --- | --- | --- | --- |
| `/livez` | 进程存活：事件循环在跑，关键后台任务没死 | `200 ok` | `503 down` |
| `/readyz` | 现在能接单：已登录、SSE 已连上、队列没满、未被禁言 | `200 ready` | `503 not ready` |
| `/archivez` | 永久归档是否在落盘；只在归档启用时存在，只回计数与布尔 | `200` + JSON | `503` + JSON，未启用时 `404` |

`/readyz` 会比 `/livez` 更早、更频繁地变成 `503`（站点临时不可达、SSE 断开、队列打满）。
所以**健康检查与自动重启只看 `/livez`**。

归档的磁盘或写入失败刻意**不**影响这两个探针：那会让宿主反复重启一个仍在正常收发消息的
进程。它由 `/archivez` 单独观测。

日志只写结构化白名单字段：消息 id、频道、结果、原因、计数这类状态量。字段名与**取值类型**
一起登记，不合规的字段整条丢弃。正文、查询、Cookie、密码与 API Key 在任何级别都不会落进
日志或数据库；上游异常正文与 MCP 子进程 stderr 的原文也不再进日志，只留失败类别、错误码、
阶段与耗时这类可分类的信息（D-111）。

密钥登记走进程级的 `SecretRegistry`：日志层与出站文本共用同一份凭据表，**一次登记两条通路
同时生效**，凭据轮换后旧值仍然脱敏。

归档里的字段值会再过一次**与控制台完全相同**的密钥替换：类型约束只保证字段形状合法，
不保证内容安全，两条通路不能只有一条脱敏。

控制台日志会被容器轮转淘汰。需要长期保留时打开 `logging.archive`（**默认关闭**）：错误事件
写进独立持久目录的 JSONL，按 UTC 日期与大小分片且**只增不删**。它有自己的只读查询与校验命令、
独立健康检查 `/archivez`，以及容量与备份要求 —— 见
[部署手册 §10.6–§10.7](docs/usage/DEPLOYMENT.md#106-永久错误归档)。

## 上线前人工检查

- [ ] 在站点把机器人账号的资料改为明确标注：这是机器人、**消息可能发送至第三方模型处理**，
      并说明大区是公开多人上下文；启用博客评论前还要写明评论与短文章正文可能发送给第三方模型。
      程序无法代劳这件事。
- [ ] 确认机器人账号已提升为 core+，且登录用户名与 `@机器人用户名` 完全一致。
- [ ] 确认密钥只存在于运行环境，没有写进任何会被提交或打包的文件。
- [ ] **启用 Exa 多 Key 池前**：确认每个 Key 都已获授权（Exa 书面许可，或同一组织依法管理的
      独立预算，或官方 Team / 付费 / 资助额度），并把批准日期、账号范围与渠道记在案。
      池不绕过任何平台额度政策。
- [ ] **启用 `/zhihu`、`/map`、`/wolfram` 前**：先用 `tools/capture_mcp_fixture.py` 取过该
      上游的真实样本，并按样本核对过解析器；知乎拿不到样本就不要发布 `/zhihu`。三个包都
      没有 `repository` 字段，无法证明是厂商官方，所以这一步不能靠「文档看起来对」跳过。
- [ ] **启用知识库前**：逐文件清点挂载目录，确认不含密钥、隐私、内部提示词、部署配置、
      日志、数据库或无权转交第三方模型的材料。目录名与文件名本身也会展示给提问者，
      所以命名同样要审；资料负责人要签字确认可见范围。
- [ ] **启用长期记忆（Beta）前**：确认账号资料的第三方模型披露仍然准确（记忆条目同样属于
      会随请求发送的内容），按 [`docs/usage/DEPLOYMENT.md`](docs/usage/DEPLOYMENT.md) §4.2.2
      的灰度顺序推进；记忆目录按敏感数据管理，不加入 Git、镜像或任何公开制品。

部署后的验证步骤（大区与私聊各自触发、回显不成环、模拟 401/403/429 与队列满载、
重启后去重保留而上下文清空、容器日志与数据库检查）见
[`docs/usage/DEPLOYMENT.md`](docs/usage/DEPLOYMENT.md) 第 8 节；上线前的六条前置确认见同一份文档
第 1 节。

## 已知限制

- **长期记忆是 Beta 功能，默认关闭**：短上下文仍然只在内存里，重启即清空（大区的回复链归属
  保留 7 天）。开启后多一层可选的长期记忆：共同记忆（`all_user` / `lobby`，管理员审批后才生效、
  所有使用者可见其内容）与用户私有记忆（每人一份，只在本人私聊里被参考）。它按接入名单灰度
  （默认 `allowlist`），私有记忆由用户自己在**私聊**里用 `/memory`（`status` / `on` / `off` /
  `auto on` / `auto off` / `list` / `forget <UM-ID>` / `clear`）与 `/remember <内容>` 查看、
  纠正、删除；博客评论区最多只用 `all_user`
  共同记忆，任何场景都不会使用别人的私有记忆。记忆读写失败是软故障，不影响聊天、评论与
  `/livez`、`/readyz`；记忆正文既不进日志也不进 SQLite。用户可见的说明见
  [`docs/usage/USAGE.md`](docs/usage/USAGE.md)，部署与灰度步骤见
  [`docs/usage/DEPLOYMENT.md`](docs/usage/DEPLOYMENT.md) §4.2.2。
- **不主动联网、不能运行代码、查不到站内数据**：别人的余额、鱼干流水、通知、申诉进度，
  都要用户自己去对应页面看。
- **图片与博客正文的边界**：图片默认关闭；聊天里**用户主动引用**的博客会读标题与正文
  （上限 `behavior.quoted_blog_max_chars`，默认 1000 字，超限只给标题），没被引用的不读；
  聊天里**被回复消息的图**只取站点给的缩略图，不追原图；博客评论区只读文章标题和不
  超过 1000 字的正文，图则另受一轮 3 张（可配）的名额约束。
- **正文里的引用语法会被展开**：消息、评论与文章正文里的 `[@<内容ID>]` 都会去取真内容
  ——8 位读云剪贴板正文、9 位读投票的选项与票数、10 位是图床图片（配了视觉才看图）。
  剪贴板与投票接口要求 Core 以上，私有内容对非作者是 403，那时留下「加载失败」占位；
  展开出来的内容只属当前轮，单条上限 `behavior.content_ref_max_chars`（默认 2000）。
- **知识库是词法检索**：没有嵌入模型与向量库，措辞差太远就可能检索不到；没有命中就不回答，
  也不会因此联网。
- **评论发现有一个上游限制的漏失窗口**：靠每 30 秒拉一次「全站最近 100 条」，两次轮询之间
  新增超过 100 条时，窗口外的评论无法恢复；启用功能之前的旧评论也不会补回复。
- **严格来说不是 exactly-once**：上游发送没有幂等键，极端网络故障下只做可恢复的尽力去重，
  宁可丢一句，也不盲目重复发送。

## 项目结构

源码位于 `src/raricy_bot/`；[内部契约](docs/design/INTERFACES.md) 按模块链接具体实现。

| 入口 | 职责 |
|---|---|
| `app.py`、`__main__.py` | 生命周期、装配、模型路径与恢复 |
| `config.py`、`texts.py`、`text_utils.py` | 配置、固定文案、文本判定 |
| `site/` | 登录、HTTP API、DTO 与单条 SSE |
| `core/` | 聊天路由、上下文、模型 worker、发送、图片与内容引用 |
| `store.py`、`quota.py`、`ops.py` | SQLite 状态、聊天额度、健康探针 |
| `comments/` | 评论发现、独立队列、配额与发送 |
| `capabilities.py`、`mcp/` | 能力声明、工具白名单、传输与适配 |
| `kb/` | 本地只读 Markdown 检索 |
| `memory/` | 共同/私有记忆、公开投影、命令与原子写入 |
| `blog/` | 定时发文的稿库/生成、调度、投递与只读对账 |
| `logging_setup.py`、`redact.py` | 白名单日志、取值类型与出站脱敏；凭据登记中心 |
| `error_archive.py` | 永久错误归档：JSONL 分片、只增不删、只读查询与校验 |

`tests/` 为隔离网络的测试，`tools/` 为开发工具；`knowledge/` 和 `drafts/` 是可选输入目录。
完整文档导航见 [docs/README.md](docs/README.md)。

## 容量与清理

运行期每 `storage.cleanup_interval_seconds`（默认 1 小时）清理一次，启动时也清理一次。
聊天、评论和定时发文共用 SQLite；**正文一律不落库**。发文允许保存脱敏后的待发布标题、
指纹和必要元数据，详见 D-107。

| 数据 | 保留 |
| --- | --- |
| 大区链映射 | 7 天（`lobby_thread_retention_seconds`） |
| 已处理事件 | 7 天，且不高于安全水位；非终态事件永不按时间删除 |
| 已发回复记录 | 7 天（其引用的事件未完成时保留） |
| 发送尝试 | 48 小时（`send_attempt_retention_seconds`） |
| 到期冷却 | 立即删除 |
| 已知私聊频道 | 只保留最近活动的 `max_dm_channels`（默认 10000）条 |
| 评论会话映射 | 30 天（`comments.conversation_retention_seconds`），到期同时作废其内存上下文 |
| 评论已发回复、通知与去重行 | 90 天（`comments.dedupe_retention_seconds`）；非终态事件不按时间删除 |
| 评论发送尝试 | 48 小时（与聊天共用 `storage.send_attempt_retention_seconds`） |

主库只设**软上限**（默认 128 MiB）：超过时日志里出现 `app.cleanup_oversize`，**不会**硬截断
——硬截断会让去重、水位或配额写入突然失败，后果比库变大严重得多。需要收缩物理文件时停机
手工 `VACUUM`，步骤见 [`docs/usage/DEPLOYMENT.md`](docs/usage/DEPLOYMENT.md) §10.3。

长期记忆（Beta，默认关闭）**不在 SQLite 里**：条目是 `memory.root_dir`（默认 `./data/memory`，
容器里即 `/app/data/memory`）下的 Markdown 文件，按**敏感数据**管理——备份、恢复与访问控制
见 [`docs/usage/DEPLOYMENT.md`](docs/usage/DEPLOYMENT.md) §4.2.2。

## 文档怎么读

| 想了解 | 读 |
|---|---|
| 全部文档的索引 | [`docs/README.md`](docs/README.md) |
| 怎么部署、升级、排障 | [`docs/usage/DEPLOYMENT.md`](docs/usage/DEPLOYMENT.md) |
| 站内用户会看到什么、怎么跟他们解释 | [`docs/usage/USAGE.md`](docs/usage/USAGE.md) |
| Exa 多 Key 池与本地知识库怎么配、怎么看日志、怎么排障 | [`docs/usage/EXA_POOL_AND_KB.md`](docs/usage/EXA_POOL_AND_KB.md) |
| 模块边界、判定顺序与代码入口 | [`docs/design/INTERFACES.md`](docs/design/INTERFACES.md) |
| 某处行为为什么是这样 | [`docs/design/DESIGN_DECISIONS.md`](docs/design/DESIGN_DECISIONS.md) |
| 上游站点 API 的原始契约 | [`docs/materials/chat-bot.md`](docs/materials/chat-bot.md) |
| 推荐的系统提示词 | [`docs/design/SYSTEM_PROMPTS.md`](docs/design/SYSTEM_PROMPTS.md) |

改代码前按 [INTERFACES.md](docs/design/INTERFACES.md) 定位相关模块与决策；
签名、字段和默认值查源码，改接口时同时检查所有消费者。已完成设计与旧稿见 [归档索引](docs/ARCHIVE.md)。
