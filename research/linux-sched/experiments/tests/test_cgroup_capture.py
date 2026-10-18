from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cgroup_capture as cc  # noqa: E402
import cgroup_stats as cs  # noqa: E402

BEFORE = """usage_usec 100
nr_periods 10
nr_throttled 2
throttled_usec 40
"""
AFTER = """usage_usec 190
nr_periods 20
nr_throttled 5
throttled_usec 100
"""


def test_capture_parses() -> None:
    assert cc.capture_cpu_stat(BEFORE)["nr_periods"] == 10


def test_mixed_pod_total() -> None:
    stats = {"a": cs.parse_cpu_stat(BEFORE), "b": cs.parse_cpu_stat(AFTER)}
    total = cc.capture_mixed_pod_total(stats)
    assert total["nr_periods"] == 30


def test_pod_preserves_containers() -> None:
    stats = {"a": cs.parse_cpu_stat(BEFORE), "b": cs.parse_cpu_stat(AFTER)}
    pod = cc.capture_pod_cpu_stats(stats)
    assert set(pod["containers"]) == {"a", "b"}
    assert pod["total"]["nr_periods"] == 30
