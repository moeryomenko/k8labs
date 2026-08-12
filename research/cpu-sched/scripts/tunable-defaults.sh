#!/usr/bin/env bash
#
# tunable-defaults.sh — Capture/restore kernel scheduler tunables on worker nodes
#
# Usage:
#   tunable-defaults.sh capture [--file path] [--dry-run]
#   tunable-defaults.sh restore [--file path] [--dry-run]
#   tunable-defaults.sh list
#
# Commands:
#   capture    Collect all scheduler tunables (/sys/kernel/debug/sched/) from worker nodes and save
#              as structured JSON with per-hostname entries.
#   restore    Restore tunables on all workers from a previously captured
#              baseline JSON file.
#   list       Print discovered scheduler tunables (/sys/kernel/debug/sched/) on the local machine.
#
# Options:
#   --file path   Path to baseline JSON file (default: research/cpu-sched/data/tunable-baseline.json)
#   --dry-run     Print what would be done without making changes
#   -h, --help    Show this help and exit
#
# Safety:
#   - Restore verifies readback on every tunable
#   - SSH connections use 10-second timeout
#   - Dry-run mode previews all changes
#   - Captured data includes timestamp for audit

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

# ---- Constants ----
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

DEFAULT_DATA_DIR="${PROJECT_ROOT}/data"
DEFAULT_BASELINE_FILE="${DEFAULT_DATA_DIR}/tunable-baseline.json"

# Source the shared DHCP lease helper for worker IP discovery
# shellcheck source=../bin/lease-common.sh
source "${SCRIPT_DIR}/../bin/lease-common.sh"

# Known scheduler tunables we always capture explicitly
# These map to files under /sys/kernel/debug/sched/ on Fedora 44+
KNOWN_TUNABLES=(
    base_slice_ns
    min_granularity_ns
    latency_ns
    migration_cost_ns
    wakeup_granularity_ns
    nr_migrate
)

# ---- Logging ----
log_info()  { printf '[%s] INFO:  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
log_warn()  { printf '[%s] WARN:  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
log_error() { printf '[%s] ERROR: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }

# ---- Global State ----
DRY_RUN=false
BASELINE_FILE="$DEFAULT_BASELINE_FILE"
COMMAND=""

# ---- Cleanup ----
_cleanup() {
    local exit_code=$?
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
Usage: $SCRIPT_NAME {capture|restore|list} [OPTIONS]

Capture, restore, or list kernel scheduler tunables on worker nodes (via /sys/kernel/debug/sched/).

Commands:
  capture    Collect scheduler tunables (via /sys/kernel/debug/sched/) from all workers and save as JSON
  restore    Restore tunables on all workers from a baseline JSON file
  list       Print discovered sched tunables on the local machine (/sys/kernel/debug/sched/)

Options:
  --file path   Path for baseline JSON (default: $DEFAULT_BASELINE_FILE)
  --dry-run     Print what would be done without making changes
  -h, --help    Show this help and exit

Examples:
  $SCRIPT_NAME capture
  $SCRIPT_NAME capture --file /tmp/baseline.json
  $SCRIPT_NAME restore
  $SCRIPT_NAME restore --dry-run
  $SCRIPT_NAME list
EOF
    exit "${1:-0}"
}

# ---- Dependency Checking ----
check_deps() {
    local -a missing=()
    for cmd in ssh tofu terraform; do
        if ! command -v "$cmd" &>/dev/null; then
            if [[ "$cmd" == "tofu" || "$cmd" == "terraform" ]]; then
                continue
            fi
            missing+=("$cmd")
        fi
    done
    if ! command -v tofu &>/dev/null && ! command -v terraform &>/dev/null; then
        missing+=("tofu or terraform")
    fi
    if ! command -v jq &>/dev/null; then
        missing+=("jq")
    fi
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Required tools not found: ${missing[*]}"
        return 1
    fi
}

# ---- Project Root Resolution ----
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
    fi
}

# ---- Worker IP Discovery ----
# get_worker_ips comes from lease-common.sh (sourced above): it resolves
# worker IPs from the systemd-networkd DHCP server lease (authoritative) with
# a dnsmasq fallback, ordered by WORKER_MACS.

# Worker MACs for node resolution (must match terraform.tfvars)
: "${WORKER_MACS:=c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03}"

# ---- SSH Helper ----
ssh_node() {
    local ip="$1"
    shift
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=10 -o BatchMode=yes \
        "root@$ip" "$@"
}

# ---- Capture per-worker tunables ----
# Returns JSON with tunable key-value pairs for a single worker
capture_worker_tunables() {
    local ip="$1"

    # Get hostname
    local hostname
    hostname="$(ssh_node "$ip" "hostname -s 2>/dev/null")" || hostname="$ip"

    # Collect known tunables explicitly via debugfs
    local -A tunables=()
    local tunable
    for tunable in "${KNOWN_TUNABLES[@]}"; do
        local value
        value="$(ssh_node "$ip" "cat /sys/kernel/debug/sched/$tunable 2>/dev/null" || true)"
        if [[ -n "$value" ]]; then
            tunables["$tunable"]="$value"
        fi
    done

    # Also discover any other sched tunables from debugfs
    local extra_tunables
    extra_tunables="$(ssh_node "$ip" "for f in /sys/kernel/debug/sched/*; do [ -f \"\$f\" ] && echo \"\$(basename \$f)=\$(cat \$f)\"; done 2>/dev/null || true")"

    local line
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local key="${line%%=*}"
        local val="${line#*=}"
        if [[ -n "$key" && -n "$val" && -z "${tunables[$key]:-}" ]]; then
            tunables["$key"]="$val"
        fi
    done <<< "$extra_tunables"

    # Build JSON
    local json="{"
    local first=true
    for key in "${!tunables[@]}"; do
        if [[ "$first" == true ]]; then
            first=false
        else
            json+=", "
        fi
        # Escape quotes in key/value for JSON
        local escaped_key="${key//\"/\\\"}"
        local escaped_val="${tunables[$key]//\"/\\\"}"
        json+="\"${escaped_key}\": \"${escaped_val}\""
    done
    json+="}"

    printf '%s\n' "$hostname"
    printf '%s\n' "$json"
}

# ---- Capture Command ----
cmd_capture() {
    log_info "Capturing scheduler tunables from all workers..."

    resolve_project_root

    local worker_ips_str
    worker_ips_str="$(get_worker_ips)" || return 1
    local -a worker_ips=()
    local ip
    for ip in $worker_ips_str; do
        worker_ips+=("$ip")
    done

    if [[ ${#worker_ips[@]} -eq 0 ]]; then
        log_error "No worker IPs found"
        return 1
    fi
    log_info "Found ${#worker_ips[@]} worker(s): ${worker_ips[*]}"

    # Build the JSON document
    local json="{"
    json+="\"timestamp\": \"$(date -u +'%Y-%m-%dT%H:%M:%SZ')\""
    json+=", \"captured_by\": \"${SCRIPT_NAME}\""
    json+=", \"workers\": {"

    local first_worker=true
    local ip2
    for ip2 in "${worker_ips[@]}"; do
        local output
        output="$(capture_worker_tunables "$ip2")" || {
            log_warn "Failed to capture tunables from $ip2, skipping"
            continue
        }

        # First line is hostname, rest is JSON
        local worker_hostname
        worker_hostname="$(printf '%s\n' "$output" | head -1)"
        local worker_json
        worker_json="$(printf '%s\n' "$output" | tail -n +2)"

        if [[ "$first_worker" == true ]]; then
            first_worker=false
        else
            json+=", "
        fi
        json+="\"${worker_hostname}\": ${worker_json}"

        log_info "Captured ${#KNOWN_TUNABLES}+ tunables from ${worker_hostname} ($ip2)"
    done

    json+="}}"  # close workers and root

    # Ensure data directory exists
    mkdir -p "$(dirname "$BASELINE_FILE")"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] Would write baseline to: $BASELINE_FILE"
        printf '%s\n' "$json" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$json"
        log_info "[DRY-RUN] Baseline capture complete (dry-run)"
        return 0
    fi

    # Write atomically: temp file then mv
    local tmpfile
    tmpfile="$(mktemp "$(dirname "$BASELINE_FILE")/tunable-baseline.XXXXXXXXXX.json")" || {
        log_error "Failed to create temp file for baseline"
        return 1
    }
    printf '%s\n' "$json" | python3 -m json.tool > "$tmpfile" 2>/dev/null \
        || printf '%s\n' "$json" > "$tmpfile"

    mv -- "$tmpfile" "$BASELINE_FILE"
    log_info "Baseline saved to: $BASELINE_FILE"
    log_info "Capture complete — ${#worker_ips[@]} worker(s) recorded"
}

# ---- Restore Command ----
cmd_restore() {
    log_info "Restoring scheduler tunables from baseline..."

    if [[ ! -f "$BASELINE_FILE" ]]; then
        log_error "Baseline file not found: $BASELINE_FILE"
        return 1
    fi

    resolve_project_root

    local worker_ips_str
    worker_ips_str="$(get_worker_ips)" || return 1
    local -a worker_ips=()
    local ip
    for ip in $worker_ips_str; do
        worker_ips+=("$ip")
    done

    if [[ ${#worker_ips[@]} -eq 0 ]]; then
        log_error "No worker IPs found"
        return 1
    fi

    local total_errors=0

    local ip2
    for ip2 in "${worker_ips[@]}"; do
        # Get the hostname for this worker
        local worker_hostname
        worker_hostname="$(ssh_node "$ip2" "hostname -s 2>/dev/null")" || worker_hostname="$ip2"

        log_info "Restoring tunables on ${worker_hostname} ($ip2)..."

        # Extract tunables for this hostname from JSON
        local worker_data
        worker_data="$(python3 -c "
import sys, json
with open('$BASELINE_FILE') as f:
    data = json.load(f)
workers = data.get('workers', data)
if '$worker_hostname' in workers:
    print(json.dumps(workers['$worker_hostname']))
elif '$ip2' in workers:
    print(json.dumps(workers['$ip2']))
else:
    print('{}')
")" || {
            log_warn "No tunable data found for ${worker_hostname} ($ip2) in baseline, skipping"
            continue
        }

        # Parse each tunable from the worker JSON
        local tunable_keys
        tunable_keys="$(printf '%s\n' "$worker_data" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for k in data:
    print(k)
")" || continue

        local tunable
        while IFS= read -r tunable; do
            [[ -z "$tunable" ]] && continue

            local expected_value
            expected_value="$(printf '%s\n' "$worker_data" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('$tunable', ''))
")"

            if [[ -z "$expected_value" ]]; then
                continue
            fi

            if [[ "$DRY_RUN" == "true" ]]; then
                log_info "[DRY-RUN] Would restore ${tunable}=${expected_value} on ${worker_hostname}"
                continue
            fi

            # Apply via debugfs
            local apply_result
            apply_result="$(ssh_node "$ip2" "echo '${expected_value}' > /sys/kernel/debug/sched/${tunable} 2>&1")" || {
                log_error "Failed to set ${tunable}=${expected_value} on ${worker_hostname}: $apply_result"
                total_errors=$((total_errors + 1))
                continue
            }
            log_info "Set ${tunable}=${expected_value} on ${worker_hostname}"

            # Verify readback
            local readback
            readback="$(ssh_node "$ip2" "cat /sys/kernel/debug/sched/${tunable} 2>/dev/null")" || readback=""
            if [[ "$readback" != "$expected_value" ]]; then
                log_error "Verification failed for ${tunable} on ${worker_hostname}: expected ${expected_value}, read ${readback:-<empty>}"
                total_errors=$((total_errors + 1))
            else
                log_info "Verified ${tunable}=${readback} on ${worker_hostname}"
            fi
        done <<< "$tunable_keys"
    done

    if [[ $total_errors -gt 0 ]]; then
        log_warn "Restore completed with ${total_errors} error(s)"
        return 1
    fi
    log_info "Restore complete — all tunables verified on all workers"
}

# ---- List Command ----
cmd_list() {
    log_info "Discovered scheduler tunables on local machine (/sys/kernel/debug/sched/):"
    printf '\n'
    if [[ -d /sys/kernel/debug/sched ]]; then
        for f in /sys/kernel/debug/sched/*; do
            if [[ -f "$f" ]]; then
                printf '  %s=%s\n' "$(basename "$f")" "$(cat "$f")"
            fi
        done
    else
        log_error "/sys/kernel/debug/sched/ not found -- is debugfs mounted?"
        return 1
    fi
    printf '\n'
    log_info "To capture remote worker tunables, run: $SCRIPT_NAME capture"
}

# ---- Argument Parsing ----
main() {
    local positional_count=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage 0
                ;;
            --file)
                BASELINE_FILE="${2:?--file requires a path argument}"
                shift 2
                ;;
            --file=*)
                BASELINE_FILE="${1#*=}"
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            -*)
                log_error "Unknown option: $1"
                usage 1
                ;;
            *)
                if [[ $positional_count -eq 0 ]]; then
                    COMMAND="$1"
                    positional_count=1
                else
                    log_error "Unexpected argument: $1"
                    usage 1
                fi
                shift
                ;;
        esac
    done

    # Validate command
    case "${COMMAND:-}" in
        capture|restore|list)
            ;;
        "")
            log_error "Missing command (capture|restore|list)"
            usage 1
            ;;
        *)
            log_error "Invalid command: '$COMMAND' (must be capture, restore, or list)"
            usage 1
            ;;
    esac

    # For capture/restore, ensure we have a valid baseline file path
    if [[ "$COMMAND" == "capture" || "$COMMAND" == "restore" ]]; then
        # Normalize path to absolute
        if [[ "$BASELINE_FILE" != /* ]]; then
            BASELINE_FILE="${PROJECT_ROOT}/${BASELINE_FILE}"
        fi
    fi

    case "$COMMAND" in
        capture)
            check_deps || exit 1
            cmd_capture
            ;;
        restore)
            check_deps || exit 1
            cmd_restore
            ;;
        list)
            cmd_list
            ;;
    esac
}

main "$@"
