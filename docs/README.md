# 文档索引

现行行为由代码和测试验证；站方 API 契约优先于本项目设计。发现代码与契约不一致时，
先核对相关决策和消费者，不能把差异自动视为新的约定。

## 按任务查阅

| 任务 | 文档 |
|---|---|
| 快速启动、功能概览 | [根 README](../README.md)、[配置示例](../config.example.yaml) |
| 开发约定 | [AGENTS.md](../AGENTS.md) |
| Light 独立发行版、首次配置与后台静默运行（L0–L5 已实现；干净 Windows 与真实站点验收待做） | [LIGHT_EDITION_DESIGN.md](design/LIGHT_EDITION_DESIGN.md)：完整版保留，包含架构、配置/凭据、进程控制、API、打包与验收设计 |
| 跨模块行为与代码入口 | [INTERFACES.md](design/INTERFACES.md) |
| 本次代码审查四项修复的问题、理由与替代关系 | [DESIGN_DECISIONS.md](design/DESIGN_DECISIONS.md) 的 D-115–D-118；已实施，一次性计划见[归档索引](ARCHIVE.md) |
| 行为的理由、例外与取舍 | [DESIGN_DECISIONS.md](design/DESIGN_DECISIONS.md)（D-1–D-129） |
| 系统提示词规范 | [SYSTEM_PROMPTS.md](design/SYSTEM_PROMPTS.md)；文档不参与运行，配置提示词须同步到部署配置，静态附加说明须同步 `texts.py`；第三类静态来源（表情包，由已校验的部署配置渲染）见 §1.10、D-119 |
| 用户命令、隐私说明、定时发文操作与待验收项 | [USAGE.md](usage/USAGE.md) |
| Light 桌面版安装、升级、排障与未验收项 | [LIGHT.md](usage/LIGHT.md) |
| 部署、升级、恢复、排障 | [DEPLOYMENT.md](usage/DEPLOYMENT.md) |
| Exa 多 Key 池与本地知识库 | [EXA_POOL_AND_KB.md](usage/EXA_POOL_AND_KB.md) |
| 尚未定案的线上异常 | [INCIDENTS.md](usage/INCIDENTS.md)；保留取证清单，不随已完成设计归档 |
| 已完成设计、实施计划与旧版契约 | [归档索引](ARCHIVE.md)；仅作历史参考 |

## materials/：上游契约与资料

站方文件保留原样，只按上游新版本整份替换并更新此表。发布文案与知识库来源说明是项目资料，
不具有 API 契约效力。

| 文件 | 用途与版本 |
|---|---|
| [chat-bot.md](materials/chat-bot.md) | 聊天 API 契约，上游 `5eace12`（2026-09-18） |
| [comment-bot.md](materials/comment-bot.md) | 评论、通知与限频契约，同上 |
| [SITE_DOCS_SOURCE.md](materials/SITE_DOCS_SOURCE.md) | 知识库的许可、锁定版本、逐篇来源与更新复核步骤；改 `knowledge/` 前阅读 |
| [推文-Logos-发布稿.md](materials/推文-Logos-发布稿.md) | 机器人形象发布文案 |
| [推文-长期记忆-发布稿.md](materials/推文-长期记忆-发布稿.md) | 长期记忆 Beta 的对外发布文案 |

内容引用的补充读接口见 D-50；定时发文普通用户接口例外见 D-106。上游提供某项 API，
不代表机器人已经支持对应功能。新取得但尚未核对来源的材料不据此列为现行契约。

## 维护规则

- `design/` 保留仍需维护的行为契约、决策和提示词；签名、字段与默认值直接链接代码，避免手抄副本。
- 变更行为时同步相关 § / D 条目、测试和使用说明；明确标出替代关系，不让新旧规则同时生效。
- 设计与计划完成后移到 `archive/`，更新归档索引；未完成的验收、运维待办先移入 `usage/`。
- 保留 §0–§53 与 D 编号。旧版细分节号、实施期 R/A 编号通过归档索引追溯，不再扩写任务交接历史。
- 现有忽略策略不变：`archive/`、`AGENTS.md`、`CLAUDE.md`、`tests/` 等是本地文件。
  归档不等于提交或备份；曾入库的版本可从 Git 恢复，未入库材料须保留本地副本。

构建期 MCP 版本唯一来源是 [mcp-tools.package.json](../mcp-tools.package.json)（D-92）；
[capture_mcp_fixture.py](../tools/capture_mcp_fixture.py) 仅供开发取样，运行期不导入。
