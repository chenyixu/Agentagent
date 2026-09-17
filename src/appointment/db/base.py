"""声明式基类与公共列约定。

设计稿 §1 公共审计：每个可变业务聚合带 created_at、updated_at 与 version；
不可变记录只带 created_at。所有业务表 tenant_id 必填，并建立 UNIQUE(tenant_id, id)
以便跨表使用组合外键 (tenant_id, 引用 id) 防止串租户。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, MetaData, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CreatedAtMixin:
    """不可变记录只需 created_at。"""

    @declared_attr
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        )


class TimestampMixin(CreatedAtMixin):
    """可变聚合的公共审计列。"""

    @declared_attr
    def updated_at(cls) -> Mapped[datetime]:
        return mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )


class VersionMixin:
    """业务乐观锁版本。version/epoch/fencing_token 三者不能相互代替。

    同时给出客户端默认值与服务端默认值：前者让 ORM 对象 flush 后立刻可读，
    后者保证裸 SQL 插入也满足 NOT NULL 契约。
    """

    @declared_attr
    def version(cls) -> Mapped[int]:
        return mapped_column(
            BigInteger, nullable=False, default=1, server_default=text("1")
        )


def enum_check(column: str, enum_cls: type, *, name: str) -> CheckConstraint:
    """由受控枚举生成 CHECK 约束，避免出现随意字符串状态。

    CHECK 只能验证枚举取值，不能验证"旧状态→新状态"或调用主体；
    合法迁移由领域服务结合 expected version / epoch / fence 判定。
    """

    values = ", ".join(f"'{member.value}'" for member in enum_cls)  # type: ignore[attr-defined]
    return CheckConstraint(f"{column} IN ({values})", name=name)


def non_negative(column: str, *, name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} >= 0", name=name)


def positive(column: str, *, name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} > 0", name=name)


def time_range_ordered(name: str = "time_range_ordered") -> CheckConstraint:
    """预约区间为 [start_at, end_at)。"""

    return CheckConstraint("start_at < end_at", name=name)
