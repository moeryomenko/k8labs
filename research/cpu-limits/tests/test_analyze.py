"""Tests for fail-closed CPU-limits campaign analysis."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from analysis.analyze import analyze, load_campaign, write_outputs

BLOCK_COLUMNS = (
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
)
CGROUP_COLUMNS = (
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
)
METRIC_COLUMNS = (
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
)


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def campaign_rows() -> tuple[
    list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]
]:
    blocks: list[dict[str, str]] = []
    cgroups: list[dict[str, str]] = []
    metrics: list[dict[str, str]] = []
    for block_id in ("1", "2", "3"):
        for policy, value in (("fit", "10"), ("burstable-low", "8")):
            cell_id = f"cpu-bound-{policy}-solo"
            base = {
                "protocol": "cpu-limits-v1",
                "run_id": "run-1",
                "block_id": block_id,
                "cell_id": cell_id,
            }
            blocks.append(
                {
                    **base,
                    "phase": "confirmatory",
                    "seed": "7",
                    "position": "1",
                    "workload": "cpu-bound",
                    "policy": policy,
                    "condition": "solo",
                    "node": "worker-1",
                    "pod_uid": "pod-1",
                    "status": "complete",
                    "started_at": "2026-10-24T00:00:00Z",
                    "finished_at": "2026-10-24T00:00:15Z",
                }
            )
            for key, measured, unit in (
                ("cpu.stat.usage_usec", str(int(value) * 1_000_000), "microseconds"),
                ("cpu.stat.nr_periods", "100", "count"),
                ("cpu.stat.nr_throttled", "20", "count"),
                ("cpu.stat.throttled_usec", "2000000", "microseconds"),
                ("cpu.max.quota", "100000", "microseconds"),
                ("cpu.max.period", "100000", "microseconds"),
                ("cpu.weight", "100", "weight"),
            ):
                cgroups.append(
                    {
                        **base,
                        "captured_at": "2026-10-24T00:00:15Z",
                        "window": "measurement_delta"
                        if key.startswith("cpu.stat")
                        else "end",
                        "level": "container_leaf",
                        "path": "/kubepods/test",
                        "key": key,
                        "value": measured,
                        "unit": unit,
                    }
                )
            for key, measured in (
                ("cpu.pressure.some_total_usec", "1000000"),
                ("cpu.pressure.full_total_usec", "500000"),
            ):
                cgroups.extend(
                    [
                        {
                            **base,
                            "captured_at": "2026-10-24T00:00:00Z",
                            "window": "start",
                            "level": "container_leaf",
                            "path": "/kubepods/test",
                            "key": key,
                            "value": "0",
                            "unit": "microseconds",
                        },
                        {
                            **base,
                            "captured_at": "2026-10-24T00:00:15Z",
                            "window": "end",
                            "level": "container_leaf",
                            "path": "/kubepods/test",
                            "key": key,
                            "value": measured,
                            "unit": "microseconds",
                        },
                    ]
                )
            for timestamp, metric_value in (
                ("2026-10-24T00:00:00Z", "0"),
                ("2026-10-24T00:00:15Z", value),
            ):
                metrics.append(
                    {
                        **base,
                        "timestamp": timestamp,
                        "source": "workload",
                        "metric": "throughput",
                        "value": metric_value,
                        "unit": "operations_per_second",
                        "labels_json": "{}",
                    }
                )
    return blocks, cgroups, metrics


def write_campaign(tmp_path: Path) -> tuple[Path, Path, Path]:
    blocks, cgroups, metrics = campaign_rows()
    paths = tuple(
        tmp_path / name for name in ("blocks.csv", "cgroups.csv", "metrics.csv")
    )
    write_csv(paths[0], BLOCK_COLUMNS, blocks)
    write_csv(paths[1], CGROUP_COLUMNS, cgroups)
    write_csv(paths[2], METRIC_COLUMNS, metrics)
    return paths


def test_missing_column_is_an_error(tmp_path: Path) -> None:
    blocks, cgroups, metrics = campaign_rows()
    paths = tuple(
        tmp_path / name for name in ("blocks.csv", "cgroups.csv", "metrics.csv")
    )
    write_csv(
        paths[0],
        BLOCK_COLUMNS[:-1],
        [{key: row[key] for key in BLOCK_COLUMNS[:-1]} for row in blocks],
    )
    write_csv(paths[1], CGROUP_COLUMNS, cgroups)
    write_csv(paths[2], METRIC_COLUMNS, metrics)

    with pytest.raises(ValueError, match="missing required column"):
        load_campaign(*paths)


def test_missing_contrast_is_incomplete_not_zero(tmp_path: Path) -> None:
    blocks_path, cgroups_path, metrics_path = write_campaign(tmp_path)
    blocks, cgroups, metrics = campaign_rows()
    blocks = [
        row
        for row in blocks
        if not (row["block_id"] == "3" and row["policy"] == "burstable-low")
    ]
    cgroups = [
        row
        for row in cgroups
        if not (row["block_id"] == "3" and "burstable-low" in row["cell_id"])
    ]
    metrics = [
        row
        for row in metrics
        if not (row["block_id"] == "3" and "burstable-low" in row["cell_id"])
    ]
    write_csv(blocks_path, BLOCK_COLUMNS, blocks)
    write_csv(cgroups_path, CGROUP_COLUMNS, cgroups)
    write_csv(metrics_path, METRIC_COLUMNS, metrics)

    result = analyze(load_campaign(blocks_path, cgroups_path, metrics_path))

    contrast = next(
        item for item in result["contrasts"] if item["family"] == "limit_effect"
    )
    assert contrast["decision"] == "incomplete"
    assert contrast["estimate"] is None
    assert contrast["missing_blocks"] == ["3"]


def test_analyzer_refuses_pilot_rows_and_pilot_tree(tmp_path: Path) -> None:
    blocks_path, cgroups_path, metrics_path = write_campaign(tmp_path)
    blocks, _cgroups, _metrics = campaign_rows()
    blocks[0]["phase"] = "pilot"
    write_csv(blocks_path, BLOCK_COLUMNS, blocks)

    with pytest.raises(ValueError, match="pilot"):
        load_campaign(blocks_path, cgroups_path, metrics_path)

    pilot = tmp_path / "pilot"
    pilot.mkdir()
    pilot_paths = tuple(
        pilot / path.name for path in (blocks_path, cgroups_path, metrics_path)
    )
    for source, destination in zip(
        (blocks_path, cgroups_path, metrics_path), pilot_paths
    ):
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    blocks[0]["phase"] = "confirmatory"
    write_csv(pilot_paths[0], BLOCK_COLUMNS, blocks)

    with pytest.raises(ValueError, match="pilot tree"):
        load_campaign(*pilot_paths)


def test_summary_outputs_report_cell_metrics_and_parameters(tmp_path: Path) -> None:
    paths = write_campaign(tmp_path)
    result = analyze(load_campaign(*paths))
    write_outputs(tmp_path / "analysis", result)

    summary = json.loads(
        (tmp_path / "analysis" / "summary.json").read_text(encoding="utf-8")
    )
    fit = next(cell for cell in summary["cells"] if cell["policy"] == "fit")
    assert fit["cpu_usage_seconds"] is not None
    assert fit["quota_utilization"] == pytest.approx(1.0)
    assert fit["cgroup_parameters"]["cpu.weight"] == "100"
    assert (tmp_path / "analysis" / "summary.md").is_file()


def test_pilot_analysis_is_descriptive_and_sets_the_phase(tmp_path: Path) -> None:
    paths = write_campaign(tmp_path)
    blocks, _cgroups, _metrics = campaign_rows()
    for row in blocks:
        row["phase"] = "pilot"
    write_csv(paths[0], BLOCK_COLUMNS, blocks)

    result = analyze(load_campaign(*paths, expected_phase="pilot"), phase="pilot")
    write_outputs(tmp_path / "analysis", result)

    summary = json.loads(
        (tmp_path / "analysis" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["phase"] == "pilot"
    assert "pilot descriptive" in {row["decision"] for row in summary["contrasts"]}
    assert "pilot descriptive summary" in (
        tmp_path / "analysis" / "summary.md"
    ).read_text(encoding="utf-8")
