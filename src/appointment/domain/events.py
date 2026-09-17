"""领域事件、Outbox 与审计的写入。

设计稿 §9：appointment_committed、proposal_invalidated 等业务事件由已提交事务
产生，不能由 ReplyEndEvent 推断。事件 ID 与 SDK reply_id 分开，task_event 是
重连依据。

Outbox 与订单同事务写入，Worker 至少一次处理；消费只代表逻辑投递已持久受理。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import TaskEventType
from ..core.hashing import content_hash
from ..db import models as m
from .context import TrustedContext


async def emit_task_event(
    session: AsyncSession,
    *,
    task: m.Task,
    event_type: TaskEventType,
    payload: dict[str, Any],
    occurred_at: datetime,
) -> m.TaskEvent:
    """在会话锁内分配单调 sequence 并写入事件。

    调用方必须已锁住 task 行（``SELECT ... FOR UPDATE``），否则并发轮次可能
    分配到相同 sequence。

    序号取"计数器"与"已落库事实"的较大者。``task.next_event_sequence`` 只是
    缓存，唯一约束认的是 ``task_event`` 里已经写进去的行。两者一旦不一致
    （历史数据、中断的写入、人工修补），只信计数器会让**每一次** emit 都撞唯一
    约束——一个无法自愈的硬失败，且失败发生在事务里，会把会话打成待回滚状态，
    连错误信息都变得不可读。这里以事实为准：多付一次索引扫描，换回自愈能力。

    本会话内 autoflush 关闭，所以同轮先前 pending 的事件不会被这次查询看到；
    但那时 ``task.next_event_sequence`` 已经领先于它，``max`` 仍然给出正确值。
    """

    latest_committed = await latest_task_event_sequence(
        session, tenant_id=task.tenant_id, task_id=task.id
    )
    sequence = max(task.next_event_sequence, latest_committed + 1)
    event = m.TaskEvent(
        id=_new_uuid(),
        tenant_id=task.tenant_id,
        task_id=task.id,
        sequence=sequence,
        task_version=task.version,
        type=event_type.value,
        payload=payload,
        occurred_at=occurred_at,
    )
    session.add(event)
    task.next_event_sequence = sequence + 1
    return event


async def emit_outbox(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, Any],
    occurred_at: datetime,
    event_key: str = "",
    available_at: datetime | None = None,
) -> m.Outbox:
    row = m.Outbox(
        id=_new_uuid(),
        tenant_id=tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        event_key=event_key,
        payload=payload,
        occurred_at=occurred_at,
        available_at=available_at or occurred_at,
    )
    session.add(row)
    return row


async def record_audit(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    action: str,
    object_type: str,
    object_id: str | UUID,
    occurred_at: datetime,
    before_version: int | None = None,
    after_version: int | None = None,
    operation_id: UUID | None = None,
    confirmation_id: UUID | None = None,
    reason_code: str | None = None,
    protected_detail: dict[str, Any] | None = None,
) -> m.AuditEvent:
    """审计不可变。不得写入裸 token 或未脱敏手机号。"""

    row = m.AuditEvent(
        id=_new_uuid(),
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        action=action,
        object_type=object_type,
        object_id=str(object_id),
        before_version=before_version,
        after_version=after_version,
        operation_id=operation_id,
        confirmation_id=confirmation_id,
        request_id=ctx.request_id,
        reason_code=reason_code,
        protected_detail=protected_detail,
    )
    session.add(row)
    return row


async def latest_task_event_sequence(
    session: AsyncSession, *, tenant_id: UUID, task_id: UUID
) -> int:
    row = (
        await session.execute(
            select(m.TaskEvent.sequence)
            .where(m.TaskEvent.tenant_id == tenant_id, m.TaskEvent.task_id == task_id)
            .order_by(m.TaskEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(row or 0)


def _new_uuid() -> UUID:
    from ..core.ids import new_id

    return new_id()


def idempotency_fingerprint(payload: dict[str, Any]) -> str:
    return content_hash(payload)
