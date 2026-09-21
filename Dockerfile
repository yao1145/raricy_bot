# Raricy 站内聊天机器人镜像。
# 三个 stdio MCP 服务器在构建期固定安装；运行用户非 root；密钥只通过环境变量注入，
# 绝不写进镜像。
FROM node:22-bookworm-slim AS mcp-tools

WORKDIR /opt/mcp-tools

# 只在构建阶段访问 npm。最终镜像只复制 node、这三个包和入口，不包含 npm，
# 运行期也不会执行 npx、npm install 或访问 npm registry。
#
# 包与版本写在 mcp-tools.package.json 里，它与下面这条 install 一起构成唯一的
# 安装口径。**不要把依赖改回 `npm install <包名>` 那种写法**：
# amap 把 @modelcontextprotocol/sdk 精确钉在 1.0.1，一条命令装三个包时 npm 会把
# 这份 1.0.1 提升到顶层；而 wolfram-mcp 声明的 `^1.0.1` 恰好被 1.0.1 满足，
# 于是它拿不到自己的嵌套副本 —— 但 1.0.1 里没有 `server/mcp.js`，wolfram 会当场
# 以 ERR_MODULE_NOT_FOUND 退出，用户只看到 `/wolfram` 不可用（2026-09-16）。
# 清单里的 overrides 就是为它钉一份真正可用的 SDK，同时不动 amap。
#
# 版本一律钉死。exa-mcp-server 只有一个上游（exa-labs），另外两个都没有 repository
# 字段，无法证明是厂商官方 —— 所以升级必须是显式动作，绝不能靠浮动的 tag 悄悄漂移。
# 换版本要按 docs/usage/DEPLOYMENT.md 的「上游取样」重新采样本，并同步
# src/raricy_bot/capabilities.py 的白名单与 config.example.yaml 的注释。
#
# 知乎不进这个镜像：它只有远程 MCP-over-SSE，没有子进程（见 mcp/sse.py）。
COPY mcp-tools.package.json ./package.json
RUN npm install --omit=dev --no-audit --no-fund

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BOT_CONFIG_PATH=/app/config.yaml \
    PATH=/opt/mcp-tools/node_modules/.bin:/usr/local/bin:${PATH}

WORKDIR /app

# node:22-bookworm-slim 与 python:3.12-slim-bookworm 使用同一 Debian 系列；
# 只复制运行三个 stdio MCP 服务器所需的 Node 二进制与固定依赖。
COPY --from=mcp-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=mcp-tools /opt/mcp-tools /opt/mcp-tools

# 先拷构建清单与源码，再安装，避免把测试、文档与本地配置带进镜像。
COPY pyproject.toml ./
COPY src ./src
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn
RUN pip install --no-cache-dir .

# 非 root 运行（设计文档 §2.1）。
# 数据目录不变量：WORKDIR 是 /app，配置默认 storage.db_path=./data/bot.db，
# 解析为 /app/data/bot.db；docker-compose.yml 必须把数据卷挂到 /app/data，
# 否则默认配置会把 SQLite 落到临时层、容器重启即丢。改动其一时同步改另一处。
# 归档目录不变量：配置里 logging.archive.directory 相对 config.yaml 解析，
# 而 config.yaml 挂在 /app/config.yaml，所以 ./logs/errors 指的是 /app/logs/errors。
# 这里连同 /app/data 一起建出来并交给 bot：根文件系统是只读的（compose 的
# read_only: true），唯一的可写处就是这两个挂载点。只读根 + 非 root 都不放宽。
RUN useradd -r -u 10001 bot \
    && mkdir -p /app/data /app/logs \
    && chown -R bot:bot /app

USER bot

# 运维端口只供容器内健康检查与编排访问，Compose 不发布到宿主（D-10）。
# 不变量：下面的健康检查硬编码 8080，与 docker-compose.yml 的 healthcheck 一致；
# 若把 ops.port 改成其他值，必须同步修改这两处健康检查。
EXPOSE 8080

# 用 python 而不是 curl 探活：slim 镜像里没有 curl。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/livez', timeout=3).getcode() == 200 else 1)"]

CMD ["python", "-m", "raricy_bot"]
