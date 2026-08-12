"""Contract tests for the MAC auto-assignment subcommand.

The subcommand under test is ``scripts/nodes.py --fill-macs``: it assigns the
next monotonically increasing address in the ``c6:e5:50:1c:ec`` family to
every node missing a mac, writes the file back atomically, and prints the
resulting node list as JSON on stdout (same shape as ``--list``). The next
address is one greater than the largest last octet already present in the
family; freed slots (from removed nodes) are never reused, the counter stops
at ``ec:fe`` with a hard error past it, foreign-family MACs do not advance
the counter, and a file with validation violations is refused without being
written. Write-back must preserve unrelated content and formatting
byte-for-byte and must never leave a partial file on failure.

Every test mutates a copy of a fixture in pytest's ``tmp_path``; the
committed fixtures are never touched.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

MAC_FAMILY = "c6:e5:50:1c:ec"

CliRunner = Callable[..., subprocess.CompletedProcess[str]]
TfvarsWriter = Callable[..., Path]


def node_by_name(stdout: str, name: str) -> dict[str, object]:
    """Parse --fill-macs stdout and return the single node with the given name."""
    parsed = json.loads(stdout)
    assert isinstance(parsed, list)
    matches = [node for node in parsed if node["name"] == name]
    assert len(matches) == 1, f"expected exactly one node {name!r}, got {len(matches)}"
    return matches[0]


def test_fill_macs_assigns_next_after_highest_octet(
    run_cli: CliRunner, copy_fixture: Callable[[str], Path]
) -> None:
    path = copy_fixture("fillmacs_basic.tfvars")

    proc = run_cli("--fill-macs", "--tfvars", str(path))

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert node_by_name(proc.stdout, "w3")["mac"] == f"{MAC_FAMILY}:04"
    assert f'"{MAC_FAMILY}:04"' in path.read_text(encoding="utf-8")


def test_fill_macs_rerun_is_byte_identical(
    run_cli: CliRunner, copy_fixture: Callable[[str], Path]
) -> None:
    path = copy_fixture("fillmacs_basic.tfvars")

    first = run_cli("--fill-macs", "--tfvars", str(path))
    assert first.returncode == 0
    assert node_by_name(first.stdout, "w3")["mac"] == f"{MAC_FAMILY}:04"
    bytes_after_first = path.read_bytes()

    second = run_cli("--fill-macs", "--tfvars", str(path))

    assert second.returncode == 0
    assert second.stdout == first.stdout
    assert path.read_bytes() == bytes_after_first


def test_fill_macs_never_reuses_freed_slot(
    run_cli: CliRunner, copy_fixture: Callable[[str], Path]
) -> None:
    # The fixture holds ec:01, ec:02, ec:04 (the ec:03 slot belongs to a
    # removed worker) plus a name-only w4: the counter must go to ec:05, not
    # back down to the freed ec:03.
    path = copy_fixture("fillmacs_freed_slot.tfvars")

    proc = run_cli("--fill-macs", "--tfvars", str(path))

    assert proc.returncode == 0
    assert node_by_name(proc.stdout, "w4")["mac"] == f"{MAC_FAMILY}:05"
    assert f'"{MAC_FAMILY}:05"' in path.read_text(encoding="utf-8")


def test_fill_macs_assigns_multiple_missing_in_order(
    run_cli: CliRunner, write_tfvars: TfvarsWriter
) -> None:
    path = write_tfvars(
        {
            "name": "cp1",
            "cpu": 2,
            "ram": 2048,
            "disk": 20480,
            "mac": f"{MAC_FAMILY}:03",
        },
        [
            {"name": "w1", "cpu": 2, "ram": 4096, "disk": 40960},
            {"name": "w2", "cpu": 2, "ram": 4096, "disk": 40960},
        ],
    )

    proc = run_cli("--fill-macs", "--tfvars", str(path))

    assert proc.returncode == 0
    assert node_by_name(proc.stdout, "w1")["mac"] == f"{MAC_FAMILY}:04"
    assert node_by_name(proc.stdout, "w2")["mac"] == f"{MAC_FAMILY}:05"


def test_fill_macs_boundary_ec_fe_assigns_last_slot(
    run_cli: CliRunner, copy_fixture: Callable[[str], Path]
) -> None:
    path = copy_fixture("fillmacs_boundary_success.tfvars")

    proc = run_cli("--fill-macs", "--tfvars", str(path))

    assert proc.returncode == 0
    assert node_by_name(proc.stdout, "w1")["mac"] == f"{MAC_FAMILY}:fe"


def test_fill_macs_past_ec_fe_fails_without_writing(
    run_cli: CliRunner, copy_fixture: Callable[[str], Path]
) -> None:
    # The fixture holds ec:fe and a name-only w1; the next address would be
    # ec:ff, which is past the last assignable slot, so the run must fail
    # loudly and leave the file byte-identical (no partial write).
    path = copy_fixture("fillmacs_overflow.tfvars")
    before = path.read_bytes()

    proc = run_cli("--fill-macs", "--tfvars", str(path))

    assert proc.returncode != 0
    assert "w1" in proc.stderr
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


def test_fill_macs_preserves_unrelated_blocks_byte_for_byte(
    run_cli: CliRunner, copy_fixture: Callable[[str], Path]
) -> None:
    path = copy_fixture("fillmacs_preserve.tfvars")

    proc = run_cli("--fill-macs", "--tfvars", str(path))

    assert proc.returncode == 0
    assert node_by_name(proc.stdout, "w3")["mac"] == f"{MAC_FAMILY}:04"
    written = path.read_text(encoding="utf-8")
    preserved = [
        'base_image_path = "../build/k8labs-base.qcow2" # trailing comment',
        'firmware_path   = "../build/CLOUDHV.fd"',
        "  cpu  = 2 # control plane cores",
        '    mac  = "c6:e5:50:1c:ec:02"',
        '    mac  = "c6:e5:50:1c:ec:03"',
        "    cpu  = 4",
        "    disk = 81920",
        'ssh_public_key = "ssh-ed25519 AAAAC3... user@host"',
    ]
    for block in preserved:
        assert block in written, f"unrelated block not preserved: {block!r}"
    assert f'"{MAC_FAMILY}:04"' in written


def test_fill_macs_fills_control_plane_too(
    run_cli: CliRunner, write_tfvars: TfvarsWriter
) -> None:
    path = write_tfvars(
        {"name": "cp1", "cpu": 2, "ram": 2048, "disk": 20480},
        [
            {
                "name": "w1",
                "cpu": 2,
                "ram": 4096,
                "disk": 40960,
                "mac": f"{MAC_FAMILY}:01",
            },
            {
                "name": "w2",
                "cpu": 2,
                "ram": 4096,
                "disk": 40960,
                "mac": f"{MAC_FAMILY}:02",
            },
        ],
    )

    proc = run_cli("--fill-macs", "--tfvars", str(path))

    assert proc.returncode == 0
    assert node_by_name(proc.stdout, "cp1")["mac"] == f"{MAC_FAMILY}:03"


def test_fill_macs_counter_ignores_other_mac_families(
    run_cli: CliRunner, write_tfvars: TfvarsWriter
) -> None:
    # A foreign-family MAC (locally administered unicast, e.g. the packer VM
    # address) must not advance the c6:e5:50:1c:ec counter: with ec:02 in the
    # family and a de:ad:...:ff address present, the next assignment is ec:03,
    # not an overflow error.
    path = write_tfvars(
        {
            "name": "cp1",
            "cpu": 2,
            "ram": 2048,
            "disk": 20480,
            "mac": "de:ad:be:ef:00:ff",
        },
        [
            {
                "name": "w1",
                "cpu": 2,
                "ram": 4096,
                "disk": 40960,
                "mac": f"{MAC_FAMILY}:02",
            },
            {"name": "w2", "cpu": 2, "ram": 4096, "disk": 40960},
        ],
    )

    proc = run_cli("--fill-macs", "--tfvars", str(path))

    assert proc.returncode == 0
    assert node_by_name(proc.stdout, "w2")["mac"] == f"{MAC_FAMILY}:03"


def test_fill_macs_refuses_invalid_file_without_writing(
    run_cli: CliRunner, copy_fixture: Callable[[str], Path]
) -> None:
    # Validation applies to every subcommand that consumes the file: a
    # duplicate name must block assignment and leave the file untouched.
    path = copy_fixture("validate_duplicate_name.tfvars")
    before = path.read_bytes()

    proc = run_cli("--fill-macs", "--tfvars", str(path))

    assert proc.returncode != 0
    assert "w1" in proc.stderr
    assert path.read_bytes() == before
