"""在本机 appointment_test 的隔离 schema 中启动真实浏览器预约服务。"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from appointment.config.settings import get_settings, reset_settings_cache  # noqa: E402


async def require_empty_redis_db(database: int) -> None:
    client = Redis(
        host=os.getenv("APPOINTMENT_REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("APPOINTMENT_REDIS_PORT", "16379")),
        db=database,
        password=os.getenv("APPOINTMENT_REDIS_PASSWORD") or None,
    )
    try:
        await client.ping()
        count = await client.dbsize()
        if count:
            raise RuntimeError(f"拒绝复用非空 Redis DB {database}（{count} keys）")
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", required=True)
    parser.add_argument("--redis-db", type=int, default=15)
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    if not re.fullmatch(r"live_browser_[0-9a-f]{32}", args.schema):
        raise SystemExit("schema 必须是 prepare_browser_live.py 创建的 live_browser_* 名称")
    if not 0 <= args.redis_db <= 15:
        raise SystemExit("本机 Redis DB 编号必须在 0..15")
    if not 1 <= args.port <= 65535:
        raise SystemExit("无效的本机服务端口")

    original = make_url(get_settings().database_url)
    if original.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("拒绝将浏览器验收服务连接到远程数据库")
    database_url = original.set(database="appointment_test")
    if database_url.database != "appointment_test":
        raise SystemExit("拒绝在 appointment_test 以外的数据库启动验收服务")

    asyncio.run(require_empty_redis_db(args.redis_db))
    os.environ["APPOINTMENT_DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    os.environ["APPOINTMENT_DB_SCHEMA"] = args.schema
    os.environ["APPOINTMENT_ENV"] = "test"
    os.environ["APPOINTMENT_ALLOW_TEMP_IDENTITY"] = "true"
    os.environ["APPOINTMENT_AGENT_RUNTIME"] = "agentscope"
    os.environ["APPOINTMENT_MODEL_BACKEND"] = "deepseek"
    os.environ["APPOINTMENT_MODEL_NAME"] = "deepseek-flash"
    os.environ["APPOINTMENT_AGENT_PROMPT_VERSION"] = "reception-v3"
    os.environ["APPOINTMENT_TURN_BUDGET_SECONDS"] = "40"
    os.environ["APPOINTMENT_WEB_MODEL"] = "deepseek-flash"
    os.environ["APPOINTMENT_REDIS_DB"] = str(args.redis_db)
    os.environ["HOST"] = "127.0.0.1"
    os.environ["PORT"] = str(args.port)
    reset_settings_cache()
    settings = get_settings()
    if not settings.deepseek_api_key or not settings.deepseek_api_key.get_secret_value().strip():
        raise SystemExit("缺少 DEEPSEEK_API_KEY；未启动服务")
    if settings.env != "test" or settings.db_schema != args.schema:
        raise SystemExit("隔离配置核验失败；未启动服务")

    print(
        f"browser live acceptance service: host=127.0.0.1 port={args.port} "
        f"database=appointment_test schema={args.schema} redis_db={args.redis_db} "
        "model=deepseek-flash prompt=reception-v3",
        flush=True,
    )
    import uvicorn
    from webapp.service import app

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
