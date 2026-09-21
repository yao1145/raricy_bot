"""基于官方 MCP Python SDK 的 stdio Provider。"""

from __future__ import annotations

import codecs
import os
import re
import threading
from contextlib import AsyncExitStack
from typing import Any, TextIO

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import McpServerConfig
from ..logging_setup import MODULE_RE, register_secret
from ..redact import Redactor, SecretRegistry
from .session import MissingEnvironmentError, SessionMcpProvider

# 保留旧路径的导入名：调用点与测试按 `mcp.stdio` 写。
__all__ = ["MissingEnvironmentError", "StdioMcpProvider"]

_SAFE_INHERITED_ENV = ("PATH", "SYSTEMROOT", "HOME", "LANG", "TMP", "TEMP", "TMPDIR")

# 子进程 stderr 保留的末尾字符数。失败原因总在最后几行，而整段留下既会刷屏，
# 也会把上游的内部细节带进日志。
_STDERR_TAIL_CHARS = 2000
_STDERR_READ_CHUNK = 4096

# 固定的失败分类。未识别的一律 `unknown` —— 不把「密钥已被替换」当成
# 「可以保存任意正文」的依据（计划 §3.3、D-111）。
CATEGORY_MODULE_MISSING = "module_missing"
CATEGORY_PACKAGE_MISSING = "package_missing"
CATEGORY_PERMISSION = "permission"
CATEGORY_PORT_IN_USE = "port_in_use"
CATEGORY_CONFIG = "config"
CATEGORY_UNKNOWN = "unknown"

# 判定顺序敏感：错误码与短语有重叠时以更具体的在前。
_STDERR_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (CATEGORY_MODULE_MISSING, re.compile(r"ERR_MODULE_NOT_FOUND|Cannot find (?:module|package) ")),
    (CATEGORY_PACKAGE_MISSING, re.compile(r"\bE404\b|404 Not Found - GET")),
    (CATEGORY_PERMISSION, re.compile(r"\bEACCES\b|\bEPERM\b|Permission denied")),
    (CATEGORY_PORT_IN_USE, re.compile(r"\bEADDRINUSE\b")),
    (CATEGORY_CONFIG, re.compile(r"Missing required environment variable|\bEINVAL\b")),
)

# 从 stderr 里抽取的模块标识：只允许 npm 包路径的字符集，越界一律不取。
_MODULE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Cannot find module '([^']{1,120})'"),
    re.compile(r"Cannot find package '([^']{1,120})'"),
    re.compile(r"ERR_MODULE_NOT_FOUND[^\n]{0,40}?['\"]([^'\"]{1,120})['\"]"),
)


def describe_stderr(text: str, *, redactor: Redactor | None = None) -> dict[str, object]:
    """把子进程 stderr 压成**受限的结构化字段**，不再保存原文。

    在此之前这里是「末尾 2000 字符、压单行、交给日志层脱敏」（D-91）。脱敏是精确
    字符串替换，不承诺识别 URL 编码、JSON 转义或跨截断边界拆开的秘密；而诊断真正
    需要的信息其实只有两样：**哪一类失败**、**缺的是哪个模块**。于是改成先分类
    再提取，两条都在固定字符集里，正文一个字都不留（D-111 取代 D-91）。

    先脱敏再匹配：即使上游把密钥拼进了模块名，替换后也只剩 ``[redacted]``，
    不会通过下面那两道正则。
    """
    flattened = " ".join(text.split())
    if not flattened:
        return {}
    if redactor is not None:
        flattened = redactor.redact(flattened)
    category = CATEGORY_UNKNOWN
    for name, pattern in _STDERR_RULES:
        if pattern.search(flattened):
            category = name
            break
    fields: dict[str, object] = {"category": category}
    for pattern in _MODULE_PATTERNS:
        match = pattern.search(flattened)
        if match is None:
            continue
        candidate = match.group(1).strip()
        if MODULE_RE.match(candidate):
            fields["module"] = candidate
        break
    return fields

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

    缓冲里的字节用**增量解码器**转成文本：一次 ``os.read`` 的边界与 UTF-8 字符
    边界无关，逐块 ``decode`` 会把跨块的多字节字符变成替换符 —— 那既破坏诊断，
    也会让同一条错误在两个时刻看起来不一样（计划 §3.3）。

    脱敏与分类都在读取之后：``tail()`` 只是内存里的有界缓冲，真正进日志的是
    ``describe_stderr()`` 提取出来的受限字段。

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
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = os.read(self._read_fd, _STDERR_READ_CHUNK)
                if not chunk:
                    break
                # final=False：跨块的半个字符留给下一次 decode 补齐。
                text = decoder.decode(chunk, final=False)
                with self._lock:
                    self._buffer = (self._buffer + text)[-self._limit :]
            tail = decoder.decode(b"", final=True)
            if tail:
                with self._lock:
                    self._buffer = (self._buffer + tail)[-self._limit :]
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
        registry: SecretRegistry | None = None,
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
        # 装配方注入共享登记中心时，出站 Redactor 订阅同一份凭据表：此后任何一次
        # register() 两个都自动生效（计划 §3.1）。没注入就走下面的兼容路径。
        self._registry = registry
        if self._registry is not None and self._redactor is not None:
            self._registry.attach(self._redactor)
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
            # 一次 register 同时覆盖出站文本与日志层：此前这里要写两遍，
            # 漏一遍不会报错，只会让某一侧的密钥原样落出去（计划 §3.1）。
            self._register(value)
        return environment

    def _register(self, value: str) -> None:
        """登记一个凭据；没有共享登记中心时退回「两处各写一遍」的兼容路径。"""
        if self._registry is not None:
            self._registry.register(value)
            return
        if self._redactor is not None:
            self._redactor.add_secret(value)
        register_secret(value)

    def diagnostics(self) -> dict[str, object] | None:
        """返回子进程 stderr 的**结构化**诊断字段；没有可说的就返回 None。"""
        if self._stderr_capture is None:
            return None
        return describe_stderr(self._stderr_capture.tail(), redactor=self._redactor) or None

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
