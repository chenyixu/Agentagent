"""九个业务工具的处理器。

每个处理器都是"类型化函数 + 领域服务"的薄层，职责只有三件事：

1. 把已校验的输入翻译成领域服务的参数（**不**做业务校验）；
2. 调用领域服务，由它执行权限、版本、归属与事务规则；
3. 把领域结果整理成统一结果契约里的 ``data``。

这里刻意不放任何业务判断：如果某个规则写在工具层，人工后台走管理命令时就会绕过
它（设计稿 §9："业务 API 同样调用这些领域服务"）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import Clock
from ..core.enums import TaskEventType, TaskState
from ..core.errors import DomainError, ErrorCode, validation_error
from ..db import models as m
from ..domain.availability import (
    ResourcePreferences,
    search_availability,
)
from ..domain.booking import (
    cancel_appointment,
    confirm_appointment,
    create_hold,
    get_appointment,
    reschedule_appointment,
)
from ..domain.catalog import load_service
from ..domain.context import TrustedContext
from ..domain.events import emit_task_event
from ..domain.knowledge import search_knowledge
from ..domain.quote import get_service_quote
from ..domain.tasks import load_task, set_task_state
from .schemas import (
    CancelAppointmentInput,
    ConfirmAppointmentInput,
    CreateHoldInput,
    GetAppointmentInput,
    GetServiceQuoteInput,
    RescheduleAppointmentInput,
    SearchAvailabilityInput,
    SearchKnowledgeInput,
    TransferToHumanInput,
)


# ---------------------------------------------------------------------------
# 只读工具
# ---------------------------------------------------------------------------
async def search_knowledge_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    payload: SearchKnowledgeInput,
    *,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    result = await search_knowledge(
        session,
        ctx,
        query=payload.query,
        store_id=payload.store_id,
        as_of=now,
        top_k=payload.top_k,
    )
    data = result.to_data()
    # 无证据拒答：空命中不是错误，但必须把"为什么没有"一起返回，
    # 否则调用方会把"证据不足"误读成"没有这项政策"。
    data["answered"] = bool(result.hits)
    return data


async def get_service_quote_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    payload: GetServiceQuoteInput,
    *,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    ctx.require("catalog:read")
    ctx.require_store_scope(payload.store_id)
    customer_id = ctx.require_customer()
    service = await load_service(
        session,
        tenant_id=ctx.tenant_id,
        store_id=payload.store_id,
        service_id=payload.service_id,
    )
    offer = await get_service_quote(
        session,
        tenant_id=ctx.tenant_id,
        customer_id=customer_id,
        store_id=payload.store_id,
        service=service,
        as_of=now,
    )
    # 带上 service_id：模型要能说清这份报价对应哪个项目，账本重建也需要它做归属校验。
    return {
        **offer.to_data(),
        "service_id": str(service.service_id),
        "store_id": str(payload.store_id),
    }


async def search_availability_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    payload: SearchAvailabilityInput,
    *,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    ctx.require("catalog:read")
    ctx.require_store_scope(payload.store_id)
    service = await load_service(
        session,
        tenant_id=ctx.tenant_id,
        store_id=payload.store_id,
        service_id=payload.service_id,
    )
    result = await search_availability(
        session,
        tenant_id=ctx.tenant_id,
        store_id=payload.store_id,
        service=service,
        window_start=payload.window_start,
        window_end=payload.window_end,
        now=now,
        desired_start=payload.desired_start,
        preferences=ResourcePreferences(
            preferred_resource_ids=list(payload.preferred_resource_ids),
            excluded_resource_ids=list(payload.excluded_resource_ids),
            gender=payload.gender,
            gender_hard=payload.gender_hard,
            skill_codes=list(payload.skill_codes),
            allow_substitute=payload.allow_substitute,
        ),
        budget_minor=payload.budget_minor,
        limit=payload.limit,
    )
    return result.to_data()


async def get_appointment_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    payload: GetAppointmentInput,
    *,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    appointment = await get_appointment(
        session, ctx, appointment_id=payload.appointment_id
    )
    return _appointment_data(appointment)


# ---------------------------------------------------------------------------
# 写入工具
# ---------------------------------------------------------------------------
async def create_hold_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    payload: CreateHoldInput,
    *,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    ctx.require("task:write")
    task = await load_task(session, ctx, task_id=payload.task_id)
    outcome = await create_hold(
        session,
        ctx,
        task=task,
        expected_task_version=payload.expected_task_version,
        store_id=payload.store_id,
        service_id=payload.service_id,
        candidate_id=payload.candidate_id,
        start_at=payload.start_at,
        end_at=payload.end_at,
        resource_ids=list(payload.resource_ids),
        quote_token=payload.quote_token,
        now=now,
        clock=clock,
    )
    return {
        "hold_id": str(outcome.hold.id),
        "hold_state": outcome.hold.state,
        "expires_at": outcome.hold.expires_at.isoformat()
        if outcome.hold.expires_at is not None
        else None,
        "proposal_id": str(outcome.proposal.id),
        "proposal_version": outcome.proposal.version,
        "proposal_content_hash": outcome.proposal.content_hash,
        "confirmation_id": str(outcome.confirmation.id),
        "confirmation_token": outcome.confirmation_token,
        "confirmation_expires_at": outcome.confirmation.expires_at.isoformat(),
        # 占位事务会推进任务版本，确认时必须用推进后的版本作为 expected。
        "task_version": outcome.task_version,
        "replayed": outcome.replayed,
        "allocations": [
            {
                "resource_id": str(allocation.resource_id),
                "state": allocation.state,
                "start_at": allocation.start_at.isoformat(),
                "end_at": allocation.end_at.isoformat(),
            }
            for allocation in outcome.allocations
        ],
    }


async def confirm_appointment_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    payload: ConfirmAppointmentInput,
    *,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    ctx.require("appointment:create")
    outcome = await confirm_appointment(
        session,
        ctx,
        proposal_id=payload.proposal_id,
        proposal_version=payload.proposal_version,
        confirmation_token=payload.confirmation_token,
        client_confirmation_event_id=payload.client_confirmation_event_id,
        idempotency_key=payload.idempotency_key,
        expected_task_version=payload.expected_task_version,
        now=now,
        clock=clock,
    )
    return {
        "appointment_id": str(outcome.appointment.id),
        "committed_status": outcome.appointment.status,
        "appointment_version": outcome.appointment.version,
        "task_version": outcome.task_version,
        "replayed": outcome.replayed,
        **_appointment_data(outcome.appointment),
    }


async def reschedule_appointment_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    payload: RescheduleAppointmentInput,
    *,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    ctx.require("appointment:reschedule")
    result = await reschedule_appointment(
        session,
        ctx,
        appointment_id=payload.appointment_id,
        expected_appointment_version=payload.expected_appointment_version,
        new_start_at=payload.new_start_at,
        new_end_at=payload.new_end_at,
        new_resource_ids=list(payload.new_resource_ids),
        quote_token=payload.quote_token,
        idempotency_key=payload.idempotency_key,
        now=now,
        clock=clock,
    )
    return result


async def cancel_appointment_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    payload: CancelAppointmentInput,
    *,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    ctx.require("appointment:cancel")
    return await cancel_appointment(
        session,
        ctx,
        appointment_id=payload.appointment_id,
        expected_appointment_version=payload.expected_appointment_version,
        idempotency_key=payload.idempotency_key,
        reason_code=payload.reason_code,
        now=now,
        clock=clock,
    )


async def transfer_to_human_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    payload: TransferToHumanInput,
    *,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """转人工：建立工单并让任务进入 HUMAN_TAKEOVER。

    接管会提升任务 epoch，自动 Agent 随即失权（设计稿 §5.4）。摘要只保存**引用**
    与必要字段，不把整段对话复制进工单，避免敏感内容扩散。
    """

    ctx.require("task:write")
    decision_now = clock.now() if clock is not None else now
    task = await load_task(session, ctx, task_id=payload.task_id)

    existing = (
        await session.execute(
            select(m.HandoffCase).where(
                m.HandoffCase.tenant_id == ctx.tenant_id,
                m.HandoffCase.task_id == task.id,
                m.HandoffCase.status.in_(["OPEN", "CLAIMED"]),
            )
        )
    ).scalars().first()

    if existing is not None:
        # 幂等：同一任务已有进行中的工单，返回既有工单而不是再开一张。
        case = existing
        created = False
    else:
        if task.state in (
            TaskState.SUCCEEDED.value,
            TaskState.CANCELLED.value,
            TaskState.FAILED.value,
        ):
            raise validation_error("任务已结束，无法转人工")
        case = m.HandoffCase(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            task_id=task.id,
            reason_code=payload.reason_code,
            summary_ref=_summary_ref(ctx=ctx, payload=payload, now=decision_now),
            epoch=task.epoch + 1,
            status="OPEN",
        )
        session.add(case)
        await session.flush()
        created = True

    if task.state != TaskState.HUMAN_TAKEOVER.value:
        task = await set_task_state(
            session,
            ctx,
            task=task,
            target=TaskState.HUMAN_TAKEOVER,
            expected_version=task.version,
            now=decision_now,
            payload={"reason_code": payload.reason_code, "case_id": str(case.id)},
        )
        # 接管提升 epoch：旧 epoch 的 Agent 动作随即被拒绝（设计稿 §5.4）。
        task.epoch = task.epoch + 1
        await session.flush()
        await emit_task_event(
            session,
            task=task,
            event_type=TaskEventType.HANDOFF_CHANGED,
            payload={
                "case_id": str(case.id),
                "handoff_status": case.status,
                "task_epoch": task.epoch,
            },
            occurred_at=decision_now,
        )

    return {
        "case_id": str(case.id),
        "handoff_status": case.status,
        "task_id": str(task.id),
        "task_state": task.state,
        "task_epoch": task.epoch,
        "created": created,
        "urgency": payload.urgency,
    }


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------
def _summary_ref(
    *, ctx: TrustedContext, payload: TransferToHumanInput, now: datetime
) -> str:
    """工单摘要引用。

    只保存"去哪里取摘要"的指针，不内联对话原文：工单系统往往权限比业务库更宽，
    把敏感内容复制过去等于绕过原本的访问控制。
    """

    return f"task:{payload.task_id}#{payload.reason_code}@{now.isoformat()}#req:{ctx.request_id}"[
        :128
    ]


def _appointment_data(appointment: m.Appointment) -> dict[str, Any]:
    return {
        "appointment_id": str(appointment.id),
        "store_id": str(appointment.store_id),
        "authoritative_status": appointment.status,
        "fulfillment_status": appointment.fulfillment_status,
        "version": appointment.version,
        "start_at": appointment.start_at.isoformat(),
        "end_at": appointment.end_at.isoformat(),
        "amount_minor": appointment.amount_minor,
        "currency": appointment.currency,
        "service": appointment.service_snapshot or {},
        "resources": appointment.resource_snapshot or [],
    }


__all__ = [
    "cancel_appointment_tool",
    "confirm_appointment_tool",
    "create_hold_tool",
    "get_appointment_tool",
    "get_service_quote_tool",
    "reschedule_appointment_tool",
    "search_availability_tool",
    "search_knowledge_tool",
    "transfer_to_human_tool",
]
