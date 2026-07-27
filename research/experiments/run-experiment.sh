#!/usr/bin/env bash
#
# run-experiment.sh — Main entry point for CPU throttling experiments
#
# Usage:
#   run-experiment.sh <config.yaml> [--output-dir path] [--dry-run]
#   run-experiment.sh --help
#
# Orchestrates the full experiment lifecycle:
#   1. Source common library
#   2. Load and validate config
#   3. For each matrix cell x replicate:
#      a. Deploy workload pod with substituted CPU params
#      b. Pre-warm period
#      c. Collect cgroup data + kubectl top
#      d. Save data and metadata
#      e. Delete pod
#      f. Cooldown period
#   4. Generate summary CSV

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

# ---- Source common library ----
# shellcheck source=./common.sh
source "${SCRIPT_DIR}/common.sh"

# ---- Defaults ----
DRY_RUN=false
OUTPUT_BASE_DIR="${EXPERIMENTS_DIR}/data"

# ---- Background Process Tracking ----
declare -a _BG_PIDS=()

# ---- Cleanup ----
_CLEANUP_CALLED=false

_cleanup() {
    local exit_code=$?
    [[ "$_CLEANUP_CALLED" == false ]] || return 0
    _CLEANUP_CALLED=true

    # Kill any remaining background processes
    for pid in "${_BG_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done

    # Wait for them
    wait 2>/dev/null || true

    trap - EXIT ERR
    exit "$exit_code"
}

_error_handler() {
    local line=$1
    local cmd=$2
    printf '[ERROR] Command failed at line %d: %s\n' "$line" "$cmd" >&2
}

trap _cleanup EXIT
trap '_error_handler $LINENO "$BASH_COMMAND"' ERR

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} <config.yaml> [OPTIONS]

Run a CPU throttling experiment defined by the YAML config file.

Arguments:
  config.yaml       Experiment configuration file (required)

Options:
  --output-dir DIR  Output directory (default: research/experiments/data)
  --dry-run         Validate configuration and show what would run without
                    actually deploying anything
  -h, --help        Show this help and exit

Config format: See research/experiments/configs/*.yaml for examples.

The config file uses flat YAML structure with a matrix of CPU parameter
combinations. Each combination is run as a separate cell with the specified
number of replicates.

Output:
  data/<experiment-name>/<timestamp>/  — per-cell data and metadata
  data/<experiment-name>/summary.csv   — aggregated summary across all cells
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# run_cmd — Execute or dry-run a command
# ---------------------------------------------------------------------------
run_cmd() {
    if [[ "$DRY_RUN" == true ]]; then
        printf '[DRY-RUN] %s\n' "$*" >&2
        return 0
    fi
    "$@"
}

# ---------------------------------------------------------------------------
# bg_start — Track a background process PID
# ---------------------------------------------------------------------------
bg_start() {
    local pid=$!
    _BG_PIDS+=("$pid")
}

# ---------------------------------------------------------------------------
# bg_stop_all — Stop all tracked background processes
# ---------------------------------------------------------------------------
bg_stop_all() {
    local pids=("${_BG_PIDS[@]}")
    _BG_PIDS=()

    for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done

    # Wait a moment then force-kill any remaining
    sleep 1
    for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done

    wait 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# check_prerequisites — Verify all required tools and cluster state
# ---------------------------------------------------------------------------
check_prerequisites() {
    require_tools kubectl jq python3 sed grep timeout || die "Missing required tools"

    # Check for tofu or terraform
    if ! command -v tofu &>/dev/null && ! command -v terraform &>/dev/null; then
        die "Neither 'tofu' nor 'terraform' found on PATH"
    fi

    # Check if we have the research tools
    local cgroup_watch="${RESEARCH_DIR}/bin/cgroup-watch.sh"
    [[ -f "$cgroup_watch" ]] || die "cgroup-watch.sh not found at: ${cgroup_watch}"

    require_cluster
}

# ---------------------------------------------------------------------------
# clean_cell_label — Create a safe filename label from config cell params
# ---------------------------------------------------------------------------
clean_cell_label() {
    local cell="$1"
    # Replace ';' with '-' and '=' with '-' and strip empty values
    local label
    label="$(printf '%s' "$cell" \
        | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
        | sed 's/[[:space:]]*=[[:space:]]*/=/g' \
        | sed 's/;/-/g' \
        | sed 's/""/none/g')"
    printf '%s' "$label"
}

# ---------------------------------------------------------------------------
# resolve_cell_params — Parse cell string into individual parameters
# Returns key=value pairs via global associative array
# ---------------------------------------------------------------------------
declare -A CELL_PARAMS=()

resolve_cell_params() {
    local cell="$1"
    CELL_PARAMS=()

    local IFS=';'
    local -a pairs=($cell)
    unset IFS

    for pair in "${pairs[@]}"; do
        local trimmed_pair
        trimmed_pair="$(printf '%s' "$pair" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        local key="${trimmed_pair%%=*}"
        local val="${trimmed_pair#*=}"
        CELL_PARAMS["$key"]="$val"
    done
}

# ---------------------------------------------------------------------------
# get_workload_template — Get workload template path from config type
# ---------------------------------------------------------------------------
get_workload_template() {
    local workload_type="$1"
    case "$workload_type" in
        stress-ng)
            echo "${RESEARCH_DIR}/workloads/stress-ng/deploy.yaml"
            ;;
        cpu-burner)
            echo "${RESEARCH_DIR}/workloads/cpu-burner/deploy.yaml"
            ;;
        latency-sensitive)
            echo "${RESEARCH_DIR}/workloads/co-located/latency-sensitive.yaml"
            ;;
        batch)
            echo "${RESEARCH_DIR}/workloads/co-located/batch-burner.yaml"
            ;;
        *)
            die "Unknown workload type: ${workload_type}"
            ;;
    esac
}

# ===========================================================================
# main — Entry point
# ===========================================================================
main() {
    local config_file=""

    # ---- Parse arguments ----
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage 0
                ;;
            --output-dir)
                OUTPUT_BASE_DIR="${2:?--output-dir requires a value}"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            -*)
                printf 'ERROR: Unknown option: %s\n' "$1" >&2
                usage 1
                ;;
            *)
                if [[ -z "$config_file" ]]; then
                    config_file="$1"
                    shift
                else
                    printf 'ERROR: Unexpected argument: %s\n' "$1" >&2
                    usage 1
                fi
                ;;
        esac
    done

    # ---- Validate config file ----
    [[ -n "$config_file" ]] || { printf 'ERROR: config file required\n' >&2; usage 1; }
    [[ -f "$config_file" ]] || die "Config file not found: ${config_file}"

    # ---- Resolve project root before anything else ----
    resolve_project_root

    # ---- Parse config values ----
    local experiment_name
    experiment_name="$(parse_yaml_value "$config_file" "experiment.name")" \
        || die "Config missing 'experiment.name'"

    local experiment_desc
    experiment_desc="$(parse_yaml_value "$config_file" "experiment.description" 2>/dev/null || echo "")"

    local replicates
    replicates="$(parse_yaml_value "$config_file" "replicates")" \
        || die "Config missing 'replicates'"
    [[ "$replicates" =~ ^[0-9]+$ && "$replicates" -ge 1 ]] \
        || die "Invalid replicates value: ${replicates}"

    local pre_warm
    pre_warm="$(parse_yaml_value "$config_file" "pre_warm")" \
        || die "Config missing 'pre_warm'"

    local duration
    duration="$(parse_yaml_value "$config_file" "duration")" \
        || die "Config missing 'duration'"

    local cooldown
    cooldown="$(parse_yaml_value "$config_file" "cooldown")" \
        || die "Config missing 'cooldown'"

    local cgroup_interval
    cgroup_interval="$(parse_yaml_value "$config_file" "measurement.cgroup_interval" 2>/dev/null)" \
        || cgroup_interval="$(parse_yaml_value "$config_file" "measurement:cgroup_interval" 2>/dev/null)" \
        || die "Config missing 'measurement.cgroup_interval'"

    # Detect if this is a co-located experiment (key: "workloads:" at top level)
    local is_colocated=false
    local ls_workload_type="" batch_workload_type=""
    if grep -qE '^workloads:' "$config_file" 2>/dev/null; then
        is_colocated=true
        log "Detected co-located experiment configuration"
    fi

    local single_workload_type=""
    local single_workload_params_endpoint=""
    if [[ "$is_colocated" == false ]]; then
        single_workload_type="$(parse_yaml_value "$config_file" "workload.type")" \
            || die "Config missing 'workload.type'"
        single_workload_params_endpoint="$(parse_yaml_value "$config_file" "workload.params.endpoint" 2>/dev/null || true)"
    fi

    # ---- Parse matrix entries ----
    local -a matrix_entries=()
    while IFS= read -r entry; do
        [[ -n "$entry" ]] && matrix_entries+=("$entry")
    done < <(parse_matrix_entries "$config_file")

    [[ ${#matrix_entries[@]} -gt 0 ]] || die "No matrix entries found in config"

    # ---- Log experiment info ----
    log "============================================================"
    log "Experiment: ${experiment_name}"
    log "  Description: ${experiment_desc}"
    log "  Replicates: ${replicates}"
    log "  Pre-warm: ${pre_warm}s"
    log "  Duration: ${duration}s"
    log "  Cooldown: ${cooldown}s"
    log "  Cgroup interval: ${cgroup_interval}s"
    log "  Matrix cells: ${#matrix_entries[@]}"
    log "  Data directory: ${OUTPUT_BASE_DIR}"
    log "============================================================"

    if [[ "$DRY_RUN" == true ]]; then
        log "DRY RUN MODE — no deployments will be made"
        log ""
        log "Matrix cells:"
        for entry in "${matrix_entries[@]}"; do
            log "  - ${entry}"
        done
        log ""
        log "Prerequisites check passed — cluster reachable, tools found"
        log "Experiment would run ${#matrix_entries[@]} cells x ${replicates} replicates = $(( ${#matrix_entries[@]} * replicates )) total runs"
        log "Estimated total time: $(( (pre_warm + duration + cooldown) * ${#matrix_entries[@]} * replicates ))s"
        return 0
    fi

    # ---- Verify prerequisites (only if not dry-run) ----
    check_prerequisites

    # ---- Create output directory ----
    local base_data_dir
    base_data_dir="$(make_data_dir "$experiment_name" "$OUTPUT_BASE_DIR")"

    # ---- Summary CSV header ----
    local summary_file="${OUTPUT_BASE_DIR}/${experiment_name}/summary.csv"
    mkdir -p "$(dirname "$summary_file")"

    local summary_header="cell_label,replicate,nr_periods,nr_throttled,throttled_usec,usage_usec,cpu_weight,cpu_max"
    printf '%s\n' "$summary_header" > "$summary_file"

    # ---- Main experiment loop ----
    local total_cells=${#matrix_entries[@]}
    local cell_idx=0

    for entry in "${matrix_entries[@]}"; do
        resolve_cell_params "$entry"
        local cell_label
        cell_label="$(clean_cell_label "$entry")"
        cell_idx=$((cell_idx + 1))

        for ((rep = 1; rep <= replicates; rep++)); do
            log ""
            log "--- Cell ${cell_idx}/${total_cells}: ${cell_label} (replicate ${rep}/${replicates}) ---"

            # Build cell output directory
            local cell_dir="${base_data_dir}/${cell_label}/replicate-${rep}"
            mkdir -p "$cell_dir"

            if [[ "$is_colocated" == true ]]; then
                # ---- Co-located experiment ----
                local ls_request="${CELL_PARAMS[ls_request]:-}"
                local ls_limit="${CELL_PARAMS[ls_limit]:-}"
                local batch_request="${CELL_PARAMS[batch_request]:-}"
                local batch_limit="${CELL_PARAMS[batch_limit]:-}"

                log "Co-located config — LS: req=${ls_request} lim=${ls_limit}, Batch: req=${batch_request} lim=${batch_limit}"

                # Substitute templates for latency-sensitive workload
                local ls_template
                ls_template="$(get_workload_template "latency-sensitive")"
                local ls_manifest="${cell_dir}/latency-sensitive.yaml"
                LS_CPU_REQUEST="${ls_request}" LS_CPU_LIMIT="${ls_limit}" \
                    substitute_cpu_params "$ls_template" "$ls_request" "$ls_limit" "$ls_manifest"

                # Substitute templates for batch workload
                local batch_template
                batch_template="$(get_workload_template "batch")"
                local batch_manifest="${cell_dir}/batch-burner.yaml"
                BATCH_CPU_REQUEST="${batch_request}" BATCH_CPU_LIMIT="${batch_limit}" \
                    substitute_cpu_params "$batch_template" "$batch_request" "$batch_limit" "$batch_manifest"

                # Deploy both pods
                kubectl --kubeconfig "$KUBECONFIG" delete -f "$ls_manifest" --ignore-not-found --now 2>/dev/null || true
                kubectl --kubeconfig "$KUBECONFIG" delete -f "$batch_manifest" --ignore-not-found --now 2>/dev/null || true
                kubectl --kubeconfig "$KUBECONFIG" apply -f "$ls_manifest" >/dev/null
                kubectl --kubeconfig "$KUBECONFIG" apply -f "$batch_manifest" >/dev/null

                local ls_pod_name="latency-sensitive"
                local batch_pod_name="batch-burner"

                # Wait for both pods
                wait_for_pod_running "$ls_pod_name" || die "Latency-sensitive pod did not start"
                wait_for_pod_running "$batch_pod_name" || die "Batch pod did not start"

                # Pre-warm
                log "Pre-warm period: ${pre_warm}s"
                run_cmd sleep "$pre_warm"

                # Collect cgroup data for both pods (in background)
                local ls_cgroup_file="${cell_dir}/cgroup-latency-sensitive.csv"
                local batch_cgroup_file="${cell_dir}/cgroup-batch-burner.csv"

                collect_cgroup_data "$ls_pod_name" "$duration" "$cgroup_interval" "$ls_cgroup_file" &
                bg_start
                collect_cgroup_data "$batch_pod_name" "$duration" "$cgroup_interval" "$batch_cgroup_file" &
                bg_start

                # Collect kubectl top for both
                local top_file="${cell_dir}/kubectl-top.csv"
                collect_kubectl_top "$ls_pod_name" "$duration" "$cgroup_interval" "${cell_dir}/kubectl-top-ls.csv" &
                bg_start
                collect_kubectl_top "$batch_pod_name" "$duration" "$cgroup_interval" "${cell_dir}/kubectl-top-batch.csv" &
                bg_start

                # Wait for duration
                log "Measurement period: ${duration}s"
                run_cmd sleep "$duration"

                # Stop background processes
                log "Stopping data collection..."
                bg_stop_all

                # Save metadata
                save_cell_metadata "$base_data_dir" "$cell_label" "$rep" "$ls_pod_name"

                # For co-located, we need a combined summary entry
                # Parse the first CSV from each to get last stats
                local ls_last_stats=""
                ls_last_stats="$(tail -1 "$ls_cgroup_file" 2>/dev/null || true)"
                local batch_last_stats=""
                batch_last_stats="$(tail -1 "$batch_cgroup_file" 2>/dev/null || true)"

                # Extract values (CSV: timestamp,pod,container,nr_periods,nr_throttled,throttled_usec,usage_usec,cpu_weight,cpu_max_quota,cpu_max_period)
                if [[ -n "$ls_last_stats" ]]; then
                    local ls_nr_periods ls_nr_throttled ls_throttled_usec ls_usage_usec ls_cpu_weight ls_cpu_max
                    ls_nr_periods="$(printf '%s' "$ls_last_stats" | cut -d',' -f4)"
                    ls_nr_throttled="$(printf '%s' "$ls_last_stats" | cut -d',' -f5)"
                    ls_throttled_usec="$(printf '%s' "$ls_last_stats" | cut -d',' -f6)"
                    ls_usage_usec="$(printf '%s' "$ls_last_stats" | cut -d',' -f7)"
                    ls_cpu_weight="$(printf '%s' "$ls_last_stats" | cut -d',' -f8)"
                    ls_cpu_max="$(printf '%s' "$ls_last_stats" | cut -d',' -f9)"
                    printf 'ls-%s,%d,%s,%s,%s,%s,%s,%s\n' \
                        "$cell_label" "$rep" \
                        "$ls_nr_periods" "$ls_nr_throttled" "$ls_throttled_usec" \
                        "$ls_usage_usec" "$ls_cpu_weight" "$ls_cpu_max" \
                        >> "$summary_file"
                fi
                if [[ -n "$batch_last_stats" ]]; then
                    local batch_nr_periods batch_nr_throttled batch_throttled_usec batch_usage_usec batch_cpu_weight batch_cpu_max
                    batch_nr_periods="$(printf '%s' "$batch_last_stats" | cut -d',' -f4)"
                    batch_nr_throttled="$(printf '%s' "$batch_last_stats" | cut -d',' -f5)"
                    batch_throttled_usec="$(printf '%s' "$batch_last_stats" | cut -d',' -f6)"
                    batch_usage_usec="$(printf '%s' "$batch_last_stats" | cut -d',' -f7)"
                    batch_cpu_weight="$(printf '%s' "$batch_last_stats" | cut -d',' -f8)"
                    batch_cpu_max="$(printf '%s' "$batch_last_stats" | cut -d',' -f9)"
                    printf 'batch-%s,%d,%s,%s,%s,%s,%s,%s\n' \
                        "$cell_label" "$rep" \
                        "$batch_nr_periods" "$batch_nr_throttled" "$batch_throttled_usec" \
                        "$batch_usage_usec" "$batch_cpu_weight" "$batch_cpu_max" \
                        >> "$summary_file"
                fi

                # Delete pods
                delete_pod "$ls_pod_name"
                delete_pod "$batch_pod_name"

            else
                # ---- Single workload experiment ----
                local cpu_request="${CELL_PARAMS[request]:-}"
                local cpu_limit="${CELL_PARAMS[limit]:-}"

                log "Config — request='${cpu_request}' limit='${cpu_limit}'"

                # Get template and substitute
                local template
                template="$(get_workload_template "$single_workload_type")"
                local manifest="${cell_dir}/deploy.yaml"
                substitute_cpu_params "$template" "$cpu_request" "$cpu_limit" "$manifest"

                # Deploy pod
                local pod_name
                pod_name="$(run_cmd deploy_pod "$manifest")"

                # Pre-warm period
                log "Pre-warm period: ${pre_warm}s"
                run_cmd sleep "$pre_warm"

                # Start cgroup data collection in background
                local cgroup_file="${cell_dir}/cgroup.csv"
                run_cmd collect_cgroup_data "$pod_name" "$duration" "$cgroup_interval" "$cgroup_file" &
                bg_start

                # Start kubectl top sampling in background
                local top_file="${cell_dir}/kubectl-top.csv"
                run_cmd collect_kubectl_top "$pod_name" "$duration" "$cgroup_interval" "$top_file" &
                bg_start

                # Wait for measurement duration
                log "Measurement period: ${duration}s"
                run_cmd sleep "$duration"

                # Stop background processes
                log "Stopping data collection..."
                bg_stop_all

                # Save metadata
                save_cell_metadata "$base_data_dir" "$cell_label" "$rep" "$pod_name"

                # Parse last data line of cgroup CSV for summary
                local last_stats=""
                last_stats="$(grep -v '^timestamp\|^$' "$cgroup_file" 2>/dev/null | tail -1 || true)"

                if [[ -n "$last_stats" ]]; then
                    local nr_periods nr_throttled throttled_usec usage_usec cpu_weight cpu_max
                    nr_periods="$(printf '%s' "$last_stats" | cut -d',' -f4)"
                    nr_throttled="$(printf '%s' "$last_stats" | cut -d',' -f5)"
                    throttled_usec="$(printf '%s' "$last_stats" | cut -d',' -f6)"
                    usage_usec="$(printf '%s' "$last_stats" | cut -d',' -f7)"
                    cpu_weight="$(printf '%s' "$last_stats" | cut -d',' -f8)"
                    cpu_max="$(printf '%s' "$last_stats" | cut -d',' -f9)"
                    printf '%s,%d,%s,%s,%s,%s,%s,%s\n' \
                        "$cell_label" "$rep" \
                        "$nr_periods" "$nr_throttled" "$throttled_usec" \
                        "$usage_usec" "$cpu_weight" "$cpu_max" \
                        >> "$summary_file"
                fi

                # Delete pod
                run_cmd delete_pod "$pod_name"
            fi

            # Cooldown period
            log "Cooldown period: ${cooldown}s"
            run_cmd sleep "$cooldown"

        done
    done

    # ---- Final summary ----
    log ""
    log "============================================================"
    log "Experiment complete: ${experiment_name}"
    log "  Summary: ${summary_file}"
    log "  Data: ${base_data_dir}"
    log "============================================================"
    log ""
    log "Summary CSV columns: ${summary_header}"
    log "Use 'make report' to generate analysis"
}

main "$@"
