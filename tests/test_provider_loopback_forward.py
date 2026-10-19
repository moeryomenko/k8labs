"""Unit tests for the provider loopback-forward workaround."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_loopback_forward import (
    HOST_GATEWAY,
    LOOPBACK_VM_PORTS,
    build_socat_command,
)


def test_build_socat_command_forwards_apiserver_and_etcd() -> None:
    command = build_socat_command({6443: 20009, 2379: 20011})
    assert command[0] == "sh" and command[1] == "-c"
    forwards = command[2]
    assert f"TCP-LISTEN:20009,fork,reuseaddr TCP:{HOST_GATEWAY}:20009" in forwards
    assert f"TCP-LISTEN:20011,fork,reuseaddr TCP:{HOST_GATEWAY}:20011" in forwards
    assert forwards.endswith("& wait")


def test_build_socat_command_rejects_missing_port() -> None:
    with pytest.raises(ValueError, match="vmPort 2379"):
        build_socat_command({6443: 20009})


def test_loopback_ports_are_the_ones_the_provider_dials() -> None:
    assert LOOPBACK_VM_PORTS == (6443, 2379)


def test_gateway_matches_the_coredns_host_alias() -> None:
    # CoreDNS maps host.containers.internal to this address; the sidecar must
    # forward to the same reachable host endpoint.
    assert HOST_GATEWAY == "169.254.1.2"
