"""Unit tests for the node-native observer provisioning gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import observer_check  # noqa: E402
from observer_check import REQUIRED_PACKAGES, _dnf_install_block, check  # noqa: E402


def test_repository_state_passes_the_gate() -> None:
    assert check() == []


def test_dnf_install_block_collects_continuations() -> None:
    text = "dnf install -y \\\n    bpftrace \\\n    kernel-tools\nother command\n"
    block = _dnf_install_block(text)
    assert "bpftrace" in block and "kernel-tools" in block
    assert "other command" not in block


def test_observer_check_required_packages() -> None:
    assert set(REQUIRED_PACKAGES) == {
        "bpftrace",
        "perf",
        "bcc-tools",
        "libbpf-tools",
        "python3-bcc",
        "bpftool",
    }


def test_versioned_package_satisfies_the_gate(tmp_path: Path, monkeypatch) -> None:
    # kernel-devel is installed as kernel-devel-<version>; the match must accept
    # the version suffix while still rejecting substring matches.
    provisioner = tmp_path / "provisioner.sh"
    provisioner.write_text(
        "dnf install -y bpftrace perf bcc-tools libbpf-tools python3-bcc bpftool "
        '"kernel-devel-${KERNEL_VERSION}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(observer_check, "PACKER_PROVISIONER", provisioner)
    errors = check()
    assert not any("does not install" in e for e in errors)


def test_missing_package_is_reported(tmp_path: Path, monkeypatch) -> None:
    provisioner = tmp_path / "provisioner.sh"
    provisioner.write_text("dnf install -y bpftrace\n", encoding="utf-8")
    monkeypatch.setattr(observer_check, "PACKER_PROVISIONER", provisioner)
    errors = check()
    assert any("perf" in e and "does not install" in e for e in errors)


def test_package_substring_does_not_match(tmp_path: Path, monkeypatch) -> None:
    # `bpftool` must not satisfy the `bpftrace` requirement.
    provisioner = tmp_path / "provisioner.sh"
    provisioner.write_text("dnf install -y bpftool\n", encoding="utf-8")
    monkeypatch.setattr(observer_check, "PACKER_PROVISIONER", provisioner)
    errors = check()
    assert any("bpftrace" in e and "does not install" in e for e in errors)


def test_reintroduced_privileged_manifests_are_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(observer_check, "SERIES_DIR", tmp_path)
    (tmp_path / "telemetry").mkdir()
    errors = check()
    assert any("telemetry still exists" in e for e in errors)
