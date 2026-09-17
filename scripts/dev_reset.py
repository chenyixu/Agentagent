"""开发库重置 + 种子数据装载（人工测试入口）。

设计稿 §13.1 要求场景夹具确定、可复现：这里把测试套件里的 ``seed()`` 暴露成
命令行入口，人工测试不再需要"手写一段脚本调 seed"。

用法::

    .venv/bin/python scripts/dev_reset.py            # 清空后重新灌入种子
    .venv/bin/python scripts/dev_reset.py --keep     # 保留已有数据，只补种子

**参考时刻取真实当前时间**（不是测试里的 FrozenClock）：人工测试要说"明天下午"
这类相对时间，夹具必须挂在真实日历上才符合直觉。

脚本会把身份头直接打印出来，复制即可发起请求。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 必须在导入 appointment 之前设定：Settings 有 lru_cache。
os.environ.setdefault(
    "APPOINTMENT_DATABASE_URL",
    "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment",
)
os.environ.setdefault("APPOINTMENT_ENV", "local_dev")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from appointment.db.schema import create_all, verify_exclusion_constraint  # noqa: E402
from appointment.db.session import dispose_engine, get_engine  # noqa: E402
from appointment.seed import STORE_TIMEZONE, SeedResult, seed  # noqa: E402


async def truncate_all(session) -> list[str]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename NOT LIKE 'spatial_%'"
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    quoted = ", ".join(f'"{name}"' for name in rows)
    await session.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
    await session.commit()
    return list(rows)


def _print_identity(result: SeedResult) -> None:
    print("\n=== 身份头（复制到请求里） ===")
    print(f"X-Tenant-Id:    {result.tenant_id}")
    print(f"X-Store-Scope:  {result.store_id}")
    print("\n-- 客户 0：林女士 --")
    print(f"X-Actor-Id:     {result.customer_actor_ids[0]}")
    print(f"X-Customer-Id:  {result.customer_ids[0]}")
    print("X-Role:         customer")
    print("\n-- 客户 1：赵先生 --")
    print(f"X-Actor-Id:     {result.customer_actor_ids[1]}")
    print(f"X-Customer-Id:  {result.customer_ids[1]}")
    print("X-Role:         customer")
    print("\n-- 店长 / 员工 --")
    print(f"X-Actor-Id:     {result.manager_actor_id}   (X-Role: store_manager)")
    print(f"X-Actor-Id:     {result.staff_actor_id}     (X-Role: staff)")
    print("\n=== 关键业务标识 ===")
    print(f"store_id:       {result.store_id}")
    for name, sid in result.service_ids.items():
        print(f"service[{name}]: {sid}")
    print(f"resources:      {', '.join(str(r) for r in result.resource_ids)}")
    print(f"price(minor):   {result.price_amount_minor}")

    from appointment.domain.timeutil import load_zone

    tz = load_zone(STORE_TIMEZONE)
    today = datetime.now(timezone.utc).astimezone(tz).date()
    tomorrow = today.fromordinal(today.toordinal() + 1)
    print(
        f"\n门店时区: {STORE_TIMEZONE}；今天(门店本地)={today.isoformat()}，"
        f"明天={tomorrow.isoformat()}"
    )
    print("营业时间：每天 10:00–22:00")


def _purge_appointment_sessions() -> int:
    """清掉 Redis 里属于本演示的 AgentScope 会话/事件/消息键。

    业务库一重置，``seed()`` 就会生成新的客户 UUID，而 AgentScope 会话记录的
    (tenant_id, customer_id) 是**不可变的**——旧会话再也无法通过框架的作用域检查。
    留着它们不会造成数据泄露（框架会拒绝），但会让人对着 403 猜，所以默认一起清掉。

    只删名字里带 ``appointment`` 的键：同一个 Redis 里可能还跑着别的演示，误删别人
    的会话比留下几条垃圾键糟糕得多。
    """

    import redis as redis_lib

    client = redis_lib.Redis(
        host=os.getenv("APPOINTMENT_REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("APPOINTMENT_REDIS_PORT", "16379")),
        db=int(os.getenv("APPOINTMENT_REDIS_DB", "0")),
        password=os.getenv("APPOINTMENT_REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    try:
        keys = list(client.scan_iter(match="*appointment*"))
        if keys:
            client.delete(*keys)
        return len(keys)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"!! 清理 Redis 失败（不影响业务库）：{exc}", file=sys.stderr)
        return 0
    finally:
        client.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="重置并装载开发库种子数据")
    parser.add_argument("--keep", action="store_true", help="保留已有数据，只补种子")
    parser.add_argument(
        "--skip-redis",
        action="store_true",
        help="不清 Redis 里本演示的 AgentScope 会话（默认会清）",
    )
    args = parser.parse_args()

    engine = get_engine()
    await create_all(engine)
    if not await verify_exclusion_constraint(engine):
        print("!! resource_allocation 缺少区间排他约束，拒绝继续", file=sys.stderr)
        return 2

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        if not args.keep:
            names = await truncate_all(session)
            print(f"已清空 {len(names)} 张表")
        result = await seed(session, now=datetime.now(timezone.utc))
        await session.commit()

    await dispose_engine()
    _print_identity(result)

    if not args.skip_redis:
        removed = _purge_appointment_sessions()
        print(f"\n已清理 {removed} 个 AgentScope 会话键（--skip-redis 可跳过）")

    print(
        "完成。启动浏览器侧服务："
        ".venv/bin/python -m webapp.service  （默认 http://127.0.0.1:8010）"
    )
    print(
        "机器客户端接口："
        ".venv/bin/python -m uvicorn appointment.api.app:create_app --factory --port 8000"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
