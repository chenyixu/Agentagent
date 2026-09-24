"""强杀进程覆盖预约确认的未提交与已提交响应丢失窗口。

运行：``.venv/bin/python -B evals/run_confirmation_crash_eval.py``。
仅在 appointment_test 的新 exp_* schema 运行；每次运行的 schema 与报告均保留。
不执行 schema、测试数据或报告清理。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from datetime import datetime, time as day_time, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from appointment.core.clock import FrozenClock
from appointment.core.enums import (
    AllocationState,
    AppointmentStatus,
    ConfirmationStatus,
    HoldState,
    ProposalStatus,
    TaskEventType,
    TaskState,
)
from appointment.db import models as m
from appointment.db.schema import create_all, verify_exclusion_constraint
from appointment.domain.availability import search_availability
from appointment.domain.booking import confirm_appointment, create_hold
from appointment.domain.catalog import load_service
from appointment.domain.context import TrustedContext
from appointment.domain.quote import get_service_quote
from appointment.domain.tasks import ensure_conversation, get_or_create_task, set_task_state
from appointment.seed import STORE_TIMEZONE, seed
from appointment.domain.timeutil import load_zone, resolve_local_datetime

DATABASE_URL = os.environ.get(
    "APPOINTMENT_DATABASE_URL",
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
)
NOW = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)
REPORT_DIR = Path(__file__).resolve().parent / "reports"
ROOT = Path(__file__).resolve().parents[1]


def _engine(schema: str):
    return create_async_engine(
        DATABASE_URL,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


def _context(data: dict, request_id: str) -> TrustedContext:
    return TrustedContext(
        tenant_id=UUID(data["tenant_id"]),
        actor_id=UUID(data["actor_id"]),
        customer_id=UUID(data["customer_id"]),
        role="customer",
        request_id=request_id,
        release_id="release-local-1",
    )


def _confirm_args(data: dict) -> dict:
    return {
        "proposal_id": UUID(data["proposal_id"]),
        "proposal_version": int(data["proposal_version"]),
        "confirmation_token": data["confirmation_token"],
        "client_confirmation_event_id": data["client_confirmation_event_id"],
        "idempotency_key": data["idempotency_key"],
        "expected_task_version": int(data["expected_task_version"]),
        "now": NOW,
        "clock": FrozenClock(NOW),
    }


async def _prepare_confirmation(schema: str) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    clock = FrozenClock(NOW)
    try:
        async with factory() as session:
            base = await seed(session, now=NOW)
            ctx = TrustedContext(
                tenant_id=base.tenant_id,
                actor_id=base.customer_actor_ids[0],
                customer_id=base.customer_ids[0],
                role="customer",
                request_id="crash-eval-prepare",
                release_id="release-local-1",
            )
            conversation = await ensure_conversation(
                session, ctx, store_id=base.store_id, now=NOW
            )
            task = (await get_or_create_task(
                session, ctx, conversation=conversation,
                release_id=ctx.release_id, now=NOW,
            )).task
            for state in (TaskState.SEARCHING, TaskState.PROPOSED):
                task = await set_task_state(
                    session, ctx, task=task, target=state,
                    expected_version=task.version, now=NOW,
                )

            service = await load_service(
                session, tenant_id=base.tenant_id, store_id=base.store_id,
                service_id=base.service_ids["shoulder"],
            )
            quote = await get_service_quote(
                session, tenant_id=base.tenant_id, customer_id=base.customer_ids[0],
                store_id=base.store_id, service=service, as_of=NOW,
            )
            tz = load_zone(STORE_TIMEZONE)
            local_day = NOW.astimezone(tz).date() + timedelta(days=1)
            window_start = resolve_local_datetime(
                datetime.combine(local_day, day_time(15, 0)), tz
            )
            window_end = resolve_local_datetime(
                datetime.combine(local_day, day_time(18, 0)), tz
            )
            availability = await search_availability(
                session,
                tenant_id=base.tenant_id,
                store_id=base.store_id,
                service=service,
                window_start=window_start,
                window_end=window_end,
                desired_start=window_start,
                amount_minor=quote.amount_minor,
                now=NOW,
                limit=5,
            )
            if not availability.candidates:
                raise AssertionError("种子数据没有可用候选，无法构造崩溃场景")
            candidate = availability.candidates[0]
            hold = await create_hold(
                session,
                ctx,
                task=task,
                expected_task_version=task.version,
                store_id=base.store_id,
                service_id=base.service_ids["shoulder"],
                candidate_id=candidate.candidate_id,
                start_at=candidate.start_at,
                end_at=candidate.end_at,
                resource_ids=[item.resource_id for item in candidate.resources],
                quote_token=quote.quote_token,
                now=NOW,
                clock=clock,
            )
            await session.commit()
            return {
                "tenant_id": str(base.tenant_id),
                "actor_id": str(ctx.actor_id),
                "customer_id": str(ctx.customer_id),
                "store_id": str(base.store_id),
                "task_id": str(task.id),
                "hold_id": str(hold.hold.id),
                "proposal_id": str(hold.proposal.id),
                "proposal_version": hold.proposal.version,
                "confirmation_id": str(hold.confirmation.id),
                "confirmation_token": hold.confirmation_token,
                "expected_task_version": hold.task_version,
                "client_confirmation_event_id": "crash-eval-confirm-event",
                "idempotency_key": "crash-eval-confirm-key-v1",
                "resource_ids": [str(item.resource_id) for item in candidate.resources],
            }
    finally:
        await engine.dispose()


async def _snapshot(schema: str, data: dict) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            task_id = UUID(data["task_id"])
            tenant_id = UUID(data["tenant_id"])
            hold_id = UUID(data["hold_id"])
            confirmation_id = UUID(data["confirmation_id"])
            appointment_count = int((await session.execute(
                select(func.count()).select_from(m.Appointment).where(
                    m.Appointment.tenant_id == tenant_id,
                    m.Appointment.customer_id == UUID(data["customer_id"]),
                )
            )).scalar_one())
            allocations = (await session.execute(
                select(m.ResourceAllocation).where(
                    m.ResourceAllocation.tenant_id == tenant_id,
                    m.ResourceAllocation.hold_id == hold_id,
                )
            )).scalars().all()
            hold = (await session.execute(
                select(m.Hold).where(m.Hold.tenant_id == tenant_id, m.Hold.id == hold_id)
            )).scalar_one()
            confirmation = (await session.execute(
                select(m.Confirmation).where(
                    m.Confirmation.tenant_id == tenant_id,
                    m.Confirmation.id == confirmation_id,
                )
            )).scalar_one()
            proposal = (await session.execute(
                select(m.Proposal).where(
                    m.Proposal.tenant_id == tenant_id,
                    m.Proposal.id == UUID(data["proposal_id"]),
                    m.Proposal.version == int(data["proposal_version"]),
                )
            )).scalar_one()
            task = (await session.execute(
                select(m.Task).where(m.Task.tenant_id == tenant_id, m.Task.id == task_id)
            )).scalar_one()
            confirmation_ops = (await session.execute(
                select(m.Operation).where(
                    m.Operation.tenant_id == tenant_id,
                    m.Operation.task_id == task_id,
                    m.Operation.confirmation_id == confirmation_id,
                )
            )).scalars().all()
            commit_events = (await session.execute(
                select(m.TaskEvent).where(
                    m.TaskEvent.tenant_id == tenant_id,
                    m.TaskEvent.task_id == task_id,
                    m.TaskEvent.type == TaskEventType.APPOINTMENT_COMMITTED.value,
                )
            )).scalars().all()
            appt_ids = [UUID(item.result["appointment_id"])
                        for item in confirmation_ops if item.result and "appointment_id" in item.result]
            outbox_count = 0
            audit_count = 0
            revision_count = 0
            if appt_ids:
                appt_id = appt_ids[0]
                outbox_count = int((await session.execute(
                    select(func.count()).select_from(m.Outbox).where(
                        m.Outbox.tenant_id == tenant_id,
                        m.Outbox.aggregate_type == "appointment",
                        m.Outbox.aggregate_id == appt_id,
                        m.Outbox.event_type == "appointment_confirmed",
                    )
                )).scalar_one())
                audit_count = int((await session.execute(
                    select(func.count()).select_from(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant_id,
                        m.AuditEvent.object_type == "appointment",
                        m.AuditEvent.object_id == str(appt_id),
                        m.AuditEvent.action == "confirm_appointment",
                    )
                )).scalar_one())
                revision_count = int((await session.execute(
                    select(func.count()).select_from(m.AppointmentRevision).where(
                        m.AppointmentRevision.tenant_id == tenant_id,
                        m.AppointmentRevision.appointment_id == appt_id,
                    )
                )).scalar_one())
            appointments = (await session.execute(
                select(m.Appointment).where(
                    m.Appointment.tenant_id == tenant_id,
                    m.Appointment.customer_id == UUID(data["customer_id"]),
                )
            )).scalars().all()
            return {
                "appointment_count": appointment_count,
                "appointment_statuses": sorted(item.status for item in appointments),
                "hold_state": hold.state,
                "allocation_states": sorted(item.state for item in allocations),
                "allocation_count": len(allocations),
                "confirmation_state": confirmation.status,
                "proposal_state": proposal.status,
                "task_state": task.state,
                "confirmation_operation_count": len(confirmation_ops),
                "confirmation_operation_statuses": sorted(item.status for item in confirmation_ops),
                "appointment_committed_event_count": len(commit_events),
                "outbox_confirmation_count": outbox_count,
                "confirmation_audit_count": audit_count,
                "appointment_revision_count": revision_count,
            }
    finally:
        await engine.dispose()


async def _replay(schema: str, data: dict) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            outcome = await confirm_appointment(
                session, _context(data, "crash-eval-replay"), **_confirm_args(data)
            )
            await session.commit()
            return {
                "replayed": outcome.replayed,
                "appointment_id": str(outcome.appointment.id),
                "operation_id": str(outcome.operation.id),
            }
    finally:
        await engine.dispose()


def _child(schema: str, mode: str) -> None:
    payload = json.loads(sys.stdin.readline())

    async def work() -> None:
        engine = _engine(schema)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        session = factory()
        try:
            outcome = await confirm_appointment(
                session, _context(payload, f"crash-eval-{mode}"), **_confirm_args(payload)
            )
            if mode == "before_commit":
                await session.flush()
                print(json.dumps({"phase": "flushed_uncommitted"}), flush=True)
                await asyncio.Event().wait()
            elif mode == "after_commit":
                await session.commit()
                print(json.dumps({
                    "phase": "committed_before_response",
                    "appointment_id": str(outcome.appointment.id),
                    "operation_id": str(outcome.operation.id),
                }), flush=True)
                await asyncio.Event().wait()
            else:
                raise ValueError(f"unknown kill point: {mode}")
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(work())


def _start_and_kill(schema: str, data: dict, mode: str) -> dict:
    env = os.environ.copy()
    env.update({"APPOINTMENT_DATABASE_URL": DATABASE_URL, "APPOINTMENT_ENV": "test"})
    process = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--child", mode, schema],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(data) + "\n")
        process.stdin.flush()
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=45):
                raise TimeoutError(f"子进程未到达故障注入点：{mode}")
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"子进程未报告故障注入点：{mode}; stderr={stderr[-2000:]}")
        marker = json.loads(line)
        expected_phase = "flushed_uncommitted" if mode == "before_commit" else "committed_before_response"
        if marker.get("phase") != expected_phase:
            raise AssertionError(f"故障注入点错误：{marker}")
        os.kill(process.pid, signal.SIGKILL)
        return_code = process.wait(timeout=30)
        stderr = process.stderr.read() if process.stderr else ""
        return {"marker": marker, "returncode": return_code, "stderr_tail": stderr[-1000:]}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


async def run_eval() -> Path:
    url = make_url(DATABASE_URL)
    if url.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("崩溃窗口评测仅允许在 test 环境的 appointment_test 数据库运行")
    schema = "exp_" + uuid4().hex
    admin = await asyncpg.connect(
        user=url.username, password=url.password, database=url.database,
        host=url.host or "127.0.0.1", port=url.port or 5432,
    )
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin.close()
    engine = _engine(schema)
    try:
        await create_all(engine, schema=schema)
        if not await verify_exclusion_constraint(engine):
            raise RuntimeError("崩溃评测 schema 缺少区间排他约束")
    finally:
        await engine.dispose()

    started = time.perf_counter()
    data = await _prepare_confirmation(schema)
    before_kill = _start_and_kill(schema, data, "before_commit")
    after_rollback = await _snapshot(schema, data)
    precommit_safe = (
        before_kill["returncode"] == -signal.SIGKILL
        and after_rollback["appointment_count"] == 0
        and after_rollback["hold_state"] == HoldState.HELD.value
        and after_rollback["allocation_states"] == [AllocationState.HELD.value] * 2
        and after_rollback["allocation_count"] == 2
        and after_rollback["confirmation_state"] == ConfirmationStatus.ISSUED.value
        and after_rollback["proposal_state"] == ProposalStatus.ACTIVE.value
        and after_rollback["task_state"] == TaskState.WAITING_CONFIRMATION.value
        and after_rollback["confirmation_operation_count"] == 0
        and after_rollback["appointment_committed_event_count"] == 0
        and after_rollback["outbox_confirmation_count"] == 0
        and after_rollback["confirmation_audit_count"] == 0
        and after_rollback["appointment_revision_count"] == 0
    )
    if not precommit_safe:
        raise AssertionError(f"未提交事务强杀后出现非预期状态：{after_rollback}")

    after_kill = _start_and_kill(schema, data, "after_commit")
    after_commit = await _snapshot(schema, data)
    expected_appt_id = after_kill["marker"].get("appointment_id")
    expected_operation_id = after_kill["marker"].get("operation_id")
    committed_once = (
        after_kill["returncode"] == -signal.SIGKILL
        and after_commit["appointment_count"] == 1
        and after_commit["appointment_statuses"] == [AppointmentStatus.CONFIRMED.value]
        and after_commit["hold_state"] == HoldState.BOOKED.value
        and after_commit["allocation_states"] == [AllocationState.BOOKED.value] * 2
        and after_commit["confirmation_state"] == ConfirmationStatus.CONSUMED.value
        and after_commit["proposal_state"] == ProposalStatus.COMMITTED.value
        and after_commit["task_state"] == TaskState.SUCCEEDED.value
        and after_commit["confirmation_operation_count"] == 1
        and after_commit["confirmation_operation_statuses"] == ["SUCCEEDED"]
        and after_commit["appointment_committed_event_count"] == 1
        and after_commit["outbox_confirmation_count"] == 1
        and after_commit["confirmation_audit_count"] == 1
        and after_commit["appointment_revision_count"] == 1
    )
    if not committed_once:
        raise AssertionError(f"已提交后强杀的业务快照不满足单次效果：{after_commit}")

    replay = await _replay(schema, data)
    after_replay = await _snapshot(schema, data)
    replay_safe = (
        replay["replayed"] is True
        and replay["appointment_id"] == expected_appt_id
        and replay["operation_id"] == expected_operation_id
        and after_replay == after_commit
    )
    if not replay_safe:
        raise AssertionError(f"响应丢失后的同键重试未精确重放：{replay}; {after_replay}")

    report = {
        "kind": "confirmation_process_crash_window_eval",
        "schema": schema,
        "database": url.database,
        "crash_points": ["after_business_flush_before_commit", "after_commit_before_response"],
        "kill_signal": "SIGKILL",
        "precommit_child": before_kill,
        "snapshot_after_precommit_crash": after_rollback,
        "postcommit_child": after_kill,
        "snapshot_after_postcommit_crash": after_commit,
        "idempotent_retry": replay,
        "snapshot_after_retry": after_replay,
        "precommit_rollback_safe": precommit_safe,
        "committed_effect_exactly_once": committed_once,
        "lost_response_retry_replayed_same_operation": replay_safe,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "booking_module_sha256": hashlib.sha256(
            (ROOT / "src/appointment/domain/booking.py").read_bytes()
        ).hexdigest(),
        "note": "单场景本地 PostgreSQL 故障注入；非生产部署或高可用承诺。schema 与报告保留。",
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"confirmation_crash_eval_{stamp}_{schema[-8:]}.json"
    with report_path.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return report_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", nargs=2, metavar=("MODE", "SCHEMA"))
    args = parser.parse_args()
    if args.child:
        _child(args.child[1], args.child[0])
    else:
        print(asyncio.run(run_eval()))
