#!/usr/bin/env bash
#
# eevdf-snapshot.sh — One-shot full EEVDF state dump for a pod
#
# Usage:
#   eevdf-snapshot.sh <pod-name>
#   eevdf-snapshot.sh --help
#
# Combines: cgroup CPU stats (cpu.stat, cpu.weight, cpu.max) + EEVDF per-task
# metrics (from eevdf-observe.sh) + node-level sched_debug summary.
# Outputs comprehensive JSON suitable for before/after comparison.
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

One-shot EEVDF state dump combining cgroup CPU stats, per-task scheduler
metrics, and node-level /proc/sched_debug summary.

Arguments:
  pod-name          Name of the pod (required)

Options:
  -h, --help        Show this help and exit

Output JSON structure:
  timestamp, pod,
  cgroup_stats: { pod, container, node, cpu_weight, cpu_max_quota_us, ... },
  sched_metrics: { per-task EEVDF fields from eevdf-observe.sh },
  node_sched_debug: { per-CPU summary from /proc/sched_debug }

Dependencies: kubectl, tofu/terraform, ssh, jq, python3
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# parse_sched_debug_summary — Parse /proc/sched_debug into a compact JSON
#
# Extracts per-CPU: current task pid, min_vruntime, nr_running
# Returns JSON object keyed by CPU number.
# ---------------------------------------------------------------------------
parse_sched_debug_summary() {
    local raw_content="$1"

    python3 -c "
import sys, re, json

text = sys.stdin.read()
lines = text.split('\n')

cpus = {}
current_cpu = None
current_min_vruntime = None
current_curr_pid = None
current_nr_running = None
in_runnable = False

for line in lines:
    cpu_match = re.match(r'^cpu#(\d+)', line)
    if cpu_match:
        # Save previous CPU
        if current_cpu is not None:
            cpus[str(current_cpu)] = {
                'curr_pid': current_curr_pid or '',
                'min_vruntime': current_min_vruntime or '',
                'nr_running': current_nr_running or ''
            }
        current_cpu = int(cpu_match.group(1))
        current_min_vruntime = None
        current_curr_pid = None
        current_nr_running = None
        in_runnable = False
        continue

    if current_cpu is None:
        continue

    # .curr->pid
    m = re.match(r'\.curr->pid\s+:\s+(\d+)', line)
    if m:
        current_curr_pid = m.group(1)

    # .min_vruntime (from cfs_rq section)
    m = re.match(r'\.min_vruntime\s+:\s+([0-9.]+)', line)
    if m and current_min_vruntime is None:
        current_min_vruntime = m.group(1)

    # .nr_running
    m = re.match(r'\.nr_running\s+:\s+(\d+)', line)
    if m and current_nr_running is None:
        current_nr_running = m.group(1)

    if 'runnable tasks:' in line:
        in_runnable = True

# Save last CPU
if current_cpu is not None:
    cpus[str(current_cpu)] = {
        'curr_pid': current_curr_pid or '',
        'min_vruntime': current_min_vruntime or '',
        'nr_running': current_nr_running or ''
    }

print(json.dumps(cpus, indent=2))
" <<< "$raw_content"
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

    local timestamp
    timestamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

    # ---------------------------------------------------------------
    # 1. Collect EEVDF per-task metrics via eevdf-observe
    # ---------------------------------------------------------------
    local observe_json
    observe_json="$("$SCRIPT_DIR/eevdf-observe.sh" "$pod_name" 2>/dev/null)" || {
        log_error "eevdf-observe.sh failed for pod '$pod_name'"
        observe_json='{"error":"eevdf-observe failed","task_count":0,"tasks":[]}'
    }

    # Extract node_ip from observe output
    local node_ip
    node_ip="$(jq -r '.node_ip // empty' <<< "$observe_json" 2>/dev/null || true)"
    if [[ -z "$node_ip" ]]; then
        node_ip="$(get_pod_node_ip "$pod_name" 2>/dev/null || true)"
    fi

    # ---------------------------------------------------------------
    # 2. Collect cgroup CPU stats
    # ---------------------------------------------------------------
    local cgroup_data
    cgroup_data="$(get_cgroup_data "$pod_name" 2>/dev/null)" || {
        log_error "get_cgroup_data failed for pod '$pod_name'"
        cgroup_data='{"error":"cgroup data unavailable"}'
    }

    # ---------------------------------------------------------------
    # 3. Collect node-level /proc/sched_debug summary
    # ---------------------------------------------------------------
    local sched_debug_json="{}"
    local sched_debug_note=""

    if [[ -n "$node_ip" ]]; then
        if check_sched_debug_available "$node_ip"; then
            local sched_debug_raw
            sched_debug_raw="$(read_sched_debug "$node_ip" 2>/dev/null)" || true
            if [[ -n "$sched_debug_raw" ]]; then
                sched_debug_json="$(parse_sched_debug_summary "$sched_debug_raw" 2>/dev/null)" || {
                    sched_debug_json='{"error":"parse failed"}'
                    sched_debug_note="sched_debug parse error"
                }
            fi
        else
            sched_debug_note="/proc/sched_debug not available (CONFIG_SCHED_DEBUG=n or not mounted)"
        fi
    else
        sched_debug_note="node IP not resolvable"
    fi

    # ---------------------------------------------------------------
    # 4. Output combined JSON
    # ---------------------------------------------------------------
    jq -n \
        --arg timestamp "$timestamp" \
        --arg pod "$pod_name" \
        --argjson cgroup "$cgroup_data" \
        --argjson observe "$observe_json" \
        --argjson sched_debug "$sched_debug_json" \
        --arg sched_debug_note "$sched_debug_note" \
        '{
            timestamp: $timestamp,
            pod: $pod,
            cgroup_stats: $cgroup,
            sched_metrics: $observe,
            node_sched_debug: {
                per_cpu: $sched_debug,
                note: $sched_debug_note
            }
        }'
}

main "$@"
