"""Generate a reproducible, constraint-consistent synthetic appointment dataset.

This dataset is for offline evaluation and fixture design. It contains no real
customer information and makes no claim about real-world booking distributions.

Run from the repository root:
    .venv/bin/python -B evals/generate_synthetic_business_data.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "evals" / "cases" / "synthetic_business_v1.json"
DEFAULT_SEED = 20260924
TIMEZONE = "Asia/Shanghai"
TZ = ZoneInfo(TIMEZONE)
REFERENCE_TIME = datetime.fromisoformat("2026-09-24T08:00:00+08:00")
FIRST_SERVICE_DATE = date(2026, 9, 25)
HORIZON_DAYS = 14
SLOT_STEP_MINUTES = 30
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

SERVICE_SPECS: dict[str, dict[str, Any]] = {
    "neck_relief": {
        "name": "肩颈舒缓",
        "aliases": ["肩颈", "肩颈舒缓", "肩颈按摩"],
        "duration_minutes": 60,
        "buffer_minutes": 15,
        "price_minor": 26800,
        "required_skill": "tuina",
        "room_type": "treatment",
    },
    "back_therapy": {
        "name": "开背理疗",
        "aliases": ["开背", "开背理疗"],
        "duration_minutes": 90,
        "buffer_minutes": 15,
        "price_minor": 39800,
        "required_skill": "tuina",
        "room_type": "treatment",
    },
    "foot_relax": {
        "name": "足部放松",
        "aliases": ["足部", "足部放松", "足疗"],
        "duration_minutes": 45,
        "buffer_minutes": 15,
        "price_minor": 19800,
        "required_skill": "foot",
        "room_type": "foot",
    },
    "aroma_relax": {
        "name": "芳香舒缓",
        "aliases": ["芳香", "芳香舒缓"],
        "duration_minutes": 60,
        "buffer_minutes": 15,
        "price_minor": 32800,
        "required_skill": "aroma",
        "room_type": "treatment",
    },
    "body_relax": {
        "name": "全身放松",
        "aliases": ["全身放松", "放松护理"],
        "duration_minutes": 75,
        "buffer_minutes": 15,
        "price_minor": 35800,
        "required_skill": "relax",
        "room_type": "treatment",
    },
}

STORE_SPECS = (
    {
        "store_id": "STORE-A",
        "name": "合成门店甲",
        "service_ids": ["neck_relief", "back_therapy", "foot_relax", "body_relax"],
        "weekday_hours": ("10:00", "20:00"),
        "weekend_hours": ("11:00", "19:00"),
        "closed_weekdays": [],
    },
    {
        "store_id": "STORE-B",
        "name": "合成门店乙",
        "service_ids": ["neck_relief", "foot_relax", "aroma_relax", "body_relax"],
        "weekday_hours": ("11:00", "21:00"),
        "weekend_hours": ("11:00", "21:00"),
        "closed_weekdays": [],
    },
    {
        "store_id": "STORE-C",
        "name": "合成门店丙",
        "service_ids": ["back_therapy", "aroma_relax", "body_relax"],
        "weekday_hours": ("09:00", "19:00"),
        "weekend_hours": ("10:00", "18:00"),
        "closed_weekdays": ["mon"],
    },
)

THERAPIST_PROFILES = (
    ("tuina", "relax"),
    ("tuina", "foot"),
    ("foot", "relax"),
    ("aroma", "relax"),
    ("tuina", "aroma"),
)

EXPECTED_CASE_COUNTS = {
    "feasible_exact_slot": 12,
    "missing_service": 6,
    "ambiguous_service": 6,
    "outside_hours": 6,
    "preferred_therapist_unavailable": 8,
    "booking_conflict": 8,
    "stale_candidate": 6,
    "idempotent_confirmation": 6,
    "concurrent_single_slot_race": 6,
}


def _at(day: date, clock: str) -> datetime:
    hour, minute = (int(piece) for piece in clock.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


def _overlaps(left_start: datetime, left_end: datetime, right_start: datetime, right_end: datetime) -> bool:
    return left_start < right_end and right_start < left_end


def _store_hours(store: dict[str, Any], day: date) -> tuple[datetime, datetime] | None:
    weekday = WEEKDAYS[day.weekday()]
    hours = store["weekly_hours"][weekday]
    if hours is None:
        return None
    return _at(day, hours["open"]), _at(day, hours["close"])


def _staff_is_free(
    therapist: dict[str, Any],
    start: datetime,
    occupied_end: datetime,
    *,
    exclude_booking_ids: set[str] | None = None,
    bookings: list[dict[str, Any]],
) -> bool:
    excluded = exclude_booking_ids or set()
    for shift in therapist["shifts"]:
        shift_start = datetime.fromisoformat(shift["start_at"])
        shift_end = datetime.fromisoformat(shift["end_at"])
        if shift_start <= start and occupied_end <= shift_end:
            if any(
                _overlaps(
                    start,
                    occupied_end,
                    datetime.fromisoformat(item["start_at"]),
                    datetime.fromisoformat(item["occupancy_end_at"]),
                )
                for item in shift["breaks"]
            ):
                continue
            if any(
                booking["therapist_id"] == therapist["therapist_id"]
                and booking["booking_id"] not in excluded
                and _overlaps(
                    start,
                    occupied_end,
                    datetime.fromisoformat(booking["start_at"]),
                    datetime.fromisoformat(booking["occupancy_end_at"]),
                )
                for booking in bookings
            ):
                continue
            return True
    return False


def _room_is_free(
    room: dict[str, Any],
    start: datetime,
    occupied_end: datetime,
    *,
    exclude_booking_ids: set[str] | None = None,
    bookings: list[dict[str, Any]],
) -> bool:
    excluded = exclude_booking_ids or set()
    return not any(
        booking["room_id"] == room["room_id"]
        and booking["booking_id"] not in excluded
        and _overlaps(
            start,
            occupied_end,
            datetime.fromisoformat(booking["start_at"]),
            datetime.fromisoformat(booking["occupancy_end_at"]),
        )
        for booking in bookings
    )


def candidate_pairs_at(
    world: dict[str, Any],
    *,
    store_id: str,
    service_id: str,
    start_at: str,
    exclude_booking_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    """Reference availability oracle for the synthetic fixture only."""

    stores = {item["store_id"]: item for item in world["stores"]}
    services = {item["service_id"]: item for item in world["services"]}
    store = stores[store_id]
    service = services[service_id]
    start = datetime.fromisoformat(start_at)
    occupied_end = start + timedelta(
        minutes=service["duration_minutes"] + service["buffer_minutes"]
    )
    hours = _store_hours(store, start.date())
    if hours is None or start < hours[0] or occupied_end > hours[1]:
        return []

    bookings = world["existing_bookings"]
    eligible_therapists = [
        therapist
        for therapist in world["therapists"]
        if therapist["store_id"] == store_id
        and service["required_skill"] in therapist["skills"]
        and _staff_is_free(
            therapist,
            start,
            occupied_end,
            exclude_booking_ids=exclude_booking_ids,
            bookings=bookings,
        )
    ]
    eligible_rooms = [
        room
        for room in world["rooms"]
        if room["store_id"] == store_id
        and room["room_type"] == service["room_type"]
        and _room_is_free(
            room,
            start,
            occupied_end,
            exclude_booking_ids=exclude_booking_ids,
            bookings=bookings,
        )
    ]
    return [
        {"therapist_id": therapist["therapist_id"], "room_id": room["room_id"]}
        for therapist in eligible_therapists
        for room in eligible_rooms
    ]


def _build_world(rng: random.Random) -> dict[str, Any]:
    stores: list[dict[str, Any]] = []
    for spec in STORE_SPECS:
        weekly_hours: dict[str, dict[str, str] | None] = {}
        for weekday in WEEKDAYS:
            if weekday in spec["closed_weekdays"]:
                weekly_hours[weekday] = None
            else:
                opened, closed = (
                    spec["weekend_hours"] if weekday in {"sat", "sun"} else spec["weekday_hours"]
                )
                weekly_hours[weekday] = {"open": opened, "close": closed}
        stores.append({
            "store_id": spec["store_id"],
            "name": spec["name"],
            "timezone": TIMEZONE,
            "weekly_hours": weekly_hours,
            "service_ids": list(spec["service_ids"]),
            "closed_weekdays": list(spec["closed_weekdays"]),
        })

    services = [
        {"service_id": service_id, **spec}
        for service_id, spec in SERVICE_SPECS.items()
    ]
    rooms: list[dict[str, Any]] = []
    therapists: list[dict[str, Any]] = []
    for store_index, store in enumerate(stores):
        store_id = store["store_id"]
        for room_index, room_type in enumerate(("treatment", "treatment", "foot"), start=1):
            rooms.append({
                "room_id": f"ROOM-{store_id[-1]}-{room_index:02d}",
                "store_id": store_id,
                "room_type": room_type,
                "status": "ACTIVE",
            })

        for therapist_index, skills in enumerate(THERAPIST_PROFILES, start=1):
            therapist_id = f"TECH-{store_id[-1]}-{therapist_index:02d}"
            shifts: list[dict[str, Any]] = []
            start_offsets = (0, 60, 120, 180, 0)
            for day_offset in range(HORIZON_DAYS):
                day = FIRST_SERVICE_DATE + timedelta(days=day_offset)
                hours = _store_hours(store, day)
                if hours is None:
                    continue
                # Deterministic rotating rest day: five or six shifts per week.
                if (day.weekday() + therapist_index + store_index) % 7 in {0, 1}:
                    continue
                shift_start = max(
                    hours[0],
                    hours[0] + timedelta(minutes=start_offsets[therapist_index - 1]),
                )
                shift_end = min(hours[1], shift_start + timedelta(hours=8))
                if shift_end - shift_start < timedelta(hours=6):
                    shift_start = hours[0]
                    shift_end = min(hours[1], shift_start + timedelta(hours=8))
                break_start = shift_start + timedelta(hours=4)
                break_end = min(break_start + timedelta(minutes=30), shift_end)
                shifts.append({
                    "date": day.isoformat(),
                    "start_at": _iso(shift_start),
                    "end_at": _iso(shift_end),
                    "breaks": ([{
                        "start_at": _iso(break_start),
                        "occupancy_end_at": _iso(break_end),
                    }] if break_end > break_start else []),
                })
            therapists.append({
                "therapist_id": therapist_id,
                "store_id": store_id,
                "display_name": f"合成技师{store_id[-1]}-{therapist_index:02d}",
                "skills": list(skills),
                "status": "ACTIVE",
                "shifts": shifts,
            })

    customers: list[dict[str, Any]] = []
    customer_ids_by_store: dict[str, list[str]] = {}
    for store in stores:
        store_id = store["store_id"]
        customer_ids_by_store[store_id] = []
        offered = store["service_ids"]
        store_therapists = [item for item in therapists if item["store_id"] == store_id]
        for customer_index in range(1, 11):
            customer_id = f"CUS-{store_id[-1]}-{customer_index:03d}"
            customer_ids_by_store[store_id].append(customer_id)
            visit_count = 0 if customer_index <= 2 else rng.randint(1, 8)
            history = []
            for visit_index in range(visit_count):
                service_id = rng.choice(offered)
                service = SERVICE_SPECS[service_id]
                qualified = [
                    item["therapist_id"] for item in store_therapists
                    if service["required_skill"] in item["skills"]
                ]
                history.append({
                    "service_id": service_id,
                    "therapist_id": rng.choice(qualified),
                    "visited_at": _iso(
                        REFERENCE_TIME - timedelta(days=rng.randint(30, 365))
                    ),
                    "signal": rng.choice(("repeat_choice", "positive_feedback")),
                })
            customers.append({
                "customer_id": customer_id,
                "store_id": store_id,
                "history": history,
            })

    world: dict[str, Any] = {
        "stores": stores,
        "services": services,
        "therapists": therapists,
        "rooms": rooms,
        "customers": customers,
        "existing_bookings": [],
    }
    for therapist in therapists:
        store = next(item for item in stores if item["store_id"] == therapist["store_id"])
        service_options = [
            (service_id, SERVICE_SPECS[service_id])
            for service_id in store["service_ids"]
            if SERVICE_SPECS[service_id]["required_skill"] in therapist["skills"]
        ]
        if not service_options:
            continue
        for shift in therapist["shifts"]:
            if rng.random() > 0.43:
                continue
            service_id, service = rng.choice(service_options)
            shift_start = datetime.fromisoformat(shift["start_at"])
            shift_end = datetime.fromisoformat(shift["end_at"])
            candidate_starts: list[datetime] = []
            cursor = shift_start
            while cursor + timedelta(
                minutes=service["duration_minutes"] + service["buffer_minutes"]
            ) <= shift_end:
                candidate_starts.append(cursor)
                cursor += timedelta(minutes=SLOT_STEP_MINUTES)
            rng.shuffle(candidate_starts)
            customer_ids = customer_ids_by_store[therapist["store_id"]]
            for start in candidate_starts:
                pairs = candidate_pairs_at(
                    world,
                    store_id=therapist["store_id"],
                    service_id=service_id,
                    start_at=_iso(start),
                )
                possible = [pair for pair in pairs if pair["therapist_id"] == therapist["therapist_id"]]
                if not possible:
                    continue
                pair = rng.choice(possible)
                end = start + timedelta(minutes=service["duration_minutes"])
                occupancy_end = end + timedelta(minutes=service["buffer_minutes"])
                world["existing_bookings"].append({
                    "booking_id": f"BOOK-{len(world['existing_bookings']) + 1:04d}",
                    "store_id": therapist["store_id"],
                    "customer_id": rng.choice(customer_ids),
                    "service_id": service_id,
                    "therapist_id": pair["therapist_id"],
                    "room_id": pair["room_id"],
                    "start_at": _iso(start),
                    "end_at": _iso(end),
                    "occupancy_end_at": _iso(occupancy_end),
                    "status": "CONFIRMED",
                })
                break
    return world


def _all_exact_slots(world: dict[str, Any]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for store in world["stores"]:
        for service_id in store["service_ids"]:
            for day_offset in range(HORIZON_DAYS):
                day = FIRST_SERVICE_DATE + timedelta(days=day_offset)
                hours = _store_hours(store, day)
                if hours is None:
                    continue
                cursor = hours[0]
                while cursor < hours[1]:
                    pairs = candidate_pairs_at(
                        world,
                        store_id=store["store_id"],
                        service_id=service_id,
                        start_at=_iso(cursor),
                    )
                    if pairs:
                        slots.append({
                            "store_id": store["store_id"],
                            "service_id": service_id,
                            "start_at": _iso(cursor),
                            "candidate_pairs": pairs,
                        })
                    cursor += timedelta(minutes=SLOT_STEP_MINUTES)
    return slots


def _request_text(service_name: str, start_at: str) -> str:
    start = datetime.fromisoformat(start_at)
    return f"请帮我预约{service_name}，{start:%Y年%m月%d日 %H:%M}。"


def _case(
    case_id: str,
    category: str,
    *,
    store_id: str,
    customer_id: str,
    request: str,
    service_id: str | None,
    requested_start_at: str | None,
    expected: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": case_id,
        "category": category,
        "store_id": store_id,
        "customer_id": customer_id,
        "request": request,
        "service_id": service_id,
        "requested_start_at": requested_start_at,
        "expected": expected,
    }


def _build_cases(world: dict[str, Any], rng: random.Random) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    customers_by_store = {
        store_id: [item["customer_id"] for item in world["customers"] if item["store_id"] == store_id]
        for store_id in (item["store_id"] for item in world["stores"])
    }
    services_by_id = {item["service_id"]: item for item in world["services"]}
    stores_by_id = {item["store_id"]: item for item in world["stores"]}
    candidates = _all_exact_slots(world)
    if len(candidates) < 40:
        raise ValueError(f"synthetic generator produced too few available slots: {len(candidates)}")

    rng.shuffle(candidates)
    selected_slots = candidates[:12]
    for index, slot in enumerate(selected_slots, start=1):
        service = services_by_id[slot["service_id"]]
        cases.append(_case(
            f"SB-FEASIBLE-{index:03d}",
            "feasible_exact_slot",
            store_id=slot["store_id"],
            customer_id=rng.choice(customers_by_store[slot["store_id"]]),
            request=_request_text(service["name"], slot["start_at"]),
            service_id=slot["service_id"],
            requested_start_at=slot["start_at"],
            expected={
                "decision": "offer_candidates",
                "candidate_pairs": slot["candidate_pairs"],
                "appointment_count_delta_before_confirmation": 0,
                "hold_count_delta_before_selection": 0,
            },
        ))

    for index in range(1, 7):
        store = world["stores"][(index - 1) % len(world["stores"])]
        day = FIRST_SERVICE_DATE + timedelta(days=index)
        start = _at(day, "15:00")
        cases.append(_case(
            f"SB-MISSING-SERVICE-{index:03d}",
            "missing_service",
            store_id=store["store_id"],
            customer_id=rng.choice(customers_by_store[store["store_id"]]),
            request=f"我想在{start:%Y年%m月%d日 %H:%M}预约，能先介绍一下项目吗？",
            service_id=None,
            requested_start_at=_iso(start),
            expected={
                "decision": "clarify",
                "required_fields": ["service_id"],
                "appointment_count_delta": 0,
                "hold_count_delta": 0,
            },
        ))

    for index in range(1, 7):
        store = world["stores"][(index - 1) % len(world["stores"])]
        day = FIRST_SERVICE_DATE + timedelta(days=index + 2)
        start = _at(day, "16:00")
        choices = store["service_ids"][:2]
        names = [services_by_id[item]["name"] for item in choices]
        cases.append(_case(
            f"SB-AMBIGUOUS-SERVICE-{index:03d}",
            "ambiguous_service",
            store_id=store["store_id"],
            customer_id=rng.choice(customers_by_store[store["store_id"]]),
            request=f"我想做个放松项目，{start:%Y年%m月%d日 %H:%M}左右有空吗？",
            service_id=None,
            requested_start_at=_iso(start),
            expected={
                "decision": "clarify_service",
                "candidate_service_ids": choices,
                "candidate_service_names": names,
                "appointment_count_delta": 0,
                "hold_count_delta": 0,
            },
        ))

    for index in range(1, 7):
        store = world["stores"][(index - 1) % len(world["stores"])]
        service_id = store["service_ids"][(index - 1) % len(store["service_ids"])]
        service = services_by_id[service_id]
        day = FIRST_SERVICE_DATE + timedelta(days=index + 4)
        hours = _store_hours(store, day)
        if hours is None:
            day += timedelta(days=1)
            hours = _store_hours(store, day)
        assert hours is not None
        start = hours[1] - timedelta(minutes=30)
        cases.append(_case(
            f"SB-OUTSIDE-HOURS-{index:03d}",
            "outside_hours",
            store_id=store["store_id"],
            customer_id=rng.choice(customers_by_store[store["store_id"]]),
            request=_request_text(service["name"], _iso(start)),
            service_id=service_id,
            requested_start_at=_iso(start),
            expected={
                "decision": "offer_alternative_or_explain_unavailable",
                "candidate_pairs": [],
                "appointment_count_delta": 0,
                "hold_count_delta": 0,
            },
        ))

    preferred_pool: list[tuple[dict[str, Any], str]] = []
    for slot in candidates:
        service = services_by_id[slot["service_id"]]
        qualified = [
            therapist["therapist_id"]
            for therapist in world["therapists"]
            if therapist["store_id"] == slot["store_id"]
            and service["required_skill"] in therapist["skills"]
        ]
        available = {pair["therapist_id"] for pair in slot["candidate_pairs"]}
        preferred_options = [item for item in qualified if item not in available]
        if preferred_options:
            preferred_pool.append((slot, rng.choice(preferred_options)))
    if len(preferred_pool) < 8:
        raise ValueError("synthetic generator could not produce enough unavailable-preference cases")
    rng.shuffle(preferred_pool)
    for index, (slot, preferred_id) in enumerate(preferred_pool[:8], start=1):
        service = services_by_id[slot["service_id"]]
        cases.append(_case(
            f"SB-PREFERRED-UNAVAILABLE-{index:03d}",
            "preferred_therapist_unavailable",
            store_id=slot["store_id"],
            customer_id=rng.choice(customers_by_store[slot["store_id"]]),
            request=(
                _request_text(service["name"], slot["start_at"])
                + f"如果可以，优先安排{preferred_id}。"
            ),
            service_id=slot["service_id"],
            requested_start_at=slot["start_at"],
            expected={
                "decision": "offer_available_alternative",
                "preferred_therapist_id": preferred_id,
                "candidate_pairs": slot["candidate_pairs"],
                "preferred_therapist_must_not_be_offered": True,
                "appointment_count_delta_before_confirmation": 0,
            },
        ))

    bookings = list(world["existing_bookings"])
    if len(bookings) < 8:
        raise ValueError(f"synthetic generator produced too few existing bookings: {len(bookings)}")
    rng.shuffle(bookings)
    for index, booking in enumerate(bookings[:8], start=1):
        service = services_by_id[booking["service_id"]]
        pairs = candidate_pairs_at(
            world,
            store_id=booking["store_id"],
            service_id=booking["service_id"],
            start_at=booking["start_at"],
        )
        blocked_pair = {
            "therapist_id": booking["therapist_id"],
            "room_id": booking["room_id"],
        }
        cases.append(_case(
            f"SB-BOOKING-CONFLICT-{index:03d}",
            "booking_conflict",
            store_id=booking["store_id"],
            customer_id=rng.choice(customers_by_store[booking["store_id"]]),
            request=_request_text(service["name"], booking["start_at"]),
            service_id=booking["service_id"],
            requested_start_at=booking["start_at"],
            expected={
                "decision": "exclude_occupied_resources",
                "blocked_booking_id": booking["booking_id"],
                "blocked_pair": blocked_pair,
                "candidate_pairs": pairs,
                "blocked_pair_must_not_be_offered": True,
            },
        ))

    for index in range(1, 7):
        slot = candidates[(index * 7) % len(candidates)]
        service = services_by_id[slot["service_id"]]
        invalidation = (
            "shift_revision_changed",
            "price_revision_changed",
            "candidate_expired",
        )[(index - 1) % 3]
        cases.append(_case(
            f"SB-STALE-CANDIDATE-{index:03d}",
            "stale_candidate",
            store_id=slot["store_id"],
            customer_id=rng.choice(customers_by_store[slot["store_id"]]),
            request=_request_text(service["name"], slot["start_at"]),
            service_id=slot["service_id"],
            requested_start_at=slot["start_at"],
            expected={
                "decision": "reject_stale_confirmation",
                "invalidation": invalidation,
                "appointment_count_delta": 0,
                "hold_count_delta": 0,
                "operation_count_delta": 0,
            },
        ))

    for index in range(1, 7):
        store = world["stores"][(index - 1) % len(world["stores"])]
        customer = next(
            item for item in world["customers"]
            if item["store_id"] == store["store_id"]
        )
        idempotency_key = f"SYN-IDEMPOTENCY-{index:03d}"
        request_identity = {
            "proposal_id": f"SYN-PROPOSAL-{index:03d}",
            "action": "CREATE",
            "customer_id": customer["customer_id"],
            "store_id": store["store_id"],
        }
        request_digest = hashlib.sha256(
            json.dumps(request_identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cases.append(_case(
            f"SB-IDEMPOTENCY-{index:03d}",
            "idempotent_confirmation",
            store_id=store["store_id"],
            customer_id=customer["customer_id"],
            request="对同一份待确认预约重复提交两次相同确认请求。",
            service_id=None,
            requested_start_at=None,
            expected={
                "decision": "replay_original_result",
                "idempotency_key": idempotency_key,
                "request_digest": request_digest,
                "repeat_count": 2,
                "same_idempotency_key": True,
                "same_request_digest": True,
                "appointment_count_delta": 1,
                "operation_count_delta": 1,
                "response_appointment_ids_equal": True,
            },
        ))

    race_slots = list(candidates)
    rng.shuffle(race_slots)
    for index, slot in enumerate(race_slots[:6], start=1):
        cases.append(_case(
            f"SB-CONCURRENT-RACE-{index:03d}",
            "concurrent_single_slot_race",
            store_id=slot["store_id"],
            customer_id=rng.choice(customers_by_store[slot["store_id"]]),
            request=(
                f"两个测试客户同时竞争{slot['service_id']}的"
                f"{datetime.fromisoformat(slot['start_at']):%Y年%m月%d日 %H:%M}时段。"
            ),
            service_id=slot["service_id"],
            requested_start_at=slot["start_at"],
            expected={
                "decision": "exactly_one_competing_confirmation_wins",
                "target_pair": slot["candidate_pairs"][0],
                "contending_customer_ids": customers_by_store[slot["store_id"]][:2],
                "confirmed_effect_count": 1,
                "overlapping_allocations": 0,
            },
        ))

    return cases


def build_dataset(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    world = _build_world(rng)
    cases = _build_cases(world, rng)
    dataset = {
        "dataset_version": "synthetic-business-v1",
        "data_origin": "generated_synthetic",
        "generator": "evals/generate_synthetic_business_data.py",
        "seed": seed,
        "reference_time": _iso(REFERENCE_TIME),
        "schedule_start_date": FIRST_SERVICE_DATE.isoformat(),
        "schedule_days": HORIZON_DAYS,
        "timezone": TIMEZONE,
        "assumptions": [
            "All stores, people, prices, shifts, histories and bookings are synthetic.",
            "This fixture is constraint-focused and is not sampled from production distributions.",
            (
                "Availability uses 30-minute start increments and service duration plus "
                "a 15-minute resource buffer."
            ),
            (
                "Appointment and operation effects in case expectations are evaluated "
                "in an isolated test database."
            ),
        ],
        "world": world,
        "cases": cases,
    }
    validate_dataset(dataset)
    return dataset


def validate_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    if dataset.get("data_origin") != "generated_synthetic":
        raise ValueError("data_origin must remain generated_synthetic")
    world = dataset["world"]
    stores = {item["store_id"]: item for item in world["stores"]}
    services = {item["service_id"]: item for item in world["services"]}
    therapists = {item["therapist_id"]: item for item in world["therapists"]}
    rooms = {item["room_id"]: item for item in world["rooms"]}
    customers = {item["customer_id"]: item for item in world["customers"]}
    bookings = world["existing_bookings"]
    if len(stores) != len(world["stores"]) or len(services) != len(world["services"]):
        raise ValueError("store and service IDs must be unique")
    if len(therapists) != len(world["therapists"]) or len(rooms) != len(world["rooms"]):
        raise ValueError("therapist and room IDs must be unique")
    if len(customers) != len(world["customers"]):
        raise ValueError("customer IDs must be unique")

    for store in world["stores"]:
        for service_id in store["service_ids"]:
            if service_id not in services:
                raise ValueError(f"unknown service {service_id} in {store['store_id']}")
    for therapist in world["therapists"]:
        if therapist["store_id"] not in stores:
            raise ValueError(f"unknown store for {therapist['therapist_id']}")
        for shift in therapist["shifts"]:
            start = datetime.fromisoformat(shift["start_at"])
            end = datetime.fromisoformat(shift["end_at"])
            store = stores[therapist["store_id"]]
            hours = _store_hours(store, start.date())
            if start >= end or hours is None or start < hours[0] or end > hours[1]:
                raise ValueError(f"shift outside business hours for {therapist['therapist_id']}")
            if any(
                _overlaps(
                    datetime.fromisoformat(left["start_at"]),
                    datetime.fromisoformat(left["end_at"]),
                    datetime.fromisoformat(right["start_at"]),
                    datetime.fromisoformat(right["end_at"]),
                )
                for index, left in enumerate(therapist["shifts"])
                for right in therapist["shifts"][index + 1:]
            ):
                raise ValueError(f"overlapping shifts for {therapist['therapist_id']}")
    for customer in world["customers"]:
        if customer["store_id"] not in stores:
            raise ValueError(f"unknown store for {customer['customer_id']}")
        for visit in customer["history"]:
            if visit["service_id"] not in stores[customer["store_id"]]["service_ids"]:
                raise ValueError(
                    "customer history service not offered at home store: "
                    f"{customer['customer_id']}"
                )
            therapist = therapists.get(visit["therapist_id"])
            if therapist is None or therapist["store_id"] != customer["store_id"]:
                raise ValueError(f"unknown therapist in history: {customer['customer_id']}")
            if services[visit["service_id"]]["required_skill"] not in therapist["skills"]:
                raise ValueError(f"unqualified therapist in history: {customer['customer_id']}")

    seen_bookings: set[str] = set()
    for booking in bookings:
        booking_id = booking["booking_id"]
        if booking_id in seen_bookings:
            raise ValueError(f"duplicate booking ID: {booking_id}")
        seen_bookings.add(booking_id)
        store = stores[booking["store_id"]]
        service = services[booking["service_id"]]
        therapist = therapists[booking["therapist_id"]]
        room = rooms[booking["room_id"]]
        if booking["service_id"] not in store["service_ids"]:
            raise ValueError(f"booking service unavailable at store: {booking_id}")
        if therapist["store_id"] != store["store_id"] or room["store_id"] != store["store_id"]:
            raise ValueError(f"booking resources belong to another store: {booking_id}")
        if service["required_skill"] not in therapist["skills"]:
            raise ValueError(f"therapist lacks required skill: {booking_id}")
        if room["room_type"] != service["room_type"]:
            raise ValueError(f"room type mismatch: {booking_id}")
        if booking["customer_id"] not in customers:
            raise ValueError(f"unknown customer: {booking_id}")
        if customers[booking["customer_id"]]["store_id"] != store["store_id"]:
            raise ValueError(f"booking customer belongs to another store: {booking_id}")
        start = datetime.fromisoformat(booking["start_at"])
        end = datetime.fromisoformat(booking["end_at"])
        occupied_end = datetime.fromisoformat(booking["occupancy_end_at"])
        expected_end = start + timedelta(minutes=service["duration_minutes"])
        expected_occupied_end = expected_end + timedelta(minutes=service["buffer_minutes"])
        if end != expected_end or occupied_end != expected_occupied_end:
            raise ValueError(f"booking duration mismatch: {booking_id}")
        if not _staff_is_free(
            therapist,
            start,
            occupied_end,
            exclude_booking_ids={booking_id},
            bookings=bookings,
        ):
            raise ValueError(f"booking outside therapist shift or overlaps a break: {booking_id}")
        if not _room_is_free(
            room,
            start,
            occupied_end,
            exclude_booking_ids={booking_id},
            bookings=bookings,
        ):
            raise ValueError(f"booking overlaps room occupancy: {booking_id}")
        if candidate_pairs_at(
            world,
            store_id=store["store_id"],
            service_id=service["service_id"],
            start_at=booking["start_at"],
            exclude_booking_ids={booking_id},
        ).count({"therapist_id": therapist["therapist_id"], "room_id": room["room_id"]}) != 1:
            raise ValueError(f"booking is not a valid candidate in its world: {booking_id}")

    category_counts = Counter(case["category"] for case in dataset["cases"])
    case_ids = [case["id"] for case in dataset["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("scenario IDs must be unique")
    if dict(category_counts) != EXPECTED_CASE_COUNTS:
        raise ValueError(f"unexpected scenario coverage: {dict(category_counts)}")
    for case in dataset["cases"]:
        if case["store_id"] not in stores or case["customer_id"] not in customers:
            raise ValueError(f"unknown store/customer in {case['id']}")
        if customers[case["customer_id"]]["store_id"] != case["store_id"]:
            raise ValueError(f"scenario customer belongs to another store: {case['id']}")
        if (
            case["service_id"] is not None
            and case["service_id"] not in stores[case["store_id"]]["service_ids"]
        ):
            raise ValueError(f"scenario service is unavailable at store: {case['id']}")
        expected = case["expected"]
        category = case["category"]
        if category in {
            "feasible_exact_slot",
            "outside_hours",
            "preferred_therapist_unavailable",
            "booking_conflict",
        }:
            actual_pairs = candidate_pairs_at(
                world,
                store_id=case["store_id"],
                service_id=case["service_id"],
                start_at=case["requested_start_at"],
            )
            if actual_pairs != expected["candidate_pairs"]:
                raise ValueError(f"candidate oracle mismatch in {case['id']}")
            if category == "feasible_exact_slot" and not actual_pairs:
                raise ValueError(f"expected feasible slot missing in {case['id']}")
            if category == "outside_hours" and actual_pairs:
                raise ValueError(f"outside-hours case unexpectedly feasible: {case['id']}")
            if category == "preferred_therapist_unavailable":
                available_ids = {item["therapist_id"] for item in actual_pairs}
                if expected["preferred_therapist_id"] in available_ids:
                    raise ValueError(f"preferred unavailable therapist was offered: {case['id']}")
            if category == "booking_conflict" and expected["blocked_pair"] in actual_pairs:
                raise ValueError(f"busy resource pair was offered: {case['id']}")
        if category == "missing_service" and expected["required_fields"] != ["service_id"]:
            raise ValueError(f"invalid missing-service oracle in {case['id']}")
        if category == "ambiguous_service" and len(expected["candidate_service_ids"]) < 2:
            raise ValueError(f"ambiguous-service case needs multiple choices: {case['id']}")
        if category in {"stale_candidate", "missing_service", "ambiguous_service", "outside_hours"}:
            if expected.get("appointment_count_delta") != 0:
                raise ValueError(f"non-booking case permits a write: {case['id']}")
        if category in {"feasible_exact_slot", "preferred_therapist_unavailable"}:
            if expected.get("appointment_count_delta_before_confirmation") != 0:
                raise ValueError(f"candidate-only case permits a write before confirmation: {case['id']}")
        if category == "concurrent_single_slot_race":
            if expected["confirmed_effect_count"] != 1 or expected["overlapping_allocations"] != 0:
                raise ValueError(f"invalid race oracle in {case['id']}")
            if expected["target_pair"] not in candidate_pairs_at(
                world,
                store_id=case["store_id"],
                service_id=case["service_id"],
                start_at=case["requested_start_at"],
            ):
                raise ValueError(f"race target is not available in {case['id']}")
        if category == "idempotent_confirmation":
            if not expected.get("idempotency_key") or len(expected.get("request_digest", "")) != 64:
                raise ValueError(f"idempotency case needs a key and SHA-256 digest: {case['id']}")

    return {
        "stores": len(stores),
        "services": len(services),
        "therapists": len(therapists),
        "rooms": len(rooms),
        "customers": len(customers),
        "existing_bookings": len(bookings),
        "scenarios": len(dataset["cases"]),
        "scenario_counts": dict(sorted(category_counts.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    dataset = build_dataset(args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = validate_dataset(dataset)
    print(json.dumps({"output": str(args.output), "seed": args.seed, **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
