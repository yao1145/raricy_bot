"""永久错误归档：单写者 JSONL，分片只增不删（计划 §5、INTERFACES §2）。

它和控制台是**两条通路**：控制台保留现有的 Docker json-file 轮转，归档写在独立
持久目录里，按 UTC 日期与大小分片，文件名带 `boot_id` 与递增序号。这里刻意不使用
`logging.handlers.RotatingFileHandler` —— 它的 `backupCount` 会删掉旧文件，而
「永久保留」的第一条就是**不设置自动到期、不覆盖旧分片**。

四条贯穿全文件的约束：

- 归档只接收**已清洗的安全事件**（字段名与取值都过了 `logging_setup` 的白名单与
  类型约束）。它不解析、也不复制 Docker 或子进程的原始日志。
- 单写者：所有写入走同一把锁与同一个文件对象；不引入可能溢出的内存队列，
  代价是 fsync 会落在调用线程上（这个取舍是显式接受的，见 §5.1）。
- 归档自己的状态事件（写入失败、恢复、磁盘告警）只走 stderr，绝不回到归档文件，
  否则一条写失败会变成一次递归。
- 读工具跳过损坏末行，但**不截断、不修复**原文件：损坏就是损坏，不能把它
  改造成"看起来完整"的记录。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from . import logging_setup
from .logging_setup import (
    ARCHIVE_INFO_EVENTS,
    component_of,
    event_payload,
    get_logger,
    log_event,
    third_party_event,
    utc_timestamp,
)

_logger = get_logger("archive")

# 归档文件的结构版本。读工具按它选择解析方式；新增字段不改变版本号，
# 改变字段含义或信封结构才递增（计划 §6 阶段 E）。
SCHEMA_VERSION: Final[int] = 1

# 分片文件名的模板：日期 + 进程标识 + 分片序号。三者缺一不可 ——
# 只有日期会在同一天多次重启时碰撞，只有 boot_id 会在长跑进程里无法按时换片。
_SEGMENT_TEMPLATE: Final[str] = "errors-{day}-{boot}-{index:04d}.jsonl"

# 磁盘巡检的默认周期（秒）。即使一条新错误都没有，也要能发现空间不足。
_DISK_CHECK_SECONDS: Final[float] = 60.0

# 单条归档行的字节上限。它挡的是「某个字段被上游撑大」这类意外：
# 字段本身已受类型与长度约束，这里是最后一道兜底。
MAX_ENTRY_BYTES: Final[int] = 8192

_POSIX_DIR_MODE: Final[int] = 0o700
_POSIX_FILE_MODE: Final[int] = 0o600

# 只读巡检时识别"这条 jsonl 行是不是写了一半"用的：真正的写入总是以换行结束。
_LINE_TERMINATOR: Final[str] = "\n"


class ArchiveError(RuntimeError):
    """归档无法初始化（目录不可用、分片建不出来）；启动阶段致命。"""


@dataclass(frozen=True)
class SegmentReport:
    """一个分片的只读巡检结果。"""

    path: str
    entries: int
    corrupt_lines: int
    complete_tail: bool
    first_ts: str | None
    last_ts: str | None
    last_seq: int | None


def _default_wall_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_disk_free(directory: Path) -> int:
    return shutil.disk_usage(directory).free


class ErrorArchive:
    """单个归档目录的写入者；`open()` / `close()` 成对使用。

    时间、磁盘查询与文件系统写入都可注入：测试因此不依赖真实时钟、真实磁盘余量，
    也不需要把宿主磁盘填满来验证"磁盘满"这条路径。
    """

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        segment_max_bytes: int = 10 * 1024 * 1024,
        fsync_interval_seconds: float = 5.0,
        disk_warning_free_bytes: int = 2 * 1024 * 1024 * 1024,
        boot: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _default_wall_clock,
        disk_free: Callable[[Path], int] = _default_disk_free,
        disk_check_interval_seconds: float = _DISK_CHECK_SECONDS,
    ) -> None:
        self.directory = Path(directory)
        self._segment_max_bytes = max(1024, int(segment_max_bytes))
        self._fsync_interval = max(0.0, float(fsync_interval_seconds))
        self._disk_warning_free_bytes = max(0, int(disk_warning_free_bytes))
        self._disk_check_interval = max(0.0, float(disk_check_interval_seconds))
        # 动态读取而不是导入时固化：`boot_id` 是进程级状态，测试会整套换新。
        self._boot = boot if boot is not None else logging_setup.boot_id
        self._clock = clock
        self._wall_clock = wall_clock
        self._disk_free = disk_free

        self._lock = threading.RLock()
        self._file: Any = None
        self._path: Path | None = None
        self._segment_index = 0
        self._seq = 0
        self._written = 0
        self._pending_fsync = False
        self._last_fsync = 0.0
        self._failed = False
        self._unpersisted = 0
        self._gap_reported = 0
        self._disk_low = False
        self._last_disk_check = 0.0
        # 归档自己的状态事件在 `_announce_locked` 里把当前线程的计数抬起；
        # handler 见到非零就不再落盘，递归就在这一行被切断。
        self._local = threading.local()
        self._stopping = threading.Event()
        self._maintenance: threading.Thread | None = None

    # --- 生命周期 ---------------------------------------------------------

    def open(self) -> None:
        """准备目录并新建一个分片；任何失败都以 `ArchiveError` 抛出。

        分片永远**新建**：重启不会往旧文件后面追加，因此「旧分片被改坏」只有
        磁盘损坏一种可能，而写入端的重启恢复也就退化成一次只读巡检。
        """
        self._prepare_directory()
        self._inspect_existing_segments()
        self._open_segment()
        self._last_fsync = self._clock()
        self._last_disk_check = self._clock()
        self._start_maintenance()
        log_event(
            _logger,
            logging.INFO,
            "archive.started",
            segment=self._segment_index,
        )

    def close(self) -> None:
        """同步、关上文件并停掉巡检线程；可重复调用。"""
        self._stopping.set()
        thread, self._maintenance = self._maintenance, None
        if thread is not None:
            thread.join(timeout=2.0)
        with self._lock:
            if self._file is None:
                return
            try:
                self._sync_locked()
            except OSError:
                # 关闭路径上的同步失败无法再补救：文件仍会被关掉，
                # 缺的那几个事件计入未持久化计数。
                pass
            finally:
                try:
                    self._file.close()
                finally:
                    self._file = None
        log_event(_logger, logging.INFO, "archive.stopped", written_count=self._written)

    @property
    def path(self) -> Path | None:
        """当前分片路径；尚未打开时为 None。"""
        return self._path

    @property
    def healthy(self) -> bool:
        """归档当前是否在正常落盘。"""
        with self._lock:
            return self._file is not None and not self._failed

    def status(self) -> dict[str, object]:
        """给本地健康检查用的计数；只有数字与布尔，不含路径与组件名。"""
        with self._lock:
            return {
                "enabled": True,
                "healthy": self._file is not None and not self._failed,
                "written": self._written,
                "unpersisted": self._unpersisted,
                "disk_low": self._disk_low,
            }

    # --- 写入 -------------------------------------------------------------

    def append(self, entry: dict[str, Any]) -> bool:
        """写入一条已清洗的事件；失败时隔离错误并返回 False。"""
        with self._lock:
            if self._file is None or not isinstance(entry, dict):
                return False
            try:
                line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                # 事件本身不是可编码的 JSON：这是调用方的错误，不能拖垮归档。
                self._unpersisted += 1
                return False
            raw = line + _LINE_TERMINATOR
            if len(raw.encode("utf-8")) > MAX_ENTRY_BYTES:
                self._unpersisted += 1
                self._announce_locked(
                    logging.ERROR, "archive.entry_rejected", reason="too_large"
                )
                return False
            try:
                if self._file.tell() >= self._segment_max_bytes:
                    self._rotate_locked()
                self._file.write(raw)
                self._file.flush()
                self._written += 1
                level = _level_value(entry.get("level"))
                if level >= logging.ERROR:
                    self._sync_locked()
                else:
                    self._pending_fsync = True
                if self._failed:
                    # 上一次写失败过，这一次成功了 —— 现在才是"已恢复"。
                    self._failed = False
                    self._note_recovery_locked()
                return True
            except OSError as exc:
                self._note_failure_locked(exc)
                return False

    def flush(self, *, force: bool = False) -> None:
        """按策略同步到磁盘；`force=True` 无视间隔。"""
        with self._lock:
            if self._file is None:
                return
            if not self._pending_fsync:
                return
            if not force and self._clock() - self._last_fsync < self._fsync_interval:
                return
            try:
                self._sync_locked()
            except OSError as exc:
                self._note_failure_locked(exc)

    def check_disk(self, *, force: bool = False) -> None:
        """巡检剩余空间；低于阈值时告警一次，恢复后再告警一次。"""
        with self._lock:
            now = self._clock()
            if not force and now - self._last_disk_check < self._disk_check_interval:
                return
            self._last_disk_check = now
            try:
                free = self._disk_free(self.directory)
            except OSError:
                return
            low = free < self._disk_warning_free_bytes
            if low and not self._disk_low:
                self._disk_low = True
                self._announce_locked(
                    logging.ERROR, "archive.disk_low", free_bytes=free
                )
            elif not low and self._disk_low:
                self._disk_low = False
                self._announce_locked(logging.WARNING, "archive.disk_ok", free_bytes=free)

    # --- 内部实现 ---------------------------------------------------------

    def _prepare_directory(self) -> None:
        """建目录并收紧权限；Windows 上不假装 chmod 等于 ACL。"""
        target = self.directory
        if target.is_symlink():
            raise ArchiveError("归档目录不得是符号链接")
        resolved = Path(os.path.realpath(target))
        parent = resolved if resolved.exists() else resolved.parent
        if not os.access(parent, os.W_OK):
            raise ArchiveError("归档目录不可写")
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ArchiveError(f"归档目录创建失败：{type(exc).__name__}") from exc
        if not resolved.is_dir():
            raise ArchiveError("归档路径不是目录")
        if os.name == "posix":
            try:
                os.chmod(resolved, _POSIX_DIR_MODE)
            except OSError as exc:
                raise ArchiveError(f"归档目录权限设置失败：{type(exc).__name__}") from exc

    def _inspect_existing_segments(self) -> None:
        """只读巡检既有分片；尾部不完整时保留原文件并留一条稳定事件。

        这里刻意**不**修复：把损坏末行删掉会让一个「写到一半就断电」的文件
        看起来完全正常，那正是"把损坏文件截断后冒充完整记录"要禁止的事。
        """
        for report in verify_segments(self.directory):
            if report.complete_tail:
                continue
            log_event(
                _logger,
                logging.WARNING,
                "archive.tail_incomplete",
                count=report.corrupt_lines,
                segment=self._segment_index_of(report.path),
            )

    @staticmethod
    def _segment_index_of(name: str) -> int | None:
        """从 `errors-<day>-<boot>-<index>.jsonl` 里取回分片序号；取不到返回 None。"""
        stem = os.path.splitext(os.path.basename(name))[0]
        parts = stem.split("-")
        if len(parts) < 4:
            return None
        try:
            return int(parts[-1])
        except ValueError:
            return None

    def _open_segment(self) -> None:
        """独占创建一个新分片；已存在就换下一个序号，绝不覆盖。"""
        day = self._wall_clock().strftime("%Y%m%d")
        for _ in range(1000):
            self._segment_index += 1
            path = self.directory / _SEGMENT_TEMPLATE.format(
                day=day, boot=self._boot, index=self._segment_index
            )
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _POSIX_FILE_MODE)
            except FileExistsError:
                # 时钟回拨或同一 boot_id 复用：换序号继续，不碰已有文件。
                continue
            except OSError as exc:
                raise ArchiveError(f"归档分片创建失败：{type(exc).__name__}") from exc
            self._file = os.fdopen(fd, "a", encoding="utf-8", newline="")
            self._path = path
            return
        raise ArchiveError("归档分片序号耗尽")

    def _rotate_locked(self) -> None:
        """换片：先同步并关掉旧片，再独占创建新片；旧片一个都不删。"""
        old = self._segment_index
        try:
            self._sync_locked()
        except OSError:
            pass
        try:
            self._file.close()
        finally:
            self._file = None
        self._open_segment()
        self._announce_locked(
            logging.INFO, "archive.segment_rolled", segment=old
        )

    def _sync_locked(self) -> None:
        """把缓冲刷进操作系统并 fsync 到设备。"""
        assert self._file is not None
        self._file.flush()
        os.fsync(self._file.fileno())
        self._pending_fsync = False
        self._last_fsync = self._clock()

    def _note_failure_locked(self, exc: BaseException) -> None:
        """隔离一次写失败：继续服务，只在 stderr 限频报告并累计缺口。"""
        self._failed = True
        self._unpersisted += 1
        if self._unpersisted != 1 and self._unpersisted % 100 != 0:
            # 限频：磁盘满时每一条 ERROR 都失败，逐条报告会把 stderr 也写满。
            return
        self._announce_locked(
            logging.ERROR,
            "archive.write_failed",
            error=type(exc).__name__,
            gap_count=self._unpersisted,
        )

    def _note_recovery_locked(self) -> None:
        """写失败之后又写成功了：把恢复与缺口一次记清楚。"""
        gap, self._unpersisted = self._unpersisted, 0
        self._gap_reported = gap
        entry = self._build_entry(
            level=logging.WARNING,
            event="archive.recovered",
            component="archive",
            fields={"gap_count": gap},
        )
        if entry is not None:
            # 恢复事件直接落盘：此刻 `_failed` 已清，写这一条不会再触发递归。
            try:
                line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                self._file.write(line + _LINE_TERMINATOR)
                self._file.flush()
                self._written += 1
            except OSError:
                pass
        self._announce_locked(
            logging.WARNING,
            "archive.recovered",
            gap_count=gap,
        )

    def _announce_locked(self, level: int, event: str, **fields: object) -> None:
        """发出归档自身的状态事件：只到 stderr，不再回到归档文件。

        未落盘的事件**不能假装可以补回**：这里报告的是缺口计数，不是补写。
        """
        self._local.announce_depth = getattr(self._local, "announce_depth", 0) + 1
        try:
            log_event(_logger, level, event, **fields)
        finally:
            self._local.announce_depth -= 1

    @property
    def suppressed(self) -> bool:
        """**当前线程**正在报告归档自身的状态；此时 handler 必须拒绝落盘。

        用线程本地而不是全局标志：全局标志会在报告的那一瞬间顺带丢掉别的线程
        发来的事件 —— 那些事件既没落盘、也不计进缺口，等于静默丢失。
        """
        return getattr(self._local, "announce_depth", 0) > 0

    def _build_entry(
        self, *, level: int, event: str, component: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """构造一条归档信封；序号在这里递增（未落盘的那条也占号）。"""
        self._seq += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "ts": utc_timestamp(self._wall_clock()),
            "level": logging.getLevelName(level),
            "event": event,
            "component": component,
            "boot_id": self._boot,
            "seq": self._seq,
            "fields": fields,
        }

    def entry_for(self, record: logging.LogRecord) -> dict[str, Any] | None:
        """把一条日志记录转成归档信封；不接受就返回 None。

        我们自己的记录直接用已清洗的事件字段；第三方记录**不原样转存** ——
        它的消息文本没经过白名单，压成固定的 `third_party.failure` 事件，
        只留来源 logger 名（受 TOKEN 约束）与级别。
        """
        payload = event_payload(record)
        if payload is None:
            # 与控制台共用同一条转换：两条通路对"第三方记录长什么样"必须有
            # 同一个答案，否则控制台与归档会各自漂移成不同的安全边界。
            fallback = third_party_event(record)
            if fallback is None:
                return None
            return self._build_entry(
                level=record.levelno,
                event=fallback.name,
                component="third_party",
                fields=fallback.as_mapping(),
            )
        if record.levelno < logging.WARNING and not (
            record.levelno == logging.INFO and payload.name in ARCHIVE_INFO_EVENTS
        ):
            return None
        return self._build_entry(
            level=record.levelno,
            event=payload.name,
            component=component_of(record),
            fields=payload.as_mapping(),
        )

    def _start_maintenance(self) -> None:
        """起一个守护线程做周期同步与磁盘巡检。"""
        # 巡检周期取两个策略里更短的那个：同步要按 fsync 间隔，空间要按磁盘间隔。
        interval = min(
            value for value in (self._fsync_interval, self._disk_check_interval) if value > 0
        )
        thread = threading.Thread(
            target=self._maintain, args=(max(0.1, interval),), name="archive-maintenance",
            daemon=True,
        )
        self._maintenance = thread
        thread.start()

    def _maintain(self, interval: float) -> None:
        while not self._stopping.wait(interval):
            try:
                self.flush()
                self.check_disk()
            except Exception:
                # 巡检线程绝不向外抛：它随进程结束而消失，异常只会变成噪音。
                continue


def _level_value(name: object) -> int:
    """把归档信封里的级别名换回数值；未知一律当 WARNING。"""
    if not isinstance(name, str):
        return logging.WARNING
    return logging.getLevelNamesMapping().get(name, logging.WARNING)


class ArchiveHandler(logging.Handler):
    """把日志记录交给 `ErrorArchive` 的 handler。

    它自己不做任何筛选之外的加工：记录到信封的转换在 `ErrorArchive.entry_for`，
    因为只有那里同时知道"归档收什么"与"归档怎么编码"。
    """

    def __init__(self, archive: ErrorArchive) -> None:
        super().__init__(level=logging.INFO)
        self.archive = archive

    def emit(self, record: logging.LogRecord) -> None:
        if self.archive.suppressed:
            # 归档自己的状态事件：让它进文件就是一条写失败的递归。
            return
        try:
            entry = self.archive.entry_for(record)
            if entry is None:
                return
            self.archive.append(entry)
        except Exception:
            # handler 里绝不抛：logging 的默认兜底会把 traceback 写进 stderr，
            # 那正是要避免的原始正文外泄路径。
            return

    def close(self) -> None:
        try:
            self.archive.close()
        finally:
            super().close()


# --- 只读工具 ---------------------------------------------------------------


def segment_paths(directory: str | os.PathLike[str]) -> tuple[Path, ...]:
    """列出归档目录里的分片，按文件名排序（即日期 + 序号）。"""
    root = Path(directory)
    if not root.is_dir():
        return ()
    return tuple(sorted(path for path in root.glob("*.jsonl") if path.is_file()))


def iter_entries(
    directory: str | os.PathLike[str],
    *,
    event: str | None = None,
    level: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """按顺序读出归档事件；跳过损坏行，不修改文件。

    `since` 是 ISO-8601 前缀（如 `2026-09-21T00:00:00`）：时间戳是定长 UTC 串，
    字典序即时间序，因此前缀比较就够，不必解析成 datetime。
    """
    emitted = 0
    for report in verify_segments(directory):
        path = Path(report.path)
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except (TypeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if event is not None and entry.get("event") != event:
                    continue
                if level is not None and entry.get("level") != level:
                    continue
                if since is not None and str(entry.get("ts", "")) < since:
                    continue
                yield entry
                emitted += 1
                if limit is not None and emitted >= limit:
                    return


def verify_segments(directory: str | os.PathLike[str]) -> tuple[SegmentReport, ...]:
    """巡检全部分片：可解析行数、损坏行数、末行是否完整。

    `complete_tail=False` 只说明最后一行的写入被打断（进程被强杀、断电）。
    其余行仍然可读，读工具照常跳过它 —— 但巡检**不修复**该文件。
    """
    reports: list[SegmentReport] = []
    for path in segment_paths(directory):
        entries = 0
        corrupt = 0
        first_ts: str | None = None
        last_ts: str | None = None
        last_seq: int | None = None
        complete = True
        with open(path, "rb") as handle:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    complete = False
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    corrupt += 1
                    continue
                if not isinstance(entry, dict):
                    corrupt += 1
                    continue
                entries += 1
                ts = entry.get("ts")
                if isinstance(ts, str):
                    first_ts = first_ts or ts
                    last_ts = ts
                seq = entry.get("seq")
                if isinstance(seq, int):
                    last_seq = seq
        reports.append(
            SegmentReport(
                path=str(path),
                entries=entries,
                corrupt_lines=corrupt,
                complete_tail=complete,
                first_ts=first_ts,
                last_ts=last_ts,
                last_seq=last_seq,
            )
        )
    return tuple(reports)
