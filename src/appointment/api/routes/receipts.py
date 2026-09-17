"""供应商回调入口。

这是全系统**唯一不需要租户身份**的写入口，也正因如此它是攻击面：

- 身份来自 ``(provider, account_id)`` 路径 + 签名 + 时间窗口，不来自请求头，
  更不来自 payload。回调 payload 自报的 tenant 一律忽略；
- 账户配置决定信任哪些签名密钥、窗口多长、是否支持查单；
- 认不出/验不过的请求**照样入库隔离**（而不是丢弃）：一批过期签名往往就是
  重放尝试，丢掉线索等于放弃发现它。

因此这个路由不挂 ``get_identity``。如果它挂了，就等于要求"供应商先用我们的
身份体系登录"——那是把回调做成前端表单。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...config.settings import Settings
from ...db.session import live_now
from ...worker.receipts import ProviderAccountConfig, ingest_receipt, sandbox_account
from ..deps import get_app_settings, get_session
from ..schemas import ProviderReceiptResponse

router = APIRouter(prefix="/v1/provider-receipts", tags=["receipts"])

SIGNATURE_HEADER = "X-Provider-Signature"


def account_for(settings: Settings, provider: str, account_id: str):
    """按路径解析账户配置。

    首版只有沙箱一个真实账户；接入真实供应商时这里换成密钥管理查询，
    但"由服务端配置决定信任谁"这一点不变。
    """

    if provider == "sandbox" and account_id == "sandbox-account":
        return sandbox_account()
    return None


@router.post("/{provider}/{account_id}", response_model=ProviderReceiptResponse)
async def receive_provider_receipt(
    provider: str,
    account_id: str,
    request: Request,
    x_provider_signature: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> ProviderReceiptResponse:
    """接收通知供应商的回执。

    始终返回 200 并把结论放在 ``action`` 里：回调是**异步通知**，用 4xx 回绝
    只会让供应商反复重投同一条（很多供应商对非 2xx 无差别重试），而我们的
    真实结论是"记下了、隔离了、不做任何状态变更"。需要告警的是 action 的取值，
    不是 HTTP 码。
    """

    account: ProviderAccountConfig | None = account_for(settings, provider, account_id)
    body = await request.json()
    if not isinstance(body, dict):
        body = {"event_type": "malformed", "payload": body}

    result = await ingest_receipt(
        session,
        account=account,
        body=body,
        signature=x_provider_signature,
        now=await live_now(session),
    )
    await session.commit()

    return ProviderReceiptResponse(
        action=result.action,
        receipt_id=None if result.receipt_id is None else str(result.receipt_id),
        delivery_id=None if result.delivery_id is None else str(result.delivery_id),
        status=result.status,
        reason=result.reason,
    )


__all__ = ["SIGNATURE_HEADER", "account_for", "router"]
