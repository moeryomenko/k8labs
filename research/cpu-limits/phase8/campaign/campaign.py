"""Deterministic factorial campaign design and preflight budget guard."""

from __future__ import annotations

import itertools
import json
import random
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BudgetExceeded(ValueError):
    """The requested design exceeds its cap without explicit confirmation."""


@dataclass(frozen=True)
class Factor:
    name: str
    levels: tuple[Any, ...]


@dataclass(frozen=True)
class CampaignConfig:
    repetitions: int = 1
    seed: int = 1
    budget_cap: int = 64
    confirmed: bool = False
    allow_fractional: bool = False
    seconds_per_trial: int = 600
    cpu_millicores_per_trial: int = 500


@dataclass(frozen=True)
class CampaignPlan:
    design: str
    full_cells: int
    cells: tuple[dict[str, Any], ...]
    total_trials: int
    preview: dict[str, int]
    seed: int
    repetitions: int


def plan_campaign(
    factors: tuple[Factor, ...], config: CampaignConfig = CampaignConfig()
) -> CampaignPlan:
    """Create a seeded randomized design and refuse unapproved over-budget work."""
    if config.repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if config.budget_cap < 1:
        raise ValueError("budget_cap must be at least 1")
    if config.seconds_per_trial < 1 or config.cpu_millicores_per_trial < 1:
        raise ValueError("trial resource estimates must be positive")
    names = [factor.name for factor in factors]
    if not factors or len(names) != len(set(names)):
        raise ValueError("factors must have unique names and cannot be empty")
    if any(
        len(factor.levels) < 2 or len(set(factor.levels)) != len(factor.levels)
        for factor in factors
    ):
        raise ValueError("each factor must have at least two unique levels")

    full_cells = 1
    for factor in factors:
        full_cells *= len(factor.levels)
    full_trials = full_cells * config.repetitions
    use_fractional = full_trials > config.budget_cap and config.allow_fractional
    design = "fractional" if use_fractional else "full"

    all_cells = [
        dict(zip(names, values, strict=True))
        for values in itertools.product(*(f.levels for f in factors))
    ]
    if use_fractional:
        baseline = all_cells[0]
        rng = random.Random(config.seed)
        rest = all_cells[1:]
        rng.shuffle(rest)
        max_cells = config.budget_cap // config.repetitions
        if max_cells < 2:
            raise BudgetExceeded("budget cannot fit baseline and one fractional cell")
        cells = [baseline, *rest[: max_cells - 1]]
    else:
        cells = all_cells

    total_trials = len(cells) * config.repetitions
    if total_trials > config.budget_cap and not config.confirmed:
        raise BudgetExceeded(
            f"{design} design requires {total_trials} trials, above budget cap "
            f"{config.budget_cap}; explicit confirmation is required"
        )
    if use_fractional:
        # Keep the all-minimum baseline first for a cheap, deterministic health
        # check before randomized campaign cells are launched.
        remainder = cells[1:]
        random.Random(config.seed).shuffle(remainder)
        cells = [cells[0], *remainder]
    else:
        random.Random(config.seed).shuffle(cells)
    return CampaignPlan(
        design=design,
        full_cells=full_cells,
        cells=tuple(cells),
        total_trials=total_trials,
        preview={
            "full_trials": full_trials,
            "planned_trials": total_trials,
            "estimated_seconds": total_trials * config.seconds_per_trial,
            "estimated_cpu_millicore_seconds": total_trials
            * config.seconds_per_trial
            * config.cpu_millicores_per_trial,
        },
        seed=config.seed,
        repetitions=config.repetitions,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--factors",
        type=Path,
        required=True,
        help="JSON object mapping factor names to level arrays",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Write the plan preview as JSON"
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--budget-cap", type=int, default=64)
    parser.add_argument("--confirm-over-budget", action="store_true")
    parser.add_argument("--fractional-on-over-budget", action="store_true")
    args = parser.parse_args()
    raw = json.loads(args.factors.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or any(
        not isinstance(levels, list) for levels in raw.values()
    ):
        parser.error("--factors must contain a JSON object of arrays")
    design = tuple(Factor(name, tuple(levels)) for name, levels in raw.items())
    config = CampaignConfig(
        repetitions=args.repetitions,
        seed=args.seed,
        budget_cap=args.budget_cap,
        confirmed=args.confirm_over_budget,
        allow_fractional=args.fractional_on_over_budget,
    )
    try:
        plan = plan_campaign(design, config)
    except (ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    document = {
        "design": plan.design,
        "seed": plan.seed,
        "repetitions": plan.repetitions,
        "full_cells": plan.full_cells,
        "planned_cells": len(plan.cells),
        "total_trials": plan.total_trials,
        "preview": plan.preview,
        "cells": plan.cells,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
