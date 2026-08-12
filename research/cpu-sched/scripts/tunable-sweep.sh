#!/usr/bin/env bash
#
# tunable-sweep.sh — Apply/restore kernel scheduler tunable sets on worker nodes
#
# Usage:
#   tunable-sweep.sh apply <config-key> [--file path] [--dry-run]
#   tunable-sweep.sh restore [--file path] [--dry-run]
#   tunable-sweep.sh list
#   tunable-sweep.sh set <tunable=value> [<tunable=value> ...] [--dry-run]
#
# Commands:
#   apply     Apply a named tunable set from tunable-sets.json to all workers
#   restore   Restore defaults from a captured baseline JSON file
#   list      List available config keys from the tunable sets file
#   set       Apply specific tunable=value pairs directly (no config file)
#
# Options:
#   --file path   Path to tunable sets JSON (default: research/cpu-sched/data/tunable-sets.json)
#                 For restore, path to baseline JSON (default: research/cpu-sched/data/tunable-baseline.json)
#   --dry-run     Print what would be done without making changes
#   -h, --help    Show this help and exit
#
# Safety:
#   - Range validation rejects out-of-bounds values before applying
#   - Readback verified on every tunable after setting
#   - SSH timeout 10s on all connections
#   - EXIT trap warns if tunables were modified (prompts for restore consideration)
#   - Dry-run mode previews all changes without applying

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

# ---- Constants ----
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

DEFAULT_DATA_DIR="${PROJECT_ROOT}/data"
DEFAULT_SETS_FILE="${DEFAULT_DATA_DIR}/tunable-sets.json"
DEFAULT_BASELINE_FILE="${DEFAULT_DATA_DIR}/tunable-baseline.json"

# Source the shared DHCP lease helper for worker IP discovery
# shellcheck source=../bin/lease-common.sh
source "${SCRIPT_DIR}/../bin/lease-common.sh"

# Tunable range limits (inclusive)
# These map to files under /sys/kernel/debug/sched/ on Fedora 44+
declare -A TUNABLE_RANGES=(
    [base_slice_ns]="500000-50000000"
    [min_granularity_ns]="100000-100000000"
    [latency_ns]="100000-1000000000"
    [migration_cost_ns]="0-5000000"
    [wakeup_granularity_ns]="0-100000000"
    [nr_migrate]="0-10000"
)

# ---- Logging ----
log_info()  { printf '[%s] INFO:  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
log_warn()  { printf '[%s] WARN:  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
log_error() { printf '[%s] ERROR: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }

# ---- Global State ----
DRY_RUN=false
ACCEPT=false                     # skip restore prompt on exit
_TUNABLES_MODIFIED=false         # tracks whether we applied any tunables
COMMAND=""
CONFIG_KEY=""
SET_ARGS=()

# Files
SETS_FILE="$DEFAULT_SETS_FILE"
RESTORE_FILE="$DEFAULT_BASELINE_FILE"

# ---- Cleanup / Safety ----
_cleanup() {
    local exit_code=$?

    if [[ "$_TUNABLES_MODIFIED" == "true" && "$ACCEPT" != "true" && "$DRY_RUN" != "true" ]]; then
        log_warn "Tunables were modified during this session."
        log_warn "Run '$SCRIPT_NAME restore' to restore defaults from baseline."
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
Usage: $SCRIPT_NAME {apply|restore|list|set} [ARGS] [OPTIONS]

Apply named tunable sets, restore defaults, list available sets, or set
specific values directly (via /sys/kernel/debug/sched/).

Commands:
  apply <key>     Apply named tunable set to all workers
  restore         Restore defaults from captured baseline JSON
  list            List available config keys
  set <kv> [...]  Apply specific tunable=value pairs directly

Options:
  --file path     For apply: path to tunable sets JSON
                  For restore: path to baseline JSON
  --dry-run       Print what would be done without making changes
  -h, --help      Show this help and exit

Examples:
  $SCRIPT_NAME list
  $SCRIPT_NAME apply default
  $SCRIPT_NAME apply base-slice-low --dry-run
  $SCRIPT_NAME apply all-low
  $SCRIPT_NAME restore
  $SCRIPT_NAME set base_slice_ns=5000000
  $SCRIPT_NAME set min_granularity_ns=1000000 \\
                migration_cost_ns=0
EOF
    exit "${1:-0}"
}

# ---- Dependency Checking ----
check_deps() {
    local -a missing=()
    for cmd in ssh tofu terraform jq; do
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

# ---- SSH Helper ----
ssh_node() {
    local ip="$1"
    shift
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=10 -o BatchMode=yes \
        "root@$ip" "$@"
}

# ---- Range Validation ----
# Returns 0 if value is within range, 1 if out of bounds
validate_tunable_value() {
    local tunable="$1"
    local value="$2"

    # Allow empty value for non-range-checked tunables
    if [[ -z "$value" ]]; then
        log_error "Empty value for $tunable"
        return 1
    fi

    # Check if we have a range for this tunable
    local range="${TUNABLE_RANGES[$tunable]:-}"
    if [[ -z "$range" ]]; then
        # Unknown tunable — accept but warn
        log_warn "No range defined for $tunable — accepting value $value without validation"
        return 0
    fi

    # Parse min and max from range string (e.g., "100000-1000000000")
    local min_val="${range%-*}"
    local max_val="${range#*-}"

    # Ensure value is numeric (positive integer)
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        log_error "Invalid non-numeric value '$value' for $tunable (must be positive integer)"
        return 1
    fi

    if (( value < min_val )); then
        log_error "Value $value for $tunable is below minimum $min_val"
        return 1
    fi

    if (( value > max_val )); then
        log_error "Value $value for $tunable exceeds maximum $max_val"
        return 1
    fi

    return 0
}

# ---- Apply tunables to all workers ----
# Takes a list of "tunable=value" strings
apply_tunables() {
    local -a target_tunables=("$@")
    local total_errors=0

    if [[ ${#target_tunables[@]} -eq 0 ]]; then
        log_error "No tunables specified to apply"
        return 1
    fi

    # Validate all tunables first
    log_info "Validating tunable values..."
    local kv
    for kv in "${target_tunables[@]}"; do
        local tunable="${kv%%=*}"
        local value="${kv#*=}"

        if [[ -z "$tunable" || "$tunable" == "$kv" ]]; then
            log_error "Invalid tunable specification: '$kv' (expected format: tunable=value)"
            return 1
        fi

        validate_tunable_value "$tunable" "$value" || return 1
    done
    log_info "All tunable values pass range validation"

    # Resolve worker IPs
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

    # For dry-run, log a summary
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY-RUN] Would apply to ${#worker_ips[@]} worker(s): ${worker_ips[*]}"
        for kv in "${target_tunables[@]}"; do
            local tunable="${kv%%=*}"
            local value="${kv#*=}"
            log_info "[DRY-RUN]   ${tunable}=${value}"
        done
        return 0
    fi

    # Apply on each worker
    local ip2
    for ip2 in "${worker_ips[@]}"; do
        local worker_hostname
        worker_hostname="$(ssh_node "$ip2" "hostname -s 2>/dev/null")" || worker_hostname="$ip2"

        log_info "Applying tunables on ${worker_hostname} ($ip2)..."

        for kv in "${target_tunables[@]}"; do
            local tunable="${kv%%=*}"
            local value="${kv#*=}"

            # Apply via debugfs
            local apply_result
            apply_result="$(ssh_node "$ip2" "echo '${value}' > /sys/kernel/debug/sched/${tunable} 2>&1")" || {
                log_error "Failed to set ${tunable}=${value} on ${worker_hostname}: $(printf '%s' "$apply_result" | head -1)"
                total_errors=$((total_errors + 1))
                continue
            }
            log_info "Set ${tunable}=${value} on ${worker_hostname}"

            # Verify readback
            local readback
            readback="$(ssh_node "$ip2" "cat /sys/kernel/debug/sched/${tunable} 2>/dev/null")" || readback=""
            if [[ "$readback" != "$value" ]]; then
                log_error "Verification FAILED for ${tunable} on ${worker_hostname}: expected ${value}, read ${readback:-<empty>}"
                total_errors=$((total_errors + 1))
            else
                log_info "Verified ${tunable}=${readback} on ${worker_hostname}"
            fi
        done
    done

    if [[ $total_errors -gt 0 ]]; then
        log_warn "Apply completed with ${total_errors} error(s)"
        return 1
    fi

    log_info "All tunables applied and verified on ${#worker_ips[@]} worker(s)"
    return 0
}

# ---- Apply Command ----
cmd_apply() {
    local config_key="$1"

    if [[ -z "$config_key" ]]; then
        log_error "Missing config-key argument for apply command"
        usage 1
    fi

    if [[ ! -f "$SETS_FILE" ]]; then
        log_error "Tunable sets file not found: $SETS_FILE"
        return 1
    fi

    log_info "Loading tunable set '$config_key' from: $SETS_FILE"

    # Extract the tunable set for the given key using jq
    local set_exists
    set_exists="$(jq -r --arg key "$config_key" 'has($key)' "$SETS_FILE" 2>/dev/null)" || {
        log_error "Failed to parse tunable sets file (is it valid JSON?)"
        return 1
    }

    if [[ "$set_exists" != "true" ]]; then
        log_error "Config key '$config_key' not found in $SETS_FILE"
        log_info "Available keys:"
        cmd_list
        return 1
    fi

    # Extract the tunable entries from the config-key
    local tunable_entries
    tunable_entries="$(jq -r --arg key "$config_key" '.[$key] | to_entries[] | "\(.key)=\(.value)"' "$SETS_FILE" 2>/dev/null)" || true

    if [[ -z "$tunable_entries" ]]; then
        log_error "Tunable set '$config_key' is empty or not found in $SETS_FILE"
        return 1
    fi

    # Collect into array
    local -a tunables=()
    while IFS= read -r entry; do
        [[ -z "$entry" ]] && continue
        tunables+=("$entry")
    done <<< "$tunable_entries"

    log_info "Loaded ${#tunables[@]} tunable(s) from set '$config_key'"

    # Apply
    apply_tunables "${tunables[@]}"
    _TUNABLES_MODIFIED=true
}

# ---- Restore Command ----
cmd_restore() {
    if [[ ! -f "$RESTORE_FILE" ]]; then
        log_error "Baseline file not found: $RESTORE_FILE"
        log_info "Capture defaults first with: tunable-defaults.sh capture"
        return 1
    fi

    log_info "Restoring tunables from baseline: $RESTORE_FILE"

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
        local worker_hostname
        worker_hostname="$(ssh_node "$ip2" "hostname -s 2>/dev/null")" || worker_hostname="$ip2"

        log_info "Restoring tunables on ${worker_hostname} ($ip2)..."

        # Extract tunables for this hostname from baseline JSON
        local worker_data
        worker_data="$(python3 -c "
import sys, json
with open('$RESTORE_FILE') as f:
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

            local apply_result
            apply_result="$(ssh_node "$ip2" "echo '${expected_value}' > /sys/kernel/debug/sched/${tunable} 2>&1")" || {
                log_error "Failed to set ${tunable}=${expected_value} on ${worker_hostname}: $apply_result"
                total_errors=$((total_errors + 1))
                continue
            }
            log_info "Set ${tunable}=${expected_value} on ${worker_hostname}"

            local readback
            readback="$(ssh_node "$ip2" "cat /sys/kernel/debug/sched/${tunable} 2>/dev/null")" || readback=""
            if [[ "$readback" != "$expected_value" ]]; then
                log_error "Verification FAILED for ${tunable} on ${worker_hostname}: expected ${expected_value}, read ${readback:-<empty>}"
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
    if [[ ! -f "$SETS_FILE" ]]; then
        log_error "Tunable sets file not found: $SETS_FILE"
        return 1
    fi

    log_info "Available tunable config keys in: $SETS_FILE"
    printf '\n'

    # Use jq to format nicely
    jq -r '
        to_entries[] |
        "- \(.key): \(.value | to_entries | map("\(.key)=\(.value)") | join(", "))"
    ' "$SETS_FILE" 2>/dev/null || {
        log_error "Failed to parse $SETS_FILE (is it valid JSON?)"
        return 1
    }

    printf '\n'
    log_info "Apply a set with: $SCRIPT_NAME apply <key>"
}

# ---- Set Command ----
cmd_set() {
    if [[ ${#SET_ARGS[@]} -eq 0 ]]; then
        log_error "Missing tunable=value arguments for set command"
        usage 1
    fi

    check_deps || exit 1
    apply_tunables "${SET_ARGS[@]}"
    _TUNABLES_MODIFIED=true
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
                # --file means different things for apply vs restore
                # For apply: tunable sets file
                # For restore: baseline file
                if [[ "$COMMAND" == "restore" || "$COMMAND" == "" ]]; then
                    RESTORE_FILE="${2:?--file requires a path argument}"
                else
                    SETS_FILE="${2:?--file requires a path argument}"
                fi
                shift 2
                ;;
            --file=*)
                local val="${1#*=}"
                if [[ "$COMMAND" == "restore" || "$COMMAND" == "" ]]; then
                    RESTORE_FILE="$val"
                else
                    SETS_FILE="$val"
                fi
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --accept)
                ACCEPT=true
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
                elif [[ "$COMMAND" == "apply" && $positional_count -eq 1 ]]; then
                    CONFIG_KEY="$1"
                    positional_count=2
                elif [[ "$COMMAND" == "set" ]]; then
                    SET_ARGS+=("$1")
                else
                    log_error "Unexpected argument: $1"
                    usage 1
                fi
                shift
                ;;
        esac
    done

    # Normalize file paths to absolute
    if [[ "$SETS_FILE" != /* ]]; then
        SETS_FILE="${PROJECT_ROOT}/${SETS_FILE}"
    fi
    if [[ "$RESTORE_FILE" != /* ]]; then
        RESTORE_FILE="${PROJECT_ROOT}/${RESTORE_FILE}"
    fi

    case "${COMMAND:-}" in
        apply)
            check_deps || exit 1
            if [[ -z "$CONFIG_KEY" ]]; then
                log_error "Missing config-key for apply command"
                usage 1
            fi
            cmd_apply "$CONFIG_KEY"
            ;;
        restore)
            check_deps || exit 1
            cmd_restore
            ;;
        list)
            cmd_list
            ;;
        set)
            cmd_set
            ;;
        "")
            log_error "Missing command (apply|restore|list|set)"
            usage 1
            ;;
        *)
            log_error "Invalid command: '$COMMAND' (must be apply, restore, list, or set)"
            usage 1
            ;;
    esac
}

main "$@"
