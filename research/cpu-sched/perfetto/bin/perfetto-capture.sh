#!/usr/bin/env bash
#
# perfetto-capture.sh — Full Perfetto trace lifecycle: start -> wait -> stop -> download
#
# Usage:
#   perfetto-capture.sh <node-ip> <config-name> --duration N [OPTIONS]
#   perfetto-capture.sh --help

set -Eeuo pipefail

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# shellcheck source=./perfetto-common.sh
source "$SCRIPT_DIR/perfetto-common.sh"

SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

# ---- Defaults ----
DEFAULT_OUTPUT_DIR="./perfetto-traces"

# ---- State ----
NODE_IP=""
CONFIG_NAME=""
DURATION=""
OUTPUT_DIR=""
DRY_RUN_MODE=false

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <node-ip> <config-name> --duration N [OPTIONS]

Full Perfetto trace lifecycle orchestration: upload config, start trace
on the node, wait for the specified duration, then stop and download.

Arguments:
  node-ip           IP address of the target VM
  config-name       Name of the trace config (without path/extension)

Options:
  --duration N      Trace duration in seconds (REQUIRED)
  --output-dir DIR  Local output directory (default: $DEFAULT_OUTPUT_DIR)
  --dry-run         Print what would happen without executing
  -h, --help        Show this help and exit
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# _validate_duration — Check that duration is a positive integer
# ---------------------------------------------------------------------------
_validate_duration() {
    local val="$1"
    if ! [[ "$val" =~ ^[0-9]+$ ]]; then
        printf 'ERROR: --duration must be a positive integer (got: %s)\n' "$val" >&2
        exit 2
    fi
    if [[ "$val" -lt 1 ]]; then
        printf 'ERROR: --duration must be a positive integer (got: %s)\n' "$val" >&2
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# _dry_run_plan — Print the full plan in dry-run mode
# ---------------------------------------------------------------------------
_dry_run_plan() {
    local node_ip="$1"
    local config_name="$2"
    local duration="$3"
    local output_dir="$4"

    local config_path
    config_path="$(perfetto_config_path "$config_name")" || exit 1
    local config_basename
    config_basename="$(basename "$config_path")"
    local local_config="${SCRIPT_DIR}/../configs/${config_basename}"

    printf '[DRY-RUN] Perfetto Capture Plan\n'
    printf '[DRY-RUN]   Node:       %s\n' "$node_ip"
    printf '[DRY-RUN]   Config:     %s\n' "$config_path"
    printf '[DRY-RUN]   Duration:   %s seconds\n' "$duration"
    printf '[DRY-RUN]   Output dir: %s\n' "$output_dir"
    printf '[DRY-RUN]\n'
    printf '[DRY-RUN] Step 1: Upload config to node\n'
    printf '[DRY-RUN]   scp %s root@%s:%s\n' "$local_config" "$node_ip" "$config_path"
    printf '[DRY-RUN]\n'
    printf '[DRY-RUN] Step 2: Start trace on node\n'
    printf '[DRY-RUN]   ssh root@%s tracebox --txt -c %s ... &\n' "$node_ip" "$config_path"
    printf '[DRY-RUN]\n'
    printf '[DRY-RUN] Step 3: Wait %s seconds\n' "$duration"
    printf '[DRY-RUN]\n'
    printf '[DRY-RUN] Step 4: Stop trace and download\n'
    printf '[DRY-RUN]   ssh root@%s kill -TERM <pid>\n' "$node_ip"
    printf '[DRY-RUN]   scp root@%s:<remote-path> %s/\n' "$node_ip" "$output_dir"
    printf '[DRY-RUN]\n'
    printf '[DRY-RUN] Step 5: Clean up remote artifacts\n'
    printf '[DRY-RUN]   ssh root@%s rm -f <remote-path>\n' "$node_ip"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    if [[ $# -eq 0 ]]; then
        usage 1
    fi
    if [[ "$1" == "-h" || "$1" == "--help" ]]; then
        usage 0
    fi

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --duration)
                DURATION="${2:?--duration requires a value}"
                shift 2
                ;;
            --output-dir)
                OUTPUT_DIR="${2:?--output-dir requires a value}"
                shift 2
                ;;
            --dry-run)
                DRY_RUN_MODE=true
                shift
                ;;
            -h|--help)
                usage 0
                ;;
            -*)
                printf 'ERROR: Unknown option: %s\n' "$1" >&2
                usage 1
                ;;
            *)
                if [[ -z "$NODE_IP" ]]; then
                    NODE_IP="$1"
                elif [[ -z "$CONFIG_NAME" ]]; then
                    CONFIG_NAME="$1"
                else
                    printf 'ERROR: Unexpected argument: %s\n' "$1" >&2
                    usage 1
                fi
                shift
                ;;
        esac
    done

    # Validate required positional arguments
    if [[ -z "$NODE_IP" ]]; then
        printf 'ERROR: node-ip is required\n' >&2
        usage 1
    fi
    if [[ -z "$CONFIG_NAME" ]]; then
        printf 'ERROR: config-name is required\n' >&2
        usage 1
    fi

    # --duration is REQUIRED unless --dry-run is set
    if [[ -z "$DURATION" ]]; then
        if [[ "$DRY_RUN_MODE" == "true" ]]; then
            DURATION="<duration>"
        else
            printf 'ERROR: --duration is required\n' >&2
            exit 1
        fi
    fi

    # Validate duration (skip validation in dry-run with placeholder)
    if [[ "$DRY_RUN_MODE" == "true" && "$DURATION" == "<duration>" ]]; then
        : # placeholder, skip validation
    else
        _validate_duration "$DURATION"
    fi

    # Default output dir
    OUTPUT_DIR="${OUTPUT_DIR:-$DEFAULT_OUTPUT_DIR}"

    # ---- Dry-run mode ----
    if [[ "$DRY_RUN_MODE" == "true" ]]; then
        _dry_run_plan "$NODE_IP" "$CONFIG_NAME" "$DURATION" "$OUTPUT_DIR"
        exit 0
    fi

    # ---- Actual execution ----

    # Resolve config path
    local config_path
    config_path="$(perfetto_config_path "$CONFIG_NAME")" || exit 1

    # Step 1: Upload config
    printf 'Uploading config %s...\n' "$config_path" >&2
    upload_config "$NODE_IP" "$CONFIG_NAME" || exit 1

    # Step 2: Start trace
    printf 'Starting trace on %s...\n' "$NODE_IP" >&2
    local start_output
    start_output="$("$SCRIPT_DIR/perfetto-start.sh" "$NODE_IP" "$CONFIG_NAME" --duration "$DURATION")" || {
        log_error "Failed to start trace on $NODE_IP"
        exit 1
    }
    local trace_pid remote_trace_path
    trace_pid="$(printf '%s\n' "$start_output" | awk '{print $1}')"
    remote_trace_path="$(printf '%s\n' "$start_output" | awk '{$1=""; print $0}' | sed 's/^ //')"

    printf 'Trace started: PID=%s Path=%s\n' "$trace_pid" "$remote_trace_path" >&2

    # Step 3: Wait for specified duration
    printf 'Waiting %s seconds...\n' "$DURATION" >&2
    sleep "$DURATION"

    # Step 4: Stop trace and download
    printf 'Stopping trace and downloading...\n' >&2
    mkdir -p "$OUTPUT_DIR" || true
    local local_path
    local_path="$("$SCRIPT_DIR/perfetto-stop.sh" "$NODE_IP" "$trace_pid" \
        --output-dir "$OUTPUT_DIR" --remote-path "$remote_trace_path")" || {
        log_error "Failed to stop and download trace"
        exit 1
    }

    # Step 5: Clean up remote artifacts is handled by perfetto-stop.sh

    printf 'Trace saved to: %s\n' "$local_path"
}

main "$@"
