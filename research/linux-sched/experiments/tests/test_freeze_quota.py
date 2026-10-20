"""Tests for freezing the per-workload CPU quota from pilot calibration."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parents[1]
SCRIPT = HERE / "freeze_quota.py"

_spec = importlib.util.spec_from_file_location("freeze_quota", SCRIPT)
assert _spec and _spec.loader
fq = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fq
_spec.loader.exec_module(fq)


def _write(path: Path, rows: list[dict[str, str]]) -> Path:
    fields = ["workload", "cpu_limit", "throttled_period_ratio", "healthy"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _good_rows() -> list[dict[str, str]]:
    return [
        {
            "workload": "http",
            "cpu_limit": "750m",
            "throttled_period_ratio": "0.14",
            "healthy": "true",
        },
        {
            "workload": "ffmpeg",
            "cpu_limit": "2",
            "throttled_period_ratio": "0.12",
            "healthy": "true",
        },
        {
            "workload": "postgres",
            "cpu_limit": "1500m",
            "throttled_period_ratio": "0.18",
            "healthy": "yes",
        },
        {
            "workload": "stress-ng",
            "cpu_limit": "1",
            "throttled_period_ratio": "0.25",
            "healthy": "1",
        },
        {
            "workload": "mixed-http-ffmpeg",
            "cpu_limit": "1750m",
            "throttled_period_ratio": "0.11",
            "healthy": "true",
        },
    ]


def test_freezes_every_confirmatory_workload(tmp_path: Path) -> None:
    rows = _write(tmp_path / "cal.csv", _good_rows())
    manifest = fq.freeze(fq.read_calibrations(rows))
    assert manifest["kind"] == "frozen-cpu-quotas"
    assert [q["workload"] for q in manifest["quotas"]] == list(
        fq.CONFIRMATORY_WORKLOADS
    )
    assert manifest["quotas"][0]["cpu_limit"] == "750m"
    assert manifest["target_throttled_period_ratio"] == 0.10


def test_below_target_is_rejected(tmp_path: Path) -> None:
    rows = _good_rows()
    rows[0]["throttled_period_ratio"] = "0.04"
    path = _write(tmp_path / "cal.csv", rows)
    with pytest.raises(ValueError, match="http: throttled_period_ratio"):
        fq.freeze(fq.read_calibrations(path))


def test_unhealthy_workload_is_rejected(tmp_path: Path) -> None:
    rows = _good_rows()
    rows[2]["healthy"] = "false"
    path = _write(tmp_path / "cal.csv", rows)
    with pytest.raises(ValueError, match="postgres: workload is not healthy"):
        fq.freeze(fq.read_calibrations(path))


def test_over_throttled_is_rejected(tmp_path: Path) -> None:
    rows = _good_rows()
    rows[0]["throttled_period_ratio"] = "0.97"   # http, a production workload
    path = _write(tmp_path / "cal.csv", rows)
    with pytest.raises(ValueError, match="over-throttled"):
        fq.freeze(fq.read_calibrations(path))


def test_missing_and_duplicate_workloads_are_rejected(tmp_path: Path) -> None:
    rows = _good_rows()[:-1]
    path = _write(tmp_path / "cal.csv", rows)
    with pytest.raises(ValueError, match="mixed-http-ffmpeg: no calibration row"):
        fq.freeze(fq.read_calibrations(path))

    duplicated = _good_rows() + [_good_rows()[0]]
    path = _write(tmp_path / "dup.csv", duplicated)
    with pytest.raises(ValueError, match="duplicate calibration row"):
        fq.freeze(fq.read_calibrations(path))


def test_manifest_fingerprint_is_stable() -> None:
    rows = [
        fq.Calibration(
            r["workload"], r["cpu_limit"], float(r["throttled_period_ratio"]), True
        )
        for r in _good_rows()
    ]
    first = fq.freeze(rows)
    second = fq.freeze(list(reversed(rows)))
    assert fq.fingerprint(first) == fq.fingerprint(second)
    assert len(fq.fingerprint(first)) == 64


def test_saturation_control_may_exceed_the_healthy_band(tmp_path: Path) -> None:
    rows = _good_rows()
    rows[3]["throttled_period_ratio"] = "0.98"   # stress-ng
    path = _write(tmp_path / "cal.csv", rows)
    manifest = fq.freeze(fq.read_calibrations(path))
    stress = [q for q in manifest["quotas"] if q["workload"] == "stress-ng"][0]
    assert stress["observed_throttled_period_ratio"] == 0.98


def test_production_workload_still_rejects_over_throttling(tmp_path: Path) -> None:
    rows = _good_rows()
    rows[0]["throttled_period_ratio"] = "0.98"   # http
    path = _write(tmp_path / "cal.csv", rows)
    with pytest.raises(ValueError, match="http: throttled_period_ratio"):
        fq.freeze(fq.read_calibrations(path))
