"""Worker：Outbox 消费、Job 调度、投递与对账、过期占位回收。

一条纪律贯穿全文件：**每个工作单元在自己的事务里提交**。一个 job 失败不能把同批
其他 job 回滚掉，否则"至少一次"会退化成"一有异常就整批重来"，而整批重来又会放大
投递重复的概率。

三类待办的职责边界（数据契约 §6）：

- **outbox**：把业务事件翻译成"该做什么"。消费成功只写 ``PROCESSED``，
  含义是"逻辑投递已持久受理"，不是"用户已收到通知"。
- **job**：调度。带租约、fencing token、``next_run_at``、``attempt_count``。
- **notification_delivery**：投递事实与证据。只由 ``delivery`` 模块改状态。

刻意不做的事：不把 ``UNKNOWN`` 统一重跑（设计稿 §11：UNKNOWN 必须按该 job 的
业务类别查证），不因为投递失败回滚已确认订单，不把"没有联系方式"当成"发过了"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import JobStatus, OutboxStatus
from ..core.ids import new_id
from ..db import models as m
from ..db.session import db_now, get_sessionmaker
from ..domain.booking import (
    expire_due_allocations,
    expire_due_holds,
    expire_open_waitings,
)
from .claims import (
    Lease,
    LeaseLost,
    claim_jobs,
    claim_outbox,
    finish_job,
    finish_outbox,
)
from .delivery import attempt_delivery, enqueue_delivery, reconcile_delivery
from .providers import NotificationProviderPort
from .receipts import reprocess_quarantined

#: job 类型。调度逻辑按 kind 分派，不用 payload 里的字符串做分支。
JOB_NOTIFICATION_SEND = "notification.send"
JOB_NOTIFICATION_RECONCILE = "notification.reconcile"
JOB_ALERT_MANUAL = "alert.manual_followup"

#: Outbox 事件类型 → 通知模板版本。模板版本进逻辑投递唯一键，
#: 所以改文案就是要新建模板版本，而不是复用旧键改内容。
TEMPLATE_BY_EVENT: dict[str, str] = {
    "appointment_confirmed": "appointment_confirmed.v1",
    "appointment_updated": "appointment_updated.v1",
    "appointment_cancelled": "appointment_cancelled.v1",
    "appointment_reminder": "appointment_reminder.v1",
}

#: 参与调度的 job 类型。不在表里的 kind 会被明确判为不可执行而不是静默卡住。
KNOWN_JOB_KINDS = frozenset(
    {JOB_NOTIFICATION_SEND, JOB_NOTIFICATION_RECONCILE, JOB_ALERT_MANUAL}
)

RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 900


@dataclass
class WorkerReport:
    """一轮执行的观测结果。用于健康检查与测试断言，不落库。"""

    reclaimed: dict[str, int] = field(default_factory=dict)
    outbox_claimed: int = 0
    outbox_processed: int = 0
    outbox_dead_letter: int = 0
    outbox_lease_lost: int = 0
    jobs_claimed: int = 0
    jobs_succeeded: int = 0
    jobs_rescheduled: int = 0
    jobs_failed: int = 0
    jobs_lease_lost: int = 0
    deliveries_sent: int = 0
    deliveries_reconciled: int = 0
    escalations: int = 0
    receipts_reprocessed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "reclaimed": dict(self.reclaimed),
            "outbox_claimed": self.outbox_claimed,
            "outbox_processed": self.outbox_processed,
            "outbox_dead_letter": self.outbox_dead_letter,
            "outbox_lease_lost": self.outbox_lease_lost,
            "jobs_claimed": self.jobs_claimed,
            "jobs_succeeded": self.jobs_succeeded,
            "jobs_rescheduled": self.jobs_rescheduled,
            "jobs_failed": self.jobs_failed,
            "jobs_lease_lost": self.jobs_lease_lost,
            "deliveries_sent": self.deliveries_sent,
            "deliveries_reconciled": self.deliveries_reconciled,
            "escalations": self.escalations,
            "receipts_reprocessed": self.receipts_reprocessed,
        }


def _backoff(attempt_count: int) -> int:
    """指数退避，带上限。

    上限是必须的：无限增长的退避会让一条投递在几分钟内就"不再被调度"，
    而那看起来和"已经处理完了"没有区别。
    """

    if attempt_count <= 1:
        return RETRY_BASE_SECONDS
    return min(RETRY_BASE_SECONDS * (2 ** min(attempt_count - 1, 5)), RETRY_MAX_SECONDS)


class Worker:
    """可重入的 Worker。同一个实例可以被多个进程同时跑（靠租约与 SKIP LOCKED）。"""

    def __init__(
        self,
        *,
        owner: str,
        settings,
        provider: NotificationProviderPort | None,
        session_factory=None,
    ) -> None:
        self.owner = owner
        self.settings = settings
        self.provider = provider
        self._session_factory = session_factory

    @property
    def session_factory(self):
        if self._session_factory is None:
            self._session_factory = get_sessionmaker()
        return self._session_factory

    # ------------------------------------------------------------------
    # 一轮执行
    # ------------------------------------------------------------------
    async def run_once(
        self,
        *,
        now: datetime | None = None,
        reclaim: bool = True,
        outbox: bool = True,
        jobs: bool = True,
        receipts: bool = True,
    ) -> WorkerReport:
        """跑一轮。

        四个阶段可以单独关闭：一个进程只做消费、另一个只做投递，是常见的部署
        形态；测试也需要把"建投递"与"发信"分开断言。
        """

        report = WorkerReport()
        async with self.session_factory() as session:
            now = now or await db_now(session)
            tenant_ids = list(
                (await session.execute(select(m.Store.tenant_id).distinct())).scalars()
            )

        # 1) 过期占位回收。放在最前面：回收掉的行会释放区间，让后续查询与
        #    新占位看到正确的可用性。
        if reclaim:
            for tenant_id in tenant_ids:
                async with self.session_factory() as session:
                    report.reclaimed[str(tenant_id)] = await self._reclaim(
                        session, tenant_id=tenant_id, now=now
                    )

        # 2) Outbox：把业务事件翻译成待办。
        if outbox:
            report.outbox_claimed, processed, dead, lost = await self._consume_outbox(
                now=now
            )
            report.outbox_processed = processed
            report.outbox_dead_letter = dead
            report.outbox_lease_lost = lost

        # 3) Job：执行待办。
        if jobs:
            outcome = await self._run_jobs(now=now)
            report.jobs_claimed = outcome["claimed"]
            report.jobs_succeeded = outcome["succeeded"]
            report.jobs_rescheduled = outcome["rescheduled"]
            report.jobs_failed = outcome["failed"]
            report.jobs_lease_lost = outcome["lease_lost"]
            report.deliveries_sent = outcome["sent"]
            report.deliveries_reconciled = outcome["reconciled"]
            report.escalations = outcome["escalations"]

        # 4) 隔离回执重处理：同步响应可能刚刚建立了映射。
        if receipts:
            async with self.session_factory() as session:
                report.receipts_reprocessed = len(await reprocess_quarantined(session))
                await session.commit()

        return report

    # ------------------------------------------------------------------
    # 回收
    # ------------------------------------------------------------------
    async def _reclaim(
        self, session: AsyncSession, *, tenant_id, now: datetime
    ) -> int:
        freed = await expire_due_allocations(session, tenant_id=tenant_id, now=now)
        holds = await expire_due_holds(session, tenant_id=tenant_id, now=now)
        await expire_open_waitings(session, tenant_id=tenant_id, now=now)
        await session.commit()
        return freed + holds

    # ------------------------------------------------------------------
    # Outbox 消费
    # ------------------------------------------------------------------
    async def _consume_outbox(self, *, now: datetime) -> tuple[int, int, int, int]:
        async with self.session_factory() as session:
            claimed = await claim_outbox(
                session,
                owner=self.owner,
                now=now,
                lease_seconds=self.settings.worker_lease_seconds,
                limit=self.settings.worker_batch_size,
            )
            await session.commit()

        processed = dead = lost = 0
        for event, lease in claimed:
            try:
                async with self.session_factory() as session:
                    row = await session.get(m.Outbox, event.id)
                    await self._handle_outbox_event(session, row, now=now)
                    ok = await finish_outbox(
                        session,
                        event_id=event.id,
                        lease=lease,
                        status=OutboxStatus.PROCESSED,
                    )
                    await session.commit()
                if ok:
                    processed += 1
                else:
                    lost += 1
            except LeaseLost:
                lost += 1
            except Exception as exc:  # noqa: BLE001 - 单个事件失败不能拖垮整批
                async with self.session_factory() as session:
                    await self._fail_outbox(
                        session, event=event, lease=lease, now=now, error=exc
                    )
                    await session.commit()
                dead += 1
        return len(claimed), processed, dead, lost

    async def _fail_outbox(self, session, *, event, lease, now, error) -> None:
        """重排或进死信。

        超过上限进 ``DEAD_LETTER`` 而不是无限重试：Outbox 里的失败几乎都是
        "代码不认识这个事件"或"数据不自洽"，重试改变不了结论，只会淹没日志。
        """

        row = await session.get(m.Outbox, event.id)
        if row is None:
            return
        if row.attempt_count >= self.settings.outbox_max_attempts:
            await finish_outbox(
                session,
                event_id=event.id,
                lease=lease,
                status=OutboxStatus.DEAD_LETTER,
            )
            return
        # 重排：回到待处理并把 available_at 往后推。整条 UPDATE 带上 fence，
        # 所有权已被别人拿走时不要动它。
        result = await session.execute(
            sa_update(m.Outbox)
            .where(
                m.Outbox.id == event.id,
                m.Outbox.lease_owner == lease.owner,
                m.Outbox.fencing_token == lease.token,
            )
            .values(
                status=OutboxStatus.PENDING.value,
                lease_owner=None,
                lease_until=None,
                available_at=now + timedelta(seconds=_backoff(row.attempt_count)),
            )
        )
        if not result.rowcount:
            raise LeaseLost(f"outbox {event.id} 的所有权已转移")

    async def _handle_outbox_event(
        self, session: AsyncSession, event: m.Outbox, *, now: datetime
    ) -> None:
        """把一个业务事件翻译成待办。

        只做翻译与建待办，不发信：Outbox 被消费 ≠ 用户收到通知。
        """

        payload = dict(event.payload or {})
        channel = str(payload.get("channel") or "sms")
        template_version = TEMPLATE_BY_EVENT.get(event.event_type)
        if template_version is None:
            raise ValueError(f"未知的 outbox 事件类型：{event.event_type}")

        appointment_id = payload.get("appointment_id")
        customer_id = payload.get("customer_id")
        if appointment_id is None:
            raise ValueError("outbox 事件缺少 appointment_id")

        if self.provider is None:
            # 本环境不接通知渠道：**不建投递**，也不假装发过。
            return

        recipient_ref = await self._recipient_ref(
            session, tenant_id=event.tenant_id, customer_id=customer_id
        )
        if not recipient_ref:
            # 没有可用联系方式不是"已通知"。留一条人工待办，让这件事可见。
            await self._enqueue_alert(
                session,
                tenant_id=event.tenant_id,
                task_id=None,
                logical_key=f"no-recipient:{event.id}",
                now=now,
                detail={
                    "reason": "NO_RECIPIENT_CONTACT",
                    "appointment_id": str(appointment_id),
                },
            )
            return

        appointment_version = await self._appointment_version(
            session, tenant_id=event.tenant_id, appointment_id=appointment_id
        )
        delivery, created = await enqueue_delivery(
            session,
            tenant_id=event.tenant_id,
            source_event_id=event.id,
            appointment_id=appointment_id,
            # 记下**事件受理时**的订单版本。发送前会再比一次，用来识别过时提醒。
            appointment_version=appointment_version,
            channel=channel,
            recipient_ref=recipient_ref,
            template_version=template_version,
            provider=self.provider.name,
            provider_account_id=self.provider.account_id,
            payload={
                **payload,
                "template_version": template_version,
                "appointment_version": appointment_version,
            },
        )
        await self._enqueue_job(
            session,
            tenant_id=event.tenant_id,
            kind=JOB_NOTIFICATION_SEND,
            logical_key=f"delivery:{delivery.id}",
            now=now,
            deadline_at=now + timedelta(days=1),
            payload={"delivery_id": str(delivery.id)},
        )

    async def _recipient_ref(
        self, session: AsyncSession, *, tenant_id, customer_id
    ) -> str | None:
        if customer_id is None:
            return None
        return (
            await session.execute(
                select(m.Customer.protected_contact_ref).where(
                    m.Customer.tenant_id == tenant_id,
                    m.Customer.id == customer_id,
                )
            )
        ).scalar_one_or_none()

    async def _appointment_version(
        self, session: AsyncSession, *, tenant_id, appointment_id
    ) -> int:
        version = (
            await session.execute(
                select(m.Appointment.version).where(
                    m.Appointment.tenant_id == tenant_id,
                    m.Appointment.id == appointment_id,
                )
            )
        ).scalar_one_or_none()
        return int(version or 0)

    # ------------------------------------------------------------------
    # Job 执行
    # ------------------------------------------------------------------
    async def _run_jobs(self, *, now: datetime) -> dict[str, int]:
        async with self.session_factory() as session:
            claimed = await claim_jobs(
                session,
                owner=self.owner,
                now=now,
                lease_seconds=self.settings.worker_lease_seconds,
                limit=self.settings.worker_batch_size,
            )
            await session.commit()

        counters = {
            "claimed": len(claimed),
            "succeeded": 0,
            "rescheduled": 0,
            "failed": 0,
            "lease_lost": 0,
            "sent": 0,
            "reconciled": 0,
            "escalations": 0,
        }
        for job, lease in claimed:
            try:
                await self._run_job(job=job, lease=lease, now=now, counters=counters)
            except LeaseLost:
                counters["lease_lost"] += 1
        return counters

    async def _run_job(
        self, *, job: m.Job, lease: Lease, now: datetime, counters: dict[str, int]
    ) -> None:
        if job.kind == JOB_NOTIFICATION_SEND:
            await self._run_send(job=job, lease=lease, now=now, counters=counters)
            return
        if job.kind == JOB_NOTIFICATION_RECONCILE:
            await self._run_reconcile(job=job, lease=lease, now=now, counters=counters)
            return
        if job.kind == JOB_ALERT_MANUAL:
            # 人工待办本身就是产出：调度器只需把它从队列里取走并留痕，
            # 不该假装"处理完成"把人工步骤变成绿色的。
            async with self.session_factory() as session:
                ok = await finish_job(
                    session,
                    job_id=job.id,
                    lease=lease,
                    status=JobStatus.SUCCEEDED,
                    result_ref={"kind": "MANUAL", "note": "已交人工处理队列"},
                )
                await session.commit()
            counters["succeeded" if ok else "lease_lost"] += 1
            counters["escalations"] += 1
            return

        # 不认识的 kind：明确失败并留原因，避免它每轮都被重新领取。
        async with self.session_factory() as session:
            ok = await finish_job(
                session,
                job_id=job.id,
                lease=lease,
                status=JobStatus.FAILED_FINAL,
                last_error=f"未知的 job 类型：{job.kind}",
            )
            await session.commit()
        counters["failed" if ok else "lease_lost"] += 1

    async def _run_send(
        self, *, job: m.Job, lease: Lease, now: datetime, counters: dict[str, int]
    ) -> None:
        """发送 job 只做一件事：把这条投递交给供应商，然后如实记账。

        **推进状态不是它的职责**：受理之后能不能变成"送达"要看证据，那是对账
        job 的事。把两件事混在一个 job 里，就会为了等证据而占着租约，或者为了
        释放租约而把"未确认"草草收尾。
        """

        delivery_id = _uuid(job.payload.get("delivery_id"))
        if delivery_id is None:
            async with self.session_factory() as session:
                ok = await finish_job(
                    session,
                    job_id=job.id,
                    lease=lease,
                    status=JobStatus.FAILED_FINAL,
                    last_error="job payload 缺少 delivery_id",
                )
                await session.commit()
            counters["failed" if ok else "lease_lost"] += 1
            return

        result = await attempt_delivery(
            self.session_factory,
            self.provider,
            tenant_id=job.tenant_id,
            delivery_id=delivery_id,
            now=now,
            max_attempts=job.max_attempts,
            reconcile_deadline_seconds=self.settings.delivery_reconcile_deadline_seconds,
            retry_delay_seconds=_backoff(job.attempt_count),
        )

        if result.action == "FAILED" and job.deadline_at is not None and now >= job.deadline_at:
            async with self.session_factory() as session:
                ok = await self._escalate(
                    session,
                    job=job,
                    lease=lease,
                    now=now,
                    reason="DELIVERY_DEADLINE_EXCEEDED",
                    detail={"delivery_id": str(delivery_id)},
                )
                await session.commit()
            counters["escalations" if ok else "lease_lost"] += 1
            return

        if result.status == "FAILED_RETRYABLE":
            # 可重试失败：**重排同一个 job**（job 表本身就是调度器），
            # 不需要另建待办，attempt_count 继续累积直到 max_attempts。
            retry_at = result.retry_at or now + timedelta(seconds=_backoff(job.attempt_count))
            async with self.session_factory() as session:
                ok = await finish_job(
                    session,
                    job_id=job.id,
                    lease=lease,
                    status=JobStatus.FAILED_RETRYABLE,
                    next_run_at=retry_at,
                    last_error=result.detail.get("error_code") if result.detail else None,
                    result_ref={"delivery_id": str(delivery_id), "status": result.status},
                )
                await session.commit()
            counters["rescheduled" if ok else "lease_lost"] += 1
            return

        if result.action == "FAILED":
            async with self.session_factory() as session:
                ok = await finish_job(
                    session,
                    job_id=job.id,
                    lease=lease,
                    status=JobStatus.FAILED_FINAL,
                    last_error="达到最大尝试次数",
                    result_ref={"delivery_id": str(delivery_id)},
                )
                await session.commit()
            counters["failed" if ok else "lease_lost"] += 1
            return

        # SENT / REPLAYED / SKIPPED / SUPERSEDED / RECONCILED：这一轮发送职责已尽。
        async with self.session_factory() as session:
            ok = await finish_job(
                session,
                job_id=job.id,
                lease=lease,
                status=JobStatus.SUCCEEDED,
                result_ref={
                    "delivery_id": str(delivery_id),
                    "status": result.status,
                    "action": result.action,
                },
            )
            await session.commit()
        if not ok:
            counters["lease_lost"] += 1
            return
        counters["succeeded"] += 1
        if result.action in ("SENT", "REPLAYED"):
            counters["sent"] += 1

        if result.action == "SUPERSEDED":
            # 过时提醒被跳过，但更正投递必须真的被调度，否则"改约后发新消息"
            # 只是日志里的一句话。
            if result.correction_delivery_id is not None:
                async with self.session_factory() as session:
                    await self._enqueue_job(
                        session,
                        tenant_id=job.tenant_id,
                        kind=JOB_NOTIFICATION_SEND,
                        logical_key=f"delivery:{result.correction_delivery_id}",
                        now=now,
                        deadline_at=now + timedelta(days=1),
                        payload={"delivery_id": str(result.correction_delivery_id)},
                    )
                    await session.commit()
            return

        if result.status in ("ACCEPTED", "UNKNOWN", "SENDING"):
            await self._schedule_reconcile(
                delivery_id=delivery_id,
                tenant_id=job.tenant_id,
                at=result.retry_at or now + timedelta(seconds=RETRY_BASE_SECONDS),
                now=now,
            )

    async def _run_reconcile(
        self, *, job: m.Job, lease: Lease, now: datetime, counters: dict[str, int]
    ) -> None:
        delivery_id = _uuid(job.payload.get("delivery_id"))
        if delivery_id is None:
            async with self.session_factory() as session:
                ok = await finish_job(
                    session,
                    job_id=job.id,
                    lease=lease,
                    status=JobStatus.FAILED_FINAL,
                    last_error="job payload 缺少 delivery_id",
                )
                await session.commit()
            counters["failed" if ok else "lease_lost"] += 1
            return

        result = await reconcile_delivery(
            self.session_factory,
            self.provider,
            tenant_id=job.tenant_id,
            delivery_id=delivery_id,
            now=now,
        )
        counters["reconciled"] += 1

        if result.action == "ESCALATED":
            async with self.session_factory() as session:
                ok = await self._escalate(
                    session,
                    job=job,
                    lease=lease,
                    now=now,
                    reason="RECONCILE_DEADLINE_EXCEEDED",
                    detail={"delivery_id": str(delivery_id)},
                )
                await session.commit()
            counters["escalations" if ok else "lease_lost"] += 1
            return

        if result.status == "FAILED_RETRYABLE":
            # 查单明确说供应商没有这条消息：这时重发是安全的。走**新的发送
            # job**，让"第几次尝试"重新计数，而不是让对账 job 兼职发送。
            async with self.session_factory() as session:
                ok = await finish_job(
                    session,
                    job_id=job.id,
                    lease=lease,
                    status=JobStatus.SUCCEEDED,
                    result_ref={"action": "RESEND_ALLOWED"},
                )
                await session.commit()
            if not ok:
                counters["lease_lost"] += 1
                return
            counters["succeeded"] += 1
            async with self.session_factory() as session:
                await self._enqueue_job(
                    session,
                    tenant_id=job.tenant_id,
                    kind=JOB_NOTIFICATION_SEND,
                    logical_key=f"delivery:{delivery_id}",
                    now=now,
                    deadline_at=now + timedelta(days=1),
                    payload={"delivery_id": str(delivery_id)},
                    reset_if_exists=True,
                )
                await session.commit()
            return

        if result.retry_at is not None and result.status in (
            "UNKNOWN",
            "SENDING",
            "ACCEPTED",
        ):
            async with self.session_factory() as session:
                ok = await finish_job(
                    session,
                    job_id=job.id,
                    lease=lease,
                    status=JobStatus.FAILED_RETRYABLE,
                    next_run_at=result.retry_at,
                    result_ref={"delivery_id": str(delivery_id), "status": result.status},
                )
                await session.commit()
            counters["rescheduled" if ok else "lease_lost"] += 1
            return

        async with self.session_factory() as session:
            ok = await finish_job(
                session,
                job_id=job.id,
                lease=lease,
                status=JobStatus.SUCCEEDED,
                result_ref={"delivery_id": str(delivery_id), "status": result.status},
            )
            await session.commit()
        counters["succeeded" if ok else "lease_lost"] += 1

    async def _schedule_reconcile(
        self, *, delivery_id, tenant_id, at: datetime, now: datetime
    ) -> None:
        if self.provider is None or not self.provider.supports_query:
            # 没有查单能力的渠道：停在 ACCEPTED 并明确证据边界，
            # 安排对账只会产生永远无法解决的任务。
            return
        async with self.session_factory() as session:
            await self._enqueue_job(
                session,
                tenant_id=tenant_id,
                kind=JOB_NOTIFICATION_RECONCILE,
                logical_key=f"delivery:{delivery_id}",
                now=at,
                deadline_at=now + timedelta(
                    seconds=self.settings.delivery_reconcile_deadline_seconds
                ),
                payload={"delivery_id": str(delivery_id)},
                # 上一轮对账可能已经 SUCCEEDED（例如先确认受理、后又需要
                # 确认送达）。不复位就永远等不到第二轮查证。
                reset_if_exists=True,
            )
            await session.commit()

    async def _escalate(
        self, session, *, job: m.Job, lease: Lease, now: datetime, reason: str, detail: dict
    ) -> bool:
        """转人工：**不把未知改成失败**，也不自动重发（设计稿 §11.2）。"""

        ok = await finish_job(
            session,
            job_id=job.id,
            lease=lease,
            status=JobStatus.FAILED_FINAL,
            last_error=reason,
            result_ref={**detail, "escalated": True},
        )
        if not ok:
            return False
        await self._enqueue_alert(
            session,
            tenant_id=job.tenant_id,
            task_id=job.task_id,
            logical_key=f"escalate:{job.id}",
            now=now,
            detail={**detail, "reason": reason, "source_job_id": str(job.id)},
        )
        return True

    # ------------------------------------------------------------------
    # Job 入队
    # ------------------------------------------------------------------
    async def _enqueue_job(
        self,
        session: AsyncSession,
        *,
        tenant_id,
        kind: str,
        logical_key: str,
        now: datetime,
        payload: dict[str, Any],
        deadline_at: datetime | None = None,
        task_id=None,
        reset_if_exists: bool = False,
    ) -> m.Job | None:
        """建待办。``(tenant_id, kind, logical_key)`` 唯一，重复入队只产生一个。

        ``reset_if_exists`` 用于"同一条逻辑待办要再来一轮"：
        ``logical_key`` 表达的是"这是哪个业务待办"，而不是"第几次尝试"，所以重复
        触发不应该造出第二行，也不能因为上一轮已经 SUCCEEDED 就永远不再跑。
        复位时要求当前没有租约——正在被别的 Worker 处理的 job 不能被悄悄改状态。
        """

        existing = (
            await session.execute(
                select(m.Job.id).where(
                    m.Job.tenant_id == tenant_id,
                    m.Job.kind == kind,
                    m.Job.logical_key == logical_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not reset_if_exists:
                return None
            await session.execute(
                sa_update(m.Job)
                .where(
                    m.Job.id == existing,
                    m.Job.lease_owner.is_(None),
                    m.Job.lease_until.is_(None),
                )
                .values(
                    status=JobStatus.PENDING.value,
                    next_run_at=now,
                    attempt_count=0,
                    max_attempts=self.settings.outbox_max_attempts,
                    deadline_at=deadline_at,
                    payload=payload,
                    last_error=None,
                    result_ref=None,
                )
            )
            await session.flush()
            return None

        job = m.Job(
            id=new_id(),
            tenant_id=tenant_id,
            task_id=task_id,
            kind=kind,
            logical_key=logical_key,
            payload=payload,
            status=JobStatus.PENDING.value,
            next_run_at=now,
            max_attempts=self.settings.outbox_max_attempts,
            deadline_at=deadline_at,
        )
        session.add(job)
        await session.flush()
        return job

    async def _enqueue_alert(
        self,
        session: AsyncSession,
        *,
        tenant_id,
        task_id,
        logical_key: str,
        now: datetime,
        detail: dict[str, Any],
    ) -> None:
        await self._enqueue_job(
            session,
            tenant_id=tenant_id,
            kind=JOB_ALERT_MANUAL,
            logical_key=logical_key,
            now=now,
            payload=detail,
            task_id=task_id,
        )


def _uuid(value: Any):
    from uuid import UUID

    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


__all__ = [
    "JOB_ALERT_MANUAL",
    "JOB_NOTIFICATION_RECONCILE",
    "JOB_NOTIFICATION_SEND",
    "KNOWN_JOB_KINDS",
    "TEMPLATE_BY_EVENT",
    "Worker",
    "WorkerReport",
]
