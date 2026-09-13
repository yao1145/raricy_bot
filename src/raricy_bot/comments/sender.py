"""博客评论发送链。

发送顺序固定为：输出脱敏和截断、评论配额预留、POST、结果不确定时按机器人作者
与精确父评论对账、最多一次重发。评论没有幂等键，因此宁可丢一句，也不在无法
判断时盲目重复发送。
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..config import CommentConfig
from ..logging_setup import get_logger, log_event
from ..redact import Redactor
from ..site.client import SiteClient, SiteError
from ..site.comment_models import CommentNode
from ..text_utils import truncate_at_paragraph
from .router import CommentRequest

logger = get_logger("comments.sender")
_LOGGER = logger
_FORBIDDEN_BACKOFF_SECONDS = 300.0


@dataclass(frozen=True)
class CommentSendResult:
    """一次评论发送结果。"""

    delivered: bool
    comment_id: str | None
    reason: str
    # 仅在已确认发布或远端对账命中时返回实际脱敏/截断正文；失败路径为 None。
    content: str | None = None
    # 远端可能已经成功，但本地 sent/event 记录仍需恢复时置 True。
    recoverable: bool = False

    @property
    def bot_comment_id(self) -> str | None:
        """兼容调用方使用的名称。"""
        return self.comment_id

    @property
    def message_id(self) -> str | None:
        """兼容聊天发送结果的字段名称。"""
        return self.comment_id

    @property
    def sent_text(self) -> str | None:
        """返回供上下文提交的实际出站正文。"""
        return self.content

    @property
    def published_text(self) -> str | None:
        """兼容服务层字段名；失败结果不会携带未发布正文。"""
        return self.content

    @property
    def published_content(self) -> str | None:
        """兼容窄发送适配器字段名。"""
        return self.content


SendResult = CommentSendResult


class CommentSender:
    """安全发布评论回复的窄适配层。"""

    def __init__(
        self,
        *,
        client: SiteClient,
        store: Any,
        quota: Any,
        redactor: Redactor,
        cfg: CommentConfig,
        self_user_id: str | None = None,
        logger_instance: logging.Logger | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._quota = quota
        self._redactor = redactor
        self._cfg = cfg
        self._self_user_id = self_user_id
        self._logger = (
            logger_instance
            if logger_instance is not None
            else (logger if logger is not None else _LOGGER)
        )

    async def send(
        self,
        request: CommentRequest | str,
        text: str,
        parent_id: str | None = None,
        *,
        kind: str = "reply",
        blog_id: str | None = None,
        trigger_comment_id: str | None = None,
        conversation_id: str | None = None,
        reservation_token: str | None = None,
    ) -> CommentSendResult:
        """发送一条评论。

        首选调用形状是 ``send(request, text)``。为方便窄接口测试，也接受
        ``send(blog_id, text, parent_id=..., trigger_comment_id=...)``；后者不保存
        会话正文，只使用传入的评论 id 完成发送和对账。
        """
        target = self._coerce_request(
            request,
            parent_id=parent_id,
            blog_id=blog_id,
            trigger_comment_id=trigger_comment_id,
            conversation_id=conversation_id,
        )
        if target is None:
            return CommentSendResult(False, None, "failed")

        # 发送前先查本地映射；重复调度直接去重，不能先占 quota 或触发一次 POST。
        # reservation_token 由上层 Service 预留时传入；该调用方仍拥有令牌的
        # 最终化责任，因此 Sender 只在自备 reserve 的路径调用 note/release。
        externally_reserved = reservation_token is not None

        local, lookup_failed = await self._find_local_sent_status(target.comment_id)
        if local is not None:
            # 本地只保存评论 id，不保存正文。这里的 text 尚未发布，不能把它
            # 冒充实际出站内容交给上下文；远端对账命中时才从评论节点取正文。
            result = CommentSendResult(True, local, "deduped", None)
            self._log(result, target.blog_id, kind)
            return result
        if lookup_failed:
            # 无法确认本地去重表时宁可等待恢复，也不要冒险外送重复评论。
            result = CommentSendResult(False, None, "recover", recoverable=True)
            self._log(result, target.blog_id, kind)
            return result

        content = self._prepare_content(text)
        if not content.strip():
            return CommentSendResult(False, None, "failed")

        if reservation_token is None:
            reservation = await self._reserve(target.blog_id, kind, target.comment_id)
            if not self._is_allowed(reservation):
                result = CommentSendResult(False, None, self._deny_reason(reservation))
                self._log(result, target.blog_id, kind)
                return result
            reservation_token = self._reservation_token(reservation)
        try:
            result, charged = await self._deliver(
                target, content, kind, reservation_token=reservation_token
            )
        except BaseException:
            if not externally_reserved:
                await self._release(
                    target.blog_id,
                    kind,
                    target.comment_id,
                    reservation_token=reservation_token,
                )
            raise

        if charged and not externally_reserved:
            await self._note_sent(
                target.blog_id,
                target.comment_id,
                kind,
                reservation_token=reservation_token,
            )
        elif not charged and not externally_reserved:
            await self._release(
                target.blog_id, kind, target.comment_id, reservation_token=reservation_token
            )
        self._log(result, target.blog_id, kind)
        return result

    def _prepare_content(self, text: str) -> str:
        """生成唯一的实际出站正文：先脱敏，再按自然段截断。"""
        return truncate_at_paragraph(
            self._redactor.redact(text), self._cfg.max_output_chars
        )[0]

    def _coerce_request(
        self,
        request: CommentRequest | str,
        *,
        parent_id: str | None,
        blog_id: str | None,
        trigger_comment_id: str | None,
        conversation_id: str | None,
    ) -> CommentRequest | None:
        if isinstance(request, CommentRequest):
            return request
        if not isinstance(request, str) or not blog_id or not parent_id:
            return None
        comment_id = trigger_comment_id or parent_id
        return CommentRequest(
            comment_id=comment_id,
            blog_id=blog_id,
            parent_id=None,
            conversation_id=conversation_id or comment_id,
            session_key=f"comment:{conversation_id or comment_id}",
            generation=0,
            username="",
            user_text="",
            parent_bot_text=None,
            has_image=False,
            has_quoted_blog=False,
        )

    async def _deliver(
        self,
        request: CommentRequest,
        content: str,
        kind: str,
        *,
        reservation_token: str | None = None,
    ) -> tuple[CommentSendResult, bool]:
        try:
            posted = await self._post_once(request, content)
        except SiteError as exc:
            return await self._on_error(
                request, content, kind, exc, reservation_token=reservation_token
            )

        if posted is None:
            # code=200 但没有返回评论对象时不能把事件标为 done；先按不确定
            # 结果走远端对账。HTTP 已明确成功但缺少评论 UUID 时不再盲目重发，
            # 对账未命中就保留 recover，避免下一进程看不到去重键而制造两条回复。
            return await self._reconcile_and_retry(
                request,
                content,
                kind,
                allow_retry=False,
                reservation_token=reservation_token,
            )

        recorded = await self._record_sent(
            request, posted, kind, reservation_token=reservation_token
        )
        return (
            CommentSendResult(
                True,
                posted.id,
                "delivered",
                content,
                recoverable=not recorded,
            ),
            True,
        )

    async def _post_once(self, request: CommentRequest, content: str) -> CommentNode | None:
        """调用评论窄接口；只传正文和触发评论 UUID。"""
        return await self._client.post_comment(
            request.blog_id,
            content,
            parent_id=request.comment_id,
        )

    async def _on_error(
        self,
        request: CommentRequest,
        content: str,
        kind: str,
        exc: SiteError,
        *,
        reservation_token: str | None = None,
    ) -> tuple[CommentSendResult, bool]:
        if exc.status == 429:
            await self._backoff(exc)
            return CommentSendResult(False, None, "backoff"), False
        if exc.status == 400:
            # parent_id 失效与普通参数错误都不可重发；重新读取评论树只用于
            # 让上层/人工诊断知道父评论是否仍存在，绝不因树里有父评论而盲发。
            await self._refresh_parent_tree(request.blog_id)
            return CommentSendResult(False, None, "reply_target_gone"), False
        if exc.status == 404:
            return CommentSendResult(False, None, "reply_target_gone"), False
        if exc.status == 403:
            reason = "csrf" if "跨源" in exc.message or "CSRF" in exc.message else "forbidden"
            if reason == "forbidden":
                # 权限不足或禁言固定等待五分钟；一小时默认值仅属于 429
                # 未携带 Retry-After 的服务器限流场景。
                await self._backoff(exc, default_seconds=_FORBIDDEN_BACKOFF_SECONDS)
            return CommentSendResult(False, None, reason), False
        if exc.status != 0:
            return CommentSendResult(False, None, "failed"), False

        # 网络/超时结果不确定：先本地，再远端精确对账；查不到才允许一次重发。
        return await self._reconcile_and_retry(
            request, content, kind, reservation_token=reservation_token
        )

    async def _refresh_parent_tree(self, blog_id: str) -> None:
        """对 parent_id 的确定失败做一次只读确认，不返回树中正文。"""
        fetch = getattr(self._client, "fetch_blog_comments", None)
        if fetch is None:
            return
        try:
            value = fetch(blog_id)
            if hasattr(value, "__await__"):
                await value
        except Exception:
            # 400 已经是确定失败；确认请求本身失败也不能改变静默终态。
            return

    async def _reconcile_and_retry(
        self,
        request: CommentRequest,
        content: str,
        kind: str,
        *,
        allow_retry: bool = True,
        reservation_token: str | None = None,
    ) -> tuple[CommentSendResult, bool]:
        local = await self._find_local_sent(request.comment_id)
        if local is not None:
            # 既有记录已经在先前投递时计费，本次预留不能再次 note_sent；
            # Store 只保存评论 id，不保存正文，因此本次未发布的 content
            # 不能冒充实际出站正文。
            return CommentSendResult(True, local, "deduped", None), False

        try:
            tree = await self._client.fetch_blog_comments(request.blog_id)
        except SiteError as exc:
            if exc.status == 429:
                await self._backoff(exc)
                return CommentSendResult(False, None, "backoff", recoverable=True), False
            # 对账查询自身失败时不能证明远端状态，宁可少发也不双发。
            return CommentSendResult(False, None, "recover", recoverable=True), False
        except Exception:
            # 对账自身失败时不能证明远端状态，宁可少发也不双发。
            return CommentSendResult(False, None, "recover", recoverable=True), False

        try:
            matches = self._find_remote_matches(tree, request.comment_id)
        except Exception:
            # 公开树结构异常时不能确认是否已存在机器人回复。
            return CommentSendResult(False, None, "recover", recoverable=True), False
        if matches:
            chosen = matches[0]
            recorded = await self._record_sent(
                request, chosen, kind, reservation_token=reservation_token
            )
            matched_content = (
                truncate_at_paragraph(
                    self._redactor.redact(chosen.content), self._cfg.max_output_chars
                )[0]
                if isinstance(chosen.content, str)
                else content
            )
            return (
                CommentSendResult(
                    True,
                    chosen.id,
                    "deduped",
                    matched_content,
                    recoverable=not recorded,
                ),
                True,
            )

        if not allow_retry:
            return CommentSendResult(False, None, "recover", recoverable=True), False

        # 精确一次重发。重发的错误分支要保留 429 退避语义。
        try:
            posted = await self._post_once(request, content)
        except SiteError as exc:
            if exc.status == 429:
                await self._backoff(exc)
            return (
                CommentSendResult(
                    False,
                    None,
                    "backoff" if exc.status == 429 else "recover",
                    recoverable=True,
                ),
                False,
            )
        if posted is None:
            return CommentSendResult(False, None, "recover", recoverable=True), False
        recorded = await self._record_sent(
            request, posted, kind, reservation_token=reservation_token
        )
        return (
            CommentSendResult(
                True,
                posted.id,
                "delivered",
                content,
                recoverable=not recorded,
            ),
            True,
        )

    def _find_remote_matches(
        self, tree: Iterable[CommentNode], parent_id: str
    ) -> list[CommentNode]:
        """展平评论树，只接受 self author 且直接 parent_id 精确相等的节点。"""
        self_id = self._self_user_id
        if self_id is None:
            user = getattr(self._client, "self_user", None)
            self_id = getattr(user, "id", None)
        if not self_id:
            return []
        found: list[CommentNode] = []
        stack = list(tree)
        while stack:
            node = stack.pop()
            if node.author.id == self_id and node.parent_id == parent_id and not node.is_deleted:
                found.append(node)
            stack.extend(reversed(node.children))
        found.sort(key=lambda node: (node.created_at is None, node.created_at or 0.0, node.id))
        return found

    async def _find_local_sent(self, trigger_comment_id: str) -> str | None:
        value, _failed = await self._find_local_sent_status(trigger_comment_id)
        return value

    async def _find_local_sent_status(self, trigger_comment_id: str) -> tuple[str | None, bool]:
        """查本地 sent 映射，并返回 ``(bot_id, query_failed)``。"""
        for name in (
            "find_comment_sent_for_trigger",
            "find_comment_sent_reply",
            "find_sent_comment_for_trigger",
            "find_comment_reply",
        ):
            method = getattr(self._store, name, None)
            if method is None:
                continue
            try:
                try:
                    value = method(trigger_comment_id)
                except TypeError:
                    value = method(trigger_comment_id=trigger_comment_id)
                if hasattr(value, "__await__"):
                    value = await value
            except Exception:
                # 初次发送前不能把本地查询异常当成“无映射”，否则可能重复外送；
                # 结果不确定的 POST 对账路径则会忽略该标记并继续查公开树。
                return None, True
            if value is None or value is False:
                return None, False
            if isinstance(value, str):
                return value, False
            if isinstance(value, Mapping):
                value = (
                    value.get("bot_comment_id")
                    or value.get("comment_id")
                    or value.get("id")
                )
                return (value, False) if isinstance(value, str) else (None, False)
            value = (
                getattr(value, "bot_comment_id", None)
                or getattr(value, "comment_id", None)
                or getattr(value, "id", None)
            )
            return (value, False) if isinstance(value, str) else (None, False)
        return None, False

    async def _record_sent(
        self,
        request: CommentRequest,
        posted: CommentNode | None,
        kind: str,
        *,
        reservation_token: str | None = None,
    ) -> bool:
        """远端成功后尽力写入 sent/mapping；返回是否已完成本地记录。"""
        if posted is None:
            # 没有 bot_comment_id 就无法建立 dedupe/mapping；保留 recover 状态。
            return False
        for name in (
            # A 组 Store 的原子终结接口必须优先；它把 sent mapping、attempt
            # 和 event done 放进同一个事务，成功后不再额外调用 mark_done。
            "finalize_comment_send",
            "finalize_comment_sent",
            "finalize_sent_comment",
            "record_comment_sent",
            "record_sent_comment",
            "record_comment_reply",
        ):
            method = getattr(self._store, name, None)
            if method is None:
                continue
            try:
                value = self._call_store_finalize(
                    method,
                    name=name,
                    request=request,
                    posted=posted,
                    kind=kind,
                    reservation_token=reservation_token,
                )
                if hasattr(value, "__await__"):
                    value = await value
                if value is False:
                    # 窄 Store 适配器也可能用 False 表示事务未提交；它与
                    # 抛异常一样必须等待恢复，不能继续标 done。
                    return False
            except Exception as exc:
                self._record_log_error("comment.sender.record_failed", request.blog_id, exc)
                # 本地记录失败时绝不能标记 done；下次启动需先对账。
                return False
            if name in {
                "finalize_comment_send",
                "finalize_comment_sent",
                "finalize_sent_comment",
            }:
                return True
            marked = await self._mark_done(request.comment_id)
            # Store 的窄替身可能只提供 sent/mapping 记录，由 Service 负责事件
            # 状态推进；缺少 mark 接口不等于本地记录失败。只有 mark 已存在但
            # 实际抛错时，才把成功投递保留为 recoverable。
            return marked is not False
        # 没有可写的 sent/mapping 接口时，远端虽已成功也必须保留可恢复状态。
        return False

    @staticmethod
    def _call_store_finalize(
        method: Any,
        *,
        name: str,
        request: CommentRequest,
        posted: CommentNode,
        kind: str,
        reservation_token: str | None,
    ) -> Any:
        """按签名选择一次 Store 成功终结调用，避免异常后重复提交。"""
        sent_at = time.time()
        values = {
            "triggercommentid": request.comment_id,
            "commentid": request.comment_id,
            "triggerid": request.comment_id,
            "botcommentid": posted.id,
            "messageid": posted.id,
            "blogid": request.blog_id,
            "articleid": request.blog_id,
            "conversationid": request.conversation_id,
            "sessionkey": request.conversation_id,
            "kind": kind,
            "sendkind": kind,
            "sentat": sent_at,
            "now": sent_at,
            "timestamp": sent_at,
            "reservationtoken": reservation_token,
            "token": reservation_token,
            "idempotencykey": reservation_token or request.comment_id,
            "dedupekey": reservation_token or request.comment_id,
        }
        kwargs = {
            "trigger_comment_id": request.comment_id,
            "bot_comment_id": posted.id,
            "blog_id": request.blog_id,
            "conversation_id": request.conversation_id,
            "kind": kind,
            "sent_at": sent_at,
            "reservation_token": reservation_token,
        }
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            # 不可反射的窄替身通常接受 kwargs；若它不接受，异常会被上层
            # 当作本地记录失败处理，绝不再尝试第二次提交。
            return method(**kwargs)

        try:
            signature.bind(**kwargs)
        except TypeError:
            pass
        else:
            # bind 只做本地签名检查，真正调用仅发生一次；实现内部抛出的
            # TypeError 不会被误判成“参数不兼容”而重放事务。
            return method(**kwargs)

        positional: list[object] = []
        keyword_only: dict[str, object] = {}
        has_unknown_required = False
        for parameter in signature.parameters.values():
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                continue
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                continue
            normalized = parameter.name.replace("_", "").lower()
            if normalized not in values:
                if (
                    parameter.default is inspect.Parameter.empty
                    and parameter.kind is not inspect.Parameter.KEYWORD_ONLY
                ):
                    has_unknown_required = True
                continue
            value = values[normalized]
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                keyword_only[parameter.name] = value
            else:
                positional.append(value)
        if not has_unknown_required:
            try:
                signature.bind(*positional, **keyword_only)
            except TypeError:
                pass
            else:
                return method(*positional, **keyword_only)

        # A final positional-only compatibility shape is selected without
        # invoking the method until bind() succeeds. This is for very narrow
        # adapters whose names are intentionally opaque.
        candidates = [
            (request.comment_id, posted.id, request.blog_id, request.conversation_id),
        ]
        if name == "finalize_comment_send":
            candidates.append(
                (
                    request.comment_id,
                    posted.id,
                    request.blog_id,
                    request.conversation_id,
                    kind,
                ),
            )
        else:
            candidates.append(
                (
                    request.comment_id,
                    posted.id,
                    request.blog_id,
                    request.conversation_id,
                    sent_at,
                ),
            )
        for args in candidates:
            try:
                signature.bind(*args)
            except TypeError:
                continue
            return method(*args)
        raise TypeError("Store success finalize signature is unsupported")

    async def _mark_done(self, trigger_comment_id: str) -> bool | None:
        """若 Store 暴露评论事件状态接口，送达后尽力推进为 done。"""
        for name in ("mark_comment_handled", "mark_comment_done", "mark_comment_event_done"):
            method = getattr(self._store, name, None)
            if method is None:
                continue
            try:
                try:
                    value = method(trigger_comment_id, "done")
                except TypeError:
                    value = method(comment_id=trigger_comment_id, status="done")
                if hasattr(value, "__await__"):
                    value = await value
                if value is False:
                    return False
            except Exception as exc:
                self._record_log_error("comment.sender.mark_done_failed", "", exc)
                return False
            return True
        return None

    async def _reserve(self, blog_id: str, kind: str, trigger_id: str) -> Any:
        method = getattr(self._quota, "reserve")
        try:
            value = method(blog_id, kind, trigger_comment_id=trigger_id)
        except TypeError:
            value = method(blog_id, kind)
        return await value if hasattr(value, "__await__") else value

    @staticmethod
    def _reservation_token(value: Any) -> str | None:
        """取出配额预留 token，兼容 DTO、字典和旧 bool 替身。"""
        if isinstance(value, Mapping):
            token = value.get("reservation_token") or value.get("token")
        else:
            token = getattr(value, "reservation_token", None) or getattr(value, "token", None)
        return token if isinstance(token, str) and token else None

    async def _note_sent(
        self,
        blog_id: str,
        trigger_id: str,
        kind: str,
        *,
        reservation_token: str | None = None,
    ) -> None:
        method = getattr(self._quota, "note_sent")
        try:
            value = self._call_quota_note(
                method, blog_id, trigger_id, kind, reservation_token=reservation_token
            )
            if hasattr(value, "__await__"):
                await value
        except Exception as exc:
            self._record_log_error("comment.sender.quota_note_failed", blog_id, exc)

    @staticmethod
    def _call_quota_note(
        method: Any,
        blog_id: str,
        trigger_id: str,
        kind: str,
        *,
        reservation_token: str | None = None,
    ) -> Any:
        """适配旧版与带幂等键的 CommentQuotaGuard.note_sent 签名。"""
        try:
            parameters = list(inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            parameters = []

        if parameters:
            positional: list[object] = []
            keyword: dict[str, object] = {}
            has_varargs = False
            for parameter in parameters:
                if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                    has_varargs = True
                    continue
                if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                    continue
                normalized = parameter.name.replace("_", "").lower()
                if normalized in {"blogid", "articleid"}:
                    value: object = blog_id
                elif normalized in {"kind", "sendkind"}:
                    value = kind
                elif normalized in {
                    "reservationtoken",
                    "token",
                    "idempotencykey",
                    "dedupekey",
                }:
                    value = reservation_token or trigger_id
                elif (
                    "idempot" in normalized
                    or "attemptid" in normalized
                    or "dedupeid" in normalized
                    or ("trigger" in normalized and "comment" in normalized)
                    or normalized in {"triggerid", "eventid"}
                ):
                    value = trigger_id
                else:
                    continue
                if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                    keyword[parameter.name] = value
                else:
                    positional.append(value)
            if has_varargs and len(positional) == 1:
                positional.extend((trigger_id, kind))
            if positional or keyword:
                return method(*positional, **keyword)

        # 最窄的旧替身没有可反射签名时，保留既有两个调用形状。
        try:
            return method(blog_id, trigger_id, kind)
        except TypeError:
            return method(blog_id, kind)

    async def _release(
        self,
        blog_id: str,
        kind: str,
        trigger_id: str,
        *,
        reservation_token: str | None = None,
    ) -> None:
        method = getattr(self._quota, "release")
        try:
            try:
                value = method(
                    blog_id,
                    kind,
                    trigger_comment_id=trigger_id,
                    reservation_token=reservation_token,
                )
            except TypeError:
                try:
                    value = method(blog_id, kind, trigger_comment_id=trigger_id)
                except TypeError:
                    value = method(blog_id, kind)
            if hasattr(value, "__await__"):
                await value
        except Exception as exc:
            self._record_log_error("comment.sender.quota_release_failed", blog_id, exc)

    async def _backoff(
        self,
        exc: SiteError,
        *,
        default_seconds: float | None = None,
    ) -> None:
        method = getattr(self._quota, "backoff", None)
        if method is None:
            return
        wait = (
            exc.retry_after
            if exc.retry_after is not None
            else (
                self._cfg.server_backoff_seconds
                if default_seconds is None
                else default_seconds
            )
        )
        try:
            value = method(wait)
            if hasattr(value, "__await__"):
                await value
        except Exception as error:
            self._record_log_error("comment.sender.quota_backoff_failed", "", error)

    @staticmethod
    def _is_allowed(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        allowed = getattr(value, "allowed", None)
        if isinstance(allowed, bool):
            return allowed
        decision = getattr(value, "decision", value)
        raw = getattr(decision, "value", decision)
        return str(raw).lower() in {"allow", "allowed", "ok", "true"}

    @staticmethod
    def _deny_reason(value: Any) -> str:
        declared = getattr(value, "reason", None)
        if callable(declared):
            try:
                declared = declared()
            except Exception:
                declared = None
        if isinstance(declared, str) and declared in {
            "minute",
            "backoff",
            "article",
            "quota",
        }:
            return declared
        decision = getattr(value, "decision", value)
        raw = str(getattr(decision, "value", decision)).lower()
        if "minute" in raw:
            return "minute"
        if "backoff" in raw or "rate" in raw:
            return "backoff"
        return "quota"

    def _record_log_error(self, event: str, blog_id: str, exc: Exception) -> None:
        log_event(self._logger, logging.ERROR, event, blog_id=blog_id, error=type(exc).__name__)

    def _log(self, result: CommentSendResult, blog_id: str, kind: str) -> None:
        log_event(
            self._logger,
            logging.INFO if result.delivered else logging.WARNING,
            "comment.sender",
            blog_id=blog_id,
            kind=kind,
            reason=result.reason,
        )


MessageSender = CommentSender

__all__ = ["CommentSendResult", "CommentSender", "MessageSender", "SendResult"]
