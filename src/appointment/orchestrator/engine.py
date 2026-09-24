"""持久任务编排器（设计稿 §5.1、§7、§11）。

一次"活跃执行"是这样的：

    建执行尝试 → 取执行权（CAS/lease/fence）→ 构建上下文 → 循环
      { 运行时出动作 → 校验 → 应用槽位补丁 → 经工具边界执行 → 更新事实 }
    → 进入持久等待或终态 → 收尾

三条纪律贯穿全局：

1. **不在事务中调用模型或供应商**。模型只在"构建好上下文之后、打开业务事务之前"
   被调用；工具执行与状态写入才进事务。
2. **等待是持久业务状态**，不是 SDK 内存状态。需要追问时写 waiting_request 并
   结束尝试；恢复时从可信账本重建上下文，不回填挂起调用（设计稿 §5.4）。
3. **授权不来自模型**。模型提出的 slot patch 与工具请求都要过校验；工具请求必须
   在角色许可内，写入工具仍由领域层复核确认凭据与版本。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent.ports import AgentRuntimePort, ToolRequest, TurnOutput, TurnRequest
from ..config.settings import Settings, get_settings
from ..core.clock import Clock
from ..core.enums import (
    ErrorCode,
    ExecutionEndReason,
    Intent,
    TaskEventType,
    TaskState,
    ToolStatus,
    WaitingKind,
    WaitingStatus,
)
from ..core.errors import DomainError, version_conflict
from ..core.hashing import content_hash
from ..core.result import ToolResult
from ..db import models as m
from ..db.session import LockSequencer, live_now, lock_task, db_now
from ..domain.catalog import list_store_services
from ..domain.context import TrustedContext
from ..domain.events import emit_task_event, latest_task_event_sequence
from ..domain.quote import get_service_quote
from ..domain.tasks import (
    append_message,
    ensure_conversation,
    get_or_create_task,
    set_task_state,
    update_slots,
    visible_message_ids,
)
from ..tools.registry import TOOL_REGISTRY, invoke_tool
from .budget import TurnBudget

logger = logging.getLogger("appointment.orchestrator")

#: 追问的有效期。超过就重新发起一轮，而不是接受一个可能已经无意义的旧答案。
CLARIFICATION_TTL_SECONDS = 1800

#: 只读工具可以批处理；写入工具必须逐个执行并立即落账（设计稿 §5.1 第 6 步）。
READ_ONLY_TOOLS = frozenset(
    name for name, spec in TOOL_REGISTRY.items() if spec.side_effect == "read"
)

#: 工具结果落到上下文里的哪个事实键。用于判断"本轮刚算出来"还是"用户已看过"。
FACT_KEY_BY_TOOL = {
    "get_service_quote": "quote",
    "search_availability": "availability",
}

#: 依赖失败时给用户的话。**不能**说成"没有可约时段"——那是把故障说成了事实。
DEPENDENCY_FAILURE_REPLY = "查询暂时不可用，我这边稍后重试；也可以直接为您转接人工。"


@dataclass(slots=True)
class TurnReport:
    """一次消息处理的对外摘要。"""

    task_id: UUID
    task_state: str
    task_version: int
    reply_text: str | None = None
    clarification_question: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    end_reason: str = ExecutionEndReason.COMPLETED.value
    budget: dict[str, Any] = field(default_factory=dict)
    waiting_id: UUID | None = None
    #: 待确认方案的服务端凭据。只交给本次请求的客户端，**不落事件账本**：
    #: 事件会被订阅者读到，凭据的存储形态是 confirmation.token_hash（设计稿 §8.3）。
    pending_confirmation: dict[str, Any] | None = None
    #: 本轮结束后的事件游标。客户端用它订阅 SSE，保证不漏事件。
    event_cursor: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id),
            "task_state": self.task_state,
            "task_version": self.task_version,
            "reply_text": self.reply_text,
            "clarification_question": self.clarification_question,
            "tool_calls": self.tool_calls,
            "end_reason": self.end_reason,
            "budget": self.budget,
            "waiting_id": None if self.waiting_id is None else str(self.waiting_id),
            "pending_confirmation": self.pending_confirmation,
            "event_cursor": self.event_cursor,
        }


class Orchestrator:
    """编排器。持有会话，但不持有跨请求的进程内状态。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        runtime: AgentRuntimePort,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.session = session
        self.runtime = runtime
        self.settings = settings or get_settings()
        self.clock = clock

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def handle_user_message(
        self,
        ctx: TrustedContext,
        *,
        text: str,
        client_message_id: str,
        conversation_id: UUID | None = None,
        store_id: UUID | None = None,
        now: datetime | None = None,
    ) -> TurnReport:
        """处理一条用户消息。

        调用方负责提交事务：编排器只保证**一次业务效果与它的事件在同一事务内**，
        不替调用方决定何时 commit——这样重试同一请求不会留下半成品。
        """

        decision_now = now or await live_now(self.session, self.clock)
        conversation = await ensure_conversation(
            self.session,
            ctx,
            store_id=store_id,
            conversation_id=conversation_id,
            now=decision_now,
        )

        message_outcome = await append_message(
            self.session,
            ctx,
            conversation_id=conversation.id,
            client_message_id=client_message_id,
            content=text,
            role="customer",
            now=decision_now,
        )
        task_outcome = await get_or_create_task(
            self.session,
            ctx,
            conversation=conversation,
            release_id=ctx.release_id,
            store_id=store_id or conversation.store_id,
            now=decision_now,
        )
        task = task_outcome.task
        await self.session.flush()

        if message_outcome.deduplicated:
            # 同一条消息重复提交：不重跑一遍业务效果，把当前任务状态原样回给调用方。
            # 也不重发 reply_end——上一条消息的事件已经在账本里，重复发会让订阅者
            # 以为又完成了一轮。
            return TurnReport(
                task_id=task.id,
                task_state=task.state,
                task_version=task.version,
                end_reason="DUPLICATE_MESSAGE",
                reply_text=None,
                event_cursor=await latest_task_event_sequence(
                    self.session, tenant_id=ctx.tenant_id, task_id=task.id
                ),
            )

        intent_hint = _intent_hint(text)
        if intent_hint is Intent.HANDOFF:
            return await self._handoff(ctx, task, text=text, now=decision_now)

        budget = TurnBudget.from_settings(self.settings)
        attempt = await self._open_attempt(
            ctx, task=task, now=decision_now, agent_role="reception"
        )
        return await self._run_loop(
            ctx,
            task=task,
            attempt=attempt,
            budget=budget,
            user_message=text,
            evidence_message_id=str(message_outcome.message.id),
            now=decision_now,
        )

    async def resume_after_waiting(
        self,
        ctx: TrustedContext,
        *,
        task_id: UUID,
        answer: dict[str, Any],
        client_event_id: str,
        now: datetime | None = None,
    ) -> TurnReport:
        """等待被答复后重建上下文并继续。

        设计稿 §5.4：重建只保留**已完成**的结果，不回填挂起的调用。因此这里
        重新从账本构建上下文，而不是续跑某个旧的内存快照。
        """

        decision_now = now or await live_now(self.session, self.clock)
        task = await _load_task_for_update(self.session, ctx, task_id=task_id)

        prior = (
            await self.session.execute(
                select(m.WaitingAnswer)
                .join(
                    m.WaitingRequest,
                    (m.WaitingRequest.id == m.WaitingAnswer.waiting_request_id)
                    & (m.WaitingRequest.tenant_id == m.WaitingAnswer.tenant_id),
                )
                .where(
                    m.WaitingAnswer.tenant_id == ctx.tenant_id,
                    m.WaitingRequest.task_id == task.id,
                    m.WaitingAnswer.client_event_id == client_event_id,
                )
            )
        ).scalar_one_or_none()
        if prior is not None:
            if prior.answer != answer:
                raise DomainError(
                    ErrorCode.VALIDATION_ERROR,
                    "同一答复事件 ID 对应不同内容",
                )
            return TurnReport(
                task_id=task.id,
                task_state=task.state,
                task_version=task.version,
                end_reason="DUPLICATE_ANSWER",
            )

        waiting = (
            await self.session.execute(
                select(m.WaitingRequest).where(
                    m.WaitingRequest.tenant_id == ctx.tenant_id,
                    m.WaitingRequest.task_id == task.id,
                    m.WaitingRequest.status == WaitingStatus.OPEN.value,
                )
            )
        ).scalars().first()
        if waiting is None:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR, "该任务没有进行中的等待请求"
            )

        stale = (
            task.version != waiting.task_version
            or task.epoch != waiting.epoch
            or waiting.expires_at <= decision_now
            or (
                answer.get("task_version") is not None
                and int(answer["task_version"]) != waiting.task_version
            )
            or (
                answer.get("waiting_id") is not None
                and str(answer["waiting_id"]) != str(waiting.id)
            )
            or (
                answer.get("question_version") is not None
                and answer["question_version"] != waiting.question_version
            )
        )
        self.session.add(
            m.WaitingAnswer(
                id=uuid4(),
                tenant_id=ctx.tenant_id,
                waiting_request_id=waiting.id,
                client_event_id=client_event_id,
                actor_id=ctx.actor_id,
                answer=answer,
                validation_status="REJECTED_STALE" if stale else "ACCEPTED",
                received_at=decision_now,
                applied_task_version=None if stale else task.version,
            )
        )
        if stale:
            # 过时答案只留审计，不推进任务：否则一次重放就能复活旧决策。
            if waiting.expires_at <= decision_now:
                waiting.status = WaitingStatus.EXPIRED.value
            await self.session.flush()
            return TurnReport(
                task_id=task.id,
                task_state=task.state,
                task_version=task.version,
                end_reason="STALE_ANSWER_REJECTED",
            )

        waiting.status = WaitingStatus.ANSWERED.value
        waiting.answered_event_id = client_event_id
        waiting.answered_at = decision_now
        waiting.version = waiting.version + 1

        if task.state == TaskState.WAITING_USER.value:
            task = await set_task_state(
                self.session,
                ctx,
                task=task,
                target=TaskState.COLLECTING,
                expected_version=task.version,
                now=decision_now,
                payload={"waiting_id": str(waiting.id)},
            )
        await self.session.flush()

        budget = TurnBudget.from_settings(self.settings)
        attempt = await self._open_attempt(
            ctx, task=task, now=decision_now, agent_role="reception"
        )
        return await self._run_loop(
            ctx,
            task=task,
            attempt=attempt,
            budget=budget,
            user_message=answer.get("text"),
            waiting_answer=answer,
            evidence_message_id=answer.get("evidence_message_id"),
            now=decision_now,
        )

    # ------------------------------------------------------------------
    # 执行权：CAS + 租约 + fence
    # ------------------------------------------------------------------
    async def _open_attempt(
        self, ctx: TrustedContext, *, task: m.Task, now: datetime, agent_role: str
    ) -> m.ExecutionAttempt:
        """取得执行权并开一条执行尝试记录。

        租约用 CAS 写入：只有租约空闲或已过期才允许接管，接管时递增 fencing_token。
        暂停后恢复的旧实例即使还"认为"自己持有租约，也会因为 fence 不匹配而失败。
        """

        sequencer = LockSequencer()
        locked = await lock_task(
            self.session, sequencer, tenant_id=ctx.tenant_id, task_id=task.id
        )
        if locked.epoch != task.epoch:
            raise DomainError(ErrorCode.LEASE_LOST, "任务代际已变化，本次执行作废")

        lease_free = (
            locked.lease_until is None
            or locked.lease_until <= now
            or locked.lease_owner == ctx.request_id
        )
        if not lease_free:
            raise DomainError(
                ErrorCode.LEASE_LOST,
                "任务正在被另一个执行者处理",
                retryable=True,
            )

        locked.lease_owner = ctx.request_id
        locked.lease_until = now + timedelta(seconds=self.settings.worker_lease_seconds)
        locked.fencing_token = locked.fencing_token + 1
        await self.session.flush()

        attempt = m.ExecutionAttempt(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            task_id=task.id,
            release_id=ctx.release_id,
            epoch=locked.epoch,
            fencing_token=locked.fencing_token,
            reply_id=f"{ctx.request_id}:{uuid4().hex[:8]}",
            agent_role=agent_role,
            status=ExecutionEndReason.COMPLETED.value,
            started_at=now,
            snapshot={"runtime": self.runtime.runtime_name, "agent_role": agent_role},
        )
        self.session.add(attempt)
        await self.session.flush()
        task.lease_owner = locked.lease_owner
        task.lease_until = locked.lease_until
        task.fencing_token = locked.fencing_token
        return attempt

    async def _assert_fence(
        self, ctx: TrustedContext, task: m.Task, attempt: m.ExecutionAttempt
    ) -> None:
        """每次权威写入前重新确认执行权。

        fence 只能由服务端读取，客户端/模型伪造的 fence 一律无效（设计稿 §5.1）。
        """

        # autoflush is disabled: a task version we advanced in this transaction
        # must reach PostgreSQL before comparing its authoritative row version.
        # This is still an uncommitted transaction, so the flush cannot publish
        # a partial business effect; the row lock below keeps the comparison/write
        # boundary atomic.
        await self.session.flush()
        row = (
            await self.session.execute(
                select(
                    m.Task.fencing_token, m.Task.epoch,
                    m.Task.lease_owner, m.Task.version,
                )
                .where(m.Task.tenant_id == ctx.tenant_id, m.Task.id == task.id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "任务不存在或无权访问")
        if row.epoch != attempt.epoch:
            raise DomainError(ErrorCode.LEASE_LOST, "任务代际已变化，本次执行作废")
        if row.fencing_token != attempt.fencing_token or row.lease_owner != ctx.request_id:
            raise DomainError(ErrorCode.LEASE_LOST, "执行权已被接管，本次执行作废")
        if row.version != task.version:
            raise version_conflict(
                "模型决策期间任务已变化，请基于最新状态重试",
                expected_version=task.version,
                actual_version=row.version,
            )

    async def _close_attempt(
        self, attempt: m.ExecutionAttempt, *, reason: ExecutionEndReason, now: datetime
    ) -> None:
        attempt.status = reason.value
        attempt.ended_at = now
        attempt.end_reason = reason.value
        await self.session.flush()

    async def _release_lease(self, ctx: TrustedContext, *, task_id: UUID) -> None:
        """释放租约。只清掉自己的租约，不误伤别人的接管。"""

        await self.session.execute(
            update(m.Task)
            .where(
                m.Task.tenant_id == ctx.tenant_id,
                m.Task.id == task_id,
                m.Task.lease_owner == ctx.request_id,
            )
            .values(lease_owner=None, lease_until=None)
        )
        await self.session.flush()

    async def _finish_turn_cleanup(
        self,
        ctx: TrustedContext,
        *,
        task_id: UUID,
        attempt: m.ExecutionAttempt,
        end_reason: ExecutionEndReason,
        now: datetime,
        masked: bool,
    ) -> None:
        """收尾本轮：盖执行尝试的结束原因，并归还租约。

        "清理不许掩盖真实失败"是一条硬规则，不是风格偏好：本函数在 ``finally``
        里运行，而它抛出的任何异常都会**顶替**正在传播的原始异常。线上真实症状
        就是唯一约束冲突被替换成不可读的 ``PendingRollbackError``，排查方向被
        整条带偏。

        ``masked=True`` 表示本轮已有异常在传播。此时清理失败只记录：租约到期后
        本就会被接管（设计稿允许的兜底路径），而丢掉失败原因是不可接受的。
        本轮成功时清理失败仍要抛出——那是真问题。
        """

        try:
            await self._close_attempt(attempt, reason=end_reason, now=now)
            await self._release_lease(ctx, task_id=task_id)
        except Exception:
            if not masked:
                raise
            logger.warning(
                "本轮已失败，收尾阶段再次失败；保留原始异常，租约到期后由接管兜底",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def _run_loop(
        self,
        ctx: TrustedContext,
        *,
        task: m.Task,
        attempt: m.ExecutionAttempt,
        budget: TurnBudget,
        user_message: str | None,
        evidence_message_id: str | None = None,
        now: datetime,
        waiting_answer: dict[str, Any] | None = None,
    ) -> TurnReport:
        report = TurnReport(
            task_id=task.id, task_state=task.state, task_version=task.version
        )
        end_reason = ExecutionEndReason.COMPLETED
        #: 本轮活跃执行里刚由工具算出的事实。运行时要靠它区分"刚查出来的候选"
        #: 与"用户上一条消息里已经看过的候选"。
        fresh_facts: set[str] = set()
        #: 在任何可能失败的 ORM 访问之前取成纯值。会话一旦被打成待回滚，
        #: ``task.id`` 这种属性访问会触发延迟加载并抛出，把清理路径也带塌。
        task_id_for_cleanup: UUID = task.id
        #: 本轮是否有异常正在向上传播。清理阶段靠它判断"该不该让清理异常发声"。
        in_flight: BaseException | None = None
        lost_execution = False

        try:
            while True:
                stop = budget.stop_reason(started_at=now, now=self._wall_now(now))
                if stop is not None and budget.tool_calls > 0:
                    end_reason = stop
                    break

                outcome = await self._invoke_runtime(
                    ctx,
                    task=task,
                    budget=budget,
                    user_message=user_message,
                    waiting_answer=waiting_answer,
                    fresh_facts=frozenset(fresh_facts),
                    attempt_id=attempt.id,
                    now=now,
                )
                if outcome is None:
                    end_reason = ExecutionEndReason.ERROR
                    break

                # 模型调用期间没有数据库事务；拿回决策后先锁住任务并比较
                # 本次 attempt 的不可变代次/令牌，再准许更新状态或调用工具。
                await self._assert_fence(ctx, task, attempt)

                progressed = False
                state_before = task.state

                if outcome.slot_patches:
                    task, changed_slots = await self._apply_patches(
                        ctx,
                        task=task,
                        patches=outcome.slot_patches,
                        evidence_message_id=evidence_message_id,
                        now=now,
                    )
                    # 槽位一变，本轮已经算出的事实就不再对应当前方案：作废掉，
                    # 让运行时按新槽位重新取事实，而不是拿旧候选去占位。
                    fresh_facts -= _invalidated_fact_keys(changed_slots)
                    # 只有真的改了槽位才算进展：无变化的改写不该被当成"在推进"。
                    progressed = progressed or bool(changed_slots)

                # 带工具请求的文案尚未有权威执行结果，不能先作为最终回复发出。
                if outcome.reply_text and not outcome.tool_requests:
                    report.reply_text = outcome.reply_text

                # Structured-output models sometimes put a missing-slot question in
                # reply_text but omit clarification_question. Do not leave a booking
                # task active with no durable wait record: the server derives the
                # missing requirements and persists a canonical clarification.
                if (
                    not outcome.clarification_question
                    and not outcome.tool_requests
                    and TaskState(task.state) is TaskState.COLLECTING
                    and not _slots_ready(dict(task.slots or {}))
                    and (
                        outcome.intent is Intent.BOOK
                        or _intent_hint(user_message or "") is Intent.BOOK
                        or _slot_service_id(dict(task.slots or {})) is not None
                        or bool((dict(task.slots or {}).get("time_window") or {}).get("value"))
                    )
                ):
                    question = _booking_clarification_question(dict(task.slots or {}))
                    waiting = await self._open_clarification(
                        ctx,
                        task=task,
                        attempt=attempt,
                        question=question,
                        now=now,
                    )
                    report.clarification_question = question
                    report.reply_text = question
                    report.waiting_id = waiting.id
                    end_reason = ExecutionEndReason.WAITING
                    break

                if outcome.clarification_question:
                    if TaskState(task.state) in (
                        TaskState.PROPOSED, TaskState.WAITING_CONFIRMATION
                    ):
                        # 询问选哪个候选、是否确认方案都是本轮回复。
                        # 保持原业务状态，不能创建 COLLECTING 用的追问等待。
                        report.clarification_question = outcome.clarification_question
                        report.reply_text = report.reply_text or outcome.clarification_question
                        break
                    waiting = await self._open_clarification(
                        ctx,
                        task=task,
                        attempt=attempt,
                        question=outcome.clarification_question,
                        now=now,
                    )
                    report.clarification_question = outcome.clarification_question
                    report.waiting_id = waiting.id
                    end_reason = ExecutionEndReason.WAITING
                    break

                if not outcome.tool_requests:
                    # 槽位齐备就把状态推到查询阶段并在同一轮继续推进，
                    # 不让用户为了"触发查询"再发一条消息。
                    task = await self._advance_after_reads(
                        ctx, task=task, results=[], now=now
                    )
                    if task.state != state_before:
                        budget.observe_progress(progressed=True)
                        continue
                    break

                # 只读调用可以批处理；写入调用逐个执行并立即落账。
                read_batch, write_queue = _partition_requests(outcome.tool_requests)
                if read_batch:
                    results = await self._execute_batch(
                        ctx, task=task, attempt=attempt, requests=read_batch,
                        budget=budget, now=now,
                    )
                    report.tool_calls.extend(results)
                    succeeded = [
                        entry
                        for entry in results
                        if entry["status"] == ToolStatus.OK.value
                    ]
                    progressed = progressed or bool(succeeded)
                    fresh_facts.update(_fact_keys_of(succeeded))
                    task = await self._advance_after_reads(
                        ctx, task=task, results=results, now=now
                    )
                    if not succeeded and not write_queue:
                        # 只读事实一个也没拿到：再问一次同样的东西不会变好。
                        # 依赖失败必须说成失败，不能描述成"没有号"（设计稿 §11）。
                        if not outcome.reply_text:
                            report.reply_text = DEPENDENCY_FAILURE_REPLY
                        end_reason = ExecutionEndReason.ERROR
                        break

                for request in write_queue:
                    result = await self._execute_one(
                        ctx, task=task, attempt=attempt, request=request,
                        budget=budget, now=now,
                    )
                    report.tool_calls.append(result)
                    if result["status"] == ToolStatus.OK.value:
                        progressed = True
                        if request.tool_name == "create_hold":
                            report.pending_confirmation = _confirmation_view(
                                result.get("data") or {}
                            )
                        task = await _reload(self.session, ctx, task_id=task.id)
                    else:
                        # 写入失败不重试同一请求；交给上层决定追问还是转人工。
                        report.reply_text = report.reply_text or (
                            f"这一步没有成功：{result.get('error_message')}"
                        )

                if task.state != state_before:
                    progressed = True
                budget.observe_progress(progressed=progressed)
                task = await _reload(self.session, ctx, task_id=task.id)

        except DomainError as exc:
            lost_execution = exc.code is ErrorCode.LEASE_LOST
            end_reason = (
                ExecutionEndReason.WAITING
                if lost_execution
                else ExecutionEndReason.ERROR
            )
            if not lost_execution:
                report.reply_text = report.reply_text or exc.message
        except BaseException as exc:
            # 数据库/程序性失败：会话很可能已经进入"待回滚"。先把原因记下来，
            # 再做一次显式回滚——这个事务注定要回滚，回滚不会丢掉任何已经承诺的
            # 效果，却是让 finally 里的清理还能跑得动的唯一前提；更关键的是，
            # 带着待回滚的会话去碰任何 ORM 属性都会抛 PendingRollbackError，
            # 把真正的失败原因整条盖掉（真实症状：唯一约束冲突变成不可读的 500）。
            in_flight = exc
            await self.session.rollback()
            raise
        finally:
            # 清理阶段禁止掩盖真实失败：本轮本来就在失败时，清理再失败只记录；
            # 本轮本来是成功的，清理失败就是真问题，必须抛出。
            await self._finish_turn_cleanup(
                ctx,
                task_id=task_id_for_cleanup,
                attempt=attempt,
                end_reason=end_reason,
                now=now,
                masked=in_flight is not None,
            )

        task = await _reload(self.session, ctx, task_id=task_id_for_cleanup)
        report.task_state = task.state
        report.task_version = task.version
        report.end_reason = "LEASE_LOST" if lost_execution else end_reason.value
        report.budget = budget.snapshot()
        if lost_execution:
            report.reply_text = None
            report.event_cursor = await latest_task_event_sequence(
                self.session, tenant_id=ctx.tenant_id, task_id=task.id
            )
        else:
            report.event_cursor = await self._emit_reply_events(
                ctx,
                task_id=task.id,
                reply_text=report.reply_text,
                end_reason=end_reason,
                now=now,
            )
        return report

    async def _emit_reply_events(
        self,
        ctx: TrustedContext,
        *,
        task_id: UUID,
        reply_text: str | None,
        end_reason: ExecutionEndReason,
        now: datetime,
    ) -> int:
        """把这一轮对用户可见的结果写成持久事件。

        文本与 reply_end 是流程事件，不是业务事实：``appointment_committed`` 这类
        业务事件由已提交事务产生，不能由 reply_end 反推（设计稿 §9）。
        返回最新事件游标，供客户端订阅时作为起点。
        """

        sequencer = LockSequencer()
        locked = await lock_task(
            self.session, sequencer, tenant_id=ctx.tenant_id, task_id=task_id
        )
        if reply_text:
            await emit_task_event(
                self.session,
                task=locked,
                event_type=TaskEventType.ASSISTANT_MESSAGE,
                payload={"text": reply_text},
                occurred_at=now,
            )
        await emit_task_event(
            self.session,
            task=locked,
            event_type=TaskEventType.REPLY_END,
            payload={
                "end_reason": end_reason.value,
                "task_state": locked.state,
            },
            occurred_at=now,
        )
        await self.session.flush()
        return locked.next_event_sequence - 1

    # ------------------------------------------------------------------
    # 上下文构建与运行时调用
    # ------------------------------------------------------------------
    def _wall_now(self, fallback: datetime) -> datetime:
        """预算计时用的"当前时刻"。

        注入时钟时以它为推进源（测试可推进），否则退回本轮起始时刻——生产环境
        的活跃执行时延由接入层的超时控制兜底。
        """

        return self.clock.now() if self.clock is not None else fallback

    async def _build_facts(
        self,
        ctx: TrustedContext,
        *,
        task: m.Task,
        now: datetime,
        attempt_id: UUID | None = None,
        waiting_answer: dict[str, Any] | None,
    ) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "now": now,
            "store_timezone": self.settings.timezone,
            "store_id": str(task.store_id) if task.store_id else None,
            "task_id": str(task.id),
            "followups": {},
        }
        if task.store_id is not None:
            services = await list_store_services(
                self.session, tenant_id=ctx.tenant_id, store_id=task.store_id
            )
            facts["services"] = [
                {
                    "service_id": str(service.id),
                    "name": service.name,
                    "aliases": list(service.aliases or []),
                }
                for service in services
            ]
        facts["followups"] = await self._rebuild_followups(
            ctx, task=task, now=now, attempt_id=attempt_id
        )
        if waiting_answer is not None:
            facts["waiting_answer"] = waiting_answer
        return facts

    async def _rebuild_followups(
        self,
        ctx: TrustedContext,
        *,
        task: m.Task,
        now: datetime,
        attempt_id: UUID | None = None,
    ) -> dict[str, Any]:
        """从工具账本重建对话事实（设计稿 §5.4 的"重建式恢复"）。

        只取**已完成**的只读调用结果，不注入任何挂起调用。凭据（quote_token）
        不落账本，需要时在上下文构建阶段重新签发。

        事实带**查询指纹**：候选只有在"同一个服务 + 同一个时间窗"下才算数。
        否则用户改了时间之后，旧候选还会被当成当前方案摆出来。
        """

        rows = (
            await self.session.execute(
                select(m.ToolExecution)
                .join(
                    m.ExecutionAttempt,
                    (m.ExecutionAttempt.id == m.ToolExecution.attempt_id)
                    & (m.ExecutionAttempt.tenant_id == m.ToolExecution.tenant_id),
                )
                .where(
                    m.ToolExecution.tenant_id == ctx.tenant_id,
                    m.ExecutionAttempt.task_id == task.id,
                    m.ToolExecution.tool_name.in_(
                        ["search_availability", "get_service_quote"]
                    ),
                    m.ToolExecution.status == ToolStatus.OK.value,
                )
                .order_by(
                    # 同一次活跃执行的账本优先，其次取最近完成的一条。
                    (
                        m.ToolExecution.attempt_id == attempt_id
                        if attempt_id is not None
                        else m.ToolExecution.attempt_id.is_(None)
                    ).desc(),
                    m.ToolExecution.finished_at.desc(),
                )
                .limit(20)
            )
        ).scalars().all()

        service_id = _slot_service_id(task.slots)
        query_key = _availability_query_key(task.slots)

        followups: dict[str, Any] = {}
        for row in rows:
            result = row.result_ref
            if not result or result.get("task_id") != str(task.id):
                continue
            if row.tool_name == "search_availability":
                # 时间窗/服务变了，旧候选就不再是"我们刚给用户看过的那个方案"。
                if (
                    "availability" not in followups
                    and query_key is not None
                    and result.get("query_key") == query_key
                ):
                    followups["availability"] = result
            elif (
                row.tool_name == "get_service_quote"
                and "quote" not in followups
                and result.get("service_id") == service_id
            ):
                followups["quote"] = result

        # 凭据不落账本。若账本里**已经有**报价事实，按当前服务重新签发一次，
        # 让恢复后的这一轮能直接进入占位；账本里没有报价时不预签发——首次取价
        # 必须走 get_service_quote 工具，才留得下可审计的调用记录。
        recorded_quote = followups.get("quote")
        if (
            recorded_quote is not None
            and not recorded_quote.get("quote_token")
            and service_id
            and task.store_id
        ):
            try:
                service = await _load_service_facts(
                    self.session, ctx, store_id=task.store_id, service_id=service_id
                )
                offer = await get_service_quote(
                    self.session,
                    tenant_id=ctx.tenant_id,
                    customer_id=ctx.require_customer(),
                    store_id=task.store_id,
                    service=service,
                    as_of=now,
                )
                followups["quote"] = {**offer.to_data(), "task_id": str(task.id)}
            except DomainError:
                # 报价不可用时保持缺失：运行时会提示重新取价，不编造金额。
                followups.pop("quote", None)
        return followups

    async def _invoke_runtime(
        self,
        ctx: TrustedContext,
        *,
        task: m.Task,
        budget: TurnBudget,
        user_message: str | None,
        waiting_answer: dict[str, Any] | None,
        now: datetime,
        fresh_facts: frozenset[str] = frozenset(),
        attempt_id: UUID | None = None,
    ) -> TurnOutput | None:
        facts = await self._build_facts(
            ctx,
            task=task,
            now=now,
            attempt_id=attempt_id,
            waiting_answer=waiting_answer,
        )
        completed = await self._completed_action_summaries(ctx, task=task)
        allowed = _allowed_tools_for_state(TaskState(task.state))
        request = TurnRequest(
            ctx=ctx,
            task_id=task.id,
            task_state=TaskState(task.state),
            task_version=task.version,
            slots=dict(task.slots or {}),
            user_message=user_message,
            allowed_tools=allowed,
            completed_actions=completed,
            facts=facts,
            fresh_fact_keys=fresh_facts,
            waiting_answer=waiting_answer,
        )
        # 不持有任务锁和业务事务等待模型网络响应。跨调用的权威只有数据库账本；
        # 模型返回后由 _assert_fence 重新取得任务锁并确认本次执行权。
        await self.session.commit()
        try:
            return await self.runtime.run_turn(request)
        except DomainError as exc:
            if exc.code is ErrorCode.DEPENDENCY_UNAVAILABLE:
                raise
            return None

    async def _completed_action_summaries(
        self, ctx: TrustedContext, *, task: m.Task
    ) -> tuple[dict[str, Any], ...]:
        rows = (
            await self.session.execute(
                select(m.ToolExecution)
                .join(
                    m.ExecutionAttempt,
                    (m.ExecutionAttempt.id == m.ToolExecution.attempt_id)
                    & (m.ExecutionAttempt.tenant_id == m.ToolExecution.tenant_id),
                )
                .where(
                    m.ToolExecution.tenant_id == ctx.tenant_id,
                    m.ExecutionAttempt.task_id == task.id,
                    m.ToolExecution.status == ToolStatus.OK.value,
                )
                .order_by(m.ToolExecution.finished_at.desc())
                .limit(10)
            )
        ).scalars().all()
        return tuple(
            {
                "tool": row.tool_name,
                "status": row.status,
                "summary": _trim_for_context(row.result_ref or {}),
            }
            for row in rows
        )

    # ------------------------------------------------------------------
    # 槽位与状态推进
    # ------------------------------------------------------------------
    async def _apply_patches(
        self,
        ctx: TrustedContext,
        *,
        task: m.Task,
        patches: Sequence[dict[str, Any]],
        evidence_message_id: str | None,
        now: datetime,
    ) -> tuple[m.Task, set[str]]:
        """应用槽位补丁，返回 (任务, 真正发生变化的槽位名)。

        证据只允许引用**本任务所在会话**中该客户有权读取的消息：模型不能凭一段
        没有来源的文本改写槽位，也不能引用别的会话（设计稿 §6.3）。

        改动作废已经给出的候选：用户改了时间/项目之后，旧候选不再是"刚才那个
        方案"，必须回到搜索阶段重新算，状态停在 PROPOSED 会拿旧候选去占位。
        """

        state_before = TaskState(task.state)
        slots_before = dict(task.slots or {})
        rows = (
            await self.session.execute(
                select(m.Message).where(
                    m.Message.tenant_id == ctx.tenant_id,
                    m.Message.conversation_id == task.conversation_id,
                )
            )
        ).scalars().all()
        allowed_evidence = visible_message_ids(
            list(rows), customer_id=ctx.require_customer()
        )
        if evidence_message_id is not None:
            allowed_evidence = allowed_evidence | {str(evidence_message_id)}

        updated = await update_slots(
            self.session,
            ctx,
            task=task,
            patches=[
                {
                    **patch,
                    "evidence_message_id": patch.get("evidence_message_id")
                    or evidence_message_id,
                }
                for patch in patches
            ],
            allowed_evidence_message_ids=allowed_evidence,
            expected_task_version=task.version,
            now=now,
        )

        changed = _changed_slot_names(slots_before, dict(updated.slots or {}))
        if state_before is TaskState.PROPOSED and changed & PROPOSAL_INVALIDATING_SLOTS:
            updated = await set_task_state(
                self.session,
                ctx,
                task=updated,
                target=TaskState.SEARCHING,
                expected_version=updated.version,
                now=now,
                payload={
                    "reason": "slots_changed_after_proposal",
                    "changed": sorted(changed),
                },
            )
        return updated, changed

    async def _advance_after_reads(
        self,
        ctx: TrustedContext,
        *,
        task: m.Task,
        results: list[dict[str, Any]],
        now: datetime,
    ) -> m.Task:
        """根据只读调用结果推进任务状态。

        状态推进只在**事实齐备**时发生：项目与时间窗齐备才进入 SEARCHING，
        候选非空才进入 PROPOSED。没有候选时保持 SEARCHING 并把原因交给用户，
        而不是假装"查到了"。
        """

        current = TaskState(task.state)
        slots = dict(task.slots or {})

        if current is TaskState.COLLECTING and _slots_ready(slots):
            return await set_task_state(
                self.session,
                ctx,
                task=task,
                target=TaskState.SEARCHING,
                expected_version=task.version,
                now=now,
                payload={"reason": "slots_ready"},
            )

        if current is TaskState.SEARCHING:
            for entry in results:
                if entry["tool"] != "search_availability":
                    continue
                if entry["status"] != ToolStatus.OK.value:
                    continue
                candidates = (entry.get("data") or {}).get("candidates") or []
                if not candidates:
                    return task
                await emit_task_event(
                    self.session,
                    task=task,
                    event_type=TaskEventType.CANDIDATES_READY,
                    payload={
                        "candidate_count": len(candidates),
                        "candidate_ids": [
                            candidate["candidate_id"] for candidate in candidates[:5]
                        ],
                    },
                    occurred_at=now,
                )
                return await set_task_state(
                    self.session,
                    ctx,
                    task=task,
                    target=TaskState.PROPOSED,
                    expected_version=task.version,
                    now=now,
                    payload={"reason": "candidates_ready"},
                )
        return task

    # ------------------------------------------------------------------
    # 工具执行与账本
    # ------------------------------------------------------------------
    async def _execute_batch(
        self,
        ctx: TrustedContext,
        *,
        task: m.Task,
        attempt: m.ExecutionAttempt,
        requests: Sequence[ToolRequest],
        budget: TurnBudget,
        now: datetime,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for request in requests:
            if not budget.can_call_tool():
                break
            if budget.observe_call(request.tool_name, request.arguments):
                # 同一工具+参数重复出现：不重复执行，避免"看起来在推进"。
                results.append(
                    {
                        "tool": request.tool_name,
                        "status": ToolStatus.ERROR.value,
                        "error_code": ErrorCode.VALIDATION_ERROR.value,
                        "error_message": "重复调用被跳过",
                    }
                )
                continue
            results.append(
                await self._execute_one(
                    ctx, task=task, attempt=attempt, request=request,
                    budget=budget, now=now,
                )
            )
        return results

    async def _execute_one(
        self,
        ctx: TrustedContext,
        *,
        task: m.Task,
        attempt: m.ExecutionAttempt,
        request: ToolRequest,
        budget: TurnBudget,
        now: datetime,
    ) -> dict[str, Any]:
        await self._assert_fence(ctx, task, attempt)
        budget.record_tool_call()
        started_at = now
        tool_call_id = uuid4().hex[:32]

        result: ToolResult = await invoke_tool(
            self.session,
            ctx,
            request.tool_name,
            request.arguments,
            now=now,
            clock=self.clock,
            allowed_tools=_allowed_tools_for_state(TaskState(task.state)),
        )
        entry = {
            "tool": request.tool_name,
            "status": result.status.value,
            "tool_call_id": tool_call_id,
        }
        if result.data is not None:
            entry["data"] = result.data
        if result.error_code is not None:
            entry["error_code"] = result.error_code.value
            entry["error_message"] = result.error_message

        spec = TOOL_REGISTRY.get(request.tool_name)
        self.session.add(
            m.ToolExecution(
                id=uuid4(),
                tenant_id=ctx.tenant_id,
                attempt_id=attempt.id,
                tool_call_id=tool_call_id,
                tool_name=request.tool_name,
                tool_version=spec.version if spec else "1.0.0",
                operation_id=None,
                parameter_hash=content_hash(request.arguments or {}),
                status=result.status.value,
                result_ref=_persistable_projection(
                    request.tool_name,
                    result,
                    task_id=task.id,
                    query_key=_availability_query_key(task.slots),
                ),
                error_code=(
                    result.error_code.value if result.error_code is not None else None
                ),
                started_at=started_at,
                finished_at=now,
            )
        )
        await self.session.flush()
        return entry

    # ------------------------------------------------------------------
    # 等待与转人工
    # ------------------------------------------------------------------
    async def _open_clarification(
        self,
        ctx: TrustedContext,
        *,
        task: m.Task,
        attempt: m.ExecutionAttempt,
        question: str,
        now: datetime,
    ) -> m.WaitingRequest:
        await _supersede_open_waitings(self.session, tenant_id=ctx.tenant_id, task_id=task.id)
        waiting = m.WaitingRequest(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            task_id=task.id,
            attempt_id=attempt.id,
            kind=WaitingKind.CLARIFICATION.value,
            question_id=uuid4(),
            question_version=1,
            question_text=question,
            task_version=task.version,
            epoch=task.epoch,
            release_id=ctx.release_id,
            input_schema={"type": "object", "additionalProperties": False},
            expires_at=now + timedelta(seconds=CLARIFICATION_TTL_SECONDS),
            status=WaitingStatus.OPEN.value,
        )
        self.session.add(waiting)
        await self.session.flush()
        await emit_task_event(
            self.session,
            task=task,
            event_type=TaskEventType.CLARIFICATION_REQUIRED,
            payload={
                "waiting_id": str(waiting.id),
                "question_id": str(waiting.question_id),
                "question": question,
            },
            occurred_at=now,
        )
        updated = await set_task_state(
            self.session,
            ctx,
            task=task,
            target=TaskState.WAITING_USER,
            expected_version=task.version,
            now=now,
            payload={"waiting_id": str(waiting.id)},
        )
        # 追问版本记 **状态推进之后** 的版本：那才是用户看到的版本。
        # 记推进之前的值会让随后的答复被误判为"最新"，从而复活旧决策。
        waiting.task_version = updated.version
        await self.session.flush()
        return waiting

    async def _handoff(
        self, ctx: TrustedContext, task: m.Task, *, text: str, now: datetime
    ) -> TurnReport:
        attempt = await self._open_attempt(
            ctx, task=task, now=now, agent_role="reception"
        )
        result = await invoke_tool(
            self.session,
            ctx,
            "transfer_to_human",
            {
                "task_id": str(task.id),
                "reason_code": "user_request",
                "summary": text[:200],
            },
            now=now,
            clock=self.clock,
        )
        await self._close_attempt(
            attempt, reason=ExecutionEndReason.WAITING, now=now
        )
        await self._release_lease(ctx, task_id=task.id)
        task = await _reload(self.session, ctx, task_id=task.id)
        reply_text = (
            "已经为您转接人工，稍后会有同事联系您。"
            if result.status is ToolStatus.OK
            else result.error_message
        )
        cursor = await self._emit_reply_events(
            ctx,
            task_id=task.id,
            reply_text=reply_text,
            end_reason=ExecutionEndReason.WAITING,
            now=now,
        )
        return TurnReport(
            task_id=task.id,
            task_state=task.state,
            task_version=task.version,
            reply_text=reply_text,
            end_reason=ExecutionEndReason.WAITING.value,
            event_cursor=cursor,
        )


# ---------------------------------------------------------------------------
# 模块级辅助
# ---------------------------------------------------------------------------
def _intent_hint(text: str) -> Intent:
    from ..agent.deterministic import classify_intent

    return classify_intent(text or "")


def _slots_ready(slots: dict[str, Any]) -> bool:
    return bool(_slot_service_id(slots)) and bool(
        (slots.get("time_window") or {}).get("value")
    )


def _booking_clarification_question(slots: dict[str, Any]) -> str:
    missing_service = _slot_service_id(slots) is None
    missing_time = not bool((slots.get("time_window") or {}).get("value"))
    if missing_service and missing_time:
        return "请告诉我想预约的服务项目，以及希望预约的日期和时间。"
    if missing_service:
        return "我已记录您希望的时间范围，请问想预约哪个服务项目？"
    return "我已记录您选择的服务项目，请问希望哪天、大概几点到店？"


def _confirmation_view(data: dict[str, Any]) -> dict[str, Any] | None:
    """把 create_hold 的结果折成"确认卡"所需字段。

    只在这一轮的响应里交给客户端；事件账本与工具账本都不保存凭据明文。
    """

    proposal_id = data.get("proposal_id")
    if not proposal_id:
        return None
    return {
        "proposal_id": str(proposal_id),
        "proposal_version": data.get("proposal_version"),
        "proposal_content_hash": data.get("proposal_content_hash"),
        "confirmation_token": data.get("confirmation_token"),
        "expires_at": data.get("confirmation_expires_at"),
        "hold_id": data.get("hold_id"),
        "hold_expires_at": data.get("expires_at"),
        # 确认时服务端会用推进后的任务版本做 CAS，客户端必须原样带回。
        "expected_task_version": data.get("task_version"),
    }


def _slot_service_id(slots: dict[str, Any]) -> str | None:
    value = (slots.get("service") or {}).get("value") or {}
    service_id = value.get("service_id")
    return str(service_id) if service_id else None


def _availability_query_key(slots: dict[str, Any]) -> str | None:
    """可用性事实的查询指纹：服务 + 时间窗。

    候选只在"同一个服务、同一个时间窗"下有意义。带上指纹后，用户改了时间就
    不会再把旧候选当成当前方案（否则会用过期候选去占位）。
    """

    service_id = _slot_service_id(slots)
    window = (slots.get("time_window") or {}).get("value") or {}
    if not service_id or not window:
        return None
    return content_hash(
        {
            "service_id": service_id,
            "start_at": window.get("start_at"),
            "end_at": window.get("end_at"),
            "desired_start": window.get("desired_start"),
        }
    )


#: 改动这些槽位就作废当前候选：方案是按它们算出来的。
PROPOSAL_INVALIDATING_SLOTS = frozenset({"store", "service", "time_window"})


def _invalidated_fact_keys(changed_slots: set[str]) -> set[str]:
    """槽位变化会作废哪些已算出的事实。"""

    invalidated: set[str] = set()
    if changed_slots & PROPOSAL_INVALIDATING_SLOTS:
        invalidated.add("availability")
    if changed_slots & {"store", "service"}:
        invalidated.add("quote")
    return invalidated


def _slot_value(slots: dict[str, Any], name: str) -> Any:
    """只比较槽位的**值**：来源消息与时间戳变化不算"改写"。"""

    entry = slots.get(name)
    if not isinstance(entry, dict):
        return None
    return entry.get("value")


def _changed_slot_names(
    before: dict[str, Any], after: dict[str, Any]
) -> set[str]:
    return {
        name
        for name in set(before) | set(after)
        if _slot_value(before, name) != _slot_value(after, name)
    }


def _allowed_tools_for_state(state: TaskState) -> tuple[str, ...]:
    """按状态开放工具。默认拒绝：不开放与当前阶段无关的工具。"""

    base_read = ("search_knowledge", "get_service_quote", "search_availability", "get_appointment")
    if state in (
        TaskState.COLLECTING,
        TaskState.SEARCHING,
        TaskState.NEEDS_REPLAN,
        TaskState.WAITING_USER,
    ):
        return base_read + ("transfer_to_human",)
    if state is TaskState.PROPOSED:
        return base_read + ("create_hold", "transfer_to_human")
    if state is TaskState.WAITING_CONFIRMATION:
        return base_read + ("confirm_appointment", "transfer_to_human")
    if state is TaskState.SUCCEEDED:
        return ("get_appointment", "search_knowledge", "reschedule_appointment", "cancel_appointment", "transfer_to_human")
    return base_read + ("transfer_to_human",)


def _partition_requests(
    requests: Sequence[ToolRequest],
) -> tuple[list[ToolRequest], list[ToolRequest]]:
    reads = [r for r in requests if r.tool_name in READ_ONLY_TOOLS]
    writes = [r for r in requests if r.tool_name not in READ_ONLY_TOOLS]
    return reads, writes


def _fact_keys_of(entries: Sequence[dict[str, Any]]) -> set[str]:
    """把成功的只读调用映射成上下文事实键。"""

    keys: set[str] = set()
    for entry in entries:
        key = FACT_KEY_BY_TOOL.get(str(entry.get("tool")))
        if key is not None:
            keys.add(key)
    return keys


def _trim_for_context(payload: dict[str, Any], *, max_items: int = 5) -> dict[str, Any]:
    """裁剪写进模型上下文的结果。裁剪不能把关键错误状态一起裁掉。"""

    trimmed: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, list):
            trimmed[key] = value[:max_items]
        else:
            trimmed[key] = value
    return trimmed


def _persistable_projection(
    tool_name: str,
    result: ToolResult,
    *,
    task_id: UUID,
    query_key: str | None = None,
) -> dict[str, Any]:
    """写入工具账本的投影。

    只保留后续重建上下文需要的最小事实，并且**不落任何凭据**：quote_token、
    confirmation_token 都只出现在当轮的返回值里，需要时重新签发。
    候选事实额外带上查询指纹，保证它只对同一个服务 + 同一个时间窗有效。
    """

    data = result.data or {}
    projection: dict[str, Any] = {"task_id": str(task_id), "status": result.status.value}

    if tool_name == "search_availability":
        projection["candidates"] = [
            {
                "candidate_id": candidate.get("candidate_id"),
                "start_at": candidate.get("start_at"),
                "end_at": candidate.get("end_at"),
                "resources": candidate.get("resources"),
                "guarantee": candidate.get("guarantee"),
                "score": candidate.get("score"),
                "reasons": candidate.get("reasons"),
            }
            for candidate in (data.get("candidates") or [])[:10]
        ]
        projection["snapshot_at"] = data.get("snapshot_at")
        projection["guarantee"] = data.get("guarantee")
        projection["notes"] = data.get("notes")
        projection["query_key"] = query_key
    elif tool_name == "get_service_quote":
        projection.update(
            {
                "quote_id": data.get("quote_id"),
                "service_id": data.get("service_id"),
                "duration_minutes": data.get("duration_minutes"),
                "amount_minor": data.get("amount_minor"),
                "currency": data.get("currency"),
                "quote_version": data.get("quote_version"),
                "valid_until": data.get("valid_until"),
            }
        )
    elif tool_name == "search_knowledge":
        projection["hits"] = (data.get("hits") or [])[:5]
        projection["answered"] = data.get("answered")
    else:
        projection["data"] = _trim_for_context(data)

    if result.error_code is not None:
        projection["error_code"] = result.error_code.value
    return projection


async def _load_task_for_update(
    session: AsyncSession, ctx: TrustedContext, *, task_id: UUID
) -> m.Task:
    sequencer = LockSequencer()
    return await lock_task(
        session, sequencer, tenant_id=ctx.tenant_id, task_id=task_id
    )


async def _reload(session: AsyncSession, ctx: TrustedContext, *, task_id: UUID) -> m.Task:
    """重新读取任务。

    必须先 ``flush`` 再读：会话的 autoflush 是关闭的，直接 ``refresh`` 会把
    **尚未落盘**的状态改动丢掉，表现为"状态推进了但立刻又退回去"。这类静默回退
    在编排器里是致命的——工具已经执行、状态却没留下。
    """

    await session.flush()
    row = (
        await session.execute(
            select(m.Task)
            .where(m.Task.tenant_id == ctx.tenant_id, m.Task.id == task_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def _supersede_open_waitings(
    session: AsyncSession, *, tenant_id: UUID, task_id: UUID
) -> None:
    await session.execute(
        update(m.WaitingRequest)
        .where(
            m.WaitingRequest.tenant_id == tenant_id,
            m.WaitingRequest.task_id == task_id,
            m.WaitingRequest.status == WaitingStatus.OPEN.value,
        )
        .values(status=WaitingStatus.SUPERSEDED.value)
    )


async def _load_service_facts(
    session: AsyncSession, ctx: TrustedContext, *, store_id: UUID, service_id: str
):
    from ..domain.catalog import load_service

    return await load_service(
        session,
        tenant_id=ctx.tenant_id,
        store_id=store_id,
        service_id=UUID(str(service_id)),
    )


__all__ = ["CLARIFICATION_TTL_SECONDS", "Orchestrator", "TurnReport"]
