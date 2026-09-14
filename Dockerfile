# Raricy 站内聊天机器人镜像。
# Exa MCP 在构建期固定安装；运行用户非 root；密钥只通过环境变量注入，绝不写进镜像。
FROM node:22-bookworm-slim AS exa-mcp

WORKDIR /opt/exa-mcp

# 只在构建阶段访问 npm。最终镜像只复制 node、Exa 包和入口，不包含 npm，
# 运行期也不会执行 npx、npm install 或访问 npm registry。
RUN npm install --omit=dev --no-audit --no-fund exa-mcp-server@3.4.1

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BOT_CONFIG_PATH=/app/config.yaml \
    PATH=/opt/exa-mcp/node_modules/.bin:/usr/local/bin:${PATH}

WORKDIR /app

# node:22-bookworm-slim 与 python:3.12-slim-bookworm 使用同一 Debian 系列；
# 只复制运行 Exa stdio MCP 所需的 Node 二进制与固定依赖。
COPY --from=exa-mcp /usr/local/bin/node /usr/local/bin/node
COPY --from=exa-mcp /opt/exa-mcp /opt/exa-mcp

# 先拷构建清单与源码，再安装，避免把测试、文档与本地配置带进镜像。
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# 非 root 运行（设计文档 §2.1）。
# 数据目录不变量：WORKDIR 是 /app，配置默认 storage.db_path=./data/bot.db，
# 解析为 /app/data/bot.db；docker-compose.yml 必须把数据卷挂到 /app/data，
# 否则默认配置会把 SQLite 落到临时层、容器重启即丢。改动其一时同步改另一处。
RUN useradd -r -u 10001 bot \
    && mkdir -p /app/data \
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
