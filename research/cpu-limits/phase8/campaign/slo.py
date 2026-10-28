"""Turn one cell's raw observations into a pass/fail verdict against the SLO.

The pilot's only job is to decide whether a CPU limit meets the target. That
decision must be reproducible from recorded data and independent of the search
that produced it, so it lives here as a pure function over observations rather
than as a method on the runner.

Every threshold is explicit. A cell that cannot supply a required measurement
is `invalid`, never `passing`: a missing latency sample is not evidence that
latency was good.

One asymmetry is deliberate. An open-loop generator marks itself invalid when it
cannot sustain the offered load, but that can happen because the *service* was
too slow to absorb the offered requests, which is exactly what a CPU limit
causes. When the target was reached and the failure is a large share of failed
requests, the cell is a **breach**: discarding it as `invalid` would let a
starved cell look unmeasured and push the bracket upward. A shortfall or a
report with no completed requests still describes the generator, not the
service, and stays `invalid`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from bracket import Outcome

# The load generator reports a failed-request share in this reason string:
# "<errors> of <completed> requests failed; ...". It is parsed rather than
# re-derived because the report carries no separate field for the attempt count.
FAILED_REQUESTS_PATTERN = re.compile(r"^(\d+) of (\d+) requests failed")

# A cell whose target was reached but lost this share of requests is worse than
# any SLO a campaign would set, so it is recorded as a breach rather than
# discarded. The load generator itself only invalidates above 0.5, so anything
# at or above that share is realistic for a saturated cell.
DEFAULT_INVALID_ERROR_SHARE = 0.5


@dataclass(frozen=True)
class Slo:
    """Service-level objectives a cell is judged against."""

    max_p99_latency_ms: float
    min_throughput_rps: float
    max_error_ratio: float
    min_availability_ratio: float

    def __post_init__(self) -> None:
        if self.max_p99_latency_ms <= 0:
            raise ValueError("max_p99_latency_ms must be positive")
        if self.min_throughput_rps < 0:
            raise ValueError("min_throughput_rps must not be negative")
        if not 0 <= self.max_error_ratio <= 1:
            raise ValueError("max_error_ratio must be within 0..=1")
        if not 0 <= self.min_availability_ratio <= 1:
            raise ValueError("min_availability_ratio must be within 0..=1")


@dataclass(frozen=True)
class Observation:
    """What one cell actually measured.

    `None` means "not measured", which is different from zero: zero latency is a
    real, excellent result, while an absent sample is a failed measurement.
    """

    p50_latency_ms: float | None
    p99_latency_ms: float | None
    throughput_rps: float | None
    error_ratio: float | None
    availability_ratio: float | None
    cpu_usage_millicores: float | None
    throttled_ms_per_second: float | None
    completed_requests: int
    loadgen_valid: bool
    loadgen_invalid_reason: str = ""
    # Whether the cell's Service ever answered during warm-up. `None` means the
    # caller did not establish it, which keeps the strict `invalid` reading.
    target_reached: bool | None = None

    def missing_fields(self, required: tuple[str, ...]) -> list[str]:
        return [name for name in required if getattr(self, name) is None]


def failed_request_share(reason: str) -> float | None:
    """Return the failed-request share from a "N of M requests failed" reason.

    Returns `None` for any other reason string, so a caller can tell "the
    service refused a large share of offers" apart from "the generator itself
    fell behind".
    """
    match = FAILED_REQUESTS_PATTERN.search(reason)
    if match is None:
        return None
    return int(match.group(1)) / int(match.group(2))


def service_breach_share(observation: Observation) -> float | None:
    """Return the failed-request share when a loadgen-invalid cell is a breach.

    A cell counts as a service breach only when the target was reached and the
    generator failed on failed requests. Every other invalid cause describes the
    harness, so the share is `None` and the cell stays `invalid`.
    """
    if observation.loadgen_valid or observation.target_reached is not True:
        return None
    return failed_request_share(observation.loadgen_invalid_reason)


def verdict(
    observation: Observation,
    slo: Slo,
    *,
    required_fields: tuple[str, ...] = (
        "p50_latency_ms",
        "p99_latency_ms",
        "throughput_rps",
        "error_ratio",
        "availability_ratio",
        "cpu_usage_millicores",
        "throttled_ms_per_second",
    ),
    invalid_error_share: float = DEFAULT_INVALID_ERROR_SHARE,
) -> tuple[Outcome, str]:
    """Judge one cell. Returns the outcome and a human-readable reason.

    Invalid conditions are checked first and short-circuit, because a cell whose
    load generator lost control cannot be judged against throughput or latency:
    both numbers describe the generator, not the service.

    The one exception is a target that was reached and then failed a large share
    of requests: that is a service breach, not a lost generator, because the
    requests were offered and answered wrongly.
    """
    if not 0 <= invalid_error_share <= 1:
        raise ValueError("invalid_error_share must be within 0..=1")

    if not observation.loadgen_valid:
        reason = (
            observation.loadgen_invalid_reason
            or "load generator reported the run invalid"
        )
        if observation.completed_requests < 1:
            return Outcome.INVALID, f"load generator invalid: {reason}"
        share = service_breach_share(observation)
        if share is not None and share >= invalid_error_share:
            return Outcome.BREACHES_SLO, (
                f"service failed {share:.1%} of offered requests, at or above "
                f"the {invalid_error_share:.0%} breach threshold: {reason}"
            )
        return Outcome.INVALID, f"load generator invalid: {reason}"

    if observation.completed_requests < 1:
        return Outcome.INVALID, "no requests completed in the measured window"

    missing = observation.missing_fields(required_fields)
    if missing:
        return Outcome.INVALID, f"missing measurement(s): {', '.join(missing)}"

    assert observation.p99_latency_ms is not None
    assert observation.throughput_rps is not None
    assert observation.error_ratio is not None
    assert observation.availability_ratio is not None

    for name, value in (
        ("p99_latency_ms", observation.p99_latency_ms),
        ("throughput_rps", observation.throughput_rps),
        ("error_ratio", observation.error_ratio),
        ("availability_ratio", observation.availability_ratio),
    ):
        if not math.isfinite(value):
            return Outcome.INVALID, f"{name} is not finite ({value})"

    breaches: list[str] = []
    if observation.p99_latency_ms > slo.max_p99_latency_ms:
        breaches.append(
            f"p99 {observation.p99_latency_ms:.1f} ms > {slo.max_p99_latency_ms:.1f} ms"
        )
    if observation.throughput_rps < slo.min_throughput_rps:
        breaches.append(
            f"throughput {observation.throughput_rps:.1f} rps < {slo.min_throughput_rps:.1f} rps"
        )
    if observation.error_ratio > slo.max_error_ratio:
        breaches.append(
            f"error ratio {observation.error_ratio:.4f} > {slo.max_error_ratio:.4f}"
        )
    if observation.availability_ratio < slo.min_availability_ratio:
        breaches.append(
            f"availability {observation.availability_ratio:.4f} "
            f"< {slo.min_availability_ratio:.4f}"
        )

    if breaches:
        return Outcome.BREACHES_SLO, "; ".join(breaches)
    return Outcome.MEETS_SLO, "all objectives met"
