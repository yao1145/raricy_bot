# Raricy 站内聊天机器人

以普通 core+ 账号登录 Raricy，用**和真人完全一样的公开接口**收发消息：在大区里只回应精确
`@机器人用户名` 的消息，在私聊里有问必答，回复始终引用触发它的那条消息。它不修改站点，
也不需要站点的任何特殊接口。**它不是真人，也不代表站方立场。**

- 大区是**公开多人对话**：一条回复链上的所有人共享最近十来轮上下文，回复链内的消息就能加入。
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
`logging` / `comments`，外加必填的 `system_prompt`。完整字段、默认值与校验规则见
[`docs/design/INTERFACES.md`](docs/design/INTERFACES.md) 第 1 节。

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

配置有错时进程在启动阶段以退出码 2 结束，并在 stderr 打印一行原因（不打印取值）。
密钥缺失或为空同样如此；只有四个 MCP 密钥属于软故障，各自只停用它对应的那一个能力。

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

`/readyz` 会比 `/livez` 更早、更频繁地变成 `503`（站点临时不可达、SSE 断开、队列打满）。
所以**健康检查与自动重启只看 `/livez`**。

日志只写结构化白名单字段：消息 id、频道、结果、原因、计数这类状态量。正文、查询、
Cookie、密码与 API Key 在任何级别都不会落进日志或数据库。

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

```
raricy_bot/
├── src/raricy_bot/
│   ├── __main__.py               # 命令行入口：`python -m raricy_bot`
│   ├── app.py                    # 应用装配：配置、站点客户端、SSE、路由、工作器、发送器与运维端点
│   ├── config.py                 # YAML + 环境变量配置加载与校验
│   ├── logging_setup.py          # 结构化日志与脱敏过滤器
│   ├── redact.py                 # 密钥脱敏：日志与出站错误写出前统一替换
│   ├── text_utils.py             # 用户名字符、@ 提及、token 估算、段落截断与命令判定
│   ├── texts.py                  # 全部对外文案
│   ├── capabilities.py           # 能力表：命令字面量、上游工具白名单、各能力文案
│   ├── store.py                  # SQLite 运行状态（正文与密钥不落库）
│   ├── quota.py                  # 聊天配额：每分钟窗口 + 24 小时额度 + 通知冷却
│   ├── ops.py                    # 内建运维端点 `/livez` 与 `/readyz`
│   ├── site/                     # 站点 HTTP 客户端与 SSE 长连接
│   │   ├── client.py             # 登录、探活、读消息、发消息、开流
│   │   ├── models.py             # 站点 DTO 与 SSE 事件模型
│   │   ├── comment_models.py     # 博客评论接口 DTO 与受限解析
│   │   └── sse.py                # SSE 帧解析、自愈重连、断线补齐
│   ├── core/                     # 聊天主链路：路由、上下文、工作器与发送
│   │   ├── router.py             # 判定「入队 / 本地回复 / 忽略」
│   │   ├── context.py            # 按会话的内存历史（INTERFACES §11、§33）
│   │   ├── lobby_context.py      # 大区近期消息缓冲（§38）
│   │   ├── worker.py             # 模型客户端与工作器池
│   │   ├── sender.py             # 脱敏、截断、配额预留、发送与对账
│   │   ├── blog.py               # 被引用博客：状态判定、取正文与拼块
│   │   ├── content_refs.py       # 内容引用 `[@<内容ID>]` 的识别与替换（§25）
│   │   └── vision.py             # 图片输入：取回、格式判定与编码（§20）
│   ├── mcp/                      # MCP 能力：两种传输、工具注册与四个上游适配
│   │   ├── session.py            # 单个 ClientSession 的 Provider 骨架
│   │   ├── stdio.py              # stdio 传输 Provider
│   │   ├── sse.py                # 远程 MCP-over-SSE Provider
│   │   ├── runtime.py            # Provider 生命周期、软故障隔离与后台重连
│   │   ├── registry.py           # 工具发现、命名空间与 feature 白名单
│   │   ├── contracts.py          # 与模型工具循环共享的领域合同
│   │   ├── adapters.py           # 按 feature 装配适配器
│   │   ├── adapter_kit.py        # 适配器公共件：限流、文本块读取、URL 校验
│   │   ├── exa.py                # Exa 搜索结果适配
│   │   ├── pool.py               # Exa 授权密钥池：多个 stdio Provider 合成一个
│   │   ├── zhihu.py              # 知乎检索适配：白名单 + 结构化 XML 的保守提取
│   │   ├── amap.py               # 高德地图适配：参数白名单与结果清洗
│   │   └── wolfram.py            # Wolfram 适配：宿主钉死 `mode`，只放行 query
│   ├── kb/                       # 本地 Markdown 知识库：载入、索引与检索
│   │   ├── loader.py             # 扫描、读取与分块（§23.3 / §23.4）
│   │   ├── index.py              # 纯标准库的 BM25 风格词法索引（§23.5）
│   │   ├── service.py            # 生命周期、原子快照、周期刷新与查询（§23.6 / §23.7）
│   │   └── models.py             # 不可变数据类型
│   ├── comments/                 # 博客评论机器人：发现、匹配、队列与发送
│   │   ├── service.py            # 生命周期、独立队列与后台轮询
│   │   ├── discovery.py          # 评论区两个轮询发现器
│   │   ├── matcher.py            # 评论树索引与触发匹配
│   │   ├── router.py             # 博客评论候选路由
│   │   ├── quota.py              # 独立配额与节流
│   │   ├── sender.py             # 博客评论发送链
│   │   └── models.py             # 评论子系统的内存请求类型
│   └── memory/                   # 长期记忆（Beta）：Markdown 存储、AI 撰写与接入策略
│       ├── service.py            # 存储服务：快照、原子写、幂等与刷新（§30）
│       ├── codec.py              # 记忆文件与公开投影的解析与渲染（§29、§41）
│       ├── models.py             # 长期记忆的数据模型（§27）
│       ├── access.py             # Beta 接入策略与共同记忆管理权（§28）
│       ├── writer.py             # AI 撰写器：来源内容整理成一条候选记忆（§31）
│       ├── commands.py           # 命令类型与解析（§32.1 / §32.2）
│       ├── controller.py         # 命令编排：门禁、幂等、撰写、写入与文案（§32.3）
│       └── subjects.py           # 公开个人记忆的 subject 解析（§43）
├── tests/                        # pytest + pytest-asyncio 测试
├── docs/                         # 文档：materials（上游契约）/ usage（操作手册）/ design（内部契约）/ archive（历史）
├── tools/                        # 仅开发用的脚本：上游取样 `capture_mcp_fixture.py`
├── knowledge/                    # 本地知识库的 Markdown 源文件
├── config.example.yaml           # 配置示例，字段旁都有中文注释
├── mcp-tools.package.json        # 构建期安装的三个 stdio MCP 服务器及版本
├── pyproject.toml                # 依赖、打包与 pytest 配置
├── Dockerfile
└── docker-compose.yml
```

## 容量与清理

运行期每 `storage.cleanup_interval_seconds`（默认 1 小时）清理一次，启动时也清理一次。
聊天与评论共用同一个 SQLite 库；**正文一律不落库**，表里只有 id、状态、归属与计数。

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
| 模块之间锁定的接口与判定顺序 | [`docs/design/INTERFACES.md`](docs/design/INTERFACES.md) |
| 某处行为为什么是这样 | [`docs/design/DESIGN_DECISIONS.md`](docs/design/DESIGN_DECISIONS.md) |
| 上游站点 API 的原始契约 | [`docs/materials/chat-bot.md`](docs/materials/chat-bot.md) |
| 推荐的系统提示词 | [`docs/design/SYSTEM_PROMPTS.md`](docs/design/SYSTEM_PROMPTS.md) |

改代码前请先读 `INTERFACES.md`：它是本仓库唯一锁定的内部合同，改签名要同时检查所有消费者。
