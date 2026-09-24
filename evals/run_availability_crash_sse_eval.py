"""Availability ledger flush crash boundary + SSE cursor recovery evaluation.

Run with ``.venv/bin/python -B evals/run_availability_crash_sse_eval.py``.
Each run creates and retains a unique schema in appointment_test; it never cleans up.
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
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

import run_tool_ledger_crash_eval as base
from appointment.agent import DeterministicRuntime
from appointment.agent.ports import ToolRequest, TurnOutput
from appointment.config.settings import Settings
from appointment.core.clock import FrozenClock
from appointment.core.enums import Intent, TaskEventType, TaskState, ToolStatus
from appointment.core.errors import DomainError
from appointment.db import models as m
from appointment.db.schema import create_all, verify_exclusion_constraint
from appointment.orchestrator import Orchestrator
from appointment.seed import STORE_TIMEZONE
from appointment.api.sse import stream_task_events

NOW = base.NOW
DATABASE_URL = base.DATABASE_URL
ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = Path(__file__).resolve().parent / "reports"
CHILD_OWNER = "availability-crash-child"
RECOVERY_OWNER = "availability-crash-recovery"


async def _child(schema: str, payload: dict) -> None:
    engine = base._engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    class PauseBeforeCandidateTransition(Orchestrator):
        async def _advance_after_reads(self, ctx, *, task, results, now):
            availability = next(
                (
                    item for item in results
                    if item.get("tool") == "search_availability"
                    and item.get("status") == ToolStatus.OK.value
                ),
                None,
            )
            if availability is not None:
                # ToolExecution has been flushed, but the candidate event/state
                # transition is not yet written. Parent SIGKILLs this process.
                print(json.dumps({
                    "phase": "after_availability_flush_before_candidate_transition",
                    "task_state": task.state,
                    "candidate_count_in_uncommitted_result": len(
                        (availability.get("data") or {}).get("candidates") or []
                    ),
                }), flush=True)
                await asyncio.Event().wait()
            return await super()._advance_after_reads(
                ctx, task=task, results=results, now=now
            )

    class QuoteThenAvailabilityRuntime:
        runtime_name = "availability-crash-pause"

        def __init__(self):
            self.calls = 0

        async def run_turn(self, request):
            self.calls += 1
            if self.calls == 1:
                return TurnOutput(
                    intent=Intent.BOOK,
                    slot_patches=(
                        {"slot_name": "service", "op": "SET", "value": {
                            "service_id": payload["service_id"],
                            "name": payload["service_name"],
                        }},
                        {"slot_name": "time_window", "op": "SET",
                         "value": payload["time_window"]},
                    ),
                )
            if self.calls == 2:
                return TurnOutput(
                    intent=Intent.BOOK,
                    tool_requests=(ToolRequest(
                        tool_name="get_service_quote",
                        arguments={
                            "store_id": payload["store_id"],
                            "service_id": payload["service_id"],
                        },
                    ),),
                )
            if self.calls == 3:
                window = (request.slots.get("time_window") or {}).get("value") or {}
                return TurnOutput(
                    intent=Intent.BOOK,
                    tool_requests=(ToolRequest(
                        tool_name="search_availability",
                        arguments={
                            "store_id": payload["store_id"],
                            "service_id": payload["service_id"],
                            "window_start": window.get("start_at"),
                            "window_end": window.get("end_at"),
                            "desired_start": window.get("desired_start"),
                            "limit": 5,
                        },
                    ),),
                )
            raise AssertionError(f"unexpected child model call {self.calls}")

    try:
        async with factory() as session:
            waiting = (await session.execute(
                select(m.WaitingRequest).where(
                    m.WaitingRequest.tenant_id == UUID(payload["tenant_id"]),
                    m.WaitingRequest.id == UUID(payload["waiting_id"]),
                )
            )).scalar_one()
            await PauseBeforeCandidateTransition(
                session,
                runtime=QuoteThenAvailabilityRuntime(),
                settings=Settings(database_url=DATABASE_URL, env="test"),
                clock=FrozenClock(NOW),
            ).resume_after_waiting(
                base._context(payload, CHILD_OWNER),
                task_id=UUID(payload["task_id"]),
                answer=base._answer(waiting),
                client_event_id="availability-crash-answer",
                now=NOW,
            )
    finally:
        await engine.dispose()


def _kill_before_candidate_transition(schema: str, payload: dict) -> dict:
    env = os.environ.copy()
    env.update({"APPOINTMENT_DATABASE_URL": DATABASE_URL, "APPOINTMENT_ENV": "test"})
    process = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--child", schema],
        cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=45):
                raise TimeoutError("子进程未到达可用性结果已 flush 窗口")
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"子进程提前退出：{stderr[-2000:]}")
        marker = json.loads(line)
        if marker.get("phase") != "after_availability_flush_before_candidate_transition":
            raise AssertionError(f"未到达预期故障窗口：{marker}")
        os.kill(process.pid, signal.SIGKILL)
        return_code = process.wait(timeout=30)
        stderr = process.stderr.read() if process.stderr else ""
        return {"marker": marker, "returncode": return_code, "stderr_tail": stderr[-1000:]}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


async def _event_rows(schema: str, payload: dict) -> list[dict]:
    engine = base._engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            rows = (await session.execute(
                select(m.TaskEvent)
                .where(
                    m.TaskEvent.tenant_id == UUID(payload["tenant_id"]),
                    m.TaskEvent.task_id == UUID(payload["task_id"]),
                )
                .order_by(m.TaskEvent.sequence)
            )).scalars().all()
            return [{"sequence": row.sequence, "type": row.type} for row in rows]
    finally:
        await engine.dispose()


class AvailabilityRecoveryRuntime:
    runtime_name = "availability-ledger-recovery"

    def __init__(self):
        self.observations: list[dict] = []

    async def run_turn(self, request):
        followups = request.facts.get("followups") or {}
        quote_ok = bool(followups.get("quote")) and any(
            item.get("tool") == "get_service_quote"
            for item in request.completed_actions
        )
        observation = {
            "state": request.task_state.value,
            "quote_reconstructed": quote_ok,
            "availability_reconstructed": bool(followups.get("availability")),
        }
        self.observations.append(observation)
        if request.task_state is TaskState.SEARCHING:
            if not quote_ok or observation["availability_reconstructed"]:
                raise AssertionError(f"unexpected recovery facts: {observation}")
            window = (request.slots.get("time_window") or {}).get("value") or {}
            return TurnOutput(
                intent=Intent.BOOK,
                tool_requests=(ToolRequest(
                    tool_name="search_availability",
                    arguments={
                        "store_id": request.facts["store_id"],
                        "service_id": ((request.slots.get("service") or {}).get("value") or {}).get("service_id"),
                        "window_start": window.get("start_at"),
                        "window_end": window.get("end_at"),
                        "desired_start": window.get("desired_start"),
                        "limit": 5,
                    },
                ),),
            )
        return TurnOutput(reply_text="已恢复可预约时段，可继续选择。", intent=Intent.BOOK)


async def _recover(schema: str, payload: dict) -> tuple[dict, list[dict]]:
    engine = base._engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    now = NOW + timedelta(seconds=31)
    runtime = AvailabilityRecoveryRuntime()
    try:
        async with factory() as session:
            report = await Orchestrator(
                session, runtime=runtime,
                settings=Settings(database_url=DATABASE_URL, env="test"),
                clock=FrozenClock(now),
            ).handle_user_message(
                base._context(payload, RECOVERY_OWNER),
                text="继续",
                client_message_id="tool-ledger-crash-resume",
                store_id=UUID(payload["store_id"]),
                now=now,
            )
            await session.commit()
            return ({
                "task_state": report.task_state,
                "end_reason": report.end_reason,
                "tool_call_names": [item.get("tool") for item in report.tool_calls],
                "event_cursor": report.event_cursor,
            }, runtime.observations)
    finally:
        await engine.dispose()


def _event_names_and_ids(frames: list[str]) -> tuple[list[str], list[str]]:
    names: list[str] = []
    ids: list[str] = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("event: "):
                names.append(line[7:])
            elif line.startswith("id: "):
                ids.append(line[4:])
    return names, ids


async def _sse_from_cursor(schema: str, payload: dict, after_sequence: int) -> list[str]:
    engine = base._engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def now_provider(session):
        return NOW + timedelta(seconds=31)

    try:
        return [frame async for frame in stream_task_events(
            factory,
            tenant_id=UUID(payload["tenant_id"]),
            task_id=UUID(payload["task_id"]),
            after_sequence=after_sequence,
            retention_events=500,
            retention_seconds=86400,
            now_provider=now_provider,
            poll_interval=0.001,
            max_polls=5,
        )]
    finally:
        await engine.dispose()


async def run_eval() -> Path:
    url = make_url(DATABASE_URL)
    if url.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("可用性崩溃/SSE 评测仅允许在 test 环境的 appointment_test 数据库运行")
    schema = "exp_" + uuid4().hex
    admin = await asyncpg.connect(
        user=url.username, password=url.password, database=url.database,
        host=url.host or "127.0.0.1", port=url.port or 5432,
    )
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin.close()
    engine = base._engine(schema)
    try:
        await create_all(engine, schema=schema)
        if not await verify_exclusion_constraint(engine):
            raise RuntimeError("可用性崩溃评测 schema 缺少区间排他约束")
    finally:
        await engine.dispose()

    started = time.perf_counter()
    payload = await base._prepare(schema)
    cursor_before_crash = max(
        (row["sequence"] for row in await _event_rows(schema, payload)), default=0
    )
    child = _kill_before_candidate_transition(schema, payload)
    killed = await base._snapshot(schema, payload)
    events_after_kill = await _event_rows(schema, payload)
    cursor_after_kill = max((row["sequence"] for row in events_after_kill), default=0)
    rollback_atomic = (
        child["returncode"] == -signal.SIGKILL
        and child["marker"].get("candidate_count_in_uncommitted_result", 0) > 0
        and child["marker"].get("task_state") == TaskState.SEARCHING.value
        and killed["task_state"] == TaskState.SEARCHING.value
        and killed["tool_names"] == ["get_service_quote"]
        and killed["quote_result_persisted"]
        and killed["candidates_ready_count"] == 0
        and cursor_after_kill > cursor_before_crash
        and all(row["sequence"] == index for index, row in enumerate(events_after_kill, 1))
        and not any(
            row["type"] == TaskEventType.CANDIDATES_READY.value
            for row in events_after_kill
        )
        and killed["reply_end_count"] == payload["initial_reply_end_count"]
        and killed["appointment_count"] == 0
        and killed["hold_count"] == 0
    )
    if not rollback_atomic:
        raise AssertionError(f"工具结果与候选状态事务未原子回滚：{child}; {killed}; {events_after_kill}")

    early = await base._try_before_expiry(schema, payload)
    after_early = await base._snapshot(schema, payload)
    if early != {"accepted": False, "error_code": "LEASE_LOST"} or after_early != killed:
        raise AssertionError(f"租约过期前执行器未被拒绝：{early}; {after_early}")

    recovered, observations = await _recover(schema, payload)
    final = await base._snapshot(schema, payload)
    all_events = await _event_rows(schema, payload)
    final_cursor = max((row["sequence"] for row in all_events), default=0)
    # The browser's last acknowledged cursor is from the original waiting response.
    # State/slot events committed before the crash must be included on reconnect too.
    frames = await _sse_from_cursor(schema, payload, cursor_before_crash)
    sse_names, sse_ids = _event_names_and_ids(frames)
    caught_up = await _sse_from_cursor(schema, payload, final_cursor)
    caught_up_names, _ = _event_names_and_ids(caught_up)
    recovery_consistent = (
        recovered["task_state"] == TaskState.PROPOSED.value
        and recovered["end_reason"] == "COMPLETED"
        and recovered["tool_call_names"] == ["search_availability"]
        and observations[0]["state"] == TaskState.SEARCHING.value
        and observations[0]["quote_reconstructed"]
        and not observations[0]["availability_reconstructed"]
        and final["tool_names"] == ["get_service_quote", "search_availability"]
        and final["candidates_ready_count"] == 1
        and final["appointment_count"] == 0
        and final["hold_count"] == 0
        and final["answer_count"] == 1
        and final["recovery_message_count"] == 1
        and final["lease_owner"] is None
        and final["fencing_token"] > killed["fencing_token"]
        and [row["sequence"] for row in all_events] == list(range(1, final_cursor + 1))
        and sse_names.count(TaskEventType.CANDIDATES_READY.value) == 1
        and sse_names[-1] == TaskEventType.REPLY_END.value
        and sse_ids == [str(i) for i in range(cursor_before_crash + 1, final_cursor + 1)]
        and caught_up_names == ["caught_up"]
    )
    if not recovery_consistent:
        raise AssertionError(
            f"恢复或 SSE 游标判据失败：{recovered}; {observations}; {final}; "
            f"events={all_events}; sse={sse_names}/{sse_ids}; caught_up={caught_up}"
        )

    report = {
        "kind": "availability_ledger_crash_and_sse_recovery_eval",
        "schema": schema,
        "database": url.database,
        "kill_point": "after_availability_tool_flush_before_candidate_event_and_state_transition",
        "child": child,
        "snapshot_after_kill": killed,
        "events_after_kill": events_after_kill,
        "pre_expiry_recovery_attempt": early,
        "recovery": recovered,
        "recovery_observations": observations,
        "snapshot_after_recovery": final,
        "events_after_recovery": all_events,
        "sse_replay_from_cursor": {"after_sequence": cursor_before_crash, "names": sse_names, "ids": sse_ids},
        "sse_at_tail": {"after_sequence": final_cursor, "names": caught_up_names},
        "uncommitted_availability_and_candidate_transition_rolled_back_together": rollback_atomic,
        "recovery_and_sse_cursor_are_consistent": recovery_consistent,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "note": "单条合成 waiting 任务。可用性 ToolExecution 与候选事件/状态变化位于同一事务；在 flush 后、候选推进前强杀验证整体回滚，再由租约过期后的新消息重查。SSE 从已提交游标补齐连续事件。非生产故障率。schema 与报告保留。",
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"availability_crash_sse_eval_{stamp}_{schema[-8:]}.json"
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
