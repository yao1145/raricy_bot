"""会话调度器：把「等待同一会话」的请求挡在执行容量之外。

背景（计划 §5）：`WorkerPool` 原先的顺序是「先从全局队列取一条，再等会话锁」。
三个 worker 遇到同一会话的三条请求时，一个在跑、两个在等锁，执行槽位被空耗，
**其他会话的请求排在队里没人领**。本模块把「取请求」与「会话串行」合并成一件事：

- 每个会话一条 FIFO 请求队列（`_pending`）；
- 另维护一条「可运行会话」队列（`_runnable`），工作器只从这里领取；
- 被领取的会话进入 `_active`，它的后续请求留在 `_pending` 里，不占工作器，
  等本条结束后再把会话放回可运行队列**尾部**，让其他会话先拿到执行机会。

容量口径（写进 docstring，避免后来人误读）：

- `qsize()` / `full()` / `empty()` 描述的是**未派发的等待请求数**，包含 active 会话
  后面的积压。这与 `asyncio.Queue` 的语义一致 —— `get()` 之后的元素不再计入 `qsize()`。
  这样 `queue_depth` 指标、Router 入队与 readiness 的「busy」判定都不需要改口径。
- `active_count` 描述的是**执行中的请求数**（正在被 worker 处理的会话数），上限是
  `WorkerPool` 的 `concurrency`，不占用本调度器的 `maxsize`。

请求总量仍受界限约束：任何时刻 `_pending` 里的等待请求数都不超过 `maxsize`，
不存在把请求搬进无界内部队列来制造虚假空闲容量的做法。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Generic, Protocol, TypeVar

RequestT = TypeVar("RequestT")


class EnqueueQueue(Protocol[RequestT]):
    """只要求入队能力的队列形状；`MessageRouter` 只依赖这一点。

    `asyncio.Queue`（测试直接注入）与 `SessionScheduler` 都结构性地满足它。
    """

    def put_nowait(self, request: RequestT) -> None:
        """非阻塞入队；容量已满时抛 `asyncio.QueueFull`。"""
        ...


class RunnableQueue(Protocol[RequestT]):
    """`WorkerPool` 需要的队列形状：领取一条可运行会话的请求并登记完成。"""

    def get_runnable(self) -> Awaitable[RequestT]:
        """等待并返回一条**可运行会话**的请求，同时把该会话标为 active。"""
        ...

    def task_done(self, request: RequestT) -> None:
        """终结一条已领取的请求：清除 active，有积压则把会话放回可运行队列尾部。"""
        ...


def _default_session_key(request: object) -> str:
    """默认按 `session_key` 属性取会话键（`SessionRequest` 协议）。"""
    return getattr(request, "session_key")


class SessionScheduler(Generic[RequestT], EnqueueQueue[RequestT], RunnableQueue[RequestT]):
    """有界会话调度器：按会话 FIFO、按可运行会话公平派发，并保留完成计数。

    实例同时是 `WorkerPool` 的消费队列（`get_runnable` / `task_done`）和 Router 的入队
    目标（`put_nowait`），并保留 `Queue` 形状的 `qsize` / `empty` / `full` / `maxsize`，
    让 `app.py` 的 readiness、`queue_depth` 与既有 busy 语义无需改口径。
    """

    def __init__(
        self,
        *,
        maxsize: int,
        session_key: Callable[[RequestT], str] = _default_session_key,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize 必须为正数")
        self._maxsize = maxsize
        self._session_key = session_key
        # 每个会话的等待队列（FIFO）。只有「有积压或正在执行」的会话才在这里留下条目。
        self._pending: dict[str, deque[RequestT]] = {}
        # 可运行会话队列：每个会话至多出现一次；派发时从头部取，重新可运行时加到尾部，
        # 由此在多个持续积压的会话之间形成公平轮转，不会饿死。
        self._runnable: deque[str] = deque()
        self._runnable_set: set[str] = set()
        # 正在被 worker 处理的会话；已进入这里的会话在完成前不再回到可运行队列。
        self._active: set[str] = set()
        # 未派发的等待请求总数（容量口径）；包含 active 会话后面的积压。
        self._waiting = 0
        # 完成计数：put_nowait 加一，task_done 减一；归零时唤醒 join()。
        self._unfinished = 0
        self._idle = asyncio.Event()
        self._idle.set()
        # 有可运行会话时置位，唤醒正在等待的 worker。
        self._wakeup = asyncio.Event()

    # --- Queue 形状（入队、容量、完成计数）------------------------------------

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def qsize(self) -> int:
        """未派发的等待请求数（含 active 会话后面的积压）。"""
        return self._waiting

    def empty(self) -> bool:
        """没有等待中的请求时为 True；不否认有请求正在执行。"""
        return self._waiting == 0

    def full(self) -> bool:
        """等待请求数达到上限时为 True；执行中的请求不占这个额度。"""
        return self._waiting >= self._maxsize

    def put_nowait(self, request: RequestT) -> None:
        """非阻塞入队；等待请求数已达上限时抛 `asyncio.QueueFull`。

        沿用 `asyncio.QueueFull` 的异常契约，Router 的入队与 busy 判定逐字不变。
        """
        if self._waiting >= self._maxsize:
            raise asyncio.QueueFull
        key = self._session_key(request)
        bucket = self._pending.get(key)
        if bucket is None:
            bucket = deque()
            self._pending[key] = bucket
        bucket.append(request)
        self._waiting += 1
        self._unfinished += 1
        self._idle.clear()
        # 会话正在执行时不入可运行队列：它的积压由 task_done 统一放回。
        if key not in self._active:
            self._enqueue_runnable(key)

    def task_done(self, request: RequestT) -> None:
        """终结一条已领取的请求并完成调度清理。

        - 清除该会话的 active；
        - 若仍有积压，把会话放到可运行队列**尾部**（公平轮转）；
        - 减完成计数，归零时唤醒 `join()`。

        必须无条件调用（含 handler 异常与取消），否则活跃计数会失衡、`join()` 会挂住。
        """
        key = self._session_key(request)
        self._active.discard(key)
        if self._pending.get(key):
            self._enqueue_runnable(key)
        self._unfinished -= 1
        if self._unfinished <= 0:
            self._unfinished = 0
            self._idle.set()

    async def join(self) -> None:
        """阻塞到所有已入队请求都调用了 `task_done`。"""
        while self._unfinished:
            self._idle.clear()
            await self._idle.wait()

    # --- 调度（仅供 WorkerPool 使用）-----------------------------------------

    async def get_runnable(self) -> RequestT:
        """等待并领取一条可运行会话的请求，同时把该会话标为 active。

        没有可运行会话时挂起，直到 `put_nowait` 或 `task_done` 让某个会话重新可运行。
        """
        while True:
            if self._runnable:
                key = self._runnable.popleft()
                self._runnable_set.discard(key)
                bucket = self._pending.get(key)
                if not bucket:
                    # 防御性跳过：正常流程不会出现可运行但无请求的会话。
                    continue
                request = bucket.popleft()
                self._waiting -= 1
                if not bucket:
                    del self._pending[key]
                self._active.add(key)
                return request
            self._wakeup.clear()
            await self._wakeup.wait()

    # --- 观测 ----------------------------------------------------------------

    @property
    def active_count(self) -> int:
        """执行中的请求数（正在被 worker 处理的会话数），上限为 worker 并发数。"""
        return len(self._active)

    @property
    def tracked_sessions(self) -> int:
        """仍保留内部状态的会话数（有积压或正在执行）；用于监控状态回收。"""
        return len(set(self._pending) | self._active)

    # --- 内部实现 ------------------------------------------------------------

    def _enqueue_runnable(self, key: str) -> None:
        """把会话放到可运行队列尾部一次，并唤醒等待中的 worker。"""
        if key in self._runnable_set:
            return
        self._runnable.append(key)
        self._runnable_set.add(key)
        self._wakeup.set()


__all__ = [
    "EnqueueQueue",
    "RunnableQueue",
    "SessionScheduler",
]
