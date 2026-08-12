#!/usr/bin/env bash
#
# sched-debug-parse.sh — Parse /proc/sched_debug for EEVDF scheduling data
#
# Usage:
#   sched-debug-parse.sh <pod-name> [pid-filter]
#   sched-debug-parse.sh --help
#
# Reads /proc/sched_debug on the worker node running the pod (via SSH) and
# extracts per-CPU runqueue data. Outputs CSV with EEVDF scheduling timeline
# fields suitable for before/after comparison.
#
# The optional pid-filter is a comma-separated list of PIDs to include.
# If omitted, all running tasks are shown.
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

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <pod-name> [pid-filter]

Parse /proc/sched_debug on a pod's worker node and output CSV with per-CPU
EEVDF scheduling data.

Arguments:
  pod-name          Name of the pod running on the target node (required)
  pid-filter        Comma-separated list of PIDs to include (optional)

Options:
  -h, --help        Show this help and exit

Output CSV columns:
  timestamp,cpu,entity,vruntime,deadline,exec_start,min_vruntime

If pid-filter is provided, only tasks matching those PIDs are output.
If the filter is empty or no tasks match, only the CSV header is printed.

Dependencies: kubectl, tofu/terraform, ssh, jq, python3
EOF
    exit "${1:-0}"
}

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

    local pod_name="$1"
    local pid_filter="${2:-}"

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
    node_ip="$(get_pod_node_ip "$pod_name")" || exit 1

    # Check if /proc/sched_debug is available
    check_sched_debug_available "$node_ip" || {
        log_error "/proc/sched_debug is not available on node $node_ip (CONFIG_SCHED_DEBUG=n?)"
        log_error "Falling back: output CSV with header only"
        printf 'timestamp,cpu,entity,vruntime,deadline,exec_start,min_vruntime\n'
        exit 0
    }

    # Read /proc/sched_debug via SSH
    local raw_content
    raw_content="$(read_sched_debug "$node_ip")" || {
        log_error "Failed to read /proc/sched_debug on node $node_ip"
        printf 'timestamp,cpu,entity,vruntime,deadline,exec_start,min_vruntime\n'
        exit 0
    }

    if [[ -z "$raw_content" ]]; then
        log_error "Empty /proc/sched_debug on node $node_ip"
        printf 'timestamp,cpu,entity,vruntime,deadline,exec_start,min_vruntime\n'
        exit 0
    fi

    # Parse and output CSV
    parse_sched_debug_csv "$raw_content" "$pid_filter"
}

main "$@"
