"""评论树索引与触发匹配。

本模块只负责在内存中解释评论 DTO。它不访问站点、不写数据库，也不调用模型。
所有候选最终都必须交给调用方的原子 `claim_comment`，这样最近评论轮询与通知轮询
同时看到一条评论时仍然只有一个发送赢家。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ..site.comment_models import CommentNode, CommentNotification, actor_fingerprint
from ..text_utils import contains_bot_mention


class CommentTreeTooLarge(ValueError):
    """评论树超过安全节点上限，调用方不得使用部分索引。"""


@dataclass(frozen=True)
class CommentCandidate:
    """一个待交给 Store 原子领取的评论候选。"""

    comment: CommentNode
    source: str
    notification_id: str | None = None
    reason: str = ""
    # 直接回复场景的父机器人正文只在内存树中携带，供重启后的当前轮临时引用；
    # 首次 @ 和通知定位若无正文则保持 None，不会把第三方评论外送。
    parent_bot_text: str | None = field(default=None, compare=False, repr=False)

    @property
    def node(self) -> CommentNode:
        """兼容调用方把候选称为节点。"""
        return self.comment

    @property
    def comment_id(self) -> str:
        return self.comment.id

    @property
    def blog_id(self) -> str:
        return self.comment.blog_id


@dataclass(frozen=True)
class CommentTreeIndex:
    """完整评论树的扁平索引。

    `from_roots` 使用显式栈，达到节点上限时直接抛出异常，不向匹配器暴露截断后的
    前缀。`by_id` 和 `children_by_parent` 都只保留内存 DTO。
    """

    nodes: tuple[CommentNode, ...]
    by_id: Mapping[str, CommentNode]
    children_by_parent: Mapping[str | None, tuple[CommentNode, ...]]

    @classmethod
    def from_roots(
        cls, roots: Iterable[CommentNode], *, max_nodes: int = 10_000
    ) -> "CommentTreeIndex":
        if isinstance(max_nodes, bool) or max_nodes <= 0:
            raise ValueError("max_nodes 必须为正整数")

        stack = list(reversed(tuple(roots)))
        flattened: list[CommentNode] = []
        by_id: dict[str, CommentNode] = {}
        children: defaultdict[str | None, list[CommentNode]] = defaultdict(list)
        while stack:
            node = stack.pop()
            if not isinstance(node, CommentNode):
                continue
            flattened.append(node)
            if len(flattened) > max_nodes:
                raise CommentTreeTooLarge(f"comment tree exceeds {max_nodes} nodes")
            by_id.setdefault(node.id, node)
            children[node.parent_id].append(node)
            # 反转后压栈，保证展平顺序与接口返回的深度优先顺序一致；顺序本身
            # 不参与权限判断，只用于同时间候选的稳定排序。
            stack.extend(reversed(node.children))

        return cls(
            nodes=tuple(flattened),
            by_id=by_id,
            children_by_parent={key: tuple(value) for key, value in children.items()},
        )

    @classmethod
    def build(
        cls, roots: Iterable[CommentNode], *, max_nodes: int = 10_000
    ) -> "CommentTreeIndex":
        """`from_roots` 的简短别名，便于服务装配代码阅读。"""
        return cls.from_roots(roots, max_nodes=max_nodes)

    def get(self, comment_id: str) -> CommentNode | None:
        return self.by_id.get(comment_id)


def flatten_comment_tree(
    roots: Iterable[CommentNode], *, max_nodes: int = 10_000
) -> tuple[CommentNode, ...]:
    """使用显式栈展平评论树，并在超限时拒绝整棵树。"""
    return CommentTreeIndex.from_roots(roots, max_nodes=max_nodes).nodes


def _is_eligible(node: CommentNode, *, bot_user_id: str | None) -> bool:
    """判断节点是否可以被送入后续匹配。"""
    if node.status != "approved" or node.is_deleted:
        return False
    author_id = node.author.id
    if not author_id:
        return False
    return bot_user_id is None or author_id != bot_user_id


def _sort_key(node: CommentNode) -> tuple[float, str]:
    """把缺失时间放到最后，并以 UUID 字符串保证稳定顺序。"""
    return (node.created_at if node.created_at is not None else float("inf"), node.id)


class CommentMatcher:
    """按设计文档的优先级识别直接回复与首次精确提及。"""

    def __init__(
        self,
        *,
        bot_username: str,
        bot_user_id: str | None = None,
        max_nodes: int = 10_000,
    ) -> None:
        self.bot_username = bot_username
        self.bot_user_id = bot_user_id
        self.max_nodes = max_nodes

    def build_index(self, roots: Iterable[CommentNode]) -> CommentTreeIndex:
        """为一篇文章建立完整索引。"""
        return CommentTreeIndex.from_roots(roots, max_nodes=self.max_nodes)

    def recent_candidates(
        self,
        observed: Iterable[CommentNode],
        index: CommentTreeIndex,
        *,
        known_bot_comment_ids: Iterable[str] = (),
        processed_ids: Iterable[str] = (),
    ) -> tuple[CommentCandidate, ...]:
        """匹配最近评论候选；直接回复优先于首次精确 `@`。"""
        known = set(known_bot_comment_ids)
        processed = set(processed_ids)
        observed_ids = {node.id for node in observed}
        direct: list[CommentCandidate] = []
        mentions: list[CommentCandidate] = []
        for comment_id in observed_ids:
            node = index.by_id.get(comment_id)
            if node is None or node.id in processed or not _is_eligible(
                node, bot_user_id=self.bot_user_id
            ):
                continue
            is_direct = node.parent_id is not None and node.parent_id in known
            if is_direct:
                parent = index.by_id.get(node.parent_id)
                direct.append(
                    CommentCandidate(
                        node,
                        "recent",
                        reason="direct_reply",
                        parent_bot_text=(parent.content if parent is not None else None),
                    )
                )
                continue
            if node.content is not None and contains_bot_mention(
                node.content, self.bot_username
            ):
                mentions.append(CommentCandidate(node, "recent", reason="first_mention"))
        direct.sort(key=lambda item: _sort_key(item.comment))
        mentions.sort(key=lambda item: _sort_key(item.comment))
        return tuple(direct + mentions)

    def notification_candidates(
        self,
        notification: CommentNotification,
        index: CommentTreeIndex,
        *,
        known_bot_comment_ids: Iterable[str] = (),
        processed_ids: Iterable[str] = (),
    ) -> tuple[CommentCandidate, ...]:
        """在一棵文章树内定位通知 actor 的全部直接回复。"""
        if notification.action != "评论回复" or not notification.actor_id:
            return ()
        actor_fp = actor_fingerprint(notification.actor_id)
        if actor_fp is None:
            return ()
        known = set(known_bot_comment_ids)
        processed = set(processed_ids)
        matched: list[CommentCandidate] = []
        for node in index.nodes:
            if node.id in processed or not _is_eligible(
                node, bot_user_id=self.bot_user_id
            ):
                continue
            if node.parent_id not in known:
                continue
            parent = index.by_id.get(node.parent_id)
            if parent is None:
                continue
            if actor_fingerprint(node.author.id) != actor_fp:
                continue
            # 缺失时间不应阻止一个 otherwise-valid 站点节点；若两者都有时间，
            # 严格拒绝早于机器人父评论的条目，防止旧历史碰巧被通知命中。
            if (
                node.created_at is not None
                and parent.created_at is not None
                and node.created_at < parent.created_at
            ):
                continue
            matched.append(
                CommentCandidate(
                    node,
                    "notification",
                    notification_id=notification.id,
                    reason="notification_reply",
                    parent_bot_text=parent.content,
                )
            )
        matched.sort(key=lambda item: _sort_key(item.comment))
        return tuple(matched)

    def match_recent(self, *args, **kwargs) -> tuple[CommentCandidate, ...]:
        """`recent_candidates` 的兼容别名。"""
        return self.recent_candidates(*args, **kwargs)

    def match_notification(self, *args, **kwargs) -> tuple[CommentCandidate, ...]:
        """`notification_candidates` 的兼容别名。"""
        return self.notification_candidates(*args, **kwargs)


__all__ = [
    "CommentCandidate",
    "CommentMatcher",
    "CommentTreeIndex",
    "CommentTreeTooLarge",
    "flatten_comment_tree",
]
