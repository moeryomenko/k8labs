#!/usr/bin/env bash
#
# perfetto-common.sh — Shared library for Perfetto trace lifecycle management
#
# This library is sourced by perfetto-start.sh, perfetto-stop.sh,
# and perfetto-capture.sh. Provides path resolution, SSH helpers,
# and config management for remote Perfetto tracing on KVM nodes.
#
# Usage in scripts:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/perfetto-common.sh"

# Guard against double-sourcing
# Use return 0 to ensure clean exit code on re-source (avoids set -e conflict)
[[ -z ${_PERFETTO_COMMON_SH:-} ]] || return 0
_PERFETTO_COMMON_SH=1
readonly _PERFETTO_COMMON_SH

# ---- Strict Mode ----
set -Eeuo pipefail
shopt -s inherit_errexit

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# ---- Source dependencies ----
# Source cgroup-common.sh for ssh_node, resolve_project_root, etc.
# Use || true to handle guard return (cgroup-common.sh guard uses bare `return`
# which propagates exit code 1 on re-source, conflicting with set -e).
# shellcheck source=../../bin/cgroup-common.sh
source "$SCRIPT_DIR/../../bin/cgroup-common.sh" || true

# experiments/common.sh is intentionally not sourced here because its inner
# `set -Eeuo pipefail` re-enables errexit, and its own source of cgroup-common.sh
# would trigger the guard return (exit 1), killing the shell before control
# returns. All needed functions (ssh_node, resolve_project_root, etc.) are
# provided by cgroup-common.sh directly.

# ---------------------------------------------------------------------------
# perfetto_binary_path — Echo the path to the tracebox binary on a node
# ---------------------------------------------------------------------------
perfetto_binary_path() {
    printf '%s\n' "/usr/bin/tracebox"
}

# ---------------------------------------------------------------------------
# perfetto_config_path — Resolve a config name to a /tmp/ path on a node
#
# Handles:
#   "scheduling"          -> /tmp/scheduling.cfg
#   "scheduling.cfg"      -> /tmp/scheduling.cfg
#   "/tmp/custom.cfg"     -> /tmp/custom.cfg
#   ""                    -> error, returns 1
# ---------------------------------------------------------------------------
perfetto_config_path() {
    local name="${1:-}"
    if [[ -z "$name" ]]; then
        printf 'ERROR: perfetto_config_path: config name is required\n' >&2
        exit 1
    fi
    local base
    base="$(basename "$name")"
    if [[ "$base" != *.* ]]; then
        base="${base}.cfg"
    fi
    printf '/tmp/%s\n' "$base"
}

# ---------------------------------------------------------------------------
# resolve_node_ip — Resolve a KVM node name to its IP address
#
# Delegates to resolve_node_ip_from_name from cgroup-common.sh, which
# uses Terraform state and SSH hostname checks to find the IP.
# ---------------------------------------------------------------------------
resolve_node_ip() {
    local node_name="${1:-}"
    if [[ -z "$node_name" ]]; then
        log_error "resolve_node_ip: node name is required"
        exit 1
    fi
    resolve_node_ip_from_name "$node_name" || {
        log_error "Could not resolve IP for node '$node_name'"
        exit 1
    }
}

# ---------------------------------------------------------------------------
# check_tracebox_available — Return 0 if tracebox binary exists on the node
# ---------------------------------------------------------------------------
check_tracebox_available() {
    local node_ip="${1:-}"
    if [[ -z "$node_ip" ]]; then
        log_error "check_tracebox_available: node IP is required"
        exit 1
    fi
    ssh_node "$node_ip" "test -x /usr/bin/tracebox" >/dev/null 2>&1 || {
        log_error "tracebox not available on $node_ip"
        exit 1
    }
}

# ---------------------------------------------------------------------------
# upload_config — SCP a local trace config file to /tmp/ on the node
#
# Looks for the config file in research/cpu-sched/perfetto/configs/ relative to the
# script directory.
# ---------------------------------------------------------------------------
upload_config() {
    local node_ip="${1:-}"
    local config_name="${2:-}"
    if [[ -z "$node_ip" ]]; then
        log_error "upload_config: node IP is required"
        return 1
    fi
    if [[ -z "$config_name" ]]; then
        log_error "upload_config: config name is required"
        return 1
    fi

    local remote_path
    remote_path="$(perfetto_config_path "$config_name")" || return 1
    local base_name
    base_name="$(basename "$remote_path")"
    local local_path="${SCRIPT_DIR}/../configs/${base_name}"

    if [[ ! -f "$local_path" ]]; then
        log_error "Local config file not found: $local_path"
        return 1
    fi

    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        printf '[DRY-RUN] scp %s root@%s:%s\n' "$local_path" "$node_ip" "$remote_path"
        return 0
    fi

    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=5 -o BatchMode=yes \
        "$local_path" "root@${node_ip}:${remote_path}" >/dev/null 2>&1 || {
        log_error "Failed to upload config to ${node_ip}:${remote_path}"
        return 1
    }
}
