#!/bin/sh
# shellcheck disable=SC2292  # POSIX sh per AGENTS.md: [ ] not [[ ]]; rc forces shell=bash
# ---------------------------------------------------------------------------
# verify-runtime-pki.sh — Gate checks for the tofu-rendered PKI output of the
# terraform/runtime root module.
#
# Checks (one PASS/FAIL/SKIP line each):
#   1. ca.pem exists and parses with `openssl x509 -subject` (CA self-signed).
#   2. kubernetes.pem (apiserver) exists and its SANs contain the cp_ip from
#      the tfvars fixture, 10.96.0.1 and 127.0.0.1.
#   3. kubernetes.pem and ca.pem survive `openssl x509 -checkend 31536000`.
#   4. admin.kubeconfig, controller-manager.kubeconfig, scheduler.kubeconfig
#      parse with `kubectl config view` and their server URL contains
#      https://<cp_ip>:6443.
#   5. encryption-config.yaml carries a non-empty base64 secret key.
#
# Exit code: 0 iff no check FAILs. Environment degradation (openssl or
# kubectl missing) turns the affected checks into SKIP, which does not fail
# the run; every other missing/corrupt artifact is a hard FAIL.
#
# Usage:
#   verify-runtime-pki.sh [PKI_DIR]
#
# Environment:
#   PKI_DIR      rendered PKI directory (default: <repo>/build/runtime/pki)
#   TFVARS_FILE  tfvars fixture providing cp_ip (default:
#                <repo>/terraform/runtime/test.tfvars)
#   CP_IP        explicit control-plane IP; overrides the fixture
#
# Notes:
#   - Deliberately no `set -e`: every check runs and reports independently.
#   - `set -u` is on to catch variable typos.
# ---------------------------------------------------------------------------

set -u

SCRIPT_DIR=$(CDPATH='' cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH='' cd "${SCRIPT_DIR}/../.." && pwd)

PKI_DIR="${PKI_DIR:-${REPO_ROOT}/build/runtime/pki}"
if [ "$#" -gt 0 ]; then
    PKI_DIR="$1"
fi
TFVARS_FILE="${TFVARS_FILE:-${REPO_ROOT}/terraform/runtime/test.tfvars}"

PASS=0
FAIL=0
SKIP=0

pass() { printf 'PASS: %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL: %s\n' "$*"; FAIL=$((FAIL + 1)); }
skip() { printf 'SKIP: %s\n' "$*"; SKIP=$((SKIP + 1)); }

OPENSSL=$(command -v openssl || true)
KUBECTL=$(command -v kubectl || true)

# --- edge case: output directory missing ----------------------------------
if [ ! -d "${PKI_DIR}" ]; then
    fail "PKI output directory does not exist: ${PKI_DIR}"
    printf 'RESULT: FAIL (PKI output directory missing)\n'
    exit 1
fi

# --- resolve cp_ip from the tfvars fixture (or CP_IP env) ------------------
cp_ip="${CP_IP:-}"
if [ -z "${cp_ip}" ]; then
    if [ -f "${TFVARS_FILE}" ]; then
        cp_ip=$(sed -n 's/^[[:space:]]*cp_ip[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${TFVARS_FILE}" | head -n 1 || true)
    fi
fi
if [ -z "${cp_ip}" ]; then
    fail "cp_ip could not be determined (expected in ${TFVARS_FILE} or via CP_IP env)"
fi

ca_pem="${PKI_DIR}/ca.pem"
kubernetes_pem="${PKI_DIR}/kubernetes.pem"

# --- check 1: CA exists and parses -----------------------------------------
if [ ! -f "${ca_pem}" ]; then
    fail "ca.pem missing: ${ca_pem}"
elif [ -z "${OPENSSL}" ]; then
    skip "openssl not found — skipping CA certificate check"
elif "${OPENSSL}" x509 -in "${ca_pem}" -noout -subject >/dev/null 2>&1; then
    pass "ca.pem exists and parses as X.509 (openssl x509 -subject)"
else
    fail "ca.pem exists but openssl x509 -subject fails: ${ca_pem}"
fi

# --- check 2: apiserver cert SANs contain cp_ip, 10.96.0.1, 127.0.0.1 ------
if [ ! -f "${kubernetes_pem}" ]; then
    fail "kubernetes.pem (apiserver cert) missing: ${kubernetes_pem}"
elif [ -z "${OPENSSL}" ]; then
    skip "openssl not found — skipping apiserver SAN check"
elif [ -z "${cp_ip}" ]; then
    fail "cannot check apiserver SANs: cp_ip not determined"
else
    san_text=$("${OPENSSL}" x509 -in "${kubernetes_pem}" -noout -ext subjectAltName 2>/dev/null | tail -n +2 | tr -d ' \t\r\n' || true)
    missing_sans=""
    for san_ip in "${cp_ip}" "10.96.0.1" "127.0.0.1"; do
        case "${san_text}" in
            *"IPAddress:${san_ip}"*|*"IP:${san_ip}"*) : ;;
            *) missing_sans="${missing_sans} ${san_ip}" ;;
        esac
    done
    if [ -z "${missing_sans}" ]; then
        pass "kubernetes.pem SANs contain ${cp_ip}, 10.96.0.1 and 127.0.0.1"
    else
        fail "kubernetes.pem SANs missing:${missing_sans} (${kubernetes_pem})"
    fi
fi

# --- check 3: cert validity (openssl x509 -checkend 31536000) --------------
if [ -z "${OPENSSL}" ]; then
    skip "openssl not found — skipping certificate validity check"
elif [ ! -f "${kubernetes_pem}" ] || [ ! -f "${ca_pem}" ]; then
    fail "cannot check certificate validity: kubernetes.pem or ca.pem missing"
elif "${OPENSSL}" x509 -in "${kubernetes_pem}" -checkend 31536000 -noout >/dev/null 2>&1 &&
     "${OPENSSL}" x509 -in "${ca_pem}" -checkend 31536000 -noout >/dev/null 2>&1; then
    pass "kubernetes.pem and ca.pem remain valid for >= 31536000 seconds (openssl x509 -checkend)"
else
    fail "certificate validity check failed (openssl x509 -checkend 31536000) for kubernetes.pem or ca.pem"
fi

# --- check 4: kubeconfigs parse and point at https://<cp_ip>:6443 ----------
if [ -z "${KUBECTL}" ]; then
    skip "kubectl not found — skipping kubeconfig parse/server URL check"
elif [ -z "${cp_ip}" ]; then
    fail "cannot check kubeconfigs: cp_ip not determined"
else
    kc_fail=0
    for kc_name in admin controller-manager scheduler; do
        kc="${PKI_DIR}/${kc_name}.kubeconfig"
        if [ ! -f "${kc}" ]; then
            fail "kubeconfig missing: ${kc}"
            kc_fail=1
            continue
        fi
        if ! "${KUBECTL}" config view --kubeconfig="${kc}" >/dev/null 2>&1; then
            fail "kubeconfig does not parse (kubectl config view): ${kc}"
            kc_fail=1
            continue
        fi
        server_url=$("${KUBECTL}" config view --kubeconfig="${kc}" -o jsonpath='{.clusters[*].cluster.server}' 2>/dev/null || true)
        case "${server_url}" in
            *"https://${cp_ip}:6443"*)
                : ;;
            *)
                fail "kubeconfig server URL mismatch: ${kc} (got '${server_url}', want https://${cp_ip}:6443)"
                kc_fail=1
                ;;
        esac
    done
    if [ "${kc_fail}" -eq 0 ]; then
        pass "admin/controller-manager/scheduler kubeconfigs parse and server URL contains https://${cp_ip}:6443"
    fi
fi

# --- check 5: encryption-config.yaml carries a non-empty base64 key --------
encryption_config="${PKI_DIR}/encryption-config.yaml"
if [ ! -f "${encryption_config}" ]; then
    fail "encryption-config.yaml missing: ${encryption_config}"
elif grep -Eq '^[[:space:]]*secret:[[:space:]]*"?[A-Za-z0-9+/]+={0,2}"?[[:space:]]*$' "${encryption_config}"; then
    pass "encryption-config.yaml contains a non-empty base64 encryption key"
else
    fail "encryption-config.yaml has no non-empty base64 secret field"
fi

# --- summary ---------------------------------------------------------------
if [ "${FAIL}" -eq 0 ]; then
    result=PASS
else
    result=FAIL
fi
printf 'RESULT: %s (%d pass, %d fail, %d skip)\n' "${result}" "${PASS}" "${FAIL}" "${SKIP}"
[ "${FAIL}" -eq 0 ]
