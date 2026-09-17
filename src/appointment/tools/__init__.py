"""工具边界：输入 Schema、白名单注册表与统一执行入口。

设计稿 §9 的分层在这里落地：

- ``schemas``  只描述模型**可以**提供什么，身份与授权不在其中；
- ``handlers`` 只做"翻译 + 调领域服务"，不放业务判断；
- ``registry`` 是唯一的执行入口，负责白名单、参数校验、权限与统一结果契约。
"""

from .handlers import (
    cancel_appointment_tool,
    confirm_appointment_tool,
    create_hold_tool,
    get_appointment_tool,
    get_service_quote_tool,
    reschedule_appointment_tool,
    search_availability_tool,
    search_knowledge_tool,
    transfer_to_human_tool,
)
from .registry import (
    TOOL_REGISTRY,
    TOOL_SPECS,
    ToolSpec,
    invoke_tool,
    registry_snapshot,
    tool_definitions,
)
from .schemas import (
    CancelAppointmentInput,
    ConfirmAppointmentInput,
    CreateHoldInput,
    GetAppointmentInput,
    GetServiceQuoteInput,
    RescheduleAppointmentInput,
    SearchAvailabilityInput,
    SearchKnowledgeInput,
    ToolInput,
    TransferToHumanInput,
)

__all__ = [
    "TOOL_REGISTRY",
    "TOOL_SPECS",
    "CancelAppointmentInput",
    "ConfirmAppointmentInput",
    "CreateHoldInput",
    "GetAppointmentInput",
    "GetServiceQuoteInput",
    "RescheduleAppointmentInput",
    "SearchAvailabilityInput",
    "SearchKnowledgeInput",
    "ToolInput",
    "ToolSpec",
    "TransferToHumanInput",
    "cancel_appointment_tool",
    "confirm_appointment_tool",
    "create_hold_tool",
    "get_appointment_tool",
    "get_service_quote_tool",
    "invoke_tool",
    "registry_snapshot",
    "reschedule_appointment_tool",
    "search_availability_tool",
    "search_knowledge_tool",
    "tool_definitions",
    "transfer_to_human_tool",
]
