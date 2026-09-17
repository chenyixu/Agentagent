"""报价工具与锁价快照（设计稿 §9、数据契约 §2.2）。

``get_service_quote`` 只读已发布价目版本，**不立即写 quote 表**：生成服务端签名
的短期报价数据，绑定 quote_id、客户/门店/服务、价格版本、金额与有效期。
``create_hold`` 或无占位方案发布在短事务中验证该数据及撤销状态，再保存不可变
quote 快照；若相同 quote_id 已存在必须逐字段一致。

咨询展示价格不等于已经锁价；真正锁价从方案事务成功起成立，截止沿用报价有效期。
"""

from __future__ import annotations

import base64
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config.settings import get_settings
from ..core.enums import ErrorCode
from ..core.errors import DomainError
from ..core.hashing import canonical_json
from ..db import models as m
from .catalog import ServiceFacts, load_active_price, load_bookable_store

#: 报价数据有效期。展示用短期凭证，不是资源保证。
QUOTE_TOKEN_TTL_SECONDS = 900


def _secret() -> bytes:
    return get_settings().quote_token_secret.get_secret_value().encode("utf-8")


def sign_quote_payload(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(canonical_json(payload).encode("utf-8")).decode("ascii")
    signature = hmac.new(_secret(), body.encode("ascii"), sha256).hexdigest()
    return f"{body}.{signature}"


def verify_quote_token(token: str) -> dict[str, Any]:
    """校验报价数据完整性。密钥不暴露给模型；失败一律视为无效报价。"""

    import json

    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "报价凭据格式非法") from exc
    expected = hmac.new(_secret(), body.encode("ascii"), sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "报价凭据校验失败")
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except Exception as exc:  # noqa: BLE001 - 任何解码问题都视为非法凭据
        raise DomainError(ErrorCode.VALIDATION_ERROR, "报价凭据内容非法") from exc
    if not isinstance(payload, dict):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "报价凭据内容非法")
    return payload


@dataclass(frozen=True, slots=True)
class QuoteOffer:
    """报价工具返回值。``quote_token`` 是服务端保护的报价数据。"""

    quote_id: UUID
    quote_version: int
    service_version_id: UUID
    price_version_id: UUID
    duration_minutes: int
    amount_minor: int
    currency: str
    currency_exponent: int
    valid_until: datetime
    terms_hash: str
    terms_snapshot: dict[str, Any]
    lock_policy: str
    quote_token: str

    def to_data(self) -> dict[str, Any]:
        return {
            "quote_id": str(self.quote_id),
            "quote_version": self.quote_version,
            "service_version_id": str(self.service_version_id),
            "price_version_id": str(self.price_version_id),
            "duration_minutes": self.duration_minutes,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "currency_exponent": self.currency_exponent,
            "valid_until": self.valid_until.isoformat(),
            "terms_hash": self.terms_hash,
            "terms_snapshot": self.terms_snapshot,
            "lock_policy": self.lock_policy,
            "quote_token": self.quote_token,
        }


async def get_service_quote(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    customer_id: UUID,
    store_id: UUID,
    service: ServiceFacts,
    as_of: datetime,
) -> QuoteOffer:
    # 报价只对可预约的门店签发：停用门店的报价会让用户以为还能下单。
    await load_bookable_store(session, tenant_id=tenant_id, store_id=store_id)
    price = await load_active_price(
        session,
        tenant_id=tenant_id,
        store_id=store_id,
        service_version_id=service.service_version_id,
        as_of=as_of,
    )
    quote_id = uuid4()
    valid_until = as_of + timedelta(seconds=QUOTE_TOKEN_TTL_SECONDS)
    payload = {
        "quote_id": str(quote_id),
        "quote_version": 1,
        "tenant_id": str(tenant_id),
        "customer_id": str(customer_id),
        "store_id": str(store_id),
        "service_version_id": str(service.service_version_id),
        "price_version_id": str(price.price_version_id),
        "duration_minutes": service.duration_minutes,
        "amount_minor": price.amount_minor,
        "currency": price.currency,
        "currency_exponent": price.currency_exponent,
        "terms_hash": service.terms_hash,
        "valid_until": valid_until.isoformat(),
        "lock_policy": "VALID_WINDOW_LOCK",
    }
    return QuoteOffer(
        quote_id=quote_id,
        quote_version=1,
        service_version_id=service.service_version_id,
        price_version_id=price.price_version_id,
        duration_minutes=service.duration_minutes,
        amount_minor=price.amount_minor,
        currency=price.currency,
        currency_exponent=price.currency_exponent,
        valid_until=valid_until,
        terms_hash=service.terms_hash,
        terms_snapshot=price.terms_snapshot,
        lock_policy="VALID_WINDOW_LOCK",
        quote_token=sign_quote_payload(payload),
    )


async def persist_quote_snapshot(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    customer_id: UUID,
    store_id: UUID,
    offer: QuoteOffer,
    now: datetime,
) -> m.Quote:
    """在方案/占位事务中保存不可变 quote 快照。

    相同 quote_id 已存在时必须逐字段一致；否则是幂等冲突而不是静默覆盖。
    """

    payload = verify_quote_token(offer.quote_token)
    if payload.get("quote_id") != str(offer.quote_id):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "报价凭据与报价 ID 不一致")
    if payload.get("customer_id") != str(customer_id):
        raise DomainError(ErrorCode.PERMISSION_DENIED, "报价凭据不属于当前客户")
    if payload.get("tenant_id") != str(tenant_id):
        raise DomainError(ErrorCode.PERMISSION_DENIED, "报价凭据不属于当前租户")
    if datetime.fromisoformat(payload["valid_until"]) <= now:
        raise DomainError(
            ErrorCode.DEPENDENCY_UNAVAILABLE, "报价已过期，请重新获取报价"
        )

    existing = (
        await session.execute(
            select(m.Quote).where(
                m.Quote.tenant_id == tenant_id,
                m.Quote.id == offer.quote_id,
                m.Quote.quote_version == offer.quote_version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.amount_minor != offer.amount_minor
            or existing.terms_hash != offer.terms_hash
            or existing.service_version_id != offer.service_version_id
        ):
            raise DomainError(
                ErrorCode.IDEMPOTENCY_MISMATCH,
                "同一报价 ID 出现不一致内容",
            )
        return existing

    row = m.Quote(
        id=offer.quote_id,
        tenant_id=tenant_id,
        store_id=store_id,
        customer_id=customer_id,
        service_version_id=offer.service_version_id,
        price_version_id=offer.price_version_id,
        quote_version=offer.quote_version,
        duration_minutes=offer.duration_minutes,
        amount_minor=offer.amount_minor,
        currency=offer.currency,
        currency_exponent=offer.currency_exponent,
        terms_snapshot=offer.terms_snapshot,
        terms_hash=offer.terms_hash,
        valid_until=offer.valid_until,
    )
    session.add(row)
    return row
