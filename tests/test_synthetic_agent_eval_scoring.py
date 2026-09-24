"""The Agent evaluator must not confuse a successful empty search with a failure."""

from evals.run_synthetic_business_agent_eval import _count_assertions


def _status(assertions: list[dict], name: str) -> str:
    return next(item["status"] for item in assertions if item["name"] == name)


def test_conflict_case_with_no_expected_alternative_accepts_empty_successful_search():
    assertions = _count_assertions(
        category="booking_conflict",
        last_turn={"tool_calls": [{"tool": "search_availability", "status": "OK", "data": {"candidates": []}}]},
        count_delta={"holds": 0, "appointments": 0},
        expected={"blocked_pair": {"therapist_id": "TECH-A-01", "room_id": "ROOM-A-01"}, "candidate_pairs": []},
    )

    assert all(item["status"] == "pass" for item in assertions)


def test_conflict_case_still_requires_expected_alternative_when_fixture_has_one():
    assertions = _count_assertions(
        category="booking_conflict",
        last_turn={"tool_calls": [{"tool": "search_availability", "status": "OK", "data": {"candidates": []}}]},
        count_delta={"holds": 0, "appointments": 0},
        expected={
            "blocked_pair": {"therapist_id": "TECH-A-01", "room_id": "ROOM-A-01"},
            "candidate_pairs": [{"therapist_id": "TECH-A-02", "room_id": "ROOM-A-02"}],
        },
    )

    assert _status(assertions, "availability_tool_succeeded") == "pass"
    assert _status(assertions, "candidate_set_matches_conflict_oracle") == "fail"


def test_transaction_only_scenarios_are_not_scored_from_a_first_agent_turn():
    for category in ("stale_candidate", "idempotent_confirmation", "concurrent_single_slot_race"):
        assertions = _count_assertions(
            category=category,
            last_turn={"tool_calls": []},
            count_delta={"holds": 0, "appointments": 0},
            expected={},
        )
        assert assertions == []


def test_feasible_time_comparison_normalizes_iso_seconds():
    assertions = _count_assertions(
        category="feasible_exact_slot",
        last_turn={
            "tool_calls": [{
                "tool": "search_availability",
                "status": "OK",
                "data": {"candidates": [{
                    "start_at": "2026-10-03T16:30:00+08:00",
                    "resources": [
                        {"unit_code": "TECH-C-04"},
                        {"unit_code": "ROOM-C-01"},
                    ],
                }]},
            }],
        },
        count_delta={"holds": 0, "appointments": 0},
        expected={"candidate_pairs": [{"therapist_id": "TECH-C-04", "room_id": "ROOM-C-01"}]},
        requested_start_at="2026-10-03T16:30+08:00",
    )

    assert _status(assertions, "expected_feasible_pair_at_requested_time") == "pass"


def test_clarification_rescore_accepts_saved_task_state_field():
    assertions = _count_assertions(
        category="ambiguous_service",
        last_turn={"state": "WAITING_USER", "clarification_question": "您想选哪个项目？"},
        count_delta={"holds": 0, "appointments": 0},
        expected={},
    )

    assert _status(assertions, "clarification_before_booking") == "pass"
