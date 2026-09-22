# Raricy Bot Light 打包

Light 发行版的独立构建描述（LIGHT_EDITION_DESIGN §4.4、§14、§15）。仓库只维护
一份源码：构建时由 `tools/build_light.py` 把 Launcher 整包与白名单核心模块
复制进临时 staging，检查无 MCP 依赖后构建，不长期维护第二份业务代码。

## 隔离规则

- 根 `pyproject.toml` 始终是完整版（含 Linux/Docker 部署）的构建入口，不引入
  Light 专属依赖；本目录是 Light 的唯一构建描述。
- Light 与完整版共享 `raricy_bot` 导入命名空间，**不得安装到同一 Python 环境**；
  开发、CI 与发行各自使用独立环境。
- Light 专属运行依赖：`pywin32`（平台层绑定，L0 选定）；FastAPI/Uvicorn/keyring
  在对应阶段进入本目录的清单，不进根 pyproject。

## 构建

```bash
python tools/build_light.py                      # 只生成 staging 与 manifest
python tools/build_light.py --pyinstaller        # staging 后冻结为 onedir 应用
```

产物：`build/light-staging/`（staging）、`build/light-staging/dist/RaricyBotLight/`
（冻结应用，onedir + windowed）。ZIP 与干净 Windows 验收属 L5，当前均未执行。

## 当前状态（L0）

- staging 白名单：`raricy_bot/__init__.py`、`logging_setup.py`、`redact.py`
  （Launcher 的诊断出口依赖），随阶段推进按实际闭包扩充并同步本清单。
- 冻结入口 `entry_light.py` → `raricy_launcher.main:main`；`--worker` 复用同一
  可执行文件作为 Worker 入口（§10.1 首版选择）。
