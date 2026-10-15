"""Unit tests for the linux-sched paired analysis core."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sched_analysis import (  # noqa: E402
    CONTROL_SCHEDULER,
    MIN_BLOCKS_FOR_VERDICT,
    Observation,
    _decision,
    _t_ppf,
    analyse,
    read_blocks,
    required_n_paired,
    summary_csv,
    summary_json,
)


def obs(
    block: str,
    scheduler: str,
    value: float,
    metric: str = "throughput_rps",
    workload: str = "http",
    direction: str | None = None,
) -> Observation:
    return Observation(
        block_id=block,
        workload=workload,
        metric=metric,
        scheduler=scheduler,
        value=value,
        direction=direction,
    )


def test_t_ppf_matches_normal_for_large_df() -> None:
    assert _t_ppf(0.975, 100000) == pytest.approx(1.959964, abs=1e-3)


def test_t_ppf_grows_as_df_shrinks() -> None:
    assert _t_ppf(0.975, 2) > _t_ppf(0.975, 30) > _t_ppf(0.975, 1000)


def test_t_ppf_rejects_bad_probability() -> None:
    with pytest.raises(ValueError):
        _t_ppf(0.0, 10)


def test_t_ppf_rejects_unsupported_df() -> None:
    # The Cornish-Fisher approximation is only claimed for df >= 2.
    with pytest.raises(ValueError):
        _t_ppf(0.975, 1)


def test_required_n_has_frozen_floor() -> None:
    assert required_n_paired(0.0) == MIN_BLOCKS_FOR_VERDICT
    assert required_n_paired(0.0001) == MIN_BLOCKS_FOR_VERDICT


def test_required_n_grows_with_variance() -> None:
    small = required_n_paired(0.02)
    large = required_n_paired(0.20)
    assert large > small >= MIN_BLOCKS_FOR_VERDICT


def test_decision_uses_log_scale() -> None:
    band = math.log(1.05)
    # Log-scale interval fully above zero and a >=5% point effect.
    assert _decision(0.01, 0.10, band) == "improvement"
    # Fully below zero with a <=-5% point effect.
    assert _decision(-0.10, -0.01, -band) == "regression"
    # Whole interval inside the equivalence band.
    assert _decision(-0.01, 0.01, 0.0) == "equivalent"
    # Interval straddles no-effect and exceeds the band.
    assert _decision(-0.20, 0.20, 0.0) == "inconclusive"


def test_decision_improvement_requires_interval_above_zero() -> None:
    # Point effect is >= 5% but the interval crosses no-effect: must NOT be an
    # improvement. This is the regression test for the ratio/log unit mismatch.
    band = math.log(1.05)
    assert _decision(-0.01, 2 * band, band) == "inconclusive"


def test_higher_is_better_orientation() -> None:
    observations = [
        obs("b1", CONTROL_SCHEDULER, 100.0),
        obs("b1", "bpfland", 110.0),
        obs("b2", CONTROL_SCHEDULER, 100.0),
        obs("b2", "bpfland", 110.0),
        obs("b3", CONTROL_SCHEDULER, 100.0),
        obs("b3", "bpfland", 110.0),
    ]
    verdict = analyse(observations).verdicts[0]
    assert verdict.scheduler == "bpfland"
    assert verdict.geometric_mean_ratio == pytest.approx(1.10, abs=1e-6)
    assert verdict.decision == "improvement"


def test_lower_is_better_metric_is_inverted() -> None:
    observations = [
        obs(f"b{i}", CONTROL_SCHEDULER, 100.0, metric="latency_p99_ms")
        for i in range(3)
    ] + [obs(f"b{i}", "lavd", 90.0, metric="latency_p99_ms") for i in range(3)]
    verdict = analyse(observations).verdicts[0]
    # A 10% latency reduction must read as a geometric-mean improvement > 1.
    assert verdict.geometric_mean_ratio == pytest.approx(1.0 / 0.9, abs=1e-6)
    assert verdict.decision == "improvement"


def test_explicit_direction_column_overrides_metric_name() -> None:
    # An unknown metric name defaulting to higher-is-better would read this as
    # a regression; the explicit direction must win.
    observations = [
        obs(
            f"b{i}",
            CONTROL_SCHEDULER,
            100.0,
            metric="custom_score",
            direction="lower-is-better",
        )
        for i in range(3)
    ] + [
        obs(f"b{i}", "mavd", 90.0, metric="custom_score", direction="lower-is-better")
        for i in range(3)
    ]
    verdict = analyse(observations).verdicts[0]
    assert verdict.decision == "improvement"


def test_regression_is_detected() -> None:
    observations = [obs(f"b{i}", CONTROL_SCHEDULER, 100.0) for i in range(3)] + [
        obs(f"b{i}", "mavd", 80.0) for i in range(3)
    ]
    verdict = analyse(observations).verdicts[0]
    assert verdict.decision == "regression"


def test_analysis_is_byte_identical_across_runs() -> None:
    observations = [obs(f"b{i}", CONTROL_SCHEDULER, 100.0 + i) for i in range(6)] + [
        obs(f"b{i}", "mavd", 105.0 + i) for i in range(6)
    ]
    first = summary_csv(analyse(observations))
    second = summary_csv(analyse(observations))
    assert first == second
    assert summary_json(analyse(observations)) == summary_json(analyse(observations))


def test_duplicate_observations_are_rejected() -> None:
    observations = [
        obs("b1", CONTROL_SCHEDULER, 100.0),
        obs("b1", CONTROL_SCHEDULER, 101.0),
        obs("b1", "mavd", 100.0),
        obs("b2", "mavd", 100.0),
        obs("b3", "mavd", 100.0),
    ]
    with pytest.raises(ValueError, match="duplicate observation"):
        analyse(observations)


def test_missing_control_is_skipped_not_silent() -> None:
    observations = [
        obs("b1", "bpfland", 100.0),
        obs("b2", "bpfland", 100.0),
        obs("b3", "bpfland", 100.0),
    ]
    result = analyse(observations)
    assert result.verdicts == []
    assert result.skipped and result.skipped[0]["reason"] == "no control arm"


def test_too_few_blocks_is_skipped() -> None:
    observations = [
        obs("b1", CONTROL_SCHEDULER, 100.0),
        obs("b2", CONTROL_SCHEDULER, 100.0),
        obs("b1", "lavd", 90.0),
        obs("b2", "lavd", 90.0),
    ]
    result = analyse(observations)
    assert result.verdicts == []
    assert result.skipped[0]["shared_blocks"] == 2


def test_non_positive_values_rejected() -> None:
    observations = [
        obs("b1", CONTROL_SCHEDULER, 0.0),
        obs("b1", "mavd", 1.0),
        obs("b2", CONTROL_SCHEDULER, 1.0),
        obs("b2", "mavd", 1.0),
        obs("b3", CONTROL_SCHEDULER, 1.0),
        obs("b3", "mavd", 1.0),
    ]
    with pytest.raises(ValueError, match="strictly positive"):
        analyse(observations)


def test_read_blocks_requires_columns(tmp_path: Path) -> None:
    path = tmp_path / "blocks.csv"
    path.write_text("block_id,scheduler,value\nb1,eevdf,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required column"):
        read_blocks(path)


def test_read_blocks_rejects_bad_direction(tmp_path: Path) -> None:
    path = tmp_path / "blocks.csv"
    path.write_text(
        "block_id,scheduler,workload,metric,value,direction\n"
        "b1,eevdf,http,m,1,sideways\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid direction"):
        read_blocks(path)


def test_read_blocks_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "blocks.csv"
    path.write_text(
        "block_id,scheduler,workload,metric,value,direction\n"
        "b1,eevdf,http,throughput_rps,100,higher-is-better\n"
        "b1,bpfland,http,throughput_rps,101,higher-is-better\n",
        encoding="utf-8",
    )
    rows = read_blocks(path)
    assert [r.scheduler for r in rows] == ["eevdf", "bpfland"]
    assert rows[0].direction == "higher-is-better"
