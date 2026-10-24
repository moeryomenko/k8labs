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
            stream, fieldnames=("cell_id", "workload", "policy", "status")
        )
        writer.writeheader()
        writer.writerow(
            {
                "cell_id": "solo-cpu-bound-burstable-low",
                "workload": "cpu-bound",
                "policy": "burstable-low",
                "status": "complete",
            }
        )
    with cgroups.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=("cell_id", "window", "level", "key", "value")
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


def test_freeze_writes_a_fail_record_and_defaults_without_calibration(
    tmp_path: Path,
) -> None:
    output = tmp_path / "policies.json"

    assert FREEZE.main(["--data-root", str(tmp_path), "--output", str(output)]) == 1

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert all(policy["limitation"] for policy in payload["policies"])
