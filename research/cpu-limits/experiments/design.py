#!/usr/bin/env python3
"""Define and validate the frozen CPU-limits block design."""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass

WORKLOADS = ("cpu-bound", "io-bound", "memory-bound", "hybrid")
POLICIES = (
    "fit",
    "best-effort",
    "request-low",
    "request-high",
    "guaranteed-low",
    "burstable-low",
)
CONDITIONS = ("solo", "contention")

SOLO_POLICIES = ("fit", "best-effort", "request-low", "burstable-low")
CONTENTION_POLICIES = (
    "fit",
    "request-low",
    "request-high",
    "guaranteed-low",
    "burstable-low",
)

# Values are millicores per container. They are frozen by the approved plan.
CPU_VALUES = {
    "cpu-bound": {"fit": 2000, "low": 500},
    "io-bound": {"fit": 1000, "low": 250},
    "memory-bound": {"fit": 1000, "low": 500},
    "hybrid": {"fit": 1000, "low": 500},
}
LOW_REQUEST_MILLICORES = 100


@dataclass(frozen=True)
class Cell:
    """One workload, policy, and condition measurement."""

    cell_id: str
    workload: str
    policy: str
    condition: str
    request_millicores: int | None
    limit_millicores: int | None


def resources_for(workload: str, policy: str) -> tuple[int | None, int | None]:
    """Return the CPU request and limit for one valid policy."""

    if workload not in WORKLOADS:
        raise ValueError(f"unknown workload: {workload}")
    if policy not in POLICIES:
        raise ValueError(f"unknown policy: {policy}")

    values = CPU_VALUES[workload]
    if policy == "fit":
        return values["fit"], values["fit"]
    if policy == "best-effort":
        return None, None
    if policy == "request-low":
        return LOW_REQUEST_MILLICORES, None
    if policy == "request-high":
        return values["fit"], None
    if policy == "guaranteed-low":
        return values["low"], values["low"]
    return LOW_REQUEST_MILLICORES, values["low"]


def cells() -> tuple[Cell, ...]:
    """Return all 36 cells in stable identifier order."""

    result: list[Cell] = []
    policies_by_condition = {
        "solo": SOLO_POLICIES,
        "contention": CONTENTION_POLICIES,
    }
    for condition in CONDITIONS:
        for workload in WORKLOADS:
            for policy in policies_by_condition[condition]:
                request, limit = resources_for(workload, policy)
                result.append(
                    Cell(
                        cell_id=f"{condition}-{workload}-{policy}",
                        workload=workload,
                        policy=policy,
                        condition=condition,
                        request_millicores=request,
                        limit_millicores=limit,
                    )
                )
    return tuple(result)


CELLS = cells()


def schedule(blocks: int, seed: int) -> tuple[tuple[Cell, ...], ...]:
    """Return a seeded independent cell order for each complete block."""

    if blocks < 1:
        raise ValueError("blocks must be at least one")

    generator = random.Random(seed)
    return tuple(tuple(generator.sample(CELLS, k=len(CELLS))) for _ in range(blocks))


def validate_design() -> list[str]:
    """Return design violations; an empty list means the design is valid."""

    errors: list[str] = []
    if len(CELLS) != 36:
        errors.append(f"expected 36 cells, found {len(CELLS)}")
    if len({cell.cell_id for cell in CELLS}) != len(CELLS):
        errors.append("cell identifiers are not unique")

    expected_counts = {"solo": 16, "contention": 20}
    for condition, expected in expected_counts.items():
        condition_cells = [cell for cell in CELLS if cell.condition == condition]
        if len(condition_cells) != expected:
            errors.append(
                f"{condition} must contain {expected} cells, found {len(condition_cells)}"
            )

    for cell in CELLS:
        if cell.workload not in WORKLOADS:
            errors.append(f"{cell.cell_id}: unknown workload")
        if cell.policy not in POLICIES:
            errors.append(f"{cell.cell_id}: unknown policy")
        if cell.condition not in CONDITIONS:
            errors.append(f"{cell.cell_id}: unknown condition")
        allowed = SOLO_POLICIES if cell.condition == "solo" else CONTENTION_POLICIES
        if cell.policy not in allowed:
            errors.append(f"{cell.cell_id}: policy is not valid for condition")
        for name, value in (
            ("request", cell.request_millicores),
            ("limit", cell.limit_millicores),
        ):
            if value is not None and value < 0:
                errors.append(f"{cell.cell_id}: negative {name}")
        if (
            cell.request_millicores is not None
            and cell.limit_millicores is not None
            and cell.request_millicores > cell.limit_millicores
        ):
            errors.append(f"{cell.cell_id}: request exceeds limit")
    return errors


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the offline design commands."""

    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--plan", action="store_true", help="print the seeded cell plan"
    )
    action.add_argument(
        "--validate", action="store_true", help="validate the frozen design"
    )
    parser.add_argument("--blocks", type=int, default=1, help="complete blocks to plan")
    parser.add_argument("--seed", type=int, default=20261024, help="schedule seed")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested offline design command."""

    args = parse_args(argv)
    errors = validate_design()
    if args.validate:
        payload = {"valid": not errors, "errors": errors, "cell_count": len(CELLS)}
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        elif errors:
            print("INVALID")
            print("\n".join(errors))
        else:
            print("VALID: 36 cells")
        return 0 if not errors else 1

    try:
        blocks = schedule(args.blocks, args.seed)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    if args.json:
        payload = {
            "seed": args.seed,
            "blocks": [
                {"block": index, "cells": [asdict(cell) for cell in block]}
                for index, block in enumerate(blocks, start=1)
            ],
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        for index, block in enumerate(blocks, start=1):
            for cell in block:
                request = (
                    "none"
                    if cell.request_millicores is None
                    else str(cell.request_millicores)
                )
                limit = (
                    "none"
                    if cell.limit_millicores is None
                    else str(cell.limit_millicores)
                )
                print(
                    f"block-{index:02d} {cell.cell_id} request={request}m limit={limit}m"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
