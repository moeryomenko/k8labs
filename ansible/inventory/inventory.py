#!/usr/bin/env python3
"""Dynamic Ansible inventory for Cloud-Hypervisor VMs.

Reads node metadata from Terraform/OpenTofu output and resolves VM IPs by
cross-referencing MAC addresses against the dnsmasq DHCP lease file.

Usage:
    ./ansible/inventory/inventory.py --list
    ./ansible/inventory/inventory.py --host <HOST>
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Any

PROJECT_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "../.."))
TERRAFORM_DIR = os.path.join(PROJECT_DIR, "terraform")
# systemd-networkd DHCP server lease file (JSON). The legacy dnsmasq lease
# file at /var/lib/misc/dnsmasq/k8sbr0.leases is read as a fallback for
# deployments that still run dnsmasq as the DHCP server.
SYSTEMD_LEASES = "/var/lib/systemd/network/dhcp-server-lease/k8sbr0"
DNSMASQ_LEASES = "/var/lib/misc/dnsmasq/k8sbr0.leases"


def run_tofu() -> list[dict[str, str]]:
    """Query terraform/tofu output for node data.

    Returns a list of dicts with keys 'name', 'mac', and 'role', or an
    empty list when the output is unavailable.
    """
    for binary in ("tofu", "terraform"):
        try:
            subprocess.run(
                [binary, "--version"],
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            continue

        try:
            result = subprocess.run(
                [binary, "-chdir=" + TERRAFORM_DIR, "output", "-json", "nodes"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            continue

        if result.returncode != 0 or not result.stdout.strip():
            return []

        try:
            nodes = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        if not isinstance(nodes, list):
            return []

        return nodes

    return []


def read_leases(path: str) -> dict[str, str]:
    """Parse a dnsmasq lease file into a MAC-to-IP mapping.

    Lease line format::

        expiry mac ip hostname client-id

    Returns a dict mapping lower-case MAC addresses to IP strings.
    Returns an empty dict when the file cannot be read or is empty.
    """
    leases: dict[str, str] = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                mac = parts[1].lower()
                ip = parts[2]
                leases[mac] = ip
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return leases


def read_systemd_leases(path: str) -> dict[str, str]:
    """Parse the systemd-networkd DHCP server lease file into a MAC-to-IP map.

    The file is JSON with a ``Leases`` array; each lease carries
    ``HardwareAddress`` (byte list) and ``AddressString`` fields.
    Returns an empty dict when the file cannot be read or has no leases.
    """
    leases: dict[str, str] = {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        for lease in data.get("Leases", []):
            mac_bytes = lease.get("HardwareAddress", [])
            if len(mac_bytes) != 6:
                continue
            mac = ":".join(f"{b:02x}" for b in mac_bytes)
            ip = lease.get("AddressString", "")
            if ip:
                leases[mac] = ip
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        pass
    return leases


def build_inventory(
    nodes: list[dict[str, str]],
    leases: dict[str, str],
) -> dict[str, Any]:
    """Build the Ansible inventory dict matching the original bash script output.

    Groups are ``control_plane``, ``worker``, and ``cluster``.  The
    ``cluster`` group carries CIDR vars for pod, service, and load-balancer
    networks.
    """
    cp_hosts: dict[str, dict[str, str]] = {}
    worker_hosts: dict[str, dict[str, str]] = {}
    all_hosts: dict[str, dict[str, str]] = {}

    for node in nodes:
        name = node.get("name", "")
        mac = node.get("mac", "")
        role = node.get("role", "")
        if not name or not mac:
            continue

        ip = leases.get(mac.lower())
        if not ip:
            continue

        entry = {"ansible_host": ip}
        all_hosts[name] = entry
        if role == "control_plane":
            cp_hosts[name] = entry
        elif role == "worker":
            worker_hosts[name] = entry

    return {
        "control_plane": {
            "hosts": cp_hosts,
            "vars": {"node_role": "control_plane"},
        },
        "worker": {
            "hosts": worker_hosts,
            "vars": {"node_role": "worker"},
        },
        "cluster": {
            "hosts": all_hosts,
            "vars": {
                "pod_cidr": "10.244.0.0/16",
                "service_cidr": "10.96.0.0/12",
                "lb_pool_cidr": "10.0.10.0/24",
            },
        },
    }


def output_host(host_name: str) -> dict[str, str]:
    """Return static host vars for ``--host <HOST>``."""
    return {"ansible_user": "root", "node_role": "cluster"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate dynamic Ansible inventory for k8labs VMs.",
    )
    parser.add_argument("--list", action="store_true", help="Output full inventory")
    parser.add_argument("--host", metavar="HOST", help="Output vars for a host")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    if args.list:
        nodes = run_tofu()
        leases = read_systemd_leases(SYSTEMD_LEASES)
        if not leases:
            leases = read_leases(DNSMASQ_LEASES)
        inventory = build_inventory(nodes, leases)
        json.dump(inventory, sys.stdout, indent=2)
        print()
    elif args.host:
        host_vars = output_host(args.host)
        json.dump(host_vars, sys.stdout, indent=2)
        print()
    else:
        print(f"Usage: {sys.argv[0]} --list | --host <HOST>", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
