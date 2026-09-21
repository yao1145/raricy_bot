"""system 里**唯一**的动态片段：当前时间（设计 §6.1、D-114）。

本模块只做一件事：把一次 `now()` 的返回值渲染成一行定长文本，供调用点追加到 system 末尾。
三条边界必须写死在这里，防止这条明确的红线例外被逐步放宽：

1. 它是 `system` 里唯一的动态片段（依据 D-114）。系统提示与所有静态附加说明都不含运行时值，
   而这一个片段由进程时钟生成，用户完全不可控 —— 这正是它不违反「用户内容只进 role=user」的
   原因。任何新增的动态 system 内容都必须重新走一次决策记录，不得援引本次例外。
2. 除时间戳外**不接受任何入参**，因此结构上没有位置能装进请求、用户、记忆或工具数据。
3. 渲染**不做格式化以外的加工**：时区固定 UTC+8（与 `blog/planner.py` 同源），到分为止，
   不加减任何内容。

依赖方向固定为 `texts <- time_context`：本模块只 import 标准库与 `.texts`，不 import 任何
业务模块，因此四个调用点（聊天、评论与 `blog/` 两个子系统）都能安全引用，不引入新的依赖环。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import texts

# 固定东八区。与 `blog/planner.py` 同源：站内日历本就按 UTC+8 切，模型看到的日历必须一致。
# 不用 zoneinfo，避免依赖宿主机的时区数据库。
UTC8 = timezone(timedelta(hours=8))


def render_current_time(now: float) -> str:
    """把 epoch 秒渲染成 `当前时间：YYYY-MM-DD HH:MM 周X`。

    到分为止，不给秒；星期名按 `datetime.weekday()` 取自 `texts.WEEKDAYS`（周一 = 0）。
    输出定长 24 字符（设计 §7），因此预算估算不会因取值不同而漂移。
    """
    moment = datetime.fromtimestamp(now, UTC8)
    return (
        f"{texts.CURRENT_TIME_LABEL}{moment:%Y-%m-%d %H:%M} "
        f"{texts.WEEKDAYS[moment.weekday()]}"
    )


def append_current_time(system: str, now: float) -> str:
    """把当前时间片段追加到 `system` 末尾。

    `system` 非空时以空行分隔；为空串时只返回片段 —— 评论的回退分支允许 system 为空
    （设计 §6.2）。
    """
    fragment = render_current_time(now)
    if not system:
        return fragment
    return f"{system}\n\n{fragment}"
