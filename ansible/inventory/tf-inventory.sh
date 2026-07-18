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
# Get IPs from libvirt DHCP leases (virsh)
# Returns: IP_ADDRESS VM_NAME (space-separated pairs)
# ---------------------------------------------------------------------------
get_lease_ips() {
  command -v virsh &>/dev/null || return

  local net
  net=$(virsh net-list --name 2>/dev/null | grep -v "^$" | head -1 || true)
  [ -z "$net" ] && return

  # Format: IP HOSTNAME
  virsh net-dhcp-leases "$net" 2>/dev/null | awk '
    NR>2 && /[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/ {
      ip=""; hostname=""
      for(i=1;i<=NF;i++) {
        if($i ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $i !~ /^0\./) {
          ip=$i
        }
        # Look for hostname in the lease line
        if($i ~ /^k8s-/ || $i ~ /^k8labs/) {
          hostname=$i
        }
      }
      if(ip) print ip, hostname
    }
  '
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

  # Try tofu outputs
  local cp_ip_addr="" worker_ip_addr=""
  while IFS='=' read -r key val; do
    [ -z "$key" ] && continue
    case "$key" in
      cp_ip) cp_ip_addr="$val" ;;
      worker_ip) worker_ip_addr="$val" ;;
    esac
  done < <(get_tofu_ips || true)

  local cp_name="${node_names[0]:-k8s-cp-1}"
  if [ -n "$cp_ip_addr" ]; then
    node_ips["$cp_name"]="$cp_ip_addr"
    tofu_has_ip=1
  fi
  if [ -n "$worker_ip_addr" ] && [ ${#node_names[@]} -gt 1 ]; then
    node_ips["${node_names[1]}"]="$worker_ip_addr"
  elif [ -n "$worker_ip_addr" ] && [ ${#node_names[@]} -eq 1 ]; then
    node_ips["worker-1"]="$worker_ip_addr"
  fi

  # If tofu has no IPs, try virsh leases
  if [ "$tofu_has_ip" -eq 0 ]; then
    while IFS=' ' read -r ip hostname; do
      [ -z "$ip" ] && continue
      # Try to match by hostname pattern
      for node in "${node_names[@]}" "$cp_name" "${worker_names[@]}"; do
        if echo "$hostname" | grep -qi "$node" 2>/dev/null; then
          node_ips["$node"]="$ip"
        fi
      done
      # If no match by hostname, assign by position
      if [ ${#node_ips[@]} -eq 0 ] && [ ${#node_names[@]} -gt 0 ]; then
        node_ips["${node_names[0]}"]="$ip"
      elif [ ${#node_ips[@]} -eq 1 ] && [ ${#worker_names[@]} -gt 0 ]; then
        node_ips["${worker_names[0]}"]="$ip"
      fi
    done < <(get_lease_ips || true)
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
