#!/usr/bin/env bash
#
# eevdf-common.sh — Shared utilities for EEVDF kernel metric observation
#
# This library is sourced by eevdf-observe.sh, eevdf-snapshot.sh,
# sched-debug-parse.sh, and cgroup-pid-watch.sh.
#
# Usage in scripts:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/eevdf-common.sh"
#

# Guard against double-sourcing
[[ -z ${_EEVDF_COMMON_SH:-} ]] || return
_EEVDF_COMMON_SH=1
readonly _EEVDF_COMMON_SH

# ---- Strict Mode ----
set -Eeuo pipefail

# ---- Source cgroup-common for shared infrastructure ----
_EEVDF_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=./cgroup-common.sh
source "$_EEVDF_SCRIPT_DIR/cgroup-common.sh"

# ---------------------------------------------------------------------------
# resolve_container_pids — Get all container PIDs for a pod
#
# Resolves PIDs for all containers in a pod from the worker node via SSH.
# Returns space-separated list of PIDs.
# ---------------------------------------------------------------------------
resolve_container_pids() {
    local pod_name="$1"
    resolve_project_root

    local node_ip
    node_ip="$(get_pod_node_ip "$pod_name")" || return 1

    # Get all container names from the pod spec
    local container_names
    container_names="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
        -o jsonpath='{.spec.containers[*].name}' 2>/dev/null)" || {
        log_error "Failed to get container names for pod '$pod_name'"
        return 1
    }

    local all_pids=()
    local container_name container_id pid
    for container_name in $container_names; do
        container_id="$(get_container_id "$node_ip" "$pod_name" "$container_name" 2>/dev/null)" || continue
        pid="$(get_container_pid "$node_ip" "$container_id" 2>/dev/null)" || continue
        all_pids+=("$pid")
    done

    if [[ ${#all_pids[@]} -eq 0 ]]; then
        log_error "No PIDs found for pod '$pod_name' on node $node_ip"
        return 1
    fi

    echo "${all_pids[*]}"
}

# ---------------------------------------------------------------------------
# read_sched_debug — Read /proc/sched_debug on a node
#
# Usage: read_sched_debug <node_ip>
# Returns raw content of /proc/sched_debug
# ---------------------------------------------------------------------------
read_sched_debug() {
    local node_ip="$1"
    local content

    content="$(ssh_node "$node_ip" "cat /proc/sched_debug 2>/dev/null" 2>/dev/null)" || {
        log_error "Failed to read /proc/sched_debug on node $node_ip (CONFIG_SCHED_DEBUG=n?)"
        return 1
    }

    if [[ -z "$content" ]]; then
        log_error "/proc/sched_debug is empty on node $node_ip"
        return 1
    fi

    printf '%s' "$content"
}

# ---------------------------------------------------------------------------
# read_schedstat — Read /proc/<pid>/schedstat for a PID on a node
#
# Usage: read_schedstat <pid> <node_ip>
# Returns: JSON with parsed schedstat fields (sched_info.run_delay, pcount)
# ---------------------------------------------------------------------------
read_schedstat() {
    local pid="$1"
    local node_ip="$2"

    local raw
    raw="$(ssh_node "$node_ip" "cat /proc/$pid/schedstat 2>/dev/null" 2>/dev/null || true)"

    if [[ -z "$raw" ]]; then
        printf '{"pid":%s,"sched_info":{"run_delay":0,"pcount":0},"error":"schedstat not found"}' "$pid"
        return 1
    fi

    # /proc/<pid>/schedstat: cpu_time run_delay pcount (all in ns)
    local cpu_time="" run_delay="" pcount=""
    read -r cpu_time run_delay pcount _ <<< "$raw" || true

    jq -n \
        --arg pid "$pid" \
        --argjson run_delay "${run_delay:-0}" \
        --argjson pcount "${pcount:-0}" \
        --argjson cpu_time "${cpu_time:-0}" \
        '{
            pid: $pid,
            sched_info: {
                run_delay: $run_delay,
                pcount: $pcount,
                cpu_time: $cpu_time
            }
        }'
}

# ---------------------------------------------------------------------------
# read_proc_sched — Read /proc/<pid>/sched EEVDF fields for a PID on a node
#
# Usage: read_proc_sched <pid> <node_ip>
# Returns: JSON with EEVDF-specific fields (se.*, se.statistics.*)
# ---------------------------------------------------------------------------
read_proc_sched() {
    local pid="$1"
    local node_ip="$2"

    local raw
    raw="$(ssh_node "$node_ip" "cat /proc/$pid/sched 2>/dev/null" 2>/dev/null || true)"

    if [[ -z "$raw" ]]; then
        printf '{"pid":%s,"error":"proc sched not found"}' "$pid"
        return 1
    fi

    # Parse key EEVDF fields from /proc/<pid>/sched
    local se_sum_exec_runtime="" se_vruntime="" se_nr_migrations=""
    local nr_switches="" nr_voluntary_switches="" nr_involuntary_switches=""
    local wait_sum="" sleep_sum="" iowait_sum=""

    while IFS= read -r line; do
        [[ "$line" != *:* ]] && continue
        [[ "$line" == *---* ]] && continue

        # Split on ':'
        local key="${line%%:*}"
        local val="${line#*:}"
        # Trim whitespace
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        # Remove trailing comma
        val="${val%,}"

        case "$key" in
            se.sum_exec_runtime)     se_sum_exec_runtime="$val" ;;
            se.vruntime)             se_vruntime="$val" ;;
            se.nr_migrations)        se_nr_migrations="$val" ;;
            nr_switches)             nr_switches="$val" ;;
            nr_voluntary_switches)   nr_voluntary_switches="$val" ;;
            nr_involuntary_switches) nr_involuntary_switches="$val" ;;
            se.statistics.wait_sum)  wait_sum="$val" ;;
            se.statistics.sleep_sum) sleep_sum="$val" ;;
            se.statistics.iowait_sum) iowait_sum="$val" ;;
        esac
    done <<< "$raw"

    jq -n \
        --arg pid "$pid" \
        --arg se_sum_exec_runtime "${se_sum_exec_runtime:-0}" \
        --arg se_vruntime "${se_vruntime:-0}" \
        --arg se_nr_migrations "${se_nr_migrations:-0}" \
        --arg nr_switches "${nr_switches:-0}" \
        --arg nr_voluntary_switches "${nr_voluntary_switches:-0}" \
        --arg nr_involuntary_switches "${nr_involuntary_switches:-0}" \
        --arg wait_sum "${wait_sum:-0}" \
        --arg sleep_sum "${sleep_sum:-0}" \
        --arg iowait_sum "${iowait_sum:-0}" \
        '{
            pid: $pid,
            se: {
                sum_exec_runtime: ($se_sum_exec_runtime | tonumber? // 0),
                vruntime: ($se_vruntime | tonumber? // 0),
                nr_migrations: ($se_nr_migrations | tonumber? // 0)
            },
            statistics: {
                nr_switches: ($nr_switches | tonumber? // 0),
                nr_voluntary_switches: ($nr_voluntary_switches | tonumber? // 0),
                nr_involuntary_switches: ($nr_involuntary_switches | tonumber? // 0),
                wait_sum: ($wait_sum | tonumber? // 0),
                sleep_sum: ($sleep_sum | tonumber? // 0),
                iowait_sum: ($iowait_sum | tonumber? // 0)
            }
        }'
}

# ---------------------------------------------------------------------------
# pod_name_to_cgroup_path — Build cgroup v2 path for a pod on its node
#
# Usage: pod_name_to_cgroup_path <pod_name>
# Returns: Full cgroup path for the pod
# ---------------------------------------------------------------------------
pod_name_to_cgroup_path() {
    local pod_name="$1"
    resolve_project_root

    local node_ip
    node_ip="$(get_pod_node_ip "$pod_name")" || return 1

    local pod_uid
    pod_uid="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
        -o jsonpath='{.metadata.uid}' 2>/dev/null)" || {
        log_error "Failed to get UID for pod '$pod_name'"
        return 1
    }

    # For systemd cgroup driver: kubepods.slice/kubepods-pod<uid>.slice/
    local cgroup_path="/sys/fs/cgroup/kubepods.slice/kubepods-pod${pod_uid}.slice"

    # Verify the path exists on the node
    ssh_node "$node_ip" "test -d '${cgroup_path}'" 2>/dev/null || {
        # Fallback: search for pod UID in cgroup tree
        cgroup_path="$(ssh_node "$node_ip" \
            "find /sys/fs/cgroup/kubepods.slice/ -maxdepth 2 -name '*${pod_uid}*' -type d 2>/dev/null | head -1")" || {
            log_error "Cannot find cgroup path for pod '$pod_name' on node $node_ip"
            return 1
        }
        if [[ -z "$cgroup_path" ]]; then
            log_error "Cannot find cgroup path for pod '$pod_name' on node $node_ip (search returned empty)"
            return 1
        fi
    }

    echo "$cgroup_path"
}

# ---------------------------------------------------------------------------
# get_container_cgroup_path — Get container-specific cgroup path for a pod
#
# Usage: get_container_cgroup_path <pod_name> <container_name>
# Returns: Full cgroup path for the container
# ---------------------------------------------------------------------------
get_container_cgroup_path() {
    local pod_name="$1"
    local container_name="$2"
    resolve_project_root

    local node_ip
    node_ip="$(get_pod_node_ip "$pod_name")" || return 1

    local pod_cgroup_path
    pod_cgroup_path="$(pod_name_to_cgroup_path "$pod_name")" || return 1

    local container_id
    container_id="$(get_container_id "$node_ip" "$pod_name" "$container_name")" || return 1

    # For systemd cgroup driver: <pod-path>/crio-<container_id>.scope/
    local container_cgroup_path="${pod_cgroup_path}/crio-${container_id}.scope"

    ssh_node "$node_ip" "test -d '${container_cgroup_path}'" 2>/dev/null || {
        # Fallback: flat cgroup path (no scope suffix)
        container_cgroup_path="${pod_cgroup_path}"
    }

    echo "$container_cgroup_path"
}

# ---------------------------------------------------------------------------
# check_sched_debug_available — Test if /proc/sched_debug is readable on node
#
# Usage: check_sched_debug_available <node_ip>
# Returns: 0 if available, 1 if not
# ---------------------------------------------------------------------------
check_sched_debug_available() {
    local node_ip="$1"
    ssh_node "$node_ip" "test -r /proc/sched_debug && head -1 /proc/sched_debug >/dev/null 2>&1" 2>/dev/null
}

# ---------------------------------------------------------------------------
# parse_sched_debug_csv — Parse /proc/sched_debug into CSV
#
# Usage: parse_sched_debug_csv <raw_content> [pid_filter]
# Outputs CSV: timestamp,cpu,entity,vruntime,deadline,exec_start,min_vruntime
#
# Uses python3 for robust columnar parsing of the sched_debug runnable tasks table.
# If pid_filter is provided (comma-separated), only matching PIDs are output.
# ---------------------------------------------------------------------------
parse_sched_debug_csv() {
    local raw_content="$1"
    local pid_filter="${2:-}"

    python3 -c "
import sys, csv, re, io
from datetime import datetime, timezone

text = sys.stdin.read()
lines = text.split('\n')

output = csv.writer(sys.stdout)
output.writerow(['timestamp', 'cpu', 'entity', 'vruntime', 'deadline', 'exec_start', 'min_vruntime'])

ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# Parse PID filter
pid_set = set()
if '$pid_filter':
    for p in '$pid_filter'.split(','):
        p = p.strip()
        if p:
            pid_set.add(p)

current_cpu = -1
current_min_vruntime = ''
in_runnable = False
header_line = False

for line in lines:
    # Detect CPU section
    cpu_match = re.match(r'^cpu#(\d+)', line)
    if cpu_match:
        current_cpu = int(cpu_match.group(1))
        current_min_vruntime = ''
        in_runnable = False
        header_line = False
        continue

    # Track min_vruntime from cfs_rq section
    if current_cpu >= 0 and '.min_vruntime' in line:
        # Format: '.min_vruntime                    : 12345.678901'
        parts = line.split(':')
        if len(parts) >= 2:
            current_min_vruntime = parts[-1].strip()

    # Detect runnable tasks section
    if 'runnable tasks:' in line:
        in_runnable = True
        header_line = True
        continue

    if not in_runnable:
        continue

    # Skip the header line after 'runnable tasks:'
    if header_line:
        header_line = False
        continue

    # Parse task lines
    # Format (columnar): S  task_name  pid  tree-key  exec-start  min-vruntime  deadline  sw ants
    parts = line.split()
    if len(parts) < 7:
        continue

    state = parts[0]
    if state not in ('R', 'S', 'D', 'T', 't', 'X', 'Z', 'P', 'I'):
        continue

    task_name = parts[1]
    task_pid = parts[2]
    tree_key = parts[3]
    exec_start = parts[4]
    min_vruntime_col = parts[5]
    deadline = parts[6]

    # Apply PID filter (match against task_pid or task_pid from filter)
    if pid_set and task_pid not in pid_set:
        continue

    # Use cfs_rq min_vruntime if available, else column value
    min_vr = current_min_vruntime or min_vruntime_col

    output.writerow([ts, str(current_cpu), task_name + '/' + task_pid,
                     tree_key, deadline, exec_start, min_vr])
" <<< "$raw_content"
}
