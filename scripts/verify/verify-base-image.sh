#!/bin/sh
# shellcheck shell=sh
# shellcheck disable=SC2310,SC2312 # check functions run in if/! conditions and
#                                  # guestfish/sudo substitutions intentionally
#                                  # mask rc; the string tests are the check.
# shellcheck disable=SC2329 # cleanup() is invoked indirectly via trap EXIT.
#
# verify-base-image.sh — inspect build/k8labs-base.qcow2 for the baked
# sysext/confext images and static prerequisites.
#
# Read-only inspection. Backends, in preference order:
#   1. guestfish (libguestfs)  — no root required, LVM handled automatically
#   2. qemu-nbd loopback + read-only mount  — needs root; LVM handled generically
#
# Exit semantics:
#   * image absent            -> FAIL, exit 1   (that IS the detection)
#   * image present, no usable backend or mount failure -> SKIP, exit 0
#   * any check FAIL          -> exit 1
#   * all checks PASS (SKIPs ignored) -> exit 0
#
# Usage: verify-base-image.sh [path-to-qcow2]
#   IMAGE env var is honored too; default is <repo>/build/k8labs-base.qcow2

set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/../.." && pwd)
IMAGE="${1:-${IMAGE:-${REPO_ROOT}/build/k8labs-base.qcow2}}"

# The exact baked extension sets.
EXPECTED_SYSEXTS='kubelet.raw
cri-o.raw
crun.raw
cni.raw
etcd.raw
kubernetes-cp.raw
perfetto.raw'

EXPECTED_CONFEXTS='confext-cri-o.raw
confext-kubernetes.raw
confext-containers.raw'

failed=0
skipped=0

say_pass() { printf 'PASS: %s\n' "$*"; }
say_fail() { printf 'FAIL: %s\n' "$*"; failed=1; }
say_skip() { printf 'SKIP: %s\n' "$*"; skipped=1; }

# ---- image presence ---------------------------------------------------------
[ -f "${IMAGE}" ] || {
    say_fail "base image not found: ${IMAGE} (expected at build/k8labs-base.qcow2)"
    exit 1
}

# ---- backend selection ------------------------------------------------------
MODE=""
if command -v guestfish >/dev/null 2>&1; then
    MODE=guestfish
elif command -v qemu-nbd >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ] || sudo -n true 2>/dev/null; then
        MODE=nbd
    fi
fi

if [ -z "${MODE}" ]; then
    say_skip "neither guestfish nor a usable qemu-nbd (root) backend is available; cannot inspect ${IMAGE}"
    exit 0
fi

# ---- state + cleanup --------------------------------------------------------
MNT=""
NBD_DEV=""
NBD_VG=""

cleanup() {
    if [ -n "${MNT}" ] && grep -q " ${MNT} " /proc/mounts 2>/dev/null; then
        sudo umount "${MNT}" 2>/dev/null || true
    fi
    if [ -n "${NBD_VG}" ]; then
        sudo vgchange -an "${NBD_VG}" >/dev/null 2>&1 || true
    fi
    if [ -n "${NBD_DEV}" ]; then
        sudo qemu-nbd --disconnect "${NBD_DEV}" >/dev/null 2>&1 || true
    fi
    if [ -n "${MNT}" ] && [ -d "${MNT}" ]; then
        rmdir "${MNT}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---- filesystem access abstraction ------------------------------------------
if [ "${MODE}" = "guestfish" ]; then
    fs_ls() { guestfish --ro -a "${IMAGE}" -i ls "$1" 2>/dev/null || true; }
    fs_test_f() { [ "$(guestfish --ro -a "${IMAGE}" -i is-file "$1" 2>/dev/null)" = "true" ]; }
    fs_test_L() { [ "$(guestfish --ro -a "${IMAGE}" -i is-symlink "$1" 2>/dev/null)" = "true" ]; }
else
    fs_ls() { ls -1 "${MNT}${1}" 2>/dev/null || true; }
    fs_test_f() { [ -f "${MNT}${1}" ]; }
    fs_test_L() { [ -L "${MNT}${1}" ]; }
fi

# ---- qemu-nbd read-only mount (LVM-aware) -----------------------------------
nbd_mount() {
    if [ "$(id -u)" -ne 0 ]; then
        sudo modprobe nbd max_part=8 >/dev/null 2>&1 || true
        sudo udevadm settle >/dev/null 2>&1 || sleep 1
    else
        modprobe nbd max_part=8 >/dev/null 2>&1 || true
        udevadm settle >/dev/null 2>&1 || sleep 1
    fi

    i=0
    while [ "${i}" -lt 16 ]; do
        [ -e "/dev/nbd${i}" ] || { i=$((i + 1)); continue; }
        if sudo qemu-nbd --connect="/dev/nbd${i}" --read-only "${IMAGE}" >/dev/null 2>&1; then
            NBD_DEV="/dev/nbd${i}"
            if [ "$(id -u)" -ne 0 ]; then
                sudo udevadm settle >/dev/null 2>&1 || sleep 1
            else
                udevadm settle >/dev/null 2>&1 || sleep 1
            fi
            if mount_root_device; then
                return 0
            fi
            # This device is unusable (e.g. wedged from an interrupted run) —
            # release it and try the next one.
            if [ -n "${NBD_VG}" ]; then
                sudo vgchange -an "${NBD_VG}" >/dev/null 2>&1 || true
                NBD_VG=""
            fi
            sudo qemu-nbd --disconnect "${NBD_DEV}" >/dev/null 2>&1 || true
            NBD_DEV=""
        fi
        i=$((i + 1))
    done
    return 1
}

mount_root_device() {
    # LVM path: find a PV on our device, activate only that VG, mount its root LV.
    if command -v pvs >/dev/null 2>&1 && command -v vgchange >/dev/null 2>&1 && \
       command -v blkid >/dev/null 2>&1; then
        _pv=""
        for _part in "${NBD_DEV}"p* "${NBD_DEV}"; do
            [ -e "${_part}" ] || continue
            if [ "$(sudo blkid -s TYPE -o value "${_part}" 2>/dev/null)" = "LVM2_member" ]; then
                _pv="${_part}"
                break
            fi
        done
        if [ -n "${_pv}" ]; then
            NBD_VG=$(sudo pvs --noheadings -o vg_name "${_pv}" 2>/dev/null | tr -d ' ' | head -1)
            if [ -n "${NBD_VG}" ]; then
                sudo vgchange -ay "${NBD_VG}" >/dev/null 2>&1 || true
                for _lv in /dev/"${NBD_VG}"/*; do
                    [ -e "${_lv}" ] || continue
                    if sudo mount -o ro "${_lv}" "${MNT}" >/dev/null 2>&1; then
                        if [ -f "${MNT}/etc/os-release" ]; then
                            return 0
                        fi
                        sudo umount "${MNT}" >/dev/null 2>&1 || true
                    fi
                done
                # No LV mounted — leave the VG inactive for the next device.
                sudo vgchange -an "${NBD_VG}" >/dev/null 2>&1 || true
                NBD_VG=""
            fi
        fi
    fi

    # Plain partition path: try each partition, then the whole device.
    for _part in "${NBD_DEV}"p* "${NBD_DEV}"; do
        [ -e "${_part}" ] || continue
        if sudo mount -o ro "${_part}" "${MNT}" >/dev/null 2>&1; then
            if [ -f "${MNT}/etc/os-release" ]; then
                return 0
            fi
            sudo umount "${MNT}" >/dev/null 2>&1 || true
        fi
    done
    return 1
}

if [ "${MODE}" = "nbd" ]; then
    MNT=$(mktemp -d "${TMPDIR:-/tmp}/k8labs-base-verify.XXXXXX")
    if ! nbd_mount; then
        say_skip "could not mount ${IMAGE} read-only (qemu-nbd backend); cannot inspect"
        exit 0
    fi
fi

# ---- checks -----------------------------------------------------------------
check_sysext_dir() {
    listing=$(fs_ls /var/lib/extensions)
    actual=$(printf '%s\n' "${listing}" | grep '\.raw$' | LC_ALL=C sort)
    expected=$(printf '%s\n' "${EXPECTED_SYSEXTS}" | LC_ALL=C sort)
    if [ "${actual}" = "${expected}" ]; then
        say_pass "/var/lib/extensions contains exactly the 7 sysext .raw images"
    else
        found=$(printf '%s\n' "${listing}" | grep -c '\.raw$' || true)
        say_fail "/var/lib/extensions does not contain exactly the 7 sysext .raw images (found ${found} .raw)"
    fi
}

check_confext_dir() {
    listing=$(fs_ls /var/lib/confexts)
    actual=$(printf '%s\n' "${listing}" | grep '\.raw$' | LC_ALL=C sort)
    expected=$(printf '%s\n' "${EXPECTED_CONFEXTS}" | LC_ALL=C sort)
    if [ "${actual}" = "${expected}" ]; then
        say_pass "/var/lib/confexts contains exactly the 3 static confext .raw images"
    else
        found=$(printf '%s\n' "${listing}" | grep -c '\.raw$' || true)
        say_fail "/var/lib/confexts does not contain exactly the 3 static confext .raw images (found ${found} .raw)"
    fi
}

check_conmon() {
    if fs_test_f /usr/bin/conmon; then
        say_pass "/usr/bin/conmon exists"
    else
        say_fail "/usr/bin/conmon missing"
    fi
}

check_resize() {
    if fs_test_f /usr/local/sbin/resize-rootfs.sh; then
        say_pass "/usr/local/sbin/resize-rootfs.sh exists"
    else
        say_fail "/usr/local/sbin/resize-rootfs.sh missing"
    fi
}

check_merge_enabled() {
    # Deferred-merge contract (ratified in the E2E replay): the image must enable
    # k8slab-merge.service — a oneshot ordered After=cloud-config.service /
    # Before=multi-user.target that runs the sysext/confext refresh. The merge
    # must not happen at sysinit (that renders /etc read-only before cloud-init
    # can write its network profile). After=cloud-init.target is NOT used: on
    # Fedora 44 it has After=multi-user.target, which forms an ordering cycle
    # (multi-user.target -> k8slab-merge -> cloud-init.target -> multi-user.target)
    # that systemd skips the unit entirely; cloud-config.service is the last
    # cloud-init stage that writes /etc and has no multi-user.target dependency.
    found=0
    for wants in sysinit.target.wants multi-user.target.wants; do
        if fs_ls "/etc/systemd/system/${wants}" | grep -qx "k8slab-merge.service"; then
            found=1
            break
        fi
    done
    if [ "${found}" -eq 0 ] && fs_test_L "/etc/systemd/system/k8slab-merge.service"; then
        found=1
    fi
    if [ "${found}" -eq 1 ]; then
        say_pass "k8slab-merge.service enabled in image (unit symlink present)"
    else
        say_fail "k8slab-merge.service not enabled in image (no unit symlink found)"
    fi
}

check_autostart_disabled() {
    # Deferred-merge contract: systemd-sysext/systemd-confext must NOT
    # be auto-enabled at sysinit — their boot-time merge renders /etc read-only
    # before cloud-init, breaking first-boot network config. The merge
    # is deferred to k8slab-merge.service (check_merge_enabled).
    for svc in systemd-sysext systemd-confext; do
        found=0
        for wants in sysinit.target.wants multi-user.target.wants; do
            if fs_ls "/etc/systemd/system/${wants}" | grep -qx "${svc}.service"; then
                found=1
                break
            fi
        done
        if [ "${found}" -eq 0 ] && fs_test_L "/etc/systemd/system/${svc}.service"; then
            found=1
        fi
        if [ "${found}" -eq 1 ]; then
            say_fail "${svc} auto-enabled in image (unit symlink present in enablement dirs — breaks cloud-init first boot)"
        else
            say_pass "${svc} not auto-enabled in image (no unit symlink in enablement dirs)"
        fi
    done
}

check_sysext_dir
check_confext_dir
check_conmon
check_resize
check_merge_enabled
check_autostart_disabled

if [ "${failed}" -eq 1 ]; then
    exit 1
fi
exit 0
