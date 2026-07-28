#!/usr/bin/env bash
#
# eevdf-observe.sh — Per-container EEVDF scheduler metrics
#
# Usage:
#   eevdf-observe.sh <pod-name>
#   eevdf-observe.sh --help
#
# Reads /proc/<pid>/sched (se.sum_exec_runtime, se.vruntime, se.nr_migrations,
# se.statistics.*) and /proc/<pid>/schedstat (run_delay, pcount) for each
# container PID in the pod, and outputs structured JSON.
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
Usage: $SCRIPT_NAME <pod-name>

Read EEVDF scheduler metrics for all container tasks in a pod.

Arguments:
  pod-name          Name of the pod (required)

Options:
  -h, --help        Show this help and exit

Output JSON fields:
  pod, node, node_ip, timestamp, cgroup_path, task_count,
  tasks[]: pid, se.{sum_exec_runtime, vruntime, nr_migrations},
           statistics.{nr_switches, nr_voluntary_switches,
           nr_involuntary_switches, wait_sum, sleep_sum, iowait_sum},
           sched_info.{run_delay, pcount}

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
    local node_ip node_name
    node_ip="$(get_pod_node_ip "$pod_name")" || exit 1
    node_name="$(get_pod_node "$pod_name" 2>/dev/null || printf 'unknown')"

    # Resolve container PIDs
    local pids
    pids="$(resolve_container_pids "$pod_name")" || exit 1

    # Get cgroup path
    local cgroup_path
    cgroup_path="$(pod_name_to_cgroup_path "$pod_name" 2>/dev/null || printf 'unknown')"

    # Collect per-PID metrics
    local pid
    local -a task_jsons=()
    local has_errors=false

    for pid in $pids; do
        local sched_json schedstat_json merged

        sched_json="$(read_proc_sched "$pid" "$node_ip")" || {
            has_errors=true
            log_error "Failed to read /proc/$pid/sched on node $node_ip"
            continue
        }

        schedstat_json="$(read_schedstat "$pid" "$node_ip")" || {
            has_errors=true
            log_error "Failed to read /proc/$pid/schedstat on node $node_ip"
            continue
        }

        # Merge sched + schedstat JSON into one task entry
        merged="$(jq -n \
            --argjson sched "$sched_json" \
            --argjson schedstat "$schedstat_json" \
            '$sched + $schedstat' 2>/dev/null)" || continue

        task_jsons+=("$merged")
    done

    # Build JSON array of tasks
    local tasks_json
    if [[ ${#task_jsons[@]} -eq 0 ]]; then
        tasks_json='[]'
    else
        local task_str task_sep=""
        tasks_json='['
        for task_str in "${task_jsons[@]}"; do
            tasks_json+="${task_sep}${task_str}"
            task_sep=','
        done
        tasks_json+=']'
    fi

    local timestamp
    timestamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

    # Output final JSON
    jq -n \
        --arg pod "$pod_name" \
        --arg node "$node_name" \
        --arg node_ip "$node_ip" \
        --arg timestamp "$timestamp" \
        --arg cgroup_path "$cgroup_path" \
        --argjson tasks "$tasks_json" \
        --arg has_errors "$has_errors" \
        '{
            pod: $pod,
            node: $node,
            node_ip: $node_ip,
            timestamp: $timestamp,
            cgroup_path: $cgroup_path,
            task_count: ($tasks | length),
            tasks: $tasks,
            has_errors: ($has_errors == "true")
        }'
}

main "$@"
