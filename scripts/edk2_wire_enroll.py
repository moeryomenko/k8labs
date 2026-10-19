"""Wire the k8labs Secure Boot enrollment driver into a pinned EDK2 tree.

The driver and the default-key FFS files are applied to a checked-out EDK2 tree
at build time, so the upstream sources stay pristine and the overlay is fully
described by files in this repository.

The wiring is textual and anchored on stable lines:

* `OvmfPkg/CloudHv/CloudHvX64.dsc` — the component list inside the
  `!if $(SECURE_BOOT_ENABLE) == TRUE` block.
* `OvmfPkg/CloudHv/CloudHvX64.fdf` — the same block inside `[FV.DXEFV]`, where
  the driver INF and the default-key FILE FREEFORM entries are added.
"""

from __future__ import annotations

import shutil
from pathlib import Path

DSC_ANCHOR = "  OvmfPkg/EnrollDefaultKeys/EnrollDefaultKeys.inf\n"
FDF_ANCHOR = (
    "  INF  SecurityPkg/VariableAuthenticated/SecureBootConfigDxe/"
    "SecureBootConfigDxe.inf\n"
)

DRIVER_INF = "OvmfPkg/K8LabsEnrollKeys/K8LabsEnrollKeys.inf"

# GUIDs come from SecurityPkg/SecurityPkg.dec: gDefaultPKFileGuid,
# gDefaultKEKFileGuid, gDefaultdbFileGuid.
DEFAULT_KEY_FILES = (
    ("K8LabsPk.esl", "85254EA7-4759-4FC4-82D4-5EED5FB0A4A0"),
    ("K8LabsKek.esl", "6F64916E-9F7A-4C35-B952-CD041EFB05A3"),
    ("K8LabsDb.esl", "C491D352-7623-4843-ACCC-2791A7574421"),
)


def _ffs_blocks(indent: str = "  ") -> str:
    out = [f"{indent}INF  {DRIVER_INF}\n"]
    for name, guid in DEFAULT_KEY_FILES:
        out.append(f"{indent}FILE FREEFORM = {guid} {{\n")
        out.append(f"{indent}  SECTION RAW = OvmfPkg/CloudHv/{name}\n")
        out.append(f"{indent}}}\n")
    return "".join(out)


def wire_dsc(text: str) -> str:
    """Add the enrollment driver to the CloudHv DSC component list."""
    if DRIVER_INF in text:
        return text
    if DSC_ANCHOR not in text:
        raise ValueError("DSC anchor (EnrollDefaultKeys.inf) not found")
    addition = f"  {DRIVER_INF}\n"
    return text.replace(DSC_ANCHOR, DSC_ANCHOR + addition, 1)


def wire_fdf(text: str) -> str:
    """Add the enrollment driver and default-key files to [FV.DXEFV]."""
    if DRIVER_INF in text:
        return text
    if FDF_ANCHOR not in text:
        raise ValueError("FDF anchor (SecureBootConfigDxe.inf) not found")
    return text.replace(FDF_ANCHOR, FDF_ANCHOR + _ffs_blocks(), 1)


def default_key_guids() -> dict[str, str]:
    return {name: guid for name, guid in DEFAULT_KEY_FILES}


def wire(edk2_root: str | Path, driver_dir: str | Path, esl_dir: str | Path) -> None:
    """Copy the driver and ESLs into the tree and patch DSC/FDF in place."""
    root = Path(edk2_root)
    dsc = root / "OvmfPkg" / "CloudHv" / "CloudHvX64.dsc"
    fdf = root / "OvmfPkg" / "CloudHv" / "CloudHvX64.fdf"

    target_driver = root / "OvmfPkg" / "K8LabsEnrollKeys"
    if target_driver.exists():
        shutil.rmtree(target_driver)
    shutil.copytree(Path(driver_dir), target_driver)

    for name, _guid in DEFAULT_KEY_FILES:
        source = Path(esl_dir) / name
        if not source.is_file():
            raise FileNotFoundError(f"missing default-key file: {source}")
        shutil.copyfile(source, root / "OvmfPkg" / "CloudHv" / name)

    dsc.write_text(wire_dsc(dsc.read_text(encoding="utf-8")), encoding="utf-8")
    fdf.write_text(wire_fdf(fdf.read_text(encoding="utf-8")), encoding="utf-8")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edk2-root", required=True, type=Path)
    parser.add_argument("--driver-dir", required=True, type=Path)
    parser.add_argument("--esl-dir", required=True, type=Path)
    args = parser.parse_args()
    wire(args.edk2_root, args.driver_dir, args.esl_dir)
    print("wired k8labs Secure Boot enrollment driver")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
