"""测试夹具。

设计稿 §13.1：时间测试用可注入的时钟与数据库时间边界验证结合，避免只用 sleep
造成脆弱测试。这里的 ``FrozenClock`` 固定一个参考时刻，配合真实的 PostgreSQL
做事务与并发验证。

每个数据库用例使用独立的 PostgreSQL schema，保留证据且不清空现有表。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

# 必须在导入 appointment 之前设置：Settings 有 lru_cache。
os.environ.setdefault(
    "APPOINTMENT_DATABASE_URL",
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
)
os.environ.setdefault("APPOINTMENT_ENV", "test")
os.environ.setdefault("APPOINTMENT_MODEL_BACKEND", "stub")
os.environ.setdefault("APPOINTMENT_AGENT_RUNTIME", "deterministic")
os.environ.setdefault("APPOINTMENT_NOTIFICATION_PROVIDER", "sandbox")
os.environ.setdefault("APPOINTMENT_ALLOW_TEMP_IDENTITY", "true")

from typing import AsyncIterator  # noqa: E402
from uuid import UUID  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from appointment.core.clock import FrozenClock  # noqa: E402
from appointment.db.schema import create_all, verify_exclusion_constraint  # noqa: E402
from appointment.db import session as db_session  # noqa: E402
from appointment.domain.context import TrustedContext  # noqa: E402
from appointment.seed import SeedResult, seed  # noqa: E402

#: 固定参考时刻：2026-09-17 13:00 Asia/Shanghai。
REFERENCE_NOW = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """每个用例只在新 schema 建表；不触及已有业务数据。"""

    from appointment.config.settings import get_settings

    settings = get_settings()
    url = make_url(settings.database_url)
    if settings.env != "test" or url.database != "appointment_test":
        raise RuntimeError("集成测试仅允许使用 test 环境的 appointment_test 数据库")
    schema = f"case_{uuid4().hex}"
    assert re.fullmatch(r"case_[0-9a-f]{32}", schema)
    bootstrap = create_async_engine(settings.database_url)
    try:
        async with bootstrap.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await bootstrap.dispose()
    eng = create_async_engine(
        settings.database_url,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    previous_engine = db_session._engine
    previous_factory = db_session._sessionmaker
    db_session._engine = eng
    db_session._sessionmaker = async_sessionmaker(
        eng, expire_on_commit=False, autoflush=False
    )
    webapp_service = sys.modules.get("webapp.service")
    previous_webapp_factory = getattr(webapp_service, "_SESSION_FACTORY", None)
    if webapp_service is not None:
        webapp_service._SESSION_FACTORY = db_session._sessionmaker
    await create_all(eng, schema=schema)
    assert await verify_exclusion_constraint(eng), (
        "resource_allocation 缺少区间排他约束：核心不变量无法保证"
    )
    try:
        yield eng
    finally:
        if webapp_service is not None:
            webapp_service._SESSION_FACTORY = previous_webapp_factory
        db_session._engine = previous_engine
        db_session._sessionmaker = previous_factory
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每个测试拿到自己的新 schema；提交仅留在该 schema 中。"""

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as sess:
        yield sess


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(REFERENCE_NOW)


@pytest.fixture
def settings():
    """应用配置（含 Worker/通知参数）。

    测试需要改动单个参数时用 ``settings.model_copy(update={...})``，
    不要 ``reset_settings_cache`` 再改环境变量——那会污染同进程里的其他测试。
    """

    from appointment.config.settings import get_settings

    return get_settings()


@pytest_asyncio.fixture
async def seeded(session: AsyncSession, clock: FrozenClock, request) -> SeedResult:
    options = getattr(request, "param", {})
    result = await seed(session, now=clock.now(), **options)
    await session.commit()
    return result


def customer_ctx(
    seed: SeedResult, *, index: int = 0, request_id: str = "req-test"
) -> TrustedContext:
    """客户身份的可信上下文。tenant_id/actor_id 由服务端注入。"""

    return TrustedContext(
        tenant_id=seed.tenant_id,
        actor_id=seed.customer_actor_ids[index],
        role="customer",
        request_id=request_id,
        release_id="release-local-1",
        customer_id=seed.customer_ids[index],
    )


def manager_ctx(seed: SeedResult, *, request_id: str = "req-manager") -> TrustedContext:
    return TrustedContext(
        tenant_id=seed.tenant_id,
        actor_id=seed.manager_actor_id,
        role="store_manager",
        request_id=request_id,
        release_id="release-local-1",
        store_scopes=(seed.store_id,),
    )


@pytest.fixture
def tomorrow_window(clock: FrozenClock):
    """明天的 15:00–18:00（门店本地时间），用于构造确定的查询窗口。"""

    from appointment.domain.timeutil import load_zone, resolve_local_datetime
    from appointment.seed import STORE_TIMEZONE
    from datetime import date, time

    tz = load_zone(STORE_TIMEZONE)
    base = clock.now().astimezone(tz).date() + timedelta(days=1)
    start = resolve_local_datetime(datetime.combine(base, time(15, 0)), tz)
    end = resolve_local_datetime(datetime.combine(base, time(18, 0)), tz)
    return start, end, base


def as_uuid(value: str) -> UUID:
    return UUID(value)
