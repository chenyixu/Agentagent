"""只读核验真实浏览器预约写入后的任务、订单、占位与资源占用。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import make_url

from appointment.config.settings import get_settings, reset_settings_cache
from appointment.db import models as m
from appointment.db.session import dispose_engine, get_sessionmaker


REPORT_DIR = Path(__file__).resolve().parent / "reports"


async def verify(task_id: UUID, schema: str, browser_result_path: Path | None) -> Path:
    settings = get_settings()
    if settings.env != "test" or settings.db_schema is None:
        raise RuntimeError("只允许在显式配置的 test schema 中做只读验收核对")
    url = make_url(settings.database_url)
    if url.database != "appointment_test" or not settings.db_schema.startswith("live_browser_"):
        raise RuntimeError("只允许读取 appointment_test.live_browser_*")

    factory = get_sessionmaker()
    async with factory() as session:
        task = await session.get(m.Task, task_id)
        if task is None:
            raise RuntimeError("任务不存在于本次隔离 schema")
        operations = (
            await session.execute(select(m.Operation).where(m.Operation.task_id == task_id))
        ).scalars().all()
        operation_ids = [row.id for row in operations]
        appointments = (
            await session.execute(
                select(m.Appointment).where(m.Appointment.created_operation_id.in_(operation_ids))
            )
        ).scalars().all() if operation_ids else []
        holds = (
            await session.execute(select(m.Hold).where(m.Hold.task_id == task_id))
        ).scalars().all()
        allocation_query = select(m.ResourceAllocation)
        if appointments:
            allocation_query = allocation_query.where(
                m.ResourceAllocation.appointment_id.in_([row.id for row in appointments])
            )
        else:
            allocation_query = allocation_query.where(
                m.ResourceAllocation.hold_id.in_([row.id for row in holds])
            ) if holds else allocation_query.where(m.ResourceAllocation.id == UUID(int=0))
        allocations = (await session.execute(allocation_query)).scalars().all()
        events = (
            await session.execute(
                select(m.TaskEvent).where(m.TaskEvent.task_id == task_id).order_by(m.TaskEvent.sequence)
            )
        ).scalars().all()

    booked_allocations = [row for row in allocations if row.state == "BOOKED"]
    checks = {
        "task_succeeded": task.state == "SUCCEEDED",
        "exactly_one_appointment": len(appointments) == 1,
        "exactly_one_hold": len(holds) == 1,
        "exactly_two_booked_allocations": len(booked_allocations) == 2,
        "exactly_two_operations": len(operations) == 2,
        "events_have_strictly_increasing_sequences": all(
            left.sequence < right.sequence for left, right in zip(events, events[1:])
        ),
    }
    if browser_result_path:
        browser_result = json.loads(browser_result_path.read_text(encoding="utf-8"))
    else:
        browser_result = None
    report = {
        "kind": "browser_live_booking_database_verification",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "database": url.database,
        "schema": settings.db_schema,
        "task_id": str(task_id),
        "task_state": task.state,
        "appointment_ids": [str(row.id) for row in appointments],
        "appointment_statuses": [row.status for row in appointments],
        "hold_states": [row.state for row in holds],
        "allocation_states": [row.state for row in allocations],
        "operation_statuses": [row.status for row in operations],
        "event_sequences": [row.sequence for row in events],
        "browser_result": browser_result,
        "checks": checks,
        "passed": all(checks.values()) and bool(browser_result and browser_result.get("passed")),
        "cleanup": "none; isolated schema retained for audit",
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"browser_live_verify_{settings.db_schema}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"report_path": str(path), **report}, ensure_ascii=False))
    await dispose_engine()
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True, type=UUID)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--browser-result", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"live_browser_[0-9a-f]{32}", args.schema):
        raise SystemExit("schema 必须是 prepare_browser_live.py 创建的 live_browser_* 名称")
    configured = make_url(get_settings().database_url)
    if configured.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("拒绝读取远程数据库")
    os.environ["APPOINTMENT_DATABASE_URL"] = configured.set(
        database="appointment_test"
    ).render_as_string(hide_password=False)
    os.environ["APPOINTMENT_ENV"] = "test"
    os.environ["APPOINTMENT_DB_SCHEMA"] = args.schema
    reset_settings_cache()
    asyncio.run(verify(args.task_id, args.schema, args.browser_result))


if __name__ == "__main__":
    main()
