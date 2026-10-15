#!/usr/bin/env bash
#
# run-placement.sh — kube-sched placement experiment driver.
#
# Usage:
#   run-placement.sh --validate --profiles-dir DIR
#   run-placement.sh plan       --profiles-dir DIR
#   run-placement.sh run --matrix NAME --profiles-dir DIR --data-root DIR
#
# `plan` prints the seeded profile order and needs no cluster. `run` submits a
# fixed Pod burst under each profile and records submit-to-scheduled latency
# and placement correctness into pods.csv. It never reports a fake success: a
# missing cluster, unswitchable scheduler profile, or unavailable metric aborts
# the block.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
KUBE_SCHED_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
KUBECONFIG_PATH="${PROJECT_ROOT}/build/kubeconfig"

CONTROL_PROFILE="${KUBE_SCHED_CONTROL_PROFILE:-least-allocated}"
KUBE_SCHED_PLAN_SEED="${KUBE_SCHED_PLAN_SEED:-20261015}"
KUBE_SCHED_BLOCKS="${KUBE_SCHED_BLOCKS:-5}"
KUBE_SCHED_BURST="${KUBE_SCHED_BURST:-12}"
KUBE_SCHED_TOPOLOGY_KEY="${KUBE_SCHED_TOPOLOGY_KEY:-topology.kubernetes.io/zone}"
KUBE_SCHED_MAX_SKEW="${KUBE_SCHED_MAX_SKEW:-1}"
KUBE_SCHED_SCHEDULE_TIMEOUT_S="${KUBE_SCHED_SCHEDULE_TIMEOUT_S:-120}"
# Control-plane kube-scheduler configuration file (verified on the lab image).
KUBE_SCHED_SCHEDULER_CONFIG="${KUBE_SCHED_SCHEDULER_CONFIG:-/etc/kubernetes/kube-scheduler.yaml}"

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
die() { printf '[%s] FATAL: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

kubectl_wl() { kubectl --kubeconfig "$KUBECONFIG_PATH" "$@"; }

require_cmd() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || die "required tool not on PATH: $cmd"
    done
}

profile_names() {
    local dir="$1"
    find "$dir" -maxdepth 1 -name '*.yaml' -printf '%f\n' | sed 's/\.yaml$//' | sort
}

seeded_order() {
    local seed="$1"
    shift
    python3 - "$seed" "$CONTROL_PROFILE" "$@" <<'PY'
import random, sys
seed = int(sys.argv[1])
control = sys.argv[2]
profiles = sys.argv[3:]
rng = random.Random(seed)
rng.shuffle(profiles)
# Keep the control first so every block starts from a known baseline state,
# and rotate only the treatment profiles.
profiles = [p for p in profiles if p != control]
profiles = [control] + profiles
print(" ".join(profiles))
PY
}

cmd_validate() {
    local dir="$1" name
    local count=0
    for name in $(profile_names "$dir"); do
        python3 - "$dir/${name}.yaml" <<'PY' || die "profile ${name} is not a valid KubeSchedulerConfiguration"
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
assert doc.get("kind") == "KubeSchedulerConfiguration", doc.get("kind")
assert doc.get("apiVersion", "").startswith("kubescheduler.config.k8s.io/"), doc.get("apiVersion")
assert doc.get("profiles"), "no profiles"
PY
        printf '  [OK] %s\n' "$name"
        count=$(( count + 1 ))
    done
    (( count > 0 )) || die "no profiles found in ${dir}"
}

cmd_plan() {
    local dir="$1" b
    local names; names="$(profile_names "$dir" | tr '\n' ' ')"
    printf 'control=%s blocks=%s profiles=%s\n' "$CONTROL_PROFILE" "$KUBE_SCHED_BLOCKS" "$names"
    local -a profiles
    mapfile -t profiles < <(profile_names "$dir")
    for (( b = 1; b <= KUBE_SCHED_BLOCKS; b++ )); do
        local seed=$(( KUBE_SCHED_PLAN_SEED + b ))
        printf '  block=%s seed=%s order=%s\n' "$b" "$seed" "$(seeded_order "$seed" "${profiles[@]}")"
    done
}

control_plane_ip() {
    kubectl_wl get nodes --no-headers \
        -l 'node-role.kubernetes.io/control-plane' \
        -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}'
}

activate_profile() {
    local profile="$1"
    local cp_ip; cp_ip="$(control_plane_ip)"
    [[ -n "$cp_ip" ]] || die "could not resolve the control-plane node IP"
    log "activating scheduler profile ${profile} on ${cp_ip}"
    scp -i "${PROJECT_ROOT}/build/packer-ssh-key" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        "${KUBE_SCHED_DIR}/profiles/${profile}.yaml" "fedora@${cp_ip}:/tmp/kube-scheduler-profile.yaml" \
        || die "failed to copy profile ${profile} to the control plane"
    ssh -i "${PROJECT_ROOT}/build/packer-ssh-key" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "fedora@${cp_ip}" \
        "sudo install -m 0644 /tmp/kube-scheduler-profile.yaml ${KUBE_SCHED_SCHEDULER_CONFIG} && sudo systemctl restart kube-scheduler" \
        || die "failed to activate profile ${profile} (check KUBE_SCHED_SCHEDULER_CONFIG=${KUBE_SCHED_SCHEDULER_CONFIG})"
}

reset_pods() {
    # A failed or incomplete reset would let prior-profile Pods pollute the next
    # burst, so a failure here is fatal.
    kubectl_wl delete pods -l app=kube-sched-probe --ignore-not-found \
        --wait=true --timeout=120s >/dev/null \
        || die "failed to delete probe Pods before the next profile"
    local remaining
    remaining="$(kubectl_wl get pods -l app=kube-sched-probe --no-headers 2>/dev/null | wc -l)"
    [[ "$remaining" -eq 0 ]] \
        || die "${remaining} probe Pod(s) still present after reset"
}

# submit_burst MATRIX BLOCK — submit pods, wait, emit pods.csv rows on stdout.
submit_burst() {
    local matrix="$1" block="$2"
    python3 "${SCRIPT_DIR}/submit_burst.py" \
        --matrix "$matrix" --block "$block" --burst "$KUBE_SCHED_BURST" \
        --timeout "$KUBE_SCHED_SCHEDULE_TIMEOUT_S" \
        --topology-key "$KUBE_SCHED_TOPOLOGY_KEY" --max-skew "$KUBE_SCHED_MAX_SKEW"
}

cmd_run() {
    local matrix="$1" dir="$2" data_root="$3"
    require_cmd kubectl python3 ssh scp
    [[ -f "$KUBECONFIG_PATH" ]] || die "workload kubeconfig missing: ${KUBECONFIG_PATH}"
    kubectl_wl get nodes --no-headers >/dev/null 2>&1 || die "workload cluster unreachable"
    [[ -f "${dir}/${matrix}.yaml" ]] || die "unknown matrix/profile: ${matrix}"

    case "$data_root" in
        "$PROJECT_ROOT"/*) die "data root must live outside the Git worktree: ${data_root}" ;;
    esac
    mkdir -p "$data_root"
    local pods_csv="${data_root}/pods.csv"
    printf 'block_id,profile,pod,latency_ms,unschedulable,correct\n' > "$pods_csv"

    local -a names
    mapfile -t names < <(profile_names "$dir")
    local b seed profile
    for (( b = 1; b <= KUBE_SCHED_BLOCKS; b++ )); do
        seed=$(( KUBE_SCHED_PLAN_SEED + b ))
        # IFS excludes the space, so the seeded order must be split explicitly.
        local -a order
        IFS=' ' read -r -a order <<< "$(seeded_order "$seed" "${names[@]}")"
        for profile in "${order[@]}"; do
            activate_profile "$profile"
            reset_pods
            log "block ${b} profile ${profile}: submitting burst"
            submit_burst "${matrix}" "$b" >> "$pods_csv" \
                || die "burst failed for block ${b} profile ${profile}"
        done
    done
    log "placement experiment complete; pods.csv: ${pods_csv}"
}

main() {
    local subcmd="" dir=""
    local matrix=""
    local data_root=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --validate) subcmd="validate"; shift ;;
            plan|run) subcmd="$1"; shift ;;
            --profile-dir|--profiles-dir) dir="$2"; shift 2 ;;
            --matrix) matrix="$2"; shift 2 ;;
            --data-root) data_root="$2"; shift 2 ;;
            -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    [[ -n "$dir" ]] || die "--profiles-dir is required"
    case "$subcmd" in
        validate) cmd_validate "$dir" ;;
        plan) cmd_plan "$dir" ;;
        run)
            [[ -n "$matrix" ]] || die "--matrix is required"
            [[ -n "$data_root" ]] || die "--data-root is required"
            cmd_run "$matrix" "$dir" "$data_root"
            ;;
        *) die "no subcommand given; see --help" ;;
    esac
}

main "$@"
