#!/usr/bin/env bash
# Build the custom Cloud Hypervisor (EDK2) firmware with the lab UEFI db certificate.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EDK2_REPO="https://github.com/tianocore/edk2.git"
EDK2_COMMIT="d46aa46c8361194521391aa581593e556c707c6e"
EDK2_TAG="edk2-stable202508"
SRC_DIR="${K8LABS_EDK2_SRC:-${ROOT}/build/edk2}"
OUT="${ROOT}/build/firmware/k8labs-cloudhv-secureboot.fd"
CERT="${ROOT}/firmware/k8labs-uefi-db.crt"

echo "==> EDK2 source pin: ${EDK2_COMMIT} (${EDK2_TAG})"
if [[ ! -d "${SRC_DIR}/.git" ]]; then
    echo "ERROR: EDK2 source not found at ${SRC_DIR}" >&2
    echo "Clone it with: git clone ${EDK2_REPO} ${SRC_DIR} && git -C ${SRC_DIR} checkout ${EDK2_COMMIT}" >&2
    exit 1
fi
actual="$(git -C "${SRC_DIR}" rev-parse HEAD)"
if [[ "${actual}" != "${EDK2_COMMIT}" ]]; then
    echo "ERROR: EDK2 checkout ${actual} != pinned ${EDK2_COMMIT}" >&2
    exit 1
fi
if [[ ! -f "${CERT}" ]]; then
    echo "ERROR: lab public certificate missing: ${CERT}" >&2
    exit 1
fi

echo "==> Building EDK2 (CloudHypervisorPkg) for x86_64 (RELEASE, GCC5)"
echo "    The lab public certificate (${CERT}) must be enrolled into the UEFI db"
echo "    while preserving the existing platform and key-exchange trust."
echo "    Private key material is never written to the firmware or the repository."

# The concrete EDK2 build command is intentionally left to the operator's
# toolchain; this script records the pinned inputs and refuses to proceed
# without them. Cloud Hypervisor opens the firmware read-only, so the resulting
# .fd must be verified for Secure Boot before use.
echo "ERROR: automated EDK2 firmware build is not implemented in this environment." >&2
exit 2
