# 用户公开个人记忆：设计与实现规划

## 0. 文档状态

| 项目 | 内容 |
| --- | --- |
| 状态 | 产品行为已确认；内部合同已冻结（`INTERFACES.md` §39–§52、`DESIGN_DECISIONS.md` D-96–D-103，含 D-56 / D-76 的补充），实施中 |
| 日期 | 2026-09-17 |
| 功能名 | `public_personal_memory` |
| 目标 | 允许用户把自己的私有记忆条目显式公开，并只在大区或评论对话确实涉及该用户时作为当轮资料提供给模型 |
| 上游约束 | `docs/materials/chat-bot.md` 与 `docs/materials/comment-bot.md` 只读且优先级最高 |
| 数据原则 | 未公开的私有记忆绝不进入公开请求；公开个人记忆只进入当前轮 `role="user"`，不进 system、短期历史、日志或 SQLite |
| 接入原则 | 首版继续服从 `memory.enabled` 与现有 `memory.access_mode` / `allow_user_list` 门禁 |

本文前半部分用较短篇幅说明产品行为、核心设计和项目架构；后半部分给出存储格式、接口、算法、
文件级改动、实施顺序、测试和验收标准。实现前应同步修改 `INTERFACES.md`、
`DESIGN_DECISIONS.md` 与相关用户文档，不能只改代码而留下互相矛盾的合同。

该同步已于 2026-09-17 完成（本功能任务 1）：`INTERFACES.md` §39–§52 冻结了配置、公开
Markdown、模型、服务、subject resolver、站点查询、聊天/评论路径、文案与日志的合同；
`DESIGN_DECISIONS.md` D-96–D-103 记录裁决并补充了 D-56 / D-76；`GLOBAL_MEMORY_DESIGN.md`
§4.5、`GLOBAL_MEMORY_IMPLEMENTATION_PLAN.md` 头部与 `SYSTEM_PROMPTS.md` §1.9 是各自的
交叉引用与静态文案。后续任务按这些合同实现；实现中发现合同与本文冲突时停下报告，不要自行
改写边界（本文的产品语义优先，`docs/materials/*` 的上游契约再优先于本文）。

---

# 第一部分：简明设计与项目架构

## 1. 背景与目标

当前长期记忆分为两类：

- `all_user` / `lobby` 共同记忆由管理员候选、审批并对符合条件的请求生效；
- 用户私有记忆只在所属用户的私聊中读取，大区与评论路径连私人文件都不打开。

这两类都不能直接表达“这是 Alice 主动公开的个人偏好，只在本轮确实谈到 Alice 时使用”。若把此类
内容放进 `all_user`，它会在每一轮无条件出现；若直接让公开请求读取私人文件，又会削弱现有的隔离
边界。

本功能增加第三类记忆：**用户公开个人记忆**。

1. 用户在私聊中用 `/memory public <UM-ID>` 公开自己已有的一条私有记忆；
2. 公开动作不调用 AI，公开的是用户已经在 `/memory list` 中能看到的确定内容；
3. 公开内容复制到独立的公开投影文件，公开请求仍不打开私人文件；
4. 只有当大区或评论对话出现或精确提到该用户时，才把该用户的公开条目提供给模型；
5. 用户可用 `/memory unpublic <UM-ID>` 随时撤回；
6. 私聊不会加载别人的公开个人记忆；用户自己的私聊继续使用原有私有记忆路径。

“公开给 `all_user`”在本设计中表示**可见受众**，不是现有 `MemoryScope.ALL_USER` 的同义词。公开个人
记忆有独立的存储、匹配条件、预算和命令，不写入 `common.md`。

## 2. 明确不做的事情

- 不把公开个人记忆并入 `common.md` 的 `all_user` 区。
- 不允许模型、自动记忆或普通聊天文本决定公开、撤回、owner、路径或作用域。
- 不允许其他用户替条目所有者公开或撤回。
- 不在大区或评论区执行任何记忆管理命令；命令仍只在私聊使用。
- 不在 DM 中因为提到第三方而加载第三方公开记忆。
- 不做模糊用户名搜索、前缀搜索、包含搜索、编辑距离匹配或语义猜测。
- 不让模型回答、搜索结果、知识库片段或 MCP 工具结果触发用户公开记忆。
- 不把公开资料写进短期历史；下一轮必须重新满足“出现或提到”的条件。
- 不把原始 user ID、用户名、正文或参与者列表写入 SQLite 或日志。
- 不修改上游站点，也不引入新的运行时依赖。
- 首版不增加管理员审批：条目所有者的显式公开命令就是授权动作。

## 3. 核心设计原则

### 3.1 公开投影与私人真相源分离

新增目录：

```text
data/memory/
├── common.md
├── users/
│   └── <owner_key>.md       # 现有私人文件
└── public/
    └── <owner_key>.md       # 新增公开投影，只含主动公开的条目
```

`owner_key` 继续使用 `user_storage_key(user_id)`。公开文件路径和正文中都不出现原始 user ID。

公开请求只读取 `public/`。它不会先打开私人文件再过滤，因此一次选择错误最多影响已经公开的内容，
无法越过边界碰到未公开条目。`users/` 仍是私有内容的真相源；`public/` 是用户主动授权过的公开快照。

### 3.2 发布是显式快照，不是动态引用

`/memory public UM-...` 把当时的私有条目复制为公开快照。公开后：

- AI 撰写器和自动记忆不得静默修改对应私有条目；
- 用户要修正内容时，先撤回、再修改、最后重新公开；
- 重复执行 `public` 对同一条目是幂等的；
- 公开确认必须回显实际公开的 ID 与正文，并明确其可能出现在公开对话的模型请求中。

这个限制避免“用户只批准过旧版本，但自动记忆后来把新内容也公开”的隐式授权扩大。

### 3.3 身份匹配由宿主完成

模型不参与“这段话提到了谁”的身份决策。宿主先根据稳定身份与精确用户名规则产生有限的
`PublicMemorySubject`，再按 owner 读取公开投影。

普通姓名文本可以触发，但候选集合只来自已经存在公开投影的用户名索引。程序不会拿正文里的每个词
调用用户搜索接口。对不是当前已知参与者的名字，还要通过站点用户搜索做一次精确身份校验，确认
`user_storage_key(搜索结果.id)` 与公开投影的 owner 一致。

### 3.4 动态资料只进入当轮 user 数据

公开个人记忆与现有记忆一样，是不可信动态资料：

- 正文和用户名只进入当前轮 `role="user"`；
- system 中只追加不含运行时插值的静态安全说明；
- 条目不写进 `ContextManager` 历史；
- 用户当前明确陈述与公开记忆冲突时，以当前陈述为准；
- 记忆只能描述标签对应的 owner，不能转移给同一对话中的其他人。

### 3.5 软故障与保守失败

公开索引、身份查询或公开文件读取失败时，本轮省略公开个人记忆，聊天和评论照常继续。涉及删除和
撤回的写操作则保守处理：不能先删除私人来源、再留下无法确认是否已经撤回的公开副本。

## 4. 项目架构

### 4.1 组件关系

```text
私聊命令
  │
  ├─ /memory public UM-ID ───────┐
  └─ /memory unpublic UM-ID ─────┤
                                 ▼
                      MemoryController
                                 │
                                 ▼
                      MemoryService 单写锁
                   ┌─────────────┴─────────────┐
                   ▼                           ▼
        users/<owner_key>.md        public/<owner_key>.md
          私人真相源                   公开投影与用户名索引来源

大区 / 评论请求
        │
        ├─ 当前作者与短期参与者的稳定 subject
        ├─ 当前正文 / 引用正文中的精确用户名
        ├─ 实际提供的博客 / 评论文章正文
        └─ 实际展开的公开剪贴板正文
        │
        ▼
 PublicMemorySubjectResolver
        │ 精确索引匹配 + 必要时站点身份校验
        ▼
 MemoryService.public_context_for(...)
        │ 只读 public/，不读 users/
        ▼
 SupplementalItem(group="memory_public_personal")
        │
        ▼
 ContextManager 统一执行 token 取舍
        │
        ▼
 当轮 role=user 资料块
```

### 4.2 文件职责

| 文件 | 职责 |
| --- | --- |
| `memory/models.py` | 公开条目、公开文档主体、公开 subject 与稳定状态类型 |
| `memory/codec.py` | 私人、共同和公开 Markdown 的严格解析与确定性渲染 |
| `memory/service.py` | 公开投影读取、索引、发布、撤回、两阶段删除和冲突保护 |
| `memory/commands.py` | `public`、`unpublic`、`list public` 的纯解析 |
| `memory/controller.py` | 门禁、幂等、命令编排和固定文案选择 |
| `memory/subjects.py`（新增） | 用户名提取、公开用户名索引匹配、身份查询缓存与 subject 排序 |
| `site/models.py` / `site/client.py` | `/api/chat/users` 的最小 DTO 与精确用户查询 |
| `core/context.py` | 短期历史附带 subject 元数据；公开记忆分组的预算与渲染 |
| `core/router.py` | 聊天请求保留当前作者 subject；管理命令请求补充 username |
| `comments/router.py` | 在仍持有作者 ID 时计算不可逆 owner key，不把原始 ID带入评论 worker |
| `comments/service.py` | 用实际当轮正文、文章正文和展开引用解析公开记忆主体 |
| `app.py` | 装配 resolver，合并共同、私有与公开个人记忆候选 |
| `texts.py` | 命令确认、帮助披露、资料标签与静态 system addendum |

## 5. 用户可见行为

### 5.1 命令

```text
/memory public <UM-ID>
/memory unpublic <UM-ID>
/memory list public
```

原有命令同步调整：

- `/memory list`：每条私有记忆显示 `[私有]` 或 `[已公开]`；
- `/memory status`：增加公开条目数；
- `/memory forget <UM-ID>`：若条目已公开，先撤回公开副本，再删除私人来源；
- `/memory clear`：先撤回该用户全部公开条目，再清空私人条目；
- `/memory off`：只暂停私聊中的私人读取并关闭自动记忆，已公开条目仍然公开，回复必须明说；
- `/reset`：不改变任何私人或公开长期记忆。

### 5.2 作用范围

| 场景 | 共同记忆 | 自己的私有记忆 | 条件命中的公开个人记忆 |
| --- | --- | --- | --- |
| DM | `all_user` | 是，受自己的开关控制 | 否 |
| 大区 | `all_user` + `lobby` | 否 | 是 |
| 评论 | `all_user` | 否 | 是 |

公开个人记忆首版仍要求当前请求作者通过既有记忆接入门。`allowlist` 外用户不会因为提到某个公开 owner
而绕过 Beta 门；`access_mode="all"` 时所有当前作者都可使用。

## 6. “出现或提到”的最终规则

### 6.1 可以产生 subject 的来源

按优先级从高到低：

1. 当前发言者；
2. 当前消息里的精确 `@username`；
3. 当前消息普通文本里的精确 `username`；
4. 当前消息直接引用正文中的精确用户名，以及直接引用作者；
5. 当前短期回复链或评论会话里仍保留的参与者；
6. 本轮实际提供给模型的博客正文或评论文章正文；
7. 本轮实际展开的公开剪贴板正文；
8. 本轮大区近期消息块中实际选入的发言者与正文用户名。

同一 owner 每轮只出现一次。默认最多选择 4 个 owner；达到上限后，低优先级来源不再扩大集合。

### 6.2 精确用户名边界

站点用户名合法字符为 ASCII 字母、数字、`_`、`-`，长度 3–20。提取器只枚举符合该形状的完整
token，再查询本地公开用户名索引：

- 大小写敏感；
- token 前后不能紧邻 `[A-Za-z0-9_-]`；
- `alice` 命中 `alice`，不命中 `alice2`、`myalice`；
- `@alice` 与普通 `alice` 都能产生同一候选，但 `@alice` 优先；
- 不使用前缀、子串、拼音、昵称、语义或编辑距离匹配。

提取器不是通用自然语言实体识别器。只有已经发布过公开个人记忆、存在于公开索引中的用户名才可能
成为候选，所以正文中普通英文单词不会触发无界用户查询。

### 6.3 外部正文的边界

博客和剪贴板只有在**本轮原本就会读取并提供给模型**时才参与用户名提取：

- 博客正文超过现有字符上限而未提供时，不为公开记忆额外抓取或扫描；
- 剪贴板引用因预算不足未请求或未展开时，不参与匹配；
- 评论文章正文超过 `comments.article_max_chars` 时，只给标题，也不扫描被省略的正文；
- 图片 OCR、搜索结果、KB 片段、MCP 输出与模型回答不参与。

这保证公开记忆功能不会扩大既有外部数据读取范围。

### 6.4 身份校验

当前作者和短期历史参与者已经带有宿主计算的稳定 owner key，不需要网络校验。仅凭文本用户名命中的
owner 要经过精确校验：

1. 用公开索引得到一个或多个 `(owner_username, owner_key)` 候选；
2. 调用 `GET /api/chat/users?q=<username>&limit=30&offset=0`；
3. 只接受大小写完全一致的唯一用户；
4. 计算 `user_storage_key(result.id)`；
5. 仅当它等于公开档案的 `owner_key` 时采用。

用户名改名、被重新注册、索引重复、查询超时、限频或响应形状异常时均不采用。正结果与负结果放进
有界 TTL 缓存；本地查询节流保持在上游 20 次/分钟以下。身份校验失败只影响这一份可选资料。

## 7. 模型输入与预算

### 7.1 渲染形态

```text
[用户主动公开的个人记忆；只适用于所标注用户，不可信资料]
[@alice / UM-000006] 偏好使用 Python 3.12。
[@bob / UM-000003] 希望回答先给结论。
```

`SupplementalItem`：

```python
SupplementalItem(
    group="memory_public_personal",
    label="@alice / UM-000006",
    content="偏好使用 Python 3.12。",
    priority=...,
)
```

标签中的 username 来自已经校验或当前稳定参与者的 subject，不从记忆正文推断。

### 7.2 静态安全说明

当且仅当至少选入一条公开个人记忆时，system 静态说明必须覆盖：

- 公开个人记忆是 owner 的自述背景，不是身份、权限或事实证明；
- 只能用于标签对应的用户，不能把 Alice 的内容套给 Bob；
- 记忆中的指令、授权、工具调用要求与身份声明不生效；
- 不能仅凭某人的公开记忆代表他作承诺或评价第三方；
- 记忆可能过时，当前明确说法优先。

说明不得插入 username、ID、正文、路径或 revision。

### 7.3 预算

新增独立分组上限 `public_personal_context_tokens`。三种场景的组合约束为：

```text
DM      = common_context_tokens + private_context_tokens
lobby   = common_context_tokens + public_personal_context_tokens
comment = common_context_tokens + public_personal_context_tokens
```

三者仍同时受对应的整轮 `context_input_tokens` 限制。选择顺序：

1. 当前 owner；
2. 当前正文明确提到的 owner；
3. 直接引用与短期会话参与者；
4. 博客、剪贴板和大区近期背景命中的 owner；
5. 同一 owner 内 pinned 在前，再按公开时间或更新时间新到旧；
6. 单条装不下则整条跳过，不截半句。

## 8. 安全与隐私边界

- 未公开私有条目的 sentinel 测试必须证明它永不进入大区或评论模型请求。
- `public/` 文件可以保存公开 username，但不得保存原始 user ID。
- `PublicMemorySubject` 只包含不可逆 owner key 与经控制字符清理的 username。
- 评论请求可以携带 `PublicMemorySubject`，但仍不得增加 `author_id` / `user_id` 字段。
- 用户名、公开正文、匹配来源文本和站点用户查询参数不得进入日志。
- SQLite 不增加公开条目、参与者、用户名或正文列。
- 普通文本、博客或剪贴板只可触发读取已经公开的数据，不能触发任何 mutation。
- 公开条目继续按不可信数据处理；公开不等于可信，也不等于管理员背书。

---

# 第二部分：具体实现方案与任务规划

## 9. 配置合同

`MemoryConfig` 建议新增：

```python
max_public_entries_per_user: int = 8
max_public_subjects_per_turn: int = 4
public_personal_context_tokens: int = 600
```

`config.example.yaml`：

```yaml
memory:
  max_public_entries_per_user: 8
  max_public_subjects_per_turn: 4
  public_personal_context_tokens: 600
```

校验：

1. 三个值均为正整数；
2. `max_public_entries_per_user <= max_private_entries_per_user`；
3. 启用记忆时，`common_context_tokens + private_context_tokens <= behavior.context_input_tokens`；
4. 启用记忆时，`common_context_tokens + public_personal_context_tokens <= behavior.context_input_tokens`；
5. 启用评论时，`common_context_tokens + public_personal_context_tokens <= comments.context_input_tokens`；
6. `memory.root_dir/public` 仍位于 memory root 内，不增加额外可配置路径。

身份缓存和本地节流首版使用有界代码常量，例如正缓存 10 分钟、负缓存 1 分钟、最多 512 项、
最多 15 次站点查询/分钟。它们是对上游限频的实现保护，不改变产品语义；若后续需要运维调优再提升为
配置合同。

## 10. 数据模型与接口

### 10.1 新增类型

```python
@dataclass(frozen=True)
class PublicMemoryEntry:
    memory_id: str              # 沿用来源 UM-ID
    key: str
    content: str
    pinned: bool
    source_created_at: str
    source_updated_at: str
    published_at: str


@dataclass(frozen=True)
class PublicMemoryDocument:
    schema_version: int = 1
    revision: int = 0
    owner_username: str = ""
    operations: Mapping[str, OperationResult] = field(default_factory=dict)
    entries: tuple[PublicMemoryEntry, ...] = ()


@dataclass(frozen=True)
class PublicMemorySubject:
    owner_key: str
    username: str
    source_priority: int
```

`source_priority` 只表达本轮选择顺序，不落盘、不进日志。`owner_key` 必须是
`user_storage_key()` 的 64 位小写十六进制形态；username 必须满足上游用户名合同。

### 10.2 稳定状态

新增：

```python
STATUS_PUBLIC_CONFLICT = "public_conflict"
```

表示 AI 撰写或自动提取试图更新一条仍然公开的来源条目。它与文件摘要冲突的 `conflict` 不同，必须有
独立用户文案：“该条目当前已公开，请先撤回公开，再修改并重新发布。”

`operations` 允许值集合、codec 校验、Controller 映射和测试全部同步扩展。

### 10.3 服务接口

建议增加：

```python
async def public_entries(self, user_id: str) -> tuple[PublicMemoryEntry, ...]: ...

async def publish_private(
    self,
    user_id: str,
    username: str,
    memory_id: str,
    *,
    operation_id: str,
) -> OperationResult: ...

async def unpublish_private(
    self,
    user_id: str,
    memory_id: str,
    *,
    operation_id: str,
) -> OperationResult: ...

async def unpublish_all(
    self,
    user_id: str,
    *,
    operation_id: str,
) -> OperationResult: ...

async def public_context_for(
    self,
    *,
    subjects: tuple[PublicMemorySubject, ...],
    channel_kind: str,
) -> MemoryContext: ...

def public_path_from_owner_key(self, owner_key: str) -> str: ...
```

`public_context_for` 只接受 `lobby` / `comment`；DM 或未知频道返回空结果。它不接收原始 user ID，也不
自行解析正文。viewer 的接入门仍由 Router/App 在调用前判定；Service 再复查频道与 subject 形状。

## 11. 公开 Markdown 格式

建议格式：

```markdown
---
schema_version: 1
revision: 3
owner_username: alice
operations:
  publish:123:
    status: ok
    object_id: UM-000006
    revision: 3
---

# 用户公开个人记忆

## UM-000006
- key: "preferred_python_version"
- pinned: false
- source_created_at: "2026-09-16T10:00:00+08:00"
- source_updated_at: "2026-09-16T10:00:00+08:00"
- published_at: "2026-09-17T14:00:00+08:00"

偏好使用 Python 3.12。
```

约束：

- `owner_username` 必须满足站点用户名格式，不接受任意文本；
- 条目 ID 必须是 `UM-` + ASCII 十进制序号；
- 同一文档内 ID 与 key 唯一；
- 条目数不超过 `max_public_entries_per_user`；
- 正文与 key 继续执行现有长度、空白规范和密钥筛查；
- `operations` 使用现有 `max_operations` 与最旧淘汰规则；
- 渲染按 UM-ID 数字序排序，字段顺序固定，UTF-8 和换行规则与现有 codec 一致；
- 空公开文档不参与 username 索引。可保留空文件，也可在成功提交后尽力删除；无论哪种都不能改变
  模型可见行为。

## 12. 发布、撤回与删除算法

### 12.1 `/memory public <UM-ID>`

固定顺序：

1. Controller 复查 `permits_commands(user_id, "dm")`；
2. 用 `publish:<message_id>` 查询该用户公开文档的幂等结果；
3. 读取该用户私人快照并精确定位 UM-ID；
4. 找不到则 `not_found`；
5. 对正文与 key 再做一次密钥筛查，人工编辑过的私人 Markdown 也不能绕过；
6. 校验 username；
7. 达到公开条目上限则 `full`，文案必须明确是公开条目上限；
8. 在单写锁内以公开文档当前版本为基线原子添加快照；
9. 同 ID 已存在且内容一致时返回 `noop`；
10. 成功后更新内存公开索引；
11. 回复展示 ID、正文、公开场景、第三方模型传输范围和撤回命令。

整个流程不调用 `MemoryWriter`。

### 12.2 `/memory unpublic <UM-ID>`

1. 用 `unpublish:<message_id>` 查幂等；
2. 只修改该用户公开文档，不改私人来源；
3. 已不存在时返回 `not_found` 或可重复成功，首版沿用现有目标命令口径：`not_found`；
4. 删除最后一条后立刻从内存 username 索引移除 owner；
5. 回复只需确认 ID 已撤回，不回显已经撤下的正文。

### 12.3 `/memory forget <UM-ID>`

这是隐私优先的可恢复两步操作：

```text
步骤 A：公开文档删除该 UM-ID
operation_id = cmd:<message_id>:unpublish

步骤 B：私人文档删除该 UM-ID
operation_id = cmd:<message_id>
```

若步骤 A 失败，步骤 B 不执行；若进程在 A 成功、B 之前退出，重放时 A 幂等命中，再继续 B。最坏状态
是“公开副本已经撤回、私人来源仍保留”，不会出现私人来源删掉而公开副本遗留。

### 12.4 `/memory clear`

同样两步：先 `unpublish_all`，再 `clear_private`。两步都由独立 operation ID 保证重放。公开文档不可用
时不清私人文件，并给用户稳定失败文案。

### 12.5 AI 更新保护

`apply_private_proposal` 在应用 `update` 或“同 key add 替换”前检查对应 UM-ID 是否存在于公开投影：

- 公开：返回 `public_conflict`，私人和公开文件都不写；
- 公开状态无法确认：返回 `unavailable`，保守拒绝；
- 未公开：沿用现有更新逻辑。

自动提取遇到 `public_conflict` 静默跳过，不追加写入披露；显式 `/remember` 返回专用说明。

## 13. 公开索引与用户名解析

### 13.1 索引

`MemoryService.start()` 加载 `public/` 下受容量限制的合法 Markdown，建立：

```python
username_index: dict[str, tuple[str, ...]]  # username -> owner_key(s)
```

只索引至少一条有效公开条目的文档。重复 username 不擅自挑一个；resolver 必须通过站点查询得到稳定
ID 后再选择 owner key。坏文件只让该 owner 的公开资料不可用，记稳定 reason，不记路径、username 或
正文。

刷新周期复用 `memory.refresh_seconds`：

- 文件摘要没变不重解析；
- 合法外部修改原子替换对应快照并重建该 username 的索引项；
- 非法外部修改保留最后一份有效快照；
- 公开目录扫描设置文件数量硬上限，避免异常目录拖垮进程。

### 13.2 文本提取

新增 `memory/subjects.py`，输入已经明确允许扫描的文本段及来源优先级。算法：

1. 用 ASCII username token 正则枚举完整 token；
2. 通过大小写敏感的 `username_index` 查候选；
3. 检查 token 前是否为 `@`，提高显式 mention 的优先级；
4. 按 `(来源优先级, 文本出现位置)` 保持确定性顺序；
5. owner 去重；
6. 达到 `max_public_subjects_per_turn` 后停止扩张；
7. 对没有稳定 subject 的文本候选执行身份校验。

正文不落缓存；缓存只保存 `username -> owner_key | negative` 与过期时间。

### 13.3 站点用户查询

在 `SiteClient` 增加最小只读方法，不创建私聊频道：

```python
async def search_chat_users(self, query: str) -> tuple[ChatUserSummary, ...]: ...
```

要求：

- 固定 `limit=30&offset=0`；
- 复用现有登录、重登、响应字节上限和 JSON envelope 校验；
- DTO 只保留 `id` 与 `username`；
- exact resolver 自己做大小写敏感、唯一结果判定；
- 查询参数、结果 username 和 ID 不进日志；
- 429、网络错误、非法响应与无结果全部返回可降级失败，不影响聊天；
- 测试全部使用 `httpx.MockTransport`，不打开真实连接。

## 14. 短期参与者元数据

### 14.1 `ContextManager`

在不引入 memory 依赖的前提下，增加通用类型：

```python
@dataclass(frozen=True)
class ConversationSubject:
    key: str
    label: str
```

`Turn` 增加可选 `subject`；`append_exchange()` 增加默认空的 user subject 参数；新增只读方法：

```python
def recent_subjects(self, session_key: str) -> tuple[ConversationSubject, ...]: ...
```

只在成功送达并提交完整 exchange 时保存当前用户 subject。原有历史对淘汰、`reset()` 与
`invalidate()` 自然同步删除 subject；不另建一份可能漂移的参与者表。subject 不渲染进 system，
username 的可见标签仍由既有 user 内容包装提供。

### 14.2 聊天请求

聊天 `Request.message.author.id` 仍在 worker 可用。App 在内存中计算：

```python
ConversationSubject(
    key=user_storage_key(author.id),
    label=author.username,
)
```

私聊不调用公开个人记忆 resolver；大区只有 `request.memory_allowed` 为真时才调用。

### 14.3 评论请求

`CommentRouter` 仍在持有 `CommentNode.author.id` 时计算 `ConversationSubject`，写入
`CommentRequest.public_memory_subject`。不得添加原始 `author_id`、`user_id` 或等价字段。

评论 memory provider 从无参数改为请求感知：

```python
Callable[[CommentMemoryInputs], Awaitable[tuple[SupplementalItem, ...]]]
```

输入只含当前 subject、session subjects、已允许扫描的文本段和 `memory_allowed`，不含私人正文。

### 14.4 大区近期消息设计的衔接

`LOBBY_RECENT_CONTEXT_DESIGN.md` 当前建议 `LobbyRecentMessage` 不保存作者 ID。为本功能增加可选
`ConversationSubject`，其中只有哈希 owner key 与 username，不保留原始 ID。只有实际选入当轮近期
消息块的记录才贡献 subject；被预算舍弃的近期消息不应仅凭缓冲中存在就触发公开记忆。

近期消息正文自己的精确用户名同样可以触发，但优先级低于当前正文、引用、回复链参与者和文章正文。

## 15. 聊天路径集成

当前聊天 worker 在内容引用、博客与当前 pending user 都准备好之后，构造：

```python
PublicMemoryInputs(
    channel_kind=request.channel_kind,
    current_subject=...,
    conversation_subjects=ctx.recent_subjects(request.session_key),
    current_text=request.user_text,
    reply_text=request.reply_context,
    blog_text=实际提供给模型的博客正文或 None,
    expanded_clipboard_texts=实际展开的剪贴板正文,
    lobby_recent=实际选入的近期消息,
)
```

解析与取公开记忆必须发生在所有这些数据已经确定之后；不得为记忆匹配额外抓取博客或剪贴板。

`MemoryService.context_for()` 继续负责现有共同/私人候选；公开个人候选由 `public_context_for()` 独立
返回，再由 App 合并成一组 `SupplementalItem` 交给 `ContextManager`。职责分开，避免把
`user_id=None`、subject 列表与现有作用域表硬塞进同一个接口。

## 16. 评论路径集成

`CommentService._build_model_messages()` 已经拥有：

- 当前评论原文和展开后的剪贴板；
- 文章标题与实际提供的文章正文；
- 当前评论 subject；
- 评论会话短期历史 subject。

在上述内容完成字符上限和引用预算判定后调用公开记忆 provider。评论区没有站点级 @ 通知机制不影响
本功能：`@alice` 在这里仅作为机器人内部的精确文本提及，普通 `alice` 也可按同样边界匹配。

评论忙碌、失败、配额用尽仍保持静默；公开记忆解析失败不得改变评论事件状态、重试时间或 `alive`。

## 17. 文案与 system prompt

### 17.1 命令文案

新增固定或组合文案：

- `MEMORY_PUBLIC_USAGE_TEXT`；
- `MEMORY_PUBLIC_CONFLICT_TEXT`；
- 发布成功：回显 UM-ID 与正文，说明公开使用范围和 `/memory unpublic`；
- 撤回成功：确认 ID 已不再用于公开请求；
- 公开列表表头、空态和上限文案；
- `/memory off`：明确已公开条目不受影响；
- `/memory clear`：确认同时撤回并删除的条目数。

Controller 仍不得自行内联中文；全部用户可见文本放在 `texts.py`。

### 17.2 帮助与披露

当前“大区不会使用任何人的私有记忆”改为更准确的口径：

> 大区与评论区不会使用任何未公开的私有记忆。用户可以在私聊中主动公开自己的某些条目；只有当
> 当前公开对话出现或精确提到该用户时，这些公开条目才可能随本轮请求发送给第三方模型。

同时说明：

- 公开与撤回命令只在私聊；
- `/memory off` 不撤回公开条目；
- `/reset` 不删除或撤回长期记忆；
- 普通用户名、博客正文和已展开公开剪贴板都可能触发精确匹配；
- 不做模糊匹配。

### 17.3 静态 system 说明

可扩展现有 `MEMORY_SYSTEM_ADDENDUM`，也可新增只在公开个人记忆出现时追加的
`PUBLIC_PERSONAL_MEMORY_SYSTEM_ADDENDUM`。推荐后者：没有公开个人记忆时不增加额外 token，也能清晰
测试“跨用户不得套用”这一条新规则。

## 18. 日志、故障与并发

### 18.1 日志白名单

允许新增稳定字段建议只有：

```text
public_entry_count
subject_count
```

继续允许既有 `memory_id`、`revision`、`scope` 和 `status`。禁止 username、owner key、查询词、匹配
正文、文件路径和完整 subject 对象进入日志。

### 18.2 单写者与快照

公开与私人 mutation 共用 `MemoryService` 的单个 `asyncio.Lock`。公开文件也使用同目录临时文件、独占
创建、flush/fsync、摘要比对和 `os.replace`。公开快照和 username 索引用一次引用替换发布给读者。

### 18.3 软故障

| 故障 | 行为 |
| --- | --- |
| 公开目录不可读 | 本轮无公开个人记忆；聊天继续 |
| 某个公开文件损坏 | 该 owner 使用最后有效快照；冷启动无快照则省略 |
| 用户搜索失败或限频 | 仅省略需要查询验证的文本命中；稳定参与者仍可使用 |
| 公开条目预算不足 | 整条跳过，不截断 |
| publish/unpublish 写失败 | 命令回稳定失败，私人来源不改 |
| forget/clear 的撤回步骤失败 | 不执行私人删除 |
| forget/clear 撤回成功、私人删除失败 | 内容已不公开，私人来源保留；重放继续完成 |

记忆故障不得影响 `/livez`、`/readyz`、评论 `alive`、SSE 水位或聊天事件终态。

## 19. 测试方案

### 19.1 Codec 与服务

新增或扩展：

- `tests/test_memory_codec.py`：公开文档 round-trip、严格字段、重复 username/ID/key、容量、时间戳、
  UTF-8、文件上限和确定性渲染；
- `tests/test_memory_service.py`：publish/unpublish、noop、not_found、full、secret、外部编辑冲突、刷新、
  索引、LRU、公开条目排序；
- 模拟 forget/clear 在两个步骤之间失败并重放，证明先撤回后删除；
- 公开条目阻止显式与自动 AI 更新；
- public 文件损坏时普通聊天与私人读取仍可用。

### 19.2 命令与控制器

- `tests/test_memory_commands.py`：三个新增命令、大小写、参数缺失、非法 ID、额外参数、未知子命令；
- `tests/test_memory_controller.py`：门禁、无 AI 调用、幂等先于读取、固定文案、列表标记、状态计数、
  `/memory off` 不撤回、forget/clear 两阶段顺序。

### 19.3 Subject resolver

新增 `tests/test_memory_subjects.py`：

- `@alice` 与普通 `alice` 都命中；
- `alice2`、`myalice` 不误命中；
- 大小写敏感；
- 当前作者优先于正文，正文优先于文章和剪贴板；
- owner 去重与最多 4 人；
- 当前稳定参与者不调用站点查询；
- 文本匹配必须经 exact ID 校验；
- 改名、用户名重用、重复索引、无结果、多个 exact 结果全部 fail-closed；
- 正负缓存 TTL、容量与本地节流；
- 不扫描模型回答、搜索、KB 和 MCP 输出。

### 19.4 站点客户端

`tests/test_client.py` 使用 `httpx.MockTransport` 覆盖登录、成功 envelope、401 重登、429、响应过大、非法
JSON、缺字段和用户名精确筛选；断言 URL/query、用户 ID 和 username 不出现在日志。

### 19.5 聊天和评论集成

扩展 `tests/test_app.py`、`tests/test_comment_router.py` 与 `tests/test_comment_service.py`：

- 当前发言者的公开记忆在大区/评论出现；
- 同一条私人未公开 sentinel 永不出现；
- DM 不加载第三方公开记忆；
- 当前正文普通用户名、`@username`、直接引用、博客正文、评论文章正文和已展开剪贴板分别命中；
- 超限未提供的博客/文章与未展开剪贴板不命中；
- 短期历史参与者命中，reset 和历史淘汰后不再命中；
- 大区近期消息只有实际选入当轮者才贡献 subject；
- `memory_allowed=false` 时不读 public 文件、不做用户查询；
- 公开资料只在当前轮 user 消息，成功提交的历史不含资料块；
- 多 owner 预算、排序和跨用户标签不串线；
- 评论模型失败、配额和重试语义不变。

### 19.6 安全与回归

扩展 `tests/test_logging_safety.py`：

- 私人和公开正文、username、owner key、用户搜索 query 不出现在日志；
- SQLite schema 与内容没有新增 user ID、username、subject 或正文；
- 公开资料中的提示注入不能改变 system、工具权限或 owner 绑定；
- `memory.enabled=false` 时不创建 `public/`、不扫描目录、不查询用户，旧路径保持关闭态兼容。

完整验证：

```bash
python -m pytest tests/test_memory_codec.py tests/test_memory_service.py -q
python -m pytest tests/test_memory_commands.py tests/test_memory_controller.py -q
python -m pytest tests/test_memory_subjects.py tests/test_client.py -q
python -m pytest tests/test_router.py tests/test_context.py tests/test_app.py -q
python -m pytest tests/test_comment_router.py tests/test_comment_service.py -q
python -m pytest tests/test_logging_safety.py tests/test_store.py -q
python -m pytest tests -q
```

`filterwarnings = ["error"]`，所有命令必须零 warning。

## 20. 按依赖顺序的实施任务

### 任务 1：锁定合同、裁决和披露

**改动文件**：

- `docs/design/INTERFACES.md`
- `docs/design/DESIGN_DECISIONS.md`
- `docs/design/GLOBAL_MEMORY_DESIGN.md`
- `docs/design/GLOBAL_MEMORY_IMPLEMENTATION_PLAN.md`
- `docs/design/SYSTEM_PROMPTS.md`
- 本文状态段

**结果**：正式修订 D-56 与 D-76；定义公开投影不是共同记忆、DM 不读第三方公开记忆、普通用户名与
博客/剪贴板触发、身份精确校验、两阶段删除和新的稳定状态。

### 任务 2：配置、模型与公开 codec

**改动文件**：

- `src/raricy_bot/config.py`
- `config.example.yaml`
- `src/raricy_bot/memory/models.py`
- `src/raricy_bot/memory/codec.py`
- `tests/test_config.py`
- `tests/test_memory_codec.py`

**结果**：公开 Markdown 可以严格读写并确定性 round-trip；三个配置项与交叉预算校验生效；现有私人和
共同格式不迁移、不重写。

### 任务 3：公开存储服务与一致性操作

**改动文件**：

- `src/raricy_bot/memory/service.py`
- `tests/test_memory_service.py`

**结果**：完成 public 路径、快照、刷新、username 索引、publish/unpublish、两阶段 forget/clear、
`public_conflict` 和全部软故障语义；公开读路径从不打开私人文件。

### 任务 4：命令与用户文案

**改动文件**：

- `src/raricy_bot/memory/commands.py`
- `src/raricy_bot/memory/controller.py`
- `src/raricy_bot/texts.py`
- `tests/test_memory_commands.py`
- `tests/test_memory_controller.py`
- `tests/test_texts.py`

**结果**：用户可发布、撤回、列出、删除和清空；所有回复来自 `texts.py`；公开动作不调用 AI；
`/memory off` 如实说明公开状态不变。

### 任务 5：站点精确身份查询与 subject resolver

**改动文件**：

- `src/raricy_bot/site/models.py`
- `src/raricy_bot/site/client.py`
- 新增 `src/raricy_bot/memory/subjects.py`
- `tests/test_client.py`
- 新增 `tests/test_memory_subjects.py`

**结果**：只对公开索引命中的 exact token 做必要查询；稳定参与者免查询；缓存、限流、改名和用户名
重用均有确定行为；任何失败都只省略可选资料。

### 任务 6：短期参与者元数据

**改动文件**：

- `src/raricy_bot/core/context.py`
- `src/raricy_bot/core/router.py`
- `src/raricy_bot/comments/router.py`
- `tests/test_context.py`
- `tests/test_router.py`
- `tests/test_comment_router.py`
- `docs/design/LOBBY_RECENT_CONTEXT_DESIGN.md`

**结果**：成功历史、当前聊天请求、评论请求和实际选入的大区近期消息都能提供不可逆 subject；原始
评论 author ID 不离开 Router；reset 和历史淘汰不留陈旧参与者。

### 任务 7：大区模型路径接入

**改动文件**：

- `src/raricy_bot/app.py`
- `src/raricy_bot/core/context.py`
- `src/raricy_bot/texts.py`
- `tests/test_app.py`
- `tests/test_context.py`

**结果**：在内容引用、博客和近期消息的既有预算判定之后解析 subject；公开个人记忆按独立分组上限
进入当轮 user 数据；DM、能力工具与历史提交不受污染。

### 任务 8：评论模型路径接入

**改动文件**：

- `src/raricy_bot/comments/service.py`
- `src/raricy_bot/app.py`
- `tests/test_comment_service.py`
- `tests/test_app.py`

**结果**：评论作者、评论会话、普通用户名、文章正文和剪贴板可以精确触发；评论请求仍不带原始 user
ID；失败、配额、重试和静默策略保持原样。

### 任务 9：帮助、使用文档与部署说明

**改动文件**：

- `src/raricy_bot/texts.py`
- `docs/usage/USAGE.md`
- `docs/usage/推文-长期记忆-发布稿.md`
- `docs/usage/DEPLOYMENT.md`
- `docs/README.md`
- 对应文案测试

**结果**：所有用户可见说明准确披露公开范围、普通用户名/博客/剪贴板触发、第三方模型传输、撤回入口、
`/memory off` 与 `/reset` 的非撤回语义。

### 任务 10：安全审计与完整回归

**改动文件**：

- `tests/test_logging_safety.py`
- 必要的既有回归测试
- 本文状态段

**结果**：未公开 sentinel、跨用户绑定、日志/SQLite 红线、崩溃重放和关闭态回退全部有测试；全量
pytest 零 warning；本文改为“已实施”并记录最终验证日期与命令。

## 21. 验收标准

实现满足以下全部条件才算完成：

1. 只有条目所有者能在私聊中公开或撤回自己的 UM-ID。
2. 公开动作不调用 AI，公开的是命令执行时用户私人快照中的确定正文。
3. 大区和评论请求永远不打开 `users/` 私人文件，只读取 `public/`。
4. DM 不加载第三方公开个人记忆。
5. 当前作者、短期参与者、精确 `@username`、普通 username、引用正文、实际提供的博客/评论文章正文、
   已展开剪贴板和实际选入的大区近期消息均按本文规则触发。
6. 不做模糊匹配；文本用户名必须存在公开索引并通过稳定身份校验。
7. 模型回答、搜索、KB、MCP 结果和未实际提供的外部正文不触发。
8. 一轮 owner 数和公开记忆 token 均有独立上限，当前参与者优先。
9. 公开资料只进入当轮 `role="user"`，不进 system 动态文本、不进历史、日志或 SQLite。
10. `/memory unpublic` 撤回后下一轮立即不可用；`forget` / `clear` 先撤回再删除并可崩溃重放。
11. AI 与自动记忆不能静默更新已公开来源；必须先撤回。
12. `/memory off` 不撤回公开条目，回复和帮助文档明确说明这一点。
13. 改名、用户名重用、重复索引、查询失败与损坏文件均 fail-closed，不误绑其他用户。
14. 记忆故障不影响聊天、评论、健康端点、配额、水位与发送恢复。
15. `memory.enabled=false` 时不创建或读取公开目录、不发用户查询，关闭态行为保持兼容。
16. 全量测试通过且没有 warning。

## 22. 上线、迁移与回退

### 22.1 迁移

不修改现有 `common.md`、私人 Markdown 或 SQLite schema。升级后 `public/` 初始为空，所有旧私人条目
保持私有；必须由用户逐条执行 `/memory public <UM-ID>` 才会出现公开投影。因此没有默认公开、批量
迁移或历史授权推断。

### 22.2 上线顺序

1. 先部署支持读写公开投影但保持 `memory.enabled=false`；
2. 在测试目录完成 codec、发布、撤回和崩溃重放验证；
3. 用 `allowlist` 只开放少量账号；
4. 核对 `/help`、公开确认和评论披露；
5. 观察站点用户查询限频与公开上下文 token 使用；
6. 再决定是否切换 `access_mode="all"`。

### 22.3 回退

紧急回退只需设 `memory.enabled=false` 并重启：公开目录不再读取，任何公开条目都不会进入模型请求。
文件保留以便恢复，不代表运行时仍在使用。若用户要求永久撤回或删除，仍必须执行 `unpublic`、
`forget` 或 `clear`；不能把关闭功能当成删除完成。

代码级回退不需要数据库迁移：移除 resolver 与 public provider 装配、保留 Markdown 文件即可。重新
启用前应先确认文案与实际行为一致，不能在功能仍生效时恢复成“公开场景绝不使用个人记忆”的旧说明。

## 23. 已确认决策摘要

- 公开个人记忆只在大区和评论使用，不在 DM 加载第三方公开条目。
- 普通用户名文本可以触发，`@username` 不是必要条件。
- 博客正文、评论文章正文和已展开公开剪贴板中的用户名可以触发。
- 不支持模糊搜索；username 大小写敏感、按完整合法 token 匹配。
- 模型回答、搜索、KB 和 MCP 输出不触发。
- 撤回命令为 `/memory unpublic <UM-ID>`。
- 已公开来源不得被 AI 或自动记忆静默更新。
- `all_user` 受众首版继续服从现有记忆接入门。
- 用户自己的显式发布不需要管理员审批。
- 独立公开投影优先于在私人文件中添加可见性标志，以保留公开路径不读私人文件的隔离边界。
