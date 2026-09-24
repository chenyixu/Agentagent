"""专用确认接口。

设计稿 §8.3：确认是**数据绑定协议**，不是一句"用户说好"。凭据绑定 tenant、
actor、task、proposal_id/version、内容哈希、动作、有效期与 nonce；服务端保存
token 哈希与消费状态，仅有签名而没有重放约束是不够的。

因此这个接口只接受方案级凭据 + 幂等键，不接受"用户已同意"这种自然语言判断。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.enums import ConfirmationStatus, HoldState, ProposalStatus, TaskState
from ...core.enums import ErrorCode
from ...core.errors import DomainError
from ...db import models as m
from ...db.session import live_now
from ...domain.booking import confirmation_token_for, confirm_appointment
from ...domain.context import TrustedContext
from ..deps import get_identity, get_session
from ..schemas import ConfirmResponse, ConfirmationSubmit, PendingConfirmation
from ..sse import latest_sequence

router = APIRouter(prefix="/v1", tags=["confirmations"])


@router.post(
    "/tasks/{task_id}/confirmation-credential",
    response_model=PendingConfirmation,
)
async def refresh_confirmation_credential(
    task_id: UUID,
    response: Response,
    ctx: TrustedContext = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
) -> PendingConfirmation:
    """给已授权的顾客恢复待确认卡凭据；本接口不提交预约。

    凭据不进入任务快照或事件流。页面重载后，客户可在重新查看方案后取回同一凭据，
    再通过专用确认接口显式确认。只允许当前仍有效的方案、占位和未消费凭据。
    """

    ctx.require("task:write")
    response.headers["Cache-Control"] = "no-store"
    now = await live_now(session)
    task = (
        await session.execute(
            select(m.Task)
            .where(m.Task.tenant_id == ctx.tenant_id, m.Task.id == task_id)
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if (
        task is None
        or (ctx.role == "customer" and task.customer_id != ctx.customer_id)
        or task.state != TaskState.WAITING_CONFIRMATION.value
        or task.current_proposal_id is None
        or task.current_proposal_version is None
    ):
        raise DomainError(ErrorCode.CONFIRMATION_REQUIRED, "当前任务没有可恢复的待确认方案")

    proposal = (
        await session.execute(
            select(m.Proposal).where(
                m.Proposal.tenant_id == ctx.tenant_id,
                m.Proposal.id == task.current_proposal_id,
                m.Proposal.version == task.current_proposal_version,
                m.Proposal.task_id == task.id,
                m.Proposal.status == ProposalStatus.ACTIVE.value,
                m.Proposal.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise DomainError(ErrorCode.CONFIRMATION_REQUIRED, "待确认方案已失效")

    confirmation = (
        await session.execute(
            select(m.Confirmation).where(
                m.Confirmation.tenant_id == ctx.tenant_id,
                m.Confirmation.task_id == task.id,
                m.Confirmation.actor_id == ctx.actor_id,
                m.Confirmation.proposal_id == proposal.id,
                m.Confirmation.proposal_version == proposal.version,
                m.Confirmation.status == ConfirmationStatus.ISSUED.value,
                m.Confirmation.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    hold = (
        await session.execute(
            select(m.Hold).where(
                m.Hold.tenant_id == ctx.tenant_id,
                m.Hold.id == proposal.hold_id,
                m.Hold.task_id == task.id,
                m.Hold.state == HoldState.HELD.value,
                m.Hold.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    if confirmation is None or hold is None:
        raise DomainError(ErrorCode.CONFIRMATION_REQUIRED, "确认凭据或预约占位已失效")

    await session.commit()
    return PendingConfirmation(
        proposal_id=str(proposal.id),
        proposal_version=proposal.version,
        proposal_content_hash=proposal.content_hash,
        confirmation_token=confirmation_token_for(confirmation),
        expires_at=proposal.expires_at.isoformat(),
        hold_id=str(hold.id),
        hold_expires_at=hold.expires_at.isoformat(),
        expected_task_version=task.version,
    )


@router.post("/confirmations", response_model=ConfirmResponse)
async def submit_confirmation(
    payload: ConfirmationSubmit,
    ctx: TrustedContext = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
) -> ConfirmResponse:
    """用方案级凭据提交预约。

    幂等由 ``idempotency_key`` 保证：同键同参数重放返回原订单，同键不同参数报
    ``IDEMPOTENCY_MISMATCH``（设计稿 §8.2）。

    ``now`` 必须来自数据库时钟（``live_now``）：凭据有效期、占位期限、价目版本
    生效窗口都按它裁决，用进程时间会在长事务后误判。
    """

    outcome = await confirm_appointment(
        session,
        ctx,
        proposal_id=payload.proposal_id,
        proposal_version=payload.proposal_version,
        confirmation_token=payload.confirmation_token,
        client_confirmation_event_id=payload.client_confirmation_event_id,
        idempotency_key=payload.idempotency_key,
        expected_task_version=payload.expected_task_version,
        now=await live_now(session),
    )
    task_id, task_state, task_version = await _task_view(
        session, ctx, appointment_id=outcome.appointment.id
    )
    event_cursor = await latest_sequence(
        session, tenant_id=ctx.tenant_id, task_id=task_id
    )
    await session.commit()

    # 操作 ID 直接取本次确认实际使用的那个：反查是多余的，反查不到还会让客户端
    # 拿不到"结果不明时该查哪一笔操作"的唯一线索。
    return ConfirmResponse(
        appointment_id=str(outcome.appointment.id),
        committed_status=outcome.appointment.status,
        task_state=task_state,
        task_version=task_version,
        operation_id=str(outcome.operation.id),
        replayed=outcome.replayed,
        event_cursor=event_cursor,
    )


async def _task_view(session, ctx, *, appointment_id):
    """订单反查所属任务。

    查不到不要编一个状态出来：刚提交成功的订单必然挂在某条 hold 上，
    查不到说明数据不一致，按 NOT_FOUND 明确失败比返回"看起来成功"的状态安全。
    """

    row = (
        await session.execute(
            select(m.Task.id, m.Task.state, m.Task.version)
            .join(
                m.Hold,
                (m.Hold.tenant_id == m.Task.tenant_id)
                & (m.Hold.task_id == m.Task.id),
            )
            .where(
                m.Hold.tenant_id == ctx.tenant_id,
                m.Hold.booked_appointment_id == appointment_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "订单未关联到可访问的任务")
    return row.id, row.state, row.version
