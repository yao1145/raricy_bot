"""配置加载与校验。

非敏感配置来自 YAML；密钥只从环境变量读取，绝不写入配置文件。
所有配置对象都是 frozen dataclass，加载完成后不再变化。
"""

from __future__ import annotations

import hashlib
import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

# 默认配置文件路径；可被环境变量 BOT_CONFIG_PATH 覆盖。
DEFAULT_CONFIG_PATH: str = "./config.yaml"

# 配置文件大小硬上限（1 MiB）。是代码常量而不是 YAML 项：必须在解析配置之前就能判定。
MAX_CONFIG_BYTES: int = 1024 * 1024

# 站点图床的单图硬上限（10 MiB）。来源：raricy.com src/lib/image-upload.ts 的
# MAX_IMAGE_SIZE。配得比它更大的话站点根本不会给出那么大的图，只会掩盖意图。
MAX_IMAGE_BYTES: int = 10 * 1024 * 1024

# 密钥环境变量名。
USERNAME_ENV: str = "RARICY_USERNAME"
PASSWORD_ENV: str = "RARICY_PASSWORD"
LLM_API_KEY_ENV: str = "LLM_API_KEY"


class ConfigError(Exception):
    """配置缺失、格式非法或取值越界。"""


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

    recent_poll_seconds = integer("recent_poll_seconds", 30)
    notification_poll_seconds = integer("notification_poll_seconds", 15)
    notification_max_pages = integer("notification_max_pages", 5)
    queue_size = integer("queue_size", 50)
    concurrency = integer("concurrency", 1)
    context_turns = integer("context_turns", 10)
    context_input_tokens = integer("context_input_tokens", 8000)
    article_max_chars = integer("article_max_chars", 1000)
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
