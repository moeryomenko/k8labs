#!/bin/sh
# shellcheck shell=sh # POSIX sh: runs inside the Packer bake guest.
#
# Build signed Unified Kernel Images for both comparison kernels and install
# systemd-boot as the UEFI fallback. The firmware trusts the lab certificate,
# so the boot manager and every UKI must be signed by the matching key.
#
# Inputs (Packer environment_vars):
#   KERNEL_VERSION          stock kernel NVR (fallback when /etc/k8labs-kernels
#                           is absent)
#   CUSTOM_KERNEL_VERSION   comparison kernel release (same fallback)
#   DEFAULT_KERNEL          stock|custom; selects the systemd-boot default
#   K8LABS_UKI_CMDLINE      optional command line; empty reuses each BLS entry
#   K8LABS_SB_KEY           PKCS#11 URI or path to the signing key (required)
#   K8LABS_SB_CERT          path to the signing certificate (required)
#   K8LABS_UKI_SIGNING_TOOL ukify signing tool: sbsign|systemd-sbsign|pesign
#
# Fails closed: a missing key, certificate, kernel image or signature stops the
# bake rather than producing an image the firmware will reject.
set -eux

KERNEL_VERSION="${KERNEL_VERSION:?}"
CUSTOM_KERNEL_VERSION="${CUSTOM_KERNEL_VERSION:?}"
DEFAULT_KERNEL="${DEFAULT_KERNEL:-stock}"
K8LABS_UKI_CMDLINE="${K8LABS_UKI_CMDLINE:-}"
K8LABS_SB_KEY="${K8LABS_SB_KEY:-}"
K8LABS_PKCS11_MODULE="${K8LABS_PKCS11_MODULE:-/usr/lib64/pkcs11/libsofthsm2.so}"
K8LABS_UKI_SIGNING_TOOL="${K8LABS_UKI_SIGNING_TOOL:-sbsign}"
SIGNING_CERT="/root/uki-material/cert.pem"

ESP_MOUNT="/boot/efi"
BOOT_DIR="${ESP_MOUNT}/EFI/BOOT"
UKI_DIR="${ESP_MOUNT}/EFI/Linux"
LOADER_DIR="${ESP_MOUNT}/loader"
SYSTEMD_BOOT_SRC="/usr/lib/systemd/boot/efi/systemd-bootx64.efi"

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
command -v sbsign >/dev/null 2>&1 || fail "sbsign is required to sign the boot manager"

if [ -f /etc/k8labs-kernels ]; then
    # shellcheck disable=SC1091
    . /etc/k8labs-kernels
    STOCK_KERNEL_RELEASE="${stock:?}"
    CUSTOM_KERNEL_RELEASE="${custom:?}"
    DEFAULT_KERNEL="${default:-${DEFAULT_KERNEL}}"
else
    STOCK_KERNEL_RELEASE="${KERNEL_VERSION}"
    CUSTOM_KERNEL_RELEASE="${CUSTOM_KERNEL_VERSION}"
fi

[ -n "${STOCK_KERNEL_RELEASE}" ] || fail "stock kernel release is empty"
[ -n "${CUSTOM_KERNEL_RELEASE}" ] || fail "custom kernel release is empty"
case "${DEFAULT_KERNEL}" in
    stock) DEFAULT_RELEASE="${STOCK_KERNEL_RELEASE}" ;;
    custom) DEFAULT_RELEASE="${CUSTOM_KERNEL_RELEASE}" ;;
    *) fail "DEFAULT_KERNEL must be stock or custom" ;;
esac

# The bake guest does not mount the ESP; the boot entries must land on the EFI
# system partition, not on the root filesystem.
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

mkdir -p "${BOOT_DIR}" "${UKI_DIR}" "${LOADER_DIR}"

if [ "${SIGNING_MODE}" = "pkcs11" ]; then
    SIGNING_KEY="${K8LABS_SB_KEY}"
else
    SIGNING_KEY=/root/uki-material/key.pem
fi

build_uki() {
    release="$1"
    label="$2"
    kernel_image="/boot/vmlinuz-${release}"
    initramfs="/boot/initramfs-${release}.img"
    output="${UKI_DIR}/${label}-${release}.efi"

    [ -f "${kernel_image}" ] || fail "kernel image not found: ${kernel_image}"

    if [ -z "${K8LABS_UKI_CMDLINE}" ]; then
        BLS_ENTRY=""
        for entry in /boot/loader/entries/*-"${release}".conf; do
            [ -f "${entry}" ] || continue
            BLS_ENTRY="${entry}"
            break
        done
        [ -n "${BLS_ENTRY}" ] || fail "no boot loader entry for ${release}"
        UKI_CMDLINE="$(sed -n 's/^options[[:space:]]*//p' "${BLS_ENTRY}" | head -n1)"
        [ -n "${UKI_CMDLINE}" ] || fail "boot loader entry ${BLS_ENTRY} has no options line"
    else
        UKI_CMDLINE="${K8LABS_UKI_CMDLINE}"
    fi

    if [ ! -f "${initramfs}" ]; then
        command -v dracut >/dev/null 2>&1 || fail "dracut is required to build the initramfs"
        dracut --force --kver "${release}" "${initramfs}"
    fi
    [ -s "${initramfs}" ] || fail "initramfs is empty: ${initramfs}"

    set --
    if [ "${SIGNING_MODE}" = "pkcs11" ]; then
        set -- --signing-engine pkcs11
    fi

    ukify build \
        --linux "${kernel_image}" \
        --initrd "${initramfs}" \
        --cmdline "${UKI_CMDLINE}" \
        --uname "${release}" \
        --signtool "${K8LABS_UKI_SIGNING_TOOL}" \
        "$@" \
        --secureboot-private-key "${SIGNING_KEY}" \
        --secureboot-certificate "${SIGNING_CERT}" \
        --output "${output}"

    [ -s "${output}" ] || fail "ukify produced no output at ${output}"
    sbverify --cert "${SIGNING_CERT}" "${output}" \
        || fail "the UKI signature for ${release} does not verify against ${SIGNING_CERT}"
    echo "==> signed UKI installed at ${output}"
}

build_uki "${STOCK_KERNEL_RELEASE}" "k8labs-stock"
build_uki "${CUSTOM_KERNEL_RELEASE}" "k8labs-custom"

# systemd-boot discovers the UKIs in EFI/Linux and presents a menu. It is the
# firmware fallback, so it must be signed with the same lab key.
[ -f "${SYSTEMD_BOOT_SRC}" ] || fail "systemd-boot binary not found: ${SYSTEMD_BOOT_SRC}"
# Remove the stale Fedora shim/GRUB boot entry. The firmware's NVRAM picks
# EFI/fedora/shimx64.efi over the fallback path, which fails after the ESP
# layout changes. The signed systemd-boot at EFI/BOOT/BOOTX64.EFI is the only
# boot entry the comparison image should expose.
rm -rf "${ESP_MOUNT}/EFI/fedora" "${ESP_MOUNT}/NvVars"
rm -f "${BOOT_DIR}/BOOTX64.EFI"
if [ "${SIGNING_MODE}" = "pkcs11" ]; then
    sbsign --engine pkcs11 \
        --key "${SIGNING_KEY}" \
        --cert "${SIGNING_CERT}" \
        --output "${BOOT_DIR}/BOOTX64.EFI" \
        "${SYSTEMD_BOOT_SRC}"
else
    sbsign \
        --key "${SIGNING_KEY}" \
        --cert "${SIGNING_CERT}" \
        --output "${BOOT_DIR}/BOOTX64.EFI" \
        "${SYSTEMD_BOOT_SRC}"
fi
sbverify --cert "${SIGNING_CERT}" "${BOOT_DIR}/BOOTX64.EFI" \
    || fail "the boot manager signature does not verify against ${SIGNING_CERT}"

if [ "${DEFAULT_KERNEL}" = "stock" ]; then
    DEFAULT_UKI="k8labs-stock-${STOCK_KERNEL_RELEASE}.efi"
else
    DEFAULT_UKI="k8labs-custom-${CUSTOM_KERNEL_RELEASE}.efi"
fi
cat > "${LOADER_DIR}/loader.conf" <<EOF
default ${DEFAULT_UKI}
timeout 5
editor no
EOF

sync
echo "==> signed systemd-boot installed at ${BOOT_DIR}/BOOTX64.EFI"
echo "==> default UKI: ${DEFAULT_UKI}"
