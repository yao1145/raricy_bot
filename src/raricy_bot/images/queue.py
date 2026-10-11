"""图像任务调度器：同一会话最多一个在途或等待任务。"""

import asyncio

from ..core.scheduler import SessionScheduler
from .models import ImageRequest


class ImageScheduler(SessionScheduler[ImageRequest]):
    """保留公平调度和总容量，同时拒绝同会话重复入队。"""

    def __init__(self, *, maxsize: int) -> None:
        super().__init__(maxsize=maxsize)
        self._outstanding: set[str] = set()

    def put_nowait(self, request: ImageRequest) -> None:
        """占用会话只发生在成功入队后。"""
        if request.session_key in self._outstanding:
            raise asyncio.QueueFull
        super().put_nowait(request)
        self._outstanding.add(request.session_key)

    def task_done(self, request: ImageRequest) -> None:
        """完成或取消均释放会话占用。"""
        self._outstanding.discard(request.session_key)
        super().task_done(request)
