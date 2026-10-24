"""Deterministic paired statistics for the CPU-limits campaign.

The paired unit is a complete block. Ratios are treatment divided by control.
Missing values stay incomplete and are never changed to zero.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class PairedLogRatios:
    """Usable paired log ratios and the blocks that are incomplete."""

    log_ratios: tuple[float, ...]
    paired_blocks: tuple[str, ...]
    missing_blocks: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """True when every supplied block has a usable pair."""

        return not self.missing_blocks


@dataclass(frozen=True)
class RatioInterval:
    """A ratio-scale interval computed from paired log ratios."""

    estimate: float
    low: float
    high: float
    n: int


@dataclass(frozen=True)
class HolmDecision:
    """One Holm step-down result in the caller input order."""

    p_value: float
    threshold: float
    rejected: bool


def paired_log_ratios(
    control: Mapping[str, float | None], treatment: Mapping[str, float | None]
) -> PairedLogRatios:
    """Return treatment/control log ratios for matching, complete blocks.

    A missing key or a ``None`` value marks its block incomplete. A zero is not
    a missing value because it would make a ratio undefined.
    """

    paired_blocks: list[str] = []
    missing_blocks: list[str] = []
    log_ratios: list[float] = []
    for block in sorted(set(control) | set(treatment)):
        control_value = control.get(block)
        treatment_value = treatment.get(block)
        if control_value is None or treatment_value is None:
            missing_blocks.append(block)
            continue
        _require_positive(control_value, block)
        _require_positive(treatment_value, block)
        paired_blocks.append(block)
        log_ratios.append(math.log(treatment_value / control_value))
    return PairedLogRatios(
        log_ratios=tuple(log_ratios),
        paired_blocks=tuple(paired_blocks),
        missing_blocks=tuple(missing_blocks),
    )


def geometric_mean(values: Iterable[float]) -> float:
    """Return the geometric mean of strictly positive finite values."""

    collected = tuple(values)
    if not collected:
        raise ValueError("geometric mean requires at least one value")
    for value in collected:
        _require_positive(value, "value")
    return math.exp(statistics.fmean(math.log(value) for value in collected))


def geometric_mean_from_logs(log_ratios: Iterable[float]) -> float:
    """Return the geometric mean ratio from finite paired log ratios."""

    collected = tuple(log_ratios)
    if not collected:
        raise ValueError("geometric mean requires at least one log ratio")
    if not all(math.isfinite(value) for value in collected):
        raise ValueError("log ratios must be finite")
    return math.exp(statistics.fmean(collected))


def paired_t_interval(
    log_ratios: Iterable[float], confidence: float = 0.95
) -> RatioInterval:
    """Return a two-sided paired-t interval on the ratio scale."""

    logs = _finite_logs(log_ratios)
    if len(logs) < 2:
        raise ValueError("paired-t interval requires at least two paired blocks")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between zero and one")
    mean = statistics.fmean(logs)
    standard_error = statistics.stdev(logs) / math.sqrt(len(logs))
    probability = 0.5 + confidence / 2.0
    half_width = _student_t_quantile(probability, len(logs) - 1) * standard_error
    return RatioInterval(
        estimate=math.exp(mean),
        low=math.exp(mean - half_width),
        high=math.exp(mean + half_width),
        n=len(logs),
    )


def block_bootstrap_interval(
    log_ratios: Iterable[float],
    *,
    resamples: int = 10_000,
    seed: int = 20261024,
    confidence: float = 0.95,
) -> RatioInterval:
    """Return a deterministic percentile bootstrap interval by block.

    Each resample draws complete paired blocks with replacement. A local random
    generator makes the output independent of global random state.
    """

    logs = _finite_logs(log_ratios)
    if not logs:
        raise ValueError("block bootstrap requires at least one paired block")
    if resamples < 1:
        raise ValueError("resamples must be at least one")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between zero and one")
    generator = random.Random(seed)
    size = len(logs)
    means = sorted(
        statistics.fmean(logs[generator.randrange(size)] for _ in range(size))
        for _ in range(resamples)
    )
    tail = (1.0 - confidence) / 2.0
    low = means[math.floor(tail * (resamples - 1))]
    high = means[math.floor((1.0 - tail) * (resamples - 1))]
    return RatioInterval(
        estimate=math.exp(statistics.fmean(logs)),
        low=math.exp(low),
        high=math.exp(high),
        n=size,
    )


def holm_step_down(
    p_values: Iterable[float], *, planned_family_size: int, alpha: float = 0.05
) -> tuple[bool, ...]:
    """Return Holm rejections against the predeclared family size.

    The planned size may exceed observed contrasts. It may not be smaller,
    because omitted contrasts must not weaken correction.
    """

    return tuple(
        decision.rejected
        for decision in holm_decisions(
            p_values, planned_family_size=planned_family_size, alpha=alpha
        )
    )


def holm_decisions(
    p_values: Iterable[float], *, planned_family_size: int, alpha: float = 0.05
) -> tuple[HolmDecision, ...]:
    """Return Holm thresholds and decisions in caller input order."""

    values = tuple(p_values)
    if planned_family_size < len(values):
        raise ValueError("planned family size cannot be smaller than observed size")
    if planned_family_size < 1:
        raise ValueError("planned family size must be at least one")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between zero and one")
    for value in values:
        if not 0.0 <= value <= 1.0 or not math.isfinite(value):
            raise ValueError("p-values must be finite and between zero and one")

    thresholds = [0.0] * len(values)
    rejected = [False] * len(values)
    stopped = False
    for position, index in enumerate(sorted(range(len(values)), key=values.__getitem__)):
        threshold = alpha / (planned_family_size - position)
        thresholds[index] = threshold
        if not stopped and values[index] <= threshold:
            rejected[index] = True
        else:
            stopped = True
    return tuple(
        HolmDecision(
            p_value=value, threshold=thresholds[index], rejected=rejected[index]
        )
        for index, value in enumerate(values)
    )


def _require_positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be strictly positive and finite")


def _finite_logs(log_ratios: Iterable[float]) -> tuple[float, ...]:
    logs = tuple(log_ratios)
    if not all(math.isfinite(value) for value in logs):
        raise ValueError("log ratios must be finite")
    return logs


def _student_t_quantile(probability: float, degrees_of_freedom: int) -> float:
    """Invert the Student-t CDF with a deterministic binary search."""

    if not 0.0 < probability < 1.0:
        raise ValueError("probability must be between zero and one")
    if degrees_of_freedom < 1:
        raise ValueError("degrees of freedom must be at least one")
    if probability == 0.5:
        return 0.0
    if probability < 0.5:
        return -_student_t_quantile(1.0 - probability, degrees_of_freedom)

    low = 0.0
    high = 1.0
    while _student_t_cdf(high, degrees_of_freedom) < probability:
        high *= 2.0
    for _ in range(80):
        middle = (low + high) / 2.0
        if _student_t_cdf(middle, degrees_of_freedom) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _student_t_cdf(value: float, degrees_of_freedom: int) -> float:
    if value == 0.0:
        return 0.5
    x = degrees_of_freedom / (degrees_of_freedom + value * value)
    beta = _regularized_incomplete_beta(x, degrees_of_freedom / 2.0, 0.5)
    return 1.0 - beta / 2.0 if value > 0.0 else beta / 2.0


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """Evaluate the regularized incomplete beta function.

    This continued fraction is stable for the small paired-block counts used by
    the campaign. It avoids a scientific-library dependency.
    """

    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_fraction(a, b, x) / a
    return 1.0 - front * _beta_fraction(b, a, 1.0 - x) / b


def _beta_fraction(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    tolerance = 3e-14
    c = 1.0
    d = 1.0 - (a + b) * x / (a + 1.0)
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    result = d
    for iteration in range(1, 201):
        twice = 2 * iteration
        coefficient = iteration * (b - iteration) * x / ((a + twice - 1) * (a + twice))
        d = 1.0 + coefficient * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + coefficient / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        result *= d * c

        coefficient = -(a + iteration) * (a + b + iteration) * x / ((a + twice) * (a + twice + 1))
        d = 1.0 + coefficient * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + coefficient / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        change = d * c
        result *= change
        if abs(change - 1.0) < tolerance:
            return result
    raise ArithmeticError("incomplete beta fraction did not converge")
