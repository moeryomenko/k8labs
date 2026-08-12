#!/usr/bin/env bash
#
# perfetto-stop.sh — Stop a Perfetto trace and download the result
#
# Usage:
#   perfetto-stop.sh <node-ip> <trace-pid> [--output-dir path] [--remote-path path]
#   perfetto-stop.sh --help
#
# Output: local trace file path (on stdout)

set -Eeuo pipefail

# ---- Script directory (symlink-safe) ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# shellcheck source=./perfetto-common.sh
source "$SCRIPT_DIR/perfetto-common.sh"

SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

# ---- State ----
NODE_IP=""
TRACE_PID=""
OUTPUT_DIR=""
REMOTE_PATH=""

# ---------------------------------------------------------------------------
# usage — Print usage and exit
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <node-ip> <trace-pid> [OPTIONS]

Stop a Perfetto trace and download the result from a remote node.

Arguments:
  node-ip           IP address of the target VM
  trace-pid         PID of the tracebox process on the node (from perfetto-start.sh)

Options:
  --output-dir DIR    Local directory to save the trace (default: current directory)
  --remote-path PATH  Path to trace file on the node (default: reconstructed from /proc)
  -h, --help          Show this help and exit
EOF
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# _reconstruct_remote_path — Try to find the trace file path from /proc/PID/cmdline
# ---------------------------------------------------------------------------
_reconstruct_remote_path() {
    local node_ip="$1"
    local trace_pid="$2"

    local cmdline
    cmdline="$(ssh_node "$node_ip" "cat /proc/${trace_pid}/cmdline 2>/dev/null | tr '\0' ' '" 2>/dev/null)" || return 1

    # Extract the -o argument value (trace output path)
    local path
    path="$(printf '%s\n' "$cmdline" | grep -oP '/tmp/\S+\.perfetto-trace' 2>/dev/null || true)"
    if [[ -z "$path" ]]; then
        return 1
    fi
    printf '%s\n' "$path"
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
            --output-dir)
                OUTPUT_DIR="${2:?--output-dir requires a value}"
                shift 2
                ;;
            --remote-path)
                REMOTE_PATH="${2:?--remote-path requires a value}"
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
                elif [[ -z "$TRACE_PID" ]]; then
                    TRACE_PID="$1"
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
    if [[ -z "$TRACE_PID" ]]; then
        printf 'ERROR: trace-pid is required\n' >&2
        usage 1
    fi

    # Default output dir
    OUTPUT_DIR="${OUTPUT_DIR:-.}"

    # Reconstruct remote path from /proc if not provided
    if [[ -z "$REMOTE_PATH" ]]; then
        if [[ "${DRY_RUN:-false}" == "true" ]]; then
            printf '[DRY-RUN] Reconstruct remote path from /proc/%s/cmdline\n' "$TRACE_PID"
            REMOTE_PATH="/tmp/trace-${TRACE_PID}.perfetto-trace"
        else
            REMOTE_PATH="$(_reconstruct_remote_path "$NODE_IP" "$TRACE_PID")" || {
                log_error "Could not determine remote trace path for PID $TRACE_PID"
                log_error "Provide --remote-path to specify it manually"
                exit 1
            }
        fi
    fi

    # Ensure output directory exists
    if [[ ! -d "$OUTPUT_DIR" ]]; then
        if [[ "${DRY_RUN:-false}" == "true" ]]; then
            printf '[DRY-RUN] mkdir -p %s\n' "$OUTPUT_DIR"
        else
            mkdir -p "$OUTPUT_DIR"
        fi
    fi

    # Local path for downloaded trace
    local trace_name
    trace_name="$(basename "$REMOTE_PATH")"
    local local_path="${OUTPUT_DIR}/${trace_name}"

    # Dry-run: just print what would happen
    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        printf '[DRY-RUN] ssh root@%s "kill -TERM %s"\n' "$NODE_IP" "$TRACE_PID"
        printf '[DRY-RUN] sleep 3\n'
        printf '[DRY-RUN] scp root@%s:%s %s\n' "$NODE_IP" "$REMOTE_PATH" "$local_path"
        printf '[DRY-RUN] ssh root@%s "rm -f %s"\n' "$NODE_IP" "$REMOTE_PATH"
        printf '[DRY-RUN] Local trace: %s\n' "$local_path"
        printf '%s\n' "$local_path"
        return 0
    fi

    # Send SIGTERM to tracebox process on the node
    ssh_node "$NODE_IP" "kill -TERM $TRACE_PID" 2>/dev/null || true

    # Wait a few seconds for trace file to be fully written
    sleep 3

    # SCP the trace file from node to local output directory
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=5 -o BatchMode=yes \
        "root@${NODE_IP}:${REMOTE_PATH}" "$local_path" >/dev/null 2>&1 || {
        log_error "Failed to download trace from ${NODE_IP}:${REMOTE_PATH}"
        exit 1
    }

    # Remove the trace file on the node
    ssh_node "$NODE_IP" "rm -f '${REMOTE_PATH}'" >/dev/null 2>&1 || true

    # Print local trace file path on stdout
    printf '%s\n' "$local_path"
}

main "$@"
