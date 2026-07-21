#!/bin/sh
#
# packer/scripts/prepare-iso.sh — Prepare a bootable ISO with kernel parameters
#
# Creates a modified Fedora ISO with console=ttyS0 inst.sshd=1 in the
# default GRUB entry. This enables serial console capture and SSH during
# installation, which is critical for Packer's headless build.
#
# Usage: prepare-iso.sh <source-iso> <output-iso>
#
# Prerequisites: xorriso, sed, a source Fedora netinstall ISO.

set -u

SOURCE_ISO="${1:-}"
OUTPUT_ISO="${2:-}"
WORKDIR=$(mktemp -d /tmp/iso-prepare-XXXXXX)

die() {
    echo "Error: $*" >&2
    exit 1
}

cleanup() {
    rm -rf "$WORKDIR"
}

[ -n "$SOURCE_ISO" ] || die "Usage: $0 <source-iso> <output-iso>"
[ -n "$OUTPUT_ISO" ] || die "Usage: $0 <source-iso> <output-iso>"
[ -f "$SOURCE_ISO" ] || die "Source ISO not found: $SOURCE_ISO"
command -v xorriso >/dev/null 2>&1 || die "xorriso is required but not found"

trap cleanup EXIT

echo "==> Extracting GRUB config from ISO..."
xorriso -osirrox on -indev "$SOURCE_ISO" \
    -extract /boot/grub2/grub.cfg "$WORKDIR/grub.cfg" \
    2>/dev/null || die "Failed to extract grub.cfg from ISO"

# Modify the kernel command line in the default "Install Fedora 44" entry
# and the "Test this media" entry to add serial console support.
# Remove 'quiet' so we can see boot progress on serial.
# NOTE: Do NOT add inst.sshd=1 — that would let Packer connect to the
# installer's SSH (anaconda) before the OS is installed. Packer must wait
# for SSH after the full installation + reboot to get a bootable image.
sed -i \
    -e 's/\(linux.*inst\.stage2.*\) quiet/\1 console=ttyS0/' \
    -e 's/\(linux.*inst\.stage2.*\) rd\.live\.check quiet/\1 rd.live.check console=ttyS0/' \
    "$WORKDIR/grub.cfg"

echo "==> Building modified ISO..."
xorriso -indev "$SOURCE_ISO" \
    -outdev "$OUTPUT_ISO" \
    -boot_image any replay \
    -map "$WORKDIR/grub.cfg" /boot/grub2/grub.cfg \
    -volid "Fedora-E-dvd-x86_64-44" \
    2>/dev/null || die "Failed to build modified ISO"

echo "==> Modified ISO created: $(ls -lh "$OUTPUT_ISO" | awk '{print $5}')"
