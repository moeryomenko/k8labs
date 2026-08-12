"""Contract tests for the node validation subcommand.

The subcommand under test is ``scripts/nodes.py --validate``: it checks a
tfvars file for five violation classes, exits 0 when the file is clean, and
exits non-zero naming every offender when it is not:

- duplicate node names (across roles included)
- duplicate MAC addresses
- node names violating Kubernetes label rules (lowercase alphanumerics,
  ``-``/``.``, start/end alphanumeric, at most 63 chars)
- MACs that are malformed (not ``xx:xx:xx:xx:xx:xx`` lowercase hex) or not
  locally administered unicast (second hex digit without the 0x02 bit, or
  first bit of the first byte set)
- total node count beyond the DHCP pool capacity (181 addresses)

A missing ``mac`` field is not a violation: name-only nodes are the trigger
for auto-assignment and must pass validation. Messages go to stderr; a clean
file writes nothing to stderr. Tests invoke the CLI as a subprocess so exit
codes and message shape are pinned exactly. Structural fixtures live in
tests/fixtures/; parametrized name/mac variants and bulk pools are rendered
into tmp_path by the shared ``write_tfvars`` helper.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

MAC_FAMILY = "c6:e5:50:1c:ec"

CliRunner = Callable[..., subprocess.CompletedProcess[str]]
TfvarsWriter = Callable[..., Path]


def parse_list(stdout: str) -> list[dict[str, object]]:
    """Parse --list style stdout as JSON, failing the test on trailing garbage."""
    parsed = json.loads(stdout)
    assert isinstance(parsed, list)
    return parsed


def base_control_plane(mac: str | None = f"{MAC_FAMILY}:01") -> dict[str, object]:
    """A valid control-plane entry; pass mac=None for a name-only node."""
    entry: dict[str, object] = {
        "name": "cp1",
        "cpu": 2,
        "ram": 2048,
        "disk": 20480,
    }
    if mac is not None:
        entry["mac"] = mac
    return entry


def worker_entry(name: str, mac: str | None) -> dict[str, object]:
    """A single worker entry; pass mac=None for a name-only node."""
    entry: dict[str, object] = {
        "name": name,
        "cpu": 2,
        "ram": 4096,
        "disk": 40960,
    }
    if mac is not None:
        entry["mac"] = mac
    return entry


def test_validate_clean_file_exits_zero(run_cli: CliRunner) -> None:
    proc = run_cli("--validate", "--tfvars", str(FIXTURES / "example.tfvars"))

    assert proc.returncode == 0
    assert proc.stderr == ""


def test_validate_allows_name_only_nodes(run_cli: CliRunner) -> None:
    proc = run_cli("--validate", "--tfvars", str(FIXTURES / "missing_mac.tfvars"))

    assert proc.returncode == 0
    assert proc.stderr == ""


def test_validate_duplicate_worker_name_fails_naming_node(
    run_cli: CliRunner,
) -> None:
    proc = run_cli(
        "--validate", "--tfvars", str(FIXTURES / "validate_duplicate_name.tfvars")
    )

    assert proc.returncode != 0
    assert "w1" in proc.stderr


def test_validate_duplicate_name_across_roles_fails_naming_node(
    run_cli: CliRunner, write_tfvars: TfvarsWriter
) -> None:
    path = write_tfvars(
        base_control_plane(),
        [worker_entry("cp1", f"{MAC_FAMILY}:02")],
    )

    proc = run_cli("--validate", "--tfvars", str(path))

    assert proc.returncode != 0
    assert "cp1" in proc.stderr


def test_validate_duplicate_mac_fails_naming_shared_mac(
    run_cli: CliRunner,
) -> None:
    proc = run_cli(
        "--validate", "--tfvars", str(FIXTURES / "validate_duplicate_mac.tfvars")
    )

    assert proc.returncode != 0
    assert f"{MAC_FAMILY}:02" in proc.stderr


@pytest.mark.parametrize(
    "name",
    [
        "W1",  # uppercase
        "w_1",  # underscore
        "-w1",  # leading hyphen
        "w1-",  # trailing hyphen
        ".w1",  # leading dot
        "w1.",  # trailing dot
        "w" * 64,  # longer than 63 chars
    ],
    ids=[
        "uppercase",
        "underscore",
        "leading-hyphen",
        "trailing-hyphen",
        "leading-dot",
        "trailing-dot",
        "too-long",
    ],
)
def test_validate_bad_name_charset_fails_naming_node(
    run_cli: CliRunner, write_tfvars: TfvarsWriter, name: str
) -> None:
    path = write_tfvars(
        base_control_plane(),
        [worker_entry(name, f"{MAC_FAMILY}:02")],
    )

    proc = run_cli("--validate", "--tfvars", str(path))

    assert proc.returncode != 0
    assert name in proc.stderr


def test_validate_empty_name_fails_with_validation_message(
    run_cli: CliRunner, write_tfvars: TfvarsWriter
) -> None:
    path = write_tfvars(
        base_control_plane(),
        [worker_entry("", f"{MAC_FAMILY}:02")],
    )

    proc = run_cli("--validate", "--tfvars", str(path))

    assert proc.returncode != 0
    assert proc.stderr != ""
    # An empty name cannot be quoted back, so pin the failure as a validation
    # verdict rather than an argparse usage error (which is the red phase).
    assert "usage:" not in proc.stderr


@pytest.mark.parametrize(
    "name",
    ["a", "w1.dc2", "w" + "a" * 62, "w1-2"],
    ids=["single-char", "dot-separated", "63-chars", "hyphen-inside"],
)
def test_validate_valid_names_pass(
    run_cli: CliRunner, write_tfvars: TfvarsWriter, name: str
) -> None:
    path = write_tfvars(
        base_control_plane(),
        [worker_entry(name, f"{MAC_FAMILY}:02")],
    )

    proc = run_cli("--validate", "--tfvars", str(path))

    assert proc.returncode == 0
    assert proc.stderr == ""


@pytest.mark.parametrize(
    "mac",
    [
        "c6:e5:50:1c:ec",  # five octets
        "c6-e5-50-1c-ec-04",  # hyphens
        "c6:e5:50:1c:ec:zz",  # non-hex
        "C6:E5:50:1C:EC:04",  # uppercase
        "c6:e5:50:1c:ec:4",  # one-digit octet
        "c6:e5:50:1c:ec:044",  # three-digit octet
    ],
    ids=[
        "five-octets",
        "hyphens",
        "non-hex",
        "uppercase",
        "one-digit-octet",
        "three-digit-octet",
    ],
)
def test_validate_malformed_mac_fails_naming_node(
    run_cli: CliRunner, write_tfvars: TfvarsWriter, mac: str
) -> None:
    path = write_tfvars(
        base_control_plane(),
        [worker_entry("w1", mac)],
    )

    proc = run_cli("--validate", "--tfvars", str(path))

    assert proc.returncode != 0
    assert "w1" in proc.stderr


@pytest.mark.parametrize(
    "mac",
    [
        "00:11:22:33:44:55",  # globally administered, no local bit
        "01:00:5e:00:00:01",  # multicast, first bit of first byte set
        "c0:e5:50:1c:ec:04",  # second hex digit without the 0x02 bit
    ],
    ids=["global-oui", "multicast", "missing-local-bit"],
)
def test_validate_non_local_unicast_mac_fails_naming_node(
    run_cli: CliRunner, write_tfvars: TfvarsWriter, mac: str
) -> None:
    path = write_tfvars(
        base_control_plane(),
        [worker_entry("w1", mac)],
    )

    proc = run_cli("--validate", "--tfvars", str(path))

    assert proc.returncode != 0
    assert "w1" in proc.stderr


@pytest.mark.parametrize(
    "mac",
    [
        "de:ad:be:ef:00:01",  # locally administered unicast, foreign family
        "02:00:00:00:00:01",  # locally administered unicast, minimal byte
        f"{MAC_FAMILY}:fe",  # last assignable octet of the family
    ],
    ids=["foreign-family", "minimal-byte", "family-max-octet"],
)
def test_validate_valid_la_unicast_macs_pass(
    run_cli: CliRunner, write_tfvars: TfvarsWriter, mac: str
) -> None:
    path = write_tfvars(
        base_control_plane(),
        [worker_entry("w1", mac)],
    )

    proc = run_cli("--validate", "--tfvars", str(path))

    assert proc.returncode == 0
    assert proc.stderr == ""


def _pool_tfvars(write_tfvars: TfvarsWriter, worker_count: int, name: str) -> Path:
    """Render a control plane plus worker_count workers with unique MACs."""
    workers = [
        worker_entry(f"w{i}", f"{MAC_FAMILY}:{i + 1:02x}")
        for i in range(1, worker_count + 1)
    ]
    return write_tfvars(base_control_plane(), workers, name)


def test_validate_pool_overflow_fails_stating_capacity(
    run_cli: CliRunner, write_tfvars: TfvarsWriter
) -> None:
    # 182 total nodes (control plane + 181 workers) exceeds the 181-address
    # DHCP pool; every MAC is unique so only the pool class can fire.
    path = _pool_tfvars(write_tfvars, 181, "overflow.tfvars")

    proc = run_cli("--validate", "--tfvars", str(path))

    assert proc.returncode != 0
    assert "181" in proc.stderr


def test_validate_pool_at_capacity_passes(
    run_cli: CliRunner, write_tfvars: TfvarsWriter
) -> None:
    # 181 total nodes (control plane + 180 workers) is exactly the capacity.
    path = _pool_tfvars(write_tfvars, 180, "at-capacity.tfvars")

    proc = run_cli("--validate", "--tfvars", str(path))

    assert proc.returncode == 0
    assert proc.stderr == ""


def test_validate_lists_every_violation(run_cli: CliRunner) -> None:
    # One file with three classes: bad charset (W1), duplicate MAC (ec:02),
    # duplicate name (w3). The report must name all of them.
    proc = run_cli(
        "--validate", "--tfvars", str(FIXTURES / "validate_multi_violation.tfvars")
    )

    assert proc.returncode != 0
    assert "W1" in proc.stderr
    assert f"{MAC_FAMILY}:02" in proc.stderr
    assert "w3" in proc.stderr
