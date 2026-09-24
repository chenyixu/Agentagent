"""PostgreSQL 排他约束并发实验；只在 appointment_test 创建新 schema。"""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import platform
import statistics
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from appointment.db.schema import create_all, verify_exclusion_constraint

DATABASE_URL = os.environ.get(
    "APPOINTMENT_DATABASE_URL",
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
)
CONCURRENCIES = (10, 50, 100)
ROUNDS = 20
POOL_SIZE = 12
REPORT_DIR = Path(__file__).resolve().parent / "reports"


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction)))
    return round(ordered[position], 3)


async def run(*, guarded: bool = False) -> Path:
    url = make_url(DATABASE_URL)
    if url.database != "appointment_test" or os.environ.get("APPOINTMENT_ENV", "test") != "test":
        raise RuntimeError("实验只允许连接 test 环境的 appointment_test 数据库")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    schema = "exp_" + uuid4().hex
    connect = {
        "user": url.username,
        "password": url.password,
        "database": url.database,
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
    }
    bootstrap = await asyncpg.connect(**connect)
    try:
        database_version = await bootstrap.fetchval("SELECT version()")
        await bootstrap.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await bootstrap.close()

    engine = create_async_engine(
        DATABASE_URL, connect_args={"server_settings": {"search_path": f"{schema},public"}}
    )
    try:
        await create_all(engine, schema=schema)
        if not await verify_exclusion_constraint(engine):
            raise RuntimeError("隔离 schema 缺少区间排他约束")
    finally:
        await engine.dispose()

    tenant_id, store_id, resource_id = uuid4(), uuid4(), uuid4()
    pool = await asyncpg.create_pool(
        **connect, min_size=1, max_size=POOL_SIZE,
        server_settings={"search_path": f"{schema},public"},
    )
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO tenant (id, name, status) VALUES ($1, $2, 'ACTIVE')",
                    tenant_id, "并发实验租户",
                )
                await connection.execute(
                    "INSERT INTO store (id, tenant_id, name, timezone, status) "
                    "VALUES ($1, $2, $3, 'Asia/Shanghai', 'ACTIVE')",
                    store_id, tenant_id, "并发实验门店",
                )
                await connection.execute(
                    "INSERT INTO resource "
                    "(id, tenant_id, store_id, type, unit_code, display_name, status) "
                    "VALUES ($1, $2, $3, 'therapist', 'T-1', '单资源', 'ACTIVE')",
                    resource_id, tenant_id, store_id,
                )

        async def attempt(start_at: datetime) -> tuple[str, float]:
            begun = time.perf_counter()
            try:
                async with pool.acquire() as connection:
                    async with connection.transaction():
                        if guarded:
                            await connection.fetchval(
                                "SELECT id FROM resource WHERE id = $1 FOR UPDATE",
                                resource_id,
                            )
                        await connection.execute(
                            "INSERT INTO resource_allocation "
                            "(id, tenant_id, store_id, resource_id, appointment_id, "
                            "start_at, end_at, state) "
                            "VALUES ($1, $2, $3, $4, $5, $6, $7, 'BOOKED')",
                            uuid4(), tenant_id, store_id, resource_id, uuid4(),
                            start_at, start_at + timedelta(minutes=60),
                        )
                status = "committed"
            except asyncpg.ExclusionViolationError:
                status = "overlap_rejected"
            except Exception as exc:
                status = f"unexpected:{type(exc).__name__}"
            return status, (time.perf_counter() - begun) * 1000

        base = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        groups = []
        wall_started = time.perf_counter()
        for group_index, concurrency in enumerate(CONCURRENCIES):
            statuses: list[str] = []
            latencies: list[float] = []
            for round_index in range(ROUNDS):
                slot_start = base + timedelta(hours=2 * (group_index * ROUNDS + round_index))
                results = await asyncio.gather(
                    *(attempt(slot_start) for _ in range(concurrency))
                )
                statuses.extend(status for status, _ in results)
                latencies.extend(latency for _, latency in results)
            groups.append({
                "clients_per_round": concurrency,
                "rounds": ROUNDS,
                "attempts": len(statuses),
                "committed": statuses.count("committed"),
                "overlap_rejected": statuses.count("overlap_rejected"),
                "unexpected_count": sum(status.startswith("unexpected:") for status in statuses),
                "unexpected_examples": sorted({
                    status for status in statuses if status.startswith("unexpected:")
                }),
                "latency_ms_p50": percentile(latencies, 0.50),
                "latency_ms_p95": percentile(latencies, 0.95),
                "latency_ms_mean": round(statistics.mean(latencies), 3),
            })

        async with pool.acquire() as connection:
            booked_count = await connection.fetchval(
                "SELECT count(*) FROM resource_allocation WHERE state = 'BOOKED'"
            )
            overlap_pairs = await connection.fetchval(
                "SELECT count(*) FROM resource_allocation a "
                "JOIN resource_allocation b ON a.id < b.id "
                "AND a.tenant_id = b.tenant_id AND a.resource_id = b.resource_id "
                "AND tstzrange(a.start_at, a.end_at, '[)') "
                "&& tstzrange(b.start_at, b.end_at, '[)') "
                "WHERE a.state = 'BOOKED' AND b.state = 'BOOKED'"
            )
        if booked_count != len(CONCURRENCIES) * ROUNDS or overlap_pairs:
            raise AssertionError("数据库最终占用违反实验预期")
        report = {
            "run_id": run_id,
            "kind": "synthetic_database_contention",
            "scope": "单资源、单时段数据库实验；不是完整预约 API 或生产 QPS",
            "mode": "resource_guard_then_insert" if guarded else "direct_constraint_insert",
            "schema": schema,
            "database": url.database,
            "database_version": database_version,
            "python_version": platform.python_version(),
            "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "pool_size": POOL_SIZE,
            "groups": groups,
            "total_attempts": sum(group["attempts"] for group in groups),
            "booked_count": booked_count,
            "overlap_pairs": overlap_pairs,
            "wall_seconds": round(time.perf_counter() - wall_started, 3),
        }
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        destination = REPORT_DIR / f"db_contention_{run_id}.json"
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return destination
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--guarded", action="store_true")
    args = parser.parse_args()
    print(asyncio.run(run(guarded=args.guarded)))
