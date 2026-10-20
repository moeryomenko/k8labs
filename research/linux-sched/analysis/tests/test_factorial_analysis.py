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
    assert PRIMARY_CONTRASTS == 55
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
        observations.append(obs(f"b{i}", "scx_mavd", 100.0, policy="unlimited"))
        observations.append(obs(f"b{i}", "scx_mavd", 110.0, policy="limited"))
    verdicts = analyse(observations).verdicts
    interaction = [v for v in verdicts if v.contrast == "performance_interaction"]
    assert len(interaction) == 1
    assert interaction[0].scheduler == "scx_mavd"
    assert interaction[0].family == "quota_interaction"
    assert interaction[0].decision in ("improvement", "inconclusive")


def test_throttling_improvement() -> None:
    observations = []
    for i in range(5):
        observations.append(obs(f"b{i}", CONTROL_SCHEDULER, 0.2, metric="throttled_period_ratio",
                               direction="lower-is-better", policy="limited"))
        observations.append(obs(f"b{i}", "scx_mavd", 0.1, metric="throttled_period_ratio",
                               direction="lower-is-better", policy="limited"))
    verdict = analyse(observations).verdicts[0]
    assert verdict.effect == pytest.approx(0.10)
    assert verdict.contrast == "throttling"
    assert verdict.family == "throttling"


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


def _both_policies(arm, limited_value, unlimited_value=100.0, blocks=5):
    observations = []
    for i in range(blocks):
        observations.append(obs(f"b{i}", CONTROL_SCHEDULER, 100.0, policy="unlimited"))
        observations.append(obs(f"b{i}", CONTROL_SCHEDULER, 100.0, policy="limited"))
        observations.append(obs(f"b{i}", arm, unlimited_value, policy="unlimited"))
        observations.append(obs(f"b{i}", arm, limited_value, policy="limited"))
    return observations


def test_non_quota_arm_gets_only_the_unlimited_family() -> None:
    # bpfland cannot honour cpu.max, so the limited policy must not produce an
    # interaction or a throttling verdict for it.
    verdicts = analyse(_both_policies("scx_bpfland", 80.0)).verdicts
    assert [(v.contrast, v.family) for v in verdicts] == [
        ("performance", "scheduler_unlimited")
    ]


def test_non_quota_arm_throttling_is_skipped_with_a_reason() -> None:
    observations = []
    for i in range(5):
        observations.append(obs(f"b{i}", CONTROL_SCHEDULER, 0.2,
                               metric="throttled_period_ratio",
                               direction="lower-is-better", policy="limited"))
        observations.append(obs(f"b{i}", "scx_mlfq", 0.1,
                               metric="throttled_period_ratio",
                               direction="lower-is-better", policy="limited"))
    result = analyse(observations)
    assert result.verdicts == []
    assert any("cannot honour cpu.max" in s["reason"] for s in result.skipped)


def test_quota_arm_gets_unlimited_and_interaction_families() -> None:
    verdicts = analyse(_both_policies("scx_lavd", 80.0)).verdicts
    contrasts = {v.contrast: v.family for v in verdicts}
    assert contrasts == {
        "performance": "scheduler_unlimited",
        "performance_interaction": "quota_interaction",
    }


def test_summary_declares_three_families() -> None:
    payload = json.loads(summary_json(analyse(_both_policies("scx_mavd", 80.0))))
    assert payload["families"] == {
        "scheduler_unlimited": 25,
        "quota_interaction": 10,
        "throttling": 20,
    }
    assert payload["primary_contrasts"] == 55
    assert payload["quota_capable_schedulers"] == ["scx_lavd", "scx_mavd"]
    assert {v["family"] for v in payload["verdicts"]} == {
        "scheduler_unlimited",
        "quota_interaction",
    }


def test_holm_is_applied_per_family() -> None:
    # A clean interaction on a quota arm must not be made harder to reject by a
    # much larger scheduler family in the same run.
    observations = _both_policies("scx_mavd", 80.0)
    for i in range(5):
        observations.append(obs(f"b{i}", CONTROL_SCHEDULER, 100.0))
        observations.append(obs(f"b{i}", "scx_bpfland", 100.0))
    decisions = holm_decisions([
        {"df": 4, "t_two": 10.0, "t_lower": 10.0, "t_upper": -10.0,
         "mean": math.log(1.1), "effect": 0.05, "family": "quota_interaction"},
        {"df": 4, "t_two": 0.1, "t_lower": 0.1, "t_upper": -0.1,
         "mean": 0.0, "effect": 0.05, "family": "scheduler_unlimited"},
    ])
    assert decisions[0] == "improvement"
