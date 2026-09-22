"""凭据库：系统凭据库与会话内存两种实现（LIGHT_EDITION_DESIGN §7）。

- **引用**（reference）是不透明随机标识：只用于在库里定位一组凭据，本身不含账号、
  密码或任何可读的凭据材料，可以安全地写进配置与日志（§6.3）。
- 桌面默认系统凭据库（`keyring` 的系统安全后端）。**禁止**自动选择明文文件后端，
  失败时也**不**写 `.env`：宁可报告不可用，也不把密码落到磁盘上的明文文件（§7）。
  `keyring` 在真正需要时才导入 —— 会话内存模式不需要它。
- 会话内存模式：Controller 重启后引用不再可解析，调用方据此进入
  `needs_credentials`，不假装已持久保存（§6.4 最后一段）。
"""

from __future__ import annotations

import json
import secrets as _secrets
from dataclasses import dataclass
from typing import Protocol

from raricy_bot.config import Secrets

# 系统凭据库里的服务名；引用作为「用户名」槽位，凭据载荷作为「密码」槽位。
KEYRING_SERVICE: str = "RaricyBotLight"

# 明确拒绝的后端名片段：空后端、明文文件后端、必然失败的后端。
# 命中即视为「没有可用的安全后端」，由调用方降级到会话内存并如实报告（§7）。
_REFUSED_BACKEND_TOKENS: tuple[str, ...] = ("null", "plaintext", "fail")

_PAYLOAD_FIELDS: tuple[str, ...] = ("username", "password", "llm_api_key")


class CredentialStoreError(Exception):
    """凭据库操作的固定错误；消息是稳定类别码，绝不含凭据取值。"""


@dataclass(frozen=True)
class BackendStatus:
    """后端的对外描述：只有名字与可用性。"""

    name: str
    available: bool


class CredentialStore(Protocol):
    """凭据库接口；实现必须只接受不透明引用，不接受账号组合。"""

    def describe(self) -> BackendStatus: ...

    def put(self, reference: str, values: Secrets) -> None: ...

    def get(self, reference: str) -> Secrets | None: ...

    def delete(self, reference: str) -> None: ...


def new_reference() -> str:
    """新的不透明引用：随机、无账号与密钥材料，可安全落进配置（§6.3）。"""
    return _secrets.token_urlsafe(16)


class SessionMemoryStore:
    """仅本次运行的凭据库：进程结束即消失，引用随之不可解析。"""

    def __init__(self) -> None:
        self._values: dict[str, Secrets] = {}

    def describe(self) -> BackendStatus:
        return BackendStatus(name="session", available=True)

    def put(self, reference: str, values: Secrets) -> None:
        self._values[reference] = values

    def get(self, reference: str) -> Secrets | None:
        return self._values.get(reference)

    def delete(self, reference: str) -> None:
        self._values.pop(reference, None)


class SystemKeyringStore:
    """系统凭据库（`keyring`）；后端不合规时如实报告不可用。

    不可用**不是**错误：调用方据此改用会话内存（并在界面上说明内存模式在重启
    后需要重新填写，§5.1 第 6 点）。
    """

    def __init__(self, *, service: str = KEYRING_SERVICE) -> None:
        self._service = service

    def _backend(self):
        """返回可用的 keyring 模块；导入失败或后端被拒时返回 None。"""
        try:
            import keyring
        except Exception:
            # 没装 keyring、或它自身初始化失败：对调用方都只是「不可用」。
            return None
        try:
            backend = keyring.get_keyring()
        except Exception:
            return None
        name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
        if any(token in name for token in _REFUSED_BACKEND_TOKENS):
            return None
        return keyring

    def describe(self) -> BackendStatus:
        return BackendStatus(name="keyring", available=self._backend() is not None)

    def _require_backend(self):
        module = self._backend()
        if module is None:
            raise CredentialStoreError("credential_backend_unavailable")
        return module

    def put(self, reference: str, values: Secrets) -> None:
        module = self._require_backend()
        payload = json.dumps(
            {
                "username": values.username,
                "password": values.password,
                "llm_api_key": values.llm_api_key,
            },
            ensure_ascii=False,
        )
        try:
            module.set_password(self._service, reference, payload)
        except Exception as exc:
            # 后端被锁、被拒绝或写失败：只报稳定类别码，绝不带上载荷。
            raise CredentialStoreError("credential_write_failed") from exc

    def get(self, reference: str) -> Secrets | None:
        module = self._require_backend()
        try:
            payload = module.get_password(self._service, reference)
        except Exception as exc:
            raise CredentialStoreError("credential_read_failed") from exc
        if payload is None:
            return None
        try:
            parsed = json.loads(payload)
        except ValueError as exc:
            raise CredentialStoreError("credential_payload_invalid") from exc
        if not isinstance(parsed, dict) or set(parsed) != set(_PAYLOAD_FIELDS):
            raise CredentialStoreError("credential_payload_invalid")
        if not all(isinstance(parsed[field], str) for field in _PAYLOAD_FIELDS):
            raise CredentialStoreError("credential_payload_invalid")
        return Secrets(
            username=parsed["username"],
            password=parsed["password"],
            llm_api_key=parsed["llm_api_key"],
        )

    def delete(self, reference: str) -> None:
        module = self._require_backend()
        try:
            module.delete_password(self._service, reference)
        except Exception as exc:
            # 「本来就不存在」与「删除成功」在调用方看来是同一件事：该引用不再被使用。
            errors = getattr(module, "errors", None)
            if errors is not None and isinstance(exc, errors.PasswordDeleteError):
                return
            raise CredentialStoreError("credential_delete_failed") from exc
