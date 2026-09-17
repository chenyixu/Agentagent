"""可履约事实：服务目录、报价、资源、班次、日历与互斥 guard（数据契约 §3）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import (
    Base,
    CreatedAtMixin,
    TimestampMixin,
    VersionMixin,
    non_negative,
    positive,
    time_range_ordered,
)

# ---------------------------------------------------------------------------
# 服务目录与报价
# ---------------------------------------------------------------------------


class ServiceCatalog(Base, TimestampMixin, VersionMixin):
    """服务。

    ``current_version_id`` 可先为空创建，再在发布事务中绑定合法版本，不把
    循环外键当初始化顺序的魔法。
    """

    __tablename__ = "service_catalog"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "store_id", "id"),
        Index("ix_service_catalog_store_status", "tenant_id", "store_id", "status"),
        CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'RETIRED')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 服务别名，供自然语言解析（"肩颈"、"开背"）。持久化便于回归测试。
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="DRAFT")
    current_version_id: Mapped[UUID | None] = mapped_column()
    current_price_version_id: Mapped[UUID | None] = mapped_column()


class ServiceVersion(Base, CreatedAtMixin):
    """服务版本不可变。``requirements`` 含技能、资源类型与数量。"""

    __tablename__ = "service_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "service_id", "revision"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "service_id"], ["service_catalog.tenant_id", "service_catalog.id"]
        ),
        positive("duration_minutes", name="duration_minutes"),
        CheckConstraint("revision >= 1", name="revision"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    service_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 例如 {"skills": ["tuina"], "resources": [{"type": "therapist", "count": 1}]}
    requirements: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    terms_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PriceVersion(Base, CreatedAtMixin):
    """价目版本不可变。

    发布新版本不撤销已保存的 quote；显式撤销走有审计的管理命令。
    """

    __tablename__ = "price_version"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "store_id", "service_version_id", "revision",
        ),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "service_version_id"],
            ["service_version.tenant_id", "service_version.id"],
        ),
        non_negative("amount_minor", name="amount_minor"),
        CheckConstraint("currency_exponent BETWEEN 0 AND 6", name="currency_exponent"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    service_version_id: Mapped[UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 按货币规则确定。金额一律用最小货币单位整数，不用浮点。
    currency_exponent: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    terms_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Quote(Base, CreatedAtMixin):
    """锁价快照。

    保存于方案/占位事务；发布新价不改旧 quote。咨询展示价格不等于已经锁价，
    真正锁价从方案事务成功起成立。
    """

    __tablename__ = "quote"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", "quote_version"),
        ForeignKeyConstraint(
            ["tenant_id", "price_version_id"], ["price_version.tenant_id", "price_version.id"]
        ),
        non_negative("amount_minor", name="amount_minor"),
        CheckConstraint("quote_version >= 1", name="quote_version"),
        Index("ix_quote_customer_valid", "tenant_id", "customer_id", "valid_until"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    customer_id: Mapped[UUID] = mapped_column(nullable=False)
    service_version_id: Mapped[UUID] = mapped_column(nullable=False)
    price_version_id: Mapped[UUID] = mapped_column(nullable=False)
    quote_version: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    currency_exponent: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    terms_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    terms_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# 资源与技能
# ---------------------------------------------------------------------------


class Resource(Base, TimestampMixin, VersionMixin):
    """一个 resource 是容量为 1 的可分配单元。

    首版不使用"同资源多容量"绕过排他约束：房间 1、房间 2 各是一条记录。
    """

    __tablename__ = "resource"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "store_id", "unit_code"),
        Index("ix_resource_store_type", "tenant_id", "store_id", "type", "status"),
        CheckConstraint("type IN ('therapist', 'room', 'equipment')", name="type"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_code: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ACTIVE")
    #: 显式性别属性，用于"上次那位""换个女技师"这类可解释偏好，不是默认必填槽位。
    gender: Mapped[str | None] = mapped_column(String(16))
    #: 归一化评分（0..1）。缺失特征需重新归一化，不虚构评价。
    reputation_score: Mapped[float | None] = mapped_column()


class ResourceSkill(Base, CreatedAtMixin):
    __tablename__ = "resource_skill"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "resource_id", "skill_code"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"], ["resource.tenant_id", "resource.id"]
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False)
    skill_code: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# 营业日历
# ---------------------------------------------------------------------------


class BusinessCalendar(Base, CreatedAtMixin):
    """营业窗口。窗口语义由校验器检查，不只靠普通 CHECK。"""

    __tablename__ = "business_calendar"
    __table_args__ = (
        UniqueConstraint("tenant_id", "store_id", "revision"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("revision >= 1", name="revision"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    #: {"mon": [["10:00", "22:00"]], ...} 门店本地时间语义。
    weekly_windows: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)


class CalendarException(Base, TimestampMixin, VersionMixin):
    """节假日、临时闭店与特殊营业时间。"""

    __tablename__ = "calendar_exception"
    __table_args__ = (
        UniqueConstraint("tenant_id", "store_id", "local_date", "kind"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("kind IN ('CLOSED', 'SPECIAL_HOURS')", name="kind"),
        Index("ix_calendar_exception_date", "tenant_id", "store_id", "local_date"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    windows: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    reason: Mapped[str | None] = mapped_column(String(255))


# ---------------------------------------------------------------------------
# 班次与请假
# ---------------------------------------------------------------------------


class Shift(Base, TimestampMixin, VersionMixin):
    """工作区间。预约和员工调整班次必须锁定/校验相同的资源日期或班次版本。"""

    __tablename__ = "shift"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"], ["resource.tenant_id", "resource.id"]
        ),
        Index("ix_shift_resource_start", "tenant_id", "resource_id", "start_at"),
        time_range_ordered(),
        CheckConstraint("status IN ('SCHEDULED', 'CANCELLED')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="SCHEDULED")


class ResourceAbsence(Base, TimestampMixin, VersionMixin):
    """请假。申请请假不改变可用性；批准与预约提交共享规则/资源锁。"""

    __tablename__ = "resource_absence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"], ["resource.tenant_id", "resource.id"]
        ),
        Index("ix_absence_resource_start", "tenant_id", "resource_id", "start_at"),
        time_range_ordered(),
        CheckConstraint(
            "approval_status IN ('REQUESTED', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="approval_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    store_id: Mapped[UUID] = mapped_column(nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approval_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)


# ---------------------------------------------------------------------------
# 互斥 guard
# ---------------------------------------------------------------------------


class ResourceDayGuard(Base):
    """可锁定互斥行，不是占用事实。

    设计稿 §7 锁层次中的"资源日 guard"。跨午夜逐日覆盖，天数受请求限制。
    """

    __tablename__ = "resource_day_guard"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"], ["resource.tenant_id", "resource.id"]
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    resource_id: Mapped[UUID] = mapped_column(primary_key=True)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)


class CustomerHoldGuard(Base):
    """客户占位配额互斥行。

    防止多标签页绕过"每客户最多一个有效占位"。该行在事务内串行校验配额。
    """

    __tablename__ = "customer_hold_guard"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"], ["customer.tenant_id", "customer.id"]
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(primary_key=True)


__all__ = [
    "BusinessCalendar",
    "CalendarException",
    "CustomerHoldGuard",
    "PriceVersion",
    "Quote",
    "Resource",
    "ResourceAbsence",
    "ResourceDayGuard",
    "ResourceSkill",
    "ServiceCatalog",
    "ServiceVersion",
    "Shift",
]
