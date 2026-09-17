"""供应商回执：验签、去重、隔离队列、状态归并、对账截止。

这一组测试守的是"送达"这件事的可信度。失败模式都很安静：多写一笔状态没人发现，
少写一笔状态要等客户投诉。所以每条规则都显式断言，包括**不能发生的事**：

- 伪造/未知来源的回执绝不写任何租户的数据；
- 重复投递同一条事件不改状态；
- 晚到的 ACCEPTED 不能把 DELIVERED 降级；
- 回调 payload 自报的 tenant 不被采信；
- 回调先于同步响应时进隔离队列，而不是被丢掉；
- 对账超过截止转人工，而不是把"未知"改写成"失败"。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from appointment.core.enums import (
    DeliveryStatus,
    JobStatus,
    ReconcileStatus,
    ReceiptProcessingStatus,
)
from appointment.db import models as m
from appointment.db.session import get_sessionmaker
from appointment.worker.providers import sign_receipt
from appointment.worker import (
    JOB_ALERT_MANUAL,
    SandboxProvider,
    Worker,
    ingest_receipt,
    reprocess_quarantined,
    sandbox_account,
)
from tests.test_worker_delivery import _confirm_appointment, _deliveries, _jobs, _reload

pytestmark = pytest.mark.invariant


async def _sent_delivery(session, seeded, clock, tomorrow_window, settings):
    """跑到"已受理"状态：返回 ``(delivery, provider, message_id)``。"""

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    provider = SandboxProvider()
    worker = Worker(
        owner="worker-1",
        settings=settings,
        provider=provider,
        session_factory=get_sessionmaker(),
    )
    await worker.run_once(now=now)
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    return delivery, provider, provider.message_id_for(delivery.provider_idempotency_key)


def _receipt(provider, *, message_id, event_type, event_id, occurred_at):
    body, headers = provider.build_receipt(
        event_id=event_id,
        message_id=message_id,
        event_type=event_type,
        occurred_at=occurred_at.isoformat(),
    )
    return body, headers["X-Provider-Signature"]


async def _reload_delivery(session, delivery_id):
    return (
        await session.execute(
            _reload(
                select(m.NotificationDelivery).where(
                    m.NotificationDelivery.id == delivery_id
                )
            )
        )
    ).scalars().one()


# ---------------------------------------------------------------------------
# 验签与来源
# ---------------------------------------------------------------------------
async def test_receipt_with_bad_signature_is_isolated_and_changes_nothing(
    session, seeded, clock, tomorrow_window, settings
):
    """签名不对＝来源不可信：入库隔离，但不碰任何投递状态。"""

    delivery, provider, message_id = await _sent_delivery(
        session, seeded, clock, tomorrow_window, settings
    )
    body, _signature = _receipt(
        provider,
        message_id=message_id,
        event_type="delivered",
        event_id="evt-forged",
        occurred_at=clock.now(),
    )

    result = await ingest_receipt(
        session,
        account=sandbox_account(),
        body=body,
        signature="deadbeef",
        now=clock.now(),
    )
    await session.commit()

    assert result.action == "REJECTED"
    assert result.reason == "INVALID_SIGNATURE"
    receipt = (
        await session.execute(
            select(m.ProviderReceipt).where(
                m.ProviderReceipt.provider_event_id == "evt-forged"
            )
        )
    ).scalar_one()
    assert receipt.validation_status == "INVALID_SIGNATURE"
    assert receipt.processing_status == ReceiptProcessingStatus.QUARANTINED.value
    assert receipt.tenant_id is None, "隔离记录不得挂到任何租户上"

    refreshed = await _reload_delivery(session, delivery.id)
    assert refreshed.status == DeliveryStatus.ACCEPTED.value, "伪造回执不得推进状态"


async def test_unknown_account_is_quarantined_without_touching_tenants(
    session, seeded, clock, tomorrow_window, settings
):
    """不认识的账户：隔离 + 告警，不写任何租户数据。"""

    delivery, provider, message_id = await _sent_delivery(
        session, seeded, clock, tomorrow_window, settings
    )
    body, signature = _receipt(
        provider,
        message_id=message_id,
        event_type="delivered",
        event_id="evt-unknown-account",
        occurred_at=clock.now(),
    )

    result = await ingest_receipt(
        session, account=None, body=body, signature=signature, now=clock.now()
    )
    await session.commit()

    assert result.action == "REJECTED"
    assert result.reason == "UNKNOWN_SOURCE"
    receipt = (
        await session.execute(
            select(m.ProviderReceipt).where(
                m.ProviderReceipt.provider_event_id == "evt-unknown-account"
            )
        )
    ).scalar_one()
    assert receipt.validation_status == "UNKNOWN_SOURCE"
    assert receipt.tenant_id is None
    assert (await _reload_delivery(session, delivery.id)).status == (
        DeliveryStatus.ACCEPTED.value
    )


# ---------------------------------------------------------------------------
# 去重与归并
# ---------------------------------------------------------------------------
async def test_duplicate_receipt_is_deduplicated_by_provider_event_id(
    session, seeded, clock, tomorrow_window, settings
):
    """同一条供应商事件重复投递：只有第一条参与归并。"""

    delivery, provider, message_id = await _sent_delivery(
        session, seeded, clock, tomorrow_window, settings
    )
    body, signature = _receipt(
        provider,
        message_id=message_id,
        event_type="delivered",
        event_id="evt-dup",
        occurred_at=clock.now(),
    )

    first = await ingest_receipt(
        session,
        account=sandbox_account(),
        body=body,
        signature=signature,
        now=clock.now(),
    )
    await session.commit()
    assert first.action == "APPLIED"
    assert first.status == DeliveryStatus.DELIVERED.value

    second = await ingest_receipt(
        session,
        account=sandbox_account(),
        body=body,
        signature=signature,
        now=clock.now() + timedelta(seconds=5),
    )
    await session.commit()
    assert second.action == "DUPLICATE"
    assert second.receipt_id == first.receipt_id
    assert (await _reload_delivery(session, delivery.id)).status == (
        DeliveryStatus.DELIVERED.value
    )


async def test_late_accepted_receipt_does_not_downgrade_delivered(
    session, seeded, clock, tomorrow_window, settings
):
    """**核心规则**：晚到的 ACCEPTED 不把 DELIVERED 降级。"""

    delivery, provider, message_id = await _sent_delivery(
        session, seeded, clock, tomorrow_window, settings
    )
    now = clock.now()

    delivered_body, delivered_sig = _receipt(
        provider,
        message_id=message_id,
        event_type="delivered",
        event_id="evt-delivered",
        occurred_at=now,
    )
    await ingest_receipt(
        session,
        account=sandbox_account(),
        body=delivered_body,
        signature=delivered_sig,
        now=now,
    )
    await session.commit()

    # 一条更晚到达、但语义更弱的回执。
    accepted_body, accepted_sig = _receipt(
        provider,
        message_id=message_id,
        event_type="accepted",
        event_id="evt-late-accepted",
        occurred_at=now + timedelta(seconds=1),
    )
    late = await ingest_receipt(
        session,
        account=sandbox_account(),
        body=accepted_body,
        signature=accepted_sig,
        now=now + timedelta(seconds=2),
    )
    await session.commit()

    assert late.action == "APPLIED"
    assert late.status == DeliveryStatus.DELIVERED.value
    refreshed = await _reload_delivery(session, delivery.id)
    assert refreshed.status == DeliveryStatus.DELIVERED.value
    assert refreshed.reconcile_status == ReconcileStatus.RESOLVED.value
    # 冲突被留痕，而不是被静默吞掉。
    conflicts = refreshed.reconcile_result["conflicts"]
    assert conflicts and conflicts[-1]["event_type"] == "accepted"


async def test_out_of_order_receipt_by_evidence_time_is_ignored(
    session, seeded, clock, tomorrow_window, settings
):
    """按供应商发生时间乱序到达的旧事件不参与归并。"""

    delivery, provider, message_id = await _sent_delivery(
        session, seeded, clock, tomorrow_window, settings
    )
    now = clock.now()

    later_body, later_sig = _receipt(
        provider,
        message_id=message_id,
        event_type="delivered",
        event_id="evt-later",
        occurred_at=now + timedelta(minutes=5),
    )
    await ingest_receipt(
        session,
        account=sandbox_account(),
        body=later_body,
        signature=later_sig,
        now=now,
    )
    await session.commit()

    older_body, older_sig = _receipt(
        provider,
        message_id=message_id,
        event_type="bounced",
        event_id="evt-older",
        occurred_at=now,
    )
    # 事件发生时间早（乱序），但请求本身是刚刚发出的（issued_at 在重放窗口内）。
    # 这两件事必须分开：混用会让"迟到的真事件"被当成重放拒掉。
    older_body = {**older_body, "issued_at": (now + timedelta(minutes=6)).isoformat()}
    older_sig = sign_receipt(
        secret=provider.signing_secret,
        account_id=provider.account_id,
        body=older_body,
    )["X-Provider-Signature"]
    stale = await ingest_receipt(
        session,
        account=sandbox_account(),
        body=older_body,
        signature=older_sig,
        now=now + timedelta(minutes=6),
    )
    await session.commit()

    assert stale.action == "IGNORED_STALE"
    assert stale.reason == "EVIDENCE_OUT_OF_ORDER"
    assert (await _reload_delivery(session, delivery.id)).status == (
        DeliveryStatus.DELIVERED.value
    )


# ---------------------------------------------------------------------------
# 隔离队列与租户归属
# ---------------------------------------------------------------------------
async def test_receipt_arriving_before_sync_response_is_quarantined_then_reprocessed(
    session, seeded, clock, tomorrow_window, settings
):
    """回调先于同步响应 → 入隔离队列；映射建立后重处理。"""

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    provider = SandboxProvider()
    worker = Worker(
        owner="worker-1",
        settings=settings,
        provider=provider,
        session_factory=get_sessionmaker(),
    )
    # 只消费 Outbox：逻辑投递已建立，但还没调用供应商，所以没有映射。
    await worker.run_once(now=now, reclaim=False, jobs=False, receipts=False)
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    assert delivery.status == DeliveryStatus.PENDING.value

    message_id = provider.message_id_for(delivery.provider_idempotency_key)
    body, signature = _receipt(
        provider,
        message_id=message_id,
        event_type="delivered",
        event_id="evt-early",
        occurred_at=now,
    )
    early = await ingest_receipt(
        session, account=sandbox_account(), body=body, signature=signature, now=now
    )
    await session.commit()
    assert early.action == "QUARANTINED"
    assert early.reason == "NO_BINDING_YET"

    # 同步响应到了：发送并建立映射。
    await worker.run_once(
        now=now + timedelta(seconds=1),
        reclaim=False,
        outbox=False,
        receipts=False,
    )
    assert (await _reload_delivery(session, delivery.id)).status == (
        DeliveryStatus.ACCEPTED.value
    )

    # 下一轮：隔离队列重处理，送达证据终于能落地。
    await worker.run_once(
        now=now + timedelta(seconds=2),
        reclaim=False,
        outbox=False,
        jobs=False,
    )
    refreshed = await _reload_delivery(session, delivery.id)
    assert refreshed.status == DeliveryStatus.DELIVERED.value
    assert refreshed.reconcile_status == ReconcileStatus.RESOLVED.value

    receipt = (
        await session.execute(
            select(m.ProviderReceipt).where(
                m.ProviderReceipt.provider_event_id == "evt-early"
            )
        )
    ).scalar_one()
    assert receipt.processing_status == ReceiptProcessingStatus.PROCESSED.value
    assert receipt.tenant_id == seeded.tenant_id, "租户来自可信映射，不来自回调"


async def test_receipt_payload_cannot_claim_another_tenant(
    session, seeded, clock, tomorrow_window, settings
):
    """回调 payload 自报的 tenant 不被采信；归属只由可信映射决定。"""

    from uuid import uuid4

    delivery, provider, message_id = await _sent_delivery(
        session, seeded, clock, tomorrow_window, settings
    )
    foreign_tenant = str(uuid4())
    body, signature = provider.build_receipt(
        event_id="evt-tenant-spoof",
        message_id=message_id,
        event_type="delivered",
        occurred_at=clock.now().isoformat(),
        extra={"tenant_id": foreign_tenant, "appointment_id": str(uuid4())},
    )
    signed = body, signature["X-Provider-Signature"]

    result = await ingest_receipt(
        session,
        account=sandbox_account(),
        body=signed[0],
        signature=signed[1],
        now=clock.now(),
    )
    await session.commit()

    assert result.action == "APPLIED"
    assert result.delivery_id == delivery.id
    receipt = (
        await session.execute(
            select(m.ProviderReceipt).where(
                m.ProviderReceipt.provider_event_id == "evt-tenant-spoof"
            )
        )
    ).scalar_one()
    assert receipt.tenant_id == seeded.tenant_id
    assert receipt.tenant_id != foreign_tenant
    assert (await _reload_delivery(session, delivery.id)).status == (
        DeliveryStatus.DELIVERED.value
    )


# ---------------------------------------------------------------------------
# 证据边界与对账截止
# ---------------------------------------------------------------------------
async def test_channel_without_receipts_or_query_stays_accepted_with_explicit_scope(
    session, seeded, clock, tomorrow_window, settings
):
    """没有查单能力、也拿不到送达回执的渠道：停在 ACCEPTED 并明确证据边界。"""

    provider = SandboxProvider(supports_query=False)
    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    worker = Worker(
        owner="worker-1",
        settings=settings,
        provider=provider,
        session_factory=get_sessionmaker(),
    )
    await worker.run_once(now=now)

    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    assert delivery.status == DeliveryStatus.ACCEPTED.value
    # 不接受"受理即送达"，也不安排永远无法解决的对账任务。
    assert delivery.reconcile_status == ReconcileStatus.NOT_REQUIRED.value
    assert delivery.next_reconcile_at is None
    assert await _jobs(session, seeded.tenant_id, "notification.reconcile") == []
    # 之后跑多少轮都不会升级成 DELIVERED。
    await worker.run_once(now=now + timedelta(hours=1))
    assert (await _reload_delivery(session, delivery.id)).status == (
        DeliveryStatus.ACCEPTED.value
    )


async def test_reconcile_deadline_escalates_to_human_without_faking_failure(
    session, seeded, clock, tomorrow_window, settings
):
    """对账超过截止：转人工，**不把未知改写成失败**，也不自动重发。"""

    provider = SandboxProvider(query_unknown=True)
    provider.script_next(["ACCEPTED_LOST"])
    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    worker = Worker(
        owner="worker-1",
        settings=settings,
        provider=provider,
        session_factory=get_sessionmaker(),
    )
    await worker.run_once(now=now)
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    assert delivery.status == DeliveryStatus.UNKNOWN.value
    calls = len(provider.calls)

    # 跑到对账截止之后。
    beyond = now + timedelta(
        seconds=settings.delivery_reconcile_deadline_seconds + 10
    )
    report = await worker.run_once(now=beyond)

    assert report.escalations >= 1
    refreshed = await _reload_delivery(session, delivery.id)
    # 关键：状态仍是 UNKNOWN。把"可能已送达"记成 FAILED 会让客服照着错误
    # 事实回复客户，也会掩盖"要不要补发"这个真正需要人判断的问题。
    assert refreshed.status == DeliveryStatus.UNKNOWN.value
    assert refreshed.reconcile_status == ReconcileStatus.GAVE_UP.value
    assert len(provider.calls) == calls, "结果不明不得自动重发"

    alert = (await _jobs(session, seeded.tenant_id, JOB_ALERT_MANUAL))[0]
    assert alert.payload["reason"] == "RECONCILE_DEADLINE_EXCEEDED"
    assert alert.payload["delivery_id"] == str(delivery.id)
    # 人工待办是**排进队列**而不是当轮办结的：它被创建在 job 领取之后，
    # 所以这一轮只能看到它 PENDING。
    assert alert.status == JobStatus.PENDING.value

    # 下一轮把它取走留痕，交付链路其余部分不受影响。
    await worker.run_once(now=beyond + timedelta(seconds=5))
    consumed = (await _jobs(session, seeded.tenant_id, JOB_ALERT_MANUAL))[0]
    assert consumed.status == JobStatus.SUCCEEDED.value
    assert consumed.result_ref["kind"] == "MANUAL"
    assert (await _reload_delivery(session, delivery.id)).status == (
        DeliveryStatus.UNKNOWN.value
    )


async def test_unknown_event_type_is_recorded_but_does_not_move_status(
    session, seeded, clock, tomorrow_window, settings
):
    """不认识的事件类型：记录原文，但不凭猜测推进状态。"""

    delivery, provider, message_id = await _sent_delivery(
        session, seeded, clock, tomorrow_window, settings
    )
    body, signature = _receipt(
        provider,
        message_id=message_id,
        event_type="teleported",
        event_id="evt-unknown-type",
        occurred_at=clock.now(),
    )

    result = await ingest_receipt(
        session,
        account=sandbox_account(),
        body=body,
        signature=signature,
        now=clock.now(),
    )
    await session.commit()

    assert result.action == "IGNORED_STALE"
    assert result.reason == "UNKNOWN_EVENT_TYPE"
    assert (await _reload_delivery(session, delivery.id)).status == (
        DeliveryStatus.ACCEPTED.value
    )
