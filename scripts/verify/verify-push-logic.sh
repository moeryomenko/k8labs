#!/bin/sh
# shellcheck disable=SC2292  # POSIX sh per AGENTS.md: [ ] not [[ ]]; rc forces shell=bash
# shellcheck disable=SC2310,SC2312 # test functions run in if/! conditions and
#                                  # command substitutions intentionally mask
#                                  # rc; the string/regex tests are the checks.
# =============================================================================
# verify-push-logic.sh — unit-style tests for the phase-B push script
# exercising the phase-B push and refresh contract.
#
# Tests terraform/runtime/push-confext.sh against
# mocked `scp`/`ssh` stubs so no live node is required. The stubs record every
# invocation to a log and simulate a fake node's /var/lib/confexts directory
# (FAKE_NODE_DIR), so the tests can assert exactly what the push script did.
#
# CONTRACT UNDER TEST — terraform/runtime/push-confext.sh must implement:
#
#   Usage: push-confext.sh <user@host|localhost> <raw-file> [<raw-file> ...]
#
#   For the node:
#     1. For each <raw-file> (local, must exist): the remote path is always
#        /var/lib/confexts/<basename> (image name = basename without .raw).
#        Obtain the remote sha256 with exactly:
#            ssh <user@host> sha256sum /var/lib/confexts/<name>.raw
#        (or `ssh <user@host> cat /var/lib/confexts/<name>.raw` piped through
#        local sha256sum); the probe exits 1 / prints nothing when the remote
#        file does not exist yet. The probe is bounded-retried on transient
#        ssh failure (PROBE_RETRY_ATTEMPTS x PROBE_RETRY_SLEEP); only after
#        exhaustion does the script treat the node as unreachable.
#        Identical hash -> skip the scp for that image.
#        Different hash -> scp <raw-file> <user@host>:/var/lib/confexts/<name>.raw
#     2. If at least one image changed:
#        - ssh <user@host> systemd-confext refresh            (exact command)
#        - ssh <user@host> systemctl daemon-reload            (exact command)
#        - start/restart in dependency order with health gates, each as its
#          OWN ssh invocation so the sequence is observable:
#            control-plane (role derived from the image set: presence of
#            z-etcd.raw, matching outputs.tf node_confexts):
#              systemctl start crio.service
#              systemctl start etcd.service
#              ssh <user@host> etcdctl endpoint health         (wait, exit 0)
#              systemctl start kube-apiserver.service
#              ssh <user@host> curl -k -sf https://<cp_ip>:6443/healthz (wait)
#              systemctl start kube-controller-manager.service
#              systemctl start kube-scheduler.service
#              systemctl start kubelet.service
#            worker:
#              systemctl start crio.service
#              systemctl start kubelet.service
#        No enablement is performed by the push step: the units are enabled
#        by the confext merge itself — package-confext.sh ships each role
#        image's enablement symlinks inside
#        etc/systemd/system/multi-user.target.wants/, so the systemd-confext
#        refresh activates them. The push step must
#        NOT write into /etc at all (no `ln -sf` into /etc/systemd/, no
#        `systemctl enable`) — merged /etc is read-only and enable-before-
#        refresh deterministically fails EROFS.
#     3. If NO image changed: no scp, no refresh, no daemon-reload, no
#        start/restart — only the sha256 probe ssh.
#     4. Errors: missing local .raw, unparseable host arg (no '@' and not
#        'localhost'), or missing scp/ssh in PATH -> exit non-zero with a
#        clear message naming the problem.
#
# The stubs understand the exact remote commands listed above; anything else
# is logged and answered with exit 0. Probe commands are served from the fake
# node dir, so a pre-staged identical file makes the push script observe an
# equal hash and skip.
#
# USAGE:
#   ./scripts/verify/verify-push-logic.sh [PUSH_SCRIPT_PATH]
#   PUSH_SCRIPT env overrides the default <repo>/terraform/runtime/push-confext.sh
#
# Exit semantics:
#   * push script absent -> FAIL, exit 1 (proves detection before implementation)
#   * any test FAIL -> exit 1
#   * all tests PASS (SKIPs ignored) -> exit 0
#
# NOTES:
#   - Read-only for the repo: all fixtures live in a mktemp dir, removed on exit.
#   - Deliberately no `set -e`: every test runs and reports independently.
#   - `set -u` is on to catch variable typos.
# =============================================================================

set -u

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
REPO_ROOT=$(CDPATH='' cd -- "${SCRIPT_DIR}/../.." && pwd -P) || exit 1

PUSH_SCRIPT="${PUSH_SCRIPT:-${REPO_ROOT}/terraform/runtime/push-confext.sh}"
if [ "$#" -gt 0 ]; then
    PUSH_SCRIPT="$1"
fi

# Red-phase detection: a missing push script -> hard FAIL.
if [ ! -f "${PUSH_SCRIPT}" ] || [ ! -r "${PUSH_SCRIPT}" ]; then
    printf 'FAIL: push script missing: %s (terraform/runtime/push-confext.sh must exist to satisfy the phase-B push contract)\n' "${PUSH_SCRIPT}"
    printf 'RESULT: FAIL (0 pass, 1 fail, 0 skip)\n'
    exit 1
fi

for _tool in mktemp grep sed cut head tr sha256sum cmp cp mkdir chmod ln dirname basename; do
    if ! command -v "${_tool}" >/dev/null 2>&1; then
        printf 'ERROR: required tool not found: %s\n' "${_tool}" >&2
        exit 1
    fi
done

ORIG_PATH=${PATH}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/verify-push-logic.XXXXXX") || exit 1
cleanup() {
    rm -rf "${WORK}"
}
trap cleanup 0 INT TERM HUP

MOCK_BIN="${WORK}/bin"
FAKE_NODE="${WORK}/node"
TOOLBOX="${WORK}/toolbox"
LOCAL_DIR="${WORK}/local"
SCP_LOG="${WORK}/log/scp.log"
SSH_LOG="${WORK}/log/ssh.log"
PROBE_FAIL_FILE="${WORK}/log/probe-fail.count"
mkdir -p "${MOCK_BIN}" "${FAKE_NODE}/var/lib/confexts" "${TOOLBOX}" "${LOCAL_DIR}" "${WORK}/log"

# ---------------------------------------------------------------------------
# Mock scp/ssh stubs. Generated at runtime; the push script finds them first
# on PATH. They record every invocation and simulate a fake node's
# /var/lib/confexts directory.
# ---------------------------------------------------------------------------
cat > "${MOCK_BIN}/scp" <<'EOF'
#!/bin/sh
: "${SCP_LOG:?scp stub requires SCP_LOG}" "${FAKE_NODE_DIR:?scp stub requires FAKE_NODE_DIR}"
log() { printf '%s\n' "$*" >> "$SCP_LOG"; }
src=
dst=
for _a in "$@"; do
    case "${_a}" in
        -*) continue ;;
        *)
            if [ -z "$src" ]; then
                src=$_a
            elif [ -z "$dst" ]; then
                dst=$_a
            fi
            ;;
    esac
done
case "$src" in
    *:*)
        # fetch: user@host:remote-path -> local-file
        _rpath=${src#*:}
        _local=$dst
        if [ -f "${FAKE_NODE_DIR}${_rpath}" ]; then
            cp "${FAKE_NODE_DIR}${_rpath}" "$_local"
            log "FETCH $src -> $dst"
            exit 0
        fi
        log "FETCH-MISS $src -> $dst"
        exit 1
        ;;
    *)
        case "$dst" in
            *:*)
                # push: local-file -> user@host:remote-path
                _rpath=${dst#*:}
                case "$_rpath" in
                    /*) : ;;
                    *) log "SCP-UNPARSEABLE $*"; exit 1 ;;
                esac
                mkdir -p "$(dirname "${FAKE_NODE_DIR}${_rpath}")" || exit 1
                cp "$src" "${FAKE_NODE_DIR}${_rpath}" || exit 1
                log "PUSH $src -> $dst"
                exit 0
                ;;
            *)
                log "SCP-UNPARSEABLE $*"
                exit 1
                ;;
        esac
        ;;
esac
EOF

cat > "${MOCK_BIN}/ssh" <<'EOF'
#!/bin/sh
: "${SSH_LOG:?ssh stub requires SSH_LOG}" "${FAKE_NODE_DIR:?ssh stub requires FAKE_NODE_DIR}"
log() { printf '%s\n' "$*" >> "$SSH_LOG"; }
_host=
_cmd=
_skip=0
for _a in "$@"; do
    if [ "$_skip" -eq 1 ]; then
        _skip=0
        continue
    fi
    if [ -n "$_host" ]; then
        # Everything after the destination is the remote command, verbatim
        # (real ssh semantics): option-looking tokens such as ln -sf's "-sf"
        # or curl's "-sf" are part of the command, not client options.
        if [ -z "$_cmd" ]; then
            _cmd=$_a
        else
            _cmd="$_cmd $_a"
        fi
        continue
    fi
    case "${_a}" in
        -i|-o|-p|-l|-F|-E|-J|-W) _skip=1 ;;
        -*) : ;;
        *)
            _host=$_a
            ;;
    esac
done
log "SSH ${_host}: ${_cmd}"
case "$_cmd" in
    "sha256sum "*)
        # take the first token only, ignoring remote redirects (2>/dev/null ...)
        _path=${_cmd#sha256sum }
        for _tok in $_path; do
            _path=$_tok
            break
        done
        # Transient-failure injection (probe-retry test): while
        # SSH_PROBE_FAIL_FILE holds a positive count, fail the probe with a
        # generic ssh error so the push script's bounded retry loop is
        # exercised; each failure decrements the counter file.
        _fail_file="${SSH_PROBE_FAIL_FILE:-}"
        if [ -n "${_fail_file}" ] && [ -f "${_fail_file}" ]; then
            _left=$(cat "${_fail_file}" 2>/dev/null || true)
            case "${_left}" in
                ''|*[!0-9]*) _left=0 ;;
            esac
            if [ "${_left}" -gt 0 ]; then
                printf '%s\n' "$((_left - 1))" > "${_fail_file}"
                printf 'ssh: connect to host %s port 22: Connection refused\n' "$_host" >&2
                exit 1
            fi
        fi
        if [ -f "${FAKE_NODE_DIR}${_path}" ]; then
            sha256sum "${FAKE_NODE_DIR}${_path}" | sed "s|${FAKE_NODE_DIR}||"
            exit 0
        fi
        printf 'sha256sum: %s: No such file or directory\n' "$_path" >&2
        exit 1
        ;;
    "cat "*)
        _path=${_cmd#cat }
        for _tok in $_path; do
            _path=$_tok
            break
        done
        if [ -f "${FAKE_NODE_DIR}${_path}" ]; then
            cat "${FAKE_NODE_DIR}${_path}"
            exit 0
        fi
        exit 1
        ;;
esac
# everything else (systemd-confext refresh, systemctl ..., etcdctl ...,
# curl ...) is logged and answered with exit 0.
exit 0
EOF
chmod +x "${MOCK_BIN}/scp" "${MOCK_BIN}/ssh"

# Toolbox used by the missing-tool test: the tools a push script needs, but
# deliberately NO scp/ssh.
for _t in sh sha256sum basename dirname awk grep sed cat cut tr head tail sort uniq wc sleep rm cp mv mkdir ln chmod mktemp cmp date tee; do
    _tp=$(command -v "${_t}" 2>/dev/null || true)
    case "${_tp}" in
        /*) ln -sf "${_tp}" "${TOOLBOX}/${_t}" ;;
        *) : ;;
    esac
done

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0
SKIP=0

pass() { printf 'PASS: %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL: %s\n' "$*"; FAIL=$((FAIL + 1)); }
skip() { printf 'SKIP: %s\n' "$*"; SKIP=$((SKIP + 1)); }

# run_push <args...> — invoke the push script with the mock PATH and logs set.
run_push() {
    PATH="${MOCK_BIN}:${ORIG_PATH}" SCP_LOG="${SCP_LOG}" SSH_LOG="${SSH_LOG}" \
        FAKE_NODE_DIR="${FAKE_NODE}" sh "${PUSH_SCRIPT}" "$@"
}

# first_line LOG PATTERN — first line number containing the pattern, or empty.
first_line() {
    grep -n -F "$2" "$1" 2>/dev/null | head -n 1 | cut -d: -f1
}

# first_sysctl_line LOG UNIT — first systemctl invocation line for a unit.
first_sysctl_line() {
    grep -n -F "systemctl" "$1" 2>/dev/null | grep -F "$2" | head -n 1 | cut -d: -f1
}

# check_lt A B LABEL — PASS when A and B are both non-empty and A < B.
check_lt() {
    if [ -z "$1" ] || [ -z "$2" ]; then
        fail "$3 (missing log marker: first=$1 second=$2)"
        return
    fi
    if [ "$1" -lt "$2" ]; then
        pass "$3"
    else
        fail "$3 (line $1 is not before line $2)"
    fi
}

# check_no_enable LOG LABEL — FAIL if the log shows enablement writes into
# /etc (FIXER-E contract): `ln -sf ... /etc/systemd/...` enablement symlink
# writes or `systemctl enable`. Enablement ships inside the confext images,
# so the push step must never touch /etc — merged /etc is read-only and
# enable-before-refresh fails EROFS.
check_no_enable() {
    if grep -F 'ln -sf' "$1" 2>/dev/null | grep -F '/etc/systemd' >/dev/null; then
        fail "$2: enablement ln -sf write into /etc/systemd ran (must not; enablement ships inside the confext images)"
        return
    fi
    if grep -F 'systemctl enable' "$1" >/dev/null 2>&1; then
        fail "$2: systemctl enable ran (must not; enablement ships inside the confext images)"
        return
    fi
    pass "$2: no enable/ln -sf writes to /etc/systemd"
}

# ---------------------------------------------------------------------------
# Test 1 — identical hash: no push/refresh/enable
# ---------------------------------------------------------------------------
printf 'etcd-v1\n' > "${LOCAL_DIR}/z-etcd.raw"
cp "${LOCAL_DIR}/z-etcd.raw" "${FAKE_NODE}/var/lib/confexts/z-etcd.raw"
printf 'kubelet-v1\n' > "${LOCAL_DIR}/z-kubelet-cp1.raw"
cp "${LOCAL_DIR}/z-kubelet-cp1.raw" "${FAKE_NODE}/var/lib/confexts/z-kubelet-cp1.raw"
: > "${SCP_LOG}"
: > "${SSH_LOG}"
out=$(run_push root@cp1 "${LOCAL_DIR}/z-etcd.raw" "${LOCAL_DIR}/z-kubelet-cp1.raw" 2>&1)
rc=$?
if [ "${rc}" -eq 0 ]; then
    pass "t1 identical-hash: push script exits 0"
else
    fail "t1 identical-hash: push script exited ${rc} (expected 0)"
    printf '%s\n' "${out}" >&2
fi
if grep -q '^PUSH ' "${SCP_LOG}" 2>/dev/null; then
    fail "t1 identical-hash: scp PUSH ran (must be skipped)"
else
    pass "t1 identical-hash: no scp PUSH"
fi
if grep -qE 'systemd-confext|daemon-reload|systemctl|etcdctl|curl' "${SSH_LOG}" 2>/dev/null; then
    fail "t1 identical-hash: refresh/daemon-reload/start/health ssh commands ran (must be skipped)"
else
    pass "t1 identical-hash: no refresh/start ssh commands (only the sha256 probe is allowed)"
fi
if cmp -s "${LOCAL_DIR}/z-etcd.raw" "${FAKE_NODE}/var/lib/confexts/z-etcd.raw"; then
    pass "t1 identical-hash: remote z-etcd.raw unchanged"
else
    fail "t1 identical-hash: remote z-etcd.raw was modified"
fi

# ---------------------------------------------------------------------------
# Test 2 — changed hash on cp1: push + refresh + ordered start/restart
# (no enablement writes to /etc)
# ---------------------------------------------------------------------------
printf 'etcd-v2\n' > "${LOCAL_DIR}/z-etcd.raw"
printf 'etcd-v1\n' > "${FAKE_NODE}/var/lib/confexts/z-etcd.raw"
printf 'kubelet-v2\n' > "${LOCAL_DIR}/z-kubelet-cp1.raw"
printf 'kubelet-v1\n' > "${FAKE_NODE}/var/lib/confexts/z-kubelet-cp1.raw"
: > "${SCP_LOG}"
: > "${SSH_LOG}"
out=$(run_push root@cp1 "${LOCAL_DIR}/z-etcd.raw" "${LOCAL_DIR}/z-kubelet-cp1.raw" 2>&1)
rc=$?
if [ "${rc}" -eq 0 ]; then
    pass "t2 changed: push script exits 0"
else
    fail "t2 changed: push script exited ${rc} (expected 0)"
    printf '%s\n' "${out}" >&2
fi
for _img in z-etcd z-kubelet-cp1; do
    if grep -F "PUSH ${LOCAL_DIR}/${_img}.raw" "${SCP_LOG}" >/dev/null 2>&1; then
        pass "t2 changed: scp PUSH ${_img}.raw to /var/lib/confexts/"
    else
        fail "t2 changed: scp PUSH ${_img}.raw missing from scp log"
    fi
    if cmp -s "${LOCAL_DIR}/${_img}.raw" "${FAKE_NODE}/var/lib/confexts/${_img}.raw"; then
        pass "t2 changed: remote ${_img}.raw matches local content"
    else
        fail "t2 changed: remote ${_img}.raw was not updated"
    fi
done
_r=$(first_line "${SSH_LOG}" "systemd-confext")
_d=$(first_line "${SSH_LOG}" "daemon-reload")
_c=$(first_sysctl_line "${SSH_LOG}" "crio")
_e=$(first_sysctl_line "${SSH_LOG}" "etcd")
_h=$(first_line "${SSH_LOG}" "etcdctl")
_a=$(first_sysctl_line "${SSH_LOG}" "kube-apiserver")
_hz=$(first_line "${SSH_LOG}" "healthz")
_cm=$(first_sysctl_line "${SSH_LOG}" "kube-controller-manager")
_sd=$(first_sysctl_line "${SSH_LOG}" "kube-scheduler")
_k=$(first_sysctl_line "${SSH_LOG}" "kubelet")
check_lt "${_r}" "${_d}" "t2 order: systemd-confext refresh before systemctl daemon-reload"
check_lt "${_d}" "${_c}" "t2 order: daemon-reload before crio start"
check_lt "${_c}" "${_e}" "t2 order: crio before etcd start"
check_lt "${_e}" "${_h}" "t2 order: etcd start before etcdctl endpoint health wait"
check_lt "${_h}" "${_a}" "t2 order: etcd health gate before kube-apiserver start"
check_lt "${_a}" "${_hz}" "t2 order: apiserver start before /healthz wait"
check_lt "${_hz}" "${_cm}" "t2 order: /healthz gate before kube-controller-manager start"
check_lt "${_hz}" "${_sd}" "t2 order: /healthz gate before kube-scheduler start"
check_lt "${_sd}" "${_k}" "t2 order: scheduler before kubelet start"
check_no_enable "${SSH_LOG}" "t2 changed"

# ---------------------------------------------------------------------------
# Test 3 — worker: crio -> kubelet, no control-plane units
# ---------------------------------------------------------------------------
printf 'kubelet-w1-v2\n' > "${LOCAL_DIR}/z-kubelet-w1.raw"
printf 'kubelet-w1-v1\n' > "${FAKE_NODE}/var/lib/confexts/z-kubelet-w1.raw"
: > "${SCP_LOG}"
: > "${SSH_LOG}"
out=$(run_push root@w1 "${LOCAL_DIR}/z-kubelet-w1.raw" 2>&1)
rc=$?
if [ "${rc}" -eq 0 ]; then
    pass "t3 worker: push script exits 0"
else
    fail "t3 worker: push script exited ${rc} (expected 0)"
    printf '%s\n' "${out}" >&2
fi
if grep -F "PUSH ${LOCAL_DIR}/z-kubelet-w1.raw" "${SCP_LOG}" >/dev/null 2>&1; then
    pass "t3 worker: scp PUSH z-kubelet-w1.raw to /var/lib/confexts/"
else
    fail "t3 worker: scp PUSH z-kubelet-w1.raw missing from scp log"
fi
_d=$(first_line "${SSH_LOG}" "daemon-reload")
_c=$(first_sysctl_line "${SSH_LOG}" "crio")
_k=$(first_sysctl_line "${SSH_LOG}" "kubelet")
check_lt "${_d}" "${_c}" "t3 worker order: daemon-reload before crio start"
check_lt "${_c}" "${_k}" "t3 worker order: crio before kubelet start"
check_no_enable "${SSH_LOG}" "t3 worker"
if grep -F "systemctl" "${SSH_LOG}" 2>/dev/null | grep -qE 'etcd|kube-apiserver|kube-controller-manager|kube-scheduler'; then
    fail "t3 worker: control-plane units must not be started/enabled on a worker"
else
    pass "t3 worker: no control-plane units started/enabled"
fi

# ---------------------------------------------------------------------------
# Test 4 — partial change: push only the changed image, still refresh +
# start/restart the affected unit
# ---------------------------------------------------------------------------
printf 'etcd-v3\n' > "${LOCAL_DIR}/z-etcd.raw"
printf 'etcd-v1\n' > "${FAKE_NODE}/var/lib/confexts/z-etcd.raw"
printf 'kubelet-v1\n' > "${LOCAL_DIR}/z-kubelet-cp1.raw"
printf 'kubelet-v1\n' > "${FAKE_NODE}/var/lib/confexts/z-kubelet-cp1.raw"
: > "${SCP_LOG}"
: > "${SSH_LOG}"
out=$(run_push root@cp1 "${LOCAL_DIR}/z-etcd.raw" "${LOCAL_DIR}/z-kubelet-cp1.raw" 2>&1)
rc=$?
if [ "${rc}" -eq 0 ]; then
    pass "t4 partial: push script exits 0"
else
    fail "t4 partial: push script exited ${rc} (expected 0)"
    printf '%s\n' "${out}" >&2
fi
if grep -F "PUSH ${LOCAL_DIR}/z-etcd.raw" "${SCP_LOG}" >/dev/null 2>&1; then
    pass "t4 partial: changed z-etcd.raw pushed"
else
    fail "t4 partial: changed z-etcd.raw not pushed"
fi
if grep -F "z-kubelet-cp1" "${SCP_LOG}" >/dev/null 2>&1; then
    fail "t4 partial: unchanged z-kubelet-cp1.raw was pushed"
else
    pass "t4 partial: unchanged z-kubelet-cp1.raw not pushed"
fi
if grep -q "systemd-confext" "${SSH_LOG}" 2>/dev/null; then
    pass "t4 partial: systemd-confext refresh ran"
else
    fail "t4 partial: systemd-confext refresh did not run"
fi
if grep -q "daemon-reload" "${SSH_LOG}" 2>/dev/null; then
    pass "t4 partial: systemctl daemon-reload ran"
else
    fail "t4 partial: systemctl daemon-reload did not run"
fi
if grep -F "systemctl" "${SSH_LOG}" 2>/dev/null | grep -F "etcd" >/dev/null; then
    pass "t4 partial: affected unit etcd was (re)started"
else
    fail "t4 partial: affected unit etcd was not (re)started"
fi
check_no_enable "${SSH_LOG}" "t4 partial"

# ---------------------------------------------------------------------------
# Test 5 — missing scp/ssh in PATH -> clear failure naming the tool
# ---------------------------------------------------------------------------
printf 'etcd-v1\n' > "${LOCAL_DIR}/z-etcd.raw"
out=$(PATH="${TOOLBOX}" SCP_LOG="${SCP_LOG}" SSH_LOG="${SSH_LOG}" FAKE_NODE_DIR="${FAKE_NODE}" sh "${PUSH_SCRIPT}" root@cp1 "${LOCAL_DIR}/z-etcd.raw" 2>&1)
rc=$?
if [ "${rc}" -ne 0 ]; then
    case "${out}" in
        *scp*|*ssh*) pass "t5 missing scp/ssh: push script fails with a clear message naming scp/ssh" ;;
        *) fail "t5 missing scp/ssh: failure message does not name scp/ssh (rc=${rc})" ;;
    esac
else
    fail "t5 missing scp/ssh: push script exited 0 although scp/ssh are absent from PATH"
fi

# ---------------------------------------------------------------------------
# Test 6 — missing local .raw -> failure naming the file
# ---------------------------------------------------------------------------
out=$(run_push root@cp1 "${WORK}/does-not-exist.raw" 2>&1)
rc=$?
if [ "${rc}" -ne 0 ]; then
    case "${out}" in
        *does-not-exist.raw*) pass "t6 missing local .raw: push script fails naming the missing file" ;;
        *) fail "t6 missing local .raw: failure message does not name the missing file (rc=${rc})" ;;
    esac
else
    fail "t6 missing local .raw: push script exited 0 for a nonexistent local .raw"
fi

# ---------------------------------------------------------------------------
# Test 7 — unparseable host arg -> failure
# ---------------------------------------------------------------------------
out=$(run_push garbage "${LOCAL_DIR}/z-etcd.raw" 2>&1)
rc=$?
if [ "${rc}" -ne 0 ]; then
    pass "t7 unparseable host: push script rejects a host arg without '@' and not 'localhost'"
else
    fail "t7 unparseable host: push script accepted 'garbage' as a node"
fi

# ---------------------------------------------------------------------------
# Test 8 — no args -> failure with usage output
# ---------------------------------------------------------------------------
out=$(run_push 2>&1)
rc=$?
if [ "${rc}" -ne 0 ] && [ -n "${out}" ]; then
    pass "t8 no args: push script fails with a usage message"
else
    fail "t8 no args: push script must fail with a usage message (rc=${rc})"
fi

# ---------------------------------------------------------------------------
# Test 9 — probe retries preserved: transient ssh failures are retried, not
# silently skipped; exhaustion still logs WARNING and exits 0 so a
# fixture apply stays safe (FIXER-E keeps probe_remote_hash bounded retries)
# ---------------------------------------------------------------------------
printf 'etcd-retry\n' > "${LOCAL_DIR}/z-etcd.raw"
printf 'etcd-retry-old\n' > "${FAKE_NODE}/var/lib/confexts/z-etcd.raw"
printf '2\n' > "${PROBE_FAIL_FILE}"
: > "${SCP_LOG}"
: > "${SSH_LOG}"
out=$(PROBE_RETRY_ATTEMPTS=5 PROBE_RETRY_SLEEP=0 SSH_PROBE_FAIL_FILE="${PROBE_FAIL_FILE}" run_push root@cp1 "${LOCAL_DIR}/z-etcd.raw" 2>&1)
rc=$?
if [ "${rc}" -eq 0 ]; then
    pass "t9 retry: push script exits 0 after transient probe failures"
else
    fail "t9 retry: push script exited ${rc} (expected 0 after retries)"
    printf '%s\n' "${out}" >&2
fi
_probe_count=$(grep -c 'sha256sum' "${SSH_LOG}" 2>/dev/null || true)
if [ "${_probe_count:-0}" -ge 3 ]; then
    pass "t9 retry: sha256 probe retried (${_probe_count} probe ssh calls for 1 image)"
else
    fail "t9 retry: sha256 probe not retried (${_probe_count} probe ssh calls, expected >= 3)"
fi
if grep -F "PUSH ${LOCAL_DIR}/z-etcd.raw" "${SCP_LOG}" >/dev/null 2>&1; then
    pass "t9 retry: image pushed after transient probe failures"
else
    fail "t9 retry: image not pushed after transient probe failures"
fi
# exhaustion: all PROBE_RETRY_ATTEMPTS probes fail -> WARNING + exit 0
printf '5\n' > "${PROBE_FAIL_FILE}"
: > "${SCP_LOG}"
: > "${SSH_LOG}"
out=$(PROBE_RETRY_ATTEMPTS=5 PROBE_RETRY_SLEEP=0 SSH_PROBE_FAIL_FILE="${PROBE_FAIL_FILE}" run_push root@cp1 "${LOCAL_DIR}/z-etcd.raw" 2>&1)
rc=$?
if [ "${rc}" -eq 0 ]; then
    pass "t9 exhaust: push script exits 0 after probe exhaustion (fixture-safe)"
else
    fail "t9 exhaust: push script exited ${rc} (expected 0, WARNING path)"
fi
case "${out}" in
    *WARNING*) pass "t9 exhaust: WARNING logged after ${PROBE_RETRY_ATTEMPTS:-5} failed probes" ;;
    *) fail "t9 exhaust: no WARNING in output after probe exhaustion" ;;
esac
_exhaust_probes=$(grep -c 'sha256sum' "${SSH_LOG}" 2>/dev/null || true)
if [ "${_exhaust_probes:-0}" -eq 5 ]; then
    pass "t9 exhaust: exactly 5 probe attempts before WARNING"
else
    fail "t9 exhaust: ${_exhaust_probes} probe attempts (expected 5)"
fi
if grep -q '^PUSH ' "${SCP_LOG}" 2>/dev/null; then
    fail "t9 exhaust: scp PUSH ran although the probe never succeeded"
else
    pass "t9 exhaust: no scp PUSH on probe exhaustion"
fi

# ---------------------------------------------------------------------------
# Summary — exit 0 iff no test FAILed
# ---------------------------------------------------------------------------
if [ "${FAIL}" -eq 0 ]; then
    result=PASS
else
    result=FAIL
fi
printf 'RESULT: %s (%d pass, %d fail, %d skip)\n' "${result}" "${PASS}" "${FAIL}" "${SKIP}"
[ "${FAIL}" -eq 0 ]
