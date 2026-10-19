#!/usr/bin/env bash
# Build and sign the k8labs Unified Kernel Image (UKI).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="${K8LABS_UKI_KEY:-${ROOT}/build/firmware/k8labs-uefi-db.key}"
CERT="${K8LABS_UKI_CERT:-${ROOT}/firmware/k8labs-uefi-db.crt}"
KERNEL="${K8LABS_UKI_KERNEL:-/boot/vmlinuz-linux}"
INITRD="${K8LABS_UKI_INITRD:-/boot/initramfs-linux.img}"
CMDLINE_FILE="${ROOT}/build/uki/cmdline.txt"
OSRELEASE_FILE="${ROOT}/build/uki/os-release"
STUB="/usr/lib/systemd/boot/efi/linuxx64.efi.stub"
OUT="${ROOT}/build/uki/EFI/BOOT/BOOTX64.EFI"

mkdir -p "${ROOT}/build/uki/EFI/BOOT"
printf 'root=/dev/vda2 rw console=ttyS0\n' > "${CMDLINE_FILE}"
printf 'NAME="k8labs"\nID=k8labs\nVERSION_ID=1\nPRETTY_NAME="k8labs"\n' > "${OSRELEASE_FILE}"

if [[ ! -f "${KEY}" ]]; then
    echo "ERROR: signing private key not found: ${KEY}" >&2
    echo "Generate it under build/ (gitignored) and never commit it." >&2
    exit 1
fi
if [[ ! -f "${CERT}" ]]; then
    echo "ERROR: public certificate not found: ${CERT}" >&2
    exit 1
fi

ukify build \
    --linux="${KERNEL}" \
    --initrd="${INITRD}" \
    --os-release="@${OSRELEASE_FILE}" \
    --cmdline="@${CMDLINE_FILE}" \
    --secureboot-private-key="${KEY}" \
    --secureboot-certificate="${CERT}" \
    --stub="${STUB}" \
    --output="${OUT}"

echo "==> UKI written to ${OUT}"
echo "==> install as EFI/BOOT/BOOTX64.EFI for direct UEFI fallback boot"
sha256sum "${OUT}"
