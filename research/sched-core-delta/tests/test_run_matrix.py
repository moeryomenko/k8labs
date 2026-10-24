"""Offline contract tests for the worker-side matrix runner."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import run_matrix


def test_parse_cpulist_handles_ranges_and_lists() -> None:
    assert run_matrix.parse_cpulist("0-1,4") == [0, 1, 4]
    assert run_matrix.parse_cpulist("3") == [3]
    assert run_matrix.preferred_count("0-1") == 2
    assert run_matrix.preferred_count("") == 0


def test_read_stat_keeps_the_steal_field(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "stat"
    path.write_text(
        "cpu  100 0 50 1000 0 0 0 20 0 0\n"
        "cpu0 50 0 25 500 0 0 0 10 0 0\n"
        "cpu1 50 0 25 500 0 0 0 10 0 0\n"
        "ctxt 42\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_matrix, "PROC_STAT", path)
    stat = run_matrix.read_stat()
    assert stat["aggregate"]["steal"] == 20
    assert stat["aggregate"]["total"] == 1170
    assert stat["aggregate"]["idle"] == 1000
    assert stat["ctxt"] == 42
    assert stat["per_cpu"][0]["steal"] == 10


def test_stat_delta_reports_steal_and_context_switches() -> None:
    before = {
        "aggregate": {"total": 1000, "idle": 800, "steal": 10},
        "per_cpu": {
            0: {"total": 500, "idle": 400, "steal": 5},
            1: {"total": 500, "idle": 400, "steal": 5},
        },
        "ctxt": 100,
    }
    after = {
        "aggregate": {"total": 2000, "idle": 1500, "steal": 60},
        "per_cpu": {
            0: {"total": 1000, "idle": 750, "steal": 30},
            1: {"total": 1000, "idle": 750, "steal": 30},
        },
        "ctxt": 550,
    }
    delta = run_matrix.stat_delta(before, after)
    assert delta["total_usec"] == 1000
    assert delta["steal_usec"] == 50
    assert delta["steal_pct_total"] == 5.0
    assert delta["ctxt"] == 450
    assert delta["steal_pct_max_cpu"] == 5.0


def test_merge_cpu_workload_computes_rate() -> None:
    row = {"elapsed_s": 10.0}
    run_matrix.merge_workload(row, "cpu", {"iterations": 2500, "threads": 2})
    assert row["iterations"] == 2500
    assert row["iterations_per_s"] == 250.0
    assert row["cpu_threads"] == 2


def test_merge_periodic_workload_reports_fairness_and_missed_periods() -> None:
    row = {"elapsed_s": 10.0}
    run_matrix.merge_workload(
        row,
        "periodic",
        {
            "samples": 8000,
            "iterations": 8000,
            "overruns": 2,
            "missed_periods": 4,
            "p50_us": 10,
            "p99_us": 100,
            "p999_us": 200,
            "max_us": 300,
            "thread_cv": 0.125,
            "thread_iters": [2000, 2000, 2000, 2000],
        },
    )
    assert row["samples"] == 8000
    assert row["p99_us"] == 100
    assert row["missed_periods"] == 4
    assert row["thread_cv"] == 0.125
    assert row["missed_period_rate_pct"] == 4 / 8004 * 100


def test_merge_churn_and_pingpong_workloads() -> None:
    row = {"elapsed_s": 8.0}
    run_matrix.merge_workload(
        row,
        "churn",
        {
            "rate": 200,
            "work_us": 20,
            "created": 1600,
            "completed": 1600,
            "tasks_per_s": 200.0,
            "p50_us": 10,
            "p99_us": 80,
            "p999_us": 120,
            "max_us": 200,
            "completion_p50_us": 30,
            "completion_p99_us": 100,
            "completion_p999_us": 150,
            "completion_max_us": 250,
        },
    )
    assert row["tasks_per_s"] == 200.0
    assert row["p99_us"] == 80
    assert row["completion_p99_us"] == 100

    run_matrix.merge_workload(
        row,
        "pingpong",
        {
            "round_trips": 50000,
            "round_trips_per_s": 6250.0,
            "burn_us": 20,
            "pinned": 1,
            "failed": 0,
            "p50_us": 20,
            "p99_us": 80,
            "p999_us": 150,
            "max_us": 300,
        },
    )
    assert row["round_trips_per_s"] == 6250.0
    assert row["pinned"] == 1
    assert row["failed"] == 0


def test_cpu_workload_covers_warmup_and_measurement(tmp_path: Path) -> None:
    command = run_matrix.workload_command(
        spec={"kind": "cpu", "threads": 2},
        binary=tmp_path / "cpu_burn",
        output=tmp_path / "out.json",
        duration=8.0,
        warmup=2.0,
        go_load=tmp_path / "go-load",
        go_probe=tmp_path / "go-probe",
        cpus=2,
    )
    assert command[command.index("--duration") + 1] == "10.5"
    assert command[command.index("--threads") + 1] == "2"


def test_periodic_and_pingpong_commands_carry_their_arguments(tmp_path: Path) -> None:
    periodic = run_matrix.workload_command(
        spec={"kind": "periodic", "threads": 4, "period_us": 2000, "burn_us": 200},
        binary=tmp_path / "periodic_burn",
        output=tmp_path / "out.json",
        duration=8.0,
        warmup=2.0,
        go_load=tmp_path / "go-load",
        go_probe=tmp_path / "go-probe",
        cpus=2,
    )
    assert periodic[periodic.index("--period-us") + 1] == "2000"
    assert periodic[periodic.index("--burn-us") + 1] == "200"
    assert periodic[periodic.index("--threads") + 1] == "4"

    pingpong = run_matrix.workload_command(
        spec={"kind": "pingpong", "burn_us": 20},
        binary=tmp_path / "pingpong",
        output=tmp_path / "out.json",
        duration=8.0,
        warmup=2.0,
        go_load=tmp_path / "go-load",
        go_probe=tmp_path / "go-probe",
        cpus=2,
    )
    assert pingpong[pingpong.index("--pin") + 1] == "1"
    assert pingpong[pingpong.index("--pin-cpu") + 1] == "1"
    assert pingpong[pingpong.index("--burn-us") + 1] == "20"


def test_plan_is_seeded_and_balanced_within_a_rep() -> None:
    args = argparse.Namespace(
        run_id="test",
        arm="stock",
        block="01",
        reps=2,
        duration=3.0,
        warmup=1.0,
        cooldown=0.5,
        parent_weight=10000,
        seed=42,
    )
    plan = run_matrix.plan(args, ["wakeup", "quota", "slice"])
    assert plan["run_id"] == "test"
    assert plan["arm"] == "stock"
    assert len(plan["reps"]) == 2
    for rep in plan["reps"]:
        assert sorted(rep["scenario_order"]) == ["quota", "slice", "wakeup"]
    assert plan["scenarios"]["quota"][0]["name"] == "limited"


def test_steal_requires_a_governor_arm() -> None:
    # The CLI refuses this combination before any cluster or root action; the
    # plan path is not reachable with an unsupported arm/scenario pair.
    assert run_matrix.ALL_ARMS == (
        "stock",
        "custom",
        "custom-nogov",
        "custom-gov",
    )
    with pytest.raises(SystemExit):
        run_matrix.main(
            [
                "--arm",
                "stock",
                "--plan",
                "--scenarios",
                "steal",
                "--out",
                "/tmp/unused",
                "--meta",
                "/tmp/unused",
            ]
        )
