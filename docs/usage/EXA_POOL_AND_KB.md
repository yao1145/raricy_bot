# Exa 多 Key 池与本地知识库：配置与使用

面向运维这台机器人的操作者。两项能力**默认都关闭**，不打开就与它们无关；打开后各自只影响
一个显式的用户命令，不会改变普通聊天、博客评论、`/livez`、`/readyz`。

先读 [`DEPLOYMENT.md`](DEPLOYMENT.md) 第 1 节的前置确认（账号、披露、单副本、密钥），
以及本文各自开头的「前置门禁」——两条门禁都只能人工关闭，代码不替你做判断。

> 本文只讲这两项。聊天区另有 `/zhihu`、`/map`、`/wolfram` 三条同类能力命令（默认关闭，
> 启用前必须先取真实样本校准），它们的启用步骤见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §4.1。

## 0. 一分钟速览

|                  | Exa 多 Key 池                                                | 本地知识库                                         |
| ---------------- | ------------------------------------------------------------ | -------------------------------------------------- |
| 默认             | 关闭（默认是单 Key 或完全不开搜索）                          | 关闭                                               |
| 开关             | `mcp.servers.exa.account_pool`（且 `mcp.enabled: true`） | `knowledge_base.enabled: true`                   |
| 用户怎么用       | `/search <问题>`（不变）                                   | `/kb <问题>`                                     |
| 数据去哪         | 问题 → 模型；模型决定的查询 → Exa                          | 问题 + 命中片段 → 模型（**不发** Exa）      |
| 前置门禁         | Exa 授权确认（§1.2）                                        | 目录清点 + 可见范围确认（§2.2）                   |
| 故障时的用户观感 | `/search` 说「联网搜索暂时不可用」                         | `/kb` 说「知识库不可用 / 没有权限 / 没找到资料」 |

---

# 第一部分：Exa 多 Key 池

## 1.1 它做什么、不做什么

**做**：把多个已获授权的 Exa API Key 当成一个逻辑账号用。某个 Key 明确限流（429）、
额度耗尽（402）、失效（401）或它的子进程崩了，就换下一个试；对模型和用户仍然只是
一次 `/search` 和同一个 `exa__web_search_exa` 工具。

**不做**：

- 不绕过 Exa 的账户、调用量、免费额度限制。池只提高**可用性**，不提高容量。
- 如果不属于同一个 Team、共享同一份预算，轮换在容量上是无效的（`§1.6` 有验证方法）。
- 不提高搜索频率：全局仍是最小间隔 2 秒、并发 1。
- 不让模型看到「有几个账号」，也不让模型挑账号。

## 1.2 前置门禁：先拿到授权

Exa 的服务条款要求 API 使用遵守其技术文档、使用指南与调用量限制，官方团队文档也说明
同一 Team 的成员共享该 Team 的限制。「为叠加免费额度而注册多个个人账号」**没有**被官方
明确允许过。因此在拿到下面任一条之前，**保持 `account_pool` 关闭**：

- Exa 书面确认本次部署可以轮询这些账号/Key；或
- 这些 Key 来自同一组织依法管理的独立预算，且当前合同明确允许；或
- 改用官方 Team、充值、教育/创业额度等官方支持的容量方案。

确认记录只保存**批准日期、适用账号范围与批准渠道**，不要把邮件正文里的 Key 抄进任何地方。

## 1.3 配置

### 1.3.1 单 Key 与池二选一

`mcp.servers.exa` 下的 `env_from` 与 `account_pool` **互斥**，同时出现会在启动阶段直接报错。
单 Key 保持原样：

```yaml
mcp:
  enabled: true
  servers:
    exa:
      enabled: true
      transport: stdio
      command: exa-mcp-server
      args: []
      env_from:
        EXA_API_KEY: EXA_API_KEY
      env:
        ENABLED_TOOLS: web_search_exa
        DEBUG: "false"
```

改成池：把 `env_from` 那两行去掉，换成 `account_pool`。

```yaml
mcp:
  enabled: true
  servers:
    exa:
      enabled: true
      transport: stdio
      command: exa-mcp-server
      args: []
      env:
        ENABLED_TOOLS: web_search_exa
        DEBUG: "false"
      account_pool:
        child_env: EXA_API_KEY          # 首版必须逐字是这个
        host_envs:                      # 写的是环境变量“名字”，不是 Key
          - EXA_API_KEY_1
          - EXA_API_KEY_2
          - EXA_API_KEY_3
        strategy: round_robin           # 首版必须逐字是这个
        rate_limit_cooldown_seconds: 60
        transient_cooldown_seconds: 30
        quota_cooldown_seconds: 21600
```

### 1.3.2 字段

| 字段                            | 默认            | 说明                                                                       |
| ------------------------------- | --------------- | -------------------------------------------------------------------------- |
| `child_env`                   | `EXA_API_KEY` | 注入给 Exa 子进程的变量名。首版只能是这个                                  |
| `host_envs`                   | 无（必填）      | 宿主环境变量**名**的列表，每个名字对应一个槽位。个数 2–16，不得重复 |
| `strategy`                    | `round_robin` | 首版只能是这个                                                             |
| `rate_limit_cooldown_seconds` | `60`          | 收到 429 后该槽位冷却多久                                                  |
| `transient_cooldown_seconds`  | `30`          | 5xx、超时、子进程退出后的冷却，也是重启失败后的重试间隔                    |
| `quota_cooldown_seconds`      | `21600`       | 明确额度耗尽（402 等）后的重新探测间隔                                     |

槽位数就是 `host_envs` 的条数：3 个名字 = 3 个子进程 = 3 个槽位。

### 1.3.3 怎么给 Key

宿主环境里逐个给出，名字与 `host_envs` 一一对应：

```bash
# 本地运行
export EXA_API_KEY_1=第一个Key
export EXA_API_KEY_2=第二个Key
export EXA_API_KEY_3=第三个Key
```

```bash
# Docker：写进 .env，compose 里逐个透传（docker-compose.yml 已给出三行示例）
EXA_API_KEY_1=第一个Key
EXA_API_KEY_2=第二个Key
EXA_API_KEY_3=第三个Key
docker compose up -d
```

**不要**把多个 Key 拼成一个参数（例如 `EXA_API_KEY="k1,k2,k3"`）：那会被 `ps`、日志或
错误信息原样打印出来。逐个显式的变量名不会被打印。Key 在任何子进程启动前就已登记进脱敏器，
不会出现在日志里。

### 1.3.4 启动时会被拒绝的配置

配错就在启动阶段以退出码 2 失败，不会带着半套配置跑起来：

- `account_pool` 与 `env_from` 同时出现；
- `host_envs` 少于 2 个或多于 16 个、含非法环境变量名、或名字重复；
- `child_env` 不是 `EXA_API_KEY`，或 `strategy` 不是 `round_robin`；
- 三个冷却秒数不是正数。

启动前想先自查（不启动进程、只打印非敏感项）：

```bash
docker compose run --rm bot python -c "
from raricy_bot.config import load_config
c = load_config('/app/config.yaml')
server = c.mcp.servers.get('exa')
pool = None if server is None else server.account_pool
print('exa 服务器:', None if server is None else server.enabled)
print('pool 槽位数:', None if pool is None else len(pool.host_envs))
print('知识库:', c.knowledge_base.enabled, c.knowledge_base.access_mode, c.knowledge_base.allowed_channel_kinds)
"
```

它只打印槽位**个数**（不打印变量名），输出里不会出现任何 Key。

## 1.4 运行期：槽位状态与故障转移

每个槽位在内存里有一份状态，**从不写进 SQLite**：余额和 Key 状态是 Exa 侧的外部事实，
本地存一份只会在充值或月度刷新之后变成错的真相。进程重启后重新探测。

| 状态          | 什么时候进入                                                                      | 什么时候恢复                                      |
| ------------- | --------------------------------------------------------------------------------- | ------------------------------------------------- |
| `ready`     | 子进程起来且发现了绑定的工具                                                      | 调用成功后保持                                    |
| `cooldown`  | 429；5xx、超时、子进程退出                                                        | 冷却到期后由池自己重启并探测                      |
| `exhausted` | 402 /`NO_MORE_CREDITS` / `API_KEY_BUDGET_EXCEEDED` / `TEAM_BUDGET_EXCEEDED` | `quota_cooldown_seconds` 到期后探测，或重启进程 |
| `invalid`   | 401 /`INVALID_API_KEY`                                                          | **本进程内不再尝试**；换 Key 后重启生效     |
| `disabled`  | 环境变量缺失、Key 值与其他槽位重复、找不到绑定工具、schema 与其他槽位不一致       | 修正配置或环境后重启                              |

一次 `/search` 里发生的事：

1. 经过既有的全局限流（最小间隔 2 秒、并发 1）。
2. 从轮询游标之后取下一个 `ready` 槽位。
3. 成功就立刻返回，不再试别的槽位；模型侧仍然只看到一次工具调用。
4. 遇到上面那几类**账号级**错误才换下一个槽位。每个槽位在一次调用里**最多试一次**，
   尝试次数不超过当时的可用槽位数。
5. 请求级错误（400、参数或工具名不对）和无法分类的错误**不轮换**，直接把稳定的错误交给
   模型 —— 宁可这一轮说「搜索失败」，也不凭一句普通文本猜「额度耗尽」而把健康槽位冷藏六小时。
6. 全部槽位都失败时，用户看到的是和单 Key 一样的「联网搜索暂时不可用」。

两点必须知道的成本语义：

- **一次模型可见的搜索可能消耗多个上游请求**（每个失败槽位一次）。别用「模型调用次数」
  估算上游用量。
- 超时之后上游**可能已经计费**，换 Key 重试会产生重复费用。这是刻意接受的：每槽一次、
  有界尝试，且不对已经成功的结果再试。

单个槽位出问题由池自己重启，不会牵连整台服务；外层也不会因为「当前没有可用槽位」而对整个
池做重启（那会把还在冷却里的槽位一并拉起，抹掉冷却）。

## 1.5 日志怎么看

| 事件                                                     | 级别    | 能看出什么                                                   |
| -------------------------------------------------------- | ------- | ------------------------------------------------------------ |
| `mcp.pool_started`                                     | INFO    | `count=` 起来的槽位数；`count=0` 说明一个都没起来        |
| `mcp.pool_slot_disabled`                               | WARNING | 哪个槽位（`slot=`）因为什么（`reason=`）被停用           |
| `mcp.pool_slot_recover_wait`                           | INFO    | 某槽位还在失败，`delay=` 秒后再试                          |
| `mcp.pool_slot_recovered`                              | INFO    | 某槽位恢复可用                                               |
| `mcp.pool_slot_state`                                  | DEBUG   | 每次状态迁移（要看这个得把`logging.level` 调到 `DEBUG`） |
| `mcp.pool_call_failed` / `mcp.pool_call_unavailable` | DEBUG   | 一次逻辑调用全部失败 / 一个可用槽位都没有                    |
| `mcp.pool_restart_failed`                              | DEBUG   | 恢复时重启子进程失败（异常类名）                             |

**永远看不到**：Key 的值、环境变量名、查询内容、URL、摘要、上游错误正文。日志里只有槽位
序号（进程内编号，重启会变）和稳定的原因字符串，例如 `missing_env`、`duplicate_secret`、
`schema_mismatch`、`required_tools_missing`、`rate_limit`、`quota`、`invalid_key`。

```bash
# 看槽位停用与恢复（默认 INFO 就有）
docker compose logs bot | grep -E "mcp.pool_(slot_disabled|slot_recover_wait|slot_recovered)"
# 看逐个槽位的状态迁移（需要 logging.level: DEBUG）
docker compose logs bot | grep "event=mcp.pool_slot_state"
```

## 1.6 验收与排障

上线前用**非生产的测试 Key**做一遍：

1. 只给一个合法 Key，确认 `/search` 正常。
2. 换成池（其中混一个故意写错的 Key），确认 `/search` 仍然正常，且日志里那个槽位被标成
   `invalid` 或反复 `recover_wait`，其余槽位照常服务。
3. 把**全部** Key 换成错的，确认 `/search` 回「联网搜索暂时不可用」，而普通聊天、评论、
   `/livez`、`/readyz` 全部不受影响。
4. 在容器里确认子进程数量与 FD 占用随槽位数线性增长，记录启动/停止耗时（这是
   P1-03 的「待验证」项，需要真实数字，不能靠推断）。
5. 确认多个 Key 是否属于同一个 Team：同 Team 共享预算时轮换在容量上完全无效；
   429 是按 Key、按 Team、按 IP 还是按出口计也需要实测。

| 现象                                 | 先看                                                       | 多半是                                                                                |
| ------------------------------------ | ---------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `/search` 说不可用，但普通聊天正常 | `mcp.pool_call_unavailable`、`mcp.pool_started count=` | 所有槽位都在冷却里，或一个都没起来                                                    |
| 某几个 Key 再也没被用过              | `mcp.pool_slot_disabled reason=`                         | `invalid_key`（换 Key 后要重启）、`schema_mismatch` 或 `required_tools_missing` |
| 日志里完全没有池的事件               | `logging.level`                                          | 默认 INFO 看不到 DEBUG 的状态迁移；先确认`mcp.enabled: true` 且用的是池配置         |
| 换了 Key 但行为没变                  | 容器是否重建                                               | 改环境变量后要`docker compose up -d`，仅 `restart` 不会重新读取                   |

---

# 第二部分：本地 Markdown 知识库

## 2.1 它做什么、不做什么

**做**：`/kb <问题>` 时从只读目录递归读取 Markdown，按词法相关度挑出几段，连同问题一起
发给模型作答。命中片段只属于当前这一轮。

**不做**：不联网、不解析 PDF/Word/图片/网页、不做向量检索或语义重排、不把正文写进
SQLite 或日志、不在普通聊天里自动读取。

## 2.2 前置门禁：目录清点与可见范围

机器人会把命中的片段**展示给提问者**并发给第三方模型。上线前必须：

1. **逐文件清点**你打算挂载的目录，确认其中没有密钥、Cookie、个人隐私、内部提示词、
   部署配置、日志、数据库、以及无权转交第三方模型的版权材料。
2. 确认**目录名与文件名**本身也可以公开：分类名和相对路径会出现在回答的来源里。
3. 确认可见范围：默认只对**私聊 + 白名单用户**开放；要在大厅公开必须显式配置，
   并接受「大厅回复所有参与者都能看到」这一事实。资料负责人应签字确认。
4. 若目录内容可能被追问到敏感信息，宁可不挂载。

## 2.3 目录怎么放

```text
knowledge/                     ← 挂载点，根目录本身不算分类
├── 电化学/                    ← 一级子目录就是分类
│   ├── 迁移数.md
│   └── 基础/
│       └── 离子迁移.md        ← 更深层目录保留在相对路径里，分类仍是“电化学”
├── 站点规则/
│   └── 引用语法.md
└── 说明.md                    ← 根目录直接放置的文件归入保留分类 _root
```

规则：

- 只读扩展名大小写无关的 `.md`；其它文件静默忽略。
- 隐藏文件与隐藏目录（名字以 `.` 开头）跳过；符号链接 / junction / 指向根目录外的项跳过。
- 只接受 UTF-8（允许带 BOM）。编码非法的文件、超过单文件上限的文件会被跳过，**其余文件
  仍然可用**。
- 文件数或总字节超过配置上限时，**整次构建失败**并保留上一份可用索引（不会产出半份）。

## 2.4 配置

```yaml
knowledge_base:
  enabled: true
  root_dir: "./knowledge"        # 容器里解析为 /app/knowledge
  access_mode: allowlist         # allowlist（默认）| all_chat
  allowed_channel_kinds:
    - dm                         # 可选 dm / lobby
  allowed_user_ids:              # allowlist 下必填；写站点稳定 id，不是用户名
    - "站点用户id"
  refresh_seconds: 60
  max_files: 2000
  max_file_bytes: 1048576        # 单文件 1 MiB
  max_total_bytes: 67108864      # 全库 64 MiB
  chunk_chars: 2400
  chunk_overlap_chars: 200
  top_k: 6
  max_context_tokens: 4000       # 必须 <= behavior.context_input_tokens
```

| 字段                    | 作用                                                          | 硬上限（代码常量，配不上去） |
| ----------------------- | ------------------------------------------------------------- | ---------------------------- |
| `max_files`           | 最多收录多少个文件                                            | 20000                        |
| `max_file_bytes`      | 单文件字节上限，超过即跳过                                    | 8 MiB                        |
| `max_total_bytes`     | 全库累计字节上限，超过即整次失败                              | 512 MiB                      |
| `chunk_chars`         | 单块字符数                                                    | 20000                        |
| `chunk_overlap_chars` | 相邻块的重叠字符，必须小于`chunk_chars`                     | —                           |
| `top_k`               | 每轮最多给模型几段资料                                        | 10                           |
| `max_context_tokens`  | 资料块的 token 预算，必须 ≤`behavior.context_input_tokens` | 32000                        |
| `refresh_seconds`     | 重建索引的周期                                                | 86400                        |

要在大厅公开使用（大厅仍必须精确 @ 机器人）：

```yaml
  access_mode: all_chat
  allowed_channel_kinds:
    - dm
    - lobby
```

校验说明：`allowlist` 下 `allowed_user_ids` 为空、或 `max_context_tokens` 超过
`behavior.context_input_tokens`，都只在 `enabled: true` 时才报错 —— 默认关闭的部署不会
因为一个与它无关的默认值组合而启动失败。

## 2.5 挂载与权限

```bash
mkdir -p /opt/raricy_bot/knowledge/电化学
chmod -R a+rX /opt/raricy_bot/knowledge      # 容器用户 uid 10001 只需可读
```

`docker-compose.yml` 已经有 `./knowledge:/app/knowledge:ro`。要点：

- **必须只读挂载**，容器用户只需可读权限，绝不给写权限。
- **不要**把资料复制进镜像：镜像层里的旧资料会一直留着，回滚镜像等于回滚资料
  （`.dockerignore` 已经排除 `knowledge`）。
- Rocky / RHEL 启用 SELinux 时按部署指南附录 A 评估 `:Z`，先看现有目录标签再决定，
  不要直接加上去打乱已有标签。
- 不用知识库时留着那行挂载也无害：目录不存在只会让 `/kb` 回「知识库不可用」。

## 2.6 运行期：扫描、检索、刷新

- **启动**：`enabled: true` 时，首次索引在启动阶段**同步**建好（之后才启动 `/livez`）。
  目录越大启动越慢，所以 `max_files` / `max_total_bytes` 要和实际资料规模相称：
  默认的 2000 个文件、64 MiB 通常在秒级完成。停止时若正在重建，后台线程会跑完当前一轮，
  进程退出可能慢于 10 秒的优雅关闭预算，`stop_grace_period` 留点余量即可。
- **分块**：文件开头完整的 front matter 不参与检索；第一个 `#` 标题是文档标题；
  `#`–`######` 构成标题路径；优先在标题与空行边界切块，超长段落按 `chunk_chars` 硬切并保留
  重叠；代码围栏内部不会被空行拆开。
- **检索**：纯标准库词法匹配（拉丁词 + 中文单字与相邻双字），正文用 BM25，分类名、相对路径、
  标题与标题路径有固定的小幅加权；同一文件最多返回 2 段；分数太低就当作没有命中。
- **刷新**：每 `refresh_seconds` 在后台线程重建一次，**完整成功才整体替换**；单个坏文件跳过，
  目录不可读、超限或结果为空则保留上一份可用索引。所以改完文件不会立刻生效，
  最长等一个刷新周期。
- **没有命中时不调用模型**：直接回一句本地提示。这是刻意的 —— 让模型「凭常识」回答会把常识
  伪装成来自知识库的结论。

## 2.7 用户侧：命令与访问策略

| 输入                             | 结果                                         |
| -------------------------------- | -------------------------------------------- |
| 私聊`/kb 迁移数是什么`         | 检索全部分类（受访问策略限制）               |
| 大厅`@机器人 /kb 迁移数是什么` | 先满足精确提及，再检索                       |
| `/kb`                          | 本地回用法，不检索、不调模型                 |
| `/kb /help`、`/kb /reset`    | 本地命令优先，照常执行                       |
| `/search /kb 问题`             | 能力冲突：一条消息只能有一种能力，本地拒绝   |
| 博客评论里写`/kb`              | 当普通评论正文，不检索                       |
| 未授权用户`/kb 问题`           | 固定文案，不泄露目录、分类、文件数或命中情况 |

访问判据是站点**稳定的用户 id**（`author.id`），不是可以改的用户名。命中片段拼在本轮最后一条
`role="user"` 消息里，system 里只有一段静态说明；回复送达后历史里保存的是**去掉资料块**的
问题和回答。

## 2.8 日志怎么看

| 事件                    | 级别    | 能看出什么                                                                                                                  |
| ----------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------- |
| `kb.ready`            | INFO    | `snapshot_version=` 版本号、`chunk_count=` 块数、`count=` 文档数、`size_bytes=` 总字节                              |
| `kb.index_failed`     | WARNING | `reason=`：`root_missing` / `root_unreadable` / `too_many_files` / `total_too_large` / `empty` / `unexpected` |
| `kb.search_failed`    | WARNING | 检索本身出了意外（正常路径不会出现）                                                                                        |
| `kb.query_done`       | INFO    | 本轮返回了几段（`count=`）与快照版本                                                                                      |
| `app.kb_unavailable`  | INFO    | 本地拒绝的原因：`disabled` / `access` / `unavailable` / `no_results`                                                |
| `app.kb_start_failed` | WARNING | 启动知识库时的意外异常                                                                                                      |

**永远看不到**：分类名、文件名、相对/绝对路径、标题、正文、查询内容或命中片段。
判断「新资料有没有生效」看 `snapshot_version` 是否递增，而不是看文件名。

## 2.9 验收与排障

上线前至少验证：

1. 白名单里的用户私聊 `/kb <问题>` 能拿到带来源的答案。
2. 不在白名单的用户收到无权限提示，且提示里没有目录或分类信息。
3. 大区（若开放）仍然必须精确 @ 才触发。
4. `/search /kb x` 返回能力冲突提示，不检索也不调模型。
5. 改一个 `.md` 后，等一个 `refresh_seconds`，`kb.ready` 的 `snapshot_version` 递增，
   新内容可以被检索到。
6. 目录里放一个非法 UTF-8 的文件，确认其余文件仍可检索。
7. `docker exec` 进去确认 `/app/knowledge` 是只读的、uid 10001 能读。

| 现象                   | 先看                                     | 多半是                                                                          |
| ---------------------- | ---------------------------------------- | ------------------------------------------------------------------------------- |
| 用户说「没有权限」     | `app.kb_unavailable reason=access`     | `allowed_user_ids` 没包含他的 id，或频道类型不在 `allowed_channel_kinds` 里 |
| 一直「没找到资料」     | `app.kb_unavailable reason=no_results` | 词法检索匹配不上：换个措辞，或确认资料里确实有相关词                            |
| 「知识库不可用」       | `kb.index_failed reason=`              | 目录不存在、为空、超限，或整个目录都不是 UTF-8                                  |
| 改了文件没生效         | `kb.ready snapshot_version`            | 正常现象，等一个刷新周期；版本号不动说明构建一直在失败                          |
| 启动明显变慢           | `/livez` 之前的时间                    | 首次索引是同步的：调小`max_files` / `max_total_bytes`                       |
| 回答里有资料但答非所问 | `kb.query_done count=`                 | `top_k` 太大或资料切分太碎，调 `top_k` / `chunk_chars`                    |

---

# 第三部分：一起用

## 3.1 一条消息只能有一种能力

`/search /kb 问题` 这类叠加会被本地拒绝，返回一条固定提示。原因很直接：用户只理解一套披露时，
这条消息会同时把查询发给 Exa、把本地资料发给模型 —— 那不是他能预期的行为。要两个都用，
请分成两条消息。

`/search /help`、`/kb /reset` 不构成冲突：本地命令优先。

## 3.2 数据边界对照

| 数据                   | 发给模型 | 发给 Exa                         | 进日志 | 进 SQLite | 进历史             |
| ---------------------- | -------- | -------------------------------- | ------ | --------- | ------------------ |
| 普通聊天正文           | 是       | 否                               | 否     | 否        | 是（送达后）       |
| `/search` 的问题     | 是       | 模型决定要搜时，由模型生成的查询 | 否     | 否        | 是（送达后）       |
| `/search` 的搜索摘要 | 是       | —                               | 否     | 否        | 压缩摘要（送达后） |
| `/kb` 的问题         | 是       | **否**                     | 否     | 否        | 是（送达后）       |
| `/kb` 的命中片段     | 是       | **否**                     | 否     | 否        | **否**       |
| 密钥、Cookie           | 否       | 否                               | 否     | 否        | 否                 |

## 3.3 成本与配额

- 一次最终回答仍然只占**一条**站点 `reply` 配额；`/search`、`/kb` 的用法提示、无权限、
  无命中与不可用提示都属于明确用户动作，走 `notice_local`，不占主动通知冷却。
- 一次 `/search` 在模型侧仍是一个工具调用，但池的故障转移可能产生多个 Exa 上游请求。
- 知识库的检索在模型并发门之外执行，不占用聊天槽位；真正调用模型时仍走同一道门。

---

改代码前请先读 [`../design/INTERFACES.md`](../design/INTERFACES.md) §22（池）与 §23（知识库），
它们是这两项能力的锁定合同；「为什么是这样」的裁决见
[`../design/DESIGN_DECISIONS.md`](../design/DESIGN_DECISIONS.md) 的 D-36 … D-46。
