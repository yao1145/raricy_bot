"""仅开发用的上游取样脚本：调一次真实的 MCP 工具，把原始结果存成 fixture。

**运行期绝不 import 本脚本**，它也不在 ``src/`` 里 —— 它的唯一职责是回答一个
评审阶段回答不了的问题：上游返回的东西到底长什么样。

为什么需要它：`@amap/amap-maps-mcp-server` 与 `wolfram-mcp` 的 npm 包都没有
``repository`` 字段，无法证明是厂商官方；知乎更是只在文档里写了一句
"structured XML"。所以工具名与参数可以逐个从 tarball 里读出来，**结果格式不行** ——
它只能靠一次真调用确认。``mcp/amap.py`` 与 ``mcp/wolfram.py`` 的解析器是按读到
的实现写的，可信度高；``mcp/zhihu.py`` 的解析器刻意做成与标签名无关，正是因为在
拿到样本之前，任何标签名都是猜测。

用法::

    export AMAP_MAPS_API_KEY=...
    PYTHONPATH=src python tools/capture_mcp_fixture.py \\
        --server amap --tool maps_weather --args '{"city":"上海"}' \\
        --out tests/fixtures/amap_weather.json

三条硬约束，都由代码而不是由纪律保证：

1. **只能写进 ``tests/fixtures/``**。样本是开发资料，不是配置，更不该流到别处。
2. **写出的每个字符串都先过 ``Redactor``**。高德的异常正文里带请求 URL，而 URL 里带
   ``key=``（见 ``mcp/amap.py`` 的模块注释），所以「原样转储」在它身上就等于把 Key
   写进文件。脱敏在这里不是可选项。
3. **绝不回显宿主环境里的密钥值**：只打印变量名与「已设置/未设置」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from raricy_bot.config import (  # noqa: E402  (必须先调整 sys.path)
    TRANSPORT_SSE,
    TRANSPORT_STDIO,
    McpServerConfig,
)
from raricy_bot.mcp.sse import SseMcpProvider  # noqa: E402
from raricy_bot.mcp.stdio import StdioMcpProvider  # noqa: E402
from raricy_bot.redact import REDACTED, Redactor  # noqa: E402

# fixture 的落点。必须与 tests/ 里的契约测试读的目录一致。
FIXTURE_ROOT = _REPO_ROOT / "tests" / "fixtures"

# 三个上游的连接事实，与 config.example.yaml 保持逐字一致。
# 命令名来自各自的 package.json 的 bin 字段（npm view <pkg> bin 可复核）。
SERVERS: dict[str, dict[str, Any]] = {
    "amap": {
        "transport": TRANSPORT_STDIO,
        "command": "mcp-amap",
        "env_from": {"AMAP_MAPS_API_KEY": "AMAP_MAPS_API_KEY"},
    },
    "wolfram": {
        "transport": TRANSPORT_STDIO,
        "command": "wolfram-mcp",
        "env_from": {"WOLFRAM_APP_ID": "WOLFRAM_APP_ID"},
    },
    "zhihu": {
        "transport": TRANSPORT_SSE,
        "url": "https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse",
        "bearer_env": "ZHIHU_ACCESS_SECRET",
    },
}


def _build_config(server: str) -> McpServerConfig:
    if server not in SERVERS:
        raise SystemExit(f"未知服务器：{server}（可选：{', '.join(sorted(SERVERS))}）")
    return McpServerConfig(name=server, enabled=True, **SERVERS[server])


def _missing_env(config: McpServerConfig) -> tuple[str, ...]:
    """返回该服务器需要、但宿主环境里没有（或为空）的变量名。

    只返回**名字**。值在任何分支上都不经过这里，因此不可能被打印出去。
    """
    import os

    needed = [config.bearer_env] if config.transport == TRANSPORT_SSE else list(config.env_from)
    return tuple(name for name in needed if name and not os.environ.get(name, "").strip())


def _register_secrets(redactor: Redactor, config: McpServerConfig) -> None:
    """把本次用到的宿主密钥值登记进脱敏器。"""
    import os

    names = [config.bearer_env] if config.transport == TRANSPORT_SSE else list(config.env_from)
    for name in names:
        if name:
            redactor.add_secret(os.environ.get(name, "").strip())


def _result_to_jsonable(result: Any) -> Any:
    """把 ``CallToolResult`` 转成可 JSON 化的对象，不丢字段。

    优先走 pydantic 的 ``model_dump``（mcp 2.x）；1.x 的字段名是驼峰，所以再兜一层
    ``dict()``。两条都失败时退回手工提取 ``content``，宁可少字段也不抛异常 ——
    取样脚本崩溃时最需要的信息恰恰是「上游到底返回了什么」。
    """
    dumper = getattr(result, "model_dump", None)
    if callable(dumper):
        try:
            return dumper(mode="json")
        except TypeError:
            return dumper()
    if isinstance(result, dict):
        return result
    content = getattr(result, "content", None)
    if content is not None:
        return {
            "isError": bool(getattr(result, "isError", False)),
            "content": [
                {
                    "type": getattr(item, "type", None),
                    "text": getattr(item, "text", None),
                }
                for item in content
            ],
        }
    return {"repr": repr(result)}


def _redact(redactor: Redactor, value: Any) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, dict):
        return {key: _redact(redactor, item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(redactor, item) for item in value]
    return value


def _resolve_out(raw: str) -> Path:
    """把 ``--out`` 解析成绝对路径，并拒绝写进 ``tests/fixtures/`` 之外的任何地方。

    用 ``resolve()`` 之后再比较，这样 ``../`` 之类的相对成分会被先消掉，
    ``tests/fixtures/../../../etc/x`` 不会因为字符串前缀巧合而通过。
    """
    out = Path(raw)
    if not out.is_absolute():
        out = _REPO_ROOT / out
    out = out.resolve()
    root = FIXTURE_ROOT.resolve()
    if not out.is_relative_to(root):
        raise SystemExit(f"拒绝写入 {out}：样本只能落在 {root} 之内")
    return out


async def _capture(config: McpServerConfig, tool: str, arguments: dict[str, Any]) -> Any:
    redactor = Redactor()
    _register_secrets(redactor, config)
    provider: Any
    if config.transport == TRANSPORT_SSE:
        provider = SseMcpProvider(config, host_env=dict(_environ()), redactor=redactor)
    else:
        provider = StdioMcpProvider(config, host_env=dict(_environ()), redactor=redactor)
    await provider.start()
    try:
        discovered = await provider.list_tools()
        names = [item.tool_name for item in discovered]
        if tool not in names:
            raise SystemExit(
                f"{config.name} 没有发现工具 {tool}；它当前提供：{', '.join(names)}"
            )
        return await provider.call_tool(tool, arguments)
    finally:
        await provider.stop()


def _environ() -> Any:
    import os

    return os.environ


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="取一次真实的上游 MCP 调用结果并写成 fixture（仅开发用）。"
    )
    parser.add_argument("--server", required=True, choices=sorted(SERVERS))
    parser.add_argument("--tool", required=True, help="上游工具名，例如 maps_weather")
    parser.add_argument(
        "--args",
        default="{}",
        help='工具参数的 JSON 对象，例如 \'{"city":"上海"}\'（默认空对象）',
    )
    parser.add_argument("--out", required=True, help="输出路径，必须在 tests/fixtures/ 之内")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = _build_config(args.server)

    try:
        arguments = json.loads(args.args)
    except ValueError as exc:
        raise SystemExit(f"--args 不是合法 JSON：{exc}") from exc
    if not isinstance(arguments, dict):
        raise SystemExit("--args 必须是 JSON 对象")

    missing = _missing_env(config)
    if missing:
        # 只说变量名，不说值 —— 这条信息与「未设置」等价，不构成泄漏。
        raise SystemExit(f"缺少环境变量：{', '.join(missing)}（需要真实的宿主取值）")

    out = _resolve_out(args.out)

    try:
        result = asyncio.run(_capture(config, args.tool, arguments))
    except Exception as exc:
        # 异常正文可能带上游返回的原始文本（高德的错误正文里就有请求 URL），
        # 所以这里同样先脱敏再打印，并且只打印类型名之外的脱敏消息。
        redactor = Redactor()
        _register_secrets(redactor, config)
        raise SystemExit(f"取样失败：{type(exc).__name__}: {redactor.redact(str(exc))}") from exc

    redactor = Redactor()
    _register_secrets(redactor, config)
    payload = {
        "_note": (
            "capture_mcp_fixture.py 抓取的真实上游样本。落在 tests/ 之下，不入库。"
            "字符串已过 Redactor，密钥被替换为占位符。"
        ),
        "server": config.name,
        "tool": args.tool,
        "arguments": _redact(redactor, arguments),
        "result": _redact(redactor, _result_to_jsonable(result)),
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"已写入 {out}")
    print(f"脱敏占位符：{REDACTED}（出现它说明原值已被替换，属预期）")
    print(
        "提醒：样本是开发资料，不要提交、不要贴进 issue；"
        "它是解析器的评审依据，请用真实文本来校准 mcp/zhihu.py 的 _extract_items。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
