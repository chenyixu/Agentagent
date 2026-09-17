"""幂等操作账本（设计稿 §8.2、数据契约 §5.2）。

区分两类去重：

- **消息去重**：防同一 message_id 重复运行（``message`` 表唯一约束）。
- **业务效果去重**：防模型重复调用和网络重试产生多个订单（本模块）。

业务幂等键由应用为一次用户确认分配，例如 tenant + task + proposal_version +
action。客户端重试、模型修复、执行器接管必须复用同一键，不能每次生成新 UUID。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import OperationStatus, ProposalAction
from ..core.errors import DomainError, ErrorCode, idempotency_mismatch, version_conflict
from ..core.hashing import content_hash
from ..db import models as m
from ..db.session import LockSequencer
from .context import TrustedContext


@dataclass(slots=True)
class OperationHandle:
    """一次幂等命令的句柄。"""

    operation: m.Operation
    created: bool

    @property
    def is_replay(self) -> bool:
        return not self.created

    @property
    def finished(self) -> bool:
        return self.operation.status == OperationStatus.SUCCEEDED.value


async def register_operation(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    action: ProposalAction,
    idempotency_key: str,
    request_payload: dict[str, Any],
    now: datetime,
    task_id: UUID | None = None,
    customer_id: UUID | None = None,
    target_appointment_id: UUID | None = None,
    proposal_id: UUID | None = None,
    proposal_version: int | None = None,
    confirmation_id: UUID | None = None,
) -> OperationHandle:
    """登记或取回原操作。

    - 相同键同参数：返回原操作（含已成功结果），由调用方原样重放。
    - 相同键不同参数：``IDEMPOTENCY_MISMATCH``。
    - 提供了 ``confirmation_id`` 时，**凭据本身也是去重依据**：同一次授权即使换了
      幂等键也必须返回原操作。operation 表上有 ``UNIQUE(tenant_id, confirmation_id)``，
      如果只按键查找就会去 INSERT，撞上唯一约束变成一个 500；而"同一次授权产生两笔
      订单"是这里绝对不能出现的结果，所以先按凭据取回，让调用方重放原订单。
    """

    request_hash = content_hash(request_payload)
    existing = (
        await session.execute(
            select(m.Operation).where(
                m.Operation.tenant_id == ctx.tenant_id,
                m.Operation.actor_id == ctx.actor_id,
                m.Operation.action == action.value,
                m.Operation.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()

    if existing is None and confirmation_id is not None:
        existing = (
            await session.execute(
                select(m.Operation).where(
                    m.Operation.tenant_id == ctx.tenant_id,
                    m.Operation.actor_id == ctx.actor_id,
                    m.Operation.action == action.value,
                    m.Operation.confirmation_id == confirmation_id,
                )
            )
        ).scalar_one_or_none()

    if existing is not None:
        # 只有"同一个键"才谈得上参数冲突；凭据相同而键不同是重放，不是冲突。
        if (
            existing.idempotency_key == idempotency_key
            and existing.request_hash != request_hash
        ):
            raise idempotency_mismatch(
                "同一幂等键被用于不同参数；请复用原请求或使用新键"
            )
        return OperationHandle(operation=existing, created=False)

    from ..core.ids import new_id

    operation = m.Operation(
        id=new_id(),
        tenant_id=ctx.tenant_id,
        task_id=task_id,
        customer_id=customer_id,
        actor_id=ctx.actor_id,
        action=action.value,
        target_appointment_id=target_appointment_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        proposal_id=proposal_id,
        proposal_version=proposal_version,
        confirmation_id=confirmation_id,
        status=OperationStatus.REGISTERED.value,
        started_at=now,
    )
    session.add(operation)
    await session.flush()
    return OperationHandle(operation=operation, created=True)


def replay_result(handle: OperationHandle) -> dict[str, Any]:
    """返回已成功操作的原结果。

    先验证当前身份与结果读取权限（由调用方保证），不因为已消费/已过期 token
    重建一笔订单。
    """

    operation = handle.operation
    if operation.status == OperationStatus.SUCCEEDED.value:
        return dict(operation.result or {})
    if operation.status == OperationStatus.UNKNOWN.value:
        raise DomainError(
            ErrorCode.UNKNOWN_OUTCOME,
            "原操作结果不明，请先查询该操作而不是重新提交",
            operation_id=str(operation.id),
        )
    raise version_conflict(
        f"原操作状态为 {operation.status}，不能直接重放",
        operation_id=str(operation.id),
    )


async def mark_running(
    session: AsyncSession,
    handle: OperationHandle,
    *,
    lease_owner: str,
    lease_until: datetime,
    fencing_token: int,
) -> None:
    operation = handle.operation
    if operation.status not in (
        OperationStatus.REGISTERED.value,
        OperationStatus.RUNNING.value,
    ):
        raise version_conflict(
            f"操作已处于终态 {operation.status}", operation_id=str(operation.id)
        )
    operation.status = OperationStatus.RUNNING.value
    operation.lease_owner = lease_owner
    operation.lease_until = lease_until
    operation.fencing_token = fencing_token


async def mark_succeeded(
    session: AsyncSession,
    operation: m.Operation,
    *,
    result: dict[str, Any],
    now: datetime,
) -> None:
    operation.status = OperationStatus.SUCCEEDED.value
    operation.result = result
    operation.error_code = None
    operation.completed_at = now
    operation.lease_owner = None
    operation.lease_until = None


async def mark_failed(
    session: AsyncSession,
    operation: m.Operation,
    *,
    error_code: ErrorCode,
    now: datetime,
    message: str | None = None,
) -> None:
    operation.status = OperationStatus.FAILED_FINAL.value
    operation.error_code = error_code.value
    operation.completed_at = now
    operation.lease_owner = None
    operation.lease_until = None
    if message:
        operation.result = {"error_message": message}


async def mark_unknown(
    session: AsyncSession,
    operation: m.Operation,
    *,
    message: str,
    now: datetime,
) -> None:
    """写入结果不明。

    不把一次数据库暂时不可用记成永久业务失败；保留原键并先查主库。
    """

    operation.status = OperationStatus.UNKNOWN.value
    operation.error_code = ErrorCode.UNKNOWN_OUTCOME.value
    operation.result = {"error_message": message}
    operation.completed_at = now


async def load_operation_by_id(
    session: AsyncSession, *, tenant_id: UUID, operation_id: UUID
) -> m.Operation | None:
    return (
        await session.execute(
            select(m.Operation).where(
                m.Operation.tenant_id == tenant_id, m.Operation.id == operation_id
            )
        )
    ).scalar_one_or_none()


async def load_operation_by_key(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    action: ProposalAction,
    idempotency_key: str,
) -> m.Operation | None:
    return (
        await session.execute(
            select(m.Operation).where(
                m.Operation.tenant_id == tenant_id,
                m.Operation.actor_id == actor_id,
                m.Operation.action == action.value,
                m.Operation.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


# 供锁顺序文档引用。
__all__ = [
    "LockSequencer",
    "OperationHandle",
    "load_operation_by_id",
    "load_operation_by_key",
    "mark_failed",
    "mark_running",
    "mark_succeeded",
    "mark_unknown",
    "register_operation",
    "replay_result",
]
