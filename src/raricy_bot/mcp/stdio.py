"""基于官方 MCP Python SDK 的 stdio Provider。"""

from __future__ import annotations

import os
import threading
from contextlib import AsyncExitStack
from typing import Any, TextIO

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import McpServerConfig
from ..logging_setup import register_secret
from ..redact import Redactor
from .session import MissingEnvironmentError, SessionMcpProvider

# 保留旧路径的导入名：调用点与测试按 `mcp.stdio` 写。
__all__ = ["MissingEnvironmentError", "StdioMcpProvider"]

_SAFE_INHERITED_ENV = ("PATH", "SYSTEMROOT", "HOME", "LANG", "TMP", "TEMP", "TMPDIR")

# 子进程 stderr 保留的末尾字符数。失败原因总在最后几行，而整段留下既会刷屏，
# 也会把上游的内部细节带进日志。
_STDERR_TAIL_CHARS = 2000
_STDERR_READ_CHUNK = 4096

# 关闭捕获器时等待读线程读完管道的上限（秒）。正常情况下它立刻就返回：
# 调用点都发生在 SDK 已经终止子进程之后，管道里没有新的写端，读线程必然读到 EOF。
_STDERR_JOIN_SECONDS = 0.5


def _close_fd(fd: int) -> None:
    """尽力关闭一个 fd；重复关闭或已被别人关掉都不算错误。"""
    try:
        os.close(fd)
    except OSError:
        return


class _StderrCapture:
    """把 stdio 子进程的 stderr 收进一个有界的进程内缓冲，供失败时取末尾。

    它替代了原先的 ``/dev/null``。子进程把启动失败的原因写在 stderr 里，丢掉它
    意味着「子进程起不来」和「子进程起来了但不说话」在日志里完全一样 ——
    2026-09-16 的 wolfram 故障正是如此：amap 精确钉死的 SDK 版本被 npm 提升到顶层，
    wolfram 的模块解析失败、当场退出，而我们只能靠外部复刻依赖树才反推出来。

    三处约束决定了它的形状：

    1. **不能用临时文件**：容器以只读根文件系统运行（compose 的 ``read_only: true``），
       没有可写目录。
    2. **不能直接接本进程的 stderr**：那样字节会绕过日志层的 ``RedactingFilter``，
       而上游确实会把 Key 回显在错误正文里（amap 的 Key 校验失败就是），
       那正是「任何级别不得出现密钥」禁止的。
    3. **读取必须持续**：管道缓冲区只有 64 KiB，写满而无人读会让子进程阻塞。
       所以起一个守护线程做阻塞读，只保留末尾 ``limit`` 个字符。

    脱敏不在这里做：``tail()`` 的结果作为日志字段输出时会过 ``RedactingFilter``。

    **生命周期**：一次子进程会话一个捕获器，随会话一起关闭。关闭分两步且顺序
    有意义 —— ``close()`` 先关写端。此时子进程已被 SDK 终止（调用点都在
    ``stack.aclose()`` 之后），管道里再无写端，读线程必然读到 EOF 并结束，
    于是它可以自己关掉读端：**读端只能由读线程自己关**，别处关掉会有
    「fd 号被复用、读线程读到无关对象」的竞态。
    ``close()`` 随之 join，把「管道已读空」变成一个确定的时刻，
    否则 ``diagnostics()`` 紧跟在失败之后取值时可能漏掉最后一段。
    """

    def __init__(self, limit: int = _STDERR_TAIL_CHARS) -> None:
        self._limit = limit
        self._buffer = ""
        self._lock = threading.Lock()
        self._closed = False
        self._read_fd, self._write_fd = os.pipe()
        self._thread = threading.Thread(target=self._pump, name="mcp-stderr", daemon=True)
        self._thread.start()

    def fileno(self) -> int:
        """交给子进程的写端；``subprocess`` 会从这里取 fd。"""
        return self._write_fd

    def _pump(self) -> None:
        try:
            while True:
                chunk = os.read(self._read_fd, _STDERR_READ_CHUNK)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                with self._lock:
                    self._buffer = (self._buffer + text)[-self._limit :]
        except OSError:
            # 读端被意外关闭：线程退出，子进程的输出就此丢弃。
            pass
        finally:
            _close_fd(self._read_fd)

    def close(self) -> None:
        """结束采集：关写端、等读线程收尾。缓冲保留，之后仍可 ``tail()``。

        可以重复调用。join 超时（子进程意外还活着）时读线程继续阻塞，
        它随进程结束而消失；缓冲里已有的内容照样可读。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        _close_fd(self._write_fd)
        self._thread.join(timeout=_STDERR_JOIN_SECONDS)

    def tail(self) -> str:
        """返回保留内容的末尾，压成单行以便进结构化日志字段。"""
        with self._lock:
            text = self._buffer
        return " ".join(text.split())


class StdioMcpProvider(SessionMcpProvider):
    """单个 MCP stdio 子进程的生命周期封装。"""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        host_env: dict[str, str] | None = None,
        redactor: Redactor | None = None,
        connect_timeout_seconds: float = 10.0,
        call_timeout_seconds: float = 20.0,
        stderr: TextIO | None = None,
    ) -> None:
        super().__init__(
            config,
            connect_timeout_seconds=connect_timeout_seconds,
            call_timeout_seconds=call_timeout_seconds,
        )
        self._host_env = dict(os.environ if host_env is None else host_env)
        self._redactor = redactor
        self._stderr = stderr
        # 每次 ``_open_transport`` 换一个新的捕获器，旧的随会话一起关掉。
        # 关闭后对象仍留在这里：``diagnostics()`` 是在 ``_cleanup_transport()``
        # 之后才被调用的，缓冲必须活过关闭这一步。
        self._stderr_capture: _StderrCapture | None = None

    def resolve_environment(self) -> dict[str, str]:
        """生成最小子进程环境，并登记显式注入的秘密。"""
        missing = tuple(
            host_name
            for host_name in self.config.env_from.values()
            if not self._host_env.get(host_name, "").strip()
        )
        if missing:
            raise MissingEnvironmentError(tuple(dict.fromkeys(missing)))
        environment = {
            key: self._host_env[key]
            for key in _SAFE_INHERITED_ENV
            if self._host_env.get(key)
        }
        environment.update(self.config.env)
        for child_name, host_name in self.config.env_from.items():
            value = self._host_env[host_name]
            environment[child_name] = value
            if self._redactor is not None:
                self._redactor.add_secret(value)
            # 两个 Redactor 是**不同对象**：注入的那个管出站文本，日志层的
            # ``RedactingFilter`` 读进程级的 ``logging_setup`` 单例。只登记前者，
            # 密钥就不会在日志里被替换 —— 而 stderr 尾巴正是刚好会带上密钥的地方
            # （amap 的 Key 校验失败会把 Key 原样回显）。与 site/client.py 存
            # Cookie 时同样两处都登记。
            register_secret(value)
        return environment

    def diagnostics(self) -> str | None:
        """返回子进程 stderr 的末尾；没有可说的就返回 None。"""
        if self._stderr_capture is None:
            return None
        return self._stderr_capture.tail() or None

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        environment = self.resolve_environment()
        errlog: Any = self._stderr
        if errlog is None:
            # 新会话新捕获器：上一次失败留下的缓冲不能被当成本次的诊断。
            self._stderr_capture = _StderrCapture()
            errlog = self._stderr_capture
        params = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=environment,
        )
        return await stack.enter_async_context(stdio_client(params, errlog=errlog))

    def _cleanup_transport(self) -> None:
        """关闭 stderr 捕获器；此时子进程已由 SDK 终止，管道必然读到 EOF。"""
        if self._stderr_capture is not None:
            self._stderr_capture.close()
