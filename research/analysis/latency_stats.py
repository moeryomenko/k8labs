"""Latency percentile computation for k8labs experiments (TASK-007 REQ-3).

The percentile method is linear interpolation, matching the
``numpy.percentile`` / ``pandas.Series.quantile`` definition: sort ascending,
rank ``r = (n - 1) * p / 100``, and interpolate between the two neighbours
when ``r`` is fractional.

Empty input degrades to ``0.0`` for every requested percentile (never NaN) so
downstream summary math is not poisoned by a failed or empty load generation;
a single sample returns that value for every percentile.
"""

from __future__ import annotations

import pathlib
from collections.abc import Sequence

import pandas as pd

# The latency generator CSV contract: one row per request.
#     timestamp,endpoint,latency_ms,status
LATENCY_MS_COLUMN = "latency_ms"

PathLike = str | pathlib.Path


def compute_percentiles(
    values: Sequence[float],
    percentiles: Sequence[float] = (50, 95, 99),
) -> dict[float, float]:
    """Return linear-interpolation percentiles of ``values``.

    Args:
        values: Latency samples. Any sequence of numbers works (list, tuple,
            or a pandas Series, which is duck-typed as iterable). The input
            is never mutated.
        percentiles: Percentile ranks to compute, each in the open interval
            (0, 100]. Defaults to the p50/p95/p99 set.

    Returns:
        Mapping of float percentile -> interpolated value. Empty input maps
        every percentile to ``0.0``; ``p == 100`` clamps to the maximum.
    """
    sorted_values = sorted(values)
    if not sorted_values:
        return {float(p): 0.0 for p in percentiles}

    n = len(sorted_values)
    result: dict[float, float] = {}
    for p in percentiles:
        rank = (n - 1) * float(p) / 100.0
        if rank <= 0.0:
            result[float(p)] = float(sorted_values[0])
        elif rank >= n - 1:
            result[float(p)] = float(sorted_values[-1])
        else:
            lower = int(rank)
            frac = rank - lower
            lo = float(sorted_values[lower])
            hi = float(sorted_values[lower + 1])
            result[float(p)] = lo + frac * (hi - lo)
    return result


def p50_p95_p99(values: Sequence[float]) -> tuple[float, float, float]:
    """Return the p50/p95/p99 latency triple as a tuple.

    Convenience wrapper over :func:`compute_percentiles` matching the summary
    printed by the bash load generator.

    Args:
        values: Latency samples (any iterable of numbers).

    Returns:
        The ``(p50, p95, p99)`` tuple; ``(0.0, 0.0, 0.0)`` for empty input.
    """
    result = compute_percentiles(values, (50, 95, 99))
    return result[50.0], result[95.0], result[99.0]


def percentiles_from_csv(csv_path: PathLike) -> dict[float, float]:
    """Read a load-generator latency.csv and return latency percentiles.

    The CSV contract is ``timestamp,endpoint,latency_ms,status`` with one row
    per request; the ``latency_ms`` column is the sample set. An empty or
    header-only CSV yields ``0.0`` per percentile via the empty-input rule.

    Args:
        csv_path: Path to the latency.csv produced by the load generator.

    Returns:
        Mapping of percentile -> latency_ms, keyed by float percentile.
    """
    frame = pd.read_csv(csv_path)
    return compute_percentiles(frame[LATENCY_MS_COLUMN].tolist())
