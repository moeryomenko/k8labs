"""Freeze conservative confirmatory block counts from pilot observations.

The calculation uses paired log ratios, 90 percent power, and a one-sided
95 percent upper confidence bound for the pilot standard deviation. A count
above six is retained as the requirement and marked underpowered after capping.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path

from analysis.analyze import FAMILIES, load_campaign

POWER = 0.90
SD_UPPER_CONFIDENCE = 0.95
MAX_CONFIRMATORY_BLOCKS = 6
MIN_BLOCKS = 3


def _chi_square_lower_quantile(probability: float, degrees_of_freedom: int) -> float:
    """Return a deterministic Wilson-Hilferty lower chi-square quantile."""

    if not 0.0 < probability < 1.0:
        raise ValueError("probability must be between zero and one")
    if degrees_of_freedom < 1:
        raise ValueError("degrees of freedom must be positive")
    z = statistics.NormalDist().inv_cdf(probability)
    degrees = float(degrees_of_freedom)
    approximation = (
        degrees
        * (1.0 - 2.0 / (9.0 * degrees) + z * math.sqrt(2.0 / (9.0 * degrees))) ** 3
    )
    return max(approximation, 1e-12)


def sd_upper_bound(
    log_ratios: Sequence[float], confidence: float = SD_UPPER_CONFIDENCE
) -> float:
    """Return the one-sided upper confidence bound for a sample SD."""

    if len(log_ratios) < MIN_BLOCKS:
        raise ValueError("pilot variance needs at least three paired blocks")
    variance = statistics.variance(log_ratios)
    lower = _chi_square_lower_quantile(1.0 - confidence, len(log_ratios) - 1)
    return math.sqrt((len(log_ratios) - 1) * variance / lower)


def required_blocks(sd_upper: float, margin: float, planned_family_size: int) -> int:
    """Return the unbounded one-sided block requirement for the frozen margin."""

    if sd_upper < 0.0 or not math.isfinite(sd_upper):
        raise ValueError("standard deviation bound must be finite and non-negative")
    if not 0.0 < margin < 1.0:
        raise ValueError("margin must be between zero and one")
    if planned_family_size < 1:
        raise ValueError("planned family size must be positive")
    if sd_upper == 0.0:
        return MIN_BLOCKS
    alpha = 0.05 / planned_family_size
    normal = statistics.NormalDist()
    delta = math.log1p(margin)
    estimate = (
        (normal.inv_cdf(1.0 - alpha) + normal.inv_cdf(POWER)) * sd_upper / delta
    ) ** 2
    return max(MIN_BLOCKS, math.ceil(estimate))


def _value(row: Mapping[str, object], family: str) -> tuple[float | None, bool]:
    if family == "throttle_gap":
        value = row.get("throttled_period_ratio")
        return (float(value), False) if value is not None else (None, False)
    if (
        row.get("workload") in {"io-bound", "hybrid"}
        and row.get("latency_p99_ms") is not None
    ):
        return float(row["latency_p99_ms"]), True
    value = row.get("throughput")
    return (float(value), False) if value is not None else (None, False)


def estimate_sample_sizes(
    pilot_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Estimate all declared families and retain incomplete pilot contrasts."""

    scenarios: list[dict[str, object]] = []
    workloads = sorted({str(row["workload"]) for row in pilot_rows})
    for (
        family,
        control_policy,
        treatment_policy,
        condition,
        margin,
        planned_size,
    ) in FAMILIES:
        for workload in workloads:
            control: dict[str, float] = {}
            treatment: dict[str, float] = {}
            lower_is_better = False
            for row in pilot_rows:
                if row.get("workload") != workload or row.get("condition") != condition:
                    continue
                value, lower = _value(row, family)
                lower_is_better = lower
                if value is None or value <= 0.0:
                    continue
                block_id = str(row["block_id"])
                if row.get("policy") == control_policy:
                    control[block_id] = value
                elif row.get("policy") == treatment_policy:
                    treatment[block_id] = value
            all_blocks = sorted(set(control) | set(treatment))
            shared = sorted(set(control) & set(treatment))
            missing = sorted(set(all_blocks) - set(shared))
            entry: dict[str, object] = {
                "family": family,
                "workload": workload,
                "control": control_policy,
                "treatment": treatment_policy,
                "condition": condition,
                "margin": margin,
                "planned_family_size": planned_size,
                "paired_blocks": shared,
                "missing_blocks": missing,
                "power": POWER,
                "sd_upper_confidence": SD_UPPER_CONFIDENCE,
            }
            if missing or len(shared) < MIN_BLOCKS:
                entry.update(
                    {
                        "status": "incomplete",
                        "required_blocks": None,
                        "confirmatory_blocks": None,
                        "underpowered": False,
                    }
                )
                scenarios.append(entry)
                continue
            logs = [math.log(treatment[block] / control[block]) for block in shared]
            if lower_is_better:
                logs = [-value for value in logs]
            upper = sd_upper_bound(logs)
            required = required_blocks(upper, margin, planned_size)
            entry.update(
                {
                    "status": "complete",
                    "sd_upper": upper,
                    "required_blocks": required,
                    "confirmatory_blocks": min(required, MAX_CONFIRMATORY_BLOCKS),
                    "underpowered": required > MAX_CONFIRMATORY_BLOCKS,
                }
            )
            scenarios.append(entry)
    return {
        "protocol": "cpu-limits-v1",
        "phase": "pilot",
        "status": "PASS"
        if all(item["status"] == "complete" for item in scenarios)
        else "FAIL",
        "power": POWER,
        "sd_upper_confidence": SD_UPPER_CONFIDENCE,
        "cap": MAX_CONFIRMATORY_BLOCKS,
        "scenarios": scenarios,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=Path, required=True)
    parser.add_argument("--cgroups", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    cells = load_campaign(
        args.blocks, args.cgroups, args.metrics, expected_phase="pilot"
    )
    result = estimate_sample_sizes(cells)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(result['scenarios'])} sample-size records")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
