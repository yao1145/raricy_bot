"""图像任务的完整生成/编辑链路与图床租期维护（B）。

职责边界（设计 §4–§6）：

- `handle()` 消费一条 `ImageRequest`：编辑先取并校验参考图，**预留前完成**；再在滚动
  24h 硬额度内预留、调用独立图像提供方、上传图床、用现有发送器回一条带附件的消息，
  最后无论成败都终结触发事件。生成与编辑共用同一账本。
- `cleanup_once()` 只维护本功能账本的图：先删 30 天到期图，再按实际字节最旧优先淘汰。
  生成开关关闭时仍运行，避免旧图永久残留。
- 上传结果不确定时只按**精确随机文件名**只读对账，查不到不是失败证明；重启只读对账、
  绝不重新生成，丢失的字节也不靠重新生成恢复。

内存边界：描述先经脱敏，参考图字节与输出字节只在受限内存里传递，上传完成/任务结束后
释放；一律不写文件或 SQLite。日志只写白名单字段，绝不含描述、图片字节、URL 或密钥。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..config import ImageGenerationConfig
from ..logging_setup import get_logger, log_event
from ..redact import Redactor
from ..site.client import ImageFetchError, SiteClient, SiteError, image_raw_path
from ..store import (
    IMAGE_DELETE_CAPACITY,
    IMAGE_DELETE_DONE,
    IMAGE_DELETE_EXPIRED,
    IMAGE_DELETE_FAILED,
    IMAGE_REASON_DUPLICATE,
    Store,
    ImageJob,
)
from ..texts import (
    IMAGE_BLACKLIST_TEXT,
    IMAGE_FAILURE_TEXT,
    IMAGE_QUOTA_TEXT,
    IMAGE_REFERENCE_TEXT,
    IMAGE_RESULT_TEXT,
    IMAGE_SPACE_TEXT,
)
from .models import (
    MODE_EDIT,
    RETENTION_SECONDS,
    ImageOutput,
    ImageRequest,
)
from .provider import ImageProvider, ImageProviderError, REASON_CONTENT_POLICY

if TYPE_CHECKING:  # 只用于类型标注，避免运行期引入发送器依赖
    from ..core.sender import MessageSender

logger = get_logger("images")

# 有界图床维护周期（设计 §1）：启动及此后每 10 分钟检查一次到期与对账。
CLEANUP_INTERVAL_SECONDS: float = 600.0
# 关闭时等待维护任务退出的上限；超时不阻塞 App 关闭。
_CLOSE_TIMEOUT_SECONDS: float = 5.0
_BEIJING = timezone(timedelta(hours=8))

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_RIFF_MAGIC = b"RIFF"
_WEBP_MAGIC = b"WEBP"
# 编辑参考图只支持这三种位图；GIF/SVG 本地拒绝，不发给图像服务。
_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
# 发送结果里「可能已经发出、但无法确认」的稳定 reason：保留待核实，不退生成额度。
_UNCERTAIN_DELIVERY_REASONS: frozenset[str] = frozenset({"failed"})


def _bitmap_mime(data: bytes) -> str | None:
    """按 magic bytes 判定真实位图格式；不认识的格式返回 None。

    仅凭 MIME 声明或配置格式都可能与实际字节不符，参考图校验按真实字节来。
    """
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(_RIFF_MAGIC) and data[8:12] == _WEBP_MAGIC:
        return "image/webp"
    return None


class _ReferenceUnavailable(Exception):
    """参考图本地不可用：缺图、取图失败、超限或格式不支持。不占额度。"""


class ImageService:
    """图像子系统的执行者；由 App 装配 worker 与清理任务。

    `generation_is_current` 是调用方给的同步守卫（收 `ImageRequest`）：预览、上传前与
    发送前后都复核，失效任务不再上传或发图（D-154）。未知结果仍保留占额，不因失效释放。
    """

    def __init__(
        self,
        *,
        cfg: ImageGenerationConfig,
        store: Store,
        site: SiteClient,
        provider: ImageProvider | None,
        sender: "MessageSender",
        scope: str,
        generation_is_current: Callable[[ImageRequest], bool],
        redactor: Redactor,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._site = site
        self._provider = provider
        self._sender = sender
        self._scope = scope
        self._generation_is_current = generation_is_current
        self._redactor = redactor
        self._now = now
        # 图床维护/腾空间/上传共用的进程内锁：防止两个 worker 按旧快照多删（设计 §2）。
        self._maintenance = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    # --- 生命周期 -----------------------------------------------------------

    async def start(self) -> None:
        """恢复状态、只读对账待确认的上传，并启动有界维护任务。

        即使 `cfg.enabled=False` 也仍维护已有图片：清理与新的生图配置、图像密钥无关。
        恢复/对账失败是软故障，只记事件，不影响聊天、评论与健康检查。
        """
        try:
            await self._store.recover_image_jobs(self._now())
        except Exception:
            log_event(logger, logging.WARNING, "image.recover", reason="failed")
        try:
            await self._reconcile_uploads_once()
            await self.cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(logger, logging.WARNING, "image.reconcile", reason="failed")
        if self._task is None:
            self._task = asyncio.create_task(self._maintenance_loop())

    async def close(self) -> None:
        """只结束本服务维护任务；提供方由 App 关闭。等待有界，超时不阻塞关闭。"""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=_CLOSE_TIMEOUT_SECONDS)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            pass

    async def _maintenance_loop(self) -> None:
        """周期执行上传对账与租期清理；异常绝不终止循环。"""
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                await self._reconcile_uploads_once()
                await self.cleanup_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log_event(logger, logging.WARNING, "image.maintenance", reason="failed")

    # --- 单条请求 -----------------------------------------------------------

    async def handle(self, request: ImageRequest) -> None:
        """消费一条图像任务；无论成败都在 `finally` 终结触发事件。"""
        delivered = False
        try:
            delivered = await self._run(request)
        finally:
            await self._finalize_event(request, delivered)

    async def _finalize_event(self, request: ImageRequest, delivered: bool) -> None:
        """终结触发事件：已送达优先 `done`，否则过期标 `skipped`。"""
        status = "done" if delivered or not self._expired(request) else "skipped"
        try:
            await self._store.mark_handled(request.message_id, status)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(logger, logging.WARNING, "image.finalize", reason="failed")

    async def _run(self, request: ImageRequest) -> bool:
        """完整链路；返回是否已确认送达（供事件终态判定）。"""
        if self._expired(request):
            return False

        if await self._store.is_image_user_blocked(self._scope, request.actor_id):
            await self._notice(request, IMAGE_BLACKLIST_TEXT)
            return False

        # 编辑入口：预留前完成参考图取回与格式校验，缺图/超限/取图失败一律不占额度。
        reference: ImageOutput | None = None
        if request.mode == MODE_EDIT:
            try:
                reference = await self._load_reference(request)
            except _ReferenceUnavailable:
                await self._notice(request, IMAGE_REFERENCE_TEXT)
                return False
        if self._provider is None or not self._alive(request):
            return False

        prompt = self._redactor.redact(request.prompt)
        now = self._now()
        job_id = uuid.uuid4().hex
        reservation = await self._store.reserve_image_job(
            scope=self._scope,
            job_id=job_id,
            trigger_message_id=request.message_id,
            channel_id=request.channel_id,
            mode=request.mode,
            now=now,
        )
        if not reservation.allowed:
            if reservation.reason != IMAGE_REASON_DUPLICATE:
                await self._notice(request, IMAGE_QUOTA_TEXT)
            return False

        # 预留后、调用前再复核一次：失效则释放预留，不产生任何付费副作用。
        blocked = await self._store.is_image_user_blocked(self._scope, request.actor_id)
        if blocked or not self._alive(request):
            await self._store.release_image_job(job_id, self._now())
            if blocked:
                await self._notice(request, IMAGE_BLACKLIST_TEXT)
            return False
        if not await self._store.begin_image_generation(job_id, self._now()):
            return False

        output: ImageOutput
        try:
            output = await self._provider.generate(prompt, reference=reference)
        except ImageProviderError as exc:
            current = self._now()
            if exc.reason == REASON_CONTENT_POLICY:
                await self._store.block_image_user(
                    self._scope, request.actor_id, reason="content_policy",
                    now=current, trigger_message_id=request.message_id,
                )
            log_event(
                logger, logging.WARNING, "image.generate_failed",
                reason=exc.reason, status="unknown" if exc.uncertain else "rejected",
                http_status=exc.http_status, stage="generate", trace_id=request.trace_id,
            )
            if exc.uncertain:
                # 结果不确定：保留占额、不自动重试（可能已产生付费输出）。
                await self._store.mark_generation_unknown(job_id, current)
            else:
                # 明确未生成：释放本次预留。
                await self._store.fail_image_generation(job_id, exc.reason, current)
            await self._notice(request, IMAGE_BLACKLIST_TEXT if exc.reason == REASON_CONTENT_POLICY else IMAGE_FAILURE_TEXT)
            return False
        except BaseException:
            # 未预期异常或取消：请求可能已发出，保守按不确定占额后原样抛出。
            await self._store.mark_generation_unknown(job_id, self._now())
            raise
        finally:
            # 参考图字节涉密，用完立即释放，不写文件或数据库。
            reference = None

        # 生成已确认：无论输出是否合格、上传/发送是否成功，这一张都已计费。
        await self._store.confirm_image_generated(job_id, self._now())
        if await self._store.is_image_user_blocked(self._scope, request.actor_id):
            await self._notice(request, IMAGE_BLACKLIST_TEXT)
            return False
        return await self._upload_and_send(request, job_id, output)

    async def _load_reference(self, request: ImageRequest) -> ImageOutput:
        """按站内图片 ID 取出参考图字节并校验 PNG/JPEG/WebP。"""
        image_id = request.reference_image_id
        if not image_id:
            raise _ReferenceUnavailable
        try:
            path = image_raw_path(image_id)
            data = await self._site.fetch_image(
                path, max_bytes=self._cfg.max_reference_bytes
            )
        except (ImageFetchError, SiteError, ValueError):
            raise _ReferenceUnavailable from None
        mime = _bitmap_mime(data)
        if mime is None or not data:
            raise _ReferenceUnavailable
        return ImageOutput(data=data, mime_type=mime)

    async def _upload_and_send(
        self, request: ImageRequest, job_id: str, output: ImageOutput
    ) -> bool:
        """锁内腾空间并上传，锁外提示与发送；返回是否已确认送达。

        失败提示在**锁外**发出：维护锁只覆盖「维护/腾空间/上传」，不该被一次慢通知
        占住，否则会拖住清理任务与其他上传。
        """
        async with self._maintenance:
            outcome = await self._upload_locked(request, job_id, output)
        if outcome is None:
            # 上传前任务已失效：不继续上传，也不发提示。
            return False
        image_id, expires_at, failure_text = outcome
        if failure_text is not None:
            await self._notice(request, failure_text)
            return False
        # 已登记租期但不补发旧结果（D-154）；图仍进入清理计划。
        if image_id is None or expires_at is None or not self._alive(request):
            return False
        return await self._send_result(request, job_id, image_id, expires_at)

    async def _upload_locked(
        self, request: ImageRequest, job_id: str, output: ImageOutput
    ) -> tuple[str | None, float | None, str | None] | None:
        """在维护锁内腾空间并上传；返回 (image_id, expires_at, 失败文案)。"""
        if not self._alive(request):
            return None
        now = self._now()
        failure_text: str | None = None
        image_id: str | None = None
        expires_at: float | None = None
        try:
            enough = await self._ensure_space_locked(len(output.data), now)
        except SiteError:
            await self._store.fail_image_upload(job_id, "space_check_failed", now)
            return (None, None, IMAGE_SPACE_TEXT)
        if not enough:
            await self._store.fail_image_upload(job_id, "insufficient_space", now)
            return (None, None, IMAGE_SPACE_TEXT)
        if not self._alive(request):
            return None
        # 随机文件名在上传 POST **之前**持久化：崩溃后靠它精确对账。
        filename = f"{job_id}.{_MIME_EXT[output.mime_type]}"
        await self._store.mark_image_uploading(job_id, filename, now)
        if not self._alive(request):
            await self._store.fail_image_upload(job_id, "stale_generation", self._now())
            return None
        try:
            uploaded = await self._site.upload_image(
                output.data, output.mime_type, filename
            )
        except SiteError as exc:
            if exc.status == 0:
                # 网络/5xx/非法成功形状：结果不确定，只读对账、不自动重传。
                await self._store.mark_upload_unknown(job_id, self._now())
            else:
                await self._store.fail_image_upload(
                    job_id, f"http_{exc.status}", self._now()
                )
            failure_text = IMAGE_FAILURE_TEXT
        else:
            now = self._now()
            upload_job = await self._store.find_image_job(job_id)
            uploaded_at = upload_job.uploaded_at
            expires_at = upload_job.expires_at
            await self._store.confirm_image_upload(
                job_id, uploaded.id, uploaded_at, expires_at, now
            )
            image_id = uploaded.id
        return (image_id, expires_at, failure_text)

    async def _send_result(
        self, request: ImageRequest, job_id: str, image_id: str, expires_at: float
    ) -> bool:
        """把生成结果作为附件回复；不确定结果只读对账、保留待核实。"""
        text = IMAGE_RESULT_TEXT.format(expires_at=_format_deadline(expires_at))
        try:
            result = await self._sender.send(
                request.channel_id,
                text,
                request.message_id,
                kind="reply",
                image_id=image_id,
                thread_root_id=request.thread_root_id,
                expires_at=request.expires_at,
                generation_is_current=self._guard(request),
            )
        except BaseException:
            await self._store.mark_delivery_unknown(job_id, self._now())
            raise
        now = self._now()
        if getattr(result, "delivered", False):
            message_id = getattr(result, "message_id", None)
            if message_id is not None:
                await self._store.confirm_image_delivered(job_id, message_id, now)
            return True
        reason = getattr(result, "reason", "failed")
        if reason in _UNCERTAIN_DELIVERY_REASONS:
            await self._store.mark_delivery_unknown(job_id, now)
        # 其余是确定未送达（expired/stale_generation/backoff/reply_target_gone 等）：
        # 状态停在 uploaded，图仍按租期登记清理，不重投。
        return False

    async def _notice(self, request: ImageRequest, text: str) -> None:
        """发一条错误提示（notice）；失效任务不再发送，但事件终态照落。"""
        if not self._alive(request):
            return
        await self._sender.send(
            request.channel_id,
            text,
            request.message_id,
            kind="notice",
            actor_id=request.actor_id,
            thread_root_id=request.thread_root_id,
            expires_at=request.expires_at,
            generation_is_current=self._guard(request),
        )

    # --- 图床维护与容量淘汰 -------------------------------------------------

    async def cleanup_once(self) -> None:
        """一次租期维护：结清待对账的删除、删除 30 天到期图。可注入时钟测试。"""
        async with self._maintenance:
            await self._cleanup_locked(self._now())

    async def _cleanup_locked(self, now: float) -> None:
        deletes = await self._store.list_image_delete_reconcile(self._scope)
        candidates = await self._store.list_image_eviction_candidates(self._scope)
        if not deletes and not any(job.expires_at is not None and job.expires_at <= now for job in candidates):
            return
        images = await self._site.list_images()
        present = {image.id for image in images}
        # 先结清「意图已写、结果未知」的删除：图已不在列表即视为已删除。
        for job in deletes:
            if job.site_image_id not in present:
                await self._store.record_image_delete_result(
                    job.job_id, IMAGE_DELETE_DONE, now
                )
            else:
                await self._delete_image_locked(
                    job, job.delete_reason or IMAGE_DELETE_CAPACITY, now
                )
        # 再删到期图；淘汰候选按首次上传时间从旧到新，到期顺序与之同序。
        for job in await self._store.list_image_eviction_candidates(self._scope):
            if (
                job.expires_at is not None
                and job.expires_at <= now
                and job.site_image_id in present
            ):
                await self._delete_image_locked(job, IMAGE_DELETE_EXPIRED, now)

    async def _ensure_space_locked(self, needed_bytes: int, now: float) -> bool:
        """腾出至少 `needed_bytes`：先到期图，再最旧图，够用即停。返回是否已够。

        精确空间用 `list_images` 的 `file_size` 求和（图床 quota 是舍入展示值）。
        每删一张重新确认删除结果与剩余配额；本功能可淘汰图不足时不删其他图片。
        """
        images = await self._site.list_images()
        quota = await self._site.image_quota()
        if needed_bytes > quota.limit_bytes:
            return False
        used = sum(image.file_size for image in images)
        if used + needed_bytes <= quota.limit_bytes:
            return True
        for job in await self._store.list_image_eviction_candidates(self._scope):
            if used + needed_bytes <= quota.limit_bytes:
                break
            current = {image.id for image in images}
            if job.site_image_id not in current:
                continue
            reason = (
                IMAGE_DELETE_EXPIRED
                if job.expires_at is not None and job.expires_at <= now
                else IMAGE_DELETE_CAPACITY
            )
            if not await self._delete_image_locked(job, reason, now):
                continue
            images = await self._site.list_images()
            quota = await self._site.image_quota()
            used = sum(image.file_size for image in images)
        return used + needed_bytes <= quota.limit_bytes

    async def _delete_image_locked(self, job: ImageJob, reason: str, now: float) -> bool:
        """持久化删除意图与结果；返回是否确认已删除。只删本功能登记的图片。"""
        if not job.site_image_id:
            return False
        await self._store.record_image_delete_intent(job.job_id, reason, now)
        try:
            await self._site.delete_image(job.site_image_id)
        except SiteError as exc:
            if exc.status in (400, 404):
                # 已删除 / 不存在：对清理而言是终态（图床契约 §7）。
                await self._store.record_image_delete_result(
                    job.job_id, IMAGE_DELETE_DONE, now
                )
                return True
            await self._store.record_image_delete_result(
                job.job_id, IMAGE_DELETE_FAILED, now
            )
            log_event(logger, logging.WARNING, "image.delete", reason="failed")
            return False
        await self._store.record_image_delete_result(job.job_id, IMAGE_DELETE_DONE, now)
        return True

    async def _reconcile_uploads_once(self) -> None:
        """按随机文件名精确对账上传结果未知的任务；只读、不重新生成。

        查不到、列表异常或命中多个都不是「未上传」的证明：保持未知，继续占额与清理计划。
        """
        async with self._maintenance:
            jobs = await self._store.list_image_upload_reconcile(self._scope)
            if not jobs:
                return
            now = self._now()
            images = await self._site.list_images()
            by_filename: dict[str, list[str]] = {}
            for image in images:
                by_filename.setdefault(image.filename, []).append(image.id)
            for job in jobs:
                matches = by_filename.get(job.upload_filename or "", [])
                if len(matches) != 1:
                    continue
                # 保守租期起点：用进入未知的阶段时间，避免未知阶段把有效期拉长。
                started = job.uploaded_at if job.uploaded_at is not None else job.created_at
                await self._store.confirm_image_upload(
                    job.job_id, matches[0], started, started + RETENTION_SECONDS, now
                )

    # --- 判定辅助 -----------------------------------------------------------

    def _expired(self, request: ImageRequest) -> bool:
        return request.expires_at is not None and self._now() >= request.expires_at

    def _alive(self, request: ImageRequest) -> bool:
        """截止时间与代次都有效才继续；守卫异常按失效处理，不泄漏预留。"""
        if self._expired(request):
            return False
        try:
            return bool(self._generation_is_current(request))
        except Exception:
            return False

    def _guard(self, request: ImageRequest) -> Callable[[], bool]:
        """把收 `ImageRequest` 的守卫适配成发送器要的同步无参守卫。"""
        return lambda: self._generation_is_current(request)


def _format_deadline(expires_at: float) -> str:
    """把绝对 epoch 秒格式化为北京时间（UTC+8）的展示串；不含其他信息。"""
    return datetime.fromtimestamp(expires_at, tz=_BEIJING).strftime("%Y-%m-%d %H:%M")
