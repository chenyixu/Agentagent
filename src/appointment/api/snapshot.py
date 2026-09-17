"""任务快照。

"断开连接不等于业务取消"要能成立，客户端必须能随时重新拉一份**授权范围内**的
任务快照，再按游标重新订阅（数据契约 §2.3：保留窗口外必须能重建）。

快照只包含业务字段：不返回模型思考、不返回原始工具输出、不返回任何凭据
（确认凭据只在签发它的那次响应里交给客户端）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import TaskState, WaitingStatus
from ..core.errors import ErrorCode
from ..db import models as m
from ..domain.context import TrustedContext


async def build_task_snapshot(
    session: AsyncSession, ctx: TrustedContext, *, task_id: UUID
) -> dict[str, Any]:
    ctx.require("task:read")
    task = (
        await session.execute(
            select(m.Task).where(
                m.Task.tenant_id == ctx.tenant_id, m.Task.id == task_id
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise _not_found()
    if ctx.role == "customer" and task.customer_id != ctx.customer_id:
        raise _not_found()

    waiting = (
        await session.execute(
            select(m.WaitingRequest)
            .where(
                m.WaitingRequest.tenant_id == ctx.tenant_id,
                m.WaitingRequest.task_id == task.id,
                m.WaitingRequest.status == WaitingStatus.OPEN.value,
            )
            .order_by(m.WaitingRequest.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    proposal = None
    if task.current_proposal_id is not None and task.current_proposal_version is not None:
        proposal = (
            await session.execute(
                select(m.Proposal).where(
                    m.Proposal.tenant_id == ctx.tenant_id,
                    m.Proposal.id == task.current_proposal_id,
                    m.Proposal.version == task.current_proposal_version,
                )
            )
        ).scalar_one_or_none()

    hold = (
        await session.execute(
            select(m.Hold)
            .where(
                m.Hold.tenant_id == ctx.tenant_id,
                m.Hold.task_id == task.id,
                m.Hold.state.in_(["HELD", "BOOKED"]),
            )
            .order_by(m.Hold.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    appointment = None
    if hold is not None and hold.booked_appointment_id is not None:
        appointment = (
            await session.execute(
                select(m.Appointment).where(
                    m.Appointment.tenant_id == ctx.tenant_id,
                    m.Appointment.id == hold.booked_appointment_id,
                )
            )
        ).scalar_one_or_none()

    return {
        "task_id": str(task.id),
        "conversation_id": str(task.conversation_id),
        "state": task.state,
        "version": task.version,
        "epoch": task.epoch,
        "goal": task.goal,
        "store_id": None if task.store_id is None else str(task.store_id),
        "slots": _public_slots(task.slots or {}),
        "waiting": None if waiting is None else _waiting_view(waiting),
        "proposal": None if proposal is None else _proposal_view(proposal),
        "hold": None if hold is None else _hold_view(hold),
        "appointment": None
        if appointment is None
        else _appointment_view(appointment),
        "terminal": task.state in _TERMINAL_OR_PARKED,
    }


#: 这些状态表示"当前没有活跃执行在跑"，客户端可以安全地停下来不再跟随。
_TERMINAL_OR_PARKED = frozenset(
    {
        TaskState.SUCCEEDED.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
        TaskState.WAITING_USER.value,
        TaskState.WAITING_CONFIRMATION.value,
        TaskState.WAITING_EXTERNAL.value,
        TaskState.WAITING_RESULT.value,
        TaskState.HUMAN_TAKEOVER.value,
    }
)


def _public_slots(slots: dict[str, Any]) -> dict[str, Any]:
    """槽位对外只给值与解析状态：来源消息 ID 属于内部审计线索。"""

    public: dict[str, Any] = {}
    for name, entry in slots.items():
        if not isinstance(entry, dict):
            public[name] = {"value": entry}
            continue
        public[name] = {
            "value": entry.get("value"),
            "resolution_status": entry.get("resolution_status"),
            "intent": entry.get("intent"),
            "updated_at": entry.get("updated_at"),
        }
    return public


def _waiting_view(row: m.WaitingRequest) -> dict[str, Any]:
    return {
        "waiting_id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "question": row.question_text,
        "task_version": row.task_version,
        "expires_at": _iso(row.expires_at),
        "input_schema": dict(row.input_schema or {}),
    }


def _proposal_view(row: m.Proposal) -> dict[str, Any]:
    return {
        "proposal_id": str(row.id),
        "version": row.version,
        "action": row.action,
        "status": row.status,
        "expires_at": _iso(row.expires_at),
        "content_hash": row.content_hash,
        "content": dict(row.canonical_content or {}),
    }


def _hold_view(row: m.Hold) -> dict[str, Any]:
    return {
        "hold_id": str(row.id),
        "state": row.state,
        "expires_at": _iso(row.expires_at),
        "action": row.action,
        "booked_appointment_id": (
            None if row.booked_appointment_id is None else str(row.booked_appointment_id)
        ),
    }


def _appointment_view(row: m.Appointment) -> dict[str, Any]:
    return {
        "appointment_id": str(row.id),
        "status": row.status,
        "fulfillment_status": row.fulfillment_status,
        "version": row.version,
        "start_at": _iso(row.start_at),
        "end_at": _iso(row.end_at),
        "amount_minor": row.amount_minor,
        "currency": row.currency,
        "service_snapshot": dict(row.service_snapshot or {}),
    }


def _iso(value) -> str | None:
    return None if value is None else value.isoformat()


def _not_found():
    from ..core.errors import not_found

    return not_found("任务不存在或无权访问")


__all__ = ["build_task_snapshot"]
