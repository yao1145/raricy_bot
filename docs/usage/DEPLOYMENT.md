# 远程 Linux 服务器部署指南

面向第一次把这台机器人部署到远程 Linux 服务器的操作者。
每一步都给出可直接执行的命令与**预期输出**，凡是容易踩坑的地方都单独标注了原因。

正文以 Ubuntu 22.04 / Debian 12 为例；**Rocky Linux 9（以及 RHEL 系）的差异集中在
[附录 A](#附录-arocky-linux-9-差异)**，用 Rocky 的话请先扫一眼附录 A 的三处硬差异。

代码根目录约定为 `raricy_bot`，下文路径以 `/opt/raricy_bot` 为例。

---

## 1. 先读这一段：部署前必须确认的七件事

1. **机器人账号必须先手动创建并提权到 core+。**
   `chat-bot.md` §2.1 明确写了机器人账号**不开放脚本自助注册**，且注册若需要人机验证
   脚本同样无法完成。做法是人工在浏览器里注册一次，再提权。提权两条路：

   - 注册时带一个有效邀请码 → 直接就是 `core`（推荐，全自动）；
   - 或让站长在服务器上执行 `npm run cli -- promote-core <用户名>`。
     不提权的话所有聊天接口一律返回 `403 需要核心用户权限`，机器人在日志里看着像正常，
     实际一条消息也发不出去。
2. **必须人工在站点把机器人资料改成明确标注机器人身份**，并写明「消息可能发送至第三方模型处理」。
   程序无法通过公开接口修改个人资料，这一步只能人做。这是设计文档与社区约定都要求的披露。
3. **只部署一个副本。** 不要 `--scale bot=2`，也不要 `deploy.replicas: 2`。
   去重、水位、配额全部是**每个实例各自的内存 + 各自 SQLite**，两个实例会互相不知道对方
   已经回复过，结果是同一条消息被回复两遍、并双倍消耗站点配额。
   多副本选主不属于首版范围。
4. **密钥只走环境变量**，不要写进 `config.yaml`、不要写进 `docker-compose.yml`、
   不要提交到任何仓库。站点与模型凭据是 `RARICY_USERNAME`、`RARICY_PASSWORD`、
   `LLM_API_KEY`；四个 MCP 能力各自另需一个：`EXA_API_KEY`（多 Key 池则是
   `EXA_API_KEY_1..N`）、`AMAP_MAPS_API_KEY`、`WOLFRAM_APP_ID`、`ZHIHU_ACCESS_SECRET`。
   前三个只提供给对应的 stdio 子进程，最后一个只用于 SSE 的 `Authorization` 头。
   `WOLFRAM_APP_ID` 虽然名字里没有 KEY/TOKEN/SECRET，同样**不得**明文写进 `env:`
   ——它和其它凭据一样被密钥启发式拦截（D-89）。
5. **Exa 多 Key 池必须先确认授权，再上线。** 池的作用是在多个**已获授权**的 Key 之间轮询，
   它不绕过任何平台额度政策。Exa 的服务条款要求 API 使用遵守其技术文档、使用指南与调用量限制，
   官方团队文档也说明同一 Team 的成员共享该 Team 的限制；官方资料并没有承诺「为叠加免费额度
   而创建多个个人账号」是允许的。在拿到下面任一证据之前，**保持 `account_pool` 关闭**：
   - Exa 书面确认本次部署可以轮询这些账号/Key；或
   - 这些 Key 来自同一组织依法管理的独立预算，且当前合同明确允许；或
   - 改用官方 Team、充值、教育/创业额度等官方支持的容量方案。

   确认记录只保存**批准日期、适用账号范围与批准渠道**，不要把邮件正文里的 Key 抄进任何地方。
6. **知识库目录上线前必须人工清点。** 机器人会把命中的片段发给提问者、并发给第三方模型。
   密钥、Cookie、个人隐私、内部提示词、部署配置、日志、数据库、无权转交模型的版权材料，
   都不得放进挂载目录；目录名与文件名本身也会展示给提问者，所以命名同样要审。默认配置只对
   私聊白名单开放，要在大区公开必须显式改配置 —— 见 §4.2.1。
7. **永久归档默认关闭，开启前要先定好容量与备份。** `logging.archive.enabled: true` 之后
   错误事件会长期保留，**不会**自动过期。它不会自己删除旧分片，所以必须先落实宿主配额、
   独立备份目标与负责人（§10.6、§10.7），否则就是把"永久保留"变成"迟早写满磁盘"。
   容器部署已备好独立的 `bot-logs` 卷；注意 `docker compose down -v` 会把它一起删掉。

---

## 2. 服务器前提

- 一台能访问外网的 Linux（下文命令以 Ubuntu 22.04 / Debian 12 为例）。
- 出网可达：站点域名、模型服务地址。启用 MCP 能力时**按需**再加对应的上游：
  `api.exa.ai`（`/search`）、`restapi.amap.com`（`/map`）、`api.wolframalpha.com`
  （`/wolfram`）、`developer.zhihu.com`（`/zhihu`，走 SSE）。不用的能力不必放行。
  **不需要任何入站端口**——机器人只主动外连，运维端点不发布到宿主（见第 7 节）。
- 建议配置：1 核 / 512MB 内存 / 1GB 磁盘足够。SQLite 只存元数据，不存对话正文。
- **宿主机不需要装 Python 3.12 或 Node**：镜像自带 `python:3.12-slim-bookworm` 与 Node 22。
  代价是构建期要能访问 Docker Hub、PyPI 与 npm registry；运行期不访问 npm。

### 2.1 安装 Docker

```bash
# 官方脚本，最省事（Debian/Ubuntu 与 RHEL 系都支持）
curl -fsSL https://get.docker.com | sudo sh

# 把自己加进 docker 组，之后不用每条命令都 sudo（需要重新登录生效）
sudo usermod -aG docker "$USER"

# 验证
docker --version
docker compose version      # 需要 v2，命令是 `docker compose` 而不是 `docker-compose`
```

预期能看到版本号。`docker compose version` 报 `unknown command` 说明装的是只有 v1 的老包，
换成上面前两条重装。

> Rocky / RHEL 系更推荐走 Docker 官方源而不是 `get.docker.com`：系统仓库里没有 docker，
> AppStream 只有 podman。命令见[附录 A](#a2-安装-docker-ce)。
> **不要**装 `podman-docker` 这个 shim 来混用，compose 语义有差异。

### 2.2 构建期网络

`docker compose build` 会拉 Python/Node 基础镜像，在构建阶段安装 Python 依赖及三个固定版本的
MCP 服务器：`exa-mcp-server@3.4.1`、`@amap/amap-maps-mcp-server@0.0.8`、`wolfram-mcp@1.1.2`
（知乎不进镜像，它没有子进程）。运行阶段只使用镜像内的 Node 与这三个包，不执行 `npx`、
`npm install` 或访问 npm registry。Compose 同时启用只读根文件系统，运行状态只写入
`/app/data` 命名卷；三个包在运行期**不写任何文件**（静态核对：只有 wolfram 用了一次只读的
`fs.realpathSync`），所以 `read_only: true` 与它们兼容。

包与版本写在仓库根目录的 `mcp-tools.package.json` 里，Dockerfile 只负责
`npm install` 它。**不要退回 `npm install <包名>` 那种写法**：一条命令装三个包时，amap 精确
钉死的 `@modelcontextprotocol/sdk@1.0.1` 会被提升到顶层，而 wolfram 声明的 `^1.0.1` 恰好被
它满足，于是 wolfram 拿到一份没有 `server/mcp.js` 的 SDK，当场以 `ERR_MODULE_NOT_FOUND`
退出（2026-09-16）。清单里的 `overrides` 为 wolfram 单独钉一份可用的 SDK，同时不动 amap。
换版本要同步改清单、`src/raricy_bot/capabilities.py` 的白名单和 `config.example.yaml` 的注释。

国内网络可能很慢或超时。两个不改逻辑的缓解办法：

- 配 `/etc/docker/daemon.json` 的 `registry-mirrors`（可用镜像站变动频繁，自行确认）；
- 或在 `Dockerfile` 的 `RUN pip install --no-cache-dir .` 后补 `-i <可用的 PyPI 镜像>`。
- 如果 npm registry 访问不稳定，应在 Docker 构建网络层解决；不要把运行时下载改回 `npx`。

---

## 3. 把代码放到服务器

`.gitignore` 排除了 `tests/`、`docs/archive/`、`CLAUDE.md` 与 `config.yaml`/`.env`/`data/`，
同步时要自己把后三项挡在外面。三条路，按推荐顺序：

### 3.1 `tar | ssh`（推荐，两边都不需要额外装东西）

在**本机**（Windows，用 Git Bash）执行：

```bash
cd /d/Study/Code/raricy_bot

tar czf - \
  --exclude='./.git' --exclude='*/__pycache__' --exclude='./.pytest_cache' \
  --exclude='./.pytest-tmp-final' \
  --exclude='./config.yaml' --exclude='./.env' --exclude='./data' \
  . | ssh user@服务器IP 'mkdir -p /opt/raricy_bot && tar xzf - -C /opt/raricy_bot'
```

- **不需要 rsync**。Windows 10+ 自带 `tar`（Git Bash 里是 GNU tar 1.35），
  `ssh` 也是 Git Bash 自带的 OpenSSH；服务器侧只要有 `sshd` 和 `tar`，所有发行版都有。
- `--exclude` 与 rsync 的 `--exclude` 同义。**必须排除 `config.yaml`/`.env`/`data`**：
  否则服务器上已经配好的 `config.yaml` 会被本机的覆盖，本地测试库也会白传上去。
- 整个包约 400 KB（含 docs/ 与 tests/），一次传完，不需要增量。
  `tests/` 会一起过去（`tar` 不受 `.gitignore` 影响），所以服务器上可以直接跑
  `python -m pytest tests`；`Dockerfile` 只 `COPY pyproject.toml` 与 `COPY src`，
  多出来的这些不会进镜像。

**一条要记住的差别**：`tar` 没有 `--delete` 的等价物 —— 本机删掉的文件不会在服务器上消失。
要严格对齐就先删源码目录再解包：

```bash
ssh user@服务器IP 'rm -rf /opt/raricy_bot/src'
```

只删 `src`，不要删根目录（`config.yaml` / `.env` / `data/` 都在那里）。

### 3.2 管道被禁时：先传包再解开

跳板机或受限网络不允许 `ssh` 直接吃 stdin 时，拆成两步：

```bash
tar czf /tmp/raricy_bot.tgz \
  --exclude='./.git' --exclude='*/__pycache__' --exclude='./.pytest_cache' \
  --exclude='./.pytest-tmp-final' \
  --exclude='./config.yaml' --exclude='./.env' --exclude='./data' \
  .

scp /tmp/raricy_bot.tgz user@服务器IP:/tmp/
ssh user@服务器IP 'mkdir -p /opt/raricy_bot && tar xzf /tmp/raricy_bot.tgz -C /opt/raricy_bot && rm /tmp/raricy_bot.tgz'
```

**不要用 `scp -r`**：它没有排除功能，会把本机的 `config.yaml` / `.env` / `data/` 一起推上去，
静默覆盖服务器上已经能用的配置。

### 3.3 走 Git

仓库已配置远端 `origin`（`github.com/yao1145/raricy_bot.git`）；`git push` 之后在服务器上
`git clone` 即可。本机能否连通该远端请自己用 `git ls-remote origin` 确认。

注意 `.gitignore` 让 `tests/` 与 `docs/archive/` **不被跟踪**，克隆下来不会有它们；
`docs/` 的其余部分（含本文件）是入库的。若要在服务器上跑测试，
`tests/` 得另外想办法带过去。

同步完成后：

```bash
cd /opt/raricy_bot
ls
# 预期看到：Dockerfile  docker-compose.yml  pyproject.toml  src  config.example.yaml  README.md ...
```

---

## 4. 写配置文件

```bash
cd /opt/raricy_bot
cp config.example.yaml config.yaml
```

然后按实际情况改 `config.yaml` 里**至少**这三处：

```yaml
site:
  base_url: "https://站点真实域名"        # 必改，不要带结尾斜杠

model:
  base_url: "https://模型服务地址/v1"     # 必改
  model: "模型名"                          # 必改

system_prompt: |
  ...                                      # 建议按站点调性改写，但要保留「机器人身份」这一层
```

其余键保持默认即可。默认值是照设计文档定的：`concurrency: 3`、`queue_size: 50`、
`minute_attempt_limit: 100`（低于站点 120 次/分的硬限）、`daily_normal_limit: 7950`、
`daily_absolute_limit: 8000`。

### 4.1 聊天区 MCP 能力（可选）

`config.example.yaml` 中的 `mcp.enabled` 默认是 `false`。保持默认值时，机器人完全不启动
MCP，普通聊天行为不变。启用 Exa 摘要搜索时，在 `config.yaml` 中打开：

```yaml
mcp:
  enabled: true
```

并在环境中提供 `EXA_API_KEY`。配置里的 `env_from.EXA_API_KEY: EXA_API_KEY` 只表示把宿主
环境变量映射给 Exa 子进程，不是 API Key 的存储位置；不要把真实值写入 YAML、Compose、日志
或仓库。搜索仅在私聊和大厅中由用户显式发送 `/search <问题>` 触发，模型可以判断不搜索；
每轮最多执行一次 `web_search_exa`，最多返回 5 条摘要，每条最多 3000 个估算 token。评论区
不会解析任何能力命令，也不会调用 MCP。

`mcp.enabled` 是总开关；四个能力各自另有 `mcp.features.<name>.enabled`。

| 命令 | feature | 需要的环境变量 | 上游 |
| --- | --- | --- | --- |
| `/search` | `search` | `EXA_API_KEY` | Exa（stdio） |
| `/map` | `map` | `AMAP_MAPS_API_KEY` | 高德（stdio） |
| `/wolfram` | `wolfram` | `WOLFRAM_APP_ID` | Wolfram（stdio） |
| `/zhihu` | `zhihu` | `ZHIHU_ACCESS_SECRET` | 知乎（远程 SSE） |

**三个新能力在 `config.example.yaml` 里默认 `enabled: false`**，启用前先读 §4.1.2。

启动时现在会起三个子进程（原先一个）。`connect_timeout_seconds` 是全局 10 秒且按顺序启动，
所以最坏情况是一台坏服务器给启动加 10 秒；它是可接受的取舍，不是遗漏。

缺少某个能力的密钥、Node、子进程或连接失败时，**只有那一个能力**提示暂不可用，其余能力、
普通聊天、评论、`/livez` 与 `/readyz` 继续运行（三个上游在缺密钥时都是直接 `exit(1)`，
所以「缺 env 就不启动子进程」是必需的，不是优化）。更新环境变量后必须执行
`docker compose up -d`，仅 `restart` 不会重新创建容器并读取新值。

### 4.1.2 上游取样：启用 `zhihu`/`map`/`wolfram` 之前必须先做

三个上游的 npm 包**都没有 `repository` 字段**，无法证明是厂商官方。工具名与参数 schema
可以从 tarball 里逐字读出并已逐个核对（`npm view <pkg> bin` 可复核），但**结果格式只能靠
一次真调用确认**。所以在把 `mcp.features.<name>.enabled` 改成 `true` 之前：

```bash
# 在开发机上，装好三个包的对应版本（与 mcp-tools.package.json 钉的版本一致）。
# 必须装**清单**而不是逐个包名：一条命令装多个包时 npm 的提升顺序会让 wolfram 拿到
# 一份缺 server/mcp.js 的 SDK，取样还没开始子进程就已经退出了（详见 §2.2）。
mkdir -p /tmp/mcp-fixture
cp mcp-tools.package.json /tmp/mcp-fixture/package.json   # 在仓库根目录执行
cd /tmp/mcp-fixture && npm install --omit=dev --no-audit --no-fund
export PATH="/tmp/mcp-fixture/node_modules/.bin:$PATH"

export AMAP_MAPS_API_KEY=...        # 真实取值，不要提交
PYTHONPATH=src python tools/capture_mcp_fixture.py \
    --server amap --tool maps_weather --args '{"city":"上海"}' \
    --out tests/fixtures/amap_weather.json

# 同理：maps_geo、maps_text_search、wolfram_query（宿主会钉死 mode=llm）
# 知乎：--server zhihu --tool zhihu_search --args '{"query":"..."}'
```

脚本只允许写进 `tests/fixtures/`，写出的每个字符串都先过 `Redactor`（高德的异常正文里
带请求 URL，而 URL 里带 `key=`，原样转储就等于把 Key 写进文件），并且只打印变量名，
绝不回显宿主密钥。

拿到样本后按它校准解析器：高德看 `mcp/amap.py` 的 `_ITEMS`，Wolfram 看
`mcp/wolfram.py` 的 `adapt`，**知乎看 `mcp/zhihu.py` 的 `_extract_items`**。知乎最不确定 ——
官方只写 "structured XML"，没有公开标签名，所以它的解析器刻意与标签名无关（剥全部标签与
属性、只留文本、不产出未校验的链接），最坏情况是少给一点文本。**拿不到知乎样本就不发布
`/zhihu`**：把 `capabilities.py` 里的表行与 `IMPLEMENTED_FEATURES` 里的名字一起去掉，
配置校验会挡住任何启用它的尝试（D-81 / D-82）。

**版本漂移**：不要静默升级。要么留在钉住的版本并记录失败，要么当成契约变更处理 ——
重新采集该服务器**全部**样本、diff、更新 `capabilities.py` 的 `allowed_tools`、同步
本文档与 `Dockerfile` 的版本号，并补一条裁决记录。

### 4.1.1 Exa 多 Key 池（可选；上线前先读 §1 第 5 条）

> 本节只给启用步骤。字段含义、槽位状态与冷却、故障转移语义、日志与排障见
> [`EXA_POOL_AND_KB.md`](EXA_POOL_AND_KB.md) 第一部分。

确认授权之后，可以把单 Key 换成池：把 `env_from` 那两行注释掉，改用 `account_pool`。

```yaml
mcp:
  servers:
    exa:
      # env_from:            # 与 account_pool 互斥，二者只能留一个
      #   EXA_API_KEY: EXA_API_KEY
      account_pool:
        child_env: EXA_API_KEY     # 首版必须逐字是这个
        host_envs:                 # 写的是**环境变量名**，不是 Key 值
          - EXA_API_KEY_1
          - EXA_API_KEY_2
          - EXA_API_KEY_3
        strategy: round_robin      # 首版必须逐字是这个
        rate_limit_cooldown_seconds: 60
        transient_cooldown_seconds: 30
        quota_cooldown_seconds: 21600
```

每个槽位是一个独立子进程，池对模型仍然只是一个 `exa__web_search_exa` 工具；环境变量缺失
只禁用对应槽位，全部缺失才使联网搜索不可用，其余功能不受影响。日志只记槽位序号与稳定原因，
不记 Key、也不记环境变量名。

Compose 需要逐个显式透传（`docker-compose.yml` 已给出三行示例）：

```bash
# .env
EXA_API_KEY_1=第一个Key
EXA_API_KEY_2=第二个Key
EXA_API_KEY_3=第三个Key
```

**别把多个 Key 拼成一个命令行参数**（例如传 `EXA_API_KEY="k1,k2"`）：那样它会被
`ps`、日志或错误信息原样打印出来。逐个显式的变量名不会被打印。

上线前应实测确认：多个 Key 是否属于同一个 Team（同 Team 共享预算会让轮换完全没有容量收益），
以及 429 是按 Key、Team、IP 还是网络出口计的（换 Key 可能无效甚至加剧限流）。
池只提供可用性上的有界降级，**不承诺**账户、调用量或免费额度上的任何绕过。

### 4.2 启用博客评论能力（可选）

评论功能默认关闭。只有完成测试文章演练并确认机器人资料披露后，才在配置中开启：

```yaml
comments:
  enabled: true
```

评论服务与聊天使用同一账号、SQLite 和模型客户端，但队列与配额独立。它每 30 秒读取全站
最近 100 条评论、每 15 秒读取最多 5 页未读“评论回复”通知；首次启动只建立基线，不回复
历史评论或通知。两个轮询周期间新增超过 100 条时，窗口外评论可能漏失。仅精确首次
`@机器人用户名` 和直接回复机器人评论会触发，每条成功评论都会真实通知被回复的用户。
正文和不超过 1000 字的文章正文可能送往第三方模型，超过 1000 字时不提供文章正文。

评论完整树由 `comments.max_tree_nodes`（默认 10000）和
`comments.max_response_bytes`（默认 8 MiB）共同限界；超限候选会永久跳过并推进水位，
网络临时错误保留水位重试。通知基线若超过 5 页会持久保存 cutoff，直到尾页清空才完成，
actor 缺失的评论回复通知仍按 unmatched 计数。Service 在模型调用前预留独立 quota，
失败释放、成功只记一次；评论后台 task 异常影响 `/livez`，但不影响聊天 `/readyz`。
评论额度耗尽时该条评论静默跳过，不会公开发布提示。

启用前至少验证：历史 @ 不回复、新 @ 回复、直接回复继续上下文、普通旁支静默、通知延迟、
`/help`、`/reset`、重启后上下文清空但去重状态保留。评论轮询或评论禁言故障不会让聊天
`/readyz` 失败；评论后台 task 意外退出会使 `/livez` 失败，便于编排器重启。

### 4.2.1 启用本地知识库（可选；上线前先读 §1 第 6 条）

> 本节只给启用步骤。目录组织、字段含义、分块与检索行为、日志与排障见
> [`EXA_POOL_AND_KB.md`](EXA_POOL_AND_KB.md) 第二部分。

知识库默认关闭。启用前请先确认你已经按 §1 第 6 条清点过目录内容，并明确它的可见范围。
最小可用配置（只对指定的私聊用户开放）：

```yaml
knowledge_base:
  enabled: true
  root_dir: "./knowledge"        # 容器里解析为 /app/knowledge，即下面的只读挂载点
  access_mode: allowlist         # 默认值；必须显式列出 allowed_user_ids
  allowed_channel_kinds:
    - dm
  allowed_user_ids:
    - "站点上的用户id"            # 稳定 id，不是可改名的用户名
  refresh_seconds: 60
  top_k: 6
  max_context_tokens: 4000        # 必须 <= behavior.context_input_tokens
```

要在大区公开使用（大厅仍需精确 @ 机器人），必须**显式**改成：

```yaml
  access_mode: all_chat
  allowed_channel_kinds:
    - dm
    - lobby
```

目录与挂载：

```bash
mkdir -p /opt/raricy_bot/knowledge/电化学      # 一级目录名就是分类
chmod -R a+rX /opt/raricy_bot/knowledge        # 容器用户 uid 10001 只需可读
```

`docker-compose.yml` 里已经有一行 `./knowledge:/app/knowledge:ro`。**必须是只读挂载**，
也**不要**把资料复制进镜像：镜像层里的旧资料会一直留着，回滚镜像等于回滚资料。
Rocky/RHEL 启用 SELinux 时，按[附录 A](#附录-arocky-linux-9-差异)的方式评估 `:Z` 后缀
（先看现有目录的标签，别直接加，可能打乱已有标签）；容器只读这一点在两种发行版上都一样。

只读**递归**目录下的 `.md` 文件（扩展名大小写无关），一级子目录是分类，根目录直接放置的
文件归入保留分类 `_root`；非法编码、超大文件、隐藏文件与符号链接会被跳过，其余文件仍可检索。
命中片段只随当前这一轮发给第三方模型，不写进历史、SQLite 或日志，也不会发给 Exa；
没有命中时直接回一句本地提示，**不会**调用模型。知识库不可用、无权限或目录被删只影响 `/kb`，
普通聊天、评论、`/livez`、`/readyz` 照常。

> **首次索引在启动阶段同步完成**，之后才启动 `/livez`：目录越大启动越慢，请让
> `max_files` / `max_total_bytes` 与实际资料规模相称（默认 2000 个文件、64 MiB 通常在
> 秒级完成）。停止时若正在重建索引，后台线程会跑完当前一轮，进程退出可能慢于 10 秒的优雅
> 关闭预算 —— 让编排器的 `stop_grace_period` 留出余量即可。

### 4.2.2 启用长期记忆（Beta，可选）

> 这是**灰度功能**，默认关闭，而且建议按下面的顺序一步步开：每一步都能停下来、都能回退。
> 用户能看到什么、能发哪些命令，见 [`USAGE.md`](USAGE.md) 的「命令手册」与「我做不到什么」两节；
> 记忆的存储与日志口径见 `docs/design/INTERFACES.md` §29 … §37，用户主动公开条目（公开个人
> 记忆）的合同见同文件 §39 … §52。

配置全貌（`config.example.yaml` 的 `memory:` 段就是这一份，默认值即「关闭」）：

```yaml
memory:
  enabled: false              # 第一步保持 false
  access_mode: "allowlist"    # allowlist：只有名单内可用；all：所有用户可读共同记忆
  allow_user_list: []         # 第二步保持空
  admin_user_list: []         # 第三步把自己加进去
  root_dir: "./data/memory"   # 容器里即 /app/data/memory，位于 bot-data 卷内
  auto_capture_available: false   # 自动记忆是独立开关，见本节最后一段
  # 公开个人记忆（用户主动公开自己的条目）的三个容量/预算旋钮，没有独立开关：
  # 它跟随 enabled 与 access_mode，用户还要自己在私聊里逐条执行 /memory public。
  max_public_entries_per_user: 8   # 单个用户的公开条目上限，不得大于 max_private_entries_per_user
  max_public_subjects_per_turn: 4  # 一轮最多选入几位用户的公开条目
  public_personal_context_tokens: 600  # 公开个人记忆分组的渲染上限
```

**开启顺序（每一步都验证过再走下一步）**

1. **空跑一次，确认关闭时什么都不做。** 保持 `enabled: false` 启动容器，然后确认
   `docker compose exec bot ls /app/data` 里**没有** `memory/` 目录：关闭时不建目录、不读文件、
   不启后台任务（D-60），因此这一步不会动任何东西。
2. **只打开 `enabled`，名单留空。** 改成 `enabled: true` 重启；服务会创建或加载
   `common.md` 并启动周期刷新。此时 `allow_user_list` 还是空的，任何账号在私聊里发
   `/memory status` 都只会收到一条「还在测试阶段，你的账号还没有使用权限」的固定回复——
   这正是接入门在工作。日志里应能看到 `event=memory.ready`。
3. **把管理员自己加进名单。** `admin_user_list` 里的账号**也必须在** `allow_user_list` 里
   （管理权与接入门是两个条件，同时满足才生效）。改完 `docker compose up -d` 或重启容器。
4. **用管理员账号自测一遍**（全部在私聊里）：
   - `/remember 回答里优先用 Python 3.12 的示例` → 应回一条**带实际保存正文与条目 ID**
     （形如 `UM-000001`）的确认，磁盘上出现 `users/<64 位存储键>.md`；
   - `/memory list` → 看到刚保存的条目；`/memory forget <UM-ID>` → 删掉它；
   - `/memory suggest lobby 大区通用的说明` → `/memory candidates` → `/memory approve <MC-ID>`
     → `/memory list lobby`，再到**大区**里 @ 机器人提问，确认共同记忆进入了回答。
5. **小范围放行。** 把试用账号加进 `allow_user_list`，观察一段时间（条数上限、`full` 提示、
   `/memory list` 的回复都值得看一遍）。名单是**稳定用户 id**，不是可改的用户名。
6. **核对公开个人记忆的三处说明，再观察它的两项开销。** 公开个人记忆没有独立开关：名单里的
   用户随时可以在私聊里用 `/memory public <UM-ID>` 把自己的一条私有条目公开出去，因此放行
   试用账号之前就要把文案看一遍：私聊里发 `/help`（应出现「大区与评论区不会使用任何未公开的
   私有记忆……」那一段）、执行一次 `/memory public` 看重放确认、再到评论区发 `/help` 看那条
   评论披露。上线后盯两样：站点用户查询的限频（机器人按名字核对身份会调
   `GET /api/chat/users`，站点限 20 次/分钟，代码侧自己节流到 15 次/分钟；限频、超时或响应
   形状认不出来时，那一批文本命中直接落空）与公开条目的预算占用（`public_personal_context_tokens`
   是这个分组的渲染上限，装不下的条目整条跳过、不截断正文，后面的条目仍有机会）。
   注意预算取舍**不发日志**：`scope=public` 的
   `memory.context_omitted` 记的是「目录不可读」或「某位用户的公开文件读不出来」，不要把它
   当成预算太紧的告警。
7. **稳定之后再考虑 `access_mode: "all"`。** 这只影响「谁能读共同记忆 / 谁能用记忆命令」，
   不会改变私有记忆的边界（私有记忆在任何模式下都只在本人私聊里读写）。

**公开个人记忆没有迁移，也没有默认公开**

升级不解冻任何旧数据：`public/` 初始为空，所有既有私人条目保持私有，只有用户本人逐条执行
`/memory public <UM-ID>` 才会出现公开投影。因此没有批量迁移、没有「历史授权推断」，也不存在
把某人的旧条目默认为公开的路径。它的读写都在同一个 `root_dir/public/` 下，和私人条目一样是
原子的单文件替换，规则与红线（正文不进日志、不进 SQLite）完全一致。

**单副本约束（Beta 的硬限制）**

**同一份 `root_dir` 只能由一个进程写。** 不要 `--scale bot=2`、不要 `deploy.replicas: 2`，
也不要在两个部署之间共享同一个记忆目录：记忆是「内存快照 + 原子替换整个文件」，两个进程各自
持有自己的快照时，后一次替换会**静默覆盖**另一边的写入。这条与第 1 节第 3 条（整台机器人本来就
只部署一个副本）方向一致，但记忆是**独立的第二条理由**——即使两个副本用各自的数据库也不能共用
记忆目录。公开投影在这条上不比私有记忆宽松：`public/` 与 username 索引同样按「一次引用替换」
发布，两个进程写同一份 `root_dir/public/` 的后果与私有文件一样是静默覆盖。多副本选主不在
Beta 范围内。

**回退（两种方式，都不删数据）**

- `memory.enabled: false`：整段记忆装配被跳过——不读、不写、不建目录，公开目录连扫都不扫，
  不发站点用户查询，Markdown 原样留在磁盘上；
- 或者保持 `enabled: true` 但把 `allow_user_list` 清空（回到 `allowlist` 模式）：谁都用不了，
  已有文件同样保留，将来把名单加回来即可继续。

两种回退都不会删除任何条目，也不会影响聊天与评论。

**回退不等于删除：永久撤回仍然要用户自己执行命令**

`memory.enabled: false` 只让公开条目**不再进入任何模型请求**，`public/` 里的文件、`operations`
记录与私人条目都原样留着，随时可以再打开。要真正把内容撤下来或删掉，只有三条用户侧的命令：

| 想要的结果 | 命令 | 发生了什么 |
|------------|------|------------|
| 撤回某一条的公开副本 | `/memory unpublic <UM-ID>` | 公开投影里不再有它；私人条目不动 |
| 删除一条私人条目（连带撤回它的公开副本） | `/memory forget <UM-ID>` | 先撤回公开副本、再删私人条目 |
| 清空全部私人条目（连带撤回全部公开副本） | `/memory clear` | 同上，一次做完 |

**不要**把关闭功能当成「已经删除」答复用户：磁盘上的文件仍在，重新打开后（只要用户没有执行过
上面三条命令）那些公开条目会再次生效。用户要求删除时，先确认他执行过 `unpublic` / `forget` /
`clear` 中的哪一条。代码级回退（回滚到没有公开个人记忆的版本）同样不需要数据库迁移：摘掉
resolver 与公开 provider 的装配、保留 Markdown 文件即可；重新启用之前要确认 `/help` 与评论
披露已经跟着版本一起回来——功能生效期间不能恢复成「公开场景绝不使用个人记忆」的旧说明。

**备份、恢复与数据保护**

- 记忆文件在 `root_dir`，默认即 `bot-data` 命名卷里的 `/app/data/memory`，所以 §10.5 的卷备份
  **已经包含**它；单独备份时按同样方式停机打包，不要在运行中直接拷（可能拿到原子替换一半的
  中间态）。
- **整个记忆根目录按敏感数据管理**：用户文件里是用户自己交出来的内容，含私有记忆。
  不要把用户私有 Markdown 加进 Git、镜像层或任何公开制品；备份文件也要按敏感数据存放。
- **恢复前先停服务**（`docker compose stop bot`），恢复完成后再启动：刷新任务与一次性写入
  都不该和恢复并发。
- **恢复后先用 codec 离线校验再启动**（文件可能被手工编辑过或被截断）：

  ```bash
  cd /opt/raricy_bot
  docker compose run --rm -v "$PWD/memory-backup:/backup:ro" bot python -c "
  from pathlib import Path
  from raricy_bot.config import load_config
  from raricy_bot.memory.codec import parse_common, parse_private, parse_public
  cfg = load_config('/app/config.yaml').memory
  count = 0
  for path in sorted(Path('/backup').rglob('*.md')):
      data = path.read_bytes()
      # 三份文档三种解析器：公开投影在 public/ 下，用私人解析器读它一定失败。
      if path.name == 'common.md':
          parse_common(data, cfg)
      elif 'public' in path.parts:
          parse_public(data, cfg)
      else:
          parse_private(data, cfg)
      count += 1
  print('校验通过', count, '个文件')
  "
  ```

  解析失败会直接报错并指出文件——先修好或移走它，否则服务会带着「记忆不可用」的状态起来
  （不影响聊天，但那份文件读不出来）。公开文件损坏时只有它那一位 owner 受影响：冷启动时该
  owner 的条目被省略，其余人照常。

**自动提取是独立的灰度项**

`auto_capture_available` 默认 `false`，它**不会**因为 `access_mode` 切到 `all` 而自动打开：
用户还得自己在私聊里执行 `/memory auto on`，两者都满足才会自动整理。要开放时按自己的节奏单独
改这一项。它只把用户**自己写的私聊原文**交给撰写模型，不接收模型回答、搜索结果或知识库片段。

**排障时能看什么**

记忆自己的日志只有 `memory.*` 这几个事件（`ready` / `load_failed` / `refresh_failed` /
`command` / `write_failed` / `updated` / `candidate_updated` / `auto_capture` / `context_omitted`），
字段取自 §37 为记忆**新增**的那七个（`scope`、`revision`、`entry_count`、`memory_id`、
`candidate_id`，以及公开个人记忆另加的 `public_entry_count`、`subject_count`）加上白名单里原有的
通用字段（`status`、`reason`、`error`、`message_id` 等）：**正文、key、用户 id、存储键、
用户名、站点查询词与匹配到的正文、文件路径都不会进日志**（白名单外的字段一律被丢弃）。
所以「某条记忆为什么没生效」只能靠用户自己用 `/memory list` 看，不要指望日志；公开路径同样
只有计数（`public_entry_count` 是某次扫描/操作涉及的公开条目数，`subject_count` 是本轮选中的
用户数），一个名字都不会留下。公开扫描相关的行用 `scope=public` 区分：`memory.ready`、
`memory.load_failed`、`memory.refresh_failed` 与 `memory.context_omitted` 都会带它。

一条值得记住的告警：`memory.auto_capture` 带 `reason=disclosure_no_room`（WARNING）表示
`behavior.max_output_chars` 相对 `memory.max_entry_chars` 太紧——这一轮的记忆**已经写进去了**，
但回答末尾没能带上那句「（已新增私有记忆 ……）」的披露（D-77：回答优先）。用户仍能在私聊里用
`/memory list` 看到并删除它。把 `behavior.max_output_chars` 调大、或把 `memory.max_entry_chars`
调小即可消掉；启动期的校验（`enabled` 且 `auto_capture_available` 时）只保证脱敏增长为零时够用，
所以看到这条日志说明还有脱敏增长那一项在吃空间。

### 4.3 两个不要动的默认值

改了会出问题（原因见第 7 节）：

```yaml
ops:
  port: 8080                 # 容器健康检查硬编码了 8080
storage:
  db_path: "./data/bot.db"   # 容器里解析为 /app/data/bot.db，正是数据卷挂载点
```

改任意一个都必须同步改 `Dockerfile` 与 `docker-compose.yml` 里对应的那一处，
两处文件里都写了这条不变式的注释。

### 4.4 权限：`config.yaml` 与 `.env` 要求不同，不要搞混

|          | `config.yaml`                    | `.env`                   |
| -------- | ---------------------------------- | -------------------------- |
| 谁读它   | 容器里的程序（uid**10001**） | 宿主上的`docker compose` |
| 正确权限 | **644**                      | **600**              |

`config.yaml` 按设计不含任何密钥（密钥全在 `.env`），所以 644 是安全的。
把它设成 600 会让容器用户读不到配置，容器反复重启、退出码 2 ——
这正是第 13 节记录的第一个故障之一。

```bash
chmod 644 config.yaml
```

> 只有走[第 12 节](#12-不用-docker-的部署方式systemd)的 systemd 路径时，
> 配置文件由服务用户自己读，才应该收紧到 600。

### 4.5 改完先验证配置再启动

用临时容器试加载配置，不影响正在跑的那个（首次部署可跳过，直接进第 6 节）：

```bash
docker compose run --rm bot python -c "
from raricy_bot.config import load_config
c = load_config('/app/config.yaml')
print('配置可加载')
print('站点', c.site.base_url)
print('模型', c.model.model, '| timeout', c.model.timeout_seconds, '| max_tokens', c.model.max_output_tokens)
print('日志级别', c.log_level)
print('库路径', c.db_path)
"
```

它只打印非敏感项，配置有问题会直接显示 `配置错误：具体原因`。

**不要跑 `docker compose config`**：它会把 `.env` 替换后完整打印，包括密码与 API Key 明文。

---

## 5. 提供密钥

`docker compose` 会自动读取**当前目录下**名为 `.env` 的文件，用于替换 compose 里的
`${...}`。这是最省事也最不容易泄漏到 shell 历史的方式：

```bash
cd /opt/raricy_bot
cat > .env <<'EOF'
RARICY_USERNAME=机器人用户名
RARICY_PASSWORD=机器人密码
LLM_API_KEY=模型服务的Key
# 仅 mcp.enabled=true 时需要；填写实际值，不要提交此文件
EXA_API_KEY=Exa服务的Key
# 用多 Key 池时改成逐个列出，变量名与 config.yaml 的 account_pool.host_envs 一一对应
# EXA_API_KEY_1=第一个Key
# EXA_API_KEY_2=第二个Key
# EXA_API_KEY_3=第三个Key
# 三个新能力各自一个，只在启用对应能力时填；变量名必须与 config.yaml 里逐字一致
# AMAP_MAPS_API_KEY=高德Web服务Key
# WOLFRAM_APP_ID=WolframAppID
# ZHIHU_ACCESS_SECRET=知乎开放平台访问密钥
EOF
chmod 600 .env
```

> 用 `<<'EOF'`（带引号）是为了让特殊字符原样写入，不被 shell 展开。
> 密码里含 `$`、反引号、反斜杠时尤其重要。值里含 `$`、空格时也可以直接用单引号包住：
> `RARICY_PASSWORD='p@ss$word with space'`。不要加 `export` 前缀。

`.env` 与 `.env.*` 都在 `.gitignore` 与 `.dockerignore` 里，不会被误提交、也不会被带进
构建上下文（`.env.example` 是唯一豁免，且只允许放不含真实值的模板）。Git 的历史与
旧镜像不会因为新增一条忽略规则而被清掉：**忽略规则只防止新的泄漏**。已经提交过或已经
进过镜像层的凭据要按「已泄漏」处理，另行轮换，并检查备份与旧镜像里的副本。

**如果忘了站点或模型密钥**：compose 会把变量替换成空字符串，程序以配置错误退出（退出码 2），
而 `restart: unless-stopped` 会让容器**反复重启**。看到容器不断重启、日志里是
`配置错误：...` 时，先检查 `.env` 是否存在、变量名是否拼对。若只忘了 `EXA_API_KEY`，
不会导致容器退出；仅 `/search` 返回联网暂不可用。

---

## 6. 构建并启动

```bash
cd /opt/raricy_bot

docker compose build         # 首次约 1-3 分钟
docker compose up -d

docker compose ps            # 看 STATUS 列
```

**预期**：`STATUS` 先是 `Up (health: starting)`，约 30 秒后变成 `Up (healthy)`。

`start_period` 给了 30 秒，所以刚起来时的 `starting` 是正常的，不要立刻判定失败。

---

## 7. 网络与端口：为什么服务器上不用开任何入站端口

- 机器人只**主动外连**站点与模型服务，不需要被连接。
- 运维端点（`/livez`、`/readyz`）监听容器内的 `0.0.0.0:8080`，
  但 compose 用的是 `expose`，**不是 `ports`** —— 它只对容器网络可见，宿主上根本没有这个端口。
  这是刻意的（决策 D-10）：运维端点不暴露到公网。
- 因此 **firewalld / ufw 什么都不用配**，运维端点也不会暴露到公网。

要访问它们，从容器内部查：

```bash
docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read())"
# 预期：b'ready'
```

两个端点的**区别很重要**，别混用：

| 端点        | 含义                                                | 谁在用                                   |
| ----------- | --------------------------------------------------- | ---------------------------------------- |
| `/livez`  | 事件循环与关键任务还活着                            | 容器健康检查用它                         |
| `/readyz` | 已登录 + SSE 已连接 + 队列可用 + 不在权限不可用状态 | **判断「现在到底能不能干活」用它** |

容器健康检查特意用 `/livez` 而不是 `/readyz`：站点临时故障时 `/readyz` 会返回 503，
若拿它当健康检查，Docker 会把一个其实很健康的容器反复重启，形成重启风暴。
启用评论能力后，`/livez` 还要求评论后台任务存活；`/readyz` 仍然只反映聊天通路。

---

## 8. 验证部署成功

按顺序做，每步都有明确的预期。

```bash
# 1) 容器健康
docker compose ps
#   预期 STATUS 含 (healthy)

# 2) 就绪状态
docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read())"
#   预期 b'ready'
#   若返回 b'not ready'：说明未登录 / SSE 未连上 / 队列满 / 处于权限不可用状态，
#   去看日志（下一步）分辨是哪一种。

# 3) 日志：应当看到启动与 SSE 连接，且**不含**任何密码、API Key、Cookie、消息正文
docker compose logs --tail=50 bot
#   看到 event=app.started 即启动完成

# 4) 端到端：用你自己的普通账号，在站点大区里发一条
#    @机器人名 你好
#   预期机器人回复，且回复引用了你那条消息。
#   然后私聊机器人发一条「你好」，预期同样回复。

# 5) 确认没有重复回复
#   同一条消息只应有一条回复。若出现两条，说明可能起了多个副本（见第 1 节第 3 条）。
```

### 8.1 启动需要时间，别立刻探活

运维端点是在**登录成功之后**才开始监听 8080 的（`app.py` 的顺序是
`Store.open` → 登录 → 构造 SSE → 工作池 → `OpsServer.start`）。所以容器刚起来的几秒内
访问 `readyz` 会得到 `Connection refused`，这不是故障。

`Connection refused` 与 `503 not ready` 含义不同：前者是端口还没监听（启动中），
后者是端口通了但不能接单（未登录 / SSE 未连上 / 队列满 / 403 不可用）。

### 8.2 确认不是重启循环

```bash
docker inspect -f '{{.RestartCount}}  {{.State.Status}}  {{.State.ExitCode}}' raricy_bot-bot-1
```

`RestartCount` 一直涨就是在重启循环，对照退出码：`2` 配置错误、`1` 运行期致命错误。

---

## 9. 修改配置

两个文件的生效机制**完全不同**：

|              | `config.yaml`                | `.env`                                     |
| ------------ | ------------------------------ | -------------------------------------------- |
| 新值怎么生效 | `docker compose restart bot` | **必须** `docker compose up -d`      |
| 原因         | 程序启动时读一次               | 环境变量在容器创建时固化，`restart` 读不到 |

改完**先按第 4.5 节验证，再重启**，避开重启循环。

### 9.1 可以随便改的

```yaml
model:
  model: "deepseek-flash"
  temperature: 0.4
  timeout_seconds: 20          # 调小会让超时更快暴露，见第 15.2 节
  max_output_tokens: 2000      # 调大避免回复被切断，见第 15.1 节
  vision_enabled: false        # 图片输入，默认关；仅当本模型支持视觉时才开
  max_image_bytes: 5242880     # 单图下载上限（5 MiB），不得超过站点图床的 10 MiB
behavior:
  notice_cooldown_seconds: 60
logging:
  level: "DEBUG"               # 排查期临时开，完了改回 INFO
system_prompt: |
  ...
```

把 `mcp.enabled` 与 `knowledge_base.enabled` 关掉，等于退回「普通聊天 + 评论」，
对应模块的队列、子进程与目录扫描都不会启动。

`/kb` 的检索质量随 `knowledge_base` 的这几个键变化，改完重启即可生效（不需要改代码）：

```yaml
knowledge_base:
  top_k: 6                     # 每轮最多给模型几段资料，上限 10
  chunk_chars: 2400            # 单块字符数；资料偏长可调大，密集短文档可调小
  chunk_overlap_chars: 200     # 必须小于 chunk_chars
  max_context_tokens: 4000     # 必须 <= behavior.context_input_tokens
  refresh_seconds: 60          # 改完文件多久能被检索到
```

### 9.2 图片输入（可选，默认关闭）

```yaml
model:
  vision_enabled: true         # 打开前先确认模型真的支持视觉
  max_image_bytes: 5242880     # 单图上限，1 .. 10485760
```

打开后，用户消息里附带的那张图会由机器人自己从站点取回（`GET /api/images/<id>/raw`，
同源、带会话 Cookie）、按字节判定格式、编码成 base64 data URL，**只随当前这一轮**交给模型。
图片不落 SQLite、不写日志、不写文件，也不进对话历史（历史里留一行 `[图片]` 标记）。

要点：

- **必须确认模型支持视觉。** 配了纯文本模型时，每一条带图的消息都会以 400 失败并回一条
  失败提示。程序**不会**自动降级成纯文本重试 —— 那会把配置错误伪装成偶发故障。
- **关闭时进程不会对图床发出任何请求**，行为与没有这个功能时逐字一致。
- 取图不消耗站点限频（图床接口不限频）；但纯图消息会从「一条本地提示」变成
  「一次模型调用 + 一次回复配额」，这是开启后唯一新增的用量面。
- 图床文件上限是 10 MiB；超过 `max_image_bytes` 的图会按「读不到」处理，回本地提示。
- 不转发 SVG（图床上传白名单里有它，但它是唯一带脚本能力的格式）。
- 日志里只会有 `vision.image_unavailable reason=... size_bytes=...`，**不会有图片 URL**。

### 9.3 引用博客的正文上限（可选，默认 1000 字）

用户在大区或私聊里**引用**一篇博客时，机器人会去读那篇博客的标题与正文，随这一轮
交给第三方模型。正文上限由 `behavior.quoted_blog_max_chars` 控制（默认 1000，
与 `comments.article_max_chars` 的默认值相同但是**两个键**，可各自调）：

```yaml
behavior:
  quoted_blog_max_chars: 1000   # 超过就只给标题，并在正文位置写明原因
```

- 超限、已删除、取不到，机器人都会明确写下「正文没给」，不会让模型以为文章是空的。
- 正文**只属于当前轮**，不进会话历史：调大这个值只会让**引用它的那一轮**变大，
  不会让后续每一轮都被重复外送。
- 想把聊天与评论区调成同一个值，两个键都要写：聊天看 `behavior.quoted_blog_max_chars`，
  评论区看 `comments.article_max_chars`。

### 9.4 改了必须同步改 Dockerfile 与 docker-compose.yml

```yaml
ops:
  port: 8080                   # 两处 healthcheck 硬编码了 8080
storage:
  db_path: "./data/bot.db"     # 容器内解析为 /app/data/bot.db，正是数据卷挂载点
```

### 9.5 两条校验会拦住你

- `daily_normal_limit` 必须**小于** `daily_absolute_limit`
- `minute_attempt_limit` 默认 100 是照站点 120 次/分硬限留的余量（约 1/6），**不要往上调**

### 9.6 编辑 config.yaml 之后（Rocky / SELinux）

若日志又出现 `配置错误：无法读取配置文件`，是编辑器重写文件时把 SELinux 标签带掉了。
重建一次让它重新打标：

```bash
docker compose up -d --force-recreate
```

---

## 10. 日常运维

### 10.1 看日志

```bash
docker compose logs -f --tail=100 bot                          # 实时跟随
docker compose logs --since 10m bot                            # 最近 10 分钟
docker compose logs --tail=500 bot | grep -E ' (WARNING|ERROR) '
docker compose logs --tail=500 bot | grep 'router.route'       # 每条被处理消息的判定
docker compose logs --tail=500 bot | grep 'sender.send'        # 发送结果
docker compose logs bot | grep 'message_id=12345'              # 追一条消息的全过程
```

`--tail` 不加会打印全部历史，大区活跃时上千行，始终带上。日志格式形如：

```
2026-09-12 03:14:22,153 INFO raricy.core.router event=router.route reason=queued channel_id=lobby message_id=12345 channel_kind=lobby event_id=987
```

字段只有白名单里那些（`event`、`reason`、`channel_id`、`message_id`、`kind`、`status`、
`error` 等）。**`reason` 是最有用的一列。** `reason=no_mention` 是大区里所有没 @ 机器人的
消息，排查时先剔掉：

```bash
docker compose logs --since 30m bot | grep -E 'router\.route|sender\.send|app\.' | grep -v 'reason=no_mention'
```

**两个必须知道的点：**

- **时间戳是 UTC**，比北京时间晚 8 小时。想对齐，在 compose 的 `bot:` 下加
  `TZ: Asia/Shanghai` 到 `environment`，再 `docker compose up -d`。
- **日志里永远不会有消息正文**。这是设计红线（`logging_setup.py` 连 openai SDK
  的 DEBUG 都专门压掉了，因为它在 DEBUG 下会打印完整请求体）。你能看到"哪条消息被
  怎么判定了"，但看不到它说了什么。

日志刻意做得极简：只记启动/停止、组件名、错误类别、HTTP 状态和事件 id，
**永远不会**出现 Cookie、密码、API Key、消息正文或模型请求体。
被动事件（输入中、已读、大区里与自己无关的消息）记在 DEBUG 级别，
所以 `INFO` 下看到的一行行是一次真正的动作，不是噪音。

字段的取值也受约束：`error` 只接受异常类名，`stage`/`kind`/`reason` 只接受代码里
定义的短标识，安全堆栈只留「模块.函数:行号」。**子进程的 stderr 原文与上游异常正文
不再进日志**（D-111）——MCP 启动失败会告诉你失败类别与缺哪个模块，而不是把整段
输出贴上来。第三方库的 WARNING 及以上也只留 `event=third_party.failure source=<库名>`。

上面这些行是**控制台**输出，会随容器重建、也会被 json-file 轮转淘汰。需要长期保留
的是下一节的永久归档，两者是平行的两条通路。

### 10.2 限制日志体积

默认的 `json-file` 驱动**不限制**日志大小，长期运行会把磁盘写满。
本仓库的 `docker-compose.yml` 已经带上了这段：

```yaml
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

即**每容器最多约 30 MiB**。改完要 `docker compose up -d` 才生效
（`restart` 只重启容器，不会重建它）。

**裸机（systemd / 直接运行）时应用只写 `stderr`**，不自己滚动文件：文件轮转是宿主的职责。
用 systemd 就跑在 journald 下（由 journald 限体积），否则由 shell 重定向到文件时
自行配 logrotate。应用不与宿主争夺轮转职责。

这一节的限制只针对**控制台**。永久归档是另一套东西：它自己有分片上限但**不删除**
旧分片，因此需要单独的容量与备份安排，见 §10.6。

### 10.3 数据库容量与清理

运行期每小时清理一次（启动时也清理一次），保留规则见根目录
[README 的「容量与清理」](../README.md#容量与清理)。
聊天与评论共用同一个库，评论正文与文章正文都不落库。

- SQLite 主库有 **128 MiB 软上限**（`storage.sqlite_soft_limit_bytes`）：
  超过时日志里出现 `app.cleanup_oversize`，但**不会**被硬截断。
  硬截断会让去重、水位或配额写入突然失败，可能造成重复回复或消息丢失。
- 需要收缩物理文件时**停机**执行（运行期不自动 `VACUUM`，它会长时间独占数据库锁）：

  ```bash
  docker compose stop bot
  docker compose run --rm bot python -c "import sqlite3; c = sqlite3.connect('/app/data/bot.db'); c.execute('VACUUM'); c.close()"
  docker compose start bot
  ```

  执行前先按 §10.5 备份。
- 配置文件本身有 **1 MiB 硬上限**：超过时进程在启动阶段直接以配置错误退出（退出码 2），
  日志里会写明原因。

### 10.4 重启 / 停止

| 命令                           | 做什么              | 容器 | 数据卷         |
| ------------------------------ | ------------------- | ---- | -------------- |
| `docker compose restart bot` | 只重启进程          | 保留 | 保留           |
| `docker compose stop bot`    | 只停进程            | 保留 | 保留           |
| `docker compose down`        | 停进程并删容器/网络 | 删除 | **保留** |
| `docker compose down -v`     | 上面 + 删数据卷     | 删除 | **删除**（数据卷与日志卷一起） |

推荐 `down`。**不要用 `down -v`**，它会丢掉去重记录、SSE 水位与当日配额计数，
后果是当天额度从零重算，且可能重复回复已经回过的消息。启用永久归档后它还会一并
删掉 `bot-logs` 卷 —— 那正是本功能唯一要保住的东西，**禁止把 `down -v` 当日常升级步骤**。

重启后对话记忆会清空（设计上如此，上下文只存内存），但**去重记录与当日配额计数会保留**，
所以重启不会导致重复回复，也不会把当天额度重置。

停机是安全的：程序捕获 `SIGTERM` 并优雅关闭，日志末尾应有
`event=ops.stopped` 与 `event=app.stopped`。写库中途被杀也不丢消息——下次启动时
`mark_orphans_recoverable()` 会把未完成记录标成可认领，水位会把它们补发回来。

`restart: unless-stopped` 的含义是**手动停过之后服务器重启也不会自动拉起**。
想彻底禁止 Docker 开机自启（影响这台机器上所有容器）：`sudo systemctl disable --now docker`。

注意 `docker compose start bot` **不会**重新读取 `.env`。停机期间改过 `.env` 就必须用
`up -d` 而不是 `start`。

### 10.5 备份数据

数据库在命名卷 `bot-data` 里。**在线直接拷文件可能拿到撕裂的副本**（SQLite 处于 WAL 模式），
所以用下面两种方式之一。

方式一，短暂停机后打包（最简单、最不容易出错）：

```bash
cd /opt/raricy_bot
docker compose stop bot
docker run --rm -v raricy_bot_bot-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/bot-data-$(date +%F).tgz -C /data .
docker compose start bot
ls -lh bot-data-*.tgz
```

> 卷名默认是 `<目录名>_bot-data`，即 `/opt/raricy_bot` 对应 `raricy_bot_bot-data`。
> 用 `docker volume ls` 确认真实名字。

方式二，不停机、用 SQLite 自己的在线备份接口（容器内有 Python，无需装 sqlite3 CLI）：

```bash
docker compose exec bot python -c "
import sqlite3
src = sqlite3.connect('/app/data/bot.db')
dst = sqlite3.connect('/app/data/backup.db')
src.backup(dst)
dst.close(); src.close()
print('ok')
"
docker compose cp bot:/app/data/backup.db ./bot-data-$(date +%F).db
docker compose exec bot rm /app/data/backup.db
```

恢复：把备份解回卷里（先 `stop`，恢复完 `start`）。

> 备份里**没有**对话正文——数据库本来就不存正文。但也别把备份随手放到公开的地方，
> 里面含机器人账号的会话状态。

若启用了长期记忆（§4.2.2），卷里还多一个 `/app/data/memory` 目录，上面两种方式都会把它一起
备走。它**含有用户私有内容**，按敏感数据管理；单独备份与恢复的步骤（停服务、先用 codec 离线
校验、不进公开制品）见 §4.2.2。

### 10.6 永久错误归档

控制台日志会被轮转淘汰（§10.2），容器重建后也没了。`logging.archive` 打开的是**另一条
通路**：应用把 WARNING 及以上、以及少量选定的 INFO 事件写成 JSONL，落在独立持久目录，
**分片只增不删**。

```yaml
logging:
  level: INFO  # 控制台级别；不抬高归档的 WARNING 门槛
  archive:
    enabled: false  # 部署验收时显式改为 true
    directory: ./logs/errors  # 相对 config.yaml 解析，容器里即 /app/logs/errors
    segment_max_bytes: 10485760
    fsync_interval_seconds: 5
    disk_warning_free_bytes: 2147483648
```

**目录与卷。** `docker-compose.yml` 已把命名卷 `bot-logs` 挂在 `/app/logs`，Dockerfile 也
建好并授权给 uid 10001，只读根文件系统与非 root 用户都不放宽。`directory` **不得**与
知识库、记忆、稿库或数据库目录重叠：归档只增不减，落在数据树里会挤占它们的空间，
事后也没人能分清哪些文件属于谁。**启用归档时**配置加载会拒绝这种重叠 —— 关闭时不做
这项检查，否则像 `storage.db_path: ./bot.db` 这样的旧配置会因为一个从不写盘的目录而
起不来。

**启用前先做一次离线验收**：在测试目录或测试容器里打开归档，制造几条已知事件，确认
分片生成、`/archivez` 可见、`verify` 通过，再动生产配置。归档已启用却打不开目录时进程
以退出码 3 结束 —— 这是刻意的，继续跑只会让人以为永久记录在工作。

**查询。**

```bash
# 巡检：每个分片一行，末行被写坏会标 DAMAGED 并以非零退出码结束
docker compose exec bot python -m raricy_bot archive verify --directory /app/logs/errors

# 查询：按级别、事件名与时间前缀过滤；--since 用定长 UTC 串的前缀即可
docker compose exec bot python -m raricy_bot archive read \
  --directory /app/logs/errors --level WARNING --since 2026-09-21T00:00:00

# 追一条消息：trace_id 会把路由判定、模型失败与发送结果串起来
docker compose exec bot python -m raricy_bot archive read \
  --directory /app/logs/errors | grep 'trace_id=<值>'
```

`read` 与 `verify` 都是只读的：它们跳过损坏的末行，但**不会**截断或修复原文件。

归档的时间戳**永远是 UTC**（带 `Z` 后缀），不受容器 `TZ` 影响 —— 结构化归档要对齐
跨机器的记录，本地时区的偏移只会让两边的同一时刻看起来不同。控制台仍按 §10.1 的规则。
分片也按 **UTC 日期**滚动：跨过 UTC 零点后的第一条事件会写进新日期的分片，所以
「文件名里的日期」就是内容日期的可靠上界 —— §10.7 的增量备份正是靠这一点选片。

归档里的字段值在写出前会再过一次**与控制台完全相同**的密钥替换：类型约束只保证字段
"形状"合法，不保证内容安全，所以两条通路不能只有一条脱敏。

**健康检查。** `/livez` 与 `/readyz` 的语义没有变，磁盘或日志写入失败不会让它们翻红
—— 否则宿主会反复重启一个仍在正常收发消息的进程。归档有自己的端点：

```bash
docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/archivez').read().decode())"
```

它只回计数与布尔（`written`、`unpersisted`、`disk_low`、`healthy`），不含路径与组件名；
归档未启用时返回 404。把 `unpersisted > 0` 或 `disk_low: true` 接到宿主告警上。

**容量。** `bot-logs` 是独立卷，**但不等于独立磁盘**。长期增长最终会吃掉宿主的同一块盘，
所以要么给日志单独划一个文件系统，要么在宿主上设配额。空间不足时应用只告警
（`archive.disk_low`），**不会**删旧日志腾地方。记录正常流量与故障风暴下的归档增长量，
据此定容量预算。

**写入失败的语义。** 磁盘满、权限变化或 I/O 错误时，归档自己隔离异常并继续服务，
在 stderr 限频报告 `archive.write_failed` 并累计缺口；恢复后记 `archive.recovered`
与缺口计数。**未落盘的事件补不回来** —— 缺口计数说的是"丢了多少条"，不是"会补上"。

### 10.7 备份归档

本地卷只覆盖重启与容器重建；宿主故障还需要第二份副本。

分片在**卷内的 `errors/` 子目录**里（`/app/logs/errors`，卷挂在 `/app/logs`），
所以下面的辅助容器看到的是 `/logs/errors/*.jsonl`。漏掉这一层会得到一条
「复制成功、什么都没拷到」的命令 —— `cp` 对不存在的通配符不报错。

**推荐的每日增量（短暂停机，最不易出错）：**

```bash
cd /opt/raricy_bot
mkdir -p /backup/raricy-archive
docker compose stop bot          # 应用会同步并关闭归档：此刻每个分片都是完整文件
docker run --rm -v raricy_bot_bot-logs:/logs:ro -v /backup/raricy-archive:/backup alpine \
  sh -c 'cp /logs/errors/*.jsonl /backup/ && ls -lt /backup | head -5'
docker compose start bot
```

用 `cp` 而不是 `cp -n`：已关闭的分片不会再变，重复拷同样的字节没有副作用；而
`cp -n` 一旦拷到过一份不完整的文件，之后**永远**不会再更新它。

**不停机的变体（可选）：** 只拷**日期早于今天（UTC）**的分片，然后对备份跑一次
`verify`。跨过 UTC 零点后第一个分片才会封口，所以这个筛选有几分钟的窗口 ——
`verify` 正好能发现它：报 `DAMAGED` 就说明拷到了还在写入的那个，等下一个换片
周期再拷一次即可。当天那份只能靠停机或文件系统一致性快照。

```bash
TODAY=$(date -u +%Y%m%d)
docker run --rm -v raricy_bot_bot-logs:/logs:ro -v /backup/raricy-archive:/backup \
  -e TODAY="$TODAY" alpine \
  sh -c 'cp /logs/errors/*.jsonl /backup/ 2>/dev/null; rm -f /backup/errors-$TODAY-*; ls -lt /backup | head -5'
python -m raricy_bot archive verify --directory /backup/raricy-archive
```

备份内容校验与恢复：

```bash
# 校验：对备份目录跑同一套巡检；DAMAGED 会让退出码非零，可直接接告警
python -m raricy_bot archive verify --directory /backup/raricy-archive

# 恢复：拷回卷里（先 stop），再 verify 一遍确认
docker compose stop bot
docker run --rm -v raricy_bot_bot-logs:/logs -v /backup/raricy-archive:/backup alpine \
  sh -c 'mkdir -p /logs/errors && cp /backup/*.jsonl /logs/errors/'
docker compose start bot
python -m raricy_bot archive verify --directory /backup/raricy-archive
```

还原本地后也可以直接在容器里核对一次（`archive read` 是只读的，不会动文件）：

```bash
docker compose exec bot python -m raricy_bot archive read \
  --directory /app/logs/errors --limit 5
```

**备份不自动过期**：不要给它配保留策略，那与「永久保留」直接冲突。至少每月做一次抽样
恢复，并记录上次成功时间；超过 24 小时没有成功备份就要告警。**实际恢复点取决于最近一次
成功的备份** —— 磁盘损毁会丢掉那之后的记录，这是这套方案接受的上限。

> 下面两项属于上线时必须落实的运维信息，不能由代码或文档代替。**未填之前不得声称灾备已完成。**
>
> | 项目 | 取值 |
> |---|---|
> | 备份目标（独立介质或已有受控备份系统） | 待填 |
> | 备份负责人 | 待填 |
> | 宿主容量/告警接收方式 | 待填 |

**回退。** 需要回退时先正常关闭并同步归档，**保留日志卷与备份**。回退后旧程序会忽略
新字段，必须显式提示"已不再归档"—— 不能因为容器健康就误判永久保留仍在工作。可以关闭
`logging.archive` 或回退归档实现，**不可回退已经修复的密钥保护**。

JSONL 每行带 `schema_version`，读工具兼容已有版本。历史日志不会因为新增这个功能而自动
恢复；不批量迁移可能含密钥的旧日志，也不在未获授权时删除历史证据。

---

## 11. 升级

```bash
cd /opt/raricy_bot

# 传入新代码（沿用第 3 节的方式）
# 注意：config.yaml 是你的本地配置，不要被覆盖

docker compose build
docker compose up -d
docker compose ps
```

数据卷不动，去重、水位与配额状态全部保留。若新版本改了配置项，
对照 `config.example.yaml` 补上新增的键（缺键会用默认值，未知键会被忽略，所以旧配置通常仍可启动）。

---

## 12. 不用 Docker 的部署方式（systemd）

只有在服务器不方便装 Docker 时才用这条路。

启用任一 stdio MCP 能力时，systemd 主机还需预先安装 Node 22，并在构建/部署阶段固定安装
对应的 MCP 包。**用仓库里的 `mcp-tools.package.json` 装，不要逐个 `npm install -g`**
（理由见 §2.2：逐个装时谁被提升取决于安装顺序，先装 amap 会让 wolfram 拿到一份没有
`server/mcp.js` 的 SDK 并当场退出）：

```bash
node --version                    # 需为 v22.x
sudo mkdir -p /opt/mcp-tools
sudo cp mcp-tools.package.json /opt/mcp-tools/package.json
cd /opt/mcp-tools && sudo npm install --omit=dev --no-audit --no-fund
ls /opt/mcp-tools/node_modules/.bin/   # 应有 exa-mcp-server、mcp-amap、wolfram-mcp
```

只启用的部分不必删：`config.yaml` 里没配的服务器根本不会被启动。三个命令通过
`node_modules/.bin` 提供，所以要把它加进服务进程的 `PATH`（下面的单元文件已加）：
命令名必须与 `config.yaml` 里的 `command` 保持一致。

> 升级包时先改 `mcp-tools.package.json`（连同 `overrides`），再重跑上面的 `npm install`。
> 换版本还要按 §4.1.2 重新取上游样本，并同步 `src/raricy_bot/capabilities.py` 的白名单与
> `config.example.yaml` 的注释。

知乎**不需要**这一步：它只有远程 MCP-over-SSE，没有子进程。

这是一次性的部署步骤；服务运行时不执行 `npx`、`npm install`，也不依赖 npm registry。
若不启用 `mcp.enabled`，无需安装任何 MCP 包或 Node。

```bash
# 1) Python 3.12+（Ubuntu 22.04 自带 3.10，需要另装）
python3 --version        # 需 >= 3.12

# 2) 建目录与专用用户
sudo useradd -r -s /usr/sbin/nologin -d /opt/raricy_bot raricybot
sudo mkdir -p /opt/raricy_bot /var/lib/raricy_bot
sudo chown -R raricybot:raricybot /opt/raricy_bot /var/lib/raricy_bot

# 3) 传代码到 /opt/raricy_bot，然后建虚拟环境并安装
cd /opt/raricy_bot
sudo -u raricybot python3 -m venv .venv
sudo -u raricybot .venv/bin/pip install .
```

注意此时 `storage.db_path` 要改成一个**绝对路径**（非容器环境下 `./data/bot.db`
是相对当前工作目录解析的，容易落到意料之外的地方）：

```yaml
storage:
  db_path: "/var/lib/raricy_bot/bot.db"
ops:
  host: "127.0.0.1"     # 非容器环境建议只听本地，不要听 0.0.0.0
```

此时配置文件由服务用户自己读，权限应收紧到 **600**（与容器路径下的 644 不同）：

```bash
sudo chown raricybot:raricybot /opt/raricy_bot/config.yaml
sudo chmod 600 /opt/raricy_bot/config.yaml
```

密钥放文件，权限收紧：

```bash
sudo install -m 600 -o raricybot -g raricybot /dev/null /etc/raricy-bot.env
sudo tee /etc/raricy-bot.env >/dev/null <<'EOF'
RARICY_USERNAME=机器人用户名
RARICY_PASSWORD=机器人密码
LLM_API_KEY=模型服务的Key
# 仅 mcp.enabled=true 时需要；填写实际值，不要提交此文件
EXA_API_KEY=Exa服务的Key
# 用多 Key 池时改成逐个列出，变量名与 config.yaml 的 account_pool.host_envs 一一对应
# EXA_API_KEY_1=第一个Key
# EXA_API_KEY_2=第二个Key
# EXA_API_KEY_3=第三个Key
# 三个新能力各自一个，只在启用对应能力时填；变量名必须与 config.yaml 里逐字一致
# AMAP_MAPS_API_KEY=高德Web服务Key
# WOLFRAM_APP_ID=WolframAppID
# ZHIHU_ACCESS_SECRET=知乎开放平台访问密钥
EOF
sudo chmod 600 /etc/raricy-bot.env
```

systemd 单元 `/etc/systemd/system/raricy-bot.service`：

```ini
[Unit]
Description=Raricy 站内聊天机器人
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=raricybot
Group=raricybot
WorkingDirectory=/opt/raricy_bot
EnvironmentFile=/etc/raricy-bot.env
Environment=BOT_CONFIG_PATH=/opt/raricy_bot/config.yaml
# 仅 mcp.enabled=true 时需要：stdio MCP 命令由这里的 .bin 提供（见本节开头）
Environment=PATH=/opt/mcp-tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/raricy_bot/.venv/bin/python -m raricy_bot
Restart=always
RestartSec=5
# 收紧权限：只需要写数据目录
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/raricy_bot

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now raricy-bot
systemctl status raricy-bot
journalctl -u raricy-bot -f          # 跟日志
```

对齐容器语义：`Restart=always` 对应 `restart: unless-stopped`；
`SIGTERM` 会被程序捕获并优雅关闭（停止 SSE、工作池、运维端点、客户端、数据库）。

---

## 13. 排错速查

先看日志，再看退出码。`__main__.py` 的退出码是有含义的：

| 退出码 | 含义                                                | 怎么办                                                                                    |
| ------ | --------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `2`  | 配置错误（缺必填项、YAML 非法、环境变量缺失或为空）；或装配接缝错位（配置启用了某子域、入口没有注入对应实现） | `docker compose logs` 里会有一行 `配置错误：具体原因`；装配错位是一行 `装配错误：类别码`（如 `blog_service_factory_required`），属打包/入口问题，不是用户配置 |
| `3`  | 归档已启用却打不开（目录不可写、分片建不出来）      | 日志里有一行 `归档启动失败：具体原因`；修目录权限或 `logging.archive.directory`。**这是刻意致命**：继续跑只会让人以为永久记录在工作 |
| `4`  | 数据目录不可用或已有写者（公共数据档案锁） | 日志里有一行 `数据目录不可用：data_in_use`；确认没有另一个完整版/Light 进程在用同一份数据。锁随进程退出自动释放，不需要删除任何文件 |
| `1`  | 运行期致命错误                                      | 日志里只有异常**类型名**（不泄露取值），据此定位                                    |
| `0`  | 正常停止（收到 SIGTERM/SIGINT）                     | 正常                                                                                      |

| 现象                                                   | 原因与处理                                                                                                                                                                                   |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 容器不断重启                                           | 多半是环境变量没给上（退出码 2），或`config.yaml` 没挂载。检查 `.env` 与卷挂载                                                                                                           |
| `配置错误：无法读取配置文件：/app/config.yaml`       | 两个原因会给出**完全相同**的日志，都要查：(1) `config.yaml` 被 `chmod 600`，容器用户 uid 10001 读不到 → 改回 644；(2) SELinux 标签没打（Rocky）→ 挂载加 `,Z`。两者常常同时存在 |
| `docker compose exec` 报 `container is restarting` | 容器在重启循环，先修文件再重建，别指望 exec 进去                                                                                                                                             |
| `readyz` 刚启动时 `Connection refused`             | 正常。运维端点在登录成功之后才监听，等 30 秒                                                                                                                                                 |
| `/readyz` 返回 `not ready`                         | 看日志分辨：登录失败 / SSE 未连上 / 队列满 / 权限不可用                                                                                                                                      |
| 日志里`403 需要核心用户权限`                         | 账号还是`user`，没提权到 core+，见第 1 节第 1 条                                                                                                                                           |
| 日志里出现 CSRF 相关错误                               | 说明客户端错发了`Origin`/`Referer`。程序本身**不会**设置这两个头，若出现说明代码被改动过。它不会被当作权限问题去反复探测，只记一条 error                                           |
| `docker compose version` 报 unknown command          | 装的是老 v1，补装`docker-compose-plugin`                                                                                                                                                   |
| 大区里 @ 机器人没反应                                  | 确认是**区分大小写的精确** @ `机器人用户名`，且用户名两侧不是字母/数字/`_`/`-`。`@机器人名x` 不算命中                                                                          |
| 大区里普通消息（没 @）没反应                           | 触发上这是设计如此：大区只回应精确 @，避免烧光每日额度。但这些消息**会**进内存里的近期上下文（最近 50 条、每条前 500 字），并在下一次有人 @ 机器人时随该轮外送                                                                                                 |
| 私聊不回                                               | 检查是不是空消息或纯博客（这些只回一次「读不了」提示）；纯图在开了图片输入时会进模型                                                                                                         |
| 一段时间后完全不回                                     | 可能当日额度用尽（7950 条后停止模型回复，8000 条后完全静默），或账号被禁言                                                                                                                   |
| 机器人在线但对所有人不回，且 `/livez` 持续 503         | 未定案的线上现象，见 `usage/INCIDENTS.md` 事件一。特征：每 30 秒一条健康检查 503、日志再无 `sender.send`/`httpx2`/`app.*` 行、健康检查节拍没有中断（没重启过）。**先按事件一的取证清单抓现场，再重启**——重启会销毁现场 |
| 回复里出现`[redacted]`                               | 输出命中已加载的机密被替换了。若被替换的是**机器人自己的名字**，说明有人把用户名注册成了机密——用户名不是机密，不该被注册                                                             |
| 某个能力（`/search`、`/map`、`/wolfram`、`/zhihu`）恒回「暂不可用」 | 先 `docker compose logs bot \| grep 'event=mcp\.'` 看那台服务器那一行，一行就够定位：`provider_disabled reason=missing_env` = 密钥没给上（改 `.env` 后必须 `docker compose up -d`，`restart` 不会重新读值）；`provider_start_failed` 后面的 `error=`、`code=`、`stage=` 与 `category=` 是子进程给出的退出原因——`category=module_missing` 说明镜像里的依赖树不对（见 §2.2），不是账号问题。**子进程 stderr 的原文不再进日志**（D-111），需要更细的现场时按 §4.1.2 在开发机上复刻取样 |
| 日志里出现 `event=mcp.phase_stalled` | 某个 MCP 阶段（连接/发现/调用/关闭）超过 60 秒没结束。它**只报警、不取消**：可能有副作用的调用为了日志被取消会制造重复副作用。随后出现 `phase_finished` 说明它自己走完了 |
| 日志里出现 `event=app.task_exit reason=cancelled_escaped` | **没有**任何取消请求，某个后台任务却以 `CancelledError` 收尾。这正是 `usage/INCIDENTS.md` 事件一记录过的形态——先按那份取证清单抓现场 |
| 日志里出现 `event=archive.write_failed` | 归档写不进去（磁盘满、权限变化、I/O 错误）。服务本身不受影响，但**未落盘的事件补不回来**：`gap_count` 是"丢了多少条"。恢复后会有 `event=archive.recovered` |
| 日志报`429`                                          | 站点限频。程序会遵循`Retry-After`，没有该头则等 60 秒并退避                                                                                                                                |
| 日志只有一行行`router.route reason=no_mention`       | 正常噪音，剔掉再看：`grep -v 'reason=no_mention'`                                                                                                                                          |

### 13.1 诊断配置读取问题

`无法读取配置文件` 是 `_read_yaml` 收到 `OSError`，需分辨三种原因：

```bash
cd /opt/raricy_bot

docker compose exec bot ls -l /app/config.yaml       # 以容器内 bot 用户的视角
docker compose exec bot head -c 20 /app/config.yaml
ls -lZ config.yaml                                   # 宿主侧权限与 SELinux 上下文
grep -n 'config.yaml' docker-compose.yml             # 挂载参数是否带了 Z
getenforce
```

| 现象                                               | 结论                                                |
| -------------------------------------------------- | --------------------------------------------------- |
| `No such file or directory`                      | 没挂上，查 compose 的 volumes 与`BOT_CONFIG_PATH` |
| `ls -l` 正常但 `head` 报 `Permission denied` | 权限或 SELinux                                      |
| `ls -lZ` 是 `-rw-------` 且 owner 不是 10001   | 权限问题                                            |
| `ls -lZ` 上下文不是 `container_file_t`         | SELinux 问题                                        |

---

## 14. 为什么有些消息不回复（判定链路速查）

"不回复"在代码里对应**至少十种互不相同的机制**，先分清是**完全静默**还是
**回了一句固定话**。

默认 `logging.level: "INFO"` 会把下面第一张表里大部分原因**故意降到 DEBUG**，
所以不打开 DEBUG 就看不到真正的原因：

```bash
sed -i 's/^  level: "INFO"/  level: "DEBUG"/' config.yaml
docker compose restart bot
# 复现一条"不回复"的消息，然后：
docker compose logs --tail=200 bot | grep -E 'router\.route|sender\.send|app\.'
```

排查完记得改回 `"INFO"`——DEBUG 在大区活跃时每个 SSE 帧都会记一行。

### 14.1 完全静默

| 触发条件                                       | 代码位置      | 日志判据                                                         |
| ---------------------------------------------- | ------------- | ---------------------------------------------------------------- |
| 大区消息**没有精确 @**                   | `router.py` | `reason=no_mention`（DEBUG）                                   |
| 账号 403（没提权/被禁言）→ 全站静默           | `app.py`    | ERROR`app.unavailable`，之后每 300 秒一行 `app.probe_failed` |
| 当日额度用尽                                   | `quota.py`  | WARNING`sender.send reason=quota`；2000 用尽后连这行也没有     |
| 自己的消息 / 已删除 / 拍一拍                   | `router.py` | `reason=self_message` / `deleted` / `pat`                  |
| SSE 重连重放被去重                             | `router.py` | `reason=duplicate`                                             |
| 主动提示被冷却吞掉（按 (频道, 触发者) 5 分钟） | `app.py`    | 无日志，直接 return                                              |

**@ 的精确性**：区分大小写，且用户名两侧不能是字母/数字/`_`/`-`
（`text_utils.py`）。`@Bot` 与 `@bot` 是两个东西，`@机器人名x` 不算命中。

### 14.2 回了一句固定话

| 固定话                                 | 触发条件                                         | 判据                                                         |
| -------------------------------------- | ------------------------------------------------ | ------------------------------------------------------------ |
| "我不能提供系统提示、密钥或内部配置。" | 命中`SECRET_PROBE_PATTERNS`                    | `reason=secret_probe`                                      |
| "这条消息太长了…"                     | 正文超过`max_input_chars`（8000 字符）         | `reason=too_long`                                          |
| "这张图片我没能读取…"                 | 有图但读不到：未开图片输入 / 图已失效 / 取图失败 | `reason=media_only`（纯图）或 `vision.image_unavailable` |
| "我暂时不能查看博客内容…"             | 只有博客、没有可读图片                           | `reason=media_only`                                        |
| "我没有看到要处理的内容…"             | 空白 / 只 @ 了机器人                             | `reason=empty`                                             |
| "当前排队较多…"                       | 队列满（默认 50）                                | `reason=queue_full`                                        |
| "抱歉，这次的回复没有生成成功。"       | 模型调用最终失败（超时不重试，D-19）             | `app.model_failed`                                         |
| "今天的回复额度已经用完…"             | 触及 7950 条                                     | 无独立事件，随 quota 通知                                    |

**`SECRET_PROBE_PATTERNS` 有误伤**（`text_utils.py`）：里面是 `token`、`config`、
`env`、`密钥`、`口令`、`配置文件` 这类词，且是**子串匹配 + 大小写不敏感**。
所以在技术社区里，"这个 token 怎么申请"、"我的 config 写错了"、"env 变量怎么设"
这类正常提问会被直接回绝，不进模型。

---

## 15. 三个已知行为（不是故障）

### 15.1 回复被截断

有**两层**截断，只有一层会留痕迹：

| 层     | 参数                                | 触发点                 | 有无提示                                                                      |
| ------ | ----------------------------------- | ---------------------- | ----------------------------------------------------------------------------- |
| 模型侧 | `model.max_output_tokens: 600`    | `worker.py` 传给 API | **无**。`finish_reason` 全项目只在测试夹具里出现过，`src/` 从不读它 |
| 本地侧 | `behavior.max_output_chars: 5000` | `sender.py`          | 有，追加`（内容过长，已截断）`                                              |

中文大致 1 token ≈ 1 字，600 token 会在 600 字左右就切断，而本地那层要超过 5000 字才触发。
**所以在默认配置下你看到的截断都是模型侧无标记的那种**——回答到一半戛然而止，没有任何说明。

截断**不会**导致不回复：`truncate_at_paragraph` 截断后必定附带后缀，内容不可能为空
（`text_utils.py`），照常发出、照常计配额、日志是正常的 `sender.send reason=delivered`。

治法是调大 `model.max_output_tokens`。

### 15.2 超时与失败提示要等多久、谁会收到

超时**不重试**（D-19）：`model.timeout_seconds`（默认 45 秒）一旦超时即判失败，
用户在**最长约 45 秒**后收到"抱歉，这次的回复没有生成成功。"。
此前是「超时后无间隔重试一次」，最坏静默约 90 秒，且本进程的 worker 被占满同样长的时间，
队列跟着堆积（并发上限 `behavior.concurrency: 3`，同一会话严格串行；队列 50 满回 busy 提示）。

主动提示（`failure` / `busy` / `quota` 三类）的冷却按 **(频道, 触发者)** 计，
时长 `notice_cooldown_seconds`（默认 300 秒，D-18）：

| 场景                              | 用户看到                     |
| --------------------------------- | ---------------------------- |
| 大区里某人超时/失败/排队/额度用尽 | 该用户收到对应提示           |
| 同一人 5 分钟内再次触发           | 不再发（冷却中）             |
| 大区里的其他人                    | **不受影响**，各收各的 |
| 私聊                              | 各自独立                     |

此前按「每频道 24 小时一条」计，而大区是全站唯一频道，于是全天只有第一个触发的人收得到，
其余人完全静默。那是个缺陷，已修（D-18）。

提示不是免费的：三类提示共同消耗 2000 总量的剩余预算。额度用尽后，剩下的预算会被
"今天的回复额度已经用完"逐条吃掉，吃完即彻底静默（2000 的语义未变）。

### 15.3 站点侧慢

`site.request_timeout_seconds: 20`。发消息 POST 超时会被映射成 `SiteError(0, ...)`
（`client.py`），进对账分支（`sender.py`）：查到自己的回复就记 `deduped`，
查不到就**重发一次**。但若对账查询本身也超时，结果是 `reason=failed`，
而 `app.py` 只对 `reason == "quota"` 做处理——这是唯一一条"站点慢 → 彻底静默"的路径。

---

## 16. 部署完成检查清单

- [ ] 机器人账号已在站点注册并提权到 **core+**
- [ ] 机器人资料已人工标注「机器人」与「消息可能发送至第三方模型处理」
- [ ] `config.yaml` 里 `site.base_url`、`model.base_url`、`model.model` 已改成真实值
- [ ] `config.yaml` 权限是 **644**，`.env` 权限是 **600**
- [ ] `.env` 存在且 `chmod 600`，站点与模型三个必需密钥都非空，变量名未改
- [ ] 若启用 `mcp.enabled`：`EXA_API_KEY` 已注入，且 `/search` 能完成一次摘要搜索；未启用时可保持为空
- [ ] 若要启用 `zhihu`/`map`/`wolfram`：已按 §4.1.2 用 `tools/capture_mcp_fixture.py` 取过真实样本、
      按样本校准过解析器、并确认对应密钥已注入；只启用其中一部分时其余保持 `enabled: false`
- [ ] `docker compose ps` 显示 `(healthy)`，`RestartCount` 不再增长
- [ ] `/readyz` 返回 `ready`
- [ ] 大区里精确 @ 机器人能得到回复，且回复引用了原消息
- [ ] 私聊机器人能得到回复
- [ ] 只运行了**一个**副本
- [ ] 日志里没有密码、API Key、Cookie 或消息正文
- [ ] 已配置日志体积上限（第 10.2 节）与 `TZ`（第 10.1 节）
- [ ] 已做过一次备份并验证能解开（第 10.5 节）
- [ ] 若要启用永久归档：已在测试目录验证过开启、分片生成、`archive verify` 与 `/archivez`，
      并填好 §10.7 的备份目标与负责人；宿主容量与告警也已落实（**未落实不得声称灾备完成**）
- [ ] 若启用评论能力：已在测试文章上验证首次 @、直接回复、旁支静默、`/help` 与 `/reset`
- [ ] 若启用长期记忆（Beta）：按 §4.2.2 走完「空跑确认不建目录 → 名单留空验证接入门 →
      管理员自测 `/remember` 与共同候选 → 小范围放行」，核对过 `/help`、公开确认与评论披露，
      并演练过一次回退（回退**不等于删除**：永久撤回仍要用户自己执行 `unpublic` / `forget` /
      `clear`）
- [ ] Rocky 上另见[附录 A](#附录-arocky-linux-9-差异)的补充检查项

---

## 17. 已知限制（不是部署问题，是首版范围）

- 不支持博客理解与通用工具调用。**长期记忆是默认关闭的 Beta 可选项**（灰度步骤、单副本约束、
  回退与备份见 §4.2.2）：开启后也只保存被模型整理成**条目**的内容，不是「记住整段对话」，
  且不改变短上下文的规则（仍然只在内存、重启即空）。同一开关下还有**用户主动公开的个人记忆**
  （§4.2.2）：用户逐条公开自己的条目之后，大区与评论里出现或精确提到他时，那几条才可能随
  当轮请求发给模型；未公开的私有条目永远不进公开请求，关掉 `memory.enabled` 则连公开目录都不读。
  聊天区 MCP 能力是**默认关闭**的可选项，
  四条命令各只授权自己那一轮：`/search` 查 Exa、`/zhihu` 查知乎、`/map` 查高德、
  `/wolfram` 算 Wolfram；一条消息最多带一个能力命令，叠加会被本地拒绝。
  其中 `zhihu`/`map`/`wolfram` 三个在示例配置里默认关闭，启用前必须先取样校准（§4.1.2）。
  评论区不联网。图片理解是**可选项**：
  `model.vision_enabled` 默认 `false`，开启后也只把当前轮那一张图取回内存交给模型
  （不落库、不写日志、不进历史），且要求模型本身支持视觉。
- 对话上下文只存内存，**进程重启即清空**（去重、大区链归属与配额状态保留）。
  大区的近期公开消息同样只在内存里（最近 50 条、每条前 500 字），进程重启即失，
  并且只在下一次唤起时用一次；它不进 SQLite、不进日志，也不会单独触发模型调用。
  这条上下文按 `behavior.context_input_tokens`（默认 8000）选最新的一段连续后缀，
  装不下就一条都不给 —— 它永远挤不掉 system 与本轮正文。**没有单独的开关**：
  机器人一旦监听大区，未被 @ 的公开文字消息就会进这份内存上下文。不接受这一点，
  就不要把机器人放进大区（账号资料里的披露见 `USAGE.md` 第一部分）。
- 上游没有发送幂等键，极端网络故障下无法保证严格 exactly-once；
  程序用「查询该频道 `after=<触发消息id>` 的消息、比对 `reply.id`」来对账，最多补发一次。
  评论侧同理：结果不确定时按（机器人作者，父评论）对账，最多重发一次，宁可丢一句。
- 积压超过 100 条时服务端会要求 resync；此期间**首次出现的新私聊**可能无法恢复，
  因为公开接口无法列出机器人尚不知道的私聊频道。
- 评论发现依赖「最近 100 条评论」的滚动窗口，两轮之间溢出会永久漏失；冷启动基线建立前的
  旧评论不会补回复。
- **「永久保留」不是有限磁盘上的无限容量承诺。** 归档不设自动到期、不覆盖旧分片，因此
  磁盘告警、扩容、备份与恢复验证属于完整交付条件；备份目标与负责人未落实前不能算灾备完成。
  它也不保证断电、磁盘损坏、强制杀进程或写入失败时零丢失 —— 写入失败只累计缺口计数，
  未落盘的事件补不回来。
- **配置解析成功、归档初始化完成之前**的启动错误只能安全写 stderr，仍依赖宿主保存；
  OS/OOM/断电等进程外故障同样依赖宿主监控。不能宣称所有启动失败都已入应用归档。
- 归档只保留**诊断事件**：不保存聊天正文、模型请求/响应、工具参数与结果。它解决的是
  「出故障时能定位」，不是「事后能还原现场」。
- **Docker 产物未在本机构建验证过**（开发机没有 Docker）。本文的命令按标准用法写成，
  但首次在服务器上执行时请留意 `docker compose build` 阶段的输出，
  若有报错请以实际输出为准。

---

## 附录 A：Rocky Linux 9 差异

Rocky Linux 9（及 RHEL 系）与 Ubuntu/Debian 有三处硬差异，另附若干便利信息。

### A.1 三处硬差异

1. **SELinux 默认 Enforcing**。`./config.yaml:/app/config.yaml:ro` 这个 bind mount 在 Rocky 上
   会因标签不对而读不到文件，容器反复重启、退出码 2。必须加 `Z`（见 A.3）。
   这是 Rocky 与 Ubuntu 最大的一处不同。
2. **系统仓库里没有 docker**，AppStream 只有 podman。必须加 Docker 官方源（见 A.2）。
3. **宿主机不需要 Python 3.12 或 Node 22**。镜像自带 Python 与 Node；代价是构建期要能访问
   Docker Hub、PyPI 与 npm registry。运行中的容器不访问 npm。

另外两点在 Rocky 上是好消息：

- 只用了 `expose` 而不是 `ports`，**firewalld 什么都不用配**，运维端点也不会暴露到公网。
- 最小安装不带 `vim`，但自带 `vi`（来自 `vim-minimal`）。想用完整版：
  `sudo dnf install -y vim-enhanced`。

### A.2 安装 Docker CE

```bash
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker

# 建好组再加人（需要重新登录生效）
sudo usermod -aG docker "$USER"

docker --version
docker compose version      # 必须是 v2，命令写作 `docker compose`
```

`docker compose version` 报 `unknown command` 说明装的是老 v1，补装 `docker-compose-plugin`。
构建期网络慢的缓解办法见第 2.2 节。

### A.3 SELinux：给配置挂载加 `Z`

先确认状态：

```bash
getenforce      # Enforcing 就必须做本节
```

编辑 `docker-compose.yml`，把配置挂载那一行：

```yaml
      - ./config.yaml:/app/config.yaml:ro
```

改成：

```yaml
      - ./config.yaml:/app/config.yaml:ro,Z
```

`Z` 让 Docker 把该宿主机文件重新打上仅本容器可用的 SELinux 标签。

数据卷 `bot-data:/app/data` **不用改**——命名卷由 Docker 自己管理标签与属主，
这一点与 bind mount 不同。命名卷首次创建时会继承镜像里 `/app/data` 的属主（10001），
所以非 root 运行也不会写不进去。若你把 data 改成了 bind mount，就必须手工
`sudo chown -R 10001:10001 <宿主目录>`。

```bash
sed -i 's#-#- ./config.yaml:/app/config.yaml:ro$#-#- ./config.yaml:/app/config.yaml:ro,Z#' docker-compose.yml
```

改完配置后若报 `无法读取配置文件`，是编辑器重写文件带掉了标签，按第 9.6 节重建容器。

### A.4 Rocky 上的补充检查项

在第 16 节清单之外另需确认：

- [ ] `config.yaml` 权限是 **644**（不是 600），容器用户 uid 10001 读得到
- [ ] compose 的配置挂载带了 **`,Z`**，`getenforce` 为 Enforcing 时尤其必须
- [ ] `docker compose version` 是 v2
