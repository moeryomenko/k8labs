"""Wire the k8labs Secure Boot enrollment driver into a pinned EDK2 tree.

The driver is applied to a checked-out EDK2 tree at build time, so upstream
sources stay pristine and the overlay is fully described by files here.

Only the DSC component list is patched. The driver carries its signature lists
as compiled-in arrays: adding default-key FILE FREEFORM entries to `[FV.DXEFV]`
was tried and rejected, because it corrupted the firmware's PEI variable store
("NvVarStore FV headers were invalid", then an ASSERT in Platform.c).
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


def wire_dsc(text: str) -> str:
    """Add the enrollment driver to the CloudHv DSC component list."""
    if DRIVER_INF in text:
        return text
    if DSC_ANCHOR not in text:
        raise ValueError("DSC anchor (EnrollDefaultKeys.inf) not found")
    addition = f"  {DRIVER_INF}\n"
    return text.replace(DSC_ANCHOR, DSC_ANCHOR + addition, 1)


def wire_fdf(text: str) -> str:
    """Place the enrollment driver in [FV.DXEFV].

    The DSC only builds a module; the FDF decides what is placed in the
    firmware volume. Omitting this step produced a firmware that built cleanly
    and never ran the driver.
    """
    if f"INF  {DRIVER_INF}" in text:
        return text
    if FDF_ANCHOR not in text:
        raise ValueError("FDF anchor (SecureBootConfigDxe.inf) not found")
    return text.replace(FDF_ANCHOR, FDF_ANCHOR + f"  INF  {DRIVER_INF}\n", 1)


def wire(edk2_root: str | Path, driver_dir: str | Path) -> None:
    """Copy the driver into the tree and patch the DSC in place."""
    root = Path(edk2_root)
    dsc = root / "OvmfPkg" / "CloudHv" / "CloudHvX64.dsc"
    fdf = root / "OvmfPkg" / "CloudHv" / "CloudHvX64.fdf"

    source_driver = Path(driver_dir)
    header = source_driver / "K8LabsSecureBootKeys.h"
    if not header.is_file():
        raise FileNotFoundError(
            f"missing generated key header: {header} (run scripts/edk2_esl.py --header-out)"
        )

    target_driver = root / "OvmfPkg" / "K8LabsEnrollKeys"
    if target_driver.exists():
        shutil.rmtree(target_driver)
    shutil.copytree(source_driver, target_driver)

    dsc.write_text(wire_dsc(dsc.read_text(encoding="utf-8")), encoding="utf-8")
    fdf.write_text(wire_fdf(fdf.read_text(encoding="utf-8")), encoding="utf-8")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edk2-root", required=True, type=Path)
    parser.add_argument("--driver-dir", required=True, type=Path)
    args = parser.parse_args()
    wire(args.edk2_root, args.driver_dir)
    print("wired k8labs Secure Boot enrollment driver")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
