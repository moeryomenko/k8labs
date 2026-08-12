#!/usr/bin/env bash
#
# cgroup-hierarchy-snapshot.sh — Read-only snapshot of a node's cgroup v2 CPU
# hierarchy over SSH, emitted as JSON.
#
# Usage:
#   cgroup-hierarchy-snapshot.sh [--node <ip>] <ip>
#   cgroup-hierarchy-snapshot.sh --help
#
# Walks the kubelet-managed hierarchy under kubepods.slice on the target node:
#   kubepods.slice/cpu.weight                      -> kubepods_slice_weight
#   kubepods-*.slice children (QoS slices)         -> slices[].name/.cpu_weight
#   kubepods-*-pod*.slice children (pod slices)    -> slices[].pods[] with
#                                                     name/cpu_weight/cpu_max
#
# All reads are performed over SSH with read-only commands (cat/find); the
# script never writes to the node. Output is pure JSON on stdout; diagnostics
# go to stderr.

set -Eeuo pipefail

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# ---- Source common library (ssh_node, require_tools, log_error) ----
# shellcheck source=./cgroup-common.sh
source "$SCRIPT_DIR/cgroup-common.sh"

# ---- Constants ----
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME [--node <ip>] <ip>

Read-only snapshot of a node's cgroup v2 CPU hierarchy over SSH, emitted as
JSON on stdout. The hierarchy under kubepods.slice is walked: the top-level
cpu.weight, each QoS slice (kubepods-burstable.slice, kubepods-besteffort.slice,
kubepods-guaranteed.slice and any other kubepods-*.slice child), and the pod
slices inside each QoS slice with their cpu.weight and cpu.max values.

Arguments:
  <ip>                 Node IP (required; e.g. 192.168.124.26)

Options:
  --node <ip>          Node IP (alternative to the positional argument)
  -h, --help           Show this help and exit

Output JSON:
  node, timestamp, kubepods_slice_weight,
  slices[ { name, cpu_weight, pods[ { name, cpu_weight, cpu_max } ] } ]

Dependencies: ssh, jq
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# ssh_read — Run a read-only remote command, propagating SSH failures.
#
# Used for commands whose success is required (connectivity probe, directory
# discovery). On failure logs a clear message naming the node and returns 1.
# ---------------------------------------------------------------------------
ssh_read() {
    local node_ip="$1"
    local remote_cmd="$2"
    local value
    value="$(ssh_node "$node_ip" "$remote_cmd")" || {
        log_error "SSH command failed for node $node_ip: $remote_cmd"
        return 1
    }
    printf '%s\n' "$value"
}

# ---------------------------------------------------------------------------
# read_remote_file — Read a single cgroup file on the node, tolerantly.
#
# Missing files (or a dropped connection after the initial probe) yield an
# empty string instead of aborting the snapshot; the remote cat already
# suppresses its own error output. The local stderr is suppressed so a
# transient failure cannot pollute diagnostics.
# ---------------------------------------------------------------------------
read_remote_file() {
    local node_ip="$1"
    local cgroup_file="$2"
    local value
    value="$(ssh_node "$node_ip" "cat '$cgroup_file' 2>/dev/null" 2>/dev/null || true)"
    printf '%s\n' "$value"
}

# ---------------------------------------------------------------------------
# discover_dirs — List the sorted child directories of $parent matching
# $pattern at depth 1. Output: one absolute path per line.
# ---------------------------------------------------------------------------
discover_dirs() {
    local node_ip="$1"
    local parent="$2"
    local pattern="$3"
    local raw dirs=()

    raw="$(ssh_read "$node_ip" "find '$parent' -maxdepth 1 -type d -name '$pattern'")" || return 1
    if [[ -n "$raw" ]]; then
        mapfile -t dirs <<< "$raw"
        mapfile -t dirs < <(printf '%s\n' "${dirs[@]}" | sort)
    fi
    printf '%s\n' "${dirs[@]}"
}

# ---------------------------------------------------------------------------
# build_slices — Walk the QoS slice hierarchy under kubepods.slice and emit a
# JSON array of slice objects ({name, cpu_weight, pods[]}).
# ---------------------------------------------------------------------------
build_slices() {
    local node_ip="$1"
    local qos_dirs=() pod_dirs=() pods=() slices=()
    local qos_dir qos_name qos_weight pod_dir pod_name pod_weight pod_max
    local pods_json slices_json

    mapfile -t qos_dirs < <(discover_dirs "$node_ip" \
        "/sys/fs/cgroup/kubepods.slice" "kubepods-*.slice") || return 1

    for qos_dir in "${qos_dirs[@]}"; do
        qos_name="$(basename -- "$qos_dir")"
        qos_weight="$(read_remote_file "$node_ip" "$qos_dir/cpu.weight")"

        if [[ "$qos_name" == kubepods-pod*.slice ]]; then
            # Direct guaranteed pod slice (systemd cgroup driver): a TRUE
            # Guaranteed pod (memory request==limit) has NO
            # kubepods-guaranteed.slice wrapper — its pod slice sits directly
            # under kubepods.slice. Emit ONE self-representing pod entry
            # mirroring the slice itself (name = slice name, cpu_weight = slice
            # cpu.weight, cpu_max = slice cpu.max) so the weight is never lost.
            pod_max="$(read_remote_file "$node_ip" "$qos_dir/cpu.max")"
            pods_json="$(jq -cn \
                --arg name "$qos_name" \
                --arg cpu_weight "$qos_weight" \
                --arg cpu_max "$pod_max" \
                '{name: $name, cpu_weight: $cpu_weight, cpu_max: $cpu_max}' \
                | jq -sc .)"
        else
            pod_dirs=()
            mapfile -t pod_dirs < <(discover_dirs "$node_ip" "$qos_dir" "kubepods-*-pod*.slice") || return 1

            pods=()
            for pod_dir in "${pod_dirs[@]}"; do
                pod_name="$(basename -- "$pod_dir")"
                pod_weight="$(read_remote_file "$node_ip" "$pod_dir/cpu.weight")"
                pod_max="$(read_remote_file "$node_ip" "$pod_dir/cpu.max")"
                pods+=("$(jq -cn \
                    --arg name "$pod_name" \
                    --arg cpu_weight "$pod_weight" \
                    --arg cpu_max "$pod_max" \
                    '{name: $name, cpu_weight: $cpu_weight, cpu_max: $cpu_max}')")
            done

            if [[ ${#pods[@]} -eq 0 ]]; then
                pods_json='[]'
            else
                pods_json="$(printf '%s\n' "${pods[@]}" | jq -sc .)"
            fi
        fi

        slices+=("$(jq -cn \
            --arg name "$qos_name" \
            --arg cpu_weight "$qos_weight" \
            --argjson pods "$pods_json" \
            '{name: $name, cpu_weight: $cpu_weight, pods: $pods}')")
    done

    if [[ ${#slices[@]} -eq 0 ]]; then
        printf '[]'
    else
        printf '%s\n' "${slices[@]}" | jq -sc .
    fi
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    local node=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage 0
                ;;
            --node)
                if [[ $# -lt 2 ]]; then
                    log_error "Option --node requires an argument"
                    usage 1
                fi
                node="$2"
                shift 2
                ;;
            -*)
                log_error "Unknown option: $1"
                usage 1
                ;;
            *)
                if [[ -n "$node" ]]; then
                    log_error "Unexpected extra argument: $1"
                    usage 1
                fi
                node="$1"
                shift
                ;;
        esac
    done

    if [[ -z "$node" ]]; then
        log_error "Missing node argument"
        usage 1
    fi

    require_tools ssh jq date || exit 1

    local timestamp
    timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    # Connectivity probe + top-level weight: a failure here is fatal.
    local kubepods_weight
    kubepods_weight="$(ssh_read "$node" \
        "cat /sys/fs/cgroup/kubepods.slice/cpu.weight 2>/dev/null")" || exit 1

    local slices_json
    slices_json="$(build_slices "$node")" || exit 1

    jq -n \
        --arg node "$node" \
        --arg timestamp "$timestamp" \
        --arg kubepods_weight "$kubepods_weight" \
        --argjson slices "$slices_json" \
        '{ node: $node, timestamp: $timestamp, kubepods_slice_weight: $kubepods_weight, slices: $slices }'
}

main "$@"
