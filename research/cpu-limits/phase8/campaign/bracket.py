"""Bracketed search for the CPU limit at which a workload stops meeting its SLO.

The pilot answers one question before the full campaign spends budget: which
CPU limits are worth measuring? It evaluates a small, increasing set of limits
and stops at the first one that meets the SLO.

Three properties matter more than speed:

* **Monotonic sampling.** Limits only increase, so no cell is re-measured at the
  same limit and noise cannot make the search oscillate between two values.
* **Node-capacity bound.** No sampled limit may exceed the allocatable CPU of
  the node the cell runs on. A larger limit is not expressible by the scheduler,
  so measuring it would describe a cluster that cannot exist.
* **Explicit failure.** If every admissible limit fails, the result is
  `unsatisfiable` rather than a guessed bracket. A model fitted over a bracket
  that contains no passing cell would be extrapolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Outcome(str, Enum):
    """Result of evaluating one limit against the SLO.

    Defined here, next to the search that consumes it, so the search and the
    verdict function cannot drift apart by importing two different enums.
    """

    MEETS_SLO = "meets-slo"
    BREACHES_SLO = "breaches-slo"
    INVALID = "invalid"


class StopReason(str, Enum):
    MEETS_SLO = "meets-slo"
    CAPACITY_REACHED = "capacity-reached"
    INVALID_ONLY = "invalid-only"


class PilotError(RuntimeError):
    """The pilot cannot produce a usable bracket."""


def plan_limits(
    *,
    min_limit_millicores: int,
    max_limit_millicores: int,
    growth: float,
    max_cells: int,
) -> tuple[int, ...]:
    """Limits to try, strictly ascending, inside `[min, max]`, at most `max_cells`.

    Growth is multiplicative so a bracketed search spans an order of magnitude
    in a few cells. The final limit is clamped to `max_limit_millicores`, which
    is the allocatable CPU of the largest node the pilot may run on.

    Values are rounded to 10m above 100m and to 1m below it, which keeps the
    numbers legible without ever rounding a limit past the node cap.
    """
    if min_limit_millicores < 1:
        raise ValueError("min_limit_millicores must be at least 1")
    if max_limit_millicores < min_limit_millicores:
        raise ValueError("max_limit_millicores must be at least min_limit_millicores")
    if growth <= 1.0:
        raise ValueError("growth must be greater than 1 or the plan never grows")
    if max_cells < 1:
        raise ValueError("max_cells must be at least 1")

    limits: list[int] = []
    current = float(min_limit_millicores)
    while len(limits) < max_cells:
        if current >= 100:
            limit = int(current / 10.0 + 0.5) * 10
        else:
            limit = int(current + 0.5)
        limit = min(max(limit, min_limit_millicores), max_limit_millicores)

        # A duplicate means growth stalled against the cap, and the cap is the
        # only value left to try.
        if limits and limit <= limits[-1]:
            break

        limits.append(limit)
        if limit >= max_limit_millicores:
            break
        current *= growth

    return tuple(limits)


@dataclass(frozen=True)
class Trial:
    """One evaluated limit and what the measurement said about it."""

    limit_millicores: int
    outcome: Outcome
    reason: str = ""


@dataclass(frozen=True)
class Bracket:
    """Limits around the SLO boundary, ready to seed the campaign.

    `passing_millicores` is the smallest limit that met the SLO. `failing_below`
    is the largest limit under it that was measured and did not, or None when
    the pilot never observed a failure below it. None means the true boundary
    lies somewhere at or below `passing_millicores` and is not yet bracketed,
    which the campaign must probe rather than assume.
    """

    passing_millicores: int
    failing_below_millicores: int | None
    trials: tuple[Trial, ...]
    stop: StopReason
    node_capacity_millicores: int


def evaluate_trials(trials: list[Trial], node_capacity_millicores: int) -> Bracket:
    """Derive the bracket from trial outcomes, or raise when there is none.

    Trials must be strictly ascending in limit; anything else means the caller
    measured a limit twice and the ordering assumptions above no longer hold.
    """
    if not trials:
        raise PilotError("no trials were recorded")

    for earlier, later in zip(trials, trials[1:]):
        if later.limit_millicores <= earlier.limit_millicores:
            raise PilotError(
                "trial limits must strictly increase: "
                f"{earlier.limit_millicores}m then {later.limit_millicores}m"
            )

    for trial in trials:
        if trial.limit_millicores > node_capacity_millicores:
            raise PilotError(
                f"trial limit {trial.limit_millicores}m exceeds node capacity "
                f"{node_capacity_millicores}m; the result would describe an "
                "unschedulable limit"
            )

    passing: Trial | None = None
    for trial in trials:
        if trial.outcome is Outcome.MEETS_SLO:
            passing = trial
            break

    if passing is None:
        invalid = [t for t in trials if t.outcome is Outcome.INVALID]
        if invalid and len(invalid) == len(trials):
            raise PilotError(
                "every trial was invalid; the measurement chain never produced "
                "usable data"
            )
        raise PilotError(
            "unsatisfiable: no admissible limit met the SLO, "
            f"highest tried was {trials[-1].limit_millicores}m"
            f" against node capacity {node_capacity_millicores}m"
        )

    failing_below: int | None = None
    for trial in trials:
        if trial.limit_millicores >= passing.limit_millicores:
            break
        if trial.outcome is Outcome.BREACHES_SLO:
            failing_below = trial.limit_millicores

    stop = (
        StopReason.CAPACITY_REACHED
        if passing.limit_millicores >= node_capacity_millicores
        else StopReason.MEETS_SLO
    )

    return Bracket(
        passing_millicores=passing.limit_millicores,
        failing_below_millicores=failing_below,
        trials=tuple(trials),
        stop=stop,
        node_capacity_millicores=node_capacity_millicores,
    )
