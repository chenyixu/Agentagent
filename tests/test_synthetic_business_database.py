"""Database-backed checks that exercise generated data through booking services."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from appointment.core.clock import FrozenClock
from appointment.core.enums import AllocationState, ErrorCode, TaskState, ToolStatus
from appointment.core.errors import DomainError
from appointment.db import models as m
from appointment.db.session import get_sessionmaker
from appointment.domain.availability import ResourcePreferences, search_availability
from appointment.domain.booking import confirm_appointment, create_hold
from appointment.domain.catalog import load_service
from appointment.domain.context import TrustedContext
from appointment.domain.quote import get_service_quote
from appointment.domain.tasks import ensure_conversation, get_or_create_task, set_task_state
from appointment.tools.registry import invoke_tool
from evals.generate_synthetic_business_data import DEFAULT_OUTPUT
from evals.synthetic_business_db import seed_synthetic_world


def _load_dataset() -> dict:
    return json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))


def _external_ids(seed) -> dict:
    return {value: key for key, value in seed.resource_ids.items()}


@pytest.mark.asyncio
async def test_generated_availability_cases_match_postgres_domain_results(session):
    dataset = _load_dataset()
    seeded = await seed_synthetic_world(session, dataset)
    await session.commit()

    resource_keys = _external_ids(seeded)
    now = datetime.fromisoformat(dataset["reference_time"])
    target_categories = {
        "feasible_exact_slot",
        "outside_hours",
        "preferred_therapist_unavailable",
        "booking_conflict",
    }
    cases = [
        case for case in dataset["cases"]
        if case["category"] in target_categories
    ]
    assert len(cases) == 34

    initial_counts = {
        "appointments": (
            await session.execute(select(func.count()).select_from(m.Appointment))
        ).scalar_one(),
        "allocations": (
            await session.execute(select(func.count()).select_from(m.ResourceAllocation))
        ).scalar_one(),
        "holds": (
            await session.execute(select(func.count()).select_from(m.Hold))
        ).scalar_one(),
    }

    for case in cases:
        store_id = seeded.store_ids[case["store_id"]]
        service_id = seeded.service_ids[(case["store_id"], case["service_id"])]
        service = await load_service(
            session,
            tenant_id=seeded.tenant_id,
            store_id=store_id,
            service_id=service_id,
        )
        start_at = datetime.fromisoformat(case["requested_start_at"])
        preferences = None
        if case["category"] == "preferred_therapist_unavailable":
            preferences = ResourcePreferences(
                preferred_resource_ids=[
                    seeded.resource_ids[case["expected"]["preferred_therapist_id"]]
                ],
                allow_substitute=True,
            )
        result = await search_availability(
            session,
            tenant_id=seeded.tenant_id,
            store_id=store_id,
            service=service,
            window_start=start_at,
            window_end=start_at + timedelta(minutes=service.duration_minutes),
            desired_start=start_at,
            preferences=preferences,
            now=now,
            limit=5,
        )
        at_requested_time = [
            item for item in result.candidates if item.start_at == start_at
        ]
        expected_pairs = case["expected"]["candidate_pairs"]
        assert bool(at_requested_time) == bool(expected_pairs), (
            case["id"], result.notes, service.requirements,
        )

        if at_requested_time:
            candidate = at_requested_time[0]
            selected = {
                unit.type: resource_keys[unit.resource_id]
                for unit in candidate.resources
            }
            selected_pair = {
                "therapist_id": selected["therapist"],
                "room_id": selected["room"],
            }
            assert selected_pair in expected_pairs, case["id"]
            if case["category"] == "preferred_therapist_unavailable":
                assert selected_pair["therapist_id"] != case["expected"]["preferred_therapist_id"]
            if case["category"] == "booking_conflict":
                assert selected_pair != case["expected"]["blocked_pair"]

    final_counts = {
        "appointments": (
            await session.execute(select(func.count()).select_from(m.Appointment))
        ).scalar_one(),
        "allocations": (
            await session.execute(select(func.count()).select_from(m.ResourceAllocation))
        ).scalar_one(),
        "holds": (
            await session.execute(select(func.count()).select_from(m.Hold))
        ).scalar_one(),
    }
    assert final_counts == initial_counts


@pytest.mark.asyncio
async def test_generated_concurrent_slot_race_creates_one_hold_and_no_overlapping_allocations(
    session,
):
    dataset = _load_dataset()
    seeded = await seed_synthetic_world(session, dataset)
    await session.commit()
    appointments_before = (
        await session.execute(select(func.count()).select_from(m.Appointment))
    ).scalar_one()
    operations_before = (
        await session.execute(select(func.count()).select_from(m.Operation))
    ).scalar_one()
    now = datetime.fromisoformat(dataset["reference_time"])
    race_clock = FrozenClock(now)
    race = next(
        item for item in dataset["cases"]
        if item["category"] == "concurrent_single_slot_race"
    )
    expected = race["expected"]
    store_id = seeded.store_ids[race["store_id"]]
    service_id = seeded.service_ids[(race["store_id"], race["service_id"])]
    service = await load_service(
        session, tenant_id=seeded.tenant_id, store_id=store_id, service_id=service_id
    )
    start_at = datetime.fromisoformat(race["requested_start_at"])
    target_resources = {
        seeded.resource_ids[expected["target_pair"]["therapist_id"]],
        seeded.resource_ids[expected["target_pair"]["room_id"]],
    }
    store_resource_ids = {
        resource_id
        for key, resource_id in seeded.resource_ids.items()
        if key.startswith((f"TECH-{race['store_id'][-1]}-", f"ROOM-{race['store_id'][-1]}-"))
    }
    preferences = ResourcePreferences(
        excluded_resource_ids=list(store_resource_ids - target_resources),
        allow_substitute=True,
    )
    availability = await search_availability(
        session,
        tenant_id=seeded.tenant_id,
        store_id=store_id,
        service=service,
        window_start=start_at,
        window_end=start_at + timedelta(minutes=service.duration_minutes),
        desired_start=start_at,
        preferences=preferences,
        now=now,
        limit=1,
    )
    assert len(availability.candidates) == 1, race["id"]
    candidate = availability.candidates[0]
    assert {item.resource_id for item in candidate.resources} == target_resources

    prepared = []
    for index, customer_key in enumerate(expected["contending_customer_ids"]):
        context = TrustedContext(
            tenant_id=seeded.tenant_id,
            actor_id=seeded.actor_ids[customer_key],
            customer_id=seeded.customer_ids[customer_key],
            role="customer",
            request_id=f"{race['id']}-contender-{index}",
            release_id="synthetic-db-eval-v1",
        )
        conversation = await ensure_conversation(
            session, context, store_id=store_id, now=now
        )
        task = (await get_or_create_task(
            session, context, conversation=conversation,
            release_id=context.release_id, now=now,
        )).task
        for state in (TaskState.SEARCHING, TaskState.PROPOSED):
            task = await set_task_state(
                session, context, task=task, target=state,
                expected_version=task.version, now=now,
            )
        quote = await get_service_quote(
            session,
            tenant_id=seeded.tenant_id,
            customer_id=context.customer_id,
            store_id=store_id,
            service=service,
            as_of=now,
        )
        prepared.append((context, task.id, task.version, quote.quote_token))
    await session.commit()

    gate = asyncio.Event()

    async def compete(context, task_id, task_version, quote_token):
        async with get_sessionmaker()() as contender:
            task = (await contender.execute(
                select(m.Task).where(m.Task.id == task_id)
            )).scalar_one()
            await gate.wait()
            try:
                outcome = await create_hold(
                    contender,
                    context,
                    task=task,
                    expected_task_version=task_version,
                    store_id=store_id,
                    service_id=service_id,
                    candidate_id=candidate.candidate_id,
                    start_at=candidate.start_at,
                    end_at=candidate.end_at,
                    resource_ids=[unit.resource_id for unit in candidate.resources],
                    quote_token=quote_token,
                    now=now,
                    clock=race_clock,
                )
                await contender.commit()
                return "committed", (context, outcome)
            except DomainError as exc:
                await contender.rollback()
                return exc.code.value, None

    pending = [asyncio.create_task(compete(*item)) for item in prepared]
    gate.set()
    outcomes = await asyncio.gather(*pending)
    assert sum(state == "committed" for state, _ in outcomes) == 1, outcomes
    assert {state for state, _ in outcomes} == {
        "committed", ErrorCode.SLOT_CONFLICT.value,
    }
    winner_context, hold_outcome = next(
        result for state, result in outcomes if state == "committed"
    )
    committed = await confirm_appointment(
        session,
        winner_context,
        proposal_id=hold_outcome.proposal.id,
        proposal_version=hold_outcome.proposal.version,
        confirmation_token=hold_outcome.confirmation_token,
        client_confirmation_event_id=f"{race['id']}-confirm-event",
        idempotency_key=f"{race['id']}-confirm-key",
        expected_task_version=hold_outcome.task_version,
        now=now,
        clock=race_clock,
    )
    await session.commit()
    replay = await confirm_appointment(
        session,
        winner_context,
        proposal_id=hold_outcome.proposal.id,
        proposal_version=hold_outcome.proposal.version,
        confirmation_token=hold_outcome.confirmation_token,
        client_confirmation_event_id=f"{race['id']}-confirm-event",
        idempotency_key=f"{race['id']}-confirm-key",
        expected_task_version=hold_outcome.task_version,
        now=now,
        clock=race_clock,
    )
    await session.commit()

    holds = (await session.execute(select(m.Hold).where(
        m.Hold.tenant_id == seeded.tenant_id
    ))).scalars().all()
    allocations = (await session.execute(select(m.ResourceAllocation).where(
        m.ResourceAllocation.tenant_id == seeded.tenant_id,
        m.ResourceAllocation.appointment_id == committed.appointment.id,
        m.ResourceAllocation.state == AllocationState.BOOKED.value,
        m.ResourceAllocation.start_at == start_at,
    ))).scalars().all()
    assert len(holds) == 1
    assert {item.resource_id for item in allocations} == target_resources
    assert len(allocations) == 2
    appointments_after = (
        await session.execute(select(func.count()).select_from(m.Appointment))
    ).scalar_one()
    operations_after = (
        await session.execute(select(func.count()).select_from(m.Operation))
    ).scalar_one()
    assert appointments_after == appointments_before + 1
    assert operations_after == operations_before + 2
    assert replay.replayed is True
    assert replay.appointment.id == committed.appointment.id
    assert all(item.state == AllocationState.BOOKED.value for item in allocations)
    assert committed.appointment.end_at == start_at + timedelta(
        minutes=service.duration_minutes
    )
    assert all(
        item.end_at == committed.appointment.end_at + timedelta(
            minutes=service.requirements["buffer_minutes"]
        )
        for item in allocations
    )


@pytest.mark.asyncio
async def test_write_tool_conflict_rolls_back_savepoint_and_keeps_session_usable(session):
    """A rejected concurrent write must not poison the Agent's outer task transaction."""

    dataset = _load_dataset()
    seeded = await seed_synthetic_world(session, dataset)
    await session.commit()
    now = datetime.fromisoformat(dataset["reference_time"])
    race = next(item for item in dataset["cases"] if item["category"] == "concurrent_single_slot_race")
    expected = race["expected"]
    store_id = seeded.store_ids[race["store_id"]]
    service_id = seeded.service_ids[(race["store_id"], race["service_id"])]
    start_at = datetime.fromisoformat(race["requested_start_at"])
    target_resources = {
        seeded.resource_ids[expected["target_pair"]["therapist_id"]],
        seeded.resource_ids[expected["target_pair"]["room_id"]],
    }
    store_resources = {
        resource_id for key, resource_id in seeded.resource_ids.items()
        if key.startswith((f"TECH-{race['store_id'][-1]}-", f"ROOM-{race['store_id'][-1]}-"))
    }
    preferences = ResourcePreferences(
        excluded_resource_ids=list(store_resources - target_resources),
        allow_substitute=True,
    )
    service = await load_service(
        session, tenant_id=seeded.tenant_id, store_id=store_id, service_id=service_id
    )
    availability = await search_availability(
        session,
        tenant_id=seeded.tenant_id,
        store_id=store_id,
        service=service,
        window_start=start_at,
        window_end=start_at + timedelta(minutes=service.duration_minutes),
        desired_start=start_at,
        preferences=preferences,
        now=now,
        limit=1,
    )
    assert len(availability.candidates) == 1
    candidate = availability.candidates[0]

    prepared = []
    for index, customer_key in enumerate(expected["contending_customer_ids"]):
        ctx = TrustedContext(
            tenant_id=seeded.tenant_id,
            actor_id=seeded.actor_ids[customer_key],
            customer_id=seeded.customer_ids[customer_key],
            role="customer",
            request_id=f"{race['id']}-savepoint-{index}",
            release_id="synthetic-write-savepoint-test",
        )
        conversation = await ensure_conversation(session, ctx, store_id=store_id, now=now)
        task = (await get_or_create_task(
            session, ctx, conversation=conversation, release_id=ctx.release_id, now=now,
        )).task
        for state in (TaskState.SEARCHING, TaskState.PROPOSED):
            task = await set_task_state(
                session, ctx, task=task, target=state,
                expected_version=task.version, now=now,
            )
        quote = await get_service_quote(
            session,
            tenant_id=seeded.tenant_id,
            customer_id=ctx.customer_id,
            store_id=store_id,
            service=service,
            as_of=now,
        )
        prepared.append((ctx, task, quote.quote_token))
    await session.commit()

    winner_ctx, winner_task, winner_quote = prepared[0]
    winner = await create_hold(
        session,
        winner_ctx,
        task=winner_task,
        expected_task_version=winner_task.version,
        store_id=store_id,
        service_id=service_id,
        candidate_id=candidate.candidate_id,
        start_at=candidate.start_at,
        end_at=candidate.end_at,
        resource_ids=[item.resource_id for item in candidate.resources],
        quote_token=winner_quote,
        now=now,
        clock=FrozenClock(now),
    )
    await session.commit()

    loser_ctx, loser_task, loser_quote = prepared[1]
    result = await invoke_tool(
        session,
        loser_ctx,
        "create_hold",
        {
            "task_id": str(loser_task.id),
            "expected_task_version": loser_task.version,
            "store_id": str(store_id),
            "service_id": str(service_id),
            "candidate_id": candidate.candidate_id,
            "start_at": candidate.start_at.isoformat(),
            "end_at": candidate.end_at.isoformat(),
            "resource_ids": [str(item.resource_id) for item in candidate.resources],
            "quote_token": loser_quote,
        },
        now=now,
        clock=FrozenClock(now),
        allowed_tools=("create_hold",),
    )
    assert result.status is ToolStatus.ERROR
    assert result.error_code is ErrorCode.SLOT_CONFLICT

    # A normal outer write/query after the rejected tool proves the savepoint
    # rollback recovered the session instead of leaving it in PendingRollbackError.
    await session.execute(select(m.Task).where(m.Task.id == loser_task.id))
    await session.commit()
    holds = (await session.execute(
        select(m.Hold).where(m.Hold.tenant_id == seeded.tenant_id)
    )).scalars().all()
    assert len(holds) == 1
    assert holds[0].id == winner.hold.id
