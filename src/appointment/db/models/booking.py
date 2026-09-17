"""方案、占位、确认与订单表（数据契约 §5）。

核心不变量"同一租户中的同一单位容量资源，不能存在时间重叠的有效占用"由
``resource_allocation`` 上的 GiST 区间排他约束保证，不由应用层读后检查保证。
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
    SmallInteger,
    String,
    UniqueConstraint,
    column,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...core.enums import (
    AllocationState,
    AppointmentStatus,
    ConfirmationStatus,
    FulfillmentStatus,
    HoldState,
    OperationStatus,
    ProposalAction,
    ProposalStatus,
)
from ..base import (
    Base,
    CreatedAtMixin,
    TimestampMixin,
    VersionMixin,
    enum_check,
    non_negative,
    positive,
    time_range_ordered,
)

#: 参与排他约束的状态。谓词必须是 immutable 表达式，不能用随时间变化的
#: "expires_at > now()" 来实现自动过期（设计稿 §5.1）。
_BLOCKING_STATES_SQL = ", ".join(
    f"'{state.value}'" for state in (AllocationState.HELD, AllocationState.BOOKED)
)


class Proposal(Base, CreatedAtMixin):
    """可版本化方案。方案不等于资源保证。

    ``proposal`` 是"默认单列主键"约定的例外：采用 (id, version) 复合主键；
    所有方案引用都必须包含 version，不能引用任意最新版本。
    """

    __tablename__ = "proposal"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "id", "version"
        ),
        UniqueConstraint("tenant_id", "id", "version"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["task.tenant_id", "task.id"]),
        Index("ix_proposal_task_created", "tenant_id", "task_id", "created_at"),
        enum_check("action", ProposalAction, name="action"),
        enum_check("status", ProposalStatus, name="status"),
        positive("version", name="version_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_appointment_id: Mapped[UUID | None] = mapped_column()
    expected_appointment_version: Mapped[int | None] = mapped_column(Integer)
    hold_id: Mapped[UUID | None] = mapped_column()
    quote_id: Mapped[UUID | None] = mapped_column()
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    #: 内容字段不可变，status 可变。
    canonical_content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 提交时要复核的依赖事实及其版本，避免"读完再变化"的二次 TOCTOU。
    dependency_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ProposalStatus.ACTIVE.value
    )


class Hold(Base, TimestampMixin, VersionMixin):
    """一次有截止时间的临时资源保证，可关联多条 allocation。"""

    __tablename__ = "hold"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["task.tenant_id", "task.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"], ["customer.tenant_id", "customer.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "store_id"], ["store.tenant_id", "store.id"]),
        # created_operation_id 唯一，防重复占位。
        UniqueConstraint("tenant_id", "created_operation_id"),
        Index("ix_hold_customer_state", "tenant_id", "customer_id", "state"),
        Index("ix_hold_state_expires", "tenant_id", "state", "expires_at"),
        # 首版：每客户最多一个有效普通占位。该部分唯一索引也包含"逻辑到期但
        # 未改状态"的行，所以 create_hold 必须先锁 customer_hold_guard 并把
        # 到期 hold 状态化，然后替换。
        Index(
            "uq_hold_customer_active",
            "tenant_id",
            "customer_id",
            unique=True,
            postgresql_where=text("state = 'HELD'"),
        ),
        enum_check("state", HoldState, name="state"),
        enum_check("action", ProposalAction, name="action"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    customer_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    quote_id: Mapped[UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_appointment_id: Mapped[UUID | None] = mapped_column()
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_operation_id: Mapped[UUID] = mapped_column(nullable=False)
    booked_appointment_id: Mapped[UUID | None] = mapped_column()


class ResourceAllocation(Base, TimestampMixin, VersionMixin):
    """对技师、房间等单位资源的实际区间占用。

    技师与房间各有一条记录，整个预约所需资源在同一事务中获取，任一冲突则
    全部回滚。
    """

    __tablename__ = "resource_allocation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"], ["resource.tenant_id", "resource.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "store_id"], ["store.tenant_id", "store.id"]),
        ForeignKeyConstraint(["tenant_id", "hold_id"], ["hold.tenant_id", "hold.id"]),
        Index("ix_allocation_hold", "tenant_id", "hold_id"),
        Index("ix_allocation_appointment", "tenant_id", "appointment_id"),
        Index("ix_allocation_resource_range", "tenant_id", "resource_id", "start_at"),
        time_range_ordered(),
        enum_check("state", AllocationState, name="state"),
        # HELD 必须 hold_id/expiry 且 appointment_id 为空；BOOKED 必须
        # appointment_id 且 expiry 为空。
        CheckConstraint(
            "(state = 'HELD' AND hold_id IS NOT NULL AND expires_at IS NOT NULL "
            "AND appointment_id IS NULL) OR "
            "(state = 'BOOKED' AND appointment_id IS NOT NULL AND expires_at IS NULL) OR "
            "(state IN ('RELEASED', 'EXPIRED'))",
            name="state_shape",
        ),
        # 同租户、同资源、有效占用区间不得重叠。半开区间 [start_at, end_at)，
        # 清洁或缓冲时间应计入实际占用。
        ExcludeConstraint(
            (column("tenant_id"), "="),
            (column("resource_id"), "="),
            (text("tstzrange(start_at, end_at, '[)')"), "&&"),
            using="gist",
            where=text(f"state IN ({_BLOCKING_STATES_SQL})"),
            name="ex_resource_allocation_no_overlap",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False)
    hold_id: Mapped[UUID | None] = mapped_column()
    appointment_id: Mapped[UUID | None] = mapped_column()
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Appointment(Base, TimestampMixin, VersionMixin):
    """预约订单。状态由业务数据库决定，模型回答与聊天摘要不能改变事实。"""

    __tablename__ = "appointment"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"], ["customer.tenant_id", "customer.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "store_id"], ["store.tenant_id", "store.id"]),
        # created_operation_id 唯一；last_operation_id 不唯一（不要错误限制
        # 每单只能有一个操作）。
        UniqueConstraint("tenant_id", "created_operation_id"),
        Index(
            "ix_appointment_customer_start", "tenant_id", "customer_id", "start_at", "id"
        ),
        Index(
            "ix_appointment_store_start", "tenant_id", "store_id", "start_at", "status"
        ),
        time_range_ordered(),
        non_negative("amount_minor", name="amount_minor"),
        enum_check("status", AppointmentStatus, name="status"),
        enum_check("fulfillment_status", FulfillmentStatus, name="fulfillment_status"),
        CheckConstraint("currency_exponent BETWEEN 0 AND 6", name="currency_exponent"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    customer_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    fulfillment_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=FulfillmentStatus.READY.value
    )
    service_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    resource_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    currency_exponent: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    terms_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_operation_id: Mapped[UUID] = mapped_column(nullable=False)
    last_operation_id: Mapped[UUID | None] = mapped_column()
    actual_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quote_id: Mapped[UUID | None] = mapped_column()


class AppointmentRevision(Base, CreatedAtMixin):
    """改约/取消/履约历史。不靠当前快照推测历史。"""

    __tablename__ = "appointment_revision"
    __table_args__ = (
        UniqueConstraint("tenant_id", "appointment_id", "version"),
        ForeignKeyConstraint(
            ["tenant_id", "appointment_id"],
            ["appointment.tenant_id", "appointment.id"],
        ),
        positive("version", name="version_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    appointment_id: Mapped[UUID] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column()


class Confirmation(Base, TimestampMixin, VersionMixin):
    """可信用户事件对具体动作和方案内容的授权证据。

    绑定 tenant、actor、task、proposal_id/version、内容哈希、动作、有效期和唯一
    nonce。签发 token 不等于用户已确认；消费绑定一个操作。
    """

    __tablename__ = "confirmation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("token_hash"),
        UniqueConstraint("nonce"),
        UniqueConstraint(
            "tenant_id", "actor_id", "client_event_id"
        ),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["task.tenant_id", "task.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "proposal_id", "proposal_version"],
            ["proposal.tenant_id", "proposal.id", "proposal.version"],
        ),
        Index("ix_confirmation_task", "tenant_id", "task_id", "status"),
        enum_check("status", ConfirmationStatus, name="status"),
        enum_check("action", ProposalAction, name="action"),
        CheckConstraint("proposal_version >= 1", name="proposal_version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_id: Mapped[UUID] = mapped_column(nullable=False)
    proposal_id: Mapped[UUID] = mapped_column(nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ConfirmationStatus.ISSUED.value
    )
    client_event_id: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_operation_id: Mapped[UUID | None] = mapped_column()


class Operation(Base, TimestampMixin, VersionMixin):
    """一个幂等业务命令及结果。同一次确认的重试复用它。"""

    __tablename__ = "operation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        # 作用域内幂等键唯一：相同键同参数返回原结果；相同键不同参数冲突。
        UniqueConstraint(
            "tenant_id", "actor_id", "action", "idempotency_key"
        ),
        # 防同一凭据换键重复产生业务效果。
        UniqueConstraint("tenant_id", "confirmation_id"),
        Index("ix_operation_status_lease", "tenant_id", "status", "lease_until"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["task.tenant_id", "task.id"]),
        enum_check("status", OperationStatus, name="status"),
        enum_check("action", ProposalAction, name="action"),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_until IS NULL)", name="lease_pair"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID | None] = mapped_column()
    customer_id: Mapped[UUID | None] = mapped_column()
    actor_id: Mapped[UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_appointment_id: Mapped[UUID | None] = mapped_column()
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_id: Mapped[UUID | None] = mapped_column()
    proposal_version: Mapped[int | None] = mapped_column(Integer)
    confirmation_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=OperationStatus.REGISTERED.value
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolExecution(Base, CreatedAtMixin):
    """工具调用账本。SDK 调用去重不能替代 operation 业务去重。"""

    __tablename__ = "tool_execution"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "attempt_id", "tool_call_id"
        ),
        Index("ix_tool_execution_attempt", "tenant_id", "attempt_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_id: Mapped[UUID | None] = mapped_column()
    parameter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "Appointment",
    "AppointmentRevision",
    "Confirmation",
    "Hold",
    "Operation",
    "Proposal",
    "ResourceAllocation",
    "ToolExecution",
]
