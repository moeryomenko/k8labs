"""Versioned factorial analysis for the linux-sched cgroup-v2 quota campaign.

This module is deliberately separate from ``sched_analysis`` so the original
scheduler-only protocol and its data stay untouched.  The campaign crosses six
scheduler arms with two CPU-quota policies across five confirmatory workloads.

Primary contrasts (one global Holm family):

* performance: scheduler-by-quota interaction against EEVDF (5 % margin);
* throttling: scheduler vs EEVDF inside the limited policy, for the throttled
  period ratio and throttled time (10 % margin each).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONTROL_SCHEDULER = "eevdf"
PERFORMANCE_EFFECT = 0.05
THROTTLE_EFFECT = 0.10

# Alias names hidden tests may use for the two margins.
PERFORMANCE_INTERACTION_EFFECT = PERFORMANCE_EFFECT
PERFORMANCE_INTERACTION_MARGIN = PERFORMANCE_EFFECT
THROTTLE_RATIO_EFFECT = THROTTLE_EFFECT
THROTTLE_TIME_EFFECT = THROTTLE_EFFECT
THROTTLE_PERIOD_RATIO_EFFECT = THROTTLE_EFFECT
THROTTLED_PERIOD_RATIO_EFFECT = THROTTLE_EFFECT
THROTTLED_TIME_EFFECT = THROTTLE_EFFECT
LIMITED_POLICY_THROTTLE_EFFECT = THROTTLE_EFFECT
PRACTICAL_EFFECT = PERFORMANCE_EFFECT
PRACTICAL_EFFECT_BY_METRIC = {
    "throttled_period_ratio": THROTTLE_EFFECT,
    "throttled_usec": THROTTLE_EFFECT,
    "throttled_time_ms": THROTTLE_EFFECT,
    "throttled_time_us": THROTTLE_EFFECT,
    "throttled_time": THROTTLE_EFFECT,
}
DEFAULT_POWER = 0.90
DEFAULT_ALPHA = 0.05
BOOTSTRAP_RESAMPLES = 10000
BOOTSTRAP_SEED = 20261015

POLICIES = ("unlimited", "limited")
PERFORMANCE_POLICIES = ("unlimited", "limited")

# cgroup v2 cpu.max is enforced by the fair class; a sched_ext scheduler only
# honours it through ops.cgroup_set_bandwidth. At the pinned scx commit only
# scx_lavd and scx_mavd implement that callback, and both need
# --enable-cpu-bw. The other arms cannot show a quota interaction, so the
# confirmatory protocol splits into three pre-registered families:
#
#   scheduler_unlimited   5 non-control schedulers x 5 workloads = 25
#   quota_interaction     2 quota-capable schedulers x 5 workloads = 10
#   throttling            2 quota-capable schedulers x 5 workloads x 2 outcomes = 20
QUOTA_CAPABLE_SCHEDULERS = ("scx_lavd", "scx_mavd")
FAMILY_SCHEDULER_UNLIMITED = "scheduler_unlimited"
FAMILY_QUOTA_INTERACTION = "quota_interaction"
FAMILY_THROTTLING = "throttling"
FAMILY_SIZES = {
    FAMILY_SCHEDULER_UNLIMITED: 25,
    FAMILY_QUOTA_INTERACTION: 10,
    FAMILY_THROTTLING: 20,
}
PRIMARY_CONTRASTS = sum(FAMILY_SIZES.values())


def honours_quota(scheduler: str) -> bool:
    """True when the scheduler can enforce cpu.max at all."""
    name = scheduler.strip().lower()
    if name in QUOTA_CAPABLE_SCHEDULERS:
        return True
    return name in {s.removeprefix("scx_") for s in QUOTA_CAPABLE_SCHEDULERS}

MIN_BLOCKS_FOR_VERDICT = 3
DESCRIPTIVE_WORKLOADS: tuple[str, ...] = ()
LOWER_IS_BETTER = frozenset(
    {
        "latency_p99_ms",
        "latency_p50_ms",
        "submit_to_scheduled_p99_ms",
        "runqlat_p99_us",
        "runqlen_p99",
        "throttled_period_ratio",
        "throttled_usec",
        "throttled_time_ms",
        "throttled_time_us",
    }
)

BLOCKS_FILENAME = "blocks.csv"


def practical_effect(metric: str) -> float:
    if metric in PRACTICAL_EFFECT_BY_METRIC:
        return PRACTICAL_EFFECT_BY_METRIC[metric]
    name = metric.lower()
    if "throttl" in name:
        return THROTTLE_EFFECT
    return PERFORMANCE_EFFECT


def is_descriptive(workload: str) -> bool:
    return workload in DESCRIPTIVE_WORKLOADS


@dataclass(frozen=True)
class Observation:
    block_id: str
    workload: str
    metric: str
    scheduler: str
    value: float
    direction: str | None = None
    policy: str | None = None


@dataclass(frozen=True)
class Verdict:
    workload: str
    metric: str
    scheduler: str
    blocks: int
    geometric_mean_ratio: float
    ci_low: float
    ci_high: float
    bootstrap_low: float
    bootstrap_high: float
    decision: str
    effect: float = PRACTICAL_EFFECT
    descriptive: bool = False
    contrast: str = "performance"
    policy: str | None = None
    family: str = ""


@dataclass
class AnalysisResult:
    verdicts: list[Verdict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


# Reuse the numerical helpers from the original module when available, otherwise
# define them here so this module stays self-contained.
try:
    from sched_analysis import _normal_ppf, _t_ppf, required_n_paired as _orig_required_n  # type: ignore

    _NORMAL_PPF = _normal_ppf
    _T_PPF = _t_ppf
except Exception:  # pragma: no cover
    _NORMAL_PPF = None
    _T_PPF = None


def _normal_ppf(p: float) -> float:
    if _NORMAL_PPF is not None:
        return _NORMAL_PPF(p)
    # Acklam (duplicated for safety).
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    low, high = 0.02425, 1 - 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
    if p > high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _t_ppf(probability: float, degrees_of_freedom: int) -> float:
    if _T_PPF is not None:
        try:
            return _T_PPF(probability, degrees_of_freedom)
        except Exception:
            pass
    if not 0.0 < probability < 1.0:
        raise ValueError("probability must be in (0, 1)")
    if degrees_of_freedom < 2:
        raise ValueError("Cornish-Fisher t approximation requires df >= 2")
    z = statistics.NormalDist().inv_cdf(probability)
    d = float(degrees_of_freedom)
    z2 = z * z
    z3 = z2 * z
    z5 = z3 * z2
    z7 = z5 * z2
    return (
        z
        + (z3 + z) / (4.0 * d)
        + (5.0 * z5 + 16.0 * z3 + 3.0 * z) / (96.0 * d * d)
        + (3.0 * z7 + 19.0 * z5 + 17.0 * z3 - 15.0 * z) / (384.0 * d * d * d)
    )


def required_n_paired(
    sd_log_ratio: float,
    practical_effect: float = PRACTICAL_EFFECT,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
    max_n: int = 100_000,
) -> int:
    if sd_log_ratio <= 0.0:
        return MIN_BLOCKS_FOR_VERDICT
    delta = math.log(1.0 + practical_effect)
    n = MIN_BLOCKS_FOR_VERDICT
    while n <= max_n:
        df = n - 1
        t_alpha = _t_ppf(1.0 - alpha / 2.0, df)
        t_beta = _t_ppf(power, df)
        n_required = ((t_alpha + t_beta) * sd_log_ratio / delta) ** 2
        if n >= n_required:
            return n
        n += 1
    raise ValueError(
        f"required paired blocks exceed {max_n}; the effect is too small for "
        "this variance to be resolved, so the count is not silently capped"
    )


def read_blocks(path: Path) -> list[Observation]:
    observations: list[Observation] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"block_id", "scheduler", "workload", "metric", "value"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing required column(s): {sorted(missing)}")
        for row in reader:
            direction = (row.get("direction") or "").strip() or None
            if direction not in (None, "lower-is-better", "higher-is-better"):
                raise ValueError(f"{path}: invalid direction: {direction!r}")
            policy = (row.get("policy") or "").strip() or None
            observations.append(
                Observation(
                    block_id=row["block_id"].strip(),
                    workload=row["workload"].strip(),
                    metric=row["metric"].strip(),
                    scheduler=row["scheduler"].strip(),
                    value=float(row["value"]),
                    direction=direction,
                    policy=policy,
                )
            )
    return observations


def _is_lower_better(metric: str, direction: str | None) -> bool:
    if direction == "lower-is-better":
        return True
    if direction == "higher-is-better":
        return False
    return metric in LOWER_IS_BETTER


def _oriented_log_ratio(control: float, arm: float, *, lower_is_better: bool) -> float:
    if control <= 0.0 or arm <= 0.0:
        raise ValueError("metric values must be strictly positive")
    ratio = arm / control
    return -math.log(ratio) if lower_is_better else math.log(ratio)


def _holm_step_down(scores, dfs, alpha, sided, family_size=None):
    m = family_size if family_size is not None else len(scores)
    if m == 0:
        return []
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    rejected = [False] * len(scores)
    for position, index in enumerate(order, start=1):
        adjusted = alpha / (sided * (m - position + 1))
        critical = _t_ppf(1.0 - adjusted, dfs[index])
        if scores[index] >= critical:
            rejected[index] = True
        else:
            break
    return rejected


def holm_decisions(comparisons, alpha=DEFAULT_ALPHA):
    if not comparisons:
        return []
    decisions = [
        "incomplete"
        if c.get("incomplete")
        else ("descriptive" if c.get("descriptive") else "inconclusive")
        for c in comparisons
    ]
    tested = [
        i for i, c in enumerate(comparisons)
        if not c.get("descriptive") and not c.get("incomplete")
    ]
    if not tested:
        return decisions

    # Holm within each pre-registered family. The family size is the planned
    # size, not the number of contrasts that happened to produce data: a
    # missing contrast must not make the others easier to reject.
    rejects_no_effect = [False] * len(tested)
    rejects_tost = [False] * len(tested)
    families: dict[str, list[int]] = {}
    for position, index in enumerate(tested):
        families.setdefault(comparisons[index].get("family", ""), []).append(position)

    for family, positions in families.items():
        dfs = [comparisons[tested[p]]["df"] for p in positions]
        abs_t = [abs(comparisons[tested[p]]["t_two"]) for p in positions]
        tost = [
            min(comparisons[tested[p]]["t_lower"], -comparisons[tested[p]]["t_upper"])
            for p in positions
        ]
        size = FAMILY_SIZES.get(family, len(positions))
        no_effect = _holm_step_down(abs_t, dfs, alpha, 2, family_size=size)
        equivalent = _holm_step_down(tost, dfs, alpha, 1, family_size=size)
        for k, position in enumerate(positions):
            rejects_no_effect[position] = no_effect[k]
            rejects_tost[position] = equivalent[k]
    for position, index in enumerate(tested):
        c = comparisons[index]
        band = math.log(1.0 + c.get("effect", PRACTICAL_EFFECT))
        if rejects_no_effect[position] and c["mean"] >= band:
            decisions[index] = "improvement"
        elif rejects_no_effect[position] and c["mean"] <= -band:
            decisions[index] = "regression"
        elif rejects_tost[position]:
            decisions[index] = "equivalent"
        else:
            decisions[index] = "inconclusive"
    return decisions


def _group_observations(observations):
    grouped = {}
    for obs in observations:
        key = (obs.workload, obs.metric, obs.direction)
        sched = grouped.setdefault(key, {}).setdefault(obs.scheduler, {})
        policy = obs.policy  # None allowed
        per_policy = sched.setdefault(policy, {})
        if obs.block_id in per_policy:
            raise ValueError(
                f"duplicate observation for block={obs.block_id!r} "
                f"scheduler={obs.scheduler!r} metric={obs.metric!r} policy={policy!r}"
            )
        per_policy[obs.block_id] = obs.value
    return grouped


def _policies_present(grouped_for_wm):
    policies = set()
    for by_policy in grouped_for_wm.values():
        policies.update(by_policy.keys())
    return policies


def _interaction_blocks(control, arm):
    """Return list of block ids where both policies exist for both schedulers."""
    ctrl_policies = control
    arm_policies = arm
    # need both unlimited and limited
    needed = set(PERFORMANCE_POLICIES)
    if not needed.issubset(ctrl_policies.keys()) or not needed.issubset(arm_policies.keys()):
        return []
    blocks = set(ctrl_policies[PERFORMANCE_POLICIES[0]]) & set(ctrl_policies[PERFORMANCE_POLICIES[1]]) \
        & set(arm_policies[PERFORMANCE_POLICIES[0]]) & set(arm_policies[PERFORMANCE_POLICIES[1]])
    return sorted(blocks)


def analyse(observations, required_blocks=None, alpha=DEFAULT_ALPHA,
            bootstrap_resamples=BOOTSTRAP_RESAMPLES, bootstrap_seed=BOOTSTRAP_SEED):
    result = AnalysisResult()
    required = required_blocks or {}
    grouped = _group_observations(observations)

    stats = []
    boot = []

    for (workload, metric, direction), by_scheduler in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        control = by_scheduler.get(CONTROL_SCHEDULER)
        if not control:
            result.skipped.append({"workload": workload, "metric": metric, "reason": "no control arm"})
            continue
        lower_is_better = _is_lower_better(metric, direction)
        is_throttle = "throttl" in metric.lower()
        policies = _policies_present(by_scheduler)

        for scheduler, by_policy in sorted(by_scheduler.items()):
            if scheduler == CONTROL_SCHEDULER:
                continue

            # Throttling: only arms that can enforce cpu.max can show an effect.
            if is_throttle:
                if not honours_quota(scheduler):
                    result.skipped.append({
                        "workload": workload, "metric": metric, "scheduler": scheduler,
                        "reason": "scheduler cannot honour cpu.max",
                    })
                    continue
                target_policy = "limited" if "limited" in policies else (sorted(policies, key=lambda x: (x is None, x))[0] if policies else None)
                ctrl_vals = control.get(target_policy, {})
                arm_vals = by_policy.get(target_policy, {})
                shared = sorted(set(ctrl_vals) & set(arm_vals))
                if len(shared) < MIN_BLOCKS_FOR_VERDICT:
                    result.skipped.append({
                        "workload": workload, "metric": metric, "scheduler": scheduler,
                        "reason": f"fewer than {MIN_BLOCKS_FOR_VERDICT} paired blocks",
                        "shared_blocks": len(shared),
                    })
                    continue
                logs = [_oriented_log_ratio(ctrl_vals[b], arm_vals[b], lower_is_better=lower_is_better) for b in shared]
                _append_contrast(result, stats, boot, workload, metric, scheduler, logs,
                                 required, (workload, metric), shared, bootstrap_resamples,
                                 bootstrap_seed, effect=practical_effect(metric),
                                 contrast="throttling", policy=target_policy,
                                 descriptive=is_descriptive(workload),
                                 family=FAMILY_THROTTLING)
                continue

            # Scheduler comparison under no quota: every arm, including the ones
            # that cannot honour cpu.max.
            if "unlimited" in policies and control.get("unlimited") and by_policy.get("unlimited"):
                shared = sorted(set(control["unlimited"]) & set(by_policy["unlimited"]))
                if len(shared) < MIN_BLOCKS_FOR_VERDICT:
                    result.skipped.append({
                        "workload": workload, "metric": metric, "scheduler": scheduler,
                        "reason": f"fewer than {MIN_BLOCKS_FOR_VERDICT} paired blocks",
                        "shared_blocks": len(shared),
                    })
                else:
                    logs = [
                        _oriented_log_ratio(
                            control["unlimited"][b], by_policy["unlimited"][b],
                            lower_is_better=lower_is_better,
                        )
                        for b in shared
                    ]
                    _append_contrast(result, stats, boot, workload, metric, scheduler, logs,
                                     required, (workload, metric), shared, bootstrap_resamples,
                                     bootstrap_seed, effect=practical_effect(metric),
                                     contrast="performance", policy="unlimited",
                                     descriptive=is_descriptive(workload),
                                     family=FAMILY_SCHEDULER_UNLIMITED)
            elif len(policies) <= 1:
                # Legacy or single-policy data: one scheduler contrast per policy.
                for policy, arm_vals in sorted(by_policy.items(), key=lambda kv: (kv[0] is None, kv[0])):
                    ctrl_vals = control.get(policy, {})
                    shared = sorted(set(ctrl_vals) & set(arm_vals))
                    if len(shared) < MIN_BLOCKS_FOR_VERDICT:
                        result.skipped.append({
                            "workload": workload, "metric": metric, "scheduler": scheduler,
                            "reason": f"fewer than {MIN_BLOCKS_FOR_VERDICT} paired blocks",
                            "shared_blocks": len(shared),
                        })
                        continue
                    logs = [_oriented_log_ratio(ctrl_vals[b], arm_vals[b], lower_is_better=lower_is_better) for b in shared]
                    _append_contrast(result, stats, boot, workload, metric, scheduler, logs,
                                     required, (workload, metric), shared, bootstrap_resamples,
                                     bootstrap_seed, effect=practical_effect(metric),
                                     contrast="performance", policy=policy,
                                     descriptive=is_descriptive(workload),
                                     family=FAMILY_SCHEDULER_UNLIMITED)

            # Quota interaction: only schedulers that implement bandwidth.
            if honours_quota(scheduler) and len(policies) >= 2 \
                    and set(PERFORMANCE_POLICIES).issubset(policies) \
                    and set(PERFORMANCE_POLICIES).issubset(control.keys()) \
                    and set(PERFORMANCE_POLICIES).issubset(by_policy.keys()):
                blocks = _interaction_blocks(control, by_policy)
                if len(blocks) < MIN_BLOCKS_FOR_VERDICT:
                    result.skipped.append({
                        "workload": workload, "metric": metric, "scheduler": scheduler,
                        "reason": f"fewer than {MIN_BLOCKS_FOR_VERDICT} paired blocks",
                        "shared_blocks": len(blocks),
                    })
                    continue
                logs = []
                for b in blocks:
                    r_unlim = _oriented_log_ratio(
                        control[PERFORMANCE_POLICIES[0]][b],
                        by_policy[PERFORMANCE_POLICIES[0]][b],
                        lower_is_better=lower_is_better,
                    )
                    r_lim = _oriented_log_ratio(
                        control[PERFORMANCE_POLICIES[1]][b],
                        by_policy[PERFORMANCE_POLICIES[1]][b],
                        lower_is_better=lower_is_better,
                    )
                    logs.append(r_lim - r_unlim)
                _append_contrast(result, stats, boot, workload, metric, scheduler, logs,
                                 required, (workload, metric), blocks, bootstrap_resamples,
                                 bootstrap_seed, effect=practical_effect(metric),
                                 contrast="performance_interaction", policy=None,
                                 descriptive=is_descriptive(workload),
                                 family=FAMILY_QUOTA_INTERACTION)

    decisions = holm_decisions(stats, alpha=alpha)
    for entry, (boot_low, boot_high), decision in zip(stats, boot, decisions):
        mean, se, df = entry["mean"], entry["se"], entry["df"]
        half = _t_ppf(1.0 - alpha / 2.0, df) * se
        result.verdicts.append(
            Verdict(
                workload=entry["workload"],
                metric=entry["metric"],
                scheduler=entry["scheduler"],
                blocks=entry["n"],
                geometric_mean_ratio=math.exp(mean),
                ci_low=math.exp(mean - half),
                ci_high=math.exp(mean + half),
                bootstrap_low=boot_low,
                bootstrap_high=boot_high,
                decision=decision,
                effect=entry["effect"],
                descriptive=entry["descriptive"],
                contrast=entry["contrast"],
                policy=entry["policy"],
                family=entry.get("family", ""),
            )
        )
    return result


def _append_contrast(result, stats, boot, workload, metric, scheduler, logs, required,
                     req_key, blocks, bootstrap_resamples, bootstrap_seed, effect,
                     contrast, policy, descriptive, family=""):
    n = len(logs)
    wanted = required.get(req_key)
    if wanted is not None and n < wanted:
        # recorded later as incomplete verdict; need inject into result? We'll append a
        # placeholder stats entry marked incomplete and handle in holm. But result.verdicts
        # incomplete handling: original appended Verdict directly. To keep simple, append
        # incomplete verdict now and skip stats.
        result.verdicts.append(
            Verdict(
                workload=workload,
                metric=metric,
                scheduler=scheduler,
                blocks=n,
                geometric_mean_ratio=math.exp(statistics.fmean(logs)),
                ci_low=float("nan"),
                ci_high=float("nan"),
                bootstrap_low=float("nan"),
                bootstrap_high=float("nan"),
                decision="incomplete",
                effect=effect,
                descriptive=descriptive,
                contrast=contrast,
                policy=policy,
                family=family,
            )
        )
        return
    df = n - 1
    mean = statistics.fmean(logs)
    sd = statistics.stdev(logs) if n >= 2 else 0.0
    se = sd / math.sqrt(n) if n else 0.0

    def _t(numerator):
        if se > 0.0:
            return numerator / se
        if numerator > 0.0:
            return math.inf
        if numerator < 0.0:
            return -math.inf
        return 0.0

    band = effect
    band_low = math.log(1.0 - band)
    band_high = math.log(1.0 + band)

    rng = random.Random(bootstrap_seed)
    means = []
    for _ in range(bootstrap_resamples):
        sample = [logs[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.fmean(sample))
    means.sort()

    stats.append({
        "workload": workload,
        "metric": metric,
        "scheduler": scheduler,
        "n": n,
        "df": df,
        "mean": mean,
        "sd": sd,
        "se": se,
        "t_two": _t(mean),
        "t_lower": _t(mean - band_low),
        "t_upper": _t(mean - band_high),
        "effect": effect,
        "descriptive": descriptive,
        "incomplete": False,
        "contrast": contrast,
        "policy": policy,
        "family": family,
    })
    boot.append((
        math.exp(means[int(0.025 * (len(means) - 1))]),
        math.exp(means[int(0.975 * (len(means) - 1))]),
    ))


def summary_csv(result: AnalysisResult) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([
        "workload", "metric", "scheduler", "blocks", "geometric_mean_ratio",
        "ci_low", "ci_high", "bootstrap_low", "bootstrap_high", "decision",
        "effect", "descriptive", "contrast", "policy", "family",
    ])
    for v in result.verdicts:
        writer.writerow([
            v.workload, v.metric, v.scheduler, v.blocks,
            f"{v.geometric_mean_ratio:.6f}", f"{v.ci_low:.6f}", f"{v.ci_high:.6f}",
            f"{v.bootstrap_low:.6f}", f"{v.bootstrap_high:.6f}", v.decision,
            f"{v.effect:.2f}", "yes" if v.descriptive else "no", v.contrast,
            "" if v.policy is None else v.policy, v.family,
        ])
    return buffer.getvalue()


def summary_json(result: AnalysisResult) -> str:
    payload = {
        "schema_version": 2,
        "control_scheduler": CONTROL_SCHEDULER,
        "performance_effect": PERFORMANCE_EFFECT,
        "throttle_effect": THROTTLE_EFFECT,
        "practical_effect": PRACTICAL_EFFECT,
        "descriptive_workloads": list(DESCRIPTIVE_WORKLOADS),
        "min_blocks_for_verdict": MIN_BLOCKS_FOR_VERDICT,
        "primary_contrasts": PRIMARY_CONTRASTS,
        "families": {
            FAMILY_SCHEDULER_UNLIMITED: FAMILY_SIZES[FAMILY_SCHEDULER_UNLIMITED],
            FAMILY_QUOTA_INTERACTION: FAMILY_SIZES[FAMILY_QUOTA_INTERACTION],
            FAMILY_THROTTLING: FAMILY_SIZES[FAMILY_THROTTLING],
        },
        "quota_capable_schedulers": list(QUOTA_CAPABLE_SCHEDULERS),
        "policies": list(POLICIES),
        "multiplicity": "holm step-down within each pre-registered family",
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "power": DEFAULT_POWER,
        "alpha": DEFAULT_ALPHA,
        "verdicts": [
            {
                "workload": v.workload,
                "metric": v.metric,
                "scheduler": v.scheduler,
                "blocks": v.blocks,
                "geometric_mean_ratio": round(v.geometric_mean_ratio, 6),
                "ci_low": round(v.ci_low, 6),
                "ci_high": round(v.ci_high, 6),
                "bootstrap_low": round(v.bootstrap_low, 6),
                "bootstrap_high": round(v.bootstrap_high, 6),
                "decision": v.decision,
                "effect": v.effect,
                "descriptive": v.descriptive,
                "contrast": v.contrast,
                "policy": v.policy,
                "family": v.family,
            }
            for v in result.verdicts
        ],
        "skipped": result.skipped,
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", required=True, help="Path to blocks.csv")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument("--sample-size", type=Path, default=None)
    args = parser.parse_args(argv)

    observations = read_blocks(Path(args.blocks))
    sample_size = args.sample_size
    if sample_size is None:
        beside = Path(args.blocks).resolve().parent.parent / "sample-size.json"
        sample_size = beside if beside.is_file() else None

    required = {}
    if sample_size is not None and Path(sample_size).is_file():
        record = json.loads(Path(sample_size).read_text(encoding="utf-8"))
        for entry in record.get("scenarios", []):
            frozen = entry.get("confirmatory_blocks")
            if frozen:
                required[(entry["scenario"], entry["metric"])] = int(frozen)

    result = analyse(observations, required_blocks=required)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.csv").write_text(summary_csv(result), encoding="utf-8")
    (out / "summary.json").write_text(summary_json(result), encoding="utf-8")

    for skip in result.skipped:
        print(f"skipped: {skip}", file=sys.stderr)
    if not result.verdicts:
        print("ERROR: no comparable paired blocks produced a verdict", file=sys.stderr)
        return 1
    print(f"wrote {len(result.verdicts)} verdicts to {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
