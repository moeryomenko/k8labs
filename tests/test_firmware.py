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


def _pins() -> str:
    """The always-required source and trust pins, as manifest text."""
    return (
        "firmware_source_repo: https://github.com/tianocore/edk2.git\n"
        "firmware_source_commit: " + mod.FIRMWARE_SOURCE_COMMIT + "\n"
        "firmware_source_tag: " + mod.EDK2_SOURCE_TAG + "\n"
        "firmware_arch: " + mod.FIRMWARE_ARCH + "\n"
        "public_key_fingerprint: " + mod.PUBLIC_KEY_FINGERPRINT + "\n"
    )


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


def test_present_artifact_without_recorded_hash_fails(tmp_path: Path) -> None:
    artifact = tmp_path / mod.QCOW2_PATH
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"unverified image")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(_pins(), encoding="utf-8")
    errors = mod.check(manifest, root=tmp_path)
    assert any("no qcow2_sha256 is recorded" in error for error in errors), errors


def test_present_artifact_with_wrong_hash_fails(tmp_path: Path) -> None:
    artifact = tmp_path / mod.QCOW2_PATH
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"custom kernel image")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        _pins() + 'qcow2_sha256: "' + "0" * 64 + '"\n', encoding="utf-8"
    )
    errors = mod.check(manifest, root=tmp_path)
    assert any("hash mismatch" in error for error in errors), errors


def test_present_artifact_with_matching_hash_passes(tmp_path: Path) -> None:
    artifact = tmp_path / mod.QCOW2_PATH
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"custom kernel image")
    digest = mod.sha256_file(artifact)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(_pins() + f"qcow2_sha256: {digest}\n", encoding="utf-8")
    assert mod.check(manifest, root=tmp_path) == []


def test_absent_artifact_records_no_hash() -> None:
    """A committed manifest must not claim a hash for an unbuilt artifact."""
    manifest = mod.manifest_pin_fields(mod.load_manifest())
    for relative, field in (
        (mod.UKI_OUTPUT, "uki_sha256"),
        (mod.QCOW2_PATH, "qcow2_sha256"),
    ):
        if not (ROOT / relative).is_file():
            assert manifest.get(field) in (None, ""), (
                f"{field} claims a hash but {relative} is not built"
            )


def test_no_placeholder_zero_hashes_recorded() -> None:
    manifest = mod.manifest_pin_fields(mod.load_manifest())
    for field in ("uki_sha256", "qcow2_sha256"):
        assert manifest.get(field) != "0" * 64
