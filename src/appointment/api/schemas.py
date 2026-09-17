"""对外请求与响应契约。

两处刻意的设计：

- 所有请求模型 ``extra="forbid"``：试图在请求体里自带 ``tenant_id`` / ``actor_id``
  会被直接拒绝，而不是被静默忽略。身份只能来自请求头（设计稿 §5.1 第 1 步）。
- 响应不包含模型内部思考与未经验证的原始工具内容，只暴露业务字段与统一错误码
  （设计稿 §9）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

STRICT = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------
class MessageSubmit(BaseModel):
    """提交一条用户消息。"""

    model_config = STRICT

    client_message_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=8000)
    conversation_id: UUID | None = None
    store_id: UUID | None = None


class WaitingAnswerSubmit(BaseModel):
    """回答一次追问。"""

    model_config = STRICT

    client_event_id: str = Field(min_length=1, max_length=64)
    answer: dict[str, Any]
    expected_task_version: int | None = None


class ConfirmationSubmit(BaseModel):
    """专用确认接口。

    凭据是方案级的：绑定 actor / task / 方案版本 / 内容哈希 / 动作 / 有效期。
    纯文本"好"不构成授权，也不经由这个接口（设计稿 §8.3）。
    """

    model_config = STRICT

    proposal_id: UUID
    proposal_version: int
    confirmation_token: str = Field(min_length=8, max_length=512)
    client_confirmation_event_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_task_version: int


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------
class PendingConfirmation(BaseModel):
    """确认卡内容。凭据只在本次响应里交给客户端，不写入事件账本。"""

    proposal_id: str
    proposal_version: int | None = None
    proposal_content_hash: str | None = None
    confirmation_token: str | None = None
    expires_at: str | None = None
    hold_id: str | None = None
    hold_expires_at: str | None = None
    expected_task_version: int | None = None


class ToolCallView(BaseModel):
    tool: str
    status: str
    tool_call_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class MessageResponse(BaseModel):
    task_id: str
    conversation_id: str | None = None
    task_state: str
    task_version: int
    reply_text: str | None = None
    clarification_question: str | None = None
    waiting_id: str | None = None
    end_reason: str
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    pending_confirmation: PendingConfirmation | None = None
    #: 订阅 SSE 时的起点游标，保证不漏事件。
    event_cursor: int | None = None


class ConfirmResponse(BaseModel):
    appointment_id: str
    committed_status: str
    task_state: str
    task_version: int
    operation_id: str | None = None
    #: True 表示这是同键重试重放的原订单，本次没有产生新的业务效果。
    replayed: bool = False
    #: 提交后的事件游标：客户端据此重新订阅，避免用旧游标空等。
    event_cursor: int | None = None


class TaskSnapshotResponse(BaseModel):
    task_id: str
    conversation_id: str
    state: str
    version: int
    epoch: int
    goal: str
    store_id: str | None = None
    slots: dict[str, Any] = Field(default_factory=dict)
    waiting: dict[str, Any] | None = None
    proposal: dict[str, Any] | None = None
    hold: dict[str, Any] | None = None
    appointment: dict[str, Any] | None = None
    event_cursor: int = 0


class OperationResponse(BaseModel):
    operation_id: str
    status: str
    action: str
    result: dict[str, Any] | None = None
    error_code: str | None = None
    appointment_id: str | None = None
    next_poll_after_seconds: int | None = None


class ProviderReceiptResponse(BaseModel):
    """供应商回执的接收结论。

    HTTP 码固定 200：回调是异步通知，非 2xx 只会让供应商反复重投同一条。
    真正的结论（应用了 / 隔离了 / 拒了）放在 ``action`` 里。
    """

    action: str
    receipt_id: str | None = None
    delivery_id: str | None = None
    status: str | None = None
    reason: str | None = None


class ErrorEnvelope(BaseModel):
    model_config = STRICT

    error: dict[str, Any]
    schema_version: int


class HealthResponse(BaseModel):
    status: Literal["ok"]
    runtime: str
    env: str
    now: datetime


__all__ = [
    "ConfirmResponse",
    "ConfirmationSubmit",
    "ErrorEnvelope",
    "HealthResponse",
    "MessageResponse",
    "MessageSubmit",
    "OperationResponse",
    "ProviderReceiptResponse",
    "PendingConfirmation",
    "TaskSnapshotResponse",
    "ToolCallView",
    "WaitingAnswerSubmit",
]
