#!/usr/bin/env bash
# Prepare the Fedora Cloud source for the two-UKI comparison image.
#
# Fedora's Cloud Base image ships a ~100 MiB ESP. Two signed UKIs (stock +
# comparison) need ~100 MiB by themselves, so the ESP is grown to 256 MiB, the
# vfat filesystem is recreated with the same UUID used by /etc/fstab, and the
# original Fedora boot files are copied back so the bake VM can boot.
set -euo pipefail

ROOT=$(CDPATH=; cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE=${SOURCE:-${ROOT}/build/fedora-cloud-base.qcow2}
TARGET=${TARGET:-${ROOT}/build/fedora-cloud-base-esp.qcow2}
RAW=${RAW:-${ROOT}/build/fedora-cloud-base.raw}
WORK=${WORK:-${ROOT}/build/esp-prepare}

for tool in virt-resize guestfish qemu-img mkfs.vfat; do
    command -v "${tool}" >/dev/null 2>&1 || {
        echo "ERROR: ${tool} is required (install guestfs-tools and dosfstools)" >&2
        exit 1
    }
done

[[ -f "${SOURCE}" ]] || {
    echo "ERROR: Fedora source image not found: ${SOURCE}" >&2
    echo "Run: make base-deps" >&2
    exit 1
}

rm -rf "${WORK}"
mkdir -p "${WORK}"
trap 'rm -rf "${WORK}"' EXIT

echo "==> Resizing ESP to 256 MiB and expanding the root partition"
qemu-img create -f qcow2 "${WORK}/resized.qcow2" 6G >/dev/null
virt-resize --resize /dev/sda2=256M --expand /dev/sda3 \
    "${SOURCE}" "${WORK}/resized.qcow2"

echo "==> Reformatting the ESP and preserving its fstab UUID"
old_uuid=$(guestfish --ro -a "${WORK}/resized.qcow2" \
    -m '/dev/sda3:/:subvol=root' \
    cat /etc/fstab 2>/dev/null \
    | awk '$2 == "/boot/efi" { print $1 }' | sed 's/^UUID=//')
[[ -n "${old_uuid}" ]] || {
    echo "ERROR: could not find the /boot/efi UUID in the source fstab" >&2
    exit 1
}

guestfish -a "${WORK}/resized.qcow2" -m '/dev/sda3:/:subvol=root' \
    download /etc/fstab "${WORK}/fstab" 2>/dev/null
new_uuid=$(guestfish -a "${WORK}/resized.qcow2" <<'EOF' 2>/dev/null | tail -n1
run
mkfs vfat /dev/sda2 label:EFI
vfs-uuid /dev/sda2
EOF
)
[[ -n "${new_uuid}" ]] || {
    echo "ERROR: could not read the new ESP UUID" >&2
    exit 1
}
sed -i "s/^UUID=${old_uuid} /UUID=${new_uuid} /" "${WORK}/fstab"
guestfish -a "${WORK}/resized.qcow2" -m '/dev/sda3:/:subvol=root' \
    upload "${WORK}/fstab" /etc/fstab

echo "==> Restoring the Fedora boot files onto the enlarged ESP"
guestfish --ro -a "${SOURCE}" -m /dev/sda2:/ tar-out / "${WORK}/esp.tar" 2>/dev/null
guestfish -a "${WORK}/resized.qcow2" -m /dev/sda2:/ tar-in "${WORK}/esp.tar" /

echo "==> Writing ${TARGET} and ${RAW}"
qemu-img convert -O qcow2 "${WORK}/resized.qcow2" "${TARGET}"
qemu-img convert -O raw "${TARGET}" "${RAW}"
echo "==> comparison source ready: ${RAW}"
