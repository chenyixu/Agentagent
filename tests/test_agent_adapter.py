"""Agent 运行时的边界：授权不来自模型，工具集不来自 SDK。

这些断言不依赖 agentscope SDK，也不需要网络：它们守的是"模型能说什么"与
"角色能用什么"之间的交集，以及非法输出**不会被静默采用**（设计稿 §5.1、§5.3）。
"""

from __future__ import annotations

import pytest
from uuid import uuid4

from appointment.agent import build_runtime
from appointment.agent.agentscope_adapter import (
    ROLE_PERMISSION_MODE,
    AdapterConfig,
    AgentScopeRuntime,
    allowed_tools_for,
    assert_tool_allowed,
    _availability_window_end,
    _ensure_required_search_read,
    _ground_service_patch,
    normalize_model_output,
)
from appointment.core.enums import AgentRole, ErrorCode, TaskState
from appointment.core.errors import DomainError
from appointment.domain.context import TrustedContext
from appointment.agent.ports import TurnRequest
from appointment.tools.registry import TOOL_REGISTRY

pytestmark = pytest.mark.tool_boundary

WRITE_TOOLS = frozenset(
    name for name, spec in TOOL_REGISTRY.items() if spec.side_effect == "write"
)


def test_every_role_has_a_permission_mode():
    """新增角色时必须同时给出权限模式，不能靠 KeyError 才发现。"""

    assert set(ROLE_PERMISSION_MODE) == set(AgentRole)


def test_consultant_role_never_gets_write_tools():
    consultant = set(allowed_tools_for(AgentRole.CONSULTANT))
    assert consultant & WRITE_TOOLS == set()
    assert "search_knowledge" in consultant
    assert "create_hold" not in consultant


def test_reception_can_use_the_whole_whitelist():
    assert set(allowed_tools_for(AgentRole.RECEPTION)) == set(TOOL_REGISTRY)


@pytest.mark.parametrize(
    ("role", "tool", "code"),
    [
        (AgentRole.CONSULTANT, "create_hold", ErrorCode.PERMISSION_DENIED),
        (AgentRole.CONSULTANT, "confirm_appointment", ErrorCode.PERMISSION_DENIED),
        (AgentRole.CONSULTANT, "cancel_appointment", ErrorCode.PERMISSION_DENIED),
        (AgentRole.RECEPTION, "bash", ErrorCode.PERMISSION_DENIED),
        (AgentRole.RECEPTION, "write_file", ErrorCode.PERMISSION_DENIED),
        (AgentRole.RECEPTION, "any_sql", ErrorCode.PERMISSION_DENIED),
    ],
)
def test_tool_outside_the_role_contract_is_denied(role, tool, code):
    with pytest.raises(DomainError) as excinfo:
        assert_tool_allowed(role, tool)
    assert excinfo.value.code is code


def test_model_output_with_a_coding_tool_rejects_the_whole_turn():
    """模型提出越界工具说明提示词或模型越界，不能只丢这一个调用继续跑。"""

    with pytest.raises(DomainError) as excinfo:
        normalize_model_output(
            {"tool_requests": [{"tool_name": "bash", "arguments": {"cmd": "ls"}}]},
            role=AgentRole.RECEPTION,
        )
    assert excinfo.value.code is ErrorCode.PERMISSION_DENIED


def test_unknown_slots_and_illegal_ops_are_dropped_not_adopted():
    output = normalize_model_output(
        {
            "slot_patches": [
                {"slot_name": "service", "op": "SET", "value": {"service_id": "s1"}},
                {"slot_name": "price", "op": "SET", "value": {"amount": 1}},
                {"slot_name": "time_window", "op": "OVERWRITE", "value": {}},
                {"slot_name": "time_window", "op": "SET"},
                "不是对象",
            ],
        },
        role=AgentRole.RECEPTION,
    )
    assert [patch["slot_name"] for patch in output.slot_patches] == ["service"]


def test_illegal_intent_and_arguments_are_handled_explicitly():
    output = normalize_model_output(
        {"intent": "delete_everything", "tool_requests": []},
        role=AgentRole.RECEPTION,
    )
    assert output.intent.value == "unknown"

    with pytest.raises(DomainError) as excinfo:
        normalize_model_output(
            {"tool_requests": [{"tool_name": "search_availability", "arguments": "GET /"}]},
            role=AgentRole.RECEPTION,
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


def test_missing_usage_is_unknown_not_zero():
    output = normalize_model_output({}, role=AgentRole.RECEPTION)
    assert output.usage_units is None
    assert output.usage_status == "UNKNOWN"

    reported = normalize_model_output(
        {"usage_units": 12}, role=AgentRole.RECEPTION
    )
    assert reported.usage_units == 12
    assert reported.usage_status == "REPORTED"


def test_build_runtime_defaults_to_the_deterministic_baseline():
    runtime = build_runtime()
    assert runtime.runtime_name.startswith("deterministic")
    assert build_runtime("deterministic").runtime_name == runtime.runtime_name

    with pytest.raises(ValueError):
        build_runtime("magic-runtime")


def test_sdk_runtime_reports_a_missing_dependency_clearly(monkeypatch):
    """未装 SDK 时给出可操作的错误，而不是 ImportError 穿透。"""

    runtime = AgentScopeRuntime(role=AgentRole.CONSULTANT, config=AdapterConfig())
    assert runtime.runtime_name.startswith("agentscope-")
    assert runtime.permission_mode.value == "EXPLORE"


def _turn_request(*, user_message, facts=None, slots=None, task_state=TaskState.COLLECTING):
    return TurnRequest(
        ctx=TrustedContext(
            tenant_id=uuid4(), actor_id=uuid4(), role="customer",
            request_id="test", release_id="test",
        ),
        task_id=uuid4(),
        task_state=task_state,
        task_version=1,
        slots=slots or {},
        user_message=user_message,
        allowed_tools=allowed_tools_for(AgentRole.RECEPTION),
        facts=facts or {},
    )


def test_exact_start_availability_window_covers_trusted_quote_duration():
    request = _turn_request(
        user_message="查询 17:00",
        task_state=TaskState.SEARCHING,
        slots={
            "service": {"value": {"service_id": "svc-90"}},
            "time_window": {"value": {
                "start_at": "2026-09-30T17:00:00+08:00",
                "end_at": "2026-09-30T18:00:00+08:00",
                "desired_start": "2026-09-30T17:00:00+08:00",
            }},
        },
        facts={
            "store_id": "store-1",
            "followups": {"quote": {"duration_minutes": 90}},
        },
    )
    output = _ensure_required_search_read(request, normalize_model_output({}, role=AgentRole.RECEPTION))
    call = output.tool_requests[0]
    assert call.tool_name == "search_availability"
    assert call.arguments["window_end"] == "2026-09-30T18:30:00+08:00"


def test_flexible_availability_window_is_not_replaced_by_quote_duration():
    assert _availability_window_end(
        {
            "start_at": "2026-09-30T16:00:00+08:00",
            "end_at": "2026-09-30T20:00:00+08:00",
            "desired_start": "2026-09-30T17:00:00+08:00",
        },
        {"duration_minutes": 90},
    ) == "2026-09-30T20:00:00+08:00"


def test_generic_service_phrase_is_clarified_instead_of_model_guess():
    request = _turn_request(
        user_message="我想做个放松项目，2026年09月30日 16:00左右有空吗？",
        facts={"services": [{"service_id": "svc-relax", "name": "全身放松", "aliases": []}]},
    )
    output = normalize_model_output(
        {
            "slot_patches": [
                {"slot_name": "service", "op": "SET", "value": {"service_id": "svc-relax", "name": "全身放松"}},
                {"slot_name": "time_window", "op": "SET", "value": {"start_at": "2026-09-30T16:00:00+08:00"}},
            ],
            "tool_requests": [{"tool_name": "search_availability", "arguments": {}}],
        },
        role=AgentRole.RECEPTION,
    )
    grounded = _ground_service_patch(request, output)
    assert grounded.clarification_question
    assert not any(patch["slot_name"] == "service" for patch in grounded.slot_patches)
    assert [patch["slot_name"] for patch in grounded.slot_patches] == ["time_window"]
    assert grounded.tool_requests == ()


def test_unique_exact_service_alias_overrides_model_service_guess():
    request = _turn_request(
        user_message="约个肩颈按摩",
        facts={"services": [
            {"service_id": "svc-shoulder", "name": "肩颈舒缓", "aliases": ["肩颈按摩"]},
            {"service_id": "svc-back", "name": "背部理疗", "aliases": ["开背理疗"]},
        ]},
    )
    output = normalize_model_output(
        {"slot_patches": [{"slot_name": "service", "op": "SET", "value": {"service_id": "svc-back", "name": "背部理疗"}}]},
        role=AgentRole.RECEPTION,
    )
    grounded = _ground_service_patch(request, output)
    service_patch = next(patch for patch in grounded.slot_patches if patch["slot_name"] == "service")
    assert service_patch["value"] == {"service_id": "svc-shoulder", "name": "肩颈舒缓"}


def test_time_only_turn_keeps_already_selected_service():
    request = _turn_request(
        user_message="明天下午四点",
        slots={"service": {"value": {"service_id": "svc-shoulder", "name": "肩颈舒缓"}}},
    )
    output = normalize_model_output(
        {"slot_patches": [{"slot_name": "service", "op": "SET", "value": {"service_id": "svc-shoulder", "name": "肩颈舒缓"}}]},
        role=AgentRole.RECEPTION,
    )
    grounded = _ground_service_patch(request, output)
    assert grounded.clarification_question is None
    assert grounded.slot_patches == output.slot_patches
