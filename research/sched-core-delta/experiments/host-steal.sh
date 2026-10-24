#!/usr/bin/env bash
# shellcheck disable=SC2312  # process and affinity probes are best-effort
# Create controlled host-side CPU contention for the steal-governor scenario.
#
# The lab worker's cloud-hypervisor vCPU threads float across the host CPUs.
# This helper pins them to a small set of physical CPUs and starts one spinner
# per chosen CPU, so the guest sees real steal time. It records the original
# affinity in /tmp/sched-core-delta-steal/state-<node>/ and restores it on
# --stop. Run --stop even if the guest experiment fails.
#
# Usage:
#   host-steal.sh --node k8labs-md-custom-... --start
#   host-steal.sh --node k8labs-md-custom-... --status
#   host-steal.sh --node k8labs-md-custom-... --stop
#
# The helper never reboots or reconfigures the VM. It only changes the
# affinity of that VM's vCPU threads and runs host CPU spinners until stopped.
set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

WORK_DIR=${WORK_DIR:-/tmp/sched-core-delta-steal}
SERIES_DIR=$(CDPATH=; cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
CPU_BURN_SRC="${SERIES_DIR}/experiments/cpu_burn.c"
CPU_BURN_BIN="${WORK_DIR}/cpu_burn"
NODE=${NODE:-}
CPUS=${CPUS:-2,4}
HOGS=${HOGS:-2}
DURATION=${DURATION:-300}
ACTION=""

log() {
    printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: host-steal.sh --node NAME --start|--stop|--status [OPTIONS]

Options:
  --node NAME        Target worker VM name (required)
  --cpus LIST        Host CPUs to pin the vCPUs and spinners to (default: 2,4)
  --hogs N           Number of host spinners (default: 2)
  --duration S       Safety lifetime of the spinners (default: 300)
  -h, --help         Show this help
EOF
}

while (($#)); do
    case "$1" in
        --node)
            NODE=${2:?--node requires a value}
            shift 2
            ;;
        --cpus)
            CPUS=${2:?--cpus requires a value}
            shift 2
            ;;
        --hogs)
            HOGS=${2:?--hogs requires a value}
            shift 2
            ;;
        --duration)
            DURATION=${2:?--duration requires a value}
            shift 2
            ;;
        --start)
            ACTION=start
            shift
            ;;
        --stop)
            ACTION=stop
            shift
            ;;
        --status)
            ACTION=status
            shift
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

[[ -n "${NODE}" ]] || die "--node is required"
[[ -n "${ACTION}" ]] || die "one of --start, --stop or --status is required"
[[ "${HOGS}" =~ ^[0-9]+$ ]] || die "--hogs must be an integer"
((HOGS >= 1)) || die "--hogs must be >= 1"
[[ "${DURATION}" =~ ^[0-9]+$ ]] || die "--duration must be an integer"
((DURATION >= 1)) || die "--duration must be >= 1"

STATE="${WORK_DIR}/state-${NODE}"
mkdir -p "${WORK_DIR}"

read_cpus() {
    IFS=',' read -r -a cpu_array <<<"${CPUS}"
    ((${#cpu_array[@]} > 0)) || die "--cpus is empty"
    for cpu in "${cpu_array[@]}"; do
        [[ "${cpu}" =~ ^[0-9]+$ ]] || die "invalid CPU: ${cpu}"
        [[ -d "/sys/devices/system/cpu/cpu${cpu}" ]] || die "CPU ${cpu} does not exist"
    done
}

find_pid() {
    pgrep -f "cloud-hypervisor.*api-socket path=/tmp/ch-capi/${NODE}/api.sock" | head -n1
}

find_vcpu_tids() {
    local pid=$1
    ps -T -p "${pid}" -o tid=,comm= | awk '$2 ~ /^vcpu/ {print $1}'
}

original_affinity() {
    local tid=$1
    taskset -pc "${tid}" 2>/dev/null | sed 's/.*: //'
}

start() {
    [[ ! -d "${STATE}" ]] || die "state already exists for ${NODE}; run --stop first"
    read_cpus
    local pid
    pid=$(find_pid)
    [[ -n "${pid}" ]] || die "cloud-hypervisor process for ${NODE} not found"

    mapfile -t tids < <(find_vcpu_tids "${pid}")
    ((${#tids[@]} >= 1)) || die "no vcpu threads found for ${NODE}"
    ((${#tids[@]} <= ${#cpu_array[@]})) ||
        log "warning: ${#tids[@]} vCPUs share ${#cpu_array[@]} host CPUs"

    if [[ ! -x "${CPU_BURN_BIN}" ]]; then
        gcc -O2 -std=gnu11 -pthread -o "${CPU_BURN_BIN}" "${CPU_BURN_SRC}"
    fi

    mkdir -p "${STATE}"
    printf '%s\n' "${pid}" >"${STATE}/pid"
    printf '%s\n' "${CPUS}" >"${STATE}/cpus"
    for i in "${!tids[@]}"; do
        tid=${tids[${i}]}
        affinity=$(original_affinity "${tid}")
        printf '%s %s\n' "${tid}" "${affinity}" >>"${STATE}/vcpus"
        cpu=${cpu_array[$((i % ${#cpu_array[@]}))]}
        taskset -pc "${cpu}" "${tid}" >/dev/null
    done

    : >"${STATE}/hogs"
    for ((i = 0; i < HOGS; i++)); do
        cpu=${cpu_array[$((i % ${#cpu_array[@]}))]}
        taskset -c "${cpu}" "${CPU_BURN_BIN}" \
            --threads 1 \
            --duration "${DURATION}" \
            --wait-file "${STATE}/go" \
            --output "${STATE}/hog-${i}.json" \
            >>"${STATE}/hog.log" 2>&1 &
        printf '%s\n' "$!" >>"${STATE}/hogs"
    done
    touch "${STATE}/go"
    log "pinned ${#tids[@]} vCPUs to ${CPUS} and started ${HOGS} spinners for ${NODE}"
    log "state: ${STATE}"
}

stop() {
    [[ -d "${STATE}" ]] || {
        log "no state for ${NODE}"
        return 0
    }
    if [[ -f "${STATE}/hogs" ]]; then
        while IFS= read -r hog; do
            [[ -n "${hog}" ]] || continue
            kill "${hog}" 2>/dev/null || true
        done <"${STATE}/hogs"
    fi
    if [[ -f "${STATE}/vcpus" ]]; then
        while IFS=' ' read -r tid affinity; do
            [[ -n "${tid}" && -n "${affinity}" ]] || continue
            taskset -pc "${affinity}" "${tid}" >/dev/null 2>&1 || true
        done <"${STATE}/vcpus"
    fi
    rm -rf "${STATE}"
    log "stopped host spinners and restored vCPU affinity for ${NODE}"
}

status() {
    [[ -d "${STATE}" ]] || {
        log "no state for ${NODE}"
        return 0
    }
    printf 'node: %s\n' "${NODE}"
    printf 'pid: %s\n' "$(cat "${STATE}/pid" 2>/dev/null || true)"
    printf 'cpus: %s\n' "$(cat "${STATE}/cpus" 2>/dev/null || true)"
    printf 'state: %s\n' "${STATE}"
    while IFS=' ' read -r tid affinity; do
        printf 'vcpu %s: original=%s current=%s\n' \
            "${tid}" "${affinity}" "$(original_affinity "${tid}")"
    done <"${STATE}/vcpus"
    while IFS= read -r hog; do
        [[ -n "${hog}" ]] || continue
        if kill -0 "${hog}" 2>/dev/null; then
            printf 'hog %s: running\n' "${hog}"
        else
            printf 'hog %s: not running\n' "${hog}"
        fi
    done <"${STATE}/hogs"
}

case "${ACTION}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *) die "unknown action: ${ACTION}" ;;
esac
