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
#      /var/lib/confexts/<name>.raw`; a probe that fails because the remote
#      file does not exist ("No such file" from sha256sum) means the image
#      must be pushed. Identical hash -> skip that image (no scp, no refresh,
#      no restart). Different/missing -> `scp <raw-file>
#      <target>:/var/lib/confexts/<name>.raw`.
#   2. If at least one image changed:
#      - `ssh <target> systemd-confext refresh`
#      - `ssh <target> systemctl daemon-reload`
#      - enable/start in dependency order, each as its OWN ssh invocation so
#        the sequence is observable; units whose image changed and
#        that are already active are then explicitly restarted so the new
#        confext content is loaded:
#        control-plane (role derived from the image set: presence of
#        z-etcd.raw / z-kubernetes-cp.raw):
#          systemctl enable --now crio.service
#          systemctl enable --now etcd.service
#          wait: ssh <target> etcdctl endpoint health
#          systemctl enable --now kube-apiserver.service
#          wait: ssh <target> curl -k -sf https://127.0.0.1:6443/healthz
#          systemctl enable --now kube-controller-manager.service
#          systemctl enable --now kube-scheduler.service
#        worker:
#          systemctl enable --now crio.service
#          systemctl enable --now kubelet.service
#        The affected-unit set is derived from the changed images (z-etcd ->
#        etcd; z-kubernetes-cp -> kube-apiserver/kube-controller-manager/
#        kube-scheduler; z-kubelet-<node> -> kubelet) plus crio, which always
#        applies. For an affected unit the sequence is: probe
#        `systemctl is-active --quiet <unit>`; `systemctl enable --now <unit>`;
#        if the probe said the unit was already active, an explicit
#        `systemctl restart <unit>` follows so the running service picks up the
#        new merged content. A first activation (unit inactive) is started once
#        by enable --now and is not double-started.
#   3. If no image changed: only the sha256 probe ssh runs.
#   4. Errors: missing local .raw, unparseable host arg (no '@' and not
#      'localhost'), or missing scp/ssh in PATH -> exit non-zero with a clear
#      message naming the problem. An unreachable host (the probe ssh cannot
#      connect) logs a WARNING and exits 0 so a fixture apply stays safe; the
#      real path is validated in E2E.
#
# Environment:
#   PUSH_SSH_OPTS  extra ssh options (default: batch-mode + 5s connect timeout,
#                  host key check disabled like wait-ssh — destroy/recreate can
#                  reuse DHCP IPs with a new host key)
#   PUSH_SCP_OPTS  extra scp options (default: none)
#   ETCD_WAIT_ATTEMPTS / ETCD_WAIT_SLEEP        etcdctl health retry bounds
#   APISERVER_WAIT_ATTEMPTS / APISERVER_WAIT_SLEEP  /healthz retry bounds
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
    die "ssh not found in PATH (required for the remote sha256 probe and service enable)"
fi

for raw in "$@"; do
    if [ ! -f "${raw}" ]; then
        die "local confext image not found: ${raw}"
    fi
done

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

# start_or_restart <target> <unit> <changed> — enable --now the unit, then, when
# the unit was already active and its image changed, restart it so the new
# merged confext content is loaded by the running service. The
# is-active probe runs before enable so a first activation (unit inactive) is
# started once by enable --now and not double-started. Each step is its own ssh
# invocation so the sequence is observable.
start_or_restart() {
    _was_active=0
    if ssh ${SSH_OPTS} "$1" systemctl is-active --quiet "$2"; then
        _was_active=1
    fi
    ssh ${SSH_OPTS} "$1" systemctl enable --now "$2"
    if [ "${_was_active}" -eq 1 ] && [ "$3" -eq 1 ]; then
        # shellcheck disable=SC2029 # unit name is client-constructed (trusted literals from enable_units), expansion is intended
        ssh ${SSH_OPTS} "$1" systemctl restart "$2"
    fi
}

# enable_units <target> <role> <etcd_changed> <kcp_changed> <kubelet_changed>
# — systemctl enable --now in dependency order with health gates, one ssh
# invocation per step. crio always applies (it is the first step on
# every changed-content run); the control-plane units restart only when
# z-kubernetes-cp changed, etcd only when z-etcd changed, kubelet only when
# z-kubelet-<node> changed.
enable_units() {
    start_or_restart "$1" crio.service 1
    if [ "$2" = "control-plane" ]; then
        start_or_restart "$1" etcd.service "$3"
        wait_for_etcd "$1"
        start_or_restart "$1" kube-apiserver.service "$4"
        wait_for_apiserver "$1"
        start_or_restart "$1" kube-controller-manager.service "$4"
        start_or_restart "$1" kube-scheduler.service "$4"
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
    if remote_out=$(ssh ${SSH_OPTS} "${target}" sha256sum "${remote_path}" 2>&1); then
        remote_hash=${remote_out%% *}
    else
        case "${remote_out}" in
            *sha256sum:*"No such file"*)
                # Remote image absent -> treated as changed, will push.
                remote_hash=""
                ;;
            *)
                # The probe itself failed (host unreachable, bad identity,
                # etc.). Keep fixture applies safe: WARNING, no push.
                echo "push-confext: WARNING: node ${target} unreachable or ssh probe failed; skipping push for this apply" >&2
                exit 0
                ;;
        esac
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

echo "push-confext: enable/start services on ${target} (role=${role})"
enable_units "${target}" "${role}" "${etcd_changed}" "${kcp_changed}" "${kubelet_changed}"

exit 0
