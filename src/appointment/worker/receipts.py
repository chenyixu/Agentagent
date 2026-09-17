"""供应商回执入口：验签、去重、映射、状态归并、隔离队列重处理。

回执路径上最容易被写错的三件事，这里都做成显式规则：

1. **回调 payload 自报的 tenant 不可信。** 租户只能由
   ``provider_message_binding``（同步响应里建立的映射）反查得到。因此
   ``provider_receipt.tenant_id`` 在绑定前是空的——这是数据契约里明确的
   "租户隔离例外"，未绑定记录只有平台隔离处理权限能看到。
2. **状态归并不是通用的"后到覆盖前到"。** 迟到的 ``ACCEPTED`` 不能把已经确认的
   ``DELIVERED`` 降级；反过来一条可重试失败也不是重发的理由，除非查单明确说
   供应商没有这条消息。所以这里用一张优先级表，而不是 ``if new: overwrite``。
3. **回执可能先于同步响应到达。** 这时还没有映射，唯一正确的做法是持久入隔离
   队列（``QUARANTINED``），等映射建立后重处理；直接丢弃会让"送达"这件事永远
   查无此事。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import DeliveryStatus, ReceiptProcessingStatus
from ..core.ids import new_id
from ..db import models as m
from .providers import SANDBOX_SIGNING_SECRET, verify_receipt_signature

#: 回执事件类型 → 对投递状态的主张。
RECEIPT_EVENT_TYPES = ("accepted", "delivered", "bounced", "complained")

IngestAction = Literal["APPLIED", "IGNORED_STALE", "DUPLICATE", "QUARANTINED", "REJECTED"]


@dataclass(frozen=True, slots=True)
class ProviderAccountConfig:
    """一个供应商账户的信任配置。

    幂等支持/有效窗口、查单支持、回执语义与签名密钥都在这里——它们属于**配置**
    而不是代码分支（设计稿 §11.2）。
    """

    provider: str
    account_id: str
    signing_secret: str
    supports_query: bool = True
    #: 防重放窗口：签名只在这段时间内有效。没有它，一次被截获的回执可以
    #: 在任意久之后重投；``provider_event_id`` 去重挡不住换了新 ID 的重放。
    replay_window_seconds: int = 300


@dataclass(frozen=True, slots=True)
class ReceiptResult:
    action: IngestAction
    receipt_id: UUID | None
    delivery_id: UUID | None
    status: str | None
    reason: str | None = None


def sandbox_account(
    *, account_id: str = "sandbox-account", signing_secret: str | None = None
) -> ProviderAccountConfig:
    return ProviderAccountConfig(
        provider="sandbox",
        account_id=account_id,
        signing_secret=signing_secret or SANDBOX_SIGNING_SECRET,
    )


# ---------------------------------------------------------------------------
# 归并规则
# ---------------------------------------------------------------------------
#: 证据强度。数值越大越"更接近真实送达"。
_EVIDENCE_RANK = {
    DeliveryStatus.PENDING.value: 0,
    DeliveryStatus.SENDING.value: 1,
    DeliveryStatus.ACCEPTED.value: 2,
    DeliveryStatus.DELIVERED.value: 3,
}

#: 这些状态是业务决定或终态，回执不改变它们，只记录冲突。
_FROZEN = {
    DeliveryStatus.SUPERSEDED.value,
    DeliveryStatus.FAILED_FINAL.value,
}


def resolve_status(current: str, evidence: str) -> tuple[str, bool]:
    """按证据强度归并。返回 ``(new_status, is_conflict)``。

    ``is_conflict=True`` 表示回执与当前状态矛盾（例如已判定失败又收到送达），
    这时保留较强的证据但记下冲突：既不能让"送达"被掩盖，也不能悄悄改掉终态而不留痕。
    """

    if evidence == DeliveryStatus.DELIVERED.value:
        if current in _FROZEN:
            return DeliveryStatus.DELIVERED.value, current != DeliveryStatus.DELIVERED.value
        return DeliveryStatus.DELIVERED.value, _EVIDENCE_RANK.get(
            current, 0
        ) > _EVIDENCE_RANK[DeliveryStatus.DELIVERED.value]

    if evidence == DeliveryStatus.ACCEPTED.value:
        if current in _FROZEN:
            # 已终态：晚到的"受理"不改结论，但确实矛盾。
            return current, True
        if current == DeliveryStatus.DELIVERED.value:
            # **核心规则**：晚到的 ACCEPTED 不降级 DELIVERED。
            return current, True
        return DeliveryStatus.ACCEPTED.value, current == DeliveryStatus.FAILED_RETRYABLE.value

    # bounced / complained 等否定证据
    if current == DeliveryStatus.DELIVERED.value:
        return current, True
    if current in _FROZEN:
        return current, False
    return DeliveryStatus.FAILED_FINAL.value, False


def _claim_status(event_type: str) -> str | None:
    if event_type == "delivered":
        return DeliveryStatus.DELIVERED.value
    if event_type == "accepted":
        return DeliveryStatus.ACCEPTED.value
    if event_type in ("bounced", "complained"):
        return DeliveryStatus.FAILED_FINAL.value
    return None


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------
async def ingest_receipt(
    session: AsyncSession,
    *,
    account: ProviderAccountConfig | None,
    body: dict[str, Any],
    signature: str | None,
    now: datetime,
) -> ReceiptResult:
    """接收一条回执。**生产者调用时传入的 ``account`` 来自回调路由**，不来自 payload。"""

    if account is None:
        # 不认识这个账户：来源不明，隔离并告警，绝不写任何租户的数据。
        receipt = await _store_receipt(
            session,
            provider=str(body.get("provider") or "unknown"),
            account_id=str(body.get("account_id") or "unknown"),
            body=body,
            validation_status="UNKNOWN_SOURCE",
            processing_status=ReceiptProcessingStatus.QUARANTINED.value,
            now=now,
        )
        await session.commit()
        return ReceiptResult(
            action="REJECTED",
            receipt_id=receipt.id,
            delivery_id=None,
            status=None,
            reason="UNKNOWN_SOURCE",
        )

    valid = verify_receipt_signature(
        secret=account.signing_secret,
        account_id=account.account_id,
        body=body,
        signature=signature,
    )
    if not valid:
        receipt = await _store_receipt(
            session,
            provider=account.provider,
            account_id=account.account_id,
            body=body,
            validation_status="INVALID_SIGNATURE",
            processing_status=ReceiptProcessingStatus.QUARANTINED.value,
            now=now,
        )
        await session.commit()
        return ReceiptResult(
            action="REJECTED",
            receipt_id=receipt.id,
            delivery_id=None,
            status=None,
            reason="INVALID_SIGNATURE",
        )

    event_id = str(body.get("event_id") or "")
    message_id = body.get("message_id")
    if not event_id:
        receipt = await _store_receipt(
            session,
            provider=account.provider,
            account_id=account.account_id,
            body=body,
            validation_status="MALFORMED",
            processing_status=ReceiptProcessingStatus.QUARANTINED.value,
            now=now,
        )
        await session.commit()
        return ReceiptResult(
            action="REJECTED",
            receipt_id=receipt.id,
            delivery_id=None,
            status=None,
            reason="MALFORMED",
        )

    # 时间窗口：签名有效但"过期太久"的请求视为重放。签名只证明"内容没被改过"，
    # 不证明"这是一次新请求"。
    issued_at = _parse_moment(body.get("issued_at"))
    if issued_at is None:
        receipt = await _store_receipt(
            session,
            provider=account.provider,
            account_id=account.account_id,
            body=body,
            validation_status="MALFORMED",
            processing_status=ReceiptProcessingStatus.QUARANTINED.value,
            now=now,
        )
        await session.commit()
        return ReceiptResult(
            action="REJECTED",
            receipt_id=receipt.id,
            delivery_id=None,
            status=None,
            reason="MISSING_ISSUED_AT",
        )
    skew = abs((now - issued_at).total_seconds())
    if skew > account.replay_window_seconds:
        # 落成 INVALID_SIGNATURE 而不是新增一个枚举值：数据契约把
        # validation_status 固定为四个取值，而"超出时间窗口的签名"在这个契约
        # 里的含义正是"此刻已失效"。具体原因由返回值 reason 与隔离原文区分，
        # 用于告警（一批过期签名往往就是重放尝试）。
        receipt = await _store_receipt(
            session,
            provider=account.provider,
            account_id=account.account_id,
            body=body,
            validation_status="INVALID_SIGNATURE",
            processing_status=ReceiptProcessingStatus.QUARANTINED.value,
            now=now,
        )
        await session.commit()
        return ReceiptResult(
            action="REJECTED",
            receipt_id=receipt.id,
            delivery_id=None,
            status=None,
            reason="REPLAY_WINDOW_EXCEEDED",
        )

    existing = (
        await session.execute(
            select(m.ProviderReceipt).where(
                m.ProviderReceipt.provider == account.provider,
                m.ProviderReceipt.provider_account_id == account.account_id,
                m.ProviderReceipt.provider_event_id == event_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # provider_event_id 唯一去重：同一条供应商事件重复投递不再改状态。
        await session.commit()
        return ReceiptResult(
            action="DUPLICATE",
            receipt_id=existing.id,
            delivery_id=existing.delivery_id,
            status=None,
            reason="DUPLICATE_EVENT",
        )

    binding = await _lookup_binding(
        session,
        provider=account.provider,
        account_id=account.account_id,
        provider_message_id=message_id,
    )
    receipt = await _store_receipt(
        session,
        provider=account.provider,
        account_id=account.account_id,
        body=body,
        validation_status="VALID",
        processing_status=(
            ReceiptProcessingStatus.PENDING.value
            if binding is not None
            else ReceiptProcessingStatus.QUARANTINED.value
        ),
        now=now,
        delivery_id=None if binding is None else binding.delivery_id,
        # 租户只能来自可信绑定，不来自 payload。
        tenant_id=None if binding is None else binding.tenant_id,
    )
    if binding is None:
        await session.commit()
        return ReceiptResult(
            action="QUARANTINED",
            receipt_id=receipt.id,
            delivery_id=None,
            status=None,
            reason="NO_BINDING_YET",
        )

    return await _apply(session, receipt=receipt, now=now)


async def reprocess_quarantined(
    session: AsyncSession, *, limit: int = 50
) -> list[ReceiptResult]:
    """重处理隔离队列。

    同步响应建立映射之后（或另一个账户配置补齐之后），之前隔离的回执就能落地。
    """

    rows = (
        await session.execute(
            select(m.ProviderReceipt)
            .where(
                m.ProviderReceipt.processing_status
                == ReceiptProcessingStatus.QUARANTINED.value,
                m.ProviderReceipt.validation_status == "VALID",
            )
            .order_by(m.ProviderReceipt.received_at)
            .limit(limit)
        )
    ).scalars().all()

    results: list[ReceiptResult] = []
    for receipt in rows:
        binding = await _lookup_binding(
            session,
            provider=receipt.provider,
            account_id=receipt.provider_account_id,
            provider_message_id=receipt.provider_message_id,
        )
        if binding is None:
            continue
        receipt.delivery_id = binding.delivery_id
        receipt.tenant_id = binding.tenant_id
        results.append(
            await _apply(session, receipt=receipt, now=receipt.received_at)
        )
    return results


async def _apply(
    session: AsyncSession, *, receipt: m.ProviderReceipt, now: datetime
) -> ReceiptResult:
    assert receipt.delivery_id is not None and receipt.tenant_id is not None

    delivery = (
        await session.execute(
            select(m.NotificationDelivery)
            .where(
                m.NotificationDelivery.tenant_id == receipt.tenant_id,
                m.NotificationDelivery.id == receipt.delivery_id,
            )
            .with_for_update()
        )
    ).scalar_one()

    claimed = _claim_status(receipt.event_type)
    if claimed is None:
        # 未知事件类型：记录但不改状态。凭猜测来推进状态比不推进更危险。
        receipt.processing_status = ReceiptProcessingStatus.PROCESSED.value
        await session.commit()
        return ReceiptResult(
            action="IGNORED_STALE",
            receipt_id=receipt.id,
            delivery_id=delivery.id,
            status=delivery.status,
            reason="UNKNOWN_EVENT_TYPE",
        )

    # 证据时间顺序：晚到的旧事件（按供应商发生时间）不参与归并。
    prior = dict(delivery.reconcile_result or {})
    last_evidence_at = prior.get("last_evidence_at")
    occurred = receipt.provider_occurred_at
    if occurred is not None and last_evidence_at is not None:
        if occurred.isoformat() < last_evidence_at:
            receipt.processing_status = ReceiptProcessingStatus.PROCESSED.value
            await session.commit()
            return ReceiptResult(
                action="IGNORED_STALE",
                receipt_id=receipt.id,
                delivery_id=delivery.id,
                status=delivery.status,
                reason="EVIDENCE_OUT_OF_ORDER",
            )

    new_status, conflict = resolve_status(delivery.status, claimed)
    delivery.status = new_status
    if new_status == DeliveryStatus.DELIVERED.value:
        delivery.reconcile_status = "RESOLVED"
        delivery.next_reconcile_at = None
    elif new_status == DeliveryStatus.FAILED_FINAL.value:
        delivery.reconcile_status = "GAVE_UP"
        delivery.next_reconcile_at = None
    elif new_status == DeliveryStatus.ACCEPTED.value and delivery.reconcile_status == "GAVE_UP":
        # 受理证据推翻了"放弃"的结论，重新纳入对账。
        delivery.reconcile_status = "PENDING"

    conflicts = list(prior.get("conflicts") or [])
    if conflict:
        conflicts.append(
            {
                "event_type": receipt.event_type,
                "delivery_status": new_status,
                "receipt_id": str(receipt.id),
            }
        )
    delivery.reconcile_result = {
        **prior,
        "last_evidence_at": (
            occurred.isoformat() if occurred is not None else now.isoformat()
        ),
        "last_receipt_event_type": receipt.event_type,
        "conflicts": conflicts,
    }
    receipt.processing_status = ReceiptProcessingStatus.PROCESSED.value
    await session.commit()
    return ReceiptResult(
        action="APPLIED",
        receipt_id=receipt.id,
        delivery_id=delivery.id,
        status=new_status,
        reason="CONFLICT" if conflict else None,
    )


async def _lookup_binding(
    session: AsyncSession, *, provider: str, account_id: str, provider_message_id: str | None
) -> m.ProviderMessageBinding | None:
    if not provider_message_id:
        return None
    return (
        await session.execute(
            select(m.ProviderMessageBinding).where(
                m.ProviderMessageBinding.provider == provider,
                m.ProviderMessageBinding.provider_account_id == account_id,
                m.ProviderMessageBinding.provider_message_id == provider_message_id,
            )
        )
    ).scalar_one_or_none()


async def _store_receipt(
    session: AsyncSession,
    *,
    provider: str,
    account_id: str,
    body: dict[str, Any],
    validation_status: str,
    processing_status: str,
    now: datetime,
    delivery_id: UUID | None = None,
    tenant_id: UUID | None = None,
) -> m.ProviderReceipt:
    receipt = m.ProviderReceipt(
        id=new_id(),
        provider=provider,
        provider_account_id=account_id,
        provider_event_id=str(body.get("event_id") or f"unknown-{new_id()}"),
        provider_message_id=body.get("message_id"),
        delivery_id=delivery_id,
        tenant_id=tenant_id,
        event_type=str(body.get("event_type") or "unknown"),
        provider_occurred_at=_parse_moment(body.get("occurred_at")),
        received_at=now,
        validation_status=validation_status,
        processing_status=processing_status,
        # 回执原文受保护：只落库给隔离处理权限看，日志里只留 ID。
        protected_raw=body,
    )
    session.add(receipt)
    try:
        await session.flush()
    except IntegrityError:
        # 并发重复投递：唯一约束已经挡住了，取回原记录即可。
        await session.rollback()
        existing = (
            await session.execute(
                select(m.ProviderReceipt).where(
                    m.ProviderReceipt.provider == provider,
                    m.ProviderReceipt.provider_account_id == account_id,
                    m.ProviderReceipt.provider_event_id == receipt.provider_event_id,
                )
            )
        ).scalar_one()
        return existing
    return receipt


def _parse_moment(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    from datetime import timezone

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


__all__ = [
    "IngestAction",
    "ProviderAccountConfig",
    "RECEIPT_EVENT_TYPES",
    "ReceiptResult",
    "ingest_receipt",
    "reprocess_quarantined",
    "resolve_status",
    "sandbox_account",
]
