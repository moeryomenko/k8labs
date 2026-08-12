#!/usr/bin/env bash
#
# switch-cpu-manager.sh — Toggle kubelet CPU manager policy between none and static
#
# Usage:
#   switch-cpu-manager.sh {none|static} [--dry-run] [--yes]
#
# Switches the CPU manager policy on all worker nodes. Drains each node,
# modifies the kubelet config, restarts kubelet, uncordons, and verifies
# the node returns to Ready state.
#
# Requires:
#   - KUBECONFIG env var or kubeconfig file in project root
#   - kubectl, ssh, tofu/terraform on PATH
#   - SSH key-based access to worker nodes as root
#
# Safety:
#   - Backs up kubelet config before modification
#   - Traps EXIT to uncordon nodes on failure
#   - Asks confirmation unless --yes is passed
#   - Max 5-minute timeout per drain/health operation

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

# ---- Constants ----
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
KUBELET_CONFIG_PATHS=(
    "/etc/kubernetes/kubelet-config.yaml"
    "/var/lib/kubelet/kubelet-config.yaml"
    "/etc/kubernetes/kubelet.conf"
)

# Source the shared DHCP lease helper for worker IP discovery
# shellcheck source=../bin/lease-common.sh
source "${SCRIPT_DIR}/../bin/lease-common.sh"

# ---- Logging ----
log_info()  { printf '[%s] INFO:  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
log_warn()  { printf '[%s] WARN:  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
log_error() { printf '[%s] ERROR: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }

# ---- Global state for rollback ----
declare -a _UNCORDON_QUEUE=()
declare -a _WORKER_IPS=()
DRY_RUN=false
YES=false
TARGET_POLICY=""

# ---- Cleanup / Rollback ----
_cleanup() {
    local exit_code=$?

    if [[ ${#_UNCORDON_QUEUE[@]} -gt 0 ]]; then
        log_warn "Script exiting — uncordoning queued nodes..."
        local node
        for node in "${_UNCORDON_QUEUE[@]}"; do
            log_info "Uncordoning node: $node"
            if [[ "$DRY_RUN" == "true" ]]; then
                log_info "[DRY-RUN] Would uncordon node: $node"
            else
                kubectl --kubeconfig "$KUBECONFIG" uncordon "$node" 2>/dev/null || true
            fi
        done
    fi

    trap - EXIT ERR
    exit "$exit_code"
}
trap _cleanup EXIT

_error_handler() {
    local line=$1
    local cmd=$2
    log_error "Command failed at line $line: $cmd"
}
trap '_error_handler $LINENO "$BASH_COMMAND"' ERR

# ---- Help ----
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME {none|static} [OPTIONS]

Toggle kubelet CPU manager policy on all worker nodes.

Positional:
  none                  Set CPU manager policy to "None" (default, no pinning)
  static                Set CPU manager policy to "Static" (exclusive CPU pinning
                        for Guaranteed QoS pods)

Options:
  --dry-run             Print what would be done without making changes
  --yes                 Skip confirmation prompt
  -h, --help            Show this help and exit

Safety:
  - Each node is drained (cordon + evict) before modification
  - Kubelet config is backed up before modification
  - On failure, queued nodes are uncordoned automatically
  - Each operation has a 5-minute timeout

Examples:
  $SCRIPT_NAME none                 # Switch to None (default)
  $SCRIPT_NAME static --dry-run     # Preview switching to Static
  $SCRIPT_NAME static --yes         # Switch to Static without confirmation
EOF
    exit "${1:-0}"
}

# ---- Dependency Checking ----
check_deps() {
    local -a missing=()
    local cmd
    for cmd in kubectl ssh tofu terraform; do
        if ! command -v "$cmd" &>/dev/null; then
            # tofu/terraform are optional (need at least one)
            if [[ "$cmd" == "tofu" || "$cmd" == "terraform" ]]; then
                continue
            fi
            missing+=("$cmd")
        fi
    done

    # Check for either tofu or terraform
    if ! command -v tofu &>/dev/null && ! command -v terraform &>/dev/null; then
        missing+=("tofu or terraform")
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Required tools not found: ${missing[*]}"
        return 1
    fi
}

# ---- Project Root and KUBECONFIG Resolution ----
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

    log_error "Cannot find k8labs project root (no Makefile + terraform/ found)"
    return 1
}

resolve_project_root() {
    if [[ -z ${PROJECT_ROOT:-} ]]; then
        PROJECT_ROOT="$(find_project_root)" || exit 1
        readonly PROJECT_ROOT
    fi

    if [[ -z "${KUBECONFIG:-}" ]]; then
        KUBECONFIG="$PROJECT_ROOT/kubeconfig"
    fi
    export KUBECONFIG

    if [[ ! -f "$KUBECONFIG" ]]; then
        log_error "Kubeconfig not found at '$KUBECONFIG'. Set KUBECONFIG env var."
        return 1
    fi
}

# ---- Worker IP Discovery ----
# get_worker_ips comes from lease-common.sh (sourced above): it resolves
# worker IPs from the systemd-networkd DHCP server lease (authoritative) with
# a dnsmasq fallback, ordered by WORKER_MACS.

# ---- SSH Helper ----
ssh_node() {
    local ip="$1"
    shift
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=5 -o BatchMode=yes \
        "root@$ip" "$@"
}

# ---- Kubelet Config Operations ----
# check_kubelet_config_path — Find the kubelet config file on a worker node
# Returns the config path on stdout, or empty string if not found
check_kubelet_config_path() {
    local ip="$1"
    local path
    for path in "${KUBELET_CONFIG_PATHS[@]}"; do
        if ssh_node "$ip" "test -f '$path'" 2>/dev/null; then
            echo "$path"
            return 0
        fi
    done
    return 1
}

# get_kubelet_version — Get kubelet version on a worker node
get_kubelet_version() {
    local ip="$1"
    ssh_node "$ip" "kubelet --version 2>/dev/null" || echo "unknown"
}

# modify_kubelet_config — Set cpuManagerPolicy in kubelet config via SSH
# Uses sed to change the policy value or insert it if missing
modify_kubelet_config() {
    local ip="$1"
    local policy="$2"  # "None" or "Static"

    local config_path
    config_path="$(check_kubelet_config_path "$ip")" || {
        log_error "Cannot find kubelet config file on worker $ip"
        return 1
    }

    log_info "Modifying kubelet config on $ip: $config_path"

    # Backup the config
    local backup_path
    backup_path="${config_path}.backup.$(date -u +'%Y%m%dT%H%M%SZ')"
    ssh_node "$ip" "cp '$config_path' '$backup_path'" || {
        log_error "Failed to backup kubelet config on $ip"
        return 1
    }
    log_info "Backup saved to $backup_path on $ip"

    # Modify the cpuManagerPolicy field using sed
    # If the field exists, replace its value
    # If not, insert after 'kind: KubeletConfiguration' or at end of file

    local has_field
    has_field="$(ssh_node "$ip" "grep -c 'cpuManagerPolicy:' '$config_path' 2>/dev/null || true")"

    if [[ "$has_field" -gt 0 ]]; then
        # Field exists — replace it
        ssh_node "$ip" "sed -i 's/cpuManagerPolicy:.*/cpuManagerPolicy: $policy/' '$config_path'" || {
            log_error "Failed to modify cpuManagerPolicy in $config_path on $ip"
            return 1
        }
    else
        # Field does not exist — insert it after the apiVersion or kind line
        ssh_node "$ip" "sed -i '0,/^kind:/{/^kind:/a\\
cpuManagerPolicy: $policy
}' '$config_path'" || {
            log_error "Failed to insert cpuManagerPolicy in $config_path on $ip"
            return 1
        }
    fi

    # Verify the change
    local new_value
    new_value="$(ssh_node "$ip" "grep 'cpuManagerPolicy:' '$config_path' 2>/dev/null | head -1 | awk '{print \$2}'")"
    if [[ "$new_value" != "$policy" ]]; then
        log_error "Failed to verify cpuManagerPolicy change on $ip (got: $new_value, expected: $policy)"
        return 1
    fi

    log_info "cpuManagerPolicy set to '$policy' on $ip"
    return 0
}

# restart_kubelet — Restart kubelet service on a worker node and wait for health
restart_kubelet() {
    local ip="$1"
    local max_wait="${2:-60}"

    log_info "Restarting kubelet on $ip..."
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] Would restart kubelet on $ip"
        return 0
    fi

    ssh_node "$ip" "systemctl restart kubelet" || {
        log_error "Failed to restart kubelet on $ip"
        return 1
    }

    # Wait for kubelet to become healthy
    log_info "Waiting for kubelet to become healthy on $ip (max ${max_wait}s)..."
    local deadline=$(( $(date +%s) + max_wait ))
    while [[ $(date +%s) -lt $deadline ]]; do
        local status
        status="$(ssh_node "$ip" "systemctl is-active kubelet 2>/dev/null" || true)"
        if [[ "$status" == "active" ]]; then
            log_info "Kubelet is active on $ip"
            return 0
        fi
        sleep 2
    done

    log_error "Kubelet did not become active on $ip within ${max_wait}s"
    return 1
}

# ---- Node Operations ----
drain_node() {
    local node="$1"
    local max_wait="${2:-300}"

    log_info "Draining node: $node (timeout: ${max_wait}s)"
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] Would drain node: $node"
        _UNCORDON_QUEUE+=("$node")
        return 0
    fi

    # Cordon first
    kubectl --kubeconfig "$KUBECONFIG" cordon "$node" --request-timeout=30s || {
        log_error "Failed to cordon node $node"
        return 1
    }

    # Drain with timeout
    kubectl --kubeconfig "$KUBECONFIG" drain "$node" \
        --ignore-daemonsets --delete-emptydir-data \
        --timeout="${max_wait}s" --force 2>/dev/null || {
        local drain_exit=$?
        log_warn "Drain on $node exited with code $drain_exit (may be partial)"
        # Continue — some pods may not evict but we can still proceed
    }

    _UNCORDON_QUEUE+=("$node")
    log_info "Node $node drained"
}

uncordon_node() {
    local node="$1"

    log_info "Uncordoning node: $node"
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] Would uncordon node: $node"
        # Remove from queue
        _UNCORDON_QUEUE=("${_UNCORDON_QUEUE[@]/$node}")
        return 0
    fi

    kubectl --kubeconfig "$KUBECONFIG" uncordon "$node" --request-timeout=30s || {
        log_error "Failed to uncordon node $node"
        return 1
    }

    # Remove from queue
    local idx=0
    for item in "${_UNCORDON_QUEUE[@]}"; do
        if [[ "$item" != "$node" ]]; then
            _UNCORDON_QUEUE[idx]="$item"
            ((idx++)) || true
        fi
    done
    unset '_UNCORDON_QUEUE[$idx]' 2>/dev/null || true
}

wait_node_ready() {
    local node="$1"
    local max_wait="${2:-300}"

    log_info "Waiting for node $node to become Ready (max ${max_wait}s)..."
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] Would wait for node $node to become Ready"
        return 0
    fi

    kubectl --kubeconfig "$KUBECONFIG" wait --for=condition=Ready "node/$node" \
        --timeout="${max_wait}s" || {
        log_error "Node $node did not become Ready within ${max_wait}s"
        return 1
    }

    log_info "Node $node is Ready"
}

# ---- Verification ----
verify_policy_on_node() {
    local ip="$1"
    local expected_policy="$2"

    local config_path
    config_path="$(check_kubelet_config_path "$ip" 2>/dev/null || true)"

    if [[ -z "$config_path" ]]; then
        log_warn "Cannot verify policy on $ip (no config file found)"
        return 1
    fi

    local actual
    actual="$(ssh_node "$ip" "grep 'cpuManagerPolicy:' '$config_path' 2>/dev/null | head -1 | awk '{print \$2}'" || true)"

    if [[ "$actual" == "$expected_policy" ]]; then
        log_info "Verified: cpuManagerPolicy=$actual on $ip"
        return 0
    else
        local got_val="${actual:-<not set>}"
        log_error "Policy mismatch on $ip: expected '$expected_policy', got '$got_val'"
        return 1
    fi
}

# ---- Main Switch Logic ----
switch_policy() {
    local policy="$1"
    local policy_display
    local kubelet_policy_value

    if [[ "$policy" == "static" ]]; then
        policy_display="Static"
        kubelet_policy_value="Static"
    elif [[ "$policy" == "none" ]]; then
        policy_display="None"
        kubelet_policy_value="None"
    else
        log_error "Invalid policy: $policy (must be 'none' or 'static')"
        usage 1
    fi

    log_info "Switching CPU manager policy to: $policy_display"

    # --- Prerequisites ---
    resolve_project_root
    check_deps

    # Verify cluster reachable
    kubectl --kubeconfig "$KUBECONFIG" cluster-info --request-timeout=5s &>/dev/null || {
        log_error "Cannot reach Kubernetes cluster — is kubeconfig valid at '$KUBECONFIG'?"
        return 1
    }
    log_info "Cluster reachable"

    # Get worker IPs
    mapfile -t _WORKER_IPS < <(get_worker_ips) || return 1
    if [[ ${#_WORKER_IPS[@]} -eq 0 ]]; then
        log_error "No worker IPs found"
        return 1
    fi
    log_info "Found ${#_WORKER_IPS[@]} worker(s): ${_WORKER_IPS[*]}"

    # Resolve worker node names from kubectl
    local -a node_names=()
    local ip node_name

    # Get node list with internal IPs (-o wide shows INTERNAL-IP in column 6)
    local node_list
    node_list="$(kubectl --kubeconfig "$KUBECONFIG" get nodes -o wide --no-headers 2>/dev/null || true)"

    for ip in "${_WORKER_IPS[@]}"; do
        node_name="$(printf '%s' "$node_list" | awk -v ip="$ip" '{if($6 == ip) print $1}' 2>/dev/null || true)"
        if [[ -z "$node_name" ]]; then
            log_warn "Could not resolve node name for IP $ip, using IP as identifier"
            node_name="$ip"
        fi
        node_names+=("$node_name")
    done

    # --- Confirmation ---
    if [[ "$YES" != "true" ]]; then
        local worker_list=""
        for n in "${node_names[@]}"; do
            worker_list+="  - $n\n"
        done
        printf 'WARNING: This will modify CPU manager policy on the following nodes:\n%s' "$worker_list" >&2
        printf 'Policy: %s\n' "$policy_display" >&2
        printf 'Kubeconfig: %s\n' "$KUBECONFIG" >&2
        printf 'Dry-run: %s\n' "$DRY_RUN" >&2

        if [[ "$DRY_RUN" != "true" ]]; then
            printf 'Proceed? [y/N] ' >&2
            read -r confirm
            if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
                log_info "Operation cancelled by user"
                exit 0
            fi
        fi
    fi

    # --- For static policy: verify kubelet version support ---
    if [[ "$policy" == "static" ]]; then
        log_info "Verifying kubelet version supports static CPU manager..."
        for ip in "${_WORKER_IPS[@]}"; do
            local version_str
            version_str="$(get_kubelet_version "$ip")"
            if [[ "$version_str" == "unknown" ]]; then
                log_warn "Could not determine kubelet version on $ip — proceeding but may fail"
                continue
            fi

            # Extract major.minor from "Kubernetes v1.28.3" or similar
            local ver_num
            ver_num="$(echo "$version_str" | grep -oP 'v\K[0-9]+\.[0-9]+' || true)"
            if [[ -z "$ver_num" ]]; then
                log_warn "Could not parse kubelet version from '$version_str' on $ip"
                continue
            fi

            local major="${ver_num%.*}"
            local minor="${ver_num#*.}"

            # Static CPU manager is GA since 1.18, beta since 1.12
            if (( major < 1 )) || (( major == 1 && minor < 12 )); then
                log_error "Kubelet on $ip is v${ver_num} — static CPU manager requires >= 1.12"
                return 1
            fi
            log_info "Kubelet on $ip: v${ver_num} (supports static CPU manager)"
        done
    fi

    # --- Process each worker ---
    local idx=0
    for ip in "${_WORKER_IPS[@]}"; do
        local node="${node_names[$idx]}"
        log_info "=== Processing worker $node ($ip) ==="

        # 1. Drain
        drain_node "$node" 300 || {
            log_error "Failed to drain $node, skipping"
            ((idx++)) || true
            continue
        }

        # 2. Modify kubelet config
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "[DRY-RUN] Would modify kubelet config on $ip"
        else
            modify_kubelet_config "$ip" "$kubelet_policy_value" || {
                log_error "Failed to modify kubelet config on $ip"
                # Uncordon this node and continue
                uncordon_node "$node" || true
                ((idx++)) || true
                continue
            }
        fi

        # 3. Restart kubelet
        restart_kubelet "$ip" 60 || {
            log_error "Failed to restart kubelet on $ip"
            uncordon_node "$node" || true
            ((idx++)) || true
            continue
        }

        # 4. Uncordon
        uncordon_node "$node" || {
            log_error "Failed to uncordon $node"
            ((idx++)) || true
            continue
        }

        # 5. Wait for node Ready
        wait_node_ready "$node" 300 || {
            log_error "Node $node did not become Ready"
            ((idx++)) || true
            continue
        }

        # 6. Verify policy on this worker
        if [[ "$DRY_RUN" != "true" ]]; then
            verify_policy_on_node "$ip" "$kubelet_policy_value" || {
                log_warn "Policy verification failed on $ip"
            }
        fi

        log_info "=== Worker $node ($ip) completed ==="
        ((idx++)) || true
    done

    # --- All workers processed ---
    log_info "All workers processed. Verifying cluster health..."
    if [[ "$DRY_RUN" != "true" ]]; then
        kubectl --kubeconfig "$KUBECONFIG" wait --for=condition=Ready node --all --timeout=60s || {
            log_warn "Some nodes are not Ready after policy switch"
        }

        # Run verification
        local verify_script
        verify_script="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/verify-cpu-manager.sh"
        if [[ -f "$verify_script" ]]; then
            log_info "Running verification..."
            bash "$verify_script" --policy "$policy" || {
                log_warn "Verification reported issues (exit code $?)"
            }
        else
            log_warn "Verification script not found at $verify_script"
        fi
    fi

    log_info "CPU manager policy switch to '$policy_display' complete"
}

# ---- Argument Parsing ----
main() {
    local positional_count=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage 0
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --yes)
                YES=true
                shift
                ;;
            -*)
                log_error "Unknown option: $1"
                usage 1
                ;;
            *)
                if [[ $positional_count -eq 0 ]]; then
                    TARGET_POLICY="$1"
                    positional_count=1
                else
                    log_error "Unexpected argument: $1"
                    usage 1
                fi
                shift
                ;;
        esac
    done

    if [[ -z "$TARGET_POLICY" ]]; then
        log_error "Missing policy argument (none|static)"
        usage 1
    fi

    if [[ "$TARGET_POLICY" != "none" && "$TARGET_POLICY" != "static" ]]; then
        log_error "Invalid policy: '$TARGET_POLICY' (must be 'none' or 'static')"
        usage 1
    fi

    switch_policy "$TARGET_POLICY"
}

main "$@"
