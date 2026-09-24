"""演示与测试用种子数据。

设计稿 §13.1 要求场景夹具确定、可复现：参考时钟与时区、租户/用户夹具、服务与
排班夹具都显式传入，不依赖"当前系统时间"这类隐式状态。

注意：插入按外键层级分阶段 flush。ORM 的 flush 顺序由 relationship 决定，而本
项目有意不使用 relationship（组合外键 + 显式列），因此顺序必须写清楚，不能依赖
隐式排序。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from .core.hashing import content_hash
from .db import models as m
from .domain.timeutil import load_zone, resolve_local_datetime
from .knowledge.index import build_chunks
from .knowledge.tokenizer import tokenize

STORE_TIMEZONE = "Asia/Shanghai"

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(slots=True)
class SeedResult:
    tenant_id: UUID
    store_id: UUID
    customer_ids: list[UUID]
    customer_actor_ids: list[UUID]
    manager_actor_id: UUID
    staff_actor_id: UUID
    service_ids: dict[str, UUID]
    resource_ids: list[UUID]
    therapist_ids: list[UUID]
    room_ids: list[UUID]
    price_amount_minor: int


async def seed(
    session: AsyncSession,
    *,
    now: datetime,
    days: int = 21,
    tenant_name: str = "演示连锁",
    store_name: str = "静安店",
    skillless_therapist_indices: frozenset[int] = frozenset(),
) -> SeedResult:
    tz = load_zone(STORE_TIMEZONE)
    tenant_id = uuid4()
    store_id = uuid4()

    # ---------- 第 1 层：租户与门店 ----------
    session.add(m.Tenant(id=tenant_id, name=tenant_name, status="ACTIVE"))
    await session.flush()
    session.add(
        m.Store(
            id=store_id,
            tenant_id=tenant_id,
            name=store_name,
            timezone=STORE_TIMEZONE,
            status="ACTIVE",
            calendar_version=1,
        )
    )
    await session.flush()

    # 营业时间：每天 10:00–22:00（门店本地时间）。
    session.add(
        m.BusinessCalendar(
            id=uuid4(),
            tenant_id=tenant_id,
            store_id=store_id,
            revision=1,
            weekly_windows={day: [["10:00", "22:00"]] for day in WEEKDAYS},
            effective_from=now.astimezone(tz).date() - timedelta(days=30),
        )
    )
    await session.flush()

    # ---------- 第 2 层：主体与客户 ----------
    manager_actor_id = uuid4()
    staff_actor_id = uuid4()
    customer_actor_ids: list[UUID] = []
    session.add_all(
        [
            m.Actor(id=manager_actor_id, tenant_id=tenant_id, status="ACTIVE"),
            m.Actor(id=staff_actor_id, tenant_id=tenant_id, status="ACTIVE"),
        ]
    )
    for _ in ("林女士", "赵先生"):
        actor_id = uuid4()
        customer_actor_ids.append(actor_id)
        session.add(m.Actor(id=actor_id, tenant_id=tenant_id, status="ACTIVE"))
    await session.flush()

    session.add_all(
        [
            m.StaffMembership(
                id=uuid4(),
                tenant_id=tenant_id,
                actor_id=manager_actor_id,
                store_id=store_id,
                role="store_manager",
                status="ACTIVE",
            ),
            m.StaffMembership(
                id=uuid4(),
                tenant_id=tenant_id,
                actor_id=staff_actor_id,
                store_id=store_id,
                role="staff",
                status="ACTIVE",
            ),
        ]
    )
    customer_ids: list[UUID] = []
    for index, (actor_id, name) in enumerate(
        zip(customer_actor_ids, ("林女士", "赵先生")), start=1
    ):
        customer_id = uuid4()
        customer_ids.append(customer_id)
        session.add(
            m.Customer(
                id=customer_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                display_name=name,
                protected_contact_ref=f"hmac:customer-{index}",
                status="ACTIVE",
            )
        )
    await session.flush()

    # ---------- 第 3 层：资源与技能 ----------
    resource_specs: list[dict[str, Any]] = [
        {"type": "therapist", "name": "王技师", "gender": "female", "reputation": 0.92},
        {"type": "therapist", "name": "李技师", "gender": "male", "reputation": 0.85},
        {"type": "therapist", "name": "陈技师", "gender": "female", "reputation": 0.78},
        {"type": "room", "name": "按摩房1", "gender": None, "reputation": None},
        {"type": "room", "name": "按摩房2", "gender": None, "reputation": None},
    ]
    resource_ids: list[UUID] = []
    therapist_ids: list[UUID] = []
    room_ids: list[UUID] = []
    for index, spec in enumerate(resource_specs, start=1):
        resource_id = uuid4()
        resource_ids.append(resource_id)
        session.add(
            m.Resource(
                id=resource_id,
                tenant_id=tenant_id,
                store_id=store_id,
                type=spec["type"],
                unit_code=f"{spec['type']}-{index:02d}",
                display_name=spec["name"],
                status="ACTIVE",
                gender=spec["gender"],
                reputation_score=spec["reputation"],
            )
        )
        if spec["type"] == "therapist":
            therapist_ids.append(resource_id)
        else:
            room_ids.append(resource_id)
    await session.flush()

    for index, resource_id in enumerate(therapist_ids):
        if index in skillless_therapist_indices:
            continue
        for skill in ("tuina", "relax"):
            session.add(
                m.ResourceSkill(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    resource_id=resource_id,
                    skill_code=skill,
                )
            )
    await session.flush()

    # ---------- 第 4 层：班次 ----------
    first_day = now.astimezone(tz).date()
    for offset in range(days):
        local_date = first_day + timedelta(days=offset)
        day_start = resolve_local_datetime(datetime.combine(local_date, time(10, 0)), tz)
        day_end = resolve_local_datetime(datetime.combine(local_date, time(22, 0)), tz)
        for resource_id in resource_ids:
            session.add(
                m.Shift(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    store_id=store_id,
                    resource_id=resource_id,
                    start_at=day_start,
                    end_at=day_end,
                    status="SCHEDULED",
                )
            )
    await session.flush()

    # ---------- 第 5 层：服务、版本、价目 ----------
    service_specs = [
        {
            "key": "shoulder",
            "name": "肩颈舒缓 60 分钟",
            "aliases": ["肩颈", "肩颈按摩", "肩颈舒缓"],
            "duration": 60,
            "amount": 26800,  # 268.00 元，最小货币单位为分
            "requirements": {
                "skills": ["tuina"],
                "resources": [
                    {"type": "therapist", "count": 1},
                    {"type": "room", "count": 1},
                ],
            },
        },
        {
            "key": "back",
            "name": "开背理疗 90 分钟",
            "aliases": ["开背", "开背理疗"],
            "duration": 90,
            "amount": 39800,
            "requirements": {
                "skills": ["tuina"],
                "resources": [
                    {"type": "therapist", "count": 1},
                    {"type": "room", "count": 1},
                ],
            },
        },
    ]
    service_ids: dict[str, UUID] = {}
    price_amount_minor = service_specs[0]["amount"]
    for spec in service_specs:
        service_id = uuid4()
        version_id = uuid4()
        price_id = uuid4()
        service_ids[spec["key"]] = service_id
        terms_hash = content_hash(
            {"service": spec["key"], "rule_version": "rules-2026-09-17"}
        )
        session.add(
            m.ServiceCatalog(
                id=service_id,
                tenant_id=tenant_id,
                store_id=store_id,
                name=spec["name"],
                aliases=list(spec["aliases"]),
                status="ACTIVE",
                current_version_id=version_id,
                current_price_version_id=price_id,
            )
        )
        await session.flush()
        session.add(
            m.ServiceVersion(
                id=version_id,
                tenant_id=tenant_id,
                service_id=service_id,
                store_id=store_id,
                revision=1,
                duration_minutes=spec["duration"],
                requirements=spec["requirements"],
                terms_hash=terms_hash,
                valid_from=now - timedelta(days=30),
            )
        )
        await session.flush()
        session.add(
            m.PriceVersion(
                id=price_id,
                tenant_id=tenant_id,
                store_id=store_id,
                service_version_id=version_id,
                revision=1,
                amount_minor=spec["amount"],
                currency="CNY",
                currency_exponent=2,
                terms_snapshot={
                    "rule_version": "rules-2026-09-17",
                    "cancel_window_hours": 4,
                    "cancel_fee_minor": 0,
                },
                valid_from=now - timedelta(days=30),
            )
        )
        await session.flush()

    # ---------- 第 6 层：知识 ----------
    knowledge_docs: list[dict[str, str]] = [
        {
            "doc_key": "faq-cancel",
            "title": "取消与改约政策",
            "text": (
                "开约前 4 小时以上可以免费取消或改约。"
                "开约前 4 小时以内取消，可能产生费用，具体以门店确认为准。"
                "改约请尽量提前联系门店前台，改约后原时段会立即释放。"
            ),
        },
        {
            "doc_key": "faq-notice",
            "title": "到店注意事项",
            "text": (
                "请提前 10 分钟到店以便更换衣物。"
                "按摩前后建议适量饮水。"
                "孕期、皮肤破损或有急性损伤时请提前告知技师。"
            ),
        },
        {
            "doc_key": "faq-service",
            "title": "服务项目说明",
            "text": (
                "肩颈舒缓 60 分钟主要针对颈肩部位，适合久坐人群。"
                "开背理疗 90 分钟覆盖背部与肩颈，力度可以现场调整。"
                "所有项目都可以要求调整力度。"
            ),
        },
    ]
    for doc in knowledge_docs:
        document_id = uuid4()
        session.add(
            m.KnowledgeDocument(
                id=document_id,
                tenant_id=tenant_id,
                doc_key=doc["doc_key"],
                title=doc["title"],
                version=1,
                source="运营发布 · knowledge-v1",
                valid_from=now - timedelta(days=7),
                status="PUBLISHED",
            )
        )
        await session.flush()
        for ordinal, chunk in enumerate(build_chunks(doc["text"])):
            session.add(
                m.KnowledgeChunk(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    document_id=document_id,
                    document_version=1,
                    ordinal=ordinal,
                    content=chunk,
                    keyword_tokens=tokenize(chunk),
                    source_span={"doc_key": doc["doc_key"], "ordinal": ordinal},
                )
            )
        await session.flush()

    return SeedResult(
        tenant_id=tenant_id,
        store_id=store_id,
        customer_ids=customer_ids,
        customer_actor_ids=customer_actor_ids,
        manager_actor_id=manager_actor_id,
        staff_actor_id=staff_actor_id,
        service_ids=service_ids,
        resource_ids=resource_ids,
        therapist_ids=therapist_ids,
        room_ids=room_ids,
        price_amount_minor=price_amount_minor,
    )


def local_date_offset(now: datetime, *, days_ahead: int = 1) -> date:
    """门店本地日期偏移，供测试定位"明天"这类相对表达。"""

    tz = load_zone(STORE_TIMEZONE)
    return now.astimezone(tz).date() + timedelta(days=days_ahead)
