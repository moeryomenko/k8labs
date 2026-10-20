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

# Schedulers registered for the cgroup-v2 campaign. The first entry is the
# control arm. Runtime preflight must confirm each sched_ext arm can build,
# attach, make progress, and detach on the pinned kernel before it is measured.
LINUX_SCHED_CONTROL="${LINUX_SCHED_CONTROL:-eevdf}"
LINUX_SCHED_ARMS="${LINUX_SCHED_ARMS:-eevdf scx_bpfland scx_lavd scx_mavd scx_mlfq scx_nitosis}"

# Seed for the deterministic, position-balanced block schedule. It lives here
# rather than in the runner because the schedule helpers are one of the things
# tests source directly.
LINUX_SCHED_PLAN_SEED="${LINUX_SCHED_PLAN_SEED:-20261015}"

# Minimum samples frozen by the plan.
LINUX_SCHED_MIN_BLOCKS="${LINUX_SCHED_MIN_BLOCKS:-5}"
LINUX_SCHED_PILOT_BLOCKS="${LINUX_SCHED_PILOT_BLOCKS:-3}"
LINUX_SCHED_COOLDOWN_S="${LINUX_SCHED_COOLDOWN_S:-15}"
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
# The knee is refined until the passing/failing bracket is this close to the
# passing rate, so the frozen value is not one coarse sweep step below the real
# knee.
LINUX_SCHED_RATE_REFINE_PCT="${LINUX_SCHED_RATE_REFINE_PCT:-5}"

# How long `perf sched` records for, and how much of the window the scheduler
# trace covers. The node's root filesystem is about 5 GiB in practice (the
# provider ignores the machine template's disk size — see GATES.md), and kubelet
# evicts a Pod once ephemeral storage falls below ~789 MB. With the base image,
# the observer's tooling and the workload already on that filesystem, a
# full-window `perf.data` or a full-window trace does not fit: a workload
# sharing the node was evicted in the middle of a load. Both are therefore
# bounded, and what a sample keeps is the run-queue histograms over the whole
# window, the `perf sched latency` summary, and a compressed trace slice.
LINUX_SCHED_PERF_WINDOW_S="${LINUX_SCHED_PERF_WINDOW_S:-10}"
LINUX_SCHED_TRACE_WINDOW_S="${LINUX_SCHED_TRACE_WINDOW_S:-10}"

# How long to wait for a workload Pod to start serving before the load begins.
LINUX_SCHED_WORKLOAD_READY_TIMEOUT_S="${LINUX_SCHED_WORKLOAD_READY_TIMEOUT_S:-120}"

# ---- Storage budget (see GATES.md) ----
# The worker root filesystem is about 5 GiB and kubelet evicts a Pod once free
# space falls below the measured threshold. A sample needs room for the
# observer spool, the workload's ephemeral storage and a reserve, so the gate is
# the sum rather than any one of them.
#
# Every term below is measured rather than assumed, because the gate decides
# whether the study can run at all on this hardware: the node must also hold the
# workload images for all five scenarios (~830 MB) plus the observer image, and
# an over-sized gate refuses arms on a node that in fact has ample room.
LINUX_SCHED_EVICTION_THRESHOLD_B="${LINUX_SCHED_EVICTION_THRESHOLD_B:-789102828}"
# Measured sample spool with the 10-second evidence windows: 26-33 MB across all
# five scenarios (about 9 MB of compressed trace plus roughly 22 MB of compressed
# perf data, plus the histograms). The cap is twice that, which still catches a
# runaway while leaving the node room to hold every scenario's image — the images,
# not the samples, are what fills these 5 GiB workers.
LINUX_SCHED_SPOOL_CAP_MIB="${LINUX_SCHED_SPOOL_CAP_MIB:-64}"
# Largest measured workload ephemeral storage is the Postgres scale-2 database
# at about 36 MB; the compute-only scenarios use almost none. Allowance is
# under two times the largest measured value.
LINUX_SCHED_WORKLOAD_STORAGE_MIB="${LINUX_SCHED_WORKLOAD_STORAGE_MIB:-64}"
# Headroom held back on top of the kubelet eviction threshold. Zero because the
# two allowances above are already twice the measured transient need; the gate's
# job is to keep the measured need away from the eviction threshold, not to demand
# unallocated space the node cannot have.
LINUX_SCHED_DISK_RESERVE_MIB="${LINUX_SCHED_DISK_RESERVE_MIB:-0}"
LINUX_SCHED_QUIESCE_TIMEOUT_S="${LINUX_SCHED_QUIESCE_TIMEOUT_S:-300}"

# Wall-clock ceiling for one confirmatory run, in seconds. The stopping rule is
# time, not the data: it is independent of the results, so the frozen decision
# rule needs no alpha adjustment, and a run that hits the ceiling reports whatever
# the frozen rule can decide and calls the rest inconclusive rather than complete.
LINUX_SCHED_WALL_CLOCK_CAP_S="${LINUX_SCHED_WALL_CLOCK_CAP_S:-14400}"

# Charged on top of the slowest observed block when deciding whether the next
# block fits the wall-clock budget: quiescence, artifact verification and cleanup
# happen after the measurement and are part of the block's cost.
LINUX_SCHED_CLEANUP_ALLOWANCE_S="${LINUX_SCHED_CLEANUP_ALLOWANCE_S:-120}"

# Names the measurement protocol a block belongs to, so data from an earlier
# window length is never pooled with data from this one.
LINUX_SCHED_PROTOCOL_VERSION="${LINUX_SCHED_PROTOCOL_VERSION:-v3}"

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
# Scenario-scoped variables, cleared before each scenario is sourced.
#
# A scenario that omits one of these would otherwise inherit the previous
# scenario's value in any process that loads more than one — which is how an
# ffmpeg profiling session ran postgres's readiness probe and died on
# `pg_isready` against a database that scenario never starts.
SCENARIO_VARIABLES=(
    SCENARIO_NAME SCENARIO_DESCRIPTION WORKLOAD PRIMARY_METRIC PRIMARY_DIRECTION
    DURATION_S WARMUP_S TARGET_HOST LOAD_HOST WORKLOAD_MANIFEST METRIC_COMMAND
    PRIMARY_SOURCE SECONDARY_METRIC_COMMAND SECONDARY_SOURCE SECONDARY_DIRECTION
    LOAD_COMMAND LOAD_ARGS LOAD_READY_PROBE
)

load_scenario() {
    local dir="$1" name="$2" variable
    local file="${dir}/${name}.conf"
    [[ -f "$file" ]] || die "scenario not found: $file"
    for variable in "${SCENARIO_VARIABLES[@]}"; do
        unset "$variable"
    done
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

# guest_cpu_busy_pct NODE — guest CPU busy percentage, read from the
# observer Pod's host /proc/stat (the observer runs with hostPID).
#
# Two samples one second apart, because the running totals in /proc/stat are an
# average since boot: a node that ran anything heavy hours ago still reports a
# high average, so the cumulative figure cannot answer "is it quiet now". Only
# the aggregate `cpu` line is read; the per-CPU and interrupt lines have
# different field layouts and made the arithmetic meaningless.
guest_cpu_busy_pct() {
    local node="$1" pod
    pod="$(observer_pod_for_node "$node")"
    [[ -n "$pod" ]] || { echo 100; return 0; }
    kexec "$pod" sh -c '
        read_cpu() {
            awk "/^cpu /{print \$2+\$3+\$4, \$5; exit}" /proc/stat
        }
        set -- $(read_cpu); busy1=$1; idle1=$2
        sleep 1
        set -- $(read_cpu); busy2=$1; idle2=$2
        busy=$((busy2 - busy1))
        idle=$((idle2 - idle1))
        total=$((busy + idle))
        if [ "$total" -gt 0 ]; then
            echo $((busy * 100 / total))
        else
            echo 0
        fi
    ' 2>/dev/null || echo 100
}

# ---- Storage budget helpers ----
# guest_free_bytes NODE — free bytes on the node's root filesystem, read through
# the observer's host mount (the spool and the workload both live there).
guest_free_bytes() {
    local node="$1" pod
    pod="$(observer_pod_for_node "$node")"
    [[ -n "$pod" ]] || { echo 0; return 0; }
    kexec "$pod" sh -c "df -B1 --output=avail /var/tmp | tail -1" 2>/dev/null \
        | tr -d '[:space:]' || echo 0
}

# required_guest_free_bytes — the free space a worker needs before a sample.
required_guest_free_bytes() {
    local mib=$(( LINUX_SCHED_SPOOL_CAP_MIB
        + LINUX_SCHED_WORKLOAD_STORAGE_MIB
        + LINUX_SCHED_DISK_RESERVE_MIB ))
    echo $(( LINUX_SCHED_EVICTION_THRESHOLD_B + mib * 1048576 ))
}

# require_guest_disk NODE — refuse to start a sample the worker cannot hold.
#
# The measured failure this prevents: a workload Pod evicted for
# ephemeral-storage in the middle of a load, which surfaces as the database
# backend dying rather than as a disk error.
require_guest_disk() {
    local node="$1" free required
    required="$(required_guest_free_bytes)"
    free="$(guest_free_bytes "$node")"
    [[ "$free" =~ ^[0-9]+$ ]] || die "could not read free space on ${node}"
    if (( free < required )); then
        die "worker ${node} has ${free} B free but a sample needs ${required} B; refusing to run"
    fi
    printf '%s' "$free"
}

# balanced_block_order BLOCK — the arm order for a block, position-balanced.
#
# A seeded base permutation rotated by the block index, so that over every
# three-block cycle each arm occupies each position exactly once. Rotating is
# what makes position balance hold for any block count that is a multiple of
# three, which is why the confirmatory count is rounded up to one.
balanced_block_order() {
    local block="$1"
    local -a base out
    local n rot i
    # seeded_permutation prints the arms space-joined on one line, so the words
    # have to be split explicitly before they can be indexed.
    mapfile -t base < <(split_words "$(seeded_permutation "$LINUX_SCHED_PLAN_SEED")")
    n="${#base[@]}"
    (( n > 0 )) || die "no scheduler arms configured"
    rot=$(( (block - 1) % n ))
    out=()
    for (( i = 0; i < n; i++ )); do
        out+=( "${base[$(( (i + rot) % n ))]}" )
    done
    printf '%s\n' "${out[@]}"
}

# next_block_id BLOCKS_CSV — the first unused block id in a phase's results.
#
# A re-run — after an interrupted block, or a storage-gate refusal — must not
# append a second row for a block id that already has rows: that produces
# duplicate (block, scheduler) pairs, which the analysis rejects outright rather
# than silently choosing one. Continuing the numbering keeps blocks consistent.
next_block_id() {
    local csv="$1"
    [[ -s "$csv" ]] || { echo 1; return 0; }
    awk -F, 'NR > 1 && $1+0 > m { m = $1+0 } END { print m+1 }' "$csv"
}

# within_wall_clock_cap ELAPSED_S ESTIMATE_S CAP_S — true when another block fits.
#
# Checked before a block starts, against an estimate of how long one takes, so the
# run cannot be cut off inside a block: a partial block would be discarded and its
# arms wasted.
within_wall_clock_cap() {
    local elapsed="$1" estimate="$2" cap="$3"
    (( cap <= 0 )) && return 0
    (( elapsed + estimate <= cap ))
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

    # Detach check. The protocol's gates require a clean detach, and a survivor
    # would keep scheduling the arm that is about to run, so this is verified
    # rather than assumed from the kill having been issued. The scheduler
    # binaries are matched by name, because the kernel's own `scx_enable_helper`
    # thread also matches `scx_*` and exists whether or not one is attached.
    #
    # The wait matters: unregistering takes a moment, and checking the instant
    # after the signal fails an arm that is actually detaching cleanly.
    local remaining=1 deadline=$(( SECONDS + 15 ))
    while (( SECONDS < deadline )); do
        remaining="$(kexec "$pod" sh -c '
            n=0
            for p in /proc/[0-9]*; do
                c=$(cat "$p/comm" 2>/dev/null) || continue
                case "$c" in scx_bpfland|scx_lavd|scx_mavd|scx_mlfq|scx_nitosis) n=$((n + 1));; esac
            done
            echo "$n"
        ' 2>/dev/null || echo 1)"
        [[ "${remaining:-1}" -eq 0 ]] && return 0
        sleep 1
    done
    die "sched_ext scheduler still attached on ${node} after reset (${remaining} process(es))"
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
# kernel_cgroup_bw_supported NODE — does the running kernel deliver
# ops.cgroup_set_bandwidth to sched_ext? The fair class enforces cpu.max itself,
# but sched_ext schedulers only see the setting through that callback. On a
# kernel without it, passing --enable-cpu-bw makes the scheduler refuse to start,
# so the capability is probed before the flag is used.
kernel_cgroup_bw_supported() {
    local node="$1" pod
    pod="$(observer_pod_for_node "$node")"
    [[ -n "$pod" ]] || return 1
    kubectl --kubeconfig "$(workload_kubeconfig)" -n "$OBSERVER_NAMESPACE" \
        exec "$pod" -- sh -c \
        'bpftool btf dump file /sys/kernel/btf/vmlinux format c 2>/dev/null | grep -q cgroup_set_bandwidth' \
        >/dev/null 2>&1
}

scx_start() {
    local node="$1" name="$2" pod log pid
    shift 2
    local -a extra=("$@")
    pod="$(observer_pod_for_node "$node")"
    [[ -n "$pod" ]] || die "no observer pod on ${node}"
    scx_reset "$node"
    log="${LINUX_SCHED_SCX_LOG_DIR:-/tmp}/${name}.log"
    # The binary is exec'd through `sh -c`: the runtime refuses to exec a
    # hostPath binary directly ("exists but it is not executable").
    local cmd="exec ${REMOTE_DIR:-/var/tmp/linux-sched}/bin/${name}"
    if (( ${#extra[@]} > 0 )); then
        cmd+=" $(printf '%q ' "${extra[@]}")"
    fi
    kubectl --kubeconfig "$(workload_kubeconfig)" -n "$OBSERVER_NAMESPACE" \
        exec "$pod" -- sh -c "$cmd" \
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
    local policy="${8:-}"
    if [[ ! -f "$file" ]]; then
        printf 'block_id,scheduler,workload,metric,value,direction,policy\n' > "$file"
    fi
    printf '%s,%s,%s,%s,%s,%s,%s\n' \
        "$block" "$scheduler" "$workload" "$metric" "$value" "$direction" "$policy" >> "$file"
}

# frozen_quota_for WORKLOAD - the CPU limit frozen for this workload.
#
# The quota is a property of the workload, not of the phase, so it is read from
# the quota campaign root. Missing or malformed input is fatal: a confirmatory
# run must never guess a quota.
frozen_quota_for() {
    local workload="$1"
    local file="${LINUX_SCHED_FROZEN_QUOTA_FILE:-${RAW_DATA_ROOT}/frozen-quotas.yaml}"
    [[ -f "$file" ]] || die "no frozen quota manifest at ${file}; run make linux-sched-freeze-quota"
    python3 - "$file" "$workload" <<'PYQUOTA'
import sys
import yaml

manifest = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for entry in manifest.get("quotas", []):
    if entry.get("workload") == sys.argv[2]:
        print(entry["cpu_limit"])
        break
else:
    raise SystemExit(f"no frozen quota for workload {sys.argv[2]!r}")
PYQUOTA
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
