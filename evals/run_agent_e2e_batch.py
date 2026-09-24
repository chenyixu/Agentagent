"""运行真实模型合成预约闭环并按数据库业务结果评分。

示例：``.venv/bin/python -B evals/run_agent_e2e_batch.py --runs 5`` 或
``.venv/bin/python -B evals/run_agent_e2e_batch.py --case-set evals/cases/full_booking_pilot_v1.json``。
每次试验由 ``run_agent_e2e_smoke`` 创建独立 appointment_test schema；
该脚本不清理任何数据库对象。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

try:  # 支持作为 evals 子模块导入以及以脚本方式直接运行。
    from .run_agent_e2e_smoke import REPORT_DIR, run
except ImportError:  # pragma: no cover - direct script entry point
    from run_agent_e2e_smoke import REPORT_DIR, run

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "src/appointment/agent/agentscope_adapter.py",
    "src/appointment/orchestrator/engine.py",
    "src/appointment/domain/booking.py",
    "evals/run_agent_e2e_smoke.py",
    "evals/run_agent_e2e_batch.py",
)


def sha256_file(relative_path: str) -> str:
    return hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()


def checks(report: dict, case: dict | None = None) -> dict[str, bool]:
    outcome = report.get("outcome", {})
    confirmation = outcome.get("confirmation") or {}
    replay = outcome.get("confirmation_replay") or {}
    counts = report.get("database_counts", {})
    result = {
        "orchestrator_ok": outcome.get("status") == "ok",
        "task_succeeded": report.get("task_states") == ["SUCCEEDED"],
        "one_appointment": counts.get("appointments") == 1,
        "one_hold": counts.get("holds") == 1,
        "two_booked_allocations": (
            counts.get("allocations") == 2
            and report.get("allocation_states") == ["BOOKED", "BOOKED"]
        ),
        "one_hold_and_one_confirmation_operation": counts.get("operations") == 2,
        "confirmation_replay_same_appointment": (
            confirmation.get("appointment_id") is not None
            and confirmation.get("appointment_id") == replay.get("appointment_id")
        ),
        "confirmation_replay_same_operation": (
            confirmation.get("operation_id") is not None
            and confirmation.get("operation_id") == replay.get("operation_id")
        ),
        "first_confirmation_then_replay": (
            confirmation.get("replayed") is False and replay.get("replayed") is True
        ),
        "all_model_turns_returned": (
            bool(report.get("model_turns"))
            and all(turn.get("status") == "ok" for turn in report["model_turns"])
        ),
    }
    if case is not None:
        first_turn = outcome.get("turn") or {}
        selection_turn = outcome.get("selection_turn") or {}
        interactions = outcome.get("interactions", [])
        tool_calls = [
            call
            for interaction in interactions
            for call in interaction.get("tool_calls", [])
        ] or first_turn.get("tool_calls", [])
        quotes = [call.get("data", {}) for call in tool_calls if call.get("tool") == "get_service_quote"]
        availability = [call.get("data", {}) for call in tool_calls if call.get("tool") == "search_availability"]
        expected_start = case.get("expected_start_at")
        expected_amount = case.get("expected_amount_minor")
        result.update({
            "proposal_reached": any(
                interaction.get("task_state") == "PROPOSED"
                for interaction in interactions
            ) or first_turn.get("task_state") == "PROPOSED",
            "expected_service_price_returned": any(
                quote.get("amount_minor") == expected_amount for quote in quotes
            ),
            "requested_slot_offered": any(
                candidate.get("start_at") == expected_start
                for response in availability
                for candidate in response.get("candidates", [])
            ),
            "selection_created_confirmation": (
                selection_turn.get("task_state") == "WAITING_CONFIRMATION"
                and selection_turn.get("pending_confirmation") is not None
            ),
        })
        if "expected_states" in case:
            result["expected_interaction_states"] = [
                interaction.get("task_state") for interaction in interactions
            ] == case["expected_states"]
        if "expected_hold_counts" in case:
            result["hold_counts_at_interaction_boundaries"] = [
                (interaction.get("counts") or {}).get("holds")
                for interaction in interactions
            ] == case["expected_hold_counts"]
        if "expected_clarifications" in case:
            actual_clarifications = [
                bool(interaction.get("clarification_question"))
                for interaction in interactions
            ]
            expected_clarifications = case["expected_clarifications"]
            result["expected_clarification_presence"] = (
                len(actual_clarifications) == len(expected_clarifications)
                and all(
                    expected is None or actual == expected
                    for actual, expected in zip(
                        actual_clarifications, expected_clarifications
                    )
                )
            )
        if "expected_min_candidate_count" in case:
            result["multiple_candidates_offered"] = any(
                len((call.get("data") or {}).get("candidates") or [])
                >= case["expected_min_candidate_count"]
                for call in tool_calls
                if call.get("tool") == "search_availability"
            )
    return result


def passes(report: dict, case: dict | None = None) -> bool:
    return all(checks(report, case).values())


async def run_batch(runs: int = 3, case_set_path: Path | None = None) -> Path:
    if case_set_path is None:
        if not 1 <= runs <= 20:
            raise ValueError("runs 必须在 1 到 20 之间")
        dataset = {
            "dataset_version": "single-scenario-repeat-v1",
            "data_origin": "handwritten_synthetic",
            "selection_message": "第一个可以",
            "cases": [{
                "id": "E2E_DEFAULT",
                "category": "shoulder_relative_date",
                "input": "我想约肩颈，明天下午三点",
                "expected_start_at": "2026-09-24T15:00:00+08:00",
                "expected_amount_minor": 26800,
            } for _ in range(runs)],
        }
        dataset_hash = hashlib.sha256(
            json.dumps(dataset, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        mode = "real_model_synthetic_booking_repeated_eval"
    else:
        dataset = json.loads(case_set_path.read_text(encoding="utf-8"))
        if not isinstance(dataset.get("cases"), list) or not dataset["cases"]:
            raise ValueError("case set must contain a non-empty cases array")
        if len(dataset["cases"]) > 20:
            raise ValueError("case set must contain at most 20 cases per batch")
        required = {"id", "input", "expected_start_at", "expected_amount_minor"}
        for item in dataset["cases"]:
            missing = required - item.keys()
            if missing:
                raise ValueError(f"case {item.get('id')} is missing: {sorted(missing)}")
        runs = len(dataset["cases"])
        dataset_hash = hashlib.sha256(case_set_path.read_bytes()).hexdigest()
        mode = "real_model_synthetic_booking_scenario_eval"
    started = datetime.now(timezone.utc)
    source_hashes = {path: sha256_file(path) for path in SOURCE_FILES}
    reports = []
    for index, case in enumerate(dataset["cases"], start=1):
        path = await run(
            full=True,
            case_id=case["id"],
            initial_message=case["input"],
            selection_message=dataset.get("selection_message", "第一个可以"),
            conversation_steps=case.get("conversation_steps"),
        )
        item = json.loads(path.read_text(encoding="utf-8"))
        case_checks = checks(item, case)
        reports.append({
            "trial": index,
            "case_id": case["id"],
            "category": case.get("category"),
            "input": case["input"],
            "expected_start_at": case["expected_start_at"],
            "expected_amount_minor": case["expected_amount_minor"],
            "report_path": str(path.relative_to(ROOT)),
            "passed": all(case_checks.values()),
            "checks": case_checks,
            "failed_checks": [name for name, passed in case_checks.items() if not passed],
            "elapsed_ms": item.get("elapsed_ms"),
            "schema": item.get("schema"),
            "task_states": item.get("task_states"),
            "database_counts": item.get("database_counts"),
            "confirmation": item.get("outcome", {}).get("confirmation"),
            "confirmation_replay": item.get("outcome", {}).get("confirmation_replay"),
            "model_turns": len(item.get("model_turns", [])),
            "interactions": [
                {
                    "kind": interaction.get("kind"),
                    "input": interaction.get("input"),
                    "task_state": interaction.get("task_state"),
                    "waiting_id": interaction.get("waiting_id"),
                    "counts": interaction.get("counts"),
                    "candidate_counts": [
                        len((call.get("data") or {}).get("candidates") or [])
                        for call in interaction.get("tool_calls", [])
                        if call.get("tool") == "search_availability"
                    ],
                }
                for interaction in item.get("outcome", {}).get("interactions", [])
            ],
            "model_turn_statuses": [
                turn.get("status") for turn in item.get("model_turns", [])
            ],
            "runtime_output_tool_requests": [
                {
                    "state": turn.get("state"),
                    "normalized_tool_names": turn.get("normalized_tool_names", []),
                }
                for turn in item.get("model_turns", [])
            ],
            "git_sha": item.get("git_sha"),
            "model_name": item.get("model_name"),
            "prompt_version": item.get("prompt_version"),
        })

    latencies = [item["elapsed_ms"] for item in reports if item["elapsed_ms"] is not None]
    passed = sum(item["passed"] for item in reports)
    by_category = {}
    for category in sorted({item.get("category") or "uncategorized" for item in reports}):
        group = [item for item in reports if (item.get("category") or "uncategorized") == category]
        by_category[category] = {"passed": sum(item["passed"] for item in group), "total": len(group)}
    batch = {
        "kind": mode,
        "started_at": started.isoformat(),
        "runs": runs,
        "passed": passed,
        "pass_rate": round(passed / runs, 4),
        "passed_by_category": by_category,
        "elapsed_ms_median": round(statistics.median(latencies), 3) if latencies else None,
        "elapsed_ms_min": min(latencies) if latencies else None,
        "elapsed_ms_max": max(latencies) if latencies else None,
        "source_sha256": source_hashes,
        "dataset_version": dataset["dataset_version"],
        "dataset_origin": dataset["data_origin"],
        "case_set_sha256": dataset_hash,
        "trials": reports,
        "case": {
            "selection_message": dataset.get("selection_message", "第一个可以"),
            "confirmation": "server-issued confirmation token and idempotency key",
            "assertion": (
                "Expected quote amount and exact slot offered; proposal and explicit candidate selection; "
                "SUCCEEDED task; exactly one appointment and hold; two BOOKED allocations; "
                "two operations; confirmation retry returns the original appointment and operation"
            ),
        },
        "limitations": [
            "All cases are hand-authored synthetic scenarios against a two-service seeded store; results are not production task completion rates.",
            "Business reads and hold creation include deterministic server orchestration; this does not isolate model tool-choice quality.",
            "runtime_output_tool_requests records adapter-normalized output after server policy; it is not a trace of raw provider tool-call intent.",
            "Model provider usage and cost are unavailable in the underlying reports.",
            "Latency is full local synthetic flow duration for each trial; no p95 is reported for this small sample.",
            "Each trial leaves its isolated test schema and raw report in place.",
        ],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    destination = REPORT_DIR / f"agent_e2e_batch_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(batch, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--case-set", type=Path)
    args = parser.parse_args()
    print(asyncio.run(run_batch(args.runs, args.case_set)))
