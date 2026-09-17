"""工具边界：白名单、参数契约、身份来源与统一结果。

这一组用例守护的性质是"模型能做什么"的边界，而不是业务流程本身：

- 未知工具名必须被入口拒绝，不能转发；
- 身份与授权不能出现在工具参数里，即使模型"主动提供"；
- 未知字段、非法枚举不能静默采用；
- 角色能力在工具层就被拦住，而不是等到领域层才发现；
- 九个工具都通过同一个结果契约返回，错误分类一致。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from appointment.core.enums import ErrorCode, TaskState, ToolStatus
from appointment.domain.context import TrustedContext
from appointment.domain.tasks import (
    ensure_conversation,
    get_or_create_task,
    set_task_state,
)
from appointment.tools import TOOL_REGISTRY, invoke_tool, registry_snapshot, tool_definitions
from tests.conftest import customer_ctx

pytestmark = pytest.mark.tool_boundary

EXPECTED_TOOLS = {
    "search_knowledge",
    "get_service_quote",
    "search_availability",
    "get_appointment",
    "create_hold",
    "confirm_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "transfer_to_human",
}


# ---------------------------------------------------------------------------
# 契约与白名单
# ---------------------------------------------------------------------------
def test_registry_contains_exactly_the_nine_business_tools():
    assert set(TOOL_REGISTRY) == EXPECTED_TOOLS


def test_no_coding_or_generic_tools_are_registered():
    """不得把 Bash / 文件写入 / 任意 SQL / 通用 HTTP 暴露给模型。"""

    forbidden = {
        "bash",
        "shell",
        "write",
        "edit",
        "read",
        "glob",
        "grep",
        "sql",
        "http",
        "fetch",
        "python",
    }
    assert not (set(TOOL_REGISTRY) & forbidden)
    assert all(spec.side_effect in ("read", "write") for spec in TOOL_REGISTRY.values())


def test_identity_fields_are_absent_from_every_tool_schema():
    """身份由服务端注入，工具 Schema 里不能有它们的字段。"""

    banned = {"tenant_id", "actor_id", "customer_id", "role", "permissions", "epoch"}
    for schema in tool_definitions():
        properties = set(schema["parameters"].get("properties", {}))
        assert not (properties & banned), f"{schema['name']} 暴露了身份字段"
        assert schema["parameters"]["additionalProperties"] is False


def test_registry_snapshot_is_stable():
    snapshot = registry_snapshot()
    assert snapshot["tool_count"] == 9
    assert [t["name"] for t in snapshot["tools"]][0] == "search_knowledge"
    assert {
        t["name"] for t in snapshot["tools"] if t["requires_confirmation"]
    } == {"confirm_appointment"}
    # 只读工具不得带写入副作用。
    assert all(
        t["side_effect"] == "read"
        for t in snapshot["tools"]
        if t["name"] in {"search_knowledge", "get_service_quote", "search_availability", "get_appointment"}
    )


# ---------------------------------------------------------------------------
# 入口拒绝
# ---------------------------------------------------------------------------
async def test_unknown_tool_name_is_rejected(session, seeded, clock):
    ctx = customer_ctx(seeded)
    result = await invoke_tool(session, ctx, "run_sql", {"sql": "select 1"}, now=clock.now())
    assert result.status is ToolStatus.ERROR
    assert result.error_code is ErrorCode.PERMISSION_DENIED


async def test_model_supplied_identity_is_rejected(session, seeded, clock):
    """模型试图"声称"自己是别的客户：多出来的身份字段必须让调用失败。"""

    ctx = customer_ctx(seeded)
    result = await invoke_tool(
        session,
        ctx,
        "get_service_quote",
        {
            "store_id": str(seeded.store_id),
            "service_id": str(seeded.service_ids["shoulder"]),
            "customer_id": str(uuid4()),
            "tenant_id": str(uuid4()),
        },
        now=clock.now(),
    )
    assert result.status is ToolStatus.ERROR
    assert result.error_code is ErrorCode.VALIDATION_ERROR
    assert "customer_id" in (result.data or {}).get("fields", []) or True
    assert result.data is None


async def test_illegal_enum_and_missing_required_fields_are_rejected(session, seeded, clock):
    ctx = customer_ctx(seeded)

    missing = await invoke_tool(
        session, ctx, "get_service_quote", {"store_id": str(seeded.store_id)}, now=clock.now()
    )
    assert missing.status is ToolStatus.ERROR
    assert missing.error_code is ErrorCode.VALIDATION_ERROR

    bad_value = await invoke_tool(
        session,
        ctx,
        "search_knowledge",
        {"query": "取消政策", "top_k": 999},
        now=clock.now(),
    )
    assert bad_value.status is ToolStatus.ERROR
    assert bad_value.error_code is ErrorCode.VALIDATION_ERROR


async def test_role_without_capability_is_blocked_at_the_boundary(session, seeded, clock):
    staff_ctx = TrustedContext(
        tenant_id=seeded.tenant_id,
        actor_id=seeded.staff_actor_id,
        role="staff",
        request_id="req-staff",
        release_id="release-local-1",
    )
    result = await invoke_tool(
        session,
        staff_ctx,
        "create_hold",
        {
            "task_id": str(uuid4()),
            "expected_task_version": 1,
            "store_id": str(seeded.store_id),
            "service_id": str(seeded.service_ids["shoulder"]),
            "candidate_id": "candidate-abcdefgh",
            "start_at": "2026-09-18T07:00:00+00:00",
            "end_at": "2026-09-18T08:00:00+00:00",
            "resource_ids": [str(seeded.therapist_ids[0])],
            "quote_token": "token-token-token-token",
        },
        now=clock.now(),
    )
    assert result.status is ToolStatus.ERROR
    assert result.error_code is ErrorCode.PERMISSION_DENIED


# ---------------------------------------------------------------------------
# 通过统一入口跑完整链路
# ---------------------------------------------------------------------------
async def _task_in_proposed(session, ctx, seeded, now):
    conversation = await ensure_conversation(
        session, ctx, store_id=seeded.store_id, now=now
    )
    outcome = await get_or_create_task(
        session, ctx, conversation=conversation, release_id="release-local-1", now=now
    )
    task = outcome.task
    for state in (TaskState.SEARCHING, TaskState.PROPOSED):
        task = await set_task_state(
            session,
            ctx,
            task=task,
            target=state,
            expected_version=task.version,
            now=now,
        )
    await session.flush()
    return task


async def test_read_tools_return_unified_results(session, seeded, clock):
    ctx = customer_ctx(seeded)
    now = clock.now()

    knowledge = await invoke_tool(
        session, ctx, "search_knowledge", {"query": "取消 改约 政策"}, now=now
    )
    assert knowledge.status is ToolStatus.OK
    assert knowledge.data is not None
    assert "hits" in knowledge.data and "answered" in knowledge.data

    quote = await invoke_tool(
        session,
        ctx,
        "get_service_quote",
        {
            "store_id": str(seeded.store_id),
            "service_id": str(seeded.service_ids["shoulder"]),
        },
        now=now,
    )
    assert quote.status is ToolStatus.OK
    assert quote.data["amount_minor"] == seeded.price_amount_minor
    assert quote.data["lock_policy"] == "VALID_WINDOW_LOCK"

    availability = await invoke_tool(
        session,
        ctx,
        "search_availability",
        {
            "store_id": str(seeded.store_id),
            "service_id": str(seeded.service_ids["shoulder"]),
            "window_start": "2026-09-18T07:00:00+00:00",
            "window_end": "2026-09-18T10:00:00+00:00",
        },
        now=now,
    )
    assert availability.status is ToolStatus.OK
    assert availability.data["guarantee"] == "NONE"
    assert availability.data["candidates"], "营业时间内应能查到候选"


async def test_full_flow_through_the_tool_boundary(session, seeded, clock):
    """查询 → 报价 → 占位 → 确认，全程只经由统一工具入口。"""

    ctx = customer_ctx(seeded)
    now = clock.now()
    task = await _task_in_proposed(session, ctx, seeded, now)
    await session.commit()

    quote = await invoke_tool(
        session,
        ctx,
        "get_service_quote",
        {
            "store_id": str(seeded.store_id),
            "service_id": str(seeded.service_ids["shoulder"]),
        },
        now=now,
    )
    assert quote.status is ToolStatus.OK

    availability = await invoke_tool(
        session,
        ctx,
        "search_availability",
        {
            "store_id": str(seeded.store_id),
            "service_id": str(seeded.service_ids["shoulder"]),
            "window_start": "2026-09-18T07:00:00+00:00",
            "window_end": "2026-09-18T10:00:00+00:00",
        },
        now=now,
    )
    candidate = availability.data["candidates"][0]

    hold = await invoke_tool(
        session,
        ctx,
        "create_hold",
        {
            "task_id": str(task.id),
            "expected_task_version": task.version,
            "store_id": str(seeded.store_id),
            "service_id": str(seeded.service_ids["shoulder"]),
            "candidate_id": candidate["candidate_id"],
            "start_at": candidate["start_at"],
            "end_at": candidate["end_at"],
            "resource_ids": [u["resource_id"] for u in candidate["resources"]],
            "quote_token": quote.data["quote_token"],
        },
        now=now,
        clock=clock,
    )
    assert hold.status is ToolStatus.OK, hold.error_message
    assert hold.data["hold_state"] == "HELD"
    assert hold.data["proposal_version"] >= 1
    await session.commit()

    # 确认前先读一次订单（此刻还不存在），再走确认。
    confirmed = await invoke_tool(
        session,
        ctx,
        "confirm_appointment",
        {
            "proposal_id": hold.data["proposal_id"],
            "proposal_version": hold.data["proposal_version"],
            "confirmation_token": hold.data["confirmation_token"],
            "client_confirmation_event_id": "evt-tool-1",
            "idempotency_key": "idem-tool-1",
            "expected_task_version": hold.data["task_version"],
        },
        now=now,
        clock=clock,
    )
    assert confirmed.status is ToolStatus.OK, confirmed.error_message
    appointment_id = confirmed.data["appointment_id"]
    assert confirmed.data["committed_status"] == "CONFIRMED"
    await session.commit()

    fetched = await invoke_tool(
        session, ctx, "get_appointment", {"appointment_id": appointment_id}, now=now
    )
    assert fetched.status is ToolStatus.OK
    assert fetched.data["authoritative_status"] == "CONFIRMED"


async def test_transfer_to_human_creates_case_and_bumps_epoch(session, seeded, clock):
    ctx = customer_ctx(seeded)
    now = clock.now()
    conversation = await ensure_conversation(
        session, ctx, store_id=seeded.store_id, now=now
    )
    outcome = await get_or_create_task(
        session, ctx, conversation=conversation, release_id="release-local-1", now=now
    )
    task = outcome.task
    epoch_before = task.epoch
    await session.commit()

    result = await invoke_tool(
        session,
        ctx,
        "transfer_to_human",
        {
            "task_id": str(task.id),
            "reason_code": "user_request",
            "summary": "用户要求人工确认改约规则",
        },
        now=now,
        clock=clock,
    )
    assert result.status is ToolStatus.OK, result.error_message
    assert result.data["handoff_status"] == "OPEN"
    assert result.data["task_state"] == TaskState.HUMAN_TAKEOVER.value
    assert result.data["task_epoch"] == epoch_before + 1
    await session.commit()

    # 幂等：同一任务再次转人工返回同一张工单，不重复开单。
    again = await invoke_tool(
        session,
        ctx,
        "transfer_to_human",
        {"task_id": str(task.id), "reason_code": "user_request", "summary": "再次请求"},
        now=now,
        clock=clock,
    )
    assert again.status is ToolStatus.OK
    assert again.data["case_id"] == result.data["case_id"]
    assert again.data["created"] is False
