"""Tests for the read-only CPU-limits Prometheus collector."""

from __future__ import annotations

import json
from pathlib import Path

from observers.collector import Collector, FixtureInventory


def _write_cgroup(
    root: Path,
    relative_path: str,
    *,
    cpu_stat: str,
    cpu_max: str = "50000 100000\n",
) -> None:
    """Write the cgroup files needed by one test target."""

    cgroup = root / relative_path
    cgroup.mkdir(parents=True)
    (cgroup / "cpu.stat").write_text(cpu_stat, encoding="utf-8")
    (cgroup / "cpu.weight").write_text("200\n", encoding="utf-8")
    (cgroup / "cpu.max").write_text(cpu_max, encoding="utf-8")
    (cgroup / "memory.current").write_text("1024\n", encoding="utf-8")
    (cgroup / "memory.stat").write_text("inactive_file 24\n", encoding="utf-8")
    (cgroup / "cpu.pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=2500000\n",
        encoding="utf-8",
    )


def _inventory_file(path: Path, relative_path: str) -> Path:
    """Write one offline pod inventory with characters that need escaping."""

    path.write_text(
        json.dumps(
            {
                "pods": [
                    {
                        "uid": "pod-uid-1",
                        "namespace": "cpu-limits",
                        "pod": 'quote"pod',
                        "qos": "Burstable",
                        "cell_id": "cell\none",
                        "containers": [
                            {
                                "name": "app\\main",
                                "cgroups": {"container_leaf": relative_path},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_exposition_has_help_type_values_and_escaped_labels(tmp_path: Path) -> None:
    """The collector emits valid metric families for one target cgroup."""

    relative_path = "kubepods.slice/podpod-uid-1/app.scope"
    _write_cgroup(
        tmp_path,
        relative_path,
        cpu_stat=(
            "usage_usec 1500000\nnr_periods 12\nnr_throttled 3\nthrottled_usec 250000\n"
        ),
    )
    inventory = FixtureInventory.from_file(
        _inventory_file(tmp_path / "inventory.json", relative_path)
    )
    exposition = Collector(tmp_path, inventory, node="worker-a").render_metrics()

    assert "# HELP container_cpu_usage_seconds_total" in exposition
    assert "# TYPE container_cpu_usage_seconds_total counter" in exposition
    assert "# TYPE k8labs_cgroup_cpu_weight gauge" in exposition
    assert 'pod="quote\\"pod"' in exposition
    assert 'container="app\\\\main"' in exposition
    assert 'cell_id="cell\\none"' in exposition
    assert "container_cpu_usage_seconds_total" in exposition
    assert " 1.5\n" in exposition
    assert "container_memory_working_set_bytes" in exposition
    assert " 1000\n" in exposition
    assert "container_pressure_cpu_waiting_seconds_total" in exposition
    assert " 2.5\n" in exposition
    assert "k8labs_cgroup_cpu_max_quota" in exposition
    assert " 50000\n" in exposition


def test_unknown_uid_returns_no_cgroups_or_unrelated_series(tmp_path: Path) -> None:
    """An unknown UID cannot expose a target or an unrelated host cgroup."""

    target_path = "kubepods.slice/podpod-uid-1/app.scope"
    unrelated_path = "system.slice/unrelated.service"
    _write_cgroup(tmp_path, target_path, cpu_stat="usage_usec 1\n")
    _write_cgroup(tmp_path, unrelated_path, cpu_stat="usage_usec 999999\n")
    inventory = FixtureInventory.from_file(
        _inventory_file(tmp_path / "inventory.json", target_path)
    )
    collector = Collector(tmp_path, inventory, node="worker-a")

    assert collector.cgroups_for_uid("unknown-uid") == []
    exposition = collector.render_metrics()
    assert "unrelated.service" not in exposition
    assert "999999" not in exposition


def test_missing_counters_are_not_emitted(tmp_path: Path) -> None:
    """Missing optional cpu.stat counters do not become false zero values."""

    relative_path = "kubepods.slice/podpod-uid-1/app.scope"
    _write_cgroup(
        tmp_path,
        relative_path,
        cpu_stat="usage_usec 500000\nnr_periods 7\n",
        cpu_max="max 100000\n",
    )
    inventory = FixtureInventory.from_file(
        _inventory_file(tmp_path / "inventory.json", relative_path)
    )
    exposition = Collector(tmp_path, inventory, node="worker-a").render_metrics()

    assert "container_cpu_usage_seconds_total" in exposition
    assert "container_cpu_cfs_periods_total" in exposition
    assert "container_cpu_cfs_throttled_periods_total{" not in exposition
    assert "container_cpu_cfs_throttled_seconds_total{" not in exposition
    assert "k8labs_cgroup_cpu_max_quota{" not in exposition
    assert "k8labs_cgroup_cpu_max_period" in exposition
