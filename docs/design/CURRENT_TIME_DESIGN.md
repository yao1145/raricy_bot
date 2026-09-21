# 模型当前时间上下文：设计

## 0. 文档状态

| 项目 | 内容 |
| --- | --- |
| 状态 | 待评审（方案已与维护者确认，尚未实施） |
| 日期 | 2026-09-21 |
| 目标 | 让模型在每一轮请求里知道当前的日期、星期与时刻 |
| 上游约束 | 不新增站点 API；不改 `docs/materials/` 的站方材料 |
| 兼容范围 | 路由、配额、去重、发送、历史提交、记忆与能力授权全部不变 |
| 落地后 | 本文属一次性设计，按 `docs/README.md` 的维护规则移入 `docs/archive/` |

已确认的三项取舍：**放在 system**；覆盖**聊天、评论、定时发文**（记忆撰写器除外）；**固定
UTC+8，不做配置项**。

## 1. 背景与目标

现在送进模型的 system 只有两类内容：`config.yaml` 的 `system_prompt`，以及 `texts.py` 里
按轮次条件追加的静态附加说明。两者都不含运行时数据，模型因此没有任何时间锚点 ——
被问「今天星期几」「现在几点」只能猜，写「今天是……」这类句子时也无从核对。

本功能只做一件事：在送进模型的 system 末尾追加一行当前时间。

## 2. 明确不做的事情

- **不做模型可调用的时间工具。** 当前架构「一条消息最多一个能力」（D-39），再加一个本地
  能力会挤占 MCP 能力的名额；而且每次回复都要多一次工具往返，延迟与 token 都不划算，
  算出来还是同一个值。
- **不加配置开关，不做时区配置。** 固定 UTC+8，与 `blog/planner.py` 的 `UTC8` 同源。
  站点是中文社区，签到、发文日期这些站内日历本来就走 UTC+8；可配时区会把「站内日历」
  与「模型看到的日历」拆成两套，而分歧只会出现在跨日边界上，最难排查。
- **不给历史轮次打时间戳。** 时间只出现在每轮 system 里，模型看到的是「现在」。
  回看旧轮时它不知道那些话发生在何时 —— 这是 system 方案的固有代价，见 §5。
- **不加到记忆撰写器**（`memory/writer.py`）。它做的是数据整理，时间用不上，
  只会白占 `max_context_tokens` 的预算。
- **不改 `config.yaml` 的 `system_prompt` 正文。** 静态文本里写死一个时间没有意义；
  不碰正文，`system_prompt_sha256` 因此保持不变。

## 3. 用户可见行为

- 聊天（大区与私聊）、评论回复、定时发文正文里，模型都能说出正确的日期、星期与时刻。
- 时间刻度是 UTC+8，与站内日历一致。
- 没有新命令，`/help` 文案不变。
- 模型只获得「知道现在」这一件事，不因此获得任何行动能力：定时发文仍由站内调度决定。

## 4. 口径：时区、格式与文案归属

片段形如：

```text
当前时间：2026-09-21 14:30 周一
```

- **到分为止**，不给秒。秒在对话里没有用途，只会增加无谓的差异。
- **不加 `(UTC+8)` 后缀。** 它会诱导模型向用户解释时区换算，而用户关心的就是站内时间。
- **定长**：`YYYY-MM-DD HH:MM` 加 `周一`–`周日` 之一，宽度恒定。这一点对预算有实际意义，见 §7。
- **文案归属**：标签 `当前时间：` 与七个星期名放 `texts.py`（工程约定：会展示给用户的字符串
  集中在该模块）；渲染函数放新模块 §6。两者都不做字符串格式化以外的加工。

## 5. 放置位置：system 的最后一段，以及一次明确的红线例外

现有约定是「system 附加说明必须静态、不含占位符」（D-24、`SYSTEM_PROMPTS.md` §1.6、
`INTERFACES.md` §5）。本功能引入**唯一**的动态 system 片段。

**为什么这条例外是安全的。** 那条红线的目的是「用户可控的内容不得进 system」
（`INTERFACES.md` §19 第 2 条）。时间由代码从进程时钟生成，用户完全不可控，
不构成那条通道。反过来，把它放进 `role="user"` 也不是稳妥的做法：模型会把它当成
用户说的话，而且它会跟着 `append_exchange` 进历史，每轮堆一个时间戳。

**必须写进契约的三条约束**，防止例外被逐步放宽：

1. 片段只由 `now()` 的返回值渲染，**不含任何请求、用户、记忆或工具数据**；
   渲染函数除时间戳外不接受任何入参 —— 它没有位置能装进别的东西。
2. 它固定是 system 的**最后一段**，排在 `system_prompt`、所有静态附加说明与记忆说明之后。
3. 此后任何新增的动态 system 内容，都必须重新走一次决策记录，不得援引本次例外。

### 5.1 备选方案与否决理由

| 方案 | 否决理由 |
| --- | --- |
| 拼进当前轮 `role="user"` 正文（像大区近期消息的块头） | 模型会当作用户内容；要么进历史、每轮堆一个时间戳，要么与「历史只留问题本身」（D-22 的 `_pending_turn`）相冲突 |
| 做成模型可调用的本地能力 | 占用「一条消息最多一个能力」的名额（D-39）；每轮多一次往返 |
| 写进 `system_prompt` 静态正文 | 静态文本里没有值，等于没做 |
| 只在聊天路径做 | 评论与发文同样会写「今天」，同一套口径应当一致 |

## 6. 架构与改动点

### 6.1 新模块 `src/raricy_bot/time_context.py`

```python
UTC8 = timezone(timedelta(hours=8))          # 与 blog/planner.py 同源

def render_current_time(now: float) -> str:  # -> "当前时间：2026-09-21 14:30 周一"
def append_current_time(system: str, now: float) -> str
```

`append_current_time` 在 `system` 非空时于末尾接 `"\n\n" + 片段`，为空时只返回片段
（评论的回退分支允许 system 为空，见 §6.2）。

依赖方向固定为 `texts ← time_context`：只依赖标准库与 `texts`，不 import 任何业务模块，
四个调用点（含评论与 `blog/` 两个子系统）都能安全引用，不引入新的依赖环。

### 6.2 调用点

| 调用点 | 改动 |
| --- | --- |
| [core/context.py](../../src/raricy_bot/core/context.py)（聊天） | `ContextManager.__init__` 新增 keyword-only `now: Callable[[], float] = time.time`；`build_messages` 在 system 全部组装完成后追加 |
| [app.py:294](../../src/raricy_bot/app.py#L294)、[app.py:389](../../src/raricy_bot/app.py#L389) | 两个 `ContextManager` 构造沿用默认时钟，不必改动；测试需要固定时间时另行注入 |
| [comments/service.py](../../src/raricy_bot/comments/service.py) | 走 `build_messages` 的路径自动获得；**无 `context_manager` 的回退分支**（直接拼 `system` 的那条）手动调用 `append_current_time`；默认构造的 `ContextManager` 传入 service 已有的 `self.now`，两条路径共用同一个时钟 |
| [blog/writer.py](../../src/raricy_bot/blog/writer.py) | `BlogWriter` 新增 keyword-only `now`；`_messages` 由 `@staticmethod` 改为实例方法 |
| [memory/writer.py](../../src/raricy_bot/memory/writer.py) | **不改** |

聊天与评论两条主路径都经由 `ContextManager`，因此时间只在那一处组装；其余调用点各自
调用同一渲染函数，不存在第二份实现。

## 7. 预算

片段 24 字符、按 `text_utils.estimate_tokens` 的口径 12 token。

- **聊天**：`build_messages` 把片段计入 `base_tokens`（与 `system_addendum` 同样单独估算，
  避免先拼接再估算少算非 CJK 分段的取整项）。`select_recent_suffix` 必须用**同一个私有方法**
  取片段 —— 否则 §45.2 的 S1 契约（S1 必须是 `build_messages` 选中项的上界）会分叉。
  片段定长，两次读取之间跨越分钟边界也不会造成预算偏差。
- **评论**：同上，走同一个 `ContextManager`。
- **定时发文**：`blog.write` 的 `max_input_tokens` 严格检查（§53.9）会多算这一点；
  预算卡得极紧的部署需要留意，默认值下可忽略。
- 评论与发文的正文长度限制都在输出侧，不受影响。

## 8. 时钟注入与测试

时钟一律可注入（`AGENTS.md`：时钟、sleep、random 可注入）：
`ContextManager(now=...)`、`CommentService(now=...)`（该参数已存在）、`BlogWriter(now=...)`。

测试清单：

- 新建 `tests/test_time_context.py`：固定时间戳的精确输出；跨 UTC 午夜的 UTC+8 换算
  （例如 UTC `2026-09-21 16:30` 应渲染成 UTC+8 的次日 `00:30`）；七个星期名；
  `append_current_time` 在空 system 下的行为；片段定长。
- `tests/test_context.py`：注入固定 clock 后，system 的末段恰为该片段；紧预算下
  `select_recent_suffix` 的 S1 与 `build_messages` 的选中项仍满足后缀关系。
- `tests/test_comment_service.py`：两条装配路径（有 / 无 `context_manager`）各断言 system 末段。
- 定时发文的撰写器测试：断言 system 末段。
- 既有断言 system 全文的用例需要同步更新（预计集中在 `test_context.py`、
  `test_comment_service.py`、`test_app.py`、`test_memory_context.py`）。
- 不访问真实站点与真实模型，全部用替身。

## 9. 契约与文档同步清单

| 文件 | 动作 |
| --- | --- |
| `docs/design/INTERFACES.md` | 新增 §54；§5（`texts.py`）补文案归属；§11（`core/context.py`）补 `now` 参数与片段位置 |
| `docs/design/DESIGN_DECISIONS.md` | 新增 D-114（唯一动态 system 片段及其边界）；开头索引段落补上新的编号区段 |
| `docs/design/SYSTEM_PROMPTS.md` | §1.2 预算加一笔；§1.6 说明「静态」从此有一处明确例外并指向 D-114 |
| `src/raricy_bot/texts.py` | 标签与星期名常量 |
| `src/raricy_bot/time_context.py` | 新模块 |
| `docs/usage/USAGE.md` | §4「我会自动读什么」补一条：每轮会带上当前时间（UTC+8） |
| 根 `README.md` | 视「在聊天里怎么用它」一节的口径决定是否补一句 |
| `tests/` | 见 §8（`tests/` 不入库，本地维护） |

## 10. 验收标准

1. 聊天、评论、定时发文三条路径送出的 messages，system 末段都是当前时间片段。
2. 注入 `now` 后输出完全确定，可逐字符断言。
3. 除「system 全文断言」这类必须同步的用例外，既有测试全部通过，且不新增 warning
   （`filterwarnings = ["error"]`，不以屏蔽警告代替修复）。
4. 紧预算下 `select_recent_suffix` 与 `build_messages` 的后缀关系仍然成立。
5. 全部为离线验证；不把离线测试称为真实站点验收。
