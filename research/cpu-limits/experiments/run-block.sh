#!/usr/bin/env bash
# Run frozen CPU-limits cells in their seeded block order.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "${script_dir}/../../.." && pwd -P)"
phase="smoke"
workload=""
condition=""
blocks=1
seed=20261024
trace_cells=""
data_root="${K8LABS_RESEARCH_DATA_ROOT:-${repo_root}/build/research}/cpu-limits"
node=""
collector_url=""
collector_pod=""
namespace="cpu-limits"
kubeconfig="${KUBECONFIG:-}"
ready_timeout=120
dry_run=0
run_id="cpu-limits-$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
    cat <<'EOF'
Usage: run-block.sh --node NODE --collector-url URL --collector-pod POD [OPTIONS]

Run the frozen seeded CPU-limits schedule. Without filters, each requested block
contains all 36 cells. A workload or condition filter is for a bounded smoke run.

Options:
  --phase smoke|pilot|confirmatory  Campaign phase (default: smoke)
  --workload ID                     Run only one frozen workload
  --condition solo|contention       Run only one frozen condition
  --blocks COUNT                    Complete seeded blocks (default: 1)
  --trace-cells LIST                Comma-separated cell IDs, or all
  --data-root PATH                  Output root
  --node NAME                       Selected worker node
  --collector-url URL               Collector /metrics URL
  --collector-pod POD               Collector Pod on the selected node
  --namespace NAME                  Namespace (default: cpu-limits)
  --kubeconfig PATH                 Kubeconfig path
  --ready-timeout SECONDS           Pod readiness timeout
  --dry-run                         Print the selected schedule only
  --run-id ID                       Stable run identifier
  --seed NUMBER                     Deterministic schedule seed
  -h, --help                        Show this help
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

parse_args() {
    while (( $# > 0 )); do
        case "$1" in
            --phase) phase="${2:?--phase requires a value}"; shift 2 ;;
            --workload) workload="${2:?--workload requires a value}"; shift 2 ;;
            --condition) condition="${2:?--condition requires a value}"; shift 2 ;;
            --blocks) blocks="${2:?--blocks requires a value}"; shift 2 ;;
            --trace-cells) trace_cells="${2:?--trace-cells requires a value}"; shift 2 ;;
            --data-root) data_root="${2:?--data-root requires a value}"; shift 2 ;;
            --node) node="${2:?--node requires a value}"; shift 2 ;;
            --collector-url) collector_url="${2:?--collector-url requires a value}"; shift 2 ;;
            --collector-pod) collector_pod="${2:?--collector-pod requires a value}"; shift 2 ;;
            --namespace) namespace="${2:?--namespace requires a value}"; shift 2 ;;
            --kubeconfig) kubeconfig="${2:?--kubeconfig requires a value}"; shift 2 ;;
            --ready-timeout) ready_timeout="${2:?--ready-timeout requires a value}"; shift 2 ;;
            --dry-run) dry_run=1; shift ;;
            --run-id) run_id="${2:?--run-id requires a value}"; shift 2 ;;
            --seed) seed="${2:?--seed requires a value}"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
}

validate_args() {
    case "${phase}" in smoke|pilot|confirmatory) ;; *) die "--phase is invalid: ${phase}" ;; esac
    [[ "${blocks}" =~ ^[1-9][0-9]*$ ]] || die "--blocks must be a positive integer"
    [[ "${ready_timeout}" =~ ^[1-9][0-9]*$ ]] || die "--ready-timeout must be a positive integer"
    [[ "${seed}" =~ ^-?[0-9]+$ ]] || die "--seed must be an integer"
    [[ -n "${node}" ]] || die "--node is required"
    [[ -n "${collector_url}" ]] || die "--collector-url is required"
    [[ -n "${collector_pod}" ]] || die "--collector-pod is required"
    [[ -x "${script_dir}/run-cell.py" || -f "${script_dir}/run-cell.py" ]] || die "run-cell.py is missing"

    if [[ -n "${workload}" ]]; then
        python3 "${script_dir}/design.py" --validate >/dev/null
        case "${workload}" in cpu-bound|io-bound|memory-bound|hybrid) ;; *) die "unknown workload: ${workload}" ;; esac
    fi
    if [[ -n "${condition}" ]]; then
        case "${condition}" in solo|contention) ;; *) die "unknown condition: ${condition}" ;; esac
    fi
    if [[ "${phase}" == "confirmatory" ]]; then
        [[ -f "${data_root}/policies.json" ]] || die "confirmatory phase requires ${data_root}/policies.json"
        [[ -f "${data_root}/sample-size.json" ]] || die "confirmatory phase requires ${data_root}/sample-size.json"
    fi
}

is_trace_cell() {
    local cell_id="$1"
    [[ -n "${trace_cells}" ]] || return 1
    [[ "${trace_cells}" == "all" ]] && return 0
    local item
    IFS=',' read -r -a items <<< "${trace_cells}"
    for item in "${items[@]}"; do
        [[ "${item}" == "${cell_id}" ]] && return 0
    done
    return 1
}

emit_schedule() {
    python3 - "${script_dir}/design.py" "${blocks}" "${seed}" "${workload}" "${condition}" <<'PY'
import importlib.util
import sys
from pathlib import Path

path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("cpu_limits_design", path)
if spec is None or spec.loader is None:
    raise SystemExit("ERROR: cannot load frozen design")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
for block_number, block in enumerate(module.schedule(int(sys.argv[2]), int(sys.argv[3])), start=1):
    for position, cell in enumerate(block, start=1):
        if sys.argv[4] and cell.workload != sys.argv[4]:
            continue
        if sys.argv[5] and cell.condition != sys.argv[5]:
            continue
        print(f"{block_number}\t{position}\t{cell.cell_id}")
PY
}

main() {
    parse_args "$@"
    validate_args
    local block_id position cell_id
    while IFS=$'\t' read -r block_id position cell_id; do
        [[ -n "${cell_id}" ]] || continue
        if (( dry_run )); then
            printf 'block=%s position=%s cell=%s\n' "${block_id}" "${position}" "${cell_id}"
            continue
        fi
        command=(
            python3 "${script_dir}/run-cell.py"
            --phase "${phase}"
            --cell-id "${cell_id}"
            --run-id "${run_id}"
            --block-id "${block_id}"
            --position "${position}"
            --seed "${seed}"
            --node "${node}"
            --data-root "${data_root}"
            --collector-url "${collector_url}"
            --collector-pod "${collector_pod}"
            --namespace "${namespace}"
            --ready-timeout "${ready_timeout}"
        )
        if [[ -n "${kubeconfig}" ]]; then command+=(--kubeconfig "${kubeconfig}"); fi
        if is_trace_cell "${cell_id}"; then command+=(--trace); fi
        "${command[@]}"
    done < <(emit_schedule)
}

main "$@"
