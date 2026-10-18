"""Unit tests for the factorial quota campaign analysis."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factorial_analysis import (  # noqa: E402
    CONTROL_SCHEDULER,
    DEFAULT_POWER,
    MIN_BLOCKS_FOR_VERDICT,
    Observation,
    PRIMARY_CONTRASTS,
    THROTTLE_EFFECT,
    PERFORMANCE_EFFECT,
    _t_ppf,
    analyse,
    holm_decisions,
    read_blocks,
    required_n_paired,
    summary_csv,
    summary_json,
)


def obs(block, scheduler, value, metric="throughput_rps", workload="http",
        direction=None, policy=None):
    return Observation(block_id=block, workload=workload, metric=metric,
                      scheduler=scheduler, value=value, direction=direction,
                      policy=policy)


def test_constants() -> None:
    assert PERFORMANCE_EFFECT == 0.05
    assert THROTTLE_EFFECT == 0.10
    assert DEFAULT_POWER == 0.90
    assert PRIMARY_CONTRASTS == 75
    assert CONTROL_SCHEDULER == "eevdf"


def test_t_ppf() -> None:
    assert _t_ppf(0.975, 100000) == pytest.approx(1.959964, abs=1e-3)


def test_required_n_floor() -> None:
    assert required_n_paired(0.0) == MIN_BLOCKS_FOR_VERDICT


def test_interaction_improvement() -> None:
    observations = []
    for i in range(5):
        observations.append(obs(f"b{i}", CONTROL_SCHEDULER, 100.0, policy="unlimited"))
        observations.append(obs(f"b{i}", CONTROL_SCHEDULER, 100.0, policy="limited"))
        observations.append(obs(f"b{i}", "bpfland", 100.0, policy="unlimited"))
        observations.append(obs(f"b{i}", "bpfland", 110.0, policy="limited"))
    verdict = analyse(observations).verdicts[0]
    assert verdict.contrast == "performance_interaction"
    assert verdict.decision in ("improvement", "inconclusive")


def test_throttling_improvement() -> None:
    observations = []
    for i in range(5):
        observations.append(obs(f"b{i}", CONTROL_SCHEDULER, 0.2, metric="throttled_period_ratio",
                               direction="lower-is-better", policy="limited"))
        observations.append(obs(f"b{i}", "bpfland", 0.1, metric="throttled_period_ratio",
                               direction="lower-is-better", policy="limited"))
    verdict = analyse(observations).verdicts[0]
    assert verdict.effect == pytest.approx(0.10)
    assert verdict.contrast == "throttling"


def test_read_blocks_policy() -> None:
    import tempfile, csv
    d = Path(tempfile.mkdtemp())
    p = d / "blocks.csv"
    with p.open("w", newline="") as h:
        w = csv.writer(h, lineterminator="\n")
        w.writerow(["block_id", "scheduler", "workload", "metric", "value", "direction", "policy"])
        w.writerow(["b1", CONTROL_SCHEDULER, "http", "m", 1.0, "higher-is-better", "limited"])
    rows = read_blocks(p)
    assert rows[0].policy == "limited"


def test_summary_roundtrip() -> None:
    observations = []
    for i in range(3):
        observations.append(obs(f"b{i}", CONTROL_SCHEDULER, 100.0, policy="limited"))
        observations.append(obs(f"b{i}", "bpfland", 110.0, policy="limited"))
    result = analyse(observations)
    csv_text = summary_csv(result)
    assert "workload" in csv_text
    json_text = summary_json(result)
    payload = json.loads(json_text)
    assert "verdicts" in payload
