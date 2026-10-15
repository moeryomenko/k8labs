from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_spec = importlib.util.spec_from_file_location(
    "lock", Path(__file__).parents[1] / "scripts/lock.py"
)
assert _spec and _spec.loader
lock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lock)


LOCK = Path(__file__).parents[1] / "versions.lock.yaml"


def test_repository_lock_is_valid_and_canonical() -> None:
    data = lock.load_lock(LOCK)
    encoded = lock.canonical_json(data)
    assert data["schema_version"] == 2
    assert b'"version":"v1.37.0"' in encoded
    assert b'"node_image":"kindest/node:v1.37.0@sha256:' in encoded


def test_mutable_reference_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "versions.yaml"
    path.write_text(
        "fedora:\n  kernel: 7.2.10-200.fc44\n"
        "kubernetes:\n  version: v1.37.0\n  cri_o: v1.37.0\n  crun: 1.29.1\n"
        "cilium:\n  version: v1.20.2\n"
        "cluster_api:\n  version: v1.14.2\n"
        "kind:\n  version: v0.33.0\n  node_image: kindest/node:latest@sha256:"
        + "0"
        * 64
        + "\npacker:\n  version: v1.16.1\n"
        "cloud_hypervisor:\n  version: v53.0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mutable reference"):
        lock.load_lock(path)


def test_lock_json_is_machine_readable() -> None:
    data = lock.load_lock(LOCK)
    assert json.loads(lock.canonical_json(data))["images"]["policy"] == "digest-only"


def test_provider_and_bootstrap_images_are_immutable() -> None:
    data = lock.load_lock(LOCK)
    provider = data["cluster_api_hypervisor"]
    assert provider["version"] == "v0.3.15"
    assert len(provider["commit"]) == 40
    assert provider["image"]["reference"].startswith(
        "ghcr.io/moeryomenko/cluster-api-hypervisor@sha256:"
    )
    assert {image["id"] for image in data["oci_images"]} >= {
        "kind-node",
        "cluster-api-hypervisor",
    }
    assert all("@sha256:" in image["reference"] for image in data["oci_images"])
    assert all(image["platform"] == "linux/amd64" for image in data["oci_images"])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sha256", "not-a-digest", "sha256"),
        ("url", "http://example.test/artifact", "HTTPS"),
        ("cache_path", "../escape", "relative path"),
    ],
)
def test_lock_rejects_invalid_artifact_entries(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    data = yaml.safe_load(LOCK.read_text(encoding="utf-8"))
    artifact = {
        "id": "test-artifact",
        "cache_path": "guest/test-artifact",
        "sha256": "a" * 64,
        "url": "https://example.test/artifact",
    }
    artifact[field] = value
    data["artifacts"].append(artifact)
    path = tmp_path / "versions.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        lock.load_lock(path)


def test_lock_rejects_duplicate_artifact_ids(tmp_path: Path) -> None:
    data = yaml.safe_load(LOCK.read_text(encoding="utf-8"))
    data["artifacts"].append(data["artifacts"][0] | {"cache_path": "duplicate/path"})
    path = tmp_path / "versions.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate artifact id"):
        lock.load_lock(path)


def test_lock_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    data = yaml.safe_load(LOCK.read_text(encoding="utf-8"))
    data["schema_version"] = 1
    path = tmp_path / "versions.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version 2"):
        lock.load_lock(path)


def test_guest_upgrade_matrix_pins_sequential_same_minor_inputs() -> None:
    data = lock.load_lock(LOCK)
    stages = data["kubernetes_upgrade_matrix"]
    assert [stage["kubernetes"].split(".")[1] for stage in stages] == [
        "32",
        "33",
        "34",
        "35",
        "36",
        "37",
    ]

    artifacts = {
        item["id"]: item for item in data["artifacts"] + data["guest_upgrade_artifacts"]
    }
    for stage in stages:
        assert stage["kubernetes"].split(".")[1] == stage["cri_o"].split(".")[1]
        assert artifacts[stage["kubelet_artifact"]]["version"] == stage["kubernetes"]
        assert (
            artifacts[stage["control_plane_artifact"]]["version"] == stage["kubernetes"]
        )
        assert artifacts[stage["cri_o_artifact"]]["version"] == stage["cri_o"]


def test_lock_rejects_crio_from_different_kubernetes_minor(tmp_path: Path) -> None:
    data = yaml.safe_load(LOCK.read_text(encoding="utf-8"))
    if "kubernetes_upgrade_matrix" not in data:
        data["kubernetes_upgrade_matrix"] = []
    data["kubernetes_upgrade_matrix"] = [
        {
            "kubernetes": "v1.32.13",
            "kubelet_artifact": "kubelet-v1.32.13",
            "control_plane_artifact": "kubernetes-server-v1.32.13",
            "cri_o": "v1.35.5",
            "cri_o_artifact": "cri-o-v1.35.5",
        }
    ]
    path = tmp_path / "versions.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ValueError, match="same minor"):
        lock.load_lock(path)
