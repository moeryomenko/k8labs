#!/usr/bin/env bash
#
# common.sh — Shared functions for experiment scripts
#
# This library is sourced by all experiment runners. It provides logging,
# cluster validation, pod lifecycle management, and data collection.
#
# Usage in scripts:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
#   then call any of the exported functions.

# Guard against double-sourcing
[[ -z ${_EXPERIMENTS_COMMON_SH:-} ]] || return
_EXPERIMENTS_COMMON_SH=1
readonly _EXPERIMENTS_COMMON_SH

# ---- Strict Mode ----
set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

# ---- Paths ----
# SCRIPT_DIR is the directory containing this file
_EXPERIMENTS_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly _EXPERIMENTS_SCRIPT_DIR

# Source the research cgroup-common library
# shellcheck source=../bin/cgroup-common.sh
source "${_EXPERIMENTS_SCRIPT_DIR}/../bin/cgroup-common.sh"

# ---- Constants ----
: "${KUBECONFIG:=}"
EXPERIMENTS_DIR="${_EXPERIMENTS_SCRIPT_DIR}"
RESEARCH_DIR="$(cd -- "${_EXPERIMENTS_SCRIPT_DIR}/.." && pwd -P)"
readonly EXPERIMENTS_DIR RESEARCH_DIR

# Perfetto tools directory
PERFETTO_DIR="$(cd -- "${_EXPERIMENTS_SCRIPT_DIR}/../perfetto" && pwd -P)"
readonly PERFETTO_DIR

# ---- Logging ----

# ---------------------------------------------------------------------------
# log — Timestamped logging to stderr
# Usage: log "message"
# ---------------------------------------------------------------------------
log() {
    printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

# ---------------------------------------------------------------------------
# die — Error exit with message
# Usage: die "fatal error" [exit_code]
# ---------------------------------------------------------------------------
die() {
    local msg="${1:-unknown error}"
    local code="${2:-1}"
    printf '[%s] FATAL: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$msg" >&2
    exit "$code"
}

# ---- Cluster Validation ----

# ---------------------------------------------------------------------------
# require_cluster — Check kubectl works and all nodes are Ready
# ---------------------------------------------------------------------------
require_cluster() {
    resolve_project_root

    log "Verifying cluster connectivity..."
    kubectl --kubeconfig "$KUBECONFIG" cluster-info --request-timeout=5s &>/dev/null \
        || die "Cannot reach Kubernetes cluster — is kubeconfig valid at '$KUBECONFIG'?"

    log "Checking node status..."
    local not_ready
    not_ready="$(kubectl --kubeconfig "$KUBECONFIG" get nodes --no-headers 2>/dev/null \
        | awk '{if($2!="Ready"){print $1}}' || true)"

    if [[ -n "$not_ready" ]]; then
        die "Some nodes are not Ready: ${not_ready}"
    fi

    log "Cluster is healthy — all nodes Ready"
}

# ---- Worker IPs ----

# ---------------------------------------------------------------------------
# get_worker_ips — Return worker IPs from DHCP leases (by MAC address)
# Returns space-separated IP addresses to stdout
# ---------------------------------------------------------------------------
get_worker_ips() {
    local dnsmasq_leases="${DNSMASQ_LEASES:-/var/lib/misc/dnsmasq/k8sbr0.leases}"
    local macs="${WORKER_MACS:-c6:e5:50:1c:ec:02}"
    local ips=()
    local mac ip

    if [[ ! -f "$dnsmasq_leases" ]]; then
        die "DHCP lease file not found: $dnsmasq_leases"
    fi

    for mac in $macs; do
        ip=$(awk -v m="$mac" 'BEGIN{IGNORECASE=1} $2 == m {print $3; exit}' "$dnsmasq_leases" 2>/dev/null || true)
        [[ -n "$ip" ]] && ips+=("$ip")
    done

    if [[ ${#ips[@]} -eq 0 ]]; then
        die "No worker IPs found in DHCP leases"
    fi

    echo "${ips[*]}"
}

# ---- Template Substitution ----

# ---------------------------------------------------------------------------
# substitute_cpu_params — Replace {{CPU_REQUEST}} and {{CPU_LIMIT}} in template
#
# Arguments:
#   $1 — template file path
#   $2 — CPU request value (e.g., "100m" or "" for none)
#   $3 — CPU limit value (e.g., "200m" or "" for none)
#   $4 — optional output path (default: stdout)
#
# For co-located workloads, also handles {{LS_CPU_REQUEST}}, {{LS_CPU_LIMIT}},
# {{BATCH_CPU_REQUEST}}, {{BATCH_CPU_LIMIT}} when extra variables are provided.
# ---------------------------------------------------------------------------
substitute_cpu_params() {
    local template="$1"
    local cpu_request="$2"
    local cpu_limit="$3"
    local output_path="${4:-}"

    [[ -f "$template" ]] || die "Template file not found: $template"

    local sed_expr=()
    if [[ -n "$cpu_request" ]]; then
        sed_expr+=(-e "s/{{CPU_REQUEST}}/${cpu_request}/g")
    else
        sed_expr+=(-e "/{{CPU_REQUEST}}/d")
    fi

    if [[ -n "$cpu_limit" ]]; then
        sed_expr+=(-e "s/{{CPU_LIMIT}}/${cpu_limit}/g")
    else
        sed_expr+=(-e "/{{CPU_LIMIT}}/d")
    fi

    # Also substitute co-located template variables if present
    sed_expr+=(-e "s/{{LS_CPU_REQUEST}}/${LS_CPU_REQUEST:-${cpu_request}}/g")
    sed_expr+=(-e "s/{{LS_CPU_LIMIT}}/${LS_CPU_LIMIT:-${cpu_limit}}/g")
    sed_expr+=(-e "s/{{BATCH_CPU_REQUEST}}/${BATCH_CPU_REQUEST:-${cpu_request}}/g")
    sed_expr+=(-e "s/{{BATCH_CPU_LIMIT}}/${BATCH_CPU_LIMIT:-${cpu_limit}}/g")

    if [[ -n "$output_path" ]]; then
        sed "${sed_expr[@]}" "$template" > "$output_path"
    else
        sed "${sed_expr[@]}" "$template"
    fi
}

# ---------------------------------------------------------------------------
# substitute_pod_manifest — Render a workload template into a pod manifest
# with a unique pod name and per-pod CPU request/limit values.
#
# Wraps substitute_cpu_params ({{CPU_REQUEST}}/{{CPU_LIMIT}} and the LS_/
# BATCH_ markers) and additionally rewrites metadata.name to the requested
# pod name so N co-located pods rendered from the same template get distinct
# names. Fails if any {{...CPU_...}} marker survives substitution.
#
# Arguments:
#   $1 — template file path
#   $2 — pod name (rewrites metadata.name; must be DNS-1123 safe)
#   $3 — CPU request value (e.g., "100m" or "" for none)
#   $4 — CPU limit value (e.g., "200m" or "" for none)
#   $5 — output path
# ---------------------------------------------------------------------------
substitute_pod_manifest() {
    local template="$1"
    local pod_name="$2"
    local cpu_request="$3"
    local cpu_limit="$4"
    local output_path="$5"

    substitute_cpu_params "$template" "$cpu_request" "$cpu_limit" "$output_path"

    if grep -qE '\{\{[A-Za-z_]*CPU_[A-Za-z_]*\}\}' "$output_path"; then
        die "Unresolved CPU template marker in rendered manifest '${output_path}' (template: ${template})"
    fi

    # Rewrite only the first "name:" line (metadata.name). GNU sed "0,/re/"
    # addresses the first occurrence; container names appear later in the file.
    sed -i "0,/^\([[:space:]]*\)name:[[:space:]]*.*/s//\1name: ${pod_name}/" "$output_path"
}

# ---- Pod Lifecycle ----

# ---------------------------------------------------------------------------
# deploy_pod — Deploy a pod manifest, wait for Running
#
# Arguments:
#   $1 — manifest file path
# Returns: pod name on stdout
# ---------------------------------------------------------------------------
deploy_pod() {
    local manifest="$1"
    [[ -f "$manifest" ]] || die "Pod manifest not found: $manifest"

    log "Deploying pod from: $manifest"
    # Delete first to avoid conflicts with changed fields (e.g. env vars).
    # Suppress stdout too — kubectl prints 'pod "name" deleted' to stdout,
    # which would pollute the returned pod name below.
    kubectl --kubeconfig "$KUBECONFIG" delete -f "$manifest" --ignore-not-found --now >/dev/null 2>&1 || true
    kubectl --kubeconfig "$KUBECONFIG" apply -f "$manifest" >/dev/null

    # Extract pod name from manifest
    local pod_name
    pod_name="$(grep -E '^\s*name:' "$manifest" | head -1 | awk '{print $2}')"
    [[ -n "$pod_name" ]] || die "Could not extract pod name from manifest: $manifest"

    wait_for_pod_running "$pod_name" || die "Pod '$pod_name' did not reach Running state"

    echo "$pod_name"
}

# ---------------------------------------------------------------------------
# delete_pod — Delete a pod by name, wait for Gone
#
# Arguments:
#   $1 — pod name
# ---------------------------------------------------------------------------
delete_pod() {
    local pod_name="$1"

    log "Deleting pod: $pod_name"
    kubectl --kubeconfig "$KUBECONFIG" delete pod "$pod_name" --now --ignore-not-found \
        --request-timeout=30s 2>/dev/null || true

    # Wait for pod to be gone
    local max_attempts=30
    local attempt=0
    while [[ $attempt -lt $max_attempts ]]; do
        local exists
        exists="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
            --no-headers -o custom-columns=':metadata.name' 2>/dev/null || true)"
        if [[ -z "$exists" ]]; then
            log "Pod '$pod_name' deleted"
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done

    log "WARNING: Pod '$pod_name' did not disappear within $((max_attempts * 2))s"
}

# ---------------------------------------------------------------------------
# wait_for_pod_running — Poll kubectl until pod is Running
#
# Arguments:
#   $1 — pod name
#   $2 — timeout in seconds (default: 120)
# ---------------------------------------------------------------------------
wait_for_pod_running() {
    local pod_name="$1"
    local timeout="${2:-120}"
    local max_attempts=$(( timeout / 2 ))
    local attempt=0

    log "Waiting for pod '$pod_name' to reach Running (timeout: ${timeout}s)..."
    while [[ $attempt -lt $max_attempts ]]; do
        local phase
        phase="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
            -o jsonpath='{.status.phase}' 2>/dev/null || true)"

        case "$phase" in
            Running)
                log "Pod '$pod_name' is Running"
                return 0
                ;;
            Succeeded)
                log "Pod '$pod_name' has Succeeded (workload completed)"
                return 0
                ;;
            Failed|Error)
                log "ERROR: Pod '$pod_name' entered phase '$phase'"
                kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" >&2
                return 1
                ;;
            Pending|ContainerCreating|PodInitializing)
                # Normal startup phases — keep waiting
                ;;
            "")
                # Pod may not exist yet
                ;;
        esac

        sleep 2
        attempt=$((attempt + 1))
    done

    log "ERROR: Pod '$pod_name' did not reach Running within ${timeout}s (last phase: ${phase:-unknown})"
    kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" >&2
    return 1
}

# ---- Data Collection ----

# ---------------------------------------------------------------------------
# start_load_generation — Generate HTTP load against a workload pod
#
# Used by cpu-burner and other HTTP-based workloads: the runner deploys
# the pod but the pod stays idle unless something hits its endpoint.
# The host has no route to the pod CIDR, so the load loop runs on the
# node that hosts the pod (SSH) where the pod IP is directly reachable.
#
# Arguments:
#   $1 — pod name
#   $2 — endpoint path (e.g. "/fibonacci?n=38")
#   $3 — duration in seconds
# Returns: background PID on stdout
# ---------------------------------------------------------------------------
start_load_generation() {
    local pod_name="$1"
    local endpoint="$2"
    local duration="$3"

    local pod_ip
    pod_ip="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
        -o jsonpath='{.status.podIP}' 2>/dev/null || true)"
    if [[ -z "$pod_ip" ]]; then
        log_error "Cannot resolve pod IP for '$pod_name' — skipping load generation"
        return 1
    fi

    local node_ip
    node_ip="$(get_pod_node_ip "$pod_name" 2>/dev/null || true)"
    if [[ -z "$node_ip" ]]; then
        log_error "Cannot resolve node IP for '$pod_name' — skipping load generation"
        return 1
    fi

    local url="http://${pod_ip}:8080${endpoint}"
    log "Generating HTTP load on node ${node_ip} against ${url} (duration: ${duration}s)"

    # Background loop on the pod's node: hammer the endpoint until the
    # duration elapses. Kept alive across the SSH session via nohup and
    # killed by the outer bg_stop_all (pid is the ssh client process).
    (
        ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=5 -o BatchMode=yes \
            "root@${node_ip}" \
            "end_time=\$(( \$(date +%s) + ${duration} )); \
             while [ \$(date +%s) -lt \$end_time ]; do \
                 curl -s -o /dev/null '${url}' || true; \
             done" || true
    ) &
    echo "$!"
}

# ---------------------------------------------------------------------------
# start_latency_load_generation — Run the latency-recording load generator on
# the pod's node and save the per-request latency CSV into the cell dir.
#
# REQ-2: the host has no route to the pod CIDR, so generation runs on the node
# that hosts the pod (SSH), mirroring start_load_generation. latency-loadgen.sh
# streams load-generator.sh to the node and captures the CSV rows
# (timestamp,endpoint,latency_ms,status) back into the local output file.
# Any failure (script missing, pod/node resolution, SSH, generator error)
# returns non-zero so the caller can log a warning and continue (REQ-5) —
# never fatal.
#
# Arguments:
#   $1 — pod name
#   $2 — rate (requests per second)
#   $3 — duration in seconds
#   $4 — endpoint mix (e.g. "users:30,orders:30,search:20,reports:20")
#   $5 — output file path for the latency CSV (e.g. <cell-dir>/latency.csv)
# Returns: 0 on success, non-zero on failure
# ---------------------------------------------------------------------------
start_latency_load_generation() {
    local pod_name="$1"
    local rate="$2"
    local duration="$3"
    local endpoints="$4"
    local output_file="$5"

    local latency_helper="${_EXPERIMENTS_SCRIPT_DIR}/latency-loadgen.sh"
    if [[ ! -f "$latency_helper" ]]; then
        log_error "latency-loadgen.sh not found at: ${latency_helper}"
        return 1
    fi

    local pod_ip
    pod_ip="$(kubectl --kubeconfig "$KUBECONFIG" get pod "$pod_name" \
        -o jsonpath='{.status.podIP}' 2>/dev/null || true)"
    if [[ -z "$pod_ip" ]]; then
        log_error "Cannot resolve pod IP for '$pod_name' — skipping latency load generation"
        return 1
    fi

    local node_ip
    node_ip="$(get_pod_node_ip "$pod_name" 2>/dev/null || true)"
    if [[ -z "$node_ip" ]]; then
        log_error "Cannot resolve node IP for '$pod_name' — skipping latency load generation"
        return 1
    fi

    local url="http://${pod_ip}:8080"
    log "Starting latency load generation on node ${node_ip} against ${url} (rate: ${rate} req/s, duration: ${duration}s, endpoints: ${endpoints})"

    bash "$latency_helper" "$node_ip" "$url" "$rate" "$duration" "$endpoints" "$output_file"
}

# ---------------------------------------------------------------------------
# _is_http_capable_type — True when a workload type exposes an HTTP endpoint
# on :8080 that latency-loadgen.sh can drive.
#
# Arguments:
#   $1 — workload type (api-server, cpu-burner, db-simulator, latency-sensitive, ...)
# Returns: 0 if the type is HTTP-capable, 1 otherwise
# ---------------------------------------------------------------------------
_is_http_capable_type() {
    case "$1" in
        api-server|cpu-burner|db-simulator|latency-sensitive) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# resolve_latency_load_target — Choose the pod that should receive the
# top-level latency_load generation in a co-located (N-pod) experiment.
#
# The top-level latency_load block drives latency-loadgen.sh, which needs an
# HTTP endpoint on the target pod's :8080. The target is resolved by type
# priority:
#   1. the first pod whose type is api-server
#   2. else the first pod whose type is cpu-burner
#   3. else the first pod whose type is latency-sensitive
#   4. else the first pod whose type is HTTP-capable (db-simulator, or any of
#      the priority types appearing later in the mapping)
#
# Args are "<pod-name>:<type>" pairs in workloads: mapping order, so the
# result is deterministic regardless of IFS. Returns the pod name on stdout,
# or non-zero when no HTTP-capable pod exists (the caller logs a warning and
# skips generation, non-fatal).
#
# Arguments:
#   $@ — "<pod-name>:<type>" pairs, one per pod in the workloads: mapping
# Returns: pod name on stdout, or exit 1 when no HTTP-capable pod exists
# ---------------------------------------------------------------------------
resolve_latency_load_target() {
    local entry pod type priority
    for priority in api-server cpu-burner latency-sensitive; do
        for entry in "$@"; do
            pod="${entry%%:*}"
            type="${entry#*:}"
            if [[ "$type" == "$priority" ]]; then
                printf '%s\n' "$pod"
                return 0
            fi
        done
    done
    for entry in "$@"; do
        pod="${entry%%:*}"
        type="${entry#*:}"
        if _is_http_capable_type "$type"; then
            printf '%s\n' "$pod"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# wait_latency_generation — Wait for a background latency generation job and
# report whether latency.csv landed in the cell dir.
#
# REQ-5: the cell continues regardless. A completed job with a usable CSV is
# logged as saved; a failed job or missing CSV is logged as a WARNING. The
# runner never hard-fails because of latency load generation.
#
# Arguments:
#   $1 — background PID of the latency generation job (may be empty)
#   $2 — cell directory where latency.csv should have been written
# Returns: always 0
# ---------------------------------------------------------------------------
wait_latency_generation() {
    local pid="$1"
    local cell_dir="$2"
    [[ -n "$pid" ]] || return 0

    local rc=0
    wait "$pid" 2>/dev/null || rc=$?

    if [[ -f "${cell_dir}/latency.csv" ]]; then
        local rows=0
        rows="$(tail -n +2 "${cell_dir}/latency.csv" 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
        log "Latency CSV saved: ${cell_dir}/latency.csv (${rows} data rows)"
        if [[ "$rc" -ne 0 ]]; then
            log "WARNING: latency load generation exited with code ${rc} — continuing with the partial CSV (non-fatal)"
        fi
    else
        log "WARNING: latency.csv missing — latency load generation failed; continuing without it (non-fatal)"
    fi
}

# ---------------------------------------------------------------------------
# collect_cgroup_data — Run cgroup-watch.sh for a duration, save output
#
# Arguments:
#   $1 — pod name
#   $2 — duration in seconds
#   $3 — interval in seconds
#   $4 — output file path
# ---------------------------------------------------------------------------
collect_cgroup_data() {
    local pod_name="$1"
    local duration="$2"
    local interval="$3"
    local output_file="$4"

    local cgroup_watch="${RESEARCH_DIR}/bin/cgroup-watch.sh"

    [[ -f "$cgroup_watch" ]] || die "cgroup-watch.sh not found at: $cgroup_watch"
    [[ -f "$cgroup_watch" ]] || return 1

    log "Starting cgroup data collection for pod '$pod_name' (duration: ${duration}s, interval: ${interval}s)"

    # Calculate count from duration and interval
    local count=$(( duration / interval ))

    # Run cgroup-watch with timeout
    # stdout → CSV data file; stderr → console (includes [SUMMARY] line)
    timeout "$duration" bash "$cgroup_watch" "$pod_name" \
        --interval "$interval" \
        --count "$count" \
        > "$output_file" \
        2>>"${output_file}.warnings" || {
        local exit_code=$?
        # 124 = timeout (expected), 130 = SIGINT, 0 = normal exit
        if [[ $exit_code -eq 124 ]]; then
            log "cgroup-watch.sh completed (timeout after ${duration}s)"
        elif [[ $exit_code -eq 130 ]]; then
            log "cgroup-watch.sh interrupted"
        elif [[ $exit_code -eq 0 ]]; then
            log "cgroup-watch.sh completed normally"
        else
            log "WARNING: cgroup-watch.sh exited with code $exit_code"
        fi
    }
}

# ---------------------------------------------------------------------------
# collect_kubectl_top — Sample kubectl top pod periodically
#
# Arguments:
#   $1 — pod name
#   $2 — duration in seconds
#   $3 — interval in seconds
#   $4 — output file path
# ---------------------------------------------------------------------------
collect_kubectl_top() {
    local pod_name="$1"
    local duration="$2"
    local interval="$3"
    local output_file="$4"

    log "Starting kubectl top pod sampling for '$pod_name'"

    # CSV header
    printf 'timestamp,pod_name,cpu,memory\n' > "$output_file"

    local end_time=$(( $(date +%s) + duration ))
    while [[ $(date +%s) -lt $end_time ]]; do
        local ts
        ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

        kubectl --kubeconfig "$KUBECONFIG" top pod "$pod_name" --no-headers 2>/dev/null \
            | awk -v ts="$ts" '{print ts","$1","$2","$3}' \
            >> "$output_file" || true

        sleep "$interval"
    done

    log "kubectl top sampling complete"
}

# ---- Output Management ----

# ---------------------------------------------------------------------------
# make_data_dir — Create timestamped output directory
#
# Arguments:
#   $1 — experiment name
#   $2 — base data directory (default: <project_root>/research/experiments/data)
# Returns: directory path on stdout
# ---------------------------------------------------------------------------
make_data_dir() {
    local experiment_name="$1"
    local base_dir="${2:-${EXPERIMENTS_DIR}/data}"
    local timestamp
    timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
    local dir="${base_dir}/${experiment_name}/${timestamp}"

    mkdir -p "$dir" || die "Cannot create output directory: $dir"
    log "Created output directory: $dir"
    echo "$dir"
}

# ---- Config Parsing ----

# ---------------------------------------------------------------------------
# parse_yaml_value — Extract a scalar value from a flat YAML key
#
# Handles the simple YAML structure used by our experiment configs.
# Does NOT handle nested YAML — only top-level keys and simple sub-keys.
#
# Arguments:
#   $1 — config file path
#   $2 — key path (dot-separated, e.g., "experiment.name" or "workload.type")
# Returns: value on stdout
# ---------------------------------------------------------------------------
parse_yaml_value() {
    local config_file="$1"
    local key_path="$2"

    [[ -f "$config_file" ]] || die "Config file not found: $config_file"

    # Split key path into parts
    local IFS='.'
    local -a parts=($key_path)
    unset IFS

    local current_indent=""
    local search_key=""
    local found=false
    local value=""

    case ${#parts[@]} in
        1)
            search_key="${parts[0]}:"
            while IFS= read -r line; do
                # Skip comments and blank lines
                [[ "$line" =~ ^[[:space:]]*# ]] && continue
                [[ -z "${line// /}" ]] && continue

                if [[ "$line" =~ ^[[:space:]]*"${search_key}"[[:space:]] ]]; then
                    value="${line#*:}"
                    value="${value## }"
                    value="${value%\"}"
                    value="${value#\"}"
                    found=true
                    break
                fi
            done < "$config_file"
            ;;
        2)
            local parent="${parts[0]}:"
            local child="${parts[1]}:"
            local in_parent=false
            while IFS= read -r line; do
                [[ "$line" =~ ^[[:space:]]*# ]] && continue

                if [[ "$line" =~ ^[a-zA-Z] ]]; then
                    # Top-level key
                    if [[ "$line" =~ ^"${parent}"[[:space:]]*$ ]]; then
                        in_parent=true
                    else
                        in_parent=false
                    fi
                    continue
                fi

                if [[ "$in_parent" == true ]] && [[ "$line" =~ ^[[:space:]]+"${child}"[[:space:]] ]]; then
                    value="${line#*:}"
                    value="${value## }"
                    value="${value%\"}"
                    value="${value#\"}"
                    found=true
                    break
                fi
            done < "$config_file"
            ;;
    esac

    if [[ "$found" == false ]]; then
        return 1
    fi

    printf '%s' "$value"
}

# ---------------------------------------------------------------------------
# parse_yaml_subkey — Extract a value under a parent:child key with
# support for nested subkeys like workload.params.cores
#
# Arguments:
#   $1 — config file path
#   $2 — key path (dot-separated, e.g., "workload.params.endpoint")
# Returns: value on stdout
# ---------------------------------------------------------------------------
parse_yaml_subkey() {
    local config_file="$1"
    local key_path="$2"

    [[ -f "$config_file" ]] || die "Config file not found: $config_file"

    local IFS='.'
    local -a parts=($key_path)
    unset IFS

    local depth=${#parts[@]}
    if [[ $depth -lt 2 ]]; then
        parse_yaml_value "$config_file" "$key_path"
        return $?
    fi

    # Walk down the hierarchy: each part is a level deeper
    local -a ancestors=()
    local i
    for ((i = 0; i < depth - 1; i++)); do
        ancestors+=("${parts[i]}:")
    done
    local leaf="${parts[$((depth-1))]}:"

    # Build an indent depth tracker
    local in_section=0
    local value=""
    local found=false

    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// /}" ]] && continue

        # Count leading spaces
        local line_indent=0
        local temp="$line"
        while [[ "$temp" == " "* ]]; do
            temp="${temp# }"
            line_indent=$((line_indent + 1))
        done

        if [[ $line_indent -eq 0 ]]; then
            in_section=0
            # Check each ancestor
            if [[ "$line" =~ ^"${ancestors[0]}"[[:space:]]*$ ]]; then
                in_section=1
            fi
            continue
        fi

        if [[ $in_section -ge 1 ]]; then
            # Calculate our depth from indentation (2 spaces per level)
            local current_depth=$(( line_indent / 2 ))
            if [[ $current_depth -eq $((depth - 1)) ]]; then
                # This is the leaf level (relative to ancestors)
                if [[ "$line" =~ ^[[:space:]]+"${leaf}"[[:space:]] ]]; then
                    value="${line#*:}"
                    value="${value## }"
                    value="${value%\"}"
                    value="${value#\"}"
                    found=true
                    break
                fi
            fi
        fi
    done < "$config_file"

    if [[ "$found" == false ]]; then
        return 1
    fi

    printf '%s' "$value"
}

# ---------------------------------------------------------------------------
# parse_workload_entries — Extract pod names and types from a top-level
# "workloads:" mapping (N-pod co-located configs).
#
# Supports the 2-space-indented YAML shape used by the repo configs:
#     workloads:
#       pod-a:
#         type: stress-ng
#         params:
#           cores: 2
#       pod-b:
#         type: stress-ng
#
# Each pod is printed as "pod-name<TAB>type" on stdout (one line per pod).
# A pod entry without a "type:" key is printed with an empty type so the
# runner can reject it during validation.
#
# Arguments:
#   $1 — config file path
# Returns: "pod-name<TAB>type" lines on stdout
# ---------------------------------------------------------------------------
parse_workload_entries() {
    local config_file="$1"
    [[ -f "$config_file" ]] || die "Config file not found: $config_file"

    local in_workloads=false
    local current_pod=""
    local current_type=""

    # Flush the pod currently being collected. Uses bash dynamic scoping to
    # read/write current_pod/current_type from the enclosing function.
    _flush_workload_entry() {
        [[ -n "$current_pod" ]] || return 0
        printf '%s\t%s\n' "$current_pod" "$current_type"
        current_pod=""
        current_type=""
    }

    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue

        if [[ "$line" =~ ^"workloads:" ]]; then
            in_workloads=true
            continue
        fi

        if [[ "$in_workloads" == false ]]; then
            continue
        fi

        local trimmed
        trimmed="$(printf '%s' "$line" | sed 's/^[[:space:]]*//')"
        [[ -z "$trimmed" ]] && continue

        # A top-level (indent 0) key ends the workloads block
        if [[ ! "$line" =~ ^[[:space:]] ]]; then
            _flush_workload_entry
            break
        fi

        local leading="${line%%[! ]*}"
        local line_indent=${#leading}

        if [[ $line_indent -eq 2 && "$line" =~ ^[[:space:]]*[A-Za-z0-9_.-]+:[[:space:]]*$ ]]; then
            # Pod name key at indent 2 — flush the previous pod, start a new one
            _flush_workload_entry
            current_pod="$(printf '%s' "${line%%:*}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        elif [[ $line_indent -eq 4 && -n "$current_pod" && "$line" =~ ^[[:space:]]*"type:"[[:space:]] ]]; then
            current_type="$(printf '%s' "${line#*:}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | sed 's/"//g')"
        fi
    done < "$config_file"

    # Flush any remaining pod at EOF
    _flush_workload_entry
}

# ---------------------------------------------------------------------------
# parse_matrix_entries — Extract matrix entries from config
#
# Supports two formats:
#   1. Multi-line entries (each key on its own line):
#        matrix:
#          - request: "100m"
#            limit: "200m"
#   2. Semicolon-separated entries (all on one line):
#        matrix:
#          - request: "100m"; limit: "200m"
#
# Each entry is printed as a line with key=value pairs separated by
# semicolons. For example:
#   request=100m;limit=200m
#   ls_request=200m;ls_limit=500m;batch_request=1000m;batch_limit=2000m
#
# Arguments:
#   $1 — config file path
# Returns: matrix entries on stdout, one per line
# ---------------------------------------------------------------------------
parse_matrix_entries() {
    local config_file="$1"
    [[ -f "$config_file" ]] || die "Config file not found: $config_file"

    local in_matrix=false
    local entry_indent=""
    local -a current_entry=()

    # Helper to flush a collected entry
    _flush_entry() {
        [[ ${#current_entry[@]} -eq 0 ]] && return
        local result=""
        for kv in "${current_entry[@]}"; do
            if [[ -z "$result" ]]; then
                result="$kv"
            else
                result="${result};${kv}"
            fi
        done
        printf '%s\n' "$result"
        current_entry=()
    }

    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue

        # Detect start of matrix section
        if [[ "$line" =~ ^"matrix:" ]]; then
            in_matrix=true
            continue
        fi

        if [[ "$in_matrix" == true ]]; then
            local trimmed
            trimmed="$(printf '%s' "$line" | sed 's/^[[:space:]]*//')"
            [[ -z "$trimmed" ]] && continue

            # Detect end of matrix (next top-level key at indent 0)
            if [[ ! "$line" =~ ^[[:space:]] ]]; then
                _flush_entry
                break
            fi

            # Calculate line indent (number of leading spaces)
            local leading="${line%%[! ]*}"
            local line_indent=${#leading}

            # Detect list item marker "- " — start of a new entry
            if [[ "$line" =~ ^([[:space:]]*)"- "[[:space:]]* ]]; then
                _flush_entry
                entry_indent="${BASH_REMATCH[1]}"
                # Remove the "- " prefix for parsing
                local content="${line#*- }"
                content="$(printf '%s' "$content" | sed 's/^[[:space:]]*//')"
            elif [[ -n "$entry_indent" && "$line_indent" -gt ${#entry_indent} ]]; then
                # Continuation of current entry (indented more than the list marker)
                # Trim to entry-level indent
                local content="${line:$(( ${#entry_indent} + 2 ))}"
            else
                # Line at different indent — entry boundary
                _flush_entry
                entry_indent=""
                continue
            fi

            # Parse the content line into key-value pairs
            # Handle both semicolon-separated (; key: val) and plain key: val
            local content_trimmed
            content_trimmed="$(printf '%s' "$content" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
            [[ -z "$content_trimmed" ]] && continue

            # Split by semicolons if present
            local IFS=';'
            local -a parts=($content_trimmed)
            unset IFS

            for part in "${parts[@]}"; do
                local tp
                tp="$(printf '%s' "$part" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | sed "s/\"//g")"

                # Skip if no colon present
                if [[ "$tp" != *":"* ]]; then
                    continue
                fi

                # Normalize "key: value" to "key=value"
                local normalized="${tp/: /=}"
                normalized="$(printf '%s' "$normalized" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
                current_entry+=("$normalized")
            done
        fi
    done < "$config_file"

    # Flush any remaining entry at EOF
    _flush_entry
}

# ---- Experiment Metadata ----

# ---------------------------------------------------------------------------
# save_cell_metadata — Save experiment metadata for a matrix cell
#
# Arguments:
#   $1 — output directory
#   $2 — cell label (e.g., "request=100m-limit=200m")
#   $3 — replicate number
#   $4 — pod name
#   $5 — optional filename suffix (e.g., "-pod-a"); defaults to "" which
#        keeps the historical metadata.json name
# ---------------------------------------------------------------------------
save_cell_metadata() {
    local output_dir="$1"
    local cell_label="$2"
    local replicate="$3"
    local pod_name="$4"
    local pod_suffix="${5:-}"

    local node_name=""
    node_name="$(get_pod_node "$pod_name" 2>/dev/null || echo "unknown")"

    local metadata_file="${output_dir}/${cell_label}/replicate-${replicate}/metadata${pod_suffix}.json"

    mkdir -p "$(dirname "$metadata_file")"

    cat > "$metadata_file" <<EOF
{
  "timestamp": "$(date -u +'%Y-%m-%dT%H:%M:%SZ')",
  "experiment_cell": "${cell_label}",
  "replicate": ${replicate},
  "pod_name": "${pod_name}",
  "node_name": "${node_name}"
}
EOF

    log "Metadata saved: $metadata_file"
}

# ---- Perfetto Integration ----

# ---------------------------------------------------------------------------
# resolve_pod_node_ip — Get the IP of the node running a pod
#
# Wrapper around get_pod_node_ip from cgroup-common.sh.
# Arguments:
#   $1 — pod name
# Returns: node IP on stdout
# ---------------------------------------------------------------------------
resolve_pod_node_ip() {
    local pod_name="$1"
    get_pod_node_ip "$pod_name"
}

# ---------------------------------------------------------------------------
# start_perfetto_trace — Start perfetto trace on a remote node
#
# Invokes perfetto-start.sh to begin a trace on the node and returns
# the trace PID and remote trace path for later use with stop_perfetto_trace.
#
# Arguments:
#   $1 — node IP
#   $2 — config name (e.g., "scheduling")
#   $3 — duration in seconds
# Returns: "<pid> <remote-trace-path>" on stdout
# ---------------------------------------------------------------------------
start_perfetto_trace() {
    local node_ip="$1"
    local config_name="$2"
    local duration="$3"
    local perfetto_start="${PERFETTO_DIR}/bin/perfetto-start.sh"

    [[ -f "$perfetto_start" ]] || die "perfetto-start.sh not found at: ${perfetto_start}"

    bash "$perfetto_start" "$node_ip" "$config_name" --duration "$duration"
}

# ---------------------------------------------------------------------------
# stop_perfetto_trace — Stop and download a perfetto trace from a remote node
#
# Invokes perfetto-stop.sh to stop the trace process on the node and
# download the trace file to the specified output directory.
#
# Arguments:
#   $1 — node IP
#   $2 — trace PID on the node (from start_perfetto_trace)
#   $3 — output directory for the downloaded trace file
#   $4 — remote trace path on the node
# Returns: local trace file path on stdout
# ---------------------------------------------------------------------------
stop_perfetto_trace() {
    local node_ip="$1"
    local trace_pid="$2"
    local output_dir="$3"
    local remote_path="$4"
    local perfetto_stop="${PERFETTO_DIR}/bin/perfetto-stop.sh"

    [[ -f "$perfetto_stop" ]] || die "perfetto-stop.sh not found at: ${perfetto_stop}"

    bash "$perfetto_stop" "$node_ip" "$trace_pid" \
        --output-dir "$output_dir" --remote-path "$remote_path"
}

# ---- EEVDF Integration ----

# ---------------------------------------------------------------------------
# check_eevdf_available — Verify EEVDF collection tooling is present
#
# Local availability guard mirroring check_tracebox_available (perfetto) and
# check_sched_debug_available (eevdf-common.sh). With a node IP it also probes
# that /proc/<pid>/sched is readable on the node; without one it only checks
# the local tool scripts exist. Failure is non-fatal — callers log a warning
# and continue.
#
# Arguments:
#   $1 — optional node IP to probe /proc/<pid>/sched readability
# Returns: 0 if available, 1 if not
# ---------------------------------------------------------------------------
check_eevdf_available() {
    local node_ip="${1:-}"
    local eevdf_observe="${RESEARCH_DIR}/bin/eevdf-observe.sh"
    local pid_watch="${RESEARCH_DIR}/bin/cgroup-pid-watch.sh"

    [[ -f "$eevdf_observe" ]] || return 1
    [[ -f "$pid_watch" ]] || return 1

    if [[ -n "$node_ip" ]]; then
        ssh_node "$node_ip" "test -r /proc/1/sched" 2>/dev/null || return 1
    fi

    return 0
}

# ---------------------------------------------------------------------------
# get_manifest_pod_name — Extract the pod name from a workload manifest
#
# Reads the first metadata.name line from a manifest/template so the runner
# can pin per-pod artifact names (e.g. eevdf-<pod>.json) before deployment.
#
# Arguments:
#   $1 — manifest or template file path
# Returns: pod name on stdout
# ---------------------------------------------------------------------------
get_manifest_pod_name() {
    local template="$1"
    [[ -f "$template" ]] || return 1
    grep -E '^\s*name:' "$template" | head -1 | awk '{print $2}'
}

# ---------------------------------------------------------------------------
# collect_eevdf_snapshot — Capture a one-shot EEVDF JSON snapshot for a pod
#
# Runs eevdf-observe.sh <pod> and saves its JSON to the output file. Any
# failure (missing tool, unreachable cluster, timeout) returns non-zero so
# the caller can log a warning and continue — never fatal.
#
# Arguments:
#   $1 — pod name
#   $2 — output file path (e.g. <cell-dir>/eevdf-<pod>.json)
# Returns: 0 on success, 1 on collection failure
# ---------------------------------------------------------------------------
collect_eevdf_snapshot() {
    local pod_name="$1"
    local output_file="$2"
    local eevdf_observe="${RESEARCH_DIR}/bin/eevdf-observe.sh"

    [[ -f "$eevdf_observe" ]] || {
        log "WARNING: eevdf-observe.sh not found at: ${eevdf_observe}"
        return 1
    }

    log "Capturing EEVDF snapshot for pod '$pod_name'"

    if timeout 30 bash "$eevdf_observe" "$pod_name" > "$output_file" 2>>"${output_file}.warnings"; then
        log "EEVDF snapshot saved: $output_file"
        return 0
    else
        # A failed snapshot leaves an empty file behind; drop it so the cell
        # dir only contains real artifacts.
        rm -f "$output_file"
        log "WARNING: EEVDF snapshot failed for pod '$pod_name' — continuing without snapshot"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# collect_eevdf_pids — Run the per-task EEVDF time series for a pod
#
# Runs cgroup-pid-watch.sh <pod> --interval <interval> --count <n> writing
# CSV to the output file. Mirrors collect_cgroup_data: failures are logged
# (and, in the expected timeout/SIGINT cases, treated as normal completion)
# instead of aborting the experiment.
#
# Arguments:
#   $1 — pod name
#   $2 — duration in seconds
#   $3 — interval in seconds
#   $4 — output file path (e.g. <cell-dir>/eevdf-<pod>-pids.csv)
# ---------------------------------------------------------------------------
collect_eevdf_pids() {
    local pod_name="$1"
    local duration="$2"
    local interval="$3"
    local output_file="$4"
    local pid_watch="${RESEARCH_DIR}/bin/cgroup-pid-watch.sh"

    [[ -f "$pid_watch" ]] || {
        log "WARNING: cgroup-pid-watch.sh not found at: ${pid_watch}"
        return 1
    }

    log "Starting EEVDF per-task time series for pod '$pod_name' (duration: ${duration}s, interval: ${interval}s)"

    # Calculate count from duration and interval
    local count=$(( duration / interval ))

    # stdout → CSV data file; stderr → console (includes [SUMMARY] line)
    timeout "$duration" bash "$pid_watch" "$pod_name" \
        --interval "$interval" \
        --count "$count" \
        > "$output_file" \
        2>>"${output_file}.warnings" || {
        local exit_code=$?
        if [[ $exit_code -eq 124 ]]; then
            log "cgroup-pid-watch.sh completed (timeout after ${duration}s)"
        elif [[ $exit_code -eq 130 ]]; then
            log "cgroup-pid-watch.sh interrupted"
        else
            log "WARNING: cgroup-pid-watch.sh exited with code $exit_code — continuing without EEVDF time series"
        fi
    }
}

# ---------------------------------------------------------------------------
# add_eevdf_metadata — Record EEVDF artifact bookkeeping in a cell metadata file
#
# Appends eevdf_* fields (eevdf_enabled, eevdf_artifacts) to the given JSON
# metadata file. Artifact presence is checked on disk so a failed collection
# records a null entry rather than a stale path. Never fatal — failures are
# logged as warnings.
#
# Arguments:
#   $1 — metadata file path (e.g. <cell-dir>/metadata.json)
#   $2 — cell directory (absolute path where artifacts live)
#   $3..N — pod names that had EEVDF collection attempted
# ---------------------------------------------------------------------------
add_eevdf_metadata() {
    local metadata_file="$1"
    local cell_dir="$2"
    shift 2

    local rc=0
    python3 - "$metadata_file" "$cell_dir" "$@" <<'PYEOF' 2>/dev/null || rc=1
import json
import os
import sys

metadata_file, cell_dir = sys.argv[1], sys.argv[2]
pods = sys.argv[3:]

with open(metadata_file, "r") as f:
    metadata = json.load(f)

metadata["eevdf_enabled"] = True
metadata["eevdf_artifacts"] = {}
for pod in pods:
    snapshot = os.path.join(cell_dir, "eevdf-{}.json".format(pod))
    pids_csv = os.path.join(cell_dir, "eevdf-{}-pids.csv".format(pod))
    metadata["eevdf_artifacts"][pod] = {
        "snapshot": os.path.basename(snapshot) if os.path.getsize(snapshot) > 0 else None,
        "pids_csv": os.path.basename(pids_csv) if os.path.getsize(pids_csv) > 0 else None,
    }

with open(metadata_file, "w") as f:
    json.dump(metadata, f, indent=2)
PYEOF
    if [[ $rc -ne 0 ]]; then
        log "WARNING: Failed to add EEVDF metadata to ${metadata_file}"
    fi
}
