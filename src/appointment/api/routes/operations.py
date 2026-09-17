"""操作查询接口。

设计稿 §8.2 / 数据契约 §2.3：**提交结果不明时先查原操作**，不盲目换键重试。
查询读权威主库，查不到时先确认查询成功与完整作用域，不能从超时或副本滞后
推断"没有下单"。

优先按 ``operation_id`` 查；也允许按原作用域幂等键查（``action`` +
``idempotency_key``），因为响应丢失时客户端手里可能只有原键。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.enums import OperationStatus, ProposalAction
from ...domain.context import TrustedContext
from ...domain.operation import load_operation_by_id, load_operation_by_key
from ..deps import get_identity, get_session
from ..schemas import OperationResponse

router = APIRouter(prefix="/v1", tags=["operations"])

#: 未完成时建议客户端多久后再查。查询本身没有副作用，间隔只影响负载。
NEXT_POLL_SECONDS = 2


@router.get("/operations/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: UUID,
    ctx: TrustedContext = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
) -> OperationResponse:
    ctx.require("task:read")
    row = await load_operation_by_id(
        session, tenant_id=ctx.tenant_id, operation_id=operation_id
    )
    await session.commit()
    return _view(row)


@router.get("/operations", response_model=OperationResponse)
async def get_operation_by_key(
    action: ProposalAction = Query(...),
    idempotency_key: str = Query(..., min_length=1, max_length=128),
    ctx: TrustedContext = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
) -> OperationResponse:
    """按原作用域幂等键查。找不到时返回 NOT_FOUND 的 404，而不是空对象。"""

    ctx.require("task:read")
    row = await load_operation_by_key(
        session,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        action=action,
        idempotency_key=idempotency_key,
    )
    await session.commit()
    return _view(row)


def _view(row) -> OperationResponse:
    if row is None:
        from ...core.errors import not_found

        raise not_found("操作不存在或无权访问")
    pending = row.status in (
        OperationStatus.REGISTERED.value,
        OperationStatus.RUNNING.value,
        OperationStatus.UNKNOWN.value,
    )
    result = dict(row.result or {}) if row.result else None
    return OperationResponse(
        operation_id=str(row.id),
        status=row.status,
        action=row.action,
        result=result,
        error_code=row.error_code,
        appointment_id=(
            None
            if row.target_appointment_id is None
            else str(row.target_appointment_id)
        ),
        next_poll_after_seconds=NEXT_POLL_SECONDS if pending else None,
    )
