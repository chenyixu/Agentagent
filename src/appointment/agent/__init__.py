"""Agent 层：端口、确定性基线、AgentScope 适配器。

对外只暴露端口与工厂，业务代码不直接依赖某个运行时（设计稿 §4）。
"""

from .agentscope_adapter import (
    ROLE_PERMISSION_MODE,
    ROLE_TOOLS,
    AdapterConfig,
    AgentScopeRuntime,
    allowed_tools_for,
    assert_tool_allowed,
    normalize_model_output,
)
from .deterministic import (
    DeterministicRuntime,
    TimeExpression,
    classify_intent,
    parse_time_expression,
    resolve_service,
)
from .ports import (
    AgentRuntimePort,
    BookingPort,
    DeliveryReceipt,
    KnowledgePort,
    NotificationEnvelope,
    NotificationPort,
    TaskSnapshot,
    ToolRequest,
    TurnOutput,
    TurnRequest,
    WorkflowPort,
)


def build_runtime(
    runtime: str = "deterministic",
    *,
    role: str = "reception",
    config: AdapterConfig | None = None,
    model=None,
):
    """运行时工厂。

    默认返回确定性基线：它不依赖外部依赖，因此在任何环境都能跑。选择
    ``agentscope`` 时若未安装 SDK，会在**首次使用**时给出明确的依赖错误，
    而不是让整个进程导入失败。
    """

    from ..core.enums import AgentRole

    if runtime == "deterministic":
        return DeterministicRuntime()
    if runtime == "agentscope":
        return AgentScopeRuntime(
            role=AgentRole(role), config=config, model=model
        )
    raise ValueError(f"未知的 Agent 运行时：{runtime}")


__all__ = [
    "ROLE_PERMISSION_MODE",
    "ROLE_TOOLS",
    "AdapterConfig",
    "AgentRuntimePort",
    "AgentScopeRuntime",
    "BookingPort",
    "DeliveryReceipt",
    "DeterministicRuntime",
    "KnowledgePort",
    "NotificationEnvelope",
    "NotificationPort",
    "TaskSnapshot",
    "TimeExpression",
    "ToolRequest",
    "TurnOutput",
    "TurnRequest",
    "WorkflowPort",
    "allowed_tools_for",
    "assert_tool_allowed",
    "build_runtime",
    "classify_intent",
    "normalize_model_output",
    "parse_time_expression",
    "resolve_service",
]
