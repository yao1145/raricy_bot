"""状态聚合：配置就绪、进程状态、Worker 上报与显式测试结果（§9.1、§10.2）。

三条口径与探针一致：

- 配置就绪状态与进程状态**分开**，不让一个 `running` 布尔覆盖所有情况；
- Worker 快照带采样时间，过期或从未上报就如实标 `stale` / `unknown`，
  不从历史日志猜当前状态；
- 显式测试结果带产生它的 revision：配置或凭据一变，旧结果就标为过期。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# 快照失效阈值：超过它就不再声称这些事实仍然成立（§10.2）。
SNAPSHOT_STALE_SECONDS: float = 30.0

FRESH: str = "fresh"
STALE: str = "stale"
UNKNOWN: str = "unknown"


@dataclass(frozen=True)
class TestResult:
    """一次显式测试的结果：只有分类与时间，不含任何凭据或生成内容。"""

    kind: str
    ok: bool
    detail: str
    revision: int | None
    at: float


class StatusService:
    """把三处事实聚合成一份对外状态快照。

    `snapshot()` 里的配置状态要读凭据库，**可能阻塞**（系统授权框），
    因此调用方必须在工作线程里调用它（§7）。
    """

    def __init__(self, *, instance_id: str, config_service, manager, clock=time.time) -> None:
        self._instance_id = instance_id
        self._config = config_service
        self._manager = manager
        self._clock = clock
        self._lock = threading.Lock()
        self._tests: dict[str, TestResult] = {}

    def record_test(self, kind: str, *, ok: bool, detail: str, revision: int | None) -> TestResult:
        """记录一次显式测试结果；`detail` 必须是固定类别码，不是上游原文。"""
        result = TestResult(
            kind=kind, ok=ok, detail=detail, revision=revision, at=self._clock()
        )
        with self._lock:
            self._tests[kind] = result
        return result

    def _test_view(self, kind: str, revision: int | None) -> dict:
        with self._lock:
            result = self._tests.get(kind)
        if result is None:
            return {"state": UNKNOWN}
        if revision is None:
            # 没有可用的正式配置：任何测试结果都不能算「当前有效」（审查 M3）。
            return {"state": STALE, "ok": result.ok, "detail": result.detail, "at": result.at}
        if result.revision is not None and result.revision != revision:
            # 配置或凭据已经变了：旧结果不再代表当前配置（§10.2）。
            return {"state": STALE, "ok": result.ok, "detail": result.detail, "at": result.at}
        return {"state": FRESH, "ok": result.ok, "detail": result.detail, "at": result.at}

    def snapshot(self) -> dict:
        """聚合状态；可能阻塞，调用方负责放到工作线程里。"""
        config_status = self._config.status()
        process = self._manager.status()
        worker = self._manager.last_status
        revision = config_status.revision
        if worker is None:
            freshness = UNKNOWN
        else:
            sampled = worker.get("sampled_at")
            if not isinstance(sampled, (int, float)) or (
                time.time() - float(sampled) > SNAPSHOT_STALE_SECONDS
            ):
                freshness = STALE
            else:
                freshness = FRESH
        return {
            "instance_id": self._instance_id,
            "config": {
                "state": config_status.state,
                "revision": revision,
                "account": config_status.account,
                "error": config_status.error,
            },
            "process": process,
            "worker": {
                "freshness": freshness,
                # 过期的快照不再冒充当前事实：只报「过期」，不报内容。
                "snapshot": worker if freshness == FRESH else None,
            },
            "tests": {
                "site": self._test_view("site", revision),
                "model": self._test_view("model", revision),
            },
        }
