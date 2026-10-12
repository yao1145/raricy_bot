"""生成结果在有界内存中转成小型 WebP；编码失败或超限时拒绝上传。"""

from __future__ import annotations

from io import BytesIO
import warnings

from .models import MAX_OUTPUT_BYTES, MAX_UPLOAD_BYTES, ImageOutput

MAX_UPLOAD_EDGE = 1024
_MAX_PIXELS = 16_000_000
_MAX_INPUT_EDGE = 8192
_QUALITIES = (90, 85, 75, 60, 45, 30)


class ImageCompressionError(Exception):
    """固定失败码，不携带图像字节或底层解码异常。"""

    def __init__(self) -> None:
        super().__init__("compression_failed")


def compress_output(output: ImageOutput) -> ImageOutput:
    """统一 WebP、剥离元数据并逐级缩小，成功结果严格小于 200,000 字节。"""
    # 仅启用图像链路时加载 Pillow，Light 和关闭态不依赖图像编码器。
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not output.data or len(output.data) > MAX_OUTPUT_BYTES:
        raise ImageCompressionError()
    try:
        with warnings.catch_warnings():
            # 将解压炸弹警告视为拒绝，不屏蔽警告后继续解码。
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(output.data)) as source:
                if (
                    source.format not in ("PNG", "JPEG", "WEBP")
                    or getattr(source, "n_frames", 1) != 1
                    or source.width * source.height > _MAX_PIXELS
                    or max(source.size) > _MAX_INPUT_EDGE
                ):
                    raise ImageCompressionError()
                with ImageOps.exif_transpose(source) as oriented:
                    mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
                    with oriented.convert(mode) as image:
                        image.thumbnail((MAX_UPLOAD_EDGE, MAX_UPLOAD_EDGE), Image.Resampling.LANCZOS)
                        while True:
                            for quality in _QUALITIES:
                                with BytesIO() as buffer:
                                    image.save(buffer, format="WEBP", quality=quality, method=4)
                                    if buffer.tell() < MAX_UPLOAD_BYTES:
                                        return ImageOutput(buffer.getvalue(), "image/webp")
                            edge = max(image.size)
                            if edge <= 128:
                                raise ImageCompressionError()
                            next_edge = max(128, int(edge * 0.75))
                            image.thumbnail((next_edge, next_edge), Image.Resampling.LANCZOS)
    except ImageCompressionError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageCompressionError() from None
