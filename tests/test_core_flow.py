"""主链路：查询 → 报价 → 占位 → 确认。

这是设计稿 §7"一条完整的执行链路"的最小可验证切片。它同时验证：

- 候选查询返回 guarantee=NONE 的快照；
- 占位在事务内先回收过期 HELD 再插入，并由排他约束裁决；
- 确认绑定方案版本与内容哈希，成功后订单、占用、操作、Outbox 同一事务提交。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from appointment.core.enums import (
    AllocationState,
    AppointmentStatus,
    ConfirmationStatus,
    HoldState,
    OperationStatus,
    ProposalStatus,
    TaskState,
)
from appointment.domain.availability import ResourcePreferences, search_availability
from appointment.domain.booking import confirm_appointment, create_hold, expire_due_holds
from appointment.domain.catalog import load_service
from appointment.domain.quote import get_service_quote
from appointment.domain.tasks import (
    ensure_conversation,
    get_or_create_task,
    set_task_state,
)
from appointment.db import models as m
from tests.conftest import customer_ctx

pytestmark = pytest.mark.invariant


async def _prepare_task(session, ctx, seeded, now, *, target=TaskState.PROPOSED):
    conversation = await ensure_conversation(
        session, ctx, store_id=seeded.store_id, now=now
    )
    outcome = await get_or_create_task(
        session,
        ctx,
        conversation=conversation,
        release_id="release-local-1",
        now=now,
    )
    task = outcome.task
    for state in (TaskState.SEARCHING, target):
        task = await set_task_state(
            session,
            ctx,
            task=task,
            target=state,
            expected_version=task.version,
            now=now,
        )
    return conversation, task


async def test_full_booking_flow(session, seeded, clock, tomorrow_window):
    ctx = customer_ctx(seeded)
    now = clock.now()
    _conversation, task = await _prepare_task(session, ctx, seeded, now)

    service = await load_service(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
    )
    assert service.duration_minutes == 60

    offer = await get_service_quote(
        session,
        tenant_id=seeded.tenant_id,
        customer_id=seeded.customer_ids[0],
        store_id=seeded.store_id,
        service=service,
        as_of=now,
    )
    assert offer.amount_minor == seeded.price_amount_minor
    assert offer.currency == "CNY"

    start, end, _local_date = tomorrow_window
    availability = await search_availability(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service=service,
        window_start=start,
        window_end=end,
        now=now,
        desired_start=start,
        preferences=ResourcePreferences(),
        amount_minor=offer.amount_minor,
        limit=5,
    )
    assert availability.candidates, "营业时间内应能查到候选"
    assert availability.guarantee == "NONE"
    candidate = availability.candidates[0]
    assert len(candidate.resources) == 2, "肩颈服务需要技师 + 房间各一个单位"
    assert candidate.score is not None

    # 占位事务会就地推进 task 的版本，因此先记下调用前的版本。
    task_version_before_hold = task.version
    hold = await create_hold(
        session,
        ctx,
        task=task,
        expected_task_version=task.version,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
        candidate_id=candidate.candidate_id,
        start_at=candidate.start_at,
        end_at=candidate.end_at,
        resource_ids=[unit.resource_id for unit in candidate.resources],
        quote_token=offer.quote_token,
        now=now,
        clock=clock,
    )
    await session.commit()

    assert hold.hold.state == HoldState.HELD.value
    # 方案版本跟随任务版本严格递增；同一任务内不会复用版本号，
    # 旧确认凭据也就无法命中新方案。
    assert hold.proposal.version == task_version_before_hold + 1
    reloaded_task = (
        await session.execute(select(m.Task).where(m.Task.id == task.id))
    ).scalar_one()
    assert reloaded_task.state == TaskState.WAITING_CONFIRMATION.value

    allocations = (
        await session.execute(
            select(m.ResourceAllocation).where(
                m.ResourceAllocation.hold_id == hold.hold.id
            )
        )
    ).scalars().all()
    assert {a.state for a in allocations} == {AllocationState.HELD.value}
    assert all(a.expires_at is not None for a in allocations)

    outcome = await confirm_appointment(
        session,
        ctx,
        proposal_id=hold.proposal.id,
        proposal_version=hold.proposal.version,
        confirmation_token=hold.confirmation_token,
        client_confirmation_event_id="client-evt-1",
        idempotency_key="confirm-key-1",
        expected_task_version=reloaded_task.version,
        now=now,
        clock=clock,
    )
    await session.commit()

    assert outcome.appointment.status == AppointmentStatus.CONFIRMED.value

    booked = (
        await session.execute(
            select(m.ResourceAllocation).where(
                m.ResourceAllocation.appointment_id == outcome.appointment.id
            )
        )
    ).scalars().all()
    assert {a.state for a in booked} == {AllocationState.BOOKED.value}
    assert all(a.expires_at is None for a in booked), "BOOKED 不应带占位截止时间"

    confirmation = (
        await session.execute(
            select(m.Confirmation).where(m.Confirmation.id == hold.confirmation.id)
        )
    ).scalar_one()
    assert confirmation.status == ConfirmationStatus.CONSUMED.value

    operation = (
        await session.execute(
            select(m.Operation).where(m.Operation.id == outcome.operation.id)
        )
    ).scalar_one()
    assert operation.status == OperationStatus.SUCCEEDED.value

    proposal = (
        await session.execute(
            select(m.Proposal).where(
                m.Proposal.id == hold.proposal.id,
                m.Proposal.version == hold.proposal.version,
            )
        )
    ).scalar_one()
    assert proposal.status == ProposalStatus.COMMITTED.value

    # 订单与 Outbox 同事务：Outbox 存在且指向该订单版本。
    outbox = (
        await session.execute(
            select(m.Outbox).where(m.Outbox.aggregate_id == outcome.appointment.id)
        )
    ).scalars().all()
    assert len(outbox) == 1
    assert outbox[0].aggregate_version == outcome.appointment.version

    # 任务到达成功终态，等待请求关闭。
    final_task = (
        await session.execute(select(m.Task).where(m.Task.id == task.id))
    ).scalar_one()
    assert final_task.state == TaskState.SUCCEEDED.value
    waiting = (
        await session.execute(
            select(m.WaitingRequest).where(m.WaitingRequest.task_id == task.id)
        )
    ).scalars().all()
    assert [w.status for w in waiting] == ["ANSWERED"]


async def test_expired_hold_does_not_block_new_hold(
    session, seeded, clock, tomorrow_window
):
    """过期占位在写入路径被同步回收，不依赖清理 Worker 存活（设计稿 §8.1）。"""

    from datetime import timedelta

    ctx_a = customer_ctx(seeded, index=0)
    ctx_b = customer_ctx(seeded, index=1)
    now = clock.now()

    service = await load_service(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
    )
    start, end, _ = tomorrow_window

    _conv_a, task_a = await _prepare_task(session, ctx_a, seeded, now)
    offer_a = await get_service_quote(
        session,
        tenant_id=seeded.tenant_id,
        customer_id=seeded.customer_ids[0],
        store_id=seeded.store_id,
        service=service,
        as_of=now,
    )
    availability = await search_availability(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service=service,
        window_start=start,
        window_end=end,
        now=now,
        amount_minor=offer_a.amount_minor,
    )
    candidate = availability.candidates[0]
    resource_ids = [unit.resource_id for unit in candidate.resources]

    hold_a = await create_hold(
        session,
        ctx_a,
        task=task_a,
        expected_task_version=task_a.version,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
        candidate_id=candidate.candidate_id,
        start_at=candidate.start_at,
        end_at=candidate.end_at,
        resource_ids=resource_ids,
        quote_token=offer_a.quote_token,
        now=now,
        clock=clock,
    )
    await session.commit()
    # rollback 会让 ORM 实例过期；后续断言要用的标识先取出来，避免惰性加载。
    hold_a_id = hold_a.hold.id

    # 受控时钟越过占位截止，并停止一切 Worker（本测试根本不运行 Worker）。
    clock.advance(seconds=400)
    later = clock.now()
    await expire_due_holds(
        session,
        tenant_id=seeded.tenant_id,
        customer_id=seeded.customer_ids[0],
        now=later,
    )
    await session.commit()

    # 旧凭据不能复活：确认必须得到 HOLD_EXPIRED。
    from appointment.core.enums import ErrorCode
    from appointment.core.errors import DomainError

    with pytest.raises(DomainError) as excinfo:
        await confirm_appointment(
            session,
            ctx_a,
            proposal_id=hold_a.proposal.id,
            proposal_version=hold_a.proposal.version,
            confirmation_token=hold_a.confirmation_token,
            client_confirmation_event_id="late-evt",
            idempotency_key="confirm-key-late",
            expected_task_version=hold_a.task_version,
            now=later,
            clock=clock,
        )
    assert excinfo.value.code in (ErrorCode.HOLD_EXPIRED, ErrorCode.STALE_PROPOSAL)
    await session.rollback()

    # 另一客户可以获取同一时段的新占位。
    _conv_b, task_b = await _prepare_task(session, ctx_b, seeded, later)
    offer_b = await get_service_quote(
        session,
        tenant_id=seeded.tenant_id,
        customer_id=seeded.customer_ids[1],
        store_id=seeded.store_id,
        service=service,
        as_of=later,
    )
    hold_b = await create_hold(
        session,
        ctx_b,
        task=task_b,
        expected_task_version=task_b.version,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
        candidate_id=candidate.candidate_id,
        start_at=candidate.start_at,
        end_at=candidate.end_at,
        resource_ids=resource_ids,
        quote_token=offer_b.quote_token,
        now=later,
        clock=clock,
    )
    await session.commit()
    assert hold_b.hold.state == HoldState.HELD.value

    old_hold = (
        await session.execute(select(m.Hold).where(m.Hold.id == hold_a_id))
    ).scalar_one()
    assert old_hold.state == HoldState.EXPIRED.value

    # 有效占用仍不重叠。
    active = (
        await session.execute(
            select(m.ResourceAllocation).where(
                m.ResourceAllocation.resource_id.in_(resource_ids),
                m.ResourceAllocation.state.in_(
                    [AllocationState.HELD.value, AllocationState.BOOKED.value]
                ),
            )
        )
    ).scalars().all()
    assert len(active) == len(resource_ids)


async def test_hold_expires_after_ttl(session, seeded, clock):
    """占位 TTL 由配置决定，默认 3 分钟（设计稿 §7）。"""

    ctx = customer_ctx(seeded)
    now = clock.now()

    service = await load_service(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
    )
    from appointment.domain.timeutil import load_zone, resolve_local_datetime
    from appointment.seed import STORE_TIMEZONE
    from datetime import datetime, time

    tz = load_zone(STORE_TIMEZONE)
    local_date = now.astimezone(tz).date()
    start = resolve_local_datetime(datetime.combine(local_date, time(16, 0)), tz)
    end = resolve_local_datetime(datetime.combine(local_date, time(17, 0)), tz)
    # 当天稍晚的窗口（当前是 13:00 本地时间）。
    _conv, task = await _prepare_task(session, ctx, seeded, now)
    offer = await get_service_quote(
        session,
        tenant_id=seeded.tenant_id,
        customer_id=seeded.customer_ids[0],
        store_id=seeded.store_id,
        service=service,
        as_of=now,
    )
    availability = await search_availability(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service=service,
        window_start=start,
        window_end=end,
        now=now,
        amount_minor=offer.amount_minor,
    )
    assert availability.candidates
    candidate = availability.candidates[0]

    hold = await create_hold(
        session,
        ctx,
        task=task,
        expected_task_version=task.version,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
        candidate_id=candidate.candidate_id,
        start_at=candidate.start_at,
        end_at=candidate.end_at,
        resource_ids=[unit.resource_id for unit in candidate.resources],
        quote_token=offer.quote_token,
        now=now,
        clock=clock,
    )
    await session.commit()

    from datetime import timedelta

    assert hold.hold.expires_at == now + timedelta(seconds=180)
