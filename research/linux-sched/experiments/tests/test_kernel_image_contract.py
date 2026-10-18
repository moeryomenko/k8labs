"""Tests for the kernel-image contract gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kernel_image_contract as kic  # noqa: E402
from kernel_image_contract import (  # noqa: E402
    AVAILABLE_RAWHIDE_KERNEL,
    PINNED_KERNEL_VERSION,
    SCHED_EXT_FOR_7_4_COMMIT,
    check,
)


def test_repository_state_passes_the_gate() -> None:
    assert check() == []


def test_sched_ext_commit_is_recorded() -> None:
    assert SCHED_EXT_FOR_7_4_COMMIT == "1f5dde349ac2fca1f119ae8de3f0d2f1c2a8d338"


def test_no_arena_scalar_package_resolves() -> None:
    assert kic.ARENA_SCALAR_AVAILABLE is False
    assert AVAILABLE_RAWHIDE_KERNEL.startswith("7.3.0-")


def test_pinned_kernel_is_consistent(tmp_path, monkeypatch) -> None:
    # Drift in any surface is reported.
    import yaml

    lock = tmp_path / "versions.lock.yaml"
    lock.write_text(
        "fedora:\n  kernel: 7.0.0-0.fc00\n", encoding="utf-8"
    )
    monkeypatch.setattr(kic, "VERSIONS_LOCK", lock)
    errors = check()
    assert any("versions.lock.yaml" in e for e in errors)


def test_fail_closed_provisioner_is_required(tmp_path, monkeypatch) -> None:
    provisioner = tmp_path / "provisioner.sh"
    provisioner.write_text(
        'echo "Exact kernel 1.0 is unavailable; installing current"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(kic, "PACKER_PROVISIONER", provisioner)
    errors = check()
    assert any("fail-closed" in e for e in errors)
