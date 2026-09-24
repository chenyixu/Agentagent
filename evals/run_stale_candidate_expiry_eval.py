"""Repeat stale-candidate and expired-confirmation regressions in fresh schemas.

Every pytest item provisions and retains its own ``case_*`` schema. This runner
records the schema delta and each run's output without cleaning any data.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_URL = (
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test"
)
TARGETS = [
    "tests/test_orchestrator_flow.py::test_recovery_cannot_hold_a_stale_candidate",
    "tests/test_core_flow.py::test_expired_hold_does_not_block_new_hold",
]
TRIALS = 3


async def list_case_schemas(database_url: str) -> list[str]:
    url = make_url(database_url)
    asyncpg_url = url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    connection = await asyncpg.connect(asyncpg_url)
    try:
        rows = await connection.fetch(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'case_%' ORDER BY schema_name"
        )
        return [row["schema_name"] for row in rows]
    finally:
        await connection.close()


def main() -> int:
    environment = os.environ.copy()
    environment.setdefault("APPOINTMENT_ENV", "test")
    environment["APPOINTMENT_MODEL_BACKEND"] = "stub"
    environment["APPOINTMENT_AGENT_RUNTIME"] = "deterministic"
    environment["APPOINTMENT_NOTIFICATION_PROVIDER"] = "sandbox"
    environment["APPOINTMENT_ALLOW_TEMP_IDENTITY"] = "true"
    database_url = environment.get("APPOINTMENT_DATABASE_URL", DEFAULT_TEST_URL)
    parsed_url = make_url(database_url)
    if environment["APPOINTMENT_ENV"] != "test" or parsed_url.database != "appointment_test":
        raise RuntimeError(
            "拒绝在 appointment_test 以外的数据库或非 test 环境运行"
        )

    schemas_before = asyncio.run(list_case_schemas(database_url))
    seen_schemas = set(schemas_before)
    trial_results: list[dict[str, object]] = []
    for trial in range(1, TRIALS + 1):
        command = [sys.executable, "-B", "-m", "pytest", "-q", *TARGETS]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        current_schemas = asyncio.run(list_case_schemas(database_url))
        added_schemas = sorted(set(current_schemas) - seen_schemas)
        seen_schemas.update(added_schemas)
        output = (completed.stdout + completed.stderr).strip()
        trial_results.append(
            {
                "trial": trial,
                "exit_code": completed.returncode,
                "new_isolated_schemas": added_schemas,
                "schema_count": len(added_schemas),
                "output": output,
            }
        )
        print(f"trial {trial}: exit={completed.returncode}; schemas={len(added_schemas)}")
        if output:
            print(output)
        if completed.returncode != 0:
            break

    all_passed = len(trial_results) == TRIALS and all(
        item["exit_code"] == 0 and item["schema_count"] == 3
        for item in trial_results
    )
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": parsed_url.database,
        "environment": environment["APPOINTMENT_ENV"],
        "trials_requested": TRIALS,
        "trials_completed": len(trial_results),
        "targets": TARGETS,
        "schemas_before_count": len(schemas_before),
        "new_isolated_schemas": [
            schema
            for trial in trial_results
            for schema in trial["new_isolated_schemas"]
        ],
        "trial_results": trial_results,
        "all_gates_passed": all_passed,
        "cleanup": "none; isolated test schemas retained",
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = ROOT / "evals" / "reports" / f"stale_candidate_expiry_eval_{stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"report: {report_path}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
