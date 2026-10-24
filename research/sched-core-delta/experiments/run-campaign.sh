#!/usr/bin/env bash
# shellcheck disable=SC2016  # remote scripts are intentionally single-quoted
# shellcheck disable=SC2310  # SSH helpers are checked explicitly at call sites
# shellcheck disable=SC2312  # timestamps and node lists are best-effort
# Run a balanced multi-block campaign across the stock and custom workers.
#
# Each block runs the same scenario matrix once per arm. The arm order
# alternates between blocks so position and time trends do not favour one
# kernel. The workers are separate VMs, so this is a cross-node campaign; the
# README documents the same-node confirmation procedure.
#
# Usage:
#   run-campaign.sh --dry-run --blocks 3
#   run-campaign.sh --blocks 3 --profile pilot
#   run-campaign.sh --blocks 5 --profile full
set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

SERIES_DIR=$(CDPATH=; cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
EXPERIMENTS_DIR="${SERIES_DIR}/experiments"
PROJECT_ROOT=$(CDPATH=; cd -- "${SERIES_DIR}/../.." && pwd)
KUBECONFIG=${KUBECONFIG:-${PROJECT_ROOT}/build/kubeconfig}
RUNNER=${RUNNER:-${EXPERIMENTS_DIR}/run-matrix.sh}
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
BLOCKS=${BLOCKS:-3}
PROFILE=${PROFILE:-pilot}
REPS=${REPS:-}
DURATION=${DURATION:-}
WARMUP=${WARMUP:-}
COOLDOWN=${COOLDOWN:-0.5}
SCENARIOS=${SCENARIOS:-wakeup,slice,fairness,quota,churn}
PARENT_WEIGHT=${PARENT_WEIGHT:-10000}
DATA_ROOT=${DATA_ROOT:-${SCHED_CORE_DELTA_DATA_ROOT:-${K8LABS_RESEARCH_DATA_ROOT:-${PROJECT_ROOT}/build/research}/sched-core-delta}}
DRY_RUN=0

log() {
    printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: run-campaign.sh [OPTIONS]

Options:
  --blocks N            Number of paired blocks (default: 3)
  --profile pilot|full  Sample size passed to each arm
  --run-id ID           Campaign identifier (default: UTC timestamp)
  --dry-run             Print the plan and exit
  --reps N              Override the profile repetition count
  --duration S          Override the measurement window
  --warmup S            Override the warmup window
  --scenarios LIST      Comma-separated scenario names
  -h, --help            Show this help
EOF
}

while (($#)); do
    case "$1" in
        --blocks)
            BLOCKS=${2:?--blocks requires a value}
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
              --dry-run)
            DRY_RUN=1
            shift
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
        --scenarios)
            SCENARIOS=${2:?--scenarios requires a value}
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

[[ "${BLOCKS}" =~ ^[0-9]+$ ]] || die "--blocks must be a positive integer"
((BLOCKS >= 1)) || die "--blocks must be >= 1"
case "${PROFILE}" in
    pilot | full) ;;
    *) die "unknown profile: ${PROFILE}" ;;
esac
[[ -x "${RUNNER}" ]] || die "runner is not executable: ${RUNNER}"

block_order() {
    local block=$1
    if ((block % 2 == 1)); then
        printf 'stock custom\n'
    else
        printf 'custom stock\n'
    fi
}

if ((DRY_RUN == 0)); then
    mkdir -p "${DATA_ROOT}/${RUN_ID}"
    {
        printf '{\n'
        printf '  "run_id": "%s",\n' "${RUN_ID}"
        printf '  "blocks": %d,\n' "${BLOCKS}"
        printf '  "profile": "%s",\n' "${PROFILE}"
        printf '  "scenarios": "%s",\n' "${SCENARIOS}"
        printf '  "data_root": "%s",\n' "${DATA_ROOT}"
        printf '  "block_orders": {\n'
        for ((block = 1; block <= BLOCKS; block++)); do
            IFS=' ' read -r first second <<<"$(block_order "${block}")"
            printf '    "block-%02d": ["%s", "%s"]%s\n' "${block}" "${first}" "${second}" \
                "$([[ ${block} -lt ${BLOCKS} ]] && printf ',' || true)"
        done
        printf '  }\n'
        printf '}\n'
    } >"${DATA_ROOT}/${RUN_ID}/campaign.json"
fi

log "campaign ${RUN_ID}: blocks=${BLOCKS} profile=${PROFILE} scenarios=${SCENARIOS}"
for ((block = 1; block <= BLOCKS; block++)); do
    IFS=' ' read -r first second <<<"$(block_order "${block}")"
    block_id=$(printf '%02d' "${block}")
    for arm in "${first}" "${second}"; do
        args=(
            --arm "${arm}"
            --block "${block_id}"
            --profile "${PROFILE}"
            --run-id "${RUN_ID}"
            --scenarios "${SCENARIOS}"
            --cooldown "${COOLDOWN}"
            --parent-weight "${PARENT_WEIGHT}"
        )
        if [[ -n "${REPS}" ]]; then
            args+=(--reps "${REPS}")
        fi
        if [[ -n "${DURATION}" ]]; then
            args+=(--duration "${DURATION}")
        fi
        if [[ -n "${WARMUP}" ]]; then
            args+=(--warmup "${WARMUP}")
        fi
        if ((DRY_RUN == 1)); then
            printf 'block-%s %s: %q ' "${block_id}" "${arm}" "${RUNNER}"
            printf '%q ' "${args[@]}"
            printf '\n'
            continue
        fi
        log "block-${block_id} ${arm}"
        DATA_ROOT="${DATA_ROOT}" "${RUNNER}" "${args[@]}"
    done
done
log "campaign ${RUN_ID} complete: ${DATA_ROOT}/${RUN_ID}"
