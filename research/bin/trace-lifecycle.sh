#!/usr/bin/env bash
#
# trace-lifecycle.sh — Trace a pod from creation to cgroup writes
#
# Usage:
#   trace-lifecycle.sh <deployment-yaml> [--no-cleanup]
#   trace-lifecycle.sh --help
#
# Creates a pod from the given YAML, traces CPU parameters through the
# entire chain (pod spec -> CRI-O -> OCI spec -> cgroup v2 files),
# then cleans up the pod unless --no-cleanup is specified.

set -Eeuo pipefail

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# ---- Source common library ----
# shellcheck source=./cgroup-common.sh
source "$SCRIPT_DIR/cgroup-common.sh"

# ---- Constants ----
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

# ---- State ----
DEPLOYMENT_YAML=""
NO_CLEANUP=false
CREATED_POD_NAME=""
TIMESTAMP=""

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <deployment-yaml> [--no-cleanup]

Trace a pod's CPU parameters through the full lifecycle chain:
  Pod spec -> kubelet -> CRI-O -> OCI spec -> crun -> cgroup v2 files

Arguments:
  deployment-yaml   Path to a Kubernetes pod or deployment YAML file

Options:
  --no-cleanup      Leave the test pod running after tracing
  -h, --help        Show this help and exit

Output:
  Rich text report showing the mapping chain with actual values at
  each layer: pod spec CPU fields, OCI spec CPU fields (via crictl inspect),
  and cgroup v2 files (cpu.weight, cpu.max, cpu.stat).

Dependencies: kubectl, tofu/terraform, ssh, jq, python3
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# cleanup — Remove the test pod on exit (unless --no-cleanup)
# ---------------------------------------------------------------------------
_cleanup() {
    local exit_code=$?
    if [[ "$NO_CLEANUP" == false && -n "$CREATED_POD_NAME" ]]; then
        printf '\n--- Cleanup: removing pod "%s" ---\n' "$CREATED_POD_NAME"
        kubectl --kubeconfig "$KUBECONFIG" delete pod "$CREATED_POD_NAME" \
            --now --ignore-not-found --request-timeout=30s 2>/dev/null || true
        printf 'Pod "%s" removed.\n' "$CREATED_POD_NAME"
    fi
    trap - EXIT ERR
    exit "$exit_code"
}

_error_handler() {
    local line=$1
    local cmd=$2
    printf '[ERROR] Command failed at line %d: %s\n' "$line" "$cmd" >&2
}

# ---------------------------------------------------------------------------
# print_header — Print a section header
# ---------------------------------------------------------------------------
print_header() {
    local len=${#1}
    printf '\n%s\n' "$1"
    printf '%*s\n' "$len" '' | tr ' ' '='
}

# ---------------------------------------------------------------------------
# print_subheader — Print a sub-section header
# ---------------------------------------------------------------------------
print_subheader() {
    printf '\n--- %s ---\n' "$1"
}

# ---------------------------------------------------------------------------
# wait_for_pod_running — Wait up to 60s for pod to reach Running phase
# ---------------------------------------------------------------------------
wait_for_pod_running() {
    local pod_name="$1"
    local max_attempts=30
    local attempt=0

    printf '  Waiting for pod to reach Running phase...'
    while [[ $attempt -lt $max_attempts ]]; do
        local phase
        phase="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
            -o jsonpath='{.status.phase}' 2>/dev/null || true)"
        if [[ "$phase" == "Running" ]]; then
            printf ' Running!\n'
            return 0
        elif [[ "$phase" == "Succeeded" ]]; then
            printf ' Succeeded (not Running — workload may have completed)\n'
            return 0
        elif [[ "$phase" == "Failed" || "$phase" == "Error" ]]; then
            printf '\n  ERROR: Pod entered phase "%s"\n' "$phase" >&2
            kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" >&2
            return 1
        fi
        printf '.'
        sleep 2
        ((attempt++))
    done
    printf '\n'
    log_error "Pod '$pod_name' did not reach Running within $((max_attempts * 2))s"
    kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" >&2
    return 1
}

# ---------------------------------------------------------------------------
# format_oci_spec — Pretty-print OCI spec CPU section
# ---------------------------------------------------------------------------
format_oci_spec() {
    local spec="$1"
    printf '  Linux.Resources.CPU:\n'
    jq -r '
        .info.runtimeSpec.linux.resources.cpu // {} |
        to_entries[] |
        "    \(.key): \(.value | tostring)"
    ' <<< "$spec" 2>/dev/null || printf '    (not found or empty)\n'
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

    DEPLOYMENT_YAML="$1"
    shift

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-cleanup)
                NO_CLEANUP=true
                shift
                ;;
            -h|--help)
                usage 0
                ;;
            *)
                printf 'ERROR: Unknown option: %s\n' "$1" >&2
                usage 1
                ;;
        esac
    done

    # Validate YAML file exists
    if [[ ! -f "$DEPLOYMENT_YAML" ]]; then
        log_error "YAML file not found: $DEPLOYMENT_YAML"
        exit 1
    fi

    # Validate dependencies
    require_tools kubectl tofu ssh jq python3 || exit 1

    # Resolve project root and KUBECONFIG
    resolve_project_root

    # Verify cluster reachable
    kubectl --kubeconfig "$KUBECONFIG" cluster-info --request-timeout=5s &>/dev/null || {
        log_error "Cannot reach Kubernetes cluster — is kubeconfig valid?"
        exit 1
    }

    # Trap for cleanup
    trap _cleanup EXIT
    trap '_error_handler $LINENO "$BASH_COMMAND"' ERR

    TIMESTAMP="$(date -u +'%Y%m%dT%H%M%SZ')"

    print_header "Container CPU Lifecycle Trace"
    printf 'Started at: %s\n' "$TIMESTAMP"
    printf 'YAML file:  %s\n' "$DEPLOYMENT_YAML"
    printf 'Project:    %s\n' "$PROJECT_ROOT"
    printf 'Kubeconfig: %s\n' "$KUBECONFIG"

    # ---- Step 1: Create the resource ----
    print_header "Step 1: Create Resource"

    printf '  Applying: %s\n' "$DEPLOYMENT_YAML"
    kubectl --kubeconfig "$KUBECONFIG" apply -f "$DEPLOYMENT_YAML" 2>&1 | sed 's/^/  /'

    # ---- Step 2: Discover pod name ----
    print_header "Step 2: Discover Pod Name"

    # Extract pod name from YAML for direct pod resources
    local yaml_pod_name
    yaml_pod_name="$(grep -E '^\s*name:' "$DEPLOYMENT_YAML" | head -1 | awk '{print $2}')"

    # Check if that pod already exists (direct pod creation)
    if [[ -n "$yaml_pod_name" && "$yaml_pod_name" != "{{"* ]]; then
        local check_pod
        check_pod="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$yaml_pod_name" \
            --no-headers -o custom-columns=':metadata.name' 2>/dev/null || true)"
        if [[ -n "$check_pod" ]]; then
            CREATED_POD_NAME="$yaml_pod_name"
            printf '  Pod name (from YAML): %s\n' "$CREATED_POD_NAME"
        fi
    fi

    # If direct name didn't work, use kubectl get pods -w to detect
    if [[ -z "$CREATED_POD_NAME" ]]; then
        printf '  Watching for pod creation (kubectl get pods -w)...\n'

        local watcher_pod_var
        watcher_pod_var="$(mktemp)" || exit 1

        # List pods before applying to distinguish new ones
        local before_pods
        before_pods="$(kubectl --kubeconfig "$KUBECONFIG" get pods \
            --no-headers -o custom-columns=':metadata.name' 2>/dev/null | sort || true)"

        # Background watcher: capture the first new pod that appears
        kubectl --kubeconfig "$KUBECONFIG" get pods -w --no-headers 2>/dev/null \
            | while IFS=' ' read -r new_pod pod_status rest; do
                if [[ -n "$new_pod" && -n "$pod_status" ]]; then
                    # Check if this is a new pod (not in before list)
                    if ! grep -qF "$new_pod" <<< "$before_pods"; then
                        printf '%s' "$new_pod" > "$watcher_pod_var"
                        break
                    fi
                fi
            done &
        local watcher_pid=$!

        # Wait for watcher to discover a pod (up to 60s)
        local discovered=false
        for ((i = 0; i < 30; i++)); do
            if [[ -s "$watcher_pod_var" ]]; then
                CREATED_POD_NAME="$(<"$watcher_pod_var")"
                discovered=true
                break
            fi
            sleep 2
        done

        # Clean up watcher
        kill "$watcher_pid" 2>/dev/null || true
        rm -f "$watcher_pod_var"

        if [[ "$discovered" == false ]]; then
            # Final fallback: just grab the latest pod
            local latest_pod
            latest_pod="$(kubectl --kubeconfig "$KUBECONFIG" get pods \
                --no-headers -o custom-columns=':metadata.name' --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -1 || true)"
            if [[ -n "$latest_pod" ]]; then
                CREATED_POD_NAME="$latest_pod"
                printf '  Fallback pod: %s\n' "$CREATED_POD_NAME"
            else
                log_error "Could not discover pod name after applying YAML"
                exit 1
            fi
        fi

        printf '  Pod name (from watcher): %s\n' "$CREATED_POD_NAME"
    fi

    # ---- Step 3: Wait for Running ----
    print_header "Step 3: Wait for Pod Running"
    wait_for_pod_running "$CREATED_POD_NAME" || exit 1

    # ---- Step 5: Get node assignment ----
    print_header "Step 5: Node Assignment"
    local node_name
    node_name="$(get_pod_node "$CREATED_POD_NAME")" || exit 1
    printf '  Node: %s\n' "$node_name"

    local node_ip
    node_ip="$(get_pod_node_ip "$CREATED_POD_NAME")" || {
        log_error "Could not resolve IP for node '$node_name'"
        exit 1
    }
    printf '  Node IP: %s\n' "$node_ip"

    # ---- Step 6: Get pod spec CPU fields ----
    print_header "Step 6: Pod Spec CPU Parameters"
    local pod_json
    pod_json="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$CREATED_POD_NAME" -o json)" || {
        log_error "Failed to get pod JSON"
        exit 1
    }

    printf '  Requests:\n'
    jq -r '
        .spec.containers[] |
        "    \(.name): cpu_request=\(.resources.requests.cpu // "not-set") memory_request=\(.resources.requests.memory // "not-set")"
    ' <<< "$pod_json" 2>/dev/null || printf '    (empty)\n'

    printf '  Limits:\n'
    jq -r '
        .spec.containers[] |
        "    \(.name): cpu_limit=\(.resources.limits.cpu // "not-set") memory_limit=\(.resources.limits.memory // "not-set")"
    ' <<< "$pod_json" 2>/dev/null || printf '    (empty)\n'

    # Get first container name from pod
    local container_name
    container_name="$(jq -r '.spec.containers[0].name // "container-0"' <<< "$pod_json")"
    printf '  Primary container: %s\n' "$container_name"

    # ---- Step 7: Get container ID via crictl ----
    print_header "Step 7: Container Discovery (CRI-O)"
    local container_id
    container_id="$(get_container_id "$node_ip" "$CREATED_POD_NAME" "$container_name")" || exit 1
    printf '  Container ID (CRI-O): %s\n' "$container_id"

    local pid
    pid="$(get_container_pid "$node_ip" "$container_id")" || exit 1
    printf '  Container PID: %s\n' "$pid"

    # ---- Step 8: Get OCI spec via crictl inspect ----
    print_header "Step 8: OCI Spec CPU Fields (crictl inspect)"
    local oci_spec
    oci_spec="$(ssh_node "$node_ip" "/usr/bin/crictl inspect '$container_id' 2>/dev/null")" || {
        log_error "crictl inspect failed on node $node_ip"
        exit 1
    }

    format_oci_spec "$oci_spec"

    # Extract OCI CPU values for the mapping table
    local oci_shares oci_quota oci_period oci_weight
    oci_shares="$(jq -r '.info.runtimeSpec.linux.resources.cpu.shares // "null"' <<< "$oci_spec")"
    oci_quota="$(jq -r '.info.runtimeSpec.linux.resources.cpu.quota // "null"' <<< "$oci_spec")"
    oci_period="$(jq -r '.info.runtimeSpec.linux.resources.cpu.period // "null"' <<< "$oci_spec")"
    oci_weight="$(jq -r '.info.runtimeSpec.linux.resources.cpu.weight // "null"' <<< "$oci_spec")"

    # ---- Step 9: Read cgroup files ----
    print_header "Step 9: Cgroup v2 Files"
    local cgroup_path
    cgroup_path="$(get_cgroup_path "$node_ip" "$pid")" || exit 1
    printf '  Cgroup path: %s\n' "$cgroup_path"

    local cpu_weight_val cpu_max_val cpu_stat_val
    cpu_weight_val="$(read_cgroup_file "$node_ip" "$cgroup_path" "cpu.weight" || printf 'UNAVAILABLE')"
    cpu_max_val="$(read_cgroup_file "$node_ip" "$cgroup_path" "cpu.max" || printf 'UNAVAILABLE')"
    cpu_stat_val="$(read_cgroup_file "$node_ip" "$cgroup_path" "cpu.stat" || printf 'UNAVAILABLE')"

    printf '\n  cpu.weight: %s\n' "$cpu_weight_val"

    local cpu_max_quota cpu_max_period
    if [[ "$cpu_max_val" != "UNAVAILABLE" ]]; then
        read -r cpu_max_quota cpu_max_period <<< "$cpu_max_val"
        printf '  cpu.max:     %s %s (quota=%s period=%s)\n' "$cpu_max_quota" "$cpu_max_period" "$cpu_max_quota" "$cpu_max_period"
    else
        printf '  cpu.max:     UNAVAILABLE\n'
    fi

    printf '\n  cpu.stat:\n'
    while IFS=' ' read -r key val; do
        [[ -z "$key" ]] && continue
        printf '    %s: %s\n' "$key" "$val"
    done <<< "$cpu_stat_val"

    # ---- Step 10: Mapping chain ----
    print_header "Step 10: End-to-End CPU Parameter Chain"

    printf '\n  %-25s %-15s %-15s %-15s\n' "Layer" "cpu.shares/weight" "cpu.quota/max" "cpu.period"
    printf '  %-25s %-15s %-15s %-15s\n' "=========================" "===============" "===============" "==============="

    # Pod spec
    local pod_request pod_limit
    pod_request="$(jq -r '.spec.containers[0].resources.requests.cpu // "not-set"' <<< "$pod_json")"
    pod_limit="$(jq -r '.spec.containers[0].resources.limits.cpu // "not-set"' <<< "$pod_json")"
    printf '  %-25s %-15s %-15s %-15s\n' "Pod Spec" "request=$pod_request" "limit=$pod_limit" "N/A"

    # OCI spec
    printf '  %-25s %-15s %-15s %-15s\n' "OCI Spec (crictl)" "shares=$oci_shares" "quota=$oci_quota" "period=$oci_period"
    if [[ "$oci_weight" != "null" ]]; then
        printf '  %-25s %-15s %-15s %-15s\n' "OCI Spec (cruntime)" "weight=$oci_weight" "" ""
    fi

    # Cgroup v2
    printf '  %-25s %-15s %-15s %-15s\n' "Cgroup v2" "weight=$cpu_weight_val" "max=${cpu_max_quota:-N/A}" "period=${cpu_max_period:-N/A}"

    # ---- Step 9: Annotations ----
    print_header "Step 9: Analysis"

    # CPU request → weight mapping (cgroups v2: weight = shares / 1024 * 100)
    if [[ "$pod_request" != "not-set" ]]; then
        local request_mcpu
        # Parse CPU request (e.g., "250m" -> 250, "1" -> 1000)
        if [[ "$pod_request" == *m ]]; then
            request_mcpu="${pod_request%m}"
        else
            request_mcpu=$(( pod_request * 1000 ))
        fi
        local expected_weight
        expected_weight=$(( request_mcpu * 100 / 1024 ))
        printf '  CPU request %s (%dm) -> expected cpu.weight ~ %d (shares=%d/1024*100)\n' \
            "$pod_request" "$request_mcpu" "$expected_weight" "$request_mcpu"

        if [[ "$cpu_weight_val" != "UNAVAILABLE" ]]; then
            local diff=$(( cpu_weight_val - expected_weight ))
            if [[ ${diff#-} -le $(( expected_weight / 10 + 1 )) ]]; then
                printf '  MATCH: cpu.weight=%d vs expected=%d (diff=%d, within 10%% tolerance)\n' \
                    "$cpu_weight_val" "$expected_weight" "$diff"
            else
                printf '  MISMATCH: cpu.weight=%d vs expected=%d (diff=%d, exceeds 10%% tolerance)\n' \
                    "$cpu_weight_val" "$expected_weight" "$diff"
            fi
        fi
    fi

    # CPU limit → cpu.max mapping
    if [[ "$pod_limit" != "not-set" ]]; then
        local limit_mcpu
        if [[ "$pod_limit" == *m ]]; then
            limit_mcpu="${pod_limit%m}"
        else
            limit_mcpu=$(( pod_limit * 1000 ))
        fi
        local expected_quota=$(( limit_mcpu * 100000 / 1000 ))  # quota_us = limit_mcpu * period_us / 1000
        printf '  CPU limit %s (%dm) -> expected cpu.max quota ~ %d us (period=100000 us)\n' \
            "$pod_limit" "$limit_mcpu" "$expected_quota"

        if [[ -n "${cpu_max_quota:-}" && "$cpu_max_quota" != "UNAVAILABLE" && "$cpu_max_quota" != "max" ]]; then
            local quota_diff=$(( cpu_max_quota - expected_quota ))
            if [[ ${quota_diff#-} -le $(( expected_quota / 100 + 1 )) ]]; then
                printf '  MATCH: cpu.max quota=%d vs expected=%d (diff=%d, within 1%% tolerance)\n' \
                    "$cpu_max_quota" "$expected_quota" "$quota_diff"
            else
                printf '  MISMATCH: cpu.max quota=%d vs expected=%d (diff=%d, exceeds 1%% tolerance)\n' \
                    "$cpu_max_quota" "$expected_quota" "$quota_diff"
            fi
        elif [[ "$cpu_max_quota" == "max" ]]; then
            printf '  NOTE: cpu.max quota is "max" (no limit). The pod had limit=%s.\n' "$pod_limit"
        fi
    fi

    # ---- Done ----
    printf '\n=== Trace complete ===\n'
    if [[ "$NO_CLEANUP" == false ]]; then
        printf 'Cleanup will remove pod "%s" on exit.\n' "$CREATED_POD_NAME"
    else
        printf 'Pod "%s" left running (--no-cleanup).\n' "$CREATED_POD_NAME"
    fi
}

main "$@"
