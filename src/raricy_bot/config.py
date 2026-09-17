"""配置加载与校验。

非敏感配置来自 YAML；密钥只从环境变量读取，绝不写入配置文件。
所有配置对象都是 frozen dataclass，加载完成后不再变化。
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import texts
from .capabilities import (
    CAPABILITY_BY_FEATURE,
    IMPLEMENTED_FEATURES,
    SHAPE_SINGLE,
    SOURCE_MCP,
)

# 默认配置文件路径；可被环境变量 BOT_CONFIG_PATH 覆盖。
DEFAULT_CONFIG_PATH: str = "./config.yaml"

# 配置文件大小硬上限（1 MiB）。是代码常量而不是 YAML 项：必须在解析配置之前就能判定。
MAX_CONFIG_BYTES: int = 1024 * 1024

# 站点图床的单图硬上限（10 MiB）。来源：raricy.com src/lib/image-upload.ts 的
# MAX_IMAGE_SIZE。配得比它更大的话站点根本不会给出那么大的图，只会掩盖意图。
MAX_IMAGE_BYTES: int = 10 * 1024 * 1024

# 自动提取披露里条目 ID 的保守宽度（`UM-` 加九位十进制）。codec 的规范是六位，
# 多算三位只是把配置校验卡得更早；计数器真的涨过九位时由 `app.py` 那一侧的兜底接住。
WORST_CASE_MEMORY_ID: str = "UM-999999999"

# 密钥环境变量名。
USERNAME_ENV: str = "RARICY_USERNAME"
PASSWORD_ENV: str = "RARICY_PASSWORD"
LLM_API_KEY_ENV: str = "LLM_API_KEY"


class ConfigError(Exception):
    """配置缺失、格式非法或取值越界。"""


@dataclass(frozen=True)
class McpBindingConfig:
    """一个 feature 允许调用的 MCP 工具。"""

    server: str
    tool: str


# 池规模的代码常量：下限保证「轮换」有意义，上限防止一个配置错误就拉起几十个子进程。
POOL_MIN_SLOTS: int = 2
POOL_MAX_SLOTS: int = 16

# 知识库容量的代码常量硬上限（INTERFACES §23.1）。它们不是 YAML 里可改的项：
# 上限的作用是让「配置写错一个零」变成启动失败，而不是把进程拖垮。
KB_MAX_FILES: int = 20000
KB_MAX_FILE_BYTES: int = 8 * 1024 * 1024
KB_MAX_TOTAL_BYTES: int = 512 * 1024 * 1024
KB_MAX_CHUNK_CHARS: int = 20000
KB_MAX_TOP_K: int = 10
KB_MAX_CONTEXT_TOKENS: int = 32000
KB_MAX_REFRESH_SECONDS: int = 86400

# 首版锁死的两处：密钥注入的目标变量名与选择策略。
POOL_CHILD_ENV: str = "EXA_API_KEY"
POOL_STRATEGY: str = "round_robin"


@dataclass(frozen=True)
class McpAccountPoolConfig:
    """Exa 授权密钥池配置（INTERFACES §22.1）。

    **只保存环境变量名，永远不保存 Key 值**：真实值只在 Provider 构造时从宿主环境读取，
    因此 repr(配置)、异常消息和日志里都不可能带出密钥。
    """

    child_env: str = POOL_CHILD_ENV
    host_envs: tuple[str, ...] = ()
    strategy: str = POOL_STRATEGY
    rate_limit_cooldown_seconds: float = 60.0
    transient_cooldown_seconds: float = 30.0
    quota_cooldown_seconds: float = 21600.0


@dataclass(frozen=True)
class McpServerConfig:
    """MCP 服务器配置；值只包含非敏感配置，不保存解析后的密钥。"""

    name: str
    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    env_from: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    # 非空时该服务器是多 Key 池（INTERFACES §22.4）；与 env_from 互斥。
    account_pool: McpAccountPoolConfig | None = None
    # 以下三项只属于 transport="sse"（远程服务器，没有子进程）。
    # `url` 是 SSE 端点；`bearer_env` 是**宿主环境变量名**，值是 Bearer 令牌。
    url: str = ""
    bearer_env: str = ""
    # 长连接的读侧超时：站点 SSE 的既有教训是它绝不能继承普通请求超时，
    # 否则任何安静期都会把连接掐断重连。SDK 默认 300s，显式写进配置。
    stream_read_timeout_seconds: float = 300.0


@dataclass(frozen=True)
class McpFeatureConfig:
    """面向产品功能的 MCP 白名单与资源限制。"""

    name: str
    enabled: bool = True
    bindings: tuple[McpBindingConfig, ...] = ()
    result_count: int = 5
    result_item_token_limit: int = 3000
    history_item_token_limit: int = 500
    max_query_chars: int = 500
    max_tool_calls_per_turn: int = 1
    max_concurrency: int = 1
    min_interval_seconds: float = 2.0


@dataclass(frozen=True)
class McpConfig:
    """MCP 总开关、服务器和 feature 配置；默认完全关闭。"""

    enabled: bool = False
    connect_timeout_seconds: float = 10.0
    call_timeout_seconds: float = 20.0
    reconnect_base_seconds: float = 3.0
    reconnect_max_seconds: float = 60.0
    servers: dict[str, McpServerConfig] = field(default_factory=dict)
    features: dict[str, McpFeatureConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class SiteConfig:
    """站点接口配置。"""

    base_url: str
    request_timeout_seconds: float = 20.0


@dataclass(frozen=True)
class ModelConfig:
    """模型服务配置。"""

    base_url: str
    model: str
    temperature: float = 0.4
    timeout_seconds: float = 45.0
    max_output_tokens: int = 600
    # 图片输入：默认关闭。配的模型未必支持视觉，开启而模型不支持时每一轮带图的消息
    # 都会以 400 失败并回一条失败提示；默认关闭让既有部署升级后行为逐字节不变。
    vision_enabled: bool = False
    # 单张图的下载期硬上限（字节）。默认 5 MiB：base64 后约 6.7 MiB，
    # 在 concurrency=3 时峰值可控。
    max_image_bytes: int = 5 * 1024 * 1024


@dataclass(frozen=True)
class BehaviorConfig:
    """行为与配额配置。"""

    context_turns: int = 10
    context_input_tokens: int = 8000
    max_input_chars: int = 8000
    # 引用博客正文的长度上限。默认值与 comments.article_max_chars 相同，但**是两个键**：
    # 聊天模型与评论模型未必是同一个，两边各自可调。
    quoted_blog_max_chars: int = 1000
    # 单条内容引用（`[@<ID>]`）展开出来的字符上限：剪贴板正文上限 5 万字、
    # 投票选项同理，都属于**别人写的**内容，不给上限就等于让一条十个字的消息
    # 变成几十万字的外送正文。它同时是**消息与评论正文**展开时的总预算
    # （博客正文与文章正文的总预算另有其键，见上一条与 comments.article_max_chars）。
    # 默认 2000 与站点在聊天/评论里的截断口径一致。
    content_ref_max_chars: int = 2000
    max_output_chars: int = 5000
    concurrency: int = 3
    queue_size: int = 50
    minute_attempt_limit: int = 25
    daily_normal_limit: int = 1950
    daily_absolute_limit: int = 2000
    notice_cooldown_seconds: int = 300
    reconnect_base_seconds: float = 3.0
    reconnect_max_seconds: float = 60.0
    ready_probe_seconds: float = 300.0
    rate_limit_wait_seconds: float = 60.0


@dataclass(frozen=True)
class OpsConfig:
    """运维接口配置。"""

    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True)
class CommentConfig:
    """博客评论机器人配置；默认关闭以保持现有部署行为。"""

    enabled: bool = False
    recent_poll_seconds: int = 30
    notification_poll_seconds: int = 15
    notification_max_pages: int = 5
    queue_size: int = 50
    concurrency: int = 1
    context_turns: int = 10
    context_input_tokens: int = 8000
    article_max_chars: int = 1000
    # 一轮评论回复最多交给模型几张图：附件与两处正文里的 `[@10位]` 引用共用这一个
    # 名额池。0 合法，等于评论侧不取图（`model.vision_enabled` 仍可整体关掉视觉）。
    max_images_per_reply: int = 3
    max_output_chars: int = 5000
    max_response_bytes: int = 8388608
    max_tree_nodes: int = 10000
    unmatched_attempt_limit: int = 5
    minute_attempt_limit: int = 20
    daily_reply_limit: int = 1950
    daily_absolute_limit: int = 2000
    article_cooldown_seconds: int = 5
    conversation_retention_seconds: int = 2592000
    dedupe_retention_seconds: int = 7776000
    retry_base_seconds: int = 5
    retry_max_seconds: int = 300
    server_backoff_seconds: int = 3600


@dataclass(frozen=True)
class KnowledgeBaseConfig:
    """本地 Markdown 知识库配置（INTERFACES §23.1）；默认关闭。"""

    enabled: bool = False
    root_dir: str = "./knowledge"
    access_mode: str = "allowlist"
    allowed_channel_kinds: tuple[str, ...] = ("dm",)
    allowed_user_ids: tuple[str, ...] = ()
    refresh_seconds: int = 60
    max_files: int = 2000
    max_file_bytes: int = 1048576
    max_total_bytes: int = 67108864
    chunk_chars: int = 2400
    chunk_overlap_chars: int = 200
    top_k: int = 6
    max_context_tokens: int = 4000


@dataclass(frozen=True)
class MemoryConfig:
    """长期记忆（全局记忆 Beta）配置（INTERFACES §26.1）；默认关闭。

    所有字段都可由 YAML 覆盖：本节没有「代码常量」。
    """

    enabled: bool = False
    access_mode: str = "allowlist"       # "allowlist" | "all"
    allow_user_list: tuple[str, ...] = ()
    admin_user_list: tuple[str, ...] = ()
    root_dir: str = "./data/memory"
    refresh_seconds: int = 10
    queue_size: int = 20
    max_common_entries_per_scope: int = 64
    max_private_entries_per_user: int = 32
    max_candidates: int = 128
    max_entry_chars: int = 500
    max_file_bytes: int = 262144
    max_operations: int = 512
    # 记忆自己的 token 上限（§33 的分组上限，两个都是「先给记忆封顶」的意思）：
    # 共同记忆（all_user 与 lobby）**合计**一份，私有记忆各一份；超上限的条目整条跳过，
    # 不会截断，也不会改变整轮预算（behavior/comments 的 context_input_tokens）的账目。
    common_context_tokens: int = 800
    private_context_tokens: int = 800
    # 公开个人记忆（§39；公开设计 §9）：第三类记忆的三个旋钮 —— 单个 owner 的公开条目上限、
    # 一轮最多选入的 owner 数、`memory_public_personal` 分组的渲染上限。
    max_public_entries_per_user: int = 8
    max_public_subjects_per_turn: int = 4
    public_personal_context_tokens: int = 600
    writer_context_tokens: int = 2000
    writer_timeout_seconds: float = 15.0
    auto_capture_available: bool = False


@dataclass(frozen=True)
class Secrets:
    """密钥集合；repr 必须脱敏。"""

    username: str
    password: str
    llm_api_key: str

    def __repr__(self) -> str:
        return (
            f"Secrets(username={self.username!r}, password='[redacted]', "
            f"llm_api_key='[redacted]')"
        )


@dataclass(frozen=True)
class StorageConfig:
    """存储与容量治理参数（D-20 / D-23）。"""

    db_path: str = "./data/bot.db"
    lobby_thread_retention_seconds: int = 604800
    cleanup_interval_seconds: int = 3600
    send_attempt_retention_seconds: int = 172800
    max_dm_channels: int = 10000
    sqlite_soft_limit_bytes: int = 134217728
    wal_journal_limit_bytes: int = 16777216


@dataclass(frozen=True)
class Config:
    """完整配置；不含 Cookie，也不含任何消息正文。"""

    site: SiteConfig
    model: ModelConfig
    behavior: BehaviorConfig
    storage: StorageConfig
    ops: OpsConfig
    log_level: str
    system_prompt: str
    system_prompt_sha256: str
    secrets: Secrets
    comments: CommentConfig = field(default_factory=CommentConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    knowledge_base: KnowledgeBaseConfig = field(default_factory=KnowledgeBaseConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    @property
    def db_path(self) -> str:
        """兼容别名，保留一个版本；新代码用 `config.storage.db_path`。"""
        return self.storage.db_path


def default_config_path(env: Mapping[str, str] | None = None) -> str:
    """返回默认配置文件路径：BOT_CONFIG_PATH 优先，否则 ./config.yaml。"""
    source = os.environ if env is None else env
    return source.get("BOT_CONFIG_PATH") or DEFAULT_CONFIG_PATH


def load_config(path: str | None = None, env: Mapping[str, str] | None = None) -> Config:
    """加载并校验配置；任何问题都以 ConfigError 抛出。"""
    source = os.environ if env is None else env
    resolved = path if path is not None else default_config_path(source)
    raw = _read_yaml(resolved)

    site_raw = _section(raw, "site")
    model_raw = _section(raw, "model")
    behavior_raw = _section(raw, "behavior")
    ops_raw = _section(raw, "ops")
    storage_raw = _section(raw, "storage")
    logging_raw = _section(raw, "logging")
    mcp = _mcp(_section(raw, "mcp"))

    site = SiteConfig(
        base_url=_base_url(site_raw, "site"),
        request_timeout_seconds=_positive_number(
            site_raw, "request_timeout_seconds", "site", 20.0
        ),
    )
    model = ModelConfig(
        base_url=_base_url(model_raw, "model"),
        model=_required_text(model_raw, "model", "model.model"),
        temperature=_temperature(model_raw),
        timeout_seconds=_positive_number(model_raw, "timeout_seconds", "model", 45.0),
        max_output_tokens=_positive_int(model_raw, "max_output_tokens", "model", 600),
        vision_enabled=_bool_flag(model_raw, "vision_enabled", "model", False),
        max_image_bytes=_max_image_bytes(model_raw),
    )
    behavior = _behavior(behavior_raw)
    storage = _storage(storage_raw)
    comments = _comments(_section(raw, "comments"))
    knowledge_base = _knowledge_base(_section(raw, "knowledge_base"), behavior)
    memory = _memory(
        _section(raw, "memory"), behavior, comments, knowledge_base, storage
    )
    ops = OpsConfig(
        host=_ops_host(ops_raw),
        port=_ops_port(ops_raw),
    )
    system_prompt = _required_text(raw, "system_prompt", "system_prompt")

    return Config(
        site=site,
        model=model,
        behavior=behavior,
        storage=storage,
        ops=ops,
        log_level=_text_with_default(logging_raw, "level", "logging", "INFO"),
        system_prompt=system_prompt,
        system_prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12],
        secrets=Secrets(
            username=_secret(source, USERNAME_ENV),
            password=_secret(source, PASSWORD_ENV),
            llm_api_key=_secret(source, LLM_API_KEY_ENV),
        ),
        comments=comments,
        mcp=mcp,
        knowledge_base=knowledge_base,
        memory=memory,
    )


def _read_yaml(path: str) -> dict[str, Any]:
    """有界读取 YAML 文件并保证顶层是映射；空文件视为空映射。

    上限是**代码常量**（`MAX_CONFIG_BYTES`），不是 YAML 里可改的项 ——
    它必须在解析配置之前就能判定。因此按字节读 `MAX_CONFIG_BYTES + 1`，
    多出来的那个字节只用来判断「超限了」。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{path}") from exc

    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(
            f"配置文件超过 {MAX_CONFIG_BYTES} 字节上限：{path}"
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"配置文件不是 UTF-8 文本：{path}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法的 YAML：{path}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是映射")
    return data


def _section(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    """取出一个顶层小节；缺失视为空小节，类型不对则报错。"""
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置小节 {name} 必须是映射")
    return value


def _required_text(container: Mapping[str, Any], key: str, path: str) -> str:
    """取必填文本；缺失或全为空白则报错。path 是用于报错的完整配置路径。"""
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"缺少必填配置 {path}")
    return value.strip()


def _text_with_default(
    container: Mapping[str, Any], key: str, where: str, default: str
) -> str:
    """取可选文本；缺失用默认值，类型不对或全为空白则报错。"""
    value = container.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"配置 {where}.{key} 必须是非空字符串")
    return value


def _base_url(container: Mapping[str, Any], where: str) -> str:
    """取 base_url，去尾斜杠并校验为 http/https。"""
    value = _required_text(container, "base_url", f"{where}.base_url")
    cleaned = value.rstrip("/")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"配置 {where}.base_url 必须是 http/https 地址")
    return cleaned


def _number(value: Any, where: str, key: str) -> float:
    """校验并转换数值；布尔与字符串都不算数值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置 {where}.{key} 必须是数字")
    return float(value)


def _positive_number(
    container: Mapping[str, Any], key: str, where: str, default: float
) -> float:
    """取正浮点数配置。"""
    number = _number(container.get(key, default), where, key)
    if number <= 0:
        raise ConfigError(f"配置 {where}.{key} 必须大于 0")
    return number


def _positive_int(container: Mapping[str, Any], key: str, where: str, default: int) -> int:
    """取不小于 1 的整数配置。"""
    value = container.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"配置 {where}.{key} 必须是整数")
    if value < 1:
        raise ConfigError(f"配置 {where}.{key} 必须大于等于 1")
    return value


def _temperature(container: Mapping[str, Any]) -> float:
    """取温度配置，合法区间 [0, 2]。"""
    value = _number(container.get("temperature", 0.4), "model", "temperature")
    if not 0 <= value <= 2:
        raise ConfigError("配置 model.temperature 必须在 0 到 2 之间")
    return value


def _bool_flag(
    container: Mapping[str, Any], key: str, where: str, default: bool
) -> bool:
    """取布尔开关；缺失用默认值，非布尔（含 "true" 这类字符串）一律报错。"""
    value = container.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"配置 {where}.{key} 必须是 true 或 false")
    return value


def _max_image_bytes(container: Mapping[str, Any]) -> int:
    """单图字节上限：正整数且不超过站点图床的 10 MiB 硬上限。"""
    value = _positive_int(container, "max_image_bytes", "model", 5 * 1024 * 1024)
    if value > MAX_IMAGE_BYTES:
        raise ConfigError(f"配置 model.max_image_bytes 不得超过 {MAX_IMAGE_BYTES}")
    return value


def _behavior(container: Mapping[str, Any]) -> BehaviorConfig:
    """构造行为配置，整数字段一律要求 >= 1。"""
    context_turns = _positive_int(container, "context_turns", "behavior", 10)
    context_input_tokens = _positive_int(container, "context_input_tokens", "behavior", 8000)
    max_input_chars = _positive_int(container, "max_input_chars", "behavior", 8000)
    quoted_blog_max_chars = _positive_int(
        container, "quoted_blog_max_chars", "behavior", 1000
    )
    content_ref_max_chars = _positive_int(
        container, "content_ref_max_chars", "behavior", 2000
    )
    max_output_chars = _positive_int(container, "max_output_chars", "behavior", 5000)
    concurrency = _positive_int(container, "concurrency", "behavior", 3)
    queue_size = _positive_int(container, "queue_size", "behavior", 50)
    minute_attempt_limit = _positive_int(container, "minute_attempt_limit", "behavior", 25)
    daily_normal_limit = _positive_int(container, "daily_normal_limit", "behavior", 1950)
    daily_absolute_limit = _positive_int(container, "daily_absolute_limit", "behavior", 2000)
    notice_cooldown_seconds = _positive_int(container, "notice_cooldown_seconds", "behavior", 300)

    if daily_normal_limit >= daily_absolute_limit:
        raise ConfigError("配置 behavior.daily_normal_limit 必须小于 daily_absolute_limit")

    return BehaviorConfig(
        context_turns=context_turns,
        context_input_tokens=context_input_tokens,
        max_input_chars=max_input_chars,
        quoted_blog_max_chars=quoted_blog_max_chars,
        content_ref_max_chars=content_ref_max_chars,
        max_output_chars=max_output_chars,
        concurrency=concurrency,
        queue_size=queue_size,
        minute_attempt_limit=minute_attempt_limit,
        daily_normal_limit=daily_normal_limit,
        daily_absolute_limit=daily_absolute_limit,
        notice_cooldown_seconds=notice_cooldown_seconds,
        reconnect_base_seconds=_number(
            container.get("reconnect_base_seconds", 3.0), "behavior", "reconnect_base_seconds"
        ),
        reconnect_max_seconds=_number(
            container.get("reconnect_max_seconds", 60.0), "behavior", "reconnect_max_seconds"
        ),
        ready_probe_seconds=_number(
            container.get("ready_probe_seconds", 300.0), "behavior", "ready_probe_seconds"
        ),
        rate_limit_wait_seconds=_number(
            container.get("rate_limit_wait_seconds", 60.0), "behavior", "rate_limit_wait_seconds"
        ),
    )


def _storage(container: Mapping[str, Any]) -> StorageConfig:
    """构造存储配置：整数字段一律为正整数，并校验字段间的关系。"""
    lobby_thread_retention_seconds = _positive_int(
        container, "lobby_thread_retention_seconds", "storage", 604800
    )
    cleanup_interval_seconds = _positive_int(
        container, "cleanup_interval_seconds", "storage", 3600
    )
    send_attempt_retention_seconds = _positive_int(
        container, "send_attempt_retention_seconds", "storage", 172800
    )
    max_dm_channels = _positive_int(container, "max_dm_channels", "storage", 10000)
    sqlite_soft_limit_bytes = _positive_int(
        container, "sqlite_soft_limit_bytes", "storage", 134217728
    )
    wal_journal_limit_bytes = _positive_int(
        container, "wal_journal_limit_bytes", "storage", 16777216
    )

    if cleanup_interval_seconds > lobby_thread_retention_seconds:
        raise ConfigError(
            "配置 storage.cleanup_interval_seconds 不能大于"
            " lobby_thread_retention_seconds"
        )
    if send_attempt_retention_seconds < 86400:
        # 至少要覆盖滚动 24 小时的配额窗口
        raise ConfigError("配置 storage.send_attempt_retention_seconds 不能小于 86400")
    if wal_journal_limit_bytes >= sqlite_soft_limit_bytes:
        raise ConfigError(
            "配置 storage.wal_journal_limit_bytes 必须小于 sqlite_soft_limit_bytes"
        )

    return StorageConfig(
        db_path=_text_with_default(container, "db_path", "storage", "./data/bot.db"),
        lobby_thread_retention_seconds=lobby_thread_retention_seconds,
        cleanup_interval_seconds=cleanup_interval_seconds,
        send_attempt_retention_seconds=send_attempt_retention_seconds,
        max_dm_channels=max_dm_channels,
        sqlite_soft_limit_bytes=sqlite_soft_limit_bytes,
        wal_journal_limit_bytes=wal_journal_limit_bytes,
    )


def _comments(container: Mapping[str, Any]) -> CommentConfig:
    """构造评论配置并执行设计文档中的交叉校验。"""
    enabled = container.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("配置 comments.enabled 必须是布尔值")

    def integer(key: str, default: int) -> int:
        return _positive_int(container, key, "comments", default)

    def non_negative_integer(key: str, default: int) -> int:
        """取不小于 0 的整数：0 在这里是有意义的值（评论侧不取图）。"""
        value = container.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"配置 comments.{key} 必须是整数")
        if value < 0:
            raise ConfigError(f"配置 comments.{key} 不能为负数")
        return value

    recent_poll_seconds = integer("recent_poll_seconds", 30)
    notification_poll_seconds = integer("notification_poll_seconds", 15)
    notification_max_pages = integer("notification_max_pages", 5)
    queue_size = integer("queue_size", 50)
    concurrency = integer("concurrency", 1)
    context_turns = integer("context_turns", 10)
    context_input_tokens = integer("context_input_tokens", 8000)
    article_max_chars = integer("article_max_chars", 1000)
    max_images_per_reply = non_negative_integer("max_images_per_reply", 3)
    max_output_chars = integer("max_output_chars", 5000)
    max_response_bytes = integer("max_response_bytes", 8 * 1024 * 1024)
    max_tree_nodes = integer("max_tree_nodes", 10000)
    unmatched_attempt_limit = integer("unmatched_attempt_limit", 5)
    minute_attempt_limit = integer("minute_attempt_limit", 20)
    daily_reply_limit = integer("daily_reply_limit", 1950)
    daily_absolute_limit = integer("daily_absolute_limit", 2000)
    article_cooldown_seconds = integer("article_cooldown_seconds", 5)
    conversation_retention_seconds = integer("conversation_retention_seconds", 2592000)
    dedupe_retention_seconds = integer("dedupe_retention_seconds", 7776000)
    retry_base_seconds = integer("retry_base_seconds", 5)
    retry_max_seconds = integer("retry_max_seconds", 300)
    server_backoff_seconds = integer("server_backoff_seconds", 3600)

    if concurrency != 1:
        raise ConfigError("配置 comments.concurrency 首版必须为 1")
    if not daily_reply_limit < daily_absolute_limit <= 2000:
        raise ConfigError(
            "配置 comments.daily_reply_limit 必须小于 daily_absolute_limit 且不大于 2000"
        )
    if max_output_chars > 5000:
        raise ConfigError("配置 comments.max_output_chars 不能大于 5000")
    if max_response_bytes > 8 * 1024 * 1024:
        raise ConfigError("配置 comments.max_response_bytes 不能大于 8 MiB")
    if max_tree_nodes > 10000:
        raise ConfigError("配置 comments.max_tree_nodes 不能大于 10000")
    if conversation_retention_seconds > dedupe_retention_seconds:
        raise ConfigError(
            "配置 comments.conversation_retention_seconds 不能大于 dedupe_retention_seconds"
        )
    if retry_base_seconds > retry_max_seconds:
        raise ConfigError("配置 comments.retry_base_seconds 不能大于 retry_max_seconds")

    return CommentConfig(
        enabled=enabled,
        recent_poll_seconds=recent_poll_seconds,
        notification_poll_seconds=notification_poll_seconds,
        notification_max_pages=notification_max_pages,
        queue_size=queue_size,
        concurrency=concurrency,
        context_turns=context_turns,
        context_input_tokens=context_input_tokens,
        article_max_chars=article_max_chars,
        max_images_per_reply=max_images_per_reply,
        max_output_chars=max_output_chars,
        max_response_bytes=max_response_bytes,
        max_tree_nodes=max_tree_nodes,
        unmatched_attempt_limit=unmatched_attempt_limit,
        minute_attempt_limit=minute_attempt_limit,
        daily_reply_limit=daily_reply_limit,
        daily_absolute_limit=daily_absolute_limit,
        article_cooldown_seconds=article_cooldown_seconds,
        conversation_retention_seconds=conversation_retention_seconds,
        dedupe_retention_seconds=dedupe_retention_seconds,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        server_backoff_seconds=server_backoff_seconds,
    )


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 疑似秘密的环境变量名片段。`APP_ID` 是后补的：Wolfram 的 WOLFRAM_APP_ID 不含
# KEY/TOKEN/SECRET 字样，却能通过 `env:` 明文写进 YAML —— 而 `env:` 的值从不会注册进
# Redactor，写进去就是一个永远不会被脱敏的秘密。
_SECRET_ENV_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "APP_ID")

# 传输方式。stdio = 启动子进程；sse = 连接远程 HTTP+SSE 端点（MCP 2024-11-05 传输）。
TRANSPORT_STDIO = "stdio"
TRANSPORT_SSE = "sse"


def _mcp(container: Mapping[str, Any]) -> McpConfig:
    """构造 MCP 配置；不解析或保存环境变量中的密钥值。"""
    enabled = _bool_flag(container, "enabled", "mcp", False)
    connect_timeout = _positive_number(
        container, "connect_timeout_seconds", "mcp", 10.0
    )
    call_timeout = _positive_number(container, "call_timeout_seconds", "mcp", 20.0)
    reconnect_base = _positive_number(
        container, "reconnect_base_seconds", "mcp", 3.0
    )
    reconnect_max = _positive_number(container, "reconnect_max_seconds", "mcp", 60.0)
    if reconnect_base > reconnect_max:
        raise ConfigError(
            "配置 mcp.reconnect_base_seconds 不能大于 reconnect_max_seconds"
        )

    raw_servers = container.get("servers", {})
    if not isinstance(raw_servers, dict):
        raise ConfigError("配置 mcp.servers 必须是映射")
    servers: dict[str, McpServerConfig] = {}
    for raw_name, raw_value in raw_servers.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigError("配置 mcp.servers 的服务器名必须是非空字符串")
        name = raw_name.strip()
        if name in servers:
            raise ConfigError(f"配置 mcp.servers 重复服务器：{name}")
        if not isinstance(raw_value, dict):
            raise ConfigError(f"配置 mcp.servers.{name} 必须是映射")
        server_enabled = _bool_flag(raw_value, "enabled", f"mcp.servers.{name}", True)
        raw_transport = raw_value.get("transport", "stdio")
        if not isinstance(raw_transport, str) or raw_transport.strip().lower() not in (
            TRANSPORT_STDIO,
            TRANSPORT_SSE,
        ):
            raise ConfigError(
                f"配置 mcp.servers.{name}.transport 只能是 {TRANSPORT_STDIO} 或 {TRANSPORT_SSE}"
            )
        transport = raw_transport.strip().lower()
        if transport == TRANSPORT_SSE:
            servers[name] = _mcp_sse_server(
                raw_value, name, server_enabled, raw_transport
            )
            continue
        command = raw_value.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(f"缺少必填配置 mcp.servers.{name}.command")
        # stdio 服务器不得带 SSE 字段：两套连接方式并存时谁生效取决于实现细节，
        # 直接拒绝，避免出现「以为在连远程、其实起的是子进程」的静默行为。
        _reject_server_fields(raw_value, name, ("url", "bearer_env", "stream_read_timeout_seconds"))
        args = _mcp_args(raw_value.get("args", []), name)
        env_from = _mcp_env_map(raw_value.get("env_from", {}), name, allow_secret=True)
        env = _mcp_env_map(raw_value.get("env", {}), name, allow_secret=False)
        overlap = set(env_from) & set(env)
        if overlap:
            raise ConfigError(
                f"配置 mcp.servers.{name} 的环境变量不能同时出现在 env_from 与 env"
            )
        account_pool = _mcp_account_pool(raw_value, name, env_from)
        servers[name] = McpServerConfig(
            name=name,
            enabled=server_enabled,
            transport=TRANSPORT_STDIO,
            command=command.strip(),
            args=args,
            env_from=env_from,
            env=env,
            account_pool=account_pool,
        )

    raw_features = container.get("features", {})
    if not isinstance(raw_features, dict):
        raise ConfigError("配置 mcp.features 必须是映射")
    features: dict[str, McpFeatureConfig] = {}
    for raw_name, raw_value in raw_features.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigError("配置 mcp.features 的名称必须是非空字符串")
        name = raw_name.strip()
        if name in features:
            raise ConfigError(f"配置 mcp.features 重复 feature：{name}")
        if not isinstance(raw_value, dict):
            raise ConfigError(f"配置 mcp.features.{name} 必须是映射")
        features[name] = _mcp_feature(raw_value, name, servers)

    return McpConfig(
        enabled=enabled,
        connect_timeout_seconds=connect_timeout,
        call_timeout_seconds=call_timeout,
        reconnect_base_seconds=reconnect_base,
        reconnect_max_seconds=reconnect_max,
        servers=servers,
        features=features,
    )


def _reject_server_fields(
    raw: Mapping[str, Any], name: str, forbidden: tuple[str, ...]
) -> None:
    """拒绝不属于当前传输的字段；出现即配置错误，不静默忽略。"""
    for field in forbidden:
        if field in raw:
            raise ConfigError(
                f"配置 mcp.servers.{name} 的 {field} 不适用于当前 transport"
            )


def _mcp_sse_server(
    raw: Mapping[str, Any], name: str, enabled: bool, transport: str
) -> McpServerConfig:
    """解析远程 SSE 服务器：只保存环境变量名，不读也不存它的值。

    `bearer_env` 是**宿主环境变量名**；值只在构造 Provider 时读取并登记进 Redactor，
    与 `env_from` 是同一条「密钥不进 YAML」的红线。
    """
    # 远程服务器没有子进程，因此子进程字段一律非法。
    _reject_server_fields(
        raw,
        name,
        ("command", "args", "env", "env_from", "account_pool"),
    )
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"缺少必填配置 mcp.servers.{name}.url")
    url = url.strip()
    if not url.lower().startswith("https://"):
        # Bearer 令牌会随每个请求发出，明文 http 等于把它交给链路上的任何人。
        raise ConfigError(f"配置 mcp.servers.{name}.url 必须是 https 地址")
    bearer_env = raw.get("bearer_env")
    if not isinstance(bearer_env, str) or not _ENV_NAME_RE.match(bearer_env.strip()):
        raise ConfigError(f"配置 mcp.servers.{name}.bearer_env 必须是合法的环境变量名")
    stream_read_timeout = _positive_number(
        raw, "stream_read_timeout_seconds", f"mcp.servers.{name}", 300.0
    )
    return McpServerConfig(
        name=name,
        enabled=enabled,
        transport=TRANSPORT_SSE,
        url=url,
        bearer_env=bearer_env.strip(),
        stream_read_timeout_seconds=stream_read_timeout,
    )


def _mcp_account_pool(
    server_raw: Mapping[str, Any], server: str, env_from: Mapping[str, str]
) -> McpAccountPoolConfig | None:
    """解析可选的 Exa 账户池配置；只保存环境变量名，不读也不存 Key 值。"""
    where = f"mcp.servers.{server}.account_pool"
    raw = server_raw.get("account_pool")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"配置 {where} 必须是映射")
    if env_from:
        # 两套凭证来源同时存在时，谁生效取决于实现细节；直接拒绝，避免出现
        # 「以为池在轮换、其实一直在用 env_from 那一个 Key」的静默行为。
        raise ConfigError(f"配置 mcp.servers.{server} 的 account_pool 与 env_from 互斥")

    child_env = raw.get("child_env", POOL_CHILD_ENV)
    if child_env != POOL_CHILD_ENV:
        raise ConfigError(f"配置 {where}.child_env 首版必须是 {POOL_CHILD_ENV}")
    strategy = raw.get("strategy", POOL_STRATEGY)
    if strategy != POOL_STRATEGY:
        raise ConfigError(f"配置 {where}.strategy 首版必须是 {POOL_STRATEGY}")

    raw_host_envs = raw.get("host_envs")
    if not isinstance(raw_host_envs, list) or any(
        not isinstance(item, str) for item in raw_host_envs
    ):
        raise ConfigError(f"配置 {where}.host_envs 必须是字符串列表")
    host_envs = tuple(item.strip() for item in raw_host_envs)
    for item in host_envs:
        if not _ENV_NAME_RE.fullmatch(item):
            raise ConfigError(f"配置 {where}.host_envs 必须使用合法的环境变量名")
    if len(host_envs) < POOL_MIN_SLOTS or len(host_envs) > POOL_MAX_SLOTS:
        raise ConfigError(
            f"配置 {where}.host_envs 的槽位数必须在 {POOL_MIN_SLOTS} 到 {POOL_MAX_SLOTS} 之间"
        )
    if len(set(host_envs)) != len(host_envs):
        raise ConfigError(f"配置 {where}.host_envs 不能出现重复的环境变量名")

    return McpAccountPoolConfig(
        child_env=child_env,
        host_envs=host_envs,
        strategy=strategy,
        rate_limit_cooldown_seconds=_positive_number(
            raw, "rate_limit_cooldown_seconds", where, 60.0
        ),
        transient_cooldown_seconds=_positive_number(
            raw, "transient_cooldown_seconds", where, 30.0
        ),
        quota_cooldown_seconds=_positive_number(
            raw, "quota_cooldown_seconds", where, 21600.0
        ),
    )


def _text_tuple(
    container: Mapping[str, Any], key: str, where: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    """取字符串列表配置；缺省用默认值，非列表或含非字符串项一律报错。"""
    value = container.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"配置 {where}.{key} 必须是字符串列表")
    return tuple(item.strip() for item in value)


def _knowledge_base(
    container: Mapping[str, Any], behavior: BehaviorConfig
) -> KnowledgeBaseConfig:
    """构造知识库配置；关闭时只做类型与硬上限校验，不施加启用才成立的交叉约束。"""
    where = "knowledge_base"
    enabled = _bool_flag(container, "enabled", where, False)
    root_dir = _text_with_default(container, "root_dir", where, "./knowledge")

    access_mode = container.get("access_mode", "allowlist")
    if access_mode not in ("allowlist", "all_chat"):
        raise ConfigError(f"配置 {where}.access_mode 首版必须是 allowlist 或 all_chat")

    kinds = _text_tuple(container, "allowed_channel_kinds", where, ("dm",))
    if not kinds:
        raise ConfigError(f"配置 {where}.allowed_channel_kinds 不能为空")
    for kind in kinds:
        if kind not in ("dm", "lobby"):
            raise ConfigError(f"配置 {where}.allowed_channel_kinds 只能是 dm 或 lobby")
    if len(set(kinds)) != len(kinds):
        raise ConfigError(f"配置 {where}.allowed_channel_kinds 不能重复")

    user_ids = _text_tuple(container, "allowed_user_ids", where, ())
    for user_id in user_ids:
        if not user_id:
            raise ConfigError(f"配置 {where}.allowed_user_ids 不能包含空字符串")

    refresh_seconds = _positive_int(container, "refresh_seconds", where, 60)
    max_files = _positive_int(container, "max_files", where, 2000)
    max_file_bytes = _positive_int(container, "max_file_bytes", where, 1048576)
    max_total_bytes = _positive_int(container, "max_total_bytes", where, 67108864)
    chunk_chars = _positive_int(container, "chunk_chars", where, 2400)
    chunk_overlap_chars = _positive_int(container, "chunk_overlap_chars", where, 200)
    top_k = _positive_int(container, "top_k", where, 6)
    max_context_tokens = _positive_int(container, "max_context_tokens", where, 4000)

    if refresh_seconds > KB_MAX_REFRESH_SECONDS:
        raise ConfigError(f"配置 {where}.refresh_seconds 不能大于 {KB_MAX_REFRESH_SECONDS}")
    if max_files > KB_MAX_FILES:
        raise ConfigError(f"配置 {where}.max_files 不能大于 {KB_MAX_FILES}")
    if max_file_bytes > KB_MAX_FILE_BYTES:
        raise ConfigError(f"配置 {where}.max_file_bytes 不能大于 {KB_MAX_FILE_BYTES}")
    if max_total_bytes > KB_MAX_TOTAL_BYTES:
        raise ConfigError(f"配置 {where}.max_total_bytes 不能大于 {KB_MAX_TOTAL_BYTES}")
    if chunk_chars > KB_MAX_CHUNK_CHARS:
        raise ConfigError(f"配置 {where}.chunk_chars 不能大于 {KB_MAX_CHUNK_CHARS}")
    if chunk_overlap_chars >= chunk_chars:
        raise ConfigError(f"配置 {where}.chunk_overlap_chars 必须小于 chunk_chars")
    if top_k > KB_MAX_TOP_K:
        raise ConfigError(f"配置 {where}.top_k 不能大于 {KB_MAX_TOP_K}")
    if max_context_tokens > KB_MAX_CONTEXT_TOKENS:
        raise ConfigError(
            f"配置 {where}.max_context_tokens 不能大于 {KB_MAX_CONTEXT_TOKENS}"
        )

    if enabled:
        # 这两条只在启用时成立：默认关闭的部署不该因为一个与它无关的默认值组合
        # （例如把 behavior.context_input_tokens 调得很小）而启动失败。
        if access_mode == "allowlist" and not user_ids:
            raise ConfigError(
                f"配置 {where}.allowed_user_ids 在 allowlist 模式下不能为空"
            )
        if max_context_tokens > behavior.context_input_tokens:
            raise ConfigError(
                f"配置 {where}.max_context_tokens 不能大于 behavior.context_input_tokens"
            )

    return KnowledgeBaseConfig(
        enabled=enabled,
        root_dir=root_dir,
        access_mode=access_mode,
        allowed_channel_kinds=kinds,
        allowed_user_ids=user_ids,
        refresh_seconds=refresh_seconds,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        chunk_chars=chunk_chars,
        chunk_overlap_chars=chunk_overlap_chars,
        top_k=top_k,
        max_context_tokens=max_context_tokens,
    )


def _path_contains(parent: str, child: str) -> bool:
    """child 是否位于 parent 目录内部；按真实路径包含判定，不做字符串前缀匹配。

    先各自取绝对路径再求公共前缀：`./knowledge2` 不是 `./knowledge` 的子目录，
    而字符串前缀法会把它误判成子目录。
    """
    parent_abs = os.path.abspath(parent)
    child_abs = os.path.abspath(child)
    try:
        common = os.path.commonpath([parent_abs, child_abs])
    except ValueError:
        # 不同盘符（Windows）等无法比较的路径：一定不构成包含关系。
        return False
    return os.path.normcase(common) == os.path.normcase(parent_abs)


def _check_memory_root_dir(root_dir: str, kb_root_dir: str, db_path: str) -> None:
    """记忆目录不得与数据库文件或知识库目录重叠（INTERFACES §26.2 第 10 条）。

    重叠会让记忆文件被 `/kb` 再次扫描并外送，或者让知识库文件被当成记忆条目。
    """
    if os.path.normcase(os.path.abspath(root_dir)) == os.path.normcase(
        os.path.abspath(db_path)
    ):
        raise ConfigError("配置 memory.root_dir 不能与 storage.db_path 相同")
    if _path_contains(kb_root_dir, root_dir):
        raise ConfigError("配置 memory.root_dir 不能位于 knowledge_base.root_dir 内")
    if _path_contains(root_dir, kb_root_dir):
        raise ConfigError("配置 memory.root_dir 不能包含 knowledge_base.root_dir")


def _memory(
    container: Mapping[str, Any],
    behavior: BehaviorConfig,
    comments: CommentConfig,
    knowledge_base: KnowledgeBaseConfig,
    storage: StorageConfig,
) -> MemoryConfig:
    """构造长期记忆配置；关闭时只做类型与单字段范围校验，交叉约束只在启用时施加。"""
    where = "memory"
    enabled = _bool_flag(container, "enabled", where, False)
    auto_capture_available = _bool_flag(container, "auto_capture_available", where, False)

    access_mode = container.get("access_mode", "allowlist")
    if access_mode not in ("allowlist", "all"):
        raise ConfigError(f"配置 {where}.access_mode 首版必须是 allowlist 或 all")

    allow_user_list = _text_tuple(container, "allow_user_list", where, ())
    admin_user_list = _text_tuple(container, "admin_user_list", where, ())
    for key, values in (
        ("allow_user_list", allow_user_list),
        ("admin_user_list", admin_user_list),
    ):
        if any(not user_id for user_id in values):
            raise ConfigError(f"配置 {where}.{key} 不能包含空字符串")
        if len(set(values)) != len(values):
            raise ConfigError(f"配置 {where}.{key} 不能重复")

    root_dir = _text_with_default(container, "root_dir", where, "./data/memory")
    refresh_seconds = _positive_int(container, "refresh_seconds", where, 10)
    queue_size = _positive_int(container, "queue_size", where, 20)
    max_common_entries_per_scope = _positive_int(
        container, "max_common_entries_per_scope", where, 64
    )
    max_private_entries_per_user = _positive_int(
        container, "max_private_entries_per_user", where, 32
    )
    max_candidates = _positive_int(container, "max_candidates", where, 128)
    max_entry_chars = _positive_int(container, "max_entry_chars", where, 500)
    max_file_bytes = _positive_int(container, "max_file_bytes", where, 262144)
    max_operations = _positive_int(container, "max_operations", where, 512)
    common_context_tokens = _positive_int(container, "common_context_tokens", where, 800)
    private_context_tokens = _positive_int(container, "private_context_tokens", where, 800)
    max_public_entries_per_user = _positive_int(
        container, "max_public_entries_per_user", where, 8
    )
    max_public_subjects_per_turn = _positive_int(
        container, "max_public_subjects_per_turn", where, 4
    )
    public_personal_context_tokens = _positive_int(
        container, "public_personal_context_tokens", where, 600
    )
    writer_context_tokens = _positive_int(container, "writer_context_tokens", where, 2000)
    writer_timeout_seconds = _positive_number(
        container, "writer_timeout_seconds", where, 15.0
    )

    if enabled:
        # 下面几条只在启用时成立：默认关闭的部署不该因为一个与它无关的默认值组合
        # （例如把 behavior.context_input_tokens 调得很小）而启动失败。
        if access_mode == "allowlist" and not set(admin_user_list) <= set(allow_user_list):
            raise ConfigError(
                f"配置 {where}.admin_user_list 必须是 allow_user_list 的子集"
            )
        if common_context_tokens + private_context_tokens > behavior.context_input_tokens:
            raise ConfigError(
                f"配置 {where}.common_context_tokens 与 private_context_tokens 之和"
                " 不能大于 behavior.context_input_tokens"
            )
        if common_context_tokens > comments.context_input_tokens:
            raise ConfigError(
                f"配置 {where}.common_context_tokens 不能大于"
                " comments.context_input_tokens"
            )
        if max_public_entries_per_user > max_private_entries_per_user:
            # 公开条目只可能是私人条目的快照：比私人上限还大的公开上限是死数（§39.2 第 2 条）。
            raise ConfigError(
                f"配置 {where}.max_public_entries_per_user 不能大于"
                " max_private_entries_per_user"
            )
        # 公开个人记忆与共同记忆并排在同一个分组预算里，两两之和都不得吃光整轮预算
        # （§39.2 第 4、5 条；第 3 条就是上面 common + private 那条，不重复判）。
        if (
            common_context_tokens + public_personal_context_tokens
            > behavior.context_input_tokens
        ):
            raise ConfigError(
                f"配置 {where}.common_context_tokens 与 public_personal_context_tokens 之和"
                " 不能大于 behavior.context_input_tokens"
            )
        if comments.enabled and (
            common_context_tokens + public_personal_context_tokens
            > comments.context_input_tokens
        ):
            raise ConfigError(
                f"配置 {where}.common_context_tokens 与 public_personal_context_tokens 之和"
                " 不能大于 comments.context_input_tokens"
            )
        if max_entry_chars > behavior.max_input_chars:
            raise ConfigError(
                f"配置 {where}.max_entry_chars 不能大于 behavior.max_input_chars"
            )
        if auto_capture_available:
            # 自动提取的披露要跟回答挤同一份输出空间（§34.4 / D-63）：装配层按
            # `limit = max_output_chars - len(披露) - len(TRUNCATION_SUFFIX)` 预留，披露本身
            # 是固定开销加上一条最长正文。上限装不下时 limit 会落到零以下，用户的回答就整条
            # 变成一句截断提示 —— D-60 明令禁止的结局，因此这里必须留出至少一个回答字符。
            # 只在部署真的开着自动提取时才查：用户那一侧的开关是运行时状态（启动时判不了），
            # 而它只有在部署允许时才可能被打开。
            disclosure_overhead = len(
                texts.memory_auto_capture_text(
                    memory_id=WORST_CASE_MEMORY_ID, content="", created=True
                )
            )
            if (
                disclosure_overhead + max_entry_chars + len(texts.TRUNCATION_SUFFIX)
                >= behavior.max_output_chars
            ):
                raise ConfigError(
                    f"配置 {where}.max_entry_chars 不能占满 behavior.max_output_chars："
                    "自动提取的写入披露与截断后缀还要留在同一条消息里"
                )
        _check_memory_root_dir(root_dir, knowledge_base.root_dir, storage.db_path)

    return MemoryConfig(
        enabled=enabled,
        access_mode=access_mode,
        allow_user_list=allow_user_list,
        admin_user_list=admin_user_list,
        root_dir=root_dir,
        refresh_seconds=refresh_seconds,
        queue_size=queue_size,
        max_common_entries_per_scope=max_common_entries_per_scope,
        max_private_entries_per_user=max_private_entries_per_user,
        max_candidates=max_candidates,
        max_entry_chars=max_entry_chars,
        max_file_bytes=max_file_bytes,
        max_operations=max_operations,
        common_context_tokens=common_context_tokens,
        private_context_tokens=private_context_tokens,
        max_public_entries_per_user=max_public_entries_per_user,
        max_public_subjects_per_turn=max_public_subjects_per_turn,
        public_personal_context_tokens=public_personal_context_tokens,
        writer_context_tokens=writer_context_tokens,
        writer_timeout_seconds=writer_timeout_seconds,
        auto_capture_available=auto_capture_available,
    )


def _mcp_args(value: Any, server: str) -> tuple[str, ...]:
    """校验 stdio 命令参数。"""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"配置 mcp.servers.{server}.args 必须是字符串列表")
    return tuple(value)


def _mcp_env_map(value: Any, server: str, *, allow_secret: bool) -> dict[str, str]:
    """校验子进程环境映射；敏感字段只允许通过 env_from 提供。"""
    if not isinstance(value, dict):
        key = "env_from" if allow_secret else "env"
        raise ConfigError(f"配置 mcp.servers.{server}.{key} 必须是映射")
    result: dict[str, str] = {}
    key_name = "env_from" if allow_secret else "env"
    for raw_key, raw_value in value.items():
        if (
            not isinstance(raw_key, str)
            or not _ENV_NAME_RE.fullmatch(raw_key)
            or not isinstance(raw_value, str)
            or not _ENV_NAME_RE.fullmatch(raw_value.strip())
        ):
            raise ConfigError(
                f"配置 mcp.servers.{server}.{key_name} 必须使用合法的环境变量名和值"
            )
        if not allow_secret and any(part in raw_key.upper() for part in _SECRET_ENV_PARTS):
            raise ConfigError(
                f"配置 mcp.servers.{server}.env 不得直接包含疑似敏感环境变量"
            )
        result[raw_key] = raw_value.strip() if allow_secret else raw_value
    return result


def _mcp_feature(
    container: Mapping[str, Any],
    name: str,
    servers: Mapping[str, McpServerConfig],
) -> McpFeatureConfig:
    """构造 feature 配置并执行首版安全上限校验。"""
    enabled = _bool_flag(container, "enabled", f"mcp.features.{name}", True)
    raw_bindings = container.get("bindings", [])
    if not isinstance(raw_bindings, list):
        raise ConfigError(f"配置 mcp.features.{name}.bindings 必须是列表")
    bindings: list[McpBindingConfig] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_bindings:
        if not isinstance(item, dict):
            raise ConfigError(f"配置 mcp.features.{name}.bindings 项必须是映射")
        server = item.get("server")
        tool = item.get("tool")
        if not isinstance(server, str) or not server.strip() or not isinstance(tool, str) or not tool.strip():
            raise ConfigError(f"配置 mcp.features.{name}.bindings 必须包含 server/tool")
        server, tool = server.strip(), tool.strip()
        if server not in servers:
            raise ConfigError(f"配置 mcp.features.{name} 引用了未知服务器：{server}")
        pair = (server, tool)
        if pair in seen:
            raise ConfigError(f"配置 mcp.features.{name} 存在重复工具绑定：{server}/{tool}")
        seen.add(pair)
        bindings.append(McpBindingConfig(server=server, tool=tool))

    # 白名单与绑定上限取自能力表（capabilities.py）—— 命令解析、帮助文案和这里用的是
    # 同一份声明，所以它们不可能各自漂移。未知 feature 名在这里就被拒绝：能力表是封闭的，
    # 加第五个能力要加一行表，而不是在 YAML 里随手写一个名字。
    spec = CAPABILITY_BY_FEATURE.get(name)
    if spec is None or spec.source != SOURCE_MCP:
        raise ConfigError(f"配置 mcp.features.{name} 不是已声明的 MCP 能力")
    if enabled and name not in IMPLEMENTED_FEATURES:
        # 没有适配器时 Registry 会走通用透传：把上游原文截断后直接交给模型。
        # 那既不是评审过的清洗形态，也绕过了逐条 token 上限，所以宁可在启动时报错。
        raise ConfigError(f"配置 mcp.features.{name} 的能力尚未实现，不能启用")
    if enabled and not bindings:
        raise ConfigError(f"配置 mcp.features.{name} 必须至少绑定一个工具")
    for binding in bindings:
        if binding.tool not in spec.allowed_tools:
            raise ConfigError(
                f"配置 mcp.features.{name} 不允许绑定工具：{binding.tool}"
            )
    if len(bindings) > spec.max_bindings:
        raise ConfigError(
            f"配置 mcp.features.{name} 的绑定数量不能超过 {spec.max_bindings}"
        )

    result_count = _positive_int(container, "result_count", f"mcp.features.{name}", 5)
    if result_count > 5:
        raise ConfigError(f"配置 mcp.features.{name}.result_count 不能大于 5")
    if spec.result_shape == SHAPE_SINGLE and result_count != 1:
        # `single` 的能力本轮只产出一条答案，`result_count` 参与的是
        # `result_count × result_item_token_limit` 这个整体上限，配成 5 就是 5 倍预算。
        raise ConfigError(
            f"配置 mcp.features.{name} 是整条答案型能力，result_count 必须为 1"
        )
    item_limit = _positive_int(
        container, "result_item_token_limit", f"mcp.features.{name}", 3000
    )
    history_limit = _positive_int(
        container, "history_item_token_limit", f"mcp.features.{name}", 500
    )
    if history_limit > item_limit:
        raise ConfigError(
            f"配置 mcp.features.{name}.history_item_token_limit 不能大于 result_item_token_limit"
        )
    # 默认值取能力表给该能力的上限，而不是全局的 500：知乎上游只收 2..100 字符，
    # 用 500 当默认会让每一个「看起来正常」的知乎配置在加载期就失败。
    max_query_chars = _positive_int(
        container, "max_query_chars", f"mcp.features.{name}", spec.max_query_chars
    )
    if max_query_chars > spec.max_query_chars:
        raise ConfigError(
            f"配置 mcp.features.{name}.max_query_chars 不能大于 {spec.max_query_chars}"
        )
    max_tool_calls = _positive_int(
        container, "max_tool_calls_per_turn", f"mcp.features.{name}", 1
    )
    if max_tool_calls != 1:
        raise ConfigError(f"配置 mcp.features.{name}.max_tool_calls_per_turn 必须为 1")
    max_concurrency = _positive_int(
        container, "max_concurrency", f"mcp.features.{name}", 1
    )
    if max_concurrency != 1:
        raise ConfigError(f"配置 mcp.features.{name}.max_concurrency 首版必须为 1")
    min_interval = _positive_number(
        container, "min_interval_seconds", f"mcp.features.{name}", 2.0
    )
    return McpFeatureConfig(
        name=name,
        enabled=enabled,
        bindings=tuple(bindings),
        result_count=result_count,
        result_item_token_limit=item_limit,
        history_item_token_limit=history_limit,
        max_query_chars=max_query_chars,
        max_tool_calls_per_turn=max_tool_calls,
        max_concurrency=max_concurrency,
        min_interval_seconds=min_interval,
    )


def _ops_host(container: Mapping[str, Any]) -> str:
    """取运维监听地址。"""
    value = container.get("host", "0.0.0.0")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("配置 ops.host 必须是非空字符串")
    return value


def _ops_port(container: Mapping[str, Any]) -> int:
    """取运维监听端口，合法区间 1..65535。"""
    value = container.get("port", 8080)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("配置 ops.port 必须是整数")
    if not 1 <= value <= 65535:
        raise ConfigError("配置 ops.port 必须在 1 到 65535 之间")
    return value


def _secret(env: Mapping[str, str], name: str) -> str:
    """从环境变量读取密钥；缺失或全为空白则报错（不打印取值）。"""
    value = env.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"缺少环境变量 {name}")
    return value
