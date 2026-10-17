#!/usr/bin/env python3
"""Freeze the confirmatory block count per scenario from pilot variance.

The protocol estimates paired log-ratio variance from pilot blocks and freezes
the confirmatory count *before* any confirmatory run. Nothing did that: the
runner defaulted to five blocks and the power calculation
(`sched_analysis.required_n_paired`) was never called.

For each scenario this computes the required paired count for both sched_ext
arms against the control, takes the larger, applies the frozen five-block floor,
and rounds up to a multiple of three so the balanced three-arm order stays
balanced. The result is a versioned record that the confirmatory run consumes.

The family alpha is the conservative `alpha / PRIMARY_CONTRASTS`, matching the
multiplicity correction the analysis applies, so the count is not sized for a
weaker test than the one that will decide the result.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sched_analysis import (  # noqa: E402
    CONTROL_SCHEDULER,
    DEFAULT_ALPHA,
    DEFAULT_POWER,
    MIN_BLOCKS_FOR_VERDICT,
    PRACTICAL_EFFECT_BY_METRIC,
    PRIMARY_CONTRASTS,
    _is_lower_better,
    _normal_ppf,
    _oriented_log_ratio,
    is_descriptive,
    practical_effect,
    read_blocks,
    required_n_paired,
)

BALANCE = 3


def _chi2_lower(p: float, df: int) -> float:
    """Lower quantile of the chi-square distribution (Wilson-Hilferty).

    Needed for the upper confidence bound on the pilot's variance. The
    approximation is conservative at the small degrees of freedom a short pilot
    has, which is the safe direction for a planning bound.
    """

    z = _normal_ppf(p)
    term = 1.0 - 2.0 / (9.0 * df) + z * math.sqrt(2.0 / (9.0 * df))
    return df * term**3


def sd_upper_bound(sd: float, n: int, p: float = 0.05) -> float:
    """One-sided (1 - p) upper confidence bound on a standard deviation."""

    if n < 2 or sd <= 0.0:
        return sd
    df = n - 1
    return sd * math.sqrt(df / _chi2_lower(p, df))

# The minimum paired blocks from which a sample standard deviation can be
# estimated at all. Deliberately not the confirmatory verdict floor: the pilot
# exists to produce variance, not verdicts.
MIN_PILOT_PAIRED_BLOCKS = 3

# The protocol's confirmatory floor: "take the maximum for the workload, floor at
# five, then round up to a multiple of three". It is independent of
# `MIN_BLOCKS_FOR_VERDICT`, which is the analysis's lower sanity bound for even
# attempting a comparison.
CONFIRMATORY_FLOOR_BLOCKS = 5


def round_up_to_multiple(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple


def plan(observations: list) -> dict:
    """Per-scenario sample-size plan plus the inputs it was derived from."""

    grouped: dict[tuple[str, str, str | None], dict[str, dict[str, float]]] = {}
    for obs in observations:
        key = (obs.workload, obs.metric, obs.direction)
        grouped.setdefault(key, {}).setdefault(obs.scheduler, {})[obs.block_id] = (
            obs.value
        )

    scenarios: list[dict] = []
    for (workload, metric, direction), by_scheduler in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        control = by_scheduler.get(CONTROL_SCHEDULER)
        if not control:
            raise ValueError(f"pilot for {workload}/{metric} has no control arm")
        lower_is_better = _is_lower_better(metric, direction)
        contrasts: list[dict] = []
        for scheduler, values in sorted(by_scheduler.items()):
            if scheduler == CONTROL_SCHEDULER:
                continue
            shared = sorted(set(control) & set(values))
            # Two paired blocks are the minimum from which a sample standard
            # deviation exists at all. This is *not* the confirmatory floor: a
            # three-block pilot is exactly what the protocol specifies for
            # estimating variance, and requiring the confirmatory block floor
            # here made it impossible to freeze any count.
            if len(shared) < MIN_PILOT_PAIRED_BLOCKS:
                raise ValueError(
                    f"pilot for {workload}/{scheduler} has {len(shared)} paired "
                    f"blocks; {MIN_PILOT_PAIRED_BLOCKS} are required to estimate "
                    "variance"
                )
            logs = [
                _oriented_log_ratio(
                    control[b], values[b], lower_is_better=lower_is_better
                )
                for b in shared
            ]
            sd = statistics.stdev(logs)
            upper = sd_upper_bound(sd, len(shared))
            effect = practical_effect(metric)
            required = required_n_paired(
                sd,
                practical_effect=effect,
                alpha=DEFAULT_ALPHA / PRIMARY_CONTRASTS,
                power=DEFAULT_POWER,
            )
            contrasts.append(
                {
                    "scheduler": scheduler,
                    "paired_blocks": len(shared),
                    "effect": effect,
                    "sd_log_ratio": round(sd, 6),
                    "required_n": required,
                    # Reported, not used for the freeze: with three paired blocks
                    # the point estimate can understate the spread, and a study
                    # planned on it alone would be underpowered without saying so.
                    "sd_upper95_log_ratio": round(upper, 6),
                    "required_n_upper95": required_n_paired(
                        upper,
                        practical_effect=effect,
                        alpha=DEFAULT_ALPHA / PRIMARY_CONTRASTS,
                        power=DEFAULT_POWER,
                    ),
                }
            )
        descriptive = is_descriptive(workload)
        raw_required = max(c["required_n"] for c in contrasts)
        floored = max(raw_required, CONFIRMATORY_FLOOR_BLOCKS)
        frozen = round_up_to_multiple(floored, BALANCE)
        scenarios.append(
            {
                "scenario": workload,
                "metric": metric,
                "descriptive": descriptive,
                "contrasts": contrasts,
                "required_n_raw": raw_required,
                "required_n_floored": floored,
                # A descriptive workload gets no confirmatory count: it is
                # measured and reported, but nothing is frozen for it.
                "confirmatory_blocks": None if descriptive else frozen,
                "confirmatory_blocks_upper95": None if descriptive else round_up_to_multiple(
                    max(c["required_n_upper95"] for c in contrasts), BALANCE
                ),
                "balanced_multiple": BALANCE,
            }
        )

    return {
        "schema_version": 1,
        "control_scheduler": CONTROL_SCHEDULER,
        "practical_effect_by_metric": dict(PRACTICAL_EFFECT_BY_METRIC),
        "power": DEFAULT_POWER,
        "alpha": DEFAULT_ALPHA,
        "primary_contrasts": PRIMARY_CONTRASTS,
        "alpha_per_contrast": DEFAULT_ALPHA / PRIMARY_CONTRASTS,
        "min_blocks": CONFIRMATORY_FLOOR_BLOCKS,
        "balance_multiple": BALANCE,
        "scenarios": scenarios,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-blocks", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    record = plan(read_blocks(args.pilot_blocks))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    for scenario in record["scenarios"]:
        count = scenario["confirmatory_blocks"]
        upper = scenario["confirmatory_blocks_upper95"]
        if scenario["descriptive"]:
            print(f"{scenario['scenario']}: descriptive, no confirmatory count")
            continue
        print(
            f"{scenario['scenario']}: {count} blocks "
            f"(raw {scenario['required_n_raw']}, floor {CONFIRMATORY_FLOOR_BLOCKS}, "
            f"balanced to {BALANCE}; {upper} at the 95% variance bound)"
        )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
