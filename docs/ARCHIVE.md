# 归档索引

归档用于追溯当时的设计，不是现行行为依据。现行入口见 [文档索引](README.md)。
`docs/archive/` 按现有 `.gitignore` **不入库**；这里的链接指向本地副本，新克隆可能没有这些文件。
不删除未入库的唯一副本；需要跨机器保存时另行备份。

## 2026-09-20 整理

接口文档改为行为约束与代码导航，决策记录保留全部 D 编号、理由和修订关系；完整旧稿逐字节保留：

| 文件 | 内容 |
|---|---|
| [INTERFACES.md](archive/2026-09-20/INTERFACES.md) | 精简前 §0–§53 全文；旧代码注释中的细分节号在此查询 |
| [DESIGN_DECISIONS.md](archive/2026-09-20/DESIGN_DECISIONS.md) | 精简前 D-1–D-110 全文、原文引述、实施期 A/R 裁决映射 |
| [AGENTS.md](archive/2026-09-20/AGENTS.md)、[CLAUDE.md](archive/2026-09-20/CLAUDE.md) | 原 AI 约定，含已过时规则，不再生效 |
| [DOCS_README.md](archive/2026-09-20/DOCS_README.md) | 整理前索引与归档来源记录 |
| [manifest.json](archive/2026-09-20/manifest.json) | 本次七份原文件的 SHA-256、字节数及归档路径 |
| [BLOG_PUBLISH_DESIGN.md](archive/BLOG_PUBLISH_DESIGN.md) | 定时发文设计，已实现、默认关闭 |
| [BLOG_PUBLISH_IMPLEMENTATION_PLAN.md](archive/BLOG_PUBLISH_IMPLEMENTATION_PLAN.md) | 已完成 T0–T7 的实施交接计划 |
| [SECURITY_AND_ERROR_LOGGING_PLAN.md](archive/SECURITY_AND_ERROR_LOGGING_PLAN.md) | 密钥安全修复、诊断增强与错误日志永久保留；阶段 A–D 已实施，验收项见部署手册 |

两份发文稿原样移动；归档目录保留接口与决策的跳转页，使原相对链接继续可用。历史稿中的“下一步”“待实现”和旧规则均保留为当时记录，不代表当前待办。
**定时发文真实站点验收尚未完成**，验收清单已留在 [USAGE.md §2.4](usage/USAGE.md)。
**永久错误归档的上线验收同样尚未完成**：本地编码与离线检查已完成，但实际部署路径、宿主容量、
备份目标与负责人、告警接收方式必须在上线时落实，清单见
[DEPLOYMENT.md §10.6–§10.7](usage/DEPLOYMENT.md#106-永久错误归档)。

旧索引、接口、决策和两份发文稿这五个已入库文件，可用整理前提交
`435d72eab3e3992402cd691956bbe4bef8bcc3ee` 与原路径恢复，例如：

```bash
git show 435d72eab3e3992402cd691956bbe4bef8bcc3ee:docs/design/INTERFACES.md
git show 435d72eab3e3992402cd691956bbe4bef8bcc3ee:docs/design/BLOG_PUBLISH_DESIGN.md
```

## 此前归档

表中提交号指移出前可恢复的版本，原路径为 `docs/design/<文件名>`。
“仅本地”表示该稿从未入库，不能依靠 Git 恢复。

| 主题 | `archive/` 中的文件 | Git 来源 |
|---|---|---|
| 初版背景与实现报告 | `BACKGROUND.md`、`IMPLEMENTATION_REPORT.md`、`CODE_REVIEW_2026-09-11.md` | 仅本地；审查问题已在 `0fd7026` 修复 |
| 大区共享会话、评论机器人 | `LOBBY_SHARED_CONVERSATION_DESIGN.md`、`COMMENT_BOT_DESIGN.md` | 仅本地 |
| 图片输入 | `CHAT_IMAGE_INPUT_DESIGN.md`、`CHAT_IMAGE_INPUT_PLAN.md` | `e834035` |
| MCP 搜索、Exa 池与 KB | `MCP_CHAT_SEARCH_DESIGN.md`、`EXA_ACCOUNT_POOL_AND_KB_DESIGN_PLAN.md` | `f47ce1d` |
| 博客引用 | `CHAT_BLOG_QUOTE_DESIGN.md`、`CHAT_BLOG_QUOTE_PLAN.md` | `d3c9599` |
| 长期记忆 | `GLOBAL_MEMORY_DESIGN.md`、`GLOBAL_MEMORY_IMPLEMENTATION_PLAN.md` | `980b858` |
| 大区近期消息 | `LOBBY_RECENT_CONTEXT_DESIGN.md` | `5aa2cc1`；本地另保留移出时的节号修正 |
| 站点知识库内容 | `SITE_DOCS_KB_DESIGN.md` | `e834035` |
| 公开个人记忆设计 | `PUBLIC_PERSONAL_MEMORY_DESIGN.md` | `980b858` |
| 公开个人记忆实施约束 | `PUBLIC_PERSONAL_MEMORY_PLAN.md`、`PUBLIC_PERSONAL_MEMORY_PLAN-constraints.md` | 仅本地；从原 `.superpowers/sdd/` 移出，原目录已删除 |

记忆实施期的两套 R 编号不是同一套：长期记忆 R8–R16 对应 D-68–D-76；
公开个人记忆 R1–R14 对应 §39–§52 / D-96–D-103。完整逐项映射留在旧决策记录附录。
