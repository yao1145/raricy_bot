"""通用计时与错误诊断：不依赖 MCP SDK，普通模型调用与完整版工具路径共用。

这些助手原本长在 `mcp/contracts.py` 里，但 `elapsed_ms` 被普通模型调用使用、
`describe_error` 被 App 的软故障日志使用 —— 两者都属于无 MCP 的 Light 发行闭包。
按 LIGHT_EDITION_DESIGN §4.3 移入本模块；`mcp/contracts.py` 仅做兼容再导出。
"""

from __future__ import annotations

import time

# `BaseExceptionGroup` 展开的深度上限：三层足够剥掉 SDK 的包装，再多说明结构异常。
_ERROR_UNWRAP_DEPTH = 3


def elapsed_ms(started: float) -> int:
    """`time.monotonic()` 起点到现在的毫秒数；负数（时钟被注入成回退值）截到 0。"""
    return max(0, int((time.monotonic() - started) * 1000))


def describe_error(exc: BaseException) -> dict[str, object]:
    """把异常压成一组**受限**的稳定字段，不再是正文。

    在此之前这里返回 ``"MCPError(REQUEST_TIMEOUT: ...)"`` 这样的自由文本，理由是
    「只有 ``error=MCPError`` 时各种失败长得一模一样」。那个理由是真的，做法不对：
    正文来自上游，可能带密钥或用户内容，而 ``RedactingFilter`` 的精确字符串替换
    并不承诺识别 URL 编码、JSON 转义或跨截断边界拆开的秘密（计划 §3.3）。

    可诊断的部分本来也不在正文里，而在**结构化**的信息上：异常类型、
    JSON-RPC 数字错误码、子进程退出码。这些全部保留，正文一个字都不留。
    调用点写成 ``**describe_error(exc)`` 即可。

    返回值经 ``log_event`` 的类型约束二次过滤，这里不必自己判断字段该不该写。
    """
    leaf = _leaf_exception(exc)
    fields: dict[str, object] = {"error": type(leaf).__name__}
    code = getattr(leaf, "code", None)
    if isinstance(code, (int, str)):
        fields["code"] = code
    exit_code = getattr(leaf, "exit_code", None)
    if isinstance(exit_code, int):
        fields["exit_code"] = exit_code
    return fields


def _leaf_exception(exc: BaseException, *, depth: int = 0) -> BaseException:
    """展开有界的 ``BaseExceptionGroup``，取第一条子异常。

    只走 ExceptionGroup 这一层：``ExceptionGroup`` 这个名字本身不携带任何信息，
    而子异常（``MCPError`` / ``FileNotFoundError`` …）才是真正的原因。深度设上限
    是为了让嵌套分组无法把展开变成一次无界的遍历。
    """
    if depth >= _ERROR_UNWRAP_DEPTH:
        return exc
    sub = getattr(exc, "exceptions", None)
    if isinstance(sub, tuple) and sub:
        first = sub[0]
        if isinstance(first, BaseException):
            return _leaf_exception(first, depth=depth + 1)
    return exc
