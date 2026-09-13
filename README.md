# Raricy 站内聊天机器人

一个独立运行的站内通用助手：以普通 core+ 账号登录 Raricy，订阅 `/api/chat/stream`，
精确响应大区里 `@机器人用户名` 的消息与全部私聊文本，调用 OpenAI 兼容的模型服务生成回复。
大区采用**公开多人共享上下文**：一条回复链上的所有合格消息共享最近若干轮对话，
回复链内消息即可加入；私聊行为不变。

第一版明确不支持图片理解、博客理解、工具调用、联网搜索与长期用户记忆。
机器人资料须由人工在站点上标注「机器人」及「消息可能发送至第三方模型处理」。

## 博客评论机器人（默认关闭）

评论能力必须在 `config.yaml` 中显式开启：

```yaml
comments:
  enabled: true
```

开启后服务每 30 秒检查全站最近 100 条评论、每 15 秒检查最多 5 页未读“评论回复”通知。
首次启动只建立冷启动基线，不会补回复旧评论或旧通知；若冷启动通知超过 5 页，服务会持久
保存 cutoff 并继续清理旧页，不会过早完成基线。最近列表在两个轮询周期之间溢出 100 条时，
窗口外评论可能永久漏失。只有精确首次 `@机器人用户名` 或直接回复机器人评论
才会触发，评论回复会真实通知被回复的用户。评论正文和短文章正文可能发送给第三方模型，
文章正文超过 1000 字时不会发送。文章评论使用独立队列、配额和状态记录，不占用聊天队列；
关闭 `comments.enabled` 后聊天行为不变。

启用前必须人工确认机器人资料已披露上述第三方处理、公开评论通知、短期记忆与重启失忆，
并在测试文章上验证首次 @、直接回复、旁支静默、`/help` 和 `/reset`。详见
[`docs/COMMENT_BOT_DESIGN.md`](docs/COMMENT_BOT_DESIGN.md) 与 [`docs/comment-bot.md`](docs/comment-bot.md)。

## 目录结构

```
raricy_bot/
├── src/raricy_bot/
│   ├── config.py            # YAML + 环境变量配置加载与校验
│   ├── logging_setup.py     # 结构化日志与脱敏过滤器
│   ├── redact.py            # 密钥脱敏
│   ├── text_utils.py        # @ 解析、token 估算、截断、本地规则
│   ├── texts.py             # 全部对外文案
│   ├── store.py             # SQLite 运行状态（正文/密钥不落库）
│   ├── quota.py             # 每分钟窗口 + 24 小时额度 + 通知冷却
│   ├── site/                # 站点 HTTP 客户端、SSE 接收器、DTO
│   ├── core/                # 上下文、路由器、工作器池、发送器
│   ├── ops.py               # /livez 与 /readyz
│   ├── app.py               # 组件装配与生命周期
│   └── __main__.py          # python -m raricy_bot 入口
├── tests/                   # pytest + pytest-asyncio 测试
├── config.example.yaml      # 配置示例
├── Dockerfile
└── docker-compose.yml
```

## 本地运行

先准备配置与密钥：

```bash
cp config.example.yaml config.yaml
# 按需修改 config.yaml 中的 site.base_url、model.base_url、model.model 等
export RARICY_USERNAME=你的账号
export RARICY_PASSWORD=你的密码
export LLM_API_KEY=你的模型Key
```

方式一，直接从源码运行（无需安装）：

```bash
PYTHONPATH=src python -m raricy_bot --config config.example.yaml
```

方式二，可编辑安装后运行：

```bash
pip install -e ".[dev]"
python -m raricy_bot --config config.yaml
```

不传 `--config` 时，程序先读环境变量 `BOT_CONFIG_PATH`，再退回到 `./config.yaml`。

## 运行测试

```bash
pip install -e ".[dev]"
python -m pytest tests -q
```

测试不发起任何真实网络请求：站点层用 `httpx.MockTransport` 伪造，模型层注入假客户端。

## 配置项说明

配置为只读 YAML，顶层小节有 `site` / `model` / `behavior` / `ops` / `storage` / `logging` /
`comments` 与必填的 `system_prompt`。`comments.max_response_bytes` 默认 8 MiB、
`comments.max_tree_nodes` 默认 10000，分别由 SiteClient 的响应流和显式栈解析执行；完整字段、
默认值与校验规则见 `docs/INTERFACES.md` 第 1 节。
`config.example.yaml` 是一份可直接复制的样例。

## 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `RARICY_USERNAME` | 是 | 站点登录用户名 |
| `RARICY_PASSWORD` | 是 | 站点登录密码 |
| `LLM_API_KEY` | 是 | 模型服务 API Key |
| `BOT_CONFIG_PATH` | 否 | 配置文件路径，缺省为 `./config.yaml` |

密钥只从环境变量读取，不写入 YAML、镜像或日志。缺失或为空时程序以退出码 2 结束，
并只在 stderr 打印缺失的变量名，不打印任何取值。

## Docker 部署

```bash
export RARICY_USERNAME=你的账号
export RARICY_PASSWORD=你的密码
export LLM_API_KEY=你的模型Key
docker compose up -d
docker compose logs -f bot
```

要点：

- 固定单副本运行，`restart: unless-stopped`。
- `config.yaml` 以只读方式挂载到 `/app/config.yaml`，容器内 `BOT_CONFIG_PATH` 指向它；
  `storage.db_path` 保持默认的 `./data/bot.db` 即可。
- 数据目录不变量：容器 `WORKDIR` 为 `/app`，默认 `./data/bot.db` 解析为
  `/app/data/bot.db`，因此 `docker-compose.yml` 把命名卷 `bot-data` 挂到 `/app/data`。
  这样默认配置开箱即持久化，容器重建后去重与配额状态保留，但内存中的对话上下文会清空。
  若改动 `storage.db_path` 或卷挂载点，必须同步修改另一处。
- 密钥通过宿主环境变量注入，`docker-compose.yml` 里只有 `${VAR}` 引用，不含任何取值。
- 运维端口在容器内 `expose 8080`，**不**发布到宿主；由 Compose 健康检查在容器内访问。
- 健康检查用 `/livez`，`start_period` 为 30 秒，避免启动期与站点临时故障造成重启。
- 运维端口不变量：Dockerfile 与 `docker-compose.yml` 的 `healthcheck` 都硬编码访问容器内
  的 `8080`，与 `ops.port` 默认值一致。若把 `ops.port` 改成其他值，必须同步修改这两处
  健康检查，否则容器会一直不健康而程序其实完全正常。

## 运维端点

| 路径 | 含义 | 通过 | 不通过 |
| --- | --- | --- | --- |
| `/livez` | 进程存活：事件循环在跑，SSE 与工作器池等关键任务未死 | `200 ok` | `503 down` |
| `/readyz` | 可以接单：已登录、SSE 已连接、队列未满、未处于 403 不可用状态 | `200 ready` | `503 not ready` |

`/readyz` 表示「现在能处理消息」，会比 `/livez` 更早、更频繁地变成 `503`
（例如站点暂时不可达、SSE 断开、队列被打满）。因此健康检查与自动重启只看 `/livez`。

## 上线前人工检查清单

- [ ] 在站点把机器人账号的资料改为明确标注：这是机器人、**消息可能发送至第三方模型处理**，
      并说明**大区是公开多人上下文**（同一回复链内的历史会再次发送给模型）。
- [ ] 确认机器人账号已提升为 core+，否则聊天读取与发送会被拒绝。
- [ ] 确认登录用户名与 `@机器人用户名` 完全一致（大区提及区分大小写且要求用户名边界）。
- [ ] 确认 `config.yaml` 中的 `site.base_url`、`model.base_url`、`model.model` 均为目标环境实际取值。
- [ ] 确认密钥只存在于运行环境，未写入任何被提交或被打包的文件。
- [ ] 确认 `storage.db_path` 位于持久化卷内（容器里默认即 `/app/data/bot.db`）。
- [ ] 验证大区普通消息不触发、精确 `@机器人` 触发、私聊触发，回复都引用了原消息。
- [ ] 验证机器人自己的消息回显不会形成回复循环。
- [ ] 模拟模型超时、站点 401/403/429 与队列满载，确认提示与冷却符合预期。
- [ ] 验证 `/livez`、`/readyz`、重启自动拉起与优雅关闭。
- [ ] 重启容器，确认对话上下文清空而去重、链归属与配额状态保留。
- [ ] 确认容器日志已按 `docker-compose.yml` 的 `logging` 限额轮转（约 30 MiB）。
- [ ] 确认数据库大小与清理摘要正常（`app.cleanup_done`），必要时停机 `VACUUM`。

## 已知限制

- 不具备图片理解与博客理解能力：消息只处理文本，纯图片/纯博客回一条本地提示。
- 不联网，不调用工具，不访问服务器文件，不调用站内管理接口。
- 没有长期记忆：上下文只存在内存中，进程重启即清空（大区的链归属会保留 7 天，
  但重启后模型看不到重启前的正文）。

## 容量与清理

运行期每 `storage.cleanup_interval_seconds`（默认 1 小时）清理一次，启动时也清理一次：

| 数据 | 保留 |
| --- | --- |
| 大区链映射 | 7 天（`lobby_thread_retention_seconds`） |
| 已处理事件 | 7 天，且不高于安全水位；非终态事件永不按时间删除 |
| 已发回复记录 | 7 天（其引用的事件未完成时保留） |
| 发送尝试 | 48 小时（`send_attempt_retention_seconds`） |
| 到期冷却 | 立即删除 |
| 已知私聊频道 | 只保留最近活动的 `max_dm_channels`（默认 10000）条 |

- SQLite 主库只设**软上限**（`sqlite_soft_limit_bytes`，默认 128 MiB），超过时记一条 error 日志，
  不做硬截断 —— 硬截断会让去重、水位或配额写入突然失败，后果比库变大严重得多。
- 运行期**不自动 `VACUUM`**（它会长时间独占数据库锁）。需要收缩物理文件时，
  停机备份后手工执行。
- 容器日志由 Docker 的 `json-file` 驱动限制在约 30 MiB；裸机运行时程序只写 `stderr`，
  文件轮转交给 systemd / 进程管理器 / shell 重定向，应用不与宿主争夺轮转职责。
- 配置文件有 **1 MiB 硬上限**（代码常量，不可通过 YAML 调整），超过则在启动阶段直接失败。
- 上游 POST 没有幂等键，极端网络故障下无法保证严格 exactly-once，只做可恢复的尽力去重。
- 当前接口无法列出机器人尚未知晓的私聊频道，因此积压超过 100 条期间首次出现的新私聊
  可能无法通过 resync 恢复。
- 运维端点不发布到宿主，仅容器内可见；如需外部监控，请由编排层或其他方式转发。
