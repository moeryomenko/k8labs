# shellcheck shell=bash
#
# lib.sh — shared helpers for the linux-sched experiment runner.
#
# Sourced by run-block.sh. Provides logging, scenario loading, cluster/worker
# discovery, SSH helpers, quiescence checks, and the seeded arm-order generator
# that the paired-block protocol depends on.

[[ -n ${_LINUX_SCHED_LIB_SH:-} ]] && return 0
_LINUX_SCHED_LIB_SH=1

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

# Schedulers compared in every block. The first entry is the control arm.
LINUX_SCHED_CONTROL="${LINUX_SCHED_CONTROL:-eevdf}"
LINUX_SCHED_ARMS="${LINUX_SCHED_ARMS:-eevdf scx_bpfland scx_lavd scx_mavd}"

# Minimum samples frozen by the plan.
LINUX_SCHED_MIN_BLOCKS="${LINUX_SCHED_MIN_BLOCKS:-5}"
LINUX_SCHED_PILOT_BLOCKS="${LINUX_SCHED_PILOT_BLOCKS:-3}"
LINUX_SCHED_COOLDOWN_S="${LINUX_SCHED_COOLDOWN_S:-60}"
LINUX_SCHED_QUIESCE_PCT="${LINUX_SCHED_QUIESCE_PCT:-10}"
LINUX_SCHED_QUIESCE_HOLD_S="${LINUX_SCHED_QUIESCE_HOLD_S:-30}"
LINUX_SCHED_QUIESCE_TIMEOUT_S="${LINUX_SCHED_QUIESCE_TIMEOUT_S:-300}"

OBSERVER_NAMESPACE="${OBSERVER_NAMESPACE:-linux-sched}"

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
die() { printf '[%s] FATAL: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

require_cmd() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || die "required tool not on PATH: $cmd"
    done
}

project_root() { git rev-parse --show-toplevel 2>/dev/null || pwd; }

workload_kubeconfig() { printf '%s/build/kubeconfig' "$(project_root)"; }

kubectl_wl() { kubectl --kubeconfig "$(workload_kubeconfig)" "$@"; }

# scenario_names DIR — list scenario files in deterministic order.
scenario_names() {
    local dir="$1"
    [[ -d "$dir" ]] || die "scenarios dir not found: $dir"
    find "$dir" -maxdepth 1 -name '*.conf' -printf '%f\n' | sort | sed 's/\.conf$//'
}

# load_scenario DIR NAME — source one scenario file into the current shell.
load_scenario() {
    local dir="$1" name="$2"
    local file="${dir}/${name}.conf"
    [[ -f "$file" ]] || die "scenario not found: $file"
    # shellcheck disable=SC1090
    source "$file"
    : "${SCENARIO_NAME:?scenario $name missing SCENARIO_NAME}"
    : "${WORKLOAD:?scenario $name missing WORKLOAD}"
    : "${PRIMARY_METRIC:?scenario $name missing PRIMARY_METRIC}"
    : "${PRIMARY_DIRECTION:?scenario $name missing PRIMARY_DIRECTION}"
    : "${DURATION_S:?scenario $name missing DURATION_S}"
}

# ready_workers — research workers, identified by the observer opt-in label.
#
# The lab's control-plane node carries neither a role label nor a taint (every
# node reports ROLES <none>), so `!node-role.kubernetes.io/control-plane`
# selects everything. The observer label is the only reliable discriminator.
ready_workers() {
    kubectl_wl get nodes --no-headers \
        -l 'linux-sched/research=observer' \
        -o custom-columns=:metadata.name 2>/dev/null || true
}

# observer_pod POD_NAMESPACE — name of the observer pod on the target worker.
observer_pod_for_node() {
    local node="$1"
    kubectl_wl -n "$OBSERVER_NAMESPACE" get pods \
        -l 'app.kubernetes.io/name=linux-sched-observer' \
        --field-selector "spec.nodeName=${node}" \
        -o custom-columns=:metadata.name --no-headers 2>/dev/null | head -1
}

# kexec POD [args...] — run a command inside the observer pod.
kexec() {
    local pod="$1"
    shift
    kubectl_wl -n "$OBSERVER_NAMESPACE" exec "$pod" -- "$@"
}

# NOTE: node access is through the observer DaemonSet over the Kubernetes API.
# SSH to the lab VMs never completes (see GATES.md), so no ssh helpers exist
# here on purpose.

# observer_cpu_busy_pct NODE — guest CPU busy percentage, read from the
# observer Pod's host /proc/stat (the observer runs with hostPID).
guest_cpu_busy_pct() {
    local node="$1" pod
    pod="$(observer_pod_for_node "$node")"
    [[ -n "$pod" ]] || { echo 100; return 0; }
    kexec "$pod" sh -c \
        "awk '{print 100-(\$5*100/(\$2+\$3+\$4+\$5+\$6+\$7+\$8))}' /proc/stat" \
        2>/dev/null || echo 100
}

# seeded_permutation SEED N — print a deterministic balanced permutation of the
# scheduler arms. Rotating positions across blocks is the caller's job; the
# generator is seeded and recorded so the order can be replayed.
seeded_permutation() {
    local seed="$1"
    python3 - "$seed" "$LINUX_SCHED_ARMS" <<'PY'
import random, sys
seed = int(sys.argv[1])
arms = sys.argv[2].split()
rng = random.Random(seed)
rng.shuffle(arms)
print(" ".join(arms))
PY
}

# guest_cpu_busy_pct — see definition above (observer-driven).

# wait_quiescent NODE — block until the guest is idle for the required hold time.
wait_quiescent() {
    local node="$1"
    local deadline=$(( $(date +%s) + LINUX_SCHED_QUIESCE_TIMEOUT_S ))
    local held=0
    while (( $(date +%s) < deadline )); do
        local busy
        busy="$(guest_cpu_busy_pct "$node")"
        if awk -v b="$busy" -v t="$LINUX_SCHED_QUIESCE_PCT" 'BEGIN { exit (b < t) ? 0 : 1 }'; then
            held=$(( held + 5 ))
            (( held >= LINUX_SCHED_QUIESCE_HOLD_S )) && return 0
        else
            held=0
        fi
        sleep 5
    done
    return 1
}

# append_block_row FILE BLOCK WORKLOAD METRIC SCHEDULER VALUE DIRECTION
#
# DIRECTION is the scenario's PRIMARY_DIRECTION so the analysis never has to
# re-derive it from the metric name.
append_block_row() {
    local file="$1" block="$2" workload="$3" metric="$4" scheduler="$5" value="$6" direction="$7"
    if [[ ! -f "$file" ]]; then
        printf 'block_id,scheduler,workload,metric,value,direction\n' > "$file"
    fi
    printf '%s,%s,%s,%s,%s,%s\n' \
        "$block" "$scheduler" "$workload" "$metric" "$value" "$direction" >> "$file"
}

# split_arms ORDER_STRING — print one whitespace-separated token per line.
#
# The shell's IFS deliberately excludes the space (it is newline/tab), so a
# command substitution holding a space-separated list does NOT split into
# words. Splitting explicitly here is the only safe way to iterate arms.
split_words() {
    [[ -n "${1:-}" ]] || return 0
    local -a words
    IFS=' ' read -r -a words <<< "$1"
    printf '%s\n' "${words[@]}"
}
