"""Tests for the custom Cloud Hypervisor firmware and signed UKI build."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "firmware_uki_build.py"

_spec = importlib.util.spec_from_file_location("firmware_uki_build", SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_firmware_source_commit_is_pinned() -> None:
    assert len(mod.FIRMWARE_SOURCE_COMMIT) == 40
    assert mod.FIRMWARE_SOURCE_COMMIT == mod.PINNED_FIRMWARE_COMMIT
    assert mod.FIRMWARE_SOURCE_COMMIT == mod.EDK2_SOURCE_COMMIT


def test_required_pin_fields_present_in_manifest() -> None:
    manifest = mod.load_manifest()
    assert manifest, "manifest must load"
    errors = mod.check_required_pin_fields(manifest)
    assert errors == [], errors


def test_uki_hash_matches_artifact() -> None:
    manifest = mod.manifest_pin_fields(mod.load_manifest())
    uki_path = ROOT / mod.UKI_OUTPUT
    if uki_path.is_file():
        assert manifest["uki_sha256"] == mod.sha256_file(uki_path)


def test_qcow2_hash_matches_artifact() -> None:
    manifest = mod.manifest_pin_fields(mod.load_manifest())
    qcow2_path = ROOT / mod.QCOW2_PATH
    if qcow2_path.is_file():
        assert manifest["qcow2_sha256"] == mod.sha256_file(qcow2_path)


def test_no_private_key_material_committed() -> None:
    findings = mod.scan_for_private_key_material(ROOT)
    assert findings == [], findings


def test_public_key_fingerprint_is_recorded() -> None:
    manifest = mod.manifest_pin_fields(mod.load_manifest())
    assert len(manifest["public_key_fingerprint"]) == 64
    assert manifest["public_key_fingerprint"] == mod.PUBLIC_KEY_FINGERPRINT


def test_secure_boot_is_required() -> None:
    assert mod.SECURE_BOOT_REQUIRED is True


def test_uki_installs_as_fallback_boot_path() -> None:
    assert mod.UKI_INSTALL_PATH == "EFI/BOOT/BOOTX64.EFI"
    assert mod.UKI_FALLBACK_PATH == "EFI/BOOT/BOOTX64.EFI"


def test_check_passes() -> None:
    assert mod.check() == []
