"""测试夹具。

设计稿 §13.1：时间测试用可注入的时钟与数据库时间边界验证结合，避免只用 sleep
造成脆弱测试。这里的 ``FrozenClock`` 固定一个参考时刻，配合真实的 PostgreSQL
做事务与并发验证。

测试库通过环境变量指向独立的 ``appointment_test``，避免污染开发库。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from appointment.core.clock import FrozenClock  # noqa: E402
from appointment.db.schema import create_all, verify_exclusion_constraint  # noqa: E402
from appointment.db.session import dispose_engine, get_engine  # noqa: E402
from appointment.domain.context import TrustedContext  # noqa: E402
from appointment.seed import SeedResult, seed  # noqa: E402

#: 固定参考时刻：2026-09-17 13:00 Asia/Shanghai。
REFERENCE_NOW = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = get_engine()
    await create_all(eng)
    assert await verify_exclusion_constraint(eng), (
        "resource_allocation 缺少区间排他约束：核心不变量无法保证"
    )
    yield eng
    await dispose_engine()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每个测试拿到干净的库。

    先 TRUNCATE 全部业务表，再交给测试；测试结束后不提交残留数据。
    """

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as sess:
        await _truncate_all(sess)
        yield sess


async def _truncate_all(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename NOT LIKE 'spatial_%'"
            )
        )
    ).scalars().all()
    if not rows:
        return
    quoted = ", ".join(f'"{name}"' for name in rows)
    await session.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
    await session.commit()


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
async def seeded(session: AsyncSession, clock: FrozenClock) -> SeedResult:
    result = await seed(session, now=clock.now())
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
