"""Tests for the EDK2 Secure Boot signature-list generator."""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "edk2_esl.py"

_spec = importlib.util.spec_from_file_location("edk2_esl", SCRIPT)
assert _spec and _spec.loader
esl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(esl)


def test_guid_serialisation_is_mixed_endian() -> None:
    raw = esl.guid_bytes("a5c059a1-94e4-4aa7-87b5-ab155c2bf072")
    assert len(raw) == 16
    assert raw[:4] == bytes.fromhex("a159c0a5")
    assert raw[4:6] == bytes.fromhex("e494")
    assert raw[6:8] == bytes.fromhex("a74a")
    assert raw[8:] == bytes.fromhex("87b5ab155c2bf072")


def test_signature_list_layout_single_certificate() -> None:
    cert = b"\x30\x82\x01\x00" + b"x" * 252
    blob = esl.build_signature_list([cert])

    assert blob[:16] == esl.guid_bytes(esl.EFI_CERT_X509_GUID)
    list_size, header_size, signature_size = struct.unpack("<III", blob[16:28])
    assert header_size == 0
    assert signature_size == 16 + len(cert)
    assert list_size == len(blob) == 28 + signature_size
    assert blob[28:44] == esl.guid_bytes(esl.K8LABS_OWNER_GUID)
    assert blob[44:] == cert


def test_empty_list_is_rejected() -> None:
    try:
        esl.build_signature_list([])
    except ValueError as exc:
        assert "at least one certificate" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty list must be rejected")


def test_mixed_certificate_sizes_are_rejected() -> None:
    try:
        esl.build_signature_list([b"a" * 10, b"b" * 20])
    except ValueError as exc:
        assert "equal size" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("mixed sizes must be rejected")


def test_reads_the_committed_lab_certificate() -> None:
    der = esl.read_der_certificate(ROOT / "firmware" / "k8labs-uefi-db.crt")
    # A DER X.509 certificate starts with a SEQUENCE tag.
    assert der[0] == 0x30
    assert len(der) > 500
    blob = esl.build_signature_list([der])
    assert struct.unpack("<I", blob[16:20])[0] == len(blob)


def test_c_header_contains_named_arrays() -> None:
    cert = b"\x30\x82\x01\x00" + b"x" * 20
    blob = esl.build_signature_list([cert])
    header = esl.to_c_header({"K8LABS_DB_ESL": blob})
    assert "STATIC CONST UINT8  K8LABS_DB_ESL[]" in header
    assert "#define K8LABS_DB_ESL_SIZE  (sizeof (K8LABS_DB_ESL))" in header
    assert "0x30, 0x82, 0x01, 0x00" in header


def test_c_header_rejects_empty_blob() -> None:
    try:
        esl.to_c_header({"EMPTY": b""})
    except ValueError as exc:
        assert "empty signature list" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty blob must be rejected")


def test_committed_header_matches_the_lab_certificate() -> None:
    der = esl.read_der_certificate(ROOT / "firmware" / "k8labs-uefi-db.crt")
    blob = esl.build_signature_list([der])
    expected = esl.to_c_header(
        {"K8LABS_PK_ESL": blob, "K8LABS_KEK_ESL": blob, "K8LABS_DB_ESL": blob}
    )
    committed = (
        ROOT / "firmware" / "edk2" / "K8LabsEnrollKeys" / "K8LabsSecureBootKeys.h"
    ).read_text(encoding="utf-8")
    assert committed == expected
