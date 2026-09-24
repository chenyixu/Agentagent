"""模型调用期间强杀编排进程，再验证租约到期后可继续执行。

运行：``.venv/bin/python -B evals/run_worker_crash_recovery_eval.py``。
每轮只在 appointment_test 新建并保留一个 exp_* schema，不清理数据库或报告。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from appointment.agent import DeterministicRuntime
from appointment.agent.ports import TurnOutput
from appointment.config.settings import Settings
from appointment.core.clock import FrozenClock
from appointment.core.errors import DomainError
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
ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = Path(__file__).resolve().parent / "reports"
CHILD_OWNER = "worker-crash-eval-child"
RECOVERY_OWNER = "worker-crash-eval-recovery"


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


async def _prepare(schema: str) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            base = await seed(session, now=NOW)
            ctx = _context({
                "tenant_id": str(base.tenant_id),
                "actor_id": str(base.customer_actor_ids[0]),
                "customer_id": str(base.customer_ids[0]),
            }, "worker-crash-eval-prepare")
            report = await Orchestrator(
                session,
                runtime=DeterministicRuntime(),
                settings=Settings(database_url=DATABASE_URL, env="test"),
                clock=FrozenClock(NOW),
            ).handle_user_message(
                ctx,
                text="你好",
                client_message_id="worker-crash-eval-initial",
                store_id=base.store_id,
                now=NOW,
            )
            await session.commit()
            if not report.waiting_id:
                raise AssertionError("初始化没有创建持久澄清任务")
            waiting = (await session.execute(
                select(m.WaitingRequest).where(m.WaitingRequest.id == report.waiting_id)
            )).scalar_one()
            return {
                "tenant_id": str(base.tenant_id),
                "actor_id": str(ctx.actor_id),
                "customer_id": str(ctx.customer_id),
                "store_id": str(base.store_id),
                "task_id": str(report.task_id),
                "waiting_id": str(waiting.id),
                "task_version": waiting.task_version,
                "question_version": waiting.question_version,
                "initial_event_cursor": report.event_cursor,
                "initial_reply_end_count": int((await session.execute(
                    select(func.count()).select_from(m.TaskEvent).where(
                        m.TaskEvent.tenant_id == base.tenant_id,
                        m.TaskEvent.task_id == report.task_id,
                        m.TaskEvent.type == "reply_end",
                    )
                )).scalar_one()),
            }
    finally:
        await engine.dispose()


async def _child(schema: str, payload: dict) -> None:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    class BlockInsideModelCall:
        runtime_name = "crash-eval-blocked-runtime"

        async def run_turn(self, request):
            print(json.dumps({
                "phase": "model_call_started",
                "task_state": request.task_state.value,
                "waiting_answer": request.waiting_answer,
            }, ensure_ascii=False), flush=True)
            await asyncio.Event().wait()
            return TurnOutput(reply_text="unreachable")

    try:
        async with factory() as session:
            waiting = (await session.execute(
                select(m.WaitingRequest).where(
                    m.WaitingRequest.tenant_id == UUID(payload["tenant_id"]),
                    m.WaitingRequest.id == UUID(payload["waiting_id"]),
                )
            )).scalar_one()
            answer = {
                "text": "肩颈，明天下午三点",
                "task_version": waiting.task_version,
                "waiting_id": str(waiting.id),
                "question_version": waiting.question_version,
            }
            await Orchestrator(
                session,
                runtime=BlockInsideModelCall(),
                settings=Settings(database_url=DATABASE_URL, env="test"),
                clock=FrozenClock(NOW),
            ).resume_after_waiting(
                _context(payload, CHILD_OWNER),
                task_id=UUID(payload["task_id"]),
                answer=answer,
                client_event_id="worker-crash-eval-answer",
                now=NOW,
            )
    finally:
        await engine.dispose()


def _kill_during_model_call(schema: str, payload: dict) -> dict:
    env = os.environ.copy()
    env.update({"APPOINTMENT_DATABASE_URL": DATABASE_URL, "APPOINTMENT_ENV": "test"})
    process = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--child", schema],
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
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=45):
                raise TimeoutError("子进程没有进入模型调用")
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"模型调用前子进程退出：{stderr[-2000:]}")
        marker = json.loads(line)
        if marker.get("phase") != "model_call_started":
            raise AssertionError(f"未知子进程标记：{marker}")
        os.kill(process.pid, signal.SIGKILL)
        return_code = process.wait(timeout=30)
        stderr = process.stderr.read() if process.stderr else ""
        return {"marker": marker, "returncode": return_code, "stderr_tail": stderr[-1000:]}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


async def _snapshot(schema: str, payload: dict) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            tenant_id = UUID(payload["tenant_id"])
            task_id = UUID(payload["task_id"])
            task = (await session.execute(
                select(m.Task).where(m.Task.tenant_id == tenant_id, m.Task.id == task_id)
            )).scalar_one()
            waiting = (await session.execute(
                select(m.WaitingRequest).where(
                    m.WaitingRequest.tenant_id == tenant_id,
                    m.WaitingRequest.id == UUID(payload["waiting_id"]),
                )
            )).scalar_one()
            answer = (await session.execute(
                select(m.WaitingAnswer).where(
                    m.WaitingAnswer.tenant_id == tenant_id,
                    m.WaitingAnswer.waiting_request_id == waiting.id,
                )
            )).scalars().all()
            message_count = int((await session.execute(
                select(func.count()).select_from(m.Message).where(
                    m.Message.tenant_id == tenant_id,
                    m.Message.client_message_id == "worker-crash-eval-resume",
                )
            )).scalar_one())
            reply_ends = int((await session.execute(
                select(func.count()).select_from(m.TaskEvent).where(
                    m.TaskEvent.tenant_id == tenant_id,
                    m.TaskEvent.task_id == task_id,
                    m.TaskEvent.type == "reply_end",
                )
            )).scalar_one())
            attempts = (await session.execute(
                select(m.ExecutionAttempt).where(
                    m.ExecutionAttempt.tenant_id == tenant_id,
                    m.ExecutionAttempt.task_id == task_id,
                ).order_by(m.ExecutionAttempt.started_at)
            )).scalars().all()
            bookings = int((await session.execute(
                select(func.count()).select_from(m.Appointment).where(
                    m.Appointment.tenant_id == tenant_id,
                    m.Appointment.customer_id == UUID(payload["customer_id"]),
                )
            )).scalar_one())
            return {
                "task_state": task.state,
                "task_version": task.version,
                "epoch": task.epoch,
                "fencing_token": task.fencing_token,
                "lease_owner": task.lease_owner,
                "lease_until": None if task.lease_until is None else task.lease_until.isoformat(),
                "waiting_state": waiting.status,
                "answer_count": len(answer),
                "answer_statuses": sorted(item.validation_status for item in answer),
                "message_count_for_recovery_request": message_count,
                "reply_end_count": reply_ends,
                "attempt_count": len(attempts),
                "attempt_fences": [item.fencing_token for item in attempts],
                "appointment_count": bookings,
            }
    finally:
        await engine.dispose()


async def _try_before_lease_expiry(schema: str, payload: dict) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    decision_now = NOW + timedelta(seconds=15)
    try:
        async with factory() as session:
            try:
                await Orchestrator(
                    session, runtime=DeterministicRuntime(),
                    settings=Settings(database_url=DATABASE_URL, env="test"),
                    clock=FrozenClock(decision_now),
                ).handle_user_message(
                    _context(payload, "worker-crash-eval-too-early"),
                    text="肩颈，明天下午三点",
                    client_message_id="worker-crash-eval-resume",
                    store_id=UUID(payload["store_id"]),
                    now=decision_now,
                )
                await session.commit()
                return {"accepted": True, "error_code": None}
            except DomainError as exc:
                await session.rollback()
                return {"accepted": False, "error_code": exc.code.value}
    finally:
        await engine.dispose()


async def _resume_after_expiry(schema: str, payload: dict) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    decision_now = NOW + timedelta(seconds=31)
    try:
        async with factory() as session:
            report = await Orchestrator(
                session,
                runtime=DeterministicRuntime(),
                settings=Settings(database_url=DATABASE_URL, env="test"),
                clock=FrozenClock(decision_now),
            ).handle_user_message(
                _context(payload, RECOVERY_OWNER),
                text="肩颈，明天下午三点",
                client_message_id="worker-crash-eval-resume",
                store_id=UUID(payload["store_id"]),
                now=decision_now,
            )
            await session.commit()
            return {
                "task_state": report.task_state,
                "end_reason": report.end_reason,
                "tool_call_count": len(report.tool_calls),
                "reply_present": bool(report.reply_text),
            }
    finally:
        await engine.dispose()


async def run_eval() -> Path:
    url = make_url(DATABASE_URL)
    if url.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("Worker 强杀恢复评测仅允许在 test 环境的 appointment_test 数据库运行")
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
            raise RuntimeError("Worker 恢复评测 schema 缺少区间排他约束")
    finally:
        await engine.dispose()

    started = time.perf_counter()
    payload = await _prepare(schema)
    child = _kill_during_model_call(schema, payload)
    killed_snapshot = await _snapshot(schema, payload)
    child_was_killed = child["returncode"] == -signal.SIGKILL
    interruption_persisted = (
        child_was_killed
        and child["marker"].get("task_state") == "COLLECTING"
        and killed_snapshot["task_state"] == "COLLECTING"
        and killed_snapshot["lease_owner"] == CHILD_OWNER
        and killed_snapshot["waiting_state"] == "ANSWERED"
        and killed_snapshot["answer_count"] == 1
        and killed_snapshot["answer_statuses"] == ["ACCEPTED"]
        and killed_snapshot["message_count_for_recovery_request"] == 0
        and killed_snapshot["reply_end_count"] == payload["initial_reply_end_count"]
        and killed_snapshot["appointment_count"] == 0
    )
    if not interruption_persisted:
        raise AssertionError(f"模型调用期间强杀后的账本不符合预期：{killed_snapshot}")

    premature = await _try_before_lease_expiry(schema, payload)
    after_premature = await _snapshot(schema, payload)
    if premature != {"accepted": False, "error_code": "LEASE_LOST"}:
        raise AssertionError(f"租约到期前新执行器未被挡住：{premature}")
    if after_premature["message_count_for_recovery_request"] != 0:
        raise AssertionError("租约到期前被拒请求意外留下用户消息")
    if after_premature["fencing_token"] != killed_snapshot["fencing_token"]:
        raise AssertionError("被拒执行器不应改变 fencing token")

    recovered = await _resume_after_expiry(schema, payload)
    after_recovery = await _snapshot(schema, payload)
    recovery_safe = (
        recovered["task_state"] == "PROPOSED"
        and recovered["end_reason"] == "COMPLETED"
        and after_recovery["task_state"] == "PROPOSED"
        and after_recovery["lease_owner"] is None
        and after_recovery["fencing_token"] > killed_snapshot["fencing_token"]
        and after_recovery["answer_count"] == 1
        and after_recovery["answer_statuses"] == ["ACCEPTED"]
        and after_recovery["message_count_for_recovery_request"] == 1
        and after_recovery["reply_end_count"] == payload["initial_reply_end_count"] + 1
        and after_recovery["appointment_count"] == 0
    )
    if not recovery_safe:
        raise AssertionError(f"租约到期后恢复结果不满足账本判据：{recovered}; {after_recovery}")

    report = {
        "kind": "orchestrator_model_call_kill_and_lease_recovery_eval",
        "schema": schema,
        "database": url.database,
        "kill_point": "after_waiting_answer_and_attempt_commit_inside_model_call",
        "child": child,
        "snapshot_after_kill": killed_snapshot,
        "pre_expiry_attempt": premature,
        "snapshot_after_pre_expiry_attempt": after_premature,
        "recovery_after_lease_expiry": recovered,
        "snapshot_after_recovery": after_recovery,
        "interruption_state_persisted": interruption_persisted,
        "new_executor_blocked_until_lease_expiry": True,
        "recovered_after_expiry_without_duplicate_answer_or_booking": recovery_safe,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "note": "单条合成 waiting 任务的本地 PostgreSQL 进程故障注入；不代表生产恢复率。schema 与报告保留。",
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"worker_crash_recovery_eval_{stamp}_{schema[-8:]}.json"
    with path.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    args = parser.parse_args()
    if args.child:
        payload = json.loads(sys.stdin.readline())
        asyncio.run(_child(args.child, payload))
    else:
        print(asyncio.run(run_eval()))
