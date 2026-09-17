"""消息提交与追问答复。

接入层的职责在这里收口：身份解析（依赖注入）、输入大小限制（Schema）、消息去重
（``client_message_id`` 唯一约束 + 编排器的重放检测）、事务提交。

一条重要纪律：**同一条消息重复提交不重跑业务效果**。去重在
:func:`appointment.domain.tasks.append_message` 里做，编排器看到
``deduplicated`` 就原样返回当前任务状态，而不是再执行一遍工具。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.enums import ErrorCode
from ...core.errors import DomainError
from ...db import models as m
from ...domain.context import TrustedContext
from ...orchestrator import Orchestrator
from ..deps import get_app_settings, get_identity, get_runtime, get_session
from ..schemas import (
    MessageResponse,
    MessageSubmit,
    PendingConfirmation,
    ToolCallView,
    WaitingAnswerSubmit,
)

router = APIRouter(prefix="/v1", tags=["messages"])


@router.post("/messages", response_model=MessageResponse)
async def submit_message(
    payload: MessageSubmit,
    ctx: TrustedContext = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
    runtime=Depends(get_runtime),
    settings=Depends(get_app_settings),
) -> MessageResponse:
    """提交一条用户消息并推进任务。

    调用方无需先建会话：未显式给出 ``conversation_id`` 时，服务端会复用该客户在
    该门店的开放会话（同一会话同时只有一个活跃任务）。
    """

    ctx.require("task:write")

    orchestrator = Orchestrator(session, runtime=runtime, settings=settings)
    report = await orchestrator.handle_user_message(
        ctx,
        text=payload.text,
        client_message_id=payload.client_message_id,
        conversation_id=payload.conversation_id,
        store_id=payload.store_id,
    )
    conversation_id = await _conversation_of(session, ctx, task_id=report.task_id)
    # 业务效果与事件必须在同一事务提交。
    await session.commit()

    return MessageResponse(
        task_id=str(report.task_id),
        conversation_id=None if conversation_id is None else str(conversation_id),
        task_state=report.task_state,
        task_version=report.task_version,
        reply_text=report.reply_text,
        clarification_question=report.clarification_question,
        waiting_id=None if report.waiting_id is None else str(report.waiting_id),
        end_reason=report.end_reason,
        tool_calls=[
            ToolCallView(
                tool=entry["tool"],
                status=entry["status"],
                tool_call_id=entry.get("tool_call_id"),
                error_code=entry.get("error_code"),
                error_message=entry.get("error_message"),
            )
            for entry in report.tool_calls
        ],
        pending_confirmation=(
            None
            if report.pending_confirmation is None
            else PendingConfirmation(**report.pending_confirmation)
        ),
        event_cursor=report.event_cursor,
    )


@router.post("/tasks/{task_id}/answers", response_model=MessageResponse)
async def answer_waiting_request(
    task_id: UUID,
    payload: WaitingAnswerSubmit,
    ctx: TrustedContext = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
    runtime=Depends(get_runtime),
    settings=Depends(get_app_settings),
) -> MessageResponse:
    """回答一次追问。

    答案必须带 ``expected_task_version``（或沿用等待请求记录的问题版本）：
    过时答案只留审计，不推进任务（设计稿 §5.4 第 3 步）。
    """

    ctx.require("task:write")
    answer = dict(payload.answer)
    if payload.expected_task_version is not None:
        answer["task_version"] = payload.expected_task_version

    orchestrator = Orchestrator(session, runtime=runtime, settings=settings)
    report = await orchestrator.resume_after_waiting(
        ctx,
        task_id=task_id,
        answer=answer,
        client_event_id=payload.client_event_id,
    )
    conversation_id = await _conversation_of(session, ctx, task_id=report.task_id)
    await session.commit()

    return MessageResponse(
        task_id=str(report.task_id),
        conversation_id=None if conversation_id is None else str(conversation_id),
        task_state=report.task_state,
        task_version=report.task_version,
        reply_text=report.reply_text,
        clarification_question=report.clarification_question,
        waiting_id=None if report.waiting_id is None else str(report.waiting_id),
        end_reason=report.end_reason,
        tool_calls=[
            ToolCallView(
                tool=entry["tool"],
                status=entry["status"],
                tool_call_id=entry.get("tool_call_id"),
                error_code=entry.get("error_code"),
                error_message=entry.get("error_message"),
            )
            for entry in report.tool_calls
        ],
        pending_confirmation=(
            None
            if report.pending_confirmation is None
            else PendingConfirmation(**report.pending_confirmation)
        ),
        event_cursor=report.event_cursor,
    )


async def _conversation_of(
    session: AsyncSession, ctx: TrustedContext, *, task_id: UUID
) -> UUID | None:
    row = (
        await session.execute(
            select(m.Task.conversation_id).where(
                m.Task.tenant_id == ctx.tenant_id, m.Task.id == task_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise DomainError(ErrorCode.NOT_FOUND, "任务不存在或无权访问")
    return row
