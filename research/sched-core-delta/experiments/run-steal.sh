#!/usr/bin/env bash
# shellcheck disable=SC2016  # remote scripts are intentionally single-quoted
# shellcheck disable=SC2310  # SSH helpers are checked explicitly at call sites
# shellcheck disable=SC2312  # timestamps and node lists are best-effort
# Run the steal-governor contrast under controlled host CPU contention.
#
# For every block the two custom arms run with the governor unloaded and
# loaded. The host helper pins the custom worker's vCPUs to a small CPU set and
# starts one spinner per CPU for the duration. The helper is always stopped in
# an EXIT trap.
set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

SERIES_DIR=$(CDPATH=; cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
EXPERIMENTS_DIR="${SERIES_DIR}/experiments"
PROJECT_ROOT=$(CDPATH=; cd -- "${SERIES_DIR}/../.." && pwd)
KUBECONFIG=${KUBECONFIG:-${PROJECT_ROOT}/build/kubeconfig}
RUNNER=${RUNNER:-${EXPERIMENTS_DIR}/run-matrix.sh}
HOST_STEAL=${HOST_STEAL:-${EXPERIMENTS_DIR}/host-steal.sh}
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
BLOCKS=${BLOCKS:-3}
REPS=${REPS:-3}
DURATION=${DURATION:-8}
WARMUP=${WARMUP:-2}
NODE=${NODE:-}
CPUS=${CPUS:-2,4}
HOGS=${HOGS:-2}
GOV_INTERVAL_MS=${GOV_INTERVAL_MS:-1000}
GOV_HIGH=${GOV_HIGH:-500}
GOV_LOW=${GOV_LOW:-200}
DATA_ROOT=${DATA_ROOT:-${SCHED_CORE_DELTA_DATA_ROOT:-${K8LABS_RESEARCH_DATA_ROOT:-${PROJECT_ROOT}/build/research}/sched-core-delta}}
STOPPED=0

log() {
    printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: run-steal.sh [OPTIONS]

Options:
  --node NAME           Custom worker name (default: discover *-md-custom-*)
  --blocks N            Paired blocks (default: 3)
  --reps N              Repetitions per arm (default: 3)
  --duration S          Measurement window (default: 8)
  --warmup S            Warmup window (default: 2)
  --run-id ID           Run identifier
  --cpus LIST           Host CPUs for the vCPUs and spinners (default: 2,4)
  --hogs N              Host spinners (default: 2)
  --gov-high N          Governor high threshold (default: 500 = 5%)
  --gov-low N           Governor low threshold (default: 200 = 2%)
  --gov-interval-ms N   Governor sampling interval (default: 1000)
  -h, --help            Show this help
EOF
}

while (($#)); do
    case "$1" in
        --node)
            NODE=${2:?--node requires a value}
            shift 2
            ;;
        --blocks)
            BLOCKS=${2:?--blocks requires a value}
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
        --run-id)
            RUN_ID=${2:?--run-id requires a value}
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
        --gov-high)
            GOV_HIGH=${2:?--gov-high requires a value}
            shift 2
            ;;
        --gov-low)
            GOV_LOW=${2:?--gov-low requires a value}
            shift 2
            ;;
        --gov-interval-ms)
            GOV_INTERVAL_MS=${2:?--gov-interval-ms requires a value}
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

[[ -x "${RUNNER}" ]] || die "runner is not executable: ${RUNNER}"
[[ -x "${HOST_STEAL}" ]] || die "host-steal helper is not executable: ${HOST_STEAL}"
if [[ ! "${BLOCKS}" =~ ^[0-9]+$ ]] || ((BLOCKS < 1)); then
    die "--blocks must be >= 1"
fi

if [[ -z "${NODE}" ]]; then
    NODE=$(kubectl --kubeconfig "${KUBECONFIG}" get nodes \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' |
        awk '/-md-custom-/ {print; exit}')
fi
[[ -n "${NODE}" ]] || die "could not discover a custom worker; pass --node"

mkdir -p "${DATA_ROOT}/${RUN_ID}"
python3 - "${DATA_ROOT}/${RUN_ID}/campaign.json" "${RUN_ID}" "${BLOCKS}" \
    "${REPS}" "${DURATION}" "${WARMUP}" "${NODE}" "${CPUS}" "${HOGS}" \
    "${GOV_INTERVAL_MS}" "${GOV_HIGH}" "${GOV_LOW}" "${DATA_ROOT}" <<'PYJSON'
import json
import sys

(
    path,
    run_id,
    blocks,
    reps,
    duration,
    warmup,
    node,
    cpus,
    hogs,
    gov_interval,
    gov_high,
    gov_low,
    data_root,
) = sys.argv[1:]
payload = {
    "run_id": run_id,
    "blocks": int(blocks),
    "profile": "steal",
    "scenarios": "steal",
    "contrast": "custom-gov vs custom-nogov",
    "reps": int(reps),
    "duration_s": float(duration),
    "warmup_s": float(warmup),
    "node": node,
    "cpus": cpus,
    "hogs": int(hogs),
    "governor": {
        "interval_ms": int(gov_interval),
        "high_threshold": int(gov_high),
        "low_threshold": int(gov_low),
    },
    "data_root": data_root,
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PYJSON

stop_host_steal() {
    if ((STOPPED == 0)); then
        STOPPED=1
        "${HOST_STEAL}" --node "${NODE}" --stop || true
    fi
}
trap stop_host_steal EXIT INT TERM

STEAL_SECONDS=$((REPS * (DURATION + WARMUP + 5) + 120))
if ((STEAL_SECONDS < 300)); then
    STEAL_SECONDS=300
fi
log "steal campaign ${RUN_ID}: node=${NODE} blocks=${BLOCKS} reps=${REPS} duration=${DURATION}s"
for ((block = 1; block <= BLOCKS; block++)); do
    block_id=$(printf '%02d' "${block}")
    if ((block % 2 == 1)); then
        arms=(custom-nogov custom-gov)
    else
        arms=(custom-gov custom-nogov)
    fi
    for arm in "${arms[@]}"; do
        log "block-${block_id} ${arm}: starting host steal"
        STOPPED=0
        "${HOST_STEAL}" --node "${NODE}" --cpus "${CPUS}" --hogs "${HOGS}" \
            --duration "${STEAL_SECONDS}" --start
        log "block-${block_id} ${arm}: running guest matrix"
        DATA_ROOT="${DATA_ROOT}" "${RUNNER}" \
            --arm "${arm}" \
            --block "${block_id}" \
            --run-id "${RUN_ID}" \
            --scenarios steal \
            --reps "${REPS}" \
            --duration "${DURATION}" \
            --warmup "${WARMUP}" \
            --gov-interval-ms "${GOV_INTERVAL_MS}" \
            --gov-high "${GOV_HIGH}" \
            --gov-low "${GOV_LOW}"
        stop_host_steal
    done
done
trap - EXIT INT TERM
log "steal campaign ${RUN_ID} complete"
