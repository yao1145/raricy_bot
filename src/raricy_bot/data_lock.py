"""公共数据档案锁：同一份数据同时只允许一个写者（LIGHT_EDITION_DESIGN §9.5）。

完整版 CLI 与 Light Worker 都要在打开 Store、记忆或归档**之前**调用
`acquire_data_lock()`：只由 Launcher 自己加锁阻止不了 CLI 并发写入，所以锁标识由
**规范化后的数据档案目录**派生（存储目录，即数据库文件所在目录），不是进程内约定。

实现是在该目录里对一个锁文件取排他锁：Windows 用 `msvcrt.locking` 的字节区间锁，
POSIX 用 `fcntl.flock`。两种锁都随进程退出（含崩溃）由操作系统释放，因此不存在
需要人工清理的陈旧锁；锁文件里只写 pid 与启动时间，供占用诊断，不是锁本身。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

# 锁文件名：与数据档案内的其他文件区分开，`bot.db -wal/-shm` 之外单列。
DATA_LOCK_FILE: str = ".raricy-data.lock"


class DataLockError(Exception):
    """数据目录不可用或已被占用；消息是稳定类别码。"""


def data_lock_dir(db_path: str | Path) -> Path:
    """由数据库路径得到数据档案标识：它所在目录（规范化后的绝对路径）。"""
    return _normalize(Path(db_path).parent)


def _normalize(path: Path) -> Path:
    """绝对化、解析链接/重解析点、统一大小写；目标可以尚不存在。"""
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except OSError:
        resolved = Path(os.path.abspath(Path(path).expanduser()))
    return Path(os.path.normcase(str(resolved)))


def _lock_file(fd: int) -> None:
    """取排他锁；已被占用时抛 `OSError`（非阻塞）。"""
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd: int) -> None:
    """释放锁；关闭路径不抛出（句柄关闭同样会释放）。"""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _write_owner(fd: int) -> None:
    """把占用者信息写进锁文件：只用于诊断，失败不影响持锁。"""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        stamp = datetime.now(timezone.utc).isoformat()
        os.write(fd, f"pid={os.getpid()}\nstarted={stamp}\n".encode("utf-8"))
    except OSError:
        pass


class DataLock:
    """已取得的数据档案锁；`release()` 幂等，也可直接作上下文管理器。"""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd: int | None = fd
        self.path = path

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_file(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> DataLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def acquire_data_lock(data_dir: str | Path) -> DataLock:
    """**立即**取得数据档案锁；被占用或目录不可用时抛 `DataLockError`。

    取得动作发生在这里而不是 `with` 进入时：调用方（入口）要在「打开归档与
    Store 之前」判定占用，把失败包在自己的 try 里比包住 `with` 更不容易漏。
    锁随进程退出由操作系统回收，不按陈旧 PID 判断归属：读锁文件里的 pid 只用于
    诊断，不能作为「可以夺锁」的依据（§9.3、§9.5）。
    """
    directory = _normalize(Path(data_dir))
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataLockError("data_dir_unavailable") from exc
    lock_path = directory / DATA_LOCK_FILE
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError as exc:
        raise DataLockError("data_dir_unavailable") from exc
    try:
        _lock_file(fd)
    except OSError as exc:
        os.close(fd)
        raise DataLockError("data_in_use") from exc
    _write_owner(fd)
    return DataLock(fd, lock_path)
