"""SSE 事件流：持久事件回放 + 游标恢复 + 保留窗口。

数据契约 §2.3 的 SSE 契约：

- 帧内含 ``event_id``（= task_event.sequence）、``task_id``、``task_version``、
  ``type``、``occurred_at``、``schema_version``、``payload``；
- 客户端按 ``sequence`` 去重，并携带恢复游标（``Last-Event-ID`` 或查询参数）；
- **保留窗口外返回 RESET_REQUIRED**，客户端先拉任务快照再重新订阅。

三个刻意的取舍：

1. **不持有长事务**。每次轮询开一个短会话，读完即提交关闭；SSE 连接本身不占
   数据库事务。这是"不在事务里等待外部"的直接推论。
2. **订阅是"追平 + 等到本次执行结束"**：回放到 ``reply_end`` 就关闭连接；
   如果订阅时已经追平（且尾部就是 ``reply_end``），立刻回一帧 ``caught_up`` 结束。
   下一次消息提交会给出新的游标，客户端据此重新订阅；断线不代表业务取消。
3. **进程内总线只是加速信号**，权威是 ``task_event`` 表：所以这里只读表。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import TaskEventType
from ..core.errors import SCHEMA_VERSION
from ..db import models as m

#: 轮询间隔。事件表是权威来源，这里只决定"多快看到"。
POLL_INTERVAL_SECONDS = 0.5

#: 单次回放上限，避免一个游标落后很多时一次性拉爆内存。
REPLAY_BATCH = 200

#: 不能作为业务事件交付的内部事件类型由调用方过滤；这里只做形状转换。
@dataclass(frozen=True, slots=True)
class EventFrame:
    event_id: UUID
    sequence: int
    type: str
    task_version: int
    occurred_at: datetime
    payload: dict[str, Any]

    def envelope(self, *, task_id: UUID) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "task_id": str(task_id),
            "sequence": self.sequence,
            "task_version": self.task_version,
            "type": self.type,
            "occurred_at": self.occurred_at.isoformat(),
            "schema_version": SCHEMA_VERSION,
            "payload": self.payload,
        }


def to_frame(row: m.TaskEvent) -> EventFrame:
    return EventFrame(
        event_id=row.id,
        sequence=row.sequence,
        type=row.type,
        task_version=row.task_version,
        occurred_at=row.occurred_at,
        payload=dict(row.payload or {}),
    )


def format_sse(*, event: str, data: dict[str, Any], event_id: str | None = None) -> str:
    """SSE 帧文本。

    标准 ``id:`` 字段让浏览器的 EventSource 在重连时自动带上 ``Last-Event-ID``，
    因此客户端不需要自己记住游标（但显式传游标同样支持）。
    """

    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False))
    return "\n".join(lines) + "\n\n"


async def latest_sequence(
    session: AsyncSession, *, tenant_id: UUID, task_id: UUID
) -> int:
    row = (
        await session.execute(
            select(m.TaskEvent.sequence)
            .where(m.TaskEvent.tenant_id == tenant_id, m.TaskEvent.task_id == task_id)
            .order_by(m.TaskEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(row or 0)


async def retained_from_sequence(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    task_id: UUID,
    retention_events: int,
    retention_seconds: int,
    now: datetime,
) -> int:
    """仍然保证可回放的最小 sequence。

    保留窗口有**两个维度**：条数与时间。两者都满足才算"还在窗口内"，
    因此有效下界取两者中更晚的那个。这样即使不做物理清理，也能如实告诉
    客户端"你落后太多了，请重新拉快照"。

    **时间维度的锚点取 ``min(now, 流头部事件时刻)``，而不是 ``now``。** 事件是由
    应用侧时钟盖章的（编排器的 ``now``），而 ``now`` 参数是裁决用的数据库时钟；
    两个时钟可能漂移（容器/虚拟机休眠后尤其明显）。若直接以 ``now`` 为锚，
    一个领先的数据库时钟会让**整条流**都落在窗口之外，于是每次订阅都返回
    RESET_REQUIRED——那不是保留策略生效，而是时钟故障被当成了业务结论。
    取 ``min`` 之后，漂移最多裁掉"头部之前"的历史事件，永远不会裁掉头部，
    因此"落后很多"仍然会被如实识别，而"时钟偏差"不会再冒充"数据过期"。
    """

    rows = (
        await session.execute(
            select(m.TaskEvent.sequence, m.TaskEvent.occurred_at)
            .where(m.TaskEvent.tenant_id == tenant_id, m.TaskEvent.task_id == task_id)
            .order_by(m.TaskEvent.sequence.desc())
            .limit(max(retention_events, 1))
        )
    ).all()
    if not rows:
        return 1

    seq_bound = min(sequence for sequence, _ in rows)
    head_occurred_at = max(occurred_at for _, occurred_at in rows)
    anchor = min(now, head_occurred_at)
    time_cutoff = anchor - timedelta(seconds=max(retention_seconds, 0))
    # 锚点不大于头部时刻、窗口非负，所以头部事件必然在窗口内：fresh 不会为空。
    time_bound = min(
        sequence for sequence, occurred_at in rows if occurred_at >= time_cutoff
    )
    return max(seq_bound, time_bound)


async def load_events_after(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    task_id: UUID,
    after_sequence: int,
    limit: int = REPLAY_BATCH,
) -> list[m.TaskEvent]:
    return list(
        (
            await session.execute(
                select(m.TaskEvent)
                .where(
                    m.TaskEvent.tenant_id == tenant_id,
                    m.TaskEvent.task_id == task_id,
                    m.TaskEvent.sequence > after_sequence,
                )
                .order_by(m.TaskEvent.sequence)
                .limit(limit)
            )
        ).scalars()
    )


async def stream_task_events(
    session_factory,
    *,
    tenant_id: UUID,
    task_id: UUID,
    after_sequence: int,
    retention_events: int,
    retention_seconds: int,
    now_provider,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    max_polls: int | None = None,
) -> AsyncIterator[str]:
    """按游标回放并跟到本次执行结束。

    ``now_provider`` 是一个 ``async (session) -> datetime``：保留窗口按时间裁剪，
    因此裁决时刻应取数据库时钟（``clock_timestamp()``），不能用应用进程时间。
    它与事件盖章时钟的偏差由 :func:`retained_from_sequence` 的锚点钳制兜住。

    ``max_polls`` 仅用于有界退出（例如端到端测试）：为 ``None`` 时持续跟随。
    """

    cursor = max(after_sequence, 0)
    polls = 0
    while True:
        polls += 1
        replay: list[tuple[EventFrame, str]] = []
        tail = 0
        async with session_factory() as session:
            exists = (
                await session.execute(
                    select(m.Task.id).where(
                        m.Task.tenant_id == tenant_id, m.Task.id == task_id
                    )
                )
            ).one_or_none()
            if exists is None:
                yield format_sse(
                    event="error",
                    data={"code": "NOT_FOUND", "message": "任务不存在或无权访问"},
                )
                return

            now = await now_provider(session)
            tail = await latest_sequence(session, tenant_id=tenant_id, task_id=task_id)
            bound = await retained_from_sequence(
                session,
                tenant_id=tenant_id,
                task_id=task_id,
                retention_events=retention_events,
                retention_seconds=retention_seconds,
                now=now,
            )

            if cursor > tail or cursor < bound - 1:
                # 落后到窗口之外，或拿了一个不属于本任务的游标：不要假装能续上，
                # 明确要求客户端重新拉快照（漏事件就是漏状态）。
                yield format_sse(
                    event="RESET_REQUIRED",
                    data={
                        "task_id": str(task_id),
                        "requested_cursor": cursor,
                        "retained_from": bound,
                        "tail": tail,
                        "reason": (
                            "CURSOR_AHEAD_OF_TASK"
                            if cursor > tail
                            else "CURSOR_OUT_OF_RETENTION"
                        ),
                    },
                )
                return

            rows = await load_events_after(
                session, tenant_id=tenant_id, task_id=task_id, after_sequence=cursor
            )
            replay = [
                (
                    to_frame(row),
                    format_sse(
                        event=row.type,
                        data=to_frame(row).envelope(task_id=task_id),
                        event_id=str(row.sequence),
                    ),
                )
                for row in rows
            ]

        for frame, text in replay:
            yield text
            cursor = frame.sequence
            if frame.type == TaskEventType.REPLY_END.value:
                # 一次执行结束即关闭：客户端带着新游标重新订阅即可继续。
                # 断线不代表业务取消，长期等待也不需要一直占着连接。
                return

        if not replay:
            if cursor >= tail:
                yield format_sse(
                    event="caught_up",
                    data={"task_id": str(task_id), "cursor": cursor},
                )
                return
            # 尾部不是 reply_end：说明有执行正在进行，继续跟随。 

        if max_polls is not None and polls >= max_polls:
            return
        await asyncio.sleep(poll_interval)


async def load_task_events_after(
    session: AsyncSession,
    *,
    ctx,
    task_id: UUID,
    after_sequence: int,
) -> tuple[int, list[EventFrame]]:
    """读接口：一次性返回游标之后的持久事件（不走流）。"""

    rows = await load_events_after(
        session,
        tenant_id=ctx.tenant_id,
        task_id=task_id,
        after_sequence=after_sequence,
    )
    tail = await latest_sequence(session, tenant_id=ctx.tenant_id, task_id=task_id)
    return tail, [to_frame(row) for row in rows]


__all__ = [
    "EventFrame",
    "POLL_INTERVAL_SECONDS",
    "REPLAY_BATCH",
    "format_sse",
    "latest_sequence",
    "load_events_after",
    "load_task_events_after",
    "retained_from_sequence",
    "stream_task_events",
    "to_frame",
]
