#!/usr/bin/env bash
#
# cgroup-observe.sh — Read cgroup v2 CPU stats for a given pod/container
#
# Usage:
#   cgroup-observe.sh <pod-name> [container-name]
#   cgroup-observe.sh --help
#
# Reads cpu.weight, cpu.max, and cpu.stat from the container's cgroup v2
# directory and outputs JSON.

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

Read cgroup v2 CPU stats for a pod's container and output as JSON.

Arguments:
  pod-name          Name of the pod (required)
  container-name    Container name (optional; defaults to first container)

Options:
  -h, --help        Show this help and exit

Output JSON fields:
  pod, container, node, cgroup_path, cpu_weight,
  cpu_max_quota_us, cpu_max_period_us,
  cpu_stat: { usage_usec, nr_periods, nr_throttled, throttled_usec }

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
    local container_name="${2:-}"

    # Validate dependencies
    require_tools kubectl tofu ssh jq python3 || exit 1

    # Resolve project root and KUBECONFIG
    resolve_project_root

    # Gather and print cgroup data
    get_cgroup_data "$pod_name" "$container_name" || exit 1
}

main "$@"
