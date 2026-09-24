"""Generation and internal consistency checks for the synthetic eval fixture."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evals.generate_synthetic_business_data import (
    DEFAULT_SEED,
    EXPECTED_CASE_COUNTS,
    build_dataset,
    validate_dataset,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evals" / "cases" / "synthetic_business_v1.json"


def test_synthetic_business_dataset_is_reproducible_and_matches_checked_in_fixture():
    first = build_dataset(DEFAULT_SEED)
    second = build_dataset(DEFAULT_SEED)
    saved = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    assert first == second == saved
    assert saved["data_origin"] == "generated_synthetic"


def test_dataset_covers_expected_business_and_failure_modes():
    dataset = build_dataset()
    summary = validate_dataset(dataset)

    assert summary["stores"] == 3
    assert summary["services"] == 5
    assert summary["therapists"] == 15
    assert summary["rooms"] == 9
    assert summary["customers"] == 30
    assert summary["existing_bookings"] >= 8
    assert summary["scenario_counts"] == EXPECTED_CASE_COUNTS
    assert summary["scenarios"] == sum(EXPECTED_CASE_COUNTS.values()) == 64


def test_validator_rejects_cross_store_scenario_customer():
    dataset = copy.deepcopy(build_dataset())
    dataset["cases"][0]["customer_id"] = next(
        item["customer_id"]
        for item in dataset["world"]["customers"]
        if item["store_id"] != dataset["cases"][0]["store_id"]
    )

    with pytest.raises(ValueError, match="belongs to another store"):
        validate_dataset(dataset)


def test_validator_rejects_fixture_that_is_not_marked_synthetic():
    dataset = copy.deepcopy(build_dataset())
    dataset["data_origin"] = "production"

    with pytest.raises(ValueError, match="generated_synthetic"):
        validate_dataset(dataset)
