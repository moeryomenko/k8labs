"""Tests for the fail-closed CPU limits runtime gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

import runtime_gate


def make_root(tmp_path: Path, *, focused: bool = False) -> Path:
    root = tmp_path / "root"
    write(root, "/sys/fs/cgroup/cgroup.controllers", "cpu io memory\n")
    write(root, "/proc/sys/kernel/osrelease", "7.3.0-k8labs\n")
    write(root, "/sys/kernel/debug/sched/cgroup_mode", "concur\n")
    write(root, "/sys/kernel/debug/sched/base_slice_ns", "3000000\n")
    for path in runtime_gate.WRITABLE_DEBUG_PATHS:
        (root / path.relative_to("/")).chmod(0o444)
    write(root, "/fixtures/input.mp4", "fixture\n")
    write(root, "/sys/fs/cgroup/cpu.max", "max 100000\n")
    write(root, "/sys/fs/cgroup/cpu.weight", "100\n")
    for command in runtime_gate.REQUIRED_COMMANDS:
        write(root, f"/usr/bin/{command}", "#!/bin/sh\n")
        (root / "usr" / "bin" / command).chmod(0o755)
    if focused:
        for path in runtime_gate.WRITABLE_DEBUG_PATHS:
            (root / path.relative_to("/")).chmod(0o644)
    return root


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative.removeprefix("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def checks_by_name(result: runtime_gate.GateResult) -> dict[str, runtime_gate.Check]:
    return {check.name: check for check in result.checks}


def test_complete_fake_root_passes_every_required_check(tmp_path: Path) -> None:
    result = runtime_gate.run_gate(make_root(tmp_path))

    assert result.status == "PASS"
    assert len(result.checks) == len(runtime_gate.BASE_CHECKS)
    assert {check.status for check in result.checks} == {"PASS"}


def test_every_required_path_and_command_has_a_check(tmp_path: Path) -> None:
    result = runtime_gate.run_gate(make_root(tmp_path))
    checks = checks_by_name(result)

    assert "cgroup_v2" in checks
    assert "cpu_controller" in checks
    assert "kernel_release" in checks
    assert "cgroup_mode_readable" in checks
    assert "base_slice_ns_readable" in checks
    assert "cpu_max_readable" in checks
    assert "cpu_weight_readable" in checks
    assert "fixture_input_mp4" in checks
    for command in runtime_gate.REQUIRED_COMMANDS:
        assert f"command_{command}" in checks


@pytest.mark.parametrize(
    ("check_name", "relative"),
    (
        ("cgroup_v2", "sys/fs/cgroup/cgroup.controllers"),
        ("kernel_release", "proc/sys/kernel/osrelease"),
        ("cgroup_mode_readable", "sys/kernel/debug/sched/cgroup_mode"),
        ("base_slice_ns_readable", "sys/kernel/debug/sched/base_slice_ns"),
        ("command_bpftrace", "usr/bin/bpftrace"),
        ("command_perf", "usr/bin/perf"),
        ("command_wrk2", "usr/bin/wrk2"),
        ("fixture_input_mp4", "fixtures/input.mp4"),
        ("cpu_max_readable", "sys/fs/cgroup/cpu.max"),
        ("cpu_weight_readable", "sys/fs/cgroup/cpu.weight"),
    ),
)
def test_missing_required_path_fails_closed(
    tmp_path: Path, check_name: str, relative: str
) -> None:
    root = make_root(tmp_path)
    (root / relative).unlink()

    result = runtime_gate.run_gate(root)

    assert result.status == "FAIL"
    assert checks_by_name(result)[check_name].status == "FAIL"
    assert all(check.status in {"PASS", "FAIL"} for check in result.checks)


def test_missing_cpu_controller_fails_closed(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    write(root, "/sys/fs/cgroup/cgroup.controllers", "io memory\n")

    result = runtime_gate.run_gate(root)

    assert result.status == "FAIL"
    assert checks_by_name(result)["cpu_controller"].status == "FAIL"


def test_focused_gate_requires_writable_debug_files(tmp_path: Path) -> None:
    root = make_root(tmp_path)

    result = runtime_gate.run_gate(root, focused=True)

    assert result.status == "FAIL"
    assert checks_by_name(result)["cgroup_mode_writable"].status == "FAIL"
    assert checks_by_name(result)["base_slice_ns_writable"].status == "FAIL"


def test_focused_gate_passes_when_debug_files_are_writable(tmp_path: Path) -> None:
    result = runtime_gate.run_gate(make_root(tmp_path, focused=True), focused=True)

    assert result.status == "PASS"
    assert len(result.checks) == len(runtime_gate.BASE_CHECKS) + len(
        runtime_gate.WRITABLE_DEBUG_PATHS
    )


def test_cli_writes_json_and_returns_failure_for_missing_path(
    tmp_path: Path, capsys
) -> None:
    root = make_root(tmp_path)
    (root / "sys" / "fs" / "cgroup" / "cpu.max").unlink()
    output = tmp_path / "gate.json"

    assert runtime_gate.main(["--root", str(root), "--json", str(output)]) == 1

    captured = capsys.readouterr().out
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert "FAIL cpu_max_readable" in captured
    assert payload["status"] == "FAIL"
    assert payload["checks"] == [
        check.as_dict() for check in runtime_gate.run_gate(root).checks
    ]


def test_gate_never_reports_pass_for_a_skipped_required_check(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "usr" / "bin" / "perf").unlink()

    result = runtime_gate.run_gate(root)

    assert result.status == "FAIL"
    assert checks_by_name(result)["command_perf"].status == "FAIL"
    assert "SKIP" not in {check.status for check in result.checks}
