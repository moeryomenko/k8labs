#!/usr/bin/env bash
#
# cgroup-common.sh — Shared utilities for cgroup observation tools
#
# This library is sourced by all cgroup-observe.sh, cgroup-watch.sh,
# cgroup-snapshot.sh, and trace-lifecycle.sh.
#
# Usage in scripts:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cgroup-common.sh"

# Guard against double-sourcing
[[ -z ${_CGROUP_COMMON_SH:-} ]] || return
_CGROUP_COMMON_SH=1
readonly _CGROUP_COMMON_SH

# ---- Strict Mode ----
set -Eeuo pipefail

# ---- Paths ----
# KUBECONFIG defaults to <project-root>/kubeconfig, overridable via env
: "${KUBECONFIG:=}"

# ---- Helpers ----

# ---------------------------------------------------------------------------
# find_project_root — Locate k8labs project root from any working directory
#
# Searches upward for Makefile + terraform/ markers, then falls back to
# git rev-parse. Exits 1 if not found.
# ---------------------------------------------------------------------------
find_project_root() {
    local dir
    dir="$(git rev-parse --show-toplevel 2>/dev/null)" && {
        echo "$dir"
        return 0
    }

    dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/Makefile" && -d "$dir/terraform" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done

    printf 'ERROR: Cannot find k8labs project root (no Makefile + terraform/ found)\n' >&2
    return 1
}

# ---------------------------------------------------------------------------
# resolve_project_root — Set global PROJECT_ROOT and KUBECONFIG
# ---------------------------------------------------------------------------
resolve_project_root() {
    if [[ -z ${PROJECT_ROOT:-} ]]; then
        PROJECT_ROOT="$(find_project_root)" || exit 1
        readonly PROJECT_ROOT
    fi

    if [[ -z "$KUBECONFIG" ]]; then
        KUBECONFIG="$PROJECT_ROOT/kubeconfig"
    fi
    export KUBECONFIG
}

# ---------------------------------------------------------------------------
# require_tools — Check that required CLI tools are on PATH
# ---------------------------------------------------------------------------
require_tools() {
    local -a missing=()
    local cmd
    for cmd in "$@"; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        printf 'ERROR: Required tools not found: %s\n' "${missing[*]}" >&2
        return 1
    fi
}

# ---------------------------------------------------------------------------
# log_info / log_error — Structured logging to stderr
# ---------------------------------------------------------------------------
log_info()  { printf '[INFO]  %s\n' "$*" >&2; }
log_error() { printf '[ERROR] %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# get_worker_ips — Returns space-separated list of worker IPs from Terraform
# ---------------------------------------------------------------------------
get_worker_ips() {
    tofu -chdir="$PROJECT_ROOT/terraform" output -json worker_ips 2>/dev/null \
        | python3 -c "import sys,json; ips=json.load(sys.stdin); print(' '.join(filter(None, ips)))" 2>/dev/null \
        || { log_error "Failed to get worker IPs from Terraform state"; return 1; }
}

# ---------------------------------------------------------------------------
# get_cp_ip — Returns control-plane IP from Terraform
# ---------------------------------------------------------------------------
get_cp_ip() {
    tofu -chdir="$PROJECT_ROOT/terraform" output -raw control_plane_ip 2>/dev/null \
        || { log_error "Failed to get control-plane IP from Terraform state"; return 1; }
}

# ---------------------------------------------------------------------------
# ssh_node — Run a command on a node via SSH
# Usage: ssh_node <ip> <command>
# ---------------------------------------------------------------------------
ssh_node() {
    local ip="$1"
    shift
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=5 -o BatchMode=yes \
        "root@$ip" "$@"
}

# ---------------------------------------------------------------------------
# get_pod_node — Get the node name where a pod is running
# ---------------------------------------------------------------------------
get_pod_node() {
    local pod_name="$1"
    kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" -o wide --no-headers 2>/dev/null \
        | awk '{print $7}' \
        || { log_error "Pod '$pod_name' not found or kubectl failed"; return 1; }
}

# ---------------------------------------------------------------------------
# get_pod_node_ip — Get the IP of the node running a pod
#
# This gets the node name from the pod spec, then maps it to an IP.
# Workers don't have Kubernetes node IPs accessible this way, so we
# resolve from Terraform state by node index.
# ---------------------------------------------------------------------------
get_pod_node_ip() {
    local pod_name="$1"
    local node_name
    node_name="$(get_pod_node "$pod_name")" || return 1

    # Try to get the IP from terraform node_names output by index
    local node_idx
    node_idx=$(tofu -chdir="$PROJECT_ROOT/terraform" output -json node_names 2>/dev/null \
        | python3 -c "
import sys,json
names = json.load(sys.stdin)
target = '$node_name'
for i, n in enumerate(names):
    if n == target:
        print(i)
        sys.exit(0)
print(-1)
") || true

    # If index found and it's 0 → CP IP, else worker IP
    if [[ -n "$node_idx" && "$node_idx" -ge 0 ]]; then
        if [[ "$node_idx" -eq 0 ]]; then
            get_cp_ip
        else
            local worker_idx=$(( node_idx - 1 ))
            tofu -chdir="$PROJECT_ROOT/terraform" output -json worker_ips 2>/dev/null \
                | python3 -c "
import sys,json
ips = json.load(sys.stdin)
idx = $worker_idx
if idx < len(ips) and ips[idx]:
    print(ips[idx])
else:
    print(-1)
" || { log_error "Could not resolve IP for node '$node_name'"; return 1; }
        fi
    else
        # Fallback: try to find by iterating all worker IPs and SSH-checking hostname
        local cp_ip worker_ips_fallback
        cp_ip="$(get_cp_ip 2>/dev/null || true)"
        if [[ -n "$cp_ip" ]]; then
            local hostname_on_node
            hostname_on_node="$(ssh_node "$cp_ip" "hostname" 2>/dev/null || true)"
            if [[ "$hostname_on_node" == "$node_name" ]]; then
                echo "$cp_ip"
                return 0
            fi
        fi
        worker_ips_fallback="$(get_worker_ips 2>/dev/null || true)"
        local ip
        for ip in $worker_ips_fallback; do
            hostname_on_node="$(ssh_node "$ip" "hostname" 2>/dev/null || true)"
            if [[ "$hostname_on_node" == "$node_name" ]]; then
                echo "$ip"
                return 0
            fi
        done
        log_error "Could not resolve IP for node '$node_name'"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# get_container_id — Get CRI-O container ID for a container name on a node
# Usage: get_container_id <node_ip> <container_name>
# ---------------------------------------------------------------------------
get_container_id() {
    local node_ip="$1"
    local container_name="$2"

    local cid
    cid="$(ssh_node "$node_ip" "/usr/bin/crictl ps -a --name '$container_name' --quiet" 2>/dev/null)" || {
        log_error "Container '$container_name' not found on node $node_ip"
        return 1
    }

    if [[ -z "$cid" ]]; then
        log_error "Container '$container_name' not found on node $node_ip (empty response)"
        return 1
    fi

    echo "$cid"
}

# ---------------------------------------------------------------------------
# get_container_pid — Get PID of a container from its CRI-O ID on a node
# ---------------------------------------------------------------------------
get_container_pid() {
    local node_ip="$1"
    local container_id="$2"

    local pid
    pid="$(ssh_node "$node_ip" "/usr/bin/crictl inspect '$container_id' 2>/dev/null | jq -r '.info.pid'")" || {
        log_error "Failed to get PID for container $container_id on node $node_ip"
        return 1
    }

    if [[ -z "$pid" || "$pid" == "null" ]]; then
        log_error "Container $container_id has no valid PID (not running?)"
        return 1
    fi

    echo "$pid"
}

# ---------------------------------------------------------------------------
# get_cgroup_path — Get the cgroup v2 path for a PID on a node
# Result is the full path under /sys/fs/cgroup
# ---------------------------------------------------------------------------
get_cgroup_path() {
    local node_ip="$1"
    local pid="$2"

    local rel_path
    rel_path="$(ssh_node "$node_ip" "cat /proc/$pid/cgroup 2>/dev/null | grep '0::' | cut -d: -f3")" || {
        log_error "Failed to read cgroup path for PID $pid on node $node_ip"
        return 1
    }

    if [[ -z "$rel_path" ]]; then
        log_error "No cgroup v2 path found for PID $pid (cgroups v1?)"
        return 1
    fi

    echo "/sys/fs/cgroup$rel_path"
}

# ---------------------------------------------------------------------------
# read_cgroup_file — Read a single cgroup file on a node, trimming whitespace
# ---------------------------------------------------------------------------
read_cgroup_file() {
    local node_ip="$1"
    local cgroup_path="$2"
    local filename="$3"

    local value
    value="$(ssh_node "$node_ip" "cat '$cgroup_path/$filename' 2>/dev/null" || true)"

    if [[ -z "$value" ]]; then
        printf ''
        return 1
    fi

    # Preserve newlines for multi-line files (e.g. cpu.stat)
    printf '%s\n' "$value"
}

# ---------------------------------------------------------------------------
# parse_cpu_max — Parse cpu.max ("quota period" or "max period") into
# quota_us and period_us. Sets cpu_max_quota and cpu_max_period variables.
# ---------------------------------------------------------------------------
parse_cpu_max() {
    local raw="$1"
    local _quota_var="${2:-cpu_max_quota}"
    local _period_var="${3:-cpu_max_period}"

    local quota period
    read -r quota period <<< "$raw"

    if [[ "$quota" == "max" ]]; then
        printf -v "$_quota_var" '%s' "max"
    else
        printf -v "$_quota_var" '%s' "$quota"
    fi
    printf -v "$_period_var" '%s' "${period:-100000}"
}

# ---------------------------------------------------------------------------
# read_cpu_stat — Parse cpu.stat into a JSON-like string of key=value pairs
# Returns output suitable for jq processing
# ---------------------------------------------------------------------------
read_cpu_stat() {
    local node_ip="$1"
    local cgroup_path="$2"

    local stat_raw
    stat_raw="$(read_cgroup_file "$node_ip" "$cgroup_path" "cpu.stat" 2>/dev/null || true)"

    if [[ -z "$stat_raw" ]]; then
        printf '{"usage_usec":0,"nr_periods":0,"nr_throttled":0,"throttled_usec":0}'
        return 1
    fi

    local usage_usec=0 nr_periods=0 nr_throttled=0 throttled_usec=0
    while IFS=' ' read -r key val; do
        case "$key" in
            usage_usec)    usage_usec=$val ;;
            nr_periods)    nr_periods=$val ;;
            nr_throttled)  nr_throttled=$val ;;
            throttled_usec) throttled_usec=$val ;;
        esac
    done <<< "$stat_raw"

    printf '{"usage_usec":%s,"nr_periods":%s,"nr_throttled":%s,"throttled_usec":%s}' \
        "$usage_usec" "$nr_periods" "$nr_throttled" "$throttled_usec"
}

# ---------------------------------------------------------------------------
# get_cgroup_data — Gather all standard cgroup CPU data for a container
#
# Returns JSON with: pod, container, node, cgroup_path, cpu_weight,
# cpu_max_quota_us, cpu_max_period_us, cpu_stat{...}
# ---------------------------------------------------------------------------
get_cgroup_data() {
    local pod_name="$1"
    local container_name="${2:-}"

    # Resolve project root and KUBECONFIG
    resolve_project_root

    # Verify cluster reachable
    kubectl --kubeconfig "$KUBECONFIG" cluster-info --request-timeout=5s &>/dev/null || {
        log_error "Cannot reach Kubernetes cluster — is kubeconfig valid?"
        return 1
    }

    # Get node IP
    local node_ip
    node_ip="$(get_pod_node_ip "$pod_name")" || return 1

    # Get container name (default to first container if not specified)
    if [[ -z "$container_name" ]]; then
        container_name="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
            -o jsonpath='{.spec.containers[0].name}' 2>/dev/null)" || {
            log_error "Failed to get container name from pod '$pod_name'"
            return 1
        }
    fi

    # Get container ID
    local container_id
    container_id="$(get_container_id "$node_ip" "$container_name")" || return 1

    # Get PID
    local pid
    pid="$(get_container_pid "$node_ip" "$container_id")" || return 1

    # Get cgroup path
    local cgroup_path
    cgroup_path="$(get_cgroup_path "$node_ip" "$pid")" || return 1

    # Read cgroup files
    local cpu_weight cpu_max_raw
    cpu_weight="$(read_cgroup_file "$node_ip" "$cgroup_path" "cpu.weight" || printf '')"
    cpu_max_raw="$(read_cgroup_file "$node_ip" "$cgroup_path" "cpu.max" || printf 'max 100000')"

    local cpu_max_quota cpu_max_period
    parse_cpu_max "$cpu_max_raw" cpu_max_quota cpu_max_period

    local cpu_stat_json
    cpu_stat_json="$(read_cpu_stat "$node_ip" "$cgroup_path")"

    # Get pod node name
    local node_name
    node_name="$(get_pod_node "$pod_name" 2>/dev/null || printf 'unknown')"

    # Build JSON output
    jq -n \
        --arg pod "$pod_name" \
        --arg container "$container_name" \
        --arg node "$node_name" \
        --arg cgroup_path "$cgroup_path" \
        --arg cpu_weight "${cpu_weight:-}" \
        --arg cpu_max_quota "${cpu_max_quota:-}" \
        --arg cpu_max_period "${cpu_max_period:-}" \
        --argjson cpu_stat "$cpu_stat_json" \
        '{
            pod: $pod,
            container: $container,
            node: $node,
            cgroup_path: $cgroup_path,
            cpu_weight: $cpu_weight,
            cpu_max_quota_us: $cpu_max_quota,
            cpu_max_period_us: $cpu_max_period,
            cpu_stat: $cpu_stat
        }'
}

# ---------------------------------------------------------------------------
# resolve_node_ip_from_name — Match a node name to an IP by SSH hostname check
# ---------------------------------------------------------------------------
resolve_node_ip_from_name() {
    local target_name="$1"
    local cp_ip worker_ips_fallback hostname_on_node ip

    cp_ip="$(get_cp_ip 2>/dev/null || true)"
    if [[ -n "$cp_ip" ]]; then
        hostname_on_node="$(ssh_node "$cp_ip" "hostname" 2>/dev/null || true)"
        if [[ "$hostname_on_node" == "$target_name" ]]; then
            echo "$cp_ip"
            return 0
        fi
    fi

    worker_ips_fallback="$(get_worker_ips 2>/dev/null || true)"
    for ip in $worker_ips_fallback; do
        hostname_on_node="$(ssh_node "$ip" "hostname" 2>/dev/null || true)"
        if [[ "$hostname_on_node" == "$target_name" ]]; then
            echo "$ip"
            return 0
        fi
    done

    return 1
}
