"""Exercise stale selection, idempotent confirmation and slot races via real Agent turns.

Every case runs in a fresh retained ``appointment_test`` schema. Synthetic requests
are sent to the configured model; only explicit user selections reach booking writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from appointment.api.deps import build_runtime_for_settings
from appointment.config.settings import Settings
from appointment.core.clock import FrozenClock
from appointment.core.enums import AllocationState, ErrorCode, TaskState, ToolStatus
from appointment.db import models as m
from appointment.db.schema import create_all, verify_exclusion_constraint
from appointment.domain.booking import confirm_appointment
from appointment.domain.context import TrustedContext
from appointment.orchestrator import Orchestrator
from appointment.tools.registry import tool_definitions
try:
    from evals.run_synthetic_business_agent_eval import (
        CASE_PATH, DATABASE_URL, MODEL_NAME, PROMPT_VERSION, REPORT_DIR,
        TURN_NOW, TracedRuntime, _redact, _safe_error, _sha256,
    )
    from evals.synthetic_business_db import SeededSyntheticWorld, seed_synthetic_world
except ImportError:  # Direct ``python evals/...`` execution puts evals/ on sys.path.
    from run_synthetic_business_agent_eval import (
        CASE_PATH, DATABASE_URL, MODEL_NAME, PROMPT_VERSION, REPORT_DIR,
        TURN_NOW, TracedRuntime, _redact, _safe_error, _sha256,
    )
    from synthetic_business_db import SeededSyntheticWorld, seed_synthetic_world


ROOT = Path(__file__).resolve().parents[1]
TRANSACTION_CATEGORIES = {
    "stale_candidate", "idempotent_confirmation", "concurrent_single_slot_race"
}


def _count(session: Any, table: Any) -> Any:
    return session.execute(select(func.count()).select_from(table))


async def _counts(session: Any) -> dict[str, int]:
    return {
        "holds": int((await _count(session, m.Hold)).scalar_one()),
        "appointments": int((await _count(session, m.Appointment)).scalar_one()),
        "allocations": int((await _count(session, m.ResourceAllocation)).scalar_one()),
        "operations": int((await _count(session, m.Operation)).scalar_one()),
    }


def _context(world: SeededSyntheticWorld, case: dict[str, Any], customer_key: str) -> TrustedContext:
    return TrustedContext(
        tenant_id=world.tenant_id,
        actor_id=world.actor_ids[customer_key],
        customer_id=world.customer_ids[customer_key],
        role="customer",
        request_id=f"{case['id']}:{customer_key}",
        release_id=f"synthetic-transaction-{case['id']}",
    )


async def _turn(
    session: Any,
    *,
    ctx: TrustedContext,
    store_id: UUID,
    text: str,
    message_id: str,
    settings: Settings,
    clock: FrozenClock,
    trace: TracedRuntime,
) -> Any:
    report = await asyncio.wait_for(
        Orchestrator(
            session,
            runtime=trace,
            settings=settings,
            clock=clock,
        ).handle_user_message(
            ctx,
            text=text,
            client_message_id=message_id,
            store_id=store_id,
            now=clock.now(),
        ),
        timeout=120,
    )
    await session.commit()
    return report


def _availability_candidates(report: Any) -> list[dict[str, Any]]:
    for call in report.tool_calls:
        if call.get("tool") == "search_availability" and call.get("status") == ToolStatus.OK.value:
            return list((call.get("data") or {}).get("candidates") or [])
    return []


def _candidate_pair(candidate: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(
        str(resource.get("unit_code"))
        for resource in candidate.get("resources") or []
        if resource.get("unit_code")
    ))


async def _run_stale_case(
    case: dict[str, Any], *, session: Any, world: SeededSyntheticWorld,
    settings: Settings, clock: FrozenClock, trace: TracedRuntime,
    baseline: dict[str, int],
) -> dict[str, Any]:
    ctx = _context(world, case, case["customer_id"])
    store_id = world.store_ids[case["store_id"]]
    first = await _turn(
        session, ctx=ctx, store_id=store_id, text=case["request"],
        message_id=f"{case['id']}-query", settings=settings, clock=clock, trace=trace,
    )
    candidates = _availability_candidates(first)
    if first.task_state != TaskState.PROPOSED.value or not candidates:
        return {
            "status": "fail", "phase": "query", "state": first.task_state,
            "candidate_count": len(candidates), "turns": [first.to_dict()],
        }

    invalidation = case["expected"]["invalidation"]
    candidate = candidates[0]
    candidate_start = datetime.fromisoformat(candidate["start_at"])
    resource_ids = [UUID(item["resource_id"]) for item in candidate.get("resources") or []]
    if invalidation == "shift_revision_changed":
        shifts = (await session.execute(
            select(m.Shift).where(
                m.Shift.tenant_id == world.tenant_id,
                m.Shift.resource_id.in_(resource_ids),
                m.Shift.status == "SCHEDULED",
                m.Shift.start_at <= candidate_start,
                m.Shift.end_at >= datetime.fromisoformat(candidate["end_at"]),
            )
        )).scalars().all()
        for shift in shifts:
            shift.status = "CANCELLED"
        if not shifts:
            raise AssertionError("no scheduled shifts cover the displayed candidate")
        await session.commit()
    elif invalidation == "price_revision_changed":
        service_id = world.service_ids[(case["store_id"], case["service_id"])]
        catalog = await session.get(m.ServiceCatalog, service_id)
        if catalog is None or catalog.current_price_version_id is None:
            raise AssertionError("synthetic service has no active price version")
        price = await session.get(m.PriceVersion, catalog.current_price_version_id)
        if price is None:
            raise AssertionError("synthetic service price version is missing")
        price.revoked_at = clock.now()
        await session.commit()
    elif invalidation == "candidate_expired":
        clock.set(candidate_start + timedelta(seconds=1))
    else:
        raise AssertionError(f"unknown invalidation mode: {invalidation}")

    second = await _turn(
        session, ctx=ctx, store_id=store_id, text="第一个可以",
        message_id=f"{case['id']}-select", settings=settings, clock=clock, trace=trace,
    )
    hold_calls = [item for item in second.tool_calls if item.get("tool") == "create_hold"]
    stale_error = any(
        item.get("status") == ToolStatus.ERROR.value
        and item.get("error_code") in {
            ErrorCode.STALE_PROPOSAL.value,
            ErrorCode.DEPENDENCY_UNAVAILABLE.value,
            ErrorCode.SLOT_CONFLICT.value,
        }
        for item in hold_calls
    )
    refused_without_write = (
        not second.pending_confirmation
        and second.task_state != TaskState.WAITING_CONFIRMATION.value
    )
    after = await _counts(session)
    no_effects = all(
        after[key] - baseline[key] == 0
        for key in ("holds", "appointments", "operations")
    )
    status = "pass" if no_effects and (stale_error or refused_without_write) else "fail"
    return {
        "status": status,
        "invalidation": invalidation,
        "first_state": first.task_state,
        "second_state": second.task_state,
        "candidate_count": len(candidates),
        "selection_tool_calls": hold_calls,
        "stale_rejection_observed": stale_error,
        "refused_without_write": refused_without_write,
        "after": after,
        "turns": [first.to_dict(), second.to_dict()],
    }


def _feasible_seed_for_store(dataset: dict[str, Any], store_key: str) -> dict[str, Any]:
    return next(
        item for item in dataset["cases"]
        if item["category"] == "feasible_exact_slot" and item["store_id"] == store_key
    )


async def _run_idempotency_case(
    case: dict[str, Any], *, dataset: dict[str, Any], session: Any,
    world: SeededSyntheticWorld, settings: Settings, clock: FrozenClock,
    trace: TracedRuntime, baseline: dict[str, int],
) -> dict[str, Any]:
    booking_case = _feasible_seed_for_store(dataset, case["store_id"])
    ctx = _context(world, case, case["customer_id"])
    store_id = world.store_ids[case["store_id"]]
    first = await _turn(
        session, ctx=ctx, store_id=store_id, text=booking_case["request"],
        message_id=f"{case['id']}-query", settings=settings, clock=clock, trace=trace,
    )
    candidates = _availability_candidates(first)
    if first.task_state != TaskState.PROPOSED.value or not candidates:
        return {"status": "fail", "phase": "query", "candidate_count": len(candidates), "turns": [first.to_dict()]}
    selected = await _turn(
        session, ctx=ctx, store_id=store_id, text="第一个可以",
        message_id=f"{case['id']}-select", settings=settings, clock=clock, trace=trace,
    )
    pending = selected.pending_confirmation
    if not pending:
        return {"status": "fail", "phase": "hold", "task_state": selected.task_state, "turns": [first.to_dict(), selected.to_dict()]}

    first_commit = await confirm_appointment(
        session, ctx,
        proposal_id=UUID(pending["proposal_id"]),
        proposal_version=int(pending["proposal_version"]),
        confirmation_token=pending["confirmation_token"],
        client_confirmation_event_id=f"{case['id']}-confirm-event",
        idempotency_key=case["expected"]["idempotency_key"],
        expected_task_version=int(pending["expected_task_version"]),
        now=clock.now(), clock=clock,
    )
    await session.commit()
    first_id = first_commit.appointment.id
    first_operation_id = first_commit.operation.id
    request_hash = first_commit.operation.request_hash
    replay = await confirm_appointment(
        session, ctx,
        proposal_id=UUID(pending["proposal_id"]),
        proposal_version=int(pending["proposal_version"]),
        confirmation_token=pending["confirmation_token"],
        client_confirmation_event_id=f"{case['id']}-confirm-event",
        idempotency_key=case["expected"]["idempotency_key"],
        expected_task_version=int(pending["expected_task_version"]),
        now=clock.now(), clock=clock,
    )
    await session.commit()
    after = await _counts(session)
    confirmed_operations = (await session.execute(
        select(m.Operation).where(
            m.Operation.tenant_id == world.tenant_id,
            m.Operation.idempotency_key == case["expected"]["idempotency_key"],
        )
    )).scalars().all()
    checks = {
        "appointment_created_once": after["appointments"] - baseline["appointments"] == 1,
        "hold_and_confirmation_operations_created_once": after["operations"] - baseline["operations"] == 2,
        "confirmation_operation_created_once": len(confirmed_operations) == 1,
        "same_appointment_replayed": first_id == replay.appointment.id,
        "same_operation_replayed": first_operation_id == replay.operation.id,
        "request_digest_stable": request_hash == replay.operation.request_hash,
        "idempotency_key_stable": replay.operation.idempotency_key == case["expected"]["idempotency_key"],
        "replay_flag_set": replay.replayed,
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "observed_request_digest": request_hash,
        "idempotency_key": replay.operation.idempotency_key,
        "appointment_ids_equal": first_id == replay.appointment.id,
        "replayed": replay.replayed,
        "after": after,
        "booking_fixture_case_id": booking_case["id"],
        "turns": [first.to_dict(), selected.to_dict()],
    }


async def _prepare_race_contender(
    case: dict[str, Any], *, customer_key: str, session_factory: Any,
    world: SeededSyntheticWorld, store_id: UUID, request: str,
    settings: Settings, clock: FrozenClock,
) -> dict[str, Any]:
    trace = TracedRuntime(build_runtime_for_settings(settings))
    ctx = _context(world, case, customer_key)
    async with session_factory() as session:
        first = await _turn(
            session, ctx=ctx, store_id=store_id, text=request,
            message_id=f"{case['id']}-{customer_key}-query",
            settings=settings, clock=clock, trace=trace,
        )
    candidates = _availability_candidates(first)
    return {
        "ctx": ctx, "trace": trace, "first": first,
        "candidates": candidates,
    }


async def _select_race_contender(
    case: dict[str, Any], *, contender: dict[str, Any], index: int,
    session_factory: Any, store_id: UUID, settings: Settings, clock: FrozenClock,
) -> dict[str, Any]:
    async with session_factory() as session:
        second = await _turn(
            session, ctx=contender["ctx"], store_id=store_id,
            text=f"第{index + 1}个可以",
            message_id=f"{case['id']}-{contender['ctx'].request_id}-select",
            settings=settings, clock=clock, trace=contender["trace"],
        )
    calls = [item for item in second.tool_calls if item.get("tool") == "create_hold"]
    return {**contender, "second": second, "hold_calls": calls}


async def _run_race_case(
    case: dict[str, Any], *, session_factory: Any, world: SeededSyntheticWorld,
    dataset: dict[str, Any], settings: Settings, clock: FrozenClock,
    baseline: dict[str, int],
) -> dict[str, Any]:
    store_id = world.store_ids[case["store_id"]]
    service_key = case["service_id"]
    service_data = next(
        item for item in dataset["world"]["services"]
        if item["service_id"] == service_key
    )
    start = datetime.fromisoformat(case["requested_start_at"])
    request = f"请帮我预约{service_data['name']}，{start:%Y年%m月%d日 %H:%M}。"
    customer_keys = case["expected"]["contending_customer_ids"]
    contenders = await asyncio.gather(*[
        _prepare_race_contender(
            case, customer_key=customer_key, session_factory=session_factory,
            world=world, store_id=store_id, request=request,
            settings=settings, clock=clock,
        )
        for customer_key in customer_keys
    ])
    expected_pair = case["expected"]["target_pair"]
    target = tuple(sorted((expected_pair["therapist_id"], expected_pair["room_id"])))
    indices: list[int] = []
    for contender in contenders:
        index = next((i for i, candidate in enumerate(contender["candidates"])
                      if _candidate_pair(candidate) == target
                      and datetime.fromisoformat(candidate["start_at"]) == start), None)
        if index is None:
            return {
                "status": "fail", "phase": "target_candidate_missing",
                "expected_pair": list(target),
                "candidate_pairs": [[list(_candidate_pair(c)) for c in item["candidates"]] for item in contenders],
                "model_turns": [decision for item in contenders for decision in item["trace"].decisions],
            }
        indices.append(index)

    selections = await asyncio.gather(*[
        _select_race_contender(
            case, contender=contender, index=index,
            session_factory=session_factory, store_id=store_id,
            settings=settings, clock=clock,
        )
        for contender, index in zip(contenders, indices)
    ])
    successful = [item for item in selections if item["second"].pending_confirmation]
    conflicted = [item for item in selections if not item["second"].pending_confirmation]
    hold_shape = len(successful) == 1 and len(conflicted) == 1
    slot_conflict_rejected = any(
        call.get("status") == ToolStatus.ERROR.value
        and call.get("error_code") == ErrorCode.SLOT_CONFLICT.value
        for item in conflicted for call in item["hold_calls"]
    )
    confirmed_id = None
    confirm_replayed = None
    if hold_shape:
        winner = successful[0]
        pending = winner["second"].pending_confirmation
        async with session_factory() as session:
            outcome = await confirm_appointment(
                session, winner["ctx"],
                proposal_id=UUID(pending["proposal_id"]),
                proposal_version=int(pending["proposal_version"]),
                confirmation_token=pending["confirmation_token"],
                client_confirmation_event_id=f"{case['id']}-winner-confirm-event",
                idempotency_key=f"{case['id']}-winner-confirm",
                expected_task_version=int(pending["expected_task_version"]),
                now=clock.now(), clock=clock,
            )
            await session.commit()
            confirmed_id = str(outcome.appointment.id)
    async with session_factory() as session:
        after = await _counts(session)
        booked = (await session.execute(
            select(m.ResourceAllocation).where(
                m.ResourceAllocation.tenant_id == world.tenant_id,
                m.ResourceAllocation.state == AllocationState.BOOKED.value,
                m.ResourceAllocation.start_at == start,
                m.ResourceAllocation.resource_id.in_(
                    [world.resource_ids[key] for key in expected_pair.values()]
                ),
            )
        )).scalars().all()
    booked_pairs = {
        (item.resource_id, item.start_at, item.end_at) for item in booked
    }
    target_resource_ids = {
        world.resource_ids[key] for key in expected_pair.values()
    }
    agent_selected_target = all(
        any(
            set(UUID(resource_id) for resource_id in call["arguments"].get("resource_ids", []))
            == target_resource_ids
            for decision in item["trace"].decisions
            for call in decision.get("tool_requests", [])
            if call.get("tool") == "create_hold"
        )
        for item in contenders
    )
    checks = {
        "target_pair_selected_by_both_agents": agent_selected_target,
        "exactly_one_confirmation_card": hold_shape and slot_conflict_rejected,
        "exactly_one_appointment": after["appointments"] - baseline["appointments"] == 1,
        "exactly_one_hold": after["holds"] - baseline["holds"] == 1,
        "target_resources_booked": len(booked_pairs) == 2,
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "expected_pair": list(target),
        "contender_indices": indices,
        "selection_results": [
            {
                "customer": item["ctx"].request_id,
                "task_state": item["second"].task_state,
                "hold_calls": item["hold_calls"],
                "pending_confirmation": _redact(item["second"].pending_confirmation),
            }
            for item in selections
        ],
        "appointment_id": confirmed_id,
        "after": after,
        "model_turns": [decision for item in contenders for decision in item["trace"].decisions],
    }


async def _run_case(case: dict[str, Any], dataset: dict[str, Any], settings: Settings) -> dict[str, Any]:
    url = make_url(DATABASE_URL)
    schema = f"exp_txn_{case['id'].lower().replace('-', '_')[-28:]}_{uuid4().hex[:8]}"
    bootstrap = await asyncpg.connect(
        user=url.username, password=url.password, database=url.database,
        host=url.host or "127.0.0.1", port=url.port or 5432,
    )
    try:
        await bootstrap.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await bootstrap.close()

    engine = create_async_engine(
        DATABASE_URL,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    clock = FrozenClock(TURN_NOW)
    try:
        await create_all(engine, schema=schema)
        if not await verify_exclusion_constraint(engine):
            raise RuntimeError("isolated schema is missing the resource exclusion constraint")
        session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with session_factory() as session:
            world = await seed_synthetic_world(session, dataset)
            await session.commit()
            baseline = await _counts(session)
            trace = TracedRuntime(build_runtime_for_settings(settings))
            started = time.perf_counter()
            try:
                if case["category"] == "stale_candidate":
                    result = await _run_stale_case(
                        case, session=session, world=world, settings=settings,
                        clock=clock, trace=trace, baseline=baseline,
                    )
                elif case["category"] == "idempotent_confirmation":
                    result = await _run_idempotency_case(
                        case, dataset=dataset, session=session, world=world,
                        settings=settings, clock=clock, trace=trace, baseline=baseline,
                    )
                elif case["category"] == "concurrent_single_slot_race":
                    result = await _run_race_case(
                        case, session_factory=session_factory, world=world,
                        dataset=dataset, settings=settings, clock=clock,
                        baseline=baseline,
                    )
                else:
                    raise ValueError(f"unsupported transaction category: {case['category']}")
                result.setdefault("model_turns", trace.decisions)
                result.update({
                    "type": "case", "case_id": case["id"],
                    "category": case["category"], "schema": schema,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                })
                return _redact(result)
            except Exception as exc:
                await session.rollback()
                return _redact({
                    "type": "case", "case_id": case["id"],
                    "category": case["category"], "schema": schema,
                    "status": "error", "failure": _safe_error(exc),
                    "model_turns": trace.decisions,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                })
    finally:
        await engine.dispose()


async def run(args: argparse.Namespace) -> Path:
    parsed = make_url(DATABASE_URL)
    if parsed.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("refusing to run outside appointment_test with APPOINTMENT_ENV=test")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY is required for real-model transaction evaluation")
    dataset_bytes = CASE_PATH.read_bytes()
    dataset = json.loads(dataset_bytes)
    if dataset.get("data_origin") != "generated_synthetic":
        raise RuntimeError("refusing to send non-synthetic scenario data to the model")
    settings = Settings(
        database_url=DATABASE_URL, env="test", agent_runtime="agentscope",
        model_backend="deepseek", model_name=MODEL_NAME,
        agent_prompt_version=PROMPT_VERSION, turn_budget_seconds=60, max_tool_calls=6,
    )
    cases = [item for item in dataset["cases"] if item["category"] in TRANSACTION_CATEGORIES]
    if args.case_ids:
        unknown = set(args.case_ids) - {item["id"] for item in cases}
        if unknown:
            raise ValueError(f"unknown case ids: {', '.join(sorted(unknown))}")
        selected = set(args.case_ids)
        cases = [item for item in cases if item["id"] in selected]
    if args.limit:
        cases = cases[:args.limit]

    source_paths = (
        "evals/run_synthetic_business_transaction_agent_eval.py",
        "evals/run_synthetic_business_agent_eval.py",
        "evals/synthetic_business_db.py",
        "src/appointment/agent/agentscope_adapter.py",
        "src/appointment/orchestrator/engine.py",
        "src/appointment/tools/registry.py",
        "src/appointment/tools/handlers.py",
        "src/appointment/domain/booking.py",
    )
    tool_hash = _sha256(json.dumps(
        tool_definitions(), sort_keys=True, ensure_ascii=False,
    ).encode("utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"synthetic_business_transaction_agent_{stamp}.jsonl"
    manifest = {
        "type": "metadata", "dataset_version": dataset["dataset_version"],
        "dataset_sha256": _sha256(dataset_bytes), "model_backend": "deepseek",
        "model_name": MODEL_NAME, "prompt_version": PROMPT_VERSION,
        "tool_contract_sha256": tool_hash,
        "source_sha256": {
            relative: _sha256((ROOT / relative).read_bytes())
            for relative in source_paths
        },
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "python_version": platform.python_version(), "database": parsed.database,
        "scenario_count": len(cases), "started_at": datetime.now(timezone.utc).isoformat(),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    records = []
    for index, case in enumerate(cases, 1):
        record = await _run_case(case, dataset, settings)
        records.append(record)
        with report_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        print(f"[{index}/{len(cases)}] {case['id']} {record['status']} ({record.get('elapsed_ms', 0)} ms)", flush=True)

    summary = {
        **manifest, "type": "summary", "report_jsonl": str(report_path),
        "cases_by_status": dict(Counter(item.get("status", "unknown") for item in records)),
        "cases_by_category": {
            category: dict(Counter(item.get("status", "unknown") for item in records if item["category"] == category))
            for category in sorted({item["category"] for item in records})
        },
        "model_turn_count": sum(len(item.get("model_turns") or []) for item in records),
        "database_claim": "Per-case result includes trusted multi-turn transaction flow and PostgreSQL state checks; synthetic results are not real-business performance metrics.",
    }
    summary_path = report_path.with_name(f"{report_path.stem}_summary.json")
    with summary_path.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"SUMMARY {summary_path}")
    return summary_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", action="append", dest="case_ids", help="limit to transaction case; repeat for several")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--fail-on-nonpass", action="store_true",
        help="exit non-zero unless every requested case is scored as pass",
    )
    args = parser.parse_args()
    summary_path = asyncio.run(run(args))
    if args.fail_on_nonpass:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        counts = summary.get("cases_by_status", {})
        requested = summary.get("scenario_count")
        recorded = sum(counts.values())
        if recorded != requested or counts != {"pass": requested}:
            raise SystemExit(
                "Transaction Agent evaluation gate failed: requested cases must all be recorded as pass; "
                f"requested={requested} recorded={recorded} statuses={counts}"
            )


if __name__ == "__main__":
    main()
