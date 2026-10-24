"""Tests for the read-only cgroup-v2 probe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments import cgroup_probe as PROBE

UID = "01234567-89ab-cdef-0123-456789abcdef"


def _write_level(path: Path, *, usage: int = 100) -> None:
    """Create one complete cgroup-v2 fixture level."""

    path.mkdir(parents=True, exist_ok=True)
    (path / "cpu.weight").write_text("100\n", encoding="utf-8")
    (path / "cpu.max").write_text("max 100000\n", encoding="utf-8")
    (path / "cpu.stat").write_text(
        f"usage_usec {usage}\n"
        "nr_periods 10\n"
        "nr_throttled 2\n"
        "throttled_usec 20\n",
        encoding="utf-8",
    )
    (path / "cpu.pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=3\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=4\n",
        encoding="utf-8",
    )
    (path / "memory.current").write_text("1234\n", encoding="utf-8")
    (path / "memory.peak").write_text("5678\n", encoding="utf-8")
    (path / "cgroup.type").write_text("domain\n", encoding="utf-8")
    (path / "cgroup.controllers").write_text("cpu memory\n", encoding="utf-8")


def _fixture_tree(root: Path) -> Path:
    """Create the Pod hierarchy used by each probe test."""

    uid = UID.replace("-", "_")
    qos = root / "kubepods.slice" / "kubepods-burstable.slice"
    pod = qos / f"kubepods-burstable-pod{uid}.slice"
    _write_level(qos, usage=100)
    _write_level(pod, usage=200)
    _write_level(pod / "crio-target.scope", usage=300)
    _write_level(pod / "crio-target.scope" / "container", usage=400)
    _write_level(pod / "systemd-target.scope", usage=500)
    return pod


def _tree_state(root: Path) -> dict[str, bytes]:
    """Return each fixture file without changing the fixture."""

    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_resolves_pod_slice_and_replaces_uid_hyphens(tmp_path: Path) -> None:
    pod = _fixture_tree(tmp_path)

    assert PROBE.resolve_pod_slice(tmp_path, UID) == pod


def test_snapshot_records_all_cgroup_levels_and_raw_values(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)

    record = PROBE.capture(tmp_path, UID, captured_at="2026-10-24T00:00:00Z")

    assert [level["kind"] for level in record["levels"]] == [
        "qos_slice",
        "pod_slice",
        "container_scope",
        "container_leaf",
        "container_scope",
    ]
    assert record["levels"][2]["scope_type"] == "crio"
    assert record["levels"][4]["scope_type"] == "systemd"
    for level in record["levels"]:
        assert level["raw"] == {
            "cpu.weight": "100",
            "cpu.max": "max 100000",
            "cpu.stat": (
                "usage_usec " + str(level["cpu_stat"]["usage_usec"]) + "\n"
                "nr_periods 10\n"
                "nr_throttled 2\n"
                "throttled_usec 20"
            ),
            "cpu.pressure": (
                "some avg10=0.00 avg60=0.00 avg300=0.00 total=3\n"
                "full avg10=0.00 avg60=0.00 avg300=0.00 total=4"
            ),
            "memory.current": "1234",
            "memory.peak": "5678",
            "cgroup.type": "domain",
            "cgroup.controllers": "cpu memory",
        }
        assert level["cpu_max_quota_us"] is None
        assert level["cpu_max_period_us"] == 100000
        assert level["memory"] == {"current_bytes": 1234, "peak_bytes": 5678}


def test_probe_does_not_write_under_the_cgroup_root(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)
    before = _tree_state(tmp_path)

    PROBE.capture(tmp_path, UID)

    assert _tree_state(tmp_path) == before


def test_cpu_stat_deltas_cover_all_required_counters(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)
    before = PROBE.capture(tmp_path, UID)
    after = PROBE.capture(tmp_path, UID)
    for level in after["levels"]:
        level["cpu_stat"] = {
            "usage_usec": level["cpu_stat"]["usage_usec"] + 7,
            "nr_periods": 13,
            "nr_throttled": 5,
            "throttled_usec": 29,
        }

    deltas = PROBE.cpu_stat_deltas(before, after)

    assert len(deltas) == 5
    assert all(
        delta["cpu_stat"]
        == {
            "usage_usec": 7,
            "nr_periods": 3,
            "nr_throttled": 3,
            "throttled_usec": 9,
        }
        for delta in deltas
    )


def test_missing_required_file_raises_an_error(tmp_path: Path) -> None:
    pod = _fixture_tree(tmp_path)
    (pod / "cpu.pressure").unlink()

    with pytest.raises(FileNotFoundError, match="cpu.pressure"):
        PROBE.capture(tmp_path, UID)


def test_invalid_uid_is_rejected(tmp_path: Path) -> None:
    _fixture_tree(tmp_path)

    with pytest.raises(ValueError, match="pod UID"):
        PROBE.resolve_pod_slice(tmp_path, "../../escape")
