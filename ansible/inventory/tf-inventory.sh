#!/usr/bin/env bash
# tf-inventory.sh — Dynamic Ansible inventory for Cloud-Hypervisor VMs
#
# Sources:
#   1. tofu output for node names and MAC addresses
#   2. dnsmasq DHCP lease file for IP discovery (mac→ip mapping)
#
# Usage: ./tf-inventory.sh --list
#        ./tf-inventory.sh --host <HOST>

set -Eeuo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TERRAFORM_DIR="${PROJECT_DIR}/terraform"
DNSMASQ_LEASES="/var/lib/misc/dnsmasq/k8sbr0.leases"

# ---------------------------------------------------------------------------
# Get node info from tofu output (name, mac, role)
# Returns TSV: name\tmac\trole
# ---------------------------------------------------------------------------
get_node_info() {
  local bin="tofu"
  command -v tofu &>/dev/null || bin="terraform"
  if ! command -v "$bin" &>/dev/null; then return; fi

  local json
  json=$("$bin" -chdir="$TERRAFORM_DIR" output -json nodes 2>/dev/null || true)
  [ -z "$json" ] && return

  if command -v jq &>/dev/null; then
    echo "$json" | jq -r '.[] | [.name, .mac, .role] | @tsv' 2>/dev/null
  else
    # Fallback: python3
    python3 -c "
import json, sys
nodes = json.load(sys.stdin)
for n in nodes:
    print(f'{n[\"name\"]}\t{n[\"mac\"]}\t{n[\"role\"]}')
" <<< "$json" 2>/dev/null
  fi
}

# ---------------------------------------------------------------------------
# Read dnsmasq lease file and extract IP by MAC address
# dnsmasq lease format: expiry mac ip hostname client-id
# Returns: IP_ADDRESS (empty if not found)
# ---------------------------------------------------------------------------
get_ip_by_mac() {
  local mac="$1"
  [ ! -f "$DNSMASQ_LEASES" ] && return
  awk -v m="$mac" 'BEGIN{IGNORECASE=1} $2 == m {print $3; exit}' "$DNSMASQ_LEASES" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# --list mode: build full inventory JSON
# ---------------------------------------------------------------------------
output_list() {
  # Get node info from tofu
  local node_data
  node_data=$(get_node_info || true)
  [ -z "$node_data" ] && {
    echo '{}'
    return
  }

  # Collect IPs from dnsmasq leases for each node
  local resolved=""
  while IFS=$'\t' read -r name mac role; do
    [ -z "$name" ] && continue
    local ip
    ip=$(get_ip_by_mac "$mac")
    if [ -n "$ip" ]; then
        resolved+="${name}"$'\t'"${ip}"$'\t'"${role}"$'\n'
    fi
  done <<< "$node_data"

  # Build inventory JSON
  if command -v jq &>/dev/null; then
    echo "$resolved" | jq -Rs '
      split("\n") | map(select(length > 0) | split("\t") | select(length >= 3) | {name: .[0], ip: .[1], role: .[2]}) as $nodes |
      {
        control_plane: {
          hosts: ([$nodes[] | select(.role == "control_plane") | {(.name): {ansible_host: .ip}}] | add // {}),
          vars: {node_role: "control_plane"}
        },
        worker: {
          hosts: ([$nodes[] | select(.role == "worker") | {(.name): {ansible_host: .ip}}] | add // {}),
          vars: {node_role: "worker"}
        },
        cluster: {
          hosts: ([$nodes[] | {(.name): {ansible_host: .ip}}] | add // {}),
          vars: {
            pod_cidr: "10.244.0.0/16",
            service_cidr: "10.96.0.0/12",
            lb_pool_cidr: "10.0.10.0/24"
          }
        }
      }
    '
  else
    # Fallback: python3
    python3 -c '
import json, sys

nodes = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    parts = line.split("\t")
    if len(parts) >= 3:
        nodes.append({"name": parts[0], "ip": parts[1], "role": parts[2]})

cp_hosts = {}
worker_hosts = {}
all_hosts = {}

for n in nodes:
    host_entry = {"ansible_host": n["ip"]}
    all_hosts[n["name"]] = host_entry
    if n["role"] == "control_plane":
        cp_hosts[n["name"]] = host_entry
    elif n["role"] == "worker":
        worker_hosts[n["name"]] = host_entry

result = {
    "control_plane": {"hosts": cp_hosts, "vars": {"node_role": "control_plane"}},
    "worker": {"hosts": worker_hosts, "vars": {"node_role": "worker"}},
    "cluster": {
        "hosts": all_hosts,
        "vars": {
            "pod_cidr": "10.244.0.0/16",
            "service_cidr": "10.96.0.0/12",
            "lb_pool_cidr": "10.0.10.0/24"
        }
    }
}
print(json.dumps(result, indent=2))
' <<< "$resolved"
  fi
}

# ---------------------------------------------------------------------------
# --host mode
# ---------------------------------------------------------------------------
output_host() {
  local host="$1"
  echo "{\"ansible_user\":\"root\",\"node_role\":\"cluster\"}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
case "${1:-}" in
  --list) output_list ;;
  --host) output_host "${2:-}" ;;
  *)
    echo "Usage: $0 --list | --host <HOST>" >&2
    exit 1
    ;;
esac
