"""站点发文与文章列表搜索的 DTO 与常量（INTERFACES §53.5）。

只含站点 DTO 与常量：不 import config、Store、Publisher，也**不** import `blog/` ——
依赖方向是业务层读站点层的判断结果，站点层不反过来依赖业务层。因此这里的四个
`OUTCOME_*` 是**站点层的分类**（这一次 HTTP 往返算哪一类），落库用的持久原因常量仍由
`blog/models.py` 提供，两者的映射在 Publisher 里显式做一次。

`POST /api/blogs` 是 D-106 显式记录的接口例外：它是普通用户网页表单的同一个接口，
不在站方机器人契约里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 一次发布的分类结果。取值与 `blog/models.py` 的同名原因 token 相同，
# 但语义不同：这里只回答「这次往返属于哪一类」，不回答「这一行该落什么状态」。
OUTCOME_PUBLISHED: str = "published"
OUTCOME_REJECTED: str = "rejected"
OUTCOME_RATE_LIMITED: str = "rate_limited"
OUTCOME_UNCONFIRMED: str = "unconfirmed"


@dataclass(frozen=True)
class BlogPublishResult:
    """一次 `publish_blog` 的分类结果。

    `reason` 是稳定原因 token，**绝不**带站方响应正文：错误文案可能回显标题或正文。
    `blog_id` 只在 `published` 时给出，且一定是合法 UUID（已规范化）；
    `code` 只在拿到了**解析成功的业务信封**时给出，仅供日志与分类，不参与成败判定。
    """

    outcome: str
    reason: str
    blog_id: str | None = None
    code: int | None = None


@dataclass(frozen=True)
class BlogSearchItem:
    """文章列表里的一项：只保留对账需要的三个字段。

    标题不进 repr：日志与异常回溯都不得出现搜索结果标题（INTERFACES §53.13）。
    """

    blog_id: str
    title: str = field(repr=False)
    author_id: str


@dataclass(frozen=True)
class BlogSearchPage:
    """一页文章列表；`has_next` 为假表示服务端没有下一页。"""

    items: tuple[BlogSearchItem, ...]
    has_next: bool
