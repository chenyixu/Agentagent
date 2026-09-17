"""AgentScope 适配器（惰性导入）。

设计稿 §5.3 的要点在这里落地：

- **只在运行时导入 SDK**。``agentscope`` 未安装时，选择该运行时会得到明确的
  依赖错误，而不是导入整个包就失败。确定性基线始终可用。
- **工具白名单**。只把注册表里的九个业务工具交给 SDK。SDK 包内自带的编码工具
  （Bash / Write / Edit / 通用 HTTP）不注册、不暴露；入口对未知工具名直接拒绝。
- **权限模式**。接待 Agent 用 ``DEFAULT`` 加精确许可规则；咨询 Agent 用
  ``EXPLORE`` 并显式声明只读工具。两者都不能把"工具许可"变成业务确认——
  业务确认只能由专用接口验证方案级凭据后产生（设计稿 §5.4）。
- **不跨等待续跑**。SDK 里挂起的 reply 不用于恢复：需要等待时，适配器只把结果
  交回编排器，由编排器在业务事务中持久化等待请求，之后再从可信账本重建上下文。

适配器的契约与 :class:`~appointment.agent.deterministic.DeterministicRuntime` 完全
一致，因此两者可以互换对照。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..core.enums import AgentRole, ErrorCode, Intent, PermissionMode
from ..core.errors import DomainError
from ..domain.tasks import ALLOWED_SLOTS
from ..tools.registry import TOOL_REGISTRY, tool_definitions
from .ports import ToolRequest, TurnOutput, TurnRequest

#: 各角色允许使用的工具。默认拒绝：只列出当前角色确实需要的。
ROLE_TOOLS: dict[AgentRole, frozenset[str]] = {
    AgentRole.RECEPTION: frozenset(TOOL_REGISTRY),
    # 咨询 Agent 只有只读权限，不能占位、确认、改约、取消。
    AgentRole.CONSULTANT: frozenset(
        {
            "search_knowledge",
            "get_service_quote",
            "search_availability",
            "get_appointment",
        }
    ),
    AgentRole.SYSTEM: frozenset(TOOL_REGISTRY),
}

ROLE_PERMISSION_MODE: dict[AgentRole, PermissionMode] = {
    AgentRole.RECEPTION: PermissionMode.DEFAULT,
    AgentRole.CONSULTANT: PermissionMode.EXPLORE,
    AgentRole.SYSTEM: PermissionMode.DONT_ASK,
}


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    """适配器配置。模型与提示词版本进入 release manifest，便于消融与回滚。"""

    model_backend: str = "stub"
    model_name: str = "qwen-plus"
    prompt_version: str = "reception-v1"
    max_tool_calls: int = 6
    max_consult_delegations: int = 2
    structured_output_repair_attempts: int = 1
    permission_mode: PermissionMode | None = None


def assert_tool_allowed(role: AgentRole, tool_name: str) -> None:
    """工具白名单 + 角色能力双重检查。

    这一步是"注册了什么"与"这个角色能用什么"的交集。两层都需要：白名单保证
    不存在越界工具，角色表保证一个只读 Agent 拿不到写入工具。
    """

    if tool_name not in TOOL_REGISTRY:
        raise DomainError(
            ErrorCode.PERMISSION_DENIED, f"工具 {tool_name} 不在白名单内"
        )
    if tool_name not in ROLE_TOOLS.get(role, frozenset()):
        raise DomainError(
            ErrorCode.PERMISSION_DENIED,
            f"角色 {role.value} 不具备工具 {tool_name} 的使用许可",
        )


def allowed_tools_for(role: AgentRole) -> tuple[str, ...]:
    return tuple(name for name in TOOL_REGISTRY if name in ROLE_TOOLS.get(role, frozenset()))


def normalize_model_output(raw: dict[str, Any], *, role: AgentRole) -> TurnOutput:
    """把模型输出规范化为 :class:`TurnOutput`。

    设计稿 §5.1 第 4 步：未知字段或非法枚举不能静默采用。这里的做法是**丢弃并
    记录**未知的槽位操作与非法意图，绝不"大概猜一下"。工具名不在角色许可内时
    直接拒绝整轮输出——那说明提示词或模型越界了，不能继续执行。

    槽位名同样按白名单过滤。领域层还会再拒一次（权威过滤在那里），但模型不该
    有机会把不存在的槽位带进业务流程。
    """

    patches: list[dict[str, Any]] = []
    for patch in raw.get("slot_patches") or []:
        if not isinstance(patch, dict):
            continue
        slot = patch.get("slot_name")
        op = patch.get("op")
        if not slot or str(slot) not in ALLOWED_SLOTS:
            continue
        if op not in ("SET", "CLEAR", "NO_PREFERENCE"):
            continue
        if op == "SET" and not patch.get("value"):
            continue
        patches.append(
            {
                "slot_name": str(slot),
                "op": str(op),
                "value": patch.get("value"),
                "source": "model",
            }
        )

    requests: list[ToolRequest] = []
    for item in raw.get("tool_requests") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("tool_name") or "")
        if not name:
            continue
        assert_tool_allowed(role, name)
        arguments = item.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise DomainError(
                ErrorCode.VALIDATION_ERROR, f"工具 {name} 的 arguments 必须是对象"
            )
        requests.append(
            ToolRequest(
                tool_name=name,
                arguments=arguments,
                rationale=item.get("rationale"),
            )
        )

    intent_raw = raw.get("intent")
    intent: Intent | None = None
    if intent_raw:
        try:
            intent = Intent(str(intent_raw))
        except ValueError:
            # 非法意图不采用，落成 UNKNOWN 由编排器决定是否需要追问。
            intent = Intent.UNKNOWN

    usage = raw.get("usage_units")
    return TurnOutput(
        reply_text=raw.get("reply_text"),
        slot_patches=tuple(patches),
        tool_requests=tuple(requests),
        clarification_question=raw.get("clarification_question"),
        intent=intent,
        usage_units=int(usage) if isinstance(usage, (int, float)) else None,
        usage_status="REPORTED" if isinstance(usage, (int, float)) else "UNKNOWN",
    )


class AgentScopeRuntime:
    """基于 AgentScope SDK 的运行时。

    ``run_turn`` 的返回值与确定性基线同构，因此编排器不需要知道用的是哪一个。
    """

    def __init__(
        self,
        *,
        role: AgentRole = AgentRole.RECEPTION,
        config: AdapterConfig | None = None,
        model: Any | None = None,
    ) -> None:
        self.role = role
        self.config = config or AdapterConfig()
        self._model = model
        self._sdk: Any | None = None

    @property
    def runtime_name(self) -> str:
        return f"agentscope-{self.config.model_backend}-{self.config.prompt_version}"

    @property
    def permission_mode(self) -> PermissionMode:
        return self.config.permission_mode or ROLE_PERMISSION_MODE[self.role]

    def _load_sdk(self) -> Any:
        """惰性导入 SDK。

        没装 SDK 时给出可操作的错误，而不是 ImportError 穿透到调用方。生产用
        确定性基线时完全不触碰这里。
        """

        if self._sdk is not None:
            return self._sdk
        try:  # pragma: no cover - 取决于环境是否安装 agentscope
            import agentscope  # type: ignore
        except ImportError as exc:
            raise DomainError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "未安装 agentscope：请安装后使用 AgentScope 运行时，"
                "或改用确定性基线（APPOINTMENT_AGENT_RUNTIME=deterministic）",
                details={"runtime": self.runtime_name},
            ) from exc
        self._sdk = agentscope
        return agentscope

    def build_toolkit(self) -> Any:  # pragma: no cover - 依赖 SDK
        """按白名单构造 SDK 工具集。

        只注册本角色许可的业务工具；不注册任何编码工具、文件工具或通用请求工具。
        """

        sdk = self._load_sdk()
        toolkit = sdk.tool.Toolkit()
        allowed = set(allowed_tools_for(self.role))
        for definition in tool_definitions():
            if definition["name"] not in allowed:
                continue
            toolkit.register_tool_function(
                _make_declarative_tool(definition),
                func_description=definition["description"],
                json_schema=definition["parameters"],
            )
        return toolkit

    def build_system_prompt(self, request: TurnRequest) -> str:  # pragma: no cover
        allowed = "、".join(request.allowed_tools) or "（无）"
        return (
            "你是门店预约接待助手。只使用给定工具获取事实，不要编造价格、排班或政策。"
            "身份与授权由服务端注入，不要向用户索要用户 ID、租户 ID 或权限。"
            "涉及具体金额、时段、订单状态时必须以工具结果为准。"
            f"本状态允许的工具：{allowed}。"
        )

    async def run_turn(self, request: TurnRequest) -> TurnOutput:  # pragma: no cover
        """执行一轮活跃尝试。

        注意：不在这里做业务写入，也不持久化任何等待。工具调用请求交回编排器，
        由它在工具边界之后、在同一个业务事务里写入。
        """

        for name in request.allowed_tools:
            assert_tool_allowed(self.role, name)

        sdk = self._load_sdk()
        toolkit = self.build_toolkit()
        agent = sdk.agent.ReActAgent(
            name=f"appointment-{self.role.value}",
            sys_prompt=self.build_system_prompt(request),
            model=self._model,
            toolkit=toolkit,
            memory=sdk.memory.InMemoryMemory(),
        )
        raw = await agent(_build_user_content(request))
        return normalize_model_output(_coerce_mapping(raw), role=self.role)


def _make_declarative_tool(definition: dict[str, Any]) -> Any:  # pragma: no cover
    """构造只做"声明"的工具函数。

    真实执行统一回到 :func:`appointment.tools.registry.invoke_tool`，SDK 侧只负责
    让模型看到正确的 Schema。这样"SDK 工具许可"永远不会绕过业务确认与幂等。
    """

    async def _declared(**kwargs: Any) -> dict[str, Any]:
        return {
            "status": "DELEGATED",
            "tool_name": definition["name"],
            "note": "工具调用由编排器在工具边界执行，SDK 侧不直接访问数据库",
        }

    _declared.__name__ = definition["name"]
    return _declared


def _build_user_content(request: TurnRequest) -> str:  # pragma: no cover
    import json

    payload = {
        "task_state": request.task_state.value,
        "task_version": request.task_version,
        "slots": request.slots,
        "user_message": request.user_message,
        "facts": request.facts,
        "completed_actions": list(request.completed_actions),
    }
    return json.dumps(payload, ensure_ascii=False)


def _coerce_mapping(raw: Any) -> dict[str, Any]:
    """把 SDK 返回的各种形态折叠成 dict。

    结构化输出修复失败时返回空 dict，由编排器按"无进展"处理并退出循环——
    而不是把一段自由文本硬解析成动作。
    """

    if isinstance(raw, dict):
        return raw
    content = getattr(raw, "content", None)
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        import json

        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


__all__ = [
    "ROLE_PERMISSION_MODE",
    "ROLE_TOOLS",
    "AdapterConfig",
    "AgentScopeRuntime",
    "allowed_tools_for",
    "assert_tool_allowed",
    "normalize_model_output",
]
