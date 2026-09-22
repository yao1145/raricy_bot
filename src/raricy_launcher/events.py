"""有界事件缓冲与 SSE 订阅（LIGHT_EDITION_DESIGN §12）。

- 事件字段过 `logging_setup.build_event()` 的白名单：聊天正文、提示词、模型
  请求响应、密码、Key、Cookie 与测试请求体都进不来 —— 与运行日志同一份规则。
- 环形缓冲有上限（默认 500 条）；游标过旧或来自上一个 Controller 实例时明确
  报缺口，不伪称完整回放。
- 事件 ID 由当前实例 ID 与单调序号组成；慢消费者丢帧而不是拖住发布方。
"""

from __future__ import annotations

import itertools
import queue
import threading
from dataclasses import dataclass

from raricy_bot.logging_setup import build_event

RING_CAPACITY: int = 500
SUBSCRIBER_QUEUE_SIZE: int = 100
MAX_SUBSCRIBERS: int = 8


@dataclass(frozen=True)
class Event:
    """一条已清洗的近期事件。"""

    event_id: str
    seq: int
    at: float
    level: str
    name: str
    fields: tuple[tuple[str, int | float | str], ...]

    def as_dict(self) -> dict:
        return {
            "id": self.event_id,
            "at": self.at,
            "level": self.level,
            "event": self.name,
            "fields": dict(self.fields),
        }


class EventService:
    """进程内的有界事件缓冲（§12）。"""

    def __init__(
        self,
        *,
        instance_id: str,
        clock,
        capacity: int = RING_CAPACITY,
        subscriber_queue_size: int = SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        self._instance_id = instance_id
        self._clock = clock
        self._capacity = capacity
        self._subscriber_queue_size = subscriber_queue_size
        self._lock = threading.Lock()
        self._ring: list[Event] = []
        self._sequence = itertools.count(1)
        self._subscribers: list[queue.Queue[Event | None]] = []
        self._dropped = 0

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def dropped(self) -> int:
        """慢消费者丢掉的帧数；只用于诊断。"""
        return self._dropped

    def publish(self, name: str, *, level: str = "info", at: float | None = None, **fields) -> Event:
        """发布一条事件；字段经白名单清洗，超限字段直接丢弃。"""
        payload = build_event(name, fields)
        seq = next(self._sequence)
        event = Event(
            event_id=f"{self._instance_id}-{seq}",
            seq=seq,
            at=self._clock() if at is None else at,
            level=level,
            name=payload.name,
            fields=payload.fields,
        )
        with self._lock:
            self._ring.append(event)
            if len(self._ring) > self._capacity:
                del self._ring[: len(self._ring) - self._capacity]
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # 慢消费者：丢帧，不拖住发布方；游标恢复时如实报缺口。
                self._dropped += 1
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
        return event

    def replay(self, after_event_id: str | None) -> tuple[list[Event], str | None]:
        """回放 `after_event_id` 之后的事件；返回 (事件, 缺口原因)。

        缺口原因取 `none`（无缺口）、`stale`（游标早于缓冲起点）或
        `foreign`（游标来自别的实例）。调用方据此发 reset/gap 提示（§12）。
        """
        with self._lock:
            events = list(self._ring)
        if after_event_id is None:
            return events, "none"
        instance, _, raw_seq = after_event_id.rpartition("-")
        if instance != self._instance_id or not raw_seq.isdigit():
            return events, "foreign"
        after_seq = int(raw_seq)
        if events and after_seq < events[0].seq - 1:
            return events, "stale"
        return [event for event in events if event.seq > after_seq], "none"

    def oldest_event_id(self) -> str | None:
        with self._lock:
            return self._ring[0].event_id if self._ring else None

    # --- 订阅 -------------------------------------------------------------

    def subscribe(self) -> queue.Queue[Event | None] | None:
        """订阅实时事件；已有太多订阅者时返回 None（不无限接纳）。"""
        with self._lock:
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                return None
            subscriber: queue.Queue[Event | None] = queue.Queue(
                maxsize=self._subscriber_queue_size
            )
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[Event | None]) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
        try:
            subscriber.put_nowait(None)
        except queue.Full:
            pass

    def close(self) -> None:
        """关闭全部订阅（退出时调用）。"""
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(None)
            except queue.Full:
                pass
