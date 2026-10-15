"""Deterministic paired analysis for the linux-sched research series.

The estimator is the one frozen in the approved plan:

* one *paired block* runs a workload scenario once per scheduler arm on the
  same worker VM, host, guest image, and kernel;
* per block a scheduler/control ratio is formed for the workload primary
  outcome, oriented so positive means improvement;
* the reported point estimate is the geometric mean paired ratio
  (back-transformed mean log ratio) with a 95% paired-t interval on log
  ratios, plus a paired block-bootstrap interval as a sensitivity analysis.

The module is standard library only and never embeds timestamps, so a rerun
over the same input produces byte-identical output.
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
PRACTICAL_EFFECT = 0.05
DEFAULT_POWER = 0.80
DEFAULT_ALPHA = 0.05
BOOTSTRAP_RESAMPLES = 10000
BOOTSTRAP_SEED = 20261015

# The t-quantile approximation below is only claimed accurate for df >= 2, so
# an interval-based verdict needs at least three paired blocks. The plan's
# confirmatory minimum is five; this is the hard floor for reporting anything.
MIN_BLOCKS_FOR_VERDICT = 3

# Fallback metric directions, used only when the input carries no explicit
# ``direction`` column. Scenarios always emit their own direction.
LOWER_IS_BETTER = frozenset(
    {
        "latency_p99_ms",
        "latency_p50_ms",
        "submit_to_scheduled_p99_ms",
        "runqlat_p99_us",
        "runqlen_p99",
    }
)

BLOCKS_FILENAME = "blocks.csv"


@dataclass(frozen=True)
class Observation:
    """One primary-outcome measurement for one scheduler in one block."""

    block_id: str
    workload: str
    metric: str
    scheduler: str
    value: float
    direction: str | None = None


@dataclass(frozen=True)
class Verdict:
    """Decision-rule output for one (workload, metric, scheduler) comparison."""

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


@dataclass
class AnalysisResult:
    """Verdicts plus everything that was skipped, so nothing fails silently."""

    verdicts: list[Verdict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _t_ppf(probability: float, degrees_of_freedom: int) -> float:
    """Student-t quantile via the Cornish-Fisher expansion around the normal.

    Accurate to well within the third decimal for df >= 2, which is the range
    this series uses, and avoids a scipy dependency in the core estimator.
    """

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
    max_n: int = 5000,
) -> int:
    """Required paired-block count for a two-sided paired t test.

    Iterates the t degrees of freedom to convergence, as the plan requires.
    Returns at least the frozen minimum so the floor is never undercut.
    """

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
    return max_n


def read_blocks(path: Path) -> list[Observation]:
    """Load observations from a blocks CSV.

    Required columns: ``block_id,scheduler,workload,metric,value``. The
    optional ``direction`` column (``lower-is-better`` / ``higher-is-better``)
    overrides the built-in metric-direction fallback.
    """

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
            observations.append(
                Observation(
                    block_id=row["block_id"].strip(),
                    workload=row["workload"].strip(),
                    metric=row["metric"].strip(),
                    scheduler=row["scheduler"].strip(),
                    value=float(row["value"]),
                    direction=direction,
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
    """Log ratio oriented so a positive value always means improvement."""

    if control <= 0.0 or arm <= 0.0:
        raise ValueError("metric values must be strictly positive")
    ratio = arm / control
    return -math.log(ratio) if lower_is_better else math.log(ratio)


def _decision(ci_low_log: float, ci_high_log: float, point_log: float) -> str:
    """Apply the frozen decision rule to an oriented LOG-ratio interval.

    All three arguments are on the log scale (the interval bounds of the mean
    log ratio and the mean log ratio itself). Mixing ratio-scale bounds with
    log-scale thresholds is the classic way to make a decision rule that can
    never return ``regression``.
    """

    band = math.log(1.0 + PRACTICAL_EFFECT)
    if ci_low_log > 0.0 and point_log >= band:
        return "improvement"
    if ci_high_log < 0.0 and point_log <= -band:
        return "regression"
    if -band <= ci_low_log and ci_high_log <= band:
        return "equivalent"
    return "inconclusive"


def analyse(
    observations: list[Observation],
    alpha: float = DEFAULT_ALPHA,
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> AnalysisResult:
    """Produce one verdict per (workload, metric, non-control scheduler).

    Duplicate (block, scheduler) observations within a comparison are a hard
    error: silently keeping the last value would fabricate data. Comparisons
    that cannot be evaluated are recorded in ``skipped`` instead of vanishing.
    """

    result = AnalysisResult()
    grouped: dict[tuple[str, str, str | None], dict[str, dict[str, float]]] = {}
    for obs in observations:
        key = (obs.workload, obs.metric, obs.direction)
        per_scheduler = grouped.setdefault(key, {}).setdefault(obs.scheduler, {})
        if obs.block_id in per_scheduler:
            raise ValueError(
                f"duplicate observation for block={obs.block_id!r} "
                f"scheduler={obs.scheduler!r} metric={obs.metric!r}"
            )
        per_scheduler[obs.block_id] = obs.value

    for (workload, metric, direction), by_scheduler in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        control = by_scheduler.get(CONTROL_SCHEDULER)
        lower_is_better = _is_lower_better(metric, direction)
        if not control:
            result.skipped.append(
                {"workload": workload, "metric": metric, "reason": "no control arm"}
            )
            continue
        for scheduler, values in sorted(by_scheduler.items()):
            if scheduler == CONTROL_SCHEDULER:
                continue
            shared = sorted(set(control) & set(values))
            if len(shared) < MIN_BLOCKS_FOR_VERDICT:
                result.skipped.append(
                    {
                        "workload": workload,
                        "metric": metric,
                        "scheduler": scheduler,
                        "reason": f"fewer than {MIN_BLOCKS_FOR_VERDICT} paired blocks",
                        "shared_blocks": len(shared),
                    }
                )
                continue
            logs = [
                _oriented_log_ratio(
                    control[b], values[b], lower_is_better=lower_is_better
                )
                for b in shared
            ]
            n = len(logs)
            mean = statistics.fmean(logs)
            sd = statistics.stdev(logs)
            half = _t_ppf(1.0 - alpha / 2.0, n - 1) * sd / math.sqrt(n)

            rng = random.Random(bootstrap_seed)
            means: list[float] = []
            for _ in range(bootstrap_resamples):
                sample = [logs[rng.randrange(n)] for _ in range(n)]
                means.append(statistics.fmean(sample))
            means.sort()
            lo_idx = int(0.025 * (len(means) - 1))
            hi_idx = int(0.975 * (len(means) - 1))

            result.verdicts.append(
                Verdict(
                    workload=workload,
                    metric=metric,
                    scheduler=scheduler,
                    blocks=n,
                    geometric_mean_ratio=math.exp(mean),
                    ci_low=math.exp(mean - half),
                    ci_high=math.exp(mean + half),
                    bootstrap_low=math.exp(means[lo_idx]),
                    bootstrap_high=math.exp(means[hi_idx]),
                    decision=_decision(mean - half, mean + half, mean),
                )
            )
    return result


def summary_csv(result: AnalysisResult) -> str:
    """Render verdicts as deterministic CSV."""

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "workload",
            "metric",
            "scheduler",
            "blocks",
            "geometric_mean_ratio",
            "ci_low",
            "ci_high",
            "bootstrap_low",
            "bootstrap_high",
            "decision",
        ]
    )
    for v in result.verdicts:
        writer.writerow(
            [
                v.workload,
                v.metric,
                v.scheduler,
                v.blocks,
                f"{v.geometric_mean_ratio:.6f}",
                f"{v.ci_low:.6f}",
                f"{v.ci_high:.6f}",
                f"{v.bootstrap_low:.6f}",
                f"{v.bootstrap_high:.6f}",
                v.decision,
            ]
        )
    return buffer.getvalue()


def summary_json(result: AnalysisResult) -> str:
    """Render verdicts as deterministic, schema-versioned JSON."""

    payload = {
        "schema_version": 1,
        "control_scheduler": CONTROL_SCHEDULER,
        "practical_effect": PRACTICAL_EFFECT,
        "min_blocks_for_verdict": MIN_BLOCKS_FOR_VERDICT,
        "lower_is_better_fallback": sorted(LOWER_IS_BETTER),
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_seed": BOOTSTRAP_SEED,
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
            }
            for v in result.verdicts
        ],
        "skipped": result.skipped,
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", required=True, help="Path to blocks.csv")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    args = parser.parse_args(argv)

    observations = read_blocks(Path(args.blocks))
    result = analyse(observations)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.csv").write_text(summary_csv(result), encoding="utf-8")
    (out / "summary.json").write_text(summary_json(result), encoding="utf-8")

    for skip in result.skipped:
        print(f"skipped: {skip}", file=sys.stderr)
    if not result.verdicts:
        print(
            "ERROR: no comparable paired blocks produced a verdict "
            "(see skipped entries above)",
            file=sys.stderr,
        )
        return 1
    print(f"wrote {len(result.verdicts)} verdicts to {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
