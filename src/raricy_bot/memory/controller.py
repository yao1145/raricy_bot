"""记忆命令的编排：门禁、幂等、撰写、写入与回复文案（INTERFACES §32.3、规划 §4.5 / §8.1 / §8.2）。

执行顺序是**固定**的（§4.5）：访问门 → 幂等命中 → 读取目标快照 → AI 撰写 → 宿主校验 →
原子写入 → 生成固定用户文案。顺序里最容易被写反的一步是幂等：撰写是有额度成本的，
重复 SSE、resync 或崩溃重放必须**先查 Markdown 的 `operations`**，命中时不再调用 AI，
直接返回第一次的稳定结果（§32.3、D-67）。`MemoryService` 自己也会查，但那是在撰写**之后**；
先写后查等于每次重放都白烧一次模型调用。

三层职责边界：

- **Controller（本模块）**决定「谁可以做什么、参数是什么、失败给用户看什么」；
- **Service**决定「文件里最终是什么」，并在每个 mutation 入口**独立复查** admin（R14）；
- **Writer**只撰写 `key` 与 `content`，作用域、owner 与路径都由本模块固定（D-57）。

正文与 key 绝不进日志（§37）：只记稳定状态与对象 ID。用户可见文案一律取自
`texts.py`（§36）；`texts.py` 未覆盖的输出（`status` / `list` / `candidates` 的状态与列举，
以及若干确认回复）只渲染**数据行**：条目 ID、正文、`字段=取值` 与稳定状态 token，
本模块不新造中文文案 —— 合同缺口记在任务报告里。

失败一律映射成 §27.4 的稳定状态，不抛异常；只有 `asyncio.CancelledError` 原样传播
（软故障 D-60：这里抛出去，用户就既看不到回复、也看不到错误）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .. import texts
from ..logging_setup import get_logger, log_event
from .access import MemoryAccessPolicy
from .codec import PREFIX_ALL_USER, PREFIX_CANDIDATE, PREFIX_LOBBY
from .commands import USAGE_COMMAND, MemoryCommandRequest, MemoryCommandResult
from .models import (
    STATUS_CONFLICT,
    STATUS_FORBIDDEN,
    STATUS_FULL,
    STATUS_INVALID_PROPOSAL,
    STATUS_NOT_FOUND,
    STATUS_NOOP,
    STATUS_OK,
    STATUS_SECRET_DETECTED,
    STATUS_UNAVAILABLE,
    MemoryCandidate,
    MemoryCaptureResult,
    MemoryEntry,
    MemoryScope,
    ProposalAction,
)
from .service import MemoryService, PrivateSettings
from .writer import MemoryWriter

__all__ = ["MemoryController"]

_logger = get_logger("memory")

# 私有能力（读取、命令、自动提取）的频道口径：只有 DM（§28）。
_DM_KIND: str = "dm"

# `operation_id` 的三个来源前缀（§29.1 的形状 `<来源>:<message_id>`）。
# `remember:` 是**稳定合同**（§32.3）；另外两个是宿主自定义，本模块钉死在这里。
_COMMAND_SOURCE: str = "cmd"
_REMEMBER_SOURCE: str = "remember"
_AUTO_CAPTURE_SOURCE: str = "chat"

# 一条消息可能触发两个 mutation（`/memory off` 关读取 + 关自动；`/memory auto on` 开读取 +
# 开自动），两个键必须不同，否则第二次调用会被第一次的 `operations` 命中而静默跳过。
_SECOND_STEP_SUFFIX: str = ":second"

# `/memory auto on` 在部署未开放自动提取时的稳定状态用词（§32.3「只在部署允许时成功」）。
# 这里直接用稳定状态字符串本身：`forbidden` 的合同含义就是「访问门或 admin 判定拒绝」。
_FORBIDDEN_TOKEN: str = STATUS_FORBIDDEN

# 失败状态 → 固定文案（§36）。表里没有的状态（`ok` / `noop`）走成功分支；`duplicate` 在 Beta
# 不产生（§27.4），因此既不在表里，也不会被本模块制造出来。
_FAILURE_TEXTS: dict[str, str] = {
    STATUS_UNAVAILABLE: texts.MEMORY_UNAVAILABLE_TEXT,
    STATUS_CONFLICT: texts.MEMORY_CONFLICT_TEXT,
    STATUS_FULL: texts.MEMORY_FULL_TEXT,
    STATUS_SECRET_DETECTED: texts.MEMORY_SECRET_DETECTED_TEXT,
    STATUS_NOT_FOUND: texts.MEMORY_NOT_FOUND_TEXT,
    STATUS_INVALID_PROPOSAL: texts.MEMORY_WRITE_FAILED_TEXT,
}

# 需要 admin 的命令（§32.2 的第二组）；Controller 与 Service 两层都查（§32.3、R14）。
_ADMIN_COMMANDS: frozenset[str] = frozenset({"suggest", "candidates", "approve", "reject", "delete"})

# 不接收实参的命令：对它们来说 `argument is None` 是正常形状，只有缺参数形状的命令才回用法（§32.2）。
_NO_ARGUMENT_COMMANDS: frozenset[str] = frozenset(
    {"status", "on", "off", "auto_on", "auto_off", "list", "clear", "candidates"}
)

# 命令名 → 处理方法（D-67 钉死的十四个名字）。`_run` 在分派前完成门禁、admin 判定与用法检查。
_Handler = Callable[
    ["MemoryController", MemoryCommandRequest], Awaitable[MemoryCommandResult]
]


class MemoryController:
    """记忆命令的编排器（INTERFACES §32.3）。

    `service` / `writer` / `access` 三个依赖由装配方注入（D-67）。`auto_capture_available`
    是部署级开关 `MemoryConfig.auto_capture_available`：`/memory auto on` 只在它为真时成功
    （§32.3「只在部署允许时成功」）。D-67 钉的签名里没有这个参数，而注入的三个依赖都读不到它
    （`MemoryAccessPolicy` 只看门禁，`MemoryService` 不暴露 config），因此本模块把它作为第四个
    keyword-only 参数补上，默认 `False`（= 部署未开放，与配置默认值一致）并写进报告：
    Task 11 装配时必须传入真实取值，否则 `/memory auto on` 会永远回 `forbidden`。

    三个依赖按公开属性保存（`controller.writer` 等）：装配方与规划 §13.7 的代表性用例
    都从实例上直接读它们，而这个仓库里本来就有同样的写法（如 `comments/discovery.py`）。
    """

    def __init__(
        self,
        *,
        service: MemoryService,
        writer: MemoryWriter,
        access: MemoryAccessPolicy,
        auto_capture_available: bool = False,
    ) -> None:
        self.service = service
        self.writer = writer
        self.access = access
        self._auto_capture_available = bool(auto_capture_available)

    # --- 命令入口 ---------------------------------------------------------

    async def execute_command(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """执行一条记忆命令；任何用户可预期的失败都映射成稳定状态，不抛异常（§27.4）。"""
        try:
            result = await self._run(request)
        except asyncio.CancelledError:
            # 取消原样传播：上层靠它收尾（与 §31.1 的撰写器同款）。
            raise
        except Exception:
            # 软故障（D-60）：记忆链路上的意外不允许变成「用户什么都没收到」。
            # 异常字符串可能带路径或正文，因此只记稳定状态（§37）。
            result = self._result(STATUS_UNAVAILABLE, texts.MEMORY_UNAVAILABLE_TEXT)
        self._log_command(result)
        return result

    async def auto_capture(
        self,
        *,
        user_id: str,
        message_id: int,
        source_text: str,
    ) -> MemoryCaptureResult:
        """自动提取（§32.3 / §34.4）：高精度策略，只有**这次确实写入**才返回 `ok`。

        签名里只有用户自己写的原始正文 —— 模型回答、搜索结果、知识库片段、引用正文与图片
        描述都没有位置可放（§34.4）。本方法只做记忆侧的事：撰写、写入与结果返回；
        披露的拼接与输出空间预留由调用方负责（§34.4、D-63）。

        调用方与这里两层都要满足条件：本方法自己复查 Beta 门与用户的 `auto_capture` 设置，
        免得调用方漏掉一项，就让一个没开自动记忆的用户被静默写入（§6.1 的用户授权）。
        """
        try:
            result = await self._auto_capture(
                user_id=user_id, message_id=message_id, source_text=source_text
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            result = self._capture(STATUS_UNAVAILABLE)
        log_event(
            _logger,
            logging.INFO,
            "memory.auto_capture",
            status=result.status,
            scope=MemoryScope.USER,
            memory_id=result.memory_id,
        )
        return result

    # --- 命令实现 ---------------------------------------------------------

    async def _run(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """门禁之后的分派（顺序见模块 docstring）。"""
        if not self.access.permits_commands(request.user_id, _DM_KIND):
            # Router 已经查过一次；这是本模块按 §32.3 的独立一层，不是那次检查的副本。
            return self._result(STATUS_FORBIDDEN, texts.MEMORY_BETA_DENIED_TEXT)
        name = request.command.name
        if name == USAGE_COMMAND or name not in _HANDLERS:
            # 用法哨兵与未知命令名都只回固定用法：不写任何东西，也绝不落到普通聊天。
            return self._result(STATUS_NOOP, texts.MEMORY_USAGE_TEXT)
        if name in _ADMIN_COMMANDS and not self.access.is_admin(request.user_id):
            # 第一层 admin 判定（§32.3）；Service 的四个管理 mutation 各自还会独立复查（R14）。
            return self._result(STATUS_FORBIDDEN, _FORBIDDEN_TOKEN)
        if request.command.argument is None and name not in _NO_ARGUMENT_COMMANDS:
            # 具名命令缺参数（含 `/memory forget <非法 ID>`）：固定用法，不进 AI（§32.2）。
            return self._result(STATUS_NOOP, _usage_text(name))
        if request.command.argument is not None and name in _NO_ARGUMENT_COMMANDS:
            # 反向的形状错误（不给实参的命令却带了实参）：同样只回用法，绝不「猜着执行」。
            # 解析器已经把这种文本判成用法哨兵，这里只防手搓请求绕过前门。
            return self._result(STATUS_NOOP, texts.MEMORY_USAGE_TEXT)
        return await _HANDLERS[name](self, request)

    async def _status(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory status`：当前私有设置与条目数（数据行，见模块 docstring 的文案说明）。"""
        settings = await self.service.private_settings(request.user_id)
        entries = await self.service.private_entries(request.user_id)
        return self._result(STATUS_OK, _settings_lines(settings, len(entries)))

    async def _on(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory on`：启用私有记忆读取；成功回复带首次开启的说明（D-66）。"""
        outcome = await self.service.set_private_enabled(
            request.user_id, True, operation_id=self._operation_id(request)
        )
        return await self._from_outcome(request, outcome.status, outcome.object_id)

    async def _off(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory off`：暂停读取**并**关闭自动提取，但保留已有条目（规划 §7.1）。"""
        first = await self.service.set_private_enabled(
            request.user_id, False, operation_id=self._operation_id(request)
        )
        if first.status not in (STATUS_OK, STATUS_NOOP):
            return await self._from_outcome(request, first.status, first.object_id)
        second = await self.service.set_auto_capture(
            request.user_id, False, operation_id=self._second_operation_id(request)
        )
        if second.status not in (STATUS_OK, STATUS_NOOP):
            return await self._from_outcome(request, second.status, second.object_id)
        # 两步都到位才报成功；`ok` 与 `noop`（值本来就对）对用户是同一件事。
        return await self._from_outcome(request, STATUS_OK, None)

    async def _auto_on(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory auto on`：只在部署开放时成功，并隐含启用私有记忆读取（§32.3）。"""
        if not self._auto_capture_available:
            return self._result(STATUS_FORBIDDEN, _FORBIDDEN_TOKEN)
        first = await self.service.set_private_enabled(
            request.user_id, True, operation_id=self._operation_id(request)
        )
        if first.status not in (STATUS_OK, STATUS_NOOP):
            return await self._from_outcome(request, first.status, first.object_id)
        second = await self.service.set_auto_capture(
            request.user_id, True, operation_id=self._second_operation_id(request)
        )
        if second.status not in (STATUS_OK, STATUS_NOOP):
            return await self._from_outcome(request, second.status, second.object_id)
        return await self._from_outcome(request, STATUS_OK, None)

    async def _auto_off(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory auto off`：只关自动提取，读取保持原样（不隐含关闭读取）。"""
        outcome = await self.service.set_auto_capture(
            request.user_id, False, operation_id=self._operation_id(request)
        )
        return await self._from_outcome(request, outcome.status, outcome.object_id)

    async def _list(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory list`：无参看调用者自己的私有条目；带作用域看**已生效**共同记忆（§32.3）。

        两种形式都只展示已生效内容：候选不在这里出现，只有管理员能用 `/memory candidates` 看。
        """
        scope = request.command.scope
        if scope is None:
            entries = await self.service.private_entries(request.user_id)
        else:
            entries = await self.service.common_entries(scope)
        if not entries:
            # 没有任何可见条目：`not_found` 的合同含义就是「目标不存在」，文案也已有一条现成的。
            return self._result(STATUS_NOT_FOUND, texts.MEMORY_NOT_FOUND_TEXT)
        return self._result(STATUS_OK, "\n".join(_entry_line(entry) for entry in entries))

    async def _forget(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory forget <UM-ID>`：删除一条私有条目，保留其余条目与设置。"""
        outcome = await self.service.delete_private(
            request.user_id,
            request.command.argument or "",
            operation_id=self._operation_id(request),
        )
        return await self._from_outcome(request, outcome.status, outcome.object_id)

    async def _clear(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory clear`：删全部私有条目，保留幂等元数据，因此旧命令重放不会再次执行（D-59）。"""
        outcome = await self.service.clear_private(
            request.user_id, operation_id=self._operation_id(request)
        )
        return await self._from_outcome(request, outcome.status, outcome.object_id)

    async def _candidates(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory candidates`：只对管理员显示待批准候选（§32.3）；候选绝不进任何普通模型请求。"""
        candidates = await self.service.candidates()
        if not candidates:
            return self._result(STATUS_NOT_FOUND, texts.MEMORY_NOT_FOUND_TEXT)
        return self._result(STATUS_OK, "\n".join(_candidate_line(item) for item in candidates))

    async def _remember(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/remember <内容>`：显式私有记忆（规划 §8.1）；`operation_id` 是稳定合同。"""
        source_text = request.command.argument
        if source_text is None:
            # 解析器与 `_run` 都已拦过；这一层只防调用方手搓请求时把空正文送进撰写器。
            return self._result(STATUS_NOOP, texts.REMEMBER_USAGE_TEXT)
        operation_id = self._remember_operation_id(request)
        # 幂等**先于**撰写（§32.3）：重放命中时一次模型调用都不该发生，也不该再动文件。
        replay = await self.service.find_operation(operation_id, user_id=request.user_id)
        if replay is not None:
            # D-66 要求重放「仍是同一份说明」，因此这里要把第一次的隐式开启一并复现：
            # 那次开启写在 `cmd:<message_id>` 上，这条键在不在就是「说没说过」的判据。
            opened = (
                await self.service.find_operation(
                    self._operation_id(request), user_id=request.user_id
                )
            ) is not None
            return await self._from_outcome(request, replay.status, replay.object_id, opened=opened)
        # 读取目标快照：当前用户的私有条目与设置（规划 §8.1 的第三步）。
        existing = await self.service.private_entries(request.user_id)
        before = await self.service.private_settings(request.user_id)
        proposal_result = await self.writer.propose_private(
            source_text, existing, automatic=False
        )
        proposal = proposal_result.proposal
        if proposal_result.status != STATUS_OK or proposal is None:
            # 撰写失败（超时、非法 JSON、拒答、模型异常）：什么都没有写入（§31.2 规则 10）。
            return self._result(STATUS_INVALID_PROPOSAL, texts.MEMORY_WRITE_FAILED_TEXT)
        if proposal.action is ProposalAction.NOOP:
            # AI 自己判定「没有值得长期保存的内容」（§31.1 的 noop 提案）。
            return self._result(STATUS_NOOP, texts.MEMORY_WRITE_FAILED_TEXT)
        outcome = await self.service.apply_private_proposal(
            request.user_id, proposal, operation_id=operation_id
        )
        if outcome.status != STATUS_OK:
            return await self._from_outcome(request, outcome.status, outcome.object_id)
        # 保存成功即隐式打开读取（§32.3）；说明挂在这次成功回复上，不记录「已经说过」（D-66）。
        opened = False
        if not before.private_enabled:
            enabled = await self.service.set_private_enabled(
                request.user_id, True, operation_id=self._operation_id(request)
            )
            opened = enabled.status == STATUS_OK
        return await self._from_outcome(request, STATUS_OK, outcome.object_id, opened=opened)

    async def _suggest(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory suggest <scope> <内容>`：AI 只撰写候选，作用域由解析固定（规划 §8.2、D-58）。"""
        scope = request.command.scope
        content = request.command.argument
        if scope is None or content is None:
            # 与 `_remember` 同样的兜底：手搓请求也不允许绕过形状检查。
            return self._result(STATUS_NOOP, texts.MEMORY_USAGE_TEXT)
        operation_id = self._operation_id(request)
        # 候选文件是共同的：这里传 `user_id=None`，只查共同快照（§30.1 的查询口径）。
        replay = await self.service.find_operation(operation_id)
        if replay is not None:
            return await self._from_outcome(request, replay.status, replay.object_id)
        existing = await self.service.common_entries(scope)
        proposal_result = await self.writer.propose_common(content, existing)
        proposal = proposal_result.proposal
        if proposal_result.status != STATUS_OK or proposal is None:
            return self._result(STATUS_INVALID_PROPOSAL, texts.MEMORY_WRITE_FAILED_TEXT)
        if proposal.action is ProposalAction.NOOP:
            return self._result(STATUS_NOOP, texts.MEMORY_WRITE_FAILED_TEXT)
        outcome = await self.service.add_common_candidate(
            scope,
            proposal,
            operation_id=operation_id,
            access=self.access,
            actor_id=request.user_id,
        )
        return await self._from_outcome(request, outcome.status, outcome.object_id)

    async def _approve(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory approve <MC-ID>`：**不调用 AI**，把候选原子移入生效区（规划 §8.2、D-58）。"""
        outcome = await self.service.approve_candidate(
            request.command.argument or "",
            operation_id=self._operation_id(request),
            access=self.access,
            actor_id=request.user_id,
        )
        return await self._from_outcome(request, outcome.status, outcome.object_id)

    async def _reject(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory reject <MC-ID>`：丢弃候选，不动任何已生效条目。"""
        outcome = await self.service.reject_candidate(
            request.command.argument or "",
            operation_id=self._operation_id(request),
            access=self.access,
            actor_id=request.user_id,
        )
        return await self._from_outcome(request, outcome.status, outcome.object_id)

    async def _delete(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory delete <GM-A-ID | GM-L-ID>`：删除一条已生效共同记忆。"""
        outcome = await self.service.delete_common(
            request.command.argument or "",
            operation_id=self._operation_id(request),
            access=self.access,
            actor_id=request.user_id,
        )
        return await self._from_outcome(request, outcome.status, outcome.object_id)

    # --- 自动提取 ---------------------------------------------------------

    async def _auto_capture(
        self, *, user_id: str, message_id: int, source_text: str
    ) -> MemoryCaptureResult:
        """一次自动提取：门禁 → 幂等 → 快照 → 撰写 → 宿主校验 → 原子写入。"""
        if not self.access.permits_private(user_id, _DM_KIND):
            return self._capture(STATUS_FORBIDDEN)
        settings = await self.service.private_settings(user_id)
        if not settings.auto_capture:
            # 用户没有开启自动记忆：调用方漏判也不能替用户决定（§6.1 的用户授权）。
            return self._capture(STATUS_NOOP)
        operation_id = f"{_AUTO_CAPTURE_SOURCE}:{message_id}"
        # 与 `/remember` 同样的理由：重放必须先查 operations，不能让重放再烧一次模型调用。
        replay = await self.service.find_operation(operation_id, user_id=user_id)
        if replay is not None:
            # 这一次没有写入任何东西，因此不是 `ok`（§27.2「只有确实写入成功才是 ok」）；
            # 调用方也不会为它追加一条重复的写入披露（§34.4）。
            return self._capture(STATUS_NOOP)
        existing = await self.service.private_entries(user_id)
        proposal_result = await self.writer.propose_private(source_text, existing, automatic=True)
        proposal = proposal_result.proposal
        if proposal_result.status != STATUS_OK or proposal is None:
            return self._capture(STATUS_INVALID_PROPOSAL)
        if proposal.action is ProposalAction.NOOP:
            # 低置信度与「不值得保存」都在撰写器里落成 noop（§31.1）：自动提取静默跳过。
            return self._capture(STATUS_NOOP)
        outcome = await self.service.apply_private_proposal(
            user_id, proposal, operation_id=operation_id
        )
        if outcome.status != STATUS_OK or outcome.object_id is None:
            return self._capture(outcome.status)
        return MemoryCaptureResult(
            status=STATUS_OK,
            memory_id=outcome.object_id,
            content=proposal.content,
            action=proposal.action,
        )

    # --- 回复与日志 -------------------------------------------------------

    async def _from_outcome(
        self,
        request: MemoryCommandRequest,
        status: str,
        memory_id: str | None,
        *,
        opened: bool = False,
    ) -> MemoryCommandResult:
        """把稳定状态与对象 ID 渲染成一次结果；重放走同一条路径，因此回复与首次逐字相同。"""
        text = _FAILURE_TEXTS.get(status)
        if text is None and status == STATUS_FORBIDDEN:
            # 本模块自己的门禁之外，Service 也会回 `forbidden`；两者对用户是同一件事。
            text = _FORBIDDEN_TOKEN
        if text is None:
            text = await self._success_text(request, status, memory_id, opened=opened)
        return self._result(status, text, memory_id)

    async def _success_text(
        self,
        request: MemoryCommandRequest,
        status: str,
        memory_id: str | None,
        *,
        opened: bool = False,
    ) -> str:
        """成功（`ok` / `noop`）时的回复：`texts.py` 的组合文案，或只有数据与稳定 token 的行。"""
        name = request.command.name
        if name == "remember":
            return await self._remember_text(request, status, memory_id, opened=opened)
        if name in ("on", "auto_on"):
            # D-66：开启动作的成功回复必须带上那段简明说明（不加任何持久标记）。
            return texts.MEMORY_FIRST_ENABLE_TEXT
        if name == "off":
            return "private_enabled=off\nauto_capture=off"
        if name == "auto_off":
            return "auto_capture=off"
        if name == "suggest":
            candidate = await self._candidate(memory_id)
            return _candidate_line(candidate) if candidate is not None else status
        if name == "approve":
            entry = await self._common_entry(memory_id)
            return _entry_line(entry) if entry is not None else status
        if name in ("forget", "reject") and memory_id is not None:
            # 目标已经不在了（删除 / 丢弃）：只报「对谁做了什么」，不编造正文。
            return f"{memory_id} {status}"
        # `clear` 与其它没有对象可展示的成功：只回稳定状态 token。
        return status

    async def _remember_text(
        self,
        request: MemoryCommandRequest,
        status: str,
        memory_id: str | None,
        *,
        opened: bool,
    ) -> str:
        """`/remember` 的成功回复：展示实际保存的正文与条目 ID（设计 §6.3、规划 §8.1）。

        重放时 `operations` 只存了 `{status, object_id, revision}`，没有正文也没有动作，
        因此正文从当前快照里取（条目还在时，那正是用户此刻能看到的正文），
        「新增还是更新」用 `created_at == updated_at` 推断 —— 更新会改写 `updated_at`，
        相等即这条记忆创建之后没有再被改动过；不相等或条目已不在时按更保守的一侧处理。
        """
        entry = await self._private_entry(request.user_id, memory_id)
        if entry is None:
            return status
        text = texts.memory_saved_text(
            memory_id=entry.memory_id,
            content=entry.content,
            created=entry.created_at == entry.updated_at,
        )
        if opened:
            text = text + "\n" + texts.MEMORY_FIRST_ENABLE_TEXT
        return text

    async def _private_entry(self, user_id: str, memory_id: str | None) -> MemoryEntry | None:
        if memory_id is None:
            return None
        for entry in await self.service.private_entries(user_id):
            if entry.memory_id == memory_id:
                return entry
        return None

    async def _common_entry(self, memory_id: str | None) -> MemoryEntry | None:
        """按 ID 前缀定位作用域；前缀不认识或条目已不在时返回 None（调用方回稳定 token）。"""
        if memory_id is None:
            return None
        if memory_id.startswith(PREFIX_ALL_USER):
            scope = MemoryScope.ALL_USER
        elif memory_id.startswith(PREFIX_LOBBY):
            scope = MemoryScope.LOBBY
        else:
            return None
        for entry in await self.service.common_entries(scope):
            if entry.memory_id == memory_id:
                return entry
        return None

    async def _candidate(self, candidate_id: str | None) -> MemoryCandidate | None:
        if candidate_id is None or not candidate_id.startswith(PREFIX_CANDIDATE):
            return None
        for item in await self.service.candidates():
            if item.candidate_id == candidate_id:
                return item
        return None

    def _operation_id(self, request: MemoryCommandRequest) -> str:
        """命令级幂等键：`cmd:<message_id>`（§29.1 的形状，具体取值由宿主决定）。"""
        return f"{_COMMAND_SOURCE}:{request.message_id}"

    def _second_operation_id(self, request: MemoryCommandRequest) -> str:
        """一条消息触发两个 mutation 时的第二个键（见 `_SECOND_STEP_SUFFIX` 的注释）。"""
        return f"{_COMMAND_SOURCE}:{request.message_id}{_SECOND_STEP_SUFFIX}"

    def _remember_operation_id(self, request: MemoryCommandRequest) -> str:
        """显式 `/remember` 的幂等键是稳定合同：`remember:<message_id>`（§32.3）。"""
        return f"{_REMEMBER_SOURCE}:{request.message_id}"

    def _result(
        self, status: str, text: str, memory_id: str | None = None
    ) -> MemoryCommandResult:
        return MemoryCommandResult(status=status, text=text, memory_id=memory_id)

    def _capture(self, status: str) -> MemoryCaptureResult:
        """未写入时的自动提取结果（§27.2）：正文为空串，动作取 `NOOP`。"""
        return MemoryCaptureResult(
            status=status, memory_id=None, content="", action=ProposalAction.NOOP
        )

    def _log_command(self, result: MemoryCommandResult) -> None:
        """日志只记稳定状态与对象 ID（§37）：不记正文、key、命令参数或用户 ID。"""
        fields: dict[str, object] = {"status": result.status}
        if result.memory_id is not None:
            if result.memory_id.startswith(PREFIX_CANDIDATE):
                fields["candidate_id"] = result.memory_id
            else:
                fields["memory_id"] = result.memory_id
        log_event(_logger, logging.INFO, "memory.command", **fields)


def _usage_text(name: str) -> str:
    """缺参数与非法 ID 的固定用法文案（§32.2）：`/remember` 与 `/memory` 各一条。"""
    return texts.REMEMBER_USAGE_TEXT if name == "remember" else texts.MEMORY_USAGE_TEXT


def _settings_lines(settings: PrivateSettings, entry_count: int) -> str:
    """`/memory status` 的数据行：字段名与 `PrivateSettings` 一致，只有数据、没有文案。"""
    return (
        f"private_enabled={'on' if settings.private_enabled else 'off'}\n"
        f"auto_capture={'on' if settings.auto_capture else 'off'}\n"
        f"private_entries={entry_count}"
    )


def _entry_line(entry: MemoryEntry) -> str:
    """条目行：`[<ID>] <正文>`，与撰写器渲染既有记忆的形状一致（§31.1）。"""
    return f"[{entry.memory_id}] {entry.content}"


def _candidate_line(candidate: MemoryCandidate) -> str:
    """候选行：`[<候选 ID>] <作用域> <动作> <目标|-> <正文>`（供管理员审阅）。"""
    target = candidate.target_id if candidate.target_id is not None else "-"
    scope = candidate.scope
    return f"[{candidate.candidate_id}] {scope} {candidate.action} {target} {candidate.content}"


# 命令名 → 处理方法。放在类之后定义，`_run` 运行期读它，因此顺序无关。
_HANDLERS: dict[str, _Handler] = {
    "status": MemoryController._status,
    "on": MemoryController._on,
    "off": MemoryController._off,
    "auto_on": MemoryController._auto_on,
    "auto_off": MemoryController._auto_off,
    "list": MemoryController._list,
    "forget": MemoryController._forget,
    "clear": MemoryController._clear,
    "candidates": MemoryController._candidates,
    "remember": MemoryController._remember,
    "suggest": MemoryController._suggest,
    "approve": MemoryController._approve,
    "reject": MemoryController._reject,
    "delete": MemoryController._delete,
}
