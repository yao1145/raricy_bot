"""图像子系统的公共 DTO 与代码常量。

这里只放跨模块共享的数据形状：`ImageRequest` 由路由构造、由 `ImageService` 消费；
`ImageOutput` 由提供方产出、由 `ImageService` 上传。两者都只在内存里传递，
不落库、不进日志：`prompt` 与图片字节都标了 `repr=False`，防止 `repr(dto)`
把用户描述或 Base64 带进异常文案与调试输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 全站滚动窗口：24 小时内最多 IMAGE_LIMIT 张，生成与编辑共享同一份额度（设计 §5）。
WINDOW_SECONDS: int = 86400
IMAGE_LIMIT: int = 50

# 从首次图床上传时间起最多保留 14 天（D-162）。
RETENTION_SECONDS: int = 14 * 86400

# 输出图硬上限：与站点图床单文件上限一致（10 MiB）。
MAX_OUTPUT_BYTES: int = 10 * 1024 * 1024

# 上传前压缩结果必须严格小于 200 KB（十进制字节）。
MAX_UPLOAD_BYTES: int = 200_000

# 模式字面量。`edit` 是唯一会读取参考图的入口；普通 `generate` 即使用户附图也不取图。
MODE_GENERATE: str = "generate"
MODE_EDIT: str = "edit"


@dataclass(frozen=True)
class ImageRequest:
    """一次图像任务的不可变描述（冻结接口）。

    描述与参考图字节都不进 SQLite；`trace_id` 只用于把同一条链路的多条日志串起来。
    `expires_at` 是处理截止时间（UTC 秒），超过后不再继续付费或发送。
    """

    event_id: int | None
    channel_id: str
    channel_kind: str
    session_key: str
    generation: int
    message_id: int
    actor_id: str | None
    prompt: str = field(repr=False)
    mode: str = MODE_GENERATE
    reference_image_id: str | None = None
    thread_root_id: int | None = None
    trace_id: str = ""
    expires_at: float | None = None


@dataclass(frozen=True)
class ImageOutput:
    """提供方产出的单张位图；`data` 与 `repr` 隔离，避免字节被打印出来。"""

    data: bytes = field(repr=False)
    mime_type: str
