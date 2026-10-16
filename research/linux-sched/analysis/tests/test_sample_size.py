"""Unit tests for the confirmatory sample-size freeze."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sample_size import main, plan, round_up_to_multiple  # noqa: E402
from sched_analysis import (  # noqa: E402
    CONTROL_SCHEDULER,
    MIN_BLOCKS_FOR_VERDICT,
    Observation,
    required_n_paired,
)


def _pilot(values: dict[str, dict[str, list[float]]]) -> list[Observation]:
    """values: workload -> scheduler -> per-block values."""

    rows: list[Observation] = []
    for workload, by_scheduler in values.items():
        for scheduler, per_block in by_scheduler.items():
            for index, value in enumerate(per_block, start=1):
                rows.append(
                    Observation(
                        block_id=f"b{index}",
                        workload=workload,
                        metric="throughput_rps",
                        scheduler=scheduler,
                        value=value,
                        direction="higher-is-better",
                    )
                )
    return rows


def test_round_up_to_multiple() -> None:
    assert round_up_to_multiple(5, 3) == 6
    assert round_up_to_multiple(6, 3) == 6
    assert round_up_to_multiple(7, 3) == 9


def test_plan_takes_the_larger_contrast_and_balances_to_three() -> None:
    record = plan(
        _pilot(
            {
                "http": {
                    CONTROL_SCHEDULER: [100.0, 101.0, 99.0],
                    "bpfland": [103.0, 104.0, 102.0],
                    "lavd": [101.0, 102.5, 100.5],
                }
            }
        )
    )
    scenario = record["scenarios"][0]
    assert scenario["scenario"] == "http"
    # The frozen count is at least the floor and always a multiple of three.
    assert scenario["confirmatory_blocks"] >= MIN_BLOCKS_FOR_VERDICT
    assert scenario["confirmatory_blocks"] % record["balance_multiple"] == 0
    # It is the maximum across the two contrasts, floored, then rounded.
    assert scenario["required_n_raw"] == max(
        c["required_n"] for c in scenario["contrasts"]
    )
    assert scenario["confirmatory_blocks"] >= scenario["required_n_raw"]
    # The calculation is sized for the corrected family, not a weaker test.
    assert record["primary_contrasts"] == 10
    assert record["alpha_per_contrast"] == pytest.approx(0.05 / 10)


def test_plan_uses_the_most_variable_contrast() -> None:
    # `bpfland` is far noisier, so it must drive the count.
    record = plan(
        _pilot(
            {
                "postgres": {
                    CONTROL_SCHEDULER: [100.0, 100.5, 99.5],
                    "bpfland": [100.0, 130.0, 70.0],
                    "lavd": [100.2, 100.3, 99.9],
                }
            }
        )
    )
    scenario = record["scenarios"][0]
    required = {c["scheduler"]: c["required_n"] for c in scenario["contrasts"]}
    assert required["bpfland"] > required["lavd"]
    assert scenario["required_n_raw"] == required["bpfland"]


def test_plan_matches_the_frozen_power_calculation() -> None:
    record = plan(
        _pilot(
            {
                "http": {
                    CONTROL_SCHEDULER: [100.0, 101.0, 99.0],
                    "bpfland": [103.0, 104.0, 102.0],
                }
            }
        )
    )
    contrast = record["scenarios"][0]["contrasts"][0]
    expected = required_n_paired(
        contrast["sd_log_ratio"],
        practical_effect=record["practical_effect"],
        alpha=record["alpha_per_contrast"],
        power=record["power"],
    )
    assert contrast["required_n"] == expected


def test_plan_rejects_a_pilot_without_the_control_arm() -> None:
    with pytest.raises(ValueError, match="no control arm"):
        plan(_pilot({"http": {"bpfland": [100.0, 101.0, 102.0]}}))


def test_plan_rejects_a_pilot_with_too_few_paired_blocks() -> None:
    with pytest.raises(ValueError, match="paired blocks"):
        plan(
            _pilot(
                {
                    "http": {
                        CONTROL_SCHEDULER: [100.0, 101.0],
                        "bpfland": [101.0, 102.0],
                    }
                }
            )
        )


def test_main_writes_a_versioned_record(tmp_path: Path) -> None:
    blocks = tmp_path / "blocks.csv"
    with blocks.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["block_id", "scheduler", "workload", "metric", "value", "direction"]
        )
        for index in (1, 2, 3):
            writer.writerow(
                [f"b{index}", CONTROL_SCHEDULER, "http", "m", 100.0, "higher-is-better"]
            )
            writer.writerow(
                [f"b{index}", "bpfland", "http", "m", 103.0, "higher-is-better"]
            )

    out = tmp_path / "sample-size.json"
    assert main(["--pilot-blocks", str(blocks), "--output", str(out)]) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["schema_version"] == 1
    assert record["scenarios"][0]["confirmatory_blocks"] % 3 == 0
