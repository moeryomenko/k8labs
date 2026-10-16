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
    PRACTICAL_EFFECT,
    PRIMARY_CONTRASTS,
    _is_lower_better,
    _oriented_log_ratio,
    read_blocks,
    required_n_paired,
)

BALANCE = 3

# The minimum paired blocks from which a sample standard deviation can be
# estimated at all. Deliberately not the confirmatory verdict floor: the pilot
# exists to produce variance, not verdicts.
MIN_PILOT_PAIRED_BLOCKS = 2

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
            required = required_n_paired(
                sd,
                practical_effect=PRACTICAL_EFFECT,
                alpha=DEFAULT_ALPHA / PRIMARY_CONTRASTS,
                power=DEFAULT_POWER,
            )
            contrasts.append(
                {
                    "scheduler": scheduler,
                    "paired_blocks": len(shared),
                    "sd_log_ratio": round(sd, 6),
                    "required_n": required,
                }
            )
        raw_required = max(c["required_n"] for c in contrasts)
        floored = max(raw_required, CONFIRMATORY_FLOOR_BLOCKS)
        frozen = round_up_to_multiple(floored, BALANCE)
        scenarios.append(
            {
                "scenario": workload,
                "metric": metric,
                "contrasts": contrasts,
                "required_n_raw": raw_required,
                "required_n_floored": floored,
                "confirmatory_blocks": frozen,
                "balanced_multiple": BALANCE,
            }
        )

    return {
        "schema_version": 1,
        "control_scheduler": CONTROL_SCHEDULER,
        "practical_effect": PRACTICAL_EFFECT,
        "power": DEFAULT_POWER,
        "alpha": DEFAULT_ALPHA,
        "primary_contrasts": PRIMARY_CONTRASTS,
        "alpha_per_contrast": DEFAULT_ALPHA / PRIMARY_CONTRASTS,
        "min_blocks": CONFIRMATORY_FLOOR_BLOCKS,
        "balance_multiple": BALANCE,
        "delta_log": round(math.log(1.0 + PRACTICAL_EFFECT), 6),
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
        print(
            f"{scenario['scenario']}: {scenario['confirmatory_blocks']} blocks "
            f"(raw {scenario['required_n_raw']}, floor {CONFIRMATORY_FLOOR_BLOCKS}, "
            f"balanced to {BALANCE})"
        )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
