"""Materialize the synthetic business fixture into an isolated test schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from appointment.core.hashing import content_hash
from appointment.db import models as m


@dataclass(frozen=True, slots=True)
class SeededSyntheticWorld:
    tenant_id: UUID
    store_ids: dict[str, UUID]
    service_ids: dict[tuple[str, str], UUID]
    customer_ids: dict[str, UUID]
    actor_ids: dict[str, UUID]
    resource_ids: dict[str, UUID]


def _id(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "agentagent-synthetic/" + "/".join(parts))


def _datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError(f"synthetic timestamp must include a timezone: {value}")
    return result


def _service_snapshot(
    service: dict[str, Any], service_id: UUID, version_id: UUID
) -> dict[str, Any]:
    return {
        "service_id": str(service_id),
        "service_version_id": str(version_id),
        "name": service["name"],
        "duration_minutes": service["duration_minutes"],
        "buffer_minutes": service["buffer_minutes"],
        "amount_minor": service["price_minor"],
    }


async def seed_synthetic_world(
    session: AsyncSession,
    dataset: dict[str, Any],
) -> SeededSyntheticWorld:
    """Load synthetic catalog, customers, schedules and bookings into PostgreSQL.

    This writes only to the session's already-isolated schema. IDs are stable for
    easier comparison against fixture scenario expectations.
    """

    if dataset.get("data_origin") != "generated_synthetic":
        raise ValueError("refusing to seed data not marked generated_synthetic")

    world = dataset["world"]
    tenant_id = _id("tenant", dataset["dataset_version"])
    store_ids = {item["store_id"]: _id("store", item["store_id"]) for item in world["stores"]}
    customer_ids = {
        item["customer_id"]: _id("customer", item["customer_id"])
        for item in world["customers"]
    }
    actor_ids = {
        item["customer_id"]: _id("actor", item["customer_id"])
        for item in world["customers"]
    }
    resource_ids = {
        item["therapist_id"]: _id("resource", item["therapist_id"])
        for item in world["therapists"]
    }
    resource_ids.update({
        item["room_id"]: _id("resource", item["room_id"])
        for item in world["rooms"]
    })
    service_ids = {
        (store["store_id"], service_id): _id("service", store["store_id"], service_id)
        for store in world["stores"]
        for service_id in store["service_ids"]
    }

    reference_time = _datetime(dataset["reference_time"])
    session.add(m.Tenant(id=tenant_id, name="合成预约评测租户", status="ACTIVE"))
    await session.flush()

    stores_by_key = {item["store_id"]: item for item in world["stores"]}
    session.add_all([
        m.Store(
            id=store_ids[item["store_id"]],
            tenant_id=tenant_id,
            name=item["name"],
            timezone=item["timezone"],
            status="ACTIVE",
            calendar_version=1,
        )
        for item in world["stores"]
    ])
    session.add_all([
        m.Actor(id=actor_ids[item["customer_id"]], tenant_id=tenant_id, status="ACTIVE")
        for item in world["customers"]
    ])
    await session.flush()

    session.add_all([
        m.Customer(
            id=customer_ids[item["customer_id"]],
            tenant_id=tenant_id,
            actor_id=actor_ids[item["customer_id"]],
            protected_contact_ref=f"synthetic:{item['customer_id']}",
            display_name=item["customer_id"],
            status="ACTIVE",
        )
        for item in world["customers"]
    ])
    session.add_all([
        m.Resource(
            id=resource_ids[item["therapist_id"]],
            tenant_id=tenant_id,
            store_id=store_ids[item["store_id"]],
            type="therapist",
            unit_code=item["therapist_id"],
            display_name=item["display_name"],
            status="ACTIVE",
            reputation_score=0.8,
        )
        for item in world["therapists"]
    ])
    session.add_all([
        m.Resource(
            id=resource_ids[item["room_id"]],
            tenant_id=tenant_id,
            store_id=store_ids[item["store_id"]],
            type="room",
            unit_code=item["room_id"],
            display_name=item["room_id"],
            status="ACTIVE",
        )
        for item in world["rooms"]
    ])
    await session.flush()

    session.add_all([
        m.ResourceSkill(
            id=_id("skill", therapist["therapist_id"], skill),
            tenant_id=tenant_id,
            resource_id=resource_ids[therapist["therapist_id"]],
            skill_code=skill,
        )
        for therapist in world["therapists"]
        for skill in therapist["skills"]
    ])
    session.add_all([
        m.ResourceSkill(
            id=_id("room-skill", room["room_id"], room["room_type"]),
            tenant_id=tenant_id,
            resource_id=resource_ids[room["room_id"]],
            skill_code=f"room:{room['room_type']}",
        )
        for room in world["rooms"]
    ])
    session.add_all([
        m.BusinessCalendar(
            id=_id("calendar", store["store_id"]),
            tenant_id=tenant_id,
            store_id=store_ids[store["store_id"]],
            revision=1,
            weekly_windows={
                weekday: ([[hours["open"], hours["close"]]] if hours else [])
                for weekday, hours in store["weekly_hours"].items()
            },
            effective_from=reference_time.date() - timedelta(days=365),
        )
        for store in world["stores"]
    ])
    await session.flush()

    services_by_key = {item["service_id"]: item for item in world["services"]}
    service_version_ids: dict[tuple[str, str], UUID] = {}
    service_price_ids: dict[tuple[str, str], UUID] = {}
    for store in world["stores"]:
        for service_key in store["service_ids"]:
            service = services_by_key[service_key]
            pair = (store["store_id"], service_key)
            catalog_id = service_ids[pair]
            version_id = _id("service-version", *pair)
            price_id = _id("service-price", *pair)
            service_version_ids[pair] = version_id
            service_price_ids[pair] = price_id
            terms_hash = content_hash(
                {"dataset": dataset["dataset_version"], "service": service_key}
            )
            requirements = {
                "skills": [service["required_skill"]],
                "resources": [
                    {"type": "therapist", "count": 1},
                    {
                        "type": "room",
                        "count": 1,
                        "skills": [f"room:{service['room_type']}"],
                    },
                ],
                "buffer_minutes": service["buffer_minutes"],
            }
            session.add(m.ServiceCatalog(
                id=catalog_id,
                tenant_id=tenant_id,
                store_id=store_ids[store["store_id"]],
                name=service["name"],
                aliases=list(service["aliases"]),
                status="ACTIVE",
                current_version_id=version_id,
                current_price_version_id=price_id,
            ))
            session.add(m.ServiceVersion(
                id=version_id,
                tenant_id=tenant_id,
                service_id=catalog_id,
                store_id=store_ids[store["store_id"]],
                revision=1,
                duration_minutes=service["duration_minutes"],
                requirements=requirements,
                terms_hash=terms_hash,
                valid_from=reference_time - timedelta(days=365),
            ))
    await session.flush()

    for store in world["stores"]:
        for service_key in store["service_ids"]:
            service = services_by_key[service_key]
            pair = (store["store_id"], service_key)
            session.add(m.PriceVersion(
                id=service_price_ids[pair],
                tenant_id=tenant_id,
                store_id=store_ids[store["store_id"]],
                service_version_id=service_version_ids[pair],
                revision=1,
                amount_minor=service["price_minor"],
                currency="CNY",
                currency_exponent=2,
                terms_snapshot={"synthetic": True},
                valid_from=reference_time - timedelta(days=365),
            ))
    await session.flush()

    shifts: list[m.Shift] = []
    absences: list[m.ResourceAbsence] = []
    for therapist in world["therapists"]:
        for shift_index, shift in enumerate(therapist["shifts"]):
            shifts.append(m.Shift(
                id=_id("shift", therapist["therapist_id"], str(shift_index)),
                tenant_id=tenant_id,
                store_id=store_ids[therapist["store_id"]],
                resource_id=resource_ids[therapist["therapist_id"]],
                start_at=_datetime(shift["start_at"]),
                end_at=_datetime(shift["end_at"]),
                status="SCHEDULED",
            ))
            for break_index, item in enumerate(shift["breaks"]):
                absences.append(m.ResourceAbsence(
                    id=_id("break", therapist["therapist_id"], str(shift_index), str(break_index)),
                    tenant_id=tenant_id,
                    store_id=store_ids[therapist["store_id"]],
                    resource_id=resource_ids[therapist["therapist_id"]],
                    start_at=_datetime(item["start_at"]),
                    end_at=_datetime(item["occupancy_end_at"]),
                    approval_status="APPROVED",
                    reason="合成排班休息时段",
                ))
    first_day = date.fromisoformat(dataset["schedule_start_date"])
    for room in world["rooms"]:
        store = stores_by_key[room["store_id"]]
        zone = ZoneInfo(store["timezone"])
        for day_offset in range(dataset["schedule_days"]):
            local_day = first_day + timedelta(days=day_offset)
            hours = store["weekly_hours"][
                ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[local_day.weekday()]
            ]
            if hours is None:
                continue
            shifts.append(m.Shift(
                id=_id("room-shift", room["room_id"], local_day.isoformat()),
                tenant_id=tenant_id,
                store_id=store_ids[room["store_id"]],
                resource_id=resource_ids[room["room_id"]],
                start_at=datetime.combine(
                    local_day, time.fromisoformat(hours["open"]), tzinfo=zone
                ),
                end_at=datetime.combine(
                    local_day, time.fromisoformat(hours["close"]), tzinfo=zone
                ),
                status="SCHEDULED",
            ))
    session.add_all(shifts)
    session.add_all(absences)
    await session.flush()

    operations: list[m.Operation] = []
    appointments: list[m.Appointment] = []
    allocations: list[m.ResourceAllocation] = []

    def add_appointment(
        *,
        appointment_key: str,
        store_key: str,
        customer_key: str,
        service_key: str,
        therapist_key: str,
        room_key: str,
        start_at: datetime,
        status: str,
        occupancy_end: datetime | None,
    ) -> None:
        service = services_by_key[service_key]
        appointment_id = _id("appointment", appointment_key)
        operation_id = _id("operation", appointment_key)
        pair = (store_key, service_key)
        service_id = service_ids[pair]
        service_version_id = service_version_ids[pair]
        therapist_resource_id = resource_ids[therapist_key]
        room_resource_id = resource_ids[room_key]
        actual_end = start_at + timedelta(minutes=service["duration_minutes"])
        request = {
            "synthetic_appointment": appointment_key,
            "customer_id": customer_key,
            "store_id": store_key,
            "service_id": service_key,
            "start_at": start_at.isoformat(),
        }
        operations.append(m.Operation(
            id=operation_id,
            tenant_id=tenant_id,
            customer_id=customer_ids[customer_key],
            actor_id=actor_ids[customer_key],
            action="CREATE",
            idempotency_key=f"seed:{appointment_key}",
            request_hash=content_hash(request),
            status="SUCCEEDED",
            result={"appointment_id": str(appointment_id), "synthetic_seed": True},
            started_at=start_at,
            completed_at=start_at,
        ))
        appointments.append(m.Appointment(
            id=appointment_id,
            tenant_id=tenant_id,
            customer_id=customer_ids[customer_key],
            store_id=store_ids[store_key],
            status=status,
            fulfillment_status="READY",
            service_snapshot=_service_snapshot(service, service_id, service_version_id),
            resource_snapshot={"resources": [
                {"resource_id": str(therapist_resource_id), "type": "therapist"},
                {"resource_id": str(room_resource_id), "type": service["room_type"]},
            ]},
            start_at=start_at,
            end_at=actual_end,
            amount_minor=service["price_minor"],
            currency="CNY",
            currency_exponent=2,
            terms_snapshot={"synthetic": True},
            created_operation_id=operation_id,
        ))
        if occupancy_end is not None:
            for index, resource_id in enumerate((therapist_resource_id, room_resource_id)):
                allocations.append(m.ResourceAllocation(
                    id=_id("allocation", appointment_key, str(index)),
                    tenant_id=tenant_id,
                    store_id=store_ids[store_key],
                    resource_id=resource_id,
                    hold_id=None,
                    appointment_id=appointment_id,
                    start_at=start_at,
                    end_at=occupancy_end,
                    state="BOOKED",
                    expires_at=None,
                ))

    for booking in world["existing_bookings"]:
        add_appointment(
            appointment_key=booking["booking_id"],
            store_key=booking["store_id"],
            customer_key=booking["customer_id"],
            service_key=booking["service_id"],
            therapist_key=booking["therapist_id"],
            room_key=booking["room_id"],
            start_at=_datetime(booking["start_at"]),
            status="CONFIRMED",
            occupancy_end=_datetime(booking["occupancy_end_at"]),
        )

    for customer in world["customers"]:
        for visit_index, visit in enumerate(customer["history"]):
            store_key = customer["store_id"]
            service_key = visit["service_id"]
            service = services_by_key[service_key]
            room = next(
                item for item in world["rooms"]
                if item["store_id"] == store_key and item["room_type"] == service["room_type"]
            )
            add_appointment(
                appointment_key=f"HIST-{customer['customer_id']}-{visit_index:03d}",
                store_key=store_key,
                customer_key=customer["customer_id"],
                service_key=service_key,
                therapist_key=visit["therapist_id"],
                room_key=room["room_id"],
                start_at=_datetime(visit["visited_at"]),
                status="COMPLETED",
                occupancy_end=None,
            )

    session.add_all(operations)
    session.add_all(appointments)
    await session.flush()
    session.add_all(allocations)
    await session.flush()

    return SeededSyntheticWorld(
        tenant_id=tenant_id,
        store_ids=store_ids,
        service_ids=service_ids,
        customer_ids=customer_ids,
        actor_ids=actor_ids,
        resource_ids=resource_ids,
    )
