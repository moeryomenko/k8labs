#!/usr/bin/env bash
#
# provision-volume.sh — put Pyroscope's store on a genuinely bounded 10 GiB
# filesystem, and verify it.
#
# The plan wants a dedicated host-owned volume of exactly 10 GiB, separate from
# the comparison data root and the worker disks. A directory on a 745 GiB
# filesystem is not bounded, and the 9 GiB stop threshold is only meaningful
# against a filesystem that actually fills at 10 GiB, so this creates one: a
# 10 GiB ext4 image on the host, mounted at the profile root.
#
# Idempotent: if the mountpoint is already the expected 10 GiB filesystem it
# reports and exits. Needs privileges to mount, which the host has.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

MOUNT_POINT="${PROFILE_VOLUME:?set PROFILE_VOLUME to the profile directory}"
IMAGE="${PROFILE_IMAGE:-${MOUNT_POINT}.img}"
SIZE="${PROFILE_SIZE:-10G}"
# The *image* is exactly 10 GiB, which is the plan's provision. The filesystem on
# it reports less, because ext4 spends inode tables, journal and superblock: about
# 0.25 GiB of a 10 GiB image. Usable capacity is therefore checked against a
# documented allowance rather than against the image size, and both are reported.
IMAGE_BYTES="${PROFILE_SIZE_BYTES:-10737418240}"       # exactly 10 GiB
# Measured, not assumed: a 10 GiB ext4 image reports about 9.13 GiB usable.
USABLE_FLOOR_BYTES="${PROFILE_USABLE_FLOOR_BYTES:-9663676416}"   # 9 GiB

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }

current_bytes() {
    df -B1 --output=size "$MOUNT_POINT" 2>/dev/null | tail -1 | tr -d '[:space:]'
}

if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
    bytes="$(current_bytes)"
    if [[ "$bytes" =~ ^[0-9]+$ ]] && (( bytes >= USABLE_FLOOR_BYTES )); then
        log "profile volume already mounted at ${MOUNT_POINT} (${bytes} B usable of a 10 GiB image)"
        exit 0
    fi
    log "FATAL: ${MOUNT_POINT} is mounted but holds ${bytes:-?} B, below the 9.5 GiB floor"
    exit 1
fi

if [[ -f "$IMAGE" ]]; then
    log "using the existing image ${IMAGE}"
else
    log "creating a ${SIZE} image at ${IMAGE}"
    truncate -s "$SIZE" "$IMAGE"
    mkfs.ext4 -q -F "$IMAGE"
fi

mkdir -p "$MOUNT_POINT"
log "mounting ${IMAGE} at ${MOUNT_POINT}"
sudo mount -o loop "$IMAGE" "$MOUNT_POINT"

# The container runs as its own root (rootless podman maps that to the invoking
# user), so the mount must be writable by the user that will run Pyroscope.
sudo chown "$(id -u):$(id -g)" "$MOUNT_POINT"

image_bytes="$(stat -c %s "$IMAGE")"
bytes="$(current_bytes)"
if [[ "$image_bytes" != "$IMAGE_BYTES" ]]; then
    log "FATAL: ${IMAGE} is ${image_bytes} B, expected exactly ${IMAGE_BYTES}"
    exit 1
fi
if [[ "$bytes" =~ ^[0-9]+$ ]] && (( bytes >= USABLE_FLOOR_BYTES )); then
    log "profile volume ready: ${IMAGE} is exactly 10 GiB, ${MOUNT_POINT} holds ${bytes} B usable"
else
    log "FATAL: ${MOUNT_POINT} holds ${bytes:-?} B after mounting, below the 9.5 GiB floor"
    exit 1
fi
