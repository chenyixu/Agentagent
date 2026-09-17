"""平台与租户、身份、知识、审计、模型记账表（数据契约 §3、§6）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
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

from ...core.enums import UsageStatus
from ..base import Base, CreatedAtMixin, TimestampMixin, VersionMixin

#: 由 core.enums 派生，避免状态取值在两处维护。
_USAGE_STATUS_VALUES = ", ".join(f"'{member.value}'" for member in UsageStatus)

# ---------------------------------------------------------------------------
# 租户、门店
# ---------------------------------------------------------------------------


class Tenant(Base, TimestampMixin, VersionMixin):
    __tablename__ = "tenant"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ACTIVE")


class Store(Base, TimestampMixin, VersionMixin):
    """门店。

    ``timezone`` 经服务端 IANA 校验（加载 zoneinfo），不靠普通 SQL CHECK
    验完整时区库。
    """

    __tablename__ = "store"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "name"),
        Index("ix_store_tenant_status", "tenant_id", "status"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ACTIVE")
    #: 门店营业规则版本。临时闭店与营业时间修改都推进它。
    calendar_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------


class Actor(Base, TimestampMixin, VersionMixin):
    """可信主体。请求体或模型不能指定自己的可信身份。"""

    __tablename__ = "actor"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ACTIVE")


class IdentityBinding(Base, CreatedAtMixin):
    """IdP issuer + subject 的组合才是身份；手机号不是 subject。"""

    __tablename__ = "identity_binding"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "issuer", "subject"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["actor.tenant_id", "actor.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_id: Mapped[UUID] = mapped_column(nullable=False)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Customer(Base, TimestampMixin, VersionMixin):
    """客户。手机号仅是受保护属性，不作为可信身份。"""

    __tablename__ = "customer"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "actor_id"),
        ForeignKeyConstraint(["tenant_id", "actor_id"], ["actor.tenant_id", "actor.id"]),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_id: Mapped[UUID] = mapped_column(nullable=False)
    #: 受保护联系信息引用（HMAC 假名）。模型默认只接收客户代号。
    protected_contact_ref: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, server_default="客户")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ACTIVE")


class StaffMembership(Base, TimestampMixin, VersionMixin):
    __tablename__ = "staff_membership"
    __table_args__ = (
        UniqueConstraint("tenant_id", "actor_id", "store_id", "role"),
        ForeignKeyConstraint(["tenant_id", "actor_id"], ["actor.tenant_id", "actor.id"]),
        ForeignKeyConstraint(["tenant_id", "store_id"], ["store.tenant_id", "store.id"]),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ACTIVE")


# ---------------------------------------------------------------------------
# 发布清单
# ---------------------------------------------------------------------------


class ReleaseManifestRow(Base, CreatedAtMixin):
    """平台级不可变配置。撤回发布不修改已存 manifest。"""

    __tablename__ = "release_manifest"
    __table_args__ = (
        UniqueConstraint("release_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    release_id: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ACTIVE")
    compatible_from: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


# ---------------------------------------------------------------------------
# 知识
# ---------------------------------------------------------------------------


class KnowledgeDocument(Base, TimestampMixin, VersionMixin):
    """文档入库：审批 → 标注租户/门店/适用服务/生效区间 → 切块 → 索引 → 发布。"""

    __tablename__ = "knowledge_document"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "doc_key", "version"),
        Index("ix_knowledge_document_scope", "tenant_id", "status", "valid_from"),
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'RETIRED')", name="status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    doc_key: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 门店适用范围。NULL 表示租户级通用文档。
    store_id: Mapped[UUID | None] = mapped_column()
    service_id: Mapped[UUID | None] = mapped_column()
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="DRAFT")


class KnowledgeChunk(Base, CreatedAtMixin):
    """知识片段。

    P0 走应用层中文分词的关键词检索（设计稿 §10.1 允许小语料用应用内分词与
    关键词评分）；启用 pgvector 后把 ``embedding`` 迁移为向量列并加 HNSW。
    """

    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "ordinal"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 数据库列名为 text；Python 属性另起名以免遮蔽 sqlalchemy.text。
    content: Mapped[str] = mapped_column("text", Text, nullable=False)
    #: 应用层分词结果，供关键词召回。
    keyword_tokens: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    embedding: Mapped[list[float] | None] = mapped_column(JSONB)
    embedding_model: Mapped[str | None] = mapped_column(String(120))
    embedding_version: Mapped[str | None] = mapped_column(String(64))
    #: 片段来源定位，便于返回 evidence 时给出可核验的出处。
    source_span: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class Preference(Base, TimestampMixin, VersionMixin):
    """长期偏好是有来源和有效期的结构化事实，不是通用长期记忆。"""

    __tablename__ = "preference"
    __table_args__ = (
        UniqueConstraint("tenant_id", "customer_id", "key"),
        Index("ix_preference_customer_expires", "tenant_id", "customer_id", "expires_at"),
        CheckConstraint(
            "source_kind IN ('explicit', 'inferred')", name="source_kind"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    customer_id: Mapped[UUID] = mapped_column(nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: explicit（用户显式表达）优先于 inferred（后台候选）。
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_id: Mapped[str | None] = mapped_column(String(64))
    is_explicit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 用户纠正后置位，后台不得从旧历史再次覆盖回来。
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# 审计与模型记账
# ---------------------------------------------------------------------------


class AuditEvent(Base, CreatedAtMixin):
    """不可变审计。不得存裸 token 或未脱敏手机号。"""

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_object", "tenant_id", "object_type", "object_id", "created_at"),
        Index("ix_audit_event_actor", "tenant_id", "actor_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before_version: Mapped[int | None] = mapped_column(BigInteger)
    after_version: Mapped[int | None] = mapped_column(BigInteger)
    operation_id: Mapped[UUID | None] = mapped_column()
    confirmation_id: Mapped[UUID | None] = mapped_column()
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    protected_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ModelCall(Base, CreatedAtMixin):
    """按调用尝试记账。缺 usage 标记 unknown，不计为零。

    ``cost_quantum`` 表示每计费单位对应多少预算币种，与客户售价的"分"不是
    同一精度体系。
    """

    __tablename__ = "model_call"
    __table_args__ = (
        Index("ix_model_call_task", "tenant_id", "task_id", "created_at"),
        CheckConstraint(
            f"usage_status IN ({_USAGE_STATUS_VALUES})", name="usage_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID | None] = mapped_column()
    task_id: Mapped[UUID | None] = mapped_column()
    reply_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(120))
    model_price_version: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_tokens: Mapped[int | None] = mapped_column(BigInteger)
    usage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_cost_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    estimated_cost_units: Mapped[int | None] = mapped_column(BigInteger)
    settled_cost_units: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="USD")
    cost_quantum: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="0.000000001"
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))


__all__ = [
    "Actor",
    "AuditEvent",
    "Customer",
    "IdentityBinding",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "ModelCall",
    "Preference",
    "ReleaseManifestRow",
    "StaffMembership",
    "Store",
    "Tenant",
]
