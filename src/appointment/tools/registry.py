"""工具白名单注册表与统一调度入口（设计稿 §9、§5.1 第 4 步）。

三条边界都在这里收口：

1. **白名单**。首版只注册这九个业务工具。SDK 内置的编码工具（Bash / Write /
   Edit / 通用 HTTP / 任意 SQL）一律不注册；入口对未知工具名直接拒绝，而不是
   "转发给某个更宽的执行器"。注册表本身就是验收快照：模型看到的工具集必须与
   这里登记的完全一致。
2. **统一结果契约**。任何工具的成功、领域错误、参数错误都收敛成同一种
   :class:`ToolResult`，编排器不需要为每个工具写一套错误处理。
3. **身份不来自参数**。处理器只接受 :class:`TrustedContext`，输入 Schema 里没有
   tenant/actor/customer 的位置（见 ``schemas``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Collection

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import Clock
from ..core.enums import ErrorCode
from ..core.errors import SCHEMA_VERSION, DomainError
from ..core.result import ToolResult
from ..domain.context import TrustedContext
from . import handlers
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

Handler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个已注册工具的全部契约。"""

    name: str
    description: str
    input_model: type[ToolInput]
    handler: Handler
    #: 只读工具可供咨询 Agent 使用；写入工具只能在接待流程中由编排器调用。
    side_effect: str  # "read" | "write"
    #: 业务确认要求。写工具与"提交类"动作必须有方案级凭据，不接受通用许可。
    requires_confirmation: bool = False
    #: 除身份以外的额外调用权限（由 TrustedContext.require 二次校验）。
    permission: str | None = None
    version: str = "1.0.0"
    tags: tuple[str, ...] = field(default_factory=tuple)

    def json_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "side_effect": self.side_effect,
            "requires_confirmation": self.requires_confirmation,
            "parameters": self.input_model.json_schema_for_model(),
        }


#: 九个业务工具。顺序即注册顺序，也是给模型的呈现顺序。
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_knowledge",
        description=(
            "检索门店政策、服务说明与注意事项。只返回带证据来源的片段；"
            "没有足够证据时会明确说明证据不足，不得据此编造政策。"
        ),
        input_model=SearchKnowledgeInput,
        handler=handlers.search_knowledge_tool,
        side_effect="read",
        permission="knowledge:read",
        tags=("knowledge",),
    ),
    ToolSpec(
        name="get_service_quote",
        description=(
            "取服务的当前价格与时长。只读已发布价目，不立即锁价；"
            "锁价保证只在方案/占位事务保存快照后才成立。"
        ),
        input_model=GetServiceQuoteInput,
        handler=handlers.get_service_quote_tool,
        side_effect="read",
        permission="catalog:read",
        tags=("catalog",),
    ),
    ToolSpec(
        name="search_availability",
        description=(
            "查询可预约时段与候选资源。返回值是查询时点快照（guarantee=NONE），"
            "不构成资源保证；只有 create_hold 成功才能说已保留。"
        ),
        input_model=SearchAvailabilityInput,
        handler=handlers.search_availability_tool,
        side_effect="read",
        permission="catalog:read",
        tags=("catalog", "availability"),
    ),
    ToolSpec(
        name="get_appointment",
        description="按订单 ID 查询权威状态与明细。只读，会校验对象归属。",
        input_model=GetAppointmentInput,
        handler=handlers.get_appointment_tool,
        side_effect="read",
        permission="appointment:read",
        tags=("appointment",),
    ),
    ToolSpec(
        name="create_hold",
        description=(
            "对用户选定的单个候选创建短期占位并发布方案。会消耗客户占位配额，"
            "只在用户已明确选定某个候选时调用，不要为多个候选同时占位。"
        ),
        input_model=CreateHoldInput,
        handler=handlers.create_hold_tool,
        side_effect="write",
        permission="task:write",
        tags=("booking",),
    ),
    ToolSpec(
        name="confirm_appointment",
        description=(
            "使用方案级确认凭据提交预约。必须携带服务端签发的 confirmation_token "
            "与幂等键；模型不能自行判断'用户已同意'。"
        ),
        input_model=ConfirmAppointmentInput,
        handler=handlers.confirm_appointment_tool,
        side_effect="write",
        requires_confirmation=True,
        permission="appointment:create",
        tags=("booking",),
    ),
    ToolSpec(
        name="reschedule_appointment",
        description="把已确认订单改到新时段。需要 expected_version 与新的报价凭据。",
        input_model=RescheduleAppointmentInput,
        handler=handlers.reschedule_appointment_tool,
        side_effect="write",
        permission="appointment:reschedule",
        tags=("booking",),
    ),
    ToolSpec(
        name="cancel_appointment",
        description="取消已确认订单，并按规则释放资源。需要 expected_version。",
        input_model=CancelAppointmentInput,
        handler=handlers.cancel_appointment_tool,
        side_effect="write",
        permission="appointment:cancel",
        tags=("booking",),
    ),
    ToolSpec(
        name="transfer_to_human",
        description=(
            "转人工接管。用于用户要求、连续无法推进或出现争议时；"
            "接管后自动流程立即失权。"
        ),
        input_model=TransferToHumanInput,
        handler=handlers.transfer_to_human_tool,
        side_effect="write",
        permission="task:write",
        tags=("handoff",),
    ),
)

TOOL_REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

if len(TOOL_REGISTRY) != len(TOOL_SPECS):  # pragma: no cover - 注册即失败
    raise RuntimeError("工具名重复，白名单必须唯一")


def tool_definitions() -> list[dict[str, Any]]:
    """交给模型/适配器的工具定义。"""

    return [spec.json_schema() for spec in TOOL_SPECS]


def registry_snapshot() -> dict[str, Any]:
    """注册表验收快照（设计稿 §5.3：注册表与激活工具 Schema 做快照验收）。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "tool_count": len(TOOL_SPECS),
        "tools": [
            {
                "name": spec.name,
                "version": spec.version,
                "side_effect": spec.side_effect,
                "requires_confirmation": spec.requires_confirmation,
                "permission": spec.permission,
            }
            for spec in TOOL_SPECS
        ],
    }


async def invoke_tool(
    session: AsyncSession,
    ctx: TrustedContext,
    name: str,
    arguments: dict[str, Any] | None,
    *,
    now: datetime,
    clock: Clock | None = None,
    allowed_tools: Collection[str] | None = None,
) -> ToolResult:
    """唯一的工具执行入口。

    返回统一结果契约；不抛业务异常给调用方（``ToolResult`` 已经承载错误分类）。
    参数校验、白名单、权限与领域规则各自在正确的层裁决——这里不做业务判断，
    只做"入口拒绝"。
    """

    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        # 白名单是权限边界。未知工具名不转发、不猜测，直接拒绝。
        return ToolResult.failure(
            ctx.request_id,
            now,
            DomainError(
                ErrorCode.PERMISSION_DENIED,
                f"工具 {name} 不在白名单内，已拒绝执行",
            ),
        )

    if allowed_tools is not None and name not in allowed_tools:
        return ToolResult.failure(
            ctx.request_id,
            now,
            DomainError(
                ErrorCode.PERMISSION_DENIED,
                f"工具 {name} 不允许在当前任务阶段执行",
            ),
        )

    try:
        payload = spec.input_model.model_validate(arguments or {})
    except PydanticValidationError as exc:
        # 未知字段或非法枚举在这里就被拒绝，不会"静默采用"（设计稿 §5.1 第 4 步）。
        return ToolResult.failure(
            ctx.request_id,
            now,
            DomainError(
                ErrorCode.VALIDATION_ERROR,
                _format_validation_error(exc),
                details={"tool": name, "fields": _error_fields(exc)},
            ),
        )

    if spec.permission is not None:
        try:
            ctx.require(spec.permission)
        except DomainError as exc:
            return ToolResult.failure(ctx.request_id, now, exc)

    try:
        if spec.side_effect == "write":
            # Every write tool is an atomic unit inside the caller's task/event
            # transaction. If a database constraint rejects a concurrent write,
            # rolling back this savepoint keeps the outer session usable so the
            # orchestrator can persist the rejected tool result and continue safely.
            async with session.begin_nested():
                data = await spec.handler(session, ctx, payload, now=now, clock=clock)
        else:
            data = await spec.handler(session, ctx, payload, now=now, clock=clock)
    except DomainError as exc:
        return ToolResult.failure(ctx.request_id, now, exc)

    return ToolResult.ok(ctx.request_id, now, data)


def _error_fields(exc: PydanticValidationError) -> list[str]:
    return [".".join(str(part) for part in err["loc"]) for err in exc.errors()]


def _format_validation_error(exc: PydanticValidationError) -> str:
    parts = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        parts.append(f"{location}: {err['msg']}")
    return "工具参数不合法 -> " + "; ".join(parts)


__all__ = [
    "TOOL_REGISTRY",
    "TOOL_SPECS",
    "ToolSpec",
    "invoke_tool",
    "registry_snapshot",
    "tool_definitions",
]
