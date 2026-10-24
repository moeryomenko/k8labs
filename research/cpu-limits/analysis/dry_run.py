"""Run the CPU-limits analysis path with clearly marked synthetic data.

This command is an offline integration check. It does not contact Kubernetes and
it does not create, claim, or replace a real measurement.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

SERIES_ROOT = Path(__file__).resolve().parents[1]
if str(SERIES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERIES_ROOT))

from analysis.analyze import (
    BLOCK_COLUMNS,
    CGROUP_COLUMNS,
    METRIC_COLUMNS,
    PROTOCOL,
    analyze,
    load_campaign,
    write_outputs,
)
from analysis.plots import render_figures
from analysis.report import compose_report
from analysis.sample_size import estimate_sample_sizes
from experiments.design import schedule

SYNTHETIC = "true"
BLOCKS = 3
SEED = 20261024


def _columns(required: frozenset[str]) -> tuple[str, ...]:
    """Return stable source columns plus the synthetic-data marker."""

    return tuple(sorted(required)) + ("synthetic",)


def _csv_rows(
    phase: str, *, blocks: int = BLOCKS, seed: int = SEED
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Create complete synthetic rows with the campaign's real CSV shape."""

    generator = random.Random(seed + (0 if phase == "pilot" else 1))
    block_rows: list[dict[str, str]] = []
    cgroup_rows: list[dict[str, str]] = []
    metric_rows: list[dict[str, str]] = []
    throughput_baseline = {
        "cpu-bound": 1000.0,
        "io-bound": 800.0,
        "memory-bound": 600.0,
        "hybrid": 500.0,
    }
    latency_baseline = {"io-bound": 12.0, "hybrid": 25.0}
    policy_effect = {
        "fit": 1.0,
        "best-effort": 0.94,
        "request-low": 0.96,
        "request-high": 1.01,
        "guaranteed-low": 0.89,
        "burstable-low": 0.86,
    }

    for block_number, cells in enumerate(schedule(blocks, seed), start=1):
        block_id = f"synthetic-{block_number:02d}"
        for position, cell in enumerate(cells, start=1):
            noise = math.exp(generator.gauss(0.0, 0.01))
            throughput = (
                throughput_baseline[cell.workload] * policy_effect[cell.policy] * noise
            )
            latency = latency_baseline.get(cell.workload)
            if latency is not None:
                latency = latency / policy_effect[cell.policy] * noise
            quota = (
                "max"
                if cell.limit_millicores is None
                else str(cell.limit_millicores * 100)
            )
            weight = max(1, cell.request_millicores or 1)
            throttled_ratio = (
                0.02
                if cell.limit_millicores is None
                else min(0.75, 0.10 + (1.0 - policy_effect[cell.policy]) * 2.0)
            )
            periods = 150
            throttled_periods = round(periods * throttled_ratio)
            usage_seconds = 10.0 * policy_effect[cell.policy] * noise
            base = {
                "protocol": PROTOCOL,
                "run_id": f"synthetic-{phase}",
                "block_id": block_id,
                "cell_id": cell.cell_id,
                "synthetic": SYNTHETIC,
            }
            block_rows.append(
                {
                    **base,
                    "phase": phase,
                    "seed": str(seed),
                    "position": str(position),
                    "workload": cell.workload,
                    "policy": cell.policy,
                    "condition": cell.condition,
                    "node": "synthetic-worker",
                    "pod_uid": f"synthetic-{phase}-{block_number}-{position}",
                    "status": "complete",
                    "started_at": "2026-10-24T00:00:00Z",
                    "finished_at": "2026-10-24T00:00:15Z",
                }
            )
            for key, value, unit, window in (
                (
                    "cpu.stat.usage_usec",
                    str(round(usage_seconds * 1_000_000)),
                    "microseconds",
                    "measurement_delta",
                ),
                ("cpu.stat.nr_periods", str(periods), "count", "measurement_delta"),
                (
                    "cpu.stat.nr_throttled",
                    str(throttled_periods),
                    "count",
                    "measurement_delta",
                ),
                (
                    "cpu.stat.throttled_usec",
                    str(throttled_periods * 30_000),
                    "microseconds",
                    "measurement_delta",
                ),
                ("cpu.max.quota", quota, "microseconds", "end"),
                ("cpu.max.period", "100000", "microseconds", "end"),
                ("cpu.weight", str(weight), "weight", "end"),
            ):
                cgroup_rows.append(
                    {
                        **base,
                        "captured_at": "2026-10-24T00:00:15Z",
                        "window": window,
                        "level": "container_leaf",
                        "path": f"/synthetic/{cell.cell_id}",
                        "key": key,
                        "value": value,
                        "unit": unit,
                    }
                )
            for key, value in (
                ("cpu.pressure.some_total_usec", "1000000"),
                ("cpu.pressure.full_total_usec", "250000"),
            ):
                for window, measured in (("start", "0"), ("end", value)):
                    cgroup_rows.append(
                        {
                            **base,
                            "captured_at": "2026-10-24T00:00:15Z",
                            "window": window,
                            "level": "container_leaf",
                            "path": f"/synthetic/{cell.cell_id}",
                            "key": key,
                            "value": measured,
                            "unit": "microseconds",
                        }
                    )
            metric_rows.append(
                {
                    **base,
                    "timestamp": "2026-10-24T00:00:15Z",
                    "source": "synthetic-workload",
                    "metric": "throughput",
                    "value": f"{throughput:.6f}",
                    "unit": "operations_per_second",
                    "labels_json": '{"synthetic":true}',
                }
            )
            if latency is not None:
                metric_rows.append(
                    {
                        **base,
                        "timestamp": "2026-10-24T00:00:15Z",
                        "source": "synthetic-workload",
                        "metric": "latency_p99_ms",
                        "value": f"{latency:.6f}",
                        "unit": "milliseconds",
                        "labels_json": '{"synthetic":true}',
                    }
                )
    return block_rows, cgroup_rows, metric_rows


def _write_csv(
    path: Path, fields: Iterable[str], rows: Iterable[Mapping[str, str]]
) -> None:
    """Write one marked source file with a deterministic header."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_phase(path: Path, phase: str) -> tuple[Path, Path, Path]:
    """Write a complete synthetic phase and return its three CSV paths."""

    blocks, cgroups, metrics = _csv_rows(phase)
    paths = (path / "blocks.csv", path / "cgroups.csv", path / "metrics.csv")
    _write_csv(paths[0], _columns(BLOCK_COLUMNS), blocks)
    _write_csv(paths[1], _columns(CGROUP_COLUMNS), cgroups)
    _write_csv(paths[2], _columns(METRIC_COLUMNS), metrics)
    return paths


def _mark_result(value: object) -> object:
    """Add the marker to each JSON object that represents synthetic output."""

    if isinstance(value, dict):
        return {
            **{key: _mark_result(item) for key, item in value.items()},
            "synthetic": True,
        }
    if isinstance(value, list):
        return [_mark_result(item) for item in value]
    return value


def _mark_summary_csv(path: Path) -> None:
    """Add the marker to every exported summary CSV row."""

    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or ())
        rows = list(reader)
    if "synthetic" not in fields:
        fields.append("synthetic")
    for row in rows:
        row["synthetic"] = SYNTHETIC
    _write_csv(path, fields, rows)


def _write_marked_summary(path: Path) -> None:
    """Put the marker at the start of the human-readable summary."""

    original = path.read_text(encoding="utf-8")
    path.write_text(
        "# CPU-limits synthetic dry-run summary\n\n"
        "SYNTHETIC DATA ONLY: this offline integration check is not a real measurement.\n\n"
        + original,
        encoding="utf-8",
    )


def run(output_dir: Path) -> dict[str, object]:
    """Write synthetic source rows, sample sizing, analysis, figures, and report."""

    pilot_paths = _write_phase(output_dir / "pilot", "pilot")
    confirmatory_paths = _write_phase(output_dir / "confirmatory", "confirmatory")
    pilot = load_campaign(*pilot_paths, expected_phase="pilot")
    sample_size = _mark_result(estimate_sample_sizes(pilot))
    (output_dir / "sample-size.json").write_text(
        json.dumps(sample_size, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    result = _mark_result(analyze(load_campaign(*confirmatory_paths)))
    assert isinstance(result, dict)
    analysis_dir = output_dir / "analysis"
    write_outputs(analysis_dir, result)
    _mark_summary_csv(analysis_dir / "summary.csv")
    _write_marked_summary(analysis_dir / "summary.md")
    figures = render_figures(result, analysis_dir / "figures")
    report = compose_report(
        result,
        summary_markdown=(analysis_dir / "summary.md").read_text(encoding="utf-8"),
        sample_size=sample_size,
        figures=figures,
    )
    report = report.replace(
        "# CPU-limits results",
        "# CPU-limits synthetic dry-run report",
        1,
    ).replace(
        "This report describes complete confirmatory rows only.",
        "SYNTHETIC DATA ONLY: this offline integration check is not a real measurement.",
        1,
    )
    (analysis_dir / "report.md").write_text(report, encoding="utf-8")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline dry run without accessing a Kubernetes cluster."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.output_dir)
    print(
        "wrote synthetic data, summary, and figures: "
        f"{len(result['cells'])} confirmatory cells"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
