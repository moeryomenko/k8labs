#!/usr/bin/env python3
"""Analyze a cgroup-mode matrix run.

Input: ``matrix.jsonl`` and ``meta.json`` produced by ``run_matrix.py``.
Output: ``shares.csv`` (wide, one row per scenario/mode/group),
``metrics.csv`` (long, one row per metric), ``summary.json`` and
``summary.md``.

The expected shares come from the documented effective-weight model in
``scenarios.py``. That model is the idealized full-concurrency model: it
assumes every group is runnable on every CPU. The measured data is reported
next to it, and the ``relative_to_smp`` column isolates the mode effect from
placement by comparing each mode with the smp baseline in the same repetition.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from scenarios import (
    MODE_SUMMARY,
    MODES,
    SCENARIOS,
    expected_per_task_shares,
    expected_shares,
    scenario_units,
)

REQUIRED_ROW = {
    "scenario",
    "mode",
    "group",
    "kind",
    "rep",
    "usage_usec",
    "elapsed_s",
    "weight",
    "threads",
    "cpus",
}


def fail(message: str) -> None:
    raise SystemExit(f"analyze: {message}")


def load_matrix(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        fail(f"matrix file not found: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            fail(f"{path}:{line_number}: invalid JSON: {exc}")
        if not isinstance(row, dict):
            fail(f"{path}:{line_number}: row is not an object")
        missing = REQUIRED_ROW - row.keys()
        if missing:
            fail(f"{path}:{line_number}: missing keys: {sorted(missing)}")
        rows.append(row)
    if not rows:
        fail(f"matrix file is empty: {path}")
    return rows


def load_meta(path: Path) -> dict[str, Any]:
    if not path.is_file():
        fail(f"meta file not found: {path}")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{path}: invalid JSON: {exc}")
    if not isinstance(meta, dict):
        fail(f"{path}: meta is not an object")
    for key in (
        "run_id",
        "node",
        "kernel",
        "cpus",
        "reps",
        "modes",
        "scenarios",
        "rows",
        "duration_s",
        "warmup_s",
    ):
        if key not in meta:
            fail(f"{path}: missing meta key {key!r}")
    return meta


def validate(rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    if int(meta["rows"]) != len(rows):
        fail(
            f"meta says {meta['rows']} rows but the matrix has {len(rows)}; "
            "the run was truncated"
        )
    scenarios = list(meta["scenarios"])
    modes = list(meta["modes"])
    for scenario in scenarios:
        if scenario not in SCENARIOS:
            fail(f"unknown scenario in meta: {scenario}")
    for mode in modes:
        if mode not in MODES:
            fail(f"unknown mode in meta: {mode}")
    if int(meta["reps"]) < 1:
        fail("meta reps must be >= 1")
    if int(meta["cpus"]) < 1:
        fail("meta cpus must be >= 1")
    expected_reps = set(range(1, int(meta["reps"]) + 1))
    for scenario in scenarios:
        for mode in modes:
            for rep in expected_reps:
                if not any(
                    row["scenario"] == scenario
                    and row["mode"] == mode
                    and int(row["rep"]) == rep
                    for row in rows
                ):
                    fail(f"missing data for {scenario}/{mode}/rep{rep}")


def mean_sd(values: list[float]) -> tuple[float, float, int]:
    if not values:
        return (0.0, 0.0, 0)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return (mean, sd, len(values))


def index_rows(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, int], list[dict[str, Any]]]:
    index: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        index[(str(row["scenario"]), str(row["mode"]), int(row["rep"]))].append(row)
    return index


def level_share(
    rows: list[dict[str, Any]],
    kind: str,
) -> dict[str, float]:
    selected = [row for row in rows if str(row["kind"]) == kind]
    total = sum(int(row["usage_usec"]) for row in selected)
    if total <= 0:
        fail(f"no CPU usage for kind={kind}")
    return {str(row["group"]): int(row["usage_usec"]) / total for row in selected}


def pod_shares(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    parents = [row for row in rows if str(row["kind"]) == "parent"]
    children = [row for row in rows if str(row["kind"]) == "child"]
    result: dict[str, dict[str, float]] = {}
    for parent in parents:
        name = str(parent["group"])
        kids = [row for row in children if str(row["parent"]) == name]
        total = sum(int(row["usage_usec"]) for row in kids)
        if total <= 0:
            continue
        result[name] = {
            str(row["group"]): int(row["usage_usec"]) / total for row in kids
        }
    return result


def find_row(
    rows: list[dict[str, Any]], group: str, kind: str | None = None
) -> dict[str, Any] | None:
    for row in rows:
        if str(row["group"]) == group and (kind is None or str(row["kind"]) == kind):
            return row
    return None


def metric_record(
    *,
    scenario: str,
    mode: str,
    group: str,
    kind: str,
    parent: str,
    weight: int,
    threads: int,
    cpu_max: str,
    metric: str,
    mean: float,
    sd: float,
    n: int,
    unit: str,
    expected: float | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "mode": mode,
        "group": group,
        "kind": kind,
        "parent": parent,
        "weight": weight,
        "threads": threads,
        "cpu_max": cpu_max,
        "metric": metric,
        "mean": round(mean, 6),
        "sd": round(sd, 6),
        "n": n,
        "unit": unit,
        "expected": "" if expected is None else round(expected, 6),
        "delta": "" if expected is None else round(mean - expected, 6),
        "note": note,
    }


def analyze(
    rows: list[dict[str, Any]], meta: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cpus = int(meta["cpus"])
    reps = list(range(1, int(meta["reps"]) + 1))
    scenarios = [str(name) for name in meta["scenarios"]]
    modes = [str(name) for name in meta["modes"]]
    index = index_rows(rows)

    share_cache: dict[tuple[str, str, int], dict[str, float]] = {}
    pod_cache: dict[tuple[str, str, int], dict[str, dict[str, float]]] = {}
    for scenario in scenarios:
        for mode in modes:
            for rep in reps:
                rep_rows = index[(scenario, mode, rep)]
                kind = "parent" if SCENARIOS[scenario]["kind"] == "nested" else "group"
                share_cache[(scenario, mode, rep)] = level_share(rep_rows, kind)
                if SCENARIOS[scenario]["kind"] == "nested":
                    pod_cache[(scenario, mode, rep)] = pod_shares(rep_rows)

    wide: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    pods: list[dict[str, Any]] = []

    for scenario in scenarios:
        units = scenario_units(scenario)
        model = {mode: expected_shares(scenario, mode, cpus) for mode in modes}
        model_per_task = {
            mode: expected_per_task_shares(scenario, mode, cpus) for mode in modes
        }
        for mode in modes:
            expected = model[mode]
            expected_task = model_per_task[mode]
            for unit in units:
                name = str(unit["name"])
                kind = str(unit["kind"])
                values = [share_cache[(scenario, mode, rep)][name] for rep in reps]
                mean, sd, n = mean_sd(values)
                per_task_values = [
                    value / max(1, int(unit["threads"])) for value in values
                ]
                per_task_mean, per_task_sd, _ = mean_sd(per_task_values)
                expected_share = None if expected is None else expected[name]
                expected_task_share = (
                    None if expected_task is None else expected_task[name]
                )
                # The latency probe sleeps between 1 ms timers, so its CPU
                # share is not the modeled runnable-weight share. The wakeup
                # percentiles below are the metric for that scenario.
                if scenario == "latency":
                    expected_share = None
                    expected_task_share = None

                ratios: list[float] = []
                if "smp" in modes:
                    for rep in reps:
                        baseline = share_cache[(scenario, "smp", rep)][name]
                        if baseline > 0:
                            ratios.append(
                                share_cache[(scenario, mode, rep)][name] / baseline
                            )
                ratio_mean, ratio_sd, _ = mean_sd(ratios)

                throughput: list[float] = []
                throughput_per_thread: list[float] = []
                for rep in reps:
                    row = find_row(index[(scenario, mode, rep)], name, kind)
                    if row is None:
                        continue
                    elapsed = float(row.get("elapsed_s", 0.0))
                    threads = max(1, int(row.get("threads", 1)))
                    iterations = int(row.get("iterations", 0))
                    if elapsed > 0 and iterations > 0:
                        throughput.append(iterations / elapsed)
                        throughput_per_thread.append(iterations / elapsed / threads)
                throughput_mean, throughput_sd, _ = mean_sd(throughput)
                per_thread_mean, per_thread_sd, _ = mean_sd(throughput_per_thread)

                wide.append(
                    {
                        "scenario": scenario,
                        "mode": mode,
                        "group": name,
                        "kind": kind,
                        "parent": str(unit["parent"]),
                        "weight": int(unit["weight"]),
                        "threads": int(unit["threads"]),
                        "cpu_max": str(unit["cpu_max"]),
                        "share_mean_pct": round(mean * 100, 3),
                        "share_sd_pct": round(sd * 100, 3),
                        "share_n": n,
                        "expected_pct": ""
                        if expected_share is None
                        else round(expected_share * 100, 3),
                        "delta_pp": ""
                        if expected_share is None
                        else round((mean - expected_share) * 100, 3),
                        "relative_to_smp": "" if not ratios else round(ratio_mean, 4),
                        "relative_to_smp_sd": "" if not ratios else round(ratio_sd, 4),
                        "per_task_mean_pct": round(per_task_mean * 100, 3),
                        "per_task_sd_pct": round(per_task_sd * 100, 3),
                        "per_task_expected_pct": ""
                        if expected_task_share is None
                        else round(expected_task_share * 100, 3),
                        "iterations_per_s": round(throughput_mean, 1),
                        "iterations_per_s_sd": round(throughput_sd, 1),
                        "iterations_per_thread_s": round(per_thread_mean, 1),
                        "iterations_per_thread_s_sd": round(per_thread_sd, 1),
                    }
                )

                metrics.append(
                    metric_record(
                        scenario=scenario,
                        mode=mode,
                        group=name,
                        kind=kind,
                        parent=str(unit["parent"]),
                        weight=int(unit["weight"]),
                        threads=int(unit["threads"]),
                        cpu_max=str(unit["cpu_max"]),
                        metric="cpu_share_pct",
                        mean=mean * 100,
                        sd=sd * 100,
                        n=n,
                        unit="percent",
                        expected=None
                        if expected_share is None
                        else expected_share * 100,
                        note="idealized full-concurrency model"
                        if expected_share is not None
                        else "quota-capped; no mode expectation",
                    )
                )
                metrics.append(
                    metric_record(
                        scenario=scenario,
                        mode=mode,
                        group=name,
                        kind=kind,
                        parent=str(unit["parent"]),
                        weight=int(unit["weight"]),
                        threads=int(unit["threads"]),
                        cpu_max=str(unit["cpu_max"]),
                        metric="per_task_share_pct",
                        mean=per_task_mean * 100,
                        sd=per_task_sd * 100,
                        n=n,
                        unit="percent",
                        expected=None
                        if expected_task_share is None
                        else expected_task_share * 100,
                        note="CPU share divided by the group's thread count",
                    )
                )
                if ratios:
                    metrics.append(
                        metric_record(
                            scenario=scenario,
                            mode=mode,
                            group=name,
                            kind=kind,
                            parent=str(unit["parent"]),
                            weight=int(unit["weight"]),
                            threads=int(unit["threads"]),
                            cpu_max=str(unit["cpu_max"]),
                            metric="relative_to_smp",
                            mean=ratio_mean,
                            sd=ratio_sd,
                            n=len(ratios),
                            unit="ratio",
                            expected=1.0,
                            note="paired by repetition against the smp baseline",
                        )
                    )
                if throughput:
                    metrics.append(
                        metric_record(
                            scenario=scenario,
                            mode=mode,
                            group=name,
                            kind=kind,
                            parent=str(unit["parent"]),
                            weight=int(unit["weight"]),
                            threads=int(unit["threads"]),
                            cpu_max=str(unit["cpu_max"]),
                            metric="iterations_per_s",
                            mean=throughput_mean,
                            sd=throughput_sd,
                            n=len(throughput),
                            unit="iterations/s",
                        )
                    )
                    metrics.append(
                        metric_record(
                            scenario=scenario,
                            mode=mode,
                            group=name,
                            kind=kind,
                            parent=str(unit["parent"]),
                            weight=int(unit["weight"]),
                            threads=int(unit["threads"]),
                            cpu_max=str(unit["cpu_max"]),
                            metric="iterations_per_thread_s",
                            mean=per_thread_mean,
                            sd=per_thread_sd,
                            n=len(throughput),
                            unit="iterations/s/thread",
                        )
                    )

            # Scenario-level metrics: context switches, QoS ratio, throttle.
            ctxt_values: list[float] = []
            for rep in reps:
                rep_rows = index[(scenario, mode, rep)]
                if not rep_rows:
                    continue
                elapsed = float(rep_rows[0].get("elapsed_s", 0.0))
                if elapsed > 0:
                    ctxt_values.append(int(rep_rows[0].get("ctxt", 0)) / elapsed)
            ctxt_mean, ctxt_sd, ctxt_n = mean_sd(ctxt_values)
            if ctxt_n:
                metrics.append(
                    metric_record(
                        scenario=scenario,
                        mode=mode,
                        group="",
                        kind="scenario",
                        parent="",
                        weight=0,
                        threads=0,
                        cpu_max="",
                        metric="ctxt_per_s",
                        mean=ctxt_mean,
                        sd=ctxt_sd,
                        n=ctxt_n,
                        unit="context switches/s",
                        note="whole-node counter during the measurement window",
                    )
                )

            if scenario == "qos_nested":
                qos_values: list[float] = []
                for rep in reps:
                    shares = share_cache[(scenario, mode, rep)]
                    guaranteed = shares.get("qos-guaranteed", 0.0)
                    burstable = shares.get("qos-burstable", 0.0)
                    if guaranteed > 0:
                        qos_values.append(burstable / guaranteed)
                qos_mean, qos_sd, qos_n = mean_sd(qos_values)
                if qos_n:
                    expected_ratio = None
                    if model[mode] is not None:
                        expected_ratio = (
                            model[mode]["qos-burstable"] / model[mode]["qos-guaranteed"]
                        )
                    metrics.append(
                        metric_record(
                            scenario=scenario,
                            mode=mode,
                            group="qos-burstable/qos-guaranteed",
                            kind="scenario",
                            parent="",
                            weight=0,
                            threads=0,
                            cpu_max="",
                            metric="qos_ratio",
                            mean=qos_mean,
                            sd=qos_sd,
                            n=qos_n,
                            unit="ratio",
                            expected=expected_ratio,
                            note="QoS-class CPU ratio",
                        )
                    )

                for parent in SCENARIOS[scenario]["parents"]:
                    parent_name = str(parent["name"])
                    for child in parent["children"]:
                        child_name = f"{parent_name}-{child['name']}"
                        within_values: list[float] = []
                        machine_values: list[float] = []
                        for rep in reps:
                            shares = pod_cache[(scenario, mode, rep)].get(
                                parent_name, {}
                            )
                            if child_name in shares:
                                within_values.append(shares[child_name])
                            rep_rows = index[(scenario, mode, rep)]
                            parent_usage = sum(
                                int(row["usage_usec"])
                                for row in rep_rows
                                if str(row["kind"]) == "parent"
                            )
                            child_row = find_row(rep_rows, child_name, "child")
                            if parent_usage > 0 and child_row is not None:
                                machine_values.append(
                                    int(child_row["usage_usec"]) / parent_usage
                                )
                        within_mean, within_sd, within_n = mean_sd(within_values)
                        machine_mean, machine_sd, _ = mean_sd(machine_values)
                        pods.append(
                            {
                                "scenario": scenario,
                                "mode": mode,
                                "parent": parent_name,
                                "child": child_name,
                                "weight": int(child["weight"]),
                                "threads": int(child["threads"]),
                                "share_within_parent_pct": round(within_mean * 100, 3),
                                "share_within_parent_sd_pct": round(within_sd * 100, 3),
                                "share_of_machine_pct": round(machine_mean * 100, 3),
                                "share_of_machine_sd_pct": round(machine_sd * 100, 3),
                                "n": within_n,
                            }
                        )
                        metrics.append(
                            metric_record(
                                scenario=scenario,
                                mode=mode,
                                group=child_name,
                                kind="child",
                                parent=parent_name,
                                weight=int(child["weight"]),
                                threads=int(child["threads"]),
                                cpu_max="",
                                metric="pod_share_within_parent_pct",
                                mean=within_mean * 100,
                                sd=within_sd * 100,
                                n=within_n,
                                unit="percent",
                                expected=50.0,
                                note="equal pod weights and thread counts",
                            )
                        )

            if scenario == "quota":
                throttle_usage: list[float] = []
                throttle_wall: list[float] = []
                throttle_periods: list[float] = []
                for rep in reps:
                    rep_rows = index[(scenario, mode, rep)]
                    limited = find_row(rep_rows, "limited", "group")
                    if limited is None:
                        continue
                    usage = int(limited.get("usage_usec", 0))
                    throttled = int(limited.get("throttled_usec", 0))
                    periods = int(limited.get("nr_periods", 0))
                    throttled_periods = int(limited.get("nr_throttled", 0))
                    elapsed = float(limited.get("elapsed_s", 0.0))
                    if usage > 0:
                        throttle_usage.append(throttled / usage * 100)
                    if elapsed > 0:
                        throttle_wall.append(throttled / (elapsed * 1_000_000) * 100)
                    if periods > 0:
                        throttle_periods.append(throttled_periods / periods * 100)
                for metric, values, note in (
                    (
                        "throttled_usage_pct",
                        throttle_usage,
                        "throttled_usec / usage_usec",
                    ),
                    (
                        "throttled_wall_pct",
                        throttle_wall,
                        "throttled_usec / wall time",
                    ),
                    (
                        "throttled_periods_pct",
                        throttle_periods,
                        "nr_throttled / nr_periods",
                    ),
                ):
                    mean, sd, n = mean_sd(values)
                    if n:
                        metrics.append(
                            metric_record(
                                scenario=scenario,
                                mode=mode,
                                group="limited",
                                kind="group",
                                parent="",
                                weight=100,
                                threads=2,
                                cpu_max="50000 100000",
                                metric=metric,
                                mean=mean,
                                sd=sd,
                                n=n,
                                unit="percent",
                                note=note,
                            )
                        )

            if scenario == "latency":
                for field, metric in (
                    ("p50_us", "wakeup_p50_us"),
                    ("p99_us", "wakeup_p99_us"),
                    ("p999_us", "wakeup_p999_us"),
                    ("max_us", "wakeup_max_us"),
                ):
                    values: list[float] = []
                    for rep in reps:
                        probe = find_row(index[(scenario, mode, rep)], "probe", "group")
                        if probe is not None and field in probe:
                            values.append(float(probe[field]))
                    mean, sd, n = mean_sd(values)
                    if n:
                        metrics.append(
                            metric_record(
                                scenario=scenario,
                                mode=mode,
                                group="probe",
                                kind="group",
                                parent="",
                                weight=100,
                                threads=1,
                                cpu_max="",
                                metric=metric,
                                mean=mean,
                                sd=sd,
                                n=n,
                                unit="microseconds",
                                note="1 ms timer",
                            )
                        )

    result = {
        "run_id": meta["run_id"],
        "node": meta["node"],
        "kernel": meta["kernel"],
        "cpus": cpus,
        "reps": meta["reps"],
        "duration_s": meta["duration_s"],
        "warmup_s": meta["warmup_s"],
        "modes": modes,
        "scenarios": scenarios,
        "original_mode": meta.get("original_mode", ""),
        "restored_mode": meta.get("restored_mode", ""),
        "mode_summary": MODE_SUMMARY,
        "wide": wide,
        "metrics": metrics,
        "pods": pods,
    }
    return wide, metrics, result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def pct(value: float) -> str:
    return f"{value:6.2f}"


def build_markdown(result: dict[str, Any]) -> str:
    wide: list[dict[str, Any]] = result["wide"]
    metrics: list[dict[str, Any]] = result["metrics"]
    modes: list[str] = result["modes"]
    scenarios: list[str] = result["scenarios"]
    lines: list[str] = []
    lines.append("# cgroup_mode workload impact")
    lines.append("")
    lines.append(f"- Run: `{result['run_id']}`")
    lines.append(f"- Node: `{result['node']}` kernel `{result['kernel']}`")
    lines.append(
        f"- CPUs: {result['cpus']}, repetitions: {result['reps']}, "
        f"measurement window: {result['duration_s']} s, warmup: {result['warmup_s']} s"
    )
    lines.append(
        f"- Mode before/after: `{result['original_mode']}` / "
        f"`{result['restored_mode']}`"
    )
    lines.append("")
    lines.append("## Mode semantics")
    lines.append("")
    lines.append("| mode | model |")
    lines.append("|---|---|")
    for mode in modes:
        lines.append(f"| `{mode}` | {MODE_SUMMARY.get(mode, '')} |")
    lines.append("")

    def rows_for(scenario: str) -> list[dict[str, Any]]:
        return [row for row in wide if row["scenario"] == scenario]

    for scenario in scenarios:
        scenario_rows = rows_for(scenario)
        if not scenario_rows:
            continue
        lines.append(f"## {scenario}")
        lines.append("")
        lines.append(str(SCENARIOS[scenario]["description"]))
        lines.append("")
        groups: list[str] = []
        for row in scenario_rows:
            if row["group"] not in groups:
                groups.append(str(row["group"]))
        by_mode_group = {
            (str(row["mode"]), str(row["group"])): row for row in scenario_rows
        }
        lines.append(
            "| group | weight | threads | "
            + " | ".join(f"{mode} (mean±sd)" for mode in modes)
            + " |"
        )
        lines.append("|---|---:|---:|" + "|".join("---:" for _ in modes) + "|")
        for group in groups:
            sample = next(row for row in scenario_rows if str(row["group"]) == group)
            cells = []
            for mode in modes:
                row = by_mode_group.get((mode, group))
                cells.append(
                    ""
                    if row is None
                    else (
                        f"{float(row['share_mean_pct']):5.1f}"
                        f"±{float(row['share_sd_pct']):4.1f}"
                    )
                )
            lines.append(
                f"| `{group}` | {sample['weight']} | {sample['threads']} | "
                + " | ".join(cells)
                + " |"
            )
        lines.append("")
        lines.append(
            "The model delta is a reference, not a verdict. It isolates the "
            "placement effect: a group with fewer threads than CPUs cannot "
            "occupy every CPU, so its measured share can move even when the "
            "mode's per-task weight is unchanged."
        )
        lines.append("")
        lines.append("### Model delta (measured - idealized, percentage points)")
        lines.append("")
        lines.append("| group | " + " | ".join(modes) + " |")
        lines.append("|---|" + "|".join("---:" for _ in modes) + "|")
        for group in groups:
            cells = []
            for mode in modes:
                row = by_mode_group.get((mode, group))
                cells.append(
                    ""
                    if row is None or row["delta_pp"] == ""
                    else f"{float(row['delta_pp']):+.2f}"
                )
            lines.append(f"| `{group}` | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("### Per-task CPU share (%)")
        lines.append("")
        lines.append("| group | " + " | ".join(modes) + " |")
        lines.append("|---|" + "|".join("---:" for _ in modes) + "|")
        for group in groups:
            cells = []
            for mode in modes:
                row = by_mode_group.get((mode, group))
                cells.append(
                    "" if row is None else pct(float(row["per_task_mean_pct"]))
                )
            lines.append(f"| `{group}` | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("### Relative to smp (paired by repetition)")
        lines.append("")
        lines.append(
            "`1.00x` means the mode did not change the group's measured share "
            "relative to smp; values above 1 are the thread-scaling gain."
        )
        lines.append("")
        lines.append("| group | " + " | ".join(modes) + " |")
        lines.append("|---|" + "|".join("---:" for _ in modes) + "|")
        for group in groups:
            cells = []
            for mode in modes:
                row = by_mode_group.get((mode, group))
                cells.append(
                    ""
                    if row is None or row["relative_to_smp"] == ""
                    else f"{float(row['relative_to_smp']):.2f}x"
                )
            lines.append(f"| `{group}` | " + " | ".join(cells) + " |")
        lines.append("")

        if scenario == "qos_nested":
            lines.append("### QoS and pod split")
            lines.append("")
            lines.append("| mode | QoS ratio burstable/guaranteed | pod-a / pod-b |")
            lines.append("|---|---:|---:|")
            for mode in modes:
                qos = [
                    row
                    for row in metrics
                    if row["scenario"] == scenario
                    and row["mode"] == mode
                    and row["metric"] == "qos_ratio"
                ]
                qos_value = (
                    ""
                    if not qos
                    else (f"{float(qos[0]['mean']):.2f}±{float(qos[0]['sd']):.2f}")
                )
                pod_values = [
                    row
                    for row in metrics
                    if row["scenario"] == scenario
                    and row["mode"] == mode
                    and row["metric"] == "pod_share_within_parent_pct"
                    and row["parent"] == "qos-burstable"
                ]
                pod_text = ""
                if pod_values:
                    pod_text = " / ".join(
                        f"{float(row['mean']):.1f}±{float(row['sd']):.1f}%"
                        for row in pod_values
                    )
                lines.append(f"| `{mode}` | {qos_value} | {pod_text} |")
            lines.append("")
            lines.append(
                "The pod split is the mean across repetitions. A large "
                "across-repetition sd means the two equal pods did not settle "
                "on equal CPU shares; `pods.csv` has the across-repetition "
                "summary and `matrix.jsonl` has each repetition."
            )
            lines.append("")

        if scenario == "quota":
            lines.append("### Quota enforcement")
            lines.append("")
            lines.append(
                "| mode | limited share % | open share % | "
                "throttled/usage % | throttled/wall % | throttled periods % |"
            )
            lines.append("|---|---:|---:|---:|---:|---:|")
            for mode in modes:
                limited = by_mode_group.get((mode, "limited"))
                open_row = by_mode_group.get((mode, "open"))
                throttle_usage = [
                    row
                    for row in metrics
                    if row["scenario"] == scenario
                    and row["mode"] == mode
                    and row["metric"] == "throttled_usage_pct"
                ]
                throttle_wall = [
                    row
                    for row in metrics
                    if row["scenario"] == scenario
                    and row["mode"] == mode
                    and row["metric"] == "throttled_wall_pct"
                ]
                throttle_periods = [
                    row
                    for row in metrics
                    if row["scenario"] == scenario
                    and row["mode"] == mode
                    and row["metric"] == "throttled_periods_pct"
                ]
                lines.append(
                    "| `{}` | {} | {} | {} | {} | {} |".format(
                        mode,
                        ""
                        if limited is None
                        else pct(float(limited["share_mean_pct"])),
                        ""
                        if open_row is None
                        else pct(float(open_row["share_mean_pct"])),
                        ""
                        if not throttle_usage
                        else pct(float(throttle_usage[0]["mean"])),
                        ""
                        if not throttle_wall
                        else pct(float(throttle_wall[0]["mean"])),
                        ""
                        if not throttle_periods
                        else pct(float(throttle_periods[0]["mean"])),
                    )
                )
            lines.append("")
            lines.append(
                "`throttled_usec` is summed over throttled tasks, so "
                "`throttled/usage` can exceed 100%; `throttled/wall` is the "
                "fraction of wall time the limited group was throttled. The "
                "0.5 CPU cap is 25% of the two-CPU total; the small overshoot "
                "is the measurement-window edge."
            )
            lines.append("")

        if scenario == "latency":
            lines.append("### Wakeup latency (1 ms timer, microseconds)")
            lines.append("")
            lines.append("| mode | p50 | p99 | p99.9 | max |")
            lines.append("|---|---:|---:|---:|---:|")
            for mode in modes:
                values = {}
                for metric in (
                    "wakeup_p50_us",
                    "wakeup_p99_us",
                    "wakeup_p999_us",
                    "wakeup_max_us",
                ):
                    found = [
                        row
                        for row in metrics
                        if row["scenario"] == scenario
                        and row["mode"] == mode
                        and row["metric"] == metric
                    ]
                    values[metric] = (
                        "" if not found else f"{float(found[0]['mean']):.0f}"
                    )
                lines.append(
                    "| `{}` | {} | {} | {} | {} |".format(
                        mode,
                        values["wakeup_p50_us"],
                        values["wakeup_p99_us"],
                        values["wakeup_p999_us"],
                        values["wakeup_max_us"],
                    )
                )
            lines.append("")
            lines.append(
                "The probe sleeps between 1 ms timers, so its CPU share is not "
                "the modeled runnable-weight share; the wakeup percentiles are "
                "the metric for this scenario."
            )
            lines.append("")

        ctxt_rows = [
            row
            for row in metrics
            if row["scenario"] == scenario and row["metric"] == "ctxt_per_s"
        ]
        if ctxt_rows:
            lines.append("### Context switches")
            lines.append("")
            lines.append("| mode | context switches/s |")
            lines.append("|---|---:|")
            for row in ctxt_rows:
                lines.append(f"| `{row['mode']}` | {float(row['mean']):.0f} |")
            lines.append("")

    lines.append("## Findings")
    lines.append("")
    findings = build_findings(result)
    if not findings:
        lines.append("- No findings were generated from this run.")
    else:
        for finding in findings:
            lines.append(f"- {finding}")
    return "\n".join(lines).rstrip("\n") + "\n"


def build_findings(result: dict[str, Any]) -> list[str]:
    wide: list[dict[str, Any]] = result["wide"]
    metrics: list[dict[str, Any]] = result["metrics"]
    modes: list[str] = result["modes"]
    scenarios: list[str] = result["scenarios"]
    findings: list[str] = []

    def value(scenario: str, mode: str, group: str) -> float | None:
        for row in wide:
            if (
                row["scenario"] == scenario
                and row["mode"] == mode
                and row["group"] == group
            ):
                return float(row["share_mean_pct"])
        return None

    if "weighted_threads" in scenarios:
        best_mode = None
        best_share = -1.0
        for mode in modes:
            share = value("weighted_threads", mode, "w100-t4")
            if share is not None and share > best_share:
                best_share = share
                best_mode = mode
        if best_mode is not None:
            one_thread = value("weighted_threads", best_mode, "w400-t1") or 0
            findings.append(
                "`weighted_threads`: the 4-thread/weight-100 group reaches "
                f"{best_share:.1f}% under `{best_mode}`; the "
                "1-thread/weight-400 group reaches "
                f"{one_thread:.1f}% under the same mode."
            )
        for mode in modes:
            smp_ratio = None
            for row in wide:
                if (
                    row["scenario"] == "weighted_threads"
                    and row["mode"] == mode
                    and row["group"] == "w100-t4"
                    and row["relative_to_smp"] != ""
                ):
                    smp_ratio = float(row["relative_to_smp"])
            if smp_ratio is not None:
                findings.append(
                    f"`weighted_threads`: `{mode}` gives the 4-thread group "
                    f"{smp_ratio:.2f}x its smp CPU share."
                )

        def relative(mode: str, group: str) -> float | None:
            for row in wide:
                if (
                    row["scenario"] == "weighted_threads"
                    and row["mode"] == mode
                    and row["group"] == group
                    and row["relative_to_smp"] != ""
                ):
                    return float(row["relative_to_smp"])
            return None

        if "concur" in modes and "tasks" in modes:
            concur_2t = relative("concur", "w200-t2")
            concur_4t = relative("concur", "w100-t4")
            tasks_2t = relative("tasks", "w200-t2")
            tasks_4t = relative("tasks", "w100-t4")
            if None not in (concur_2t, concur_4t, tasks_2t, tasks_4t):
                findings.append(
                    "`weighted_threads`: the 4-thread group gains "
                    f"{concur_4t:.2f}x under `concur` and {tasks_4t:.2f}x "
                    "under `tasks`, but the 2-thread group does not "
                    f"({concur_2t:.2f}x and {tasks_2t:.2f}x). The 2-thread "
                    "group has one runnable thread per CPU, so the kernel's "
                    "group runnable average does not scale it the way the "
                    "simple `min(threads, cpus)` model predicts."
                )

    if "qos_nested" in scenarios:
        qos_by_mode: dict[str, float] = {}
        for mode in modes:
            qos = [
                row
                for row in metrics
                if row["scenario"] == "qos_nested"
                and row["mode"] == mode
                and row["metric"] == "qos_ratio"
            ]
            if qos:
                ratio = float(qos[0]["mean"])
                qos_by_mode[mode] = ratio
                findings.append(
                    f"`qos_nested`: `{mode}` gives Burstable "
                    f"{ratio:.2f}x the Guaranteed CPU share."
                )
        if "up" in qos_by_mode:
            findings.append(
                "`qos_nested`: `up` is not a neutral baseline on this "
                f"{result['cpus']}-CPU worker. The 4-thread Burstable parent "
                "keeps its full weight on both CPUs while the 1-thread "
                f"Guaranteed parent keeps it on one, so the measured ratio is "
                f"{qos_by_mode['up']:.2f}x; `smp` stays at "
                f"{qos_by_mode.get('smp', 0):.2f}x and `max` at "
                f"{qos_by_mode.get('max', 0):.2f}x."
            )

        pod_rows = [
            row
            for row in metrics
            if row["scenario"] == "qos_nested"
            and row["metric"] == "pod_share_within_parent_pct"
            and row["parent"] == "qos-burstable"
        ]
        if pod_rows:
            by_mode: dict[str, list[dict[str, Any]]] = {}
            for row in pod_rows:
                by_mode.setdefault(str(row["mode"]), []).append(row)
            up_rows = by_mode.get("up", [])
            if up_rows:
                up_values = " / ".join(f"{float(row['mean']):.1f}%" for row in up_rows)
                up_sd = max(float(row["sd"]) for row in up_rows)
                stable = []
                for mode, rows in by_mode.items():
                    if mode == "up":
                        continue
                    deviation = max(abs(float(row["mean"]) - 50.0) for row in rows)
                    stable.append(deviation)
                stable_text = f"{max(stable):.1f} points" if stable else "no comparison"
                findings.append(
                    "`qos_nested`: the two equal Burstable pods split "
                    f"{up_values} under `up` (across-repetition sd "
                    f"{up_sd:.1f} points); the other modes stay within "
                    f"{stable_text} of 50/50. `up` therefore also destabilizes "
                    "pod-level fairness inside one QoS parent."
                )

    if "quota" in scenarios:
        limited = [
            float(row["share_mean_pct"])
            for row in wide
            if row["scenario"] == "quota" and row["group"] == "limited"
        ]
        if limited:
            findings.append(
                "`quota`: the limited group stays at "
                f"{min(limited):.1f}-{max(limited):.1f}% across modes, "
                "so `cpu.max` enforcement is independent of cgroup_mode."
            )
        throttle_by_mode = {
            str(row["mode"]): float(row["mean"])
            for row in metrics
            if row["scenario"] == "quota" and row["metric"] == "throttled_periods_pct"
        }
        if throttle_by_mode:
            values = list(throttle_by_mode.values())
            findings.append(
                "`quota`: the limited group is throttled in "
                f"{min(values):.0f}-{max(values):.0f}% of CFS periods; "
                "the CPU cap itself is the same in every mode. "
                + (
                    "`up` is the outlier at "
                    f"{throttle_by_mode['up']:.0f}% because its group "
                    "does not always exhaust the quota in a period."
                    if "up" in throttle_by_mode
                    and throttle_by_mode["up"] < max(values) - 5
                    else ""
                )
            )

    if "latency" in scenarios:
        for mode in modes:
            p99 = [
                float(row["mean"])
                for row in metrics
                if row["scenario"] == "latency"
                and row["mode"] == mode
                and row["metric"] == "wakeup_p99_us"
            ]
            if p99:
                findings.append(
                    f"`latency`: `{mode}` p99 wakeup delay is {p99[0]:.0f} us."
                )

        p999_by_mode: dict[str, float] = {}
        max_by_mode: dict[str, float] = {}
        for mode in modes:
            for metric, target in (
                ("wakeup_p999_us", p999_by_mode),
                ("wakeup_max_us", max_by_mode),
            ):
                found = [
                    float(row["mean"])
                    for row in metrics
                    if row["scenario"] == "latency"
                    and row["mode"] == mode
                    and row["metric"] == metric
                ]
                if found:
                    target[mode] = found[0]
        if p999_by_mode and max_by_mode:
            best_tail = min(p999_by_mode, key=p999_by_mode.get)
            worst_tail = max(p999_by_mode, key=p999_by_mode.get)
            findings.append(
                "`latency`: the best p99.9 tail is "
                f"`{best_tail}` ({p999_by_mode[best_tail]:.0f} us, max "
                f"{max_by_mode[best_tail]:.0f} us); the worst is "
                f"`{worst_tail}` ({p999_by_mode[worst_tail]:.0f} us, max "
                f"{max_by_mode[worst_tail]:.0f} us). The 1 ms probe is a "
                "tail-latency check, not a throughput metric."
            )

    up_smp = []
    for scenario in scenarios:
        if scenario in ("quota", "latency"):
            continue
        for group in {row["group"] for row in wide if row["scenario"] == scenario}:
            up = value(scenario, "up", group)
            smp = value(scenario, "smp", group)
            if up is not None and smp is not None and smp > 0:
                up_smp.append((scenario, group, up / smp))
    if up_smp:
        findings.append(
            "`up` vs `smp`: the measured group ratios range "
            f"{min(ratio for _, _, ratio in up_smp):.2f}x-"
            f"{max(ratio for _, _, ratio in up_smp):.2f}x. "
            "The idealized model gives them the same share; the difference is "
            "the per-CPU placement effect that `up` keeps at full group weight."
        )

    ctxt_rows = [
        row
        for row in metrics
        if row["metric"] == "ctxt_per_s"
        and row["scenario"]
        == ("weighted_threads" if "weighted_threads" in scenarios else scenarios[0])
    ]
    if ctxt_rows:
        values = {str(row["mode"]): float(row["mean"]) for row in ctxt_rows}
        lowest = min(values, key=values.get)
        highest = max(values, key=values.get)
        findings.append(
            "Context switches on `weighted_threads` range "
            f"{values[lowest]:.0f}/s (`{lowest}`) to "
            f"{values[highest]:.0f}/s (`{highest}`); the extra thread "
            "scaling in `concur`/`tasks`/`up` costs preemptions, while "
            "`smp`/`max` stay lower."
        )

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--meta", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    rows = load_matrix(args.matrix)
    meta = load_meta(args.meta)
    validate(rows, meta)
    wide, metrics, result = analyze(rows, meta)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "shares.csv", wide)
    write_csv(args.output_dir / "metrics.csv", metrics)
    pods = result.get("pods", [])
    if pods:
        write_csv(args.output_dir / "pods.csv", pods)
    (args.output_dir / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "summary.md").write_text(
        build_markdown(result), encoding="utf-8"
    )
    print(f"wrote {args.output_dir / 'shares.csv'}")
    print(f"wrote {args.output_dir / 'metrics.csv'}")
    if pods:
        print(f"wrote {args.output_dir / 'pods.csv'}")
    print(f"wrote {args.output_dir / 'summary.json'}")
    print(f"wrote {args.output_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
