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
`texts.py`（§36）：本模块不新造任何中文，也不把稳定状态 token 或 `字段=取值` 当回复发出去。
`texts.py` 的组合函数负责把数据拼进句子（`memory_status_text`、`memory_entry_list_text`、
`memory_candidate_line` 等），本模块只把快照里的普通值交给它们；没有对象可展示时退回
`MEMORY_TARGET_GONE_TEXT`。这条不变量由 `tests/test_memory_controller.py` 的源码级测试守住
（非 docstring 的字符串字面量里不许出现中文）。

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
    OperationResult,
    ProposalAction,
)
from .service import MemoryService
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

# 失败状态 → 固定文案（§36）。成功只有 `ok` 与 `noop` 两种，其余任何状态（含 Beta 不产生的
# `duplicate`）都不会落进成功分支，见 `_from_outcome` 的防御分支。`forbidden` 是表外的例外：
# 它的文案取决于命令是管理命令还是普通命令，见 `_service_forbidden_text`。
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
    （§32.3「只在部署允许时成功」），自动提取也只在它为真时运行（§34.4 把它列为必须条件）。
    D-67 钉的签名里没有这个参数，而注入的三个依赖都读不到它
    （`MemoryAccessPolicy` 只看门禁，`MemoryService` 不暴露 config），因此本模块把它作为第四个
    keyword-only 参数补上，默认 `False`（= 部署未开放，与配置默认值一致）并写进报告：
    Task 11 装配时必须传入真实取值，否则 `/memory auto on` 与自动提取都会被永远拒绝。

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
            # 文案是**权限**拒绝，与接入门的拒绝（MEMORY_BETA_DENIED_TEXT）不是同一件事。
            return self._result(STATUS_FORBIDDEN, texts.MEMORY_ADMIN_REQUIRED_TEXT)
        if request.command.argument is None and name not in _NO_ARGUMENT_COMMANDS:
            # 具名命令缺参数（含 `/memory forget <非法 ID>`）：固定用法，不进 AI（§32.2）。
            return self._result(STATUS_NOOP, _usage_text(name))
        if request.command.argument is not None and name in _NO_ARGUMENT_COMMANDS:
            # 反向的形状错误（不给实参的命令却带了实参）：同样只回用法，绝不「猜着执行」。
            # 解析器已经把这种文本判成用法哨兵，这里只防手搓请求绕过前门。
            return self._result(STATUS_NOOP, texts.MEMORY_USAGE_TEXT)
        return await _HANDLERS[name](self, request)

    async def _status(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory status`：私有记忆与自动记忆的开关、私有条目数（§32.2）。"""
        settings = await self.service.private_settings(request.user_id)
        entries = await self.service.private_entries(request.user_id)
        return self._result(
            STATUS_OK,
            texts.memory_status_text(
                private_enabled=settings.private_enabled,
                auto_capture=settings.auto_capture,
                entry_count=len(entries),
            ),
        )

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
        failure = await self._step_failure(request, first)
        if failure is not None:
            return failure
        second = await self.service.set_auto_capture(
            request.user_id, False, operation_id=self._second_operation_id(request)
        )
        failure = await self._step_failure(request, second)
        if failure is not None:
            return failure
        # 两步都到位才报成功；`ok` 与 `noop`（值本来就对）对用户是同一件事。
        return await self._from_outcome(request, STATUS_OK, None)

    async def _auto_on(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory auto on`：只在部署开放时成功，并隐含启用私有记忆读取（§32.3）。"""
        if not self._auto_capture_available:
            # 部署级开关拒绝：与「你不是管理员」是两回事，因此文案也不同（§36）。
            return self._result(STATUS_FORBIDDEN, texts.MEMORY_AUTO_UNAVAILABLE_TEXT)
        first = await self.service.set_private_enabled(
            request.user_id, True, operation_id=self._operation_id(request)
        )
        failure = await self._step_failure(request, first)
        if failure is not None:
            return failure
        second = await self.service.set_auto_capture(
            request.user_id, True, operation_id=self._second_operation_id(request)
        )
        failure = await self._step_failure(request, second)
        if failure is not None:
            return failure
        return await self._from_outcome(request, STATUS_OK, None)

    async def _step_failure(
        self, request: MemoryCommandRequest, outcome: OperationResult
    ) -> MemoryCommandResult | None:
        """两步命令（`off` / `auto on`）的中间判定：这一步没到位就渲染成失败回复，否则 None。

        `ok` 与 `noop`（值本来就对）都表示这一步已经到位，对用户是同一件事；两条路径共用同一个
        判定，避免各自漂移。这里不碰撰写器，因此 §4.5 的顺序不受影响。
        """
        if outcome.status in (STATUS_OK, STATUS_NOOP):
            return None
        return await self._from_outcome(request, outcome.status, outcome.object_id)

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
        lines = tuple(
            texts.memory_entry_line(memory_id=entry.memory_id, content=entry.content)
            for entry in entries
        )
        # 空列举是**成功**：用户没有点名任何目标，问题（当前有哪些条目）也已经回答，空态由
        # 文案自己说清（§36）。回 `not_found` 会让 `_log_command` 把一次日常查看记成失败，
        # 污染日志派生的健康信号 —— §27.4 的 `not_found` 只指「目标条目或候选不存在」。
        return self._result(
            STATUS_OK,
            texts.memory_entry_list_text(
                scope=None if scope is None else scope.value, lines=lines
            ),
        )

    async def _forget(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory forget <UM-ID>`：删除一条私有条目，保留其余条目与设置。"""
        outcome = await self.service.delete_private(
            request.user_id,
            request.command.argument or "",
            operation_id=self._operation_id(request),
        )
        return await self._from_outcome(request, outcome.status, outcome.object_id)

    async def _clear(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory clear`：删全部私有条目，保留幂等元数据，因此旧命令重放不会再次执行（D-59）。

        删掉的条数在清理**之前**数出来并交给文案：`clear_private` 只回状态与修订号，
        而重放时条目早就不在了（重放那一次确实一条都没删，文案如实报 0）。
        """
        removed = len(await self.service.private_entries(request.user_id))
        outcome = await self.service.clear_private(
            request.user_id, operation_id=self._operation_id(request)
        )
        return await self._from_outcome(
            request, outcome.status, outcome.object_id, removed=removed
        )

    async def _candidates(self, request: MemoryCommandRequest) -> MemoryCommandResult:
        """`/memory candidates`：只对管理员显示待批准候选（§32.3）；候选绝不进任何普通模型请求。"""
        candidates = await self.service.candidates()
        lines = tuple(
            texts.memory_candidate_line(
                candidate_id=item.candidate_id,
                scope=item.scope.value,
                action=item.action.value,
                target_id=item.target_id,
                content=item.content,
            )
            for item in candidates
        )
        # 没有候选同样是成功：空态由文案自己说清（§36），不借 `not_found` —— 那个状态只指
        # 「目标条目或候选不存在」（§27.4），而这里用户没有点名任何目标。
        return self._result(STATUS_OK, texts.memory_candidate_list_text(lines=lines))

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
        """一次自动提取：门禁 → 部署开关 → 幂等 → 快照 → 撰写 → 宿主校验 → 原子写入。"""
        if not self.access.permits_private(user_id, _DM_KIND):
            return self._capture(STATUS_FORBIDDEN)
        if not self._auto_capture_available:
            # §34.4 把部署开关与接入门并列为**必须**条件，这一步不能省：用户文件里的
            # `auto_capture` 可能是旧值或被人手改过，只信它就等于运维关不掉自动写入。
            # 状态取 `forbidden`（§27.4「访问门……拒绝」）：这是部署策略的拒绝，不是出错，
            # 也不是 `noop`（那读起来像「没什么要做的」，会把拒绝藏起来）；未写入，无披露。
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
        removed: int | None = None,
    ) -> MemoryCommandResult:
        """把稳定状态与对象 ID 渲染成一次结果；重放走同一条路径，因此回复取自同一批文案。

        `removed` 只给 `/memory clear` 用（清理前的条数），其余命令不传。
        """
        text = _FAILURE_TEXTS.get(status)
        if text is None and status == STATUS_FORBIDDEN:
            # 本模块自己的门禁之外，Service 也会回 `forbidden`（admin 复查或能力未启用）。
            text = _service_forbidden_text(request)
        if text is not None:
            return self._result(status, text, memory_id)
        if status not in (STATUS_OK, STATUS_NOOP):
            # 防御分支：成功只有 `ok` 与 `noop` 两种。`duplicate` 在 Beta 不产生（§27.4），
            # 但将来多出一个未知状态时也不许落进成功文案 —— 那会让用户看到「成功」形状的确认，
            # 却配上对不上的对象 ID。按不可用处理：状态与文案成对，且不假装认识这个状态。
            return self._result(STATUS_UNAVAILABLE, texts.MEMORY_UNAVAILABLE_TEXT)
        text = await self._success_text(request, memory_id, opened=opened, removed=removed)
        return self._result(status, text, memory_id)

    async def _success_text(
        self,
        request: MemoryCommandRequest,
        memory_id: str | None,
        *,
        opened: bool = False,
        removed: int | None = None,
    ) -> str:
        """成功（`ok` / `noop`）时的回复：一律由 `texts.py` 的组合文案拼成（§36）。

        本模块只把快照里的普通值交给 `texts`，不在任何分支里自己写中文；文案不区分 `ok` 与
        `noop`（值本来就对时用户看到的也是同一句确认）。
        """
        name = request.command.name
        if name == "remember":
            return await self._remember_text(request, memory_id, opened=opened)
        if name == "on":
            # D-66：说明已经随常量拼在开启确认之后，不在这里内联，也不加任何持久标记。
            return texts.MEMORY_ON_DONE_TEXT
        if name == "auto_on":
            return texts.MEMORY_AUTO_ON_DONE_TEXT
        if name == "off":
            return texts.MEMORY_OFF_DONE_TEXT
        if name == "auto_off":
            return texts.MEMORY_AUTO_OFF_DONE_TEXT
        if name == "forget":
            if memory_id is None:
                return texts.MEMORY_TARGET_GONE_TEXT
            return texts.memory_forgotten_text(memory_id=memory_id)
        if name == "clear":
            # `removed` 是清理前数出来的条数；防御分支（调用方没传）按 0 处理。
            return texts.memory_cleared_text(removed=removed if removed is not None else 0)
        if name == "suggest":
            candidate = await self._candidate(memory_id)
            if candidate is None:
                # 重放时候选可能已经被批准或拒绝：只报事实，不编造正文。
                return texts.MEMORY_TARGET_GONE_TEXT
            return texts.memory_candidate_created_text(
                candidate_id=candidate.candidate_id,
                scope=candidate.scope.value,
                action=candidate.action.value,
                target_id=candidate.target_id,
                content=candidate.content,
            )
        if name == "approve":
            entry = await self._common_entry(memory_id)
            if entry is None:
                return texts.MEMORY_TARGET_GONE_TEXT
            return texts.memory_approved_text(memory_id=entry.memory_id, content=entry.content)
        if name == "reject":
            if memory_id is None:
                return texts.MEMORY_TARGET_GONE_TEXT
            return texts.memory_candidate_rejected_text(candidate_id=memory_id)
        if name == "delete":
            if memory_id is None:
                return texts.MEMORY_TARGET_GONE_TEXT
            return texts.memory_deleted_text(memory_id=memory_id)
        # `status` / `list` / `candidates` 自己渲染结果，不走这里；真正会落到这一行的只有
        # 「成功但没有对象可展示」的防御分支，按目标已不可见处理，绝不回裸状态 token。
        return texts.MEMORY_TARGET_GONE_TEXT

    async def _remember_text(
        self,
        request: MemoryCommandRequest,
        memory_id: str | None,
        *,
        opened: bool,
    ) -> str:
        """`/remember` 的成功回复：展示实际保存的正文与条目 ID（设计 §6.3、规划 §8.1）。

        重放时 `operations` 只存了 `{status, object_id, revision}`，没有正文也没有动作，
        因此正文从当前快照里取（条目还在时，那正是用户此刻能看到的正文），
        「新增还是更新」用 `created_at == updated_at` 推断 —— 更新会改写 `updated_at`，
        相等即这条记忆创建之后没有再被改动过；条目已经不在时按「目标已不可见」处理。
        首次开启的说明由 `memory_saved_text(opened=...)` 拼接（D-66），不在这里手工拼串。
        """
        entry = await self._private_entry(request.user_id, memory_id)
        if entry is None:
            return texts.MEMORY_TARGET_GONE_TEXT
        return texts.memory_saved_text(
            memory_id=entry.memory_id,
            content=entry.content,
            created=entry.created_at == entry.updated_at,
            opened=opened,
        )

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


def _service_forbidden_text(request: MemoryCommandRequest) -> str:
    """Service 层回 `forbidden` 时的用户文案（§36）。

    两种来源的回绝不是同一件事：管理命令走 admin 复查（权限拒绝），其余命令走能力/接入门
    （`_mutate_private` 在部署关闭或调用方给的 user_id 不可用时回 `forbidden`），因此文案也不同。
    """
    if request.command.name in _ADMIN_COMMANDS:
        return texts.MEMORY_ADMIN_REQUIRED_TEXT
    return texts.MEMORY_BETA_DENIED_TEXT


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
