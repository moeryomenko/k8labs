#!/usr/bin/env python3
"""Analyze a stock-versus-custom scheduler delta campaign.

The estimator is a paired block ratio. Each block runs the same scenario matrix
once per arm; the per-repetition median is the block value, and the treatment
to control ratio is formed block by block. The reported point estimate is the
geometric mean ratio with a 95 % paired block-bootstrap interval.

The decision bands are predeclared in ``scenarios.py``. A result is
``improved`` or ``regressed`` only when the interval excludes 1 and the point
estimate is past the 5 % practical-effect band; ``equivalent`` requires the
whole interval inside 0.95..1.05. Three paired blocks are the minimum for a
verdict. Fewer blocks, missing arms or an interval that straddles both bands
are reported as ``inconclusive``.

The script is standard-library only and deterministic for a fixed input and
seed.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"
if str(EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS))

from scenarios import (  # noqa: E402
    CONTRASTS,
    DESCRIPTIVE_METRICS,
    PRIMARY_METRICS,
    contrast_name,
)

BAND = 0.05
MIN_BLOCKS = 3
DEFAULT_RESAMPLES = 10000
DEFAULT_SEED = 20261024
SYSTEM_METRICS = (
    ("steal_pct_total", "steal time, % of total CPU time"),
    ("steal_pct_busy", "steal time, % of busy CPU time"),
    ("preferred_min", "preferred CPUs, minimum"),
    ("preferred_changes", "preferred-mask changes"),
    ("ctxt_per_s", "context switches/s"),
    ("pingpong_non_preferred_migrations", "non-preferred migrations"),
)


@dataclass(frozen=True)
class Result:
    scenario: str
    group: str
    metric: str
    feature: str
    expected: str
    direction: str
    control: str
    treatment: str
    blocks: int
    control_median: float
    treatment_median: float
    ratio: float
    oriented_ratio: float
    ci_low: float
    ci_high: float
    decision: str
    status: str
    rationale: str
    per_block: list[dict[str, Any]]


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def metric_names() -> set[str]:
    names = {str(item["metric"]) for item in PRIMARY_METRICS}
    names.update(str(item["metric"]) for item in DESCRIPTIVE_METRICS)
    names.update(name for name, _ in SYSTEM_METRICS)
    return names


def load_campaign(
    run_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Return (group rows, system rows, profiles) with run fields filled in."""

    group_rows: list[dict[str, Any]] = []
    system_rows: list[dict[str, Any]] = []
    profiles: set[str] = set()
    for run_path in sorted(run_dir.glob("block-*/*/run.json")):
        run = read_json(run_path)
        profile = str(run.get("profile", ""))
        if profile:
            profiles.add(profile)
        arm = str(run.get("arm", run_path.parent.name))
        block = str(run.get("block", run_path.parents[1].name.removeprefix("block-")))
        matrix = run_path.parent / "matrix.jsonl"
        for row in read_jsonl(matrix):
            row.setdefault("arm", arm)
            row.setdefault("block", block)
            if row.get("kind") == "system" or row.get("group") == "system":
                system_rows.append(row)
            else:
                group_rows.append(row)
    return group_rows, system_rows, profiles


def aggregate(
    rows: list[dict[str, Any]], names: set[str]
) -> dict[tuple[str, str, str, str, str], float]:
    """Median across repetitions for each block/arm/scenario/group/metric."""

    collected: dict[tuple[str, str, str, str, str], list[float]] = {}
    for row in rows:
        key_prefix = (
            str(row.get("block", "")),
            str(row.get("arm", "")),
            str(row.get("scenario", "")),
            str(row.get("group", "")),
        )
        for name in names:
            value = row.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)):
                continue
            collected.setdefault((*key_prefix, name), []).append(float(value))
    return {
        key: statistics.median(values) for key, values in collected.items() if values
    }


def bootstrap_ci(
    logs: list[float], resamples: int, seed: int, alpha: float = 0.05
) -> tuple[float, float]:
    if not logs:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    means = []
    n = len(logs)
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            total += logs[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    low = means[int((alpha / 2.0) * (len(means) - 1))]
    high = means[int((1.0 - alpha / 2.0) * (len(means) - 1))]
    return (math.exp(low), math.exp(high))


def classify(
    *,
    direction: str,
    expected: str,
    point: float,
    ci_low: float,
    ci_high: float,
    blocks: int,
) -> str:
    if expected == "descriptive":
        return "descriptive"
    if blocks < MIN_BLOCKS:
        return "inconclusive"
    if direction == "equivalent":
        if ci_low >= 1.0 - BAND and ci_high <= 1.0 + BAND:
            return "equivalent"
        if ci_low > 1.0 + BAND or ci_high < 1.0 - BAND:
            return "different"
        return "inconclusive"
    if ci_low > 1.0 and point > 1.0 + BAND:
        return "improved"
    if ci_high < 1.0 and point < 1.0 - BAND:
        return "regressed"
    if ci_low >= 1.0 - BAND and ci_high <= 1.0 + BAND:
        return "equivalent"
    return "inconclusive"


def status_for(expected: str, decision: str, blocks: int) -> str:
    if expected == "descriptive":
        return "descriptive"
    if blocks < MIN_BLOCKS or decision in {"inconclusive", "missing"}:
        return "INCONCLUSIVE"
    if expected == "improve":
        return "PASS" if decision == "improved" else "FAIL"
    if expected == "equivalent":
        return "PASS" if decision == "equivalent" else "FAIL"
    if expected == "regress":
        return "PASS" if decision == "regressed" else "FAIL"
    return "INCONCLUSIVE"


def analyse_metric(
    spec: dict[str, Any],
    aggregated: dict[tuple[str, str, str, str, str], float],
    blocks: list[str],
    *,
    resamples: int,
    seed: int,
) -> Result:
    scenario = str(spec["scenario"])
    group = str(spec["group"])
    metric = str(spec["metric"])
    contrast = str(spec.get("contrast") or contrast_name(scenario))
    if contrast not in CONTRASTS:
        raise ValueError(f"unknown contrast: {contrast}")
    control, treatment = CONTRASTS[contrast]
    direction = str(spec.get("direction", "higher"))
    expected = str(spec.get("expected", "descriptive"))
    feature = str(spec.get("feature", ""))
    rationale = str(spec.get("rationale", ""))

    pairs: list[dict[str, Any]] = []
    for block in blocks:
        c = aggregated.get((block, control, scenario, group, metric))
        t = aggregated.get((block, treatment, scenario, group, metric))
        if c is None or t is None:
            continue
        if expected != "descriptive" and (c <= 0.0 or t <= 0.0):
            continue
        pairs.append({"block": block, "control": c, "treatment": t})

    if not pairs:
        return Result(
            scenario=scenario,
            group=group,
            metric=metric,
            feature=feature,
            expected=expected,
            direction=direction,
            control=control,
            treatment=treatment,
            blocks=0,
            control_median=math.nan,
            treatment_median=math.nan,
            ratio=math.nan,
            oriented_ratio=math.nan,
            ci_low=math.nan,
            ci_high=math.nan,
            decision="missing",
            status=status_for(expected, "missing", 0),
            rationale=rationale,
            per_block=[],
        )

    loggable = all(pair["control"] > 0.0 and pair["treatment"] > 0.0 for pair in pairs)
    if loggable:
        raw_logs = [math.log(pair["treatment"] / pair["control"]) for pair in pairs]
        raw_ratio = math.exp(sum(raw_logs) / len(raw_logs))
        raw_ci_low, raw_ci_high = bootstrap_ci(raw_logs, resamples, seed)
        if direction == "lower":
            oriented_ratio = 1.0 / raw_ratio
            oriented_ci_low = 1.0 / raw_ci_high
            oriented_ci_high = 1.0 / raw_ci_low
        else:
            oriented_ratio = raw_ratio
            oriented_ci_low = raw_ci_low
            oriented_ci_high = raw_ci_high
    else:
        raw_ratio = math.nan
        raw_ci_low = math.nan
        raw_ci_high = math.nan
        oriented_ratio = math.nan
        oriented_ci_low = math.nan
        oriented_ci_high = math.nan
    decision = classify(
        direction=direction,
        expected=expected,
        point=oriented_ratio,
        ci_low=oriented_ci_low,
        ci_high=oriented_ci_high,
        blocks=len(pairs),
    )

    return Result(
        scenario=scenario,
        group=group,
        metric=metric,
        feature=feature,
        expected=expected,
        direction=direction,
        control=control,
        treatment=treatment,
        blocks=len(pairs),
        control_median=statistics.median(pair["control"] for pair in pairs),
        treatment_median=statistics.median(pair["treatment"] for pair in pairs),
        ratio=raw_ratio,
        oriented_ratio=oriented_ratio,
        ci_low=raw_ci_low,
        ci_high=raw_ci_high,
        decision=decision,
        status=status_for(expected, decision, len(pairs)),
        rationale=rationale,
        per_block=pairs,
    )


def format_value(value: float, digits: int = 2) -> str:
    if not math.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def format_ratio(value: float) -> str:
    if not math.isfinite(value):
        return "-"
    return f"{value:.3f}x"


def feature_summary(results: list[Result]) -> list[dict[str, Any]]:
    features: dict[str, list[Result]] = {}
    for result in results:
        if result.expected == "descriptive":
            continue
        features.setdefault(result.feature, []).append(result)
    summary: list[dict[str, Any]] = []
    for feature, items in sorted(features.items()):
        statuses = {item.status for item in items}
        if "FAIL" in statuses:
            verdict = "not observed"
        elif "INCONCLUSIVE" in statuses:
            verdict = "inconclusive"
        else:
            verdict = "observed"
        summary.append(
            {
                "feature": feature,
                "metrics": [
                    f"{item.scenario}/{item.group}/{item.metric}" for item in items
                ],
                "verdict": verdict,
                "statuses": {item.metric: item.status for item in items},
            }
        )
    return summary


def system_table(
    system_rows: list[dict[str, Any]], blocks: list[str]
) -> list[dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, list[float]]] = {}
    for row in system_rows:
        arm = str(row.get("arm", ""))
        scenario = str(row.get("scenario", ""))
        block = str(row.get("block", ""))
        if block not in blocks:
            continue
        preferred_supported = bool(row.get("preferred_first"))
        for name, _ in SYSTEM_METRICS:
            if name.startswith("preferred") and not preferred_supported:
                continue
            value = row.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)):
                continue
            result.setdefault((arm, scenario), {}).setdefault(name, []).append(
                float(value)
            )
    rows: list[dict[str, Any]] = []
    for (arm, scenario), values in sorted(result.items()):
        item: dict[str, Any] = {"arm": arm, "scenario": scenario}
        for name, _ in SYSTEM_METRICS:
            samples = values.get(name, [])
            item[name] = statistics.median(samples) if samples else math.nan
        rows.append(item)
    return rows


def markdown(
    *,
    title: str,
    contrast: str,
    run_id: str,
    blocks: list[str],
    results: list[Result],
    system_rows: list[dict[str, Any]],
    warnings: list[str],
    metadata: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- Run: `{run_id}`")
    lines.append(f"- Contrast: {contrast}")
    lines.append(f"- Paired blocks: {len(blocks)}")
    lines.append(
        f"- Decision band: +/-{int(BAND * 100)} %; "
        f"minimum blocks for a verdict: {MIN_BLOCKS}"
    )
    lines.append(
        "- Estimator: geometric mean paired ratio with a 95 % block-bootstrap interval."
    )
    lines.append("")
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("## Primary results")
    lines.append("")
    lines.append(
        "| scenario | metric | control | treatment | ratio | 95% CI | "
        "expected | decision | status |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---|---|---|")
    for result in results:
        if result.expected == "descriptive":
            continue
        lines.append(
            "| {scenario} | {metric} | {control} | {treatment} | {ratio} | "
            "[{low}, {high}] | {expected} | {decision} | {status} |".format(
                scenario=result.scenario,
                metric=result.metric,
                control=format_value(result.control_median),
                treatment=format_value(result.treatment_median),
                ratio=format_ratio(result.ratio),
                low=format_ratio(result.ci_low),
                high=format_ratio(result.ci_high),
                expected=result.expected,
                decision=result.decision,
                status=result.status,
            )
        )
    lines.append("")

    lines.append("## Feature summary")
    lines.append("")
    lines.append("| feature | metrics | verdict |")
    lines.append("|---|---|---|")
    for item in feature_summary(results):
        lines.append(
            f"| {item['feature']} | {', '.join(item['metrics'])} | {item['verdict']} |"
        )
    lines.append("")

    lines.append("## Per-scenario detail")
    lines.append("")
    by_scenario: dict[str, list[Result]] = {}
    for result in results:
        by_scenario.setdefault(result.scenario, []).append(result)
    for scenario, items in by_scenario.items():
        lines.append(f"### {scenario}")
        lines.append("")
        for result in items:
            lines.append(
                f"- `{result.group}/{result.metric}`: {result.control} vs "
                f"{result.treatment} = {format_ratio(result.ratio)} "
                f"[{format_ratio(result.ci_low)}, {format_ratio(result.ci_high)}], "
                f"{result.decision} ({result.status})."
            )
            if result.per_block:
                pairs = ", ".join(
                    f"{pair['block']}: {format_value(pair['control'])}/"
                    f"{format_value(pair['treatment'])}"
                    for pair in result.per_block
                )
                lines.append(f"  Per block (control/treatment): {pairs}.")
        lines.append("")

    if system_rows:
        lines.append("## System observations")
        lines.append("")
        header = (
            "| arm | scenario | "
            + " | ".join(name.replace("_", " ") for name, _ in SYSTEM_METRICS)
            + " |"
        )
        lines.append(header)
        lines.append("|" + "---|" * (2 + len(SYSTEM_METRICS)))
        for row in system_rows:
            values = []
            for name, _ in SYSTEM_METRICS:
                value = row.get(name, math.nan)
                values.append(
                    format_value(float(value))
                    if isinstance(value, (int, float))
                    else "-"
                )
            lines.append(
                f"| {row['arm']} | {row['scenario']} | " + " | ".join(values) + " |"
            )
        lines.append("")

    lines.append("## Metadata")
    lines.append("")
    for key, value in metadata.items():
        if value in ("", None, {}, []):
            continue
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"run directory not found: {run_dir}")
    output_dir = (
        Path(args.output_dir).resolve() if args.output_dir else run_dir / "analysis"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    campaign = read_json(run_dir / "campaign.json")
    run_id = str(campaign.get("run_id", run_dir.name))

    group_rows, system_rows, profiles = load_campaign(run_dir)
    if not group_rows:
        raise SystemExit(f"no matrix rows under {run_dir}")

    names = metric_names()
    aggregated = aggregate(group_rows, names)
    blocks = sorted(
        {str(row.get("block", "")) for row in group_rows if row.get("block")}
    )
    if not blocks:
        blocks = sorted(
            {str(row.get("block", "")) for row in system_rows if row.get("block")}
        )
    if not blocks:
        raise SystemExit("no block identifiers in the data")

    available_scenarios = {str(row.get("scenario", "")) for row in group_rows}
    results: list[Result] = []
    for spec in [*PRIMARY_METRICS, *DESCRIPTIVE_METRICS]:
        if str(spec["scenario"]) not in available_scenarios:
            continue
        results.append(
            analyse_metric(
                spec,
                aggregated,
                blocks,
                resamples=args.resamples,
                seed=args.seed,
            )
        )

    metadata: dict[str, Any] = {}
    kernels = sorted({str(row.get("kernel", "")) for row in group_rows})
    nodes = sorted({str(row.get("node", "")) for row in group_rows})
    metadata["kernels"] = ", ".join(kernels)
    metadata["nodes"] = ", ".join(nodes)
    metadata["campaign"] = json.dumps(campaign, sort_keys=True) if campaign else ""
    warnings: list[str] = []
    if len(blocks) < MIN_BLOCKS:
        warnings.append(
            f"Pilot only: {len(blocks)} block(s) is below the "
            f"{MIN_BLOCKS}-block floor; no confirmatory verdict is claimed."
        )
    arms = sorted({str(row.get("arm", "")) for row in group_rows})
    if "stock" in arms and "custom" in arms:
        stock_nodes = {
            str(row.get("node", "")) for row in group_rows if row.get("arm") == "stock"
        }
        custom_nodes = {
            str(row.get("node", "")) for row in group_rows if row.get("arm") == "custom"
        }
        if stock_nodes and custom_nodes and stock_nodes != custom_nodes:
            warnings.append(
                "Cross-node campaign: the stock and custom arms ran on "
                "different VMs. Kernel is confounded with VM identity and host "
                "placement; the same-node switch described in README.md is the "
                "confirmatory design."
            )
    steal_rows = [row for row in system_rows if row.get("scenario") == "steal"]
    if steal_rows:
        max_changes = max(
            (float(row.get("preferred_changes", 0)) for row in steal_rows), default=0.0
        )
        if max_changes <= 0.0:
            warnings.append(
                "The steal scenario did not observe a preferred-mask transition; the "
                "governor was not exercised, so its mechanism rows are not evidence."
            )

    pilot = "pilot" in profiles
    if pilot:
        warnings.append(
            "Pilot profile: the run is labelled pilot (shorter repetitions); its "
            "numbers are reported for planning, but no verdict is claimed."
        )
        results = [
            replace(item, decision="inconclusive", status="INCONCLUSIVE")
            if item.expected != "descriptive"
            else item
            for item in results
        ]

    summary = {
        "run_id": run_id,
        "blocks": blocks,
        "results": [result.__dict__ for result in results],
        "warnings": warnings,
        "metadata": metadata,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    header = (
        "scenario,group,metric,feature,expected,direction,control,treatment,blocks,"
        "control_median,treatment_median,ratio,oriented_ratio,ci_low,ci_high,"
        "decision,status\n"
    )
    lines = [header.rstrip("\n")]
    for result in results:
        lines.append(
            ",".join(
                [
                    result.scenario,
                    result.group,
                    result.metric,
                    result.feature.replace(",", " "),
                    result.expected,
                    result.direction,
                    result.control,
                    result.treatment,
                    str(result.blocks),
                    format_value(result.control_median, 4),
                    format_value(result.treatment_median, 4),
                    format_value(result.ratio, 6),
                    format_value(result.oriented_ratio, 6),
                    format_value(result.ci_low, 6),
                    format_value(result.ci_high, 6),
                    result.decision,
                    result.status,
                ]
            )
        )
    (output_dir / "summary.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    system = system_table(system_rows, blocks)
    governor_only = set(arms).issubset({"custom-nogov", "custom-gov"})
    if governor_only:
        title = "Steal governor contrast on the custom kernel"
        contrast = "custom-gov (treatment) versus custom-nogov (control)"
    else:
        title = "Stock 7.3 versus custom kernel: scheduler delta"
        contrast = "custom (treatment) versus stock (control)"
    text = markdown(
        title=title,
        contrast=contrast,
        run_id=run_id,
        blocks=blocks,
        results=results,
        system_rows=system,
        warnings=warnings,
        metadata=metadata,
    )
    (output_dir / "summary.md").write_text(text, encoding="utf-8")
    print(f"  Summary: {output_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
