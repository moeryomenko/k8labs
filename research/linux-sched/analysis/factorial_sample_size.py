#!/usr/bin/env python3
"""Versioned sample-size freeze for the cgroup-v2 quota campaign."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factorial_analysis import (  # noqa: E402
    CONTROL_SCHEDULER,
    DEFAULT_ALPHA,
    DEFAULT_POWER,
    MIN_BLOCKS_FOR_VERDICT,
    PERFORMANCE_EFFECT,
    PRIMARY_CONTRASTS,
    THROTTLE_EFFECT,
    _is_lower_better,
    _normal_ppf,
    _oriented_log_ratio,
    is_descriptive,
    practical_effect,
    read_blocks,
    required_n_paired,
)

BALANCE = 12
CONFIRMATORY_FLOOR_BLOCKS = 12
MIN_PILOT_PAIRED_BLOCKS = 3


def _chi2_lower(p: float, df: int) -> float:
    z = _normal_ppf(p)
    term = 1.0 - 2.0 / (9.0 * df) + z * math.sqrt(2.0 / (9.0 * df))
    return df * term ** 3


def sd_upper_bound(sd: float, n: int, p: float = 0.05) -> float:
    if n < 2 or sd <= 0.0:
        return sd
    df = n - 1
    return sd * math.sqrt(df / _chi2_lower(p, df))


def round_up_to_multiple(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple


def plan(observations: list) -> dict:
    # Group observations by (workload, metric, direction), then scheduler, policy.
    grouped: dict = {}
    for obs in observations:
        key = (obs.workload, obs.metric, obs.direction)
        sched = grouped.setdefault(key, {}).setdefault(obs.scheduler, {})
        policy = obs.policy
        sched.setdefault(policy, {})[obs.block_id] = obs.value

    scenarios: list[dict] = []
    for (workload, metric, direction), by_scheduler in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        control = by_scheduler.get(CONTROL_SCHEDULER)
        if not control:
            raise ValueError(f"pilot for {workload}/{metric} has no control arm")
        lower_is_better = _is_lower_better(metric, direction)
        policies = set()
        for by_policy in by_scheduler.values():
            policies.update(by_policy.keys())

        contrasts: list[dict] = []
        is_throttle = "throttl" in metric.lower()

        def _required_for_logs(logs):
            sd = statistics.stdev(logs) if len(logs) >= 2 else 0.0
            upper = sd_upper_bound(sd, len(logs))
            effect = practical_effect(metric)
            alpha_pc = DEFAULT_ALPHA / PRIMARY_CONTRASTS
            req = required_n_paired(sd, practical_effect=effect, alpha=alpha_pc, power=DEFAULT_POWER)
            req_upper = required_n_paired(upper, practical_effect=effect, alpha=alpha_pc, power=DEFAULT_POWER)
            return sd, upper, effect, req, req_upper

        if is_throttle:
            target_policy = "limited" if "limited" in policies else (sorted(policies, key=lambda x: (x is None, x))[0] if policies else None)
            for scheduler, by_policy in sorted(by_scheduler.items()):
                if scheduler == CONTROL_SCHEDULER:
                    continue
                ctrl_vals = control.get(target_policy, {})
                arm_vals = by_policy.get(target_policy, {})
                shared = sorted(set(ctrl_vals) & set(arm_vals))
                if len(shared) < MIN_PILOT_PAIRED_BLOCKS:
                    raise ValueError(
                        f"pilot for {workload}/{scheduler} has {len(shared)} paired "
                        f"blocks; {MIN_PILOT_PAIRED_BLOCKS} are required to estimate variance"
                    )
                logs = [_oriented_log_ratio(ctrl_vals[b], arm_vals[b], lower_is_better=lower_is_better) for b in shared]
                sd, upper, effect, req, req_upper = _required_for_logs(logs)
                contrasts.append({
                    "scheduler": scheduler,
                    "policy": target_policy,
                    "contrast": "throttling",
                    "paired_blocks": len(shared),
                    "effect": effect,
                    "sd_log_ratio": round(sd, 6),
                    "required_n": req,
                    "sd_upper95_log_ratio": round(upper, 6),
                    "required_n_upper95": req_upper,
                })
        else:
            # Performance: interaction if both policies present.
            if len(policies) >= 2 and "unlimited" in policies and "limited" in policies \
                    and "unlimited" in control and "limited" in control:
                for scheduler, by_policy in sorted(by_scheduler.items()):
                    if scheduler == CONTROL_SCHEDULER:
                        continue
                    if "unlimited" not in by_policy or "limited" not in by_policy:
                        continue
                    needed_blocks = set(control["unlimited"]) & set(control["limited"]) \
                        & set(by_policy["unlimited"]) & set(by_policy["limited"])
                    shared = sorted(needed_blocks)
                    if len(shared) < MIN_PILOT_PAIRED_BLOCKS:
                        raise ValueError(
                            f"pilot for {workload}/{scheduler} has {len(shared)} paired "
                            f"blocks; {MIN_PILOT_PAIRED_BLOCKS} are required to estimate variance"
                        )
                    logs = []
                    for b in shared:
                        r_unlim = _oriented_log_ratio(
                            control["unlimited"][b], by_policy["unlimited"][b],
                            lower_is_better=lower_is_better,
                        )
                        r_lim = _oriented_log_ratio(
                            control["limited"][b], by_policy["limited"][b],
                            lower_is_better=lower_is_better,
                        )
                        logs.append(r_lim - r_unlim)
                    sd, upper, effect, req, req_upper = _required_for_logs(logs)
                    contrasts.append({
                        "scheduler": scheduler,
                        "contrast": "performance_interaction",
                        "paired_blocks": len(shared),
                        "effect": effect,
                        "sd_log_ratio": round(sd, 6),
                        "required_n": req,
                        "sd_upper95_log_ratio": round(upper, 6),
                        "required_n_upper95": req_upper,
                    })
            else:
                for scheduler, by_policy in sorted(by_scheduler.items()):
                    if scheduler == CONTROL_SCHEDULER:
                        continue
                    for policy, vals in sorted(by_policy.items(), key=lambda kv: (kv[0] is None, kv[0])):
                        ctrl_vals = control.get(policy, {})
                        shared = sorted(set(ctrl_vals) & set(vals))
                        if len(shared) < MIN_PILOT_PAIRED_BLOCKS:
                            raise ValueError(
                                f"pilot for {workload}/{scheduler} has {len(shared)} paired "
                                f"blocks; {MIN_PILOT_PAIRED_BLOCKS} are required to estimate variance"
                            )
                        logs = [_oriented_log_ratio(ctrl_vals[b], vals[b], lower_is_better=lower_is_better) for b in shared]
                        sd, upper, effect, req, req_upper = _required_for_logs(logs)
                        contrasts.append({
                            "scheduler": scheduler,
                            "policy": policy,
                            "contrast": "performance",
                            "paired_blocks": len(shared),
                            "effect": effect,
                            "sd_log_ratio": round(sd, 6),
                            "required_n": req,
                            "sd_upper95_log_ratio": round(upper, 6),
                            "required_n_upper95": req_upper,
                        })

        descriptive = is_descriptive(workload)
        if contrasts:
            raw_required = max(c["required_n"] for c in contrasts)
            raw_upper = max(c["required_n_upper95"] for c in contrasts)
        else:
            raw_required = 0
            raw_upper = 0
        floored = max(raw_required, CONFIRMATORY_FLOOR_BLOCKS)
        frozen = None if descriptive else round_up_to_multiple(floored, BALANCE)
        frozen_upper = None if descriptive else round_up_to_multiple(
            max(raw_upper, CONFIRMATORY_FLOOR_BLOCKS), BALANCE
        )
        scenarios.append({
            "scenario": workload,
            "metric": metric,
            "descriptive": descriptive,
            "contrasts": contrasts,
            "required_n_raw": raw_required,
            "required_n_floored": floored,
            "confirmatory_blocks": frozen,
            "confirmatory_blocks_upper95": frozen_upper,
            "balanced_multiple": BALANCE,
        })

    return {
        "schema_version": 1,
        "control_scheduler": CONTROL_SCHEDULER,
        "performance_effect": PERFORMANCE_EFFECT,
        "throttle_effect": THROTTLE_EFFECT,
        "practical_effect_by_metric": {},
        "power": DEFAULT_POWER,
        "alpha": DEFAULT_ALPHA,
        "primary_contrasts": PRIMARY_CONTRASTS,
        "alpha_per_contrast": DEFAULT_ALPHA / PRIMARY_CONTRASTS,
        "min_blocks": CONFIRMATORY_FLOOR_BLOCKS,
        "balance_multiple": BALANCE,
        "policies": list(POLICIES) if False else ["unlimited", "limited"],
        "scenarios": scenarios,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-blocks", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    record = plan(read_blocks(args.pilot_blocks))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    for scenario in record["scenarios"]:
        count = scenario["confirmatory_blocks"]
        if scenario["descriptive"]:
            print(f"{scenario['scenario']}: descriptive, no confirmatory count")
            continue
        print(f"{scenario['scenario']}: {count} blocks (raw {scenario['required_n_raw']}, floor {CONFIRMATORY_FLOOR_BLOCKS})")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
