"""Contract tests for the worker-side matrix runner and the host script."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
import run_matrix

SERIES = Path(__file__).parents[1]
SCRIPT = SERIES / "experiments" / "run-matrix.sh"


def test_weighted_threads_specs_match_the_scenario() -> None:
    specs = run_matrix.group_specs("weighted_threads")
    assert [
        (spec["name"], spec["weight"], spec["threads"], spec["workload"])
        for spec in specs
    ] == [
        ("w400-t1", 400, 1, "cpu"),
        ("w200-t2", 200, 2, "cpu"),
        ("w100-t4", 100, 4, "cpu"),
    ]


def test_nested_specs_include_the_parent_and_both_pods() -> None:
    specs = run_matrix.group_specs("qos_nested")
    by_name = {str(spec["name"]): spec for spec in specs}
    assert set(by_name) == {
        "qos-guaranteed",
        "qos-burstable",
        "qos-burstable-pod-a",
        "qos-burstable-pod-b",
    }
    assert by_name["qos-guaranteed"]["kind"] == "parent"
    assert by_name["qos-guaranteed"]["workload"] == "cpu"
    assert by_name["qos-burstable"]["kind"] == "parent"
    assert by_name["qos-burstable"]["workload"] == "none"
    assert by_name["qos-burstable"]["subtree"] is True
    assert by_name["qos-burstable-pod-a"]["kind"] == "child"
    assert by_name["qos-burstable-pod-a"]["parent"] == "qos-burstable"
    assert by_name["qos-burstable-pod-a"]["workload"] == "cpu"


def test_quota_spec_carries_the_cpu_max() -> None:
    specs = run_matrix.group_specs("quota")
    by_name = {str(spec["name"]): spec for spec in specs}
    assert by_name["limited"]["cpu_max"] == "50000 100000"
    assert by_name["open"]["cpu_max"] == ""


def test_stat_delta_clamps_a_rewound_counter() -> None:
    before = {"usage_usec": 100, "nr_periods": 4}
    after = {"usage_usec": 90, "nr_periods": 6}
    delta = run_matrix.stat_delta(before, after)
    assert delta["usage_usec"] == 0
    assert delta["nr_periods"] == 2
    assert delta["throttled_usec"] == 0


def test_parse_json_returns_empty_for_a_missing_file(tmp_path: Path) -> None:
    assert run_matrix.parse_json(tmp_path / "missing.json") == {}


def test_plan_is_seeded_and_contains_every_mode() -> None:
    args = argparse.Namespace(
        reps=2,
        seed=20261024,
        run_id="test",
        duration=3.0,
        warmup=1.0,
        cooldown=1.0,
    )
    first = run_matrix.plan(
        args, ["weighted_threads"], ["up", "smp", "concur", "max", "tasks"]
    )
    second = run_matrix.plan(
        args, ["weighted_threads"], ["up", "smp", "concur", "max", "tasks"]
    )
    assert first == second
    for rep in first["reps"]:
        assert sorted(rep["mode_order"]) == sorted(
            ["up", "smp", "concur", "max", "tasks"]
        )


def test_main_plan_does_not_require_root(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        run_matrix.main(
            [
                "--plan",
                "--out",
                "/tmp/unused-matrix.jsonl",
                "--meta",
                "/tmp/unused-meta.json",
                "--run-id",
                "plan-test",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "plan-test"
    assert payload["scenarios"]
    assert payload["kernel"]


def test_host_script_is_syntactically_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_host_script_help_does_not_touch_the_cluster() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage: run-matrix.sh" in result.stdout
    assert "--setup" in result.stdout
