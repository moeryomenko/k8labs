"""Tests for conservative CPU-limits confirmatory sample sizing."""

from __future__ import annotations

from analysis.sample_size import estimate_sample_sizes


def test_sample_size_caps_at_six_and_marks_result_underpowered() -> None:
    pilot_rows = [
        {
            "block_id": str(block),
            "workload": "cpu-bound",
            "policy": "fit",
            "condition": "solo",
            "throughput": 100.0,
        }
        for block in range(1, 4)
    ] + [
        {
            "block_id": str(block),
            "workload": "cpu-bound",
            "policy": "burstable-low",
            "condition": "solo",
            "throughput": value,
        }
        for block, value in enumerate((30.0, 300.0, 30.0), start=1)
    ]

    result = estimate_sample_sizes(pilot_rows)

    entry = next(
        item for item in result["scenarios"] if item["family"] == "limit_effect"
    )
    assert entry["required_blocks"] > 6
    assert entry["confirmatory_blocks"] == 6
    assert entry["underpowered"] is True
    assert entry["power"] == 0.90
    assert entry["sd_upper_confidence"] == 0.95


def test_missing_pilot_contrast_is_recorded_without_a_count() -> None:
    result = estimate_sample_sizes(
        [
            {
                "block_id": "1",
                "workload": "cpu-bound",
                "policy": "fit",
                "condition": "solo",
                "throughput": 100.0,
            }
        ]
    )

    entry = next(
        item for item in result["scenarios"] if item["family"] == "limit_effect"
    )
    assert entry["status"] == "incomplete"
    assert entry["confirmatory_blocks"] is None
