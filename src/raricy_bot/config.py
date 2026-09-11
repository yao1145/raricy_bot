"""配置加载与校验。

非敏感配置来自 YAML；密钥只从环境变量读取，绝不写入配置文件。
所有配置对象都是 frozen dataclass，加载完成后不再变化。
"""

from __future__ import annotations

import hashlib
import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# 默认配置文件路径；可被环境变量 BOT_CONFIG_PATH 覆盖。
DEFAULT_CONFIG_PATH: str = "./config.yaml"

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


@dataclass(frozen=True)
class BehaviorConfig:
    """行为与配额配置。"""

    context_turns: int = 10
    context_input_tokens: int = 8000
    max_input_chars: int = 8000
    max_output_chars: int = 4000
    concurrency: int = 3
    queue_size: int = 50
    minute_attempt_limit: int = 25
    daily_normal_limit: int = 750
    daily_absolute_limit: int = 790
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
class Config:
    """完整配置；不含 Cookie，也不含任何消息正文。"""

    site: SiteConfig
    model: ModelConfig
    behavior: BehaviorConfig
    ops: OpsConfig
    db_path: str
    log_level: str
    system_prompt: str
    system_prompt_sha256: str
    secrets: Secrets


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
    )
    behavior = _behavior(behavior_raw)
    ops = OpsConfig(
        host=_ops_host(ops_raw),
        port=_ops_port(ops_raw),
    )
    system_prompt = _required_text(raw, "system_prompt", "system_prompt")

    return Config(
        site=site,
        model=model,
        behavior=behavior,
        ops=ops,
        db_path=_text_with_default(storage_raw, "db_path", "storage", "./data/bot.db"),
        log_level=_text_with_default(logging_raw, "level", "logging", "INFO"),
        system_prompt=system_prompt,
        system_prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12],
        secrets=Secrets(
            username=_secret(source, USERNAME_ENV),
            password=_secret(source, PASSWORD_ENV),
            llm_api_key=_secret(source, LLM_API_KEY_ENV),
        ),
    )


def _read_yaml(path: str) -> dict[str, Any]:
    """读取 YAML 文件并保证顶层是映射；空文件视为空映射。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{path}") from exc
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


def _behavior(container: Mapping[str, Any]) -> BehaviorConfig:
    """构造行为配置，整数字段一律要求 >= 1。"""
    context_turns = _positive_int(container, "context_turns", "behavior", 10)
    context_input_tokens = _positive_int(container, "context_input_tokens", "behavior", 8000)
    max_input_chars = _positive_int(container, "max_input_chars", "behavior", 8000)
    max_output_chars = _positive_int(container, "max_output_chars", "behavior", 4000)
    concurrency = _positive_int(container, "concurrency", "behavior", 3)
    queue_size = _positive_int(container, "queue_size", "behavior", 50)
    minute_attempt_limit = _positive_int(container, "minute_attempt_limit", "behavior", 25)
    daily_normal_limit = _positive_int(container, "daily_normal_limit", "behavior", 750)
    daily_absolute_limit = _positive_int(container, "daily_absolute_limit", "behavior", 790)
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
