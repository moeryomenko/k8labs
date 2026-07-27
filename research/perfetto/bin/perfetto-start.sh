#!/usr/bin/env bash
#
# perfetto-start.sh — Start a Perfetto trace on a remote node
#
# Usage:
#   perfetto-start.sh <node-ip> <config-name> [--duration N] [--output name]
#   perfetto-start.sh --help
#
# Output: <trace-pid> <remote-trace-path>
#   (space-separated, for parsing by perfetto-stop.sh)

set -Eeuo pipefail

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# shellcheck source=./perfetto-common.sh
source "$SCRIPT_DIR/perfetto-common.sh"

SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

# ---- Defaults ----
DEFAULT_DURATION=60

# ---- State ----
NODE_IP=""
CONFIG_NAME=""
DURATION="$DEFAULT_DURATION"
OUTPUT_NAME=""

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <node-ip> <config-name> [OPTIONS]

Start a Perfetto trace on a remote node using tracebox.

Arguments:
  node-ip           IP address of the target VM
  config-name       Name of the trace config (without path/extension)

Options:
  --duration N      Trace duration in seconds (default: $DEFAULT_DURATION)
  --output name     Output filename on the node (default: <config-name>-<timestamp>)
  -h, --help        Show this help and exit
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    # Parse --help before anything else
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
            --output)
                OUTPUT_NAME="${2:?--output requires a value}"
                shift 2
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

    # Validate required arguments
    if [[ -z "$NODE_IP" ]]; then
        printf 'ERROR: node-ip is required\n' >&2
        usage 1
    fi
    if [[ -z "$CONFIG_NAME" ]]; then
        printf 'ERROR: config-name is required\n' >&2
        usage 1
    fi

    # Validate duration
    if ! [[ "$DURATION" =~ ^[0-9]+$ ]] || [[ "$DURATION" -lt 1 ]]; then
        printf 'ERROR: --duration must be a positive integer (got: %s)\n' "$DURATION" >&2
        exit 2
    fi

    # Generate output name if not provided
    if [[ -z "$OUTPUT_NAME" ]]; then
        OUTPUT_NAME="${CONFIG_NAME}-$(date -u +'%Y%m%dT%H%M%SZ')"
    fi

    # Resolve config path on node
    local config_path
    config_path="$(perfetto_config_path "$CONFIG_NAME")" || exit 1

    # Build remote trace path
    local remote_trace_path="/tmp/${OUTPUT_NAME}.perfetto-trace"

    # Upload config to node
    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        printf '[DRY-RUN] Upload config %s to %s\n' "$config_path" "$NODE_IP"
        printf '[DRY-RUN] ssh root@%s "nohup /usr/bin/tracebox -o %s --txt -c %s >/dev/null 2>&1 &"\n' \
            "$NODE_IP" "$remote_trace_path" "$config_path"
        printf '[DRY-RUN] Would print: <pid> %s\n' "$remote_trace_path"
        return 0
    fi

    upload_config "$NODE_IP" "$CONFIG_NAME" || exit 1

    # Start tracebox in background on the node, capture PID
    local pid
    pid="$(ssh_node "$NODE_IP" "nohup /usr/bin/tracebox -o '${remote_trace_path}' --txt -c '${config_path}' >/dev/null 2>&1 & echo \$!")" || {
        log_error "Failed to start trace on $NODE_IP"
        exit 1
    }

    # Output: <pid> <remote-trace-path> (space-separated for stop script)
    printf '%s %s\n' "$pid" "$remote_trace_path"
}

main "$@"
