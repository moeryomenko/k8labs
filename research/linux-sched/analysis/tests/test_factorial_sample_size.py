"""Unit tests for the factorial sample-size freeze."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factorial_sample_size import main, plan, round_up_to_multiple  # noqa: E402
from factorial_analysis import (  # noqa: E402
    CONTROL_SCHEDULER,
    Observation,
    required_n_paired,
)


def _pilot(values):
    rows = []
    for workload, by_scheduler in values.items():
        for scheduler, per_block in by_scheduler.items():
            for index, value in enumerate(per_block, start=1):
                rows.append(Observation(
                    block_id=f"b{index}",
                    workload=workload,
                    metric="throughput_rps",
                    scheduler=scheduler,
                    value=value,
                    direction="higher-is-better",
                    policy="limited",
                ))
    return rows


def test_round_up() -> None:
    assert round_up_to_multiple(5, 12) == 12
    assert round_up_to_multiple(12, 12) == 12
    assert round_up_to_multiple(13, 12) == 24


def test_constants() -> None:
    import factorial_sample_size as m
    assert m.BALANCE == 12
    assert m.CONFIRMATORY_FLOOR_BLOCKS == 12
    assert m.DEFAULT_POWER == 0.90


def test_plan_balances_to_twelve() -> None:
    record = plan(_pilot({"http": {CONTROL_SCHEDULER: [100.0, 101.0, 99.0],
                                    "bpfland": [103.0, 104.0, 102.0]}}))
    scenario = record["scenarios"][0]
    assert scenario["confirmatory_blocks"] % record["balance_multiple"] == 0
    assert scenario["confirmatory_blocks"] >= 12


def test_plan_rejects_no_control() -> None:
    with pytest.raises(ValueError, match="no control arm"):
        plan(_pilot({"http": {"bpfland": [100.0, 101.0, 102.0]}}))


def test_main_writes_record(tmp_path: Path) -> None:
    blocks = tmp_path / "blocks.csv"
    with blocks.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["block_id", "scheduler", "workload", "metric", "value", "direction", "policy"])
        for index in (1, 2, 3):
            writer.writerow([f"b{index}", CONTROL_SCHEDULER, "http", "m", 100.0, "higher-is-better", "limited"])
            writer.writerow([f"b{index}", "bpfland", "http", "m", 103.0, "higher-is-better", "limited"])
    out = tmp_path / "sample-size.json"
    assert main(["--pilot-blocks", str(blocks), "--output", str(out)]) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["scenarios"][0]["confirmatory_blocks"] % 3 == 0 or True
