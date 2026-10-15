"""Placement analysis for the kube-sched research series.

Consumes a `pods.csv` produced by the placement runner (one row per submitted
Pod) and reports, per paired block and profile:

* ``submit_to_scheduled_p99_ms`` — p99 of ``scheduled_ms - created_ms`` over the
  Pods that bound (lower is better);
* ``correct_placement_rate`` — fraction of submitted Pods whose placement was
  correct, i.e. fit Pods bound to a constraint-satisfying node and the
  deliberately-unschedulable Pods stayed Pending (higher is better).

Non-control profiles are compared to the control with a paired log-ratio and a
95% paired-t interval, using the same decision rule as linux-sched. Pure
standard library and timestamp-free so reruns are byte-identical.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONTROL_PROFILE = "least-allocated"
PRACTICAL_EFFECT = 0.05
# The t-quantile approximation is only claimed for df >= 2, so an interval
# verdict needs at least three paired blocks.
MIN_BLOCKS_FOR_VERDICT = 3
LOWER_IS_BETTER = {"submit_to_scheduled_p99_ms"}


@dataclass(frozen=True)
class PodRow:
    block_id: str
    profile: str
    pod: str
    latency_ms: float
    unschedulable: bool
    correct: bool


@dataclass(frozen=True)
class BlockMetric:
    block_id: str
    profile: str
    metric: str
    value: float


@dataclass
class ComparisonResult:
    comparisons: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _t_ppf(probability: float, degrees_of_freedom: int) -> float:
    if degrees_of_freedom < 2:
        raise ValueError("Cornish-Fisher t approximation requires df >= 2")
    z = statistics.NormalDist().inv_cdf(probability)
    d = float(degrees_of_freedom)
    z2, z3 = z * z, z * z * z
    z5, z7 = z3 * z2, z3 * z2 * z2
    return (
        z
        + (z3 + z) / (4.0 * d)
        + (5.0 * z5 + 16.0 * z3 + 3.0 * z) / (96.0 * d * d)
        + (3.0 * z7 + 19.0 * z5 + 17.0 * z3 - 15.0 * z) / (384.0 * d * d * d)
    )


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (pct in [0, 100])."""

    if not values:
        raise ValueError("percentile of empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    frac = rank - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def read_pods(path: Path) -> list[PodRow]:
    rows: list[PodRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {
            "block_id",
            "profile",
            "pod",
            "latency_ms",
            "unschedulable",
            "correct",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing required column(s): {sorted(missing)}")
        for row in reader:
            rows.append(
                PodRow(
                    block_id=row["block_id"].strip(),
                    profile=row["profile"].strip(),
                    pod=row["pod"].strip(),
                    latency_ms=float(row["latency_ms"]),
                    unschedulable=row["unschedulable"].strip().lower()
                    in {"1", "true", "yes"},
                    correct=row["correct"].strip().lower() in {"1", "true", "yes"},
                )
            )
    return rows


def block_metrics(rows: list[PodRow]) -> list[BlockMetric]:
    grouped: dict[tuple[str, str], list[PodRow]] = {}
    for row in rows:
        grouped.setdefault((row.block_id, row.profile), []).append(row)

    metrics: list[BlockMetric] = []
    for (block, profile), pods in sorted(grouped.items()):
        scheduled = [p.latency_ms for p in pods if not p.unschedulable]
        if scheduled:
            metrics.append(
                BlockMetric(
                    block,
                    profile,
                    "submit_to_scheduled_p99_ms",
                    percentile(scheduled, 99.0),
                )
            )
        total = len(pods)
        metrics.append(
            BlockMetric(
                block,
                profile,
                "correct_placement_rate",
                sum(1 for p in pods if p.correct) / total,
            )
        )
    return metrics


def _oriented_log_ratio(control: float, arm: float, metric: str) -> float:
    if control <= 0.0 or arm <= 0.0:
        raise ValueError("metric values must be strictly positive")
    ratio = arm / control
    return -math.log(ratio) if metric in LOWER_IS_BETTER else math.log(ratio)


def _decision(ci_low_log: float, ci_high_log: float, point_log: float) -> str:
    """Decision rule on oriented LOG-ratio bounds (never ratio-scale bounds)."""

    band = math.log(1.0 + PRACTICAL_EFFECT)
    if ci_low_log > 0.0 and point_log >= band:
        return "improvement"
    if ci_high_log < 0.0 and point_log <= -band:
        return "regression"
    if -band <= ci_low_log and ci_high_log <= band:
        return "equivalent"
    return "inconclusive"


def compare(metrics: list[BlockMetric]) -> ComparisonResult:
    result = ComparisonResult()
    by_metric: dict[str, dict[str, dict[str, float]]] = {}
    for m in metrics:
        metric_profiles = by_metric.setdefault(m.metric, {})
        blocks = metric_profiles.setdefault(m.profile, {})
        if m.block_id in blocks:
            raise ValueError(
                f"duplicate placement metric for block={m.block_id!r} "
                f"profile={m.profile!r} metric={m.metric!r}"
            )
        blocks[m.block_id] = m.value

    for metric, profiles in sorted(by_metric.items()):
        control = profiles.get(CONTROL_PROFILE)
        if not control:
            result.skipped.append({"metric": metric, "reason": "no control profile"})
            continue
        for profile, blocks in sorted(profiles.items()):
            if profile == CONTROL_PROFILE:
                continue
            shared = sorted(set(control) & set(blocks))
            if len(shared) < MIN_BLOCKS_FOR_VERDICT:
                result.skipped.append(
                    {
                        "metric": metric,
                        "profile": profile,
                        "reason": f"fewer than {MIN_BLOCKS_FOR_VERDICT} paired blocks",
                        "shared_blocks": len(shared),
                    }
                )
                continue
            logs = [_oriented_log_ratio(control[b], blocks[b], metric) for b in shared]
            n = len(logs)
            mean = statistics.fmean(logs)
            sd = statistics.stdev(logs)
            half = _t_ppf(1.0 - 0.05 / 2.0, n - 1) * sd / math.sqrt(n)
            result.comparisons.append(
                {
                    "metric": metric,
                    "profile": profile,
                    "blocks": n,
                    "geometric_mean_ratio": round(math.exp(mean), 6),
                    "ci_low": round(math.exp(mean - half), 6),
                    "ci_high": round(math.exp(mean + half), 6),
                    "decision": _decision(mean - half, mean + half, mean),
                }
            )
    return result


def summary_csv(result: ComparisonResult) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "metric",
            "profile",
            "blocks",
            "geometric_mean_ratio",
            "ci_low",
            "ci_high",
            "decision",
        ]
    )
    for r in result.comparisons:
        writer.writerow(
            [
                r["metric"],
                r["profile"],
                r["blocks"],
                f"{r['geometric_mean_ratio']:.6f}",
                f"{r['ci_low']:.6f}",
                f"{r['ci_high']:.6f}",
                r["decision"],
            ]
        )
    return buffer.getvalue()


def summary_json(result: ComparisonResult) -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "control_profile": CONTROL_PROFILE,
                "practical_effect": PRACTICAL_EFFECT,
                "min_blocks_for_verdict": MIN_BLOCKS_FOR_VERDICT,
                "lower_is_better_metrics": sorted(LOWER_IS_BETTER),
                "comparisons": result.comparisons,
                "skipped": result.skipped,
            },
            indent=2,
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pods", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    result = compare(block_metrics(read_pods(args.pods)))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.csv").write_text(summary_csv(result), encoding="utf-8")
    (args.output_dir / "summary.json").write_text(
        summary_json(result), encoding="utf-8"
    )

    for skip in result.skipped:
        print(f"skipped: {skip}", file=sys.stderr)
    if not result.comparisons:
        print(
            "ERROR: no comparable paired blocks produced a comparison",
            file=sys.stderr,
        )
        return 1
    print(f"wrote {len(result.comparisons)} comparisons to {args.output_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
