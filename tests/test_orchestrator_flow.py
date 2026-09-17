"""编排器端到端：一条自然语言消息驱动整条主链路。

用确定性基线跑，因此断言是确定的：不依赖模型输出，也不需要网络。
覆盖的性质：

- 一条消息即可完成"解析槽位 → 取价 → 查候选"并推进到 PROPOSED；
- 状态推进只在事实齐备时发生（缺槽位就追问，不猜）；
- 只读调用可批处理，写入调用逐个执行；
- 执行权用 CAS/租约，第二个执行者拿不到同一任务；
- 追问会持久化为 waiting_request，答复后从可信账本重建上下文；
- 重复调用会被识别为循环并止损，而不是"看起来在推进"。
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from appointment.agent import DeterministicRuntime
from appointment.core.enums import (
    ErrorCode,
    ExecutionEndReason,
    TaskEventType,
    TaskState,
    ToolStatus,
    WaitingKind,
    WaitingStatus,
)
from appointment.core.errors import DomainError
from appointment.db import models as m
from appointment.orchestrator import Orchestrator
from tests.conftest import customer_ctx

pytestmark = pytest.mark.invariant


def _orchestrator(session, clock) -> Orchestrator:
    return Orchestrator(session, runtime=DeterministicRuntime(), clock=clock)


async def test_single_message_drives_to_proposed(session, seeded, clock):
    ctx = customer_ctx(seeded)
    now = clock.now()
    orch = _orchestrator(session, clock)

    report = await orch.handle_user_message(
        ctx,
        text="我想约肩颈，明天下午三点",
        client_message_id="msg-1",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    assert report.task_state == TaskState.PROPOSED.value, report.to_dict()
    called = [entry["tool"] for entry in report.tool_calls]
    assert called == ["get_service_quote", "search_availability"], called
    assert report.reply_text, "应把候选时段回给用户"

    task = (
        await session.execute(select(m.Task).where(m.Task.id == report.task_id))
    ).scalar_one()
    assert task.slots["service"]["value"]["service_id"] == str(
        seeded.service_ids["shoulder"]
    )
    assert task.slots["time_window"]["value"]["local_date"] == "2026-09-18"
    # 没有未关闭的执行租约：执行者必须在结束时释放。
    assert task.lease_owner is None

    events = (
        await session.execute(
            select(m.TaskEvent.type).where(m.TaskEvent.task_id == task.id)
        )
    ).scalars().all()
    assert TaskEventType.CANDIDATES_READY.value in events


async def test_missing_slots_lead_to_persistent_clarification(session, seeded, clock):
    ctx = customer_ctx(seeded)
    now = clock.now()
    orch = _orchestrator(session, clock)

    report = await orch.handle_user_message(
        ctx,
        text="你好",
        client_message_id="msg-hello",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    assert report.clarification_question
    assert report.waiting_id is not None
    assert report.task_state == TaskState.WAITING_USER.value
    assert report.tool_calls == [], "缺槽位时不应发起任何业务调用"

    waiting = (
        await session.execute(
            select(m.WaitingRequest).where(m.WaitingRequest.id == report.waiting_id)
        )
    ).scalar_one()
    assert waiting.kind == WaitingKind.CLARIFICATION.value
    assert waiting.status == WaitingStatus.OPEN.value

    # 答复后从可信账本重建上下文并继续。
    resumed = await orch.resume_after_waiting(
        ctx,
        task_id=report.task_id,
        answer={"text": "肩颈，明天下午三点", "task_version": waiting.task_version},
        client_event_id="evt-answer-1",
        now=now,
    )
    await session.commit()
    assert resumed.task_state == TaskState.PROPOSED.value, resumed.to_dict()

    answered = (
        await session.execute(
            select(m.WaitingRequest).where(m.WaitingRequest.id == waiting.id)
        )
    ).scalar_one()
    assert answered.status == WaitingStatus.ANSWERED.value


async def test_stale_answer_is_rejected_without_reviving_task(session, seeded, clock):
    ctx = customer_ctx(seeded)
    now = clock.now()
    orch = _orchestrator(session, clock)

    report = await orch.handle_user_message(
        ctx,
        text="你好",
        client_message_id="msg-hello",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    # 用一个过时的 task_version 提交答案：只留审计，不推进任务状态。
    resumed = await orch.resume_after_waiting(
        ctx,
        task_id=report.task_id,
        answer={"text": "肩颈", "task_version": 1},
        client_event_id="evt-stale",
        now=now,
    )
    await session.commit()

    assert resumed.end_reason == "STALE_ANSWER_REJECTED"
    assert resumed.task_state == TaskState.WAITING_USER.value

    answers = (
        await session.execute(
            select(m.WaitingAnswer).where(m.WaitingAnswer.client_event_id == "evt-stale")
        )
    ).scalars().all()
    assert [a.validation_status for a in answers] == ["REJECTED_STALE"]


async def test_user_selection_creates_single_hold(session, seeded, clock, tomorrow_window):
    ctx = customer_ctx(seeded)
    now = clock.now()
    orch = _orchestrator(session, clock)

    first = await orch.handle_user_message(
        ctx,
        text="我要约肩颈，明天下午三点",
        client_message_id="msg-1",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()
    assert first.task_state == TaskState.PROPOSED.value

    second = await orch.handle_user_message(
        ctx,
        text="第一个可以",
        client_message_id="msg-2",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    assert second.task_state == TaskState.WAITING_CONFIRMATION.value, second.to_dict()
    hold_calls = [entry for entry in second.tool_calls if entry["tool"] == "create_hold"]
    assert len(hold_calls) == 1, "只应为用户选定的那一个候选创建占位"
    assert hold_calls[0]["status"] == ToolStatus.OK.value

    holds = (
        await session.execute(
            select(m.Hold).where(m.Hold.tenant_id == seeded.tenant_id)
        )
    ).scalars().all()
    assert len(holds) == 1

    allocations = (
        await session.execute(
            select(m.ResourceAllocation).where(
                m.ResourceAllocation.state.in_(["HELD", "BOOKED"])
            )
        )
    ).scalars().all()
    assert len(allocations) == 2, "肩颈服务应占用技师 + 房间各一个单位"


async def test_second_executor_cannot_take_the_same_task(session, seeded, clock):
    """执行权用 CAS + 租约：并发执行者不能同时处理同一个任务。"""

    ctx_a = customer_ctx(seeded, request_id="req-a")
    ctx_b = customer_ctx(seeded, request_id="req-b")
    now = clock.now()

    orch_a = _orchestrator(session, clock)
    report = await orch_a.handle_user_message(
        ctx_a,
        text="你好",
        client_message_id="msg-a",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    task = (
        await session.execute(select(m.Task).where(m.Task.id == report.task_id))
    ).scalar_one()

    # 手工占住租约，模拟另一个执行者正在处理。必须提交：别人的租约是既成事实，
    # 不能随本次请求回滚。
    task.lease_owner = "req-other"
    task.lease_until = now + timedelta(seconds=60)
    await session.commit()

    orch_b = _orchestrator(session, clock)
    with pytest.raises(DomainError) as excinfo:
        await orch_b.handle_user_message(
            ctx_b,
            text="你好",
            client_message_id="msg-b",
            store_id=seeded.store_id,
            now=now,
        )
    assert excinfo.value.code in (ErrorCode.LEASE_LOST, ErrorCode.NOT_FOUND)
    await session.rollback()

    reloaded = (
        await session.execute(select(m.Task).where(m.Task.id == report.task_id))
    ).scalar_one()
    assert reloaded.lease_owner == "req-other", "别人的租约不能被抢走"


async def test_proposal_turn_never_holds_without_a_selection(session, seeded, clock):
    """给出候选的那一轮绝不能顺手占位：用户还没说 要哪个。"""

    ctx = customer_ctx(seeded)
    now = clock.now()
    orch = _orchestrator(session, clock)

    report = await orch.handle_user_message(
        ctx,
        text="我要约肩颈，明天下午三点",
        client_message_id="msg-1",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    written = [entry for entry in report.tool_calls if entry["tool"] == "create_hold"]
    assert written == [], report.to_dict()
    assert report.task_state == TaskState.PROPOSED.value
    assert "1." in (report.reply_text or ""), "应把候选摆给用户选"

    holds = (
        await session.execute(
            select(m.Hold).where(m.Hold.tenant_id == seeded.tenant_id)
        )
    ).scalars().all()
    assert holds == [], "没有选定就不能有占位"


async def test_changing_time_after_proposal_invalidates_candidates(
    session, seeded, clock
):
    """改时间要作废旧候选并重查，而不是拿旧候选去占位。"""

    ctx = customer_ctx(seeded)
    now = clock.now()
    orch = _orchestrator(session, clock)

    first = await orch.handle_user_message(
        ctx,
        text="我要约肩颈，明天下午三点",
        client_message_id="msg-1",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()
    assert first.task_state == TaskState.PROPOSED.value

    second = await orch.handle_user_message(
        ctx,
        text="改到后天下午三点",
        client_message_id="msg-2",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    assert second.task_state == TaskState.PROPOSED.value, second.to_dict()
    called = [entry["tool"] for entry in second.tool_calls]
    # 重新取证：这一轮的价格与候选都要按新条件重查过，不能用上一条消息里的事实。
    assert called == ["get_service_quote", "search_availability"], second.to_dict()
    assert all(entry["status"] == ToolStatus.OK.value for entry in second.tool_calls)

    task = (
        await session.execute(select(m.Task).where(m.Task.id == second.task_id))
    ).scalar_one()
    assert task.slots["time_window"]["value"]["local_date"] == "2026-09-19"
    holds = (
        await session.execute(
            select(m.Hold).where(m.Hold.tenant_id == seeded.tenant_id)
        )
    ).scalars().all()
    assert holds == []


async def test_unrelated_reply_does_not_pick_a_candidate(session, seeded, clock):
    """方案页收到无关问题时，只重述候选，不占位。"""

    ctx = customer_ctx(seeded)
    now = clock.now()
    orch = _orchestrator(session, clock)

    await orch.handle_user_message(
        ctx,
        text="我要约肩颈，明天下午三点",
        client_message_id="msg-1",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    report = await orch.handle_user_message(
        ctx,
        text="你们几点关门",
        client_message_id="msg-2",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    assert report.task_state == TaskState.PROPOSED.value, report.to_dict()
    assert all(entry["tool"] != "create_hold" for entry in report.tool_calls)
    assert "序号" in (report.reply_text or ""), report.reply_text


async def test_repeated_identical_calls_are_capped_by_budget(session, seeded, clock):
    """相同工具+参数重复出现会被识别为无进展并止损，而不是无限循环。"""

    from appointment.orchestrator.budget import TurnBudget

    budget = TurnBudget(max_tool_calls=6, repeat_tolerance=2)
    arguments = {"store_id": "s", "service_id": "x"}
    assert budget.observe_call("get_service_quote", arguments) is False
    assert budget.observe_call("get_service_quote", arguments) is True
    assert budget.observe_call("get_service_quote", arguments) is True
    assert budget.is_looping() is True

    from datetime import datetime, timezone

    now = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)
    assert budget.stop_reason(started_at=now, now=now) is ExecutionEndReason.LOOP_DETECTED


async def test_budget_stops_after_max_tool_calls(session, seeded, clock):
    from appointment.orchestrator.budget import TurnBudget

    budget = TurnBudget(max_tool_calls=2)
    for _ in range(2):
        budget.record_tool_call()
    assert budget.can_call_tool() is False
    from datetime import datetime, timezone

    now = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)
    assert (
        budget.stop_reason(started_at=now, now=now)
        is ExecutionEndReason.BUDGET_EXHAUSTED
    )
    assert budget.snapshot()["tool_calls"] == 2


async def test_query_tool_failure_is_not_reported_as_no_availability(session, seeded, clock):
    """依赖失败不能描述成"没有号"（设计稿 §11 恢复表）。"""

    ctx = customer_ctx(seeded)
    now = clock.now()
    orch = _orchestrator(session, clock)

    result = await orch.session.execute(select(m.Store).where(m.Store.id == seeded.store_id))
    for row in result.scalars():
        row.status = "SUSPENDED"
    await session.flush()

    report = await orch.handle_user_message(
        ctx,
        text="我想约肩颈，明天下午三点",
        client_message_id="msg-suspended",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    failed = [e for e in report.tool_calls if e["status"] != ToolStatus.OK.value]
    assert failed, "门店停用时调用应失败"
    assert report.task_state != TaskState.PROPOSED.value
    assert "没有" not in (report.reply_text or "")


# ---------------------------------------------------------------------------
# 故障自愈与"清理不许掩盖真实失败"
#
# 这两条都是人工测试在真实进程上打出来的缺陷，单测此前只在"一切顺利"的
# 路径上跑，所以没暴露：
#
# 1. 事件序号取自 task.next_event_sequence 这个缓存，而唯一约束认的是
#    task_event 里已落库的行。两者脱节时，每一次 emit 都会撞唯一约束。
# 2. 那个失败被打成"待回滚"的会话状态，finally 里的清理去碰 task.id
#    又抛 PendingRollbackError，把唯一约束冲突整条盖掉。
# ---------------------------------------------------------------------------


async def test_stale_event_sequence_counter_self_heals(session, seeded, clock):
    """序号计数器落后于已落库事实时，下一次写入必须自愈，而不是硬失败。"""

    from sqlalchemy import update

    from appointment.domain.events import latest_task_event_sequence

    ctx = customer_ctx(seeded)
    now = clock.now()
    orch = _orchestrator(session, clock)

    first = await orch.handle_user_message(
        ctx,
        text="我想约肩颈，明天下午三点",
        client_message_id="drift-1",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    max_before = await latest_task_event_sequence(
        session, tenant_id=ctx.tenant_id, task_id=first.task_id
    )
    assert max_before > 1, "首轮应已写入多个事件"

    # 制造脱节：计数器退回到"远低于已落库最大序号"的状态。
    await session.execute(
        update(m.Task).where(m.Task.id == first.task_id).values(next_event_sequence=1)
    )
    await session.commit()
    session.expire_all()

    second = await orch.handle_user_message(
        ctx,
        text="第一个",
        client_message_id="drift-2",
        store_id=seeded.store_id,
        now=now,
    )
    await session.commit()

    max_after = await latest_task_event_sequence(
        session, tenant_id=ctx.tenant_id, task_id=second.task_id
    )
    assert max_after > max_before, "本轮必须继续在新序号上写入，而不是复用旧号"

    # 计数器已被抬到事实之上：下一次写入不会再依赖"未被污染过的缓存"。
    task = (
        await session.execute(select(m.Task).where(m.Task.id == first.task_id))
    ).scalar_one()
    assert task.next_event_sequence > max_after

    sequences = (
        (
            await session.execute(
                select(m.TaskEvent.sequence)
                .where(m.TaskEvent.task_id == first.task_id)
                .order_by(m.TaskEvent.sequence)
            )
        )
        .scalars()
        .all()
    )
    assert len(sequences) == len(set(sequences)), "序号不得重复"
    assert sequences == sorted(sequences), "序号必须单调"


async def test_turn_cleanup_never_masks_the_real_failure(
    session, seeded, clock, monkeypatch
):
    """收尾阶段失败时，必须保留原始异常，而不是让清理异常顶替它。"""

    from sqlalchemy.exc import IntegrityError

    ctx = customer_ctx(seeded)
    orch = _orchestrator(session, clock)

    real_failure = IntegrityError("insert task_event", {}, Exception("duplicate key"))

    async def _explode(*args, **kwargs):
        raise real_failure

    async def _cleanup_explodes(*args, **kwargs):
        raise RuntimeError("收尾也炸了")

    monkeypatch.setattr(Orchestrator, "_invoke_runtime", _explode)
    monkeypatch.setattr(Orchestrator, "_release_lease", _cleanup_explodes)

    with pytest.raises(IntegrityError) as caught:
        await orch.handle_user_message(
            ctx,
            text="我想约肩颈，明天下午三点",
            client_message_id="mask-1",
            store_id=seeded.store_id,
            now=clock.now(),
        )

    assert caught.value is real_failure, "上报的必须是真正的失败原因"


async def test_cleanup_failure_is_surfaced_when_the_turn_succeeded(
    session, seeded, clock, monkeypatch
):
    """本轮本身成功时，收尾失败是真问题，不能被吞掉。"""

    ctx = customer_ctx(seeded)
    orch = _orchestrator(session, clock)

    async def _cleanup_explodes(*args, **kwargs):
        raise RuntimeError("收尾炸了")

    monkeypatch.setattr(Orchestrator, "_release_lease", _cleanup_explodes)

    with pytest.raises(RuntimeError, match="收尾炸了"):
        await orch.handle_user_message(
            ctx,
            text="我想约肩颈，明天下午三点",
            client_message_id="mask-2",
            store_id=seeded.store_id,
            now=clock.now(),
        )
