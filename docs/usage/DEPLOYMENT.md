# 远程 Linux 服务器部署指南

面向第一次把这台机器人部署到远程 Linux 服务器的操作者。
每一步都给出可直接执行的命令与**预期输出**，凡是容易踩坑的地方都单独标注了原因。

正文以 Ubuntu 22.04 / Debian 12 为例；**Rocky Linux 9（以及 RHEL 系）的差异集中在
[附录 A](#附录-arocky-linux-9-差异)**，用 Rocky 的话请先扫一眼附录 A 的三处硬差异。

代码根目录约定为 `raricy_bot`，下文路径以 `/opt/raricy_bot` 为例。

---

## 1. 先读这一段：部署前必须确认的六件事

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
   `LLM_API_KEY`；启用联网搜索时另需 `EXA_API_KEY`（多 Key 池则是 `EXA_API_KEY_1..N`），
   它们只提供给 Exa MCP 子进程。
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

---

## 2. 服务器前提

- 一台能访问外网的 Linux（下文命令以 Ubuntu 22.04 / Debian 12 为例）。
- 出网可达两个地方：站点域名、模型服务地址。**不需要任何入站端口**——
  机器人只主动外连，运维端点不发布到宿主（见第 7 节）。
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

`docker compose build` 会拉 Python/Node 基础镜像，在构建阶段安装 Python 依赖及固定的
`exa-mcp-server@3.4.1`。运行阶段只使用镜像内的 Node 与 Exa 文件，不执行 `npx`、`npm install`
或访问 npm registry。Compose 同时启用只读根文件系统，运行状态只写入 `/app/data` 命名卷。
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
`minute_attempt_limit: 25`（低于站点 30 次/分的硬限）、`daily_normal_limit: 1950`、
`daily_absolute_limit: 2000`。

### 4.1 聊天区联网搜索（可选）

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
不会解析 `/search`，也不会调用 MCP。

缺少 `EXA_API_KEY`、Node、Exa 进程或 MCP 连接失败时，联网功能会提示暂不可用，但普通聊天、
评论、`/livez` 与 `/readyz` 继续运行。更新环境变量后必须执行 `docker compose up -d`，仅
`restart` 不会重新创建容器并读取新值。

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
EOF
chmod 600 .env
```

> 用 `<<'EOF'`（带引号）是为了让特殊字符原样写入，不被 shell 展开。
> 密码里含 `$`、反引号、反斜杠时尤其重要。值里含 `$`、空格时也可以直接用单引号包住：
> `RARICY_PASSWORD='p@ss$word with space'`。不要加 `export` 前缀。

`.env` 已在 `.gitignore` 里，不会被误提交。

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
- `minute_attempt_limit` 默认 25 是照站点 30 次/分硬限留的余量，**不要往上调**

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
| `docker compose down -v`     | 上面 + 删数据卷     | 删除 | **删除** |

推荐 `down`。**不要用 `down -v`**，它会丢掉去重记录、SSE 水位与当日配额计数，
后果是当天额度从零重算，且可能重复回复已经回过的消息。

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

启用 Exa 搜索时，systemd 主机还需预先安装 Node 22，并在构建/部署阶段固定安装 MCP 包：

```bash
node --version                    # 需为 v22.x
sudo npm install --global --omit=dev --no-audit --no-fund exa-mcp-server@3.4.1
command -v exa-mcp-server         # 应能找到配置中的 stdio 命令
```

这是一次性的部署步骤；服务运行时不执行 `npx`、`npm install`，也不依赖 npm registry。
若不启用 `mcp.enabled`，无需安装 Exa 或 Node。

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
| `2`  | 配置错误（缺必填项、YAML 非法、环境变量缺失或为空） | `docker compose logs` 里会有一行 `配置错误：具体原因`；修 `config.yaml` 或 `.env` |
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
| 大区里普通消息（没 @）没反应                           | 这是设计如此：大区只回应精确 @，避免烧光每日额度                                                                                                                                             |
| 私聊不回                                               | 检查是不是空消息或纯博客（这些只回一次「读不了」提示）；纯图在开了图片输入时会进模型                                                                                                         |
| 一段时间后完全不回                                     | 可能当日额度用尽（1950 条后停止模型回复，2000 条后完全静默），或账号被禁言                                                                                                                   |
| 回复里出现`[redacted]`                               | 输出命中已加载的机密被替换了。若被替换的是**机器人自己的名字**，说明有人把用户名注册成了机密——用户名不是机密，不该被注册                                                             |
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
| "今天的回复额度已经用完…"             | 触及 1950 条                                     | 无独立事件，随 quota 通知                                    |

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
- [ ] `docker compose ps` 显示 `(healthy)`，`RestartCount` 不再增长
- [ ] `/readyz` 返回 `ready`
- [ ] 大区里精确 @ 机器人能得到回复，且回复引用了原消息
- [ ] 私聊机器人能得到回复
- [ ] 只运行了**一个**副本
- [ ] 日志里没有密码、API Key、Cookie 或消息正文
- [ ] 已配置日志体积上限（第 10.2 节）与 `TZ`（第 10.1 节）
- [ ] 已做过一次备份并验证能解开（第 10.5 节）
- [ ] 若启用评论能力：已在测试文章上验证首次 @、直接回复、旁支静默、`/help` 与 `/reset`
- [ ] Rocky 上另见[附录 A](#附录-arocky-linux-9-差异)的补充检查项

---

## 17. 已知限制（不是部署问题，是首版范围）

- 不支持博客理解、通用工具调用和长期用户记忆。聊天区联网搜索是**默认关闭**的可选项，
  仅 `/search <问题>` 触发 Exa 摘要查询；评论区不联网。图片理解是**可选项**：
  `model.vision_enabled` 默认 `false`，开启后也只把当前轮那一张图取回内存交给模型
  （不落库、不写日志、不进历史），且要求模型本身支持视觉。
- 对话上下文只存内存，**进程重启即清空**（去重、大区链归属与配额状态保留）。
- 上游没有发送幂等键，极端网络故障下无法保证严格 exactly-once；
  程序用「查询该频道 `after=<触发消息id>` 的消息、比对 `reply.id`」来对账，最多补发一次。
  评论侧同理：结果不确定时按（机器人作者，父评论）对账，最多重发一次，宁可丢一句。
- 积压超过 100 条时服务端会要求 resync；此期间**首次出现的新私聊**可能无法恢复，
  因为公开接口无法列出机器人尚不知道的私聊频道。
- 评论发现依赖「最近 100 条评论」的滚动窗口，两轮之间溢出会永久漏失；冷启动基线建立前的
  旧评论不会补回复。
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
