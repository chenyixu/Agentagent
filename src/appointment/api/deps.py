"""FastAPI 依赖：会话、设置、身份、运行时。

事务边界刻意交给端点显式控制：``get_session`` 只负责"出错回滚、结束关闭"，
不替端点决定何时 commit。这样"业务效果与它的事件在同一事务内提交"是代码里
看得见的一行，而不是依赖框架的隐式时机。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent import build_runtime
from ..config.settings import Settings, get_settings
from ..db.session import get_sessionmaker
from ..domain.context import TrustedContext
from .identity import resolve_identity


def get_app_settings() -> Settings:
    return get_settings()


def get_runtime(request: Request) -> Any:
    """运行时实例由应用生命周期创建一次，请求之间共享（实现必须无跨请求状态）。"""

    return request.app.state.runtime


async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_identity(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> TrustedContext:
    """从请求头解析可信身份。

    只读请求头：请求体里的任何身份字段都会被 Schema 拒绝（``extra="forbid"``）。
    """

    return resolve_identity(
        dict(request.headers), allow_temp_identity=settings.allow_temp_identity
    )


def build_runtime_for_settings(settings: Settings) -> Any:
    """按配置构造运行时。

    默认确定性基线：它不依赖模型与网络，因此在任何环境都能跑通主链路。
    """

    runtime = build_runtime(
        settings.agent_runtime,
        role="reception",
        model=None,
    )
    return runtime


__all__ = [
    "build_runtime_for_settings",
    "get_app_settings",
    "get_identity",
    "get_runtime",
    "get_session",
]
