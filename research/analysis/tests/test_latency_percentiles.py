"""Tests for latency percentile computation.

These tests are written test-first against a module that does not exist yet:
``research/analysis/latency_stats.py``. The module must implement the following
public API, which is the contract these tests encode:

    compute_percentiles(values, percentiles=(50, 95, 99)) -> dict[float, float]
    p50_p95_p99(values) -> tuple[float, float, float]

Semantics (pinned so the implementation cannot drift):

- Percentiles follow the linear-interpolation definition used by
  ``numpy.percentile`` / ``pandas.Series.quantile``: sort ascending, rank
  ``r = (n - 1) * p / 100``, interpolate between the two neighbours when ``r``
  is fractional, clamp at the max when ``p == 100``.
- Input is any sequence of numbers; the function must NOT mutate it.
- Empty input returns ``0.0`` for every requested percentile (mirrors the
  load-generator.sh behaviour, which initializes p50/p95/p99 to 0 when the
  CSV has no data rows) instead of NaN, so downstream summary math never
  poisons on a failed or empty generation.
- A single sample returns that value for every percentile.

Expected values in the parametrized cases are hand-computed from the linear
definition and cross-checked against numpy:
    [1,2,3]   -> p50=2.0   p95=2.9   p99=2.98
    [1,2,3,4] -> p50=2.5   p95=3.85  p99=3.97
    [42]      -> 42.0 / 42.0 / 42.0
    [5,5,5]   -> 5.0 / 5.0 / 5.0
    [10,1,30,20,5] -> p50=10.0  p95=28.0  p99=29.6

Run from research/analysis:
    python3 -m pytest tests/test_latency_percentiles.py -q
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest

# The module does not exist yet — collection fails with ModuleNotFoundError,
# which is the red phase of the test-first design. Creating
# research/analysis/latency_stats.py makes these tests pass.
from latency_stats import compute_percentiles, p50_p95_p99  # noqa: F401


# =========================================================================
# p50_p95_p99 — single-purpose percentile triple
# =========================================================================


class TestP50P95P99:
    """Correctness of the p50/p95/p99 triple over a latency sample."""

    def test_odd_sample_count(self) -> None:
        """Odd count: median is the exact middle value."""
        assert p50_p95_p99([1, 2, 3]) == pytest.approx((2.0, 2.9, 2.98))

    def test_even_sample_count(self) -> None:
        """Even count: median interpolates between the two middle values."""
        assert p50_p95_p99([1, 2, 3, 4]) == pytest.approx((2.5, 3.85, 3.97))

    def test_single_sample(self) -> None:
        """A single sample is its own p50/p95/p99."""
        assert p50_p95_p99([42]) == pytest.approx((42.0, 42.0, 42.0))

    def test_ties(self) -> None:
        """Tied values return the tied value for every percentile."""
        assert p50_p95_p99([5, 5, 5]) == pytest.approx((5.0, 5.0, 5.0))

    def test_empty_input_returns_zero(self) -> None:
        """Empty input degrades to 0.0 (not NaN) for every percentile."""
        assert p50_p95_p99([]) == pytest.approx((0.0, 0.0, 0.0))

    def test_unsorted_input(self) -> None:
        """Input does not need to be pre-sorted."""
        assert p50_p95_p99([10, 1, 30, 20, 5]) == pytest.approx((10.0, 28.0, 29.6))

    def test_input_not_mutated(self) -> None:
        """The input sequence must be left unchanged."""
        values = [10, 1, 30, 20, 5]
        p50_p95_p99(values)
        assert values == [10, 1, 30, 20, 5]


# =========================================================================
# compute_percentiles — general percentile dict
# =========================================================================


class TestComputePercentiles:
    """General percentile computation with configurable percentile set."""

    def test_default_percentiles_keys(self) -> None:
        """Defaults to the p50/p95/p99 set."""
        result = compute_percentiles([1, 2, 3, 4])
        assert set(result) == {50.0, 95.0, 99.0}
        assert result[50.0] == pytest.approx(2.5)
        assert result[95.0] == pytest.approx(3.85)
        assert result[99.0] == pytest.approx(3.97)

    def test_custom_percentiles(self) -> None:
        """Custom percentile set is honoured."""
        result = compute_percentiles([1, 2, 3, 4], (25, 75))
        assert result == pytest.approx({25.0: 1.75, 75.0: 3.25})

    def test_empty_input_returns_zero_for_each_percentile(self) -> None:
        """Empty input maps to 0.0 for each requested percentile."""
        assert compute_percentiles([]) == pytest.approx(
            {50.0: 0.0, 95.0: 0.0, 99.0: 0.0}
        )


# =========================================================================
# CSV integration — percentiles computed from a real latency.csv shape
# =========================================================================


class TestLatencyCsvIntegration:
    """Compute percentiles from the CSV contract produced by the load generator.

    The load generator writes timestamp,endpoint,latency_ms,status per request;
    latency analysis reads that CSV with pandas and must get the same numbers
    as the unit-level cases.
    """

    LATENCY_CSV = """timestamp,endpoint,latency_ms,status
2026-08-03T10:00:00Z,users,1,200
2026-08-03T10:00:00Z,search,2,200
2026-08-03T10:00:01Z,users,3,200
2026-08-03T10:00:01Z,search,4,200
"""

    def _write_latency_csv(self, tmp_path: pathlib.Path) -> pathlib.Path:
        path = tmp_path / "latency.csv"
        path.write_text(self.LATENCY_CSV)
        return path

    def test_percentiles_from_latency_csv(self, tmp_path: pathlib.Path) -> None:
        """p50_p95_p99 over the latency_ms column of the generator CSV."""
        csv_path = self._write_latency_csv(tmp_path)
        df = pd.read_csv(csv_path)
        assert list(df.columns) == ["timestamp", "endpoint", "latency_ms", "status"]
        assert p50_p95_p99(df["latency_ms"].tolist()) == pytest.approx(
            (2.5, 3.85, 3.97)
        )

    def test_series_input_accepted(self, tmp_path: pathlib.Path) -> None:
        """A pandas Series is an acceptable input (no list() requirement)."""
        csv_path = self._write_latency_csv(tmp_path)
        df = pd.read_csv(csv_path)
        assert p50_p95_p99(df["latency_ms"]) == pytest.approx((2.5, 3.85, 3.97))
