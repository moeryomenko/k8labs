#!/bin/sh
# shellcheck shell=sh
#
# verify-no-ansible.sh — ansible-absence acceptance checks.
#
# Asserts the Ansible hard cutover of the node-provisioning spec
# (the hard-cutover contract):
#   1. ansible/ directory does not exist.
#   2. container/ directory does not exist.
#   3. ansible.cfg does not exist at the repo root.
#   4. mise.toml contains no ansible/ansible-playbook alias.
#   5. Makefile has no ANSIBLE_ variable definitions, no ansible-playbook
#      invocation, and no container/deploy-extensions/certs/bootstrap/
#      inventory targets.
#   6. `grep -ri ansible Makefile mise.toml` returns nothing.
#
# Read-only: inspects the working tree; touches nothing.
# Exits 0 only when every check PASSes.
#
# Usage: verify-no-ansible.sh [REPO_ROOT]
#   REPO_ROOT defaults to the repo root derived from the script location.
#   Passing an explicit path (e.g. a fixture tree under /tmp) is used to
#   prove red-phase detection without altering the real repository.

set -eu

# --- Resolve repo root: explicit arg wins, else derived from script path ---
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || exit 1
DERIVED_ROOT=$(CDPATH='' cd -- "${SCRIPT_DIR}/../.." && pwd) || exit 1
REPO_ROOT="${1:-${DERIVED_ROOT}}"

MAKEFILE="${REPO_ROOT}/Makefile"
MISE="${REPO_ROOT}/mise.toml"

failures=0

pass() {
    printf 'PASS: %s\n' "$1"
}

fail() {
    printf 'FAIL: %s - %s\n' "$1" "$2"
    failures=$((failures + 1))
}

# --- Check 1: ansible/ directory does not exist ---
if [ ! -d "${REPO_ROOT}/ansible" ]; then
    pass "ansible/ directory does not exist"
else
    fail "ansible/ directory does not exist" "found ${REPO_ROOT}/ansible"
fi

# --- Check 2: container/ directory does not exist ---
if [ ! -d "${REPO_ROOT}/container" ]; then
    pass "container/ directory does not exist"
else
    fail "container/ directory does not exist" "found ${REPO_ROOT}/container"
fi

# --- Check 3: ansible.cfg does not exist at the repo root ---
if [ ! -e "${REPO_ROOT}/ansible.cfg" ]; then
    pass "ansible.cfg does not exist at repo root"
else
    fail "ansible.cfg does not exist at repo root" "found ${REPO_ROOT}/ansible.cfg"
fi

# --- Check 4: mise.toml has no ansible/ansible-playbook alias ---
# ansible-playbook contains "ansible", so one case-insensitive grep covers
# both alias forms. Missing mise.toml -> no aliases -> PASS.
mise_lines=$(grep -in 'ansible' "${MISE}" 2>/dev/null | cut -d: -f1 | tr '\n' ' ' || true)
if [ -n "${mise_lines}" ]; then
    fail "mise.toml has no ansible/ansible-playbook alias" "matched at lines: ${mise_lines}"
else
    pass "mise.toml has no ansible/ansible-playbook alias"
fi

# --- Check 5: Makefile has no ANSIBLE_/ansible-playbook/removed targets ---
# Missing Makefile -> no ANSIBLE_ vars, no invocations, no targets -> PASS
# (absence is the goal).
makefile_problems=""

ansi_var_lines=$(grep -n 'ANSIBLE_' "${MAKEFILE}" 2>/dev/null | cut -d: -f1 | tr '\n' ' ' || true)
if [ -n "${ansi_var_lines}" ]; then
    makefile_problems="${makefile_problems} ANSIBLE_ at lines ${ansi_var_lines};"
fi

apb_lines=$(grep -n 'ansible-playbook' "${MAKEFILE}" 2>/dev/null | cut -d: -f1 | tr '\n' ' ' || true)
if [ -n "${apb_lines}" ]; then
    makefile_problems="${makefile_problems} ansible-playbook at lines ${apb_lines};"
fi

for target in container deploy-extensions certs bootstrap inventory; do
    tgt_lines=$(grep -n "^${target}:" "${MAKEFILE}" 2>/dev/null | cut -d: -f1 | tr '\n' ' ' || true)
    if [ -n "${tgt_lines}" ]; then
        makefile_problems="${makefile_problems} target ${target}: at lines ${tgt_lines};"
    fi
done

if [ -n "${makefile_problems}" ]; then
    fail "Makefile has no ANSIBLE_/ansible-playbook/removed targets" "${makefile_problems}"
else
    pass "Makefile has no ANSIBLE_/ansible-playbook/removed targets"
fi

# --- Check 6: grep -ri ansible Makefile mise.toml returns nothing ---
# Pipeline exit status is grep -q's; the output check (not grep's exit code)
# makes a missing file "return nothing" -> PASS, per the absence-is-the-goal
# edge case.
if grep -ri 'ansible' "${MAKEFILE}" "${MISE}" 2>/dev/null | grep -q .; then
    fail "grep -ri ansible Makefile mise.toml returns nothing" "matches found"
else
    pass "grep -ri ansible Makefile mise.toml returns nothing"
fi

# --- Summary ---
if [ "${failures}" -eq 0 ]; then
    printf 'verify-no-ansible: all checks passed\n'
    exit 0
fi
printf 'verify-no-ansible: %s check(s) failed\n' "${failures}"
exit 1
