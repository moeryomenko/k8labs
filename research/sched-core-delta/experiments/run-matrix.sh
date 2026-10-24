#!/usr/bin/env bash
# shellcheck disable=SC2016  # remote scripts are intentionally single-quoted
# shellcheck disable=SC2310  # SSH helpers are checked explicitly at call sites
# shellcheck disable=SC2312  # timestamps and node lists are best-effort
# Run one stock-versus-custom scheduler delta block on one k8labs worker.
#
# The worker is reachable only through the control-plane VM: the host holds the
# control-plane key, and the control plane carries the worker key at
# /tmp/id_ed25519. This script copies the experiment files through that jump
# host, runs the matrix as root on the worker, and pulls the JSONL result back.
#
# Usage:
#   run-matrix.sh --check --arm custom
#   run-matrix.sh --plan --arm stock --scenarios wakeup,quota
#   run-matrix.sh --arm custom --block 1 --profile pilot
#   run-matrix.sh --arm custom-gov --scenarios steal --block 1
#
# Environment overrides:
#   KUBECONFIG, DATA_ROOT, RUN_ID, REPS, DURATION, WARMUP, COOLDOWN,
#   SCENARIOS, ARM, BLOCK, PARENT_WEIGHT, CP_HOST, CP_PORT, CP_KEY,
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
REMOTE_DIR=${REMOTE_DIR:-/var/tmp/sched-core-delta}
ARM=${ARM:-custom}
BLOCK=${BLOCK:-01}
PROFILE=${PROFILE:-full}
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
SCENARIOS=${SCENARIOS:-wakeup,slice,fairness,quota,churn}
PARENT_WEIGHT=${PARENT_WEIGHT:-10000}
COOLDOWN=${COOLDOWN:-0.5}
GOV_INTERVAL_MS=${GOV_INTERVAL_MS:-1000}
GOV_HIGH=${GOV_HIGH:-500}
GOV_LOW=${GOV_LOW:-200}
DATA_ROOT=${DATA_ROOT:-${SCHED_CORE_DELTA_DATA_ROOT:-${K8LABS_RESEARCH_DATA_ROOT:-${PROJECT_ROOT}/build/research}/sched-core-delta}}
ACTION=run
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
  --check                 Check the worker prerequisites for the arm
  --plan                  Print the seeded plan without running it
  --arm ARM               stock, custom, custom-nogov or custom-gov
  --block ID              Block identifier (default: 01)
  --profile pilot|full    Sample size (pilot: 1 rep x 3 s; full: 3 reps x 8 s)
  --run-id ID             Run identifier (default: UTC timestamp)
  --reps N                Override the profile repetition count
  --duration S            Override the measurement window
  --warmup S              Override the warmup window
  --cooldown S            Delay between scenarios
  --scenarios LIST        Comma-separated scenario names
  --parent-weight N       cpu.weight of the test parent (default: 10000)
  --gov-interval-ms N     Steal-governor sampling interval (default: 1000)
  --gov-high N            Steal-governor high threshold (default: 500)
  --gov-low N             Steal-governor low threshold (default: 200)
  -h, --help              Show this help
EOF
}

while (($#)); do
    case "$1" in
        --check)
            ACTION=check
            shift
            ;;
        --plan)
            ACTION=plan
            shift
            ;;
        --arm)
            ARM=${2:?--arm requires a value}
            shift 2
            ;;
        --block)
            BLOCK=${2:?--block requires a value}
            shift 2
            ;;
        --profile)
            PROFILE=${2:?--profile requires a value}
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
        --parent-weight)
            PARENT_WEIGHT=${2:?--parent-weight requires a value}
            shift 2
            ;;
        --gov-interval-ms)
            GOV_INTERVAL_MS=${2:?--gov-interval-ms requires a value}
            shift 2
            ;;
        --gov-high)
            GOV_HIGH=${2:?--gov-high requires a value}
            shift 2
            ;;
        --gov-low)
            GOV_LOW=${2:?--gov-low requires a value}
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
case "${ARM}" in
    stock | custom | custom-nogov | custom-gov) ;;
    *) die "unknown arm: ${ARM}" ;;
esac

RUN_DIR=${DATA_ROOT}/${RUN_ID}/block-${BLOCK}/${ARM}

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

    case "${ARM}" in
        custom | custom-nogov | custom-gov)
            selected="${custom}"
            [[ -n "${selected}" ]] || die "no custom worker (name contains -md-custom-) found"
            ;;
        stock)
            selected="${stock}"
            [[ -n "${selected}" ]] || die "no stock worker (name contains -md-0-) found"
            ;;
        *)
            die "unknown arm: ${ARM}"
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
    log "worker ${WORKER_NODE} (${WORKER_IP}) arm=${ARM}"
}

copy_files() {
    worker_ssh_script "mkdir -p '${REMOTE_DIR}'"
    tar -C "${EXPERIMENTS_DIR}" -cf - \
        scenarios.py run_matrix.py cpu_burn.c wakeup_latency.c \
        periodic_burn.c task_churn.c pingpong.c \
        | cp_ssh "ssh -i '${WORKER_KEY}' ${SSH_OPTS_REMOTE} 'root@${WORKER_IP}' 'tar -C ${REMOTE_DIR} --touch -xf -'"
}

check_remote() {
    local kernel
    kernel=$(worker_ssh_script 'uname -r' | tr -d '[:space:]')
    [[ -n "${kernel}" ]] || die "could not read the worker kernel"
    WORKER_KERNEL="${kernel}"

    case "${ARM}" in
        stock)
            [[ "${WORKER_KERNEL}" != *k8labs* ]] || die "stock arm is on ${WORKER_KERNEL}"
            ;;
        custom | custom-nogov | custom-gov)
            [[ "${WORKER_KERNEL}" == *k8labs* ]] || die "custom arm is on ${WORKER_KERNEL}"
            ;;
        *)
            die "unknown arm: ${ARM}"
            ;;
    esac

    cp_ssh "test -f '${WORKER_KEY}'" || die "worker key ${WORKER_KEY} is missing on the control plane"
    if ! cp_ssh "ssh -i '${WORKER_KEY}' ${SSH_OPTS_REMOTE} 'root@${WORKER_IP}' true" 2>/dev/null; then
        die "cannot SSH from the control plane to ${WORKER_NODE} (${WORKER_IP})"
    fi
    log "kernel ${WORKER_KERNEL}"
}

if [[ -z "${CP_PORT}" ]]; then
    CP_PORT=$(discover_cp_port)
    CP_PORT=${CP_PORT:-20016}
fi
log "control plane ${CP_HOST}:${CP_PORT}"
cp_ssh true >/dev/null || die "cannot SSH to the control plane at ${CP_HOST}:${CP_PORT}"

discover_worker
copy_files
check_remote

if [[ "${ACTION}" == "check" ]]; then
    worker_ssh_script "cd '${REMOTE_DIR}' && python3 run_matrix.py --check --arm '${ARM}' --out /dev/null --meta /dev/null"
    log "check passed"
    exit 0
fi

if [[ "${ACTION}" == "plan" ]]; then
    worker_ssh_script "cd '${REMOTE_DIR}' && python3 run_matrix.py --plan --arm '${ARM}' --block '${BLOCK}' --reps '${REPS}' --duration '${DURATION}' --warmup '${WARMUP}' --cooldown '${COOLDOWN}' --scenarios '${SCENARIOS}' --parent-weight '${PARENT_WEIGHT}' --out /dev/null --meta /dev/null --run-id '${RUN_ID}'"
    exit 0
fi

mkdir -p "${RUN_DIR}"
log "run ${RUN_ID}: arm=${ARM} block=${BLOCK} profile=${PROFILE} reps=${REPS} duration=${DURATION}s"
log "data: ${RUN_DIR}"

set +e
worker_ssh_script "cd '${REMOTE_DIR}' && python3 run_matrix.py \
    --arm '${ARM}' \
    --block '${BLOCK}' \
    --reps '${REPS}' \
    --duration '${DURATION}' \
    --warmup '${WARMUP}' \
    --cooldown '${COOLDOWN}' \
    --scenarios '${SCENARIOS}' \
    --parent-weight '${PARENT_WEIGHT}' \
    --gov-interval-ms '${GOV_INTERVAL_MS}' \
    --gov-high '${GOV_HIGH}' \
    --gov-low '${GOV_LOW}' \
    --out '${REMOTE_DIR}/matrix.jsonl' \
    --meta '${REMOTE_DIR}/meta.json' \
    --run-id '${RUN_ID}'" 2>&1 | tee "${RUN_DIR}/run.log"
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

python3 - "${RUN_DIR}/run.json" "${RUN_ID}" "${ARM}" "${BLOCK}" "${WORKER_NODE}" \
    "${WORKER_IP}" "${WORKER_KERNEL}" "${PROFILE}" "${REPS}" "${DURATION}" \
    "${WARMUP}" "${COOLDOWN}" "${SCENARIOS}" "${PARENT_WEIGHT}" <<'PY'
import json
import sys

(
    path,
    run_id,
    arm,
    block,
    node,
    ip,
    kernel,
    profile,
    reps,
    duration,
    warmup,
    cooldown,
    scenarios,
    parent_weight,
) = sys.argv[1:]
payload = {
    "run_id": run_id,
    "arm": arm,
    "block": block,
    "node": node,
    "ip": ip,
    "kernel": kernel,
    "profile": profile,
    "reps": int(reps),
    "duration_s": float(duration),
    "warmup_s": float(warmup),
    "cooldown_s": float(cooldown),
    "scenarios": scenarios.split(","),
    "parent_weight": int(parent_weight),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
log "wrote ${RUN_DIR}/run.json"
