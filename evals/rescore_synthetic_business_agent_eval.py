"""Re-score a completed real-model run without making new model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # Support direct execution and pytest package imports.
    from evals.run_synthetic_business_agent_eval import _count_assertions
except ImportError:  # pragma: no cover - direct ``python evals/...`` invocation
    from run_synthetic_business_agent_eval import _count_assertions


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evals/cases/synthetic_business_v1.json"
UNSCORED_TRANSACTION_CATEGORIES = {
    "stale_candidate", "idempotent_confirmation", "concurrent_single_slot_race"
}


def rescore(source: Path) -> tuple[Path, Path]:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in dataset["cases"]}
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line]
    if not rows or rows[0].get("type") != "metadata":
        raise ValueError("source is not a synthetic Agent evaluation JSONL report")
    if rows[0].get("dataset_sha256") != hashlib.sha256(DATASET_PATH.read_bytes()).hexdigest():
        raise ValueError("dataset hash differs from the completed Agent run")

    for record in rows:
        if record.get("type") != "case":
            continue
        case = by_id[record["case_id"]]
        delta = record.get("database", {}).get("delta", {})
        assertions = _count_assertions(
            category=case["category"],
            last_turn=record.get("task") or {},
            count_delta=delta,
            expected=case["expected"],
            requested_start_at=case.get("requested_start_at"),
        )
        record["assertions"] = assertions
        failed = any(item["status"] == "fail" for item in assertions)
        if record.get("status") == "error":
            pass
        elif case["category"] in UNSCORED_TRANSACTION_CATEGORIES:
            record["status"] = "observed_unscored"
        elif failed:
            record["status"] = "fail"
        elif assertions:
            record["status"] = "pass"
        else:
            record["status"] = "observed_unscored"
        record["scoring_revision"] = "oracle-aligned-v2"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rescored_path = source.with_name(f"{source.stem}_rescored_{stamp}.jsonl")
    with rescored_path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    cases = [row for row in rows if row.get("type") == "case"]
    assertions = [item for row in cases for item in row.get("assertions", [])]
    counts = Counter(row.get("status", "unknown") for row in cases)
    summary: dict[str, Any] = {
        **rows[0],
        "type": "rescored_summary",
        "scoring_revision": "oracle-aligned-v2",
        "rescored_jsonl": str(rescored_path),
        "model_case_coverage": len(cases),
        "cases_by_status": dict(counts),
        "cases_scored": counts.get("pass", 0) + counts.get("fail", 0),
        "assertions_by_status": dict(Counter(item["status"] for item in assertions)),
        "cases_by_category": {
            category: dict(Counter(
                row["status"] for row in cases if row.get("category") == category
            ))
            for category in sorted({row.get("category") for row in cases})
        },
        "model_turn_count": sum(len(row.get("model_turns", [])) for row in cases),
        "cases_with_model_calls": sum(bool(row.get("model_turns")) for row in cases),
        "database_side_effects": {
            key: sum((row.get("database", {}).get("delta", {}) or {}).get(key, 0) for row in cases)
            for key in ("holds", "appointments", "allocations")
        },
        "not_evaluated_as_transaction_scenarios": {
            "categories": sorted(UNSCORED_TRANSACTION_CATEGORIES),
            "reason": "This run drove the Agent first turn but did not prepare/expire proposals or run competing write transactions.",
        },
        "note": "Model-turn observations come from the source report. Stale candidate, idempotent confirmation, and competing confirmation scenarios require the separate multi-turn transaction evaluator and are excluded from first-turn pass-rate claims.",
    }
    summary_path = source.with_name(f"{source.stem}_rescored_summary_{stamp}.json")
    with summary_path.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return rescored_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="completed evaluation JSONL report")
    args = parser.parse_args()
    result, summary = rescore(args.report)
    print(result)
    print(summary)


if __name__ == "__main__":
    main()
