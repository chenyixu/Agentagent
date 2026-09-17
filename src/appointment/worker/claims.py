"""租约与 fencing：Worker 领取待办的唯一入口。

设计稿 §11：Worker 用数据库任务表和租约领取待办，记 ``next_run_at``、
``attempt``、``lease_until``；租约到期允许接管，但**旧执行者必须被 fencing token
挡住**——仅靠 ``lease_until`` 不足以阻止"暂停后被唤醒的旧实例"继续写入。原因是
租约到期与旧实例恢复之间没有因果关系：旧实例可能在一分钟后醒来，手里拿着的仍是
它以为有效的租约。

因此这里所有领取都走同一条协议：

1. 领取时 ``fencing_token = fencing_token + 1``，把新令牌交给新持有者；
2. 任何归还/提交都必须带上 ``(owner, token)``，不匹配就是 ``LEASE_LOST``；
3. 归还被拒时**不抛业务错误**，而是让调用方放弃这次结果——因为正确的语义是
   "这次执行的所有权已经不在我手里了"，而不是"这次业务操作失败了"。

领取本身用 ``FOR UPDATE SKIP LOCKED``：多个 Worker 同时跑不会互相等待，也不会
重复领取同一行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import JobStatus, OutboxStatus
from ..db import models as m


@dataclass(frozen=True, slots=True)
class Lease:
    """一次领取的凭据。token 是 fencing token，不是普通的乐观锁版本。"""

    owner: str
    token: int
    until: datetime
    seconds: int = 30

    def renew(self, *, now: datetime, seconds: int | None = None) -> "Lease":
        span = seconds or self.seconds
        return Lease(
            owner=self.owner,
            token=self.token,
            until=now + timedelta(seconds=span),
            seconds=span,
        )


class LeaseLost(Exception):  # noqa: N818 - 与领域错误区分：这不是业务失败
    """归还脏数据时发现所有权已转移。

    刻意不继承 ``DomainError``：它不该被映射成 4xx/5xx 返回给用户，只意味着
    本次执行结果应当被丢弃（另一个执行者会重新处理）。
    """


def _lease_until(now: datetime, seconds: int) -> datetime:
    return now + timedelta(seconds=seconds)


def _claimable_job(now: datetime):
    """可领取的 job：待跑/可重试且到期，或租约已过期的运行中任务。"""

    return or_(
        and_(
            m.Job.status.in_(
                (JobStatus.PENDING.value, JobStatus.FAILED_RETRYABLE.value)
            ),
            m.Job.next_run_at <= now,
        ),
        and_(
            m.Job.status == JobStatus.RUNNING.value,
            m.Job.lease_until.is_not(None),
            m.Job.lease_until <= now,
        ),
    )


def _claimable_outbox(now: datetime):
    return or_(
        and_(
            m.Outbox.status == OutboxStatus.PENDING.value,
            m.Outbox.available_at <= now,
        ),
        and_(
            m.Outbox.status == OutboxStatus.LEASED.value,
            m.Outbox.lease_until.is_not(None),
            m.Outbox.lease_until <= now,
        ),
    )


async def claim_jobs(
    session: AsyncSession,
    *,
    owner: str,
    now: datetime,
    lease_seconds: int,
    limit: int,
    kinds: Sequence[str] | None = None,
) -> list[tuple[m.Job, Lease]]:
    """领取一批到期的 job，返回 ``(行, 租约)``。

    ``attempt_count`` 在**领取时**自增而不是完成时：进程被杀也要留下尝试痕迹，
    否则一个必崩的任务会被无限重领。
    """

    where = _claimable_job(now)
    candidates = (
        select(m.Job.id)
        .where(where)
        .order_by(m.Job.next_run_at, m.Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .subquery()
    )
    stmt = (
        update(m.Job)
        .where(m.Job.id.in_(select(candidates.c.id)))
        .values(
            status=JobStatus.RUNNING.value,
            lease_owner=owner,
            lease_until=_lease_until(now, lease_seconds),
            fencing_token=m.Job.fencing_token + 1,
            attempt_count=m.Job.attempt_count + 1,
        )
        .returning(m.Job.id, m.Job.fencing_token)
    )
    if kinds is not None:
        stmt = stmt.where(m.Job.kind.in_(tuple(kinds)))
    claimed = (await session.execute(stmt)).all()
    if not claimed:
        return []

    tokens = {row.id: row.fencing_token for row in claimed}
    rows = (
        await session.execute(select(m.Job).where(m.Job.id.in_(tokens)))
    ).scalars().all()
    until = _lease_until(now, lease_seconds)
    return [
        (
            row,
            Lease(
                owner=owner, token=tokens[row.id], until=until, seconds=lease_seconds
            ),
        )
        for row in rows
    ]


async def claim_outbox(
    session: AsyncSession,
    *,
    owner: str,
    now: datetime,
    lease_seconds: int,
    limit: int,
) -> list[tuple[m.Outbox, Lease]]:
    """领取一批 Outbox 事件。语义与 :func:`claim_jobs` 一致。"""

    candidates = (
        select(m.Outbox.id)
        .where(_claimable_outbox(now))
        .order_by(m.Outbox.available_at, m.Outbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .subquery()
    )
    claimed = (
        await session.execute(
            update(m.Outbox)
            .where(m.Outbox.id.in_(select(candidates.c.id)))
            .values(
                status=OutboxStatus.LEASED.value,
                lease_owner=owner,
                lease_until=_lease_until(now, lease_seconds),
                fencing_token=m.Outbox.fencing_token + 1,
                attempt_count=m.Outbox.attempt_count + 1,
            )
            .returning(m.Outbox.id, m.Outbox.fencing_token)
        )
    ).all()
    if not claimed:
        return []

    tokens = {row.id: row.fencing_token for row in claimed}
    rows = (
        await session.execute(select(m.Outbox).where(m.Outbox.id.in_(tokens)))
    ).scalars().all()
    until = _lease_until(now, lease_seconds)
    return [
        (
            row,
            Lease(
                owner=owner, token=tokens[row.id], until=until, seconds=lease_seconds
            ),
        )
        for row in rows
    ]


async def finish_outbox(
    session: AsyncSession,
    *,
    event_id: UUID,
    lease: Lease,
    status: OutboxStatus,
) -> bool:
    """按租约归还 Outbox。返回 False 表示所有权已转移（``LeaseLost`` 语义）。"""

    result = await session.execute(
        update(m.Outbox)
        .where(
            m.Outbox.id == event_id,
            m.Outbox.lease_owner == lease.owner,
            m.Outbox.fencing_token == lease.token,
        )
        .values(
            status=status.value,
            lease_owner=None,
            lease_until=None,
        )
    )
    return bool(result.rowcount)


async def finish_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    lease: Lease,
    status: JobStatus,
    next_run_at: datetime | None = None,
    last_error: str | None = None,
    result_ref: dict[str, Any] | None = None,
) -> bool:
    """按租约结束/重排一个 job。

    重排（``FAILED_RETRYABLE`` + ``next_run_at``）而不是立刻置失败：投递类待办的
    重试是**调度问题**，不是业务失败，业务事实早已提交。

    ``next_run_at`` 只在显式给出时才写：它是非空列，"没给"和"置空"必须区分开。
    """

    values: dict[str, Any] = {
        "status": status.value,
        "lease_owner": None,
        "lease_until": None,
        "last_error": last_error,
        "result_ref": result_ref,
    }
    if next_run_at is not None:
        values["next_run_at"] = next_run_at

    result = await session.execute(
        update(m.Job)
        .where(
            m.Job.id == job_id,
            m.Job.lease_owner == lease.owner,
            m.Job.fencing_token == lease.token,
        )
        .values(**values)
    )
    return bool(result.rowcount)


async def renew_job_lease(
    session: AsyncSession,
    *,
    job_id: UUID,
    lease: Lease,
    now: datetime,
    lease_seconds: int,
) -> Lease:
    """延长租约。长耗时步骤（调用供应商）之前调用。

    不改变 fencing_token：续租不转移所有权。
    """

    until = _lease_until(now, lease_seconds)
    result = await session.execute(
        update(m.Job)
        .where(
            m.Job.id == job_id,
            m.Job.lease_owner == lease.owner,
            m.Job.fencing_token == lease.token,
        )
        .values(lease_until=until)
    )
    if not result.rowcount:
        raise LeaseLost(f"job {job_id} 的租约已转移（owner={lease.owner}）")
    return Lease(
        owner=lease.owner, token=lease.token, until=until, seconds=lease_seconds
    )


__all__ = [
    "Lease",
    "LeaseLost",
    "claim_jobs",
    "claim_outbox",
    "finish_job",
    "finish_outbox",
    "renew_job_lease",
]
