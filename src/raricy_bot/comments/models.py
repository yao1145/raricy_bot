"""评论子系统的内存请求类型导出。

类型定义集中在路由模块以避免多份契约；这里提供一个窄的兼容入口，未增加任何
持久化模型。
"""

from .router import (
    CommentClaimIntent,
    CommentRequest,
    CommentRouteResult,
    RouteResult,
    comment_claim_intent,
    comment_session_key,
)

__all__ = [
    "CommentClaimIntent",
    "CommentRequest",
    "CommentRouteResult",
    "RouteResult",
    "comment_claim_intent",
    "comment_session_key",
]
