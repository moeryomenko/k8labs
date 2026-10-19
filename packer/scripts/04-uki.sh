#!/bin/sh
# shellcheck shell=sh # POSIX sh: runs inside the Packer bake guest.
#
# Build a signed Unified Kernel Image and install it as the UEFI fallback boot
# entry. Cloud Hypervisor opens the firmware read-only, so the guest cannot rely
# on firmware key enrollment: the firmware trusts the lab certificate and this
# UKI must be signed by the matching key.
#
# Inputs (Packer environment_vars):
#   KERNEL_VERSION          exact kernel NVR installed by 01-configure-system.sh
#   K8LABS_UKI_CMDLINE      kernel command line to embed
#   K8LABS_SB_KEY           PKCS#11 URI or path to the signing key (required)
#   K8LABS_SB_CERT          path to the signing certificate (required)
#   K8LABS_UKI_SIGNING_TOOL ukify signing tool: sbsign|systemd-sbsign|pesign
#
# Fails closed: a missing key, certificate or ukify means the bake stops rather
# than producing an image that firmware will refuse to boot.
set -eux

KERNEL_VERSION="${KERNEL_VERSION:?}"
K8LABS_UKI_CMDLINE="${K8LABS_UKI_CMDLINE:-root=/dev/vda2 rw console=ttyS0}"
K8LABS_SB_KEY="${K8LABS_SB_KEY:-}"
K8LABS_SB_CERT="${K8LABS_SB_CERT:-}"
K8LABS_UKI_SIGNING_TOOL="${K8LABS_UKI_SIGNING_TOOL:-systemd-sbsign}"

ESP_MOUNT="/boot/efi"
UKI_DIR="${ESP_MOUNT}/EFI/BOOT"
UKI_PATH="${UKI_DIR}/BOOTX64.EFI"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

[ -n "${K8LABS_SB_KEY}" ] || fail "K8LABS_SB_KEY is required: the image must be signed by the lab key"
[ -n "${K8LABS_SB_CERT}" ] || fail "K8LABS_SB_CERT is required: sbverify needs the matching certificate"
[ -f "${K8LABS_SB_CERT}" ] || fail "signing certificate not found: ${K8LABS_SB_CERT}"
command -v ukify >/dev/null 2>&1 || fail "ukify is required to build the UKI"
command -v sbverify >/dev/null 2>&1 || fail "sbverify is required to verify the signed UKI"

KERNEL_IMAGE=""
for candidate in /boot/vmlinuz-"${KERNEL_VERSION}"*; do
    [ -f "${candidate}" ] || continue
    KERNEL_IMAGE="${candidate}"
    break
done
[ -n "${KERNEL_IMAGE}" ] || fail "kernel image not found for ${KERNEL_VERSION}"

# The installed release carries the architecture suffix (for example
# -fc44.x86_64); dracut and ukify must use the release, not the RPM NVR.
KERNEL_RELEASE="${KERNEL_IMAGE#/boot/vmlinuz-}"

# The base image ships no initramfs; build one for the installed kernel so the
# UKI is self-contained.
if [ ! -f "/boot/initramfs-${KERNEL_RELEASE}.img" ]; then
    command -v dracut >/dev/null 2>&1 || fail "dracut is required to build the initramfs"
    dracut --force --kver "${KERNEL_RELEASE}" "/boot/initramfs-${KERNEL_RELEASE}.img"
fi
INITRAMFS="/boot/initramfs-${KERNEL_RELEASE}.img"
[ -s "${INITRAMFS}" ] || fail "initramfs is empty: ${INITRAMFS}"

# The bake guest does not mount the ESP; the fallback boot entry must land on
# the EFI system partition, not on the root filesystem.
if ! mountpoint -q "${ESP_MOUNT}"; then
    ESP_DEV="$(blkid -t TYPE=vfat -o device | head -n1)"
    [ -n "${ESP_DEV}" ] || fail "no EFI system partition found"
    mkdir -p "${ESP_MOUNT}"
    mount "${ESP_DEV}" "${ESP_MOUNT}"
fi

mkdir -p "${UKI_DIR}"

ukify build \
    --linux "${KERNEL_IMAGE}" \
    --initrd "${INITRAMFS}" \
    --cmdline "${K8LABS_UKI_CMDLINE}" \
    --uname "${KERNEL_RELEASE}" \
    --signtool "${K8LABS_UKI_SIGNING_TOOL}" \
    --secureboot-private-key "${K8LABS_SB_KEY}" \
    --secureboot-certificate "${K8LABS_SB_CERT}" \
    --output "${UKI_PATH}"

[ -s "${UKI_PATH}" ] || fail "ukify produced no output at ${UKI_PATH}"

# Never ship a UKI the firmware would reject: verify the signature with the
# same certificate that is enrolled in the firmware's db.
sbverify --cert "${K8LABS_SB_CERT}" "${UKI_PATH}" \
    || fail "the UKI signature does not verify against ${K8LABS_SB_CERT}"

echo "==> signed UKI installed at ${UKI_PATH}"
