"""Run every synthetic business case through the configured real Agent runtime.

Each case gets a fresh, retained ``appointment_test`` schema. The runner only sends
synthetic fixture text to DeepSeek and never connects to a production database.
Results are checkpointed as JSONL after every case so interrupted runs can resume.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from appointment.api.deps import build_runtime_for_settings
from appointment.config.settings import Settings
from appointment.core.clock import FrozenClock
from appointment.db import models as m
from appointment.db.schema import create_all, verify_exclusion_constraint
from appointment.domain.context import TrustedContext
from appointment.orchestrator import Orchestrator
from appointment.tools.registry import tool_definitions
try:  # Support both direct script execution and pytest package imports.
    from evals.synthetic_business_db import seed_synthetic_world
except ImportError:  # pragma: no cover - direct ``python evals/...`` invocation
    from synthetic_business_db import seed_synthetic_world


ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "evals/cases/synthetic_business_v1.json"
REPORT_DIR = ROOT / "evals/reports"
DATABASE_URL = os.environ.get(
    "APPOINTMENT_DATABASE_URL",
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
)
MODEL_NAME = os.environ.get("APPOINTMENT_EVAL_MODEL", "deepseek-flash")
PROMPT_VERSION = os.environ.get("APPOINTMENT_EVAL_PROMPT_VERSION", "reception-v3")
TURN_NOW = datetime.fromisoformat("2026-09-23T05:00:00+00:00")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_error(exc: BaseException) -> dict[str, Any]:
    """Keep reports useful without serializing provider URLs or request headers."""

    message = str(exc)[:500]
    message = re.sub(r"(?i)(authorization|api[_-]?key|token)(\s*[:=]\s*)\S+", r"\1\2[REDACTED]", message)
    return {
        "error_type": type(exc).__name__,
        "error_code": getattr(getattr(exc, "code", None), "value", None),
        "message": message,
    }


def _redact(value: Any, key: str | None = None) -> Any:
    """Remove short-lived credentials from tool results before persisting reports."""

    if key and ("token" in key.lower() or "secret" in key.lower()):
        return "[REDACTED]" if value is not None else None
    if isinstance(value, dict):
        return {str(child_key): _redact(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, tuple):
        return [_redact(child) for child in value]
    return value


class TracedRuntime:
    """Record model decisions and prompt fingerprints, never the API credential."""

    def __init__(self, base: Any) -> None:
        self.base = base
        self.runtime_name = base.runtime_name
        self.decisions: list[dict[str, Any]] = []

    async def run_turn(self, request: Any) -> Any:
        prompt = self.base.build_system_prompt(request)
        item: dict[str, Any] = {
            "task_state": request.task_state.value,
            "task_version": request.task_version,
            "prompt_sha256": _sha256(prompt.encode("utf-8")),
        }
        started = time.perf_counter()
        try:
            output = await self.base.run_turn(request)
            item.update({
                "status": "ok",
                "intent": None if output.intent is None else output.intent.value,
                "slot_patches": _redact(list(output.slot_patches)),
                "tool_requests": _redact([
                    {"tool": tool.tool_name, "arguments": dict(tool.arguments)}
                    for tool in output.tool_requests
                ]),
                "slot_names": [patch.get("slot_name") for patch in output.slot_patches],
                "tool_names": [tool.tool_name for tool in output.tool_requests],
                "clarification_present": bool(output.clarification_question),
                "reply_present": bool(output.reply_text),
                "usage_status": output.usage_status,
                "input_tokens": output.input_tokens,
                "output_tokens": output.output_tokens,
                "cache_input_tokens": output.cache_input_tokens,
            })
            return output
        except Exception as exc:
            item.update({"status": "error", **_safe_error(exc)})
            raise
        finally:
            item["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            self.decisions.append(item)


def _count_assertions(
    *, category: str, last_turn: dict[str, Any], count_delta: dict[str, int],
    expected: dict[str, Any], requested_start_at: str | None = None,
) -> list[dict[str, Any]]:
    """Score only claims observable in a first-turn Agent execution.

    Confirmation replay, stale-candidate invalidation, and multi-user contention
    need a prepared proposal or competing write transaction; their independent
    transaction tests are reported as not-applicable here, never as Agent passes.
    """

    tools = last_turn.get("tool_calls") or []
    availability_calls = [
        item for item in tools if item.get("tool") == "search_availability"
    ]
    candidates = [
        candidate
        for call in availability_calls
        if call.get("status") == "OK"
        for candidate in ((call.get("data") or {}).get("candidates") or [])
    ]
    all_names = {
        resource.get("unit_code")
        for candidate in candidates
        for resource in candidate.get("resources", [])
    }
    assertions: list[dict[str, Any]] = []

    def add(name: str, passed: bool, actual: Any, target: Any) -> None:
        assertions.append({
            "name": name, "status": "pass" if passed else "fail",
            "actual": actual, "expected": target,
        })

    if category in {"feasible_exact_slot", "preferred_therapist_unavailable", "booking_conflict", "outside_hours"}:
        successful_search = any(call.get("status") == "OK" for call in availability_calls)
        add("availability_tool_succeeded", successful_search,
            [call.get("status") for call in availability_calls], "at least one OK search_availability result")

    if category not in {"stale_candidate", "idempotent_confirmation", "concurrent_single_slot_race"}:
        add("no_unconfirmed_hold_or_order", count_delta.get("holds", 0) == 0 and count_delta.get("appointments", 0) == 0,
            {"holds": count_delta.get("holds", 0), "appointments": count_delta.get("appointments", 0)},
            {"holds": 0, "appointments": 0})

    if category in {"missing_service", "ambiguous_service"}:
        task_state = last_turn.get("task_state", last_turn.get("state"))
        waiting = task_state == "WAITING_USER" or bool(last_turn.get("clarification_question"))
        add("clarification_before_booking", waiting,
            {"task_state": task_state, "clarification_present": bool(last_turn.get("clarification_question"))},
            "persisted clarification or WAITING_USER")
        add("no_booking_side_effect", count_delta.get("holds", 0) == 0 and count_delta.get("appointments", 0) == 0,
            {"holds": count_delta.get("holds", 0), "appointments": count_delta.get("appointments", 0)},
            {"holds": 0, "appointments": 0})

    if category == "outside_hours":
        add("no_candidate_returned", not candidates, len(candidates), 0)
    if category == "preferred_therapist_unavailable":
        preferred = expected.get("preferred_therapist_id")
        add("unavailable_preference_not_offered", preferred not in all_names,
            sorted(name for name in all_names if name == preferred), preferred)
        expected_pairs = {
            tuple(sorted((pair.get("therapist_id"), pair.get("room_id"))))
            for pair in expected.get("candidate_pairs", [])
        }
        observed_pairs = {
            tuple(sorted(resource.get("unit_code") for resource in candidate.get("resources", [])))
            for candidate in candidates
        }
        add("expected_alternative_observed", bool(expected_pairs & observed_pairs),
            [list(pair) for pair in sorted(observed_pairs)],
            [list(pair) for pair in sorted(expected_pairs)])
    if category == "booking_conflict":
        blocked = expected.get("blocked_pair", {})
        blocked_names = {blocked.get("therapist_id"), blocked.get("room_id")}
        pair_present = any(blocked_names.issubset({
            resource.get("unit_code") for resource in candidate.get("resources", [])
        }) for candidate in candidates)
        add("occupied_pair_not_offered", not pair_present, pair_present, False)
        expected_pairs = {
            tuple(sorted((pair.get("therapist_id"), pair.get("room_id"))))
            for pair in expected.get("candidate_pairs", [])
        }
        observed_pairs = {
            tuple(sorted(resource.get("unit_code") for resource in candidate.get("resources", [])))
            for candidate in candidates
        }
        if expected_pairs:
            matches_expected = bool(expected_pairs & observed_pairs)
            target: Any = [list(pair) for pair in sorted(expected_pairs)]
        else:
            matches_expected = not observed_pairs
            target = []
        add("candidate_set_matches_conflict_oracle", matches_expected,
            [list(pair) for pair in sorted(observed_pairs)], target)
    if category == "feasible_exact_slot":
        expected_pairs = {
            (pair.get("therapist_id"), pair.get("room_id"))
            for pair in expected.get("candidate_pairs", [])
        }
        observed_pairs = {
            tuple(sorted(
                resource.get("unit_code") for resource in candidate.get("resources", [])
            ))
            for candidate in candidates
        }
        expected_normalized = {tuple(sorted(pair)) for pair in expected_pairs}
        expected_time = requested_start_at
        at_time_pairs = {
            tuple(sorted(
                resource.get("unit_code") for resource in candidate.get("resources", [])
            ))
            for candidate in candidates
            if _same_instant(candidate.get("start_at"), expected_time)
        }
        matches = bool(expected_normalized & at_time_pairs)
        add("expected_feasible_pair_at_requested_time", matches,
            {"pairs": [list(pair) for pair in sorted(at_time_pairs)], "start_at": expected_time},
            {"pairs": [list(pair) for pair in sorted(expected_normalized)], "start_at": expected_time})

    return assertions


def _same_instant(actual: str | None, expected: str | None) -> bool:
    if actual is None or expected is None:
        return actual == expected
    try:
        return datetime.fromisoformat(actual) == datetime.fromisoformat(expected)
    except ValueError:
        return False


def _scenario_prompt(case: dict[str, Any]) -> tuple[str, str]:
    """Return the fixture message as authored; specialized write oracles stay separate."""

    return case["request"], "case_request"


async def _count(session: Any, table: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(table))).scalar_one())


async def _run_case(case: dict[str, Any], dataset: dict[str, Any], settings: Settings) -> dict[str, Any]:
    url = make_url(DATABASE_URL)
    case_tag = case["id"].lower().replace("-", "_")[-30:]
    schema = f"exp_sbe_{case_tag}_{uuid4().hex[:8]}"
    bootstrap_args = {
        "user": url.username, "password": url.password,
        "database": url.database, "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
    }
    bootstrap = await asyncpg.connect(**bootstrap_args)
    try:
        await bootstrap.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await bootstrap.close()

    engine = create_async_engine(
        DATABASE_URL,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    runtime_trace: TracedRuntime | None = None
    try:
        await create_all(engine, schema=schema)
        constraint_present = await verify_exclusion_constraint(engine)
        if not constraint_present:
            raise RuntimeError("isolated test schema lacks the resource exclusion constraint")
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            world = await seed_synthetic_world(session, dataset)
            await session.commit()
            baseline = {
                "holds": await _count(session, m.Hold),
                "appointments": await _count(session, m.Appointment),
                "allocations": await _count(session, m.ResourceAllocation),
            }
            store_key = case["store_id"]
            customer_key = case["customer_id"]
            ctx = TrustedContext(
                tenant_id=world.tenant_id,
                actor_id=world.actor_ids[customer_key],
                customer_id=world.customer_ids[customer_key],
                role="customer",
                request_id=case["id"],
                release_id=f"synthetic-agent-{dataset['dataset_version']}",
            )
            prompt, input_kind = _scenario_prompt(case)
            runtime_trace = TracedRuntime(build_runtime_for_settings(settings))
            started = time.perf_counter()
            turn_report: dict[str, Any] | None = None
            failure: dict[str, Any] | None = None
            try:
                orchestrator = Orchestrator(
                    session,
                    runtime=runtime_trace,
                    settings=settings,
                    clock=FrozenClock(TURN_NOW),
                )
                turn = await asyncio.wait_for(
                    orchestrator.handle_user_message(
                        ctx,
                        text=prompt,
                        client_message_id=f"{case['id']}-agent-1",
                        store_id=world.store_ids[store_key],
                        now=TURN_NOW,
                    ),
                    timeout=120,
                )
                await session.commit()
                turn_report = turn.to_dict()
            except Exception as exc:
                await session.rollback()
                failure = _safe_error(exc)

            after = {
                "holds": await _count(session, m.Hold),
                "appointments": await _count(session, m.Appointment),
                "allocations": await _count(session, m.ResourceAllocation),
                "tasks": await _count(session, m.Task),
                "tool_executions": await _count(session, m.ToolExecution),
            }
            delta = {
                key: after[key] - baseline.get(key, 0)
                for key in ("holds", "appointments", "allocations")
            }
            actual_turn = turn_report or {}
            assertions = _count_assertions(
                category=case["category"], last_turn=actual_turn,
                count_delta=delta, expected=case["expected"],
                requested_start_at=case.get("requested_start_at"),
            )
            hard_assertions = [item for item in assertions if item["status"] in {"pass", "fail"}]
            model_error = failure is not None or any(item.get("status") == "error" for item in (runtime_trace.decisions if runtime_trace else []))
            unscored_transaction_categories = {
                "stale_candidate", "idempotent_confirmation", "concurrent_single_slot_race"
            }
            case_status = "error" if model_error else (
                "observed_unscored" if case["category"] in unscored_transaction_categories else
                "pass" if hard_assertions and all(item["status"] == "pass" for item in hard_assertions)
                else "fail" if hard_assertions else "observed_unscored"
            )
            record = {
                "type": "case",
                "case_id": case["id"],
                "category": case["category"],
                "schema": schema,
                "input_kind": input_kind,
                "agent_input": prompt,
                "status": case_status,
                "failure": failure,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "task": _redact({
                    "state": actual_turn.get("task_state"),
                    "version": actual_turn.get("task_version"),
                    "end_reason": actual_turn.get("end_reason"),
                    "reply_text": actual_turn.get("reply_text"),
                    "clarification_question": actual_turn.get("clarification_question"),
                    "waiting_id": actual_turn.get("waiting_id"),
                    "tool_calls": actual_turn.get("tool_calls", []),
                }),
                "model_turns": runtime_trace.decisions if runtime_trace else [],
                "database": {
                    "baseline": baseline,
                    "after": after,
                    "delta": delta,
                    "exclusion_constraint_verified": constraint_present,
                },
                "assertions": assertions,
                "oracle_scope": (
                    "Agent first-turn behavior; stale invalidation, duplicate confirmation, "
                    "and competing writes require prepared transactional flows and are not "
                    "claimed as tested by this record."
                    if case["category"] in {"stale_candidate", "idempotent_confirmation", "concurrent_single_slot_race"}
                    else "Agent turn plus isolated PostgreSQL state and tool-result assertions."
                ),
            }
            return record
    finally:
        await engine.dispose()


def _latest_results(path: Path) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    metadata: dict[str, Any] | None = None
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return metadata, records
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("type") == "metadata":
                metadata = item
            elif item.get("type") == "case":
                records[item["case_id"]] = item
    return metadata, records


async def run(args: argparse.Namespace) -> Path:
    url = make_url(DATABASE_URL)
    if url.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("refusing to run: APPOINTMENT_DATABASE_URL must target appointment_test with APPOINTMENT_ENV=test")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY is required for real-model evaluation")
    dataset_bytes = CASE_PATH.read_bytes()
    dataset_hash = _sha256(dataset_bytes)
    dataset = json.loads(dataset_bytes)
    if dataset.get("data_origin") != "generated_synthetic":
        raise RuntimeError("refusing to send non-synthetic scenario data to the model")
    tool_hash = _sha256(json.dumps(tool_definitions(), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    source_paths = (
        "evals/run_synthetic_business_agent_eval.py",
        "evals/synthetic_business_db.py",
        "src/appointment/agent/agentscope_adapter.py",
        "src/appointment/orchestrator/engine.py",
        "src/appointment/tools/handlers.py",
        "src/appointment/domain/availability.py",
        "src/appointment/domain/booking.py",
    )
    source_hashes = {
        relative: _sha256((ROOT / relative).read_bytes()) for relative in source_paths
    }
    settings = Settings(
        database_url=DATABASE_URL,
        env="test",
        agent_runtime="agentscope",
        model_backend="deepseek",
        model_name=MODEL_NAME,
        agent_prompt_version=PROMPT_VERSION,
        turn_budget_seconds=60,
        max_tool_calls=6,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = Path(args.resume) if args.resume else REPORT_DIR / f"synthetic_business_agent_{timestamp}.jsonl"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    metadata, previous = _latest_results(report_path)
    wanted_metadata = {
        "type": "metadata",
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": dataset_hash,
        "model_backend": "deepseek",
        "model_name": MODEL_NAME,
        "prompt_version": PROMPT_VERSION,
        "tool_contract_sha256": tool_hash,
        "source_sha256": source_hashes,
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "python_version": platform.python_version(),
        "database": url.database,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if metadata:
        for field in ("dataset_sha256", "model_backend", "model_name", "prompt_version", "tool_contract_sha256"):
            if metadata.get(field) != wanted_metadata[field]:
                raise RuntimeError(f"resume metadata mismatch for {field}")
    else:
        with report_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(wanted_metadata, ensure_ascii=False) + "\n")
            handle.flush()

    cases = dataset["cases"]
    if args.scored_only:
        transaction_categories = {
            "stale_candidate", "idempotent_confirmation", "concurrent_single_slot_race"
        }
        cases = [case for case in cases if case["category"] not in transaction_categories]
    if args.case_ids:
        unknown_ids = set(args.case_ids) - {case["id"] for case in cases}
        if unknown_ids:
            raise ValueError(f"unknown case ids: {', '.join(sorted(unknown_ids))}")
        selected = set(args.case_ids)
        cases = [case for case in cases if case["id"] in selected]
    if args.limit:
        cases = cases[:args.limit]
    pending = [case for case in cases if case["id"] not in previous or previous[case["id"]].get("status") == "error"]
    if not pending:
        print(f"No pending cases. Existing results: {len(previous)}")
    for index, case in enumerate(pending, start=1):
        try:
            record = await _run_case(case, dataset, settings)
        except Exception as exc:
            record = {
                "type": "case", "case_id": case["id"], "category": case["category"],
                "status": "error", "failure": _safe_error(exc), "model_turns": [],
                "assertions": [],
            }
        with report_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        previous[case["id"]] = record
        print(f"[{index}/{len(pending)}] {case['id']} {record['status']} ({record.get('elapsed_ms', 0)} ms)", flush=True)

    selected_ids = {case["id"] for case in cases}
    completed = [previous[case_id] for case_id in selected_ids if case_id in previous]
    assertion_items = [assertion for record in completed for assertion in record.get("assertions", [])]
    result_counts = Counter(record.get("status", "unknown") for record in completed)
    summary = {
        **wanted_metadata,
        "type": "summary",
        "report_jsonl": str(report_path),
        "cases_requested": len(cases),
        "cases_recorded": len(completed),
        "cases_by_status": dict(result_counts),
        "assertions_by_status": dict(Counter(item["status"] for item in assertion_items)),
        "cases_by_category": {
            category: dict(Counter(
                record.get("status", "unknown")
                for record in completed if record.get("category") == category
            ))
            for category in sorted({case["category"] for case in cases})
        },
        "passed_case_ids": [record["case_id"] for record in completed if record.get("status") == "pass"],
        "failed_case_ids": [record["case_id"] for record in completed if record.get("status") == "fail"],
        "error_case_ids": [record["case_id"] for record in completed if record.get("status") == "error"],
        "not_claimed_as_agent_e2e": [
            "stale_candidate invalidation", "duplicate confirmation replay", "competing confirmation race"
        ],
        "note": "Model execution coverage is reported separately from domain transaction oracles; synthetic results are not real-business performance metrics.",
    }
    summary_path = report_path.with_name(f"{report_path.stem}_summary_{datetime.now(timezone.utc).strftime('%H%M%S')}.json")
    with summary_path.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"SUMMARY {summary_path}")
    return summary_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case-id", action="append", dest="case_ids",
        help="run one dataset case (repeat the option to select several cases)",
    )
    parser.add_argument(
        "--scored-only", action="store_true",
        help="exclude transaction cases handled by the multi-turn transaction evaluator",
    )
    parser.add_argument("--limit", type=int, help="run the first N cases")
    parser.add_argument("--resume", help="append to an existing JSONL checkpoint")
    parser.add_argument(
        "--fail-on-nonpass", action="store_true",
        help="exit non-zero unless every requested case is scored as pass",
    )
    options = parser.parse_args()
    summary_path = asyncio.run(run(options))
    print(summary_path)
    if options.fail_on_nonpass:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        counts = summary.get("cases_by_status", {})
        if summary.get("cases_recorded") != summary.get("cases_requested") or counts != {"pass": summary.get("cases_requested")}:
            raise SystemExit(
                "Agent evaluation gate failed: requested cases must all be recorded as pass; "
                f"requested={summary.get('cases_requested')} recorded={summary.get('cases_recorded')} statuses={counts}"
            )


if __name__ == "__main__":
    main()
