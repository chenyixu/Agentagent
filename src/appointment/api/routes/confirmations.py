"""专用确认接口。

设计稿 §8.3：确认是**数据绑定协议**，不是一句"用户说好"。凭据绑定 tenant、
actor、task、proposal_id/version、内容哈希、动作、有效期与 nonce；服务端保存
token 哈希与消费状态，仅有签名而没有重放约束是不够的。

因此这个接口只接受方案级凭据 + 幂等键，不接受"用户已同意"这种自然语言判断。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.enums import ErrorCode
from ...core.errors import DomainError
from ...db import models as m
from ...db.session import live_now
from ...domain.booking import confirm_appointment
from ...domain.context import TrustedContext
from ..deps import get_identity, get_session
from ..schemas import ConfirmResponse, ConfirmationSubmit
from ..sse import latest_sequence

router = APIRouter(prefix="/v1", tags=["confirmations"])


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
