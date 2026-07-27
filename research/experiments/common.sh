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
# get_worker_ips — Return array of worker IPs from OpenTofu/Terraform
# Returns space-separated IP addresses to stdout
# ---------------------------------------------------------------------------
get_worker_ips() {
    local tf_cmd=""
    if command -v tofu &>/dev/null; then
        tf_cmd="tofu"
    elif command -v terraform &>/dev/null; then
        tf_cmd="terraform"
    else
        die "Neither 'tofu' nor 'terraform' found on PATH"
    fi

    local tf_dir="${PROJECT_ROOT}/terraform"
    if [[ ! -d "$tf_dir" ]]; then
        die "Terraform directory not found: $tf_dir"
    fi

    "${tf_cmd}" -chdir="${tf_dir}" output -json worker_ips 2>/dev/null \
        | python3 -c "import sys,json; ips=json.load(sys.stdin); print(' '.join(filter(None, ips)))" 2>/dev/null \
        || die "Failed to get worker IPs from Terraform state"
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
    # Delete first to avoid conflicts with changed fields (e.g. env vars)
    kubectl --kubeconfig "$KUBECONFIG" delete -f "$manifest" --ignore-not-found --now 2>/dev/null || true
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
# ---------------------------------------------------------------------------
save_cell_metadata() {
    local output_dir="$1"
    local cell_label="$2"
    local replicate="$3"
    local pod_name="$4"

    local node_name=""
    node_name="$(get_pod_node "$pod_name" 2>/dev/null || echo "unknown")"

    local metadata_file="${output_dir}/${cell_label}/replicate-${replicate}/metadata.json"

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
