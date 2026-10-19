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


def test_fdf_gains_the_driver_and_three_default_key_files() -> None:
    out = wire_mod.wire_fdf(FDF)
    assert f"INF  {wire_mod.DRIVER_INF}" in out
    guids = wire_mod.default_key_guids()
    for name, guid in guids.items():
        assert f"FILE FREEFORM = {guid}" in out
        assert f"SECTION RAW = OvmfPkg/CloudHv/{name}" in out


def test_wiring_is_idempotent() -> None:
    once = wire_mod.wire_fdf(FDF)
    assert wire_mod.wire_fdf(once) == once


def test_missing_anchor_fails_loudly() -> None:
    for func, text in (
        (wire_mod.wire_dsc, "no anchor\n"),
        (wire_mod.wire_fdf, "no anchor\n"),
    ):
        try:
            func(text)
        except ValueError as exc:
            assert "anchor" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("missing anchor must raise")


def test_default_key_guids_match_security_pkg() -> None:
    guids = wire_mod.default_key_guids()
    assert guids["K8LabsPk.esl"].lower() == "85254ea7-4759-4fc4-82d4-5eed5fb0a4a0"
    assert guids["K8LabsKek.esl"].lower() == "6f64916e-9f7a-4c35-b952-cd041efb05a3"
    assert guids["K8LabsDb.esl"].lower() == "c491d352-7623-4843-accc-2791a7574421"
