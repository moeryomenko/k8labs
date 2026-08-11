#!/bin/sh
# shellcheck disable=SC2292  # POSIX sh per AGENTS.md: [ ] not [[ ]]; rc forces shell=bash
# shellcheck disable=SC2086  # intentional word splitting of $SSH_OPTS / $SCP_OPTS
#
# push-confext.sh — Push role-split runtime confext images to a cluster node
# and activate the Kubernetes services in dependency order.
# (phase-B push and activation contract).
#
# Usage: push-confext.sh <user@host|localhost> <raw-file> [<raw-file> ...]
#
# Contract (encoded by scripts/verify/verify-push-logic.sh):
#   1. For each <raw-file> (must exist locally) the remote path is always
#      /var/lib/confexts/<name>.raw where <name> is the basename without .raw.
#      The remote sha256 is probed with `ssh <target> sha256sum
#      /var/lib/confexts/<name>.raw`, with bounded retries for transient
#      failures on a booting node; a probe that fails because the remote
#      file does not exist ("No such file" from sha256sum) means the image
#      must be pushed. Identical hash -> skip that image (no scp, no refresh,
#      no restart). Different/missing -> `scp <raw-file>
#      <target>:/var/lib/confexts/<name>.raw`.
#   2. If at least one image changed:
#      - `ssh <target> systemd-confext refresh`
#      - `ssh <target> systemctl daemon-reload`
#      - start/restart in dependency order, each as its OWN ssh invocation so
#        the sequence is observable; units whose image changed and
#        that are already active are then explicitly restarted so the new
#        confext content is loaded. Enablement is NOT done here: the
#        enablement symlinks ship inside the z- confext images
#        (etc/systemd/system/multi-user.target.wants/), so the merge enables
#        the units and /etc is already read-only when phase B runs — any
#        enable/write into /etc would fail EROFS.
#        control-plane (role derived from the image set: presence of
#        z-etcd.raw / z-kubernetes-cp.raw):
#          systemctl start crio.service
#          systemctl start etcd.service
#          wait: ssh <target> etcdctl endpoint health
#          systemctl start kube-apiserver.service
#          wait: ssh <target> curl -k -sf https://127.0.0.1:6443/healthz
#          systemctl start kube-controller-manager.service
#          systemctl start kube-scheduler.service
#          systemctl start kubelet.service        (kubelet on ALL nodes)
#        worker:
#          systemctl start crio.service
#          systemctl start kubelet.service
#        The affected-unit set is derived from the changed images (z-etcd ->
#        etcd; z-kubernetes-cp -> kube-apiserver/kube-controller-manager/
#        kube-scheduler; z-kubelet-<node> -> kubelet) plus crio, which always
#        applies. For an affected unit the sequence is: probe
#        `systemctl is-active --quiet <unit>`; `systemctl start <unit>`; if the
#        probe said the unit was already active, an explicit
#        `systemctl restart <unit>` follows so the running service picks up the
#        new merged content. A first activation (unit inactive) is started once
#        by start and is not double-started. start/restart never write into
#        /etc (read-only merged overlay), so they are safe on every phase-B run.
#   3. If no image changed: only the sha256 probe ssh runs.
#   4. Errors: missing local .raw, unparseable host arg (no '@' and not
#      'localhost'), or missing scp/ssh in PATH -> exit non-zero with a clear
#      message naming the problem. An unreachable host (the probe ssh cannot
#      connect after PROBE_RETRY_ATTEMPTS bounded retries) logs a WARNING and
#      exits 0 so a fixture apply stays safe; the real path is validated in E2E.
#
# Environment:
#   PUSH_SSH_OPTS  extra ssh options (default: batch-mode + 5s connect timeout,
#                  host key check disabled like wait-ssh — destroy/recreate can
#                  reuse DHCP IPs with a new host key)
#   PUSH_SCP_OPTS  extra scp options (default: none)
#   ETCD_WAIT_ATTEMPTS / ETCD_WAIT_SLEEP        etcdctl health retry bounds
#   APISERVER_WAIT_ATTEMPTS / APISERVER_WAIT_SLEEP  /healthz retry bounds
#   PROBE_RETRY_ATTEMPTS / PROBE_RETRY_SLEEP    remote sha256 probe retry
#                  bounds (default 5 attempts x 5s); a transient ssh failure
#                  on a booting node is retried before the host is treated as
#                  unreachable
#
# POSIX sh only (AGENTS.md); no bashisms, no pipefail.
# =============================================================================

set -eu

usage() {
    echo "Usage: push-confext.sh <user@host|localhost> <raw-file> [<raw-file> ...]" >&2
}

die() {
    echo "push-confext: error: $*" >&2
    exit 1
}

SSH_OPTS="${PUSH_SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null}"
SCP_OPTS="${PUSH_SCP_OPTS:-}"

if [ "$#" -lt 2 ]; then
    usage
    exit 2
fi

target="$1"
shift

case "${target}" in
    localhost) : ;;
    *@*) : ;;
    *)
        die "unparseable node target '${target}' (expected user@host or localhost)"
        ;;
esac

if ! command -v scp >/dev/null 2>&1; then
    die "scp not found in PATH (required to push confext images)"
fi
if ! command -v ssh >/dev/null 2>&1; then
    die "ssh not found in PATH (required for the remote sha256 probe and service start/restart)"
fi

for raw in "$@"; do
    if [ ! -f "${raw}" ]; then
        die "local confext image not found: ${raw}"
    fi
done

# probe_remote_hash <target> <remote-path> — bounded-retry remote sha256 probe
# Prints the remote hash on success and returns 0; prints an empty line
# for a missing remote file (sha256sum "No such file") and returns 0 (treated
# as changed, will push); returns 1 only after PROBE_RETRY_ATTEMPTS transient
# failures, so a node whose sshd is still coming up is retried instead of being
# silently skipped.
probe_remote_hash() {
    _attempt=0
    while [ "${_attempt}" -lt "${PROBE_RETRY_ATTEMPTS:-5}" ]; do
        # shellcheck disable=SC2029 # remote-path is client-constructed (fixed dir + local basename), expansion is intended
        if _probe_out=$(ssh ${SSH_OPTS} "$1" sha256sum "$2" 2>&1); then
            printf '%s\n' "${_probe_out%% *}"
            return 0
        fi
        case "${_probe_out}" in
            *sha256sum:*"No such file"*)
                # Remote image absent -> treated as changed, will push.
                printf '\n'
                return 0
                ;;
            *)
                # Genuine probe failure (host unreachable, sshd still coming
                # up): retry below, then treat as unreachable when exhausted.
                :
                ;;
        esac
        _attempt=$((_attempt + 1))
        if [ "${_attempt}" -lt "${PROBE_RETRY_ATTEMPTS:-5}" ]; then
            sleep "${PROBE_RETRY_SLEEP:-5}"
        fi
    done
    return 1
}

# wait_for_etcd <target> — poll `etcdctl endpoint health` over ssh.
wait_for_etcd() {
    _attempt=0
    while [ "${_attempt}" -lt "${ETCD_WAIT_ATTEMPTS:-30}" ]; do
        if ssh ${SSH_OPTS} "$1" etcdctl endpoint health >/dev/null 2>&1; then
            return 0
        fi
        _attempt=$((_attempt + 1))
        sleep "${ETCD_WAIT_SLEEP:-2}"
    done
    die "etcd did not report healthy after ${ETCD_WAIT_ATTEMPTS:-30} attempts (etcdctl endpoint health)"
}

# wait_for_apiserver <target> — poll kube-apiserver /healthz over ssh.
wait_for_apiserver() {
    _attempt=0
    while [ "${_attempt}" -lt "${APISERVER_WAIT_ATTEMPTS:-30}" ]; do
        if ssh ${SSH_OPTS} "$1" curl -k -sf https://127.0.0.1:6443/healthz >/dev/null 2>&1; then
            return 0
        fi
        _attempt=$((_attempt + 1))
        sleep "${APISERVER_WAIT_SLEEP:-2}"
    done
    die "kube-apiserver /healthz did not return ok after ${APISERVER_WAIT_ATTEMPTS:-30} attempts"
}

# start_or_restart <target> <unit> <changed> — start the unit, then, when the
# unit was already active and its image changed, restart it so the new merged
# confext content is loaded by the running service. Enablement is
# shipped inside the confext images themselves, so this step only
# starts/restarts and never writes into the read-only merged /etc.
# The is-active probe runs before start so a first activation (unit inactive)
# is started once and not double-started. Each step is its own ssh invocation
# so the sequence is observable.
start_or_restart() {
    _was_active=0
    if ssh ${SSH_OPTS} "$1" systemctl is-active --quiet "$2"; then
        _was_active=1
    fi
    # shellcheck disable=SC2029 # unit name is client-constructed (trusted literals from start_units), expansion is intended
    ssh ${SSH_OPTS} "$1" systemctl start "$2"
    if [ "${_was_active}" -eq 1 ] && [ "$3" -eq 1 ]; then
        # shellcheck disable=SC2029 # unit name is client-constructed (trusted literals from start_units), expansion is intended
        ssh ${SSH_OPTS} "$1" systemctl restart "$2"
    fi
}

# start_units <target> <role> <etcd_changed> <kcp_changed> <kubelet_changed>
# — systemctl start in dependency order with health gates, one ssh
# invocation per step. crio always applies (it is the first step on
# every changed-content run); the control-plane units restart only when
# z-kubernetes-cp changed, etcd only when z-etcd changed, kubelet only when
# z-kubelet-<node> changed.
start_units() {
    start_or_restart "$1" crio.service 1
    if [ "$2" = "control-plane" ]; then
        start_or_restart "$1" etcd.service "$3"
        wait_for_etcd "$1"
        start_or_restart "$1" kube-apiserver.service "$4"
        wait_for_apiserver "$1"
        start_or_restart "$1" kube-controller-manager.service "$4"
        start_or_restart "$1" kube-scheduler.service "$4"
        start_or_restart "$1" kubelet.service "$5"
    else
        start_or_restart "$1" kubelet.service "$5"
    fi
}

# Role is derived from the image set: any control-plane image (z-etcd,
# z-kubernetes-cp) marks a control-plane node; otherwise worker.
role=worker
for raw in "$@"; do
    case "$(basename "${raw}")" in
        z-etcd.raw | z-kubernetes-cp.raw) role=control-plane ;;
        *) : ;;
    esac
done

changed=0
etcd_changed=0
kcp_changed=0
kubelet_changed=0
for raw in "$@"; do
    name=$(basename "${raw}" .raw)
    remote_path="/var/lib/confexts/${name}.raw"

    local_hash=$(sha256sum "${raw}" 2>/dev/null)
    local_hash=${local_hash%% *}

    # shellcheck disable=SC2029 # remote_path is client-constructed (fixed dir + local basename), expansion is intended
    _probe_rc=0
    # shellcheck disable=SC2310,SC2311 # probe failure is handled by the caller (WARNING + exit 0), not by set -e; POSIX sh has no inherit_errexit
    remote_hash=$(probe_remote_hash "${target}" "${remote_path}") || _probe_rc=$?
    if [ "${_probe_rc}" -ne 0 ]; then
        # The probe failed after all retries (host unreachable, bad identity,
        # etc.). Keep fixture applies safe: WARNING, no push. Real nodes are
        # retried for PROBE_RETRY_ATTEMPTS x PROBE_RETRY_SLEEP before this is
        # reached, so a transient sshd race on a booting node no longer skips
        # the push silently.
        echo "push-confext: WARNING: node ${target} unreachable or ssh probe failed after ${PROBE_RETRY_ATTEMPTS:-5} attempts; skipping push for this apply" >&2
        exit 0
    fi

    if [ "${local_hash}" = "${remote_hash}" ]; then
        echo "push-confext: SKIP ${name}: ${raw} already matches remote ${remote_path}"
    else
        echo "push-confext: PUSH ${name}: ${raw} -> ${target}:${remote_path}"
        scp ${SCP_OPTS} "${raw}" "${target}:${remote_path}"
        changed=1
        case "$(basename "${raw}")" in
            z-etcd.raw) etcd_changed=1 ;;
            z-kubernetes-cp.raw) kcp_changed=1 ;;
            z-kubelet-*.raw) kubelet_changed=1 ;;
            *) : ;;
        esac
    fi
done

if [ "${changed}" -eq 0 ]; then
    echo "push-confext: no changes for ${target}; all images already match"
    exit 0
fi

echo "push-confext: refresh confext overlay on ${target}"
ssh ${SSH_OPTS} "${target}" systemd-confext refresh

echo "push-confext: reload systemd on ${target}"
ssh ${SSH_OPTS} "${target}" systemctl daemon-reload

echo "push-confext: start/restart services on ${target} (role=${role})"
start_units "${target}" "${role}" "${etcd_changed}" "${kcp_changed}" "${kubelet_changed}"

exit 0
