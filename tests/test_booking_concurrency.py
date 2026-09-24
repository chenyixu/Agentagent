"""完整占位链路的双客户真实事务竞争。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from appointment.core.enums import AllocationState, ErrorCode, TaskState
from appointment.core.errors import DomainError
from appointment.db import models as m
from appointment.db.session import get_sessionmaker
from appointment.domain.availability import search_availability
from appointment.domain.booking import create_hold
from appointment.domain.catalog import load_service
from appointment.domain.quote import get_service_quote
from appointment.domain.tasks import ensure_conversation, get_or_create_task, set_task_state
from tests.conftest import customer_ctx


@pytest.mark.parametrize("round_number", range(20))
async def test_two_customers_competing_for_same_candidate_leave_one_hold(
    session, seeded, clock, tomorrow_window, round_number
):
    now = clock.now()
    service = await load_service(
        session, tenant_id=seeded.tenant_id,
        store_id=seeded.store_id, service_id=seeded.service_ids["shoulder"],
    )
    availability = await search_availability(
        session, tenant_id=seeded.tenant_id, store_id=seeded.store_id,
        service=service, window_start=tomorrow_window[0],
        window_end=tomorrow_window[1], now=now, limit=1,
    )
    candidate = availability.candidates[0]
    prepared = []
    for index in (0, 1):
        ctx = customer_ctx(seeded, index=index, request_id=f"compete-{index}")
        conversation = await ensure_conversation(
            session, ctx, store_id=seeded.store_id, now=now,
        )
        task = (await get_or_create_task(
            session, ctx, conversation=conversation,
            release_id=ctx.release_id, now=now,
        )).task
        for state in (TaskState.SEARCHING, TaskState.PROPOSED):
            task = await set_task_state(
                session, ctx, task=task, target=state,
                expected_version=task.version, now=now,
            )
        quote = await get_service_quote(
            session, tenant_id=seeded.tenant_id,
            customer_id=ctx.customer_id, store_id=seeded.store_id,
            service=service, as_of=now,
        )
        prepared.append((ctx, task.id, task.version, quote.quote_token))
    await session.commit()

    gate = asyncio.Event()

    async def compete(ctx, task_id, version, quote_token):
        async with get_sessionmaker()() as contender:
            task = (await contender.execute(select(m.Task).where(m.Task.id == task_id))).scalar_one()
            await gate.wait()
            try:
                outcome = await create_hold(
                    contender, ctx, task=task, expected_task_version=version,
                    store_id=seeded.store_id,
                    service_id=seeded.service_ids["shoulder"],
                    candidate_id=candidate.candidate_id,
                    start_at=candidate.start_at, end_at=candidate.end_at,
                    resource_ids=[unit.resource_id for unit in candidate.resources],
                    quote_token=quote_token, now=now, clock=clock,
                )
                await contender.commit()
                return "committed", outcome.hold.id
            except DomainError as exc:
                await contender.rollback()
                return exc.code.value, None

    contenders = [asyncio.create_task(compete(*item)) for item in prepared]
    gate.set()
    outcomes = await asyncio.gather(*contenders)
    assert sum(status == "committed" for status, _ in outcomes) == 1, outcomes
    assert {status for status, _ in outcomes} == {"committed", ErrorCode.SLOT_CONFLICT.value}
    holds = (await session.execute(select(m.Hold).where(
        m.Hold.tenant_id == seeded.tenant_id
    ))).scalars().all()
    allocations = (await session.execute(select(m.ResourceAllocation).where(
        m.ResourceAllocation.tenant_id == seeded.tenant_id,
        m.ResourceAllocation.state == AllocationState.HELD.value,
    ))).scalars().all()
    assert len(holds) == 1
    assert len(allocations) == len(candidate.resources)
