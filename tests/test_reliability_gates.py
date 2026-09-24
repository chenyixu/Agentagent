"""恢复与权限新增门禁；数据库用例由独立 schema 承载。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy import update

from appointment.agent import DeterministicRuntime
from appointment.agent.ports import ToolRequest, TurnOutput
from appointment.agent.agentscope_adapter import (
    AgentScopeRuntime,
    _ensure_explicit_candidate_hold,
    _ensure_required_search_read,
    _reject_model_write_requests,
)
from appointment.agent.ports import TurnRequest
from appointment.api.deps import build_runtime_for_settings
from appointment.config.settings import Settings
from appointment.core.enums import ErrorCode, TaskState, ToolStatus, WaitingStatus
from appointment.core.errors import DomainError
from appointment.db import models as m
from appointment.db.session import get_sessionmaker
from appointment.domain.context import TrustedContext
from appointment.domain.tasks import ensure_conversation, get_or_create_task, set_task_state
from appointment.orchestrator import Orchestrator
from appointment.tools.registry import invoke_tool
from appointment.db import session as db_session
from tests.conftest import customer_ctx


def test_database_schema_override_is_local_and_identifier_safe():
    settings = Settings(
        database_url="postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
        env="test",
        db_schema="live_browser_acceptance",
    )
    assert settings.db_schema == "live_browser_acceptance"

    with pytest.raises(ValueError, match="安全的小写 PostgreSQL 标识符"):
        Settings(
            database_url=settings.database_url,
            env="test",
            db_schema='test; DROP SCHEMA public CASCADE',
        )

    with pytest.raises(ValueError, match="仅允许在 local_dev/test"):
        Settings(
            database_url=settings.database_url,
            env="production",
            allow_temp_identity=False,
            oidc_issuer="https://issuer.example",
            oidc_audience="appointment",
            oidc_jwks_url="https://issuer.example/jwks",
            db_schema="live_browser_acceptance",
        )


def test_database_engine_applies_schema_search_path(monkeypatch):
    settings = Settings(
        database_url="postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
        env="test",
        db_schema="live_browser_acceptance",
    )
    captured = {}
    engine = object()
    monkeypatch.setattr(db_session, "get_settings", lambda: settings)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_sessionmaker", None)
    monkeypatch.setattr(
        db_session,
        "create_async_engine",
        lambda url, **kwargs: captured.update({"url": url, **kwargs}) or engine,
    )

    assert db_session.get_engine() is engine
    assert captured["connect_args"] == {
        "server_settings": {"search_path": "live_browser_acceptance,public"}
    }


@pytest.mark.asyncio
async def test_stage_denies_write_before_parsing_arguments():
    ctx = TrustedContext(
        tenant_id=uuid4(), actor_id=uuid4(), customer_id=uuid4(),
        role="customer", request_id="stage-deny", release_id="test",
    )
    result = await invoke_tool(
        None, ctx, "create_hold", {},
        now=datetime(2026, 9, 17, tzinfo=timezone.utc),
        allowed_tools=("search_knowledge",),
    )
    assert result.status is ToolStatus.ERROR
    assert result.error_code is ErrorCode.PERMISSION_DENIED


def test_staff_without_store_scope_is_denied():
    ctx = TrustedContext(
        tenant_id=uuid4(), actor_id=uuid4(), role="staff",
        request_id="no-store", release_id="test",
    )
    with pytest.raises(DomainError) as error:
        ctx.require_store_scope(uuid4())
    assert error.value.code is ErrorCode.NOT_FOUND


async def test_agentscope_toolkit_only_contains_current_stage_tools():
    toolkit = await AgentScopeRuntime().build_toolkit(["search_knowledge"])
    schemas = await toolkit.get_tool_schemas()
    assert [item["function"]["name"] for item in schemas] == ["search_knowledge"]


def test_agentscope_requires_configured_provider():
    settings = Settings(
        database_url="postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
        env="test", agent_runtime="agentscope", model_backend="stub",
    )
    with pytest.raises(ValueError, match="需要 deepseek 或 dashscope"):
        build_runtime_for_settings(settings)


def test_deepseek_runtime_factory_uses_configured_model_without_network(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.invalid")
    settings = Settings(
        database_url="postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
        env="test",
        agent_runtime="agentscope",
        model_backend="deepseek",
        model_name="deepseek-flash",
        agent_prompt_version="reception-v3",
    )

    runtime = build_runtime_for_settings(settings)

    assert runtime.runtime_name == "agentscope-deepseek-reception-v3"
    assert runtime.config.model_name == "deepseek-flash"
    assert runtime._model is not None
    assert runtime._model.credential.base_url == "https://api.example.invalid"
    assert "test-only-not-a-real-key" not in repr(settings)
    assert "deepseek_api_key" not in settings.model_dump()


def test_deepseek_runtime_factory_rejects_missing_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = Settings(
        database_url="postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment_test",
        env="test",
        agent_runtime="agentscope",
        model_backend="deepseek",
        model_name="deepseek-flash",
        _env_file=None,
    )

    with pytest.raises(ValueError, match="缺少 DEEPSEEK_API_KEY"):
        build_runtime_for_settings(settings)


def test_searching_stage_schedules_required_reads_after_empty_model_decision():
    ctx = TrustedContext(
        tenant_id=uuid4(), actor_id=uuid4(), customer_id=uuid4(), role="customer",
        request_id="required-read", release_id="test",
    )
    base = TurnRequest(
        ctx=ctx, task_id=uuid4(), task_state=TaskState.SEARCHING,
        task_version=2,
        slots={
            "service": {"value": {"service_id": str(uuid4())}},
            "time_window": {"value": {
                "start_at": "2026-09-24T07:00:00+00:00",
                "end_at": "2026-09-24T08:00:00+00:00",
                "desired_start": "2026-09-24T07:00:00+00:00",
            }},
        },
        user_message=None,
        allowed_tools=("get_service_quote", "search_availability"),
        facts={"store_id": str(uuid4()), "followups": {}},
    )
    quote = _ensure_required_search_read(base, TurnOutput(reply_text="我来查"))
    assert [item.tool_name for item in quote.tool_requests] == ["get_service_quote"]
    assert quote.reply_text is None
    incomplete_quote = _ensure_required_search_read(
        base,
        TurnOutput(
            reply_text="我正在查询报价。",
            tool_requests=(ToolRequest(tool_name="get_service_quote", arguments={}),),
        ),
    )
    assert incomplete_quote.reply_text is None
    assert incomplete_quote.tool_requests[0].arguments == {
        "store_id": base.facts["store_id"],
        "service_id": base.slots["service"]["value"]["service_id"],
    }

    with_quote = replace(base, facts={**base.facts, "followups": {"quote": {"amount_minor": 100}}})
    availability = _ensure_required_search_read(with_quote, TurnOutput(reply_text="继续查询"))
    assert [item.tool_name for item in availability.tool_requests] == ["search_availability"]
    incomplete_availability = _ensure_required_search_read(
        with_quote,
        TurnOutput(tool_requests=(ToolRequest(
            tool_name="search_availability",
            arguments={"store_id": "untrusted-store", "limit": 999},
        ),)),
    )
    assert incomplete_availability.tool_requests[0].arguments == {
        "store_id": base.facts["store_id"],
        "service_id": base.slots["service"]["value"]["service_id"],
        "window_start": base.slots["time_window"]["value"]["start_at"],
        "window_end": base.slots["time_window"]["value"]["end_at"],
        "desired_start": base.slots["time_window"]["value"]["desired_start"],
        "limit": 5,
    }


def test_explicit_candidate_selection_uses_ledger_but_ambiguity_does_not_hold():
    ctx = TrustedContext(
        tenant_id=uuid4(), actor_id=uuid4(), customer_id=uuid4(), role="customer",
        request_id="selection", release_id="test",
    )
    candidates = [
        {
            "candidate_id": f"candidate-abcdefgh-{index}",
            "start_at": "2026-09-24T07:00:00+00:00",
            "end_at": "2026-09-24T08:00:00+00:00",
            "resources": [{"resource_id": str(uuid4())}],
        }
        for index in (0, 1)
    ]
    base = TurnRequest(
        ctx=ctx, task_id=uuid4(), task_state=TaskState.PROPOSED,
        task_version=4,
        slots={"service": {"value": {"service_id": str(uuid4())}}},
        user_message="第一个可以", allowed_tools=("create_hold",),
        facts={
            "store_id": str(uuid4()),
            "followups": {
                "availability": {"candidates": candidates},
                "quote": {"quote_token": "signed-quote-token-from-server"},
            },
        },
    )
    selected = _ensure_explicit_candidate_hold(base, TurnOutput(reply_text="先占位"))
    assert [item.tool_name for item in selected.tool_requests] == ["create_hold"]
    assert selected.tool_requests[0].arguments["candidate_id"] == candidates[0]["candidate_id"]
    assert selected.reply_text is None
    ambiguous = _ensure_explicit_candidate_hold(
        replace(base, user_message="可以"), TurnOutput(reply_text="请选一个"
        )
    )
    assert ambiguous.tool_requests == ()
    assert ambiguous.clarification_question

    # A model-issued write request is not an authorization token. Even when the
    # proposed arguments are syntactically valid, an ambiguous answer cannot hold.
    malicious_ambiguous = _ensure_explicit_candidate_hold(
        replace(base, user_message="可以"),
        TurnOutput(
            reply_text="已为您占位",
            tool_requests=(ToolRequest(
                tool_name="create_hold",
                arguments={"candidate_id": candidates[0]["candidate_id"]},
            ),),
        ),
    )
    assert malicious_ambiguous.tool_requests == ()
    assert malicious_ambiguous.clarification_question
    assert "已为您占位" not in (malicious_ambiguous.reply_text or "")

    # An affirmative in the original request cannot select a candidate that is
    # only discovered later in the same execution turn, even when it is unique.
    fresh_facts = {
        "store_id": base.facts["store_id"],
        "followups": {
            "availability": {"candidates": candidates[:1]},
            "quote": {"quote_token": "signed-quote-token-from-server"},
        },
    }
    pre_candidate_affirmation = _ensure_explicit_candidate_hold(
        replace(
            base,
            user_message="可以的话想预约",
            facts=fresh_facts,
            fresh_fact_keys=frozenset({"availability"}),
        ),
        TurnOutput(reply_text="我来帮您查询。"),
    )
    assert pre_candidate_affirmation.tool_requests == ()
    assert pre_candidate_affirmation.clarification_question

    model_selected = _ensure_explicit_candidate_hold(
        replace(base, user_message="第一个可以"),
        TurnOutput(tool_requests=(ToolRequest(
            tool_name="create_hold", arguments={"candidate_id": "forged"}
        ),)),
    )
    assert [item.tool_name for item in model_selected.tool_requests] == ["create_hold"]
    assert model_selected.tool_requests[0].arguments["candidate_id"] == candidates[0]["candidate_id"]

    prompt = AgentScopeRuntime().build_system_prompt(base)
    assert "create_hold" not in prompt
    assert "模型不得请求写工具" in prompt

    rejected_confirmation = _reject_model_write_requests(TurnOutput(
        reply_text="我已经替你下单",
        tool_requests=(ToolRequest("confirm_appointment", {"idempotency_key": "forged"}),),
    ))
    assert rejected_confirmation.tool_requests == ()
    assert "不能由模型直接提交" in (rejected_confirmation.reply_text or "")


async def test_completed_actions_are_scoped_to_one_task(session, seeded, clock):
    now = clock.now()
    first_ctx = customer_ctx(seeded, index=0, request_id="task-first")
    second_ctx = customer_ctx(seeded, index=1, request_id="task-second")
    first = await Orchestrator(session, runtime=DeterministicRuntime(), clock=clock).handle_user_message(
        first_ctx, text="预约肩颈，明天下午三点", client_message_id="first-msg",
        store_id=seeded.store_id, now=now,
    )
    second = await Orchestrator(session, runtime=DeterministicRuntime(), clock=clock).handle_user_message(
        second_ctx, text="预约肩颈，明天下午三点", client_message_id="second-msg",
        store_id=seeded.store_id, now=now,
    )
    assert first.task_id != second.task_id
    first_task = (await session.execute(select(m.Task).where(m.Task.id == first.task_id))).scalar_one()
    second_task = (await session.execute(select(m.Task).where(m.Task.id == second.task_id))).scalar_one()
    first_actions = await Orchestrator(session, runtime=DeterministicRuntime(), clock=clock)._completed_action_summaries(
        first_ctx, task=first_task
    )
    second_actions = await Orchestrator(session, runtime=DeterministicRuntime(), clock=clock)._completed_action_summaries(
        second_ctx, task=second_task
    )
    assert first_actions and second_actions
    first_ledger = (
        await session.execute(
            select(m.ToolExecution.id)
            .join(m.ExecutionAttempt, m.ExecutionAttempt.id == m.ToolExecution.attempt_id)
            .where(m.ExecutionAttempt.task_id == first.task_id)
        )
    ).scalars().all()
    assert len(first_actions) == len(first_ledger)
    assert len(second_actions) == len(first_ledger)


async def test_expired_waiting_answer_is_audited_without_resuming(session, seeded, clock):
    ctx = customer_ctx(seeded)
    orchestrator = Orchestrator(session, runtime=DeterministicRuntime(), clock=clock)
    first = await orchestrator.handle_user_message(
        ctx, text="你好", client_message_id="hello-expiry",
        store_id=seeded.store_id, now=clock.now(),
    )
    await session.commit()
    waiting = (await session.execute(
        select(m.WaitingRequest).where(m.WaitingRequest.id == first.waiting_id)
    )).scalar_one()
    result = await orchestrator.resume_after_waiting(
        ctx, task_id=first.task_id,
        answer={"text": "肩颈，明天下午三点", "task_version": waiting.task_version},
        client_event_id="expired-answer", now=clock.now() + timedelta(seconds=1801),
    )
    await session.commit()
    assert result.end_reason == "STALE_ANSWER_REJECTED"
    assert waiting.status == WaitingStatus.EXPIRED.value
    answer = (await session.execute(select(m.WaitingAnswer).where(
        m.WaitingAnswer.client_event_id == "expired-answer"
    ))).scalar_one()
    assert answer.validation_status == "REJECTED_STALE"


async def test_answer_event_is_idempotent_and_conflict_is_rejected(session, seeded, clock):
    ctx = customer_ctx(seeded)
    orchestrator = Orchestrator(session, runtime=DeterministicRuntime(), clock=clock)
    first = await orchestrator.handle_user_message(
        ctx, text="你好", client_message_id="hello-duplicate",
        store_id=seeded.store_id, now=clock.now(),
    )
    await session.commit()
    waiting = (await session.execute(
        select(m.WaitingRequest).where(m.WaitingRequest.id == first.waiting_id)
    )).scalar_one()
    payload = {"text": "肩颈，明天下午三点", "task_version": waiting.task_version}
    await orchestrator.resume_after_waiting(
        ctx, task_id=first.task_id, answer=payload,
        client_event_id="answer-once", now=clock.now(),
    )
    await session.commit()
    duplicate = await orchestrator.resume_after_waiting(
        ctx, task_id=first.task_id, answer=payload,
        client_event_id="answer-once", now=clock.now(),
    )
    assert duplicate.end_reason == "DUPLICATE_ANSWER"
    with pytest.raises(DomainError) as error:
        await orchestrator.resume_after_waiting(
            ctx, task_id=first.task_id,
            answer={**payload, "text": "别的内容"},
            client_event_id="answer-once", now=clock.now(),
        )
    assert error.value.code is ErrorCode.VALIDATION_ERROR


async def test_old_executor_cannot_emit_after_takeover_during_model_call(
    session, seeded, clock
):
    class TakeoverRuntime:
        runtime_name = "takeover-interleaving"

        async def run_turn(self, request):
            # 独立连接模拟模型请求尚未返回时人工接管。这里能提交，证明
            # 编排器没有在模型网络调用期间占着任务事务锁。
            async with get_sessionmaker()() as other:
                await other.execute(
                    update(m.Task)
                    .where(m.Task.id == request.task_id)
                    .values(
                        epoch=m.Task.epoch + 1,
                        fencing_token=m.Task.fencing_token + 1,
                        version=m.Task.version + 1,
                        lease_owner="human-takeover",
                    )
                )
                await other.commit()
            return TurnOutput(reply_text="旧执行器的回复不应成为事件")

    ctx = customer_ctx(seeded, request_id="old-executor")
    result = await Orchestrator(session, runtime=TakeoverRuntime(), clock=clock).handle_user_message(
        ctx, text="预约肩颈，明天下午三点", client_message_id="takeover-msg",
        store_id=seeded.store_id, now=clock.now(),
    )
    await session.commit()
    assert result.end_reason == "LEASE_LOST"
    assert result.reply_text is None
    assert (await session.execute(
        select(m.ToolExecution)
        .join(m.ExecutionAttempt, m.ExecutionAttempt.id == m.ToolExecution.attempt_id)
        .where(m.ExecutionAttempt.task_id == result.task_id)
    )).scalars().all() == []
    events = (await session.execute(
        select(m.TaskEvent).where(m.TaskEvent.task_id == result.task_id)
    )).scalars().all()
    assert all("旧执行器" not in str(event.payload) for event in events)


async def test_slot_patch_and_read_request_in_same_turn_use_new_task_version(
    session, seeded, clock, tomorrow_window
):
    """槽位与只读调用同轮返回时，版本递增不应误判成并发接管。"""

    class PatchAndQuoteRuntime:
        runtime_name = "patch-and-quote"

        def __init__(self):
            self.delegate = DeterministicRuntime()
            self.initial_turn = True

        async def run_turn(self, request):
            if self.initial_turn:
                self.initial_turn = False
                start, end, _ = tomorrow_window
                return TurnOutput(
                    slot_patches=(
                        {
                            "slot_name": "service",
                            "op": "SET",
                            "value": {
                                "service_id": str(seeded.service_ids["shoulder"]),
                                "name": "肩颈舒缓",
                            },
                        },
                        {
                            "slot_name": "time_window",
                            "op": "SET",
                            "value": {
                                "start_at": start.isoformat(),
                                "end_at": end.isoformat(),
                                "desired_start": start.isoformat(),
                            },
                        },
                    ),
                    tool_requests=(
                        ToolRequest(
                            tool_name="get_service_quote",
                            arguments={
                                "store_id": str(seeded.store_id),
                                "service_id": str(seeded.service_ids["shoulder"]),
                            },
                        ),
                    ),
                )
            return await self.delegate.run_turn(request)

    ctx = customer_ctx(seeded, request_id="patch-and-quote")
    result = await Orchestrator(
        session, runtime=PatchAndQuoteRuntime(), clock=clock
    ).handle_user_message(
        ctx,
        text="预约肩颈，明天下午",
        client_message_id="patch-and-quote-msg",
        store_id=seeded.store_id,
        now=clock.now(),
    )
    await session.commit()

    executions = (
        await session.execute(
            select(m.ToolExecution)
            .join(m.ExecutionAttempt, m.ExecutionAttempt.id == m.ToolExecution.attempt_id)
            .where(m.ExecutionAttempt.task_id == result.task_id)
            .order_by(m.ToolExecution.started_at)
        )
    ).scalars().all()
    assert result.end_reason == "COMPLETED", result
    assert result.task_state == TaskState.PROPOSED.value
    tool_names = [entry.tool_name for entry in executions]
    assert tool_names[0] == "get_service_quote"
    assert "search_availability" in tool_names
    assert set(tool_names[1:]) == {"search_availability"}
    assert all(entry.status == ToolStatus.OK.value for entry in executions)


@pytest.mark.parametrize(
    "target_state", [TaskState.PROPOSED, TaskState.WAITING_CONFIRMATION]
)
async def test_selection_and_confirmation_questions_keep_business_state(
    session, seeded, clock, target_state
):
    class CandidateReplyRuntime:
        runtime_name = "candidate-question"

        async def run_turn(self, request):
            question = (
                "请选择第一个或第二个候选。"
                if request.task_state is TaskState.PROPOSED
                else "请确认当前方案。"
            )
            return TurnOutput(
                reply_text=question,
                clarification_question=question,
            )

    ctx = customer_ctx(seeded, request_id="candidate-question")
    conversation = await ensure_conversation(
        session, ctx, store_id=seeded.store_id, now=clock.now()
    )
    task = (await get_or_create_task(
        session, ctx, conversation=conversation,
        release_id=ctx.release_id, now=clock.now(),
    )).task
    states = [TaskState.SEARCHING, TaskState.PROPOSED]
    if target_state is TaskState.WAITING_CONFIRMATION:
        states.append(target_state)
    for state in states:
        task = await set_task_state(
            session, ctx, task=task, target=state,
            expected_version=task.version, now=clock.now(),
        )
    await session.commit()
    result = await Orchestrator(
        session, runtime=CandidateReplyRuntime(), clock=clock
    ).handle_user_message(
        ctx, text="可以", client_message_id="candidate-question-msg",
        conversation_id=conversation.id, store_id=seeded.store_id, now=clock.now(),
    )
    await session.commit()
    assert result.task_state == target_state.value
    assert result.end_reason == "COMPLETED"
    assert result.waiting_id is None
    assert result.clarification_question == (
        "请选择第一个或第二个候选。"
        if target_state is TaskState.PROPOSED else "请确认当前方案。"
    )
    open_waitings = (await session.execute(select(m.WaitingRequest).where(
        m.WaitingRequest.task_id == task.id,
        m.WaitingRequest.status == WaitingStatus.OPEN.value,
    ))).scalars().all()
    assert open_waitings == []
