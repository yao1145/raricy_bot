"""用户画像命令的解析与数据传输对象（设计 §2、§8；纯函数、无 I/O）。

解析只回答「这条消息是不是画像命令、是哪一个、参数是什么」，不查权限、不碰文件、不调模型。
判定口径与既有的能力命令、记忆命令一致：整个消息**去掉首尾空白后**必须以 `/persona` 开头，
命令词之后必须是空白或字符串结束，因此 `/personax`、`/personaed` 与正文中间的 `/persona`
都不命中（返回 `None`，走普通聊天）。

三个失败口径与记忆命令同款：

- **不命中**（返回 `None`）：不是画像命令。未知子命令也**不**返回 `None`，见下条。
- **用法哨兵**（`PersonaCommand(name=USAGE_COMMAND)`）：命令词认出来了，但子命令不认识、
  参数形状不对或多余实参。它绝不是 `None`，上层据此回固定用法文案，避免把管理意图
  当成普通聊天正文递给模型（设计 §2 末段）。

隐式输入（不是命令、而是对流程消息的回复或编号回答）由 `parse_numbered_answers()` 与
`is_confirmation_text()` 判定，它们同样是纯函数：Router 只做同步的形状识别，真正的归属
判断（当前阶段是不是在等回答）由 Controller 用缓存的流程快照完成。

阶段名与流程种类（`STAGE_*` / `KIND_*`）是**共享令牌**：Controller 写、MemoryService 存，
两边必须逐字一致，因此定义在这里而不是散落在字符串字面量里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CONFIRMATION_TEXT",
    "CONTROL_COMMANDS",
    "DELIVERY_DRAFT",
    "DELIVERY_NOTICE",
    "DELIVERY_PROMPT",
    "DRAFT_PREFIX",
    "KIND_CLEAR",
    "KIND_CREATE",
    "KIND_EDIT",
    "KIND_PUBLIC",
    "MAX_ANSWER_INDEX",
    "MAX_INPUT_CHARS",
    "MUTATING_COMMANDS",
    "PERSONA_COMMAND",
    "SKIP_ANSWER",
    "STAGE_AWAITING_ANSWERS",
    "STAGE_AWAITING_FINAL_REVIEW",
    "STAGE_AWAITING_REVIEW",
    "STAGE_COMMITTED",
    "STAGE_DRAFT_PENDING_DELIVERY",
    "STAGE_REVISING",
    "USAGE_COMMAND",
    "PersonaCommand",
    "PersonaCommandRequest",
    "PersonaCommandResult",
    "PersonaSessionHint",
    "format_draft_id",
    "is_confirmation_text",
    "normalize_argument",
    "parse_draft_id",
    "parse_numbered_answers",
    "parse_persona_command",
]

# 用法哨兵的 `name` 取值：既不是任何合法子命令，也不是空字符串以外的值（与记忆命令同款）。
USAGE_COMMAND: str = ""

# 命令词本体；解析时会整词、大小写不敏感地比较。
PERSONA_COMMAND: str = "/persona"

# 草稿编号前缀（设计 §2 `/persona confirm <草稿编号>`）：编号由 Controller 生成，
# 存储层只把它当不透明字符串保存。序号只接受 ASCII 十进制。
DRAFT_PREFIX: str = "PD-"

# 建档问题的数量（设计 §1/§3：恰好 5 题，不增加第 6 个）。
MAX_ANSWER_INDEX: int = 5

# 回答 `skip` 表示明确跳过该题（设计 §2 的 `/persona answer <编号> <回答>`）。
SKIP_ANSWER: str = "skip"

# 回复草稿消息时的确认词（设计 §2：`同意` 可作为回复当前草稿的确认，独立发送必须带编号）。
CONFIRMATION_TEXT: str = "同意"

# 单条用户输入（一题的回答、一次修改意见）的长度上限。**超限即拒绝，绝不截断**：
# 截断后的回答会与用户真正写下的内容不一致，用户看到的是「已记录」而盘上是半句。
# 命令路径不经过聊天侧的超长拦截，因此这个上限由控制器在保存前显式检查
# （`texts.PERSONA_INPUT_TOO_LONG_TEXT` 的文案就是按 1000 字写的）。
MAX_INPUT_CHARS: int = 1000

# 工作流阶段（设计 §9 的清单；共享令牌，逐字固定）。
STAGE_AWAITING_ANSWERS: str = "awaiting_answers"
STAGE_DRAFT_PENDING_DELIVERY: str = "draft_pending_delivery"
STAGE_AWAITING_REVIEW: str = "awaiting_review"
STAGE_REVISING: str = "revising"
STAGE_AWAITING_FINAL_REVIEW: str = "awaiting_final_review"
STAGE_COMMITTED: str = "committed"

# 流程种类（设计 §9：公开与删除预览使用各自 kind，复用带编号确认）。
KIND_CREATE: str = "create"
KIND_EDIT: str = "edit"
KIND_PUBLIC: str = "public"
KIND_CLEAR: str = "clear"

# **立即控制类**子命令（设计 §6、§7）：收到授权命令的那一刻就要让在途的自动更新失效，
# 其中 `off` / `clear` / `cancel` 还要取消该用户在途的画像命令、清空证据缓冲；
# `edit` 进入手动编辑，`private` 撤回公开副本。Router 据这个集合调用装配层的同步回调，
# 不等排队执行（用户已经明确要求停止时，在途任务不能继续落盘）。
CONTROL_COMMANDS: frozenset[str] = frozenset(
    {"off", "clear", "cancel", "edit", "private"}
)

# **会写入**流程或画像的子命令：请求入队时的控制代次与执行时不一致就丢弃（装配层比对）。
# `view` 与用法提示不在其中：它们只读当前状态，任何时刻回答都不过期。
MUTATING_COMMANDS: frozenset[str] = frozenset(
    {
        "view",
        "on",
        "off",
        "edit",
        "answer",
        "answer_batch",
        "revise",
        "confirm",
        "cancel",
        "public",
        "private",
        "clear",
    }
)

# 送达标记的种类（`PersonaCommandResult.delivery_kind`）：
# - `notice` 是**使用说明**：它送达之后才开始采集（设计 §3.1）；
# - `prompt` 是阶段提示消息（问卷列表 / 编辑提示 / 公开预览 / 清空预览，统一落在
#   `questions_message_id` 上，供「回复该消息」的隐式输入归属）；
# - `draft` 是需要审查的草稿（送达后 `delivered` 置真，确认才有依据）。
DELIVERY_DRAFT: str = "draft"
DELIVERY_PROMPT: str = "prompt"
DELIVERY_NOTICE: str = "notice"

# 不接收实参的子命令：对它们来说「带实参」就是形状错误。
_BARE_COMMANDS: frozenset[str] = frozenset(
    {"on", "off", "edit", "cancel", "public", "private", "clear"}
)

# ID 序号只接受 ASCII 十进制（`str.isdigit()` 会放过全角数字）。
_ASCII_DIGITS = re.compile(r"[0-9]+")

# 编号回答的行形态：`1. 内容` / `1、内容` / `1：内容` / `1) 内容`，编号后必须有分隔符。
# 这样 `1 + 1 = 2` 不会被误判成第 1 题的回答。
_NUMBERED_LINE = re.compile(r"^([0-9]{1,2})\s*[.、．:：)）]\s*(\S.*)$")


@dataclass(frozen=True)
class PersonaCommand:
    """一条解析后的画像命令（设计 §2）。

    `argument` 是子命令的正文实参（`answer` 的回答、`revise` 的修改意见、`confirm` 的草稿编号）；
    `number` 只用于 `answer`（题号 1..5）与 `confirm`（草稿序号）。
    """

    name: str
    argument: str | None = None
    number: int | None = None


@dataclass(frozen=True)
class PersonaCommandRequest:
    """一次画像命令的执行请求（设计 §8）；由 Router 构造并入队。

    `user_id` / `username` 是站点身份：`user_id` 决定私有文件归属，`username` 是公开快照的索引键
    （与记忆命令同款，两者都只来自站点 DTO）。`reply_to_message_id` 是本条消息**直接回复**的
    站点消息 id（没有引用时为 None）：访谈与草稿的隐式输入据此归属到当前阶段提示消息。
    """

    event_id: int | None
    message_id: int
    channel_id: str
    channel_kind: str
    session_key: str
    user_id: str
    username: str
    command: PersonaCommand
    reply_to_message_id: int | None = None
    expires_at: float | None = None
    # 入队那一刻的**控制代次**（装配层维护）：收到 `off` / `clear` / `cancel` / `edit` /
    # `private` 时递增。worker 执行前比对，代次对不上说明这条请求已经被后来的控制命令
    # 取代，直接丢弃（用户已经改主意，旧回答不该再写进流程）。
    control_seq: int = 0


@dataclass(frozen=True)
class PersonaCommandResult:
    """一次画像命令的结果（设计 §8）：稳定状态 + 用户可见文案 + 需要标记送达的对象。

    `delivery_kind` 非空时表示「这条文案本身是一次展示」：App 确认送达后必须回调
    `PersonaController.mark_delivered()` 记下真实送达的消息 id，否则确认（`confirm`）会
    因为没有已送达的草稿而被拒（设计 §9：确认只接受已送达的当前草稿）。
    """

    status: str
    text: str
    draft_id: str | None = None
    delivery_kind: str | None = None


@dataclass(frozen=True)
class PersonaSessionHint:
    """当前用户流程的同步快照（Router 用，无 I/O）。

    它只承载 Router 判定「这条普通消息算不算流程输入」所需的稳定形状：阶段、阶段提示消息 id、
    已送达草稿的消息 id、当前草稿编号。不含问题、回答、短文或任何来源正文。
    """

    stage: str
    kind: str
    questions_message_id: int | None
    delivered_message_id: int | None
    delivered: bool
    draft_id: str = ""


def parse_persona_command(text: str) -> PersonaCommand | None:
    """解析开头的 `/persona` 命令；不是画像命令时返回 `None`（纯函数）。

    命令词大小写不敏感，`/persona off `（尾随空白）有效，`/personax` 不命中。
    """
    if not isinstance(text, str):
        # 调用方偶尔会把非法值递进来；解析器不做类型推断，一律按「不是命令」处理。
        return None
    stripped = text.strip()
    if not _starts_with_command(stripped, PERSONA_COMMAND):
        return None
    return _parse_body(stripped[len(PERSONA_COMMAND) :].strip())


def parse_numbered_answers(text: str) -> tuple[tuple[int, str], ...]:
    """从一段正文里抽出编号回答（`1. 内容` 等），按出现顺序返回 `(题号, 回答)`。

    只认 1..`MAX_ANSWER_INDEX` 的编号与带分隔符的行；同一题重复出现时保留**最后**一条
    （逐行覆盖更符合「用户改了主意」的直觉）。`skip` 原样保留，由上层与其它回答同等对待。
    """
    found: dict[int, str] = {}
    if not isinstance(text, str):
        return ()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _NUMBERED_LINE.match(line)
        if match is None:
            continue
        number = int(match.group(1))
        if not 1 <= number <= MAX_ANSWER_INDEX:
            continue
        value = match.group(2).strip()
        if value:
            found[number] = value
    return tuple(sorted(found.items()))


def is_confirmation_text(text: str) -> bool:
    """回复草稿消息时的确认词判定（设计 §2）：只有整条正文就是 `同意` 才算确认。"""
    if not isinstance(text, str):
        return False
    return text.strip() == CONFIRMATION_TEXT


def format_draft_id(number: int) -> str:
    """草稿编号的渲染形态：`PD-1`、`PD-12`（宽度不限，解析接受任意 ≥1 位）。"""
    return f"{DRAFT_PREFIX}{int(number)}"


def parse_draft_id(value: str) -> int | None:
    """草稿编号的形状校验（`PD-<ASCII 十进制>`）；不合法返回 `None`。

    前缀大小写敏感：文件与文案里只有大写形态，`pd-1` 不是一条能被找到的草稿。
    """
    if not isinstance(value, str) or not value.startswith(DRAFT_PREFIX):
        return None
    digits = value[len(DRAFT_PREFIX) :]
    if _ASCII_DIGITS.fullmatch(digits) is None:
        return None
    return int(digits)


def normalize_argument(text: str) -> str:
    """把用户实参收敛成落盘形态：只去首尾空白，**不截断**。

    长度由控制器在保存前检查并拒绝（`MAX_INPUT_CHARS`）：解析器保留用户写下的完整内容，
    上层才能把「太长了」如实告诉用户，而不是悄悄存下半句。
    """
    return text.strip()


def _parse_body(rest: str) -> PersonaCommand | None:
    """解析 `/persona` 之后的子命令；空正文是「查看」，形状错误落用法哨兵。"""
    if not rest:
        # 只发一个 `/persona`：查看现有画像，或开始首次建档（设计 §2）。它不是普通正文。
        return PersonaCommand(name="view")
    parts = rest.split(None, 1)
    head = parts[0].lower()
    tail = parts[1].strip() if len(parts) > 1 else ""

    if head in _BARE_COMMANDS:
        return PersonaCommand(name=head) if not tail else _usage()
    if head == "answer":
        return _parse_answer(tail)
    if head == "revise":
        return _parse_revise(tail)
    if head == "confirm":
        return _parse_confirm(tail)
    # 未知子命令**不**返回 None：那会让 `/persona foo` 变成一条普通聊天消息（设计 §2）。
    return _usage()


def _parse_answer(tail: str) -> PersonaCommand:
    """`/persona answer <编号> <回答>`：编号必须是 1..5 的 ASCII 十进制，回答非空。"""
    if not tail:
        return _usage()
    parts = tail.split(None, 1)
    number = _answer_index(parts[0])
    if number is None or len(parts) < 2:
        return _usage()
    value = normalize_argument(parts[1])
    if not value:
        return _usage()
    return PersonaCommand(name="answer", argument=value, number=number)


def _parse_revise(tail: str) -> PersonaCommand:
    """`/persona revise <修改意见或完整短文>`：缺内容落用法（没有意见就不是一次修改）。"""
    value = normalize_argument(tail) if tail else ""
    if not value:
        return _usage()
    return PersonaCommand(name="revise", argument=value)


def _parse_confirm(tail: str) -> PersonaCommand:
    """`/persona confirm <草稿编号>`：缺编号或编号形状不合法落用法，不进任何流程。"""
    number = parse_draft_id(tail)
    if number is None:
        return _usage()
    return PersonaCommand(name="confirm", argument=tail, number=number)


def _answer_index(token: str) -> int | None:
    """题号：只接受 1..`MAX_ANSWER_INDEX` 的 ASCII 十进制。"""
    if _ASCII_DIGITS.fullmatch(token) is None:
        return None
    number = int(token)
    if not 1 <= number <= MAX_ANSWER_INDEX:
        return None
    return number


def _starts_with_command(text: str, command: str) -> bool:
    """`command` 是否正好占据开头；命令词之后必须是空白或字符串结束。"""
    length = len(command)
    if len(text) < length or text[:length].lower() != command:
        return False
    return len(text) == length or text[length].isspace()


def _usage() -> PersonaCommand:
    """用法哨兵：命中了 `/persona` 家族但形状不合法。"""
    return PersonaCommand(name=USAGE_COMMAND)
