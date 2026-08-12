#!/bin/sh
# shellcheck disable=SC2292  # POSIX sh per AGENTS.md: [ ] not [[ ]]; rc forces shell=bash
# =============================================================================
# verify-runtime-confexts.sh — role-split runtime confext rendering checks
# (the rendered-image contract)
#
# Asserts the Section 3.4 rendered-image invariants of
# the node-provisioning spec against the tofu-generated
# role-split confext images under build/runtime/confexts/ (the module's
# confexts_output_dir, anchored via path.module to the repo-root build dir):
#
#   1. z-etcd.raw (cp1 only): contains etc/etcd/etcd.conf.yml with the real
#      cp_ip embedded, and etc/extension-release.d/extension-release.z-etcd
#      carrying ID=fedora and VERSION_ID=44 (RATIFIED 2026-08-10 E2E
#      replay: metadata must be named extension-release.<image-name>;
#      systemd 259 refuses the old z-etcd.confext naming).
#   2. z-kubernetes-cp.raw (cp1 only): contains etc/kubernetes/cp.env with
#      KUBE_ADVERTISE_ADDRESS and KUBE_ETCD_SERVERS bound to the real cp_ip,
#      etc/kubernetes/pki/ca.pem, admin/controller-manager/scheduler
#      kubeconfigs, etc/kubernetes/encryption-config.yaml, and the
#      etc/extension-release.d/extension-release.z-kubernetes-cp metadata.
#   3. z-kubelet-<node>.raw (one per node): contains etc/kubernetes/kubelet.conf,
#      etc/kubernetes/pki/ca.pem, a per-node kubelet cert/key pair, and
#      etc/extension-release.d/extension-release.z-kubelet-<node> metadata.
#   4. Every image contains no content outside etc/ (systemd-confext merges
#      only /etc; a stray usr/ or var/ subtree would silently do nothing).
#
# The per-node kubelet cert/key naming is not fixed by the spec ("per-node
# kubelet cert/key"); the check accepts either <node>.pem/<node>-key.pem
# or kubelet-<node>.pem/kubelet-<node>-key.pem under etc/kubernetes/pki/.
#
# Each check prints exactly one `PASS: <label>` / `FAIL: <label>` /
# `SKIP: <label>` line on stdout; per-check diagnostics go to stderr. The
# script exits 0 iff no check FAILs. SKIP (environmental degradation, e.g.
# unsquashfs missing) does not fail the run; every missing/corrupt artifact
# is a hard FAIL.
#
# USAGE:
#   ./scripts/verify/verify-runtime-confexts.sh [CONFEXTS_DIR]
#
# ENVIRONMENT:
#   CONFEXTS_DIR   rendered confext output directory (default:
#                  <repo>/build/runtime/confexts)
#   TFVARS_FILE    tfvars fixture providing cp_ip and the node_ips node set
#                  (default: <repo>/terraform/runtime/test.tfvars)
#   CP_IP          explicit control-plane IP; overrides the fixture
#
# NOTES:
#   - Read-only: never modifies the tree.
#   - Deliberately no `set -e`: every check runs and reports independently.
#   - `set -u` is on to catch variable typos.
# =============================================================================

set -u

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
REPO_ROOT=$(CDPATH='' cd -- "${SCRIPT_DIR}/../.." && pwd -P) || exit 1

CONFEXTS_DIR="${CONFEXTS_DIR:-${REPO_ROOT}/build/runtime/confexts}"
if [ "$#" -gt 0 ]; then
    CONFEXTS_DIR="$1"
fi
TFVARS_FILE="${TFVARS_FILE:-${REPO_ROOT}/terraform/runtime/test.tfvars}"

PASS=0
FAIL=0
SKIP=0

pass() { printf 'PASS: %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL: %s\n' "$*"; FAIL=$((FAIL + 1)); }
skip() { printf 'SKIP: %s\n' "$*"; SKIP=$((SKIP + 1)); }

UNSQUASHFS=$(command -v unsquashfs || true)

for _cmd in sed grep; do
    if ! command -v "${_cmd}" >/dev/null 2>&1; then
        printf 'ERROR: required tool not found: %s\n' "${_cmd}" >&2
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# image_has_path RAW PATH — exit 0 when PATH appears in `unsquashfs -l RAW`.
# Tolerates both `squashfs-root/`-prefixed listings (squashfs-tools default)
# and bare listings; the squashfs root line itself is ignored.
image_has_path() {
    _ihp_raw=$1
    _ihp_want=$2
    _ihp_list=$(unsquashfs -l "${_ihp_raw}" 2>/dev/null) || return 1
    _ihp_clean=$(printf '%s\n' "${_ihp_list}" | sed -e 's#^squashfs-root/##' -e 's#^\./##' -e 's#^squashfs-root$##')
    printf '%s\n' "${_ihp_clean}" | grep -Fx -e "${_ihp_want}" >/dev/null
}

# image_file_content RAW PATH — prints the content of PATH from RAW via
# `unsquashfs -cat`; empty on error (path missing or corrupt image).
image_file_content() {
    unsquashfs -cat "$1" "$2" 2>/dev/null || true
}

# contains_placeholder TEXT — exit 0 when TEXT still carries a template
# placeholder ({{, }} or CONTROL_PLANE_IP), i.e. the render did not substitute.
contains_placeholder() {
    case "$1" in
        *'{{'*|*'}}'*|*CONTROL_PLANE_IP*) return 0 ;;
        *) return 1 ;;
    esac
}

# check_confext_metadata RAW META_PATH — assert the release metadata file
# exists inside RAW and carries ID=fedora and VERSION_ID=44.
# Prints diagnostics to stderr; returns 1 on any failure.
check_confext_metadata() {
    _cm_raw=$1
    _cm_meta=$2
    if ! image_has_path "${_cm_raw}" "${_cm_meta}"; then
        printf '    missing release metadata: %s\n' "${_cm_meta}" >&2
        return 1
    fi
    _cm_content=$(image_file_content "${_cm_raw}" "${_cm_meta}")
    _cm_failed=0
    case "${_cm_content}" in
        *"ID=fedora"*) : ;;
        *) printf '    %s lacks ID=fedora\n' "${_cm_meta}" >&2; _cm_failed=1 ;;
    esac
    case "${_cm_content}" in
        *"VERSION_ID=44"*) : ;;
        *) printf '    %s lacks VERSION_ID=44\n' "${_cm_meta}" >&2; _cm_failed=1 ;;
    esac
    return "${_cm_failed}"
}

# ---------------------------------------------------------------------------
# Check 0 — output directory exists and holds at least one rendered image
# ---------------------------------------------------------------------------
if [ ! -d "${CONFEXTS_DIR}" ]; then
    fail "output directory does not exist: ${CONFEXTS_DIR} (run make configure first; confexts_output_dir must resolve here)"
    printf 'RESULT: FAIL (%d pass, %d fail, %d skip)\n' "${PASS}" "${FAIL}" "${SKIP}"
    exit 1
fi
_image_count=0
for _raw in "${CONFEXTS_DIR}"/*.raw; do
    if [ -e "${_raw}" ]; then
        _image_count=$((_image_count + 1))
    fi
done
if [ "${_image_count}" -eq 0 ]; then
    fail "no .raw confext images rendered in ${CONFEXTS_DIR} (run make configure first)"
    printf 'RESULT: FAIL (%d pass, %d fail, %d skip)\n' "${PASS}" "${FAIL}" "${SKIP}"
    exit 1
fi
pass "output directory exists with ${_image_count} rendered confext image(s): ${CONFEXTS_DIR}"

# --- resolve cp_ip from the tfvars fixture (or CP_IP env) ------------------
cp_ip="${CP_IP:-}"
if [ -z "${cp_ip}" ]; then
    if [ -f "${TFVARS_FILE}" ]; then
        cp_ip=$(sed -n 's/^[[:space:]]*cp_ip[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${TFVARS_FILE}" | head -n 1 || true)
        cp_ip=$(printf '%s' "${cp_ip}" | tr -d '\r')
    fi
fi
if [ -z "${cp_ip}" ]; then
    fail "cp_ip could not be determined (expected in ${TFVARS_FILE} or via CP_IP env)"
fi

# --- resolve the expected node set from the tfvars node_ips map ------------
# The checked node set is derived, never hardcoded. The expected nodes come
# from the runtime tfvars' node_ips map (a glob over the images on disk cannot
# detect that a node's image is absent, so the tfvars map is the completeness
# source); the on-disk z-kubelet-*.raw glob then supplies any additional
# images (e.g. a node rendered after this tfvars snapshot) for content checks.
expected_nodes=""
if [ -f "${TFVARS_FILE}" ]; then
    _node_ips_block=$(sed -n '/^[[:space:]]*node_ips[[:space:]]*=[[:space:]]*{/,/^[[:space:]]*}/p' "${TFVARS_FILE}" || true)
    # shellcheck disable=SC2086  # intentional: a name per line becomes words
    expected_nodes=$(printf '%s\n' "${_node_ips_block}" | sed -n 's/^[[:space:]]*\([A-Za-z0-9._-][A-Za-z0-9._-]*\)[[:space:]]*=[[:space:]]*"[^"]*".*/\1/p' | tr '\n' ' ' || true)
else
    fail "node_ips could not be determined (expected ${TFVARS_FILE} with a node_ips = { ... } map)"
fi
if [ -z "${expected_nodes}" ] && [ -f "${TFVARS_FILE}" ]; then
    fail "node_ips map not found in ${TFVARS_FILE} (expected a node_ips = { ... } block)"
fi

# ---------------------------------------------------------------------------
# Check 1 — z-etcd.raw: etcd.conf.yml with the real cp_ip + release metadata
# ---------------------------------------------------------------------------
etcd_raw="${CONFEXTS_DIR}/z-etcd.raw"
if [ ! -f "${etcd_raw}" ]; then
    fail "check-1 z-etcd.raw missing: ${etcd_raw}"
elif [ -z "${UNSQUASHFS}" ]; then
    skip "check-1 z-etcd.raw content check (unsquashfs not found)"
else
    check1_failed=0
    if image_has_path "${etcd_raw}" "etc/etcd/etcd.conf.yml"; then
        if [ -z "${cp_ip}" ]; then
            printf '    cannot verify etcd.conf.yml values: cp_ip not determined\n' >&2
            check1_failed=1
        else
            etcd_content=$(image_file_content "${etcd_raw}" "etc/etcd/etcd.conf.yml")
            if contains_placeholder "${etcd_content}"; then
                printf '    etcd.conf.yml still contains a template placeholder\n' >&2
                check1_failed=1
            fi
            case "${etcd_content}" in
                *"${cp_ip}"*) : ;;
                *) printf '    etcd.conf.yml does not contain the real cp_ip %s\n' "${cp_ip}" >&2; check1_failed=1 ;;
            esac
        fi
    else
        printf '    missing etc/etcd/etcd.conf.yml\n' >&2
        check1_failed=1
    fi
    check_confext_metadata "${etcd_raw}" "etc/extension-release.d/extension-release.z-etcd" || check1_failed=1
    if [ "${check1_failed}" -eq 0 ]; then
        pass "check-1 z-etcd.raw: etc/etcd/etcd.conf.yml embeds cp_ip ${cp_ip}; etc/extension-release.d/extension-release.z-etcd has ID=fedora VERSION_ID=44"
    else
        fail "check-1 z-etcd.raw content + release metadata"
    fi
fi

# ---------------------------------------------------------------------------
# Check 2 — z-kubernetes-cp.raw: cp.env + CA + kubeconfigs + encryption
# ---------------------------------------------------------------------------
cp_raw="${CONFEXTS_DIR}/z-kubernetes-cp.raw"
if [ ! -f "${cp_raw}" ]; then
    fail "check-2 z-kubernetes-cp.raw missing: ${cp_raw}"
elif [ -z "${UNSQUASHFS}" ]; then
    skip "check-2 z-kubernetes-cp.raw content check (unsquashfs not found)"
else
    check2_failed=0
    if image_has_path "${cp_raw}" "etc/kubernetes/cp.env"; then
        if [ -z "${cp_ip}" ]; then
            printf '    cannot verify cp.env values: cp_ip not determined\n' >&2
            check2_failed=1
        else
            cp_env=$(image_file_content "${cp_raw}" "etc/kubernetes/cp.env")
            if contains_placeholder "${cp_env}"; then
                printf '    cp.env still contains a template placeholder\n' >&2
                check2_failed=1
            fi
            case "${cp_env}" in
                *"KUBE_ADVERTISE_ADDRESS=${cp_ip}"*) : ;;
                *) printf '    cp.env lacks KUBE_ADVERTISE_ADDRESS=%s\n' "${cp_ip}" >&2; check2_failed=1 ;;
            esac
            etcd_servers=$(printf '%s\n' "${cp_env}" | grep -E '^[[:space:]]*KUBE_ETCD_SERVERS=' | head -n 1 || true)
            if [ -z "${etcd_servers}" ]; then
                printf '    cp.env lacks a KUBE_ETCD_SERVERS line\n' >&2
                check2_failed=1
            else
                case "${etcd_servers}" in
                    *"${cp_ip}"*) : ;;
                    *) printf '    KUBE_ETCD_SERVERS does not contain the real cp_ip %s: %s\n' "${cp_ip}" "${etcd_servers}" >&2; check2_failed=1 ;;
                esac
            fi
        fi
    else
        printf '    missing etc/kubernetes/cp.env\n' >&2
        check2_failed=1
    fi
    if image_has_path "${cp_raw}" "etc/kubernetes/pki/ca.pem"; then
        :
    else
        printf '    missing etc/kubernetes/pki/ca.pem\n' >&2
        check2_failed=1
    fi
    for _kc in admin controller-manager scheduler; do
        if image_has_path "${cp_raw}" "etc/kubernetes/${_kc}.kubeconfig"; then
            :
        else
            printf '    missing etc/kubernetes/%s.kubeconfig\n' "${_kc}" >&2
            check2_failed=1
        fi
    done
    if image_has_path "${cp_raw}" "etc/kubernetes/encryption-config.yaml"; then
        :
    else
        printf '    missing etc/kubernetes/encryption-config.yaml\n' >&2
        check2_failed=1
    fi
    check_confext_metadata "${cp_raw}" "etc/extension-release.d/extension-release.z-kubernetes-cp" || check2_failed=1
    if [ "${check2_failed}" -eq 0 ]; then
        pass "check-2 z-kubernetes-cp.raw: cp.env (KUBE_ADVERTISE_ADDRESS/KUBE_ETCD_SERVERS=${cp_ip}), pki/ca.pem, admin/controller-manager/scheduler kubeconfigs, encryption-config.yaml, release metadata"
    else
        fail "check-2 z-kubernetes-cp.raw content + release metadata"
    fi
fi

# ---------------------------------------------------------------------------
# Check 3 — z-kubelet-<node>.raw (one per node): kubelet.conf + CA + cert/key
# ---------------------------------------------------------------------------
# Derive the checked node set: every node expected by the tfvars node_ips map
# (missing image = hard FAIL naming the node) plus every z-kubelet-*.raw image
# on disk (extra images are content-checked, never rejected). Each node is
# checked exactly once.
_checked=""
for _node in ${expected_nodes}; do
    # shellcheck disable=SC2086  # intentional: space-separated node list
    kubelet_raw="${CONFEXTS_DIR}/z-kubelet-${_node}.raw"
    if [ ! -f "${kubelet_raw}" ]; then
        fail "check-3 z-kubelet-${_node}.raw missing: ${kubelet_raw}"
        continue
    fi
    _checked="${_checked} ${_node}"
done
for _raw in "${CONFEXTS_DIR}"/z-kubelet-*.raw; do
    [ -e "${_raw}" ] || continue
    _node=$(basename "${_raw}" .raw)
    _node=${_node#z-kubelet-}
    case " ${_checked} " in
        *" ${_node} "*) : ;;
        *) _checked="${_checked} ${_node}" ;;
    esac
done
for _node in ${_checked}; do
    kubelet_raw="${CONFEXTS_DIR}/z-kubelet-${_node}.raw"
    if [ -z "${UNSQUASHFS}" ]; then
        skip "check-3 z-kubelet-${_node}.raw content check (unsquashfs not found)"
        continue
    fi
    check3_failed=0
    if image_has_path "${kubelet_raw}" "etc/kubernetes/kubelet.conf"; then
        kubelet_conf=$(image_file_content "${kubelet_raw}" "etc/kubernetes/kubelet.conf")
        if contains_placeholder "${kubelet_conf}"; then
            printf '    kubelet.conf still contains a template placeholder\n' >&2
            check3_failed=1
        fi
    else
        printf '    missing etc/kubernetes/kubelet.conf\n' >&2
        check3_failed=1
    fi
    if image_has_path "${kubelet_raw}" "etc/kubernetes/pki/ca.pem"; then
        :
    else
        printf '    missing etc/kubernetes/pki/ca.pem\n' >&2
        check3_failed=1
    fi
    # per-node kubelet cert/key pair: accept <node> or kubelet-<node> naming
    cert_found=0
    key_found=0
    for _pair in "${_node}.pem|${_node}-key.pem" "kubelet-${_node}.pem|kubelet-${_node}-key.pem"; do
        _cert=${_pair%%|*}
        _key=${_pair##*|}
        if image_has_path "${kubelet_raw}" "etc/kubernetes/pki/${_cert}"; then
            cert_found=1
        fi
        if image_has_path "${kubelet_raw}" "etc/kubernetes/pki/${_key}"; then
            key_found=1
        fi
        if [ "${cert_found}" -eq 1 ] && [ "${key_found}" -eq 1 ]; then
            break
        fi
    done
    if [ "${cert_found}" -eq 1 ] && [ "${key_found}" -eq 1 ]; then
        :
    else
        printf '    missing per-node kubelet cert/key pair for %s under etc/kubernetes/pki/\n' "${_node}" >&2
        check3_failed=1
    fi
    check_confext_metadata "${kubelet_raw}" "etc/extension-release.d/extension-release.z-kubelet-${_node}" || check3_failed=1
    if [ "${check3_failed}" -eq 0 ]; then
        pass "check-3 z-kubelet-${_node}.raw: kubelet.conf, pki/ca.pem, per-node kubelet cert/key, release metadata"
    else
        fail "check-3 z-kubelet-${_node}.raw content + release metadata"
    fi
done

# ---------------------------------------------------------------------------
# Check 4 — no content outside etc/ in any rendered image
# ---------------------------------------------------------------------------
if [ -z "${UNSQUASHFS}" ]; then
    skip "check-4 content-hygiene (unsquashfs not found)"
else
    for _raw in "${CONFEXTS_DIR}"/*.raw; do
        [ -e "${_raw}" ] || continue
        _image_name=$(basename "${_raw}")
        _list=$(unsquashfs -l "${_raw}" 2>/dev/null)
        if [ -z "${_list}" ]; then
            fail "check-4 ${_image_name}: cannot list image (unsquashfs -l failed — corrupt image?)"
            continue
        fi
        _clean=$(printf '%s\n' "${_list}" | sed -e 's#^squashfs-root/##' -e 's#^\./##' -e 's#^squashfs-root$##')
        _violations=$(printf '%s\n' "${_clean}" | grep -v -E '^(etc(/.*)?)?$' || true)
        if [ -n "${_violations}" ]; then
            printf '    content outside etc/:\n' >&2
            printf '%s\n' "${_violations}" | sed 's/^/      /' >&2
            fail "check-4 ${_image_name}: image contains content outside etc/"
        else
            pass "check-4 ${_image_name}: all content under etc/ (confext mergeable)"
        fi
    done
fi

# ---------------------------------------------------------------------------
# Summary — exit 0 iff no check FAILed
# ---------------------------------------------------------------------------
if [ "${FAIL}" -eq 0 ]; then
    result=PASS
else
    result=FAIL
fi
printf 'RESULT: %s (%d pass, %d fail, %d skip)\n' "${result}" "${PASS}" "${FAIL}" "${SKIP}"
[ "${FAIL}" -eq 0 ]
