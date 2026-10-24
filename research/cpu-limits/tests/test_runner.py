"""Offline tests for the CPU-limits cell runner and policy freeze step."""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SERIES_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = SERIES_ROOT / "experiments"


def load_script(name: str):
    """Load a hyphenated runner script as an isolated test module."""

    path = EXPERIMENTS / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load_script("run-cell.py")
FREEZE = load_script("freeze_policies.py")


def runner_args(tmp_path: Path):
    """Return one valid non-live runner request."""

    return RUNNER.parse_args(
        [
            "--phase",
            "smoke",
            "--cell-id",
            "solo-cpu-bound-fit",
            "--run-id",
            "test-run",
            "--block-id",
            "1",
            "--position",
            "1",
            "--seed",
            "9",
            "--node",
            "worker-a",
            "--data-root",
            str(tmp_path),
            "--collector-url",
            "http://collector.invalid/metrics",
            "--collector-pod",
            "collector-a",
        ]
    )


def test_parse_args_accepts_a_frozen_cell_and_rejects_unknown_cell(
    tmp_path: Path,
) -> None:
    args = runner_args(tmp_path)

    assert args.cell_id == "solo-cpu-bound-fit"
    with pytest.raises(SystemExit):
        RUNNER.parse_args(
            [
                "--phase",
                "smoke",
                "--cell-id",
                "unknown",
                "--run-id",
                "run",
                "--block-id",
                "1",
                "--position",
                "1",
                "--seed",
                "1",
                "--node",
                "node",
                "--data-root",
                str(tmp_path),
                "--collector-url",
                "http://example.invalid",
                "--collector-pod",
                "collector",
            ]
        )


def test_missing_required_metric_fails_without_a_synthetic_sample() -> None:
    metrics = """\
container_cpu_usage_seconds_total{cell_id="solo-cpu-bound-fit"} 1
container_cpu_cfs_periods_total{cell_id="solo-cpu-bound-fit"} 2
"""

    with pytest.raises(RUNNER.CellRunError, match="missing required metric"):
        RUNNER.parse_metrics(metrics, "solo-cpu-bound-fit", "2026-10-24T00:00:00Z")


def test_runner_marks_cell_failed_when_teardown_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeKubectl:
        def __init__(self, namespace: str, kubeconfig: Path | None) -> None:
            del namespace, kubeconfig

        def run(self, *arguments: str, input_text: str | None = None) -> str:
            del input_text
            if arguments[:2] == ("get", "pod"):
                return json.dumps(
                    {
                        "metadata": {"uid": "01234567-89ab-cdef-0123-456789abcdef"},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                )
            if arguments[:2] == ("delete", "pod"):
                raise RUNNER.CellRunError("delete failed")
            return ""

    snapshot = {
        "captured_at": "2026-10-24T00:00:00Z",
        "levels": [
            {
                "kind": "pod_slice",
                "path": "/cg/pod",
                "cpu_weight": 100,
                "cpu_max_quota_us": 100000,
                "cpu_max_period_us": 100000,
                "cpu_stat": {
                    "usage_usec": 10,
                    "nr_periods": 2,
                    "nr_throttled": 1,
                    "throttled_usec": 3,
                },
                "cpu_pressure": {"some_total_usec": 4, "full_total_usec": 5},
                "memory": {"current_bytes": 6, "peak_bytes": 7},
            }
        ],
    }

    class FakeScraper:
        def __init__(self, url: str, cell_id: str) -> None:
            del url, cell_id

        def start(self) -> None:
            return

        def stop(self) -> list[dict[str, str]]:
            return []

    monkeypatch.setattr(RUNNER, "Kubectl", FakeKubectl)
    monkeypatch.setattr(RUNNER, "snapshot_pod", lambda *unused: snapshot)
    monkeypatch.setattr(RUNNER, "MetricsScraper", FakeScraper)
    monkeypatch.setattr(RUNNER.time, "sleep", lambda unused: None)

    result = RUNNER.run_cell(runner_args(tmp_path))

    assert result.status == "failed"
    assert result.teardown == "failed"
    status_path = (
        tmp_path
        / "smoke"
        / "cells"
        / "block-001"
        / "solo-cpu-bound-fit"
        / "status.json"
    )
    assert json.loads(status_path.read_text(encoding="utf-8"))["teardown"] == "failed"


def test_run_block_uses_the_frozen_seeded_schedule(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            "bash",
            str(EXPERIMENTS / "run-block.sh"),
            "--dry-run",
            "--node",
            "worker-a",
            "--collector-url",
            "http://collector.invalid/metrics",
            "--collector-pod",
            "collector-a",
            "--data-root",
            str(tmp_path),
            "--seed",
            "9",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    rows = completed.stdout.splitlines()
    assert len(rows) == 36
    assert rows[0].startswith("block=1 position=1 cell=")
    assert len({row.rsplit("=", 1)[1] for row in rows}) == 36


def test_run_block_refuses_confirmatory_without_frozen_inputs(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            "bash",
            str(EXPERIMENTS / "run-block.sh"),
            "--phase",
            "confirmatory",
            "--node",
            "worker-a",
            "--collector-url",
            "http://collector.invalid/metrics",
            "--collector-pod",
            "collector-a",
            "--data-root",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "policies.json" in completed.stderr


def write_pilot_files(tmp_path: Path) -> tuple[Path, Path]:
    """Write one complete low-policy calibration candidate."""

    blocks = tmp_path / "blocks.csv"
    cgroups = tmp_path / "cgroups.csv"
    with blocks.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("block_id", "cell_id", "workload", "policy", "status"),
        )
        writer.writeheader()
        writer.writerow(
            {
                "block_id": "1",
                "cell_id": "solo-cpu-bound-burstable-low",
                "workload": "cpu-bound",
                "policy": "burstable-low",
                "status": "complete",
            }
        )
    with cgroups.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("block_id", "cell_id", "window", "level", "key", "value"),
        )
        writer.writeheader()
        for key, value, window in (
            ("cpu.stat.nr_periods", "20", "measurement_delta"),
            ("cpu.stat.nr_throttled", "5", "measurement_delta"),
            ("cpu.max.quota", "50000", "end"),
            ("cpu.max.period", "100000", "end"),
        ):
            writer.writerow(
                {
                    "block_id": "1",
                    "cell_id": "solo-cpu-bound-burstable-low",
                    "window": window,
                    "level": "pod_slice",
                    "key": key,
                    "value": value,
                }
            )
    return cgroups, blocks


def test_freeze_selects_a_low_value_inside_the_throttle_band(tmp_path: Path) -> None:
    cgroups, blocks = write_pilot_files(tmp_path)

    policies = FREEZE.read_pilot_samples(cgroups, blocks)
    frozen = FREEZE.freeze_values("cpu-bound", policies)

    assert frozen.status == "PASS"
    assert frozen.low_millicores == 500
    assert frozen.limitation is None
    assert frozen.evidence[0]["block_id"] == "1"
    assert frozen.evidence[0]["cell_id"] == "solo-cpu-bound-burstable-low"


def test_freeze_retains_each_block_for_a_repeated_cell(tmp_path: Path) -> None:
    blocks = tmp_path / "blocks.csv"
    cgroups = tmp_path / "cgroups.csv"
    with blocks.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("block_id", "cell_id", "workload", "policy", "status"),
        )
        writer.writeheader()
        for block_id in ("1", "2"):
            writer.writerow(
                {
                    "block_id": block_id,
                    "cell_id": "solo-cpu-bound-burstable-low",
                    "workload": "cpu-bound",
                    "policy": "burstable-low",
                    "status": "complete",
                }
            )
    with cgroups.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("block_id", "cell_id", "window", "level", "key", "value"),
        )
        writer.writeheader()
        for block_id, throttled in (("1", "5"), ("2", "7")):
            for key, value, window in (
                ("cpu.stat.nr_periods", "20", "measurement_delta"),
                ("cpu.stat.nr_throttled", throttled, "measurement_delta"),
                ("cpu.max.quota", "50000", "end"),
                ("cpu.max.period", "100000", "end"),
            ):
                writer.writerow(
                    {
                        "block_id": block_id,
                        "cell_id": "solo-cpu-bound-burstable-low",
                        "window": window,
                        "level": "pod_slice",
                        "key": key,
                        "value": value,
                    }
                )

    samples = FREEZE.read_pilot_samples(cgroups, blocks)

    assert [sample.throttled_ratio for sample in samples] == [0.25, 0.35]


def test_freeze_writes_a_fail_record_and_defaults_without_calibration(
    tmp_path: Path,
) -> None:
    output = tmp_path / "policies.json"

    assert FREEZE.main(["--data-root", str(tmp_path), "--output", str(output)]) == 1

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert all(policy["limitation"] for policy in payload["policies"])


def test_parse_wrk2_extracts_throughput_and_latency() -> None:
    text = """\
Running 15s test @ http://10.0.0.1/
  2 threads and 16 connections
  Latency Distribution
     50%    1.25ms
     99%    4.50ms
  Requests/sec: 1234.56
"""
    metrics = RUNNER.parse_wrk2(text)

    assert metrics["requests_per_second"][0] == pytest.approx(1234.56)
    assert metrics["latency_p50_ms"][0] == pytest.approx(1.25)
    assert metrics["latency_p99_ms"][0] == pytest.approx(4.5)


def test_parse_wrk2_rejects_a_missing_percentile() -> None:
    with pytest.raises(RUNNER.CellRunError, match="latency histogram"):
        RUNNER.parse_wrk2("Requests/sec: 10\n  50% 1ms\n")


def test_parse_stress_ng_extracts_bogo_rate() -> None:
    metrics = RUNNER.parse_stress_ng(
        "stress-ng: info: [1] matrixprod: 1234.50 bogo ops/s (real time)\n"
    )

    assert metrics["bogo_ops_per_second"][0] == pytest.approx(1234.5)


def test_parse_stress_ng_extracts_the_table_rate() -> None:
    text = """\
stress-ng: info:  [1] stressor       bogo ops real time  usr time  sys time   bogo ops/s   bogo ops/s
stress-ng: info:  [1]                           (secs)    (secs)    (secs)   (real time) (usr+sys time)
stress-ng: info:  [1] cpu                1938      8.00      7.47      0.00       242.20       259.44
"""
    metrics = RUNNER.parse_stress_ng(text)

    assert metrics["bogo_ops_per_second"][0] == pytest.approx(242.20)


def test_parse_ffmpeg_uses_the_last_fps_value() -> None:
    metrics = RUNNER.parse_ffmpeg("frame= 10 fps=20.0\nframe= 20 fps=25.5\n")

    assert metrics["frames_per_second"][0] == pytest.approx(25.5)


def test_parse_wrk2_accepts_the_histogram_output() -> None:
    text = """\
       0.500     0.500000          500         2.00
       0.900     0.900000          900        10.00
       1.250     0.990000          990       100.00
       1.500     0.999000          999      1000.00
Requests/sec:    400.25
"""
    metrics = RUNNER.parse_wrk2(text)

    assert metrics["requests_per_second"][0] == pytest.approx(400.25)
    assert metrics["latency_p50_ms"][0] == pytest.approx(0.5)
    assert metrics["latency_p99_ms"][0] == pytest.approx(1.25)
