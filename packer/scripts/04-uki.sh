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
K8LABS_UKI_CMDLINE="${K8LABS_UKI_CMDLINE:-}"
K8LABS_SB_KEY="${K8LABS_SB_KEY:-}"
K8LABS_PKCS11_MODULE="${K8LABS_PKCS11_MODULE:-/usr/lib64/pkcs11/libsofthsm2.so}"
K8LABS_UKI_SIGNING_TOOL="${K8LABS_UKI_SIGNING_TOOL:-sbsign}"
SIGNING_CERT="/root/uki-material/cert.pem"

ESP_MOUNT="/boot/efi"
UKI_DIR="${ESP_MOUNT}/EFI/BOOT"
UKI_PATH="${UKI_DIR}/BOOTX64.EFI"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

[ -n "${K8LABS_SB_KEY}" ] || fail "K8LABS_SB_KEY is required: the image must be signed by the lab key"
[ -f "${SIGNING_CERT}" ] || fail "signing certificate not found: ${SIGNING_CERT}"

# A pkcs11: URI names a key inside a token; a plain string names an uploaded
# key file. Both end up as --secureboot-private-key, but only the token needs
# the OpenSSL PKCS#11 engine and a copied token store.
SIGNING_MODE="file"
case "${K8LABS_SB_KEY}" in
    pkcs11:*)
        [ -d /root/uki-material/tokens ] || fail "PKCS#11 key requested but no token store was uploaded"
        mkdir -p /var/lib/softhsm/tokens
        cp -a /root/uki-material/tokens/. /var/lib/softhsm/tokens/
        [ -f "${K8LABS_PKCS11_MODULE}" ] || fail "PKCS#11 module not found: ${K8LABS_PKCS11_MODULE}"
        export PKCS11_MODULE_PATH="${K8LABS_PKCS11_MODULE}"
        SIGNING_MODE="pkcs11"
        ;;
    *)
        [ -f /root/uki-material/key.pem ] || fail "signing key not found: /root/uki-material/key.pem"
        ;;
esac
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

# Reuse the command line the installer generated for this kernel. The image uses
# a btrfs subvolume root, so a hardcoded root= device would panic at boot.
if [ -z "${K8LABS_UKI_CMDLINE}" ]; then
    BLS_ENTRY=""
    for entry in /boot/loader/entries/*-"${KERNEL_RELEASE}".conf; do
        [ -f "${entry}" ] || continue
        BLS_ENTRY="${entry}"
        break
    done
    [ -n "${BLS_ENTRY}" ] || fail "no boot loader entry for ${KERNEL_RELEASE}"
    K8LABS_UKI_CMDLINE="$(sed -n 's/^options[[:space:]]*//p' "${BLS_ENTRY}" | head -n1)"
    [ -n "${K8LABS_UKI_CMDLINE}" ] || fail "boot loader entry has no options line"
fi
echo "==> UKI command line: ${K8LABS_UKI_CMDLINE}"

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
    # Resolve the ESP from the disk that holds /, never from "first vfat
    # device": the bake VM also has a vfat cloud-init seed disk, and picking
    # that one silently throws the UKI away with the VM.
    ROOT_SRC="$(findmnt -n -o SOURCE /)"
    ROOT_DISK="/dev/$(lsblk -no PKNAME "${ROOT_SRC}" | head -n1)"
    ESP_DEV="$(lsblk -no NAME,FSTYPE "${ROOT_DISK}" | awk '$2 == "vfat" { print "/dev/" $1; exit }')"
    [ -n "${ESP_DEV}" ] || fail "no EFI system partition found on ${ROOT_DISK}"
    mkdir -p "${ESP_MOUNT}"
    mount "${ESP_DEV}" "${ESP_MOUNT}"
fi

mkdir -p "${UKI_DIR}"

set --
[ "${SIGNING_MODE}" = "pkcs11" ] && set -- --signing-engine pkcs11

if [ "${SIGNING_MODE}" = "pkcs11" ]; then
    SIGNING_KEY="${K8LABS_SB_KEY}"
else
    SIGNING_KEY=/root/uki-material/key.pem
fi

ukify build \
    --linux "${KERNEL_IMAGE}" \
    --initrd "${INITRAMFS}" \
    --cmdline "${K8LABS_UKI_CMDLINE}" \
    --uname "${KERNEL_RELEASE}" \
    --signtool "${K8LABS_UKI_SIGNING_TOOL}" \
    "$@" \
    --secureboot-private-key "${SIGNING_KEY}" \
    --secureboot-certificate "${SIGNING_CERT}" \
    --output "${UKI_PATH}"

[ -s "${UKI_PATH}" ] || fail "ukify produced no output at ${UKI_PATH}"

# Never ship a UKI the firmware would reject: verify the signature with the
# same certificate that is enrolled in the firmware's db.
sbverify --cert "${SIGNING_CERT}" "${UKI_PATH}" \
    || fail "the UKI signature does not verify against ${SIGNING_CERT}"

sync
echo "==> signed UKI installed at ${UKI_PATH}"
