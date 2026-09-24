"""资源要求：技能归属、资源组合与提交路径复核。

这一组用例覆盖两个真实故障面：

1. **技能约束不能无差别套用到所有资源类型**。房间、设备这类资源不携带技能，
   若一并要求，"技师 + 房间"的服务会查不到任何候选——表现为一个没有解释的空结果。
2. **提交路径不能信任客户端回传的资源列表**。候选查询只负责构造组合；如果写入
   路径不独立复核，"技师 + 房间"可以被换成"两间房"，占用被错误锁定。

第 1 组是纯函数测试（不需要数据库）；第 2 组打到真实 PostgreSQL 上，因为
组合外键与排他约束的裁决只能在那里验证。
"""

from __future__ import annotations

import pytest

from appointment.core.enums import ErrorCode
from appointment.core.errors import DomainError
from appointment.db import models as m
from appointment.domain.availability import ResourcePreferences, candidate_id_for, search_availability
from appointment.domain.booking import create_hold
from appointment.domain.catalog import (
    parse_resource_requirements,
    required_skills_by_type,
)
from appointment.domain.quote import get_service_quote
from tests.conftest import customer_ctx

# ---------------------------------------------------------------------------
# 第 1 组：技能归属规则（纯函数）
# ---------------------------------------------------------------------------

SHOULDER_LIKE = {
    "skills": ["tuina"],
    "resources": [
        {"type": "therapist", "count": 1},
        {"type": "room", "count": 1},
    ],
}


def test_service_level_skills_apply_only_to_skill_bearing_types():
    """服务级 skills 只落到承载技能的类型上，房间自动豁免。"""

    mapping = required_skills_by_type(SHOULDER_LIKE, skill_bearing_types={"therapist"})
    assert mapping == {"therapist": {"tuina"}, "room": set()}


def test_resource_entry_can_override_or_exempt_skills():
    """条目自带的 skills 优先：可以替换，也可以用空列表显式豁免。"""

    mapping = required_skills_by_type(
        {
            "skills": ["tuina"],
            "resources": [
                {"type": "therapist", "count": 1, "skills": ["relax"]},
                {"type": "room", "count": 1, "skills": []},
            ],
        },
        skill_bearing_types={"therapist"},
    )
    assert mapping == {"therapist": {"relax"}, "room": set()}


def test_service_level_skills_with_no_bearing_type_is_a_loud_error():
    """门店没有任何技能声明时，服务级技能要求会退化成空操作——必须报错而不是放行。"""

    with pytest.raises(DomainError) as excinfo:
        required_skills_by_type(SHOULDER_LIKE, skill_bearing_types=set())
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR


def test_service_level_skills_that_cannot_land_anywhere_is_a_loud_error():
    """服务只用到房间却声明了技能要求：技能无法生效，同样是配置错误。"""

    with pytest.raises(DomainError):
        required_skills_by_type(
            {"skills": ["tuina"], "resources": [{"type": "room", "count": 1}]},
            skill_bearing_types={"therapist"},
        )


def test_requirements_default_to_one_therapist():
    assert parse_resource_requirements({}) == (["therapist"], {"therapist": 1})


# ---------------------------------------------------------------------------
# 第 2 组：提交路径复核（真实数据库）
# ---------------------------------------------------------------------------


async def _prepare(session, ctx, seeded, service, now, window):
    """走到"已拿到候选"这一步，返回 (task, offer, candidate)。"""

    from appointment.domain.tasks import (
        ensure_conversation,
        get_or_create_task,
        set_task_state,
    )
    from appointment.core.enums import TaskState

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

    offer = await get_service_quote(
        session,
        tenant_id=seeded.tenant_id,
        customer_id=ctx.customer_id,
        store_id=seeded.store_id,
        service=service,
        as_of=now,
    )
    start, end = window
    availability = await search_availability(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service=service,
        window_start=start,
        window_end=end,
        now=now,
        preferences=ResourcePreferences(),
        amount_minor=offer.amount_minor,
    )
    assert availability.candidates, "夹具应能查到候选"
    return task, offer, availability.candidates[0]


async def _service(session, seeded):
    from appointment.domain.catalog import load_service

    return await load_service(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service_id=seeded.service_ids["shoulder"],
    )


async def test_hold_rejects_resource_set_with_wrong_type_composition(
    session, seeded, clock, tomorrow_window
):
    """把"技师 + 房间"换成"两间房"必须被拒绝。

    候选 ID 由内容派生，因此测试可以直接伪造一个"与错误资源集自洽"的候选 ID：
    这模拟的正是"客户端回传了错误的资源列表"这一路径。资源列表本身合法、存在、
    时段空闲，唯一的问题是组合不满足服务要求。
    """

    ctx = customer_ctx(seeded)
    now = clock.now()
    service = await _service(session, seeded)
    task, offer, candidate = await _prepare(
        session, ctx, seeded, service, now, tomorrow_window[:2]
    )

    wrong_ids = [seeded.room_ids[0], seeded.room_ids[1]]
    forged_candidate_id = candidate_id_for(
        service_version_id=service.service_version_id,
        start_at=candidate.start_at,
        end_at=candidate.end_at,
        resource_ids=wrong_ids,
    )

    with pytest.raises(DomainError) as excinfo:
        await create_hold(
            session,
            ctx,
            task=task,
            expected_task_version=task.version,
            store_id=seeded.store_id,
            service_id=seeded.service_ids["shoulder"],
            candidate_id=forged_candidate_id,
            start_at=candidate.start_at,
            end_at=candidate.end_at,
            resource_ids=wrong_ids,
            quote_token=offer.quote_token,
            now=now,
            clock=clock,
        )
    assert excinfo.value.code == ErrorCode.STALE_PROPOSAL
    await session.rollback()

    # 关键：拒绝之后不能留下任何占用，否则资源被"失败的请求"锁住。
    allocations = (
        await session.execute(
            m.ResourceAllocation.__table__.select().where(
                m.ResourceAllocation.resource_id.in_(wrong_ids)
            )
        )
    ).all()
    assert allocations == []


async def test_hold_rejects_resource_without_required_skill(
    session, seeded, clock, tomorrow_window
):
    """技师缺少服务要求的技能时必须被拒绝，即使他本人完全空闲。"""

    from uuid import uuid4

    ctx = customer_ctx(seeded)
    now = clock.now()
    service = await _service(session, seeded)

    # 新增一名不声明任何技能的技师，并给他覆盖整个窗口的班次。
    unskilled_id = uuid4()
    session.add(
        m.Resource(
            id=unskilled_id,
            tenant_id=seeded.tenant_id,
            store_id=seeded.store_id,
            type="therapist",
            unit_code="therapist-99",
            display_name="新技师",
            status="ACTIVE",
            gender="female",
            reputation_score=0.5,
        )
    )
    start_at, end_at = tomorrow_window[0], tomorrow_window[1]
    session.add(
        m.Shift(
            id=uuid4(),
            tenant_id=seeded.tenant_id,
            store_id=seeded.store_id,
            resource_id=unskilled_id,
            start_at=start_at,
            end_at=end_at,
            status="SCHEDULED",
        )
    )
    await session.flush()

    task, offer, candidate = await _prepare(
        session, ctx, seeded, service, now, tomorrow_window[:2]
    )
    room_id = next(u.resource_id for u in candidate.resources if u.type == "room")
    forged_ids = [unskilled_id, room_id]
    forged_candidate_id = candidate_id_for(
        service_version_id=service.service_version_id,
        start_at=candidate.start_at,
        end_at=candidate.end_at,
        resource_ids=forged_ids,
    )

    with pytest.raises(DomainError) as excinfo:
        await create_hold(
            session,
            ctx,
            task=task,
            expected_task_version=task.version,
            store_id=seeded.store_id,
            service_id=seeded.service_ids["shoulder"],
            candidate_id=forged_candidate_id,
            start_at=candidate.start_at,
            end_at=candidate.end_at,
            resource_ids=forged_ids,
            quote_token=offer.quote_token,
            now=now,
            clock=clock,
        )
    assert excinfo.value.code == ErrorCode.STALE_PROPOSAL
    await session.rollback()


@pytest.mark.parametrize(
    "seeded", [{"skillless_therapist_indices": frozenset({0, 1, 2})}], indirect=True
)
async def test_skill_requirement_without_any_skill_data_is_a_loud_error(
    session, seeded, clock, tomorrow_window
):
    """门店没有任何技能声明时不能静默放行——技能过滤失效会让不合格资源被约出去。"""

    now = clock.now()
    service = await _service(session, seeded)

    with pytest.raises(DomainError) as excinfo:
        await search_availability(
            session,
            tenant_id=seeded.tenant_id,
            store_id=seeded.store_id,
            service=service,
            window_start=tomorrow_window[0],
            window_end=tomorrow_window[1],
            now=now,
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize(
    "seeded", [{"skillless_therapist_indices": frozenset({0})}], indirect=True
)
async def test_therapist_missing_required_skill_never_appears_as_candidate(
    session, seeded, clock, tomorrow_window
):
    """只清掉一名技师的技能：其余技师照常出候选，但这一名不得出现在任何候选里。"""

    unskilled_id = seeded.therapist_ids[0]
    now = clock.now()
    service = await _service(session, seeded)

    result = await search_availability(
        session,
        tenant_id=seeded.tenant_id,
        store_id=seeded.store_id,
        service=service,
        window_start=tomorrow_window[0],
        window_end=tomorrow_window[1],
        now=now,
        limit=200,
    )
    assert result.candidates, "其余技师仍应提供候选"
    assert all(
        unit.resource_id != unskilled_id
        for candidate in result.candidates
        for unit in candidate.resources
    ), "缺技能的技师不得进入任何候选"

    # 房间类型不受技能约束影响，仍然正常出现在候选里。
    assert all(
        any(unit.type == "room" for unit in candidate.resources)
        for candidate in result.candidates
    )
