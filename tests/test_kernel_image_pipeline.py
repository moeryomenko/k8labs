"""Contracts for the kernel image pipeline: locked RPMs, local repo, signed UKI."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKER = ROOT / "packer"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_packer_uploads_only_the_locked_kernel_rpms() -> None:
    base = _read(PACKER / "base.pkr.hcl")
    assert "source = var.kernel_rpm_source_dir" in base
    assert 'destination = "/var/lib/k8labs-kernel-repo"' in base


def test_packer_installs_the_pinned_nvr_from_the_local_repository() -> None:
    variables = _read(PACKER / "variables.pkr.hcl")
    assert 'default     = "7.3.0-0.0.next.20261016.64.k8.680e037.fc44"' in variables
    assert "file:///var/lib/k8labs-kernel-repo" in variables


def test_provisioner_builds_repository_metadata_for_local_repo() -> None:
    script = _read(PACKER / "scripts" / "01-configure-system.sh")
    assert "createrepo_c" in script
    assert 'case "${KERNEL_REPO_URL}" in' in script


def test_uki_step_fails_closed_without_a_signing_key() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert "K8LABS_SB_KEY" in script
    assert 'fail "K8LABS_SB_KEY is required' in script
    assert 'fail "K8LABS_SB_CERT is required' in script


def test_uki_step_installs_the_firmware_fallback_entry() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert 'UKI_PATH="${UKI_DIR}/BOOTX64.EFI"' in script
    assert 'UKI_DIR="${ESP_MOUNT}/EFI/BOOT"' in script


def test_uki_step_verifies_the_signature_before_shipping() -> None:
    script = _read(PACKER / "scripts" / "04-uki.sh")
    assert "sbverify --cert" in script
    # The verification must gate the bake, not merely log.
    assert "|| fail" in script.split("sbverify --cert", 1)[1][:200]


def test_uki_step_is_wired_into_the_build() -> None:
    base = _read(PACKER / "base.pkr.hcl")
    assert "scripts/04-uki.sh" in base
    assert "K8LABS_SB_KEY=${var.uki_signing_key}" in base
