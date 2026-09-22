"""激活通道的固定协议：版本、命令白名单、有界报文。

管道只提供「打开管理页」等有限激活动作，不是通用 RPC（LIGHT_EDITION_DESIGN
§9.4）。请求与响应都是小 JSON 对象，键集合固定；任何一项不符即拒绝，
不猜测、不忽略多余键。
"""

from __future__ import annotations

import json

PROTOCOL_VERSION: int = 1

# 传输层单次读取上限（防御性）；协议层请求另有自己的更小上限。
MAX_MESSAGE_BYTES: int = 8192
MAX_REQUEST_BYTES: int = 512
MAX_RESPONSE_BYTES: int = 4096

# 首版只有打开管理页一个动作；一次性引导令牌等动作在 L3 加入。
COMMANDS: frozenset[str] = frozenset({"open_admin"})


class ActivationError(Exception):
    """协议校验失败的固定错误；消息是稳定类别码。"""


def encode_request(command: str) -> bytes:
    if command not in COMMANDS:
        raise ActivationError("unknown_command")
    return json.dumps({"version": PROTOCOL_VERSION, "command": command}).encode("utf-8")


def decode_request(data: bytes) -> str:
    """校验并取出命令名；任何不符抛 ActivationError。"""
    if len(data) > MAX_REQUEST_BYTES:
        raise ActivationError("request_too_large")
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError("bad_request") from exc
    if not isinstance(obj, dict) or set(obj) != {"version", "command"}:
        raise ActivationError("bad_request")
    if obj["version"] != PROTOCOL_VERSION:
        raise ActivationError("bad_request")
    if obj["command"] not in COMMANDS:
        raise ActivationError("unknown_command")
    return obj["command"]


def encode_response(*, ok: bool, url: str | None = None, error: str | None = None) -> bytes:
    """构造响应；ok 必须带 url，失败必须带固定错误码。"""
    if ok and not url:
        raise ActivationError("response_needs_url")
    if not ok and not error:
        raise ActivationError("response_needs_error")
    payload = {"version": PROTOCOL_VERSION, "ok": ok}
    if url is not None:
        payload["url"] = url
    if error is not None:
        payload["error"] = error
    data = json.dumps(payload).encode("utf-8")
    if len(data) > MAX_RESPONSE_BYTES:
        raise ActivationError("response_too_large")
    return data


def decode_response(data: bytes) -> dict:
    """客户端侧校验响应；返回 dict，键集合固定。"""
    if len(data) > MAX_RESPONSE_BYTES:
        raise ActivationError("response_too_large")
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError("bad_response") from exc
    if not isinstance(obj, dict) or not set(obj) <= {"version", "ok", "url", "error"}:
        raise ActivationError("bad_response")
    if obj.get("version") != PROTOCOL_VERSION or not isinstance(obj.get("ok"), bool):
        raise ActivationError("bad_response")
    if obj["ok"] and not isinstance(obj.get("url"), str):
        raise ActivationError("bad_response")
    if not obj["ok"] and not isinstance(obj.get("error"), str):
        raise ActivationError("bad_response")
    return obj
