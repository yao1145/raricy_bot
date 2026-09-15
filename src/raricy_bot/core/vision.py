"""图片输入：取回、格式判定与编码（INTERFACES §20）。

三件事：从站点取图（`ImageLoader`）、按**字节**判定格式（不信任 DTO 里的 mime_type）、
编码成 data URL。图片字节只存在于内存：不落 SQLite、不写日志、不写文件、不进历史。

不转发 SVG：站点图床的上传白名单里有 image/svg+xml，但它是白名单里唯一带脚本能力的
格式，而模型对 SVG 的 data URL 也没有有效理解。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from ..logging_setup import get_logger, log_event
from ..site.client import ImageFetchError, SiteClient
from ..site.models import ChatMessage

_logger = get_logger("vision")

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


def with_image_marker(user_text: str, state: str) -> str:
    """给本轮正文加上图片标记。

    `[图片]` 表示这一轮确实带了图；`[图片未提供]` 表示本来有图但没取到 ——
    让模型知道自己没看到图，而不是以为用户什么都没发。纯图且没取到时给不出正文，
    返回空串：标记没有可依附的内容，该由调用方回一条本地提示。

    聊天区与评论区共用这一份措辞：两边说的是同一种「这一轮有没有图、模型看没看到」。
    """
    if state == "ok" and user_text:
        return f"[图片]\n---\n{user_text}"
    if state == "ok":
        return "[图片]"
    if state != "none" and user_text:
        return f"[图片未提供]\n---\n{user_text}"
    return user_text


class ImageLoader:
    """把一条消息里的图片取回并编码成模型可用的内容块。

    图片字节只在这里短暂存在：不进历史、不落库、不写日志。失败一律降级为
    `(None, reason)`，由调用方决定是转纯文本轮还是回一条本地提示。
    """

    def __init__(
        self,
        client: SiteClient,
        *,
        max_bytes: int,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._logger = logger if logger is not None else _logger

    async def load(self, message: ChatMessage) -> tuple[dict[str, Any] | None, str]:
        """返回 `(image_part | None, reason)`；reason 见 INTERFACES §20。"""
        image = message.image
        if image is None or message.image_missing:
            return None, "none"
        return await self.load_url(image.url)

    async def load_url(self, url: str) -> tuple[dict[str, Any] | None, str]:
        """按 URL 取图并编码；消息附图与 `[@10位]` 引用共用这一条路。

        URL 的形态由调用方负责：`fetch_image` 只放行与站点**完全同源**的地址，
        跨源在发请求之前就被拒。失败一律降级成 `(None, reason)`，与 `load` 同款。
        """
        try:
            data = await self._client.fetch_image(url, max_bytes=self._max_bytes)
        except ImageFetchError as exc:
            self._log_failure(exc.reason)
            return None, exc.reason

        mime = sniff_image_mime(data)
        if mime is None or mime not in IMAGE_MIME_ALLOWLIST:
            # 嗅探目前只会返回白名单里的四种，这个判断是策略的单一来源：
            # 将来嗅探支持更多格式时，仍由白名单决定「发给模型」这一侧放行什么。
            self._log_failure("unsupported_type", size_bytes=len(data))
            return None, "unsupported_type"

        return build_image_part(build_data_url(data, mime)), "ok"

    def _log_failure(self, reason: str, *, size_bytes: int | None = None) -> None:
        """记一条降级日志；字段只有 reason / size_bytes / limit_bytes，**不记 URL**。"""
        fields: dict[str, object] = {"reason": reason}
        if size_bytes is not None:
            fields["size_bytes"] = size_bytes
        if reason == "too_large":
            fields["limit_bytes"] = self._max_bytes
        log_event(self._logger, logging.INFO, "vision.image_unavailable", **fields)
