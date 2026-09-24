"""把预约机器 API 安全地接到浏览器演示服务。

浏览器只发送演示登录名 ``X-User-ID``。可信租户、顾客、员工和门店范围均从
服务端 ``IdentityRegistry`` 派生；预约写入仍走原有 ``/v1`` 路由、领域事务和
专用确认凭据，不通过 AgentScope 模型工具执行。
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

from appointment.agent.ports import AgentRuntimePort
from appointment.api.deps import (
    build_runtime_for_settings,
    get_identity,
    get_runtime,
)
from appointment.api.routes import confirmations, messages, operations, tasks
from appointment.config.settings import get_settings
from appointment.domain.context import TrustedContext

from .identity import UnknownIdentityError

router = APIRouter(prefix="/v1", tags=["browser-booking"])


async def get_browser_booking_identity(request: Request) -> TrustedContext:
    """将浏览器演示登录名映射成服务端可信身份；不读取请求体身份字段。"""

    # The lifespan publishes the registry on app.state. The local import fallback keeps
    # imports cycle-free for small ASGI mounts and preserves the dev service contract.
    registry = getattr(request.app.state, "appointment_identity_registry", None)
    if registry is None:
        from .service import loaded_registry

        registry = loaded_registry()

    user_id = (request.headers.get("x-user-id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="缺少浏览器登录身份")
    try:
        identity = registry.get(user_id)
    except UnknownIdentityError as exc:
        raise HTTPException(status_code=401, detail="未知浏览器登录身份") from exc
    if identity.role != "customer" or identity.customer_id is None:
        raise HTTPException(status_code=403, detail="该身份不能使用顾客预约页面")

    return TrustedContext(
        tenant_id=identity.tenant_id,
        actor_id=identity.actor_id,
        role=identity.role,
        request_id=f"browser-{uuid4().hex}",
        release_id="release-local-1",
        customer_id=identity.customer_id,
        store_scopes=(identity.store_id,),
    )


def get_browser_booking_runtime(request: Request) -> AgentRuntimePort:
    """为浏览器预约 API 延迟创建进程级运行时。"""

    runtime = getattr(request.app.state, "appointment_booking_runtime", None)
    if runtime is None:
        runtime = build_runtime_for_settings(get_settings())
        request.app.state.appointment_booking_runtime = runtime
    return runtime


@router.get("/context")
async def booking_context(
    ctx: TrustedContext = Depends(get_browser_booking_identity),
) -> dict[str, str]:
    """提供当前演示身份的门店上下文；所有写入仍由服务端复核范围。"""

    store_id = ctx.store_scopes[0]
    return {"store_id": str(store_id)}


def install_booking_api(app: FastAPI) -> None:
    """在 AgentScope 浏览器服务上挂载业务 API 的授权作用域路由。"""

    app.dependency_overrides[get_identity] = get_browser_booking_identity
    app.dependency_overrides[get_runtime] = get_browser_booking_runtime
    app.include_router(router, prefix="/booking")
    for subrouter in (messages.router, tasks.router, confirmations.router, operations.router):
        app.include_router(subrouter, prefix="/booking")


__all__ = [
    "get_browser_booking_identity",
    "get_browser_booking_runtime",
    "install_booking_api",
]
