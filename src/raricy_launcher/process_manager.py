"""受管 Worker 子进程：挂起创建 → Job 纳管 → 放行，及控制通道停止语义。

编排顺序钉死「已运行但未纳管」的窗口（LIGHT_EDITION_DESIGN §9.3）：

1. 创建控制管道与 Job；
2. 以挂起主线程创建子进程（参数数组、明确 cwd、``shell=False`` 等价语义）；
3. ``assign`` 成功才 ``resume``；assign 失败先终止再放行句柄，绝不放行
   一个不受 Job 约束的进程；
4. 优雅停止走控制管道 ``stop`` 指令或父端 EOF；``terminate`` 只是停止
   超时后的最后手段，调用即标记 ``forced_stop``。

子进程环境是明确的系统必需变量白名单加调用方给的额外项，不透传完整宿主
环境（§7）；凭据注入在 L2 经同一通道进入。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .platform import LauncherPlatform, SuspendedProcess, WorkerJob

# 停止指令与控制报文约定见 worker_main。
STOP_COMMAND: bytes = b"stop\n"

# 子进程环境的系统变量白名单（Windows）；凭据与 PYTHONPATH 走 extra。
_SYSTEM_ENV_KEYS: tuple[str, ...] = (
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "COMSPEC",
)


def build_worker_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """系统必需变量白名单 + 显式额外项；不透传完整宿主环境。"""
    env = {key: os.environ[key] for key in _SYSTEM_ENV_KEYS if key in os.environ}
    env.update(extra or {})
    return env


@dataclass(frozen=True)
class WorkerSpec:
    """一次 Worker 启动的固定输入：参数数组、环境、工作目录。"""

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str


def default_worker_spec() -> WorkerSpec:
    """当前运行形态下的 Worker 启动规格。

    冻结发行用同一程序的 ``--worker`` 入口（首版选择，§10.1）；开发模式
    用独立模块入口。两者都是参数数组、明确 cwd，不拼接 shell 命令。
    """
    if getattr(sys, "frozen", False):
        return WorkerSpec(
            argv=(sys.executable, "--worker"),
            env=build_worker_env(),
            cwd=str(Path(sys.executable).resolve().parent),
        )
    src_root = Path(__file__).resolve().parents[1]
    return WorkerSpec(
        argv=(sys.executable, "-m", "raricy_launcher.worker_main"),
        env=build_worker_env({"PYTHONPATH": str(src_root)}),
        cwd=str(src_root.parent),
    )


class WorkerError(Exception):
    """Worker 创建/纳管失败的固定错误；消息是稳定类别码。"""


class WorkerProcess:
    """一个受 Job 约束、经控制管道停止的 Worker 子进程。"""

    def __init__(self, platform: LauncherPlatform, spec: WorkerSpec) -> None:
        self._platform = platform
        self._job: WorkerJob | None = platform.create_worker_job()
        self._pipe = platform.create_control_pipe()
        self._process: SuspendedProcess | None = None
        self._forced_stop = False
        argv = [*spec.argv, "--control-handle", str(self._pipe.child_handle)]
        try:
            self._process = platform.spawn_suspended(argv, env=spec.env, cwd=spec.cwd)
            try:
                self._job.assign(self._process.process_handle)
            except Exception as exc:
                # 纳管失败：先终止再清理，绝不放行不受 Job 约束的进程。
                self._process.terminate()
                raise WorkerError("job_assign_failed") from exc
            self._process.resume()
            self._pipe.detach_child_end()
        except Exception:
            self.close()
            raise

    @property
    def pid(self) -> int:
        return self._process.pid if self._process is not None else -1

    @property
    def forced_stop(self) -> bool:
        return self._forced_stop

    @property
    def job(self) -> WorkerJob | None:
        """纳管本进程的 Job；Controller 持有，崩溃模拟测试据此直接关闭。"""
        return self._job

    def request_stop(self) -> None:
        """经控制管道请求优雅停止；幂等，进程已退出时不报错。"""
        if self._pipe is not None:
            self._pipe.send(STOP_COMMAND)

    def wait(self, timeout_ms: int) -> int | None:
        """等待退出；返回退出码，超时返回 None。"""
        if self._process is None:
            return None
        return self._process.wait(timeout_ms)

    def terminate(self) -> None:
        """停止超时后的强制终止；标记 forced_stop，不得声称优雅完成。"""
        self._forced_stop = True
        if self._process is not None:
            self._process.terminate()

    def close(self) -> None:
        """关闭控制管道、进程句柄与 Job；重复关闭是安全的。

        正常退出应先 request_stop + wait；直接 close 等于放弃纳管——Job
        句柄关闭会触发系统级回收（崩溃兜底路径）。
        """
        process, self._process = self._process, None
        pipe, self._pipe = self._pipe, None
        job, self._job = self._job, None
        if pipe is not None:
            pipe.close()
        if process is not None:
            process.close_handles()
        if job is not None:
            job.close()

    def __enter__(self) -> WorkerProcess:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
