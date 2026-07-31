#!/bin/bash
# vm-ip.sh — Resolve a VM IP address from its MAC address.
#
# Reads the systemd-networkd DHCP server lease file (JSON) first, then
# falls back to the legacy dnsmasq lease file. Both are populated by the
# DHCP server serving the k8sbr0 bridge.
#
# Usage: vm-ip.sh <mac-address>
#   mac-address: colon-separated MAC, e.g. c6:e5:50:1c:ec:01

set -euo pipefail

MAC="${1:-}"
if [ -z "$MAC" ]; then
    echo "Usage: $0 <mac-address>" >&2
    exit 1
fi
MAC_LC="$(echo "$MAC" | tr '[:upper:]' '[:lower:]')"

SYSTEMD_LEASES="/var/lib/systemd/network/dhcp-server-lease/k8sbr0"
DNSMASQ_LEASES="/var/lib/misc/dnsmasq/k8sbr0.leases"

# Try systemd-networkd JSON lease file first.
if [ -r "$SYSTEMD_LEASES" ]; then
    IP=$(python3 -c "
import json, sys
mac = '$MAC_LC'
with open('$SYSTEMD_LEASES') as f:
    data = json.load(f)
for lease in data.get('Leases', []):
    mac_bytes = lease.get('HardwareAddress', [])
    if len(mac_bytes) != 6:
        continue
    if ':'.join(f'{b:02x}' for b in mac_bytes) == mac:
        print(lease.get('AddressString', ''))
        break
")
    if [ -n "$IP" ]; then
        echo "$IP"
        exit 0
    fi
fi

# Fallback: dnsmasq lease file.
if [ -r "$DNSMASQ_LEASES" ]; then
    IP=$(awk -v m="$MAC_LC" 'BEGIN{IGNORECASE=1} $2 == m {print $3; exit}' "$DNSMASQ_LEASES" 2>/dev/null || true)
    if [ -n "$IP" ]; then
        echo "$IP"
        exit 0
    fi
fi

exit 1
