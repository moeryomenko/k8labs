#!/usr/bin/env bash
#
# Capture one bounded scheduler trace slice inside a CPU limits collector Pod.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROBE_DIR="${SCRIPT_DIR}/probes"

collector_pod=""
collector_container=""
namespace="cpu-limits"
kubeconfig="${KUBECONFIG:-}"
duration=""
out_dir=""
remote_dir="/var/tmp/cpu-limits-capture"
host_root="/host-sys"
remote_prepared=0
capture_pid=""
artifacts_copied=0

usage() {
    cat <<'EOF'
Usage: capture-cell.sh --collector-pod POD --duration SECONDS --out-dir DIR [OPTIONS]

Capture scheduler and CFS throttle traces inside the collector Pod.

Required:
  --collector-pod POD       Collector Pod on the target node
  --duration SECONDS        Positive bounded capture duration
  --out-dir DIR             Empty destination for raw files

Options:
  --namespace NAME          Collector namespace (default: cpu-limits)
  --container NAME          Collector container name
  --kubeconfig PATH         Kubeconfig path (default: KUBECONFIG)
  --remote-dir PATH         Collector work directory
  --host-root PATH          Host sysfs mount in the collector (default: /host-sys)
  -h, --help                Show this help
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

warn() {
    printf 'WARN: %s\n' "$*" >&2
}

kubectl_base() {
    local -a command=(kubectl)
    if [[ -n "${kubeconfig}" ]]; then
        command+=(--kubeconfig "${kubeconfig}")
    fi
    command+=(-n "${namespace}")
    command "${command[@]}" "$@"
}

collector_exec() {
    local -a container_arg=()
    if [[ -n "${collector_container}" ]]; then
        container_arg=(-c "${collector_container}")
    fi
    kubectl_base exec "${collector_pod}" "${container_arg[@]}" -- "$@"
}

collector_copy() {
    local source="$1"
    local destination="$2"
    local -a container_arg=()
    if [[ -n "${collector_container}" ]]; then
        container_arg=(--container "${collector_container}")
    fi
    kubectl_base cp "${container_arg[@]}" "${source}" "${destination}"
}

stop_and_restore_node_state() {
    (( remote_prepared )) || return 0

    # shellcheck disable=SC2016,SC2310 # The collector shell expands this best-effort cleanup script.
    collector_exec env "REMOTE_DIR=${remote_dir}" sh -cu '
        for name in sched-trace throttle perf-sched; do
            pid_file="${REMOTE_DIR}/${name}.pid"
            [ -r "${pid_file}" ] || continue
            pid=$(cat "${pid_file}" 2>/dev/null || true)
            case "${pid}" in
                ""|*[!0-9]*) continue ;;
            esac
            kill -INT "${pid}" 2>/dev/null || true
        done
        sleep 1
        if [ -r "${REMOTE_DIR}/state/tracefs-path" ] && \
           [ -r "${REMOTE_DIR}/state/tracing-on.before" ]; then
            tracefs_path=$(cat "${REMOTE_DIR}/state/tracefs-path")
            tracing_on=$(cat "${REMOTE_DIR}/state/tracing-on.before")
            printf "%s" "${tracing_on}" > "${tracefs_path}" 2>/dev/null || true
        fi
    ' >/dev/null 2>&1 || warn "could not fully stop capture or restore tracefs state"
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    stop_and_restore_node_state
    if [[ -n "${capture_pid}" ]] && kill -0 "${capture_pid}" 2>/dev/null; then
        kill -TERM "${capture_pid}" 2>/dev/null || true
        wait "${capture_pid}" 2>/dev/null || true
    fi
    if (( artifacts_copied )); then
        # shellcheck disable=SC2016,SC2310 # The collector shell expands this best-effort cleanup command.
        collector_exec env "REMOTE_DIR=${remote_dir}" sh -cu \
            'rm -rf -- "${REMOTE_DIR}"' >/dev/null 2>&1 || \
            warn "could not remove collector work directory"
    fi
    exit "${status}"
}

parse_args() {
    while (( $# > 0 )); do
        case "$1" in
            --collector-pod)
                collector_pod="${2:?--collector-pod requires a value}"
                shift 2
                ;;
            --container)
                collector_container="${2:?--container requires a value}"
                shift 2
                ;;
            --namespace)
                namespace="${2:?--namespace requires a value}"
                shift 2
                ;;
            --kubeconfig)
                kubeconfig="${2:?--kubeconfig requires a value}"
                shift 2
                ;;
            --duration)
                duration="${2:?--duration requires a value}"
                shift 2
                ;;
            --out-dir)
                out_dir="${2:?--out-dir requires a value}"
                shift 2
                ;;
            --remote-dir)
                remote_dir="${2:?--remote-dir requires a value}"
                shift 2
                ;;
            --host-root)
                host_root="${2:?--host-root requires a value}"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "unknown argument: $1"
                ;;
        esac
    done
}

validate_args() {
    [[ -n "${collector_pod}" ]] || die "--collector-pod is required"
    [[ -n "${duration}" ]] || die "--duration is required"
    [[ -n "${out_dir}" ]] || die "--out-dir is required"
    [[ "${duration}" =~ ^[1-9][0-9]*$ ]] || die "--duration must be a positive integer"
    [[ "${remote_dir}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "--remote-dir contains unsafe characters"
    [[ "${host_root}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "--host-root contains unsafe characters"
    [[ "${remote_dir}" != *".."* ]] || die "--remote-dir must not contain .."
    [[ "${host_root}" != *".."* ]] || die "--host-root must not contain .."
    [[ -f "${PROBE_DIR}/sched-trace.bt" ]] || die "missing sched-trace probe"
    [[ -f "${PROBE_DIR}/throttle.bt" ]] || die "missing throttle probe"
    [[ ! -e "${out_dir}" ]] || die "--out-dir already exists: ${out_dir}"
    command -v kubectl >/dev/null 2>&1 || die "kubectl is required"
}

prepare_collector() {
    # shellcheck disable=SC2016 # The collector shell expands the staged setup script.
    collector_exec env "REMOTE_DIR=${remote_dir}" "HOST_ROOT=${host_root}" sh -ceu '
        umask 077
        rm -rf -- "${REMOTE_DIR}"
        mkdir -p "${REMOTE_DIR}/raw" "${REMOTE_DIR}/state"
        command -v bpftrace >/dev/null
        command -v perf >/dev/null

        for candidate in \
            "${HOST_ROOT}/kernel/tracing/tracing_on" \
            "${HOST_ROOT}/kernel/debug/tracing/tracing_on" \
            /sys/kernel/tracing/tracing_on \
            /sys/kernel/debug/tracing/tracing_on; do
            if [ -r "${candidate}" ] && [ -w "${candidate}" ]; then
                printf "%s\n" "${candidate}" > "${REMOTE_DIR}/state/tracefs-path"
                cat "${candidate}" > "${REMOTE_DIR}/state/tracing-on.before"
                break
            fi
        done
    '
    remote_prepared=1

    collector_copy "${PROBE_DIR}/sched-trace.bt" \
        "${namespace}/${collector_pod}:${remote_dir}/sched-trace.bt"
    collector_copy "${PROBE_DIR}/throttle.bt" \
        "${namespace}/${collector_pod}:${remote_dir}/throttle.bt"
}

run_capture() {
    # shellcheck disable=SC2016 # The collector shell expands the bounded capture script.
    collector_exec env "REMOTE_DIR=${remote_dir}" "DURATION=${duration}" sh -ceu '
        bpftrace "${REMOTE_DIR}/sched-trace.bt" \
            > "${REMOTE_DIR}/raw/sched-trace.csv" \
            2> "${REMOTE_DIR}/raw/sched-trace.err" &
        sched_pid=$!
        printf "%s\n" "${sched_pid}" > "${REMOTE_DIR}/sched-trace.pid"

        bpftrace "${REMOTE_DIR}/throttle.bt" \
            > "${REMOTE_DIR}/raw/throttle.csv" \
            2> "${REMOTE_DIR}/raw/throttle.err" &
        throttle_pid=$!
        printf "%s\n" "${throttle_pid}" > "${REMOTE_DIR}/throttle.pid"

        perf sched record -o "${REMOTE_DIR}/raw/perf.data" -- sleep "${DURATION}" \
            > "${REMOTE_DIR}/raw/perf-sched.out" \
            2> "${REMOTE_DIR}/raw/perf-sched.err" &
        perf_pid=$!
        printf "%s\n" "${perf_pid}" > "${REMOTE_DIR}/perf-sched.pid"

        sleep "${DURATION}"
        for child in "${sched_pid}" "${throttle_pid}" "${perf_pid}"; do
            kill -INT "${child}" 2>/dev/null || true
        done
        for child in "${sched_pid}" "${throttle_pid}" "${perf_pid}"; do
            wait "${child}" || true
        done

        perf script -i "${REMOTE_DIR}/raw/perf.data" \
            > "${REMOTE_DIR}/raw/perf.script" \
            2> "${REMOTE_DIR}/raw/perf-script.err" || true
    '
}

copy_and_check_artifacts() {
    local raw_dir="${out_dir}/raw"
    local sched_records throttle_records perf_records

    mkdir -p "${raw_dir}"
    collector_copy "${namespace}/${collector_pod}:${remote_dir}/raw/." "${raw_dir}"
    artifacts_copied=1

    sched_records="$(awk 'NR > 1 { print 1; exit }' "${raw_dir}/sched-trace.csv" 2>/dev/null || true)"
    throttle_records="$(awk 'NR > 1 { print 1; exit }' "${raw_dir}/throttle.csv" 2>/dev/null || true)"
    perf_records="$(awk 'NF { print 1; exit }' "${raw_dir}/perf.script" 2>/dev/null || true)"
    if [[ -z "${sched_records}" && -z "${throttle_records}" && -z "${perf_records}" ]]; then
        die "capture produced no scheduler, throttle, or perf events"
    fi
}

main() {
    parse_args "$@"
    validate_args
    trap cleanup EXIT
    trap 'exit 130' INT TERM

    prepare_collector
    run_capture &
    capture_pid=$!
    wait "${capture_pid}"
    capture_pid=""
    copy_and_check_artifacts
    printf 'capture complete: %s\n' "${out_dir}"
}

main "$@"
