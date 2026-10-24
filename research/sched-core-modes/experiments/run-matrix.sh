#!/usr/bin/env bash
# shellcheck disable=SC2016  # remote scripts are intentionally single-quoted
# shellcheck disable=SC2310  # SSH helpers are checked explicitly at call sites
# shellcheck disable=SC2312  # timestamps and node lists are best-effort
# Run the cgroup-mode matrix on a k8labs worker.
#
# The lab worker is reachable only through the control-plane VM: the host has
# the control-plane key, and the control plane carries the worker key at
# /tmp/id_ed25519. This script copies the experiment files through that jump
# host, runs the matrix as root on the worker, pulls the JSONL result back, and
# restores `concur` and removes /sys/fs/cgroup/sched-modes on every exit path.
#
# Usage:
#   run-matrix.sh --setup
#   run-matrix.sh --plan --profile pilot
#   run-matrix.sh --profile pilot --worker custom
#   run-matrix.sh --profile full --worker stock --run-id 20261024T120000Z
#
# Environment overrides:
#   KUBECONFIG, DATA_ROOT, RUN_ID, REPS, DURATION, WARMUP, COOLDOWN,
#   SCENARIOS, MODES, PARENT_WEIGHT, WORKER, CP_HOST, CP_PORT, CP_KEY,
#   WORKER_KEY, REMOTE_DIR.
set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

SERIES_DIR=$(CDPATH=; cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
EXPERIMENTS_DIR="${SERIES_DIR}/experiments"
PROJECT_ROOT=$(CDPATH=; cd -- "${SERIES_DIR}/../.." && pwd)
KUBECONFIG=${KUBECONFIG:-${PROJECT_ROOT}/build/kubeconfig}
CP_HOST=${CP_HOST:-127.0.0.1}
CP_KEY=${CP_KEY:-${HOME}/.config/k8labs/host-agent/ssh/id_ed25519}
CP_PORT=${CP_PORT:-}
WORKER_KEY=${WORKER_KEY:-/tmp/id_ed25519}
REMOTE_DIR=${REMOTE_DIR:-/var/tmp/sched-core-modes}
WORKER=${WORKER:-custom}
PROFILE=${PROFILE:-full}
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
SCENARIOS=${SCENARIOS:-weighted_threads,qos_nested,quota,latency}
MODES=${MODES:-up,smp,concur,max,tasks}
PARENT_WEIGHT=${PARENT_WEIGHT:-300}
COOLDOWN=${COOLDOWN:-1}
DATA_ROOT=${DATA_ROOT:-${K8LABS_RESEARCH_DATA_ROOT:-${PROJECT_ROOT}/build/research}/sched-core-modes}
ACTION=run
RESTORE_MODE=0
WORKER_NODE=""
WORKER_IP=""
WORKER_KERNEL=""

SSH_OPTS=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o ConnectTimeout=10
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=4
)
# IFS is newline/tab, so ${SSH_OPTS[*]} would join with a newline. The nested
# ssh command runs through the control plane's shell and needs spaces.
SSH_OPTS_REMOTE="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

log() {
    printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: run-matrix.sh [OPTIONS]

Options:
  --setup                 Discover the worker and check the remote prerequisites
  --plan                  Print the seeded matrix plan from the worker
  --profile pilot|full    Sample size (pilot: 1 rep x 3 s; full: 3 reps x 8 s)
  --worker custom|stock   Worker class (default: custom)
  --run-id ID             Run identifier (default: UTC timestamp)
  --reps N                Override the profile repetition count
  --duration S            Override the measurement window
  --warmup S              Override the warmup window
  --cooldown S            Delay between scenarios (default: 1 s)
  --scenarios LIST        Comma-separated scenario names
  --modes LIST            Comma-separated cgroup modes
  -h, --help              Show this help
EOF
}

while (($#)); do
    case "$1" in
        --setup)
            ACTION=setup
            shift
            ;;
        --plan)
            ACTION=plan
            shift
            ;;
        --profile)
            PROFILE=${2:?--profile requires a value}
            shift 2
            ;;
        --worker)
            WORKER=${2:?--worker requires a value}
            shift 2
            ;;
        --run-id)
            RUN_ID=${2:?--run-id requires a value}
            shift 2
            ;;
        --reps)
            REPS=${2:?--reps requires a value}
            shift 2
            ;;
        --duration)
            DURATION=${2:?--duration requires a value}
            shift 2
            ;;
        --warmup)
            WARMUP=${2:?--warmup requires a value}
            shift 2
            ;;
        --cooldown)
            COOLDOWN=${2:?--cooldown requires a value}
            shift 2
            ;;
        --scenarios)
            SCENARIOS=${2:?--scenarios requires a value}
            shift 2
            ;;
        --modes)
            MODES=${2:?--modes requires a value}
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

case "${PROFILE}" in
    pilot)
        : "${REPS:=1}"
        : "${DURATION:=3}"
        : "${WARMUP:=1}"
        ;;
    full)
        : "${REPS:=3}"
        : "${DURATION:=8}"
        : "${WARMUP:=2}"
        ;;
    *)
        die "unknown profile: ${PROFILE}"
        ;;
esac
: "${REPS:?REPS is empty}"
: "${DURATION:?DURATION is empty}"
: "${WARMUP:?WARMUP is empty}"
RUN_DIR=${RUN_DIR:-${DATA_ROOT}/${RUN_ID}}

for command in kubectl python3 tar ssh; do
    command -v "${command}" >/dev/null 2>&1 || die "required tool not found: ${command}"
done
[[ -f "${KUBECONFIG}" ]] || die "workload kubeconfig not found: ${KUBECONFIG}"
[[ -f "${CP_KEY}" ]] || die "control-plane SSH key not found: ${CP_KEY}"

cp_ssh() {
    ssh -i "${CP_KEY}" -p "${CP_PORT}" "${SSH_OPTS[@]}" "root@${CP_HOST}" "$@"
}

worker_ssh_script() {
    local script="${1:?worker_ssh_script requires a script}"
    cp_ssh "ssh -i '${WORKER_KEY}' ${SSH_OPTS_REMOTE} 'root@${WORKER_IP}' bash -s" <<<"${script}"
}

discover_cp_port() {
    local state="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/k8snet/state.json"
    [[ -f "${state}" ]] || return 0
    python3 - "${state}" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1], encoding="utf-8"))
for name, ports in state.get("publish_table", {}).get("entries", {}).items():
    if "22" in ports and "-md-" not in name:
        print(ports["22"])
        break
PY
}

discover_worker() {
    local nodes custom stock selected ip ready
    mapfile -t nodes < <(
        kubectl --kubeconfig "${KUBECONFIG}" get nodes \
            -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'
    )
    ((${#nodes[@]} > 0)) || die "no nodes in ${KUBECONFIG}"

    custom=""
    stock=""
    for node in "${nodes[@]}"; do
        [[ -z "${custom}" && "${node}" == *-md-custom-* ]] && custom="${node}"
        [[ -z "${stock}" && "${node}" == *-md-0-* ]] && stock="${node}"
    done
    if [[ -z "${stock}" ]]; then
        for node in "${nodes[@]}"; do
            [[ "${node}" == *-md-* && "${node}" != *-md-custom-* ]] || continue
            stock="${node}"
            break
        done
    fi

    case "${WORKER}" in
        custom)
            selected="${custom}"
            [[ -n "${selected}" ]] || die "no custom worker (name contains -md-custom-) found"
            ;;
        stock)
            selected="${stock}"
            [[ -n "${selected}" ]] || die "no stock worker (name contains -md-0-) found"
            ;;
        *)
            die "unknown worker class: ${WORKER}"
            ;;
    esac

    ip=$(kubectl --kubeconfig "${KUBECONFIG}" get node "${selected}" \
        -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
    ready=$(kubectl --kubeconfig "${KUBECONFIG}" get node "${selected}" \
        -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{end}')
    [[ -n "${ip}" ]] || die "could not resolve the InternalIP of ${selected}"
    [[ "${ready}" == "True" ]] || die "worker ${selected} is not Ready"

    WORKER_NODE="${selected}"
    WORKER_IP="${ip}"
    log "worker ${WORKER_NODE} (${WORKER_IP})"
}

copy_files() {
    worker_ssh_script "mkdir -p '${REMOTE_DIR}'"
    tar -C "${EXPERIMENTS_DIR}" -cf - \
        scenarios.py run_matrix.py cpu_burn.c wakeup_latency.c \
        | cp_ssh "ssh -i '${WORKER_KEY}' ${SSH_OPTS_REMOTE} 'root@${WORKER_IP}' 'tar -C ${REMOTE_DIR} --touch -xf -'"
}

check_remote() {
    local mode kernel nproc
    kernel=$(worker_ssh_script 'uname -r' | tr -d '[:space:]')
    [[ -n "${kernel}" ]] || die "could not read the worker kernel"
    WORKER_KERNEL="${kernel}"

    case "${WORKER}" in
        custom)
            [[ "${kernel}" == *k8labs* ]] || die "custom worker is running ${kernel}, expected a -k8labs kernel"
            [[ "${WORKER_NODE}" == *-md-custom-* ]] || die "custom worker name lacks -md-custom-: ${WORKER_NODE}"
            ;;
        stock)
            [[ "${kernel}" != *k8labs* ]] || die "stock worker is running the comparison kernel: ${kernel}"
            ;;
        *)
            die "unknown worker class: ${WORKER}"
            ;;
    esac

    mode=$(worker_ssh_script '
        mode=/sys/kernel/debug/sched/cgroup_mode
        if [ ! -e "$mode" ]; then
            mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
        fi
        cat "$mode" 2>/dev/null || true
    ')
    [[ -n "${mode}" ]] || die "cgroup_mode is not available on ${WORKER_NODE}"
    log "kernel ${kernel}; cgroup_mode: ${mode}"

    nproc=$(worker_ssh_script 'nproc' | tr -d '[:space:]')
    log "worker CPUs: ${nproc}"

    cp_ssh "test -f '${WORKER_KEY}'" || die "worker key ${WORKER_KEY} is missing on the control plane"
    if ! cp_ssh "ssh -i '${WORKER_KEY}' ${SSH_OPTS_REMOTE} 'root@${WORKER_IP}' true" 2>/dev/null; then
        die "cannot SSH from the control plane to ${WORKER_NODE} (${WORKER_IP})"
    fi
}

prepare_mode() {
    worker_ssh_script '
        mode=/sys/kernel/debug/sched/cgroup_mode
        if [ ! -e "$mode" ]; then
            mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
        fi
        printf "concur\n" > "$mode"
        line=$(cat "$mode")
        active=${line#*\(}
        active=${active%%\)*}
        printf "mode=%s\n" "${active:-unknown}"
    '
}

restore_worker() {
    local rc=$?
    trap - EXIT INT TERM
    if [[ "${RESTORE_MODE}" == "1" && -n "${WORKER_IP}" ]]; then
        worker_ssh_script '
            mode=/sys/kernel/debug/sched/cgroup_mode
            if [ -e "$mode" ]; then
                printf "concur\n" > "$mode" 2>/dev/null || true
            fi
            root=/sys/fs/cgroup/sched-modes
            if [ -d "$root" ]; then
                find "$root" -depth -type d -exec rmdir {} + 2>/dev/null || true
            fi
            line=$(cat "$mode" 2>/dev/null || true)
            active=${line#*\(}
            active=${active%%\)*}
            printf "restored cgroup_mode: %s\n" "${active:-unknown}"
        ' >&2 || true
    fi
    exit "${rc}"
}

trap restore_worker EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -z "${CP_PORT}" ]]; then
    CP_PORT=$(discover_cp_port)
    CP_PORT=${CP_PORT:-20016}
fi
log "control plane ${CP_HOST}:${CP_PORT}"
cp_ssh true >/dev/null || die "cannot SSH to the control plane at ${CP_HOST}:${CP_PORT}"

discover_worker
copy_files
check_remote

if [[ "${ACTION}" == "setup" ]]; then
    worker_ssh_script "cd '${REMOTE_DIR}' && python3 run_matrix.py --check --out '${REMOTE_DIR}/check.jsonl' --meta '${REMOTE_DIR}/check-meta.json'"
    log "setup checks passed"
    exit 0
fi

if [[ "${ACTION}" == "plan" ]]; then
    worker_ssh_script "cd '${REMOTE_DIR}' && python3 run_matrix.py --plan --reps '${REPS}' --duration '${DURATION}' --warmup '${WARMUP}' --cooldown '${COOLDOWN}' --scenarios '${SCENARIOS}' --modes '${MODES}' --out /dev/null --meta /dev/null --run-id '${RUN_ID}'"
    exit 0
fi

RESTORE_MODE=1
mkdir -p "${RUN_DIR}"
log "run ${RUN_ID}: profile=${PROFILE} reps=${REPS} duration=${DURATION}s warmup=${WARMUP}s"
log "data: ${RUN_DIR}"
prepare_mode

set +e
worker_ssh_script "cd '${REMOTE_DIR}' && python3 run_matrix.py \
    --reps '${REPS}' \
    --duration '${DURATION}' \
    --warmup '${WARMUP}' \
    --cooldown '${COOLDOWN}' \
    --scenarios '${SCENARIOS}' \
    --modes '${MODES}' \
    --out '${REMOTE_DIR}/matrix.jsonl' \
    --meta '${REMOTE_DIR}/meta.json' \
    --run-id '${RUN_ID}' \
    --parent-weight '${PARENT_WEIGHT}'" 2>&1 | tee "${RUN_DIR}/run.log"
rc=${PIPESTATUS[0]}
set -e
if ((rc != 0)); then
    die "matrix runner failed with status ${rc}; log: ${RUN_DIR}/run.log"
fi

cp_ssh "ssh -i '${WORKER_KEY}' ${SSH_OPTS_REMOTE} 'root@${WORKER_IP}' 'cat ${REMOTE_DIR}/matrix.jsonl'" \
    >"${RUN_DIR}/matrix.jsonl"
cp_ssh "ssh -i '${WORKER_KEY}' ${SSH_OPTS_REMOTE} 'root@${WORKER_IP}' 'cat ${REMOTE_DIR}/meta.json'" \
    >"${RUN_DIR}/meta.json"
[[ -s "${RUN_DIR}/matrix.jsonl" ]] || die "pulled matrix.jsonl is empty"
[[ -s "${RUN_DIR}/meta.json" ]] || die "pulled meta.json is empty"

python3 - "${RUN_DIR}/run.json" "${RUN_ID}" "${WORKER}" "${WORKER_NODE}" \
    "${WORKER_IP}" "${WORKER_KERNEL}" "${PROFILE}" "${REPS}" "${DURATION}" \
    "${WARMUP}" "${COOLDOWN}" "${SCENARIOS}" "${MODES}" <<'PY'
import json
import sys

(
    path,
    run_id,
    worker,
    node,
    ip,
    kernel,
    profile,
    reps,
    duration,
    warmup,
    cooldown,
    scenarios,
    modes,
) = sys.argv[1:]
payload = {
    "run_id": run_id,
    "worker": worker,
    "node": node,
    "ip": ip,
    "kernel": kernel,
    "profile": profile,
    "reps": int(reps),
    "duration_s": float(duration),
    "warmup_s": float(warmup),
    "cooldown_s": float(cooldown),
    "scenarios": scenarios.split(","),
    "modes": modes.split(","),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

mkdir -p "${DATA_ROOT}"
ln -sfn "${RUN_DIR}" "${DATA_ROOT}/latest"
log "matrix: ${RUN_DIR}/matrix.jsonl"
log "next: task sched-core-modes-analyze"
