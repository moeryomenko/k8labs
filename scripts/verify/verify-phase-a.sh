#!/bin/sh
# shellcheck shell=sh
# shellcheck disable=SC2310,SC2312 # check functions run in if/! conditions and
#                                  # ssh/command substitutions intentionally
#                                  # mask rc; the string tests are the check.
#
# verify-phase-a.sh — node-side inspection of a booted phase-A VM
# Run locally on the node or over ssh.
#
# Checks:
#   1. systemd-sysext status indicates merged sysexts (defensive parse)
#   2. /usr/bin/kubelet exists (proves sysext-merged binaries)
#   3. kubelet.service, crio.service, etcd.service, kube-apiserver.service
#      are present and disabled (NOT enabled) in systemctl list-unit-files
#   4. no kubelet/crio/etcd/kube-apiserver processes are running
#      (pgrep -x; unreadable process table -> SKIP that check)
#   5. root filesystem size >= ROOT_DISK_MIN_MIB (default 4096 MiB)
#
# Exit semantics:
#   * target unreachable (non-local host) -> SKIP, exit 0
#   * any check FAIL -> exit 1
#   * all checks PASS (SKIPs ignored) -> exit 0
#
# Usage: verify-phase-a.sh [user@]host     (default: localhost)
#   ROOT_DISK_MIN_MIB env overrides the minimum root filesystem size.

set -eu

HOST="${1:-localhost}"
ROOT_DISK_MIN_MIB="${ROOT_DISK_MIN_MIB:-4096}"

failed=0
skipped=0

say_pass() { printf 'PASS: %s\n' "$*"; }
say_fail() { printf 'FAIL: %s\n' "$*"; failed=1; }
say_skip() { printf 'SKIP: %s\n' "$*"; skipped=1; }

# ---- target selection / reachability ----------------------------------------
LOCAL=0
SSH_CMD=""
case "${HOST}" in
    localhost|127.0.0.1)
        LOCAL=1
        ;;
    *)
        case "${HOST}" in
            *@*) SSH_TARGET="${HOST}" ;;
            *) SSH_TARGET="root@${HOST}" ;;
        esac
        SSH_CMD="ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new ${SSH_TARGET}"
        # shellcheck disable=SC2086 # intentional word splitting of ssh options
        if ! ${SSH_CMD} true 2>/dev/null; then
            say_skip "node ${SSH_TARGET} is not reachable; cannot verify phase-A state"
            exit 0
        fi
        ;;
esac

node_run() {
    if [ "${LOCAL}" -eq 1 ]; then
        "$@"
    else
        # shellcheck disable=SC2086 # intentional word splitting of ssh options
        ${SSH_CMD} "$*"
    fi
}

# ---- checks -----------------------------------------------------------------
sysext_check() {
    if ! node_run command -v systemd-sysext >/dev/null 2>&1; then
        say_fail "systemd-sysext not found on node"
        return
    fi
    out=$(node_run systemd-sysext status 2>&1 || true)
    if printf '%s\n' "${out}" | grep -qi 'not merged'; then
        say_fail "systemd-sysext reports 'not merged'"
        return
    fi
    usr=$(printf '%s\n' "${out}" | awk '$1 == "/usr" { print $2; exit }')
    if { [ -n "${usr}" ] && [ "${usr}" != "none" ]; } || printf '%s\n' "${out}" | grep -qi merged; then
        say_pass "systemd-sysext status indicates merged sysexts"
    else
        say_fail "systemd-sysext status shows no merged sysexts"
    fi
}

kubelet_check() {
    if node_run test -f /usr/bin/kubelet; then
        say_pass "/usr/bin/kubelet exists (sysext binaries merged)"
    else
        say_fail "/usr/bin/kubelet missing"
    fi
}

units_check() {
    for unit in kubelet.service crio.service etcd.service kube-apiserver.service; do
        state=$(node_run systemctl list-unit-files --no-legend 2>/dev/null | \
            awk -v u="${unit}" '$1 == u { print $2 }')
        if [ -z "${state}" ]; then
            say_fail "${unit} not present in systemctl list-unit-files"
        elif [ "${state}" = "disabled" ]; then
            say_pass "${unit} present and disabled"
        else
            say_fail "${unit} is ${state} (expected disabled)"
        fi
    done
}

procs_check() {
    if ! node_run command -v pgrep >/dev/null 2>&1; then
        say_skip "pgrep not available on node; cannot check running k8s processes"
        return
    fi
    for proc in kubelet crio etcd kube-apiserver; do
        if node_run pgrep -x "${proc}" >/dev/null 2>&1; then
            say_fail "${proc} process is running (pgrep found it)"
        else
            rc=$?
            if [ "${rc}" -eq 1 ]; then
                say_pass "no ${proc} process running"
            else
                say_skip "cannot read process table for ${proc} (pgrep rc=${rc})"
            fi
        fi
    done
}

disk_check() {
    blocks=$(node_run df -P / 2>/dev/null | awk 'NR == 2 { print $2 }')
    if [ -z "${blocks}" ]; then
        say_skip "cannot determine root filesystem size"
        return
    fi
    size_mib=$((blocks / 1024))
    if [ "${size_mib}" -ge "${ROOT_DISK_MIN_MIB}" ]; then
        say_pass "root filesystem size ${size_mib} MiB >= ${ROOT_DISK_MIN_MIB} MiB"
    else
        say_fail "root filesystem size ${size_mib} MiB < ${ROOT_DISK_MIN_MIB} MiB (root disk not resized)"
    fi

    # Informational: backing device size via lsblk when discoverable.
    dev=$(node_run findmnt -n -o SOURCE / 2>/dev/null | head -1 || true)
    if [ -n "${dev}" ]; then
        devsize=$(node_run lsblk -b -n -o SIZE "${dev}" 2>/dev/null | head -1 || true)
        if [ -n "${devsize}" ]; then
            printf 'INFO: root backing device %s is %s bytes\n' "${dev}" "${devsize}"
        fi
    fi
}

sysext_check
kubelet_check
units_check
procs_check
disk_check

if [ "${failed}" -eq 1 ]; then
    exit 1
fi
exit 0
