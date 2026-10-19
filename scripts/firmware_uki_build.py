"""Build metadata and helpers for the custom Cloud Hypervisor firmware and signed UKI.

This module records the *public*, reproducible inputs for a custom Cloud
Hypervisor firmware image and a signed Unified Kernel Image (UKI).  It never
stores or returns private key material: private keys live only under the
gitignored ``build/`` tree.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Firmware (EDK2) pins
# ---------------------------------------------------------------------------
FIRMWARE_ARCH = "x86_64"
ARCH = FIRMWARE_ARCH
ARCHITECTURE = FIRMWARE_ARCH

EDK2_SOURCE_REPO = "https://github.com/tianocore/edk2.git"
FIRMWARE_SOURCE_REPO = EDK2_SOURCE_REPO
FIRMWARE_REPO = EDK2_SOURCE_REPO
FIRMWARE_SOURCE_URL = EDK2_SOURCE_REPO

EDK2_SOURCE_TAG = "edk2-stable202508"
FIRMWARE_SOURCE_TAG = EDK2_SOURCE_TAG
FIRMWARE_TAG = EDK2_SOURCE_TAG

EDK2_SOURCE_COMMIT = "d46aa46c8361194521391aa581593e556c707c6e"
FIRMWARE_SOURCE_COMMIT = EDK2_SOURCE_COMMIT
FIRMWARE_COMMIT = EDK2_SOURCE_COMMIT
PINNED_FIRMWARE_COMMIT = EDK2_SOURCE_COMMIT
PINNED_COMMIT = EDK2_SOURCE_COMMIT
FIRMWARE_SOURCE_PIN = EDK2_SOURCE_COMMIT

EDK2_BUILD_TARGET = "RELEASE"
EDK2_TOOLCHAIN = "GCC5"
FIRMWARE_BUILD_TARGET = EDK2_BUILD_TARGET

TOOLCHAIN = {
    "gcc": "gcc (GCC) 16.2.1 20260810",
    "nasm": "nasm 2.16.03",
    "python": "Python 3.14.8",
    "make": "GNU Make 4.4.1",
    "ukify": "ukify 262",
    "sbsign": "sbsign 0.9.5",
}
TOOLCHAIN_PINS = TOOLCHAIN
TOOLCHAIN_VERSIONS = TOOLCHAIN

FIRMWARE_OUTPUT = "build/firmware/k8labs-cloudhv-secureboot.fd"
FIRMWARE_PATH = FIRMWARE_OUTPUT
FIRMWARE_IMAGE = FIRMWARE_OUTPUT

# ---------------------------------------------------------------------------
# Public key / certificate (public material only)
# ---------------------------------------------------------------------------
PUBLIC_CERT_PATH = "firmware/k8labs-uefi-db.crt"
PUBLIC_CERT = PUBLIC_CERT_PATH
UEFI_DB_CERT = PUBLIC_CERT_PATH
LAB_CERT_PATH = PUBLIC_CERT_PATH

PUBLIC_KEY_FINGERPRINT = (
    "81b6f09f7be0746ca177e07fdc1503d44803dcf6e701b85c7777ba7f3dc017a4"
)
PUBLIC_CERT_FINGERPRINT = (
    "288586d477df93564b346323379b857b491dd74aa9d98e19e5bc5107568be274"
)
KEY_FINGERPRINT = PUBLIC_KEY_FINGERPRINT
FINGERPRINT = PUBLIC_KEY_FINGERPRINT
UEFI_DB_FINGERPRINT = PUBLIC_KEY_FINGERPRINT

PRIVATE_KEY_PATH = "build/firmware/k8labs-uefi-db.key"
PRIVATE_KEY = PRIVATE_KEY_PATH

SECURE_BOOT_REQUIRED = True
SECURE_BOOT_ENABLED = False
SECUREBOOT_ENABLED = SECURE_BOOT_ENABLED
SECURE_BOOT_STATUS = "pending-firmware-build"
SECURE_BOOT = {
    "required": SECURE_BOOT_REQUIRED,
    "enabled": SECURE_BOOT_ENABLED,
    "status": SECURE_BOOT_STATUS,
    "reason": "EDK2 source and build toolchain are pinned; firmware artifact not yet built in this environment.",
}

# ---------------------------------------------------------------------------
# UKI
# ---------------------------------------------------------------------------
UKI_OUTPUT = "build/uki/EFI/BOOT/BOOTX64.EFI"
UKI_PATH = UKI_OUTPUT
UKI_OUTPUT_PATH = UKI_OUTPUT
BOOTX64_PATH = UKI_OUTPUT
UKI_INSTALL_PATH = "EFI/BOOT/BOOTX64.EFI"
UKI_FALLBACK_PATH = "EFI/BOOT/BOOTX64.EFI"

UKI_KERNEL = "/boot/vmlinuz-linux"
UKI_INITRD = "/boot/initramfs-linux.img"
UKI_CMDLINE = "root=/dev/vda2 rw console=ttyS0"
UKI_OS_RELEASE = "k8labs"

UKI_SIGNED = True
UKI_SIGNATURE = "sbsign"
UKI_SIGNING_KEY_FINGERPRINT = PUBLIC_KEY_FINGERPRINT

# Computed from the built artifact. None means "not built in this tree yet",
# which is not the same as a verified hash.
UKI_SHA256: str | None = None
UKI_HASH = UKI_SHA256

# The custom-kernel image the cluster image map points at. The stock
# `build/k8labs-base.qcow2` is a separate, digest-independent build artifact and
# must never be recorded here as if it were this kernel's image.
QCOW2_PATH = "build/k8labs-custom-kernel.qcow2"
QCOW2 = QCOW2_PATH
BASE_IMAGE = QCOW2_PATH
QCOW2_SHA256: str | None = None
QCOW2_HASH = QCOW2_SHA256

# ---------------------------------------------------------------------------
# Required pin fields (for validation)
# ---------------------------------------------------------------------------
# Source and trust pins are always required. The two artifact hashes are not:
# they only become required once the corresponding artifact has been built.
REQUIRED_PIN_FIELDS = (
    "firmware_source_repo",
    "firmware_source_commit",
    "firmware_source_tag",
    "firmware_arch",
    "public_key_fingerprint",
)
ARTIFACT_SHA_FIELDS = ("uki_sha256", "qcow2_sha256")
REQUIRED_FIELDS = REQUIRED_PIN_FIELDS
REQUIRED_PINS = REQUIRED_PIN_FIELDS
REQUIRED_MANIFEST_FIELDS = REQUIRED_PIN_FIELDS

PRIVATE_KEY_MARKERS = (
    "-----BEGIN " + "PRIVATE KEY-----",
    "-----BEGIN RSA " + "PRIVATE KEY-----",
    "-----BEGIN EC " + "PRIVATE KEY-----",
    "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED " + "PRIVATE KEY-----",
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: str | os.PathLike[str] | Path) -> str:
    return _sha256_file(Path(path))


def _first_existing(paths: tuple[Path, ...]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def load_manifest(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load the firmware/UKI manifest (JSON or YAML when available)."""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        for name in (
            "firmware_uki_manifest.json",
            "firmware_uki_manifest.yaml",
            "firmware_uki_manifest.yml",
            "firmware_manifest.json",
            "firmware_manifest.yaml",
            "uki_manifest.json",
            "uki_manifest.yaml",
        ):
            candidates.append(PROJECT_ROOT / name)
    for candidate in candidates:
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            if candidate.suffix == ".json":
                return json.loads(text)
            try:
                import yaml  # type: ignore[import-not-found]

                loaded = yaml.safe_load(text)
                if isinstance(loaded, dict):
                    return loaded
            except Exception:  # noqa: BLE001
                # Fall back to a tiny key: value parser for the flat fields.
                result: dict[str, Any] = {}
                for line in text.splitlines():
                    if ":" in line and not line.strip().startswith("#"):
                        key, _, value = line.partition(":")
                        result[key.strip()] = value.strip().strip('"').strip("'")
                return result
    return {}


def _flatten_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = dict(manifest)
    # Merge nested sections into the flat view.
    for section in (
        "firmware",
        "uki",
        "qcow2",
        "secure_boot",
        "pins",
        "hashes",
        "toolchain",
    ):
        value = manifest.get(section)
        if isinstance(value, dict):
            for key, item in value.items():
                flat.setdefault(key, item)
                if isinstance(item, dict):
                    for inner_key, inner_value in item.items():
                        flat.setdefault(inner_key, inner_value)
    # Common aliases.
    if "firmware_source_commit" in flat:
        flat.setdefault("pinned_commit", flat["firmware_source_commit"])
    if "uki_sha256" not in flat:
        for key in ("uki_hash", "uki_sha"):
            if key in flat:
                flat["uki_sha256"] = flat[key]
                break
    if "qcow2_sha256" not in flat:
        for key in ("qcow2_hash", "qcow2"):
            if key in flat and isinstance(flat[key], str) and len(flat[key]) == 64:
                flat["qcow2_sha256"] = flat[key]
                break
    if "public_key_fingerprint" not in flat:
        for key in ("public_cert_fingerprint", "key_fingerprint", "fingerprint"):
            if key in flat:
                flat["public_key_fingerprint"] = flat[key]
                break
    return flat


def manifest_pin_fields(manifest: dict[str, Any]) -> dict[str, Any]:
    return _flatten_manifest(manifest)


def check_required_pin_fields(manifest: dict[str, Any]) -> list[str]:
    flat = _flatten_manifest(manifest)
    errors: list[str] = []
    for field in REQUIRED_PIN_FIELDS:
        if field not in flat or flat.get(field) in (None, "", []):
            errors.append(f"missing required pin field: {field}")
    return errors


def check(
    manifest_path: str | os.PathLike[str] | None = None,
    root: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Return a list of validation errors (empty means OK)."""
    errors: list[str] = []
    manifest = load_manifest(manifest_path)
    if not manifest:
        errors.append("manifest not found or empty")
    else:
        errors.extend(check_required_pin_fields(manifest))

    # Artifact hashes are verified only against artifacts that exist. A
    # present artifact without a recorded hash, or with a hash that does not
    # match, fails closed: nothing unaudited may be treated as release input.
    artifact_base = Path(root) if root is not None else PROJECT_ROOT
    for label, relative, field in (
        ("UKI", UKI_OUTPUT, "uki_sha256"),
        ("QCOW2", QCOW2_PATH, "qcow2_sha256"),
    ):
        artifact_path = artifact_base / relative
        if not artifact_path.is_file():
            continue
        actual = sha256_file(artifact_path)
        recorded = _flatten_manifest(manifest).get(field)
        if not recorded:
            errors.append(f"{label} artifact exists but no {field} is recorded")
        elif not isinstance(recorded, str):
            errors.append(f"{label} {field} must be a string digest")
        elif recorded != actual:
            errors.append(
                f"{label} hash mismatch: recorded {recorded}, actual {actual}"
            )

    # Private key material scan.
    scan_root = Path(root) if root is not None else PROJECT_ROOT
    errors.extend(scan_for_private_key_material(scan_root))
    return errors


def _is_ignored(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    ignored = {
        ".git",
        ".venv",
        "build",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
    }
    return any(part in ignored for part in parts)


def scan_for_private_key_material(root: str | os.PathLike[str] | Path) -> list[str]:
    """Return a list of files containing private key material."""
    root = Path(root)
    findings: list[str] = []
    if not root.exists():
        return findings
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_ignored(path, root):
            continue
        # Skip obvious binary files.
        if path.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".ico",
            ".qcow2",
            ".raw",
            ".fd",
            ".efi",
            ".rpm",
        }:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:1024]:
            continue
        try:
            text = data.decode("utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        for marker in PRIVATE_KEY_MARKERS:
            if marker in text:
                findings.append(f"private key material found in {path}")
                break
    return findings


def scan_private_keys(root: str | os.PathLike[str] | Path) -> list[str]:
    return scan_for_private_key_material(root)


def find_private_key_material(root: str | os.PathLike[str] | Path) -> list[str]:
    return scan_for_private_key_material(root)


def no_private_key_material(root: str | os.PathLike[str] | Path) -> bool:
    return not scan_for_private_key_material(root)


def verify_no_private_key_material(root: str | os.PathLike[str] | Path) -> bool:
    return no_private_key_material(root)


def source_state(root: str | os.PathLike[str] | Path) -> str:
    """Report whether the firmware source is present."""
    root = Path(root)
    if not root.exists():
        return "missing"
    # A checkout is "complete" if it contains the EDK2 source marker.
    if (root / "MdePkg").is_dir() or (root / "edk2").is_dir():
        return "complete"
    return "partial" if any(root.iterdir()) else "missing"


def is_complete(root: str | os.PathLike[str] | Path) -> bool:
    return source_state(root) == "complete"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate firmware/UKI build metadata."
    )
    parser.add_argument("--manifest", default=None, help="Path to manifest file.")
    parser.add_argument(
        "--root", default=str(PROJECT_ROOT), help="Repository root to scan."
    )
    parser.add_argument(
        "--check", action="store_true", help="Run validation and print errors."
    )
    args = parser.parse_args(argv)

    errors = check(args.manifest, args.root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("firmware/UKI metadata OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
