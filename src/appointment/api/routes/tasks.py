"""任务快照与 SSE 订阅。"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.session import db_now, get_sessionmaker
from ...domain.context import TrustedContext
from ..deps import get_app_settings, get_identity, get_session
from ..schemas import TaskSnapshotResponse
from ..snapshot import build_task_snapshot
from ..sse import latest_sequence, load_task_events_after, stream_task_events

router = APIRouter(prefix="/v1", tags=["tasks"])


@router.get("/tasks/{task_id}", response_model=TaskSnapshotResponse)
async def get_task(
    task_id: UUID,
    ctx: TrustedContext = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
) -> TaskSnapshotResponse:
    """授权范围内的任务快照。断线重连、或收到 RESET_REQUIRED 后先拉它。"""

    snapshot = await build_task_snapshot(session, ctx, task_id=task_id)
    cursor = await latest_sequence(session, tenant_id=ctx.tenant_id, task_id=task_id)
    await session.commit()
    snapshot["event_cursor"] = cursor
    return TaskSnapshotResponse(**snapshot)


@router.get("/tasks/{task_id}/events")
async def stream_events(
    task_id: UUID,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ctx: TrustedContext = Depends(get_identity),
    settings=Depends(get_app_settings),
) -> StreamingResponse:
    """订阅任务事件流。

    游标来源按优先级：显式 ``after_sequence`` > 浏览器自动带回的 ``Last-Event-ID``。
    保留窗口之外会先收到 ``RESET_REQUIRED``，客户端应重新拉任务快照再订阅。
    """

    ctx.require("task:read")
    cursor = after_sequence
    if cursor == 0 and last_event_id:
        try:
            cursor = int(last_event_id)
        except ValueError:
            cursor = 0

    factory = get_sessionmaker()

    async def _now(session: AsyncSession):
        return await db_now(session)

    generator: AsyncIterator[str] = stream_task_events(
        factory,
        tenant_id=ctx.tenant_id,
        task_id=task_id,
        after_sequence=cursor,
        retention_events=settings.sse_retention_events,
        retention_seconds=settings.sse_retention_seconds,
        now_provider=_now,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 反向代理常见做法：关掉缓冲，否则事件会被攒着不发。
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/tasks/{task_id}/events/replay")
async def replay_events(
    task_id: UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    ctx: TrustedContext = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """一次性读接口：按游标取事件（便于测试与非浏览器客户端）。

    与流式接口共用同一份持久事件，不存在"两套真相"。
    """

    ctx.require("task:read")
    tail, frames = await load_task_events_after(
        session, ctx=ctx, task_id=task_id, after_sequence=after_sequence
    )
    await session.commit()
    return {
        "task_id": str(task_id),
        "tail": tail,
        "events": [frame.envelope(task_id=task_id) for frame in frames[:limit]],
    }
