"""Unit tests for the linux-sched observer DaemonSet validator."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate import (  # noqa: E402
    REQUIRED_CAPABILITIES,
    load,
    validate,
)


def daemonset(**spec_overrides) -> dict:
    spec = {
        "hostPID": True,
        "hostNetwork": False,
        "hostIPC": False,
        "automountServiceAccountToken": False,
        "nodeSelector": {"linux-sched/research": "observer"},
        "containers": [
            {
                "name": "observer",
                "securityContext": {
                    "capabilities": {
                        "add": [
                            "BPF",
                            "PERFMON",
                            "SYS_ADMIN",
                            "SYS_RESOURCE",
                            "SYS_PTRACE",
                            "IPC_LOCK",
                        ]
                    }
                },
                "volumeMounts": [],
            }
        ],
        "volumes": [{"name": "btf", "hostPath": {"path": "/sys/kernel/btf"}}],
    }
    spec.update(spec_overrides)
    return {
        "__file__": "ds.yaml",
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": "linux-sched-observer", "namespace": "linux-sched"},
        "spec": {"template": {"spec": spec}},
    }


def namespace() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "linux-sched"},
    }


def test_shipped_manifest_validates() -> None:
    assert validate() == []


def test_shipped_manifest_is_a_capability_daemonset() -> None:
    docs = load()
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    spec = ds["spec"]["template"]["spec"]
    assert spec["hostPID"] is True
    assert "privileged" not in spec.get("securityContext", {})


def test_privileged_container_is_rejected() -> None:
    docs = [
        namespace(),
        daemonset(
            containers=[
                {
                    "name": "observer",
                    "securityContext": {"privileged": True},
                    "volumeMounts": [],
                }
            ]
        ),
    ]
    errors = validate(docs)
    assert any("is privileged" in e or "missing capabilities" in e for e in errors)


def test_missing_bpf_capability_is_rejected() -> None:
    docs = [
        namespace(),
        daemonset(
            containers=[
                {
                    "name": "observer",
                    "securityContext": {"capabilities": {"add": ["SYS_ADMIN"]}},
                    "volumeMounts": [],
                }
            ]
        ),
    ]
    errors = validate(docs)
    assert any("missing capabilities" in e for e in errors)


def test_missing_hostpid_is_rejected() -> None:
    docs = [namespace(), daemonset(hostPID=False)]
    errors = validate(docs)
    assert any("hostPID" in e for e in errors)


def test_host_network_is_rejected() -> None:
    docs = [namespace(), daemonset(hostNetwork=True)]
    errors = validate(docs)
    assert any("host network/IPC" in e for e in errors)


def test_unexpected_host_path_is_rejected() -> None:
    docs = [
        namespace(),
        daemonset(volumes=[{"name": "x", "hostPath": {"path": "/etc"}}]),
    ]
    errors = validate(docs)
    assert any("unexpected hostPath" in e for e in errors)


def test_host_root_is_rejected() -> None:
    docs = [namespace(), daemonset(volumes=[{"name": "x", "hostPath": {"path": "/"}}])]
    errors = validate(docs)
    assert any("hostPath /" in e for e in errors)


def test_required_capabilities_are_bpf_and_perfmon() -> None:
    assert REQUIRED_CAPABILITIES == {"BPF", "PERFMON"}
