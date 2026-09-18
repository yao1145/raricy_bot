# 知识库的来源与刷新

`knowledge/` 下的内容**不是**站方文档的原文，而是依据站方对外文档**重写成的问答条目**，
只放事实、不放实现。因为经过重写，它**无法自动跟随上游**：站点改了文档，本地不会自己变。
本文记录内容是基于哪一版写的、每一篇从哪里来、以及上游更新时怎么复核。

> 运行行为（怎么启用、访问策略、日志、验收）见 [`../usage/EXA_POOL_AND_KB.md`](../usage/EXA_POOL_AND_KB.md)
> 与 [`../design/INTERFACES.md`](../design/INTERFACES.md) §23；内容侧的设计见
> [`../archive/SITE_DOCS_KB_DESIGN.md`](../archive/SITE_DOCS_KB_DESIGN.md)（已归档，不在版本控制里）。

## 锁定的上游版本

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/raricycms/raricy.com` |
| 许可 | MIT |
| 提交 | `9bd397f492ade75d36030a4aafe9a0f11d876d86` |
| 提交时间 | `2026-09-13T16:27:55Z`（本地 +08:00 为 2026-09-14 00:27） |
| 提交标题 | `docs(rate-limit): 对外文档同步每日配额 2000` |
| 取材范围 | `docs/guide/` 全部 13 篇 + `docs/architecture.md`、`README.md`（后两者只用于站点概览） |

上游的 `docs/README.md` 把 `docs/` 分成两层：`guide/` 给玩家和内容创作者，根下给开发与运维。
本知识库取前者；根下的开发运维文档不入库，只在写「站点概览」时从中取材（功能清单与栏目定义）。

**未采用** `raricycms/raricy.com-ng` 仓库的文档。那个仓库是 Next.js 迁移的暂存处，`docs/` 已于
2026-07-16 冻结；其中的《全站限额与频控汇总》在迁移完成后没有跟随站点更新，收录它等于把过时
数字当权威。限额一律以现行 `docs/guide/*` 各篇的「限制一览」为准。

## 复现抓取

逐个抓 `raw.githubusercontent.com` 很慢，用稀疏克隆：

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/raricycms/raricy.com.git src
cd src && git sparse-checkout set docs README.md
git log -1 --format="%H %cI %s"
```

## 对照表

每个 `knowledge/` 文件对应的上游来源、覆盖范围，以及本篇内容是基于哪个提交写的。
下表所有条目的上游版本都等于上表的 `9bd397f`。

| `knowledge/` 文件 | 上游来源 | 覆盖范围 |
|---|---|---|
| `站点概览/聪明山.md` | 站点首页文案 + `README.md` + `docs/architecture.md` §4 路由分布、§6 关键子系统 | 站点定位、栏目与路径、功能清单、账号门槛。**只取面向用户的栏目与功能名，不含技术栈、进程拓扑、数据流、迁移史** |
| `内容创作/云剪贴板.md` | `docs/guide/云剪贴板使用指南.md` | 前置条件、一–九（含限制一览与常见问题） |
| `内容创作/图床.md` | `docs/guide/图床使用指南.md` | 前置条件、一–七（含限制一览与常见问题） |
| `内容创作/投票箱.md` | `docs/guide/投票箱使用指南.md` | 前置条件、一–七（含限制一览与常见问题） |
| `内容创作/内容引用语法.md` | `docs/guide/内容引用语法指南.md` | 前置条件、零–四、六；§五「技术实现简述」按删减规则**蒸馏**为可观察行为（三种类型共用的 50 处上限、同 ID 只请求一次、评论与聊天里只出现站内图床图片） |
| `互动叙事/Cattca入门.md` | `docs/guide/cattca-guide.md` | 全部，含常见问题与完整故事模板 |
| `互动叙事/Cattca脚本语法.md` | `docs/guide/cattca-syntax.md` | 全部，含命令参考（11/11 条命令）、表达式、完整示例与语法速查 |
| `互动叙事/故事模块.md` | `docs/guide/story-module.md` | 全部 |
| `互动叙事/ATÅMAS.md` | `docs/guide/atamas-game.md` | 简介、核心玩法、界面功能、注意事项。「技术架构」与「路由」中的源码级实现已删除，可观察事实（免登录可玩、状态只存浏览器内存、刷新即丢）保留 |
| `联机对战/联机对战通用.md` | `docs/guide/{gomoku,tictactoe,xiangqi,chess,draughts}-online.md` 的公共章节 | 开一局、房号、开始之前、对局轮次、掉线与判胜、观战、再来一局、房间有效期、连接数上限 |
| `联机对战/五子棋.md` | `docs/guide/gomoku-online.md` | 棋盘与胜负、先手、黑棋禁手（三三 / 四四 / 长连）、四三不是禁手、白棋无禁手、单机版 |
| `联机对战/井字棋.md` | `docs/guide/tictactoe-online.md` | 棋盘与胜负、平局、记号、只有联机一种玩法 |
| `联机对战/中国象棋.md` | `docs/guide/xiangqi-online.md` | 棋盘、走法表、飞将、将死 / 困毙 / 和棋 / 长将判负、单机入口 |
| `联机对战/国际象棋.md` | `docs/guide/chess-online.md` | 棋盘、走法表、王车易位 / 吃过路兵 / 兵升变、非法走法、将死 / 逼和 / 和棋、无 AI 对手 |
| `联机对战/国际跳棋.md` | `docs/guide/draughts-online.md` | 棋盘、兵与王的走法、强制吃子与最大吃子、连吃途中不升王、胜负判定、点不动格子的原因 |

## 上游更新时怎么复核

1. 按上面的稀疏克隆命令重新抓取，`git log -1` 取新的提交号。
2. 与本文锁定表里的提交号比对：**一样就不用做任何事**。
3. 不一样时，看新提交动了哪些文件：

   ```bash
   git log --oneline 9bd397f..HEAD -- docs/guide docs/architecture.md README.md
   ```

4. 只对**动过的上游文件**重跑对应的问答条目（对照表里有映射），逐项核对数字、字数上限、频率、
   路径、按钮名。改动大时按 `../archive/SITE_DOCS_KB_DESIGN.md` §6 的写法重写该文件。
5. 更新本文件的提交号与提交时间。

   版本标注只维护在本文件里，`knowledge/` 下的问答条目不写版本、不写 front matter ——
   文件里的任何内容都会参与检索并可能出现在回答中，溯源信息不该混进问答正文。

**为什么必须人工复核**：问答条目是重写的，没有可自动比对的原文。改动过的数字如果没跟上，
机器人会拿旧数字当真话讲——这比知识库里没有这条更糟。

## 公开性

本知识库收录的全部是站方在**公开仓库**与**站上公开页面**里对外的说明，许可为 MIT。
收录前已逐篇复核：不含密钥、Cookie、内网地址、个人信息，也不含站方标为「面向开发与运维」的
实现细节。唯一的例外处理：图床一篇里上游提到旧图床的 IP 直链，正文保留「写死了老 IP 直链的
文章只能手动编辑」这一事实，**去掉了 IP 字面量**——它对回答用户的问题没有贡献。

知识库的命中片段会展示给提问者并发给第三方模型，上线前的目录清点与可见范围确认仍按
[`../usage/EXA_POOL_AND_KB.md`](../usage/EXA_POOL_AND_KB.md) §2.2 执行。
