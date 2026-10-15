"""Unit tests for the linux-sched telemetry manifest validator."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validate import TELEMETRY_DIR, load_documents, validate  # noqa: E402


def namespace(name: str = "research-telemetry", privileged: bool = True) -> dict:
    return {
        "__file__": "ns.yaml",
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {
                "pod-security.kubernetes.io/enforce": (
                    "privileged" if privileged else "baseline"
                )
            },
        },
    }


def daemonset(
    *,
    namespace_name: str = "research-telemetry",
    volumes: list[dict] | None = None,
    containers: list[dict] | None = None,
    init_containers: list[dict] | None = None,
) -> dict:
    spec: dict = {
        "nodeSelector": {"linux-sched/research": "observer"},
        "hostNetwork": False,
        "hostIPC": False,
        "automountServiceAccountToken": False,
        "containers": containers
        if containers is not None
        else [{"name": "c", "volumeMounts": []}],
        "volumes": volumes
        if volumes is not None
        else [{"name": "proc", "hostPath": {"path": "/proc", "type": "Directory"}}],
    }
    if init_containers is not None:
        spec["initContainers"] = init_containers
    return {
        "__file__": "ds.yaml",
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": "ds", "namespace": namespace_name},
        "spec": {"template": {"spec": spec}},
    }


def test_shipped_manifests_validate() -> None:
    assert validate() == []


def test_shipped_manifests_are_discovered() -> None:
    docs = load_documents(TELEMETRY_DIR)
    assert any(d["kind"] == "DaemonSet" for d in docs)
    assert any(d["kind"] == "Namespace" for d in docs)


def test_writable_host_mount_is_rejected() -> None:
    docs = [
        namespace(),
        daemonset(
            containers=[
                {
                    "name": "c",
                    "volumeMounts": [
                        {"name": "proc", "mountPath": "/host/proc", "readOnly": False}
                    ],
                }
            ]
        ),
    ]
    errors = validate(docs)
    assert any("must be readOnly" in e for e in errors)


def test_init_container_mounting_root_is_rejected() -> None:
    docs = [
        namespace(),
        daemonset(
            init_containers=[
                {
                    "name": "init",
                    "volumeMounts": [
                        {"name": "root", "mountPath": "/", "readOnly": True}
                    ],
                }
            ],
            volumes=[{"name": "root", "hostPath": {"path": "/", "type": "Directory"}}],
        ),
    ]
    errors = validate(docs)
    assert any("mounts host root" in e for e in errors)
    assert any("volume uses hostPath /" in e for e in errors)


def test_unexpected_host_path_is_rejected() -> None:
    docs = [
        namespace(),
        daemonset(
            volumes=[
                {"name": "etc", "hostPath": {"path": "/etc", "type": "Directory"}}
            ],
        ),
    ]
    errors = validate(docs)
    assert any("unexpected hostPath" in e for e in errors)


def test_daemonset_in_unprivileged_namespace_is_rejected() -> None:
    docs = [namespace("other", privileged=False), daemonset(namespace_name="other")]
    errors = validate(docs)
    assert any("not one of the validated privileged namespaces" in e for e in errors)


def test_daemonset_outside_validated_namespace_is_rejected() -> None:
    docs = [namespace(), daemonset(namespace_name="default")]
    errors = validate(docs)
    assert any("not one of the validated privileged namespaces" in e for e in errors)
