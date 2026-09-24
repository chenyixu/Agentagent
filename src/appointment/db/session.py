"""数据库会话、事务与锁协议。

设计稿 §7 规定锁层次，且"持锁后禁止反向补拿上层锁"：

    任务 → 客户占位 guard → 门店规则 guard → 资源/服务规则 guard
    → 资源日 guard → 订单 → quote/hold/方案/确认/operation

先读取关系确定集合，再按顺序加锁并重读版本；关系变化则回滚重试。
:class:`LockSequencer` 在运行期强制这个顺序，越序加锁直接抛错，
避免"代码写得看起来对、实际顺序不一致"的静默死锁风险。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, AsyncIterator, Sequence
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config.settings import get_settings
from ..core.enums import ErrorCode
from ..core.errors import DomainError
from . import models as m

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from ..core.clock import Clock

# ---------------------------------------------------------------------------
# 锁层次
# ---------------------------------------------------------------------------
LOCK_RANK: dict[str, int] = {
    "task": 10,
    "customer_hold_guard": 20,
    "store": 30,
    "service_catalog": 35,
    "resource": 40,
    "shift": 45,
    "resource_day_guard": 50,
    "appointment": 60,
    "quote": 70,
    "hold": 71,
    "proposal": 72,
    "confirmation": 73,
    "operation": 74,
}


class LockSequencer:
    """记录已获取的锁等级，拒绝越序加锁。"""

    __slots__ = ("_highest", "_acquired")

    def __init__(self) -> None:
        self._highest = 0
        self._acquired: list[str] = []

    def claim(self, kind: str) -> None:
        rank = LOCK_RANK[kind]
        if rank < self._highest:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"违反锁顺序：已持有 {self._acquired} 后不得再获取 {kind}；"
                "需要更高层锁时必须重新按完整协议开始",
            )
        self._highest = rank
        self._acquired.append(kind)

    @property
    def acquired(self) -> tuple[str, ...]:
        return tuple(self._acquired)


# ---------------------------------------------------------------------------
# 引擎与会话
# ---------------------------------------------------------------------------
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = (
            {
                "server_settings": {
                    "search_path": f"{settings.db_schema},public"
                }
            }
            if settings.db_schema
            else {}
        )
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """一个事务边界。

    事务保持短小：不在事务中调用模型或供应商（设计稿 §7）。
    """

    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def db_now(session: AsyncSession) -> datetime:
    """事务内数据库实际时间。

    用于到期裁决：``now()``/``CURRENT_TIMESTAMP`` 是事务起始时间，长时间等待
    锁后可能已经过时。
    """

    result = await session.execute(select(text("clock_timestamp()")))
    return result.scalar_one()


async def live_now(session: AsyncSession, clock: Clock | None = None) -> datetime:
    """拿锁后的裁决时间点。

    - ``clock`` 为 ``None``（生产默认）时取**数据库实际时钟**：这是权威时间源，
      与客户端/应用进程的时钟漂移无关。
    - 注入 ``clock`` 时以注入时钟为准，用于测试推进受控时间。

    无论哪条路径，都必须**在取得锁之后**调用：设计稿 §8.1 的要点是"不要用事务
    起始时间做有效期裁决"，而不是"必须读数据库"。注入时钟只改变时间来源，不改变
    裁决时机，因此并发正确性不受影响。
    """

    if clock is None:
        return await db_now(session)
    return clock.now()


# ---------------------------------------------------------------------------
# 加锁原语：全部要求调用方传入 LockSequencer
# ---------------------------------------------------------------------------
async def lock_task(
    session: AsyncSession,
    sequencer: LockSequencer,
    *,
    tenant_id: UUID,
    task_id: UUID,
) -> m.Task:
    sequencer.claim("task")
    row = (
        await session.execute(
            select(m.Task)
            .where(m.Task.tenant_id == tenant_id, m.Task.id == task_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "任务不存在或无权访问")
    return row


async def lock_customer_hold_guard(
    session: AsyncSession,
    sequencer: LockSequencer,
    *,
    tenant_id: UUID,
    customer_id: UUID,
) -> None:
    """锁客户占位配额行。多标签页无法绕过"每客户最多一个有效占位"。"""

    sequencer.claim("customer_hold_guard")
    await session.execute(
        text(
            "INSERT INTO customer_hold_guard (tenant_id, customer_id) "
            "VALUES (:tenant_id, :customer_id) "
            "ON CONFLICT (tenant_id, customer_id) DO NOTHING"
        ),
        {"tenant_id": tenant_id, "customer_id": customer_id},
    )
    await session.execute(
        select(m.CustomerHoldGuard)
        .where(
            m.CustomerHoldGuard.tenant_id == tenant_id,
            m.CustomerHoldGuard.customer_id == customer_id,
        )
        .with_for_update()
    )


async def lock_store(
    session: AsyncSession,
    sequencer: LockSequencer,
    *,
    tenant_id: UUID,
    store_id: UUID,
) -> m.Store:
    """门店规则 guard。闭店与营业时间修改共享这把锁。"""

    sequencer.claim("store")
    row = (
        await session.execute(
            select(m.Store)
            .where(m.Store.tenant_id == tenant_id, m.Store.id == store_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "门店不存在或无权访问")
    return row


async def lock_resource(
    session: AsyncSession,
    sequencer: LockSequencer,
    *,
    tenant_id: UUID,
    resource_id: UUID,
) -> m.Resource:
    sequencer.claim("resource")
    row = (
        await session.execute(
            select(m.Resource)
            .where(m.Resource.tenant_id == tenant_id, m.Resource.id == resource_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "资源不存在或无权访问")
    return row


async def lock_resource_day(
    session: AsyncSession,
    sequencer: LockSequencer,
    *,
    tenant_id: UUID,
    resource_id: UUID,
    resource_ids_days: Sequence[tuple[UUID, date]],
) -> None:
    """资源日 guard。跨午夜逐日覆盖，按 (resource_id, local_date) 稳定排序加锁。"""

    sequencer.claim("resource_day_guard")
    ordered = sorted({(rid, d) for rid, d in resource_ids_days})
    for rid, local_date in ordered:
        await session.execute(
            text(
                "INSERT INTO resource_day_guard (tenant_id, resource_id, local_date) "
                "VALUES (:tenant_id, :resource_id, :local_date) "
                "ON CONFLICT (tenant_id, resource_id, local_date) DO NOTHING"
            ),
            {"tenant_id": tenant_id, "resource_id": rid, "local_date": local_date},
        )
    if ordered:
        await session.execute(
            select(m.ResourceDayGuard)
            .where(
                m.ResourceDayGuard.tenant_id == tenant_id,
                m.ResourceDayGuard.resource_id.in_([rid for rid, _ in ordered]),
            )
            .order_by(m.ResourceDayGuard.resource_id, m.ResourceDayGuard.local_date)
            .with_for_update()
        )


async def lock_hold(
    session: AsyncSession,
    sequencer: LockSequencer,
    *,
    tenant_id: UUID,
    hold_id: UUID,
) -> m.Hold:
    sequencer.claim("hold")
    row = (
        await session.execute(
            select(m.Hold)
            .where(m.Hold.tenant_id == tenant_id, m.Hold.id == hold_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "占位不存在或无权访问")
    return row


async def lock_proposal(
    session: AsyncSession,
    sequencer: LockSequencer,
    *,
    tenant_id: UUID,
    proposal_id: UUID,
    proposal_version: int,
) -> m.Proposal:
    sequencer.claim("proposal")
    row = (
        await session.execute(
            select(m.Proposal)
            .where(
                m.Proposal.tenant_id == tenant_id,
                m.Proposal.id == proposal_id,
                m.Proposal.version == proposal_version,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "方案不存在或无权访问")
    return row


async def lock_confirmation(
    session: AsyncSession,
    sequencer: LockSequencer,
    *,
    tenant_id: UUID,
    confirmation_id: UUID,
) -> m.Confirmation:
    sequencer.claim("confirmation")
    row = (
        await session.execute(
            select(m.Confirmation)
            .where(
                m.Confirmation.tenant_id == tenant_id,
                m.Confirmation.id == confirmation_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "确认凭据不存在或无权访问")
    return row


async def lock_appointment(
    session: AsyncSession,
    sequencer: LockSequencer,
    *,
    tenant_id: UUID,
    appointment_id: UUID,
) -> m.Appointment:
    sequencer.claim("appointment")
    row = (
        await session.execute(
            select(m.Appointment)
            .where(
                m.Appointment.tenant_id == tenant_id,
                m.Appointment.id == appointment_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "预约不存在或无权访问")
    return row


@dataclass(slots=True)
class TxContext:
    """一次领域事务的上下文，携带锁排序器与追踪字段。"""

    session: AsyncSession
    sequencer: LockSequencer = field(default_factory=LockSequencer)
    tenant_id: UUID | None = None
    task_id: UUID | None = None
    operation_id: UUID | None = None
    request_id: str = ""
    trace_id: str | None = None

    @classmethod
    def open(cls, session: AsyncSession, **kwargs: Any) -> "TxContext":
        return cls(session=session, **kwargs)
