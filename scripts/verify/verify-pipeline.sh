#!/bin/sh
# shellcheck shell=sh
#
# verify-pipeline.sh — pipeline/validate/destroy acceptance checks.
#
# Asserts the Makefile's pipeline surface matches the node-provisioning spec
# (the pipeline contract):
#   1. `make help` lists the new target set (deploy, configure, cluster,
#      rbac, cilium, coredns, metrics-server, smoke-test, kubeconfig,
#      validate, destroy-full).
#   2. `make help` no longer lists the removed Ansible targets (container,
#      deploy-extensions, certs, bootstrap, inventory).
#   3. `make validate` covers BOTH tofu root modules (terraform/ and
#      terraform/runtime/) plus `packer validate` in packer/.
#   4. `make destroy-full` cleans terraform/runtime state, build/runtime/,
#      kubeconfig, and certs/.
#   5. `make cluster` chains configure (or a direct terraform/runtime apply),
#      kubeconfig, rbac, cilium, coredns.
#   6. The Makefile no longer references ANSIBLE_ vars or ansible-playbook.
#
# Read-only: inspects the Makefile and `make help` output; touches nothing.
# Exits 0 only when every check PASSes.

set -u

# --- Resolve repo root from the script location (invocation from any cwd) ---
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || exit 1
REPO_ROOT=$(CDPATH='' cd -- "${SCRIPT_DIR}/../.." && pwd) || exit 1
MAKEFILE="${REPO_ROOT}/Makefile"

failures=0

check_pass() {
    label=$1
    printf 'PASS: %s\n' "${label}"
}

check_fail() {
    label=$1
    detail=$2
    # Trim padding spaces around accumulated error lists for a tidy line.
    detail=$(printf '%s\n' "${detail}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    printf 'FAIL: %s - %s\n' "${label}" "${detail}"
    failures=$((failures + 1))
}

# --- Extract a target's recipe body from the Makefile ---
# Prints the `target:` line plus its recipe lines (leading tabs) until the
# next top-level (column-0) line. POSIX awk, no bashisms.
makefile_target_body() {
    _body_makefile=$1
    _body_target=$2
    awk -v tgt="${_body_target}" '
        BEGIN { in_tgt = 0 }
        in_tgt && /^[^[:space:]]/ { exit }
        in_tgt { print }
        $0 ~ "^" tgt ":" { in_tgt = 1; print }
    ' "${_body_makefile}"
}

if [ ! -f "${MAKEFILE}" ]; then
    printf 'FAIL: Makefile not found at %s\n' "${MAKEFILE}"
    printf 'verify-pipeline: cannot verify pipeline without a Makefile\n'
    exit 1
fi

# --- Authoritative target list from `make help` (called exactly once) ---
# Fast pre-check / fallback: parse documented targets (`##`) straight from
# the Makefile so a slow or unavailable `make` never blocks the checks.
ESC=$(printf '\033')
precheck_targets=$(grep -E '^[a-zA-Z_/.-]+:.*?## ' "${MAKEFILE}" \
    | sed 's/:.*##.*//' \
    | tr -d ' ' \
    | sort -u \
    | tr '\n' ' ')

help_targets=""
if command -v make >/dev/null 2>&1; then
    help_output=$(cd "${REPO_ROOT}" && make help 2>&1)
    help_status=$?
    if [ "${help_status}" -eq 0 ] && [ -n "${help_output}" ]; then
        help_targets=$(printf '%s\n' "${help_output}" \
            | sed "s/${ESC}\[[0-9;]*m//g" \
            | awk '{print $1}' \
            | grep -E '^[a-zA-Z_/.-]+$' \
            | sort -u \
            | tr '\n' ' ')
    else
        printf 'WARNING: make help exited %s; using Makefile grep as the target list\n' "${help_status}"
        help_targets=${precheck_targets}
    fi
else
    printf 'WARNING: make not found on PATH; using Makefile grep as the target list\n'
    help_targets=${precheck_targets}
fi

# --- Check 1: make help lists the required targets ---
required_targets="deploy configure cluster rbac cilium coredns metrics-server smoke-test kubeconfig validate destroy-full"
missing_targets=""
# shellcheck disable=SC2086  # intentional word splitting on the static target list
for t in ${required_targets}; do
    case " ${help_targets} " in
        *" ${t} "*) ;;
        *) missing_targets="${missing_targets} ${t}" ;;
    esac
done
if [ -n "${missing_targets}" ]; then
    check_fail "make help lists required targets" "missing:${missing_targets}"
else
    check_pass "make help lists required targets"
fi

# --- Check 2: make help does not list the removed Ansible targets ---
forbidden_targets="container deploy-extensions certs bootstrap inventory"
listed_forbidden=""
# shellcheck disable=SC2086  # intentional word splitting on the static target list
for t in ${forbidden_targets}; do
    case " ${help_targets} " in
        *" ${t} "*) listed_forbidden="${listed_forbidden} ${t}" ;;
        *) ;;
    esac
done
if [ -n "${listed_forbidden}" ]; then
    check_fail "make help omits removed targets" "still listed:${listed_forbidden}"
else
    check_pass "make help omits removed targets"
fi

# --- Check 3: make validate covers both root modules and packer ---
validate_body=$(makefile_target_body "${MAKEFILE}" validate)
validate_tf_body=$(makefile_target_body "${MAKEFILE}" validate-terraform)
validate_pk_body=$(makefile_target_body "${MAKEFILE}" validate-packer)
combined_validate="${validate_body}
${validate_tf_body}"

validate_errors=""
case "${combined_validate}" in
    *"tofu -chdir=terraform validate"*) ;;
    *) validate_errors="${validate_errors} no 'tofu -chdir=terraform validate'" ;;
esac
case "${combined_validate}" in
    *"tofu -chdir=terraform/runtime validate"*) ;;
    *) validate_errors="${validate_errors} no 'tofu -chdir=terraform/runtime validate'" ;;
esac
case "${validate_pk_body}" in
    *"packer validate"*)
        case "${validate_pk_body}" in
            *"cd packer"*|*"packer/"*|*"-chdir=packer"*) ;;
            *) validate_errors="${validate_errors} 'packer validate' not scoped to packer/" ;;
        esac
        ;;
    *) validate_errors="${validate_errors} no 'packer validate'" ;;
esac
if [ -n "${validate_errors}" ]; then
    check_fail "make validate covers both root modules and packer" "${validate_errors}"
else
    check_pass "make validate covers both root modules and packer"
fi

# --- Check 4: make destroy-full cleans runtime state, build/runtime, kubeconfig, certs ---
destroy_body=$(makefile_target_body "${MAKEFILE}" destroy-full)
destroy_errors=""
case "${destroy_body}" in
    *"terraform/runtime/terraform.tfstate"*) ;;
    *) destroy_errors="${destroy_errors} no terraform/runtime state cleanup" ;;
esac
case "${destroy_body}" in
    *"build/runtime"*) ;;
    *) destroy_errors="${destroy_errors} no build/runtime cleanup" ;;
esac
case "${destroy_body}" in
    *"kubeconfig"*) ;;
    *) destroy_errors="${destroy_errors} no kubeconfig cleanup" ;;
esac
case "${destroy_body}" in
    *"certs"*) ;;
    *) destroy_errors="${destroy_errors} no certs cleanup" ;;
esac
if [ -n "${destroy_errors}" ]; then
    check_fail "make destroy-full cleans runtime state, build/runtime, kubeconfig, certs" "${destroy_errors}"
else
    check_pass "make destroy-full cleans runtime state, build/runtime, kubeconfig, certs"
fi

# --- Check 5: make cluster chains configure, kubeconfig, rbac, cilium, coredns ---
cluster_body=$(makefile_target_body "${MAKEFILE}" cluster)
cluster_errors=""
case "${cluster_body}" in
    *"configure"*|*"terraform/runtime apply"*) ;;
    *) cluster_errors="${cluster_errors} no 'configure' or 'tofu -chdir=terraform/runtime apply'" ;;
esac
case "${cluster_body}" in
    *"kubeconfig"*) ;;
    *) cluster_errors="${cluster_errors} no 'kubeconfig'" ;;
esac
case "${cluster_body}" in
    *"rbac"*) ;;
    *) cluster_errors="${cluster_errors} no 'rbac'" ;;
esac
case "${cluster_body}" in
    *"cilium"*) ;;
    *) cluster_errors="${cluster_errors} no 'cilium'" ;;
esac
case "${cluster_body}" in
    *"coredns"*) ;;
    *) cluster_errors="${cluster_errors} no 'coredns'" ;;
esac
if [ -n "${cluster_errors}" ]; then
    check_fail "make cluster chains configure, kubeconfig, rbac, cilium, coredns" "${cluster_errors}"
else
    check_pass "make cluster chains configure, kubeconfig, rbac, cilium, coredns"
fi

# --- Check 6: Makefile has no ANSIBLE_ vars or ansible-playbook ---
if grep -n -e 'ANSIBLE_' -e 'ansible-playbook' "${MAKEFILE}" >/dev/null 2>&1; then
    ansible_lines=$(grep -n -e 'ANSIBLE_' -e 'ansible-playbook' "${MAKEFILE}" | cut -d: -f1 | tr '\n' ' ')
    check_fail "Makefile has no ANSIBLE_ vars or ansible-playbook" "matched at lines:${ansible_lines}"
else
    check_pass "Makefile has no ANSIBLE_ vars or ansible-playbook"
fi

# --- Summary ---
if [ "${failures}" -eq 0 ]; then
    printf 'verify-pipeline: all checks passed\n'
    exit 0
fi
printf 'verify-pipeline: %s check(s) failed\n' "${failures}"
exit 1
