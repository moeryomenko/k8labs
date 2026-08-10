#!/bin/sh
# shellcheck shell=sh
# POSIX sh per AGENTS.md conventions — the repo .shellcheckrc defaults to
# shell=bash; this file targets POSIX sh, so the directive above overrides it
# and ShellCheck enforces the real target dialect.
#
# verify-cluster-ops.sh — Verify the `make rbac` / `make cilium` cluster-ops
# prerequisites (static) and post-conditions (live).
#
# Asserts the spec's §3.5 cluster-ops requirements: the rbac/ and cilium/install/
# manifest groups, verified against the cluster post-conditions.
#
# STATIC section (no cluster required; must exit 0 once implemented):
#   1. rbac/ contains ClusterRole/ClusterRoleBinding manifests covering the
#      four categories extracted from ansible/playbooks/bootstrap.yml:159-259:
#        - bootstrap-token : system:kubelet-bootstrap (bootstrap.yml:176,185)
#        - system:nodes    : binding system:nodes -> role system:node
#                            (bootstrap.yml:198,202)
#        - system:admin    : binding system:admin -> cluster-admin
#                            (bootstrap.yml:218,222)
#        - apiserver-proxy : system:kube-apiserver-proxy (bootstrap.yml:238,247)
#   2. cilium/install/ holds pre-rendered install manifests (a cilium
#      ServiceAccount or Deployment/DaemonSet) plus a VERSION marker pinning
#      the Cilium version.
#   3. Makefile defines `rbac:` and `cilium:` targets.
#   4. `make help` lists `rbac` and `cilium`.
#
# LIVE section (SKIPs cleanly — exit 0 for the section — when ./kubeconfig is
#   absent or kubectl is unavailable):
#   - after `make rbac`:   kubectl get clusterrolebinding lists the bindings
#   - after `make cilium`: kube-system pods -l k8s-app=cilium are Running
#     (sample assertion; full cluster checks belong to `make smoke-test`).
#
# The static section currently FAILS on this tree (no rbac/, no
# cilium/install/, no rbac:/cilium: targets) — that is the intended
# red-phase detection behavior until the rbac/cilium manifests land.
#
# EXIT CODES:
#   0 — every applicable check PASSed (live section may be SKIPPED)
#   1 — at least one check FAILed

set -eu

# Resolve the repo root from the script location
# (scripts/verify/verify-cluster-ops.sh -> repo root).
repo_root=$(cd -- "$(dirname -- "$0")/../.." && pwd -P)

_checks=0
_failures=0

pass() {
    _checks=$((_checks + 1))
    printf 'PASS  %s\n' "$1"
}

fail() {
    _checks=$((_checks + 1))
    _failures=$((_failures + 1))
    printf 'FAIL  %s\n' "$1"
}

skip() {
    printf 'SKIP  %s\n' "$1"
}

# check_rbac_name — grep the rbac/ manifests for a role/binding name listed in
# ansible/playbooks/bootstrap.yml:159-259.
#   $1 — extended regex matched against the manifest files
#   $2 — human-readable label (name + source line reference)
check_rbac_name() {
    _pattern=$1
    _label=$2
    if grep -rEq "${_pattern}" "${repo_root}/rbac"; then
        pass "rbac/ references ${_label}"
    else
        fail "rbac/ missing ${_label}"
    fi
}

printf '%s\n' '--- STATIC ---'

# --- 1. rbac/ ClusterRole/ClusterRoleBinding manifests -------------
if [ -d "${repo_root}/rbac" ]; then
    pass 'rbac/ directory exists'

    if grep -rEq '^kind: ClusterRole$' "${repo_root}/rbac"; then
        pass 'rbac/ contains a ClusterRole kind'
    else
        fail 'rbac/ contains no ClusterRole kind'
    fi

    if grep -rEq '^kind: ClusterRoleBinding$' "${repo_root}/rbac"; then
        pass 'rbac/ contains a ClusterRoleBinding kind'
    else
        fail 'rbac/ contains no ClusterRoleBinding kind'
    fi

    _rbac_objects=$(grep -rE '^kind: (ClusterRole|ClusterRoleBinding)$' "${repo_root}/rbac" 2>/dev/null | wc -l | tr -d ' ' || true)
    if [ "${_rbac_objects:-0}" -ge 4 ]; then
        pass "rbac/ has >= 4 ClusterRole/ClusterRoleBinding objects (${_rbac_objects} found)"
    else
        fail "rbac/ has < 4 ClusterRole/ClusterRoleBinding objects (${_rbac_objects:-0} found)"
    fi

    check_rbac_name 'name: system:kubelet-bootstrap' \
        'bootstrap-token ClusterRole/Binding system:kubelet-bootstrap (bootstrap.yml:176,185)'
    check_rbac_name 'name: system:nodes' \
        'system:nodes ClusterRoleBinding (bootstrap.yml:198)'
    check_rbac_name 'name: system:node[[:space:]]*$' \
        'system:node ClusterRole/roleRef (bootstrap.yml:202)'
    check_rbac_name 'name: system:admin' \
        'system:admin ClusterRoleBinding (bootstrap.yml:218)'
    check_rbac_name 'name: cluster-admin' \
        'cluster-admin roleRef (bootstrap.yml:222)'
    check_rbac_name 'name: system:kube-apiserver-proxy' \
        'system:kube-apiserver-proxy ClusterRole/Binding (bootstrap.yml:238,247)'
else
    fail 'rbac/ directory missing — expected manifests for the rbac group (bootstrap-token, system:nodes, system:admin, system:kube-apiserver-proxy)'
fi

# --- 2. cilium/install/ pre-rendered manifests + version marker ----
if [ -d "${repo_root}/cilium/install" ]; then
    pass 'cilium/install/ directory exists'

    _cilium_workload_found=0
    for _f in "${repo_root}"/cilium/install/*; do
        [ -f "${_f}" ] || continue
        case "$(basename "${_f}")" in
            VERSION*) continue ;;
            *) : ;;
        esac
        if grep -qE 'name: cilium[[:space:]]*$' "${_f}" \
            && grep -qE '^kind: (ServiceAccount|Deployment|DaemonSet)$' "${_f}"; then
            _cilium_workload_found=1
            break
        fi
    done
    if [ "${_cilium_workload_found}" = 1 ]; then
        pass 'cilium/install/ has a manifest with a cilium ServiceAccount or Deployment/DaemonSet'
    else
        fail 'cilium/install/ has no manifest with a cilium ServiceAccount or Deployment/DaemonSet'
    fi

    if [ -f "${repo_root}/cilium/install/VERSION" ] \
        && [ -s "${repo_root}/cilium/install/VERSION" ] \
        && grep -qE '[0-9]+\.[0-9]+' "${repo_root}/cilium/install/VERSION"; then
        pass "cilium/install/VERSION pins a Cilium version ($(tr -d '\n' < "${repo_root}/cilium/install/VERSION" || true))"
    else
        fail 'cilium/install/VERSION missing, empty, or not a pinned version string'
    fi
else
    fail 'cilium/install/ directory missing — expected pre-rendered install manifests'
fi

# --- 3. Makefile targets -----------------------------------------------------
if [ -f "${repo_root}/Makefile" ]; then
    if grep -qE '^rbac:' "${repo_root}/Makefile"; then
        pass 'Makefile defines rbac: target'
    else
        fail 'Makefile missing rbac: target'
    fi

    if grep -qE '^cilium:' "${repo_root}/Makefile"; then
        pass 'Makefile defines cilium: target'
    else
        fail 'Makefile missing cilium: target'
    fi
else
    fail 'Makefile missing at repo root'
fi

# --- 4. `make help` lists the targets ---------------------------------------
if _help_output=$(make -C "${repo_root}" help 2>&1); then
    _esc=$(printf '\033')
    _help_clean=$(printf '%s\n' "${_help_output}" | sed "s/${_esc}\[[0-9;]*m//g")

    if printf '%s\n' "${_help_clean}" | grep -qE '^[[:space:]]*rbac([[:space:]]|$)'; then
        pass 'make help lists rbac target'
    else
        fail 'make help does not list rbac target'
    fi

    if printf '%s\n' "${_help_clean}" | grep -qE '^[[:space:]]*cilium([[:space:]]|$)'; then
        pass 'make help lists cilium target'
    else
        fail 'make help does not list cilium target'
    fi
else
    fail 'make help failed to run (exit non-zero)'
fi

printf '%s\n' '--- LIVE ---'

# --- LIVE prerequisites: SKIP cleanly when kubeconfig/kubectl unavailable ----
_kubeconfig_path="${repo_root}/kubeconfig"
_live_skip_reason=""
_live_status='RUN'

if [ ! -f "${_kubeconfig_path}" ]; then
    _live_skip_reason='no ./kubeconfig at repo root'
elif ! command -v kubectl >/dev/null 2>&1; then
    _live_skip_reason='kubectl not found in PATH'
fi

if [ -n "${_live_skip_reason}" ]; then
    _live_status="SKIPPED (${_live_skip_reason})"
    skip "live section: ${_live_skip_reason}"
    printf '%s\n' '      run this script after make rbac / make cilium on a healthy cluster'
else
    # after `make rbac`: the four bindings exist in `kubectl get clusterrolebinding`
    if _bindings=$(kubectl --kubeconfig="${_kubeconfig_path}" get clusterrolebinding 2>&1); then
        for _b in system:kubelet-bootstrap system:nodes system:admin system:kube-apiserver-proxy; do
            if printf '%s\n' "${_bindings}" | grep -qw "${_b}"; then
                pass "clusterrolebinding ${_b} exists (after make rbac)"
            else
                fail "clusterrolebinding ${_b} missing (run make rbac)"
            fi
        done
    else
        fail 'kubectl get clusterrolebinding failed — is the cluster reachable?'
    fi

    # after `make cilium`: kube-system cilium pods report Running (sample assertion)
    if _cilium_pods=$(kubectl --kubeconfig="${_kubeconfig_path}" -n kube-system get pods -l k8s-app=cilium 2>&1); then
        if printf '%s\n' "${_cilium_pods}" | grep -q 'Running'; then
            pass 'kube-system cilium pods are Running (after make cilium)'
        else
            fail 'no Running cilium pod in kube-system (run make cilium)'
        fi
    else
        fail 'kubectl get cilium pods failed — is the cluster reachable?'
    fi
fi

printf '%s\n' '--- SUMMARY ---'
if [ "${_failures}" -eq 0 ]; then
    printf 'RESULT: all %d checks passed (live: %s)\n' "${_checks}" "${_live_status}"
    exit 0
else
    printf 'RESULT: %d/%d checks passed, %d failed (live: %s)\n' \
        "$((_checks - _failures))" "${_checks}" "${_failures}" "${_live_status}"
    exit 1
fi
