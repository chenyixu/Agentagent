"""通知投递账本：逻辑投递、尝试记录、状态归并、对账。

三层职责刻意分开（设计稿 §11.2）：

    outbox  →  job  →  notification_delivery  →  delivery_attempt
    事件落地   调度    一次逻辑投递              每次真实尝试

**Outbox 被消费只代表逻辑投递已持久受理，不代表用户收到通知。** 这句话在这里
不是注释而是代码结构：``consume_outbox`` 只建投递与调度，状态停在 ``PENDING``；
真正发信是 job 的第二步，且永远停在 ``ACCEPTED``，直到可信回执把它抬到
``DELIVERED``。

三条实现纪律：

1. **调用供应商之前先落 attempt。** 否则"发出去了但进程被杀"会变成查无此事的
   空白，对账无从下手。
2. **调用供应商时不开事务。** 外部调用与数据库事务不能重叠（设计稿 §7）——把
   网络等待放进事务会拖住连接池并把锁持有时间变成不可控的网络时间。因此发送是
   「准备并提交 → 事务外调用 → 回来记账」。
3. **内容变化是新的逻辑投递，不是改旧的那条。** 逻辑投递唯一键含
   ``template_version``，更正消息用修订后的模板版本建新行并指向被替代的投递。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import DeliveryStatus, ReconcileStatus
from ..core.hashing import content_hash
from ..core.ids import new_id
from ..db import models as m
from .providers import (
    NotificationProviderPort,
    ProviderTimeout,
    SendOutcome,
    SendRequest,
)

#: 对账退避：首次查单前等一小段，之后按倍数放大到上限。
RECONCILE_BASE_DELAY_SECONDS = 30
RECONCILE_MAX_DELAY_SECONDS = 300

#: 供应商受理后如果渠道有查单能力，多久去确认一次送达证据。
DELIVERED_CONFIRM_DELAY_SECONDS = 60

Action = Literal[
    "SENT",              # 本次真的调用了供应商
    "REPLAYED",          # 供应商侧幂等重放，未产生新消息
    "SKIPPED",           # 状态已终态，无需再动
    "SUPERSEDED",        # 发送前发现订单版本已变，跳过并建更正投递
    "RECONCILED",        # 本次是对账而不是发送
    "ESCALATED",         # 对账超期，转人工
    "FAILED",            # 达到最大尝试次数
]


@dataclass(frozen=True, slots=True)
class DeliveryAttemptResult:
    delivery_id: UUID
    action: Action
    status: str
    attempt_number: int | None = None
    provider_message_id: str | None = None
    detail: dict[str, Any] | None = None
    #: 需要调度器重排的时间点。None 表示不需要再来一次。
    retry_at: datetime | None = None
    correction_delivery_id: UUID | None = None


# ---------------------------------------------------------------------------
# 逻辑投递的创建
# ---------------------------------------------------------------------------
def provider_idempotency_key(
    *,
    tenant_id: UUID,
    source_event_id: UUID,
    channel: str,
    recipient_ref: str,
    template_version: str,
) -> str:
    """跨重试稳定的供应商幂等键。

    含租户：``UNIQUE(provider, provider_account_id, provider_idempotency_key)``
    的作用域里没有租户，两个租户若用同一事件 ID 会撞在一起。
    """

    return (
        f"{tenant_id}:{source_event_id}:{channel}:{recipient_ref}:{template_version}"
    )


def corrected_template_version(base: str, revision: int) -> str:
    """更正消息的模板版本。

    内容变化必须换逻辑投递（唯一键含 ``template_version``），所以更正是在模板
    版本上做修订，而不是覆盖旧行——旧行的存在本身就是"当时发过什么"的证据。
    """

    return f"{base}+rev{revision}"


async def enqueue_delivery(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    source_event_id: UUID,
    appointment_id: UUID,
    appointment_version: int,
    channel: str,
    recipient_ref: str,
    template_version: str,
    provider: str,
    provider_account_id: str,
    payload: dict[str, Any],
    supersedes_delivery_id: UUID | None = None,
) -> tuple[m.NotificationDelivery, bool]:
    """建立（或取回）一条逻辑投递。返回 ``(行, 是否新建)``。

    幂等键在这里而不是在调用方：唯一键就是 ``(tenant, source_event, channel,
    recipient_ref, template_version)``，把它抄到调用方去判重，迟早会有一处抄错。
    """

    existing = (
        await session.execute(
            select(m.NotificationDelivery).where(
                m.NotificationDelivery.tenant_id == tenant_id,
                m.NotificationDelivery.source_event_id == source_event_id,
                m.NotificationDelivery.channel == channel,
                m.NotificationDelivery.recipient_ref == recipient_ref,
                m.NotificationDelivery.template_version == template_version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    key = provider_idempotency_key(
        tenant_id=tenant_id,
        source_event_id=source_event_id,
        channel=channel,
        recipient_ref=recipient_ref,
        template_version=template_version,
    )
    row = m.NotificationDelivery(
        id=new_id(),
        tenant_id=tenant_id,
        source_event_id=source_event_id,
        appointment_id=appointment_id,
        appointment_version=appointment_version,
        channel=channel,
        recipient_ref=recipient_ref,
        template_version=template_version,
        provider=provider,
        provider_account_id=provider_account_id,
        provider_idempotency_key=key,
        payload_hash=content_hash(payload),
        payload=payload,
        status=DeliveryStatus.PENDING.value,
        reconcile_status=ReconcileStatus.NOT_REQUIRED.value,
        supersedes_delivery_id=supersedes_delivery_id,
    )
    # 用 SAVEPOINT 包住插入：并发消费者可能同时建同一条逻辑投递，
    # 撞唯一约束时回滚到保存点再取回，而不是让整个事务失败。
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = (
            await session.execute(
                select(m.NotificationDelivery).where(
                    m.NotificationDelivery.tenant_id == tenant_id,
                    m.NotificationDelivery.source_event_id == source_event_id,
                    m.NotificationDelivery.channel == channel,
                    m.NotificationDelivery.recipient_ref == recipient_ref,
                    m.NotificationDelivery.template_version == template_version,
                )
            )
        ).scalar_one()
        return existing, False
    return row, True


async def _attempt_count(session: AsyncSession, delivery_id: UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(m.DeliveryAttempt)
                .where(m.DeliveryAttempt.delivery_id == delivery_id)
            )
        ).scalar()
        or 0
    )


async def _appointment_version(
    session: AsyncSession, *, tenant_id: UUID, appointment_id: UUID
) -> int | None:
    return (
        await session.execute(
            select(m.Appointment.version).where(
                m.Appointment.tenant_id == tenant_id,
                m.Appointment.id == appointment_id,
            )
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 发送：准备 → 事务外调用 → 记账
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Prepared:
    request: SendRequest
    attempt_number: int


async def attempt_delivery(
    session_factory,
    provider: NotificationProviderPort,
    *,
    tenant_id: UUID,
    delivery_id: UUID,
    now: datetime,
    max_attempts: int,
    reconcile_deadline_seconds: int,
    retry_delay_seconds: int = 30,
) -> DeliveryAttemptResult:
    """尝试投递一条逻辑投递。可被重复调用（幂等）。

    ``session_factory`` 而不是 ``session``：这个函数必须跨越三个事务边界，
    拿着一个外部传入的 session 会强迫调用方把外部调用包进事务里。
    """

    # ---- 阶段一：在自己的事务里决定"该做什么"，并把意图持久化 ----
    async with session_factory() as session:
        delivery = (
            await session.execute(
                select(m.NotificationDelivery)
                .where(
                    m.NotificationDelivery.tenant_id == tenant_id,
                    m.NotificationDelivery.id == delivery_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if delivery is None:
            return DeliveryAttemptResult(
                delivery_id=delivery_id,
                action="SKIPPED",
                status="MISSING",
                detail={"reason": "NOT_FOUND"},
            )

        terminal = (
            DeliveryStatus.ACCEPTED.value,
            DeliveryStatus.DELIVERED.value,
            DeliveryStatus.SUPERSEDED.value,
            DeliveryStatus.FAILED_FINAL.value,
        )
        if delivery.status in terminal:
            # 已经受理过就不要再发一次。ACCEPTED 之后的推进属于对账的职责。
            return DeliveryAttemptResult(
                delivery_id=delivery_id,
                action="SKIPPED",
                status=delivery.status,
                provider_message_id=None,
            )

        current_version = await _appointment_version(
            session, tenant_id=tenant_id, appointment_id=delivery.appointment_id
        )
        if (
            delivery.status in (DeliveryStatus.PENDING.value,)
            and current_version is not None
            and current_version != delivery.appointment_version
        ):
            # 发送前再读一次当前版本：改约后的"明天下午三点提醒"不能照旧发出去。
            # 这里只处理"尚未发出"的投递；已发出的只能靠更正消息补，无法撤回。
            delivery.status = DeliveryStatus.SUPERSEDED.value
            correction, _ = await enqueue_delivery(
                session,
                tenant_id=tenant_id,
                source_event_id=delivery.source_event_id,
                appointment_id=delivery.appointment_id,
                appointment_version=current_version,
                channel=delivery.channel,
                recipient_ref=delivery.recipient_ref,
                template_version=corrected_template_version(
                    delivery.template_version, current_version
                ),
                provider=delivery.provider,
                provider_account_id=delivery.provider_account_id,
                payload={**delivery.payload, "corrected": True,
                         "appointment_version": current_version},
                supersedes_delivery_id=delivery.id,
            )
            correction_id = correction.id
            await session.commit()
            return DeliveryAttemptResult(
                delivery_id=delivery_id,
                action="SUPERSEDED",
                status=DeliveryStatus.SUPERSEDED.value,
                detail={"appointment_version": current_version},
                correction_delivery_id=correction_id,
            )

        if delivery.status == DeliveryStatus.UNKNOWN.value:
            # 上一次调用结果不明：**不重发**，先查证（由调用方在事务外查）。
            return DeliveryAttemptResult(
                delivery_id=delivery_id,
                action="RECONCILED",
                status=delivery.status,
                detail={"reason": "NEEDS_QUERY"},
                retry_at=delivery.next_reconcile_at,
            )

        if delivery.status == DeliveryStatus.SENDING.value:
            # 交给对账：上一次的 attempt 已落库但没有响应，
            # 重发会在"其实已经发出"时打扰客户两次。
            return DeliveryAttemptResult(
                delivery_id=delivery_id,
                action="RECONCILED",
                status=delivery.status,
                detail={"reason": "IN_FLIGHT_NEEDS_QUERY"},
                retry_at=delivery.next_reconcile_at,
            )

        attempted = await _attempt_count(session, delivery_id)
        if attempted >= max_attempts:
            delivery.status = DeliveryStatus.FAILED_FINAL.value
            delivery.reconcile_status = ReconcileStatus.GAVE_UP.value
            await session.commit()
            return DeliveryAttemptResult(
                delivery_id=delivery_id,
                action="FAILED",
                status=DeliveryStatus.FAILED_FINAL.value,
                attempt_number=attempted,
                detail={"reason": "MAX_ATTEMPTS"},
            )

        attempt_number = attempted + 1
        request = SendRequest(
            delivery_id=delivery.id,
            tenant_id=tenant_id,
            channel=delivery.channel,
            recipient_ref=delivery.recipient_ref,
            template_version=delivery.template_version,
            idempotency_key=delivery.provider_idempotency_key,
            payload_hash=delivery.payload_hash,
            payload=dict(delivery.payload),
        )
        # 调用之前先落 attempt：进程在调用中被杀，也要留下"发过什么"的线索。
        session.add(
            m.DeliveryAttempt(
                id=new_id(),
                tenant_id=tenant_id,
                delivery_id=delivery.id,
                attempt_number=attempt_number,
                provider=delivery.provider,
                provider_account_id=delivery.provider_account_id,
                request_hash=content_hash(
                    {
                        "channel": request.channel,
                        "recipient_ref": request.recipient_ref,
                        "template_version": request.template_version,
                        "payload_hash": request.payload_hash,
                        "idempotency_key": request.idempotency_key,
                    }
                ),
                submitted_at=now,
                status=DeliveryStatus.SENDING.value,
            )
        )
        delivery.status = DeliveryStatus.SENDING.value
        delivery.last_reconcile_at = now
        delivery.next_reconcile_at = now + timedelta(
            seconds=RECONCILE_BASE_DELAY_SECONDS
        )
        delivery.reconcile_deadline_at = now + timedelta(
            seconds=reconcile_deadline_seconds
        )
        delivery.reconcile_status = ReconcileStatus.PENDING.value
        prepared = _Prepared(request=request, attempt_number=attempt_number)
        await session.commit()

    # ---- 阶段二：事务之外调用供应商 ----
    try:
        outcome = await provider.send(prepared.request)
    except ProviderTimeout as exc:
        outcome = SendOutcome(kind="UNKNOWN", detail={"message": str(exc)})

    # ---- 阶段三：回来记账 ----
    async with session_factory() as session:
        return await _record_outcome(
            session,
            tenant_id=tenant_id,
            delivery_id=delivery_id,
            attempt_number=prepared.attempt_number,
            outcome=outcome,
            now=now,
            retry_delay_seconds=retry_delay_seconds,
            reconcile_deadline_seconds=reconcile_deadline_seconds,
            provider=provider,
        )


async def _record_outcome(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    delivery_id: UUID,
    attempt_number: int,
    outcome: SendOutcome,
    now: datetime,
    retry_delay_seconds: int,
    reconcile_deadline_seconds: int,
    provider: NotificationProviderPort,
) -> DeliveryAttemptResult:
    delivery = (
        await session.execute(
            select(m.NotificationDelivery)
            .where(
                m.NotificationDelivery.tenant_id == tenant_id,
                m.NotificationDelivery.id == delivery_id,
            )
            .with_for_update()
        )
    ).scalar_one()

    attempt = (
        await session.execute(
            select(m.DeliveryAttempt).where(
                m.DeliveryAttempt.tenant_id == tenant_id,
                m.DeliveryAttempt.delivery_id == delivery_id,
                m.DeliveryAttempt.attempt_number == attempt_number,
            )
        )
    ).scalar_one()

    attempt.response_at = now
    attempt.status = outcome.kind
    attempt.provider_message_id = outcome.provider_message_id
    attempt.provider_request_id = outcome.provider_request_id
    attempt.error_code = outcome.error_code
    attempt.response_ref = dict(outcome.detail) or None

    if outcome.kind == "ACCEPTED":
        delivery.status = DeliveryStatus.ACCEPTED.value
        if outcome.provider_message_id:
            await _bind_message(
                session,
                tenant_id=tenant_id,
                delivery=delivery,
                provider_message_id=outcome.provider_message_id,
            )
        # 受理不是送达：有查单能力的渠道才安排确认，没有就明确停在 ACCEPTED。
        if provider.supports_query:
            delivery.reconcile_status = ReconcileStatus.PENDING.value
            delivery.next_reconcile_at = now + timedelta(
                seconds=DELIVERED_CONFIRM_DELAY_SECONDS
            )
        else:
            delivery.reconcile_status = ReconcileStatus.NOT_REQUIRED.value
            delivery.next_reconcile_at = None
    elif outcome.kind == "RETRYABLE":
        delivery.status = DeliveryStatus.FAILED_RETRYABLE.value
        delivery.reconcile_status = ReconcileStatus.NOT_REQUIRED.value
        delivery.next_reconcile_at = now + timedelta(seconds=retry_delay_seconds)
    elif outcome.kind == "FINAL":
        delivery.status = DeliveryStatus.FAILED_FINAL.value
        delivery.reconcile_status = ReconcileStatus.GAVE_UP.value
        delivery.next_reconcile_at = None
    else:  # UNKNOWN
        delivery.status = DeliveryStatus.UNKNOWN.value
        delivery.reconcile_status = ReconcileStatus.PENDING.value
        delivery.next_reconcile_at = now + timedelta(seconds=RECONCILE_BASE_DELAY_SECONDS)
        if delivery.reconcile_deadline_at is None:
            delivery.reconcile_deadline_at = now + timedelta(
                seconds=reconcile_deadline_seconds
            )

    delivery.last_reconcile_at = now
    result = DeliveryAttemptResult(
        delivery_id=delivery_id,
        action="SENT" if not outcome.detail.get("idempotent_replay") else "REPLAYED",
        status=delivery.status,
        attempt_number=attempt_number,
        provider_message_id=outcome.provider_message_id,
        detail={"kind": outcome.kind, "error_code": outcome.error_code},
    )
    await session.commit()
    return result


async def _bind_message(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    delivery: m.NotificationDelivery,
    provider_message_id: str,
) -> None:
    """建立"供应商消息 → 逻辑投递"的映射。

    这是回调能落到正确租户的唯一依据：回调 payload 自报的 tenant 不可信，
    可信的是同步响应里拿到的 message_id 与它所属的账户。
    """

    binding = (
        await session.execute(
            select(m.ProviderMessageBinding).where(
                m.ProviderMessageBinding.provider == delivery.provider,
                m.ProviderMessageBinding.provider_account_id
                == delivery.provider_account_id,
                m.ProviderMessageBinding.provider_message_id == provider_message_id,
            )
        )
    ).scalar_one_or_none()
    if binding is not None:
        return
    try:
        async with session.begin_nested():
            session.add(
                m.ProviderMessageBinding(
                    id=new_id(),
                    provider=delivery.provider,
                    provider_account_id=delivery.provider_account_id,
                    provider_message_id=provider_message_id,
                    delivery_id=delivery.id,
                    tenant_id=tenant_id,
                )
            )
            await session.flush()
    except IntegrityError:
        # 并发下另一个执行者已建立映射，视为成功。
        return


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------
async def reconcile_delivery(
    session_factory,
    provider: NotificationProviderPort,
    *,
    tenant_id: UUID,
    delivery_id: UUID,
    now: datetime,
) -> DeliveryAttemptResult:
    """查证一条结果不明的投递。

    ``UNKNOWN`` 的处理优先级是**查单**而不是重发（设计稿 §11.2）。只有当查单
    明确回答"供应商没有这条消息"时，重发才是安全的。
    """

    async with session_factory() as session:
        delivery = (
            await session.execute(
                select(m.NotificationDelivery)
                .where(
                    m.NotificationDelivery.tenant_id == tenant_id,
                    m.NotificationDelivery.id == delivery_id,
                )
            )
        ).scalar_one_or_none()
        if delivery is None:
            return DeliveryAttemptResult(
                delivery_id=delivery_id, action="SKIPPED", status="MISSING"
            )
        if delivery.status in (
            DeliveryStatus.DELIVERED.value,
            DeliveryStatus.SUPERSEDED.value,
            DeliveryStatus.FAILED_FINAL.value,
            DeliveryStatus.FAILED_RETRYABLE.value,
        ):
            return DeliveryAttemptResult(
                delivery_id=delivery_id, action="SKIPPED", status=delivery.status
            )
        key = delivery.provider_idempotency_key
        deadline = delivery.reconcile_deadline_at
        message_id = None
        if delivery.status == DeliveryStatus.ACCEPTED.value:
            message_id = (
                await session.execute(
                    select(m.DeliveryAttempt.provider_message_id)
                    .where(
                        m.DeliveryAttempt.delivery_id == delivery_id,
                        m.DeliveryAttempt.provider_message_id.is_not(None),
                    )
                    .order_by(m.DeliveryAttempt.attempt_number.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    # 查单也是外部调用：同样在事务之外。
    query = await provider.query(
        idempotency_key=key, provider_message_id=message_id
    )

    async with session_factory() as session:
        delivery = (
            await session.execute(
                select(m.NotificationDelivery)
                .where(
                    m.NotificationDelivery.tenant_id == tenant_id,
                    m.NotificationDelivery.id == delivery_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        delivery.last_reconcile_at = now
        delivery.reconcile_result = {
            "known": query.known,
            "delivered": query.delivered,
            "accepted": query.accepted,
            "error_code": query.error_code,
        }

        if query.delivered:
            delivery.status = DeliveryStatus.DELIVERED.value
            delivery.reconcile_status = ReconcileStatus.RESOLVED.value
            delivery.next_reconcile_at = None
            action: Action = "RECONCILED"
        elif query.accepted:
            # 查到了消息：那一次其实发出去了。停在 ACCEPTED 等回执。
            delivery.status = DeliveryStatus.ACCEPTED.value
            delivery.reconcile_status = (
                ReconcileStatus.PENDING.value
                if provider.supports_query
                else ReconcileStatus.NOT_REQUIRED.value
            )
            delivery.next_reconcile_at = (
                now + timedelta(seconds=DELIVERED_CONFIRM_DELAY_SECONDS)
                if provider.supports_query
                else None
            )
            action = "RECONCILED"
        elif query.known:
            # 供应商明确没有这条消息：这时重发是安全的。
            delivery.status = DeliveryStatus.FAILED_RETRYABLE.value
            delivery.reconcile_status = ReconcileStatus.RESOLVED.value
            delivery.next_reconcile_at = now
            action = "RECONCILED"
        elif deadline is None or now >= deadline:
            # 既查不到也等不到：**不自动重发**，交人工。把未知改成失败会掩盖
            # "可能已经送达"，自动重发又可能重复打扰。
            delivery.reconcile_status = ReconcileStatus.GAVE_UP.value
            delivery.next_reconcile_at = None
            action = "ESCALATED"
        else:
            delivery.next_reconcile_at = min(
                now + timedelta(seconds=RECONCILE_MAX_DELAY_SECONDS),
                deadline,
            )
            action = "RECONCILED"

        status = delivery.status
        retry_at = delivery.next_reconcile_at
        await session.commit()

    return DeliveryAttemptResult(
        delivery_id=delivery_id,
        action=action,
        status=status,
        detail={"known": query.known, "error_code": query.error_code},
        retry_at=retry_at,
    )


async def mark_superseded(
    session: AsyncSession, *, tenant_id: UUID, delivery_id: UUID
) -> None:
    """把一条投递标记为被替代（改约/取消后人工或回调路径使用）。"""

    await session.execute(
        update(m.NotificationDelivery)
        .where(
            m.NotificationDelivery.tenant_id == tenant_id,
            m.NotificationDelivery.id == delivery_id,
        )
        .values(status=DeliveryStatus.SUPERSEDED.value, next_reconcile_at=None)
    )


__all__ = [
    "Action",
    "DeliveryAttemptResult",
    "RECONCILE_BASE_DELAY_SECONDS",
    "attempt_delivery",
    "corrected_template_version",
    "enqueue_delivery",
    "mark_superseded",
    "provider_idempotency_key",
    "reconcile_delivery",
]
