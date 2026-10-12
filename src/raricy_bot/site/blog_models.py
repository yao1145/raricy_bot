"""站点发文与文章列表搜索的 DTO 与常量（INTERFACES §53.5）。

只含站点 DTO 与常量：不 import config、Store、Publisher，也**不** import `blog/` ——
依赖方向是业务层读站点层的判断结果，站点层不反过来依赖业务层。因此这里的四个
`OUTCOME_*` 是**站点层的分类**（这一次 HTTP 往返算哪一类），落库用的持久原因常量仍由
`blog_records.py` 提供，两者的映射在 Publisher 里显式做一次。

`POST /api/blogs` 遵循站方 `blog-bot.md` §7（D-106、D-157），只接受契约定义的五个字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 发文接口接受的站外可见性；不传时站点默认为仅站内可见。
BLOG_VISIBILITY_VALUES: tuple[str, ...] = ("internal", "link", "public")
DEFAULT_BLOG_VISIBILITY: str = "internal"


# 一次发布的分类结果。取值与 `blog_records.py` 的同名原因 token 相同，
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


@dataclass(frozen=True)
class PersonaBlogItem:
    """按作者搜索命中候选之一（画像采集用）。

    只保留**核验身份**与**报告时间**所需的三项：作者搜索会命中同名或近似作者，
    因此 `author_id` 是这一层唯一有判定意义的字段，标题不进来（画像不看标题抽样本，
    标题也就没有理由出现在任何日志或回溯里，§53.13）。`created_at` 是站点的展示
    字符串（blog-bot.md §5：东八区墙上时间贴 `Z` 标签），缺失时为空串，只用于
    「覆盖时间」的展示，不参与核验。
    """

    blog_id: str
    author_id: str
    created_at: str = ""


@dataclass(frozen=True)
class PersonaBlogPage:
    """一页作者搜索结果；`has_next` 为假表示服务端没有下一页。"""

    items: tuple[PersonaBlogItem, ...]
    has_next: bool


@dataclass(frozen=True)
class PersonaBlogDetail:
    """一篇带作者身份的博客详情（画像采集用）。

    标题与正文**不进 repr**：两者都是用户内容，异常回溯会把它带进日志。
    `content` 为 None 表示站点没有给出可用正文（被删除、无权限或形状不符），
    由调用方跳过该候选继续找后面的（设计 §4.1）。`author_id` 为空串表示**核验不了**
    身份，同样不得当成本人文章。
    """

    blog_id: str
    author_id: str
    title: str = field(repr=False)
    content: str | None = field(default=None, repr=False)
    created_at: str = ""
