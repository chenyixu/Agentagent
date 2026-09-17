"""会话、消息与任务的受理（设计稿 §5.1、§6.3，数据契约 §4）。

消息去重与业务效果去重是两件事：这里做的是"同一 message_id 不重复运行"，
业务效果去重在 :mod:`appointment.domain.operation`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import ErrorCode, ResolutionStatus, SlotIntent, SlotOp, TaskEventType, TaskState
from ..core.errors import DomainError, not_found, validation_error, version_conflict
from ..core.hashing import content_hash
from ..db import models as m
from ..db.session import LockSequencer, lock_task
from .context import TrustedContext
from .events import emit_task_event, record_audit

#: 允许的槽位名。不允许任意对象路径。
ALLOWED_SLOTS = frozenset(
    {
        "store",
        "service",
        "time_window",
        "resource_preference",
        "budget",
        "duration",
    }
)


@dataclass(slots=True)
class MessageOutcome:
    message: m.Message
    deduplicated: bool


@dataclass(slots=True)
class TaskOutcome:
    task: m.Task
    created: bool


async def ensure_conversation(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    store_id: UUID | None,
    conversation_id: UUID | None = None,
    now: datetime,
) -> m.Conversation:
    """取得或创建会话。首版不把任意 actor 加入会话。

    消息必须落回**同一个**会话，否则同一客户的下一条消息会开一个新任务：
    "第一个可以"这种答复就再也接不上刚才给出的候选。因此未显式给出会话 ID 时，
    按 (客户, 门店, 未关闭) 复用最近的开放会话；只有确实没有时才新建。
    """

    customer_id = ctx.require_customer()
    if conversation_id is not None:
        row = (
            await session.execute(
                select(m.Conversation).where(
                    m.Conversation.tenant_id == ctx.tenant_id,
                    m.Conversation.id == conversation_id,
                    m.Conversation.customer_id == customer_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise not_found("会话不存在或无权访问")
        return row

    existing = (
        await session.execute(
            select(m.Conversation)
            .where(
                m.Conversation.tenant_id == ctx.tenant_id,
                m.Conversation.customer_id == customer_id,
                m.Conversation.status == "OPEN",
                # 门店维度参与匹配：不能把 A 店会话上的答复当成 B 店的。
                m.Conversation.store_id.is_not_distinct_from(store_id),
            )
            .order_by(m.Conversation.created_at.desc())
        )
    ).scalars().first()
    if existing is not None:
        return existing

    row = m.Conversation(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        customer_id=customer_id,
        store_id=store_id,
        status="OPEN",
    )
    session.add(row)
    await session.flush()
    return row


async def append_message(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    conversation_id: UUID,
    client_message_id: str,
    content: str,
    role: str,
    now: datetime,
) -> MessageOutcome:
    """写入消息并在同一事务内做去重。

    同键异内容返回 ``IDEMPOTENCY_MISMATCH``；同键同内容返回原消息。
    """

    if not content or not content.strip():
        raise validation_error("消息内容不能为空")
    if len(content) > 8000:
        raise validation_error("消息内容超过长度上限")

    content_hash_value = content_hash({"content": content.strip()})
    existing = (
        await session.execute(
            select(m.Message).where(
                m.Message.tenant_id == ctx.tenant_id,
                m.Message.conversation_id == conversation_id,
                m.Message.client_message_id == client_message_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash != content_hash_value:
            raise DomainError(
                ErrorCode.IDEMPOTENCY_MISMATCH,
                "同一客户端消息 ID 使用了不同内容",
            )
        return MessageOutcome(message=existing, deduplicated=True)

    # 单调 sequence 在会话锁内分配。
    conversation = (
        await session.execute(
            select(m.Conversation)
            .where(
                m.Conversation.tenant_id == ctx.tenant_id,
                m.Conversation.id == conversation_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise not_found("会话不存在或无权访问")

    sequence = conversation.next_message_sequence
    conversation.next_message_sequence = sequence + 1

    message = m.Message(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        conversation_id=conversation_id,
        actor_id=ctx.actor_id,
        client_message_id=client_message_id,
        sequence=sequence,
        role=role,
        content_text=content.strip(),
        content_hash=content_hash_value,
        received_at=now,
    )
    session.add(message)
    await session.flush()
    return MessageOutcome(message=message, deduplicated=False)


async def get_or_create_task(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    conversation: m.Conversation,
    release_id: str,
    goal: str = "book",
    store_id: UUID | None = None,
    now: datetime,
) -> TaskOutcome:
    """一个会话同时只保留一个活跃任务；终态任务不阻止新任务。

    "取消"在未下单草稿、占位、已确认订单三个阶段含义不同，因此这里只按
    任务终态判断是否复用，不根据文案猜测。
    """

    active = (
        await session.execute(
            select(m.Task)
            .where(
                m.Task.tenant_id == ctx.tenant_id,
                m.Task.conversation_id == conversation.id,
                m.Task.state.notin_(
                    [TaskState.SUCCEEDED.value, TaskState.CANCELLED.value]
                ),
            )
            .order_by(m.Task.created_at.desc())
        )
    ).scalars().first()
    if active is not None:
        return TaskOutcome(task=active, created=False)

    task = m.Task(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        conversation_id=conversation.id,
        customer_id=ctx.require_customer(),
        store_id=store_id or conversation.store_id,
        goal=goal,
        state=TaskState.COLLECTING.value,
        slots={},
        release_id=release_id,
    )
    session.add(task)
    await session.flush()
    return TaskOutcome(task=task, created=True)


async def load_task(
    session: AsyncSession, ctx: TrustedContext, *, task_id: UUID
) -> m.Task:
    task = (
        await session.execute(
            select(m.Task).where(
                m.Task.tenant_id == ctx.tenant_id, m.Task.id == task_id
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise not_found("任务不存在或无权访问")
    if ctx.role == "customer" and task.customer_id != ctx.customer_id:
        raise not_found("任务不存在或无权访问")
    return task


def apply_slot_patches(
    *,
    slots: Mapping[str, Any],
    patches: Sequence[Mapping[str, Any]],
    allowed_evidence_message_ids: set[str],
    now: datetime,
) -> dict[str, Any]:
    """把模型的槽位 Patch 合并进结构化槽位。

    规则（设计稿 §2.1、§6.3）：

    - 未出现该槽位表示不修改；
    - ``SET`` 必须有 value；``CLEAR`` 是明确撤销；``NO_PREFERENCE`` 是"交给系统选"，
      不是缺失；
    - 歧义时间保留原文与候选，不覆盖已确认值；
    - ``evidence_message_id`` 必须是本任务有权读取的消息。
    """

    updated = {key: dict(value) if isinstance(value, Mapping) else value for key, value in slots.items()}
    for patch in patches:
        name = patch.get("slot_name")
        if name not in ALLOWED_SLOTS:
            raise validation_error(f"未知槽位：{name!r}")
        op = patch.get("op")
        if op not in (SlotOp.SET.value, SlotOp.CLEAR.value, SlotOp.NO_PREFERENCE.value):
            raise validation_error(f"非法槽位操作：{op!r}")
        evidence = patch.get("evidence_message_id")
        if evidence is not None and str(evidence) not in allowed_evidence_message_ids:
            raise DomainError(
                ErrorCode.PERMISSION_DENIED, "槽位证据引用了无权读取的消息"
            )

        if op == SlotOp.SET.value:
            if "value" not in patch or patch["value"] is None:
                raise validation_error(f"SET 操作必须携带 value：{name}")
            resolution = patch.get("resolution_status", ResolutionStatus.RESOLVED.value)
            if resolution not in tuple(r.value for r in ResolutionStatus):
                raise validation_error(f"非法 resolution_status：{resolution!r}")
            entry: dict[str, Any] = {
                "value": patch["value"],
                "source_message_id": evidence,
                "resolution_status": resolution,
                "intent": SlotIntent.VALUE.value,
                "updated_at": now.isoformat(),
            }
            if resolution != ResolutionStatus.RESOLVED.value:
                entry["original_text"] = patch.get("original_text")
                entry["candidates"] = list(patch.get("candidates") or [])
            updated[name] = entry
        elif op == SlotOp.CLEAR.value:
            updated.pop(name, None)
            updated[name] = {
                "value": None,
                "source_message_id": evidence,
                "resolution_status": ResolutionStatus.UNRESOLVED.value,
                "intent": SlotIntent.CLEARED.value,
                "updated_at": now.isoformat(),
            }
        else:
            updated[name] = {
                "value": None,
                "source_message_id": evidence,
                "resolution_status": ResolutionStatus.UNRESOLVED.value,
                "intent": SlotIntent.NO_PREFERENCE.value,
                "updated_at": now.isoformat(),
            }
    return updated


async def update_slots(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    task: m.Task,
    patches: Sequence[Mapping[str, Any]],
    allowed_evidence_message_ids: set[str],
    expected_task_version: int,
    now: datetime,
) -> m.Task:
    """写槽位并推进任务版本。

    每个被接受的状态变更推进任务版本（设计稿 §5.1 第 5 步）。
    """

    sequencer = LockSequencer()
    locked = await lock_task(
        session, sequencer, tenant_id=ctx.tenant_id, task_id=task.id
    )
    if locked.version != expected_task_version:
        raise version_conflict(
            "任务已被更新，请基于最新状态重试",
            expected_version=expected_task_version,
            actual_version=locked.version,
        )
    merged = apply_slot_patches(
        slots=locked.slots or {},
        patches=patches,
        allowed_evidence_message_ids=allowed_evidence_message_ids,
        now=now,
    )
    if merged != (locked.slots or {}):
        locked.slots = merged
        locked.version = locked.version + 1
        await emit_task_event(
            session,
            task=locked,
            event_type=TaskEventType.SLOTS_UPDATED,
            payload={
                "slots": merged,
                "changed": [p.get("slot_name") for p in patches],
            },
            occurred_at=now,
        )
    return locked


async def set_task_state(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    task: m.Task,
    target: TaskState,
    expected_version: int,
    now: datetime,
    payload: dict[str, Any] | None = None,
) -> m.Task:
    """受控的任务状态写入。

    非法迁移在这里被拒绝；普通消息修改意图增加 version，控制权变化才增加 epoch。
    """

    from . import state_machine

    sequencer = LockSequencer()
    locked = await lock_task(
        session, sequencer, tenant_id=ctx.tenant_id, task_id=task.id
    )
    if locked.version != expected_version:
        raise version_conflict(
            "任务已被更新，请基于最新状态重试",
            expected_version=expected_version,
            actual_version=locked.version,
        )
    state_machine.assert_transition(TaskState(locked.state), target)
    previous = locked.state
    locked.state = target.value
    locked.version = locked.version + 1
    await emit_task_event(
        session,
        task=locked,
        event_type=TaskEventType.TASK_STATE_CHANGED,
        payload={"from": previous, "to": target.value, **(payload or {})},
        occurred_at=now,
    )
    await record_audit(
        session,
        ctx,
        action="task_state_changed",
        object_type="task",
        object_id=locked.id,
        occurred_at=now,
        before_version=expected_version,
        after_version=locked.version,
        reason_code=target.value,
    )
    return locked


def visible_message_ids(messages: Sequence[m.Message], *, customer_id: UUID) -> set[str]:
    """本任务有权读取的消息 ID。模型不能凭空提供其他用户的消息。"""

    return {str(message.id) for message in messages}


__all__ = [
    "ALLOWED_SLOTS",
    "MessageOutcome",
    "TaskOutcome",
    "append_message",
    "apply_slot_patches",
    "ensure_conversation",
    "get_or_create_task",
    "load_task",
    "set_task_state",
    "update_slots",
    "visible_message_ids",
]
