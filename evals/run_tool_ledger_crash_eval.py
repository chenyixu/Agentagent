"""只读工具结果落账后、下一次模型决策期间强杀，再从账本恢复。

运行：``.venv/bin/python -B evals/run_tool_ledger_crash_eval.py``。
每轮只在 appointment_test 新建并保留一个 exp_* schema，不清理数据或报告。
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
from appointment.agent.deterministic import parse_time_expression
from appointment.agent.ports import ToolRequest, TurnOutput
from appointment.config.settings import Settings
from appointment.core.clock import FrozenClock
from appointment.core.enums import Intent, TaskEventType, TaskState, ToolStatus
from appointment.core.errors import DomainError
from appointment.db import models as m
from appointment.db.schema import create_all, verify_exclusion_constraint
from appointment.domain.context import TrustedContext
from appointment.orchestrator import Orchestrator
from appointment.seed import STORE_TIMEZONE, seed

DATABASE_URL = os.environ.get(
    "APPOINTMENT_DATABASE_URL",
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
)
NOW = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = Path(__file__).resolve().parent / "reports"
CHILD_OWNER = "tool-ledger-crash-child"
RECOVERY_OWNER = "tool-ledger-crash-recovery"


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
            }, "tool-ledger-crash-prepare")
            initial = await Orchestrator(
                session,
                runtime=DeterministicRuntime(),
                settings=Settings(database_url=DATABASE_URL, env="test"),
                clock=FrozenClock(NOW),
            ).handle_user_message(
                ctx, text="你好", client_message_id="tool-ledger-crash-initial",
                store_id=base.store_id, now=NOW,
            )
            await session.commit()
            if not initial.waiting_id:
                raise AssertionError("初始化没有创建持久澄清任务")
            waiting = (await session.execute(
                select(m.WaitingRequest).where(m.WaitingRequest.id == initial.waiting_id)
            )).scalar_one()
            service = (await session.execute(
                select(m.ServiceCatalog).where(
                    m.ServiceCatalog.tenant_id == base.tenant_id,
                    m.ServiceCatalog.id == base.service_ids["shoulder"],
                )
            )).scalar_one()
            parsed_time = parse_time_expression(
                "明天下午三点", now=NOW, tz_name=STORE_TIMEZONE
            )
            if parsed_time is None:
                raise AssertionError("固定时间表达式无法解析")
            return {
                "tenant_id": str(base.tenant_id),
                "actor_id": str(ctx.actor_id),
                "customer_id": str(ctx.customer_id),
                "store_id": str(base.store_id),
                "service_id": str(base.service_ids["shoulder"]),
                "service_name": service.name,
                "task_id": str(initial.task_id),
                "waiting_id": str(waiting.id),
                "task_version": waiting.task_version,
                "question_version": waiting.question_version,
                "initial_reply_end_count": int((await session.execute(
                    select(func.count()).select_from(m.TaskEvent).where(
                        m.TaskEvent.tenant_id == base.tenant_id,
                        m.TaskEvent.task_id == initial.task_id,
                        m.TaskEvent.type == TaskEventType.REPLY_END.value,
                    )
                )).scalar_one()),
                "time_window": parsed_time.to_slot_value(),
            }
    finally:
        await engine.dispose()


def _answer(waiting: m.WaitingRequest) -> dict:
    return {
        "text": "肩颈，明天下午三点",
        "task_version": waiting.task_version,
        "waiting_id": str(waiting.id),
        "question_version": waiting.question_version,
    }


async def _child(schema: str, payload: dict) -> None:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    class QuoteThenPauseRuntime:
        runtime_name = "tool-ledger-crash-pause"

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
                # _invoke_runtime commits the completed ToolExecution before calling
                # this method. The parent kills this process after receiving marker.
                print(json.dumps({
                    "phase": "after_quote_ledger_commit",
                    "task_state": request.task_state.value,
                    "quote_fact_rebuilt": bool(
                        (request.facts.get("followups") or {}).get("quote")
                    ),
                    "quote_in_completed_actions": any(
                        action.get("tool") == "get_service_quote"
                        for action in request.completed_actions
                    ),
                }), flush=True)
                await asyncio.Event().wait()
            raise AssertionError(f"unexpected child model call {self.calls}")

    try:
        async with factory() as session:
            waiting = (await session.execute(
                select(m.WaitingRequest).where(
                    m.WaitingRequest.tenant_id == UUID(payload["tenant_id"]),
                    m.WaitingRequest.id == UUID(payload["waiting_id"]),
                )
            )).scalar_one()
            await Orchestrator(
                session,
                runtime=QuoteThenPauseRuntime(),
                settings=Settings(database_url=DATABASE_URL, env="test"),
                clock=FrozenClock(NOW),
            ).resume_after_waiting(
                _context(payload, CHILD_OWNER),
                task_id=UUID(payload["task_id"]),
                answer=_answer(waiting),
                client_event_id="tool-ledger-crash-answer",
                now=NOW,
            )
    finally:
        await engine.dispose()


def _kill_after_ledger_commit(schema: str, payload: dict) -> dict:
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
                raise TimeoutError("子进程未到达只读工具结果已提交窗口")
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"模型暂停点之前子进程退出：{stderr[-2000:]}")
        marker = json.loads(line)
        if marker.get("phase") != "after_quote_ledger_commit":
            raise AssertionError(f"未到达预期工具账本窗口：{marker}")
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
            answers = (await session.execute(
                select(m.WaitingAnswer).where(
                    m.WaitingAnswer.tenant_id == tenant_id,
                    m.WaitingAnswer.waiting_request_id == waiting.id,
                )
            )).scalars().all()
            tool_rows = (await session.execute(
                select(m.ToolExecution)
                .join(
                    m.ExecutionAttempt,
                    (m.ExecutionAttempt.tenant_id == m.ToolExecution.tenant_id)
                    & (m.ExecutionAttempt.id == m.ToolExecution.attempt_id),
                )
                .where(
                    m.ExecutionAttempt.tenant_id == tenant_id,
                    m.ExecutionAttempt.task_id == task_id,
                )
                .order_by(m.ToolExecution.created_at)
            )).scalars().all()
            reply_ends = int((await session.execute(
                select(func.count()).select_from(m.TaskEvent).where(
                    m.TaskEvent.tenant_id == tenant_id,
                    m.TaskEvent.task_id == task_id,
                    m.TaskEvent.type == TaskEventType.REPLY_END.value,
                )
            )).scalar_one())
            candidates_ready = int((await session.execute(
                select(func.count()).select_from(m.TaskEvent).where(
                    m.TaskEvent.tenant_id == tenant_id,
                    m.TaskEvent.task_id == task_id,
                    m.TaskEvent.type == TaskEventType.CANDIDATES_READY.value,
                )
            )).scalar_one())
            appointments = int((await session.execute(
                select(func.count()).select_from(m.Appointment).where(
                    m.Appointment.tenant_id == tenant_id,
                    m.Appointment.customer_id == UUID(payload["customer_id"]),
                )
            )).scalar_one())
            holds = int((await session.execute(
                select(func.count()).select_from(m.Hold).where(
                    m.Hold.tenant_id == tenant_id,
                    m.Hold.task_id == task_id,
                )
            )).scalar_one())
            messages = int((await session.execute(
                select(func.count()).select_from(m.Message).where(
                    m.Message.tenant_id == tenant_id,
                    m.Message.client_message_id == "tool-ledger-crash-resume",
                )
            )).scalar_one())
            return {
                "task_state": task.state,
                "task_version": task.version,
                "fencing_token": task.fencing_token,
                "lease_owner": task.lease_owner,
                "waiting_state": waiting.status,
                "answer_count": len(answers),
                "answer_statuses": sorted(item.validation_status for item in answers),
                "tool_names": [item.tool_name for item in tool_rows],
                "tool_statuses": [item.status for item in tool_rows],
                "quote_result_persisted": any(
                    item.tool_name == "get_service_quote"
                    and item.status == ToolStatus.OK.value
                    and bool(item.result_ref)
                    for item in tool_rows
                ),
                "reply_end_count": reply_ends,
                "candidates_ready_count": candidates_ready,
                "appointment_count": appointments,
                "hold_count": holds,
                "recovery_message_count": messages,
            }
    finally:
        await engine.dispose()


async def _try_before_expiry(schema: str, payload: dict) -> dict:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    now = NOW + timedelta(seconds=15)
    try:
        async with factory() as session:
            try:
                await Orchestrator(
                    session, runtime=DeterministicRuntime(),
                    settings=Settings(database_url=DATABASE_URL, env="test"),
                    clock=FrozenClock(now),
                ).handle_user_message(
                    _context(payload, "tool-ledger-too-early"),
                    text="继续",
                    client_message_id="tool-ledger-crash-resume",
                    store_id=UUID(payload["store_id"]),
                    now=now,
                )
                await session.commit()
                return {"accepted": True, "error_code": None}
            except DomainError as exc:
                await session.rollback()
                return {"accepted": False, "error_code": exc.code.value}
    finally:
        await engine.dispose()


class LedgerAwareRecoveryRuntime:
    runtime_name = "ledger-aware-crash-recovery"

    def __init__(self):
        self.observations: list[dict] = []

    async def run_turn(self, request):
        followups = request.facts.get("followups") or {}
        quote = followups.get("quote")
        quote_action = any(
            item.get("tool") == "get_service_quote"
            for item in request.completed_actions
        )
        observation = {
            "state": request.task_state.value,
            "quote_fact_reconstructed": bool(quote),
            "quote_action_in_ledger": quote_action,
            "fresh_fact_keys": sorted(request.fresh_fact_keys),
        }
        self.observations.append(observation)
        if request.task_state is TaskState.SEARCHING:
            if not quote or not quote_action:
                raise AssertionError(f"恢复上下文没有重建报价账本：{observation}")
            service_id = ((request.slots.get("service") or {}).get("value") or {}).get("service_id")
            window = (request.slots.get("time_window") or {}).get("value") or {}
            return TurnOutput(
                intent=Intent.BOOK,
                tool_requests=(ToolRequest(
                    tool_name="search_availability",
                    arguments={
                        "store_id": request.facts["store_id"],
                        "service_id": service_id,
                        "window_start": window.get("start_at"),
                        "window_end": window.get("end_at"),
                        "desired_start": window.get("desired_start"),
                        "limit": 5,
                    },
                ),),
            )
        return TurnOutput(reply_text="已根据已保存的查询结果继续展示可预约时段。", intent=Intent.BOOK)


async def _recover(schema: str, payload: dict) -> tuple[dict, list[dict]]:
    engine = _engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    now = NOW + timedelta(seconds=31)
    runtime = LedgerAwareRecoveryRuntime()
    try:
        async with factory() as session:
            report = await Orchestrator(
                session, runtime=runtime,
                settings=Settings(database_url=DATABASE_URL, env="test"),
                clock=FrozenClock(now),
            ).handle_user_message(
                _context(payload, RECOVERY_OWNER),
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
                "reply_present": bool(report.reply_text),
            }, runtime.observations)
    finally:
        await engine.dispose()


async def run_eval() -> Path:
    url = make_url(DATABASE_URL)
    if url.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("工具账本崩溃评测仅允许在 test 环境的 appointment_test 数据库运行")
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
            raise RuntimeError("工具账本崩溃评测 schema 缺少区间排他约束")
    finally:
        await engine.dispose()

    started = time.perf_counter()
    payload = await _prepare(schema)
    child = _kill_after_ledger_commit(schema, payload)
    killed = await _snapshot(schema, payload)
    crash_persisted = (
        child["returncode"] == -signal.SIGKILL
        and child["marker"].get("quote_fact_rebuilt") is True
        and child["marker"].get("quote_in_completed_actions") is True
        and killed["task_state"] == TaskState.SEARCHING.value
        and killed["lease_owner"] == CHILD_OWNER
        and killed["waiting_state"] == "ANSWERED"
        and killed["answer_count"] == 1
        and killed["answer_statuses"] == ["ACCEPTED"]
        and killed["tool_names"] == ["get_service_quote"]
        and killed["tool_statuses"] == [ToolStatus.OK.value]
        and killed["quote_result_persisted"]
        and killed["appointment_count"] == 0
        and killed["hold_count"] == 0
        and killed["reply_end_count"] == payload["initial_reply_end_count"]
    )
    if not crash_persisted:
        raise AssertionError(f"强杀后的已提交工具账本不符合预期：{child}; {killed}")

    early = await _try_before_expiry(schema, payload)
    after_early = await _snapshot(schema, payload)
    if early != {"accepted": False, "error_code": "LEASE_LOST"}:
        raise AssertionError(f"租约到期前恢复请求未被拒绝：{early}")
    if after_early != killed:
        raise AssertionError(f"租约到期前请求意外改变了账本：{after_early}")

    recovered, observations = await _recover(schema, payload)
    final = await _snapshot(schema, payload)
    recovery_safe = (
        recovered["task_state"] == TaskState.PROPOSED.value
        and recovered["end_reason"] == "COMPLETED"
        and recovered["tool_call_names"] == ["search_availability"]
        and observations[0]["quote_fact_reconstructed"]
        and observations[0]["quote_action_in_ledger"]
        and final["tool_names"] == ["get_service_quote", "search_availability"]
        and final["appointment_count"] == 0
        and final["hold_count"] == 0
        and final["candidates_ready_count"] == 1
        and final["reply_end_count"] == payload["initial_reply_end_count"] + 1
        and final["waiting_state"] == "ANSWERED"
        and final["answer_count"] == 1
        and final["recovery_message_count"] == 1
        and final["lease_owner"] is None
        and final["fencing_token"] > killed["fencing_token"]
    )
    if not recovery_safe:
        raise AssertionError(f"从工具账本恢复失败：{recovered}; {observations}; {final}")

    report = {
        "kind": "read_tool_ledger_process_crash_recovery_eval",
        "schema": schema,
        "database": url.database,
        "kill_point": "after_quote_tool_execution_committed_before_next_model_decision",
        "child": child,
        "snapshot_after_kill": killed,
        "pre_expiry_recovery_attempt": early,
        "snapshot_after_pre_expiry_attempt": after_early,
        "recovery": recovered,
        "recovery_runtime_observations": observations,
        "snapshot_after_recovery": final,
        "committed_tool_ledger_survived_kill": crash_persisted,
        "recovery_reconstructed_context_without_repeating_quote_tool": recovery_safe,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "note": "单条合成 waiting 任务；只读报价结果先持久化，故障由租约过期后的新消息触发恢复。非生产恢复率。schema 与报告保留。",
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"tool_ledger_crash_eval_{stamp}_{schema[-8:]}.json"
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
