"""Analyze versioned CPU-limits campaign rows.

The analyzer keeps every missing row explicit. Confirmatory analysis rejects
pilot data. Pilot analysis produces descriptive estimates only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from analysis.stats import (
    holm_decisions,
    paired_log_ratios,
    paired_t_interval,
)

PROTOCOL = "cpu-limits-v1"
MIN_BLOCKS = 3
PERFORMANCE_MARGIN = 0.05
THROTTLE_MARGIN = 0.10

BLOCK_COLUMNS = frozenset(
    {
        "protocol",
        "phase",
        "run_id",
        "block_id",
        "seed",
        "position",
        "cell_id",
        "workload",
        "policy",
        "condition",
        "node",
        "pod_uid",
        "status",
        "started_at",
        "finished_at",
    }
)
CGROUP_COLUMNS = frozenset(
    {
        "protocol",
        "run_id",
        "block_id",
        "cell_id",
        "captured_at",
        "window",
        "level",
        "path",
        "key",
        "value",
        "unit",
    }
)
METRIC_COLUMNS = frozenset(
    {
        "protocol",
        "run_id",
        "block_id",
        "cell_id",
        "timestamp",
        "source",
        "metric",
        "value",
        "unit",
        "labels_json",
    }
)

# These are the predeclared confirmatory comparisons. The planned family size
# remains fixed when a contrast is unavailable.
FAMILIES = (
    ("limit_effect", "fit", "burstable-low", "solo", PERFORMANCE_MARGIN, 4),
    (
        "request_weight",
        "request-high",
        "request-low",
        "contention",
        PERFORMANCE_MARGIN,
        4,
    ),
    (
        "guaranteed_vs_burstable",
        "burstable-low",
        "guaranteed-low",
        "contention",
        PERFORMANCE_MARGIN,
        4,
    ),
    ("throttle_gap", "fit", "burstable-low", "solo", THROTTLE_MARGIN, 8),
)
LOWER_IS_BETTER = frozenset({"latency_p50_ms", "latency_p99_ms"})
THROUGHPUT_METRICS = {
    "cpu-bound": ("bogo_ops_per_second", "ops_per_second", "throughput"),
    "io-bound": ("requests_per_second", "throughput"),
    "memory-bound": ("bogo_ops_per_second", "ops_per_second", "throughput"),
    "hybrid": ("frames_per_second", "throughput"),
}
LATENCY_NAMES = frozenset(
    {"latency_p50_ms", "latency_p99_ms", "p50_latency_ms", "p99_latency_ms"}
)


class CampaignCells(list[dict[str, object]]):
    """Complete cell summaries with separate incomplete-cell metadata."""

    def __init__(
        self,
        values: Iterable[dict[str, object]],
        incomplete_cells: list[dict[str, object]],
    ) -> None:
        super().__init__(values)
        self.incomplete_cells = incomplete_cells


def _read_csv(path: Path, required: frozenset[str]) -> list[dict[str, str]]:
    """Read one campaign CSV and validate its stable header and protocol."""

    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        available = set(reader.fieldnames or ())
        missing = sorted(required - available)
        if missing:
            raise ValueError(f"{path}: missing required column(s): {missing}")
        rows = [dict(row) for row in reader]
    for row in rows:
        if row["protocol"] != PROTOCOL:
            raise ValueError(f"{path}: unsupported protocol {row['protocol']!r}")
    return rows


def _number(value: str, name: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def _pilot_path(path: Path) -> bool:
    return "pilot" in {part.lower() for part in path.parts}


def _metric_values(rows: Iterable[Mapping[str, str]]) -> dict[str, float]:
    """Return each metric's final stored sample for a cell."""

    values: dict[str, tuple[str, float]] = {}
    for row in rows:
        metric = row["metric"].strip()
        if not metric:
            raise ValueError("metrics CSV has an empty metric name")
        timestamp = row["timestamp"].strip()
        value = _number(row["value"], f"metric {metric}")
        old = values.get(metric)
        if old is None or timestamp >= old[0]:
            values[metric] = (timestamp, value)
    return {name: value for name, (_, value) in values.items()}


def _cgroup_summary(
    rows: Iterable[Mapping[str, str]],
) -> tuple[dict[str, float | None], dict[str, str]]:
    """Compute deltas and parameters from the preferred target cgroup level."""

    by_level: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        by_level[row["level"]].append(row)
    selected = next(
        (
            level
            for level in ("container_leaf", "container_scope", "pod_slice")
            if level in by_level
        ),
        None,
    )
    if selected is None:
        return {}, {}
    selected_rows = by_level[selected]
    values: dict[tuple[str, str], float] = {}
    parameters: dict[str, str] = {}
    for row in selected_rows:
        key, window = row["key"], row["window"]
        raw = row["value"].strip()
        if key == "cpu.max.quota" and raw == "max":
            parameters[key] = raw
            continue
        value = _number(raw, f"cgroup {key}")
        if key in {"cpu.weight", "cpu.max.period", "cpu.max.quota"} and window in {
            "start",
            "end",
        }:
            parameters[key] = raw
        values[(window, key)] = value

    def delta(key: str) -> float | None:
        measured = values.get(("measurement_delta", key))
        if measured is not None:
            return measured
        start, end = values.get(("start", key)), values.get(("end", key))
        return None if start is None or end is None else end - start

    usage = delta("cpu.stat.usage_usec")
    periods = delta("cpu.stat.nr_periods")
    throttled_periods = delta("cpu.stat.nr_throttled")
    throttled_usec = delta("cpu.stat.throttled_usec")
    some = delta("cpu.pressure.some_total_usec")
    full = delta("cpu.pressure.full_total_usec")
    quota = parameters.get("cpu.max.quota")
    period = parameters.get("cpu.max.period")
    quota_utilization: float | None = None
    if (
        usage is not None
        and periods is not None
        and quota not in (None, "max")
        and period is not None
    ):
        numeric_quota = _number(quota, "cpu.max.quota")
        if numeric_quota > 0.0 and periods > 0.0:
            quota_utilization = usage / (numeric_quota * periods)
    if (
        periods is not None
        and throttled_periods is not None
        and throttled_periods > periods
    ):
        raise ValueError("nr_throttled exceeds nr_periods")
    summary: dict[str, float | None] = {
        "cpu_usage_seconds": None if usage is None else usage / 1_000_000,
        "quota_utilization": quota_utilization,
        "throttled_period_ratio": None
        if periods in (None, 0.0) or throttled_periods is None
        else throttled_periods / periods,
        "throttled_time_seconds": None
        if throttled_usec is None
        else throttled_usec / 1_000_000,
        "psi_some_seconds": None if some is None else some / 1_000_000,
        "psi_full_seconds": None if full is None else full / 1_000_000,
    }
    return summary, parameters


def load_campaign(
    blocks_path: Path,
    cgroups_path: Path,
    metrics_path: Path,
    *,
    expected_phase: str = "confirmatory",
) -> list[dict[str, object]]:
    """Load one phase and calculate one summary record per complete cell."""

    paths = (blocks_path, cgroups_path, metrics_path)
    if expected_phase == "confirmatory" and any(_pilot_path(path) for path in paths):
        raise ValueError("confirmatory analysis rejects rows from a pilot tree")
    blocks = _read_csv(blocks_path, BLOCK_COLUMNS)
    cgroups = _read_csv(cgroups_path, CGROUP_COLUMNS)
    metrics = _read_csv(metrics_path, METRIC_COLUMNS)
    complete: dict[tuple[str, str], Mapping[str, str]] = {}
    incomplete: list[dict[str, object]] = []
    for row in blocks:
        phase = row["phase"].strip()
        if phase == "pilot" and expected_phase == "confirmatory":
            raise ValueError("confirmatory analysis refuses pilot rows")
        if phase != expected_phase:
            raise ValueError(
                f"blocks CSV has {phase!r} row in {expected_phase!r} analysis"
            )
        key = (row["block_id"], row["cell_id"])
        if key in complete:
            raise ValueError(
                f"duplicate scheduled cell block={key[0]!r} cell={key[1]!r}"
            )
        if row["status"] == "complete":
            complete[key] = row
        else:
            incomplete.append(
                {"block_id": key[0], "cell_id": key[1], "status": row["status"]}
            )
    cgroup_by_cell: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
    for row in cgroups:
        cgroup_by_cell[(row["block_id"], row["cell_id"])].append(row)
    metric_by_cell: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
    for row in metrics:
        metric_by_cell[(row["block_id"], row["cell_id"])].append(row)

    cells: list[dict[str, object]] = []
    for key, row in sorted(complete.items()):
        cgroup_summary, parameters = _cgroup_summary(cgroup_by_cell[key])
        values = _metric_values(metric_by_cell[key])
        throughput = next(
            (
                values[name]
                for name in THROUGHPUT_METRICS.get(row["workload"], ())
                if name in values
            ),
            None,
        )
        latency = next(
            (
                values[name]
                for name in ("latency_p99_ms", "p99_latency_ms")
                if name in values
            ),
            None,
        )
        cells.append(
            {
                "block_id": key[0],
                "cell_id": key[1],
                "workload": row["workload"],
                "policy": row["policy"],
                "condition": row["condition"],
                **cgroup_summary,
                "throughput": throughput,
                "latency_p99_ms": latency,
                "metrics": values,
                "cgroup_parameters": parameters,
            }
        )
    cells.sort(key=lambda item: (str(item["block_id"]), str(item["cell_id"])))
    return CampaignCells(cells, incomplete)


def _outcome(cell: Mapping[str, object], family: str) -> tuple[str, float | None, bool]:
    if family == "throttle_gap":
        return "throttled_period_ratio", cell["throttled_period_ratio"], False
    if (
        cell["workload"] in {"io-bound", "hybrid"}
        and cell["latency_p99_ms"] is not None
    ):
        return "latency_p99_ms", cell["latency_p99_ms"], True
    return "throughput", cell["throughput"], False


def _p_value(log_ratios: Sequence[float]) -> float:
    if len(log_ratios) < 2:
        return 1.0
    mean = statistics.fmean(log_ratios)
    standard_error = statistics.stdev(log_ratios) / math.sqrt(len(log_ratios))
    if standard_error == 0.0:
        return 0.0 if mean != 0.0 else 1.0
    # stats supplies the deterministic Student-t CDF used by the interval code.
    from analysis.stats import _student_t_cdf

    return min(
        1.0,
        2.0 * (1.0 - _student_t_cdf(abs(mean / standard_error), len(log_ratios) - 1)),
    )


def analyze(
    cells: Sequence[Mapping[str, object]], *, phase: str = "confirmatory"
) -> dict[str, object]:
    """Analyze one phase without filling missing pairs."""

    if phase not in {"pilot", "confirmatory"}:
        raise ValueError(f"unsupported analysis phase: {phase!r}")

    contrasts: list[dict[str, object]] = []
    for (
        family,
        control_policy,
        treatment_policy,
        condition,
        margin,
        planned_size,
    ) in FAMILIES:
        workloads = sorted({str(cell["workload"]) for cell in cells})
        for workload in workloads:
            control: dict[str, float | None] = {}
            treatment: dict[str, float | None] = {}
            metric = "throughput"
            lower_is_better = False
            for cell in cells:
                if cell["workload"] != workload or cell["condition"] != condition:
                    continue
                found_metric, value, lower = _outcome(cell, family)
                metric, lower_is_better = found_metric, lower
                if cell["policy"] == control_policy:
                    control[str(cell["block_id"])] = (
                        value if isinstance(value, float) else None
                    )
                if cell["policy"] == treatment_policy:
                    treatment[str(cell["block_id"])] = (
                        value if isinstance(value, float) else None
                    )
            # Log-ratio estimates are undefined for a zero throttle ratio. In a
            # pilot, retain those blocks as incomplete rather than inventing a value.
            if phase == "pilot" and family == "throttle_gap":
                control = {
                    block: value if value is not None and value > 0.0 else None
                    for block, value in control.items()
                }
                treatment = {
                    block: value if value is not None and value > 0.0 else None
                    for block, value in treatment.items()
                }
            paired = paired_log_ratios(control, treatment)
            row: dict[str, object] = {
                "family": family,
                "workload": workload,
                "metric": metric,
                "control": control_policy,
                "treatment": treatment_policy,
                "condition": condition,
                "margin": margin,
                "planned_family_size": planned_size,
                "paired_blocks": list(paired.paired_blocks),
                "missing_blocks": list(paired.missing_blocks),
            }
            if paired.missing_blocks or len(paired.log_ratios) < MIN_BLOCKS:
                row.update(
                    {
                        "decision": "incomplete",
                        "estimate": None,
                        "ci_low": None,
                        "ci_high": None,
                    }
                )
            else:
                logs = tuple(
                    -value if lower_is_better else value for value in paired.log_ratios
                )
                interval = paired_t_interval(logs)
                row.update(
                    {
                        "decision": "pilot descriptive"
                        if phase == "pilot"
                        else "inconclusive",
                        "estimate": interval.estimate,
                        "ci_low": interval.low,
                        "ci_high": interval.high,
                        "p_value": _p_value(logs),
                        "oriented": True,
                    }
                )
            contrasts.append(row)
    for family, _, _, _, margin, planned_size in FAMILIES:
        if phase == "pilot":
            continue
        eligible = [
            row
            for row in contrasts
            if row["family"] == family and row["decision"] != "incomplete"
        ]
        decisions = holm_decisions(
            [float(row["p_value"]) for row in eligible],
            planned_family_size=planned_size,
        )
        for row, correction in zip(eligible, decisions):
            row["holm_threshold"] = correction.threshold
            row["holm_rejected"] = correction.rejected
            estimate, low, high = (
                float(row["estimate"]),
                float(row["ci_low"]),
                float(row["ci_high"]),
            )
            band = math.log(1.0 + margin)
            if correction.rejected and estimate >= 1.0 + margin:
                row["decision"] = "improvement"
            elif correction.rejected and estimate <= 1.0 / (1.0 + margin):
                row["decision"] = "regression"
            elif math.log(low) >= -band and math.log(high) <= band:
                row["decision"] = "equivalent"
    incomplete_cells = list(getattr(cells, "incomplete_cells", []))
    return {
        "protocol": PROTOCOL,
        "phase": phase,
        "cells": list(cells),
        "contrasts": contrasts,
        "incomplete_cells": incomplete_cells,
    }


def write_outputs(output_dir: Path, result: Mapping[str, object]) -> None:
    """Write stable CSV, JSON, and Markdown summaries."""

    output_dir.mkdir(parents=True, exist_ok=True)
    cells = list(result["cells"])
    columns = [
        "block_id",
        "cell_id",
        "workload",
        "policy",
        "condition",
        "cpu_usage_seconds",
        "quota_utilization",
        "throttled_period_ratio",
        "throttled_time_seconds",
        "psi_some_seconds",
        "psi_full_seconds",
        "throughput",
        "latency_p99_ms",
        "cgroup_parameters",
    ]
    with (output_dir / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for cell in cells:
            writer.writerow(
                {
                    key: json.dumps(cell[key], sort_keys=True)
                    if key == "cgroup_parameters"
                    else cell[key]
                    for key in columns
                }
            )
    (output_dir / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    is_pilot = result["phase"] == "pilot"
    lines = [
        f"# CPU-limits {result['phase']} "
        + ("descriptive summary" if is_pilot else "summary"),
        "",
        f"Complete cells: {len(cells)}",
        f"Incomplete cells: {len(result['incomplete_cells'])}",
        "",
        "## "
        + ("Pilot descriptive contrast availability" if is_pilot else "Contrasts"),
        "",
        "| family | workload | metric | "
        + ("pilot label" if is_pilot else "decision")
        + " | paired blocks |",
        "|---|---|---|---|---:|",
    ]
    for contrast in result["contrasts"]:
        lines.append(
            f"| {contrast['family']} | {contrast['workload']} | {contrast['metric']} | {contrast['decision']} | {len(contrast['paired_blocks'])} |"
        )
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=("pilot", "confirmatory"), default="confirmatory"
    )
    parser.add_argument("--blocks", type=Path, required=True)
    parser.add_argument("--cgroups", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--trace-rows", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = analyze(
        load_campaign(
            args.blocks, args.cgroups, args.metrics, expected_phase=args.phase
        ),
        phase=args.phase,
    )
    if args.trace_rows is not None:
        trace_payload = json.loads(args.trace_rows.read_text(encoding="utf-8"))
        if not isinstance(trace_payload, Mapping):
            raise ValueError("trace rows must contain a JSON object")
        trace_rows = trace_payload.get("trace_rows", [])
        if not isinstance(trace_rows, list):
            raise ValueError("trace rows must contain a trace_rows list")
        result["trace_rows"] = trace_rows
    write_outputs(args.output_dir, result)
    print(f"wrote {len(result['cells'])} complete cell summaries")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
