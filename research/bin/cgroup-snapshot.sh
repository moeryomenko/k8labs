#!/usr/bin/env bash
#
# cgroup-snapshot.sh — One-shot full cgroup state dump for a container
#
# Usage:
#   cgroup-snapshot.sh <pod-name> [container-name]
#   cgroup-snapshot.sh --help
#
# Reads ALL files in the container's cgroup directory under the CPU
# controller and outputs JSON. Includes optional cpuset files if available.

set -Eeuo pipefail

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# ---- Source common library ----
# shellcheck source=./cgroup-common.sh
source "$SCRIPT_DIR/cgroup-common.sh"

# ---- Constants ----
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <pod-name> [container-name]

One-shot full cgroup state dump for a container. Reads all CPU controller
cgroup files and optional cpuset files.

Arguments:
  pod-name          Name of the pod (required)
  container-name    Container name (optional; defaults to first container)

Options:
  -h, --help        Show this help and exit

Output JSON includes all fields from:
  cpu.weight, cpu.weight.nice, cpu.max, cpu.max.burst, cpu.stat, cpu.idle,
  cpuset.cpus (if available), cpuset.mems (if available)

Dependencies: kubectl, tofu/terraform, ssh, jq, python3
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# read_cgroup_file_or_null — Read a cgroup file or return JSON null
# ---------------------------------------------------------------------------
read_cgroup_file_or_null() {
    local node_ip="$1"
    local cgroup_path="$2"
    local filename="$3"

    local value
    value="$(read_cgroup_file "$node_ip" "$cgroup_path" "$filename" 2>/dev/null || true)"
    if [[ -z "$value" ]]; then
        printf 'null'
    else
        printf '%s' "$value" | jq -Rs 'gsub("\\n$";"")' || printf '"%s"' "$value"
    fi
}

# ---------------------------------------------------------------------------
# read_cpu_stat_full — Parse cpu.stat into JSON object
# ---------------------------------------------------------------------------
read_cpu_stat_full() {
    local node_ip="$1"
    local cgroup_path="$2"

    local stat_raw
    stat_raw="$(read_cgroup_file "$node_ip" "$cgroup_path" "cpu.stat" 2>/dev/null || true)"

    if [[ -z "$stat_raw" ]]; then
        printf '{}'
        return 0
    fi

    local -a keys=()
    local -a vals=()
    while IFS=' ' read -r key val; do
        [[ -z "$key" ]] && continue
        keys+=("$key")
        vals+=("$val")
    done <<< "$stat_raw"

    # Build JSON object
    local json='{'
    local sep=''
    for i in "${!keys[@]}"; do
        json+="${sep}\"${keys[i]}\":${vals[i]}"
        sep=','
    done
    json+='}'
    printf '%s' "$json"
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
    local container_name="${2:-}"

    # Validate dependencies
    require_tools kubectl tofu ssh jq python3 || exit 1

    # Resolve project root and KUBECONFIG
    resolve_project_root

    # Verify cluster reachable
    kubectl --kubeconfig "$KUBECONFIG" cluster-info --request-timeout=5s &>/dev/null || {
        log_error "Cannot reach Kubernetes cluster — is kubeconfig valid?"
        exit 1
    }

    # Get node IP
    local node_ip
    node_ip="$(get_pod_node_ip "$pod_name")" || exit 1

    # Get container name (default to first container if not specified)
    if [[ -z "$container_name" ]]; then
        container_name="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
            -o jsonpath='{.spec.containers[0].name}' 2>/dev/null)" || {
            log_error "Failed to get container name from pod '$pod_name'"
            exit 1
        }
    fi

    # Get container ID
    local container_id
    container_id="$(get_container_id "$node_ip" "$container_name")" || exit 1

    # Get PID
    local pid
    pid="$(get_container_pid "$node_ip" "$container_id")" || exit 1

    # Get cgroup path
    local cgroup_path
    cgroup_path="$(get_cgroup_path "$node_ip" "$pid")" || exit 1

    # Get pod node name
    local node_name
    node_name="$(get_pod_node "$pod_name" 2>/dev/null || printf 'unknown')"

    # Read all cgroup files
    local cpu_weight cpu_weight_nice cpu_max_raw cpu_max_burst cpu_idle
    local cpuset_cpus cpuset_mems

    cpu_weight="$(read_cgroup_file_or_null "$node_ip" "$cgroup_path" "cpu.weight")"
    cpu_weight_nice="$(read_cgroup_file_or_null "$node_ip" "$cgroup_path" "cpu.weight.nice")"
    cpu_max_raw="$(read_cgroup_file_or_null "$node_ip" "$cgroup_path" "cpu.max")"
    cpu_max_burst="$(read_cgroup_file_or_null "$node_ip" "$cgroup_path" "cpu.max.burst")"
    cpu_idle="$(read_cgroup_file_or_null "$node_ip" "$cgroup_path" "cpu.idle")"
    cpuset_cpus="$(read_cgroup_file_or_null "$node_ip" "$cgroup_path" "cpuset.cpus")"
    cpuset_mems="$(read_cgroup_file_or_null "$node_ip" "$cgroup_path" "cpuset.mems")"

    # Parse cpu.max
    local cpu_max_quota_val cpu_max_period_val
    if [[ "$cpu_max_raw" != "null" ]]; then
        local raw_val
        raw_val="$(jq -r '. // ""' <<< "$cpu_max_raw")"
        if [[ -n "$raw_val" ]]; then
            parse_cpu_max "$raw_val" cpu_max_quota_val cpu_max_period_val
        else
            cpu_max_quota_val=""
            cpu_max_period_val=""
        fi
    else
        cpu_max_quota_val=""
        cpu_max_period_val=""
    fi

    # Read full cpu.stat
    local cpu_stat_json
    cpu_stat_json="$(read_cpu_stat_full "$node_ip" "$cgroup_path")"

    # Build JSON output
    jq -n \
        --arg pod "$pod_name" \
        --arg container "$container_name" \
        --arg node "$node_name" \
        --arg cgroup_path "$cgroup_path" \
        --argjson cpu_weight "$cpu_weight" \
        --argjson cpu_weight_nice "$cpu_weight_nice" \
        --argjson cpu_max "$cpu_max_raw" \
        --argjson cpu_max_burst "$cpu_max_burst" \
        --argjson cpu_idle "$cpu_idle" \
        --argjson cpuset_cpus "$cpuset_cpus" \
        --argjson cpuset_mems "$cpuset_mems" \
        --argjson cpu_stat "$cpu_stat_json" \
        --arg cpu_max_quota "${cpu_max_quota_val:-}" \
        --arg cpu_max_period "${cpu_max_period_val:-}" \
        '{
            pod: $pod,
            container: $container,
            node: $node,
            cgroup_path: $cgroup_path,
            cpu: {
                weight: $cpu_weight,
                weight_nice: $cpu_weight_nice,
                max: $cpu_max,
                max_quota_us: $cpu_max_quota,
                max_period_us: $cpu_max_period,
                max_burst: $cpu_max_burst,
                idle: $cpu_idle,
                stat: $cpu_stat
            },
            cpuset: {
                cpus: $cpuset_cpus,
                mems: $cpuset_mems
            }
        }'
}

main "$@"
