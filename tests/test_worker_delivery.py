"""Worker 与投递账本：租约、fencing、两阶段发送、对账、过期回收。

每个测试对应设计稿 §11 里的一行故障处置，重点是**不该做的事**也要被断言：

- 消费者重复领取同一事件，不产生第二条逻辑投递、不重复调用供应商；
- 调用供应商前先落 attempt，"发出去但响应丢了"必须查单而不是重发；
- 租约被接管后旧持有者写不进去（仅靠 lease_until 挡不住被唤醒的旧实例）；
- 占位到期由后台状态化回收，不依赖进程内 finally；
- 达到上限进死信/转人工，而不是无限重试。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from appointment.core.enums import (
    AllocationState,
    DeliveryStatus,
    HoldState,
    JobStatus,
    OutboxStatus,
    ReconcileStatus,
    TaskState,
)
from appointment.core.ids import new_id
from appointment.db import models as m
from appointment.db.session import get_sessionmaker
from appointment.domain.availability import ResourcePreferences, search_availability
from appointment.domain.booking import confirm_appointment, create_hold
from appointment.domain.catalog import load_service
from appointment.domain.quote import get_service_quote
from appointment.domain.tasks import (
    ensure_conversation,
    get_or_create_task,
    set_task_state,
)
from appointment.worker import (
    JOB_ALERT_MANUAL,
    JOB_NOTIFICATION_RECONCILE,
    JOB_NOTIFICATION_SEND,
    SandboxProvider,
    Worker,
    attempt_delivery,
    claim_jobs,
    finish_job,
)
from tests.conftest import customer_ctx

pytestmark = pytest.mark.invariant


# ---------------------------------------------------------------------------
# 走真实主链路拿到"已确认订单 + Outbox 事件"
# ---------------------------------------------------------------------------
async def _confirm_appointment(session, seeded, clock, tomorrow_window):
    ctx = customer_ctx(seeded)
    now = clock.now()
    conversation = await ensure_conversation(
        session, ctx, store_id=seeded.store_id, now=now
    )
    task = (
        await get_or_create_task(
            session,
            ctx,
            conversation=conversation,
            release_id="release-local-1",
            now=now,
        )
    ).task
    for state in (TaskState.SEARCHING, TaskState.PROPOSED):
        task = await set_task_state(
            session, ctx, task=task, target=state, expected_version=task.version, now=now
        )

    service = await load_service(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
    )
    offer = await get_service_quote(
        session,
        tenant_id=seeded.tenant_id,
        customer_id=seeded.customer_ids[0],
        store_id=seeded.store_id,
        service=service,
        as_of=now,
    )
    start, end, _ = tomorrow_window
    availability = await search_availability(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service=service,
        window_start=start,
        window_end=end,
        now=now,
        desired_start=start,
        preferences=ResourcePreferences(),
        amount_minor=offer.amount_minor,
        limit=5,
    )
    candidate = availability.candidates[0]
    hold = await create_hold(
        session,
        ctx,
        task=task,
        expected_task_version=task.version,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
        candidate_id=candidate.candidate_id,
        start_at=candidate.start_at,
        end_at=candidate.end_at,
        resource_ids=[unit.resource_id for unit in candidate.resources],
        quote_token=offer.quote_token,
        now=now,
        clock=clock,
    )
    await session.commit()

    reloaded = (
        await session.execute(select(m.Task).where(m.Task.id == task.id))
    ).scalar_one()
    outcome = await confirm_appointment(
        session,
        ctx,
        proposal_id=hold.proposal.id,
        proposal_version=hold.proposal.version,
        confirmation_token=hold.confirmation_token,
        client_confirmation_event_id="worker-evt-1",
        idempotency_key="worker-key-1",
        expected_task_version=reloaded.version,
        now=now,
        clock=clock,
    )
    await session.commit()
    return ctx, outcome.appointment, hold


def _worker(settings, provider, owner="worker-1"):
    return Worker(
        owner=owner,
        settings=settings,
        provider=provider,
        session_factory=get_sessionmaker(),
    )


def _reload(stmt):
    """强制重读，绕过会话身份映射里的旧对象。

    Worker 在**别的会话**里改了行，测试会话里同主键的对象仍然是老值；
    ``expire_on_commit=False`` 让这一点更隐蔽——断言会拿着过期状态通过或失败，
    而两者的原因都不是被测代码。所以断言前一律用 ``populate_existing`` 重读。
    """

    return stmt.execution_options(populate_existing=True)


async def _deliveries(session, tenant_id):
    return list(
        (
            await session.execute(
                _reload(
                    select(m.NotificationDelivery)
                    .where(m.NotificationDelivery.tenant_id == tenant_id)
                    .order_by(m.NotificationDelivery.created_at, m.NotificationDelivery.id)
                )
            )
        ).scalars()
    )


async def _jobs(session, tenant_id, kind):
    return list(
        (
            await session.execute(
                _reload(
                    select(m.Job).where(
                        m.Job.tenant_id == tenant_id, m.Job.kind == kind
                    )
                )
            )
        ).scalars()
    )


async def _one(session, stmt):
    return (await session.execute(_reload(stmt))).scalars().one()


# ---------------------------------------------------------------------------
# 发送链路
# ---------------------------------------------------------------------------
async def test_outbox_consumption_creates_one_delivery_and_sends_once(
    session, seeded, clock, tomorrow_window, settings
):
    """消费 Outbox → 建逻辑投递 → 调度 → 发送。受理不等于送达。"""

    _ctx, appointment, _hold = await _confirm_appointment(
        session, seeded, clock, tomorrow_window
    )
    now = clock.now()
    provider = SandboxProvider()
    worker = _worker(settings, provider)

    report = await worker.run_once(now=now)

    assert report.outbox_claimed == 1
    assert report.outbox_processed == 1
    assert report.deliveries_sent == 1

    deliveries = await _deliveries(session, seeded.tenant_id)
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery.appointment_id == appointment.id
    assert delivery.channel == "sms"
    assert delivery.recipient_ref, "接收者必须用受保护引用，不能为空"
    # 供应商受理 ≠ 送达：只有可信回执才能推到 DELIVERED。
    assert delivery.status == DeliveryStatus.ACCEPTED.value
    assert delivery.reconcile_status == ReconcileStatus.PENDING.value

    # 调用前先落 attempt，且记下了请求哈希与提交时间。
    attempts = list(
        (
            await session.execute(
                _reload(
                    select(m.DeliveryAttempt).where(
                        m.DeliveryAttempt.delivery_id == delivery.id
                    )
                )
            )
        ).scalars()
    )
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].request_hash
    assert attempts[0].submitted_at is not None
    assert attempts[0].provider_message_id

    # 同步响应建立了映射，回调才能落到正确租户。
    binding = await _one(
        session,
        select(m.ProviderMessageBinding).where(
            m.ProviderMessageBinding.delivery_id == delivery.id
        ),
    )
    assert binding.tenant_id == seeded.tenant_id
    assert binding.provider_message_id == attempts[0].provider_message_id

    outbox = list(
        (await session.execute(_reload(select(m.Outbox)))).scalars()
    )
    assert [row.status for row in outbox] == [OutboxStatus.PROCESSED.value]
    assert provider.calls == [delivery.provider_idempotency_key]

    # 受理之后仍需查证送达：对账待办必须被排上。
    reconcile_jobs = await _jobs(session, seeded.tenant_id, JOB_NOTIFICATION_RECONCILE)
    assert len(reconcile_jobs) == 1
    assert reconcile_jobs[0].payload["delivery_id"] == str(delivery.id)


async def test_repeated_worker_pass_does_not_resend(
    session, seeded, clock, tomorrow_window, settings
):
    """Worker 重复跑（至少一次投递）不能变成"至少发两次"。"""

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    provider = SandboxProvider()
    worker = _worker(settings, provider)

    await worker.run_once(now=now)
    first_delivery = (await _deliveries(session, seeded.tenant_id))[0]
    calls_after_first = len(provider.calls)

    # 第二、三轮：Outbox 已 PROCESSED，没有新的可领取事件。
    second = await worker.run_once(now=now + timedelta(seconds=1))
    third = await worker.run_once(now=now + timedelta(seconds=2))

    assert second.outbox_claimed == 0
    assert third.outbox_claimed == 0
    assert len(provider.calls) == calls_after_first, "不得重复调用供应商"
    deliveries = await _deliveries(session, seeded.tenant_id)
    assert len(deliveries) == 1
    assert deliveries[0].id == first_delivery.id


async def test_accepted_but_response_lost_is_reconciled_not_resent(
    session, seeded, clock, tomorrow_window, settings
):
    """供应商已受理但响应丢失 → 先查单，不重发。

    这是整条投递链路存在的理由：这里若重发，客户收到两条提醒；若记成失败，
    客户可能什么都收不到。所以必须查。
    """

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    provider = SandboxProvider()
    provider.script_next(["ACCEPTED_LOST"])
    worker = _worker(settings, provider)

    first = await worker.run_once(now=now)
    assert first.deliveries_sent == 1
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    assert delivery.status == DeliveryStatus.UNKNOWN.value
    assert delivery.reconcile_status == ReconcileStatus.PENDING.value
    calls_after_send = len(provider.calls)

    # 对账到点：只查单，不再发送。
    await worker.run_once(now=now + timedelta(seconds=60))

    assert len(provider.calls) == calls_after_send, "对账阶段不得再次发送"
    assert provider.queries, "必须查单才能判断效果"
    refreshed = (await _deliveries(session, seeded.tenant_id))[0]
    assert refreshed.status == DeliveryStatus.ACCEPTED.value
    # 一次真正发出的消息、一次对账查单：投递尝试表里仍只有一条发送记录。
    attempts = (
        await session.execute(
            select(func.count())
            .select_from(m.DeliveryAttempt)
            .where(m.DeliveryAttempt.delivery_id == delivery.id)
        )
    ).scalar()
    assert attempts == 1


async def test_timeout_with_no_message_at_provider_allows_resend(
    session, seeded, clock, tomorrow_window, settings
):
    """查单明确说"没有这条消息"时，重发才是安全的。"""

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    provider = SandboxProvider()
    provider.script_next(["UNKNOWN"])
    worker = _worker(settings, provider)

    await worker.run_once(now=now)
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    assert delivery.status == DeliveryStatus.UNKNOWN.value
    assert not provider._messages, "超时未记录消息：供应商那里确实没有"

    await worker.run_once(now=now + timedelta(seconds=60))
    refreshed = (await _deliveries(session, seeded.tenant_id))[0]
    assert refreshed.status == DeliveryStatus.FAILED_RETRYABLE.value
    assert provider.queries

    # 再跑一轮把重发做掉：重发走的是**新的发送 job**。
    await worker.run_once(now=now + timedelta(seconds=90))
    final = (await _deliveries(session, seeded.tenant_id))[0]
    assert final.status == DeliveryStatus.ACCEPTED.value
    assert len(provider.calls) == 2
    assert final.id == delivery.id, "重发的是同一条逻辑投递，不是新投递"


async def test_in_flight_without_response_is_queried_not_resent(
    session, seeded, clock, tomorrow_window, settings
):
    """SENDING + 无响应（进程在调用中被杀）→ 只查单。"""

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    provider = SandboxProvider()
    provider.script_next(["ACCEPTED_LOST"])
    worker = _worker(settings, provider)
    await worker.run_once(now=now)

    # 人为回到"调用中"：模拟 attempt 已落库但进程被杀，状态没来得及回收。
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    delivery.status = DeliveryStatus.SENDING.value
    delivery.reconcile_status = ReconcileStatus.PENDING.value
    await session.commit()
    calls_before = len(provider.calls)

    result = await attempt_delivery(
        get_sessionmaker(),
        provider,
        tenant_id=seeded.tenant_id,
        delivery_id=delivery.id,
        now=now + timedelta(seconds=10),
        max_attempts=8,
        reconcile_deadline_seconds=900,
    )
    assert result.action == "RECONCILED"
    assert result.detail and result.detail["reason"] == "IN_FLIGHT_NEEDS_QUERY"
    assert len(provider.calls) == calls_before, "在飞状态不得重发"


async def test_stale_delivery_is_superseded_and_correction_scheduled(
    session, seeded, clock, tomorrow_window, settings
):
    """改约后未发出的提醒必须被跳过，并且真的调度更正投递。"""

    _ctx, appointment, _hold = await _confirm_appointment(
        session, seeded, clock, tomorrow_window
    )
    now = clock.now()
    provider = SandboxProvider()
    worker = _worker(settings, provider)

    # 只做 Outbox 消费：先把逻辑投递建出来，不让发送 job 跑掉。
    await worker.run_once(now=now, reclaim=False, jobs=False, receipts=False)
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    assert delivery.status == DeliveryStatus.PENDING.value

    # 订单被改约：版本 +1。发送前校验必须发现这一点。
    appointment.version = appointment.version + 1
    await session.commit()
    new_version = appointment.version

    result = await attempt_delivery(
        get_sessionmaker(),
        provider,
        tenant_id=seeded.tenant_id,
        delivery_id=delivery.id,
        now=now,
        max_attempts=8,
        reconcile_deadline_seconds=900,
    )
    assert result.action == "SUPERSEDED"
    assert result.correction_delivery_id is not None
    assert provider.calls == [], "过时提醒绝不能发出去"

    deliveries = await _deliveries(session, seeded.tenant_id)
    assert len(deliveries) == 2
    old = next(d for d in deliveries if d.id == delivery.id)
    new = next(d for d in deliveries if d.id == result.correction_delivery_id)
    assert old.status == DeliveryStatus.SUPERSEDED.value
    assert new.supersedes_delivery_id == old.id
    assert new.appointment_version == new_version
    assert new.template_version.endswith(f"rev{new_version}")


async def test_superseded_send_job_schedules_the_correction_delivery(
    session, seeded, clock, tomorrow_window, settings
):
    """过时提醒被跳过后，更正投递必须被排进队列（而不只是日志里的一句话）。"""

    _ctx, appointment, _hold = await _confirm_appointment(
        session, seeded, clock, tomorrow_window
    )
    now = clock.now()
    provider = SandboxProvider()
    worker = _worker(settings, provider)

    await worker.run_once(now=now, reclaim=False, jobs=False, receipts=False)
    delivery = (await _deliveries(session, seeded.tenant_id))[0]

    # 发送 job 还没跑，此时订单被改约。
    appointment.version = appointment.version + 1
    await session.commit()

    await worker.run_once(now=now, reclaim=False, outbox=False, receipts=False)

    deliveries = await _deliveries(session, seeded.tenant_id)
    assert len(deliveries) == 2
    correction = next(d for d in deliveries if d.id != delivery.id)
    assert correction.supersedes_delivery_id == delivery.id

    # 更正投递也要真的发出去。
    await worker.run_once(now=now, reclaim=False, outbox=False, receipts=False)
    refreshed = next(
        d for d in await _deliveries(session, seeded.tenant_id) if d.id == correction.id
    )
    assert refreshed.status == DeliveryStatus.ACCEPTED.value
    assert len(provider.calls) == 1


async def test_retryable_provider_failure_reschedules_the_same_job(
    session, seeded, clock, tomorrow_window, settings
):
    """可重试失败重排同一个 job，而不是新建待办（job 表就是调度器）。"""

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    provider = SandboxProvider()
    provider.script_next(["RETRYABLE", "ACCEPTED"])
    worker = _worker(settings, provider)

    first = await worker.run_once(now=now)
    assert first.jobs_rescheduled == 1
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    assert delivery.status == DeliveryStatus.FAILED_RETRYABLE.value

    send_jobs = await _jobs(session, seeded.tenant_id, JOB_NOTIFICATION_SEND)
    assert len(send_jobs) == 1, "同一条逻辑待办只能有一行"
    assert send_jobs[0].status == JobStatus.FAILED_RETRYABLE.value

    # 退避到期后再跑一轮即成功。
    await worker.run_once(now=now + timedelta(seconds=120))
    refreshed = (await _deliveries(session, seeded.tenant_id))[0]
    assert refreshed.status == DeliveryStatus.ACCEPTED.value
    assert len(provider.calls) == 2


async def test_final_provider_failure_is_not_retried_forever(
    session, seeded, clock, tomorrow_window, settings
):
    """终态失败（例如收件人无效）不重试。"""

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    provider = SandboxProvider()
    provider.script_next(["FINAL"])
    worker = _worker(settings, provider)

    await worker.run_once(now=now)
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    assert delivery.status == DeliveryStatus.FAILED_FINAL.value
    calls = len(provider.calls)

    report = await worker.run_once(now=now + timedelta(seconds=120))
    assert len(provider.calls) == calls, "终态失败不得再调用供应商"
    assert report.outbox_claimed == 0


async def test_unknown_outbox_event_goes_to_dead_letter(
    session, seeded, clock, tomorrow_window, settings
):
    """不认识的事件类型重试到上限后进死信，而不是无限重试。"""

    from uuid import uuid4

    _ctx, appointment, _hold = await _confirm_appointment(
        session, seeded, clock, tomorrow_window
    )
    now = clock.now()
    session.add(
        m.Outbox(
            id=uuid4(),
            tenant_id=seeded.tenant_id,
            aggregate_type="appointment",
            aggregate_id=appointment.id,
            aggregate_version=1,
            event_type="mystery_event",
            payload={"appointment_id": str(appointment.id), "channel": "sms"},
            occurred_at=now,
            available_at=now,
            status=OutboxStatus.PENDING.value,
        )
    )
    await session.commit()

    tight = settings.model_copy(update={"outbox_max_attempts": 1})
    worker = _worker(tight, SandboxProvider())
    report = await worker.run_once(now=now)

    assert report.outbox_dead_letter == 1
    mystery = await _one(
        session, select(m.Outbox).where(m.Outbox.event_type == "mystery_event")
    )
    assert mystery.status == OutboxStatus.DEAD_LETTER.value


# ---------------------------------------------------------------------------
# 租约与 fencing
# ---------------------------------------------------------------------------
async def test_expired_lease_can_be_taken_over_and_old_holder_is_fenced(
    session, seeded, clock, tomorrow_window, settings
):
    """租约到期允许接管；旧持有者拿着旧 token 写不进去。"""

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    worker = _worker(settings, SandboxProvider(), owner="worker-A")
    # 只建待办，不执行：否则 job 会被这一轮做完。
    await worker.run_once(now=now, reclaim=False, jobs=False, receipts=False)

    async with get_sessionmaker()() as s:
        first = await claim_jobs(s, owner="worker-A", now=now, lease_seconds=0, limit=1)
        await s.commit()
    assert len(first) == 1
    job_a, lease_a = first[0]

    # 租约立刻过期，另一个 Worker 接管。
    async with get_sessionmaker()() as s:
        second = await claim_jobs(
            s, owner="worker-B", now=now, lease_seconds=30, limit=1
        )
        await s.commit()
    assert len(second) == 1, "到期租约必须允许接管"
    job_b, lease_b = second[0]
    assert job_b.id == job_a.id
    assert lease_b.token > lease_a.token, "接管必须推进 fencing token"

    # 旧持有者醒来后按旧 token 提交：必须被挡住。
    async with get_sessionmaker()() as s:
        stale_ok = await finish_job(
            s, job_id=job_a.id, lease=lease_a, status=JobStatus.SUCCEEDED
        )
        await s.commit()
    assert stale_ok is False, "旧持有者不得写回结果"

    # 新持有者可以正常提交。
    async with get_sessionmaker()() as s:
        fresh_ok = await finish_job(
            s, job_id=job_b.id, lease=lease_b, status=JobStatus.SUCCEEDED
        )
        await s.commit()
    assert fresh_ok is True


# ---------------------------------------------------------------------------
# 过期占位回收
# ---------------------------------------------------------------------------
async def test_worker_reclaims_expired_holds_and_allocations(
    session, seeded, clock, tomorrow_window, settings
):
    """占位到期由后台状态化回收，不依赖进程内 finally 释放。"""

    _ctx, _appointment, hold = await _confirm_appointment(
        session, seeded, clock, tomorrow_window
    )
    # 把 hold 打回 HELD 并设一个已过期的截止时间，模拟"占位后进程退出"。
    expired_at = clock.now() - timedelta(seconds=1)
    hold.hold.state = HoldState.HELD.value
    hold.hold.expires_at = expired_at
    hold.hold.booked_appointment_id = None
    for allocation in (
        await session.execute(
            select(m.ResourceAllocation).where(
                m.ResourceAllocation.hold_id == hold.hold.id
            )
        )
    ).scalars():
        # HELD 的形状约束要求 expires_at 非空且 appointment_id 为空。
        allocation.state = AllocationState.HELD.value
        allocation.expires_at = expired_at
        allocation.appointment_id = None
    await session.commit()

    worker = _worker(settings, SandboxProvider())
    report = await worker.run_once(now=clock.now())

    assert report.reclaimed[str(seeded.tenant_id)] >= 3, "两个资源占用 + 一个占位头"
    refreshed = await _one(session, select(m.Hold).where(m.Hold.id == hold.hold.id))
    assert refreshed.state == HoldState.EXPIRED.value

    allocations = list(
        (
            await session.execute(
                _reload(
                    select(m.ResourceAllocation).where(
                        m.ResourceAllocation.hold_id == hold.hold.id
                    )
                )
            )
        ).scalars()
    )
    assert {row.state for row in allocations} == {AllocationState.EXPIRED.value}
    # expires_at 被保留（它是历史事实），真正释放区间的是状态：排他约束的谓词是
    # state IN ('HELD','BOOKED')。所以这里直接验证"同资源同区间可以重新占住"。
    assert all(row.expires_at is not None for row in allocations)
    reclaimed = allocations[0]
    session.add(
        m.ResourceAllocation(
            id=new_id(),
            tenant_id=seeded.tenant_id,
            resource_id=reclaimed.resource_id,
            store_id=reclaimed.store_id,
            hold_id=hold.hold.id,
            start_at=reclaimed.start_at,
            end_at=reclaimed.end_at,
            state=AllocationState.HELD.value,
            expires_at=clock.now() + timedelta(seconds=300),
        )
    )
    await session.commit()


async def test_missing_recipient_creates_manual_alert_not_a_fake_delivery(
    session, seeded, clock, tomorrow_window, settings
):
    """没有可用联系方式不是"已通知"：留人工待办，而不是建一条假装发过的投递。"""

    _ctx, _appointment, _hold = await _confirm_appointment(
        session, seeded, clock, tomorrow_window
    )
    now = clock.now()

    customer = (
        await session.execute(
            select(m.Customer).where(m.Customer.id == seeded.customer_ids[0])
        )
    ).scalar_one()
    customer.protected_contact_ref = None
    await session.commit()

    provider = SandboxProvider()
    worker = _worker(settings, provider)
    await worker.run_once(now=now)

    assert await _deliveries(session, seeded.tenant_id) == []
    assert provider.calls == []

    alert = (await _jobs(session, seeded.tenant_id, JOB_ALERT_MANUAL))[0]
    assert alert.payload["reason"] == "NO_RECIPIENT_CONTACT"
    # 人工待办不能被伪装成已完成的业务效果：这里只表示"已交人工队列"。
    assert alert.result_ref["kind"] == "MANUAL"
    assert alert.status == JobStatus.SUCCEEDED.value


async def test_no_provider_configured_consumes_outbox_without_fake_delivery(
    session, seeded, clock, tomorrow_window, settings
):
    """未接通知渠道的环境：Outbox 照常消费，但不建投递、不假装发过。"""

    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    now = clock.now()
    worker = _worker(settings, None)

    report = await worker.run_once(now=now)

    assert report.outbox_claimed == 1
    assert report.outbox_processed == 1
    assert report.deliveries_sent == 0
    assert await _deliveries(session, seeded.tenant_id) == []
    job_kinds = {
        row.kind
        for row in (
            await session.execute(_reload(select(m.Job)))
        ).scalars()
    }
    assert JOB_NOTIFICATION_SEND not in job_kinds
