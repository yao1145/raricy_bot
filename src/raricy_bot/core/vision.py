"""图片输入：取回、格式判定与编码（INTERFACES §20）。

三件事：从站点取图（`ImageLoader`）、按**字节**判定格式（不信任 DTO 里的 mime_type）、
编码成 data URL。图片字节只存在于内存：不落 SQLite、不写日志、不写文件、不进历史。

不转发 SVG：站点图床的上传白名单里有 image/svg+xml，但它是白名单里唯一带脚本能力的
格式，而模型对 SVG 的 data URL 也没有有效理解。
"""

from __future__ import annotations

import base64
from typing import Any

# 允许转交给模型的图片格式。判定一律以字节嗅探为准，DTO 的 mime_type 只作参考。
IMAGE_MIME_ALLOWLIST: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)

_PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE: bytes = b"\xff\xd8\xff"
_GIF_SIGNATURE: bytes = b"GIF8"


def sniff_image_mime(data: bytes) -> str | None:
    """按 magic bytes 判定图片格式；不在白名单里的返回 None。

    只认这四种：PNG / JPEG / GIF / WEBP。SVG（XML 文本）与其它一律 None。
    """
    if data.startswith(_PNG_SIGNATURE):
        return "image/png"
    if data.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    if data.startswith(_GIF_SIGNATURE):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def build_data_url(data: bytes, mime: str) -> str:
    """把图片字节编码成 base64 data URL。"""
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_image_part(data_url: str) -> dict[str, Any]:
    """构造 OpenAI 兼容的 image_url 内容块。"""
    return {"type": "image_url", "image_url": {"url": data_url}}


def attach_image(messages: list[dict[str, Any]], part: dict[str, Any]) -> None:
    """就地把图片块挂到**最后一条** user 消息上。

    文本内容（str）就地升级为内容块列表，已是列表则追加。找不到 user 消息时不做任何事
    —— 调用方（app 的 worker）保证最后一轮一定存在 user 消息。

    调用时机：必须在 `_apply_reply_prefix` **之后**，因为后者按 str 拼接 content。
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") != "user":
            continue
        content = messages[index].get("content")
        if isinstance(content, list):
            content.append(part)
        else:
            messages[index] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": content if isinstance(content, str) else ""},
                    part,
                ],
            }
        return
