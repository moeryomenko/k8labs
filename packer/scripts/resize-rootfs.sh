#!/bin/sh
# =============================================================================
# resize-rootfs.sh — First-boot root filesystem resize helper
#
# Grows the root partition to fill the disk and, when the root filesystem
# lives on LVM, grows the PV/VG/LV so the filesystem fills the whole disk.
# This is the image-baked replacement for the Ansible resize task
# (ansible/playbooks/deploy-extensions.yml:25-43); it runs once per instance
# as a first-boot root resize invoked by the node cloud-init user-data (the
# provider renders CIDATA now) and is packaged by Packer at
# /usr/local/sbin/resize-rootfs.sh.
#
# Layout assumption: the Fedora Cloud Base image partitions the virtio root
# disk as vda1=ESP, vda2=/boot, vda3=root. The root filesystem may be btrfs
# (Fedora 44 Cloud Base default, mounted as a subvolume -> findmnt reports
# "/dev/vda3[/root]") or an LVM PV/VG/LV chain (older Fedora Cloud layout,
# VG fedora_localhost, root LV); the script detects the real layout at
# runtime and also covers a plain-partition root filesystem, so no partition
# number is hardcoded.
#
# lsblk pads column output with spaces (e.g. `lsblk -no PARTN /dev/vda3`
# prints " 3", right-aligned), so every PARTN/PKNAME/SIZE/END value is
# whitespace-trimmed (`tr -d ' '`) before it is used as a number or device
# name — growpart rejects a padded partition number.
#
# Idempotency: every step is a no-op when the disk is already fully sized —
# growpart reports NOCHANGE (exit 1) for an already-grown partition, the parted
# fallback is guarded by a partition-end size check, pvresize is a no-op when
# the PV already fills its partition, lvextend runs only when the VG has free
# extents, and resize2fs/xfs_growfs report "nothing to do" on a full
# filesystem. Exit status is 0 whether or not a resize was needed; genuine
# errors (missing tool, unsupported layout) exit non-zero so cloud-init logs
# them.
# =============================================================================

# This script is POSIX sh (AGENTS.md convention). The project .shellcheckrc
# defaults to shell=bash; declare the real shell so ShellCheck applies the sh
# ruleset.
# shellcheck shell=sh

set -eu

log() {
    printf 'resize-rootfs: %s\n' "$*"
}

die() {
    printf 'resize-rootfs: ERROR: %s\n' "$*" >&2
    exit 1
}

require_cmd() {
    cmd=$1
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        die "required command '${cmd}' not found"
    fi
}

# grow_partition DISK_DEV PARTNUM — grow partition PARTNUM of DISK_DEV to the
# end of the disk. Uses growpart when available (idempotent: exit 1 = no
# change) and falls back to parted guarded by a partition-end size check.
grow_partition() {
    disk_dev=$1
    partnum=$2

    if command -v growpart >/dev/null 2>&1; then
        if growpart "${disk_dev}" "${partnum}"; then
            log "grew partition ${disk_dev}${partnum} to the end of the disk"
        else
            rc=$?
            if [ "${rc}" -eq 1 ]; then
                log "partition ${disk_dev}${partnum} already fills the disk (growpart: no change)"
            else
                die "growpart ${disk_dev} ${partnum} failed (exit ${rc})"
            fi
        fi
    elif command -v parted >/dev/null 2>&1; then
        # Only run parted when the partition does not already reach the end of
        # the disk. The last usable GPT sector is a few KiB short of the raw
        # disk end (backup header + entries); allow 4 MiB slack so a
        # fully-grown partition is never re-grown on subsequent boots.
        disk_bytes=$(lsblk -bno SIZE "${disk_dev}" | tr -d ' ')
        part_end_bytes=$(lsblk -bno END "${disk_dev}${partnum}" | tr -d ' ')
        if [ -n "${disk_bytes}" ] && [ -n "${part_end_bytes}" ] && \
            [ "$((disk_bytes - part_end_bytes))" -le 4194304 ]; then
            log "partition ${disk_dev}${partnum} already fills the disk; nothing to do"
        else
            log "growing partition ${disk_dev}${partnum} with parted"
            parted -s "${disk_dev}" resizepart "${partnum}" 100%
            # Refresh the kernel partition table so pvresize/resize2fs see the
            # new size without a reboot.
            if command -v partx >/dev/null 2>&1; then
                partx -u "${disk_dev}"
            elif command -v partprobe >/dev/null 2>&1; then
                partprobe "${disk_dev}"
            fi
        fi
    else
        die "neither growpart nor parted is available to grow ${disk_dev}${partnum}"
    fi
}

# grow_partition_of PV — grow the partition backing LVM PV "pv" (if any).
grow_partition_of() {
    pv=$1
    disk=$(lsblk -no PKNAME "${pv}" | tr -d ' ')
    partnum=$(lsblk -no PARTN "${pv}" | tr -d ' ')
    if [ -z "${disk}" ] || [ -z "${partnum}" ]; then
        log "PV ${pv} is not backed by a partition; skipping partition grow"
        return 0
    fi
    grow_partition "/dev/${disk}" "${partnum}"
}

# resize_filesystem DEV — grow the filesystem on DEV to fill the (grown)
# block device. ext* uses resize2fs on the device; xfs uses xfs_growfs on the
# root mountpoint.
resize_filesystem() {
    dev=$1
    fstype=$(lsblk -no FSTYPE "${dev}")
    log "root filesystem type: ${fstype}"

    case "${fstype}" in
        ext2|ext3|ext4)
            require_cmd resize2fs
            log "growing ext filesystem on ${dev} with resize2fs"
            resize2fs "${dev}"
            ;;
        xfs)
            require_cmd xfs_growfs
            log "growing xfs filesystem on / with xfs_growfs"
            xfs_growfs /
            ;;
        btrfs)
            require_cmd btrfs
            log "growing btrfs filesystem on / with btrfs filesystem resize"
            btrfs filesystem resize max /
            ;;
        *)
            log "unsupported root filesystem type '${fstype}'; partition/LVM growth completed, fs grow skipped"
            ;;
    esac
}

# grow_lvm_root LV_DEV — grow the PV/VG/LV chain for a root filesystem on LVM.
grow_lvm_root() {
    lv_dev=$1
    require_cmd pvs
    require_cmd pvresize
    require_cmd vgs
    require_cmd lvextend

    vg=$(lsblk -no VGROUP "${lv_dev}")
    if [ -z "${vg}" ]; then
        require_cmd lvs
        vg=$(lvs --noheadings -o vg_name "${lv_dev}" | tr -d ' ')
    fi
    [ -n "${vg}" ] || die "cannot determine volume group for root LV ${lv_dev}"
    log "root LV ${lv_dev} is in volume group ${vg}"

    pv_list=$(pvs --noheadings -o pv_name "${vg}")
    [ -n "${pv_list}" ] || die "no physical volumes found in volume group ${vg}"
    log "growing physical volumes in ${vg}:"
    printf '%s\n' "${pv_list}" | while IFS= read -r pv; do
        log "  PV ${pv}"
        grow_partition_of "${pv}"
        log "  extending PV ${pv} to the grown partition"
        pvresize "${pv}"
    done

    free_extents=$(vgs --noheadings -o vg_free_count "${vg}" | tr -d ' ')
    [ -n "${free_extents}" ] || die "cannot read free extent count for volume group ${vg}"
    if [ "${free_extents}" -gt 0 ]; then
        log "VG ${vg} has ${free_extents} free extents; extending ${lv_dev}"
        lvextend -l +100%FREE "${lv_dev}"
    else
        log "VG ${vg} has no free space; LV ${lv_dev} already fills the volume group"
    fi

    resize_filesystem "${lv_dev}"
}

# grow_plain_root ROOT_DEV — grow the root partition and filesystem when the
# root filesystem sits directly on a partition.
grow_plain_root() {
    root_dev=$1
    disk=$(lsblk -no PKNAME "${root_dev}" | tr -d ' ')
    partnum=$(lsblk -no PARTN "${root_dev}" | tr -d ' ')
    [ -n "${disk}" ] || die "cannot determine disk for root device ${root_dev}"
    [ -n "${partnum}" ] || die "cannot determine partition number for root device ${root_dev}"
    log "root partition ${root_dev} is partition ${partnum} of /dev/${disk}"
    grow_partition "/dev/${disk}" "${partnum}"
    resize_filesystem "${root_dev}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
uid=$(id -u)
if [ "${uid}" -ne 0 ]; then
    die "must be run as root"
fi

require_cmd lsblk
require_cmd findmnt

root_dev=$(findmnt -no SOURCE /)
[ -n "${root_dev}" ] || die "cannot determine the root device from /proc/mounts"

root_fstype=$(findmnt -no FSTYPE /)
if [ "${root_fstype}" = "btrfs" ]; then
    # btrfs subvolume root: findmnt reports the subvolume path
    # (/dev/vda3[/root]). Strip the [subvol] suffix so partition/PV operations
    # target the block device; the filesystem grow itself uses
    # "btrfs filesystem resize max /" on the mountpoint (resize_filesystem).
    root_dev=${root_dev%%\[*}
    log "btrfs root; block device: ${root_dev}"
fi
log "root device: ${root_dev}"

root_type=$(lsblk -no TYPE "${root_dev}")
log "root device type: ${root_type}"

case "${root_type}" in
    lvm)
        grow_lvm_root "${root_dev}"
        ;;
    part)
        grow_plain_root "${root_dev}"
        ;;
    disk)
        log "root filesystem sits directly on a whole disk; no partition to grow"
        resize_filesystem "${root_dev}"
        ;;
    *)
        die "unsupported root device type '${root_type}'"
        ;;
esac

log "root filesystem resize complete"
