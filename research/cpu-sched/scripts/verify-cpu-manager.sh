#!/usr/bin/env bash
#
# verify-cpu-manager.sh — Verify kubelet CPU manager policy and CPU pinning
#
# Usage:
#   verify-cpu-manager.sh [--policy {none|static}] [--cleanup-only]
#
# Checks the current CPU manager policy on all worker nodes and performs
# pinning verification using test pods:
#   - For static policy: deploys Guaranteed QoS pod, verifies cpuset.cpus
#     is a subset of CPUs; deploys Burstable pod, verifies cpuset.cpus
#     includes all CPUs.
#   - For none policy: deploys Guaranteed QoS pod, verifies cpuset.cpus
#     is empty or all CPUs (no pinning).
#
# Test pods are automatically cleaned up on completion.

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

TEST_POD_GUARANTEED="cpu-pin-test"
TEST_POD_BURSTABLE="cpu-burstable-test"
TEST_IMAGE="registry.access.redhat.com/ubi9/ubi-minimal:latest"

# Source the shared DHCP lease helper for worker IP discovery
# shellcheck source=../bin/lease-common.sh
source "${SCRIPT_DIR}/../bin/lease-common.sh"

# ---- Global state for cleanup ----
declare -a _CLEANUP_PODS=()

# ---- Logging ----
log_info()  { printf '[%s] INFO:  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
log_warn()  { printf '[%s] WARN:  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
log_error() { printf '[%s] ERROR: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }

# ---- Cleanup ----
_cleanup() {
    local exit_code=$?

    if [[ ${#_CLEANUP_PODS[@]} -gt 0 ]]; then
        log_info "Cleaning up test pods..."
        local pod
        for pod in "${_CLEANUP_PODS[@]}"; do
            kubectl --kubeconfig "$KUBECONFIG" delete pod "$pod" --now --ignore-not-found \
                --request-timeout=30s 2>/dev/null || true
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
Usage: $SCRIPT_NAME [OPTIONS]

Verify kubelet CPU manager policy and test CPU pinning.

Options:
  --policy {none|static}   Expected policy (default: auto-detect)
  --cleanup-only           Only clean up any leftover test pods, skip verification
  -h, --help               Show this help and exit

Verification:
  1. Checks cpuManagerPolicy on each worker node
  2. For static policy:
     - Deploys Guaranteed QoS pod, verifies cpuset.cpus is a subset of node CPUs
     - Deploys Burstable QoS pod, verifies cpuset.cpus includes all node CPUs
  3. For none policy:
     - Deploys Guaranteed QoS pod, verifies no CPU pinning (all or no cpuset)

Requires KUBECONFIG env var or kubeconfig in project root.
EOF
    exit "${1:-0}"
}

# ---- Project Root and KUBECONFIG ----
find_project_root() {
    local dir
    dir="$(git rev-parse --show-toplevel 2>/dev/null)" && { echo "$dir"; return 0; }
    dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/Makefile" && -d "$dir/terraform" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    log_error "Cannot find k8labs project root"
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
        log_error "Kubeconfig not found at '$KUBECONFIG'"
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

# ---- Policy Checking ----
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

get_cpu_manager_policy() {
    local ip="$1"
    local config_path
    config_path="$(check_kubelet_config_path "$ip" 2>/dev/null || true)"
    if [[ -z "$config_path" ]]; then
        # Fallback: try kubelet --show-config
        local policy
        policy="$(ssh_node "$ip" "kubelet --show-config 2>/dev/null | grep cpuManagerPolicy | awk '{print \$2}'" 2>/dev/null || true)"
        if [[ -z "$policy" ]]; then
            echo "unknown"
            return 1
        fi
        echo "$policy"
        return 0
    fi
    local policy
    policy="$(ssh_node "$ip" "grep 'cpuManagerPolicy:' '$config_path' 2>/dev/null | head -1 | awk '{print \$2}'" || true)"
    echo "${policy:-unknown}"
}

get_node_cpu_list() {
    local ip="$1"
    # Return the list of online CPUs (e.g., "0-1" or "0,1,2,3")
    ssh_node "$ip" "cat /sys/devices/system/cpu/online 2>/dev/null" || echo "0"
}

# ---- Pod Operations ----
deploy_test_pod() {
    local pod_name="$1"
    local cpu_request="$2"
    local cpu_limit="$3"
    local mem="${4:-64Mi}"

    # Delete if exists
    kubectl --kubeconfig "$KUBECONFIG" delete pod "$pod_name" --now --ignore-not-found \
        --request-timeout=30s 2>/dev/null || true

    log_info "Deploying test pod: $pod_name (cpu: request=$cpu_request, limit=$cpu_limit)"
    kubectl --kubeconfig "$KUBECONFIG" run "$pod_name" \
        --image="$TEST_IMAGE" \
        --restart=Never \
        --requests="cpu=${cpu_request},memory=${mem}" \
        --limits="cpu=${cpu_limit},memory=${mem}" \
        -- sleep 30 2>/dev/null || {
        log_error "Failed to create pod $pod_name"
        return 1
    }

    _CLEANUP_PODS+=("$pod_name")

    # Wait for pod to reach Running
    local max_attempts=60
    local attempt=0
    while [[ $attempt -lt $max_attempts ]]; do
        local phase
        phase="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
            -o jsonpath='{.status.phase}' 2>/dev/null || true)"
        if [[ "$phase" == "Running" ]]; then
            log_info "Pod $pod_name is Running"
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done

    log_error "Pod $pod_name did not reach Running state within $((max_attempts * 2))s"
    kubectl --kubeconfig "$KUBECONFIG" describe pod "$pod_name" >&2 || true
    return 1
}

delete_pod() {
    local pod_name="$1"
    log_info "Deleting pod: $pod_name"
    kubectl --kubeconfig "$KUBECONFIG" delete pod "$pod_name" --now --ignore-not-found \
        --request-timeout=30s 2>/dev/null || true

    # Remove from cleanup list
    local -a remaining=()
    local p
    for p in "${_CLEANUP_PODS[@]}"; do
        [[ "$p" != "$pod_name" ]] && remaining+=("$p")
    done
    _CLEANUP_PODS=("${remaining[@]}")
}

get_pod_node() {
    local pod_name="$1"
    kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" -o wide --no-headers 2>/dev/null \
        | awk '{print $7}'
}

# Verify CPU pinning for a pod on a given node
verify_pod_pinning() {
    local pod_name="$1"
    local expected_pinned="$2"  # "true" for guaranteed (should be pinned), "false" for no pinning

    log_info "Verifying CPU pinning for pod '$pod_name' (expected pinned: $expected_pinned)..."

    # Find which node the pod runs on
    local node_name
    node_name="$(get_pod_node "$pod_name")"
    if [[ -z "$node_name" ]]; then
        log_error "Could not find node for pod $pod_name"
        return 1
    fi
    log_info "Pod $pod_name is on node $node_name"

    # Resolve node IP
    local node_ip
    node_ip="$(kubectl --kubeconfig "$KUBECONFIG" get node "$node_name" \
        -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"

    if [[ -z "$node_ip" ]]; then
        # Fallback: try to find by iterating workers
        local workers
        workers="$(get_worker_ips 2>/dev/null || true)"
        local w
        for w in $workers; do
            local hn
            hn="$(ssh_node "$w" "hostname" 2>/dev/null || true)"
            if [[ "$hn" == "$node_name" ]]; then
                node_ip="$w"
                break
            fi
        done
    fi

    if [[ -z "$node_ip" ]]; then
        log_error "Could not resolve IP for node $node_name"
        return 1
    fi
    log_info "Node IP: $node_ip"

    # Find container PID via crictl
    local container_id
    container_id="$(ssh_node "$node_ip" "/usr/bin/crictl ps --name '.*' --quiet 2>/dev/null | head -1" || true)"
    if [[ -z "$container_id" ]]; then
        log_error "No containers found via crictl on $node_ip"
        return 1
    fi

    local pid
    pid="$(ssh_node "$node_ip" "/usr/bin/crictl inspect '$container_id' 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get(\"info\",{}).get(\"pid\",\"\"))' 2>/dev/null" || true)"
    if [[ -z "$pid" || "$pid" == "None" ]]; then
        log_error "Could not get PID for container on $node_ip"
        return 1
    fi
    log_info "Container PID: $pid"

    # Read Cpus_allowed_list from /proc/<PID>/status
    local cpus_allowed
    cpus_allowed="$(ssh_node "$node_ip" "grep 'Cpus_allowed_list:' /proc/$pid/status 2>/dev/null | awk '{print \$2}'" || true)"
    log_info "Cpus_allowed_list for PID $pid: ${cpus_allowed:-unknown}"

    # Read cpuset.cpus from cgroupfs
    local cgroup_rel
    cgroup_rel="$(ssh_node "$node_ip" "grep '0::' /proc/$pid/cgroup 2>/dev/null | cut -d: -f3" || true)"
    local cpuset_cpus=""
    if [[ -n "$cgroup_rel" ]]; then
        cpuset_cpus="$(ssh_node "$node_ip" "cat /sys/fs/cgroup${cgroup_rel}/cpuset.cpus 2>/dev/null" || true)"
    fi
    log_info "cpuset.cpus: ${cpuset_cpus:-unknown}"

    # Get node CPU list
    local node_cpus
    node_cpus="$(get_node_cpu_list "$node_ip")"
    log_info "Node online CPUs: $node_cpus"

    # Determine if pinned
    if [[ "$expected_pinned" == "true" ]]; then
        # For static policy with Guaranteed QoS, cpuset.cpus should be a subset
        if [[ -n "$cpuset_cpus" && "$cpuset_cpus" != "$node_cpus" ]]; then
            log_info "PASS: cpuset.cpus ($cpuset_cpus) is a subset of node CPUs ($node_cpus)"
            echo "{\"node\":\"$node_name\",\"pid\":$pid,\"cpus_allowed_list\":\"${cpus_allowed:-}\",\"cpuset_cpus\":\"${cpuset_cpus:-}\",\"node_cpus\":\"$node_cpus\",\"pinned\":true}"
            return 0
        else
            log_warn "FAIL: Expected CPU pinning but cpuset.cpus ($cpuset_cpus) equals all node CPUs ($node_cpus)"
            echo "{\"node\":\"$node_name\",\"pid\":$pid,\"cpus_allowed_list\":\"${cpus_allowed:-}\",\"cpuset_cpus\":\"${cpuset_cpus:-}\",\"node_cpus\":\"$node_cpus\",\"pinned\":false}"
            return 1
        fi
    else
        # For none/burstable, cpuset.cpus should be all CPUs or empty
        if [[ -z "$cpuset_cpus" || "$cpuset_cpus" == "$node_cpus" ]]; then
            log_info "PASS: No pinning (cpuset.cpus=$cpuset_cpus matches node CPUs=$node_cpus)"
            echo "{\"node\":\"$node_name\",\"pid\":$pid,\"cpus_allowed_list\":\"${cpus_allowed:-}\",\"cpuset_cpus\":\"${cpuset_cpus:-}\",\"node_cpus\":\"$node_cpus\",\"pinned\":false}"
            return 0
        else
            log_warn "FAIL: Expected no pinning but cpuset.cpus ($cpuset_cpus) differs from node CPUs ($node_cpus)"
            echo "{\"node\":\"$node_name\",\"pid\":$pid,\"cpuset_cpus\":\"${cpuset_cpus:-}\",\"node_cpus\":\"$node_cpus\",\"pinned\":true}"
            return 1
        fi
    fi
}

# ---- Policy Detection ----
detect_policy() {
    local workers
    workers="$(get_worker_ips 2>/dev/null || true)"
    if [[ -z "$workers" ]]; then
        echo "unknown"
        return
    fi

    local policy=""
    local w
    for w in $workers; do
        local p
        p="$(get_cpu_manager_policy "$w")"
        if [[ -z "$policy" ]]; then
            policy="$p"
        elif [[ "$p" != "$policy" ]]; then
            log_warn "Inconsistent policies across workers: '$policy' and '$p'"
        fi
    done
    echo "$policy"
}

# ---- Main ----
main() {
    local expected_policy=""
    local cleanup_only=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage 0
                ;;
            --policy)
                expected_policy="${2:?--policy requires argument}"
                if [[ "$expected_policy" != "none" && "$expected_policy" != "static" ]]; then
                    log_error "Invalid policy: '$expected_policy' (must be 'none' or 'static')"
                    usage 1
                fi
                shift 2
                ;;
            --cleanup-only)
                cleanup_only=true
                shift
                ;;
            -*)
                log_error "Unknown option: $1"
                usage 1
                ;;
            *)
                log_error "Unexpected argument: $1"
                usage 1
                ;;
        esac
    done

    resolve_project_root

    # Verify cluster
    kubectl --kubeconfig "$KUBECONFIG" cluster-info --request-timeout=5s &>/dev/null || {
        log_error "Cannot reach Kubernetes cluster"
        return 1
    }

    if [[ "$cleanup_only" == "true" ]]; then
        log_info "Cleanup only mode — removing any leftover test pods"
        for pod in "$TEST_POD_GUARANTEED" "$TEST_POD_BURSTABLE"; do
            delete_pod "$pod"
        done
        return 0
    fi

    # Detect policy if not specified
    if [[ -z "$expected_policy" ]]; then
        expected_policy="$(detect_policy)"
        log_info "Detected CPU manager policy: $expected_policy"
    fi

    # Check policy on each worker
    local workers
    workers="$(get_worker_ips 2>/dev/null || true)"
    if [[ -z "$workers" ]]; then
        log_error "No worker IPs found"
        return 1
    fi

    log_info "Checking CPU manager policy on workers..."
    local all_match=true
    local w
    for w in $workers; do
        local actual
        actual="$(get_cpu_manager_policy "$w")"
        log_info "  Worker $w: cpuManagerPolicy=$actual"
        if [[ "$actual" != "$expected_policy" && "$expected_policy" != "unknown" ]]; then
            log_warn "  Worker $w policy '$actual' does not match expected '$expected_policy'"
            all_match=false
        fi
    done

    if [[ "$all_match" == "false" ]]; then
        log_warn "Policy mismatch detected"
    fi

    # For static policy: verify Guaranteed pod pinning
    if [[ "$expected_policy" == "static" ]]; then
        log_info "--- Verifying Guaranteed QoS pinning (static policy) ---"
        deploy_test_pod "$TEST_POD_GUARANTEED" "200m" "200m" "64Mi" || {
            log_error "Failed to deploy guaranteed test pod"
            return 1
        }
        verify_pod_pinning "$TEST_POD_GUARANTEED" "true" || {
            log_warn "Guaranteed pod pinning verification failed"
        }
        delete_pod "$TEST_POD_GUARANTEED"

        log_info "--- Verifying Burstable QoS (no pinning expected) ---"
        deploy_test_pod "$TEST_POD_BURSTABLE" "100m" "400m" "64Mi" || {
            log_error "Failed to deploy burstable test pod"
            return 1
        }
        verify_pod_pinning "$TEST_POD_BURSTABLE" "false" || {
            log_warn "Burstable pod no-pinning verification failed"
        }
        delete_pod "$TEST_POD_BURSTABLE"

    elif [[ "$expected_policy" == "none" ]]; then
        log_info "--- Verifying no CPU pinning (none policy) ---"
        deploy_test_pod "$TEST_POD_GUARANTEED" "200m" "200m" "64Mi" || {
            log_error "Failed to deploy guaranteed test pod"
            return 1
        }
        verify_pod_pinning "$TEST_POD_GUARANTEED" "false" || {
            log_warn "Guaranteed pod no-pinning verification failed"
        }
        delete_pod "$TEST_POD_GUARANTEED"

    else
        log_warn "Policy is unknown — skipping pinning verification"
    fi

    log_info "Verification complete"
}

main "$@"
