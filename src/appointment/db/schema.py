"""建库脚本。

扩展依赖（设计稿 §5.1、§8.1）：

- ``btree_gist``：让 GiST 排他约束能对 UUID 做等值比较，这是"同租户同资源
  不得重叠"约束的前提。
- ``pg_trgm``：中文关键词检索的辅助相似度（P0 主要走应用层分词）。

迁移策略：首个 Alembic 修订直接基于 ``Base.metadata`` 建表，避免模型与迁移
两处维护；后续修订使用显式 ``op`` 操作。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .base import Base

REQUIRED_EXTENSIONS = ("btree_gist", "pg_trgm")


async def create_extensions(engine: AsyncEngine) -> list[str]:
    """创建所需扩展，返回本次实际启用的扩展名。"""

    enabled: list[str] = []
    async with engine.begin() as conn:
        for ext in REQUIRED_EXTENSIONS:
            available = (
                await conn.execute(
                    text("SELECT 1 FROM pg_available_extensions WHERE name = :name"),
                    {"name": ext},
                )
            ).scalar_one_or_none()
            if available is None:
                raise RuntimeError(
                    f"数据库缺少扩展 {ext}；核心不变量依赖区间排他约束，"
                    "请先安装对应 contrib 包"
                )
            await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{ext}"'))
            enabled.append(ext)
    return enabled


async def verify_exclusion_constraint(engine: AsyncEngine) -> bool:
    """确认排他约束确实存在。

    这是安全不变量而非可选优化：缺少它时"不超卖"只剩应用层读后检查，
    无法覆盖并发写入。
    """

    async with engine.connect() as conn:
        found = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'ck_resource_allocation_no_overlap' "
                    "   OR conname = 'ex_resource_allocation_no_overlap' "
                    "   OR (conrelid = 'resource_allocation'::regclass "
                    "       AND contype = 'x')"
                )
            )
        ).first()
        return found is not None


async def create_all(engine: AsyncEngine) -> None:
    await create_extensions(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
