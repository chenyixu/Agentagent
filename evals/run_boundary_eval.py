"""评估工具入口对越权与伪造身份请求的拒绝效果。

运行：``.venv/bin/python evals/run_boundary_eval.py --requests 300``。
只允许测试环境 ``appointment_test``；每次运行创建并保留新的 ``exp_*`` schema。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from appointment.config.settings import Settings
from appointment.core.enums import ToolStatus
from appointment.core.clock import FrozenClock
from appointment.db import models as m
from appointment.db.schema import create_all, verify_exclusion_constraint
from appointment.domain.context import TrustedContext
from appointment.seed import seed
from appointment.tools.registry import TOOL_REGISTRY, invoke_tool

DATABASE_URL = os.environ.get(
    "APPOINTMENT_DATABASE_URL",
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
)
NOW = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)
REPORT_DIR = Path(__file__).resolve().parent / "reports"
IDENTITY_FIELDS = ("tenant_id", "actor_id", "customer_id", "role", "epoch")
OBSERVED_TABLES = (
    m.Appointment,
    m.Hold,
    m.ResourceAllocation,
    m.Operation,
    m.Task,
    m.TaskEvent,
    m.ToolExecution,
    m.AuditEvent,
    m.Outbox,
)


def _arguments(name: str, *, seed_data) -> dict:
    """每个工具一份通过 schema 的参数，确保角色测试抵达权限判定层。"""

    service_id = seed_data.service_ids["shoulder"]
    store_id = seed_data.store_id
    resource_id = seed_data.resource_ids[0]
    start = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc)
    values = {
        "search_knowledge": {"query": "预约政策", "store_id": store_id},
        "get_service_quote": {"store_id": store_id, "service_id": service_id},
        "search_availability": {
            "store_id": store_id, "service_id": service_id,
            "window_start": start, "window_end": end,
        },
        "get_appointment": {"appointment_id": uuid4()},
        "create_hold": {
            "task_id": uuid4(), "expected_task_version": 1,
            "store_id": store_id, "service_id": service_id,
            "candidate_id": "candidate-eval-0001", "start_at": start,
            "end_at": end, "resource_ids": [resource_id],
            "quote_token": "server-signed-evaluation-quote",
        },
        "confirm_appointment": {
            "proposal_id": uuid4(), "proposal_version": 1,
            "confirmation_token": "server-signed-confirm-token",
            "client_confirmation_event_id": "evaluation-event-0001",
            "idempotency_key": "evaluation-confirm-0001",
            "expected_task_version": 1,
        },
        "reschedule_appointment": {
            "appointment_id": uuid4(), "expected_appointment_version": 1,
            "new_start_at": start, "new_end_at": end,
            "new_resource_ids": [resource_id],
            "quote_token": "server-signed-evaluation-quote",
            "idempotency_key": "evaluation-reschedule-0001",
        },
        "cancel_appointment": {
            "appointment_id": uuid4(), "expected_appointment_version": 1,
            "idempotency_key": "evaluation-cancel-0001",
        },
        "transfer_to_human": {
            "task_id": uuid4(), "reason_code": "evaluation",
            "summary": "fabricated evaluation request", "urgency": "normal",
        },
    }
    return values[name]


async def _counts(session) -> dict[str, int]:
    result = {}
    for table in OBSERVED_TABLES:
        result[table.__name__] = int(
            (await session.execute(select(func.count()).select_from(table))).scalar_one()
        )
    return result


async def run(request_count: int) -> Path:
    url = make_url(DATABASE_URL)
    if url.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("越权评测只允许在 test 环境的 appointment_test 数据库运行")
    if request_count <= 0 or request_count % 3:
        raise ValueError("请求数必须是正整数且可被 3 整除，以平衡三类攻击")

    schema = "exp_" + uuid4().hex
    admin = await asyncpg.connect(
        user=url.username, password=url.password, database=url.database,
        host=url.host or "127.0.0.1", port=url.port or 5432,
    )
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin.close()

    engine = create_async_engine(
        DATABASE_URL,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    try:
        await create_all(engine, schema=schema)
        if not await verify_exclusion_constraint(engine):
            raise RuntimeError("实验 schema 缺少资源区间排他约束")
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        settings = Settings(database_url=DATABASE_URL, env="test")
        clock = FrozenClock(NOW)
        outcomes: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        by_tool: Counter[str] = Counter()
        by_attack: Counter[str] = Counter()
        handler_calls = 0

        async with factory() as session:
            seed_data = await seed(session, now=NOW)
            await session.commit()
            before = await _counts(session)
            names = tuple(TOOL_REGISTRY)
            per_attack = request_count // 3

            requests = []
            for index in range(per_attack):
                tool = names[index % len(names)]
                requests.append(("stage_denied", tool, "customer", {}, ()))

                tool = names[(index + 3) % len(names)]
                requests.append(("role_denied", tool, "untrusted", _arguments(tool, seed_data=seed_data), (tool,)))

                tool = names[(index + 6) % len(names)]
                forged = _arguments(tool, seed_data=seed_data)
                forged[IDENTITY_FIELDS[index % len(IDENTITY_FIELDS)]] = str(uuid4())
                requests.append(("identity_injection", tool, "customer", forged, (tool,)))

            started = time.perf_counter()
            for index, (attack, tool, role, arguments, allowed) in enumerate(requests):
                ctx = TrustedContext(
                    tenant_id=seed_data.tenant_id,
                    actor_id=seed_data.customer_actor_ids[0],
                    customer_id=seed_data.customer_ids[0],
                    role=role,
                    request_id=f"boundary-{index:04d}",
                    release_id="boundary-eval-v1",
                )
                result = await invoke_tool(
                    session, ctx, tool, arguments, now=NOW, clock=clock,
                    allowed_tools=allowed,
                )
                outcomes[result.status.value] += 1
                if result.error_code:
                    errors[result.error_code.value] += 1
                by_tool[tool] += 1
                by_attack[attack] += 1
                if result.status is ToolStatus.OK:
                    handler_calls += 1

            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            after = await _counts(session)

        blocked = sum(outcomes.values()) == request_count and outcomes.get(ToolStatus.OK.value, 0) == 0
        state_unchanged = before == after
        report = {
            "kind": "tool_boundary_adversarial_eval",
            "schema": schema,
            "database": url.database,
            "registry_tool_count": len(TOOL_REGISTRY),
            "request_count": request_count,
            "attack_groups": dict(by_attack),
            "requests_by_tool": dict(by_tool),
            "result_statuses": dict(outcomes),
            "error_codes": dict(errors),
            "successful_handler_calls": handler_calls,
            "business_state_before": before,
            "business_state_after": after,
            "business_state_unchanged": state_unchanged,
            "all_requests_blocked": blocked,
            "elapsed_ms": elapsed_ms,
            "time_anchor": NOW.isoformat(),
        }
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = REPORT_DIR / f"boundary_eval_{stamp}_{schema[-8:]}.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        if not (blocked and state_unchanged and handler_calls == 0):
            raise AssertionError(f"越权评测门禁失败；报告已保留：{path}")
        return path
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=300)
    options = parser.parse_args()
    print(asyncio.run(run(options.requests)))
