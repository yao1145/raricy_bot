"""密钥脱敏：日志与出站错误信息在写出前统一过一遍替换。

替换按密钥长度降序进行，避免短密钥先命中而破坏长密钥；内部用锁保证线程安全
（`Store` 会在工作线程里调用）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any

# 脱敏后的占位符，与 INTERFACES.md §3 一致。
REDACTED: str = "[redacted]"


class Redactor:
    """维护密钥列表，把文本中出现过的密钥替换为占位符。"""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        # 列表始终按长度降序保持，替换时按此顺序逐个处理。
        self._secrets: list[str] = []
        for value in secrets:
            self.add_secret(value)

    def add_secret(self, value: str) -> None:
        """注册一个密钥；空串、None 或已存在的值直接忽略。"""
        if not value:
            return
        with self._lock:
            if value in self._secrets:
                return
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    @property
    def secrets(self) -> tuple[str, ...]:
        """当前密钥快照，只读；测试用。"""
        with self._lock:
            return tuple(self._secrets)

    def redact(self, text: str) -> str:
        """把文本中出现的所有密钥替换为占位符。"""
        with self._lock:
            snapshot = tuple(self._secrets)
        for value in snapshot:
            text = text.replace(value, REDACTED)
        return text

    def redact_mapping(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """递归处理映射的值：str 脱敏，list/dict 逐层展开，其余类型原样返回。"""
        return {key: self._redact_value(value) for key, value in data.items()}

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return self.redact_mapping(value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value
