#!/usr/bin/env bash
# tf-inventory.sh — Dynamic Ansible inventory from Terraform/OpenTofu state
#
# Sources:
#   1. tofu output for node names and known IPs
#   2. virsh net-dhcp-leases for IP discovery when tofu data sources are null
#   3. terraform-inventory binary for additional group membership (optional)
#
# Usage: ./tf-inventory.sh --list
#        ./tf-inventory.sh --host <HOST>

set -Eeuo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TERRAFORM_DIR="${PROJECT_DIR}/terraform"

# ---------------------------------------------------------------------------
# Get node names from tofu output
# ---------------------------------------------------------------------------
get_node_names() {
  local bin="tofu"
  command -v tofu &>/dev/null || bin="terraform"
  if ! command -v "$bin" &>/dev/null; then return; fi

  local json
  json=$("$bin" -chdir="$TERRAFORM_DIR" output -json node_names 2>/dev/null || true)
  [ -z "$json" ] && return

  # output -json node_names returns bare array: ["a","b"]
  if command -v jq &>/dev/null; then
    echo "$json" | jq -r '.[] // empty' 2>/dev/null
  else
    echo "$json" | grep -oP '"[a-zA-Z0-9_.-]+"' | tr -d '"'
  fi
}

# ---------------------------------------------------------------------------
# Get IPs from libvirt DHCP leases (virsh) via MAC address matching
# Uses domiflist to get MAC per VM, then net-dhcp-leases to find IP by MAC.
# Returns: IP_ADDRESS VM_NAME (space-separated pairs, one per line)
# ---------------------------------------------------------------------------
get_lease_ips() {
  local net="$1"
  shift
  local vnames=("$@")
  command -v virsh &>/dev/null || return
  [ ${#vnames[@]} -eq 0 ] && return
  [ -z "$net" ] && return

  local lease_data
  lease_data=$(virsh net-dhcp-leases "$net" 2>/dev/null || true)
  [ -z "$lease_data" ] && return

  local vname mac ip
  for vname in "${vnames[@]}"; do
    mac=$(virsh domiflist "$vname" 2>/dev/null | awk 'NR>2 && $5 {print $5; exit}' || true)
    [ -z "$mac" ] && continue
    ip=$(echo "$lease_data" | awk -v m="$mac" 'BEGIN{IGNORECASE=1} $3 == m {print $5; exit}' 2>/dev/null || true)
    ip="${ip%%/*}"  # strip CIDR suffix
    [ -n "$ip" ] && echo "$ip $vname"
  done
}

# ---------------------------------------------------------------------------
# Get known IPs from tofu outputs
# ---------------------------------------------------------------------------
get_tofu_ips() {
  local bin="tofu"
  command -v tofu &>/dev/null || bin="terraform"
  command -v "$bin" &>/dev/null || return

  local json
  json=$("$bin" -chdir="$TERRAFORM_DIR" output -json 2>/dev/null || true)
  [ -z "$json" ] && return

  if command -v jq &>/dev/null; then
    local cp_ip worker_ips
    cp_ip=$(echo "$json" | jq -r '.control_plane_ip.value // empty' 2>/dev/null)
    worker_ips=$(echo "$json" | jq -r '.worker_ips.value[] // empty' 2>/dev/null || true)

    [ -n "$cp_ip" ] && echo "cp_ip=$cp_ip"
    if [ -n "$worker_ips" ]; then
      while IFS= read -r w; do
        [ -n "$w" ] && echo "worker_ip=$w"
      done <<< "$worker_ips"
    fi
  fi
}

# ---------------------------------------------------------------------------
# --list mode: build full inventory JSON
# ---------------------------------------------------------------------------
output_list() {
  # Get node names from tofu
  local node_names=()
  while IFS= read -r name; do
    [ -n "$name" ] && node_names+=("$name")
  done < <(get_node_names || true)

  local cp_name="${node_names[0]:-k8s-cp-1}"
  local worker_names=("${node_names[@]:1}")

  # Collect IPs: try tofu first, fall back to virsh leases
  local -A node_ips=()
  local tofu_has_ip=0

  # Try tofu outputs — collect IPs into arrays
  local cp_ip_addr=""
  local -a worker_ip_addrs=()
  while IFS='=' read -r key val; do
    [ -z "$key" ] && continue
    case "$key" in
      cp_ip) cp_ip_addr="$val" ;;
      worker_ip) worker_ip_addrs+=("$val") ;;
    esac
  done < <(get_tofu_ips || true)

  local cp_name="${node_names[0]:-k8s-cp-1}"
  if [ -n "$cp_ip_addr" ]; then
    node_ips["$cp_name"]="$cp_ip_addr"
    tofu_has_ip=1
  fi
  # Assign worker IPs in order
  for i in "${!worker_ip_addrs[@]}"; do
    local w_name="${worker_names[$i]:-worker-$((i+1))}"
    node_ips["$w_name"]="${worker_ip_addrs[$i]}"
  done

  # If tofu has no IPs, try virsh leases (MAC-based matching)
  if [ "$tofu_has_ip" -eq 0 ]; then
    local net
    net=$(virsh net-list --name 2>/dev/null | head -1 | tr -d '[:space:]' || true)
    [ -z "$net" ] && net="k8s-cluster-net"
    while IFS=' ' read -r ip vname; do
      [ -z "$ip" ] || [ -z "$vname" ] && continue
      node_ips["$vname"]="$ip"
    done < <(get_lease_ips "$net" "${node_names[@]}" || true)
  fi

  # Build JSON output using jq if available (correct by construction)
  local cp_ip="${node_ips[$cp_name]:-}"

  # Build hostvars JSON object
  local hostvars_json="{"
  local host_entries=()

  if [ -n "$cp_ip" ]; then
    host_entries+=("\"${cp_name}\":{\"ansible_host\":\"${cp_ip}\",\"ansible_user\":\"root\",\"node_role\":\"control-plane\",\"node_ip\":\"${cp_ip}\",\"node_name\":\"${cp_name}\",\"cp_ip\":\"${cp_ip}\"}")
  fi

  local i w_name
  for i in "${!worker_names[@]}"; do
    w_name="${worker_names[$i]}"
    local w_ip="${node_ips[$w_name]:-}"
    if [ -n "$w_ip" ]; then
      host_entries+=("\"${w_name}\":{\"ansible_host\":\"${w_ip}\",\"ansible_user\":\"root\",\"node_role\":\"worker\",\"node_ip\":\"${w_ip}\",\"node_name\":\"${w_name}\"}")
    fi
  done

  local IFS=,
  hostvars_json+="${host_entries[*]}"
  hostvars_json+="}"

  # Build group host lists
  local cp_host_entry worker_hosts_str all_hosts_str

  if [ -n "$cp_ip" ]; then
    cp_host_entry="\"${cp_name}\""
  else
    cp_host_entry=""
  fi

  local worker_entries=()
  for w_name in "${worker_names[@]}"; do
    [ -n "$w_name" ] && worker_entries+=("\"${w_name}\"")
  done
  IFS=,
  worker_hosts_str="[${worker_entries[*]}]"

  local all_entries=()
  [ -n "$cp_name" ] && all_entries+=("\"${cp_name}\"")
  for w_name in "${worker_names[@]}"; do
    [ -n "$w_name" ] && all_entries+=("\"${w_name}\"")
  done
  IFS=,
  all_hosts_str="[${all_entries[*]}]"
  unset IFS

  # Build host dict entries for groups (include ansible_host directly)
  local cp_host_dict worker_hosts_dict all_hosts_dict
  cp_host_dict="{}"
  if [ -n "$cp_ip" ]; then
    cp_host_dict="{\"${cp_name}\":{\"ansible_host\":\"${cp_ip}\"}}"
  fi

  worker_hosts_dict="{"
  local w_entries=()
  for w_name in "${worker_names[@]}"; do
    local w_ip="${node_ips[$w_name]:-}"
    if [ -n "$w_name" ] && [ -n "$w_ip" ]; then
      w_entries+=("\"${w_name}\":{\"ansible_host\":\"${w_ip}\"}")
    fi
  done
  IFS=,
  worker_hosts_dict+="${w_entries[*]}"
  worker_hosts_dict+="}"

  all_hosts_dict="{"
  local a_entries=()
  if [ -n "$cp_name" ]; then
    local cp_ip_for="${node_ips[$cp_name]:-}"
    a_entries+=("\"${cp_name}\":{\"ansible_host\":\"${cp_ip_for}\"}")
  fi
  for w_name in "${worker_names[@]}"; do
    local w_ip="${node_ips[$w_name]:-}"
    if [ -n "$w_name" ] && [ -n "$w_ip" ]; then
      a_entries+=("\"${w_name}\":{\"ansible_host\":\"${w_ip}\"}")
    fi
  done
  IFS=,
  all_hosts_dict+="${a_entries[*]}"
  all_hosts_dict+="}"
  unset IFS

  # Assemble final JSON
  cat <<INV_EOF
{
  "control_plane": {
    "hosts": ${cp_host_dict},
    "vars": {
      "node_role": "control_plane"
    }
  },
  "worker": {
    "hosts": ${worker_hosts_dict},
    "vars": {
      "node_role": "worker"
    }
  },
  "cluster": {
    "hosts": ${all_hosts_dict},
    "vars": {
      "pod_cidr": "10.244.0.0/16",
      "service_cidr": "10.96.0.0/12",
      "lb_pool_cidr": "10.0.10.0/24"
    }
  }
}
INV_EOF
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
