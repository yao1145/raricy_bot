# Raricy Bot Light 打包

Light 发行版的独立构建描述（LIGHT_EDITION_DESIGN §4.4、§14、§15）。仓库只维护
一份源码：构建时由 `tools/build_light.py` 把 Launcher 整包与 Light 闭包复制进临时
staging，检查无 MCP 依赖后构建，不长期维护第二份业务代码。

## 隔离规则

- 根 `pyproject.toml` 始终是完整版（含 Linux/Docker 部署）的构建入口，不引入
  Light 专属依赖；本目录是 Light 的唯一构建描述。
- Light 与完整版共享 `raricy_bot` 导入命名空间，**不得安装到同一 Python 环境**；
  开发、CI 与发行各自使用独立环境。
- 运行依赖：Light 闭包用到的 `httpx`、`openai`、`PyYAML`、`aiohttp`，凭据库
  `keyring`（只接受系统安全后端，§7）与平台层绑定 `pywin32`（约束与根 pyproject
  一致，**不**含 `mcp`）。清单由测试钉住：闭包里出现新的第三方导入而没有在此
  声明时直接失败。FastAPI/Uvicorn 在 L3 进入本目录的清单，不进根 pyproject。

## 构建与打包

发行构建是「确定性 staging → 冻结 → 打包」三步（[D-129](../../docs/design/DESIGN_DECISIONS.md#d-129)），
由 `tools/build_light.py` 串起，在仓库根执行：

```bash
python tools/build_light.py                      # 只生成 staging 与 manifest
python tools/build_light.py --pyinstaller        # staging 后冻结为 onedir 应用
python tools/build_light.py --pyinstaller --zip  # 冻结后打出 ZIP 与 .sha256（发行用）
```

- 构建环境需要 Python >=3.12、本目录 `pyproject.toml` 声明的 Light 运行依赖，以及 PyInstaller
  （脚本按 `python -m PyInstaller` 调用当前解释器）。**不需要 Node**：前端产物已入库（D-129）。
  按上面的隔离规则，这个环境不与完整版共用。
- `--staging DIR` 可改 staging 目录（默认 `build/light-staging`）。已存在的目录必须带
  `.raricy-light-staging` 标记才会被清理重建；指向仓库根、祖先目录或源码树的路径一律拒绝。
- staging 源码树可复现（逐文件 sha256 记在 `manifest.json`）；ZIP 本身**不是**字节可复现的
  —— `build-info.json` 含构建时间，PyInstaller 产物也与构建平台绑定。校验和用于核对「拿到
  的包就是构建出的那个包」，不是用于比对两次构建。

产物（以默认 staging 目录为例）：

| 路径 | 内容 |
|---|---|
| `build/light-staging/src/`、`manifest.json` | Light 闭包源码树与逐文件 sha256 清单 |
| `build/light-staging/build-info.json` | 版本、IPC 协议版本、Python 版本、依赖清单、文件数、整包校验和 |
| `build/light-staging/dist/RaricyBotLight/` | 冻结应用（onedir + windowed，自带运行时） |
| `build/light-staging/RaricyBotLight-<版本>-win64.zip` | 交付给用户的 ZIP |
| `build/light-staging/RaricyBotLight-<版本>-win64.zip.sha256` | 上一步 ZIP 的 SHA-256 |

### 生成 ZIP 与对应的 SHA-256

`--zip` 必须与 `--pyinstaller` 写进同一次调用：脚本每次运行都重建 staging（清掉旧目录再
复制），所以没有「对已有冻结产物补打一次包」这种用法。

```bash
python tools/build_light.py --pyinstaller --zip
```

它只收录 `dist/RaricyBotLight/` 整个目录，条目都落在 `RaricyBotLight/` 之下；staging 根的
`build-info.json` 额外收录为 `RaricyBotLight/build-info.json` —— 用户保留、升级时整体替换的
就是这个目录，构建信息随它一起走。缺冻结产物或构建信息时直接失败，不发不合格的包。ZIP 与
`.sha256` 的文件名里，版本取自本目录 `pyproject.toml` 的 `version`。

打完包立刻对 ZIP 算 SHA-256，写成同目录、同名的 `.sha256` 文件，格式是 `sha256sum` 的两空格
格式（一个文件对应一个包）：

```
<64 位十六进制摘要>  RaricyBotLight-0.1.0-win64.zip
```

核对（在 ZIP 所在目录执行，任选一种）：

```powershell
Get-FileHash -LiteralPath '.\RaricyBotLight-0.1.0-win64.zip' -Algorithm SHA256
```

```bat
certutil -hashfile RaricyBotLight-0.1.0-win64.zip SHA256
```

```bash
sha256sum -c RaricyBotLight-0.1.0-win64.zip.sha256
```

ZIP 与 `.sha256` 必须成对分发；对不上时不要分发，也不要只重发 `.sha256`，重新构建整包。
`build-info.json` 里的 `manifest_sha256` 只覆盖 staging 源码树，**不含**冻结产物与 ZIP 自身，
发行包完整性以旁挂的 `.sha256` 为准。用户侧的核对步骤见[使用手册](../../docs/usage/LIGHT.md) §1。

### 冻结冒烟

冻结完成后、分发之前跑冒烟，确认发行目录里的程序能自己跑起来（不连站点、不建机器人；
`--zip` 不会替你跑）：

```bash
python tools/smoke_light.py --app-dir build/light-staging/dist/RaricyBotLight
```

覆盖启动、激活、会话兑换、状态、管理页与页面构建产物、系统凭据后端、退出与元数据清理
（LIGHT_EDITION_DESIGN §17.2 可自动化部分）。激活通道与单实例互斥体按当前用户命名，跑之前
先退出本机其它 Light 实例，否则冒烟会安全失败。无 Python/Node/Docker 的干净 Windows 验收
仍须在独立机器上人工执行（见使用手册 §8）。

## 闭包与入口

- staging 闭包 = `raricy_bot` **整包减去** `mcp/`、`blog/` 与完整版 CLI 入口
  `__main__.py`（§4.2、§4.3）。完整版专属的构造经工厂注入 App
  （`assembly.py` 的接缝），Store 需要的发文记录与 UTC+8 日历在包外的
  `blog_records.py`。闭包由 `tools/build_light.py` 从源码树现算，并有
  「无 MCP SDK 环境导入全部模块」的验证测试。
- 冻结入口 `entry_light.py` → `raricy_launcher.main:main`；`--worker` 复用同一
  可执行文件作为 Worker 入口（§10.1 首版选择）。
