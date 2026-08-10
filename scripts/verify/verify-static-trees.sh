#!/bin/sh
# =============================================================================
# verify-static-trees.sh — static-tree hygiene acceptance checks
#
# Asserts the Section 3.1 static-tree hygiene invariants of
# the node-provisioning spec, covering Verification
# contracts (placeholder-free static trees) and the static part of
# (release metadata at the systemd image paths).
#
# Each of the nine numbered checks prints exactly one `PASS: <label>` /
# `FAIL: <label>` line on stdout; per-check diagnostics go to stderr. The
# script exits 0 iff every check passes.
#
# USAGE:
#   ./scripts/verify/verify-static-trees.sh
#
# EXIT CODES:
#   0 — every check PASSed
#   1 — at least one check FAILed, a required directory is missing, or a
#       required tool is unavailable
#
# Notes:
#   - Read-only: never modifies the tree.
#   - POSIX sh (#!/bin/sh) per AGENTS.md conventions. `set -e` is deliberately
#     not used: checks intentionally run greps that return non-zero and each
#     check manages its own status.
#   - Runs from any cwd; the repository root is resolved from this script's
#     own path.
# =============================================================================

set -u

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Layout: <repo>/scripts/verify/verify-static-trees.sh
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
ROOT=$(CDPATH='' cd -- "${SCRIPT_DIR}/../.." && pwd -P) || exit 1

SYSEXT_DIR="${ROOT}/sysext"
CONFEXT_DIR="${ROOT}/confext"

# Bail with a clear message only when the resolved root looks wrong entirely
# (both trees missing). Individual missing trees are reported per check.
if [ ! -d "${SYSEXT_DIR}" ] && [ ! -d "${CONFEXT_DIR}" ]; then
    printf 'ERROR: cannot locate repository trees under %s\n' "${ROOT}" >&2
    exit 1
fi

for _cmd in grep sed find; do
    if ! command -v "${_cmd}" >/dev/null 2>&1; then
        printf 'ERROR: required tool not found: %s\n' "${_cmd}" >&2
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# assert_contains FILE PATTERN DESCRIPTION
#   Fixed-string grep for PATTERN inside FILE. On a miss, prints DESCRIPTION
#   to stderr and returns 1. `-e` keeps PATTERN safe when it starts with `-`
#   (e.g. systemd unit flags like --kubeconfig=...).
assert_contains() {
    _ac_file=$1
    _ac_pattern=$2
    _ac_description=$3

    if ! grep -qF -e "${_ac_pattern}" "${_ac_file}"; then
        printf '    missing: %s\n' "${_ac_description}" >&2
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Check 1 — no placeholder tokens under sysext/ or confext/
#
# Deviation from the literal check command: `-I` (ignore binary files) is
# added. The shipped sysext binaries embed `{{`/`}}` byte sequences (Go
# template/format strings), so the literal `grep -rE` would always match
# them and the check could never pass. The check targets placeholder tokens in
# static text content, which `grep -rI` isolates (verified: with -I the only
# matches are the real {{CP_IP}}/CONTROL_PLANE_IP tokens in unit/config
# files). Escalated to @build with this evidence.
# ---------------------------------------------------------------------------
check_placeholders() {
    label='check-1 placeholder-free static trees'
    if [ ! -d "${SYSEXT_DIR}" ]; then
        printf 'FAIL: %s\n' "${label}"
        printf '    missing directory: %s\n' "${SYSEXT_DIR}" >&2
        return 1
    fi
    if [ ! -d "${CONFEXT_DIR}" ]; then
        printf 'FAIL: %s\n' "${label}"
        printf '    missing directory: %s\n' "${CONFEXT_DIR}" >&2
        return 1
    fi

    matches=$(grep -rI -E '\{\{|\}\}|CONTROL_PLANE_IP' "${SYSEXT_DIR}" "${CONFEXT_DIR}")
    status=$?
    if [ "${status}" -eq 0 ]; then
        printf 'FAIL: %s\n' "${label}"
        printf '    placeholder tokens found:\n' >&2
        printf '%s\n' "${matches}" | sed 's/^/      /' >&2
        return 1
    fi
    if [ "${status}" -eq 1 ]; then
        printf 'PASS: %s\n' "${label}"
        return 0
    fi
    printf 'FAIL: %s\n' "${label}"
    printf '    grep failed with exit status %s\n' "${status}" >&2
    return 1
}

# ---------------------------------------------------------------------------
# Check 2 — sysext release metadata at usr/lib/extension-release.d/
#
# Expected metadata filenames follow the existing per-tree naming that the
# implementation moves into place (sysext tree cri-o carries crio.sysext,
# matching extensions/manifest.yaml extension_name: crio).
# ---------------------------------------------------------------------------
check_sysext_metadata() {
    label='check-2 sysext metadata paths'
    set -- \
        'kubelet kubelet.sysext' \
        'cri-o crio.sysext' \
        'crun crun.sysext' \
        'cni cni.sysext' \
        'etcd etcd.sysext' \
        'kubernetes-cp kubernetes-cp.sysext' \
        'perfetto perfetto.sysext'
    failed=0
    while [ $# -gt 0 ]; do
        pair=$1
        tree=${pair%% *}
        meta=${pair##* }
        file="${SYSEXT_DIR}/${tree}/usr/lib/extension-release.d/${meta}"
        old="${SYSEXT_DIR}/${tree}/extension-release.d"
        if [ ! -f "${file}" ]; then
            printf '    missing sysext metadata: %s\n' "${file}" >&2
            failed=1
        fi
        if [ -e "${old}" ]; then
            printf '    tree-root extension-release.d still present: %s\n' "${old}" >&2
            failed=1
        fi
        shift
    done
    if [ "${failed}" -eq 0 ]; then
        printf 'PASS: %s\n' "${label}"
        return 0
    fi
    printf 'FAIL: %s\n' "${label}"
    return 1
}

# ---------------------------------------------------------------------------
# Check 3 — confext release metadata at etc/extension-release.d/
# ---------------------------------------------------------------------------
check_confext_metadata() {
    label='check-3 confext metadata paths'
    set -- \
        'cri-o cri-o.confext' \
        'kubernetes kubernetes.confext' \
        'containers containers.confext'
    failed=0
    while [ $# -gt 0 ]; do
        pair=$1
        tree=${pair%% *}
        meta=${pair##* }
        file="${CONFEXT_DIR}/${tree}/etc/extension-release.d/${meta}"
        old="${CONFEXT_DIR}/${tree}/extension-release.d"
        if [ ! -f "${file}" ]; then
            printf '    missing confext metadata: %s\n' "${file}" >&2
            failed=1
        fi
        if [ -e "${old}" ]; then
            printf '    tree-root extension-release.d still present: %s\n' "${old}" >&2
            failed=1
        fi
        shift
    done
    if [ "${failed}" -eq 0 ]; then
        printf 'PASS: %s\n' "${label}"
        return 0
    fi
    printf 'FAIL: %s\n' "${label}"
    return 1
}

# ---------------------------------------------------------------------------
# Check 4 — confext set is exactly cri-o, kubernetes,
# containers (worker/etcd/kubernetes-cp must not exist)
# ---------------------------------------------------------------------------
check_confext_set() {
    label='check-4 confext set'
    if [ ! -d "${CONFEXT_DIR}" ]; then
        printf 'FAIL: %s\n' "${label}"
        printf '    missing directory: %s\n' "${CONFEXT_DIR}" >&2
        return 1
    fi
    failed=0
    for tree in cri-o kubernetes containers; do
        if [ ! -d "${CONFEXT_DIR}/${tree}" ]; then
            printf '    missing required confext tree: %s\n' "${CONFEXT_DIR}/${tree}" >&2
            failed=1
        fi
    done
    for tree in worker etcd kubernetes-cp; do
        if [ -e "${CONFEXT_DIR}/${tree}" ]; then
            printf '    forbidden confext tree exists: %s\n' "${CONFEXT_DIR}/${tree}" >&2
            failed=1
        fi
    done
    if [ "${failed}" -eq 0 ]; then
        printf 'PASS: %s\n' "${label}"
        return 0
    fi
    printf 'FAIL: %s\n' "${label}"
    return 1
}

# ---------------------------------------------------------------------------
# Check 5 — kubelet config.yaml has staticPodPath: "" and no
# sysctl.d/modules-load.d files under confext/kubernetes/
# ---------------------------------------------------------------------------
check_kubelet_config() {
    label='check-5 kubelet config hygiene'
    config="${CONFEXT_DIR}/kubernetes/etc/kubernetes/kubelet/config.yaml"
    if [ ! -f "${config}" ]; then
        printf 'FAIL: %s\n' "${label}"
        printf '    missing file: %s\n' "${config}" >&2
        return 1
    fi
    failed=0
    if ! grep -q 'staticPodPath: ""' "${config}"; then
        printf '    kubelet config.yaml lacks staticPodPath: ""\n' >&2
        failed=1
    fi
    for d in sysctl.d modules-load.d; do
        dir="${CONFEXT_DIR}/kubernetes/etc/${d}"
        if [ -d "${dir}" ]; then
            files=$(find "${dir}" -type f)
            if [ -n "${files}" ]; then
                printf '    files remain under %s:\n' "${dir}" >&2
                printf '%s\n' "${files}" | sed 's/^/      /' >&2
                failed=1
            fi
        fi
    done
    if [ "${failed}" -eq 0 ]; then
        printf 'PASS: %s\n' "${label}"
        return 0
    fi
    printf 'FAIL: %s\n' "${label}"
    return 1
}

# ---------------------------------------------------------------------------
# Check 6 — crio.conf references only /usr/bin/conmon (no conmonrs)
# ---------------------------------------------------------------------------
check_crio_conmon() {
    label='check-6 crio.conf conmon references'
    conf="${CONFEXT_DIR}/cri-o/etc/crio/crio.conf"
    if [ ! -f "${conf}" ]; then
        printf 'FAIL: %s\n' "${label}"
        printf '    missing file: %s\n' "${conf}" >&2
        return 1
    fi
    failed=0
    if grep -q 'conmonrs' "${conf}"; then
        printf '    conmonrs references remain:\n' >&2
        grep -n 'conmonrs' "${conf}" | sed 's/^/      /' >&2
        failed=1
    fi
    bad=$(grep -n 'conmon' "${conf}" | grep -v '/usr/bin/conmon')
    if [ -n "${bad}" ]; then
        printf '    conmon references other than /usr/bin/conmon:\n' >&2
        printf '%s\n' "${bad}" | sed 's/^/      /' >&2
        failed=1
    fi
    if [ "${failed}" -eq 0 ]; then
        printf 'PASS: %s\n' "${label}"
        return 0
    fi
    printf 'FAIL: %s\n' "${label}"
    return 1
}

# ---------------------------------------------------------------------------
# Check 7 — kube-apiserver unit de-placeholdered, consumes cp.env,
# uses ${KUBE_ADVERTISE_ADDRESS}/${KUBE_ETCD_SERVERS}, keeps the aggregation
# flags, and is gated on the CA existing.
# ---------------------------------------------------------------------------
check_apiserver_unit() {
    label='check-7 kube-apiserver unit'
    unit="${SYSEXT_DIR}/kubernetes-cp/usr/lib/systemd/system/kube-apiserver.service"
    if [ ! -f "${unit}" ]; then
        printf 'FAIL: %s\n' "${label}"
        printf '    missing file: %s\n' "${unit}" >&2
        return 1
    fi
    failed=0
    if grep -qE '\{\{|\}\}' "${unit}"; then
        printf '    placeholder tokens remain in unit\n' >&2
        failed=1
    fi
    assert_contains "${unit}" 'EnvironmentFile=/etc/kubernetes/cp.env' 'EnvironmentFile=/etc/kubernetes/cp.env' || failed=1
    assert_contains "${unit}" "\${KUBE_ADVERTISE_ADDRESS}" 'KUBE_ADVERTISE_ADDRESS variable expansion' || failed=1
    assert_contains "${unit}" "\${KUBE_ETCD_SERVERS}" 'KUBE_ETCD_SERVERS variable expansion' || failed=1
    assert_contains "${unit}" '--requestheader-client-ca-file=/etc/kubernetes/pki/front-proxy-ca.pem' 'requestheader-client-ca-file aggregation flag' || failed=1
    assert_contains "${unit}" '--proxy-client-cert-file=/etc/kubernetes/pki/front-proxy-client.pem' 'proxy-client-cert-file aggregation flag' || failed=1
    assert_contains "${unit}" '--proxy-client-key-file=/etc/kubernetes/pki/front-proxy-client-key.pem' 'proxy-client-key-file aggregation flag' || failed=1
    assert_contains "${unit}" 'ConditionPathExists=/etc/kubernetes/pki/ca.pem' 'ConditionPathExists=/etc/kubernetes/pki/ca.pem' || failed=1
    if [ "${failed}" -eq 0 ]; then
        printf 'PASS: %s\n' "${label}"
        return 0
    fi
    printf 'FAIL: %s\n' "${label}"
    return 1
}

# ---------------------------------------------------------------------------
# Check 8 — kubelet unit kubeconfig path + gate
# ---------------------------------------------------------------------------
check_kubelet_unit() {
    label='check-8 kubelet unit'
    unit="${SYSEXT_DIR}/kubelet/usr/lib/systemd/system/kubelet.service"
    if [ ! -f "${unit}" ]; then
        printf 'FAIL: %s\n' "${label}"
        printf '    missing file: %s\n' "${unit}" >&2
        return 1
    fi
    failed=0
    assert_contains "${unit}" '--kubeconfig=/etc/kubernetes/kubelet.conf' '--kubeconfig=/etc/kubernetes/kubelet.conf' || failed=1
    assert_contains "${unit}" 'ConditionPathExists=/etc/kubernetes/kubelet.conf' 'ConditionPathExists=/etc/kubernetes/kubelet.conf' || failed=1
    if [ "${failed}" -eq 0 ]; then
        printf 'PASS: %s\n' "${label}"
        return 0
    fi
    printf 'FAIL: %s\n' "${label}"
    return 1
}

# ---------------------------------------------------------------------------
# Check 9 — etcd unit gate
# ---------------------------------------------------------------------------
check_etcd_unit() {
    label='check-9 etcd unit'
    unit="${SYSEXT_DIR}/etcd/usr/lib/systemd/system/etcd.service"
    if [ ! -f "${unit}" ]; then
        printf 'FAIL: %s\n' "${label}"
        printf '    missing file: %s\n' "${unit}" >&2
        return 1
    fi
    failed=0
    assert_contains "${unit}" 'ConditionPathExists=/etc/etcd/etcd.conf.yml' 'ConditionPathExists=/etc/etcd/etcd.conf.yml' || failed=1
    if [ "${failed}" -eq 0 ]; then
        printf 'PASS: %s\n' "${label}"
        return 0
    fi
    printf 'FAIL: %s\n' "${label}"
    return 1
}

# ---------------------------------------------------------------------------
# Driver — run every check, then exit 0 iff all passed
# ---------------------------------------------------------------------------
overall=0
check_placeholders || overall=1
check_sysext_metadata || overall=1
check_confext_metadata || overall=1
check_confext_set || overall=1
check_kubelet_config || overall=1
check_crio_conmon || overall=1
check_apiserver_unit || overall=1
check_kubelet_unit || overall=1
check_etcd_unit || overall=1
exit "${overall}"
