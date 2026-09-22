"""日志初始化与结构化事件输出。

业务代码只通过 `log_event()` 输出结构化字段。白名单同时约束**字段名**与**取值**：
只挡字段名挡不住 `error=describe_error(exc)` 这种把上游正文塞进合法字段名的写法，
所以每个字段还钉死了取值类型，不满足就整条丢弃 —— 不截断、不转写、不"尽力保留"。

脱敏作用在 handler 的**最终输出**上（`RedactingFormatter`），不是改写共享
`LogRecord`：异常堆栈、`stack_info` 与 extra 字段都在 `getMessage()` 之外，
只过滤消息等于给它们留了后门；而改写共享 record 会让多个 handler 的先后顺序
影响安全性。

新字段要先在 `FIELD_KINDS` 登记字段名与取值类型，再投入使用（INTERFACES §2）。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Final

from .redact import Redactor, SecretRegistry

# --- 字段白名单与取值类型 ---------------------------------------------------

# 取值类型（INTERFACES §2）。名字与值一起登记，新增字段必须先想清楚它是哪一类。
TOKEN: Final[str] = "token"  # 受控标识：整数、布尔，或 [A-Za-z0-9_.:@+-] 的短串
NAME: Final[str] = "name"  # Python 标识符：异常类名、模块名，如 MCPError
LABEL: Final[str] = "label"  # 配置里的短名称（发文任务名）：去控制字符、限长
FRAMES: Final[str] = "frames"  # 安全堆栈：`模块.函数:行号` 的逗号列表
MODULE: Final[str] = "module"  # npm 包路径（`@scope/name/path.js`）：比 TOKEN 多一个 `/`

# 字段名 → 取值类型。所有事件共用的 `event` 自己也是一条受控标识。
FIELD_KINDS: dict[str, str] = {
    "event": TOKEN,
    "component": TOKEN,
    "status": TOKEN,
    "error": NAME,
    "kind": TOKEN,
    "reason": TOKEN,
    "event_id": TOKEN,
    "message_id": TOKEN,
    "channel_id": TOKEN,
    "channel_kind": TOKEN,
    "count": TOKEN,
    "attempt": TOKEN,
    "delay": TOKEN,
    # 大区共享链与容量治理（D-20 / D-23）：只放 id 与字节数，绝不放用户名或正文。
    "thread_root_id": TOKEN,
    # 评论对象公开 UUID 仅用于诊断；正文、用户名与 actor id 不在白名单内。
    "comment_id": TOKEN,
    "blog_id": TOKEN,
    "notification_id": TOKEN,
    "conversation_id": TOKEN,
    "source": TOKEN,
    "size_bytes": TOKEN,
    "limit_bytes": TOKEN,
    # MCP：设计 §8.3 允许记录服务器名、工具名与 feature 名。三者都来自配置，
    # 不是用户数据；模型自己生成的工具名**不在**此处（拒绝路径只记 reason）。
    "server": TOKEN,
    "tool": TOKEN,
    "feature": TOKEN,
    # Exa 池与知识库：都是进程内序号或计数，既不含 Key / 环境变量名，
    # 也不含查询、路径、标题或正文（INTERFACES §22 / §23）。
    "slot": TOKEN,
    "snapshot_version": TOKEN,
    "chunk_count": TOKEN,
    "available_count": TOKEN,
    # 长期记忆（INTERFACES §37）：只承载稳定标识与计数，绝不含正文、key、
    # user id、文件路径或命令参数。
    "scope": TOKEN,
    "revision": TOKEN,
    "entry_count": TOKEN,
    "memory_id": TOKEN,
    "candidate_id": TOKEN,
    # 公开个人记忆（INTERFACES §51）：本功能**只**新增这两个字段，两者都只承载计数 ——
    # 前者是某次扫描/操作涉及的公开条目数，后者是本轮选中的 subject 数。username、
    # owner key、查询词、匹配正文、文件路径与完整 subject 对象一律不进日志。
    "public_entry_count": TOKEN,
    "subject_count": TOKEN,
    # 定时发文（INTERFACES §53.13）：`task_name` 是 YAML 里的任务名（允许中文），
    # 因此是 LABEL 而不是 TOKEN；其余是本地自增主键与 UTC+8 的 YYYY-MM-DD。
    # `chars` 只表示**出站标题**的 UTF-16 长度：文章正文的长度、片段与指纹一律不进日志。
    "task_name": LABEL,
    "post_id": TOKEN,
    "run_id": TOKEN,
    "day": TOKEN,
    "chars": TOKEN,
    # 进程诊断（LIGHT_EDITION_DESIGN §10.2）：只放 pid，不放命令行或路径。
    "pid": TOKEN,
    # --- 安全诊断（计划 §4）新增字段 -----------------------------------------
    # 本地随机关联标识：只编码随机数，不编码任何用户数据。
    "trace_id": TOKEN,
    "retryable": TOKEN,
    "http_status": TOKEN,
    "stage": TOKEN,
    "duration_ms": TOKEN,
    # MCP 数字错误码、子进程退出码与信号；都是上游给的数字，不是正文。
    "code": TOKEN,
    "exit_code": TOKEN,
    "signal": TOKEN,
    # 子进程 stderr 的**结构化分类**结果（D-111）：不再保存 stderr 原文。
    # `module` 是经校验的 npm 包路径，`category` 是固定的失败类别。
    "module": MODULE,
    "category": TOKEN,
    # 后台任务结束与健康变化。
    "task": TOKEN,
    "stack": FRAMES,
    "from_state": TOKEN,
    "to_state": TOKEN,
    "queue_depth": TOKEN,
    "worker_count": TOKEN,
    # 发送结果与对账不需要新字段名：`sender.send` 用 `kind`（发送类型）+
    # `reason`（delivered/deduped/failed/minute/backoff/quota），发文用
    # `status`（published/unconfirmed/failed）+ `reason`。再加一组同义字段只会
    # 让「同一个事实有两个名字」，读日志的人不知道该信哪一个。
    #
    # 归档自身状态：启用、写入失败/恢复、磁盘阈值、缺口计数。
    "segment": TOKEN,
    "free_bytes": TOKEN,
    "gap_count": TOKEN,
    "written_count": TOKEN,
    # 表情包出站规范化（设计 §4.6）：`sticker.render` 的四个计数只描述一次归一里候选
    # 的去向，互斥且完备 —— `candidates == kept + fixed + dropped`。正文与具体名字
    # 属于模型生成的内容，一律不进日志，因此这四个字段都是计数。
    "candidates": TOKEN,
    "kept": TOKEN,
    "fixed": TOKEN,
    "dropped": TOKEN,
    # 逐 token 敏感串复检的计数（设计 §4.1 第 3 步）。它与上面四个**处于不同阶段**、可以
    # 重叠：能命中复检的只可能是被归一判为 kept 或 fixed 的 token。因此实际发出的 token
    # 数是 `kept + fixed - dropped_for_secret`，不是 `candidates - dropped`。
    "dropped_for_secret": TOKEN,
}

# 允许出现在日志里的字段名白名单；由 FIELD_KINDS 派生，两者不允许各存一份。
LOG_FIELDS: frozenset[str] = frozenset(FIELD_KINDS)

# 会被**永久归档**的 INFO 事件（计划 §4）：启动/停止、故障恢复、健康变化、
# MCP 阶段变化。普通成功聊天、健康检查访问行与 DEBUG 不永久保存。
# WARNING 及以上不受这张表约束 —— 归档门槛独立于控制台级别，且不采样、不合并删除。
ARCHIVE_INFO_EVENTS: frozenset[str] = frozenset(
    {
        "app.started",
        "app.stopped",
        "app.health_changed",
        "app.task_exit",
        "mcp.disabled",
        "mcp.provider_started",
        "mcp.provider_recovered",
        "mcp.provider_stopped",
        "mcp.phase_started",
        "mcp.phase_finished",
        "mcp.phase_stalled",
        "comment.service_stopped",
        "comment.service_started",
        "archive.enabled",
        "archive.recovered",
        "archive.started",
        "archive.stopped",
        "archive.disk_low",
        "archive.segment_rolled",
    }
)

# 归档接收的最低级别：INFO（再由 ARCHIVE_INFO_EVENTS 收窄），DEBUG 永不归档。
ARCHIVE_MIN_LEVEL: Final[int] = logging.INFO

# 日志行格式：单行输出，正文只保留稳定字段。
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s %(message)s"

# 必须压制的第三方 logger。
# 把根 logger 设成 DEBUG 会连带打开它们的 DEBUG 输出，而 `openai._base_client`
# 在 DEBUG 下会打印完整请求体，包含 system prompt 与用户正文：
#
#   openai._base_client DEBUG Request options: {... 'json_data': {'messages': [...]}}
#
# 这直接违反 §19.1「任何级别不得出现正文或模型请求体」。
# 这里用**定级**而不是「发现敏感串就过滤」：SDK 的日志格式随版本变化，
# 字符串过滤器很容易漏掉嵌套字段。降级后仍可诊断 —— 我们自己的
# `model.retry` / 错误类别 / HTTP 状态等走 `raricy.*` 命名空间，不受影响。
#
# `httpx2` 不是笔误：openai 2.54 与 mcp 2.2 在装得上 `httpx2` 时会改用它，
# 其 `httpx2` logger 会漏进应用日志（事件档案 §六.6 的记录）。两个包名都要列。
NOISY_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore._trace",
    "anyio",
)

# `raricy.*` 是我们自己的命名空间；其余记录都算第三方。
ROOT_NAMESPACE: Final[str] = "raricy"

# --- 进程级状态 -------------------------------------------------------------

# 凭据登记中心：日志侧 Redactor 只是它的一个订阅者，出站 Redactor 由装配方
# 用 `attach()` 订阅同一个实例（计划 §3.1）。
_registry = SecretRegistry()

# 日志侧脱敏器，`RedactingFormatter` 与 `RedactingFilter` 读它。
_redactor = Redactor()
_registry.attach(_redactor)

# 进程内一次性的随机标识：同一份部署的多次启动在归档里可区分，且不含任何用户数据。
boot_id: str = uuid.uuid4().hex[:12]

# 已安装的 stderr handler 与归档 handler，保证 setup_logging 可重复调用而不叠加。
_handler: logging.Handler | None = None
_archive_handler: logging.Handler | None = None
_console_level: str = "INFO"

_logger = logging.getLogger(f"{ROOT_NAMESPACE}.logging")


class RedactingFilter(logging.Filter):
    """把最终消息文本脱敏后写回 record。

    生产路径不再用它：改写共享 `LogRecord` 会让多个 handler 的先后顺序决定
    谁能看到原文（见 `RedactingFormatter`）。保留它是给测试与 `caplog` 这类
    外部 handler 用的挂钩，行为和以前一致。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = redact_text(record.getMessage())
        record.msg = message
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """先按规格渲染，再对**最终字符串**脱敏。

    顺序不能反。`record.getMessage()` 只覆盖消息模板与参数，异常堆栈
    （`exc_text`）、`stack_info` 与 extra 字段都在它之外；只过滤消息等于给
    它们留了后门。在最终文本上替换还避开了改写共享 record 的问题。

    第三方库的 WARNING 及以上不渲染原文，改渲染受限事件（见 `third_party_event`）：
    「新文件安全、旧 stderr 仍泄露」是明确要避免的组合，而控制台也是"旧 stderr"。
    低于 WARNING 的第三方行（aiohttp 访问日志、httpx2 的 INFO）保持原样 ——
    它们没有上游错误正文，却是事件档案里证明过有用的观测。
    """

    def format(self, record: logging.LogRecord) -> str:
        event = third_party_event(record)
        if event is not None:
            return (
                f"{self.formatTime(record, self.datefmt)} "
                f"{record.levelname} {record.name or ROOT_NAMESPACE} {event.as_text()}"
            )
        return redact_text(super().format(record))


def third_party_event(record: logging.LogRecord) -> LogEvent | None:
    """把一条**第三方** WARNING+ 记录压成受限事件；其余的返回 None。

    判定依赖两件事，缺一不可：记录不是我们 `raricy.*` 命名空间发出的，且没带
    `log_event` 构造的载荷。第三方消息的模板与参数完全由上游决定，因此原文
    既不进归档，也不进控制台 —— 只留来源 logger 名（受 TOKEN 约束、再过一次
    脱敏）与级别。
    """
    if record.levelno < logging.WARNING:
        return None
    if event_payload(record) is not None:
        return None
    name = record.name or ""
    if name == ROOT_NAMESPACE or name.startswith(f"{ROOT_NAMESPACE}."):
        return None
    return LogEvent(name="third_party.failure", fields=(("source", _source_name(name)),))


def _source_name(name: str) -> str:
    """来源 logger 名；不合规或含密钥一律收敛成 `unknown`。"""
    redacted = redact_text(name)
    return redacted if _TOKEN_RE.match(redacted) else "unknown"


def new_trace_id() -> str:
    """生成一个本地随机关联标识。

    它**只**由随机数生成：不含 message_id、用户、频道或任何可逆编码，因此可以
    安全地进日志与永久归档（计划 §4 的「请求失败」一行）。
    """
    return uuid.uuid4().hex[:12]


def redact_text(text: str) -> str:
    """对任意文本施加当前进程的密钥替换；供日志之外的通路复用。"""
    return _redactor.redact(text)


def setup_logging(level: str = "INFO") -> None:
    """配置根 logger：单行格式输出到 stderr，并挂上脱敏格式化器。

    同时把第三方库（模型 SDK、HTTP 栈）的级别压到 WARNING —— 否则根 logger 设成
    DEBUG 时会连带打开它们的 DEBUG 输出，把模型请求体写进 stderr。

    根 logger 的级别取「控制台级别」与「归档门槛」中更低的那个：归档不得因为
    控制台设成 ERROR 就丢掉 WARNING（计划 §5.1）。
    """
    global _handler, _console_level
    _console_level = level.upper()
    root = logging.getLogger()
    root.setLevel(_root_level())

    # 放在 if 之外：本函数可能被重复调用，每次都要确保压制生效。
    for name in NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    if _handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(_console_level)
        handler.setFormatter(RedactingFormatter(LOG_FORMAT))
        root.addHandler(handler)
        _handler = handler
    else:
        _handler.setLevel(_console_level)


def install_archive(handler: logging.Handler) -> None:
    """挂上永久归档 handler；重复调用先摘掉旧的，避免同一事件被写两遍。"""
    global _archive_handler
    root = logging.getLogger()
    if _archive_handler is not None:
        root.removeHandler(_archive_handler)
    handler.setLevel(ARCHIVE_MIN_LEVEL)
    root.addHandler(handler)
    _archive_handler = handler
    root.setLevel(_root_level())


def shutdown_logging() -> None:
    """冲洗并摘掉**我们自己装的** handler，关闭归档持有的文件；可重复调用。

    关闭顺序有意义：先关归档再关控制台，最后一条 `archive.stopped` 才有地方去。
    刻意不调用 `logging.shutdown()`：那会把进程里所有 handler（包括外部宿主、
    测试框架装的）一并关掉，而本函数只该管自己装的那两个。
    """
    global _handler, _archive_handler
    root = logging.getLogger()
    if _archive_handler is not None:
        root.removeHandler(_archive_handler)
        try:
            _archive_handler.close()
        finally:
            _archive_handler = None
    if _handler is not None:
        root.removeHandler(_handler)
        try:
            _handler.close()
        finally:
            _handler = None


def reset_logging_state() -> None:
    """换成一套全新的进程级状态；**只供测试**，避免用例之间互相污染。

    真实运行不需要它：`boot_id`、凭据表与 handler 都该活到进程结束。
    """
    global _registry, _redactor, boot_id, _console_level
    shutdown_logging()
    _registry = SecretRegistry()
    _redactor = Redactor()
    _registry.attach(_redactor)
    boot_id = uuid.uuid4().hex[:12]
    _console_level = "INFO"
    logging.getLogger().setLevel(logging.WARNING)


def _root_level() -> int:
    """控制台级别与归档门槛里更低的那一个，避免根 logger 提前滤掉要归档的事件。"""
    console = logging.getLevelNamesMapping().get(_console_level, logging.INFO)
    if _archive_handler is None:
        return console
    return min(console, ARCHIVE_MIN_LEVEL)


def get_logger(component: str) -> logging.Logger:
    """按组件名取 logger，统一挂在 raricy 命名空间下。"""
    return logging.getLogger(f"{ROOT_NAMESPACE}.{component}")


def secret_registry() -> SecretRegistry:
    """返回进程级凭据登记中心；装配方用它把出站 Redactor 一起订阅上。"""
    return _registry


def register_secret(value: str | None) -> None:
    """登记需要脱敏的运行时密钥（如会话 Cookie）。

    兼容入口：新代码应当拿到共享登记中心（`secret_registry()`）后一次登记，
    而不是在每个调用点手工登记两次。
    """
    _registry.register(value)


def attach_redactor(redactor: Redactor) -> None:
    """把出站 Redactor 订阅到进程级登记中心，并补齐此前登记的凭据。"""
    _registry.attach(redactor)


# --- 事件构造 ---------------------------------------------------------------

_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_.:@+-]{1,64}\Z")
# npm 包路径比 TOKEN 多一个 `/`，但**只多这一个**：单独一种取值类型比放宽
# TOKEN 的字符集安全 —— 后者会顺带允许 `https://host/x` 这类 URL 通过。
# 这条正则同时是 stdio 的 stderr 提取器与日志层的执行点，两者不允许各存一份。
MODULE_RE = re.compile(r"\A@?[A-Za-z0-9][A-Za-z0-9._-]{0,60}(/[A-Za-z0-9._-]{1,60}){0,4}\Z")
_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_FRAME_PART = r"[A-Za-z_][A-Za-z0-9_.]{0,31}:[0-9]{1,6}"
_FRAMES_RE = re.compile(rf"\A{_FRAME_PART}(,{_FRAME_PART}){{0,15}}\Z")
_LABEL_MAX_CHARS = 64

# 不用 `import math`：只为了一个 isfinite 不值得，而且 NaN/Inf 本来就不该出现在计数里。
_MAX_FLOAT = 1e18


def encode_field(name: str, value: object) -> int | float | str | None:
    """按字段登记的类型编码取值；不满足类型约束返回 None（整条字段丢弃）。

    这里不做截断：截断会把「类型不对」变成一个看起来正常的值，掩盖调用方的错误。
    """
    kind = FIELD_KINDS.get(name)
    if kind == TOKEN:
        return _encode_token(value)
    if kind == NAME:
        return value if isinstance(value, str) and _NAME_RE.match(value) else None
    if kind == LABEL:
        return _encode_label(value)
    if kind == FRAMES:
        return value if isinstance(value, str) and _FRAMES_RE.match(value) else None
    if kind == MODULE:
        return value if isinstance(value, str) and MODULE_RE.match(value) else None
    return None


def _encode_token(value: object) -> int | float | str | None:
    """受控标识：数字原样保留，字符串必须落在受限字符集内。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or abs(value) > _MAX_FLOAT:
            return None
        return value
    if isinstance(value, str) and _TOKEN_RE.match(value):
        return value
    return None


def _encode_label(value: object) -> str | None:
    """配置里的短名称：压平空白、去掉控制字符，超长或非字符串一律丢弃。"""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > _LABEL_MAX_CHARS:
        return None
    if any(character < " " or character == "\x7f" for character in text):
        return None
    return text


@dataclass(frozen=True)
class LogEvent:
    """一条已通过校验的事件：事件名是受控标识，字段名与取值都已编码。"""

    name: str
    fields: tuple[tuple[str, int | float | str], ...] = field(default=())

    def as_text(self) -> str:
        """控制台文本编码：`event=名字 字段=值 ...`，字段顺序即调用顺序。"""
        parts = [f"event={self.name}"]
        parts.extend(f"{key}={value}" for key, value in self.fields)
        return " ".join(parts)

    def as_mapping(self) -> dict[str, int | float | str]:
        """归档 JSON 编码用的扁平字段；事件名由归档信封单独承载。"""
        return {key: value for key, value in self.fields}

    def get(self, name: str) -> int | float | str | None:
        """按字段名取值；不存在返回 None。"""
        for key, value in self.fields:
            if key == name:
                return value
        return None


def build_event(event: str, fields: dict[str, object]) -> LogEvent:
    """把调用方给的字段过一遍白名单与类型约束，构造安全事件。"""
    name = event if isinstance(event, str) and _TOKEN_RE.match(event) else "invalid"
    encoded: list[tuple[str, int | float | str]] = []
    for key, value in fields.items():
        if key not in FIELD_KINDS or key == "event":
            continue
        converted = encode_field(key, value)
        if converted is None:
            continue
        encoded.append((key, converted))
    return LogEvent(name=name, fields=tuple(encoded))


# 记录上承载安全事件的属性名；归档 handler 读它。
EVENT_ATTRIBUTE: Final[str] = "raricy_event"


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """输出一条结构化事件：`event=<名称> 白名单字段=值`。"""
    payload = build_event(event, fields)
    logger.log(level, payload.as_text(), extra={EVENT_ATTRIBUTE: payload})


def event_payload(record: logging.LogRecord) -> LogEvent | None:
    """取记录上承载的安全事件；非 `log_event` 产生的记录返回 None。"""
    payload = record.__dict__.get(EVENT_ATTRIBUTE)
    return payload if isinstance(payload, LogEvent) else None


def component_of(record: logging.LogRecord) -> str:
    """从 logger 名字得到组件名；不在 `raricy.` 命名空间下的一律归到第三方。"""
    name = record.name or ""
    if name == ROOT_NAMESPACE:
        return ROOT_NAMESPACE
    prefix = f"{ROOT_NAMESPACE}."
    if name.startswith(prefix):
        return name[len(prefix) :]
    return "third_party"


# --- 安全堆栈 ---------------------------------------------------------------

# 异常链与帧数的上限。堆栈是用来定位「哪一行」的，不是用来复现现场的。
_STACK_MAX_FRAMES: Final[int] = 6
_STACK_MAX_DEPTH: Final[int] = 3


def _safe_frame(filename: str, function: str) -> str | None:
    """把一帧压成 `模块.函数:行号`；名字不合法就整帧丢弃。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    if not _NAME_RE.match(stem):
        return None
    function_name = "module" if function == "<module>" else function
    if not _NAME_RE.match(function_name):
        function_name = "function"
    return f"{stem}.{function_name}"


def safe_stack(exc: BaseException | None, *, line_of: Any | None = None) -> str | None:
    """把异常的调用链压成 `模块.函数:行号` 列表，供日志字段使用。

    只保留文件名主干、函数名与行号：不含源码行、局部变量、异常原文与绝对路径。
    异常链（`__cause__` / `__context__`）按深度上限展开，取每条链**末尾**的若干帧
    —— 越靠后越接近真正抛出的位置。
    """
    if exc is None:
        return None
    frames: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and depth < _STACK_MAX_DEPTH and id(current) not in seen:
        seen.add(id(current))
        extracted = _extract_frames(current)
        if len(frames) + len(extracted) > _STACK_MAX_FRAMES:
            frames.extend(extracted[len(extracted) - (_STACK_MAX_FRAMES - len(frames)) :])
        else:
            frames.extend(extracted)
        current = current.__cause__ or current.__context__
        depth += 1
    return ",".join(frames) or None


def _extract_frames(exc: BaseException) -> list[str]:
    """取单条异常的末尾若干帧；取不到来源信息的合成异常返回空列表。"""
    traceback = exc.__traceback__
    if traceback is None:
        return []
    frames: list[str] = []
    while traceback is not None:
        code = traceback.tb_frame.f_code
        rendered = _safe_frame(code.co_filename, code.co_name)
        if rendered is not None:
            frames.append(f"{rendered}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    return frames[-_STACK_MAX_FRAMES:]


# --- 后台任务的结束观察 -----------------------------------------------------

# 任务结束方式的稳定取值。`cancelled_escaped` 与 `cancelled` 的区别是这张表存在的
# 全部理由：前者是事件档案 §六.1 记下的形态（没有取消请求却以 CancelledError 结束），
# 后者是主动关停的正常路径。把它们合并成一个 `cancelled` 就等于什么都没记。
TASK_STOPPED: Final[str] = "stopped"
TASK_CANCELLED: Final[str] = "cancelled"
TASK_CANCELLED_ESCAPED: Final[str] = "cancelled_escaped"
TASK_FAILED: Final[str] = "failed"


def observe_task(task: Any, name: str, *, component: str = "app") -> None:
    """给后台任务挂一个结束观察器：只补可观测性，不动控制流。

    不吞 `CancelledError`、不重启任务、不改任何返回值 —— 计划 §4 明确只加观测，
    业务侧的取消与重试语义留给各自的专题。
    """
    logger = get_logger(component)

    def _done(finished: Any) -> None:
        outcome, stack = task_outcome(finished)
        log_event(
            logger,
            logging.INFO if outcome == TASK_STOPPED else logging.ERROR,
            "app.task_exit",
            task=name,
            reason=outcome,
            stack=stack,
        )

    task.add_done_callback(_done)


def task_outcome(task: Any) -> tuple[str, str | None]:
    """判定一个已结束任务的结束方式，返回 `(稳定原因, 安全堆栈)`。"""
    if task.cancelled():
        # `cancelling() > 0` 说明有人请求过取消（主动关停）；否则是**逃逸取消**：
        # 没有任何人要求结束，任务却以 CancelledError 收尾。
        escaped = getattr(task, "cancelling", None)
        requested = escaped() if callable(escaped) else 1
        if requested > 0:
            return TASK_CANCELLED, None
        return TASK_CANCELLED_ESCAPED, None
    try:
        exc = task.exception()
    except Exception:  # pragma: no cover - 已判过 cancelled，这里兜的是替身任务
        return TASK_FAILED, None
    if exc is None:
        return TASK_STOPPED, None
    return TASK_FAILED, safe_stack(exc)


# --- 未捕获异常的兜底 -------------------------------------------------------


def install_exception_hooks() -> None:
    """为未捕获异常与线程异常装安全兜底。

    刻意**不**链回默认钩子：默认钩子会把原始 traceback（含源码行与异常原文）
    打进 stderr。安全堆栈由 `task.unhandled_exception` 事件承载。
    """
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


def install_asyncio_exception_handler(loop: Any) -> None:
    """替换事件循环的未取回异常处理器，避免默认处理器打印原始 traceback。"""
    loop.set_exception_handler(_asyncio_exception_handler)


def _excepthook(
    kind: type[BaseException], value: BaseException, traceback: TracebackType | None
) -> None:
    log_event(
        _logger,
        logging.CRITICAL,
        "task.unhandled_exception",
        task="main",
        error=type(value).__name__,
        stack=safe_stack(value),
    )


def _thread_excepthook(args: Any) -> None:
    if args.exc_type is SystemExit:
        return
    log_event(
        _logger,
        logging.ERROR,
        "task.unhandled_exception",
        task="thread",
        error=getattr(args.exc_type, "__name__", None),
        stack=safe_stack(args.exc_value),
    )


def _asyncio_exception_handler(loop: Any, context: dict[str, Any]) -> None:
    """不读取 context 的自由文本（`message`），只记稳定分类与安全堆栈。

    `context["message"]` 是 asyncio 自己拼的自由文本，可能带上任务名与异常原文；
    它整条不进日志，可诊断的部分由 `task` / `error` / `stack` 三个受控字段承担。
    """
    del loop
    exc = context.get("exception")
    task = context.get("task")
    get_name = getattr(task, "get_name", None)
    name = get_name() if callable(get_name) else None
    log_event(
        _logger,
        logging.ERROR,
        "task.unhandled_exception",
        task=name or "asyncio",
        error=type(exc).__name__ if isinstance(exc, BaseException) else None,
        stack=safe_stack(exc) if isinstance(exc, BaseException) else None,
    )


# --- 供归档与诊断复用的时间编码 ---------------------------------------------


def utc_timestamp(moment: datetime | None = None) -> str:
    """UTC 时间戳，毫秒精度、带 `Z` 后缀；归档信封用。"""
    value = datetime.now(timezone.utc) if moment is None else moment
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
