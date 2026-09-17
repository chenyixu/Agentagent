"""可用性查询（设计稿 §7 第 4 步、§9）。

关键语义：**候选查询结果只是时间点快照，不构成预约承诺**（``guarantee=NONE``）。
候选 ID 由内容派生；服务端在占位事务中重新校验资源、技能、班次与门店，
因此候选 ID 本身不是占位凭据。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import AllocationState
from ..core.errors import validation_error
from ..core.hashing import content_hash
from ..db import models as m
from .catalog import (
    ServiceFacts,
    load_bookable_store,
    load_resource_skills,
    parse_resource_requirements,
    resolve_required_skills,
)
from .recommendation import (
    ScoreInput,
    explicit_preference_fit,
    format_reasons,
    price_fit,
    score_candidate,
    time_fit,
)
from .timeutil import (
    LocalWindow,
    iter_local_dates,
    load_zone,
    local_date_of,
    snap_to_step,
    window_instants,
    windows_for_local_date,
)

#: 候选起点网格。值是可调起点，不是已验证最优值。
STEP_MINUTES = 15
PER_TYPE_CAP = 4
MAX_COMBINATIONS = 128


@dataclass(frozen=True, slots=True)
class ResourceUnit:
    """候选占用的一个具体单位资源（容量为 1）。"""

    resource_id: UUID
    type: str
    unit_code: str
    display_name: str
    gender: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": str(self.resource_id),
            "type": self.type,
            "unit_code": self.unit_code,
            "display_name": self.display_name,
            "gender": self.gender,
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    service_version_id: UUID
    start_at: datetime
    end_at: datetime
    resources: tuple[ResourceUnit, ...]
    constraints: tuple[str, ...]
    dependency_versions: dict[str, Any]
    snapshot_at: datetime
    #: 候选不是资源保证。只有 create_hold 事务成功才能说"已保留"。
    guarantee: str = "NONE"
    score: float | None = None
    score_detail: dict[str, Any] | None = None
    fallback: bool = False
    reasons: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "service_version_id": str(self.service_version_id),
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "resources": [unit.to_dict() for unit in self.resources],
            "constraints": list(self.constraints),
            "dependency_versions": self.dependency_versions,
            "snapshot_at": self.snapshot_at.isoformat(),
            "guarantee": self.guarantee,
            "score": None if self.score is None else round(self.score, 4),
            "score_detail": self.score_detail,
            "fallback": self.fallback,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    candidates: list[Candidate]
    snapshot_at: datetime
    guarantee: str = "NONE"
    notes: list[str] = field(default_factory=list)

    def to_data(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_data() for c in self.candidates],
            "snapshot_at": self.snapshot_at.isoformat(),
            "guarantee": self.guarantee,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class ResourcePreferences:
    """资源偏好。

    点名技师默认是"软偏好"：只在用户已表达"可以换"（``allow_substitute``）时
    才生成替代候选，否则宁可返回空并要求澄清。
    """

    preferred_resource_ids: list[UUID] = field(default_factory=list)
    excluded_resource_ids: list[UUID] = field(default_factory=list)
    gender: str | None = None
    gender_hard: bool = False
    skill_codes: list[str] = field(default_factory=list)
    allow_substitute: bool = True


def candidate_id_for(
    *,
    service_version_id: UUID,
    start_at: datetime,
    end_at: datetime,
    resource_ids: Sequence[UUID],
) -> str:
    return content_hash(
        {
            "service_version_id": str(service_version_id),
            "start_at": start_at,
            "end_at": end_at,
            "resources": sorted(str(rid) for rid in resource_ids),
        }
    )


# ---------------------------------------------------------------------------
# 批量读取可履约事实
# ---------------------------------------------------------------------------


async def _load_resources(
    session: AsyncSession, *, tenant_id: UUID, store_id: UUID, types: Sequence[str]
) -> list[m.Resource]:
    if not types:
        return []
    return list(
        (
            await session.execute(
                select(m.Resource)
                .where(
                    m.Resource.tenant_id == tenant_id,
                    m.Resource.store_id == store_id,
                    m.Resource.status == "ACTIVE",
                    m.Resource.type.in_(list(types)),
                )
                .order_by(m.Resource.unit_code)
            )
        ).scalars()
    )


async def _load_intervals(
    session: AsyncSession,
    model: Any,
    *,
    tenant_id: UUID,
    resource_ids: Sequence[UUID],
    window_start: datetime,
    window_end: datetime,
    extra_conditions: Sequence[Any] = (),
) -> dict[UUID, list[tuple[datetime, datetime]]]:
    if not resource_ids:
        return {}
    rows = (
        await session.execute(
            select(model.resource_id, model.start_at, model.end_at).where(
                model.tenant_id == tenant_id,
                model.resource_id.in_(list(resource_ids)),
                model.start_at < window_end,
                model.end_at > window_start,
                *extra_conditions,
            )
        )
    ).all()
    result: dict[UUID, list[tuple[datetime, datetime]]] = {}
    for resource_id, start_at, end_at in rows:
        result.setdefault(resource_id, []).append((start_at, end_at))
    return result


async def _load_busy_intervals(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    resource_ids: Sequence[UUID],
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> dict[UUID, list[tuple[datetime, datetime]]]:
    """已有占用。查询时忽略已到期的 HELD（设计稿 §8.1 的过期兜底）。

    注意：写入侧不能只靠这个忽略，仍必须在事务内先把到期 HELD 转 EXPIRED，
    否则排他约束会挡住新插入。
    """

    return await _load_intervals(
        session,
        m.ResourceAllocation,
        tenant_id=tenant_id,
        resource_ids=resource_ids,
        window_start=window_start,
        window_end=window_end,
        extra_conditions=(
            or_(
                m.ResourceAllocation.state == AllocationState.BOOKED.value,
                and_(
                    m.ResourceAllocation.state == AllocationState.HELD.value,
                    m.ResourceAllocation.expires_at > now,
                ),
            ),
        ),
    )


async def _load_shifts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    resource_ids: Sequence[UUID],
    window_start: datetime,
    window_end: datetime,
) -> dict[UUID, list[tuple[datetime, datetime]]]:
    return await _load_intervals(
        session,
        m.Shift,
        tenant_id=tenant_id,
        resource_ids=resource_ids,
        window_start=window_start,
        window_end=window_end,
        extra_conditions=(m.Shift.status == "SCHEDULED",),
    )


async def _load_absences(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    resource_ids: Sequence[UUID],
    window_start: datetime,
    window_end: datetime,
) -> dict[UUID, list[tuple[datetime, datetime]]]:
    return await _load_intervals(
        session,
        m.ResourceAbsence,
        tenant_id=tenant_id,
        resource_ids=resource_ids,
        window_start=window_start,
        window_end=window_end,
        extra_conditions=(m.ResourceAbsence.approval_status == "APPROVED",),
    )


async def _load_calendar(
    session: AsyncSession, *, tenant_id: UUID, store_id: UUID
) -> tuple[dict[str, Any], dict[date, list[list[str]] | None]]:
    """返回 (周规则, 例外表)。例外值为 None 表示当天闭店。"""

    calendar = (
        await session.execute(
            select(m.BusinessCalendar)
            .where(
                m.BusinessCalendar.tenant_id == tenant_id,
                m.BusinessCalendar.store_id == store_id,
            )
            .order_by(m.BusinessCalendar.revision.desc())
        )
    ).scalars().first()
    weekly = dict(calendar.weekly_windows) if calendar is not None else {}

    exceptions = (
        await session.execute(
            select(m.CalendarException).where(
                m.CalendarException.tenant_id == tenant_id,
                m.CalendarException.store_id == store_id,
            )
        )
    ).scalars()

    table: dict[date, list[list[str]] | None] = {}
    for exc in exceptions:
        table[exc.local_date] = None if exc.kind == "CLOSED" else list(exc.windows)
    return weekly, table


def day_windows(
    weekly: dict[str, Any],
    exceptions: dict[date, list[list[str]] | None],
    local_date: date,
) -> list[LocalWindow] | None:
    """某一天的营业窗口。返回 ``None`` 表示当天闭店。"""

    if local_date in exceptions:
        override = exceptions[local_date]
        if override is None:
            return None
        result: list[LocalWindow] = []
        for entry in override:
            if len(entry) != 2:
                raise validation_error(
                    f"特殊营业时间格式非法：{entry!r}，应为 [开始, 结束]"
                )
            result.append(
                LocalWindow(
                    start=time.fromisoformat(entry[0]), end=time.fromisoformat(entry[1])
                )
            )
        return result
    return windows_for_local_date(weekly, local_date)


def _overlaps(
    intervals: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime
) -> bool:
    return any(b_start < end and b_end > start for b_start, b_end in intervals)


def _covered(
    intervals: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime
) -> bool:
    return any(i_start <= start and i_end >= end for i_start, i_end in intervals)


# ---------------------------------------------------------------------------
# 主查询
# ---------------------------------------------------------------------------


async def search_availability(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    store_id: UUID,
    service: ServiceFacts,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    desired_start: datetime | None = None,
    preferences: ResourcePreferences | None = None,
    budget_minor: int | None = None,
    amount_minor: int | None = None,
    limit: int = 5,
    weights: dict[str, float] | None = None,
) -> AvailabilityResult:
    if window_end <= window_start:
        raise validation_error("查询时间窗非法：结束必须晚于开始")
    if limit <= 0:
        raise validation_error("limit 必须为正")

    prefs = preferences or ResourcePreferences()
    # 门店停用时必须报错，而不是返回空候选：空结果会说话成"这个点没号"。
    store = await load_bookable_store(
        session, tenant_id=tenant_id, store_id=store_id
    )
    tz = load_zone(store.timezone)

    requirements = service.requirements or {}
    required_types, type_counts = _parse_requirements(requirements)

    resources = await _load_resources(
        session, tenant_id=tenant_id, store_id=store_id, types=required_types
    )
    if not resources:
        return AvailabilityResult(
            candidates=[], snapshot_at=now, notes=["该门店没有可用资源"]
        )

    resource_ids = [r.id for r in resources]
    skills = await load_resource_skills(
        session, tenant_id=tenant_id, resource_ids=resource_ids
    )
    shifts = await _load_shifts(
        session,
        tenant_id=tenant_id,
        resource_ids=resource_ids,
        window_start=window_start,
        window_end=window_end,
    )
    absences = await _load_absences(
        session,
        tenant_id=tenant_id,
        resource_ids=resource_ids,
        window_start=window_start,
        window_end=window_end,
    )
    busy = await _load_busy_intervals(
        session,
        tenant_id=tenant_id,
        resource_ids=resource_ids,
        window_start=window_start,
        window_end=window_end,
        now=now,
    )
    weekly, exceptions = await _load_calendar(
        session, tenant_id=tenant_id, store_id=store_id
    )

    required_skills_by_type = await resolve_required_skills(
        session,
        tenant_id=tenant_id,
        store_id=store_id,
        requirements=requirements,
        resource_types=required_types,
        extra_skills=prefs.skill_codes,
    )
    # 技能匹配打分用到的是"服务要求了技能"这一事实，不区分具体类型。
    any_required_skills: set[str] = set().union(*required_skills_by_type.values())

    excluded = set(prefs.excluded_resource_ids)

    eligible_by_type: dict[str, list[m.Resource]] = {t: [] for t in required_types}
    skill_rejected: dict[str, int] = {}
    for resource in resources:
        if resource.id in excluded:
            continue
        if prefs.gender_hard and prefs.gender and resource.gender != prefs.gender:
            continue
        needed = required_skills_by_type.get(resource.type, set())
        if needed and not needed.issubset(skills.get(resource.id, set())):
            skill_rejected[resource.type] = skill_rejected.get(resource.type, 0) + 1
            continue
        eligible_by_type.setdefault(resource.type, []).append(resource)

    named_ids = set(prefs.preferred_resource_ids)
    if named_ids and not prefs.allow_substitute:
        named_resources = [r for r in resources if r.id in named_ids]
        named_free = [
            r
            for r in named_resources
            if not _overlaps(busy.get(r.id, []), window_start, window_end)
        ]
        if named_resources and not named_free:
            return AvailabilityResult(
                candidates=[],
                snapshot_at=now,
                notes=["用户点名的资源在该时间窗内不可用，且未表达可以换人，需要澄清"],
            )

    duration = timedelta(minutes=service.duration_minutes)
    buffer_minutes = int(requirements.get("buffer_minutes", 0) or 0)
    occupied = duration + timedelta(minutes=buffer_minutes)
    window_seconds = max((window_end - window_start).total_seconds(), 1.0)

    candidates: list[Candidate] = []
    seen: set[tuple[tuple[str, ...], datetime]] = set()

    for local_date in iter_local_dates(window_start, window_end, tz):
        windows = day_windows(weekly, exceptions, local_date)
        if not windows:
            continue
        for window in windows:
            win_start, win_end = window_instants(local_date, window, tz)
            cursor = snap_to_step(max(win_start, window_start), STEP_MINUTES)
            hard_end = min(win_end, window_end)
            while cursor + duration <= hard_end:
                slot_start = cursor
                slot_end = cursor + duration
                if slot_start + occupied > win_end:
                    break

                per_type = _feasible_picks(
                    required_types=required_types,
                    type_counts=type_counts,
                    eligible_by_type=eligible_by_type,
                    shifts=shifts,
                    absences=absences,
                    busy=busy,
                    slot_start=slot_start,
                    slot_end=slot_end,
                    alloc_end=slot_start + occupied,
                )
                if per_type is None:
                    cursor += timedelta(minutes=STEP_MINUTES)
                    continue

                ordered_types = sorted(required_types)
                for combo in itertools.islice(
                    itertools.product(*[per_type[t][:PER_TYPE_CAP] for t in ordered_types]),
                    MAX_COMBINATIONS,
                ):
                    units = tuple(
                        ResourceUnit(
                            resource_id=resource.id,
                            type=resource.type,
                            unit_code=resource.unit_code,
                            display_name=resource.display_name,
                            gender=resource.gender,
                        )
                        for rtype, resource in zip(ordered_types, combo)
                        for _ in range(type_counts.get(rtype, 1))
                    )
                    key = (
                        tuple(sorted(u.resource_id.hex for u in units)),
                        slot_start,
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        _build_candidate(
                            units=units,
                            service=service,
                            slot_start=slot_start,
                            slot_end=slot_end,
                            now=now,
                            store_calendar_version=store.calendar_version,
                            resources=resources,
                            prefs=prefs,
                            required_skills=any_required_skills,
                            budget_minor=budget_minor,
                            amount_minor=amount_minor,
                            desired_start=desired_start,
                            window_seconds=window_seconds,
                            weights=weights,
                        )
                    )
                cursor += timedelta(minutes=STEP_MINUTES)

    candidates.sort(key=lambda c: (-(c.score or 0.0), c.start_at))
    deduped: list[Candidate] = []
    seen_start: set[datetime] = set()
    for cand in candidates:
        # 同一时段只保留最高分组合，避免用等价方案淹没用户。
        if cand.start_at in seen_start:
            continue
        seen_start.add(cand.start_at)
        deduped.append(cand)
        if len(deduped) >= limit:
            break

    notes: list[str] = []
    if not deduped and skill_rejected:
        # 让"技能不匹配"这类原因显式暴露，而不是表现为一个没有解释的空结果。
        notes.append(
            "有资源因技能不满足服务要求被排除："
            + "、".join(f"{t} {n} 个" for t, n in sorted(skill_rejected.items()))
        )
    return AvailabilityResult(candidates=deduped, snapshot_at=now, notes=notes)


def _parse_requirements(requirements: dict[str, Any]) -> tuple[list[str], dict[str, int]]:
    """兼容旧调用点的薄封装；实现见 :func:`catalog.parse_resource_requirements`。"""

    return parse_resource_requirements(requirements)


def _feasible_picks(
    *,
    required_types: Sequence[str],
    type_counts: dict[str, int],
    eligible_by_type: dict[str, list[m.Resource]],
    shifts: dict[UUID, list[tuple[datetime, datetime]]],
    absences: dict[UUID, list[tuple[datetime, datetime]]],
    busy: dict[UUID, list[tuple[datetime, datetime]]],
    slot_start: datetime,
    slot_end: datetime,
    alloc_end: datetime,
) -> dict[str, list[m.Resource]] | None:
    """按类型挑选可用资源。任一类型数量不足即返回 None。"""

    per_type: dict[str, list[m.Resource]] = {}
    for rtype in required_types:
        need = type_counts.get(rtype, 1)
        picks: list[m.Resource] = []
        for resource in eligible_by_type.get(rtype, []):
            if not _covered(shifts.get(resource.id, []), slot_start, slot_end):
                continue
            if _overlaps(absences.get(resource.id, []), slot_start, slot_end):
                continue
            if _overlaps(busy.get(resource.id, []), slot_start, alloc_end):
                continue
            picks.append(resource)
        if len(picks) < need:
            return None
        per_type[rtype] = picks
    return per_type


def _build_candidate(
    *,
    units: tuple[ResourceUnit, ...],
    service: ServiceFacts,
    slot_start: datetime,
    slot_end: datetime,
    now: datetime,
    store_calendar_version: int,
    resources: list[m.Resource],
    prefs: ResourcePreferences,
    required_skills: set[str],
    budget_minor: int | None,
    amount_minor: int | None,
    desired_start: datetime | None,
    window_seconds: float,
    weights: dict[str, float] | None,
) -> Candidate:
    primary = units[0]
    named_ids = set(prefs.preferred_resource_ids)
    named_hit = any(u.resource_id in named_ids for u in units)

    therapists = [u for u in units if u.type == "therapist"]
    if prefs.gender:
        attribute_match: bool | None = (
            all(u.gender == prefs.gender for u in therapists) if therapists else None
        )
    else:
        attribute_match = None

    reputation = next(
        (r.reputation_score for r in resources if r.id == primary.resource_id), None
    )
    scored = score_candidate(
        ScoreInput(
            skill_match=1.0 if required_skills else None,
            explicit_preference=explicit_preference_fit(
                resource_id=primary.resource_id,
                preferred_resource_ids=list(prefs.preferred_resource_ids),
                attribute_match=attribute_match,
            ),
            time_fit=time_fit(
                candidate_start=slot_start,
                desired_start=desired_start,
                window_seconds=window_seconds,
            ),
            price_fit=price_fit(amount_minor=amount_minor or 0, budget_minor=budget_minor),
            reputation=reputation,
        ),
        weights=weights,
    )

    reasons = format_reasons(scored)
    fallback = bool(named_ids and not named_hit and prefs.allow_substitute)
    if fallback:
        reasons.append("用户点名的技师在该时段不可用，这是经授权的替代候选")

    return Candidate(
        candidate_id=candidate_id_for(
            service_version_id=service.service_version_id,
            start_at=slot_start,
            end_at=slot_end,
            resource_ids=[u.resource_id for u in units],
        ),
        service_version_id=service.service_version_id,
        start_at=slot_start,
        end_at=slot_end,
        resources=units,
        constraints=(
            "资源班次覆盖该时段",
            "资源技能满足服务要求",
            "落在门店营业窗口内",
            "与现有有效占用不重叠（快照时点）",
        ),
        dependency_versions={
            "store_calendar_version": store_calendar_version,
            "service_version_revision": service.revision,
            "snapshot_at": now.isoformat(),
        },
        snapshot_at=now,
        score=scored.score,
        score_detail=scored.to_dict(),
        fallback=fallback,
        reasons=tuple(reasons),
    )
