"""Controller 与 Light Worker 之间的私有控制协议（LIGHT_EDITION_DESIGN §10.1）。

帧是**有界**的：4 字节大端长度 + UTF-8 JSON 对象；单帧与整体缓冲都有上限，
超限、未知种类或身份不符一律以固定类别拒绝，并终止该会话。

信封固定携带 `protocol_version`、`instance_id`、`run_id` 与递增的 `seq`：

- 命令（Controller → Worker）：``stop``、``status_request``；
- 上报（Worker → Controller）：``ready``、``status``、``log``、``stopped``。

凭据**不**走这条通道：它仍按 §7 经子进程环境注入。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

PROTOCOL_VERSION: int = 1

# 单帧上限：状态快照与白名单日志事件都很小，超过这个数就是对方在说另一种协议。
MAX_FRAME_BYTES: int = 64 * 1024

# 会话开始前最多容忍多少字节的垃圾；超过即判定协议违例。
MAX_PENDING_BYTES: int = 256 * 1024

COMMANDS: frozenset[str] = frozenset({"stop", "status_request"})
REPORTS: frozenset[str] = frozenset({"ready", "status", "log", "stopped"})


class IpcError(Exception):
    """协议违例；消息是稳定类别码。"""


def encode_frame(
    kind: str,
    *,
    protocol_version: int = PROTOCOL_VERSION,
    instance_id: str = "",
    run_id: str = "",
    seq: int = 0,
    payload: Mapping[str, Any] | None = None,
) -> bytes:
    """构造一帧；`kind` 必须在调用方一侧的已知集合里。"""
    body = {
        "v": protocol_version,
        "kind": kind,
        "instance_id": instance_id,
        "run_id": run_id,
        "seq": int(seq),
        "payload": dict(payload or {}),
    }
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise IpcError("frame_too_large")
    return len(data).to_bytes(4, "big") + data


def decode_frame(
    data: bytes,
    *,
    allowed: frozenset[str],
    expect_instance: str | None = None,
    expect_run: str | None = None,
) -> dict[str, Any]:
    """校验一帧的形状、身份与种类；返回完整信封。"""
    if len(data) > MAX_FRAME_BYTES:
        raise IpcError("frame_too_large")
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcError("bad_frame") from exc
    if not isinstance(body, dict) or set(body) != {
        "v",
        "kind",
        "instance_id",
        "run_id",
        "seq",
        "payload",
    }:
        raise IpcError("bad_frame")
    if body["v"] != PROTOCOL_VERSION:
        raise IpcError("protocol_mismatch")
    if not isinstance(body["kind"], str) or body["kind"] not in allowed:
        raise IpcError("unknown_kind")
    if not isinstance(body["seq"], int) or isinstance(body["seq"], bool) or body["seq"] < 0:
        raise IpcError("bad_frame")
    if not isinstance(body["payload"], dict):
        raise IpcError("bad_frame")
    if expect_instance is not None and body["instance_id"] != expect_instance:
        raise IpcError("instance_mismatch")
    if expect_run is not None and body["run_id"] != expect_run:
        raise IpcError("run_mismatch")
    return body


def read_frame(
    read: Callable[[int], bytes],
    *,
    allowed: frozenset[str],
    expect_instance: str | None = None,
    expect_run: str | None = None,
) -> dict[str, Any] | None:
    """从 `read(n)` 读一帧；对端正常关闭（EOF）返回 None，协议违例抛 `IpcError`。

    读取是**增量**的：调用方每次给一小块，长度前缀到齐之后才分配整帧缓冲，
    因此一个乱报长度的对端也不会让本进程分配超大内存。
    """
    header = _read_exact(read, 4)
    if header is None:
        return None
    length = int.from_bytes(header, "big")
    if length <= 0 or length > MAX_FRAME_BYTES:
        raise IpcError("frame_too_large")
    body = _read_exact(read, length)
    if body is None:
        # 对端在帧中间关闭：不是完整帧，属于协议违例而不是「正常结束」。
        raise IpcError("truncated_frame")
    return decode_frame(
        body, allowed=allowed, expect_instance=expect_instance, expect_run=expect_run
    )


def _read_exact(read: Callable[[int], bytes], size: int) -> bytes | None:
    """读满 size 字节；读到 EOF 且一个字节都没有时返回 None。"""
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = read(remaining)
        if not chunk:
            if not chunks:
                return None
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
