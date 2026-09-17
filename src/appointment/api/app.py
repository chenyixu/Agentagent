"""FastAPI 应用工厂。

设计稿 §4.1 选 B：FastAPI 业务入口嵌入 SDK，而不是启动完整 Agent Service。
因此这里就是一个普通的模块化单体入口：请求进来 → 解析身份 → 编排器推进任务 →
同一事务提交业务效果与事件。

启动时的事实核查：

- ``create_all`` 会校验 ``resource_allocation`` 的区间排他约束存在。缺少它时
  "不超卖"就无法保证，宁可直接启动失败，也不要带病运行。
- 运行时（确定性基线 / AgentScope）只创建一次，实现必须无跨请求状态。

已知边界（刻意写在代码里而不是含糊过去）：本里程碑的模型运行时不发起真实
LLM 调用。接入真实模型前必须把"调用模型"与"打开业务事务"拆开——设计稿 §5.1
明确要求调用模型时不持有事务或连接；当前实现只在运行时无外部 I/O 时成立。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config.settings import Settings, get_settings
from ..core.errors import SCHEMA_VERSION
from ..db.schema import create_all, verify_exclusion_constraint
from ..db.session import dispose_engine, get_engine
from .deps import build_runtime_for_settings
from .errors import install_error_handlers
from .routes import confirmations, messages, operations, receipts, tasks

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    *,
    settings: Settings | None = None,
    verify_schema: bool = True,
) -> FastAPI:
    """构造应用。

    ``verify_schema=False`` 供已经迁移好的环境使用（跳过 DDL 与约束检查）。
    """

    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = resolved
        app.state.runtime = build_runtime_for_settings(resolved)
        if verify_schema:
            engine = get_engine()
            await create_all(engine)
            if not await verify_exclusion_constraint(engine):
                raise RuntimeError(
                    "resource_allocation 缺少区间排他约束：核心不变量无法保证"
                )
        yield
        await dispose_engine()

    app = FastAPI(
        title="智能预约 Agent 系统",
        version="0.1.0",
        summary="受约束的 Agentic Workflow + 确定性预约领域核心",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(messages.router)
    app.include_router(tasks.router)
    app.include_router(confirmations.router)
    app.include_router(operations.router)
    # 回调入口是唯一不带租户身份的路由（身份来自账户配置 + 签名 + 时间窗口）。
    app.include_router(receipts.router)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict:
        from datetime import datetime, timezone

        return {
            "status": "ok",
            "runtime": app.state.runtime.runtime_name,
            "env": resolved.env,
            "schema_version": SCHEMA_VERSION,
            "now": datetime.now(timezone.utc).isoformat(),
        }

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


__all__ = ["STATIC_DIR", "create_app"]
