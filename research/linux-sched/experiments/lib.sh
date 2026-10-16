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
#
# scx_mavd is absent on purpose: its object sets BPF_F_ARENA_SCALAR, which the
# pinned kernel rejects, and its sources no longer build in the classic arena
# form. It rejoins the series when a kernel with that flag is available; set
# LINUX_SCHED_ARMS to add it back. See GATES.md.
LINUX_SCHED_CONTROL="${LINUX_SCHED_CONTROL:-eevdf}"
LINUX_SCHED_ARMS="${LINUX_SCHED_ARMS:-eevdf scx_bpfland scx_lavd}"

# Minimum samples frozen by the plan.
LINUX_SCHED_MIN_BLOCKS="${LINUX_SCHED_MIN_BLOCKS:-5}"
LINUX_SCHED_PILOT_BLOCKS="${LINUX_SCHED_PILOT_BLOCKS:-3}"
LINUX_SCHED_COOLDOWN_S="${LINUX_SCHED_COOLDOWN_S:-60}"
LINUX_SCHED_QUIESCE_PCT="${LINUX_SCHED_QUIESCE_PCT:-10}"
LINUX_SCHED_QUIESCE_HOLD_S="${LINUX_SCHED_QUIESCE_HOLD_S:-30}"

# Offered-rate calibration (http, mixed). The rate is found on the control arm
# by raising it until p99 crosses the target, then freezing the highest rate
# that stayed under it. Every arm then offers that same rate, so the comparison
# is at equal load rather than at equal achieved throughput.
LINUX_SCHED_RATE_P99_TARGET_MS="${LINUX_SCHED_RATE_P99_TARGET_MS:-10}"
LINUX_SCHED_RATE_SWEEP_START="${LINUX_SCHED_RATE_SWEEP_START:-500}"
LINUX_SCHED_RATE_SWEEP_FACTOR="${LINUX_SCHED_RATE_SWEEP_FACTOR:-1.5}"
LINUX_SCHED_RATE_SWEEP_MAX="${LINUX_SCHED_RATE_SWEEP_MAX:-65536}"
LINUX_SCHED_RATE_PROBE_S="${LINUX_SCHED_RATE_PROBE_S:-30}"

# How long `perf sched` records for. The node's root filesystem is ~4.9 GB in
# practice (see GATES.md), and a full-window `perf.data` alone is over a
# gigabyte, which trips kubelet's disk-pressure eviction; the record is
# therefore bounded and its `perf sched latency` summary is what is kept.
LINUX_SCHED_PERF_WINDOW_S="${LINUX_SCHED_PERF_WINDOW_S:-60}"

# How long to wait for a workload Pod to start serving before the load begins.
LINUX_SCHED_WORKLOAD_READY_TIMEOUT_S="${LINUX_SCHED_WORKLOAD_READY_TIMEOUT_S:-120}"
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
#
# Bounded: a hung kubectl exec would otherwise stall an experiment with no
# output, which is worse than a failed sample.
kexec() {
    local pod="$1"
    shift
    timeout "${LINUX_SCHED_EXEC_TIMEOUT_S:-60}" \
        kubectl --kubeconfig "$(workload_kubeconfig)" -n "$OBSERVER_NAMESPACE" \
        exec "$pod" -- "$@"
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

# ---- sched_ext scheduler helpers ----

# scx_root — checkout holding the built sched_ext schedulers.
scx_root() {
    printf '%s' "${LINUX_SCHED_SCX_ROOT:-$(project_root)/../scx}"
}

# scx_binary NAME — path to a built scheduler binary, or a clear failure.
#
# The binaries must be built against the running kernel's BTF (see GATES.md);
# the runner does not build them, it only stages what is there.
scx_binary() {
    local name="$1" path
    path="$(scx_root)/target/release/${name}"
    [[ -x "$path" ]] \
        || die "missing ${path}; build ${name} against the running kernel's BTF first (see GATES.md)"
    printf '%s' "$path"
}

# scx_state_file — local file holding the pid of the supervising kubectl exec.
scx_state_file() { printf '%s' "${LINUX_SCHED_SCX_STATE:-/tmp/linux-sched-scx.pid}"; }

# load_needs_rate — true when the scenario's load arguments carry a rate
# placeholder that the calibration has to fill in.
load_needs_rate() {
    [[ "${LOAD_ARGS:-}" == *"__RPS__"* ]]
}

# offered_rate_file DATA_ROOT SCENARIO — where the calibrated rate is frozen.
# One file per scenario, so a run cannot pick up another scenario's rate.
offered_rate_file() {
    printf '%s/%s/offered-rate-rps' "$1" "$2"
}

# render_load_command RATE DURATION — fill the load template's placeholders.
# __RPS__ is the calibrated offered rate; __DURATION__ is the window, which a
# calibration probe shortens so it does not need a second copy of the command.
render_load_command() {
    local rate="$1" duration="$2"
    local cmd="${LOAD_COMMAND:-}${LOAD_ARGS:+ ${LOAD_ARGS}}"
    cmd="${cmd//__RPS__/${rate}}"
    cmd="${cmd//__DURATION__/${duration}}"
    printf '%s' "$cmd"
}

# frozen_offered_rate DATA_ROOT SCENARIO — the calibrated rate, or a clear
# failure. A scenario that needs a rate must not run on a guessed one.
frozen_offered_rate() {
    local file
    file="$(offered_rate_file "$1" "$2")"
    [[ -s "$file" ]] \
        || die "no calibrated offered rate in ${file}; run 'run-block.sh calibrate --scenario $2' first"
    tr -d '[:space:]' < "$file"
}

# scx_reset NODE — stop any running scheduler and clear its stats socket.
#
# A scheduler started through `kubectl exec` dies when that exec ends, so it is
# supervised by a long-lived local `kubectl exec` whose pid is recorded; killing
# that ends the remote process. The stats socket is cleared by renaming its
# directory, because root cannot unlink the socket itself.
scx_reset() {
    local node="$1" pod state pid
    state="$(scx_state_file)"
    if [[ -s "$state" ]]; then
        pid="$(cat "$state")"
        kill -TERM "$pid" 2>/dev/null || true
        sleep 1
        kill -KILL "$pid" 2>/dev/null || true
        rm -f "$state"
    fi
    pod="$(observer_pod_for_node "$node")"
    [[ -n "$pod" ]] || return 0
    # Kill by comm, not by `pkill -f`: a -f pattern would also match this
    # command's own command line and take the exec session down with it.
    kexec "$pod" sh -c '
        for p in /proc/[0-9]*; do
            c=$(cat "$p/comm" 2>/dev/null) || continue
            case "$c" in
                scx_*) kill "${p#/proc/}" 2>/dev/null || true ;;
            esac
        done
        if [ -d /var/run/scx ]; then
            mv /var/run/scx "/var/run/scx.stale.$$" 2>/dev/null \
                || rm -rf /var/run/scx 2>/dev/null || true
        fi
    ' >/dev/null 2>&1 || true
}

# scx_stage NODE — copy the built schedulers into that node's observer.
#
# Stages exactly the sched_ext arms the run will use, so a binary that is absent
# (or one the series has deferred) cannot break staging for the others.
scx_stage() {
    local node="$1" pod name
    local -a arms
    pod="$(observer_pod_for_node "$node")"
    [[ -n "$pod" ]] || die "no observer pod on ${node}"
    kexec "$pod" sh -c 'mkdir -p /var/tmp/linux-sched/bin'
    mapfile -t arms < <(split_words "$LINUX_SCHED_ARMS")
    for name in "${arms[@]}"; do
        case "$name" in scx_*) ;; *) continue ;; esac
        kubectl_wl cp "$(scx_binary "$name")" \
            "${OBSERVER_NAMESPACE}/${pod}:/var/tmp/linux-sched/bin/${name}" >/dev/null \
            || die "failed to copy ${name} to ${pod}"
    done
    kexec "$pod" sh -c 'chmod +x /var/tmp/linux-sched/bin/*'
}

# scx_start NODE NAME — start a scheduler and verify it attached; prints the pid
# of the supervising local `kubectl exec`.
scx_start() {
    local node="$1" name="$2" pod log pid
    pod="$(observer_pod_for_node "$node")"
    [[ -n "$pod" ]] || die "no observer pod on ${node}"
    scx_reset "$node"
    log="${LINUX_SCHED_SCX_LOG_DIR:-/tmp}/${name}.log"
    # The binary is exec'd through `sh -c`: the runtime refuses to exec a
    # hostPath binary directly ("exists but it is not executable").
    kubectl --kubeconfig "$(workload_kubeconfig)" -n "$OBSERVER_NAMESPACE" \
        exec "$pod" -- sh -c "exec ${REMOTE_DIR:-/var/tmp/linux-sched}/bin/${name}" \
        > "$log" 2>&1 &
    pid=$!
    printf '%s' "$pid" > "$(scx_state_file)"
    sleep "${LINUX_SCHED_SCX_ATTACH_S:-5}"
    if ! kill -0 "$pid" 2>/dev/null; then
        tail -5 "$log" >&2 || true
        die "${name} failed to attach on ${node}"
    fi
    printf '%s' "$pid"
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
