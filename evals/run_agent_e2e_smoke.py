"""真实模型驱动一条合成预约消息；仅在独立 appointment_test schema 运行。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from appointment.api.deps import build_runtime_for_settings
from appointment.config.settings import Settings
from appointment.core.clock import FrozenClock
from appointment.domain.booking import confirm_appointment
from appointment.db import models as m
from appointment.db.schema import create_all, verify_exclusion_constraint
from appointment.domain.context import TrustedContext
from appointment.orchestrator import Orchestrator
from appointment.seed import seed

DATABASE_URL = os.environ.get(
    "APPOINTMENT_DATABASE_URL",
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
)
MODEL_NAME = os.environ.get("APPOINTMENT_EVAL_MODEL", "deepseek-flash")
PROMPT_VERSION = os.environ.get("APPOINTMENT_EVAL_PROMPT_VERSION", "reception-v3")
FIXED_NOW = datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc)
REPORT_DIR = Path(__file__).resolve().parent / "reports"


class TracedRuntime:
    def __init__(self, base):
        self.base = base
        self.runtime_name = base.runtime_name
        self.decisions: list[dict] = []

    async def run_turn(self, request):
        item = {"state": request.task_state.value, "task_version": request.task_version}
        try:
            result = await self.base.run_turn(request)
            item.update({
                "status": "ok",
                "slot_names": [patch.get("slot_name") for patch in result.slot_patches],
                "normalized_tool_names": [tool.tool_name for tool in result.tool_requests],
                "reply_present": bool(result.reply_text),
                "clarification_present": bool(result.clarification_question),
            })
            return result
        except Exception as exc:
            item.update({
                "status": "error", "error_type": type(exc).__name__,
                "error_code": getattr(getattr(exc, "code", None), "value", None),
            })
            raise
        finally:
            self.decisions.append(item)


async def run(
    *,
    full: bool = False,
    case_id: str = "E2E_DEFAULT",
    initial_message: str = "我想约肩颈，明天下午三点",
    selection_message: str = "第一个可以",
    conversation_steps: list[dict] | None = None,
) -> Path:
    url = make_url(DATABASE_URL)
    if url.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("实验仅可使用 appointment_test")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
    schema = "exp_" + uuid4().hex
    connection_args = {
        "user": url.username, "password": url.password,
        "database": url.database, "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
    }
    bootstrap = await asyncpg.connect(**connection_args)
    try:
        await bootstrap.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await bootstrap.close()
    engine = create_async_engine(
        DATABASE_URL,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    try:
        await create_all(engine, schema=schema)
        if not await verify_exclusion_constraint(engine):
            raise RuntimeError("测试 schema 缺少排他约束")
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        settings = Settings(
            database_url=DATABASE_URL, env="test", agent_runtime="agentscope",
            model_backend="deepseek", model_name=MODEL_NAME,
            agent_prompt_version=PROMPT_VERSION, turn_budget_seconds=40,
        )
        runtime = TracedRuntime(build_runtime_for_settings(settings))
        async with factory() as session:
            prepared = await seed(session, now=FIXED_NOW)
            await session.commit()
            ctx = TrustedContext(
                tenant_id=prepared.tenant_id,
                actor_id=prepared.customer_actor_ids[0],
                customer_id=prepared.customer_ids[0],
                role="customer", request_id="synthetic-e2e-v1",
                release_id="release-local-1",
            )
            begun = time.perf_counter()
            steps = conversation_steps or [
                {"kind": "message", "text": initial_message},
                {"kind": "message", "text": selection_message},
            ]
            interactions = []
            conversation_id = None
            turn = None
            try:
                for index, step in enumerate(steps, start=1):
                    orchestrator = Orchestrator(
                        session, runtime=runtime, settings=settings,
                        clock=FrozenClock(FIXED_NOW),
                    )
                    kind = step.get("kind", "message")
                    if kind == "answer":
                        if turn is None:
                            raise ValueError("answer step requires a preceding message")
                        waiting = (
                            await session.execute(
                                select(m.WaitingRequest).where(
                                    m.WaitingRequest.task_id == turn.task_id,
                                    m.WaitingRequest.status == "OPEN",
                                )
                            )
                        ).scalars().one()
                        turn = await asyncio.wait_for(
                            orchestrator.resume_after_waiting(
                                ctx,
                                task_id=turn.task_id,
                                answer={
                                    "text": step["text"],
                                    "waiting_id": str(waiting.id),
                                    "task_version": waiting.task_version,
                                },
                                client_event_id=f"synthetic-answer-{index}",
                                now=FIXED_NOW,
                            ),
                            timeout=90,
                        )
                    elif kind == "message":
                        turn = await asyncio.wait_for(
                            orchestrator.handle_user_message(
                                ctx,
                                text=step["text"],
                                client_message_id=f"synthetic-message-{index}",
                                conversation_id=conversation_id,
                                store_id=prepared.store_id,
                                now=FIXED_NOW,
                            ),
                            timeout=90,
                        )
                        if conversation_id is None:
                            conversation_id = (
                                await session.execute(
                                    select(m.Task.conversation_id).where(
                                        m.Task.id == turn.task_id
                                    )
                                )
                            ).scalar_one()
                    else:
                        raise ValueError(f"unsupported conversation step kind: {kind}")
                    await session.commit()
                    interaction_counts = {}
                    for key, table in (("holds", m.Hold), ("appointments", m.Appointment)):
                        interaction_counts[key] = (
                            await session.execute(
                                select(func.count()).select_from(table)
                            )
                        ).scalar_one()
                    interactions.append({
                        "index": index,
                        "kind": kind,
                        "input": step["text"],
                        "task_state": turn.task_state,
                        "task_version": turn.task_version,
                        "waiting_id": None if turn.waiting_id is None else str(turn.waiting_id),
                        "reply_text": turn.reply_text,
                        "clarification_question": turn.clarification_question,
                        "tool_calls": turn.tool_calls,
                        "counts": interaction_counts,
                        "report": turn.to_dict(),
                    })
                    expected_state = step.get("expected_state")
                    if expected_state and turn.task_state != expected_state:
                        # Later user turns depend on the expected state. Stop here
                        # to record the real failure without manufacturing invalid
                        # follow-up requests against a task still awaiting an answer.
                        interactions[-1]["expectation_mismatch"] = {
                            "expected": expected_state,
                            "actual": turn.task_state,
                        }
                        break
                outcome = {
                    "status": "ok",
                    "turn": interactions[0]["report"] if interactions else None,
                    "interactions": interactions,
                    "stopped_early": len(interactions) < len(steps),
                }
                if interactions:
                    # Preserve the original single-message-plus-selection output shape.
                    if len(interactions) == 2:
                        outcome["selection_turn"] = interactions[1]["report"]
                    elif len(interactions) > 2:
                        outcome["selection_turn"] = interactions[-1]["report"]
                card = turn.pending_confirmation if turn is not None else None
                if full and card is not None:
                    committed = await confirm_appointment(
                        session, ctx,
                        proposal_id=UUID(card["proposal_id"]),
                        proposal_version=card["proposal_version"],
                        confirmation_token=card["confirmation_token"],
                        client_confirmation_event_id="synthetic-confirm-1",
                        idempotency_key="synthetic-e2e-confirm-1",
                        expected_task_version=card["expected_task_version"],
                        now=FIXED_NOW, clock=FrozenClock(FIXED_NOW),
                    )
                    await session.commit()
                    outcome["confirmation"] = {
                        "appointment_id": str(committed.appointment.id),
                        "operation_id": str(committed.operation.id),
                        "replayed": committed.replayed,
                    }
                    replay = await confirm_appointment(
                        session, ctx,
                        proposal_id=UUID(card["proposal_id"]),
                        proposal_version=card["proposal_version"],
                        confirmation_token=card["confirmation_token"],
                        client_confirmation_event_id="synthetic-confirm-1",
                        idempotency_key="synthetic-e2e-confirm-1",
                        expected_task_version=card["expected_task_version"],
                        now=FIXED_NOW, clock=FrozenClock(FIXED_NOW),
                    )
                    await session.commit()
                    outcome["confirmation_replay"] = {
                        "appointment_id": str(replay.appointment.id),
                        "operation_id": str(replay.operation.id),
                        "replayed": replay.replayed,
                    }
            except Exception as exc:
                await session.rollback()
                outcome = {
                    "status": "error", "error_type": type(exc).__name__,
                    "error_code": getattr(getattr(exc, "code", None), "value", None),
                    "interactions": interactions,
                }
            elapsed_ms = round((time.perf_counter() - begun) * 1000, 3)
            counts = {}
            for key, table in (
                ("tasks", m.Task), ("tool_executions", m.ToolExecution),
                ("holds", m.Hold), ("appointments", m.Appointment),
                ("allocations", m.ResourceAllocation), ("operations", m.Operation),
            ):
                counts[key] = (await session.execute(select(func.count()).select_from(table))).scalar_one()
            task_states = (await session.execute(select(m.Task.state))).scalars().all()
            allocation_states = (
                await session.execute(select(m.ResourceAllocation.state))
            ).scalars().all()
            report = {
                "kind": "real_model_full_booking_smoke" if full else "real_model_single_message_orchestrator_smoke",
                "schema": schema, "database": url.database,
                "model_backend": "deepseek", "model_name": MODEL_NAME,
                "prompt_version": settings.agent_prompt_version,
                "case_id": case_id,
                "input": initial_message,
                "selection_input": selection_message if full else None,
                "conversation_steps": conversation_steps,
                "elapsed_ms": elapsed_ms,
                "outcome": outcome, "database_counts": counts,
                "task_states": task_states,
                "allocation_states": allocation_states,
                "model_turns": runtime.decisions,
                "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "note": (
                    "合成用户明确选第一个候选并通过专用确认凭据提交。"
                    if full else "单条合成消息；不包含用户选候选、确认或订单完成。"
                ),
            }
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"agent_e2e_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return path
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    print(asyncio.run(run(full=args.full)))
