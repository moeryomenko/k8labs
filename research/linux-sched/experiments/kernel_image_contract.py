"""Kernel-image contract for the linux-sched research series."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKER_VARIABLES = PROJECT_ROOT / "packer" / "variables.pkr.hcl"
PACKER_PROVISIONER = PROJECT_ROOT / "packer" / "scripts" / "01-configure-system.sh"
VERSIONS_LOCK = PROJECT_ROOT / "versions.lock.yaml"
EXTENSIONS_MANIFEST = PROJECT_ROOT / "extensions" / "manifest.yaml"
GATES_DOC = PROJECT_ROOT / "research" / "linux-sched" / "GATES.md"
README_DOC = PROJECT_ROOT / "research" / "linux-sched" / "README.md"

SCHED_EXT_FOR_7_4_COMMIT = "1f5dde349ac2fca1f119ae8de3f0d2f1c2a8d338"
PINNED_KERNEL_VERSION = "7.3.0-0.rc7.261014g4056b3c9d3c9.60.fc46"
AVAILABLE_RAWHIDE_KERNEL = "7.3.0-0.rc7.261016g2f2f7eeef8b0.62.fc46"
ARENA_SCALAR_AVAILABLE = False

BLOCKER = (
    "No Fedora/rawhide kernel package resolves with BPF_F_ARENA_SCALAR "
    "(required by scx_mavd); Fedora rawhide ships only "
    f"{AVAILABLE_RAWHIDE_KERNEL}, and the sched_ext/for-7.4 branch "
    f"({SCHED_EXT_FOR_7_4_COMMIT}) is still a 7.3-based tree without the flag. "
    "scx_mavd stays deferred until a kernel with the flag is packaged."
)


def _packer_kernel_version() -> str | None:
    text = PACKER_VARIABLES.read_text(encoding="utf-8")
    match = re.search(
        r'variable "kernel_version" \{.*?default\s*=\s*"([^"]+)"',
        text,
        re.S,
    )
    return match.group(1) if match else None


def _lock_kernel_version() -> str | None:
    if not VERSIONS_LOCK.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(VERSIONS_LOCK.read_text(encoding="utf-8"))
        return data.get("fedora", {}).get("kernel")
    except Exception:
        return None


def _manifest_kernel_version() -> str | None:
    if not EXTENSIONS_MANIFEST.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(EXTENSIONS_MANIFEST.read_text(encoding="utf-8"))
        return data.get("settings", {}).get("kernel_version")
    except Exception:
        return None


def _provisioner_is_fail_closed() -> bool:
    if not PACKER_PROVISIONER.is_file():
        return False
    text = PACKER_PROVISIONER.read_text(encoding="utf-8")
    if "Exact kernel" in text and "is unavailable" in text:
        return False
    return "kernel-modules-extra-" in text


def check() -> list[str]:
    errors: list[str] = []

    packer_version = _packer_kernel_version()
    if packer_version != PINNED_KERNEL_VERSION:
        errors.append(
            f"packer kernel_version {packer_version!r} != pinned "
            f"{PINNED_KERNEL_VERSION!r}"
        )

    lock_version = _lock_kernel_version()
    if lock_version is not None and lock_version != PINNED_KERNEL_VERSION:
        errors.append(
            f"versions.lock.yaml kernel {lock_version!r} != pinned "
            f"{PINNED_KERNEL_VERSION!r}"
        )

    manifest_version = _manifest_kernel_version()
    if manifest_version is not None and manifest_version != PINNED_KERNEL_VERSION:
        errors.append(
            f"extensions/manifest.yaml kernel_version {manifest_version!r} != "
            f"pinned {PINNED_KERNEL_VERSION!r}"
        )

    if not _provisioner_is_fail_closed():
        errors.append("packer provisioner is not fail-closed on an unavailable kernel")

    for doc in (GATES_DOC, README_DOC):
        if doc.is_file():
            text = doc.read_text(encoding="utf-8")
            if SCHED_EXT_FOR_7_4_COMMIT not in text and "BPF_F_ARENA_SCALAR" not in text:
                errors.append(f"{doc} does not document the kernel-image blocker")
                break

    return errors


def main() -> int:
    errors = check()
    if errors:
        import sys

        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("kernel image contract OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
