"""站点测试与启停共用的生命周期门（F3、D-132、§59）。

一个**进程内、非阻塞、线程安全**的租约门：站点测试在整段执行期间持有租约，
启停操作只在「取租约 → 调用管理器」这一小段里持有。两者互斥，使「检查进程
状态 → 派发」之间没有窗口能让另一个入口溜进来 —— 修复前 `POST /api/test/site`
只在开始时看一次 `manager.state`，测试交给线程之后 `POST /api/bot/start`
完全不看是否有测试在途。

不变量：

- 任一时刻站点测试最多一个持有者；持有站点测试租约时任何启停操作都取不到门。
- 操作之间**不**互斥：启停的串行与抢占（`stop` 抢 `starting`）仍由
  `WorkerManager` 自己负责，本门只解决跨入口（测试 vs 启停）的竞态。
- 取租约与释放都**不等待**：持锁期间只做内存操作，绝不等 Worker、网络或
  keyring（§5.1 第 3 条）。取不到门立刻返回 `None`，由调用方回 409，不排队。
- `end()` 幂等：重复释放不会顺手放开别人还持有的租约。

租约的释放责任在持有者自己：站点测试由执行它的**工作线程**在 `finally` 里
释放（请求协程被取消时线程仍在跑，提前释放等于把门开在测试进行中）；启停
操作由发起请求的调用方在 `finally` 里释放，`manager` 调用抛异常也不例外。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# 站点测试的类型码：它挡所有入口；其他类型码只挡站点测试。
TEST_KIND: str = "site_test"


@dataclass(frozen=True)
class Ticket:
    """一次租约的凭据。

    `kind` 记录当时持门的是谁（`site_test` 或 `start` / `stop` / `restart`），
    操作记录与诊断据此说明「当时被谁挡着」；N0 不为此引入持久化或操作日志。
    """

    ticket_id: int
    kind: str


class LifecycleGate:
    """进程内的生命周期租约门；调用方共享同一个实例才有意义（控制器装配一个）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[int, Ticket] = {}
        self._next_id = 0

    # --- 取租约 -----------------------------------------------------------

    def begin_test(self) -> Ticket | None:
        """站点测试取租约；任何持有者（测试或操作）在门内时返回 None。"""
        return self._acquire(TEST_KIND)

    def begin_operation(self, kind: str) -> Ticket | None:
        """启停操作取租约；只有站点测试能挡住它，操作之间不互斥。"""
        if kind == TEST_KIND:
            # 冒用测试类型会静默造出第二个测试租约，直接拒绝而不是照单全收。
            raise ValueError("test_kind_is_reserved")
        return self._acquire(kind)

    def _acquire(self, kind: str) -> Ticket | None:
        """取租约的公共路径：判据是类型码，临界区里只有内存操作，绝不等待。"""
        with self._lock:
            if self._blocked_by(kind):
                return None
            ticket = Ticket(ticket_id=self._next_id, kind=kind)
            self._next_id += 1
            self._tickets[ticket.ticket_id] = ticket
            return ticket

    def _blocked_by(self, kind: str) -> bool:
        """站点测试挡所有入口；其他入口只被站点测试挡。"""
        if kind == TEST_KIND:
            return bool(self._tickets)
        return any(ticket.kind == TEST_KIND for ticket in self._tickets.values())

    # --- 释放 -------------------------------------------------------------

    def end(self, ticket: Ticket) -> None:
        """释放租约；幂等（重复释放、释放已被释放的票据都是无操作）。"""
        with self._lock:
            self._tickets.pop(ticket.ticket_id, None)
