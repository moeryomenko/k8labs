"""Tests for turning one cell's observations into a pass/fail verdict.

The rules that matter: a missing measurement is never a pass, a load generator
that lost control cannot produce a verdict at all, and every SLO dimension is
checked independently so one breach is not masked by another passing.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "campaign"))

from bracket import Outcome  # noqa: E402
from slo import Observation, Slo, verdict  # noqa: E402

SLO = Slo(
    max_p99_latency_ms=100.0,
    min_throughput_rps=47.5,
    max_error_ratio=0.0,
    min_availability_ratio=0.99,
)


def observation(**overrides: object) -> Observation:
    defaults: dict[str, object] = {
        "p50_latency_ms": 5.0,
        "p99_latency_ms": 40.0,
        "throughput_rps": 50.0,
        "error_ratio": 0.0,
        "availability_ratio": 1.0,
        "cpu_usage_millicores": 120.0,
        "throttled_ms_per_second": 0.0,
        "completed_requests": 3000,
        "loadgen_valid": True,
        "loadgen_invalid_reason": "",
    }
    defaults.update(overrides)
    return Observation(**defaults)  # type: ignore[arg-type]


class TestVerdict:
    def test_healthy_cell_meets_slo(self) -> None:
        outcome, reason = verdict(observation(), SLO)
        assert outcome is Outcome.MEETS_SLO
        assert "met" in reason

    def test_latency_breach(self) -> None:
        outcome, reason = verdict(observation(p99_latency_ms=161.0), SLO)
        assert outcome is Outcome.BREACHES_SLO
        assert "p99" in reason

    def test_throughput_breach(self) -> None:
        outcome, reason = verdict(observation(throughput_rps=30.0), SLO)
        assert outcome is Outcome.BREACHES_SLO
        assert "throughput" in reason

    def test_error_ratio_breach(self) -> None:
        outcome, reason = verdict(observation(error_ratio=0.01), SLO)
        assert outcome is Outcome.BREACHES_SLO
        assert "error ratio" in reason

    def test_availability_breach(self) -> None:
        outcome, reason = verdict(observation(availability_ratio=0.95), SLO)
        assert outcome is Outcome.BREACHES_SLO
        assert "availability" in reason

    def test_multiple_breaches_are_all_reported(self) -> None:
        _, reason = verdict(observation(p99_latency_ms=500.0, throughput_rps=1.0), SLO)
        assert "p99" in reason and "throughput" in reason

    def test_exactly_at_the_boundary_meets(self) -> None:
        outcome, _ = verdict(
            observation(
                p99_latency_ms=100.0,
                throughput_rps=47.5,
                error_ratio=0.0,
                availability_ratio=0.99,
            ),
            SLO,
        )
        assert outcome is Outcome.MEETS_SLO

    def test_invalid_loadgen_short_circuits(self) -> None:
        outcome, reason = verdict(
            observation(
                loadgen_valid=False, loadgen_invalid_reason="scheduler fell behind"
            ),
            SLO,
        )
        assert outcome is Outcome.INVALID
        assert "scheduler fell behind" in reason

    def test_invalid_loadgen_without_reason_still_invalid(self) -> None:
        outcome, _ = verdict(observation(loadgen_valid=False), SLO)
        assert outcome is Outcome.INVALID

    def test_no_completed_requests_is_invalid(self) -> None:
        outcome, reason = verdict(observation(completed_requests=0), SLO)
        assert outcome is Outcome.INVALID
        assert "no requests" in reason

    def test_offered_load_shortfall_stays_invalid(self) -> None:
        outcome, reason = verdict(
            observation(
                loadgen_valid=False,
                loadgen_invalid_reason=(
                    "generator could not sustain the offered load: offered 50.00 rps, "
                    "achieved 20.00 rps (60.0% short)"
                ),
                target_reached=True,
            ),
            SLO,
        )
        assert outcome is Outcome.INVALID
        assert "short" in reason

    def test_reached_target_with_large_error_share_is_a_breach(self) -> None:
        outcome, reason = verdict(
            observation(
                loadgen_valid=False,
                loadgen_invalid_reason=(
                    "900 of 1000 requests failed; the load generator cannot "
                    "characterise a failing target"
                ),
                completed_requests=1000,
                target_reached=True,
            ),
            SLO,
        )
        assert outcome is Outcome.BREACHES_SLO
        assert "90.0%" in reason

    def test_large_error_share_without_reached_target_stays_invalid(self) -> None:
        outcome, _ = verdict(
            observation(
                loadgen_valid=False,
                loadgen_invalid_reason="900 of 1000 requests failed",
                completed_requests=1000,
                target_reached=False,
            ),
            SLO,
        )
        assert outcome is Outcome.INVALID

    def test_unknown_target_reach_stays_invalid(self) -> None:
        outcome, _ = verdict(
            observation(
                loadgen_valid=False,
                loadgen_invalid_reason="900 of 1000 requests failed",
                completed_requests=1000,
            ),
            SLO,
        )
        assert outcome is Outcome.INVALID

    def test_error_share_below_threshold_stays_invalid(self) -> None:
        outcome, _ = verdict(
            observation(
                loadgen_valid=False,
                loadgen_invalid_reason="300 of 1000 requests failed",
                completed_requests=1000,
                target_reached=True,
            ),
            SLO,
        )
        assert outcome is Outcome.INVALID

    def test_error_share_threshold_is_configurable(self) -> None:
        outcome, _ = verdict(
            observation(
                loadgen_valid=False,
                loadgen_invalid_reason="300 of 1000 requests failed",
                completed_requests=1000,
                target_reached=True,
            ),
            SLO,
            invalid_error_share=0.25,
        )
        assert outcome is Outcome.BREACHES_SLO

    def test_rejects_out_of_range_error_share(self) -> None:
        with pytest.raises(ValueError, match="invalid_error_share"):
            verdict(observation(), SLO, invalid_error_share=1.5)

    def test_reached_target_with_zero_completed_stays_invalid(self) -> None:
        outcome, _ = verdict(
            observation(
                loadgen_valid=False,
                loadgen_invalid_reason="no requests completed in the measured window",
                completed_requests=0,
                target_reached=True,
            ),
            SLO,
        )
        assert outcome is Outcome.INVALID

    def test_missing_latency_is_invalid_not_passing(self) -> None:
        outcome, reason = verdict(observation(p99_latency_ms=None), SLO)
        assert outcome is Outcome.INVALID
        assert "p99_latency_ms" in reason

    def test_missing_cgroup_derivative_is_invalid(self) -> None:
        outcome, reason = verdict(observation(throttled_ms_per_second=None), SLO)
        assert outcome is Outcome.INVALID
        assert "throttled_ms_per_second" in reason

    @pytest.mark.parametrize("field", ["p99_latency_ms", "throughput_rps"])
    def test_non_finite_values_are_invalid(self, field: str) -> None:
        outcome, reason = verdict(observation(**{field: math.nan}), SLO)
        assert outcome is Outcome.INVALID
        assert field in reason


class TestSloValidation:
    def test_rejects_non_positive_latency(self) -> None:
        with pytest.raises(ValueError, match="max_p99_latency_ms"):
            Slo(
                max_p99_latency_ms=0,
                min_throughput_rps=1,
                max_error_ratio=0,
                min_availability_ratio=1,
            )

    def test_rejects_ratio_above_one(self) -> None:
        with pytest.raises(ValueError, match="max_error_ratio"):
            Slo(
                max_p99_latency_ms=100,
                min_throughput_rps=1,
                max_error_ratio=1.5,
                min_availability_ratio=1,
            )

    def test_rejects_negative_availability(self) -> None:
        with pytest.raises(ValueError, match="min_availability_ratio"):
            Slo(
                max_p99_latency_ms=100,
                min_throughput_rps=1,
                max_error_ratio=0,
                min_availability_ratio=-0.1,
            )
