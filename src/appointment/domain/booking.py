"""预约领域服务：占位、确认、改约、取消（设计稿 §7、§8，数据契约 §5、§7）。

三条必须同时成立的性质：

1. **不超卖** —— 同租户同资源的有效占用区间不得重叠。应用层的读后检查只用于
   提前给出可读错误，最终裁决是 GiST 排他约束。
2. **不重复业务效果** —— operation 唯一键 + 确认唯一键 + 规范化请求哈希。
3. **不越权确认** —— 凭据绑定 tenant/actor/task/方案版本/内容哈希/动作/有效期。

事务纪律：不在事务中调用模型或供应商；持锁后禁止反向补拿上层锁。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config.settings import get_settings
from ..core.clock import Clock
from ..core.enums import (
    AllocationState,
    AppointmentStatus,
    ConfirmationStatus,
    ErrorCode,
    FulfillmentStatus,
    HoldState,
    ProposalAction,
    ProposalStatus,
    TaskEventType,
    TaskState,
    WaitingKind,
    WaitingStatus,
)
from ..core.errors import (
    DomainError,
    confirmation_required,
    hold_expired,
    not_found,
    permission_denied,
    slot_conflict,
    stale_proposal,
    validation_error,
    version_conflict,
)
from ..core.hashing import content_hash, hash_secret, json_safe
from ..core.ids import idempotency_key as build_idempotency_key
from ..db import models as m
from ..db.session import (
    LockSequencer,
    live_now,
    lock_confirmation,
    lock_customer_hold_guard,
    lock_hold,
    lock_proposal,
    lock_resource,
    lock_resource_day,
    lock_store,
    lock_task,
)
from . import state_machine
from .availability import candidate_id_for
from .catalog import (
    ServiceFacts,
    load_service,
    load_service_version_requirements,
    validate_resource_composition,
)
from .context import TrustedContext
from .events import emit_outbox, emit_task_event, record_audit
from .operation import (
    OperationHandle,
    mark_succeeded,
    register_operation,
    replay_result,
)
from .quote import QuoteOffer, persist_quote_snapshot, verify_quote_token
from .timeutil import iter_local_dates, load_zone

CONFIRMATION_TOKEN_HEADER = "cft"


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HoldOutcome:
    hold: m.Hold
    proposal: m.Proposal
    confirmation: m.Confirmation
    confirmation_token: str
    task_version: int
    allocations: list[m.ResourceAllocation] = field(default_factory=list)
    replayed: bool = False

    def to_data(self) -> dict[str, Any]:
        return {
            "hold_id": str(self.hold.id),
            "expires_at": self.hold.expires_at.isoformat(),
            "allocations": [
                {
                    "resource_id": str(a.resource_id),
                    "start_at": a.start_at.isoformat(),
                    "end_at": a.end_at.isoformat(),
                    "state": a.state,
                }
                for a in self.allocations
            ],
            "proposal_id": str(self.proposal.id),
            "proposal_version": self.proposal.version,
            "proposal_hash": self.proposal.content_hash,
            "confirmation_id": str(self.confirmation.id),
            # 只用于客户端渲染确认卡；工具边界会在喂回模型前剥离该字段。
            "confirmation_token": self.confirmation_token,
            "task_version": self.task_version,
            "replayed": self.replayed,
        }


@dataclass(slots=True)
class CommitOutcome:
    appointment: m.Appointment
    operation: m.Operation
    task_version: int
    replayed: bool = False

    def to_data(self) -> dict[str, Any]:
        return {
            "appointment_id": str(self.appointment.id),
            "committed_status": self.appointment.status,
            "appointment_version": self.appointment.version,
            "operation_id": str(self.operation.id),
            "task_version": self.task_version,
            "start_at": self.appointment.start_at.isoformat(),
            "end_at": self.appointment.end_at.isoformat(),
            "amount_minor": self.appointment.amount_minor,
            "currency": self.appointment.currency,
            "replayed": self.replayed,
        }


# ---------------------------------------------------------------------------
# 过期占位回收
# ---------------------------------------------------------------------------


async def expire_due_allocations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    now: datetime,
    resource_ids: Sequence[UUID] | None = None,
) -> int:
    """把已到期的 HELD 占用状态化为 EXPIRED。

    ``now`` 必须由调用方在**拿到锁之后**取得（``live_now``），而不是事务起始时间。

    这一步不能省：排他约束谓词是 ``state IN ('HELD','BOOKED')``，未状态化的
    过期行仍会挡住新插入。查询侧忽略过期 HELD 只是让用户看到正确可用性，
    写入侧必须在锁内先回收（设计稿 §8.1）。
    """

    where = ["tenant_id = :tenant_id", "state = 'HELD'", "expires_at <= :now"]
    params: dict[str, Any] = {"tenant_id": tenant_id, "now": now}
    if resource_ids is not None:
        where.append("resource_id = ANY(:resource_ids)")
        params["resource_ids"] = list(resource_ids)
    result = await session.execute(
        text(
            "UPDATE resource_allocation SET state = 'EXPIRED', "
            "version = version + 1, updated_at = :now "
            f"WHERE {' AND '.join(where)}"
        ),
        params,
    )
    return result.rowcount or 0


async def expire_due_holds(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    now: datetime,
    customer_id: UUID | None = None,
) -> int:
    """把到期的占位头状态化为 EXPIRED。

    ``customer_id`` 为 None 时作用于整个租户（Worker 批量回收用）；领域写路径
    应传入具体客户，避免误伤其他客户的占位头。
    """

    where = ["tenant_id = :tenant_id", "state = 'HELD'", "expires_at <= :now"]
    params: dict[str, Any] = {"tenant_id": tenant_id, "now": now}
    if customer_id is not None:
        where.append("customer_id = :customer_id")
        params["customer_id"] = customer_id
    result = await session.execute(
        text(
            "UPDATE hold SET state = 'EXPIRED', version = version + 1, "
            "updated_at = :now "
            f"WHERE {' AND '.join(where)}"
        ),
        params,
    )
    return result.rowcount or 0


async def expire_open_waitings(
    session: AsyncSession, *, tenant_id: UUID, now: datetime
) -> int:
    """把已过期的 OPEN 等待标记为 EXPIRED。"""

    result = await session.execute(
        text(
            "UPDATE waiting_request SET status = 'EXPIRED', version = version + 1, "
            "updated_at = :now "
            "WHERE tenant_id = :tenant_id AND status = 'OPEN' "
            "AND expires_at <= :now"
        ),
        {"tenant_id": tenant_id, "now": now},
    )
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# 依赖事实校验
# ---------------------------------------------------------------------------


async def validate_fulfillment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    store_id: UUID,
    resource_ids: Sequence[UUID],
    start_at: datetime,
    end_at: datetime,
    now: datetime,
) -> None:
    """提交与占位都要复核的依赖事实。

    调用方必须已按锁顺序锁住 store 与 resource：单纯读一次最新版本并不足够，
    与后台修改共享同一套锁才能避免"读完再变化"的二次 TOCTOU（设计稿 §8.2）。
    """

    if start_at <= now:
        raise stale_proposal("预约时段已开始或已过去，请重新查询可用时间")

    store = (
        await session.execute(
            select(m.Store).where(m.Store.tenant_id == tenant_id, m.Store.id == store_id)
        )
    ).scalar_one_or_none()
    if store is None or store.status != "ACTIVE":
        raise stale_proposal("门店已停用或关闭")

    wanted = set(resource_ids)
    resources = (
        await session.execute(
            select(m.Resource).where(
                m.Resource.tenant_id == tenant_id, m.Resource.id.in_(list(wanted))
            )
        )
    ).scalars().all()
    if {r.id for r in resources} != wanted:
        raise stale_proposal("资源不存在")
    if any(r.status != "ACTIVE" for r in resources):
        raise stale_proposal("资源已停用")

    shifts = (
        await session.execute(
            select(m.Shift.resource_id).where(
                m.Shift.tenant_id == tenant_id,
                m.Shift.resource_id.in_(list(wanted)),
                m.Shift.status == "SCHEDULED",
                m.Shift.start_at <= start_at,
                m.Shift.end_at >= end_at,
            )
        )
    ).scalars().all()
    if wanted - set(shifts):
        raise stale_proposal("资源班次已变更，无法覆盖该时段")

    absences = (
        await session.execute(
            select(m.ResourceAbsence.resource_id).where(
                m.ResourceAbsence.tenant_id == tenant_id,
                m.ResourceAbsence.resource_id.in_(list(wanted)),
                m.ResourceAbsence.approval_status == "APPROVED",
                m.ResourceAbsence.start_at < end_at,
                m.ResourceAbsence.end_at > start_at,
            )
        )
    ).scalars().all()
    if absences:
        raise stale_proposal("资源在该时段有已批准的请假")

    tz = load_zone(store.timezone)
    local_dates = iter_local_dates(start_at, end_at, tz)
    closed = (
        await session.execute(
            select(m.CalendarException.local_date).where(
                m.CalendarException.tenant_id == tenant_id,
                m.CalendarException.store_id == store_id,
                m.CalendarException.kind == "CLOSED",
                m.CalendarException.local_date.in_(local_dates),
            )
        )
    ).scalars().all()
    if closed:
        raise stale_proposal("门店在该日期临时闭店")

    return None


# ---------------------------------------------------------------------------
# 状态推进辅助
# ---------------------------------------------------------------------------


def _advance_to_waiting_confirmation(current: TaskState) -> TaskState:
    """按合法迁移路径推进到 WAITING_CONFIRMATION。

    忠实于设计稿 §18.2 的图：SEARCHING → PROPOSED → WAITING_CONFIRMATION，
    而不是走一条图上不存在的捷径。
    """

    path: dict[TaskState, list[TaskState]] = {
        TaskState.SEARCHING: [TaskState.PROPOSED, TaskState.WAITING_CONFIRMATION],
        TaskState.PROPOSED: [TaskState.WAITING_CONFIRMATION],
        TaskState.NEEDS_REPLAN: [
            TaskState.SEARCHING,
            TaskState.PROPOSED,
            TaskState.WAITING_CONFIRMATION,
        ],
        TaskState.WAITING_USER: [
            TaskState.COLLECTING,
            TaskState.SEARCHING,
            TaskState.PROPOSED,
            TaskState.WAITING_CONFIRMATION,
        ],
        # 重新占位：保持在同一状态，但仍校验它是否是合法起点。
        TaskState.WAITING_CONFIRMATION: [],
    }
    steps = path.get(current)
    if steps is None:
        raise validation_error(
            f"任务当前状态 {current.value} 不能创建占位；"
            "请先补全槽位并查询可用时间"
        )
    cursor = current
    for step in steps:
        state_machine.assert_transition(cursor, step)
        cursor = step
    return cursor


def _translate_allocation_conflict(exc: IntegrityError) -> DomainError:
    """把排他约束/唯一索引冲突翻译成可读的业务错误。

    这是数据库对并发写入的最终裁决：两个事务的读后检查可能都通过了，
    但只有一个能提交成功。
    """

    message = str(getattr(exc, "orig", exc))
    if "ex_resource_allocation_no_overlap" in message:
        return slot_conflict(
            "该时段刚被占用，请刷新候选后重新选择",
            constraint="ex_resource_allocation_no_overlap",
        )
    if "uq_hold_customer_active" in message:
        return slot_conflict(
            "该客户已有未完成的占位，请先确认或等待其过期",
            constraint="uq_hold_customer_active",
        )
    if "uq_waiting_request_open_per_task" in message:
        return version_conflict("任务已有进行中的等待请求")
    return DomainError(ErrorCode.VALIDATION_ERROR, "写入违反数据库约束", retryable=True)


# ---------------------------------------------------------------------------
# 确认凭据
# ---------------------------------------------------------------------------


def confirmation_token_for(confirmation: m.Confirmation) -> str:
    """由 nonce 与方案哈希确定性重建凭据。

    只在服务端使用；重放路径靠它把原凭据还给同一个客户端，而不新建凭据。
    """

    return f"{CONFIRMATION_TOKEN_HEADER}.{confirmation.nonce}.{confirmation.proposal_hash}"


async def issue_confirmation(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    task_id: UUID,
    proposal: m.Proposal,
    now: datetime,
) -> tuple[m.Confirmation, str]:
    """签发方案级确认凭据。

    签发不等于用户已确认：AskUser metadata、SDK UserConfirmResultEvent、普通
    工具许可都不能创建它（设计稿 §5.5、§8.3）。
    """

    nonce = uuid4().hex
    row = m.Confirmation(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        task_id=task_id,
        actor_id=ctx.actor_id,
        proposal_id=proposal.id,
        proposal_version=proposal.version,
        proposal_hash=proposal.content_hash,
        action=proposal.action,
        token_hash=hash_secret(
            f"{CONFIRMATION_TOKEN_HEADER}.{nonce}.{proposal.content_hash}"
        ),
        nonce=nonce,
        expires_at=proposal.expires_at,
        status=ConfirmationStatus.ISSUED.value,
    )
    session.add(row)
    await session.flush()
    return row, confirmation_token_for(row)


async def record_confirmation_event(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    confirmation: m.Confirmation,
    client_event_id: str,
    now: datetime,
) -> None:
    """记录可信确认事件。

    UNIQUE(tenant_id, actor_id, client_event_id) 保证同一事件只受理一次。
    """

    if confirmation.actor_id != ctx.actor_id:
        raise permission_denied("确认凭据不属于当前用户")
    if confirmation.status not in (
        ConfirmationStatus.ISSUED.value,
        ConfirmationStatus.CONFIRMED.value,
    ):
        raise confirmation_required(f"确认凭据状态为 {confirmation.status}，不能再次使用")
    confirmation.status = ConfirmationStatus.CONFIRMED.value
    confirmation.client_event_id = client_event_id
    confirmation.confirmed_at = now


# ---------------------------------------------------------------------------
# create_hold
# ---------------------------------------------------------------------------


async def _requirements_for_snapshot(
    session: AsyncSession, *, tenant_id: UUID, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """从方案/订单快照取出服务版本要求，供提交路径复核技能与资源组合。"""

    service_version_id = snapshot.get("service_version_id")
    if not service_version_id:
        raise stale_proposal("快照缺少服务版本信息，请重新查询时段")
    return await load_service_version_requirements(
        session, tenant_id=tenant_id, service_version_id=UUID(str(service_version_id))
    )


def _quote_offer_from_token(quote_token: str, service: ServiceFacts) -> QuoteOffer:
    """解析并校验报价凭据，构造锁价快照。"""

    payload = verify_quote_token(quote_token)
    if payload.get("service_version_id") != str(service.service_version_id):
        raise stale_proposal("报价对应的服务版本已变化，请重新获取报价")
    return QuoteOffer(
        quote_id=UUID(payload["quote_id"]),
        quote_version=int(payload["quote_version"]),
        service_version_id=service.service_version_id,
        price_version_id=UUID(payload["price_version_id"]),
        duration_minutes=int(payload["duration_minutes"]),
        amount_minor=int(payload["amount_minor"]),
        currency=payload["currency"],
        currency_exponent=int(payload["currency_exponent"]),
        valid_until=datetime.fromisoformat(payload["valid_until"]),
        terms_hash=payload["terms_hash"],
        terms_snapshot={},
        lock_policy=payload.get("lock_policy", "VALID_WINDOW_LOCK"),
        quote_token=quote_token,
    )


async def _load_hold_outcome(
    session: AsyncSession, handle: OperationHandle, *, tenant_id: UUID
) -> HoldOutcome | None:
    """从已成功操作重建占位结果（幂等重放）。

    重放不重新生成业务效果，只读回原结果；凭据由 nonce 与方案哈希确定性重建，
    同一客户得到同一个凭据，不会因为重试产生第二个占位。
    """

    result = handle.operation.result or {}
    hold_id = result.get("hold_id")
    proposal_id = result.get("proposal_id")
    proposal_version = result.get("proposal_version")
    confirmation_id = result.get("confirmation_id")
    if not (hold_id and proposal_id and proposal_version and confirmation_id):
        return None

    hold = (
        await session.execute(
            select(m.Hold).where(
                m.Hold.tenant_id == tenant_id, m.Hold.id == UUID(hold_id)
            )
        )
    ).scalar_one_or_none()
    proposal = (
        await session.execute(
            select(m.Proposal).where(
                m.Proposal.tenant_id == tenant_id,
                m.Proposal.id == UUID(proposal_id),
                m.Proposal.version == int(proposal_version),
            )
        )
    ).scalar_one_or_none()
    confirmation = (
        await session.execute(
            select(m.Confirmation).where(
                m.Confirmation.tenant_id == tenant_id,
                m.Confirmation.id == UUID(confirmation_id),
            )
        )
    ).scalar_one_or_none()
    if hold is None or proposal is None or confirmation is None:
        return None
    allocations = list(
        (
            await session.execute(
                select(m.ResourceAllocation).where(
                    m.ResourceAllocation.tenant_id == tenant_id,
                    m.ResourceAllocation.hold_id == hold.id,
                )
            )
        ).scalars()
    )
    return HoldOutcome(
        hold=hold,
        proposal=proposal,
        confirmation=confirmation,
        confirmation_token=confirmation_token_for(confirmation),
        task_version=proposal.version,
        allocations=allocations,
        replayed=True,
    )


async def create_hold(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    task: m.Task,
    expected_task_version: int,
    store_id: UUID,
    service_id: UUID,
    candidate_id: str,
    start_at: datetime,
    end_at: datetime,
    resource_ids: Sequence[UUID],
    quote_token: str,
    now: datetime,
    clock: Clock | None = None,
) -> HoldOutcome:
    """创建单个短期占位并发布方案。

    用户选定一个候选后才创建**单个**占位；不为所有推荐方案同时锁资源。

    ``clock`` 只影响"拿锁后的裁决时刻"的来源（生产为数据库时钟，测试为受控时钟）。
    """

    ctx.require("appointment:create")
    ctx.require_store_scope(store_id)
    customer_id = ctx.require_customer()

    sequencer = LockSequencer()
    locked_task = await lock_task(
        session, sequencer, tenant_id=ctx.tenant_id, task_id=task.id
    )
    if locked_task.version != expected_task_version:
        raise version_conflict(
            "任务已被更新，请基于最新状态重试",
            expected_version=expected_task_version,
            actual_version=locked_task.version,
        )
    if ctx.epoch is not None and locked_task.epoch != ctx.epoch:
        raise DomainError(ErrorCode.LEASE_LOST, "任务控制权代际已变化")

    service = await load_service(
        session, tenant_id=ctx.tenant_id, store_id=store_id, service_id=service_id
    )

    # 候选 ID 必须与内容一致：候选不是占位凭据，服务端在此重新校验它。
    if not hmac.compare_digest(
        candidate_id_for(
            service_version_id=service.service_version_id,
            start_at=start_at,
            end_at=end_at,
            resource_ids=list(resource_ids),
        ),
        candidate_id,
    ):
        raise validation_error("候选 ID 与候选内容不一致，请重新查询可用时间")
    if end_at <= start_at:
        raise validation_error("预约区间非法")
    if end_at - start_at != timedelta(minutes=service.duration_minutes):
        raise validation_error("预约时长与服务时长不一致")
    buffer_minutes = int(service.requirements.get("buffer_minutes", 0) or 0)
    allocation_end_at = end_at + timedelta(minutes=buffer_minutes)

    # 锁顺序：客户配额 guard → 门店 → 资源 → 资源日。
    await lock_customer_hold_guard(
        session, sequencer, tenant_id=ctx.tenant_id, customer_id=customer_id
    )
    await expire_due_holds(
        session,
        tenant_id=ctx.tenant_id,
        customer_id=customer_id,
        now=await live_now(session, clock),
    )
    store = await lock_store(
        session, sequencer, tenant_id=ctx.tenant_id, store_id=store_id
    )

    ordered_resource_ids = sorted(set(resource_ids), key=str)
    for rid in ordered_resource_ids:
        await lock_resource(session, sequencer, tenant_id=ctx.tenant_id, resource_id=rid)

    tz = load_zone(store.timezone)
    days = iter_local_dates(start_at, allocation_end_at, tz)
    await lock_resource_day(
        session,
        sequencer,
        tenant_id=ctx.tenant_id,
        resource_id=ordered_resource_ids[0],
        resource_ids_days=[(rid, day) for rid in ordered_resource_ids for day in days],
    )

    # 拿到锁之后才是有效裁决时间点。
    decision_now = await live_now(session, clock)
    await expire_due_allocations(
        session,
        tenant_id=ctx.tenant_id,
        resource_ids=ordered_resource_ids,
        now=decision_now,
    )

    # 在登记写操作之前复核候选仍然有效，避免过期候选留下 PENDING 操作。
    # 门店与资源锁已持有，因此班次、请假和闭店判断不会与并发变更交错。
    await validate_fulfillment(
        session,
        tenant_id=ctx.tenant_id,
        store_id=store_id,
        resource_ids=ordered_resource_ids,
        start_at=start_at,
        end_at=allocation_end_at,
        now=decision_now,
    )
    await validate_resource_composition(
        session,
        tenant_id=ctx.tenant_id,
        store_id=store_id,
        resource_ids=ordered_resource_ids,
        requirements=service.requirements,
    )

    request_payload = {
        "action": ProposalAction.CREATE.value,
        "task_id": str(task.id),
        "store_id": str(store_id),
        "service_id": str(service_id),
        "candidate_id": candidate_id,
        "start_at": start_at,
        "end_at": end_at,
        "resources": [str(rid) for rid in ordered_resource_ids],
        "expected_task_version": expected_task_version,
    }
    key = build_idempotency_key(
        tenant_id=str(ctx.tenant_id),
        task_id=str(task.id),
        proposal_version=locked_task.version + 1,
        action=ProposalAction.CREATE.value,
    )
    handle = await register_operation(
        session,
        ctx,
        action=ProposalAction.CREATE,
        idempotency_key=key,
        request_payload=request_payload,
        now=decision_now,
        task_id=task.id,
        customer_id=customer_id,
    )
    if handle.is_replay and handle.finished:
        replayed = await _load_hold_outcome(session, handle, tenant_id=ctx.tenant_id)
        if replayed is None:
            raise version_conflict("原占位操作已完成，但关联占位不可用，请重新查询时间")
        return replayed

    offer = _quote_offer_from_token(quote_token, service)
    quote_row = await persist_quote_snapshot(
        session,
        tenant_id=ctx.tenant_id,
        customer_id=customer_id,
        store_id=store_id,
        offer=offer,
        now=decision_now,
    )

    settings = get_settings()
    hold_ttl = min(
        settings.hold_ttl_seconds,
        max(int((offer.valid_until - decision_now).total_seconds()), 1),
    )
    expires_at = decision_now + timedelta(seconds=hold_ttl)

    hold = m.Hold(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        task_id=task.id,
        customer_id=customer_id,
        store_id=store_id,
        quote_id=quote_row.id,
        action=ProposalAction.CREATE.value,
        state=HoldState.HELD.value,
        expires_at=expires_at,
        created_operation_id=handle.operation.id,
    )
    session.add(hold)
    await session.flush()

    allocations: list[m.ResourceAllocation] = []
    try:
        for rid in ordered_resource_ids:
            allocation = m.ResourceAllocation(
                id=uuid4(),
                tenant_id=ctx.tenant_id,
                store_id=store_id,
                resource_id=rid,
                hold_id=hold.id,
                appointment_id=None,
                start_at=start_at,
                end_at=allocation_end_at,
                state=AllocationState.HELD.value,
                expires_at=expires_at,
            )
            session.add(allocation)
            allocations.append(allocation)
        # 排他约束在此裁决：任一资源冲突则全部回滚，不存在"部分资源成功"。
        await session.flush()
    except IntegrityError as exc:
        raise _translate_allocation_conflict(exc) from exc

    resource_snapshot = [
        {"resource_id": str(rid), "start_at": start_at, "end_at": end_at}
        for rid in ordered_resource_ids
    ]
    canonical_content = json_safe(
        {
            "action": ProposalAction.CREATE.value,
            "store_id": str(store_id),
            "service_id": str(service_id),
            "service_name": service.service_name,
            "service_version_id": str(service.service_version_id),
            "duration_minutes": service.duration_minutes,
            "start_at": start_at,
            "end_at": end_at,
            "resources": resource_snapshot,
            "amount_minor": offer.amount_minor,
            "currency": offer.currency,
            "currency_exponent": offer.currency_exponent,
            "terms_hash": offer.terms_hash,
        }
    )
    proposal = m.Proposal(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        # 方案版本取"任务版本 + 1"：任务版本在每次状态推进/乐观锁更新时严格递增，
        # 因此同一任务下的方案版本严格递增，旧凭据无法在新方案上生效。
        version=locked_task.version + 1,
        task_id=task.id,
        action=ProposalAction.CREATE.value,
        hold_id=hold.id,
        quote_id=quote_row.id,
        canonical_content=canonical_content,
        content_hash=content_hash(canonical_content),
        dependency_snapshot={
            "store_calendar_version": store.calendar_version,
            "service_version_id": str(service.service_version_id),
            "resources": [str(rid) for rid in ordered_resource_ids],
            "shift_snapshot_at": decision_now.isoformat(),
        },
        expires_at=expires_at,
        status=ProposalStatus.ACTIVE.value,
    )
    session.add(proposal)
    # confirmation 通过组合外键 (tenant_id, proposal_id, proposal_version) 指向
    # proposal。本项目有意不使用 relationship，ORM 的隐式排序不可依赖，
    # 因此在这里显式落盘，保证引用行先于引用它的行写入。
    await session.flush()

    confirmation, token = await issue_confirmation(
        session, ctx, task_id=task.id, proposal=proposal, now=decision_now
    )

    new_state = _advance_to_waiting_confirmation(TaskState(locked_task.state))
    locked_task.state = new_state.value
    locked_task.version = locked_task.version + 1
    locked_task.current_proposal_id = proposal.id
    locked_task.current_proposal_version = proposal.version

    await _supersede_open_waiting(session, tenant_id=ctx.tenant_id, task_id=task.id)
    session.add(
        m.WaitingRequest(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            task_id=task.id,
            kind=WaitingKind.CONFIRMATION.value,
            proposal_id=proposal.id,
            proposal_version=proposal.version,
            task_version=locked_task.version,
            epoch=locked_task.epoch,
            release_id=locked_task.release_id,
            input_schema={
                "type": "object",
                "properties": {
                    "decision": {"enum": ["confirm", "decline"]},
                    "client_confirmation_event_id": {"type": "string"},
                },
                "required": ["decision"],
            },
            expires_at=expires_at,
            status=WaitingStatus.OPEN.value,
        )
    )

    await emit_task_event(
        session,
        task=locked_task,
        event_type=TaskEventType.PROPOSAL_PUBLISHED,
        payload={
            "proposal_id": str(proposal.id),
            "proposal_version": proposal.version,
            "hold_id": str(hold.id),
            "expires_at": expires_at.isoformat(),
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "resource_snapshot": [
                {
                    "resource_id": str(rid),
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat(),
                }
                for rid in ordered_resource_ids
            ],
            "amount_minor": offer.amount_minor,
            "currency": offer.currency,
            "service_name": service.service_name,
            "duration_minutes": service.duration_minutes,
        },
        occurred_at=decision_now,
    )
    await record_audit(
        session,
        ctx,
        action="create_hold",
        object_type="hold",
        object_id=hold.id,
        occurred_at=decision_now,
        after_version=locked_task.version,
        operation_id=handle.operation.id,
        protected_detail={"proposal_id": str(proposal.id)},
    )

    await mark_succeeded(
        session,
        handle.operation,
        result={
            "hold_id": str(hold.id),
            "proposal_id": str(proposal.id),
            "proposal_version": proposal.version,
            "confirmation_id": str(confirmation.id),
            "task_version": locked_task.version,
        },
        now=decision_now,
    )
    await session.flush()

    return HoldOutcome(
        hold=hold,
        proposal=proposal,
        confirmation=confirmation,
        confirmation_token=token,
        task_version=locked_task.version,
        allocations=allocations,
    )


# ---------------------------------------------------------------------------
# confirm_appointment
# ---------------------------------------------------------------------------


def _create_request_payload(
    *,
    proposal_id: UUID,
    proposal_version: int,
    proposal_hash: str,
    task_id: UUID,
    expected_task_version: int,
) -> dict[str, Any]:
    """CREATE 动作的规范化请求体。

    幂等判定与登记必须看到**同一份**内容，所以这里抽成一个函数：如果两处各拼
    一次，将来改字段就会悄悄改变哈希，让"相同键同参数"在重试时变成
    ``IDEMPOTENCY_MISMATCH``。
    """

    return {
        "action": ProposalAction.CREATE.value,
        "proposal_id": str(proposal_id),
        "proposal_version": proposal_version,
        "proposal_hash": proposal_hash,
        "task_id": str(task_id),
        "expected_task_version": expected_task_version,
    }


async def _replay_commit(
    session: AsyncSession, ctx: TrustedContext, handle: OperationHandle
) -> CommitOutcome:
    """重放一次已受理的确认。

    先取原结果再读订单：结果里的 ``appointment_id`` 是操作的权威产物，
    读不到说明数据被破坏，宁可报错也不要新造一笔订单。
    """

    result = replay_result(handle)
    appointment = (
        await session.execute(
            select(m.Appointment).where(
                m.Appointment.tenant_id == ctx.tenant_id,
                m.Appointment.id == UUID(result["appointment_id"]),
            )
        )
    ).scalar_one()
    return CommitOutcome(
        appointment=appointment,
        operation=handle.operation,
        task_version=int(result.get("task_version", 0)),
        replayed=True,
    )


async def confirm_appointment(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    proposal_id: UUID,
    proposal_version: int,
    confirmation_token: str,
    client_confirmation_event_id: str,
    idempotency_key: str,
    expected_task_version: int,
    now: datetime,
    clock: Clock | None = None,
) -> CommitOutcome:
    """提交预约。

    同一事务中锁定并校验任务、授权、占位和依赖事实，然后更新占用为 BOOKED、
    写订单、记录操作结果、写 Outbox 和审计。

    两个顺序是刻意安排的：

    1. **幂等先于版本裁决**。确认成功会把 task.version 推进一位，所以"响应丢失
       后同键重试"如果先去比 ``expected_task_version``，就必然被自己的第一次
       提交判成 ``VERSION_CONFLICT``——幂等键形同虚设。因此登记/取回原操作排在
       版本检查之前：已有 SUCCEEDED 原操作就直接重放原订单。
    2. **锁按声明的层次升序获取** （hold → proposal → confirmation，见
       ``LOCK_RANK``）：先无锁读关系确定要锁哪些行，再按顺序加锁并重读，
       避免与其他写路径形成交叉等待。
    """

    ctx.require("appointment:create")
    customer_id = ctx.require_customer()
    sequencer = LockSequencer()

    # --- 无锁读关系：确定要锁哪些行 -------------------------------------
    relation = (
        await session.execute(
            select(
                m.Proposal.task_id,
                m.Proposal.hold_id,
                m.Proposal.quote_id,
                m.Proposal.version,
            ).where(
                m.Proposal.tenant_id == ctx.tenant_id,
                m.Proposal.id == proposal_id,
                m.Proposal.version == proposal_version,
            )
        )
    ).one_or_none()
    if relation is None:
        raise not_found("方案不存在或无权访问")

    confirmation_peek = (
        await session.execute(
            select(
                m.Confirmation.id,
                m.Confirmation.proposal_id,
                m.Confirmation.proposal_version,
                m.Confirmation.proposal_hash,
            ).where(
                m.Confirmation.tenant_id == ctx.tenant_id,
                m.Confirmation.token_hash == hash_secret(confirmation_token),
            )
        )
    ).one_or_none()
    if confirmation_peek is None:
        raise confirmation_required("确认凭据无效")
    if (
        confirmation_peek.proposal_id != proposal_id
        or confirmation_peek.proposal_version != proposal_version
    ):
        raise confirmation_required("确认凭据与当前方案不匹配")

    locked_task = await lock_task(
        session, sequencer, tenant_id=ctx.tenant_id, task_id=relation.task_id
    )

    request_payload = _create_request_payload(
        proposal_id=proposal_id,
        proposal_version=proposal_version,
        proposal_hash=confirmation_peek.proposal_hash,
        task_id=locked_task.id,
        expected_task_version=expected_task_version,
    )
    handle = await register_operation(
        session,
        ctx,
        action=ProposalAction.CREATE,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        now=now,
        task_id=locked_task.id,
        customer_id=customer_id,
        proposal_id=proposal_id,
        proposal_version=proposal_version,
        confirmation_id=confirmation_peek.id,
    )
    if handle.is_replay:
        return await _replay_commit(session, ctx, handle)

    # 先读取占位关联以确定锁集合，再按统一的低到高等级加锁。拿到占位锁后
    # 还要复核集合未变化；无锁读取本身不构成履约依据。
    hold_peek = (
        await session.execute(
            select(m.Hold.store_id).where(
                m.Hold.tenant_id == ctx.tenant_id, m.Hold.id == relation.hold_id
            )
        )
    ).scalar_one_or_none()
    if hold_peek is None:
        raise stale_proposal("占位不存在")
    ctx.require_store_scope(hold_peek)
    allocation_peek = list(
        (
            await session.execute(
                select(
                    m.ResourceAllocation.resource_id,
                    m.ResourceAllocation.start_at,
                    m.ResourceAllocation.end_at,
                ).where(
                    m.ResourceAllocation.tenant_id == ctx.tenant_id,
                    m.ResourceAllocation.hold_id == relation.hold_id,
                    m.ResourceAllocation.state == AllocationState.HELD.value,
                )
            )
        ).all()
    )
    if not allocation_peek:
        raise stale_proposal("占位没有有效资源占用")
    store_guard = await lock_store(
        session, sequencer, tenant_id=ctx.tenant_id, store_id=hold_peek
    )
    peek_resource_ids = sorted({row.resource_id for row in allocation_peek}, key=str)
    for resource_id in peek_resource_ids:
        await lock_resource(
            session, sequencer, tenant_id=ctx.tenant_id, resource_id=resource_id
        )
    store_zone = load_zone(store_guard.timezone)
    await lock_resource_day(
        session,
        sequencer,
        tenant_id=ctx.tenant_id,
        resource_id=peek_resource_ids[0],
        resource_ids_days=[
            (row.resource_id, day)
            for row in allocation_peek
            for day in iter_local_dates(row.start_at, row.end_at, store_zone)
        ],
    )

    # --- 只有全新操作才做版本与代际裁决 ---------------------------------
    if locked_task.version != expected_task_version:
        raise version_conflict(
            "任务已被更新，旧确认不再适用",
            expected_version=expected_task_version,
            actual_version=locked_task.version,
        )
    if ctx.epoch is not None and locked_task.epoch != ctx.epoch:
        raise DomainError(ErrorCode.LEASE_LOST, "任务控制权代际已变化")

    # --- 按锁层次升序加锁并重读：占位 → 方案 → 凭据 -----------------------
    hold = await lock_hold(
        session, sequencer, tenant_id=ctx.tenant_id, hold_id=relation.hold_id
    )
    # 拿到锁之后的实际时间：占位过期瞬间确认必须失败，而不是被放行。
    decision_now = await live_now(session, clock)

    proposal_locked = await lock_proposal(
        session,
        sequencer,
        tenant_id=ctx.tenant_id,
        proposal_id=proposal_id,
        proposal_version=proposal_version,
    )
    confirmation = await lock_confirmation(
        session,
        sequencer,
        tenant_id=ctx.tenant_id,
        confirmation_id=confirmation_peek.id,
    )

    if proposal_locked.status != ProposalStatus.ACTIVE.value:
        raise stale_proposal(f"方案状态为 {proposal_locked.status}，需要重新确认")
    if proposal_locked.expires_at <= decision_now:
        raise hold_expired("方案已过期，请重新查询时段并再次确认")
    if proposal_locked.content_hash != confirmation.proposal_hash:
        # 门店、技师、时间、服务、金额或关键条款变化时旧确认必须失效。
        raise stale_proposal("方案内容已变化，旧确认失效")
    if confirmation.status not in (
        ConfirmationStatus.ISSUED.value,
        ConfirmationStatus.CONFIRMED.value,
    ):
        raise confirmation_required(f"确认凭据状态为 {confirmation.status}")
    if confirmation.expires_at <= decision_now:
        raise confirmation_required("确认凭据已过期，请重新获取确认方案")

    if hold.state != HoldState.HELD.value:
        raise hold_expired(f"占位已处于 {hold.state} 状态")
    if hold.expires_at <= decision_now:
        await session.execute(
            update(m.ResourceAllocation)
            .where(
                m.ResourceAllocation.tenant_id == ctx.tenant_id,
                m.ResourceAllocation.hold_id == hold.id,
                m.ResourceAllocation.state == AllocationState.HELD.value,
            )
            .values(
                state=AllocationState.EXPIRED.value,
                version=m.ResourceAllocation.version + 1,
                updated_at=decision_now,
            )
        )
        hold.state = HoldState.EXPIRED.value
        raise hold_expired("占位已过期，请重新查询时段并再次确认")

    allocations = list(
        (
            await session.execute(
                select(m.ResourceAllocation)
                .where(
                    m.ResourceAllocation.tenant_id == ctx.tenant_id,
                    m.ResourceAllocation.hold_id == hold.id,
                    m.ResourceAllocation.state == AllocationState.HELD.value,
                )
                .order_by(m.ResourceAllocation.resource_id)
                .with_for_update()
            )
        ).scalars()
    )
    if not allocations:
        raise stale_proposal("占位没有有效资源占用")

    start_at = min(a.start_at for a in allocations)
    allocation_end_at = max(a.end_at for a in allocations)
    resource_ids = [a.resource_id for a in allocations]
    if hold.store_id != hold_peek or set(resource_ids) != set(peek_resource_ids):
        raise stale_proposal("占位资源已变化，请重新确认")

    requirements = await _requirements_for_snapshot(
        session, tenant_id=ctx.tenant_id, snapshot=proposal_locked.canonical_content
    )
    duration_minutes = int(proposal_locked.canonical_content.get("duration_minutes", 0))
    if duration_minutes <= 0:
        raise stale_proposal("方案缺少有效服务时长")
    end_at = start_at + timedelta(minutes=duration_minutes)
    expected_allocation_end = end_at + timedelta(
        minutes=int(requirements.get("buffer_minutes", 0) or 0)
    )
    if allocation_end_at != expected_allocation_end:
        raise stale_proposal("占位资源区间与服务缓冲时长不一致")

    await validate_fulfillment(
        session,
        tenant_id=ctx.tenant_id,
        store_id=hold.store_id,
        resource_ids=resource_ids,
        start_at=start_at,
        end_at=allocation_end_at,
        now=decision_now,
    )
    await validate_resource_composition(
        session,
        tenant_id=ctx.tenant_id,
        store_id=hold.store_id,
        resource_ids=resource_ids,
        requirements=requirements,
    )

    quote = (
        await session.execute(
            select(m.Quote).where(
                m.Quote.tenant_id == ctx.tenant_id, m.Quote.id == proposal_locked.quote_id
            )
        )
    ).scalar_one_or_none()
    if quote is None or quote.revoked_at is not None or quote.valid_until <= decision_now:
        raise stale_proposal("报价已失效，请重新确认价格")

    content = proposal_locked.canonical_content
    appointment = m.Appointment(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        customer_id=hold.customer_id,
        store_id=hold.store_id,
        status=AppointmentStatus.CONFIRMED.value,
        fulfillment_status=FulfillmentStatus.READY.value,
        service_snapshot={
            "service_id": content.get("service_id"),
            "service_name": content.get("service_name"),
            "service_version_id": content.get("service_version_id"),
            "duration_minutes": content.get("duration_minutes"),
        },
        resource_snapshot=content.get("resources", []),
        start_at=start_at,
        end_at=end_at,
        amount_minor=quote.amount_minor,
        currency=quote.currency,
        currency_exponent=quote.currency_exponent,
        terms_snapshot=quote.terms_snapshot,
        created_operation_id=handle.operation.id,
        last_operation_id=handle.operation.id,
        quote_id=quote.id,
    )
    session.add(appointment)
    await session.flush()

    # 回填操作产出的订单：否则"按订单查操作"只能靠 request_hash 猜，而
    # 「结果不明时先查原操作」正是靠这条链路才能落地。
    handle.operation.target_appointment_id = appointment.id

    for allocation in allocations:
        allocation.state = AllocationState.BOOKED.value
        allocation.appointment_id = appointment.id
        allocation.expires_at = None
        allocation.version = allocation.version + 1

    hold.state = HoldState.BOOKED.value
    hold.booked_appointment_id = appointment.id
    hold.version = hold.version + 1

    confirmation.status = ConfirmationStatus.CONSUMED.value
    confirmation.consumed_operation_id = handle.operation.id
    if not confirmation.client_event_id:
        confirmation.client_event_id = client_confirmation_event_id
        confirmation.confirmed_at = decision_now

    proposal_locked.status = ProposalStatus.COMMITTED.value

    session.add(
        m.AppointmentRevision(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            appointment_id=appointment.id,
            version=appointment.version,
            snapshot={
                "status": appointment.status,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "resources": [str(rid) for rid in resource_ids],
            },
            operation_id=handle.operation.id,
            reason_code="CREATE",
            actor_id=ctx.actor_id,
        )
    )

    state_machine.assert_transition(TaskState(locked_task.state), TaskState.COMMITTING)
    state_machine.assert_transition(TaskState.COMMITTING, TaskState.SUCCEEDED)
    locked_task.state = TaskState.SUCCEEDED.value
    locked_task.version = locked_task.version + 1
    locked_task.current_proposal_id = None
    locked_task.current_proposal_version = None

    await _close_waiting(
        session,
        tenant_id=ctx.tenant_id,
        task_id=locked_task.id,
        status=WaitingStatus.ANSWERED,
        now=decision_now,
    )

    result = {
        "appointment_id": str(appointment.id),
        "committed_status": appointment.status,
        "appointment_version": appointment.version,
        "operation_id": str(handle.operation.id),
        "task_version": locked_task.version,
    }
    await mark_succeeded(session, handle.operation, result=result, now=decision_now)

    await emit_task_event(
        session,
        task=locked_task,
        event_type=TaskEventType.APPOINTMENT_COMMITTED,
        payload={
            "appointment_id": str(appointment.id),
            "store_id": str(appointment.store_id),
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "amount_minor": appointment.amount_minor,
            "currency": appointment.currency,
            "status": appointment.status,
            "service_name": content.get("service_name"),
            "resources": [str(rid) for rid in resource_ids],
        },
        occurred_at=decision_now,
    )
    await emit_outbox(
        session,
        tenant_id=ctx.tenant_id,
        aggregate_type="appointment",
        aggregate_id=appointment.id,
        aggregate_version=appointment.version,
        event_type="appointment_confirmed",
        payload={
            "appointment_id": str(appointment.id),
            "customer_id": str(appointment.customer_id),
            "store_id": str(appointment.store_id),
            "start_at": start_at.isoformat(),
            "channel": "sms",
        },
        occurred_at=decision_now,
    )
    await record_audit(
        session,
        ctx,
        action="confirm_appointment",
        object_type="appointment",
        object_id=appointment.id,
        occurred_at=decision_now,
        after_version=appointment.version,
        operation_id=handle.operation.id,
        confirmation_id=confirmation.id,
    )

    await session.flush()
    return CommitOutcome(
        appointment=appointment,
        operation=handle.operation,
        task_version=locked_task.version,
    )


# ---------------------------------------------------------------------------
# 取消与改约
# ---------------------------------------------------------------------------


async def cancel_appointment(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    appointment_id: UUID,
    expected_appointment_version: int,
    idempotency_key: str,
    reason_code: str,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """取消。原子变更订单、资源占用与事件；不物理移除订单历史。"""

    ctx.require("appointment:cancel")
    sequencer = LockSequencer()
    from ..db.session import lock_appointment

    appointment = await lock_appointment(
        session, sequencer, tenant_id=ctx.tenant_id, appointment_id=appointment_id
    )
    if ctx.role == "customer" and appointment.customer_id != ctx.customer_id:
        raise not_found("预约不存在或无权访问")

    handle = await register_operation(
        session,
        ctx,
        action=ProposalAction.CANCEL,
        idempotency_key=idempotency_key,
        request_payload={
            "action": ProposalAction.CANCEL.value,
            "appointment_id": str(appointment_id),
            "expected_appointment_version": expected_appointment_version,
            "reason_code": reason_code,
        },
        now=now,
        customer_id=appointment.customer_id,
        target_appointment_id=appointment_id,
    )
    if handle.is_replay and handle.finished:
        return replay_result(handle)

    if appointment.version != expected_appointment_version:
        raise version_conflict(
            "预约已被修改，请刷新后重试",
            expected_version=expected_appointment_version,
            actual_version=appointment.version,
        )
    if appointment.status != AppointmentStatus.CONFIRMED.value:
        raise version_conflict(
            f"预约当前状态为 {appointment.status}，不能取消",
            actual_version=appointment.version,
        )

    decision_now = await live_now(session, clock)
    released = (
        await session.execute(
            update(m.ResourceAllocation)
            .where(
                m.ResourceAllocation.tenant_id == ctx.tenant_id,
                m.ResourceAllocation.appointment_id == appointment.id,
                m.ResourceAllocation.state == AllocationState.BOOKED.value,
            )
            .values(
                state=AllocationState.RELEASED.value,
                version=m.ResourceAllocation.version + 1,
                updated_at=decision_now,
            )
            .returning(m.ResourceAllocation.id)
        )
    ).scalars().all()

    appointment.status = AppointmentStatus.CANCELLED.value
    appointment.version = appointment.version + 1
    appointment.last_operation_id = handle.operation.id

    session.add(
        m.AppointmentRevision(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            appointment_id=appointment.id,
            version=appointment.version,
            snapshot={"status": appointment.status, "reason_code": reason_code},
            operation_id=handle.operation.id,
            reason_code=reason_code,
            actor_id=ctx.actor_id,
        )
    )

    result = {
        "appointment_id": str(appointment.id),
        "committed_status": appointment.status,
        "appointment_version": appointment.version,
        "released_allocation_ids": [str(x) for x in released],
        "operation_id": str(handle.operation.id),
    }
    await mark_succeeded(session, handle.operation, result=result, now=decision_now)
    await record_audit(
        session,
        ctx,
        action="cancel_appointment",
        object_type="appointment",
        object_id=appointment.id,
        occurred_at=decision_now,
        before_version=expected_appointment_version,
        after_version=appointment.version,
        operation_id=handle.operation.id,
        reason_code=reason_code,
    )
    await session.flush()
    return result


async def reschedule_appointment(
    session: AsyncSession,
    ctx: TrustedContext,
    *,
    appointment_id: UUID,
    expected_appointment_version: int,
    new_start_at: datetime,
    new_end_at: datetime,
    new_resource_ids: Sequence[UUID],
    quote_token: str,
    idempotency_key: str,
    now: datetime,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """改约。

    同一本地数据库内优先单事务：按门店/资源/订单顺序锁定并检查 expected_version，校验新资源，
    变更资源占用、订单版本、操作结果与事件。任何新资源冲突都回滚，
    因此旧预约仍然有效。
    """

    ctx.require("appointment:reschedule")
    sequencer = LockSequencer()
    from ..db.session import lock_appointment

    appointment_peek = (
        await session.execute(
            select(
                m.Appointment.store_id,
                m.Appointment.start_at,
                m.Appointment.end_at,
                m.Appointment.service_snapshot,
            ).where(
                m.Appointment.tenant_id == ctx.tenant_id,
                m.Appointment.id == appointment_id,
            )
        )
    ).one_or_none()
    if appointment_peek is None:
        raise not_found("预约不存在或无权访问")
    ctx.require_store_scope(appointment_peek.store_id)
    old_allocation_peek = list(
        (
            await session.execute(
                select(
                    m.ResourceAllocation.resource_id,
                    m.ResourceAllocation.start_at,
                    m.ResourceAllocation.end_at,
                ).where(
                    m.ResourceAllocation.tenant_id == ctx.tenant_id,
                    m.ResourceAllocation.appointment_id == appointment_id,
                    m.ResourceAllocation.state == AllocationState.BOOKED.value,
                )
            )
        ).all()
    )
    old_resource_ids = {row.resource_id for row in old_allocation_peek}
    requirements = await _requirements_for_snapshot(
        session,
        tenant_id=ctx.tenant_id,
        snapshot=appointment_peek.service_snapshot or {},
    )
    allocation_end_at = new_end_at + timedelta(
        minutes=int(requirements.get("buffer_minutes", 0) or 0)
    )
    ordered = sorted(set(new_resource_ids), key=str)
    store_guard = await lock_store(
        session, sequencer, tenant_id=ctx.tenant_id, store_id=appointment_peek.store_id
    )
    for resource_id in sorted(old_resource_ids | set(ordered), key=str):
        await lock_resource(
            session, sequencer, tenant_id=ctx.tenant_id, resource_id=resource_id
        )
    zone = load_zone(store_guard.timezone)
    guard_days = [
        (resource_id, day)
        for old_allocation in old_allocation_peek
        for resource_id in (old_allocation.resource_id,)
        for day in iter_local_dates(old_allocation.start_at, old_allocation.end_at, zone)
    ] + [
        (resource_id, day)
        for resource_id in ordered
        for day in iter_local_dates(new_start_at, allocation_end_at, zone)
    ]
    if guard_days:
        await lock_resource_day(
            session, sequencer, tenant_id=ctx.tenant_id,
            resource_id=guard_days[0][0], resource_ids_days=guard_days,
        )

    appointment = await lock_appointment(
        session, sequencer, tenant_id=ctx.tenant_id, appointment_id=appointment_id
    )
    if ctx.role == "customer" and appointment.customer_id != ctx.customer_id:
        raise not_found("预约不存在或无权访问")

    handle = await register_operation(
        session,
        ctx,
        action=ProposalAction.RESCHEDULE,
        idempotency_key=idempotency_key,
        request_payload={
            "action": ProposalAction.RESCHEDULE.value,
            "appointment_id": str(appointment_id),
            "expected_appointment_version": expected_appointment_version,
            "new_start_at": new_start_at,
            "new_end_at": new_end_at,
            "resources": [str(rid) for rid in ordered],
        },
        now=now,
        customer_id=appointment.customer_id,
        target_appointment_id=appointment_id,
    )
    if handle.is_replay and handle.finished:
        return replay_result(handle)

    if appointment.version != expected_appointment_version:
        raise version_conflict(
            "预约已被修改，请刷新后重试",
            expected_version=expected_appointment_version,
            actual_version=appointment.version,
        )
    if appointment.status != AppointmentStatus.CONFIRMED.value:
        raise version_conflict(f"预约当前状态为 {appointment.status}，不能改约")

    decision_now = await live_now(session, clock)
    old_allocations = list(
        (
            await session.execute(
                select(m.ResourceAllocation)
                .where(
                    m.ResourceAllocation.tenant_id == ctx.tenant_id,
                    m.ResourceAllocation.appointment_id == appointment.id,
                    m.ResourceAllocation.state == AllocationState.BOOKED.value,
                )
                .order_by(m.ResourceAllocation.resource_id)
                .with_for_update()
            )
        ).scalars()
    )
    if not old_allocations:
        raise stale_proposal("预约没有有效资源占用")
    if (
        appointment.store_id != appointment_peek.store_id
        or {row.resource_id for row in old_allocations} != old_resource_ids
    ):
        raise stale_proposal("订单占用已变化，请刷新后重试")

    # 旧占用先释放再插入新占用：否则新时间与原占用重叠时会与"自己"冲突。
    # 事务失败会整体回滚，旧预约事实不变。
    for allocation in old_allocations:
        allocation.state = AllocationState.RELEASED.value
        allocation.version = allocation.version + 1
    await session.flush()

    await validate_fulfillment(
        session,
        tenant_id=ctx.tenant_id,
        store_id=appointment.store_id,
        resource_ids=ordered,
        start_at=new_start_at,
        end_at=allocation_end_at,
        now=decision_now,
    )
    await validate_resource_composition(
        session,
        tenant_id=ctx.tenant_id,
        store_id=appointment.store_id,
        resource_ids=ordered,
        requirements=requirements,
    )

    payload = verify_quote_token(quote_token)
    quote = (
        await session.execute(
            select(m.Quote).where(
                m.Quote.tenant_id == ctx.tenant_id, m.Quote.id == UUID(payload["quote_id"])
            )
        )
    ).scalar_one_or_none()
    if quote is None:
        raise stale_proposal("报价不存在")
    if quote.valid_until <= decision_now or quote.revoked_at is not None:
        raise stale_proposal("报价已失效")

    try:
        for rid in ordered:
            session.add(
                m.ResourceAllocation(
                    id=uuid4(),
                    tenant_id=ctx.tenant_id,
                    store_id=appointment.store_id,
                    resource_id=rid,
                    hold_id=None,
                    appointment_id=appointment.id,
                    start_at=new_start_at,
                    end_at=allocation_end_at,
                    state=AllocationState.BOOKED.value,
                    expires_at=None,
                )
            )
        await session.flush()
    except IntegrityError as exc:
        raise _translate_allocation_conflict(exc) from exc

    appointment.start_at = new_start_at
    appointment.end_at = new_end_at
    appointment.amount_minor = quote.amount_minor
    appointment.currency = quote.currency
    appointment.currency_exponent = quote.currency_exponent
    appointment.resource_snapshot = json_safe(
        [
            {
                "resource_id": str(rid),
                "start_at": new_start_at,
                "end_at": new_end_at,
            }
            for rid in ordered
        ]
    )
    appointment.version = appointment.version + 1
    appointment.last_operation_id = handle.operation.id

    session.add(
        m.AppointmentRevision(
            id=uuid4(),
            tenant_id=ctx.tenant_id,
            appointment_id=appointment.id,
            version=appointment.version,
            snapshot={
                "status": appointment.status,
                "start_at": new_start_at.isoformat(),
                "end_at": new_end_at.isoformat(),
                "resources": [str(rid) for rid in ordered],
            },
            operation_id=handle.operation.id,
            reason_code="RESCHEDULE",
            actor_id=ctx.actor_id,
        )
    )

    result = {
        "appointment_id": str(appointment.id),
        "appointment_version": appointment.version,
        "start_at": new_start_at.isoformat(),
        "end_at": new_end_at.isoformat(),
        "resources": [str(rid) for rid in ordered],
        "operation_id": str(handle.operation.id),
    }
    await mark_succeeded(session, handle.operation, result=result, now=decision_now)
    await record_audit(
        session,
        ctx,
        action="reschedule_appointment",
        object_type="appointment",
        object_id=appointment.id,
        occurred_at=decision_now,
        before_version=expected_appointment_version,
        after_version=appointment.version,
        operation_id=handle.operation.id,
    )
    await session.flush()
    return result


# ---------------------------------------------------------------------------
# 查询与等待辅助
# ---------------------------------------------------------------------------


async def get_appointment(
    session: AsyncSession, ctx: TrustedContext, *, appointment_id: UUID
) -> m.Appointment:
    """只读查询，带对象归属验证。无权访问统一返回 NOT_FOUND。"""

    ctx.require("appointment:read")
    appointment = (
        await session.execute(
            select(m.Appointment).where(
                m.Appointment.tenant_id == ctx.tenant_id,
                m.Appointment.id == appointment_id,
            )
        )
    ).scalar_one_or_none()
    if appointment is None:
        raise not_found("预约不存在或无权访问")
    if ctx.role == "customer" and appointment.customer_id != ctx.customer_id:
        raise not_found("预约不存在或无权访问")
    ctx.require_store_scope(appointment.store_id)
    return appointment


async def _supersede_open_waiting(
    session: AsyncSession, *, tenant_id: UUID, task_id: UUID
) -> None:
    """首版每任务一个 OPEN 等待，切换前先关闭旧请求。"""

    await session.execute(
        update(m.WaitingRequest)
        .where(
            m.WaitingRequest.tenant_id == tenant_id,
            m.WaitingRequest.task_id == task_id,
            m.WaitingRequest.status == WaitingStatus.OPEN.value,
        )
        .values(status=WaitingStatus.SUPERSEDED.value, version=m.WaitingRequest.version + 1)
    )


async def _close_waiting(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    task_id: UUID,
    status: WaitingStatus,
    now: datetime,
) -> None:
    await session.execute(
        update(m.WaitingRequest)
        .where(
            m.WaitingRequest.tenant_id == tenant_id,
            m.WaitingRequest.task_id == task_id,
            m.WaitingRequest.status == WaitingStatus.OPEN.value,
        )
        .values(status=status.value, answered_at=now, version=m.WaitingRequest.version + 1)
    )


__all__ = [
    "CommitOutcome",
    "HoldOutcome",
    "cancel_appointment",
    "confirm_appointment",
    "confirmation_token_for",
    "create_hold",
    "expire_due_allocations",
    "expire_due_holds",
    "expire_open_waitings",
    "get_appointment",
    "issue_confirmation",
    "record_confirmation_event",
    "reschedule_appointment",
    "validate_fulfillment",
]
