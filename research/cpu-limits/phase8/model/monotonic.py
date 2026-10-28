"""Small, bounded monotonic response fitter for measured campaign cells."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    x: float
    value: float


@dataclass(frozen=True)
class MonotonicFit:
    direction: str
    x_min: float
    x_max: float
    xs: tuple[float, ...]
    values: tuple[float, ...]
    lower: tuple[float, ...]
    upper: tuple[float, ...]
    sample_count: int = 0

    def predict(self, x: float) -> float:
        if x < self.x_min or x > self.x_max:
            raise ValueError(
                f"x={x} is outside measured range [{self.x_min}, {self.x_max}]"
            )
        if x in self.xs:
            return self.values[self.xs.index(x)]
        for left, right, left_value, right_value in zip(
            self.xs, self.xs[1:], self.values, self.values[1:]
        ):
            if left <= x <= right:
                fraction = (x - left) / (right - left)
                return left_value + fraction * (right_value - left_value)
        raise AssertionError("bounded interpolation failed")


def _pav(values: list[float], *, decreasing: bool) -> list[float]:
    signed = [-value for value in values] if decreasing else values[:]
    blocks: list[list[float]] = []
    for value in signed:
        blocks.append([value])
        while len(blocks) >= 2 and sum(blocks[-2]) / len(blocks[-2]) > sum(
            blocks[-1]
        ) / len(blocks[-1]):
            blocks[-2].extend(blocks.pop())
    fitted = [
        value for block in blocks for value in [sum(block) / len(block)] * len(block)
    ]
    return [-value for value in fitted] if decreasing else fitted


def fit_monotonic(
    observations: list[Observation], *, direction: str, confidence: float = 0.95
) -> MonotonicFit:
    if direction not in {"lower", "higher"}:
        raise ValueError("direction must be lower or higher")
    if not observations:
        raise ValueError("at least one observation is required")
    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be between 0.5 and 1")
    ordered = sorted(observations, key=lambda item: item.x)
    if any(
        not math.isfinite(item.x) or not math.isfinite(item.value) for item in ordered
    ):
        raise ValueError("observations must be finite")
    if any(right.x <= left.x for left, right in zip(ordered, ordered[1:])):
        raise ValueError("x values must be strictly increasing")
    raw = [item.value for item in ordered]
    values = _pav(raw, decreasing=direction == "lower")
    residuals = [observed - fitted for observed, fitted in zip(raw, values)]
    spread = statistics.pstdev(residuals) if len(residuals) > 1 else abs(raw[0]) * 0.05
    # Normal quantiles avoid a scipy dependency in the benchmark image while
    # retaining explicit, deterministic confidence levels for the artifact.
    z_score = {
        0.90: 1.645,
        0.95: 1.960,
        0.99: 2.576,
    }.get(round(confidence, 2), 1.960)
    margin = z_score * spread
    lower = tuple(max(0.0, value - margin) for value in values)
    upper = tuple(value + margin for value in values)
    return MonotonicFit(
        direction=direction,
        x_min=ordered[0].x,
        x_max=ordered[-1].x,
        xs=tuple(item.x for item in ordered),
        values=tuple(values),
        lower=lower,
        upper=upper,
    )


def fit_response_table(
    rows: Sequence[Mapping[str, object]],
    *,
    x_key: str,
    y_key: str,
    direction: str,
    min_samples: int = 3,
    confidence: float = 0.95,
) -> MonotonicFit:
    """Fit valid, unconfounded measured rows; never impute or extrapolate."""
    if min_samples < 2:
        raise ValueError("min_samples must be at least two for confidence estimation")
    grouped: dict[float, list[float]] = {}
    for row in rows:
        if row.get("valid") is not True:
            continue
        if row.get("dependency_bound", "no") not in (None, "no"):
            continue
        x, value = row.get(x_key), row.get(y_key)
        if not isinstance(x, (int, float)) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(float(x)) or not math.isfinite(float(value)):
            continue
        grouped.setdefault(float(x), []).append(float(value))

    usable = [
        Observation(x, statistics.fmean(values))
        for x, values in sorted(grouped.items())
        if len(values) >= min_samples
    ]
    if len(usable) < 2:
        raise ValueError(
            f"insufficient usable measured cells: need at least two levels "
            f"with {min_samples} samples each"
        )
    fit = fit_monotonic(usable, direction=direction, confidence=confidence)

    # Confidence bounds include within-cell measurement uncertainty as well as
    # monotonic-fit residuals. The normal approximation is conservative only
    # after the minimum repeated-sample gate above.
    lower: list[float] = []
    upper: list[float] = []
    z_score = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}.get(round(confidence, 2), 1.960)
    for x, fitted, fit_low, fit_high in zip(fit.xs, fit.values, fit.lower, fit.upper):
        samples = grouped[x]
        standard_error = statistics.stdev(samples) / math.sqrt(len(samples))
        margin = z_score * standard_error
        lower.append(max(0.0, min(fit_low, fitted - margin)))
        upper.append(max(fit_high, fitted + margin))
    return MonotonicFit(
        direction=fit.direction,
        x_min=fit.x_min,
        x_max=fit.x_max,
        xs=fit.xs,
        values=fit.values,
        lower=tuple(lower),
        upper=tuple(upper),
        sample_count=sum(len(grouped[x]) for x in fit.xs),
    )
