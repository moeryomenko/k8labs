"""Tests for bounded monotonic response fitting."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

from monotonic import (  # noqa: E402
    Observation,
    fit_response_table,
    fit_monotonic,
)


def test_fit_is_non_decreasing_and_has_confidence_bounds() -> None:
    result = fit_monotonic(
        [
            Observation(100, 120.0),
            Observation(200, 100.0),
            Observation(300, 90.0),
            Observation(400, 80.0),
        ],
        direction="lower",
        confidence=0.95,
    )

    assert result.x_min == 100
    assert result.x_max == 400
    assert result.values == (120.0, 100.0, 90.0, 80.0)
    assert all(a >= b for a, b in zip(result.upper, result.upper[1:]))
    assert all(a >= b for a, b in zip(result.lower, result.lower[1:]))
    assert all(
        low <= value <= high
        for low, value, high in zip(result.lower, result.values, result.upper)
    )


def test_fit_clamps_noise_to_monotonic_lower_better_curve() -> None:
    result = fit_monotonic(
        [Observation(100, 10), Observation(200, 15), Observation(300, 12)],
        direction="lower",
    )

    assert result.values == (12.5, 12.5, 12.0)


def test_fit_rejects_extrapolation_and_bad_inputs() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        fit_monotonic([Observation(100, 1), Observation(100, 2)], direction="lower")
    result = fit_monotonic(
        [Observation(100, 1), Observation(200, 2)], direction="higher"
    )
    with pytest.raises(ValueError, match="outside"):
        result.predict(99)


def test_response_table_excludes_invalid_and_dependency_bound_cells() -> None:
    rows = (
        [
            {"cpu_limit_millicores": 100, "p99_latency_ms": value, "valid": True}
            for value in (198, 200, 202)
        ]
        + [
            {"cpu_limit_millicores": 200, "p99_latency_ms": value, "valid": True}
            for value in (119, 120, 121)
        ]
        + [
            {"cpu_limit_millicores": 300, "p99_latency_ms": value, "valid": True}
            for value in (99, 100, 101)
        ]
        + [
            {"cpu_limit_millicores": 400, "p99_latency_ms": 90, "valid": False},
            {
                "cpu_limit_millicores": 500,
                "p99_latency_ms": 80,
                "valid": True,
                "dependency_bound": "postgres",
            },
        ]
    )

    fit = fit_response_table(
        rows,
        x_key="cpu_limit_millicores",
        y_key="p99_latency_ms",
        direction="lower",
        min_samples=3,
    )

    assert fit.xs == (100, 200, 300)
    assert fit.x_max == 300


def test_response_table_rejects_insufficient_usable_cells() -> None:
    with pytest.raises(ValueError, match="usable"):
        fit_response_table(
            [{"cpu_limit_millicores": 100, "p99_latency_ms": 10, "valid": True}],
            x_key="cpu_limit_millicores",
            y_key="p99_latency_ms",
            direction="lower",
            min_samples=3,
        )


def test_response_table_confidence_bounds_include_repeated_sample_variance() -> None:
    rows = [
        {"cpu": 100, "latency": value, "valid": True} for value in (90, 100, 110)
    ] + [{"cpu": 200, "latency": value, "valid": True} for value in (40, 50, 60)]

    fit = fit_response_table(
        rows,
        x_key="cpu",
        y_key="latency",
        direction="lower",
        min_samples=3,
    )

    assert fit.sample_count == 6
    assert fit.lower[0] < fit.values[0] < fit.upper[0]
    assert fit.lower[1] < fit.values[1] < fit.upper[1]
