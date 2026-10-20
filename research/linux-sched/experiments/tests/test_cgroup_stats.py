"""Unit tests for cgroup v2 CPU-stat snapshot processing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cgroup_stats import delta, parse_cpu_stat  # noqa: E402


BEFORE = """usage_usec 100
user_usec 75
system_usec 25
nr_periods 10
nr_throttled 2
throttled_usec 40
"""
AFTER = """usage_usec 190
user_usec 135
system_usec 55
nr_periods 20
nr_throttled 5
throttled_usec 100
"""


def test_parse_cpu_stat_reads_required_cgroup_v2_counters() -> None:
    assert parse_cpu_stat(BEFORE) == {
        "usage_usec": 100,
        "nr_periods": 10,
        "nr_throttled": 2,
        "throttled_usec": 40,
    }


def test_delta_derives_quota_period_ratio_and_throttled_time() -> None:
    assert delta(parse_cpu_stat(BEFORE), parse_cpu_stat(AFTER)) == {
        "usage_usec": 90,
        "nr_periods": 10,
        "nr_throttled": 3,
        "throttled_usec": 60,
        "throttled_period_ratio": pytest.approx(0.3),
    }


def test_parse_cpu_stat_rejects_missing_required_counter() -> None:
    with pytest.raises(
        ValueError, match="missing required cpu.stat counter: nr_throttled"
    ):
        parse_cpu_stat("usage_usec 1\nnr_periods 1\nthrottled_usec 1\n")


def test_delta_rejects_counter_regression() -> None:
    before = parse_cpu_stat(BEFORE)
    after = parse_cpu_stat(
        "usage_usec 90\nnr_periods 20\nnr_throttled 5\nthrottled_usec 100\n"
    )
    with pytest.raises(ValueError, match="counter regressed: usage_usec"):
        delta(before, after)


def test_delta_rejects_zero_periods() -> None:
    before = parse_cpu_stat(
        "usage_usec 1\nnr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"
    )
    after = parse_cpu_stat(
        "usage_usec 2\nnr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"
    )
    with pytest.raises(ValueError, match="no quota periods observed"):
        delta(before, after)


def test_cli_compares_two_snapshots(tmp_path, capsys) -> None:
    import cgroup_stats as cs

    before = tmp_path / "before.stat"
    after = tmp_path / "after.stat"
    before.write_text("usage_usec 100\nnr_periods 100\nnr_throttled 10\nthrottled_usec 50\n")
    after.write_text("usage_usec 200\nnr_periods 200\nnr_throttled 40\nthrottled_usec 90\n")
    assert cs.main(["--before", str(before), "--after", str(after), "--json"]) == 0
    out = capsys.readouterr().out
    assert '"throttled_period_ratio": 0.3' in out
