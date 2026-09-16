# 文档索引

本目录按用途分三类，另有一个归档目录。

> 注意：`archive/` 被仓库根的 `.gitignore` 忽略，**归档文件不在版本控制里**，
> 重命名或删除都没有 git 兜底。`materials/`、`usage/`、`design/` 与本文正常入库。
>
> 其中六份例外：`CHAT_IMAGE_INPUT_DESIGN.md` 与 `CHAT_IMAGE_INPUT_PLAN.md`（最后提交
> `e834035`）、`MCP_CHAT_SEARCH_DESIGN.md` 与 `EXA_ACCOUNT_POOL_AND_KB_DESIGN_PLAN.md`
> （`f47ce1d`）、`CHAT_BLOG_QUOTE_DESIGN.md` 与 `CHAT_BLOG_QUOTE_PLAN.md`（`d3c9599`）
> 都是 2026-09-15 从 `design/` 移进来的，它们**进过版本控制**，需要时用
> `git show <上面的提交号>:docs/design/<文件名>` 取回全文。归档目录里其余五份从未提交过。
> 移动之后它们不再被 git 跟踪——归档就是「退出仓库」，本目录里剩下的永远是最新一份。

## 现行口径在哪里

运行行为以代码与测试为准，其次是仓库根的 `README.md`。`materials/` 是站方所写的上游契约，
必须遵守；`design/` 是内部契约与规范来源，改代码前先读；`archive/` 是历史，不要据此改代码。

## materials/ —— 资料

站方契约与项目对外资料：**只读不改**——它们是站方文件的原样副本，本地不编辑。
要更新就按上游新版本**原地替换**，并把新提交号记在下面这张表里。

| 文件 | 是什么 | 权威性 |
|------|--------|--------|
| `materials/chat-bot.md` | 上游站点的聊天 API 契约，站方所写。同步于上游 `374c94c`（2026-09-15） | **不可变**。与其他任何文档冲突时以它为准，本项目只准用它 |
| `materials/comment-bot.md` | 上游站点的博客评论 API 契约，站方所写。同步于上游 `374c94c`（2026-09-15） | **不可变**。评论读写、通知与限频冲突时以它为准 |
| `materials/推文-Logos-发布稿.md` | 机器人形象的对外发布文案 | 与代码无关，仍在用 |
| `materials/SITE_DOCS_SOURCE.md` | `knowledge/` 知识库的来源留痕：上游仓库与许可、锁定的提交 SHA、逐篇对照表、上游更新时的人工复核步骤 | **知识库内容的溯源口径**。全量重写无法自动跟随上游，改 `knowledge/` 前先读它 |

## usage/ —— 使用

给人照着做的操作手册。

| 文件 | 是什么 |
|------|--------|
| `usage/USAGE.md` | 写给**跟机器人聊天的人**：怎么唤起、能问什么、为什么有时不回、隐私与额度。第一部分可直接发布到站点，第二部分才是给维护者看的 |
| `usage/DEPLOYMENT.md` | 从零到上线的远程 Linux 部署指南。正文以 Ubuntu/Debian 为例，**附录 A** 是 Rocky Linux 9 / RHEL 系的差异 |
| `usage/EXA_POOL_AND_KB.md` | 两项可选能力的配置与使用说明：Exa 多 Key 池、本地 Markdown 知识库。字段表、运行期行为、日志、验收与排障都在这里 |
| `usage/推文-长期记忆-发布稿.md` | 长期记忆（Beta）的对外发布稿，面向站内所有用户，可直接发布到站内博客：灰度口径、命令、范例与边界；管理员命令单列一节。部署侧见 `usage/DEPLOYMENT.md` §4.2.2，内部契约见 `design/INTERFACES.md` §26 … §37 |

## design/ —— 设计与规范

本项目的内部契约与规范来源。改代码前先读这里。

| 文件 | 是什么 | 权威性 |
|------|--------|--------|
| `design/INTERFACES.md` | 本项目内部契约：锁定的签名、字段名、默认值、判定谓词 | 改签名前先查全部消费者 |
| `design/DESIGN_DECISIONS.md` | 设计未明确处的裁决记录（D-1 … D-93） | 觉得某处行为怪，先读对应条目——不少「看起来像 bug」的选择是刻意的 |
| `design/SYSTEM_PROMPTS.md` | 系统提示词的正式来源 + 站点速查表 | 提示词正文的权威副本，**但它不参与运行**：改完必须手动同步到 `config.yaml` |
| `design/SITE_DOCS_KB_DESIGN.md` | `knowledge/` 知识库**内容侧**的取材范围、删减规则、问答写法、留痕与验收口径 | 已实施（2026-09-15，15 篇 / 220 条）。只约束内容，运行行为见 `usage/EXA_POOL_AND_KB.md` |

## archive/ —— 归档（历史，不是现行口径）

实现前的设计稿与一次性产物。**不要据此改代码**；需要追溯当时的判断时再翻。

| 文件 | 是什么 |
|------|--------|
| `archive/LOBBY_SHARED_CONVERSATION_DESIGN.md` | 大区多人共享对话的实现设计（已实现，行为以 `design/` 与代码为准） |
| `archive/COMMENT_BOT_DESIGN.md` | 评论发现、通知匹配、队列与恢复的实现设计（已实现，行为以 `design/` 与代码为准） |
| `archive/BACKGROUND.md` | 项目最初的《设计文档（评审稿）》 |
| `archive/CODE_REVIEW_2026-09-11.md` | 外部代码审查提出的四个问题（两 P1、两 P2），均已在提交 `0fd7026` 修复 |
| `archive/IMPLEMENTATION_REPORT.md` | 首个可用版本的实现完成报告（含修复过程与验证方式） |
| `archive/CHAT_IMAGE_INPUT_DESIGN.md` | 聊天图片输入（识图）的实现前设计稿。功能已上线：合同见 `design/INTERFACES.md` §20，裁决见 D-28 … D-30，测试见 `tests/test_vision.py` |
| `archive/CHAT_IMAGE_INPUT_PLAN.md` | 上一条的实现计划与任务清单（同上，已上线；行为以 `design/` 与代码为准） |
| `archive/MCP_CHAT_SEARCH_DESIGN.md` | 聊天区 Exa MCP 搜索的设计与实现规划。已实现并通过验收：合同见 `design/INTERFACES.md` §21，配置与排障见 `usage/EXA_POOL_AND_KB.md`（2026-09-15 从 `design/` 归档） |
| `archive/EXA_ACCOUNT_POOL_AND_KB_DESIGN_PLAN.md` | Exa 授权密钥池与 `/kb` 本地知识库的设计及实施计划（含 P0 处置记录）。已实施：合同见 `design/INTERFACES.md` §22/§23，裁决见 D-36 … D-46（2026-09-15 从 `design/` 归档） |
| `archive/CHAT_BLOG_QUOTE_DESIGN.md` | 聊天区引用内容的实现前设计稿：被引用博客的正文进模型、引用边角的标记。已上线：裁决见 D-47 / D-48 / D-54，合同见 `design/INTERFACES.md` §24（2026-09-15 从 `design/` 归档） |
| `archive/CHAT_BLOG_QUOTE_PLAN.md` | 上一条的实现计划与任务清单（同上，已上线；行为以 `design/` 与代码为准） |

## 仓库根（不在本目录）

| 文件 | 是什么 |
|------|--------|
| `../README.md` | 快速开始、命令一览、部署要点。**现行口径之一** |
| `../mcp-tools.package.json` | 构建期安装的三个 stdio MCP 服务器及其版本，`overrides` 裁决 SDK 提升冲突。**唯一的安装口径**：Dockerfile 与 `usage/DEPLOYMENT.md` §12 都装它，不要退回逐个 `npm install <包名>`（理由见 D-92） |
| `../CLAUDE.md`、`../AGENTS.md` | 给 AI 助手的项目约定（两份内容一致） |
| `../tools/capture_mcp_fixture.py` | **仅开发用**的上游取样脚本：调一次真实 MCP 工具并转储成 fixture，用来校准解析器。运行期绝不 import；只能写进 `tests/fixtures/`，写出的内容先过脱敏。什么时候用它见 `usage/DEPLOYMENT.md` §4.1.2 |
