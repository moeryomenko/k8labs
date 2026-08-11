#!/bin/sh
# shellcheck disable=SC2292  # POSIX sh per AGENTS.md: [ ] not [[ ]]; rc forces shell=bash
# shellcheck disable=SC2310,SC2311,SC2312 # check functions run in if/!
#                                  # conditions and command substitutions
#                                  # intentionally mask rc; the string tests
#                                  # are the checks.
# shellcheck disable=SC2086  # intentional word splitting of the expected-image
#                            # and unit lists built with unquoted concat
# =============================================================================
# verify-node-confexts.sh — live-node checks for the phase-B confext merge
# Run locally on a
# node or over ssh from the host.
#
# Checks (role-aware; role from NODE_ROLE env or the node hostname cp*/w*):
#   1. /var/lib/confexts/ contains the expected z- images:
#        z-kubelet-<node> always; z-etcd + z-kubernetes-cp on control-plane.
#   2. `systemd-confext status` lists them AND the merged /etc files exist
#      (/etc/kubernetes/kubelet.conf always; /etc/etcd/etcd.conf.yml +
#      /etc/kubernetes/cp.env on control-plane). The /etc files prove the
#      overlay is actually merged, not merely listed.
#   3. systemctl is-enabled == enabled and is-active == active for
#      crio.service + kubelet.service always; etcd.service,
#      kube-apiserver.service, kube-controller-manager.service and
#      kube-scheduler.service on control-plane. On workers, etcd.service and
#      kube-apiserver.service must NOT be enabled (worker order is
#      crio -> kubelet only).
#   4. On control-plane: `etcdctl endpoint health` prints "is healthy" and
#      `curl -k https://<cp_ip>:6443/healthz` returns HTTP 200 with body "ok".
#      cp_ip comes from arg 2 or the CP_IP env; when absent the /healthz check
#      SKIPs (the caller-side input is missing, not a node defect).
#
# Exit semantics:
#   * target unreachable (non-local host) -> SKIP, exit 0
#   * local host with an unrecognized hostname and no /var/lib/confexts ->
#     SKIP, exit 0 (not a phase-B node)
#   * any check FAIL -> exit 1
#   * all checks PASS (SKIPs ignored) -> exit 0
#
# Usage: verify-node-confexts.sh [user@]host [CP_IP]
#   NODE_ROLE=control-plane|worker overrides hostname-derived role detection.
#   CP_IP may instead be provided via the CP_IP environment variable.
# =============================================================================

set -eu

HOST="${1:-localhost}"
CP_IP="${2:-${CP_IP:-}}"

pass_count=0
fail_count=0
skip_count=0
failed=0

say_pass() { printf 'PASS: %s\n' "$*"; pass_count=$((pass_count + 1)); }
say_fail() { printf 'FAIL: %s\n' "$*"; fail_count=$((fail_count + 1)); failed=1; }
say_skip() { printf 'SKIP: %s\n' "$*"; skip_count=$((skip_count + 1)); }

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
        SSH_CMD="ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${SSH_TARGET}"
        # shellcheck disable=SC2086 # intentional word splitting of ssh options
        if ! ${SSH_CMD} true 2>/dev/null; then
            say_skip "node ${SSH_TARGET} is not reachable; cannot verify phase-B confext state"
            printf 'RESULT: SKIP (%d pass, %d fail, %d skip)\n' "${pass_count}" "${fail_count}" "${skip_count}"
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

# ---- node role detection ----------------------------------------------------
hn=$(node_run hostname 2>/dev/null | tr -d '\r' || true)
role="${NODE_ROLE:-}"
if [ -z "${role}" ]; then
    case "${hn}" in
        cp*) role=control-plane ;;
        w*) role=worker ;;
        *)
            # not a recognizable k8s node: SKIP when there is no phase-B
            # confexts dir, otherwise FAIL (it is a node, just oddly named)
            if node_run test -d /var/lib/confexts 2>/dev/null; then
                say_fail "cannot determine node role from hostname '${hn}'; set NODE_ROLE=control-plane|worker"
            else
                say_skip "host '${HOST}' is not a phase-B node (hostname '${hn}', no /var/lib/confexts)"
                printf 'RESULT: SKIP (%d pass, %d fail, %d skip)\n' "${pass_count}" "${fail_count}" "${skip_count}"
                exit 0
            fi
            ;;
    esac
fi
printf 'INFO: node=%s hostname=%s role=%s\n' "${HOST}" "${hn}" "${role}"

# ---- check 1: expected z- images in /var/lib/confexts -----------------------
check_images() {
    _expected="z-kubelet-${hn}"
    if [ "${role}" = "control-plane" ]; then
        _expected="${_expected} z-etcd z-kubernetes-cp"
    fi
    for _img in ${_expected}; do
        if node_run test -f "/var/lib/confexts/${_img}.raw"; then
            say_pass "/var/lib/confexts/${_img}.raw present"
        else
            say_fail "/var/lib/confexts/${_img}.raw missing"
        fi
    done
}

# ---- check 2: systemd-confext status lists images + merged /etc files -------
check_status() {
    if ! node_run command -v systemd-confext >/dev/null 2>&1; then
        say_fail "systemd-confext not found on node"
        return
    fi
    _out=$(node_run systemd-confext status 2>&1 || true)
    _expected="z-kubelet-${hn}"
    if [ "${role}" = "control-plane" ]; then
        _expected="${_expected} z-etcd z-kubernetes-cp"
    fi
    for _img in ${_expected}; do
        if printf '%s\n' "${_out}" | grep -F "${_img}" >/dev/null; then
            say_pass "systemd-confext status lists ${_img}"
        else
            say_fail "systemd-confext status does not list ${_img}"
        fi
    done
    # merged /etc files prove the overlay is actually merged
    _merged_files="/etc/kubernetes/kubelet.conf"
    if [ "${role}" = "control-plane" ]; then
        _merged_files="${_merged_files} /etc/etcd/etcd.conf.yml /etc/kubernetes/cp.env"
    fi
    for _f in ${_merged_files}; do
        if node_run test -f "${_f}"; then
            say_pass "merged file ${_f} present"
        else
            say_fail "merged file ${_f} missing (confext not merged?)"
        fi
    done
}

# ---- check 3: units enabled + active per role --------------------------------
check_units() {
    _units="crio.service kubelet.service"
    if [ "${role}" = "control-plane" ]; then
        _units="${_units} etcd.service kube-apiserver.service kube-controller-manager.service kube-scheduler.service"
    fi
    for _unit in ${_units}; do
        _enabled=$(node_run systemctl is-enabled "${_unit}" 2>&1 || true)
        if [ "${_enabled}" = "enabled" ]; then
            say_pass "${_unit} is-enabled: enabled"
        else
            say_fail "${_unit} is-enabled: ${_enabled} (expected enabled)"
        fi
        _active=$(node_run systemctl is-active "${_unit}" 2>&1 || true)
        if [ "${_active}" = "active" ]; then
            say_pass "${_unit} is-active: active"
        else
            say_fail "${_unit} is-active: ${_active} (expected active)"
        fi
    done
    if [ "${role}" = "worker" ]; then
        for _unit in etcd.service kube-apiserver.service; do
            _enabled=$(node_run systemctl is-enabled "${_unit}" 2>&1 || true)
            if [ "${_enabled}" = "enabled" ]; then
                say_fail "${_unit} is-enabled: enabled (worker must not enable control-plane units)"
            else
                say_pass "${_unit} is-enabled: ${_enabled} (worker does not enable it)"
            fi
        done
    fi
}

# ---- check 4: control-plane health gates ------------------------------------
check_health() {
    if [ "${role}" != "control-plane" ]; then
        return
    fi
    if ! node_run command -v etcdctl >/dev/null 2>&1; then
        say_fail "etcdctl not found on control-plane node (etcd sysext not merged?)"
    else
        _health=$(node_run etcdctl endpoint health 2>&1 || true)
        if printf '%s\n' "${_health}" | grep -F "is healthy" >/dev/null; then
            say_pass "etcdctl endpoint health: healthy"
        else
            say_fail "etcdctl endpoint health: not healthy: ${_health}"
        fi
    fi
    if [ -z "${CP_IP}" ]; then
        say_skip "apiserver /healthz check skipped: CP_IP not provided (pass as arg 2 or via CP_IP env)"
        return
    fi
    if ! node_run command -v curl >/dev/null 2>&1; then
        say_fail "curl not found on control-plane node (apiserver /healthz cannot be verified)"
        return
    fi
    _code=$(node_run curl -k -s -o /dev/null -w '%{http_code}' "https://${CP_IP}:6443/healthz" 2>&1 || true)
    if [ "${_code}" = "200" ]; then
        say_pass "kube-apiserver https://${CP_IP}:6443/healthz returns HTTP 200"
    else
        say_fail "kube-apiserver /healthz returned HTTP ${_code} (expected 200)"
    fi
    _body=$(node_run curl -k -s "https://${CP_IP}:6443/healthz" 2>&1 || true)
    if [ "${_body}" = "ok" ]; then
        say_pass "kube-apiserver /healthz body: ok"
    else
        say_fail "kube-apiserver /healthz body: '${_body}' (expected ok)"
    fi
}

check_images
check_status
check_units
check_health

if [ "${failed}" -eq 1 ]; then
    result=FAIL
else
    result=PASS
fi
printf 'RESULT: %s (%d pass, %d fail, %d skip)\n' "${result}" "${pass_count}" "${fail_count}" "${skip_count}"
[ "${failed}" -ne 1 ]
