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
PERFETTO_ENABLED=false
PERFETTO_CONFIG="scheduling"
EEVDF_ENABLED=false

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
  --output-dir DIR  Output directory (default: research/cpu-sched/experiments/data)
  --dry-run         Validate configuration and show what would run without
                    actually deploying anything
  --perfetto        Enable Perfetto tracing on workload nodes
  --perfetto-config CONFIG
                    Perfetto trace config name (default: scheduling)
  --eevdf           Collect per-pod EEVDF scheduler metrics during the
                    measurement window (JSON snapshots + per-task time series)
  -h, --help        Show this help and exit

Config format: See research/cpu-sched/experiments/configs/*.yaml for examples.

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
        # Join arguments with spaces for the dry-run message. A plain "$*"
        # would join on the first char of IFS ($'\n\t'), splitting the
        # command across lines.
        printf '[DRY-RUN]' >&2
        for arg in "$@"; do
            printf ' %s' "$arg" >&2
        done
        printf '\n' >&2
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
# millicores_of — Normalize a CPU quantity to integer millicores
#
# Handles the repo's millicore format ("500m" -> 500), bare CPU counts
# ("2" -> 2000), and the unset sentinel ("" or "max" -> 0). Any other value
# is a config error and dies with a message naming the offending value.
#
# Arguments:
#   $1 — CPU quantity string
# Returns: integer millicores on stdout
# ---------------------------------------------------------------------------
millicores_of() {
    local val="${1:-}"
    if [[ -z "$val" || "$val" == "max" ]]; then
        printf '0\n'
        return 0
    fi
    case "$val" in
        *m) val="${val%m}" ;;
    esac
    [[ "$val" =~ ^[0-9]+$ ]] \
        || die "Invalid CPU quantity '${1}' (expected millicores like '500m')"
    printf '%s\n' "$(( 10#$val ))"
}

# ---------------------------------------------------------------------------
# validate_cell_cpu_params — Reject a matrix cell whose request exceeds limit
#
# Compares every request/limit pair in the cell as integer millicores (the
# "m" suffix is stripped). Covers all key shapes used by the configs:
#   single-pod        request/limit
#   legacy 2-pod      ls_request/ls_limit, batch_request/batch_limit
#   generic N-pod     <prefix>_request/<prefix>_limit (a_, b_, c_, ...)
#
# A pair is checked only when BOTH values are non-empty; empty request and/or
# empty limit are valid (BestEffort / request-only cells) and are left alone.
# An offending pair dies with a clear error naming the cell and the keys.
#
# Arguments:
#   $1 — matrix cell string (semicolon-separated key=value pairs)
# ---------------------------------------------------------------------------
validate_cell_cpu_params() {
    local cell="$1"
    resolve_cell_params "$cell"

    local key
    for key in "${!CELL_PARAMS[@]}"; do
        local req_key="" lim_key=""
        case "$key" in
            request)        req_key="request";       lim_key="limit" ;;
            ls_request)     req_key="ls_request";    lim_key="ls_limit" ;;
            batch_request)  req_key="batch_request"; lim_key="batch_limit" ;;
            *_request)      req_key="$key";          lim_key="${key%_request}_limit" ;;
            *)              continue ;;
        esac

        local req="${CELL_PARAMS[$req_key]:-}"
        local lim="${CELL_PARAMS[$lim_key]:-}"
        [[ -n "$req" && -n "$lim" ]] || continue

        local req_m lim_m
        req_m="$(millicores_of "$req")"
        lim_m="$(millicores_of "$lim")"
        if (( req_m > lim_m )); then
            die "Invalid cell: ${req_key}=${req} exceeds ${lim_key}=${lim} (cell: ${cell})"
        fi
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
        stress-ng-guaranteed)
            echo "${RESEARCH_DIR}/workloads/stress-ng/deploy-guaranteed.yaml"
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
        api-server)
            echo "${RESEARCH_DIR}/workloads/api-server/deploy.yaml"
            ;;
        db-simulator)
            echo "${RESEARCH_DIR}/workloads/db-simulator/deploy.yaml"
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
            --perfetto)
                PERFETTO_ENABLED=true
                shift
                ;;
            --perfetto-config)
                PERFETTO_CONFIG="${2:?--perfetto-config requires a value}"
                shift 2
                ;;
            --eevdf)
                EEVDF_ENABLED=true
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

    # ---- Perfetto initialization ----
    if [[ "$PERFETTO_ENABLED" == true ]]; then
        # Validate that config name is non-empty
        if [[ -z "$PERFETTO_CONFIG" ]]; then
            die "Perfetto config name is empty"
        fi

        # Validate that the local config file exists
        # (mirrors perfetto_config_path logic from perfetto-common.sh)
        if [[ "$PERFETTO_CONFIG" == */* ]]; then
            die "Invalid perfetto config name: ${PERFETTO_CONFIG}"
        fi
        local config_basename="${PERFETTO_CONFIG}"
        if [[ "$config_basename" != *.* ]]; then
            config_basename="${config_basename}.cfg"
        fi
        local local_config="${SCRIPT_DIR}/../perfetto/configs/${config_basename}"
        if [[ ! -f "$local_config" ]]; then
            die "Perfetto config not found: ${local_config}"
        fi

        log "Perfetto tracing enabled (config: ${PERFETTO_CONFIG})"
    fi

    # ---- EEVDF initialization ----
    # Availability is only gateable in real runs (dry-run always advertises the
    # plan); missing tooling degrades gracefully to no EEVDF collection.
    if [[ "$EEVDF_ENABLED" == true && "$DRY_RUN" == false ]]; then
        if check_eevdf_available; then
            log "EEVDF scheduler metric collection enabled"
        else
            log "WARNING: EEVDF tooling unavailable (eevdf-observe.sh/cgroup-pid-watch.sh) — continuing without EEVDF metrics"
            EEVDF_ENABLED=false
        fi
    fi

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

    # Optional top-level node: key pins every pod manifest
    # to a worker node via spec.nodeName. Absent key keeps the historical w1
    # default (backward compat). The value is injected by
    # substitute_cpu_params / substitute_pod_manifest through NODE_NAME.
    local node_name
    node_name="$(parse_yaml_value "$config_file" "node" 2>/dev/null || echo "w1")"
    [[ -n "$node_name" ]] || node_name="w1"
    NODE_NAME="$node_name"

    # Detect if this is a co-located experiment (key: "workloads:" at top level)
    local is_colocated=false
    local legacy_colocated=false
    local ls_workload_type="" batch_workload_type=""
    if grep -qE '^workloads:' "$config_file" 2>/dev/null; then
        is_colocated=true
        log "Detected co-located experiment configuration"
    fi

    # ---- Parse the workloads: mapping (N-pod co-located configs) ----
    local -a workload_pods=()
    local -a workload_types=()
    if [[ "$is_colocated" == true ]]; then
        while IFS=$'\t' read -r pod_name pod_type; do
            [[ -n "$pod_name" ]] || continue
            workload_pods+=("$pod_name")
            workload_types+=("$pod_type")
        done < <(parse_workload_entries "$config_file")

        [[ ${#workload_pods[@]} -gt 0 ]] || die "Config has 'workloads:' but no workload entries found"

        # Legacy special case: exactly latency-sensitive + batch keeps the
        # original 2-pod co-located path (old configs and old matrix keys).
        if [[ ${#workload_pods[@]} -eq 2 \
            && "${workload_pods[0]}" == "latency-sensitive" \
            && "${workload_pods[1]}" == "batch" ]]; then
            legacy_colocated=true
        fi

        # Generic N-pod validation: every pod needs a known workload type
        if [[ "$legacy_colocated" == false ]]; then
            for i in "${!workload_pods[@]}"; do
                local wpod="${workload_pods[$i]}"
                local wtype="${workload_types[$i]}"
                if [[ -z "$wtype" ]]; then
                    die "Pod '${wpod}' under 'workloads:' is missing a 'type' key"
                fi
                get_workload_template "$wtype" >/dev/null
            done
            log "N-pod co-located configuration: ${#workload_pods[@]} workloads"
        fi
    fi

    # Co-located configs may declare a top-level latency_load block
    # (same sub-keys as workload.params.latency_load). It targets the
    # latency-sensitive pod: the "latency-sensitive" pod in the legacy 2-pod
    # layout, or the first pod whose type is latency-sensitive in the generic
    # N-pod layout. Absent block keeps the legacy behaviour unchanged.
    local colocated_latency_load=""
    local colocated_latency_rate=""
    local colocated_latency_duration=""
    local colocated_latency_endpoints=""
    if [[ "$is_colocated" == true ]]; then
        colocated_latency_rate="$(parse_yaml_subkey "$config_file" "latency_load.rate" 2>/dev/null || true)"
        if [[ -n "$colocated_latency_rate" ]]; then
            colocated_latency_load="true"
            colocated_latency_duration="$(parse_yaml_subkey "$config_file" "latency_load.duration" 2>/dev/null || true)"
            colocated_latency_endpoints="$(parse_yaml_subkey "$config_file" "latency_load.endpoints" 2>/dev/null || true)"
            [[ -n "$colocated_latency_duration" ]] || colocated_latency_duration="$duration"
            [[ -n "$colocated_latency_endpoints" ]] || colocated_latency_endpoints="users:30,orders:30,search:20,reports:20"
        fi
    fi

    local single_workload_type=""
    local single_workload_params_endpoint=""
    local single_workload_latency_load=""
    local single_workload_latency_rate=""
    local single_workload_latency_duration=""
    local single_workload_latency_endpoints=""
    if [[ "$is_colocated" == false ]]; then
        single_workload_type="$(parse_yaml_value "$config_file" "workload.type")" \
            || die "Config missing 'workload.type'"
        single_workload_params_endpoint="$(parse_yaml_subkey "$config_file" "workload.params.endpoint" 2>/dev/null || true)"

        # Optional latency-recording load generation block. The rate is
        # the presence probe; duration defaults to the experiment duration and
        # the endpoint mix to the generator's default when not declared.
        single_workload_latency_rate="$(parse_yaml_subkey "$config_file" "workload.params.latency_load.rate" 2>/dev/null || true)"
        if [[ -n "$single_workload_latency_rate" ]]; then
            single_workload_latency_load="true"
            single_workload_latency_duration="$(parse_yaml_subkey "$config_file" "workload.params.latency_load.duration" 2>/dev/null || true)"
            single_workload_latency_endpoints="$(parse_yaml_subkey "$config_file" "workload.params.latency_load.endpoints" 2>/dev/null || true)"
            [[ -n "$single_workload_latency_duration" ]] || single_workload_latency_duration="$duration"
            [[ -n "$single_workload_latency_endpoints" ]] || single_workload_latency_endpoints="users:30,orders:30,search:20,reports:20"
        fi
    fi

    # ---- Parse matrix entries ----
    local -a matrix_entries=()
    while IFS= read -r entry; do
        [[ -n "$entry" ]] && matrix_entries+=("$entry")
    done < <(parse_matrix_entries "$config_file")

    [[ ${#matrix_entries[@]} -gt 0 ]] || die "No matrix entries found in config"

    # Reject any cell whose request exceeds its limit before
    # anything runs. Applied to every matrix entry up front so --dry-run and
    # real runs fail identically, across single-pod, legacy 2-pod, and generic
    # N-pod key shapes.
    local entry
    for entry in "${matrix_entries[@]}"; do
        validate_cell_cpu_params "$entry"
    done

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

        # Latency load generation plan (single-pod workload.params.latency_load)
        if [[ "$is_colocated" == false && -n "$single_workload_latency_load" ]]; then
            local dry_cell_dir="${OUTPUT_BASE_DIR}/${experiment_name}/<cell>/replicate-<n>"
            log ""
            log "Latency load generation (workload.params.latency_load):"
            log "  Target: <pod> at http://<pod-ip>:8080 (runs on the pod's node via SSH)"
            log "  Rate: ${single_workload_latency_rate} req/s"
            log "  Duration: ${single_workload_latency_duration}s"
            log "  Endpoints: ${single_workload_latency_endpoints}"
            log "  CSV: ${dry_cell_dir}/latency.csv"
            log "  Degradation: generator failure is non-fatal — the cell continues with a warning and latency.csv may be absent"
        fi

        # Latency load generation plan (co-located top-level latency_load)
        if [[ "$is_colocated" == true && -n "$colocated_latency_load" ]]; then
            local dry_cell_dir="${OUTPUT_BASE_DIR}/${experiment_name}/<cell>/replicate-<n>"
            local dry_latency_target="latency-sensitive"
            if [[ "$legacy_colocated" == false ]]; then
                local -a latency_args=()
                for i in "${!workload_pods[@]}"; do
                    latency_args+=("${workload_pods[$i]}:${workload_types[$i]}")
                done
                # Target api-server/cpu-burner/latency-sensitive
                # (in that order) or the first HTTP-capable pod, not just
                # latency-sensitive — Family D uses an api-server LS pod.
                dry_latency_target="$(resolve_latency_load_target "${latency_args[@]}")" \
                    || dry_latency_target="<http-capable pod>"
            fi
            log ""
            log "Latency load generation (top-level latency_load):"
            log "  Target pod: ${dry_latency_target}"
            log "  Rate: ${colocated_latency_rate} req/s"
            log "  Duration: ${colocated_latency_duration}s"
            log "  Endpoints: ${colocated_latency_endpoints}"
            log "  CSV: ${dry_cell_dir}/latency.csv"
            log "  Degradation: generator failure is non-fatal — the cell continues with a warning and latency.csv may be absent"
        fi

        # Single-pod deployment plan. Dry-run must make node
        # pinning visible per pod (spec.nodeName from the config `node:` key,
        # default w1) — today the single-pod path prints no deployment line.
        if [[ "$is_colocated" == false ]]; then
            local dry_cell_dir="${OUTPUT_BASE_DIR}/${experiment_name}/<cell>/replicate-<n>"
            local single_template single_pod_name
            single_template="$(get_workload_template "$single_workload_type")"
            single_pod_name="$(get_manifest_pod_name "$single_template" 2>/dev/null || true)"
            log ""
            log "Workload deployment:"
            log "  Pod: ${single_pod_name} (type: ${single_workload_type}) -> nodeName: ${node_name}"
            log "  Manifest: ${dry_cell_dir}/deploy.yaml"
        fi

        if [[ "$is_colocated" == true && "$legacy_colocated" == false ]]; then
            # Generic N-pod plan: enumerate every deployment and
            # data-collection stream. Resolve the first matrix cell so the
            # shown request/limit values are the real ones.
            if [[ ${#matrix_entries[@]} -gt 0 ]]; then
                resolve_cell_params "${matrix_entries[0]}"
            fi
            local dry_cell_dir="${OUTPUT_BASE_DIR}/${experiment_name}/<cell>/replicate-<n>"
            log ""
            log "N-pod co-located workloads:"
            for i in "${!workload_pods[@]}"; do
                local wpod="${workload_pods[$i]}"
                local wtype="${workload_types[$i]}"
                local wreq_key="${wpod#pod-}_request"
                local wlim_key="${wpod#pod-}_limit"
                if [[ -n "${CELL_PARAMS[${wpod}_request]+x}" ]]; then
                    wreq_key="${wpod}_request"
                    wlim_key="${wpod}_limit"
                fi
                local wtemplate
                wtemplate="$(get_workload_template "$wtype")"
                log "  Deployment $((i + 1))/${#workload_pods[@]}: ${wpod} (type: ${wtype}, request=${CELL_PARAMS[$wreq_key]:-}, limit=${CELL_PARAMS[$wlim_key]:-}) -> nodeName: ${node_name}"
                run_cmd substitute_pod_manifest "$wtemplate" "$wpod" \
                    "${CELL_PARAMS[$wreq_key]:-}" "${CELL_PARAMS[$wlim_key]:-}" \
                    "${dry_cell_dir}/${wpod}.yaml"
            done
            log ""
            log "Data collection per pod:"
            for pod in "${workload_pods[@]}"; do
                run_cmd collect_cgroup_data "$pod" "$duration" "$cgroup_interval" "${dry_cell_dir}/cgroup-${pod}.csv"
                run_cmd collect_kubectl_top "$pod" "$duration" "$cgroup_interval" "${dry_cell_dir}/kubectl-top-${pod}.csv"
            done
        fi

        if [[ "$PERFETTO_ENABLED" == true ]]; then
            log ""
            log "Perfetto tracing enabled:"
            log "  Config: ${PERFETTO_CONFIG}"
            log "  Node resolution: per-cell pod node IP lookup"
            log "  Trace capture: start -> measurement -> stop -> download"
            log "  Trace file: <cell-dir>/perfetto-trace.perfetto-trace"
            log "  Metadata: perfetto_trace_file, perfetto_config in metadata.json"
        fi

        if [[ "$EEVDF_ENABLED" == true ]]; then
            local dry_cell_dir="${OUTPUT_BASE_DIR}/${experiment_name}/<cell>/replicate-<n>"
            log ""
            log "EEVDF scheduler metric collection enabled:"
            if [[ "$is_colocated" == true && "$legacy_colocated" == true ]]; then
                # Legacy 2-pod co-located: one EEVDF stream per pod
                for pod in latency-sensitive batch-burner; do
                    log "  Snapshot: ${dry_cell_dir}/eevdf-${pod}.json (eevdf-observe.sh)"
                    run_cmd collect_eevdf_snapshot "$pod" "${dry_cell_dir}/eevdf-${pod}.json"
                    log "  Time series: ${dry_cell_dir}/eevdf-${pod}-pids.csv (cgroup-pid-watch.sh)"
                    run_cmd collect_eevdf_pids "$pod" "$duration" "$cgroup_interval" "${dry_cell_dir}/eevdf-${pod}-pids.csv"
                done
            elif [[ "$is_colocated" == true ]]; then
                # Generic N-pod co-located: one EEVDF stream per pod
                for pod in "${workload_pods[@]}"; do
                    log "  Snapshot: ${dry_cell_dir}/eevdf-${pod}.json (eevdf-observe.sh)"
                    run_cmd collect_eevdf_snapshot "$pod" "${dry_cell_dir}/eevdf-${pod}.json"
                    log "  Time series: ${dry_cell_dir}/eevdf-${pod}-pids.csv (cgroup-pid-watch.sh)"
                    run_cmd collect_eevdf_pids "$pod" "$duration" "$cgroup_interval" "${dry_cell_dir}/eevdf-${pod}-pids.csv"
                done
            else
                # Single-pod: artifact name pinned to the manifest pod name
                local single_pod_name
                single_pod_name="$(get_manifest_pod_name "$(get_workload_template "$single_workload_type")" 2>/dev/null || true)"
                log "  Snapshot: ${dry_cell_dir}/eevdf-${single_pod_name}.json (eevdf-observe.sh)"
                run_cmd collect_eevdf_snapshot "$single_pod_name" "${dry_cell_dir}/eevdf-${single_pod_name}.json"
                log "  Time series: ${dry_cell_dir}/eevdf-${single_pod_name}-pids.csv (cgroup-pid-watch.sh)"
                run_cmd collect_eevdf_pids "$single_pod_name" "$duration" "$cgroup_interval" "${dry_cell_dir}/eevdf-${single_pod_name}-pids.csv"
            fi
            log "  Metadata: eevdf_* fields (eevdf_enabled, eevdf_artifacts) in cell metadata.json"
        fi
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

            if [[ "$is_colocated" == true && "$legacy_colocated" == true ]]; then
                # ---- Co-located experiment (legacy 2-pod special case) ----
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

                # ---- Latency load generation (top-level latency_load → latency-sensitive pod) ----
                local latency_pid=""
                if [[ -n "$colocated_latency_load" && "$DRY_RUN" == false ]]; then
                    log "Starting latency load generation against latency-sensitive pod (rate: ${colocated_latency_rate} req/s, duration: ${colocated_latency_duration}s, endpoints: ${colocated_latency_endpoints})"
                    start_latency_load_generation "$ls_pod_name" \
                        "$colocated_latency_rate" \
                        "$colocated_latency_duration" \
                        "$colocated_latency_endpoints" \
                        "${cell_dir}/latency.csv" &
                    latency_pid=$!
                fi

                # ---- EEVDF snapshots (start of measurement window) ----
                if [[ "$EEVDF_ENABLED" == true ]]; then
                    collect_eevdf_snapshot "$ls_pod_name" "${cell_dir}/eevdf-${ls_pod_name}.json" || true
                    collect_eevdf_snapshot "$batch_pod_name" "${cell_dir}/eevdf-${batch_pod_name}.json" || true
                fi

                # ---- Perfetto trace setup (uses first pod for node resolution) ----
                local perfetto_node_ip=""
                local perfetto_trace_pid=""
                local perfetto_remote_path=""
                local perfetto_trace_file=""
                if [[ "$PERFETTO_ENABLED" == true ]]; then
                    perfetto_node_ip="$(get_pod_node_ip "$ls_pod_name" 2>/dev/null || true)"
                    if [[ -n "$perfetto_node_ip" ]] && ssh_node "$perfetto_node_ip" "test -x /usr/bin/tracebox" >/dev/null 2>&1; then
                        log "Starting Perfetto trace on node ${perfetto_node_ip} (config: ${PERFETTO_CONFIG})"
                        local po
                        po="$("${SCRIPT_DIR}/../perfetto/bin/perfetto-start.sh" "$perfetto_node_ip" "$PERFETTO_CONFIG" --duration "$duration")"
                        perfetto_trace_pid="$(printf '%s\n' "$po" | awk '{print $1}')"
                        perfetto_remote_path="$(printf '%s\n' "$po" | awk '{$1=""; print $0}' | sed 's/^ //')"
                    else
                        log "WARNING: tracebox not available on node ${perfetto_node_ip:-unknown}, skipping Perfetto tracing"
                    fi
                fi

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

                # Collect EEVDF per-task time series for both (in background)
                if [[ "$EEVDF_ENABLED" == true ]]; then
                    collect_eevdf_pids "$ls_pod_name" "$duration" "$cgroup_interval" "${cell_dir}/eevdf-${ls_pod_name}-pids.csv" &
                    bg_start
                    collect_eevdf_pids "$batch_pod_name" "$duration" "$cgroup_interval" "${cell_dir}/eevdf-${batch_pod_name}-pids.csv" &
                    bg_start
                fi

                # Wait for duration
                log "Measurement period: ${duration}s"
                run_cmd sleep "$duration"

                # Wait for latency generation to finish and report the CSV
                wait_latency_generation "$latency_pid" "$cell_dir"

                # Stop background processes
                log "Stopping data collection..."
                bg_stop_all

                # ---- Stop perfetto trace ----
                if [[ "$PERFETTO_ENABLED" == true && -n "${perfetto_trace_pid:-}" ]]; then
                    log "Stopping Perfetto trace on node ${perfetto_node_ip}"
                    local trace_dl
                    trace_dl="$("${SCRIPT_DIR}/../perfetto/bin/perfetto-stop.sh" "$perfetto_node_ip" "$perfetto_trace_pid" \
                        --output-dir "$cell_dir" --remote-path "$perfetto_remote_path")" || {
                        log "WARNING: Failed to stop/download Perfetto trace"
                        trace_dl=""
                    }
                    if [[ -n "$trace_dl" ]]; then
                        perfetto_trace_file="$trace_dl"
                        log "Perfetto trace saved: ${perfetto_trace_file}"
                    fi
                fi

                # Save metadata
                save_cell_metadata "$base_data_dir" "$cell_label" "$rep" "$ls_pod_name"

                # ---- Add perfetto metadata to existing metadata.json ----
                if [[ "$PERFETTO_ENABLED" == true && -n "${perfetto_trace_file:-}" ]]; then
                    local mdf="${cell_dir}/metadata.json"
                    python3 -c "
import json
with open('${mdf}', 'r') as f:
    m = json.load(f)
m['perfetto_trace_file'] = '$(basename "$perfetto_trace_file" 2>/dev/null || echo "")'
m['perfetto_config'] = '${PERFETTO_CONFIG}'
with open('${mdf}', 'w') as f:
    json.dump(m, f, indent=2)
" 2>/dev/null || log "WARNING: Failed to add perfetto metadata to ${mdf}"
                fi

                # ---- Add EEVDF metadata to existing metadata.json ----
                if [[ "$EEVDF_ENABLED" == true ]]; then
                    add_eevdf_metadata "${cell_dir}/metadata.json" "$cell_dir" "$ls_pod_name" "$batch_pod_name"
                fi

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

            elif [[ "$is_colocated" == true ]]; then
                # ---- Generic N-pod co-located experiment ----
                local -a pod_manifests=()
                local -a pod_names=()
                local i
                for ((i = 0; i < ${#workload_pods[@]}; i++)); do
                    local wpod="${workload_pods[$i]}"
                    local wtype="${workload_types[$i]}"
                    local wreq_key="${wpod#pod-}_request"
                    local wlim_key="${wpod#pod-}_limit"
                    if [[ -n "${CELL_PARAMS[${wpod}_request]+x}" ]]; then
                        wreq_key="${wpod}_request"
                        wlim_key="${wpod}_limit"
                    fi
                    local wpod_request="${CELL_PARAMS[$wreq_key]:-}"
                    local wpod_limit="${CELL_PARAMS[$wlim_key]:-}"

                    local wtemplate
                    wtemplate="$(get_workload_template "$wtype")"
                    local wmanifest="${cell_dir}/${wpod}.yaml"
                    substitute_pod_manifest "$wtemplate" "$wpod" "$wpod_request" "$wpod_limit" "$wmanifest"
                    pod_manifests+=("$wmanifest")
                    pod_names+=("$wpod")
                    log "Pod '${wpod}' (${wtype}): req=${wpod_request} lim=${wpod_limit} manifest=${wmanifest}"
                done

                # Deploy all pods
                for wm in "${pod_manifests[@]}"; do
                    kubectl --kubeconfig "$KUBECONFIG" delete -f "$wm" --ignore-not-found --now 2>/dev/null || true
                    kubectl --kubeconfig "$KUBECONFIG" apply -f "$wm" >/dev/null
                done

                # Wait for every pod to reach Running
                for wpod in "${pod_names[@]}"; do
                    wait_for_pod_running "$wpod" || die "Pod '${wpod}' did not start"
                done

                # ---- Latency load generation (top-level latency_load → resolved HTTP-capable target) ----
                local latency_pid=""
                if [[ -n "$colocated_latency_load" && "$DRY_RUN" == false ]]; then
                    local -a latency_args=()
                    for i in "${!workload_pods[@]}"; do
                        latency_args+=("${workload_pods[$i]}:${workload_types[$i]}")
                    done
                    # Resolve the pod that should receive the
                    # latency load — api-server/cpu-burner/latency-sensitive (in
                    # that order) or the first HTTP-capable pod. Family D uses an
                    # api-server LS pod, which the old latency-sensitive-only
                    # lookup skipped.
                    local latency_target_pod=""
                    latency_target_pod="$(resolve_latency_load_target "${latency_args[@]}")" || latency_target_pod=""
                    if [[ -n "$latency_target_pod" ]]; then
                        log "Starting latency load generation against ${latency_target_pod} (rate: ${colocated_latency_rate} req/s, duration: ${colocated_latency_duration}s, endpoints: ${colocated_latency_endpoints})"
                        start_latency_load_generation "$latency_target_pod" \
                            "$colocated_latency_rate" \
                            "$colocated_latency_duration" \
                            "$colocated_latency_endpoints" \
                            "${cell_dir}/latency.csv" &
                        latency_pid=$!
                    else
                        log "WARNING: latency_load declared but no HTTP-capable pod found in workloads: — skipping latency load generation (non-fatal)"
                    fi
                fi

                # ---- EEVDF snapshots (start of measurement window) ----
                if [[ "$EEVDF_ENABLED" == true ]]; then
                    for wpod in "${pod_names[@]}"; do
                        collect_eevdf_snapshot "$wpod" "${cell_dir}/eevdf-${wpod}.json" || true
                    done
                fi

                # ---- Perfetto trace setup (uses first pod for node resolution) ----
                local perfetto_node_ip=""
                local perfetto_trace_pid=""
                local perfetto_remote_path=""
                local perfetto_trace_file=""
                if [[ "$PERFETTO_ENABLED" == true ]]; then
                    perfetto_node_ip="$(get_pod_node_ip "${pod_names[0]}" 2>/dev/null || true)"
                    if [[ -n "$perfetto_node_ip" ]] && ssh_node "$perfetto_node_ip" "test -x /usr/bin/tracebox" >/dev/null 2>&1; then
                        log "Starting Perfetto trace on node ${perfetto_node_ip} (config: ${PERFETTO_CONFIG})"
                        local po
                        po="$("${SCRIPT_DIR}/../perfetto/bin/perfetto-start.sh" "$perfetto_node_ip" "$PERFETTO_CONFIG" --duration "$duration")"
                        perfetto_trace_pid="$(printf '%s\n' "$po" | awk '{print $1}')"
                        perfetto_remote_path="$(printf '%s\n' "$po" | awk '{$1=""; print $0}' | sed 's/^ //')"
                    else
                        log "WARNING: tracebox not available on node ${perfetto_node_ip:-unknown}, skipping Perfetto tracing"
                    fi
                fi

                # Pre-warm
                log "Pre-warm period: ${pre_warm}s"
                run_cmd sleep "$pre_warm"

                # Collect cgroup + kubectl top data per pod (in background)
                for wpod in "${pod_names[@]}"; do
                    collect_cgroup_data "$wpod" "$duration" "$cgroup_interval" "${cell_dir}/cgroup-${wpod}.csv" &
                    bg_start
                    collect_kubectl_top "$wpod" "$duration" "$cgroup_interval" "${cell_dir}/kubectl-top-${wpod}.csv" &
                    bg_start
                done

                # Collect EEVDF per-task time series per pod (in background)
                if [[ "$EEVDF_ENABLED" == true ]]; then
                    for wpod in "${pod_names[@]}"; do
                        collect_eevdf_pids "$wpod" "$duration" "$cgroup_interval" "${cell_dir}/eevdf-${wpod}-pids.csv" &
                        bg_start
                    done
                fi

                # Wait for duration
                log "Measurement period: ${duration}s"
                run_cmd sleep "$duration"

                # Wait for latency generation to finish and report the CSV
                wait_latency_generation "$latency_pid" "$cell_dir"

                # Stop background processes
                log "Stopping data collection..."
                bg_stop_all

                # ---- Stop perfetto trace ----
                if [[ "$PERFETTO_ENABLED" == true && -n "${perfetto_trace_pid:-}" ]]; then
                    log "Stopping Perfetto trace on node ${perfetto_node_ip}"
                    local trace_dl
                    trace_dl="$("${SCRIPT_DIR}/../perfetto/bin/perfetto-stop.sh" "$perfetto_node_ip" "$perfetto_trace_pid" \
                        --output-dir "$cell_dir" --remote-path "$perfetto_remote_path")" || {
                        log "WARNING: Failed to stop/download Perfetto trace"
                        trace_dl=""
                    }
                    if [[ -n "$trace_dl" ]]; then
                        perfetto_trace_file="$trace_dl"
                        log "Perfetto trace saved: ${perfetto_trace_file}"
                    fi
                fi

                # Save metadata per pod (distinct metadata-<pod>.json files)
                for wpod in "${pod_names[@]}"; do
                    save_cell_metadata "$base_data_dir" "$cell_label" "$rep" "$wpod" "-${wpod}"
                done

                # ---- Add perfetto metadata to the first pod's metadata.json ----
                if [[ "$PERFETTO_ENABLED" == true && -n "${perfetto_trace_file:-}" ]]; then
                    local mdf="${cell_dir}/metadata-${pod_names[0]}.json"
                    python3 -c "
import json
with open('${mdf}', 'r') as f:
    m = json.load(f)
m['perfetto_trace_file'] = '$(basename "$perfetto_trace_file" 2>/dev/null || echo "")'
m['perfetto_config'] = '${PERFETTO_CONFIG}'
with open('${mdf}', 'w') as f:
    json.dump(m, f, indent=2)
" 2>/dev/null || log "WARNING: Failed to add perfetto metadata to ${mdf}"
                fi

                # ---- Add EEVDF metadata to each pod's metadata file ----
                if [[ "$EEVDF_ENABLED" == true ]]; then
                    for wpod in "${pod_names[@]}"; do
                        add_eevdf_metadata "${cell_dir}/metadata-${wpod}.json" "$cell_dir" "$wpod"
                    done
                fi

                # One summary row per pod
                # (CSV: timestamp,pod,container,nr_periods,nr_throttled,throttled_usec,usage_usec,cpu_weight,cpu_max_quota,cpu_max_period)
                for wpod in "${pod_names[@]}"; do
                    local wlast_stats=""
                    wlast_stats="$(tail -1 "${cell_dir}/cgroup-${wpod}.csv" 2>/dev/null || true)"
                    if [[ -n "$wlast_stats" ]]; then
                        local wnr_periods wnr_throttled wthrottled_usec wusage_usec wcpu_weight wcpu_max
                        wnr_periods="$(printf '%s' "$wlast_stats" | cut -d',' -f4)"
                        wnr_throttled="$(printf '%s' "$wlast_stats" | cut -d',' -f5)"
                        wthrottled_usec="$(printf '%s' "$wlast_stats" | cut -d',' -f6)"
                        wusage_usec="$(printf '%s' "$wlast_stats" | cut -d',' -f7)"
                        wcpu_weight="$(printf '%s' "$wlast_stats" | cut -d',' -f8)"
                        wcpu_max="$(printf '%s' "$wlast_stats" | cut -d',' -f9)"
                        printf '%s-%s,%d,%s,%s,%s,%s,%s,%s\n' \
                            "$wpod" "$cell_label" "$rep" \
                            "$wnr_periods" "$wnr_throttled" "$wthrottled_usec" \
                            "$wusage_usec" "$wcpu_weight" "$wcpu_max" \
                            >> "$summary_file"
                    fi
                done

                # Delete all pods
                for wpod in "${pod_names[@]}"; do
                    delete_pod "$wpod"
                done

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

                # Start HTTP load generation for endpoint-based workloads
                local load_pid=""
                if [[ -n "$single_workload_params_endpoint" && "$DRY_RUN" == false ]]; then
                    load_pid="$(start_load_generation "$pod_name" "$single_workload_params_endpoint" "$duration" 2>/dev/null || true)"
                    if [[ -n "$load_pid" ]]; then
                        _BG_PIDS+=("$load_pid")
                        log "Load generator started (PID ${load_pid})"
                    fi
                fi

                # Start latency-recording load generation
                # (workload.params.latency_load). Runs in background; the CSV is
                # checked after the measurement window (non-fatal).
                local latency_pid=""
                if [[ -n "$single_workload_latency_load" && "$DRY_RUN" == false ]]; then
                    log "Starting latency load generation (rate: ${single_workload_latency_rate} req/s, duration: ${single_workload_latency_duration}s, endpoints: ${single_workload_latency_endpoints})"
                    start_latency_load_generation "$pod_name" \
                        "$single_workload_latency_rate" \
                        "$single_workload_latency_duration" \
                        "$single_workload_latency_endpoints" \
                        "${cell_dir}/latency.csv" &
                    latency_pid=$!
                fi

                # ---- EEVDF snapshot (start of measurement window) ----
                if [[ "$EEVDF_ENABLED" == true ]]; then
                    collect_eevdf_snapshot "$pod_name" "${cell_dir}/eevdf-${pod_name}.json" || true
                fi

                # ---- Perfetto trace setup ----
                local perfetto_node_ip=""
                local perfetto_trace_pid=""
                local perfetto_remote_path=""
                local perfetto_trace_file=""
                if [[ "$PERFETTO_ENABLED" == true ]]; then
                    perfetto_node_ip="$(get_pod_node_ip "$pod_name" 2>/dev/null || true)"
                    if [[ -n "$perfetto_node_ip" ]] && ssh_node "$perfetto_node_ip" "test -x /usr/bin/tracebox" >/dev/null 2>&1; then
                        log "Starting Perfetto trace on node ${perfetto_node_ip} (config: ${PERFETTO_CONFIG})"
                        local po
                        po="$("${SCRIPT_DIR}/../perfetto/bin/perfetto-start.sh" "$perfetto_node_ip" "$PERFETTO_CONFIG" --duration "$duration")"
                        perfetto_trace_pid="$(printf '%s\n' "$po" | awk '{print $1}')"
                        perfetto_remote_path="$(printf '%s\n' "$po" | awk '{$1=""; print $0}' | sed 's/^ //')"
                    else
                        log "WARNING: tracebox not available on node ${perfetto_node_ip:-unknown}, skipping Perfetto tracing"
                    fi
                fi

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

                # Start EEVDF per-task time series in background
                if [[ "$EEVDF_ENABLED" == true ]]; then
                    run_cmd collect_eevdf_pids "$pod_name" "$duration" "$cgroup_interval" "${cell_dir}/eevdf-${pod_name}-pids.csv" &
                    bg_start
                fi

                # Wait for measurement duration
                log "Measurement period: ${duration}s"
                run_cmd sleep "$duration"

                # Wait for latency generation to finish and report the CSV
                wait_latency_generation "$latency_pid" "$cell_dir"

                # Stop background processes
                log "Stopping data collection..."
                bg_stop_all

                # ---- Stop perfetto trace ----
                if [[ "$PERFETTO_ENABLED" == true && -n "${perfetto_trace_pid:-}" ]]; then
                    log "Stopping Perfetto trace on node ${perfetto_node_ip}"
                    local trace_dl
                    trace_dl="$("${SCRIPT_DIR}/../perfetto/bin/perfetto-stop.sh" "$perfetto_node_ip" "$perfetto_trace_pid" \
                        --output-dir "$cell_dir" --remote-path "$perfetto_remote_path")" || {
                        log "WARNING: Failed to stop/download Perfetto trace"
                        trace_dl=""
                    }
                    if [[ -n "$trace_dl" ]]; then
                        perfetto_trace_file="$trace_dl"
                        log "Perfetto trace saved: ${perfetto_trace_file}"
                    fi
                fi

                # Save metadata
                save_cell_metadata "$base_data_dir" "$cell_label" "$rep" "$pod_name"

                # ---- Add perfetto metadata to existing metadata.json ----
                if [[ "$PERFETTO_ENABLED" == true && -n "${perfetto_trace_file:-}" ]]; then
                    local mdf="${cell_dir}/metadata.json"
                    python3 -c "
import json
with open('${mdf}', 'r') as f:
    m = json.load(f)
m['perfetto_trace_file'] = '$(basename "$perfetto_trace_file" 2>/dev/null || echo "")'
m['perfetto_config'] = '${PERFETTO_CONFIG}'
with open('${mdf}', 'w') as f:
    json.dump(m, f, indent=2)
" 2>/dev/null || log "WARNING: Failed to add perfetto metadata to ${mdf}"
                fi

                # ---- Add EEVDF metadata to existing metadata.json ----
                if [[ "$EEVDF_ENABLED" == true ]]; then
                    add_eevdf_metadata "${cell_dir}/metadata.json" "$cell_dir" "$pod_name"
                fi

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
