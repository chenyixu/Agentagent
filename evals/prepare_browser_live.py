"""创建并保留一份浏览器真实服务验收专用 PostgreSQL schema。"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from appointment.config.settings import get_settings
from appointment.db.schema import create_all, verify_exclusion_constraint
from appointment.seed import seed
from webapp.identity import IdentityRegistry


REPORT_DIR = Path(__file__).resolve().parent / "reports"


async def prepare() -> Path:
    configured = make_url(get_settings().database_url)
    if configured.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("浏览器联调只允许连接本机 PostgreSQL")
    url = configured.set(database="appointment_test")
    schema = f"live_browser_{uuid4().hex}"
    if not re.fullmatch(r"live_browser_[0-9a-f]{32}", schema):
        raise RuntimeError("内部 schema 名称校验失败")

    bootstrap = await asyncpg.connect(
        user=url.username,
        password=url.password,
        database="appointment_test",
        host=url.host,
        port=url.port,
    )
    try:
        database_name = await bootstrap.fetchval("SELECT current_database()")
        if database_name != "appointment_test":
            raise RuntimeError("拒绝在 appointment_test 以外的数据库写入测试数据")
        await bootstrap.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await bootstrap.close()

    database_url = url.render_as_string(hide_password=False)
    engine = create_async_engine(
        database_url,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    try:
        await create_all(engine, schema=schema)
        if not await verify_exclusion_constraint(engine):
            raise RuntimeError("隔离 schema 缺少资源区间排他约束")
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        now = datetime.now(timezone.utc)
        async with factory() as session:
            prepared = await seed(session, now=now)
            await session.commit()
            identities = await IdentityRegistry.load(session)
            identities_found = sorted(identities.all(), key=lambda value: value.user_id)
        report = {
            "kind": "browser_live_schema_preparation",
            "prepared_at": now.isoformat(),
            "database": "appointment_test",
            "schema": schema,
            "database_host": url.host,
            "database_port": url.port,
            "seed": {
                "tenant_id": str(prepared.tenant_id),
                "store_id": str(prepared.store_id),
                "customers": len(prepared.customer_ids),
                "therapists": len(prepared.therapist_ids),
                "rooms": len(prepared.room_ids),
                "services": len(prepared.service_ids),
                "login_ids": [item.user_id for item in identities_found],
            },
            "exclusion_constraint_verified": True,
            "cleanup": "none; isolated schema retained for audit",
        }
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"browser_live_prep_{schema}.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(json.dumps({"report_path": str(path), **report}, ensure_ascii=False))
        return path
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(prepare())
