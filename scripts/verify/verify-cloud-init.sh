#!/bin/sh
# =============================================================================
# verify-cloud-init.sh — Phase-A cloud-init invariants
#
# Asserts that the cloud-init sources under terraform/cloud-init/ keep the
# phase-A boot contract:
#   [1] cloud_init.cfg adds a runcmd invoking /usr/local/sbin/resize-rootfs.sh
#   [2] cloud_init.cfg keeps the ssh_authorized_keys template line
#       (${ssh_public_key}), ssh_pwauth: false, disable_root: false
#   [3] meta-data.tmpl still sets local-hostname (hostname injection)
#   [4] network_config.cfg still requests dhcp4: true
#   [5] (optional) create-cloudinit.sh renders a sample user-data that parses
#       as cloud-config; SKIP when the render path is unavailable
#
# The script is read-only against the tree. It resolves the repo root from
# its own location, so it runs correctly from any cwd.
#
# USAGE:
#   ./scripts/verify/verify-cloud-init.sh
#
# EXIT CODES:
#   0 — every applicable check PASSed (check 5 may SKIP)
#   1 — at least one check FAILed
# =============================================================================

# This file is POSIX sh (AGENTS.md convention). The project .shellcheckrc
# defaults to shell=bash; declare the real shell here so ShellCheck applies
# the sh ruleset.
# shellcheck shell=sh

# The grep/sed patterns below deliberately match literal template tokens such
# as ${ssh_public_key} / ${hostname} / ${instance_id}; they must NOT be
# shell-expanded, so they are single-quoted on purpose (SC2016 is intentional).
# shellcheck disable=SC2016

set -eu

# ---------------------------------------------------------------------------
# Paths — resolve the repo root from the script location
# ---------------------------------------------------------------------------
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(CDPATH='' cd -- "${SCRIPT_DIR}/../.." && pwd -P)

CLOUD_INIT="${REPO_ROOT}/terraform/cloud-init/cloud_init.cfg"
META_DATA="${REPO_ROOT}/terraform/cloud-init/meta-data.tmpl"
NETWORK_CONFIG="${REPO_ROOT}/terraform/cloud-init/network_config.cfg"
CREATE_CLOUDINIT="${REPO_ROOT}/scripts/create-cloudinit.sh"

# ---------------------------------------------------------------------------
# Reporting — one PASS/FAIL line per check
# ---------------------------------------------------------------------------
failed=0

report_pass() {
    printf 'PASS: %s\n' "$1"
}

report_fail() {
    printf 'FAIL: %s\n' "$1"
    failed=1
}

# ---------------------------------------------------------------------------
# Check 1 — cloud_init.cfg invokes resize-rootfs.sh from a runcmd block
# ---------------------------------------------------------------------------
if [ ! -f "${CLOUD_INIT}" ]; then
    report_fail "[1] ${CLOUD_INIT} missing - cannot check runcmd resize hook"
elif awk '
    /^runcmd:/ { in_block = 1 }
    in_block && /\/usr\/local\/sbin\/resize-rootfs\.sh/ { found = 1 }
    in_block && /^[^[:space:]#]/ && !/^runcmd:/ { in_block = 0 }
    END { exit (found ? 0 : 1) }
' "${CLOUD_INIT}"; then
    report_pass "[1] cloud_init.cfg runcmd invokes /usr/local/sbin/resize-rootfs.sh"
else
    report_fail "[1] cloud_init.cfg has no runcmd invoking /usr/local/sbin/resize-rootfs.sh"
fi

# ---------------------------------------------------------------------------
# Check 2 — cloud_init.cfg keeps the ssh key template and auth policy
# ---------------------------------------------------------------------------
if [ ! -f "${CLOUD_INIT}" ]; then
    report_fail "[2] ${CLOUD_INIT} missing - cannot check ssh/auth policy"
else
    missing=''
    grep -Fq '${ssh_public_key}' "${CLOUD_INIT}" || missing='ssh_authorized_keys template line'
    grep -Fq 'ssh_pwauth: false' "${CLOUD_INIT}" || missing="${missing}${missing:+, }ssh_pwauth: false"
    grep -Fq 'disable_root: false' "${CLOUD_INIT}" || missing="${missing}${missing:+, }disable_root: false"
    if [ -n "${missing}" ]; then
        report_fail "[2] cloud_init.cfg lost: ${missing}"
    else
        report_pass "[2] cloud_init.cfg keeps ssh_authorized_keys template, ssh_pwauth: false, disable_root: false"
    fi
fi

# ---------------------------------------------------------------------------
# Check 3 — meta-data.tmpl keeps hostname injection
# ---------------------------------------------------------------------------
if [ ! -f "${META_DATA}" ]; then
    report_fail "[3] ${META_DATA} missing - cannot check local-hostname"
elif grep -Fq 'local-hostname:' "${META_DATA}" && grep -Fq '${hostname}' "${META_DATA}"; then
    report_pass "[3] meta-data.tmpl sets local-hostname (hostname injection preserved)"
else
    report_fail '[3] meta-data.tmpl no longer sets local-hostname / ${hostname} injection'
fi

# ---------------------------------------------------------------------------
# Check 4 — network_config.cfg keeps DHCP
# ---------------------------------------------------------------------------
if [ ! -f "${NETWORK_CONFIG}" ]; then
    report_fail "[4] ${NETWORK_CONFIG} missing - cannot check dhcp4"
elif grep -Fq 'dhcp4: true' "${NETWORK_CONFIG}"; then
    report_pass "[4] network_config.cfg requests dhcp4: true"
else
    report_fail "[4] network_config.cfg no longer requests dhcp4: true"
fi

# ---------------------------------------------------------------------------
# Check 5 (optional) — sample render parses as cloud-config
# ---------------------------------------------------------------------------
skip5_reason=''
if [ ! -x "${CREATE_CLOUDINIT}" ]; then
    skip5_reason="create-cloudinit.sh not found/executable: ${CREATE_CLOUDINIT}"
elif ! command -v mkdosfs >/dev/null 2>&1; then
    skip5_reason='mkdosfs not available (install dosfstools)'
elif ! command -v mcopy >/dev/null 2>&1; then
    skip5_reason='mcopy not available (install mtools)'
elif ! command -v mtype >/dev/null 2>&1; then
    skip5_reason='mtype not available (install mtools)'
elif [ ! -f "${CLOUD_INIT}" ] || [ ! -f "${META_DATA}" ] || [ ! -f "${NETWORK_CONFIG}" ]; then
    skip5_reason='one or more cloud-init sources missing (see checks 1-4)'
fi

if [ -n "${skip5_reason}" ]; then
    printf 'SKIP: [5] sample render path unavailable: %s\n' "${skip5_reason}"
elif ! tmpdir=$(mktemp -d); then
    printf 'SKIP: [5] cannot create temporary directory\n'
else
    trap 'rm -rf -- "$tmpdir"' EXIT HUP INT TERM
    if sed 's|\${ssh_public_key}|ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLE verify-cloud-init|' "${CLOUD_INIT}" > "${tmpdir}/user-data.yaml" \
        && sed 's|\${instance_id}|verify-cloud-init|; s|\${hostname}|verify-cloud-init|' "${META_DATA}" > "${tmpdir}/meta-data.yaml"; then
        if "${CREATE_CLOUDINIT}" \
            --user-data "${tmpdir}/user-data.yaml" \
            --meta-data "${tmpdir}/meta-data.yaml" \
            --network-config "${NETWORK_CONFIG}" \
            --output "${tmpdir}/cidata.img" > "${tmpdir}/create.log" 2>&1; then
            readback=$(mtype -i "${tmpdir}/cidata.img" ::user-data 2>/dev/null) || readback=''
            case "${readback}" in
                '#cloud-config'*)
                    report_pass '[5] rendered sample user-data parses as cloud-config (#cloud-config)'
                    ;;
                *)
                    report_fail '[5] rendered sample user-data is missing the #cloud-config marker'
                    ;;
            esac
        else
            create_log_line=$(sed -n '1p' "${tmpdir}/create.log") || create_log_line=''
            printf 'SKIP: [5] create-cloudinit.sh could not render without a live cluster: %s\n' "${create_log_line}"
        fi
    else
        printf 'SKIP: [5] could not render sample templates from the cloud-init sources\n'
    fi
fi

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
if [ "${failed}" -eq 0 ]; then
    exit 0
fi
exit 1
