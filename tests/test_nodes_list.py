"""CLI contract tests for the node parser/list tool.

Pins the behavior of ``scripts/nodes.py`` against tfvars fixtures. The tool
parses an HCL tfvars file (control_plane block + workers list) and exposes two
subcommands under test here:

``--list``
    Print a JSON array of nodes: ``[{name, role, cpu, ram, disk, mac}]``.
    ``role`` is ``control-plane`` for the control-plane node and ``worker``
    for every workers[] entry; ``mac`` is ``""`` when the field is absent.
    Order is control-plane first, then workers in tfvars order.

``--worker-macs``
    Print the worker MACs space-separated, in tfvars order. Exits non-zero
    with a message when any worker lacks a MAC. Empty when there are no
    workers.

Both subcommands take ``--tfvars <path>``; a missing or malformed file exits
non-zero with a message on stderr. Tests invoke the CLI as a subprocess so
exit codes and stdout/stderr shape are pinned exactly. Fixtures live in
``tests/fixtures/``; ``example.tfvars`` is a byte-copy of the committed
terraform example file and the canonical input.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "scripts" / "nodes.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures"

EXPECTED_EXAMPLE_NODES = [
    {
        "name": "cp1",
        "role": "control-plane",
        "cpu": 2,
        "ram": 2048,
        "disk": 20480,
        "mac": "c6:e5:50:1c:ec:01",
    },
    {
        "name": "w1",
        "role": "worker",
        "cpu": 2,
        "ram": 4096,
        "disk": 40960,
        "mac": "c6:e5:50:1c:ec:02",
    },
    {
        "name": "w2",
        "role": "worker",
        "cpu": 4,
        "ram": 4096,
        "disk": 40960,
        "mac": "c6:e5:50:1c:ec:03",
    },
]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run scripts/nodes.py in the repo venv with the given args."""
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
        check=False,
    )


def parse_list(stdout: str) -> list[dict[str, object]]:
    """Parse --list stdout as JSON, failing the test on trailing garbage."""
    parsed = json.loads(stdout)
    assert isinstance(parsed, list)
    return parsed


def test_list_on_example_returns_exactly_cp1_w1_w2() -> None:
    proc = run_cli("--list", "--tfvars", str(FIXTURES / "example.tfvars"))

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert parse_list(proc.stdout) == EXPECTED_EXAMPLE_NODES


def test_worker_macs_on_example_are_space_separated_in_order() -> None:
    proc = run_cli("--worker-macs", "--tfvars", str(FIXTURES / "example.tfvars"))

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout.strip() == "c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03"
    assert proc.stdout.split() == [
        "c6:e5:50:1c:ec:02",
        "c6:e5:50:1c:ec:03",
    ]


def test_missing_tfvars_file_exits_nonzero_with_message() -> None:
    missing = FIXTURES / "does-not-exist.tfvars"

    proc = run_cli("--list", "--tfvars", str(missing))

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert str(missing) in proc.stderr


def test_malformed_tfvars_exits_nonzero_with_message() -> None:
    proc = run_cli("--list", "--tfvars", str(FIXTURES / "malformed.tfvars"))

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert proc.stderr != ""
    # The failure must come from parsing the file, not from the tool itself
    # being absent on disk.
    assert "No such file" not in proc.stderr


def test_list_with_empty_workers_returns_only_control_plane() -> None:
    proc = run_cli("--list", "--tfvars", str(FIXTURES / "empty_workers.tfvars"))

    assert proc.returncode == 0
    assert parse_list(proc.stdout) == [
        {
            "name": "cp1",
            "role": "control-plane",
            "cpu": 2,
            "ram": 2048,
            "disk": 20480,
            "mac": "c6:e5:50:1c:ec:01",
        }
    ]


def test_worker_macs_with_no_workers_prints_nothing() -> None:
    proc = run_cli("--worker-macs", "--tfvars", str(FIXTURES / "empty_workers.tfvars"))

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout.strip() == ""


def test_list_renders_absent_mac_as_empty_string() -> None:
    proc = run_cli("--list", "--tfvars", str(FIXTURES / "missing_mac.tfvars"))

    assert proc.returncode == 0
    nodes = parse_list(proc.stdout)
    w1 = next(node for node in nodes if node["name"] == "w1")
    assert w1["mac"] == ""


def test_worker_macs_fails_naming_node_without_mac() -> None:
    proc = run_cli("--worker-macs", "--tfvars", str(FIXTURES / "missing_mac.tfvars"))

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "w1" in proc.stderr


def test_formatting_variance_preserves_example_values() -> None:
    formatted = run_cli("--list", "--tfvars", str(FIXTURES / "formatting.tfvars"))
    canonical = run_cli("--list", "--tfvars", str(FIXTURES / "example.tfvars"))

    assert formatted.returncode == 0
    assert canonical.returncode == 0
    assert parse_list(formatted.stdout) == parse_list(canonical.stdout)


def test_non_w_prefixed_worker_is_listed_and_keeps_order() -> None:
    proc = run_cli("--list", "--tfvars", str(FIXTURES / "non_w_name.tfvars"))

    assert proc.returncode == 0
    assert parse_list(proc.stdout) == [
        {
            "name": "cp1",
            "role": "control-plane",
            "cpu": 2,
            "ram": 2048,
            "disk": 20480,
            "mac": "c6:e5:50:1c:ec:01",
        },
        {
            "name": "gpu-1",
            "role": "worker",
            "cpu": 8,
            "ram": 16384,
            "disk": 102400,
            "mac": "c6:e5:50:1c:ec:04",
        },
        {
            "name": "w2",
            "role": "worker",
            "cpu": 4,
            "ram": 4096,
            "disk": 40960,
            "mac": "c6:e5:50:1c:ec:03",
        },
    ]

    macs = run_cli("--worker-macs", "--tfvars", str(FIXTURES / "non_w_name.tfvars"))
    assert macs.returncode == 0
    assert macs.stdout.split() == ["c6:e5:50:1c:ec:04", "c6:e5:50:1c:ec:03"]


def test_single_worker_returns_control_plane_and_worker() -> None:
    proc = run_cli("--list", "--tfvars", str(FIXTURES / "single_worker.tfvars"))

    assert proc.returncode == 0
    assert [node["name"] for node in parse_list(proc.stdout)] == ["cp1", "w1"]
