"""服务目录与可履约事实读取。

这些是"结构化业务 API"要读的事实：当前价格、排班、资源技能。它们不走 RAG
（设计稿 §10.1：服务介绍、FAQ 适合 RAG；当前价格、排班、订单状态必须走结构化
查询）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import ErrorCode
from ..core.errors import DomainError, not_found, stale_proposal, validation_error
from ..db import models as m


@dataclass(frozen=True, slots=True)
class ServiceFacts:
    """一次快照的服务事实。提交前会再次校验，避免"读完再变化"。"""

    service_id: UUID
    service_version_id: UUID
    store_id: UUID
    service_name: str
    revision: int
    duration_minutes: int
    requirements: dict
    terms_hash: str


@dataclass(frozen=True, slots=True)
class PriceFacts:
    price_version_id: UUID
    amount_minor: int
    currency: str
    currency_exponent: int
    revision: int
    terms_snapshot: dict
    valid_from: datetime
    valid_to: datetime | None


async def load_store(
    session: AsyncSession, *, tenant_id: UUID, store_id: UUID
) -> m.Store:
    row = (
        await session.execute(
            select(m.Store).where(
                m.Store.tenant_id == tenant_id, m.Store.id == store_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("门店不存在或无权访问")
    return row


#: 可以接受新预约的门店状态。写入路径已经用它做校验，读取路径也必须一致。
BOOKABLE_STORE_STATUSES: frozenset[str] = frozenset({"ACTIVE"})


async def load_bookable_store(
    session: AsyncSession, *, tenant_id: UUID, store_id: UUID
) -> m.Store:
    """读取门店并确认它当前可以接受新预约。

    停用/关闭的门店必须表现为**失败**，而不是"没有可约时段"。否则依赖状态会被
    错误地描述成"没号"，用户据此做出错误判断（设计稿 §11 恢复表）。
    """

    store = await load_store(session, tenant_id=tenant_id, store_id=store_id)
    if store.status not in BOOKABLE_STORE_STATUSES:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"门店当前不可预约（状态：{store.status}）",
        )
    return store


async def load_service(
    session: AsyncSession, *, tenant_id: UUID, store_id: UUID, service_id: UUID
) -> ServiceFacts:
    """读取门店下当前生效的服务版本。

    门店、服务、资源必须属于同一允许门店，不仅同租户。
    """

    service = (
        await session.execute(
            select(m.ServiceCatalog).where(
                m.ServiceCatalog.tenant_id == tenant_id,
                m.ServiceCatalog.id == service_id,
                m.ServiceCatalog.store_id == store_id,
            )
        )
    ).scalar_one_or_none()
    if service is None or service.status != "ACTIVE":
        raise not_found("服务不存在或未上架")
    if service.current_version_id is None:
        raise DomainError(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务尚未发布版本")

    version = (
        await session.execute(
            select(m.ServiceVersion).where(
                m.ServiceVersion.tenant_id == tenant_id,
                m.ServiceVersion.id == service.current_version_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise DomainError(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务版本缺失")

    return ServiceFacts(
        service_id=service.id,
        service_version_id=version.id,
        store_id=store_id,
        service_name=service.name,
        revision=version.revision,
        duration_minutes=version.duration_minutes,
        requirements=dict(version.requirements),
        terms_hash=version.terms_hash,
    )


async def load_active_price(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    store_id: UUID,
    service_version_id: UUID,
    as_of: datetime,
) -> PriceFacts:
    """读取已发布且在有效期内的价目版本。

    价目内容不可变；发布新版本不撤销已保存的 quote。
    """

    row = (
        await session.execute(
            select(m.PriceVersion)
            .where(
                m.PriceVersion.tenant_id == tenant_id,
                m.PriceVersion.store_id == store_id,
                m.PriceVersion.service_version_id == service_version_id,
                m.PriceVersion.revoked_at.is_(None),
                m.PriceVersion.valid_from <= as_of,
            )
            .order_by(m.PriceVersion.revision.desc())
        )
    ).scalars().first()
    if row is None:
        raise DomainError(ErrorCode.DEPENDENCY_UNAVAILABLE, "该服务暂无有效价目")
    if row.valid_to is not None and row.valid_to <= as_of:
        raise DomainError(ErrorCode.DEPENDENCY_UNAVAILABLE, "该服务价目已过期")
    return PriceFacts(
        price_version_id=row.id,
        amount_minor=row.amount_minor,
        currency=row.currency,
        currency_exponent=row.currency_exponent,
        revision=row.revision,
        terms_snapshot=dict(row.terms_snapshot),
        valid_from=row.valid_from,
        valid_to=row.valid_to,
    )


async def list_store_services(
    session: AsyncSession, *, tenant_id: UUID, store_id: UUID
) -> list[m.ServiceCatalog]:
    return list(
        (
            await session.execute(
                select(m.ServiceCatalog)
                .where(
                    m.ServiceCatalog.tenant_id == tenant_id,
                    m.ServiceCatalog.store_id == store_id,
                    m.ServiceCatalog.status == "ACTIVE",
                )
                .order_by(m.ServiceCatalog.name)
            )
        ).scalars()
    )


async def resolve_service_by_text(
    session: AsyncSession, *, tenant_id: UUID, store_id: UUID, text: str
) -> list[m.ServiceCatalog]:
    """把自然语言片段解析到合法服务 ID。

    只做名称/别名匹配，不做模糊猜测：不足以判断具体项目时返回空列表，由上层
    追问（设计稿 §7 第 3 步）。
    """

    needle = (text or "").strip()
    if not needle:
        return []
    services = await list_store_services(
        session, tenant_id=tenant_id, store_id=store_id
    )
    hits = [
        svc
        for svc in services
        if needle in svc.name or svc.name in needle or needle in list(svc.aliases)
    ]
    if not hits:
        hits = [
            svc
            for svc in services
            if any(alias and alias in needle for alias in list(svc.aliases))
        ]
    return hits


async def load_resource_skills(
    session: AsyncSession, *, tenant_id: UUID, resource_ids: list[UUID]
) -> dict[UUID, set[str]]:
    if not resource_ids:
        return {}
    rows = (
        await session.execute(
            select(m.ResourceSkill).where(
                m.ResourceSkill.tenant_id == tenant_id,
                m.ResourceSkill.resource_id.in_(resource_ids),
            )
        )
    ).scalars()
    result: dict[UUID, set[str]] = {rid: set() for rid in resource_ids}
    for row in rows:
        result.setdefault(row.resource_id, set()).add(row.skill_code)
    return result


# ---------------------------------------------------------------------------
# 资源要求：类型、数量与技能归属
# ---------------------------------------------------------------------------
#
# 服务版本 ``requirements`` 的约定形状（数据契约 §3）：
#
#   {
#     "skills": ["tuina"],                    # 服务级技能要求（可省略）
#     "buffer_minutes": 0,                    # 可省略
#     "resources": [
#       {"type": "therapist", "count": 1},            # 继承服务级 skills
#       {"type": "room",      "count": 1, "skills": []}   # 显式豁免技能要求
#     ]
#   }
#
# 技能约束**不能**无差别地套到所有资源类型上：房间、设备这类资源本来就不携带
# 技能，若一并要求，任何需要"技师 + 房间"的服务都会查不到候选。因此：
#
# 1. 资源条目自带 ``skills`` 时以它为准，``[]`` 表示该条目不要求技能；
# 2. 否则服务级 ``skills`` 只作用于该门店**承载技能**的资源类型
#    （``load_skill_bearing_types`` 的判定结果）；
# 3. 服务级声明了技能却无法作用到任何要求类型时，直接报配置错误——不允许悄悄
#    退化成"技能过滤失效"，那会变成一次错误的可用性放行。

DEFAULT_RESOURCE_TYPE = "therapist"


def parse_resource_requirements(
    requirements: Mapping[str, Any] | None,
) -> tuple[list[str], dict[str, int]]:
    """解析 (所需资源类型列表, 类型 → 所需数量)。

    未声明 ``resources`` 时按"一名技师"处理，与首版默认一致。
    """

    required_types: list[str] = []
    type_counts: dict[str, int] = {}
    for entry in (requirements or {}).get("resources", []) or []:
        rtype = entry.get("type")
        count = int(entry.get("count", 1))
        if rtype and count > 0:
            required_types.append(str(rtype))
            type_counts[str(rtype)] = type_counts.get(str(rtype), 0) + count
    if not required_types:
        return [DEFAULT_RESOURCE_TYPE], {DEFAULT_RESOURCE_TYPE: 1}
    return required_types, type_counts


def required_skills_by_type(
    requirements: Mapping[str, Any] | None,
    *,
    resource_types: Sequence[str] = (),
    skill_bearing_types: AbstractSet[str] | None = None,
    extra_skills: Sequence[str] = (),
) -> dict[str, set[str]]:
    """把技能要求落到具体资源类型上，返回 类型 → 该类型必须具备的技能集合。

    ``skill_bearing_types`` 由 :func:`load_skill_bearing_types` 计算；传 ``None``
    表示"所有类型都承载技能"（用于不依赖门店数据的纯计算场景）。
    """

    payload = requirements or {}
    service_skills = {str(s) for s in (payload.get("skills") or []) if s}
    service_skills.update(str(s) for s in extra_skills if s)

    entries = [
        entry for entry in (payload.get("resources") or []) if entry.get("type")
    ]

    mapping: dict[str, set[str]] = {}
    implicit_types: list[str] = []
    if entries:
        for entry in entries:
            rtype = str(entry["type"])
            mapping.setdefault(rtype, set())
            if "skills" in entry:
                mapping[rtype] |= {str(s) for s in (entry.get("skills") or []) if s}
            else:
                implicit_types.append(rtype)
    else:
        implicit_types = list(resource_types) or [DEFAULT_RESOURCE_TYPE]
        for rtype in implicit_types:
            mapping.setdefault(rtype, set())

    if service_skills:
        bearing = (
            set(skill_bearing_types)
            if skill_bearing_types is not None
            else set(mapping)
        )
        applied_to = bearing.intersection(implicit_types)
        if applied_to:
            for rtype in applied_to:
                mapping[rtype] |= service_skills
        elif implicit_types:
            # 隐式类型存在，却没有任何一个承载技能：服务级技能要求会变成空操作，
            # 可用性查询将放行技能不达标的资源。这是配置错误，必须显式报错，
            # 不能退化成"技能过滤静默失效"。
            raise validation_error(
                "服务要求技能 {}，但无法作用到任何所需资源类型（{}）；"
                "请检查 requirements.skills 与该门店的资源技能数据是否配套".format(
                    "、".join(sorted(service_skills)),
                    "、".join(sorted(set(implicit_types))),
                )
            )
        # 所有条目都显式声明了 skills：服务级要求已被逐项覆盖，无需求值。

    return mapping


async def load_skill_bearing_types(
    session: AsyncSession, *, tenant_id: UUID, store_id: UUID
) -> set[str]:
    """门店内**承载技能**的资源类型集合。

    判定依据是"该类型下是否存在技能声明记录"，而不是把 ``therapist`` 这类命名
    写死在代码里：连锁门店可能用 ``masseur`` / ``doctor`` / ``nail_artist`` 等
    自定义类型，写死会让技能约束静默失效。
    """

    rows = (
        await session.execute(
            select(m.Resource.type)
            .join(
                m.ResourceSkill,
                (m.ResourceSkill.tenant_id == m.Resource.tenant_id)
                & (m.ResourceSkill.resource_id == m.Resource.id),
            )
            .where(
                m.Resource.tenant_id == tenant_id,
                m.Resource.store_id == store_id,
                m.Resource.status == "ACTIVE",
            )
            .distinct()
        )
    ).scalars().all()
    return {str(row) for row in rows}


async def resolve_required_skills(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    store_id: UUID,
    requirements: Mapping[str, Any] | None,
    resource_types: Sequence[str] = (),
    extra_skills: Sequence[str] = (),
) -> dict[str, set[str]]:
    """:func:`required_skills_by_type` 的门店数据版：先取承载技能类型再解析。"""

    bearing = await load_skill_bearing_types(
        session, tenant_id=tenant_id, store_id=store_id
    )
    return required_skills_by_type(
        requirements,
        resource_types=resource_types,
        skill_bearing_types=bearing,
        extra_skills=extra_skills,
    )


async def load_service_version_requirements(
    session: AsyncSession, *, tenant_id: UUID, service_version_id: UUID
) -> dict[str, Any]:
    """按服务版本 ID 读取 requirements。

    提交与改约路径手上只有方案/订单里快照的 ``service_version_id``，没有 catalog
    行；技能与资源组合的复核要以服务版本当时的要求为准（服务版本不可变）。
    """

    row = (
        await session.execute(
            select(m.ServiceVersion.requirements).where(
                m.ServiceVersion.tenant_id == tenant_id,
                m.ServiceVersion.id == service_version_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise stale_proposal("服务版本不存在，方案已失效")
    return dict(row)


async def validate_resource_composition(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    store_id: UUID,
    resource_ids: Sequence[UUID],
    requirements: Mapping[str, Any] | None,
) -> None:
    """复核资源组合（类型、数量、技能）是否满足服务要求。

    设计稿 §7 第 4 步要求占位/提交事务内复核"班次、技能、报价"。资源组合**必须**
    在这里独立验证：候选查询只负责构造组合，客户端回传的 resource_ids 不可信，
    否则一次错误请求就能把"技师 + 房间"换成"两间房"。

    失败一律抛 ``stale_proposal``——对用户可见的语义是"方案已失效，请重新选择"，
    而不是把内部校验细节暴露成可探测的错误。
    """

    wanted = list(dict.fromkeys(resource_ids))
    if not wanted:
        raise stale_proposal("方案未包含任何资源")

    rows = (
        await session.execute(
            select(m.Resource.id, m.Resource.type, m.Resource.status).where(
                m.Resource.tenant_id == tenant_id,
                m.Resource.store_id == store_id,
                m.Resource.id.in_(wanted),
            )
        )
    ).all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(wanted):
        raise stale_proposal("资源不存在或不属于该门店")
    if any(row.status != "ACTIVE" for row in by_id.values()):
        raise stale_proposal("资源已停用")

    required_types, type_counts = parse_resource_requirements(requirements)
    actual_counts: dict[str, int] = {}
    for row in by_id.values():
        actual_counts[row.type] = actual_counts.get(row.type, 0) + 1
    if set(actual_counts) != set(type_counts) or any(
        actual_counts.get(rtype, 0) != need for rtype, need in type_counts.items()
    ):
        raise stale_proposal("资源组合与服务要求不一致，请重新查询可用时间")

    skills_needed = await resolve_required_skills(
        session,
        tenant_id=tenant_id,
        store_id=store_id,
        requirements=requirements,
        resource_types=required_types,
    )
    if not any(skills_needed.values()):
        return None

    actual_skills = await load_resource_skills(
        session, tenant_id=tenant_id, resource_ids=wanted
    )
    for row in by_id.values():
        need = skills_needed.get(row.type, set())
        if need and not need.issubset(actual_skills.get(row.id, set())):
            raise stale_proposal("资源技能不满足服务要求，请重新查询可用时间")
    return None
