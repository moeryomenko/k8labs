#!/usr/bin/env bash
# destroy-taps.sh -- Destroy TAP devices, bridge, and stop dnsmasq
#
# Usage: ./scripts/destroy-taps.sh [num_workers]
#
# Tears down what create-taps.sh creates:
#   - Stops dnsmasq
#   - Removes TAP devices 'k8s-cp1'..'k8s-wN'
#   - Removes bridge 'k8sbr0'
#
# Default: 1 worker (total 2 VMs)
# Is idempotent: safe to run multiple times.
set -Eeuo pipefail
IFS=$'\n\t'

# ---- Configuration ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

NUM_WORKERS="${1:-1}"
BRIDGE="k8sbr0"
TAP_PREFIX="k8s"
DNSMASQ_PID_FILE="/var/run/k8sbr0-dnsmasq.pid"

# ---- Helpers ----
log_info() { printf '[%s] %s\n' "${SCRIPT_NAME}" "$*"; }
log_error() { printf '[%s] ERROR: %s\n' "${SCRIPT_NAME}" "$*" >&2; }

# ---- Pre-flight checks ----
if [[ ${EUID} -ne 0 ]]; then
	log_error "This script must be run as root (use sudo)"
	exit 1
fi

if ! [[ "${NUM_WORKERS}" =~ ^[0-9]+$ ]] || (( NUM_WORKERS < 1 )); then
	log_error "num_workers must be a positive integer, got '${NUM_WORKERS}'"
	exit 1
fi

if ! command -v ip &>/dev/null; then
	log_error "Required command not found: ip"
	exit 1
fi

# ---- Step 1: Stop dnsmasq ----
log_info "Step 1/4: Stopping dnsmasq..."

if [[ -f "${DNSMASQ_PID_FILE}" ]]; then
	dnsmasq_pid_val=$(cat "${DNSMASQ_PID_FILE}")
	if kill -0 "${dnsmasq_pid_val}" 2>/dev/null; then
		kill "${dnsmasq_pid_val}" 2>/dev/null || true
		log_info "  Stopped dnsmasq (PID ${dnsmasq_pid_val})"
	else
		log_info "  dnsmasq not running (stale PID file)"
	fi
	rm -f "${DNSMASQ_PID_FILE}"
else
	log_info "  No PID file found (dnsmasq not running)"
fi

# ---- Step 2: Remove TAP devices ----
log_info "Step 2/4: Removing TAP devices..."

destroy_tap() {
	local tap="$1"
	if ip link show "${tap}" &>/dev/null; then
		ip link delete "${tap}"
		log_info "  Removed TAP ${tap}"
	else
		log_info "  TAP ${tap} does not exist"
	fi
}

TAP_CP="${TAP_PREFIX}-cp1"
destroy_tap "${TAP_CP}"
for ((i = 1; i <= NUM_WORKERS; i++)); do
	destroy_tap "${TAP_PREFIX}-w${i}"
done

# ---- Step 3: Remove bridge ----
log_info "Step 3/4: Removing bridge ${BRIDGE}..."

if ip link show "${BRIDGE}" &>/dev/null; then
	ip link set "${BRIDGE}" down 2>/dev/null || true
	ip link delete "${BRIDGE}"
	log_info "  Removed bridge ${BRIDGE}"
else
	log_info "  Bridge ${BRIDGE} does not exist"
fi

# ---- Step 4: Summary ----
log_info "Step 4/4: Teardown complete"
log_info "  dnsmasq: stopped"
log_info "  TAPs:    cleaned"
log_info "  Bridge:  removed"
