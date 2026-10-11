"""图像命令解析及当前轮参考图选择，不进行网络请求。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..capabilities import IMAGE_COMMAND
from ..site.models import ChatMessage
from ..text_utils import _match_capability_command

_IMAGE_REF = re.compile(r"\[@([A-Za-z0-9]{10})\]")
_IMAGE_PATH = re.compile(r"/api/images/([A-Za-z0-9]{10})/raw")


@dataclass(frozen=True)
class ImageCommand:
    """明确的生成模式与当轮描述，描述不出现在 repr 中。"""

    mode: str
    prompt: str = field(repr=False)
    references: tuple[str, ...] = ()


class ImageCommandError(ValueError):
    """参考图不可用，仅保存稳定错误码。"""


def parse_image_command(text: str) -> ImageCommand | None:
    """只解析完整 /image 前缀及完整 edit 子命令。"""
    body = _match_capability_command(text, IMAGE_COMMAND)
    if body is None:
        return None
    words = body.split(maxsplit=1)
    mode = "edit" if words and words[0].lower() == "edit" else "generate"
    if mode == "generate":
        return ImageCommand(mode, body)
    prompt = words[1] if len(words) > 1 else ""
    references = tuple(_IMAGE_REF.findall(prompt))
    return ImageCommand(mode, _IMAGE_REF.sub("", prompt).strip(), references)


def reference_for_edit(
    command: ImageCommand, message: ChatMessage, *, site_base_url: str = ""
) -> str | None:
    """编辑时只选当前附件、显式引用或直接回复图；普通生成不取图。"""
    if command.mode != "edit":
        return None
    if message.image_missing:
        raise ImageCommandError("missing")
    current = list(command.references)
    if message.image is not None:
        if message.image_missing:
            raise ImageCommandError("missing")
        current.append(message.image.id)
    if len(current) > 1:
        raise ImageCommandError("multiple")
    if current:
        image_id = current[0]
        if not re.fullmatch(r"[A-Za-z0-9]{10}", image_id):
            raise ImageCommandError("invalid")
        return image_id
    reply = message.reply
    if reply is None or reply.is_deleted or not reply.image_url:
        raise ImageCommandError("missing")
    parsed = urlsplit(reply.image_url)
    if parsed.query or parsed.fragment:
        raise ImageCommandError("invalid")
    if parsed.netloc or parsed.scheme:
        try:
            base = urlsplit(site_base_url)
            if (not site_base_url or parsed.scheme not in ("http", "https")
                or parsed.username is not None or parsed.password is not None
                or (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
                != (base.scheme, base.hostname, base.port or (443 if base.scheme == "https" else 80))):
                raise ImageCommandError("origin")
        except ValueError:
            raise ImageCommandError("invalid") from None
    match = _IMAGE_PATH.fullmatch(parsed.path)
    if match is None:
        raise ImageCommandError("invalid")
    return match.group(1)
