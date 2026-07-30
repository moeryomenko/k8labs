#!/usr/bin/env bash
# create-taps.sh -- Create bridge and TAP devices for cluster VM networking
#
# Usage: ./scripts/create-taps.sh [num_workers]
#
# Creates:
#   - Linux bridge 'k8sbr0' (192.168.124.1/24)
#   - TAP device 'k8s-cp1' for the control-plane VM
#   - TAP devices 'k8s-w1'..'k8s-wN' for worker VMs
#   - Starts dnsmasq on the bridge for DHCP
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
BRIDGE_ADDR="192.168.124.1/24"
TAP_PREFIX="k8s"
DNSMASQ_PID_FILE="/var/run/k8sbr0-dnsmasq.pid"
DNSMASQ_CONF="${SCRIPT_DIR}/dnsmasq.conf"
DNSMASQ_LEASE_DIR="/var/lib/misc/dnsmasq"
DNSMASQ_LOG="/var/log/k8sbr0-dnsmasq.log"

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

for cmd in ip dnsmasq; do
	if ! command -v "${cmd}" &>/dev/null; then
		log_error "Required command not found: ${cmd}"
		exit 1
	fi
done

# ---- Step 1: Bridge ----
log_info "Step 1/6: Ensuring bridge ${BRIDGE}..."

if ip link show "${BRIDGE}" &>/dev/null; then
	log_info "  Bridge ${BRIDGE} already exists, skipping creation"
else
	ip link add name "${BRIDGE}" type bridge
	log_info "  Created bridge ${BRIDGE}"
fi

# Assign bridge IP (idempotent)
if ip addr show dev "${BRIDGE}" | grep -qF "inet ${BRIDGE_ADDR}"; then
	log_info "  Bridge ${BRIDGE} already has address ${BRIDGE_ADDR}"
else
	ip addr add "${BRIDGE_ADDR}" dev "${BRIDGE}"
	log_info "  Assigned ${BRIDGE_ADDR} to ${BRIDGE}"
fi

ip link set "${BRIDGE}" up

# ---- Step 2: TAP devices ----
log_info "Step 2/6: Creating TAP devices..."

create_tap() {
	local tap="$1"
	if ip link show "${tap}" &>/dev/null; then
		log_info "  TAP ${tap} already exists"
	else
		ip tuntap add dev "${tap}" mode tap
		log_info "  Created TAP ${tap}"
	fi
}

TAP_CP="${TAP_PREFIX}-cp1"
create_tap "${TAP_CP}"
for ((i = 1; i <= NUM_WORKERS; i++)); do
	create_tap "${TAP_PREFIX}-w${i}"
done

# ---- Step 3: Enslave TAPs to bridge ----
log_info "Step 3/6: Attaching TAP devices to bridge ${BRIDGE}..."

enslave_tap() {
	local tap="$1"
	if ip link show "${tap}" | grep -q "master ${BRIDGE}"; then
		log_info "  ${tap} already enslaved to ${BRIDGE}"
	else
		ip link set "${tap}" master "${BRIDGE}"
		log_info "  Attached ${tap} to ${BRIDGE}"
	fi
}

enslave_tap "${TAP_CP}"
for ((i = 1; i <= NUM_WORKERS; i++)); do
	enslave_tap "${TAP_PREFIX}-w${i}"
done

# ---- Step 4: Bring TAPs up ----
log_info "Step 4/6: Bringing TAP devices up..."

bring_tap_up() {
	local tap="$1"
	ip link set "${tap}" up
	log_info "  ${tap} is up"
}

bring_tap_up "${TAP_CP}"
for ((i = 1; i <= NUM_WORKERS; i++)); do
	bring_tap_up "${TAP_PREFIX}-w${i}"
done

# ---- Step 5: Start dnsmasq ----
log_info "Step 5/6: Starting dnsmasq on ${BRIDGE}..."

# Ensure lease directory exists
mkdir -p "${DNSMASQ_LEASE_DIR}"

# Idempotent start using PID file
dnsmasq_running=false
if [[ -f "${DNSMASQ_PID_FILE}" ]]; then
	dnsmasq_pid_val=$(cat "${DNSMASQ_PID_FILE}")
	if kill -0 "${dnsmasq_pid_val}" 2>/dev/null; then
		dnsmasq_running=true
	fi
fi

if [[ "${dnsmasq_running}" == "true" ]]; then
	log_info "  dnsmasq already running (PID ${dnsmasq_pid_val})"
else
	nohup dnsmasq -k \
		--pid-file="${DNSMASQ_PID_FILE}" \
		-C "${DNSMASQ_CONF}" \
		>"${DNSMASQ_LOG}" 2>&1 &
	log_info "  Started dnsmasq (PID $!)"
fi

# ---- Step 6: Summary ----
log_info "Step 6/6: Setup complete"
log_info "  Bridge:  ${BRIDGE} (${BRIDGE_ADDR})"
log_info "  TAPs:    ${TAP_PREFIX}-cp1 + ${NUM_WORKERS} worker(s)"
log_info "  DHCP:    192.168.124.20 - 192.168.124.200 (12h lease)"
