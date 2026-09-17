"""九个业务工具的输入 Schema。

设计稿 §9 的硬约束，在这里用类型系统落地：

1. **身份不由模型提供**。``tenant_id`` / ``actor_id`` / ``customer_id`` / 角色
   一律不在输入 Schema 里——它们在 :class:`TrustedContext` 中由服务端注入。
   一个模型无法"声称"自己是别的客户。
2. **未知字段与非法枚举不能静默采用**（设计稿 §5.1 第 4 步）。所有模型都是
   ``extra="forbid"``，并且服务端生成的枚举在解析阶段就拒绝越界值。
3. **写工具的授权不是输入字段**。``confirmation_token`` 是凭据而不是"是否授权"
   的布尔开关：它由服务端签发、和主体/方案版本/内容哈希绑定，客户端只能回传，
   不能构造。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 所有输入模型的共同配置。
_BASE = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolInput(BaseModel):
    """工具输入基类。子类只声明业务字段。"""

    model_config = _BASE

    @classmethod
    def json_schema_for_model(cls) -> dict[str, Any]:
        """给模型看的 JSON Schema。

        刻意不暴露 ``title``/``description`` 之外的内部字段，并保留
        ``additionalProperties: false``，让"不接受未知字段"对模型也是可见约束。
        """

        schema = cls.model_json_schema()
        schema["additionalProperties"] = False
        return schema


class SearchKnowledgeInput(ToolInput):
    """检索门店政策、服务说明与注意事项。只读。"""

    query: str = Field(min_length=1, max_length=500, description="用户的问题或关键词")
    store_id: UUID | None = Field(
        default=None, description="限定门店；为空时只检索租户级通用文档"
    )
    top_k: int = Field(default=3, ge=1, le=8)


class GetServiceQuoteInput(ToolInput):
    """取服务当前价格与时长。只读，不立即锁价。"""

    store_id: UUID = Field(description="门店 ID")
    service_id: UUID = Field(description="服务目录 ID")


class SearchAvailabilityInput(ToolInput):
    """查询可用时段。只读快照，不构成资源保证。"""

    store_id: UUID
    service_id: UUID
    window_start: datetime = Field(description="查询窗口开始（ISO 8601，带时区）")
    window_end: datetime = Field(description="查询窗口结束（ISO 8601，带时区）")
    desired_start: datetime | None = Field(
        default=None, description="用户偏好的开始时间，用于时间贴合度打分"
    )
    preferred_resource_ids: list[UUID] = Field(default_factory=list, max_length=8)
    excluded_resource_ids: list[UUID] = Field(default_factory=list, max_length=8)
    gender: str | None = Field(default=None, description="技师性别偏好")
    gender_hard: bool = Field(default=False, description="性别是否为硬约束")
    skill_codes: list[str] = Field(default_factory=list, max_length=8)
    allow_substitute: bool = Field(
        default=True, description="点名技师不可用时是否允许替代"
    )
    budget_minor: int | None = Field(default=None, ge=0, description="预算，最小货币单位")
    limit: int = Field(default=5, ge=1, le=10)

    @field_validator("window_end")
    @classmethod
    def _require_ordered_window(cls, value: datetime, info) -> datetime:
        start = info.data.get("window_start")
        if start is not None and value <= start:
            raise ValueError("window_end 必须晚于 window_start")
        return value

    @field_validator("window_start", "window_end", "desired_start")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        # naive 时间会被静默按本地时区解释，跨时区门店会直接排错时段。
        if value is not None and value.tzinfo is None:
            raise ValueError("时间必须带时区偏移")
        return value


class GetAppointmentInput(ToolInput):
    """按 ID 查订单。只读，且校验对象归属。"""

    appointment_id: UUID


class CreateHoldInput(ToolInput):
    """对某个候选创建短期占位并发布方案。写入。"""

    task_id: UUID
    expected_task_version: int = Field(ge=1)
    store_id: UUID
    service_id: UUID
    candidate_id: str = Field(min_length=8, max_length=128)
    start_at: datetime
    end_at: datetime
    resource_ids: list[UUID] = Field(min_length=1, max_length=8)
    quote_token: str = Field(min_length=16, description="服务端签发的报价凭据")

    @field_validator("start_at", "end_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("时间必须带时区偏移")
        return value


class ConfirmAppointmentInput(ToolInput):
    """用方案级确认凭据提交预约。写入。"""

    proposal_id: UUID
    proposal_version: int = Field(ge=1)
    confirmation_token: str = Field(min_length=16)
    client_confirmation_event_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=200)
    expected_task_version: int = Field(ge=1)


class RescheduleAppointmentInput(ToolInput):
    """改约到新时段。写入。"""

    appointment_id: UUID
    expected_appointment_version: int = Field(ge=1)
    new_start_at: datetime
    new_end_at: datetime
    new_resource_ids: list[UUID] = Field(min_length=1, max_length=8)
    quote_token: str = Field(min_length=16)
    idempotency_key: str = Field(min_length=8, max_length=200)
    candidate_id: str | None = Field(default=None, max_length=128)

    @field_validator("new_start_at", "new_end_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("时间必须带时区偏移")
        return value


class CancelAppointmentInput(ToolInput):
    """取消订单。写入。"""

    appointment_id: UUID
    expected_appointment_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=200)
    reason_code: str = Field(default="user_request", min_length=1, max_length=64)


class TransferToHumanInput(ToolInput):
    """转人工。工单写入。"""

    task_id: UUID
    reason_code: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=1000)
    urgency: str = Field(default="normal", pattern="^(low|normal|high)$")


__all__ = [
    "CancelAppointmentInput",
    "ConfirmAppointmentInput",
    "CreateHoldInput",
    "GetAppointmentInput",
    "GetServiceQuoteInput",
    "RescheduleAppointmentInput",
    "SearchAvailabilityInput",
    "SearchKnowledgeInput",
    "ToolInput",
    "TransferToHumanInput",
]
