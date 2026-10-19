"""Tests for wiring the k8labs enrollment driver into a pinned EDK2 tree."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "edk2_wire_enroll.py"

_spec = importlib.util.spec_from_file_location("edk2_wire_enroll", SCRIPT)
assert _spec and _spec.loader
wire_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wire_mod)

DSC = "  OvmfPkg/EnrollDefaultKeys/EnrollDefaultKeys.inf\n!endif\n"
FDF = (
    "!if $(SECURE_BOOT_ENABLE) == TRUE\n"
    "  INF  SecurityPkg/VariableAuthenticated/SecureBootConfigDxe/"
    "SecureBootConfigDxe.inf\n"
    "!endif\n"
)


def test_dsc_gains_the_driver_after_the_secure_boot_anchor() -> None:
    out = wire_mod.wire_dsc(DSC)
    assert wire_mod.DRIVER_INF in out
    assert out.index(wire_mod.DSC_ANCHOR) < out.index(wire_mod.DRIVER_INF)


def test_fdf_places_the_driver_in_the_firmware_volume() -> None:
    out = wire_mod.wire_fdf(FDF)
    assert f"INF  {wire_mod.DRIVER_INF}" in out


def test_wiring_is_idempotent() -> None:
    once = wire_mod.wire_dsc(DSC)
    assert wire_mod.wire_dsc(once) == once
    fdf_once = wire_mod.wire_fdf(FDF)
    assert wire_mod.wire_fdf(fdf_once) == fdf_once


def test_missing_anchor_fails_loudly() -> None:
    for func in (wire_mod.wire_dsc, wire_mod.wire_fdf):
        try:
            func("no anchor\n")
        except ValueError as exc:
            assert "anchor" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("missing anchor must raise")


def test_firmware_does_not_carry_default_key_ffs_files() -> None:
    # Regression: FFS default-key entries corrupted the PEI variable store.
    fdf = ROOT / "firmware" / "edk2" / "K8LabsEnrollKeys"
    assert fdf.is_dir()
    sources = " ".join(p.read_text(errors="ignore") for p in fdf.iterdir())
    assert "FILE FREEFORM" not in sources
    assert "gDefault" not in sources
