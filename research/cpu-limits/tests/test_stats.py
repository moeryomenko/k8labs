"""Tests for deterministic paired CPU-limits statistics."""

from __future__ import annotations

import math

import pytest
from analysis.stats import (
    block_bootstrap_interval,
    geometric_mean,
    geometric_mean_from_logs,
    holm_decisions,
    holm_step_down,
    paired_log_ratios,
    paired_t_interval,
)


def test_paired_log_ratios_and_geometric_means() -> None:
    paired = paired_log_ratios(
        {"block-1": 100.0, "block-2": 200.0},
        {"block-1": 110.0, "block-2": 220.0},
    )

    assert paired.complete
    assert paired.missing_blocks == ()
    assert paired.log_ratios == pytest.approx((math.log(1.1), math.log(1.1)))
    assert geometric_mean((1.1, 1.1)) == pytest.approx(1.1)
    assert geometric_mean_from_logs(paired.log_ratios) == pytest.approx(1.1)


def test_paired_t_interval_uses_paired_log_scale() -> None:
    logs = tuple(math.log(value) for value in (1.05, 1.10, 1.15, 1.20, 1.25))

    interval = paired_t_interval(logs)

    assert interval.estimate == pytest.approx((1.05 * 1.10 * 1.15 * 1.20 * 1.25) ** (1 / 5))
    assert 1.0 < interval.low < interval.estimate < interval.high
    assert interval.n == 5


def test_block_bootstrap_is_deterministic() -> None:
    logs = tuple(math.log(value) for value in (0.8, 0.9, 1.1, 1.2))

    first = block_bootstrap_interval(logs, resamples=500, seed=91)
    second = block_bootstrap_interval(logs, resamples=500, seed=91)

    assert first == second
    assert first.low <= first.estimate <= first.high


def test_holm_orders_p_values_and_stops_after_first_retention() -> None:
    decisions = holm_decisions((0.001, 0.018, 0.04), planned_family_size=4)

    assert [decision.rejected for decision in decisions] == [True, False, False]
    assert [decision.threshold for decision in decisions] == pytest.approx((0.0125, 1 / 60, 0.025))
    assert holm_step_down((0.001, 0.018, 0.04), planned_family_size=4) == (True, False, False)


def test_holm_uses_planned_family_size_not_observed_size() -> None:
    # A single observed p-value would pass at 0.05. It must fail at the
    # planned-family threshold of 0.0125.
    assert holm_step_down((0.02,), planned_family_size=4) == (False,)


def test_missing_pair_is_incomplete_not_zero() -> None:
    paired = paired_log_ratios(
        {"block-1": 100.0, "block-2": 100.0},
        {"block-1": 120.0, "block-2": None},
    )

    assert not paired.complete
    assert paired.missing_blocks == ("block-2",)
    assert paired.log_ratios == pytest.approx((math.log(1.2),))


def test_non_positive_measurement_is_rejected() -> None:
    with pytest.raises(ValueError, match="strictly positive"):
        paired_log_ratios({"block-1": 1.0}, {"block-1": 0.0})
