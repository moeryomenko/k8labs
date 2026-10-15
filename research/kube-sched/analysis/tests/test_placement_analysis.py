"""Unit tests for the kube-sched placement analysis."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from placement_analysis import (  # noqa: E402
    BlockMetric,
    MIN_BLOCKS_FOR_VERDICT,
    PodRow,
    _decision,
    block_metrics,
    compare,
    percentile,
    read_pods,
)


def pod(
    block: str,
    profile: str,
    latency: float,
    *,
    correct: bool = True,
    unschedulable: bool = False,
) -> PodRow:
    return PodRow(
        block_id=block,
        profile=profile,
        pod=f"{block}-{profile}",
        latency_ms=latency,
        unschedulable=unschedulable,
        correct=correct,
    )


def metric_block(block: str, profile: str, metric: str, value: float) -> BlockMetric:
    return BlockMetric(block, profile, metric, value)


def test_percentile_endpoints() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 100.0) == 4.0
    assert percentile(values, 50.0) == pytest.approx(2.5)


def test_percentile_single_value() -> None:
    assert percentile([7.0], 99.0) == 7.0


def test_percentile_empty_raises() -> None:
    with pytest.raises(ValueError):
        percentile([], 50.0)


def test_decision_uses_log_scale() -> None:
    band = math.log(1.05)
    assert _decision(0.01, 0.10, band) == "improvement"
    assert _decision(-0.10, -0.01, -band) == "regression"
    assert _decision(-0.01, 0.01, 0.0) == "equivalent"
    assert _decision(-0.20, 0.20, 0.0) == "inconclusive"
    # Point effect >= 5% but interval crossing no-effect is not an improvement.
    assert _decision(-0.01, 2 * band, band) == "inconclusive"


def test_block_metrics_p99_and_correct_rate() -> None:
    rows = [
        pod("b1", "least-allocated", 10.0),
        pod("b1", "least-allocated", 20.0),
        pod("b1", "least-allocated", 30.0, correct=False),
    ]
    metrics = {m.metric: m.value for m in block_metrics(rows)}
    assert metrics["submit_to_scheduled_p99_ms"] == pytest.approx(29.8)
    assert metrics["correct_placement_rate"] == pytest.approx(2 / 3)


def test_unschedulable_pods_excluded_from_latency() -> None:
    rows = [
        pod("b1", "least-allocated", 10.0),
        pod("b1", "least-allocated", 9999.0, correct=True, unschedulable=True),
    ]
    metrics = {m.metric: m.value for m in block_metrics(rows)}
    assert metrics["submit_to_scheduled_p99_ms"] == pytest.approx(10.0)


def test_compare_reports_latency_improvement() -> None:
    metrics = []
    for block in ("b1", "b2", "b3", "b4", "b5"):
        metrics.append(
            metric_block(block, "least-allocated", "submit_to_scheduled_p99_ms", 100.0)
        )
        metrics.append(
            metric_block(block, "most-allocated", "submit_to_scheduled_p99_ms", 80.0)
        )
    result = compare(metrics)
    assert len(result.comparisons) == 1
    assert result.comparisons[0]["profile"] == "most-allocated"
    assert result.comparisons[0]["geometric_mean_ratio"] == pytest.approx(
        1.25, abs=1e-6
    )
    assert result.comparisons[0]["decision"] == "improvement"


def test_compare_reports_regression() -> None:
    metrics = []
    for block in ("b1", "b2", "b3"):
        metrics.append(
            metric_block(block, "least-allocated", "submit_to_scheduled_p99_ms", 100.0)
        )
        metrics.append(
            metric_block(block, "most-allocated", "submit_to_scheduled_p99_ms", 130.0)
        )
    result = compare(metrics)
    assert result.comparisons[0]["decision"] == "regression"


def test_compare_without_control_returns_empty_and_records_skip() -> None:
    metrics = [metric_block("b1", "most-allocated", "submit_to_scheduled_p99_ms", 1.0)]
    result = compare(metrics)
    assert result.comparisons == []
    assert result.skipped[0]["reason"] == "no control profile"


def test_compare_too_few_blocks_is_skipped() -> None:
    metrics = []
    for block in ("b1", "b2"):
        metrics.append(
            metric_block(block, "least-allocated", "submit_to_scheduled_p99_ms", 100.0)
        )
        metrics.append(
            metric_block(block, "most-allocated", "submit_to_scheduled_p99_ms", 80.0)
        )
    result = compare(metrics)
    assert result.comparisons == []
    assert result.skipped[0]["shared_blocks"] == 2
    assert MIN_BLOCKS_FOR_VERDICT == 3


def test_compare_rejects_duplicate_blocks() -> None:
    metrics = [
        metric_block("b1", "least-allocated", "submit_to_scheduled_p99_ms", 100.0),
        metric_block("b1", "least-allocated", "submit_to_scheduled_p99_ms", 101.0),
    ]
    with pytest.raises(ValueError, match="duplicate placement metric"):
        compare(metrics)


def test_read_pods_requires_columns(tmp_path: Path) -> None:
    path = tmp_path / "pods.csv"
    path.write_text("block_id,profile,pod\nb1,x,p\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required column"):
        read_pods(path)


def test_read_pods_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "pods.csv"
    path.write_text(
        "block_id,profile,pod,latency_ms,unschedulable,correct\n"
        "b1,least-allocated,p1,12.5,0,1\n",
        encoding="utf-8",
    )
    rows = read_pods(path)
    assert len(rows) == 1
    assert rows[0].correct is True and rows[0].unschedulable is False
