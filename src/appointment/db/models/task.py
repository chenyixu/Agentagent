"""会话、任务与恢复表（数据契约 §4、设计稿 §5.4）。

恢复的唯一权威是业务账本：task、waiting_request、operation 与已提交订单共同
决定恢复；SDK reply/AgentState 只是一次执行尝试的临时状态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ...core.enums import (
    AgentRole,
    ExecutionEndReason,
    HandoffStatus,
    TaskEventType,
    TaskState,
    WaitingKind,
    WaitingStatus,
)
from ..base import (
    Base,
    CreatedAtMixin,
    TimestampMixin,
    VersionMixin,
    enum_check,
    non_negative,
    positive,
)

_TASK_STATE_VALUES = tuple(TaskState)
_WAITING_KIND_VALUES = tuple(WaitingKind)
_WAITING_STATUS_VALUES = tuple(WaitingStatus)
_EVENT_TYPE_VALUES = tuple(TaskEventType)


# ---------------------------------------------------------------------------
# 会话与消息
# ---------------------------------------------------------------------------


class Conversation(Base, TimestampMixin, VersionMixin):
    """首版不把任意 actor 加入会话；多参与者场景另建成员表。"""

    __tablename__ = "conversation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"], ["customer.tenant_id", "customer.id"]
        ),
        Index("ix_conversation_customer", "tenant_id", "customer_id", "updated_at"),
        CheckConstraint("status IN ('OPEN', 'CLOSED')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    customer_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="OPEN")
    #: 单调 sequence 在会话锁内分配。
    next_message_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )


class Message(Base, CreatedAtMixin):
    """消息去重：UNIQUE(tenant_id, conversation_id, client_message_id)。

    同键异内容返回 IDEMPOTENCY_MISMATCH。
    """

    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "conversation_id", "client_message_id"
        ),
        UniqueConstraint("tenant_id", "conversation_id", "sequence"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversation.tenant_id", "conversation.id"],
        ),
        Index("ix_message_conversation", "tenant_id", "conversation_id", "sequence"),
        CheckConstraint("role IN ('customer', 'assistant', 'system', 'staff')", name="role"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column()
    client_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------


class Task(Base, TimestampMixin, VersionMixin):
    """业务目标及其持久流程，可跨多轮 reply。

    与 SDK 的 planning task 不同：这里保存的是业务状态，模型上下文、缓存和
    聊天摘要都不能改变它。
    """

    __tablename__ = "task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversation.tenant_id", "conversation.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"], ["customer.tenant_id", "customer.id"]
        ),
        Index("ix_task_conversation_state", "tenant_id", "conversation_id", "state"),
        Index("ix_task_state_release", "state", "release_id"),
        non_negative("budget_limit_units", name="budget_limit"),
        non_negative("settled_cost_units", name="settled_cost"),
        non_negative("reserved_cost_units", name="reserved_cost"),
        # lease_owner / lease_until 成对为空或非空。
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_until IS NULL)", name="lease_pair"
        ),
        # current_proposal 两字段成对。
        CheckConstraint(
            "(current_proposal_id IS NULL) = (current_proposal_version IS NULL)",
            name="current_proposal_pair",
        ),
        enum_check("state", TaskState, name="state"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(nullable=False)
    customer_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID | None] = mapped_column()
    goal: Mapped[str] = mapped_column(String(64), nullable=False, server_default="book")
    state: Mapped[str] = mapped_column(
        String(48), nullable=False, server_default=TaskState.COLLECTING.value
    )
    #: 结构化槽位：{slot_name: {value, source_message_id, resolution_status, intent, updated_at}}
    slots: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    slots_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    #: 控制权代际。控制权变化（人工接管）才增加 epoch；普通消息只增加 version。
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    release_id: Mapped[str] = mapped_column(String(120), nullable=False)
    current_proposal_id: Mapped[UUID | None] = mapped_column()
    current_proposal_version: Mapped[int | None] = mapped_column(Integer)
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 每次租约接管增加 fence。仅依赖 lease_until 不足以阻止暂停后恢复的旧实例。
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    budget_limit_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("100000000")
    )
    settled_cost_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    reserved_cost_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    budget_currency: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="USD"
    )
    cost_quantum: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="0.000000001"
    )
    next_event_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )


class ExecutionAttempt(Base, CreatedAtMixin):
    """一次 SDK/确定性基线的执行尝试。

    WAITING 后结束尝试并撤销权威；旧尝试不能写当前任务。
    """

    __tablename__ = "execution_attempt"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reply_id"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["task.tenant_id", "task.id"]),
        Index("ix_execution_attempt_task", "tenant_id", "task_id", "started_at"),
        enum_check("agent_role", AgentRole, name="agent_role"),
        enum_check("status", ExecutionEndReason, name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    release_id: Mapped[str] = mapped_column(String(120), nullable=False)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reply_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ExecutionEndReason.COMPLETED.value
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(64))
    #: 诊断快照仅用于诊断与可选上下文优化，不承担跨等待续跑权威。
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class WaitingRequest(Base, TimestampMixin, VersionMixin):
    """持久业务等待。

    kind=CLARIFICATION/CONFIRMATION/EXTERNAL 分别对应任务
    WAITING_USER/WAITING_CONFIRMATION/WAITING_EXTERNAL。首版每任务一个 OPEN 等待。
    """

    __tablename__ = "waiting_request"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["task.tenant_id", "task.id"]),
        Index("ix_waiting_request_task", "tenant_id", "task_id", "status"),
        Index(
            "uq_waiting_request_open_per_task",
            "tenant_id",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        enum_check("kind", WaitingKind, name="kind"),
        enum_check("status", WaitingStatus, name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    question_id: Mapped[UUID | None] = mapped_column()
    question_version: Mapped[int | None] = mapped_column(Integer)
    proposal_id: Mapped[UUID | None] = mapped_column()
    proposal_version: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[UUID | None] = mapped_column()
    question_text: Mapped[str | None] = mapped_column(Text)
    #: 等待时的任务版本与代际，用于拒绝过时答案。
    task_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    release_id: Mapped[str] = mapped_column(String(120), nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=WaitingStatus.OPEN.value
    )
    answered_event_id: Mapped[str | None] = mapped_column(String(64))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WaitingAnswer(Base, CreatedAtMixin):
    """等待答案。重复答案事件唯一去重；过期/旧 epoch 只留审计，不复活任务。"""

    __tablename__ = "waiting_answer"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "waiting_request_id", "client_event_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "waiting_request_id"],
            ["waiting_request.tenant_id", "waiting_request.id"],
        ),
        CheckConstraint(
            "validation_status IN ('ACCEPTED', 'REJECTED_STALE', 'REJECTED_INVALID')",
            name="validation_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    waiting_request_id: Mapped[UUID] = mapped_column(nullable=False)
    client_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column()
    answer: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    applied_task_version: Mapped[int | None] = mapped_column(BigInteger)


class TaskEvent(Base, CreatedAtMixin):
    """任务事件。SSE 的恢复依据。

    UNIQUE(tenant_id, task_id, sequence)；客户端按 sequence 去重并携带恢复游标。
    """

    __tablename__ = "task_event"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "sequence"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["task.tenant_id", "task.id"]),
        Index("ix_task_event_task", "tenant_id", "task_id", "sequence"),
        Index("ix_task_event_occurred", "tenant_id", "occurred_at"),
        CheckConstraint(
            "type IN ({})".format(", ".join(f"'{t.value}'" for t in _EVENT_TYPE_VALUES)),
            name="type",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HandoffCase(Base, TimestampMixin, VersionMixin):
    """人工接手工单。接管锁 task 并增加 epoch，自动 Agent 随即失权。"""

    __tablename__ = "handoff_case"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["task.tenant_id", "task.id"]),
        Index(
            "uq_handoff_case_active_per_task",
            "tenant_id",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('OPEN', 'CLAIMED')"),
        ),
        enum_check("status", HandoffStatus, name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_ref: Mapped[str | None] = mapped_column(String(128))
    owner_actor_id: Mapped[UUID | None] = mapped_column()
    #: 接管时的任务 epoch。旧 epoch 的 Agent 动作会被拒绝。
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=HandoffStatus.OPEN.value
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "Conversation",
    "ExecutionAttempt",
    "HandoffCase",
    "Message",
    "Task",
    "TaskEvent",
    "WaitingAnswer",
    "WaitingRequest",
]
