"""持久等待、跨进程恢复、超时答复与接管 fencing 实验。

运行：``.venv/bin/python -B evals/run_recovery_eval.py --cases-per-group 50``。
父进程持久化 150 个等待任务并关闭数据库引擎；新的 Python 子进程恢复它们。
每次运行在 appointment_test 新建并保留 exp_* schema，不清理任何数据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from appointment.agent import DeterministicRuntime
from appointment.config.settings import Settings
from appointment.core.clock import FrozenClock
from appointment.core.enums import WaitingStatus
from appointment.db import models as m
from appointment.db.schema import create_all, verify_exclusion_constraint
from appointment.domain.context import TrustedContext
from appointment.orchestrator import Orchestrator
from appointment.seed import seed

DATABASE_URL = os.environ.get(
    "APPOINTMENT_DATABASE_URL",
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
)
NOW = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)
REPORT_DIR = Path(__file__).resolve().parent / "reports"
OBSERVED_TABLES = (
    m.Appointment,
    m.Hold,
    m.ResourceAllocation,
    m.Operation,
    m.ToolExecution,
    m.TaskEvent,
)


def _engine(schema: str):
    return create_async_engine(
        DATABASE_URL,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


async def _snapshot(session) -> dict[str, int]:
    from sqlalchemy import func

    result = {}
    for table in OBSERVED_TABLES:
        result[table.__name__] = int(
            (await session.execute(select(func.count()).select_from(table))).scalar_one()
        )
    return result


async def _prepare(schema: str, cases_per_group: int) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = Settings(database_url=DATABASE_URL, env="test")
    clock = FrozenClock(NOW)
    total = cases_per_group * 3
    prepared = []
    try:
        async with factory() as session:
            base = await seed(session, now=NOW)
            customers = []
            for index in range(total):
                actor_id = uuid4()
                customer_id = uuid4()
                group = ("fresh_worker", "timeout", "takeover")[index // cases_per_group]
                session.add(m.Actor(id=actor_id, tenant_id=base.tenant_id, status="ACTIVE"))
                session.add(m.Customer(
                    id=customer_id, tenant_id=base.tenant_id, actor_id=actor_id,
                    protected_contact_ref=f"eval:{schema[-8:]}:{index:04d}",
                    display_name=f"{group}-{index:04d}", status="ACTIVE",
                ))
                customers.append((group, actor_id, customer_id))
            await session.commit()

            for index, (group, actor_id, customer_id) in enumerate(customers):
                ctx = TrustedContext(
                    tenant_id=base.tenant_id, actor_id=actor_id,
                    customer_id=customer_id, role="customer",
                    request_id=f"recovery-seed-{index:04d}",
                    release_id="release-local-1",
                )
                report = await Orchestrator(
                    session, runtime=DeterministicRuntime(), clock=clock, settings=settings,
                ).handle_user_message(
                    ctx, text="你好", client_message_id=f"recovery-start-{index:04d}",
                    store_id=base.store_id, now=NOW,
                )
                await session.commit()
                if not report.waiting_id:
                    raise AssertionError(f"任务 {index} 未持久化 waiting request")
                prepared.append({
                    "group": group,
                    "task_id": str(report.task_id),
                    "waiting_id": str(report.waiting_id),
                    "tenant_id": str(base.tenant_id),
                    "actor_id": str(actor_id),
                    "customer_id": str(customer_id),
                    "store_id": str(base.store_id),
                    "release_id": "release-local-1",
                    "task_state_before": report.task_state,
                })
        return {"prepared": prepared}
    finally:
        await engine.dispose()


async def _resume_in_fresh_process(schema: str, cases_per_group: int) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = Settings(database_url=DATABASE_URL, env="test")
    base_clock = FrozenClock(NOW)
    outcomes = Counter()
    codes = Counter()
    task_results = []

    class TakeoverRuntime:
        runtime_name = "recovery-eval-takeover"

        async def run_turn(self, request):
            async with factory() as other:
                await other.execute(
                    update(m.Task)
                    .where(m.Task.id == request.task_id)
                    .values(
                        epoch=m.Task.epoch + 1,
                        fencing_token=m.Task.fencing_token + 1,
                        version=m.Task.version + 1,
                        lease_owner="human-takeover-eval",
                    )
                )
                await other.commit()
            from appointment.agent.ports import TurnOutput
            return TurnOutput(reply_text="stale executor reply sentinel")

    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(m.WaitingRequest, m.Task)
                    .join(m.Task, (m.Task.tenant_id == m.WaitingRequest.tenant_id)
                          & (m.Task.id == m.WaitingRequest.task_id))
                    .where(m.WaitingRequest.status == WaitingStatus.OPEN.value)
                    .order_by(m.Task.id)
                )
            ).all()
            if len(rows) != cases_per_group * 3:
                raise AssertionError(f"期待 {cases_per_group * 3} 个等待，实际 {len(rows)}")
            for index, (waiting, task) in enumerate(rows):
                customer = (
                    await session.execute(
                        select(m.Customer).where(
                            m.Customer.tenant_id == task.tenant_id,
                            m.Customer.id == task.customer_id,
                        )
                    )
                ).scalar_one()
                group = customer.display_name.split("-", 1)[0]
                ctx = TrustedContext(
                    tenant_id=task.tenant_id, actor_id=customer.actor_id,
                    customer_id=customer.id, role="customer",
                    request_id=f"recovery-worker-{index:04d}",
                    release_id=task.release_id,
                )
                late = group == "timeout"
                runtime = TakeoverRuntime() if group == "takeover" else DeterministicRuntime()
                decision_now = NOW + timedelta(seconds=1801) if late else NOW
                answer = {
                    "text": "肩颈，明天下午三点",
                    "task_version": waiting.task_version,
                    "waiting_id": str(waiting.id),
                    "question_version": waiting.question_version,
                }
                report = await Orchestrator(
                    session, runtime=runtime, clock=FrozenClock(decision_now), settings=settings,
                ).resume_after_waiting(
                    ctx, task_id=task.id, answer=answer,
                    client_event_id=f"recovery-answer-{index:04d}", now=decision_now,
                )
                await session.commit()
                status = (
                    "expired_rejected" if report.end_reason == "STALE_ANSWER_REJECTED"
                    else "fenced" if report.end_reason == "LEASE_LOST"
                    else "resumed" if report.end_reason == "COMPLETED"
                    else "unexpected"
                )
                outcomes[f"{group}:{status}"] += 1
                codes[str(report.end_reason)] += 1
                current_waiting = (
                    await session.execute(
                        select(m.WaitingRequest).where(m.WaitingRequest.id == waiting.id)
                    )
                ).scalar_one()
                current_task = (
                    await session.execute(select(m.Task).where(m.Task.id == task.id))
                ).scalar_one()
                task_tool_calls = (
                    await session.execute(
                        select(m.ToolExecution).where(
                            m.ToolExecution.tenant_id == task.tenant_id,
                            m.ToolExecution.attempt_id.in_(
                                select(m.ExecutionAttempt.id).where(
                                    m.ExecutionAttempt.task_id == task.id
                                )
                            ),
                        )
                    )
                ).scalars().all()
                sentinel_events = (
                    await session.execute(
                        select(m.TaskEvent).where(m.TaskEvent.task_id == task.id)
                    )
                ).scalars().all()
                task_results.append({
                    "group": group,
                    "result": status,
                    "end_reason": report.end_reason,
                    "task_state": current_task.state,
                    "waiting_state": current_waiting.status,
                    "tool_execution_count": len(task_tool_calls),
                    "stale_reply_emitted": any(
                        "stale executor reply sentinel" in str(event.payload)
                        for event in sentinel_events
                    ),
                })

        snap = await _snapshot_from_new_session(factory)
        return {
            "fresh_process_pid": os.getpid(),
            "scenario_results": dict(outcomes),
            "end_reasons": dict(codes),
            "task_results": task_results,
            "business_counts_after": snap,
        }
    finally:
        await engine.dispose()


async def _snapshot_from_new_session(factory) -> dict[str, int]:
    async with factory() as session:
        return await _snapshot(session)


async def run_parent(cases_per_group: int) -> Path:
    url = make_url(DATABASE_URL)
    if url.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("恢复评测只允许在 test 环境的 appointment_test 数据库运行")
    if cases_per_group <= 0:
        raise ValueError("cases-per-group 必须为正整数")
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
            raise RuntimeError("恢复评测 schema 缺少区间排他约束")
    finally:
        await engine.dispose()

    before = time.perf_counter()
    await _prepare(schema, cases_per_group)
    env = os.environ.copy()
    env.update({
        "APPOINTMENT_DATABASE_URL": DATABASE_URL,
        "APPOINTMENT_ENV": "test",
        "APPOINTMENT_MODEL_BACKEND": "stub",
        "APPOINTMENT_AGENT_RUNTIME": "deterministic",
        "RECOVERY_EVAL_SCHEMA": schema,
        "RECOVERY_EVAL_CASES": str(cases_per_group),
    })
    worker = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--worker"],
        cwd=Path(__file__).resolve().parents[1],
        env=env, check=True, capture_output=True, text=True, timeout=300,
    )
    result = json.loads(worker.stdout.strip().splitlines()[-1])
    elapsed = round(time.perf_counter() - before, 3)
    expected = cases_per_group * 3
    result.update({
        "kind": "persisted_task_cross_process_timeout_takeover_eval",
        "schema": schema,
        "database": url.database,
        "cases_per_group": cases_per_group,
        "total_cases": expected,
        "parent_process_pid": os.getpid(),
        "distinct_processes": os.getpid() != result.get("fresh_process_pid"),
        "worker_succeeded": worker.returncode == 0,
        "python_worker_returncode": worker.returncode,
        "elapsed_seconds": elapsed,
        "time_anchor": NOW.isoformat(),
        "business_state_before_resume": {
            "appointments": 0, "holds": 0, "resource_allocations": 0,
            "operations": 0,
        },
    })
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"recovery_eval_{stamp}_{schema[-8:]}.json"
    expected_results = {
        "fresh_worker:resumed": cases_per_group,
        "timeout:expired_rejected": cases_per_group,
        "takeover:fenced": cases_per_group,
    }
    no_stale_reply = all(not item["stale_reply_emitted"] for item in result["task_results"])
    no_booking_effect = all(
        result["business_counts_after"].get(name, 0) == 0
        for name in ("Appointment", "Hold", "ResourceAllocation", "Operation")
    )
    result["all_cases_as_expected"] = (
        len(result["task_results"]) == expected
        and result["scenario_results"] == expected_results
    )
    result["no_stale_reply_emitted"] = no_stale_reply
    result["no_booking_side_effect"] = no_booking_effect
    with path.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    if not (
        result["all_cases_as_expected"] and result["distinct_processes"]
        and result["worker_succeeded"] and no_stale_reply and no_booking_effect
    ):
        raise AssertionError(f"恢复评测门禁失败；原始报告已保留：{path}")
    return path


async def run_worker(schema: str, cases_per_group: int) -> dict:
    result = await _resume_in_fresh_process(schema, cases_per_group)
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-per-group", type=int, default=50)
    parser.add_argument("--worker", action="store_true")
    options = parser.parse_args()
    if options.worker:
        asyncio.run(run_worker(os.environ["RECOVERY_EVAL_SCHEMA"], int(os.environ["RECOVERY_EVAL_CASES"])))
    else:
        print(asyncio.run(run_parent(options.cases_per_group)))
