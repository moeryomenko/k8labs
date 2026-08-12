#!/usr/bin/env bash
#
# lease-common.sh — Shared DHCP lease resolution for research scripts
#
# Resolves worker/node IPs from the authoritative systemd-networkd DHCP
# server lease (JSON at /var/lib/systemd/network/dhcp-server-lease/k8sbr0),
# with a fallback to the legacy dnsmasq lease
# (/var/lib/misc/dnsmasq/k8sbr0.leases) for deployments that still run
# dnsmasq as the DHCP server. Mirrors ansible/inventory/inventory.py
# (read_systemd_leases / read_leases).
#
# Exposed functions:
#   read_leases_systemd [PATH]        -> "mac ip" lines from the systemd JSON
#   read_leases_dnsmasq [PATH]        -> "mac ip" lines from dnsmasq format
#   get_worker_ips [--lease-file PATH] -> worker IPs in WORKER_MACS order
#   get_node_ip <mac> [--lease-file PATH] -> IP for a single MAC
#
# Environment (overridable before sourcing):
#   SYSTEMD_LEASES     systemd-networkd DHCP server lease JSON path
#   DNSMASQ_LEASES     dnsmasq lease fallback path
#   WORKER_MACS        space-separated worker MACs; when unset or empty it is
#                      derived from tfvars at source time (see below)
#   WORKER_MACS_TFVARS tfvars file used by the WORKER_MACS derivation
#                      (default: <repo-root>/build/deploy.tfvars)
#
# Usage in scripts:
#   source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/lease-common.sh"

# Guard against double-sourcing (return 0 so a redundant source under
# `set -e` does not abort the caller)
[[ -z ${_LEASE_COMMON_SH:-} ]] || return 0
_LEASE_COMMON_SH=1
readonly _LEASE_COMMON_SH

# ---- Strict Mode ----
set -Eeuo pipefail

# ---- Lease paths (overridable via env) ----
: "${SYSTEMD_LEASES:=/var/lib/systemd/network/dhcp-server-lease/k8sbr0}"
: "${DNSMASQ_LEASES:=/var/lib/misc/dnsmasq/k8sbr0.leases}"

# ---------------------------------------------------------------------------
# _lease_find_project_root — Print the k8labs repository root, or nothing.
#
# Mirrors cgroup-common.sh's find_project_root: git rev-parse first, then an
# upward walk for Makefile + terraform/ markers. Prints the root (or nothing
# when not found); always returns 0 so the caller can use a plain assignment
# under strict mode. Used by the WORKER_MACS derivation, which must run the
# root parser (scripts/nodes.py via the root venv) from the repository root.
# ---------------------------------------------------------------------------
_lease_find_project_root() {
    local dir
    dir="$(git rev-parse --show-toplevel 2>/dev/null)" && {
        printf '%s\n' "${dir}"
        return 0
    }

    dir="${PWD}"
    while [[ "${dir}" != "/" ]]; do
        if [[ -f "${dir}/Makefile" && -d "${dir}/terraform" ]]; then
            printf '%s\n' "${dir}"
            return 0
        fi
        dir="$(dirname "${dir}")"
    done

    return 0
}

# ---------------------------------------------------------------------------
# WORKER_MACS derivation (source-time, once per process)
#
# When WORKER_MACS is unset or empty, derive the worker MAC list from the
# root parser: `uv run python scripts/nodes.py --worker-macs` against the
# tfvars file named by $WORKER_MACS_TFVARS (default:
# <repo-root>/build/deploy.tfvars). An exported non-empty WORKER_MACS wins
# and no derivation runs. Any derivation failure (missing tfvars/venv,
# missing scripts/nodes.py, parser error, non-zero exit) falls back
# non-fatally to the historical two-MAC default; a successful parse that
# yields zero workers leaves WORKER_MACS empty so resolution fails loudly
# later.
# ---------------------------------------------------------------------------
if [[ -z "${WORKER_MACS:-}" ]]; then
    _LEASE_ROOT="$( _lease_find_project_root 2>/dev/null )"

    _LEASE_TFVARS="${WORKER_MACS_TFVARS:-}"
    if [[ -z "${_LEASE_TFVARS}" && -n "${_LEASE_ROOT}" ]]; then
        _LEASE_TFVARS="${_LEASE_ROOT}/build/deploy.tfvars"
    fi

    if [[ -n "${_LEASE_TFVARS}" ]] && _LEASE_DERIVED="$(
        cd -- "${_LEASE_ROOT}" 2>/dev/null &&
        uv run python scripts/nodes.py --worker-macs --tfvars "${_LEASE_TFVARS}" 2>/dev/null
    )"; then
        WORKER_MACS="${_LEASE_DERIVED}"
    else
        WORKER_MACS="c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03"
    fi
fi

# ---------------------------------------------------------------------------
# read_leases_systemd — Print "mac ip" lines from the systemd-networkd DHCP
# server lease JSON.
#
# The JSON has a top-level "Leases" array; each lease carries
# "HardwareAddress" (list of 6 bytes) and "AddressString" (the IP). MAC keys
# are lowercased and zero-padded (c6:e5:50:1c:ec:02). Leases with a
# HardwareAddress length other than 6 or an empty AddressString are skipped.
# Unknown keys (ClientId, Expiration*) are ignored.
#
# Arguments:
#   $1 — optional lease file path (default: $SYSTEMD_LEASES)
# Returns:
#   0 — always (missing/malformed file yields empty output, exit 0)
# ---------------------------------------------------------------------------
read_leases_systemd() {
    local path="${1:-$SYSTEMD_LEASES}"
    [[ -f "$path" && -r "$path" ]] || return 0

    local out
    out="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
except Exception:
    sys.exit(0)
for lease in data.get("Leases", []):
    mac_bytes = lease.get("HardwareAddress", [])
    if len(mac_bytes) != 6:
        continue
    ip = lease.get("AddressString", "")
    if not ip:
        continue
    print(":".join(f"{b:02x}" for b in mac_bytes), ip)
' "$path" 2>/dev/null)" || return 0

    [[ -n "$out" ]] && printf '%s\n' "$out"
    return 0
}

# ---------------------------------------------------------------------------
# read_leases_dnsmasq — Print "mac ip" lines from a dnsmasq lease file.
#
# Line format: "<expiry> <mac> <ip> [hostname] [client-id]". MAC keys are
# normalized to lowercase; lines with fewer than 3 fields are skipped.
#
# Arguments:
#   $1 — optional lease file path (default: $DNSMASQ_LEASES)
# Returns:
#   0 — always (missing file yields empty output, exit 0)
# ---------------------------------------------------------------------------
read_leases_dnsmasq() {
    local path="${1:-$DNSMASQ_LEASES}"
    [[ -f "$path" && -r "$path" ]] || return 0

    awk 'NF >= 3 { print tolower($2), $3 }' "$path" 2>/dev/null || true
    return 0
}

# ---------------------------------------------------------------------------
# _lease_map — Print the effective MAC and IP mapping ("mac ip" lines).
#
# systemd lease first (inventory.py order); the dnsmasq lease is used only
# when the systemd mapping is empty (missing file, malformed JSON, or no
# usable leases). Returns 1 when both sources yield nothing.
#
# Arguments:
#   $1 — systemd lease JSON path
#   $2 — dnsmasq lease path
# Returns:
#   0 — mapping printed; 1 — no usable lease source
# ---------------------------------------------------------------------------
_lease_map() {
    local sys_path="$1"
    local dnsmasq_path="$2"
    local sys_out
    sys_out="$(read_leases_systemd "$sys_path")" || sys_out=""

    if [[ -n "$sys_out" ]]; then
        printf '%s\n' "$sys_out"
        return 0
    fi

    local dns_out
    dns_out="$(read_leases_dnsmasq "$dnsmasq_path")" || dns_out=""
    if [[ -n "$dns_out" ]]; then
        printf '%s\n' "$dns_out"
        return 0
    fi

    return 1
}

# ---------------------------------------------------------------------------
# get_worker_ips — Print worker IPs, space-separated, in WORKER_MACS order.
#
# Unknown MACs are skipped; at least one hit is success. On zero hits (or no
# usable lease file) prints an error to stderr and returns non-zero.
#
# Arguments:
#   --lease-file PATH — override the systemd lease JSON path (dnsmasq
#                       fallback path stays $DNSMASQ_LEASES)
# Returns:
#   0 — at least one IP printed; 1 — no IP resolved
# ---------------------------------------------------------------------------
get_worker_ips() {
    local IFS=' '
    local sys_path="$SYSTEMD_LEASES"
    local dnsmasq_path="$DNSMASQ_LEASES"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --lease-file)
                if [[ $# -lt 2 ]]; then
                    printf 'ERROR: --lease-file requires a path\n' >&2
                    return 1
                fi
                sys_path="$2"
                shift 2
                ;;
            --lease-file=*)
                sys_path="${1#*=}"
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    local map
    map="$(_lease_map "$sys_path" "$dnsmasq_path")" || {
        printf 'ERROR: no worker IPs found: no usable DHCP lease file (systemd: %s, dnsmasq: %s)\n' \
            "$sys_path" "$dnsmasq_path" >&2
        return 1
    }

    local -A lease_map=()
    local mac ip
    while IFS=' ' read -r mac ip; do
        lease_map["$mac"]="$ip"
    done <<< "$map"

    local -a macs=() ips=()
    read -r -a macs <<< "$WORKER_MACS" || true

    for mac in "${macs[@]}"; do
        ip="${lease_map["$mac"]:-}"
        [[ -n "$ip" ]] && ips+=("$ip")
    done

    if [[ ${#ips[@]} -eq 0 ]]; then
        printf 'ERROR: no worker IPs found for MACs [%s] in DHCP lease file %s\n' \
            "$WORKER_MACS" "$sys_path" >&2
        return 1
    fi

    printf '%s\n' "${ips[*]}"
}

# ---------------------------------------------------------------------------
# get_node_ip — Print the IP for a single MAC address.
#
# The query MAC is normalized to lowercase (case-insensitive lookup). Prints
# an error to stderr and returns non-zero when the MAC is not found or no
# usable lease file exists.
#
# Arguments:
#   $1 — MAC address (e.g. c6:e5:50:1c:ec:02)
#   --lease-file PATH — override the systemd lease JSON path
# Returns:
#   0 — IP printed; 1 — MAC not resolved
# ---------------------------------------------------------------------------
get_node_ip() {
    local IFS=' '
    local mac=""
    local sys_path="$SYSTEMD_LEASES"
    local dnsmasq_path="$DNSMASQ_LEASES"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --lease-file)
                if [[ $# -lt 2 ]]; then
                    printf 'ERROR: --lease-file requires a path\n' >&2
                    return 1
                fi
                sys_path="$2"
                shift 2
                ;;
            --lease-file=*)
                sys_path="${1#*=}"
                shift
                ;;
            *)
                if [[ -z "$mac" ]]; then
                    mac="$1"
                fi
                shift
                ;;
        esac
    done

    if [[ -z "$mac" ]]; then
        printf 'ERROR: get_node_ip requires a MAC address\n' >&2
        return 1
    fi
    mac="${mac,,}"

    local map
    map="$(_lease_map "$sys_path" "$dnsmasq_path")" || {
        printf 'ERROR: no IP found for MAC %s: no usable DHCP lease file (systemd: %s, dnsmasq: %s)\n' \
            "$mac" "$sys_path" "$dnsmasq_path" >&2
        return 1
    }

    local m ip
    while IFS=' ' read -r m ip; do
        if [[ "$m" == "$mac" ]]; then
            printf '%s\n' "$ip"
            return 0
        fi
    done <<< "$map"

    printf 'ERROR: no IP found for MAC %s in DHCP lease file %s\n' "$mac" "$sys_path" >&2
    return 1
}
