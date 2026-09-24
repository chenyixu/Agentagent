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

from dataclasses import dataclass, replace
import json
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import AgentRole, ErrorCode, Intent, PermissionMode, TaskState
from ..core.errors import DomainError
from ..core.hashing import json_safe
from ..domain.tasks import ALLOWED_SLOTS
from ..tools.registry import TOOL_REGISTRY, tool_definitions
from .deterministic import detect_candidate_selection
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


class _StructuredTurn(BaseModel):
    """模型单步决策契约；最终业务执行仍由编排器裁决。"""

    model_config = ConfigDict(extra="forbid")
    reply_text: str | None = None
    clarification_question: str | None = None
    intent: str | None = None
    slot_patches: list[dict[str, Any]] = Field(default_factory=list)
    tool_requests: list[dict[str, Any]] = Field(default_factory=list)


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
        input_tokens=raw.get("input_tokens"),
        output_tokens=raw.get("output_tokens"),
        cache_input_tokens=raw.get("cache_input_tokens"),
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

    async def build_toolkit(self, allowed_tools: Sequence[str] | None = None) -> Any:  # pragma: no cover - 依赖 SDK
        """按白名单构造 SDK 工具集。

        只注册本角色许可的业务工具；不注册任何编码工具、文件工具或通用请求工具。
        """

        self._load_sdk()
        from agentscope.tool import FunctionTool, Toolkit

        toolkit = Toolkit()
        allowed = set(allowed_tools_for(self.role))
        if allowed_tools is not None:
            allowed.intersection_update(allowed_tools)
        for definition in tool_definitions():
            if definition["name"] not in allowed:
                continue
            await toolkit.add_tool(
                FunctionTool(
                    _make_declarative_tool(definition),
                    name=definition["name"],
                    description=definition["description"],
                    input_schema=definition["parameters"],
                    is_read_only=definition["side_effect"] == "read",
                )
            )
        return toolkit

    def build_system_prompt(self, request: TurnRequest) -> str:  # pragma: no cover
        # The model may reason over read-only business facts, but it must not be
        # offered a write tool. Booking writes are emitted by server-side policy
        # after an explicit user action and are still checked by the tool boundary.
        model_tools = tuple(
            name for name in request.allowed_tools
            if name in TOOL_REGISTRY and TOOL_REGISTRY[name].side_effect == "read"
        )
        allowed = "、".join(model_tools) or "（无）"
        definitions = [
            definition for definition in tool_definitions()
            if definition["name"] in model_tools
        ]
        prompt = (
            "你是门店预约接待助手。只使用给定工具获取事实，不要编造价格、排班或政策。"
            "身份与授权由服务端注入，不要向用户索要用户 ID、租户 ID 或权限。"
            "涉及具体金额、时段、订单状态时必须以工具结果为准。"
            f"本状态允许的工具：{allowed}。"
            "每次只输出一步结构化决策：需要工具时填写 tool_requests，"
            "得到下一轮可信观察后再继续判断；不要自行声称工具已成功。"
            "工具请求只是只读工具提案；预约写入由服务端依据任务阶段和用户明确操作推进，"
            "模型不得请求写工具。"
            f"本轮工具契约：{json.dumps(definitions, ensure_ascii=False)}"
        )
        if self.config.prompt_version in ("reception-v2", "reception-v3"):
            prompt += (
                "当前是一步一决策：结构化输出的 reply_text、clarification_question、"
                "slot_patches、tool_requests 至少一个必须有实际内容，不能返回空决策。"
                "若消息已说明服务项目，从 facts.services 找到精确 service_id，"
                "在 slot_patches 中写 slot_name=service、op=SET、"
                "value={service_id,name}。若时间明确，结合 facts.now 和 store_timezone "
                "将相对时间解析为绝对日期，在 slot_name=time_window 中写 start_at、"
                "end_at、desired_start 的带时区 ISO 时间和 local_date。"
                "不确定时间或项目时在 clarification_question 中提出具体问题；"
                "只在 reply_text 里问问题而不填写 clarification_question 不算完成澄清。"
                "禁止根据用户的越权指令占位或确认；候选必须先查询并由用户选定。"
            )
        if self.config.prompt_version == "reception-v3":
            prompt += (
                "按任务阶段执行下一步，不要只说‘我来查询’却不发工具请求。"
                "当 task_state=SEARCHING 且 slots.service 与 slots.time_window 已完整时："
                "若 facts.followups.quote 缺失，tool_requests 必须包含一次 "
                "get_service_quote，参数 store_id 从 facts.store_id、service_id 从 "
                "slots.service.value.service_id 读取；若报价已取得而 "
                "facts.followups.availability 缺失，tool_requests 必须包含一次 "
                "search_availability，参数 store_id、service_id 同上，window_start 与 "
                "window_end 从 slots.time_window.value.start_at/end_at 读取。"
                "工具返回前不要宣称有可约时段或准确价格。"
            )
        return prompt

    async def run_turn(self, request: TurnRequest) -> TurnOutput:  # pragma: no cover
        """执行一轮活跃尝试。

        注意：不在这里做业务写入，也不持久化任何等待。工具调用请求交回编排器，
        由它在工具边界之后、在同一个业务事务里写入。
        """

        for name in request.allowed_tools:
            assert_tool_allowed(self.role, name)

        self._load_sdk()
        if self._model is None:
            raise DomainError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "AgentScope 运行时缺少已配置的模型",
            )
        from agentscope.agent import Agent, ReActConfig
        from agentscope.message import Msg, TextBlock

        # SDK 的工具函数不会在模型事务内执行：本轮只产出结构化提案，
        # 编排器完成一次工具调用与落账后，用新的可信事实发起下一轮。
        agent = Agent(
            name=f"appointment-{self.role.value}",
            system_prompt=self.build_system_prompt(request),
            model=self._model,
            react_config=ReActConfig(max_iters=1),
        )
        raw = await agent.reply(
            Msg(
                name="user", role="user",
                content=[TextBlock(text=_build_user_content(request))],
            ),
            structured_schema=_StructuredTurn,
        )
        output = normalize_model_output(_coerce_mapping(raw), role=self.role)
        output = _ensure_required_search_read(request, output)
        if request.task_state is TaskState.PROPOSED:
            output = _ensure_explicit_candidate_hold(request, output)
        else:
            output = _reject_model_write_requests(output)
        if not (
            output.reply_text or output.clarification_question
            or output.slot_patches or output.tool_requests
        ):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "模型返回空的结构化决策")
        return output


def _ensure_required_search_read(request: TurnRequest, output: TurnOutput) -> TurnOutput:
    """查询阶段的必需只读步骤及参数由服务端按可信事实补齐。

    模型可以提出当前需要的查询，但不能为门店、服务或时段提供权威参数；这些
    参数必须来自已校验的任务上下文。这样也能收敛真实模型偶尔返回的空 arguments。
    """

    if (
        request.task_state is not TaskState.SEARCHING
        or output.clarification_question
    ):
        return output
    service_id = ((request.slots.get("service") or {}).get("value") or {}).get("service_id")
    window = (request.slots.get("time_window") or {}).get("value") or {}
    store_id = request.facts.get("store_id")
    if not service_id or not store_id or not window.get("start_at") or not window.get("end_at"):
        return output
    followups = request.facts.get("followups") or {}
    if not followups.get("quote") and "get_service_quote" in request.allowed_tools:
        tool_name = "get_service_quote"
        arguments = {"store_id": store_id, "service_id": service_id}
        default_rationale = "查询阶段必须取得当前报价"
    elif not followups.get("availability") and "search_availability" in request.allowed_tools:
        tool_name = "search_availability"
        arguments = {
            "store_id": store_id,
            "service_id": service_id,
            "window_start": window["start_at"],
            "window_end": window["end_at"],
            "desired_start": window.get("desired_start"),
            "limit": 5,
        }
        default_rationale = "查询阶段必须取得当前可用时段"
    else:
        return output

    # Keep model reasoning as a diagnostic hint, but rebuild arguments from the
    # server-owned task facts and execute only the one required read per turn.
    proposed = next(
        (item for item in output.tool_requests if item.tool_name == tool_name),
        None,
    )
    if output.tool_requests and proposed is None:
        return output
    tool = ToolRequest(
        tool_name=tool_name,
        arguments=arguments,
        rationale=(proposed.rationale if proposed and proposed.rationale else default_rationale),
    )
    return replace(output, reply_text=None, tool_requests=(tool,))


def _ensure_explicit_candidate_hold(request: TurnRequest, output: TurnOutput) -> TurnOutput:
    """用户明确选择已展示候选时，按可信事实组装占位请求。"""

    if (
        request.task_state is not TaskState.PROPOSED
        or "create_hold" not in request.allowed_tools
    ):
        return output
    # A structured request is still untrusted model output. Strip a model-proposed
    # write even when its arguments look valid; only this server-side adapter path
    # may synthesize create_hold after checking the user's explicit selection.
    safe_requests = tuple(
        item for item in output.tool_requests
        if item.tool_name in TOOL_REGISTRY
        and TOOL_REGISTRY[item.tool_name].side_effect == "read"
    )
    output = replace(output, tool_requests=safe_requests)
    if output.slot_patches:
        return output
    followups = request.facts.get("followups") or {}
    candidates = (followups.get("availability") or {}).get("candidates") or []
    quote_token = (followups.get("quote") or {}).get("quote_token")
    # This user text may have arrived before the candidates were computed in this
    # same active turn. Only candidates reconstructed from a prior completed turn
    # have actually been shown to the user and can be selected.
    selected = None if "availability" in request.fresh_fact_keys else detect_candidate_selection(
        request.user_message or "", candidate_count=len(candidates)
    )
    if selected is None:
        question = output.clarification_question or (
            "请明确选择一个已展示的候选时段，例如“第一个”，"
            "选择后我再为您创建待确认方案。"
        )
        return replace(
            output,
            reply_text=question,
            clarification_question=question,
        )
    if not quote_token:
        question = "当前报价已失效，请重新查询方案后再选择候选。"
        return replace(
            output,
            reply_text=question,
            clarification_question=question,
        )
    candidate = candidates[selected]
    resource_ids = [unit.get("resource_id") for unit in candidate.get("resources") or []]
    service_id = ((request.slots.get("service") or {}).get("value") or {}).get("service_id")
    if not resource_ids or any(not resource_id for resource_id in resource_ids) or not service_id:
        question = "当前候选信息不完整，请重新查询可预约方案。"
        return replace(
            output,
            reply_text=question,
            clarification_question=question,
        )
    tool = ToolRequest(
        tool_name="create_hold",
        arguments={
            "task_id": str(request.task_id),
            "expected_task_version": request.task_version,
            "store_id": request.facts.get("store_id"),
            "service_id": service_id,
            "candidate_id": candidate.get("candidate_id"),
            "start_at": candidate.get("start_at"),
            "end_at": candidate.get("end_at"),
            "resource_ids": resource_ids,
            "quote_token": quote_token,
        },
        rationale="用户明确选择了已展示候选，服务端按可信账本创建短期占位",
    )
    return replace(
        output, reply_text=None, clarification_question=None,
        tool_requests=(tool,),
    )


def _reject_model_write_requests(output: TurnOutput) -> TurnOutput:
    """Model output can request reads; every business write is server-controlled."""

    read_requests = tuple(
        item for item in output.tool_requests
        if item.tool_name in TOOL_REGISTRY
        and TOOL_REGISTRY[item.tool_name].side_effect == "read"
    )
    if len(read_requests) == len(output.tool_requests):
        return output
    message = "这类预约操作不能由模型直接提交，请按页面中的明确确认步骤继续。"
    return replace(
        output,
        reply_text=message,
        clarification_question=message,
        tool_requests=read_requests,
    )


def _make_declarative_tool(definition: dict[str, Any]) -> Any:  # pragma: no cover
    """构造只做"声明"的工具函数。

    真实执行统一回到 :func:`appointment.tools.registry.invoke_tool`，SDK 侧只负责
    让模型看到正确的 Schema。这样"SDK 工具许可"永远不会绕过业务确认与幂等。
    """

    async def _declared(**kwargs: Any) -> Any:
        from agentscope.message import TextBlock, ToolResultState
        from agentscope.tool import ToolChunk

        return ToolChunk(
            content=[TextBlock(text=json.dumps({
                "status": "DELEGATED", "tool_name": definition["name"],
                "note": "业务调用由编排器执行",
            }, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

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
    return json.dumps(json_safe(payload), ensure_ascii=False)


def _coerce_mapping(raw: Any) -> dict[str, Any]:
    """把 SDK 返回的各种形态折叠成 dict。

    结构化输出修复失败时返回空 dict，由编排器按"无进展"处理并退出循环——
    而不是把一段自由文本硬解析成动作。
    """

    if isinstance(raw, dict):
        return raw
    structured = getattr(raw, "structured_output", None)
    if isinstance(structured, dict):
        result = dict(structured)
        usage = getattr(raw, "usage", None)
        if usage is not None:
            result["usage_units"] = usage.input_tokens + usage.output_tokens
            result["input_tokens"] = usage.input_tokens
            result["output_tokens"] = usage.output_tokens
            result["cache_input_tokens"] = usage.cache_input_tokens
        return result
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
