"""独立的 OpenAI 图像生成/编辑 HTTP 客户端。

与文本模型完全独立：自己的 base_url、密钥、超时，不共享 SDK 会话，也不新增运行
依赖（复用 `httpx`）。只有两条路径：

- 无参考图：`POST {base_url}/images/generations`，JSON 请求体；
- 有参考图：`POST {base_url}/images/edits`，multipart 上传参考图字节。

两者都固定 `n=1`，只接受 Base64 输出，不从任意远端 URL 下载结果。响应体与解码
后的图片各有字节硬上限，读取走流式累加，绝不把无界响应读进内存；重定向一律关闭，
图像密钥不会被发往跳转目标。

失败分类见 `ImageProviderError`：`uncertain=True` 表示**结果不确定**，调用方必须
按「这次可能已经产生了付费输出」处理（保留额度），而不是当成明确拒绝释放。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json

import httpx

from ..config import ImageGenerationConfig
from .models import MAX_OUTPUT_BYTES, ImageOutput

# 稳定失败码：调用方只比较这些字面量，不解析异常文案。
# 请求根本没发出去或明确被拒（不占额度）。
REASON_EMPTY_PROMPT: str = "empty_prompt"
REASON_PROMPT_TOO_LONG: str = "prompt_too_long"
REASON_NOT_CONFIGURED: str = "not_configured"
REASON_INVALID_REFERENCE: str = "invalid_reference"
REASON_RATE_LIMITED: str = "rate_limited"
REASON_REJECTED: str = "rejected"
REASON_CONTENT_POLICY: str = "content_policy"
# 结果不确定（保守占额）。
REASON_NETWORK: str = "network"
REASON_TIMEOUT: str = "timeout"
REASON_CANCELLED: str = "cancelled"
REASON_SERVER_ERROR: str = "server_error"
REASON_INVALID_SUCCESS: str = "invalid_success"
REASON_RESPONSE_TOO_LARGE: str = "response_too_large"
REASON_OUTPUT_TOO_LARGE: str = "output_too_large"
REASON_INVALID_OUTPUT: str = "invalid_output"

# 官方 Images API 的提示词上限；再长会被上游以 400 拒绝，本地先挡掉省一次请求。
MAX_PROMPT_CHARS: int = 32000

# 响应体硬上限：一张 10 MiB 的图 Base64 后约 13.4 MiB，再加 JSON 外壳与余量。
MAX_RESPONSE_BYTES: int = MAX_OUTPUT_BYTES * 4 // 3 + 1024 * 1024
MAX_ERROR_BYTES: int = 64 * 1024
_SAFETY_CODES = frozenset({"content_policy_violation", "image_content_policy_violation", "moderation_blocked"})


def _is_safety_error(raw: bytes) -> bool:
    """只认错误信封中的明确 code/type；不匹配可能回显描述的 message。"""
    if len(raw) > MAX_ERROR_BYTES:
        return False
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return isinstance(error, dict) and any(
        isinstance(error.get(key), str) and error[key] in _SAFETY_CODES
        for key in ("code", "type")
    )

# 只有这两种格式支持 output_compression；png 传入该字段会被上游拒绝。
_COMPRESSIBLE_FORMATS: tuple[str, ...] = ("webp", "jpeg")

# 编辑参考图只接受这三种位图；GIF/SVG 在本地拒绝，不发给图像服务。
_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_MIME_ALIASES: dict[str, str] = {"image/jpg": "image/jpeg"}

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_RIFF_MAGIC = b"RIFF"
_WEBP_MAGIC = b"WEBP"


class ImageProviderError(Exception):
    """图像接口的稳定失败分类。

    `reason` 是稳定码（模块常量），`uncertain` 表示结果不确定：无法证明这一次没有
    产生（或可能已经产生）付费输出，调用方必须按继续占额处理。异常消息只含稳定码，
    不携带 Prompt、返回 JSON、Base64 或原始 HTTP 错误文案；网络与解析异常一律
    `from None` 抛出，避免 httpx 的原始异常把 URL 等内容带进回溯。
    """

    def __init__(
        self, reason: str, *, uncertain: bool = False, http_status: int | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.uncertain = uncertain
        # 仅记录 HTTP 数字状态，不保存上游错误正文或响应数据。
        self.http_status = http_status


def _detect_bitmap_mime(data: bytes) -> str | None:
    """按 magic bytes 判定真实位图格式；不认识的格式返回 None。

    「真实」是重点：仅凭提供方标的 MIME 或配置里的 output_format 都可能与实际字节
    不符，而图床与后续展示都按真实格式处理。
    """
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(_RIFF_MAGIC) and data[8:12] == _WEBP_MAGIC:
        return "image/webp"
    return None


def _normalize_mime(value: str) -> str | None:
    """归一参考图 MIME；只返回受支持的三种，其余（含空串）返回 None。"""
    if not isinstance(value, str):
        return None
    mime = value.strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    return mime if mime in _MIME_EXT else None


class ImageProvider:
    """OpenAI Images 兼容端点的最小客户端。

    `transport` 只用于测试注入（`httpx.MockTransport`）；生产路径走 httpx 默认传输。
    """

    def __init__(
        self,
        cfg: ImageGenerationConfig,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self._api_key = api_key if isinstance(api_key, str) else ""
        self._base_url = cfg.base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            # 超时统一覆盖连接/读取/写入/取连接，不跟随重定向：图像密钥只发给
            # 配置里那一个地址，绝不因为 302/307 被送到另一个主机。
            timeout=httpx.Timeout(cfg.timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        """关闭底层连接池。"""
        await self._client.aclose()

    async def generate(
        self, prompt: str, *, reference: ImageOutput | None = None
    ) -> ImageOutput:
        """生成一张图；给了参考图就走编辑端点，否则走文生图端点。

        本地能判定的错误（空描述、超长、参考图非法、未配置密钥）在任何请求发出
        之前抛出，`uncertain=False`，不占用生成额度。
        """
        text = prompt.strip() if isinstance(prompt, str) else ""
        if not text:
            raise ImageProviderError(REASON_EMPTY_PROMPT)
        if len(text) > MAX_PROMPT_CHARS:
            raise ImageProviderError(REASON_PROMPT_TOO_LONG)
        if not self._api_key:
            raise ImageProviderError(REASON_NOT_CONFIGURED)

        if reference is None:
            raw = await self._request(
                "/images/generations", json=self._generation_payload(text)
            )
        else:
            data, files = self._edit_payload(text, reference)
            raw = await self._request("/images/edits", data=data, files=files)
        return self._decode_output(raw)

    def _generation_payload(self, prompt: str) -> dict[str, object]:
        """文生图请求体；`n` 固定为 1，用户与模型都不能覆盖。"""
        payload: dict[str, object] = {
            "model": self._cfg.model,
            "prompt": prompt,
            "n": 1,
            "size": self._cfg.size,
            "quality": self._cfg.quality,
            "output_format": self._cfg.output_format,
        }
        if self._cfg.output_format in _COMPRESSIBLE_FORMATS:
            payload["output_compression"] = self._cfg.output_compression
        # GPT Image 默认返回 Base64，且不接受 response_format；其他兼容模型
        # 可能默认返回 URL。显式请求 Base64，继续只在有界内存中接收图片。
        model_name = self._cfg.model.rsplit("/", 1)[-1]
        if not model_name.startswith(("gpt-image-", "chatgpt-image-")):
            payload["response_format"] = "b64_json"
        return payload

    def _edit_payload(
        self, prompt: str, reference: ImageOutput
    ) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
        """编辑请求的 multipart 字段与文件；参考图先做本地校验，不合法不发请求。"""
        data = reference.data
        if not data or len(data) > self._cfg.max_reference_bytes:
            raise ImageProviderError(REASON_INVALID_REFERENCE)
        mime = _normalize_mime(reference.mime_type)
        if mime is None or _detect_bitmap_mime(data) != mime:
            # 声明格式与实际字节不符（或根本不是受支持位图）时本地拒绝：
            # 把 GIF/SVG 这类交给上游既浪费一次付费请求，也放大了上传面。
            raise ImageProviderError(REASON_INVALID_REFERENCE)
        # multipart 的文本字段一律用字符串；`n` 仍然只有 "1" 一种取值。
        fields = {key: str(value) for key, value in self._generation_payload(prompt).items()}
        files = {"image": (f"reference.{_MIME_EXT[mime]}", data, mime)}
        return fields, files

    async def _request(self, path: str, **kwargs: object) -> bytes:
        """发一次请求并返回有界响应体；所有异常都收敛成稳定分类。"""
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        try:
            async with self._client.stream(
                "POST", url, headers=headers, **kwargs
            ) as response:
                status = response.status_code
                if status in (400, 403, 422) or status >= 500:
                    # 小错误体只在内存中分类；超限、断流或非法 JSON 保留原状态分类。
                    if await self._read_safety_error(response):
                        raise ImageProviderError(
                            REASON_CONTENT_POLICY, uncertain=status >= 500, http_status=status
                        )
                if status >= 500:
                    # 5xx 可能已经处理到一半，无法证明这次没有产出。
                    raise ImageProviderError(REASON_SERVER_ERROR, uncertain=True, http_status=status)
                if status == 429:
                    # 请求在计费之前就被拒了，没有图片产生。
                    raise ImageProviderError(REASON_RATE_LIMITED, http_status=status)
                if status < 200 or status >= 300:
                    # 其余 3xx/4xx 都是明确的业务拒绝；重定向已关闭，不会重复外发密钥。
                    raise ImageProviderError(REASON_REJECTED, http_status=status)
                raw = await self._read_bounded(response)
                if _is_safety_error(raw):
                    # 第三方把错误包在 2xx 时仍保守占额，不重复发出付费请求。
                    raise ImageProviderError(REASON_CONTENT_POLICY, uncertain=True, http_status=status)
                return raw
        except ImageProviderError:
            raise
        except httpx.TimeoutException:
            raise ImageProviderError(REASON_TIMEOUT, uncertain=True) from None
        except asyncio.CancelledError:
            # 取消交给服务层保守记账后传播，关闭时不能继续发送失败通知。
            raise
        except httpx.HTTPError:
            raise ImageProviderError(REASON_NETWORK, uncertain=True) from None

    async def _read_safety_error(self, response: httpx.Response) -> bool:
        """有界读取错误信封；失败时不推断安全拦截，也不保留原始错误正文。"""
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_ERROR_BYTES:
                    return False
                chunks.append(chunk)
        except httpx.HTTPError:
            return False
        return _is_safety_error(b"".join(chunks))

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        """流式累加响应体，超过硬上限立即放弃（不读完、不拼接成无界内存）。"""
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ImageProviderError(REASON_RESPONSE_TOO_LARGE, uncertain=True)
            chunks.append(chunk)
        return b"".join(chunks)

    def _decode_output(self, raw: bytes) -> ImageOutput:
        """严格解析成功响应：单张、Base64、真实位图、字节上限。"""
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ImageProviderError(REASON_INVALID_SUCCESS, uncertain=True) from None
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, list) or len(data) != 1:
            # 只要不是恰好一张，就不去猜哪一张才是用户要的：整次按不确定处理。
            raise ImageProviderError(REASON_INVALID_SUCCESS, uncertain=True)
        item = data[0]
        encoded = item.get("b64_json") if isinstance(item, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise ImageProviderError(REASON_INVALID_SUCCESS, uncertain=True)
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ImageProviderError(REASON_INVALID_SUCCESS, uncertain=True) from None
        if not payload:
            raise ImageProviderError(REASON_INVALID_OUTPUT, uncertain=True)
        if len(payload) > MAX_OUTPUT_BYTES:
            raise ImageProviderError(REASON_OUTPUT_TOO_LARGE, uncertain=True)
        mime = _detect_bitmap_mime(payload)
        if mime is None:
            raise ImageProviderError(REASON_INVALID_OUTPUT, uncertain=True)
        return ImageOutput(data=payload, mime_type=mime)
