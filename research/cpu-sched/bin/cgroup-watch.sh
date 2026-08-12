#!/usr/bin/env bash
#
# cgroup-watch.sh — Poll cgroup CPU stats at interval, output CSV
#
# Usage:
#   cgroup-watch.sh <pod-name> [--interval N] [--count N] [--container name]
#   cgroup-watch.sh --help
#
# CSV output to stdout. Polls cpu.stat fields at the specified interval.

set -Eeuo pipefail

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# ---- Source common library ----
# shellcheck source=./cgroup-common.sh
source "$SCRIPT_DIR/cgroup-common.sh"

# ---- Constants ----
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"
DEFAULT_INTERVAL=5
DEFAULT_COUNT=0        # 0 = unlimited

# ---- State ----
POD_NAME=""
CONTAINER_NAME=""
INTERVAL="$DEFAULT_INTERVAL"
COUNT="$DEFAULT_COUNT"
TOTAL_SAMPLES=0
TOTAL_THROTTLED=0
START_EPOCH=""
_SHOULD_STOP=false

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <pod-name> [OPTIONS]

Poll cgroup v2 CPU stats at interval and output CSV.

Arguments:
  pod-name          Name of the pod (required)

Options:
  --container name  Container name (default: first container in pod)
  --interval N      Polling interval in seconds (default: $DEFAULT_INTERVAL)
  --count N         Number of samples (default: 0 = unlimited; SIGINT to stop)
  -h, --help        Show this help and exit

Output CSV columns:
  timestamp,pod,container,nr_periods,nr_throttled,throttled_usec,
  usage_usec,cpu_weight,cpu_max_quota,cpu_max_period

At SIGINT, prints a final summary line with sample count and total throttled.
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# print_csv_header — Write CSV header row
# ---------------------------------------------------------------------------
print_csv_header() {
    printf 'timestamp,pod,container,nr_periods,nr_throttled,throttled_usec,usage_usec,cpu_weight,cpu_max_quota,cpu_max_period\n'
}

# ---------------------------------------------------------------------------
# print_csv_row — Write a single CSV data row from cgroup data JSON
# ---------------------------------------------------------------------------
print_csv_row() {
    local data="$1"
    local ts
    ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

    jq -r \
        --arg ts "$ts" \
        '[
            $ts,
            .pod,
            .container,
            (.cpu_stat.nr_periods | tostring),
            (.cpu_stat.nr_throttled | tostring),
            (.cpu_stat.throttled_usec | tostring),
            (.cpu_stat.usage_usec | tostring),
            (.cpu_weight | tostring),
            (.cpu_max_quota_us | tostring),
            (.cpu_max_period_us | tostring)
        ] | join(",")' <<< "$data"
}

# ---------------------------------------------------------------------------
# print_summary — Print final statistics to stderr
# ---------------------------------------------------------------------------
print_summary() {
    local elapsed=0
    local now
    now="$(date +%s)"
    elapsed=$(( now - START_EPOCH ))

    if [[ "$TOTAL_SAMPLES" -gt 0 ]]; then
        local avg_per_sample=$(( TOTAL_THROTTLED / TOTAL_SAMPLES ))
        printf '\n[SUMMARY] Samples=%d  Total-throttled=%dus  Avg-per-sample=%dus  Elapsed=%ds\n' \
            "$TOTAL_SAMPLES" "$TOTAL_THROTTLED" "$avg_per_sample" "$elapsed" >&2
    else
        printf '\n[SUMMARY] No samples collected. Elapsed=%ds\n' "$elapsed" >&2
    fi
}

# ---------------------------------------------------------------------------
# signal_handler — Handle SIGINT/SIGTERM gracefully
# ---------------------------------------------------------------------------
_signal_handler() {
    if [[ "$_SHOULD_STOP" == false ]]; then
        _SHOULD_STOP=true
        print_summary
    fi
    exit 130
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    # Parse --help
    if [[ $# -eq 0 ]]; then
        usage 1
    fi

    # Parse arguments
    if [[ "$1" == "-h" || "$1" == "--help" ]]; then
        usage 0
    fi

    POD_NAME="$1"
    shift

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --container)
                CONTAINER_NAME="${2:?--container requires a value}"
                shift 2
                ;;
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

        # Collect data
        local data
        data="$(get_cgroup_data "$POD_NAME" "$CONTAINER_NAME" 2>/dev/null)" || {
            log_error "Failed to collect cgroup data (attempt $((sample_count + 1)))"
            if [[ "$COUNT" -gt 0 ]]; then
                sample_count=$((sample_count + 1))
            fi
            sleep "$INTERVAL"
            continue
        }

        # Extract throttled_usec for summary
        local throttled
        throttled="$(jq -r '.cpu_stat.throttled_usec // 0' <<< "$data")"
        TOTAL_THROTTLED=$(( TOTAL_THROTTLED + throttled ))
        TOTAL_SAMPLES=$(( TOTAL_SAMPLES + 1 ))

        # Print CSV row
        print_csv_row "$data"

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
        print_summary
    fi
}

main "$@"
