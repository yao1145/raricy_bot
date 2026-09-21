"""密钥脱敏：日志与出站错误信息在写出前统一过一遍替换。

替换按密钥长度降序进行，避免短密钥先命中而破坏长密钥；内部用锁保证线程安全
（`Store` 会在工作线程里调用）。

`SecretRegistry` 把「登记一次」变成唯一的入口：此前每个调用点都要手工登记两次
（出站 `Redactor` 一次、`logging_setup` 的进程级单例一次），漏掉任何一次都不会
报错，只会让某一条通路上的密钥原样落出去。
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


class SecretRegistry:
    """进程内凭据登记中心：一次登记同时送达日志层与出站 Redactor。

    出站 Redactor 与日志层过滤器是**两个不同对象**（前者管交给模型/站点的文本，
    后者管落盘与 stderr），此前每个调用点都得自己记住「两处都要登记」，注释里
    也确实这么写着。那是一种只在漏了的时候才发作的约定，所以改成结构约束。

    另一条约束是**保留旧值**：凭据轮换后，迟到返回的旧请求仍带着轮换前的密钥，
    登记表不淘汰旧值才不会让它们漏出去。

    后加入的 Redactor 会补上此前登记的凭据，装配顺序（先建客户端还是先建日志）
    不再影响脱敏结果。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._targets: list[Redactor] = []
        self._values: list[str] = []

    def register(self, value: str | None) -> None:
        """登记一个凭据；空值、None 与重复值直接忽略。"""
        if not value:
            return
        with self._lock:
            if value in self._values:
                return
            self._values.append(value)
            targets = tuple(self._targets)
        for target in targets:
            target.add_secret(value)

    def attach(self, *redactors: Redactor) -> None:
        """订阅登记表；新订阅者立刻拿到此前登记的全部凭据。"""
        with self._lock:
            fresh = [
                redactor
                for redactor in redactors
                if not any(redactor is known for known in self._targets)
            ]
            self._targets.extend(fresh)
            values = tuple(self._values)
        for redactor in fresh:
            for value in values:
                redactor.add_secret(value)

    def detach(self, redactor: Redactor) -> None:
        """解除订阅；换用独立实例（测试隔离）时调用。"""
        with self._lock:
            self._targets = [known for known in self._targets if known is not redactor]

    @property
    def secrets(self) -> tuple[str, ...]:
        """当前已登记的凭据快照，只读；测试用。"""
        with self._lock:
            return tuple(self._values)
