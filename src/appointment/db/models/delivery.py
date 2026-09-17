"""异步交付、供应商证据与扰动处置表（数据契约 §6）。

Outbox 被消费只代表逻辑投递已经持久受理，不代表用户收到通知。HTTP 200 与
供应商接受不自动表示送达。
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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ...core.enums import (
    DisruptionItemStatus,
    DisruptionStatus,
    JobStatus,
    OutboxStatus,
    ReceiptProcessingStatus,
    ReconcileStatus,
)
from ..base import (
    Base,
    CreatedAtMixin,
    TimestampMixin,
    VersionMixin,
    enum_check,
    non_negative,
)


class Outbox(Base, TimestampMixin):
    """业务事件与订单同事务写入；Worker 至少一次处理。

    UNIQUE(tenant_id, aggregate_type, aggregate_id, aggregate_version, event_type,
    event_key)：同版允许多个同类事件时靠 event_key 区分。
    """

    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "event_type",
            "event_key",
        ),
        Index("ix_outbox_dispatch", "status", "available_at", "id"),
        enum_check("status", OutboxStatus, name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False)
    aggregate_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_key: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=OutboxStatus.PENDING.value
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )


class Job(Base, TimestampMixin):
    """后台任务表。Worker 用租约领取自己的待办，与 P0 不抢占的用户任务区分。"""

    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "logical_key"),
        Index("ix_job_schedule", "status", "next_run_at", "id"),
        Index("ix_job_task", "tenant_id", "task_id"),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0", name="attempts"
        ),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_until IS NULL)", name="lease_pair"
        ),
        enum_check("status", JobStatus, name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    source_event_id: Mapped[UUID | None] = mapped_column()
    task_id: Mapped[UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 幂等键。同一逻辑待办重复投递只产生一个 job。
    logical_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=JobStatus.PENDING.value
    )
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("8"))
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    #: 限制总等待。达到截止产生告警/人工待办，不把未知副作用改成失败事实。
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    result_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class NotificationDelivery(Base, TimestampMixin, VersionMixin):
    """一个逻辑投递。

    逻辑投递唯一键为 tenant + source_event_id + channel + recipient_ref +
    template_version。同一投递重试复用 provider_idempotency_key 和 payload_hash；
    内容变化创建新的更正投递。
    """

    __tablename__ = "notification_delivery"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id",
            "source_event_id",
            "channel",
            "recipient_ref",
            "template_version",
        ),
        UniqueConstraint(
            "provider",
            "provider_account_id",
            "provider_idempotency_key",
        ),
        Index(
            "ix_delivery_appointment",
            "tenant_id",
            "appointment_id",
            "appointment_version",
        ),
        Index("ix_delivery_reconcile", "status", "next_reconcile_at"),
        CheckConstraint(
            "status IN ('PENDING', 'SENDING', 'ACCEPTED', 'DELIVERED', "
            "'FAILED_RETRYABLE', 'FAILED_FINAL', 'UNKNOWN', 'SUPERSEDED')",
            name="status",
        ),
        enum_check("reconcile_status", ReconcileStatus, name="reconcile_status"),
        non_negative("appointment_version", name="appointment_version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    source_event_id: Mapped[UUID] = mapped_column(nullable=False)
    appointment_id: Mapped[UUID] = mapped_column(nullable=False)
    appointment_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 受保护接收者引用，不存明文手机号。
    recipient_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="PENDING"
    )
    last_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ReconcileStatus.NOT_REQUIRED.value
    )
    reconcile_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reconcile_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 更正投递指向被替代的投递。
    supersedes_delivery_id: Mapped[UUID | None] = mapped_column()


class DeliveryAttempt(Base, CreatedAtMixin):
    """每次发送尝试。调用供应商之前就持久记录 attempt_id、请求哈希与提交时间。"""

    __tablename__ = "delivery_attempt"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "delivery_id", "attempt_number"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "delivery_id"],
            ["notification_delivery.tenant_id", "notification_delivery.id"],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    delivery_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    response_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ProviderMessageBinding(Base, CreatedAtMixin):
    """一个供应商消息仅映射一个逻辑 delivery。

    作用域包含 provider 与 provider_account_id，避免不同供应商/账户 ID 碰撞。
    """

    __tablename__ = "provider_message_binding"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_account_id", "provider_message_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_id: Mapped[UUID] = mapped_column(nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)


class ProviderReceipt(Base, CreatedAtMixin):
    """可信供应商事件。

    这是租户隔离例外：未绑定记录仅平台隔离处理权限可见；绑定后才关联租户。
    不能相信回调 payload 自报 tenant。
    """

    __tablename__ = "provider_receipt"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_account_id", "provider_event_id"
        ),
        Index("ix_receipt_processing", "processing_status", "received_at"),
        enum_check("processing_status", ReceiptProcessingStatus, name="processing_status"),
        CheckConstraint(
            "validation_status IN ('VALID', 'INVALID_SIGNATURE', 'UNKNOWN_SOURCE', 'MALFORMED')",
            name="validation_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    delivery_id: Mapped[UUID | None] = mapped_column()
    tenant_id: Mapped[UUID | None] = mapped_column()
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    processing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ReceiptProcessingStatus.PENDING.value
    )
    #: 回执原文受保护，日志只保留脱敏引用。
    protected_raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Disruption(Base, TimestampMixin, VersionMixin):
    """紧急闭店/批量请假产生的扰动批次。

    规则生效先阻止新预约，再分批幂等处理旧订单，不持有跨人工等待的长事务。
    """

    __tablename__ = "disruption"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index("ix_disruption_store_status", "tenant_id", "store_id", "status"),
        enum_check("status", DisruptionStatus, name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=DisruptionStatus.OPEN.value
    )


class DisruptionItem(Base, TimestampMixin, VersionMixin):
    """受影响订单。已确认订单不会因日历更新自动消失。"""

    __tablename__ = "disruption_item"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "disruption_id", "appointment_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "disruption_id"], ["disruption.tenant_id", "disruption.id"]
        ),
        enum_check("status", DisruptionItemStatus, name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    disruption_id: Mapped[UUID] = mapped_column(nullable=False)
    appointment_id: Mapped[UUID] = mapped_column(nullable=False)
    detected_appointment_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=DisruptionItemStatus.PENDING.value
    )
    resolution_operation_id: Mapped[UUID | None] = mapped_column()


__all__ = [
    "DeliveryAttempt",
    "Disruption",
    "DisruptionItem",
    "Job",
    "NotificationDelivery",
    "Outbox",
    "ProviderMessageBinding",
    "ProviderReceipt",
]
