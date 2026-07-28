#!/usr/bin/env bash
#
# cgroup-pid-watch.sh — Watch per-task schedstat for pod container PIDs
#
# Usage:
#   cgroup-pid-watch.sh <pod-name> [--interval N] [--count N]
#   cgroup-pid-watch.sh --help
#
# Polls per-task /proc/<pid>/sched and /proc/<pid>/schedstat for all container
# PIDs in a pod at a configurable interval. Outputs CSV suitable for time-series
# plotting of EEVDF scheduling statistics.
#
# Dependencies: kubectl, tofu, ssh, jq, python3
#

set -Eeuo pipefail

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# ---- Source EEVDF common library ----
# shellcheck source=./eevdf-common.sh
source "$SCRIPT_DIR/eevdf-common.sh"

# ---- Constants ----
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"
DEFAULT_INTERVAL=5
DEFAULT_COUNT=0        # 0 = unlimited (SIGINT to stop)

# ---- State ----
POD_NAME=""
INTERVAL="$DEFAULT_INTERVAL"
COUNT="$DEFAULT_COUNT"
START_EPOCH=""
_SHOW_HEADER=true
_SHOULD_STOP=false

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <pod-name> [OPTIONS]

Poll per-task scheduler stats for a pod's container PIDs at interval.

Arguments:
  pod-name          Name of the pod (required)

Options:
  --interval N      Polling interval in seconds (default: $DEFAULT_INTERVAL)
  --count N         Number of samples (default: 0 = unlimited; SIGINT to stop)
  -h, --help        Show this help and exit

Output CSV columns:
  timestamp,pod,pid,sum_exec_runtime,wait_sum,sleep_sum,iowait_sum,
  nr_switches,nr_voluntary_switches,nr_involuntary_switches,run_delay,pcount

At SIGINT, prints a final summary with sample count to stderr.
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# get_pid_sched_data — Read /proc/<pid>/sched for a single PID on a node
#
# Usage: get_pid_sched_data <pid> <node_ip>
# Returns: tab-separated fields: sum_exec_runtime,wait_sum,sleep_sum,
#          iowait_sum,nr_switches,nr_voluntary_switches,nr_involuntary_switches
# ---------------------------------------------------------------------------
get_pid_sched_data() {
    local pid="$1"
    local node_ip="$2"

    local raw
    raw="$(ssh_node "$node_ip" "cat /proc/$pid/sched 2>/dev/null" 2>/dev/null || true)"

    if [[ -z "$raw" ]]; then
        printf '0\t0\t0\t0\t0\t0\t0'
        return 1
    fi

    local sum_exec_runtime=0 wait_sum=0 sleep_sum=0 iowait_sum=0
    local nr_switches=0 nr_voluntary_switches=0 nr_involuntary_switches=0

    while IFS= read -r line; do
        [[ "$line" != *:* ]] && continue
        [[ "$line" == *---* ]] && continue

        local key="${line%%:*}"
        local val="${line#*:}"
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        val="${val%,}"

        case "$key" in
            se.sum_exec_runtime)       sum_exec_runtime="$val" ;;
            se.statistics.wait_sum)    wait_sum="$val" ;;
            se.statistics.sleep_sum)   sleep_sum="$val" ;;
            se.statistics.iowait_sum)  iowait_sum="$val" ;;
            nr_switches)               nr_switches="$val" ;;
            nr_voluntary_switches)     nr_voluntary_switches="$val" ;;
            nr_involuntary_switches)   nr_involuntary_switches="$val" ;;
        esac
    done <<< "$raw"

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s' \
        "$sum_exec_runtime" "$wait_sum" "$sleep_sum" "$iowait_sum" \
        "$nr_switches" "$nr_voluntary_switches" "$nr_involuntary_switches"
}

# ---------------------------------------------------------------------------
# get_pid_schedstat_data — Read /proc/<pid>/schedstat for a single PID
#
# Usage: get_pid_schedstat_data <pid> <node_ip>
# Returns: tab-separated fields: run_delay,pcount
# ---------------------------------------------------------------------------
get_pid_schedstat_data() {
    local pid="$1"
    local node_ip="$2"

    local raw
    raw="$(ssh_node "$node_ip" "cat /proc/$pid/schedstat 2>/dev/null" 2>/dev/null || true)"

    if [[ -z "$raw" ]]; then
        printf '0\t0'
        return 1
    fi

    local run_delay pcount _unused_cpu_time
    read -r _unused_cpu_time run_delay pcount _ <<< "$raw" || true

    printf '%s\t%s' "${run_delay:-0}" "${pcount:-0}"
}

# ---------------------------------------------------------------------------
# print_csv_header — Write CSV header row
# ---------------------------------------------------------------------------
print_csv_header() {
    printf 'timestamp,pod,pid,sum_exec_runtime,wait_sum,sleep_sum,iowait_sum,nr_switches,nr_voluntary_switches,nr_involuntary_switches,run_delay,pcount\n'
}

# ---------------------------------------------------------------------------
# collect_and_print — Collect data for all PIDs and print CSV rows
# ---------------------------------------------------------------------------
collect_and_print() {
    local node_ip="$1"
    local pod_name="$2"
    shift 2
    local pid_list=("$@")

    local ts
    ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

    local pid pid_sched pid_schedstat
    local sum_exec_runtime wait_sum sleep_sum iowait_sum
    local nr_switches nr_voluntary_switches nr_involuntary_switches
    local run_delay pcount

    for pid in "${pid_list[@]}"; do
        pid_sched="$(get_pid_sched_data "$pid" "$node_ip" 2>/dev/null || true)"
        IFS=$'\t' read -r sum_exec_runtime wait_sum sleep_sum iowait_sum \
            nr_switches nr_voluntary_switches nr_involuntary_switches <<< "$pid_sched" || true

        pid_schedstat="$(get_pid_schedstat_data "$pid" "$node_ip" 2>/dev/null || true)"
        IFS=$'\t' read -r run_delay pcount <<< "$pid_schedstat" || true

        printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
            "$ts" \
            "$pod_name" \
            "$pid" \
            "${sum_exec_runtime:-0}" \
            "${wait_sum:-0}" \
            "${sleep_sum:-0}" \
            "${iowait_sum:-0}" \
            "${nr_switches:-0}" \
            "${nr_voluntary_switches:-0}" \
            "${nr_involuntary_switches:-0}" \
            "${run_delay:-0}" \
            "${pcount:-0}"
    done
}

# ---------------------------------------------------------------------------
# print_summary — Print final statistics to stderr
# ---------------------------------------------------------------------------
print_summary() {
    local sample_count="$1"
    local elapsed=0
    local now
    now="$(date +%s)"
    elapsed=$(( now - START_EPOCH ))

    printf '\n[SUMMARY] Samples=%d  Elapsed=%ds\n' "$sample_count" "$elapsed" >&2
}

# ---------------------------------------------------------------------------
# signal_handler — Handle SIGINT/SIGTERM gracefully
# ---------------------------------------------------------------------------
_signal_handler() {
    if [[ "$_SHOULD_STOP" == false ]]; then
        _SHOULD_STOP=true
        print_summary "$TOTAL_SAMPLES"
    fi
    exit 130
}

# ---- Counters ----
TOTAL_SAMPLES=0

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    # Parse --help
    if [[ $# -eq 0 ]]; then
        usage 1
    fi

    if [[ "$1" == "-h" || "$1" == "--help" ]]; then
        usage 0
    fi

    POD_NAME="$1"
    shift

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --interval)
                INTERVAL="${2:?--interval requires a number}"
                shift 2
                ;;
            --count)
                COUNT="${2:?--count requires a number}"
                shift 2
                ;;
            -h|--help)
                usage 0
                ;;
            *)
                printf 'ERROR: Unknown option: %s\n' "$1" >&2
                usage 1
                ;;
        esac
    done

    # Validate interval and count
    if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [[ "$INTERVAL" -lt 1 ]]; then
        printf 'ERROR: --interval must be a positive integer (got: %s)\n' "$INTERVAL" >&2
        exit 2
    fi

    if ! [[ "$COUNT" =~ ^[0-9]+$ ]]; then
        printf 'ERROR: --count must be a non-negative integer (got: %s)\n' "$COUNT" >&2
        exit 2
    fi

    # Validate dependencies
    require_tools kubectl tofu ssh jq python3 || exit 1

    # Resolve project root and KUBECONFIG
    resolve_project_root

    # Verify cluster reachable
    kubectl --kubeconfig "$KUBECONFIG" cluster-info --request-timeout=5s &>/dev/null || {
        log_error "Cannot reach Kubernetes cluster — is kubeconfig valid?"
        exit 1
    }

    # Get the node running the pod
    local node_ip
    node_ip="$(get_pod_node_ip "$POD_NAME")" || exit 1

    # Resolve container PIDs
    local pids_str
    pids_str="$(resolve_container_pids "$POD_NAME")" || exit 1

    # Convert space-separated PIDs to array
    local -a pid_list
    local pid
    for pid in $pids_str; do
        pid_list+=("$pid")
    done

    if [[ ${#pid_list[@]} -eq 0 ]]; then
        log_error "No container PIDs found for pod '$POD_NAME'"
        exit 1
    fi

    # Trap signals
    trap _signal_handler SIGINT SIGTERM

    # Record start time
    START_EPOCH="$(date +%s)"

    # Print CSV header
    print_csv_header

    # Poll loop
    local sample_count=0
    while true; do
        # Check if we should stop
        if [[ "$_SHOULD_STOP" == true ]]; then
            break
        fi

        # Check count limit
        if [[ "$COUNT" -gt 0 && "$sample_count" -ge "$COUNT" ]]; then
            break
        fi

        # Collect and print data for all PIDs
        collect_and_print "$node_ip" "$POD_NAME" "${pid_list[@]}" || {
            log_error "Failed to collect sched data (attempt $((sample_count + 1)))"
            if [[ "$COUNT" -gt 0 ]]; then
                sample_count=$((sample_count + 1))
            fi
            sleep "$INTERVAL"
            continue
        }

        sample_count=$((sample_count + 1))
        TOTAL_SAMPLES=$sample_count

        # Sleep (unless this was the last sample)
        if [[ "$COUNT" -gt 0 && "$sample_count" -ge "$COUNT" ]]; then
            break
        fi
        sleep "$INTERVAL"
    done

    # Print final summary if we exited normally (not via signal)
    if [[ "$_SHOULD_STOP" == false ]]; then
        print_summary "$sample_count"
    fi
}

main "$@"
