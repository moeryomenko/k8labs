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

# The family of confirmatory primary contrasts: two sched_ext arms against the
# control across five workloads. Predeclared, because the multiplicity
# correction and the sample-size calculation both need the family size before
# any confirmatory data exists.
PRIMARY_CONTRASTS = 10

# The approved minimum worthwhile effect is per metric. A 20% change in a p99 is
# the smallest latency difference this study claims to resolve; throughput metrics
# keep the 5% default. Applying one band to both would either hide a real latency
# regression or claim precision the latency metrics do not have.
PRACTICAL_EFFECT_BY_METRIC = {"latency_p99_ms": 0.20}

# Workloads the campaign reports descriptively. They are measured and reported
# with the same estimator, but no verdict is issued for them, so they are never
# classified `improvement`, `regression` or `equivalent`. They stay inside the
# predeclared family size: the correction is not weakened for conclusions drawn
# elsewhere in the family.
DESCRIPTIVE_WORKLOADS = ("mixed-http-ffmpeg",)


def practical_effect(metric: str) -> float:
    """The minimum worthwhile effect for a metric, as approved."""

    return PRACTICAL_EFFECT_BY_METRIC.get(metric, PRACTICAL_EFFECT)


def is_descriptive(workload: str) -> bool:
    """True when a workload is reported without a confirmatory verdict."""

    return workload in DESCRIPTIVE_WORKLOADS

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
    effect: float = PRACTICAL_EFFECT
    descriptive: bool = False


@dataclass
class AnalysisResult:
    """Verdicts plus everything that was skipped, so nothing fails silently."""

    verdicts: list[Verdict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _normal_ppf(p: float) -> float:
    """Standard-normal quantile (Acklam's rational approximation).

    Used for the pilot's variance bound: that bound is on a variance, so the
    reference distribution is chi-square, which is built from this quantile.
    """

    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    low, high = 0.02425, 1 - 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return (
            ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(
            ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
    ) / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


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
    max_n: int = 100_000,
) -> int:
    """Required paired-block count for a two-sided paired t test.

    Iterates the t degrees of freedom to convergence, as the plan requires.
    Returns at least the frozen minimum so the floor is never undercut.

    There is deliberately no *silent* cap: `max_n` is a runaway guard, and
    reaching it is reported as an error by the caller rather than silently
    returning an underpowered count.
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
    raise ValueError(
        f"required paired blocks exceed {max_n}; the effect is too small for "
        "this variance to be resolved, so the count is not silently capped"
    )


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


def _decision(
    ci_low_log: float,
    ci_high_log: float,
    point_log: float,
    effect: float = PRACTICAL_EFFECT,
) -> str:
    """Apply the frozen decision rule to an oriented LOG-ratio interval.

    All three arguments are on the log scale (the interval bounds of the mean
    log ratio and the mean log ratio itself). Mixing ratio-scale bounds with
    log-scale thresholds is the classic way to make a decision rule that can
    never return ``regression``.

    This is the *uncorrected* per-contrast reading used for descriptive
    reporting. The multiplicity-corrected confirmatory verdict is produced by
    :func:`holm_decisions`, which additionally requires the point estimate to
    clear the practical-effect band.
    """

    band = math.log(1.0 + effect)
    if ci_low_log > 0.0 and point_log >= band:
        return "improvement"
    if ci_high_log < 0.0 and point_log <= -band:
        return "regression"
    if -band <= ci_low_log and ci_high_log <= band:
        return "equivalent"
    return "inconclusive"


def _holm_step_down(
    scores: list[float],
    dfs: list[int],
    alpha: float,
    sided: int,
    family_size: int | None = None,
) -> list[bool]:
    """Holm step-down rejection for t-like statistics.

    `scores` are oriented so that a larger value is stronger evidence; `sided`
    is 2 for a two-sided test and 1 for a one-sided one. Ordering by the
    statistic is equivalent to ordering by p-value (the t CDF is monotone), so
    the step-down can be applied without materialising p-values. Once a
    hypothesis fails to reject, every less extreme one is retained.

    `family_size` defaults to the number of scores, but the confirmatory family
    is *predeclared*, so a run that could only evaluate part of it still divides
    by the declared size rather than earning a weaker correction.
    """

    m = family_size if family_size is not None else len(scores)
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


def holm_decisions(comparisons: list[dict], alpha: float = DEFAULT_ALPHA) -> list[str]:
    """Multiplicity-corrected verdicts for the predeclared contrast family.

    The family is the confirmatory primary contrasts. Both the no-effect test
    and the equivalence TOST are corrected across it. A verdict is only
    ``improvement``/``regression`` when the corrected no-effect test rejects
    *and* the point estimate clears the +-5% practical-effect band;
    ``equivalent`` requires the corrected TOST to reject. Everything else is
    ``inconclusive``.
    """

    if not comparisons:
        return []
    decisions = [
        "incomplete"
        if c.get("incomplete")
        else ("descriptive" if c.get("descriptive") else "inconclusive")
        for c in comparisons
    ]
    # Descriptive and incomplete contrasts are reported but not tested: they must
    # not consume a step-down position another contrast could have used, while the
    # family size stays the predeclared one.
    tested = [
        i
        for i, c in enumerate(comparisons)
        if not c.get("descriptive") and not c.get("incomplete")
    ]
    if not tested:
        return decisions

    dfs = [comparisons[i]["df"] for i in tested]
    abs_t = [abs(comparisons[i]["t_two"]) for i in tested]
    # TOST statistic: the weaker of the two one-sided tests decides, so a
    # contrast is only equivalent when both bounds are cleared.
    tost = [min(comparisons[i]["t_lower"], -comparisons[i]["t_upper"]) for i in tested]

    rejects_no_effect = _holm_step_down(
        abs_t, dfs, alpha, sided=2, family_size=PRIMARY_CONTRASTS
    )
    rejects_tost = _holm_step_down(
        tost, dfs, alpha, sided=1, family_size=PRIMARY_CONTRASTS
    )

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


def analyse(
    observations: list[Observation],
    required_blocks: dict[tuple[str, str], int] | None = None,
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
    required = required_blocks or {}
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

    # Pass 1: everything needed to state a contrast, before any verdict. The
    # decisions come from a multiplicity correction across the whole family, so
    # they cannot be made one contrast at a time as the loop walks the data.
    stats: list[dict] = []
    boot: list[tuple[float, float]] = []
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
            # A scenario the wall-clock cap left short of its frozen count is not
            # underpowered-but-complete: it is unfinished, and saying so is the
            # difference between "no effect" and "no evidence".
            wanted = required.get((workload, metric))
            if wanted is not None and n < wanted:
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
                        effect=practical_effect(metric),
                        descriptive=is_descriptive(workload),
                    )
                )
                continue
            df = n - 1
            mean = statistics.fmean(logs)
            sd = statistics.stdev(logs)
            se = sd / math.sqrt(n)

            # A perfectly consistent effect has no estimated spread: the t
            # statistic is its limit (infinite), not zero. Reporting zero would
            # turn "every block agreed" into "no evidence".
            def _t(numerator: float) -> float:
                if se > 0.0:
                    return numerator / se
                if numerator > 0.0:
                    return math.inf
                if numerator < 0.0:
                    return -math.inf
                return 0.0

            # The two-sided no-effect test and the two one-sided bounds of the
            # equivalence TOST, so both families can be corrected later. The band
            # is this metric's approved minimum worthwhile effect, and it is the
            # ratio band [1 - effect, 1 + effect] on the ratio scale.
            effect = practical_effect(metric)
            band_low = math.log(1.0 - effect)
            band_high = math.log(1.0 + effect)

            rng = random.Random(bootstrap_seed)
            means: list[float] = []
            for _ in range(bootstrap_resamples):
                sample = [logs[rng.randrange(n)] for _ in range(n)]
                means.append(statistics.fmean(sample))
            means.sort()

            stats.append(
                {
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
                    "descriptive": is_descriptive(workload),
                    "incomplete": False,
                }
            )
            boot.append(
                (
                    math.exp(means[int(0.025 * (len(means) - 1))]),
                    math.exp(means[int(0.975 * (len(means) - 1))]),
                )
            )

    # Pass 2: one multiplicity-corrected verdict per contrast.
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
            "effect",
            "descriptive",
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
                f"{v.effect:.2f}",
                "yes" if v.descriptive else "no",
            ]
        )
    return buffer.getvalue()


def summary_json(result: AnalysisResult) -> str:
    """Render verdicts as deterministic, schema-versioned JSON."""

    payload = {
        "schema_version": 2,
        "control_scheduler": CONTROL_SCHEDULER,
        "practical_effect": PRACTICAL_EFFECT,
        "practical_effect_by_metric": PRACTICAL_EFFECT_BY_METRIC,
        "descriptive_workloads": list(DESCRIPTIVE_WORKLOADS),
        "min_blocks_for_verdict": MIN_BLOCKS_FOR_VERDICT,
        "primary_contrasts": PRIMARY_CONTRASTS,
        "multiplicity": "holm step-down over the predeclared family",
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
                "effect": v.effect,
                "descriptive": v.descriptive,
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
    parser.add_argument(
        "--sample-size",
        type=Path,
        default=None,
        help="Frozen sample-size record; a scenario short of its count is "
        "incomplete rather than a verdict. Defaults to the record beside the "
        "confirmatory blocks.",
    )
    args = parser.parse_args(argv)

    observations = read_blocks(Path(args.blocks))
    sample_size = args.sample_size
    if sample_size is None:
        beside = Path(args.blocks).resolve().parent.parent / "sample-size.json"
        sample_size = beside if beside.is_file() else None

    required: dict[tuple[str, str], int] = {}
    if sample_size is not None and Path(sample_size).is_file():
        record = json.loads(Path(sample_size).read_text(encoding="utf-8"))
        for entry in record.get("scenarios", []):
            frozen = entry.get("confirmatory_blocks")
            if frozen:
                required[(entry["scenario"], entry["metric"])] = int(frozen)
        print(f"frozen counts: {sample_size} ({len(required)} scenario(s))")

    result = analyse(observations, required_blocks=required)
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
