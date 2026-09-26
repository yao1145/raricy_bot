"""状态聚合：配置就绪、进程状态、Worker 上报与显式测试结果（§9.1、§10.2）。

三条口径与探针一致：

- 配置就绪状态与进程状态**分开**，不让一个 `running` 布尔覆盖所有情况；
- Worker 快照带采样时间，过期或从未上报就如实标 `stale` / `unknown`，
  不从历史日志猜当前状态；
- 显式测试结果带产生它的身份键：档案 id、配置 revision 与档案代次
  （§5.1 第 6 条）—— A、B 同为 rev 1 时只比数字 revision 分不开两者。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .config_service import ConfigServiceError

# 快照失效阈值：超过它就不再声称这些事实仍然成立（§10.2）。
SNAPSHOT_STALE_SECONDS: float = 30.0

FRESH: str = "fresh"
STALE: str = "stale"
UNKNOWN: str = "unknown"


@dataclass(frozen=True)
class TestResult:
    """一次显式测试的结果：只有分类、身份键与时间，不含任何凭据或生成内容。

    `profile_id` / `profile_epoch` 是可选的身份键组成部分：给了它们的结果在
    档案或代次变化后标为过期；只给了 `revision` 的调用方仍按数字 revision 比较。
    """

    kind: str
    ok: bool
    detail: str
    revision: int | None
    at: float
    profile_id: str | None = None
    profile_epoch: int | None = None


class StatusService:
    """把三处事实聚合成一份对外状态快照。

    `snapshot()` 里的配置状态要读凭据库，**可能阻塞**（系统授权框），
    因此调用方必须在工作线程里调用它（§7）。

    `profile_service` 缺省为 `None`，此时不装配档案视图：`snapshot()` 的
    `active_profile_id` / `profile_epoch` / `startup_profile_id` 恒为 `None`，
    测试结果只按数字 revision 比较。该缺省只供不装配档案视图的测试使用，
    正式装配（Controller）始终注入 `ProfileService`。
    """

    def __init__(
        self,
        *,
        instance_id: str,
        config_service,
        manager,
        profile_service=None,
        clock=time.time,
    ) -> None:
        self._instance_id = instance_id
        self._config = config_service
        self._manager = manager
        self._profiles = profile_service
        self._clock = clock
        self._lock = threading.Lock()
        self._tests: dict[str, TestResult] = {}

    def record_test(
        self,
        kind: str,
        *,
        ok: bool,
        detail: str,
        revision: int | None,
        profile_id: str | None = None,
        profile_epoch: int | None = None,
    ) -> TestResult:
        """记录一次显式测试结果；`detail` 必须是固定类别码，不是上游原文。

        `profile_id` / `profile_epoch` 是这次测试的身份键（§5.1 第 6 条）：
        记录方给了什么就比什么，没给的部分退回数字 revision 的比较口径。
        """
        result = TestResult(
            kind=kind,
            ok=ok,
            detail=detail,
            revision=revision,
            at=self._clock(),
            profile_id=profile_id,
            profile_epoch=profile_epoch,
        )
        with self._lock:
            self._tests[kind] = result
        return result

    @staticmethod
    def _stale(result: TestResult) -> dict:
        return {"state": STALE, "ok": result.ok, "detail": result.detail, "at": result.at}

    def _test_view(
        self,
        kind: str,
        revision: int | None,
        profile_id: str | None = None,
        profile_epoch: int | None = None,
    ) -> dict:
        """测试结果的过期判定：身份键优先，退回数字 revision（§10.2）。

        档案不同就是过期，两条结果不能互相顶替（同号 revision 不串档案）。
        代次只在**两边都记录了**时才比较：活动指针每切换一次 `active_epoch` 就
        +1（切回原档案也一样），把当前代次当作严格相等键会让同一档案的旧结果在
        任意一次往返后永久过期 —— 那是误报，不是串档案。
        """
        with self._lock:
            result = self._tests.get(kind)
        if result is None:
            return {"state": UNKNOWN}
        if revision is None:
            # 没有可用的正式配置：任何测试结果都不能算「当前有效」（审查 M3）。
            return self._stale(result)
        if result.profile_id != profile_id:
            return self._stale(result)
        if (
            result.profile_epoch is not None
            and profile_epoch is not None
            and result.profile_epoch != profile_epoch
        ):
            return self._stale(result)
        if result.revision is not None and result.revision != revision:
            # 配置或凭据已经变了：旧结果不再代表当前配置（§10.2）。
            return self._stale(result)
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
        running_revision = process.get("running_revision")
        running_profile_id = process.get("running_profile_id")
        active_profile_id: str | None = None
        profile_epoch: int | None = None
        startup_profile_id: str | None = None
        if self._profiles is not None:
            try:
                catalog = self._profiles.catalog()
                active_profile_id = catalog.active_profile_id
                profile_epoch = catalog.active_epoch
                if active_profile_id is not None and self._config.start_bot_on_launch():
                    # N4 换成 desktop.json 的启动目标前的过渡口径（D-135）：
                    # 「打开程序就启动」偏好为真时，启动档案就是当前活动档案。
                    startup_profile_id = active_profile_id
            except ConfigServiceError:
                # 元数据或配置读不出来时 /api/status 不能 500：三个档案字段
                # 降级为 None，原因已由 config.state == "recovery" 与它的稳定码
                # 承载（D-135）。
                active_profile_id = None
                profile_epoch = None
                startup_profile_id = None
        pending = self._manager.current_operation()
        pending_operation: dict | None = None
        if pending is not None and pending.finished_at is None:
            pending_operation = {
                "operation_id": pending.operation_id,
                "kind": pending.kind,
                "state": pending.state,
                "profile_id": pending.profile_id,
                "revision": pending.target_revision,
            }
        return {
            "instance_id": self._instance_id,
            # 保存的版本与正在跑的版本分开报；两者不同就是「待重启」。
            "saved_revision": revision,
            "running_revision": running_revision,
            # 同号 revision 换档案也必须提示重启：只比数字 revision 会漏掉它（D-135）。
            "restart_required": (
                running_revision is not None
                and revision is not None
                and (
                    running_revision != revision
                    or running_profile_id != active_profile_id
                )
            ),
            "active_profile_id": active_profile_id,
            "running_profile_id": running_profile_id,
            "startup_profile_id": startup_profile_id,
            "profile_epoch": profile_epoch,
            "pending_operation": pending_operation,
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
                "site": self._test_view("site", revision, active_profile_id, profile_epoch),
                "model": self._test_view("model", revision, active_profile_id, profile_epoch),
            },
        }
