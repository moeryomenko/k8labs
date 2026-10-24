"""Tests for pilot cgroup parameter mapping."""

from __future__ import annotations

from pathlib import Path

from analysis.cgroup_mapping import summarize_cgroup_mapping
from tests.test_analyze import write_campaign


def test_mapping_reports_configured_resources_and_actual_levels(tmp_path: Path) -> None:
    blocks_path, cgroups_path, _metrics_path = write_campaign(tmp_path)

    mapping = summarize_cgroup_mapping(blocks_path, cgroups_path, phase="confirmatory")

    row = next(item for item in mapping if item["policy"] == "fit")
    assert row["request_millicores"] == 2000
    assert row["limit_millicores"] == 2000
    assert row["pod_cpu_weight"] == "not recorded"
    assert row["container_cpu_weight"] == "100"
    assert row["container_cpu_max"] == "100000 100000"
