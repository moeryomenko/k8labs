"""Build EFI_SIGNATURE_LIST blobs for EDK2 Secure Boot default-key FFS files.

EDK2 reads the default PK/KEK/db from FFS files whose GUIDs are declared in
`SecurityPkg.dec` (`gDefaultPKFileGuid`, `gDefaultKEKFileGuid`,
`gDefaultdbFileGuid`). Each file carries a raw section holding an
`EFI_SIGNATURE_LIST`, the same encoding the UEFI `db` variable uses.
"""

from __future__ import annotations

import struct
from pathlib import Path

# EFI_CERT_X509_GUID from SecurityPkg/Include/Guid/ImageAuthentication.h.
EFI_CERT_X509_GUID = "a5c059a1-94e4-4aa7-87b5-ab155c2bf072"
# Owner recorded on every signature entry. Any GUID is valid; this one is the
# lab's, and it keeps generated lists recognisable.
K8LABS_OWNER_GUID = "6b1b3c1e-6f4a-4c8e-9b2d-0f7a5c3e1d42"


def guid_bytes(value: str) -> bytes:
    """Serialise a GUID the way EFI_GUID stores it (mixed endianness)."""
    parts = value.strip().split("-")
    if len(parts) != 5:
        raise ValueError(f"invalid GUID: {value!r}")
    data1 = int(parts[0], 16)
    data2 = int(parts[1], 16)
    data3 = int(parts[2], 16)
    data4 = bytes.fromhex(parts[3] + parts[4])
    if len(data4) != 8:
        raise ValueError(f"invalid GUID: {value!r}")
    return struct.pack("<IHH", data1, data2, data3) + data4


def build_signature_list(certificates: list[bytes]) -> bytes:
    """Return one EFI_SIGNATURE_LIST holding the given DER certificates."""
    if not certificates:
        raise ValueError("at least one certificate is required")

    owner = guid_bytes(K8LABS_OWNER_GUID)
    entries = b"".join(owner + cert for cert in certificates)
    signature_size = len(owner) + len(certificates[0])
    for cert in certificates:
        if len(owner) + len(cert) != signature_size:
            raise ValueError("all certificates in one list must have equal size")

    header = guid_bytes(EFI_CERT_X509_GUID)
    # EFI_SIGNATURE_LIST: SignatureType, SignatureListSize, SignatureHeaderSize,
    # SignatureSize, then the entries.
    list_size = 16 + 4 + 4 + 4 + len(entries)
    header += struct.pack("<III", list_size, 0, signature_size)
    return header + entries


def read_der_certificate(path: str | Path) -> bytes:
    """Read a PEM or DER certificate and return DER bytes."""
    raw = Path(path).read_bytes()
    if b"-----BEGIN CERTIFICATE-----" not in raw:
        return raw
    import base64

    body = []
    inside = False
    for line in raw.decode("ascii").splitlines():
        if line.startswith("-----BEGIN CERTIFICATE-----"):
            inside = True
            continue
        if line.startswith("-----END CERTIFICATE-----"):
            break
        if inside:
            body.append(line.strip())
    if not body:
        raise ValueError(f"no certificate body found in {path}")
    return base64.b64decode("".join(body))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--certificate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.write_bytes(
        build_signature_list([read_der_certificate(args.certificate)])
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
