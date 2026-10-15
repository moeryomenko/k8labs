"""Validate and canonicalize the k8labs immutable dependency lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

REQUIRED_PATHS = (
    ("fedora", "kernel"),
    ("kubernetes", "version"),
    ("kubernetes", "cri_o"),
    ("kubernetes", "crun"),
    ("cilium", "version"),
    ("cluster_api", "version"),
    ("kind", "version"),
    ("kind", "node_image"),
    ("packer", "version"),
    ("cloud_hypervisor", "version"),
)
LOCK_SCHEMA_VERSION = 2
MUTABLE_REFERENCE = re.compile(
    r"(?:^|[/:@])(?:latest|main|master)(?:$|[/:@])", re.IGNORECASE
)
DIGEST_REFERENCE = re.compile(r"@sha256:[0-9a-f]{64}$")
KUBERNETES_VERSION = re.compile(r"^v1\.(\d+)\.(\d+)$")


def load_lock(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("lock must contain a mapping")
    for keys in REQUIRED_PATHS:
        value: Any = data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                raise ValueError(f"missing lock field: {'.'.join(keys)}")
            value = value[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"lock field must be a non-empty string: {'.'.join(keys)}")
    walk_mutable(data, "")
    if data.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise ValueError(f"lock.schema_version {LOCK_SCHEMA_VERSION} is required")
    node_image = data["kind"]["node_image"]
    if not DIGEST_REFERENCE.search(node_image):
        raise ValueError("kind.node_image must be pinned by a sha256 digest")
    provider = data.get("cluster_api_hypervisor")
    if provider is None:
        raise ValueError("missing lock field: cluster_api_hypervisor")
    if not isinstance(provider, dict):
        raise TypeError("cluster_api_hypervisor must be a mapping")
    if provider.get("version") != "v0.3.15":
        raise ValueError("cluster_api_hypervisor.version must be v0.3.15")
    commit = provider.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(
            "cluster_api_hypervisor.commit must be a full immutable Git commit"
        )
    source = provider.get("source")
    if not isinstance(source, dict):
        raise TypeError("cluster_api_hypervisor.source must be a mapping")
    if not isinstance(source.get("url"), str) or not isinstance(
        source.get("sha256"), str
    ):
        raise TypeError("cluster_api_hypervisor.source requires string url and sha256")
    image = provider.get("image")
    if not isinstance(image, dict):
        raise TypeError("cluster_api_hypervisor.image must be a mapping")
    if not isinstance(image.get("reference"), str):
        raise TypeError("cluster_api_hypervisor.image.reference must be a string")
    if not DIGEST_REFERENCE.search(image["reference"]):
        raise ValueError(
            "cluster_api_hypervisor.image.reference must be pinned by a sha256 digest"
        )
    images = data.get("oci_images")
    if not isinstance(images, list):
        raise TypeError("lock.oci_images must be a list")
    for index, entry in enumerate(images):
        if not isinstance(entry, dict):
            raise TypeError(f"lock.oci_images[{index}] must be a mapping")
        if not isinstance(entry.get("reference"), str):
            raise TypeError(f"lock.oci_images[{index}].reference must be a string")
        if not DIGEST_REFERENCE.search(entry["reference"]):
            raise ValueError(
                f"lock.oci_images[{index}].reference must be pinned by a sha256 digest"
            )
        if entry.get("platform") != "linux/amd64":
            raise ValueError(f"lock.oci_images[{index}].platform must be linux/amd64")
    artifacts = validate_artifacts(data)
    validate_upgrade_matrix(data, artifacts)
    return data


def validate_artifacts(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifact_groups = {
        "artifacts": data.get("artifacts"),
        "guest_upgrade_artifacts": data.get("guest_upgrade_artifacts", []),
    }
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    result: dict[str, dict[str, Any]] = {}
    for group_name, group in artifact_groups.items():
        if not isinstance(group, list):
            raise TypeError(f"lock.{group_name} must be a list")
        for index, artifact in enumerate(group):
            location = f"lock.{group_name}[{index}]"
            if not isinstance(artifact, dict):
                raise TypeError(f"{location} must be a mapping")

            artifact_id = artifact.get("id")
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise ValueError(f"{location}.id must be a non-empty string")
            if artifact_id in seen_ids:
                raise ValueError(f"duplicate artifact id: {artifact_id}")
            seen_ids.add(artifact_id)

            cache_path = artifact.get("cache_path")
            if not isinstance(cache_path, str) or not cache_path.strip():
                raise ValueError(
                    f"{location}.cache_path must be a non-empty relative path"
                )
            relative_path = Path(cache_path)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ValueError(
                    f"{location}.cache_path must be a relative path and stay inside cache"
                )
            if cache_path in seen_paths:
                raise ValueError(f"duplicate artifact cache path: {cache_path}")
            seen_paths.add(cache_path)

            sha256 = artifact.get("sha256")
            if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError(
                    f"{location}.sha256 must be a lowercase hexadecimal SHA-256 digest"
                )

            url = artifact.get("url")
            parsed_url = urlparse(url) if isinstance(url, str) else None
            if (
                parsed_url is None
                or parsed_url.scheme != "https"
                or not parsed_url.netloc
            ):
                raise ValueError(f"{location}.url must use HTTPS")

            for field in ("component", "version"):
                if field in artifact and (
                    not isinstance(artifact[field], str) or not artifact[field].strip()
                ):
                    raise ValueError(f"{location}.{field} must be a non-empty string")
            result[artifact_id] = artifact
    return result


def validate_upgrade_matrix(
    data: dict[str, Any], artifacts: dict[str, dict[str, Any]]
) -> None:
    matrix = data.get("kubernetes_upgrade_matrix")
    if not isinstance(matrix, list) or not matrix:
        raise ValueError("lock.kubernetes_upgrade_matrix must be a non-empty list")

    expected_components = {
        "kubelet_artifact": "kubelet",
        "control_plane_artifact": "kubernetes-control-plane",
        "cri_o_artifact": "cri-o",
    }
    stage_minors: list[int] = []
    for index, stage in enumerate(matrix):
        location = f"lock.kubernetes_upgrade_matrix[{index}]"
        if not isinstance(stage, dict):
            raise TypeError(f"{location} must be a mapping")
        versions = {}
        for field in ("kubernetes", "cri_o"):
            value = stage.get(field)
            match = (
                KUBERNETES_VERSION.fullmatch(value) if isinstance(value, str) else None
            )
            if match is None:
                raise ValueError(
                    f"{location}.{field} must be a v1.<minor>.<patch> version"
                )
            versions[field] = (int(match.group(1)), value)
        kubernetes_minor, kubernetes_version = versions["kubernetes"]
        crio_minor, crio_version = versions["cri_o"]
        if kubernetes_minor != crio_minor:
            raise ValueError(
                f"{location} Kubernetes and CRI-O versions must use the same minor"
            )
        stage_minors.append(kubernetes_minor)

        for reference_field, component in expected_components.items():
            artifact_id = stage.get(reference_field)
            artifact = (
                artifacts.get(artifact_id) if isinstance(artifact_id, str) else None
            )
            if artifact is None:
                raise ValueError(
                    f"{location}.{reference_field} must reference a locked artifact"
                )
            expected_version = (
                crio_version
                if reference_field == "cri_o_artifact"
                else kubernetes_version
            )
            if (
                artifact.get("component") != component
                or artifact.get("version") != expected_version
            ):
                raise ValueError(
                    f"{location}.{reference_field} must reference {component} {expected_version}"
                )

    target_version = data["kubernetes"]["version"]
    target_match = KUBERNETES_VERSION.fullmatch(target_version)
    if target_match is None:
        raise ValueError("kubernetes.version must be a v1.<minor>.<patch> version")
    first_minor = stage_minors[0]
    target_minor = int(target_match.group(1))
    expected_minors = list(range(first_minor, target_minor + 1))
    if stage_minors != expected_minors:
        raise ValueError(
            "lock.kubernetes_upgrade_matrix must include each Kubernetes minor exactly once"
        )
    last_stage = matrix[-1]
    if (
        last_stage["kubernetes"] != target_version
        or last_stage["cri_o"] != data["kubernetes"]["cri_o"]
    ):
        raise ValueError(
            "last upgrade matrix stage must match the locked target versions"
        )


def walk_mutable(value: Any, location: str) -> None:
    if isinstance(value, str) and MUTABLE_REFERENCE.search(value):
        raise ValueError(f"mutable reference at {location or '<root>'}: {value}")
    if isinstance(value, dict):
        for key, child in value.items():
            walk_mutable(child, f"{location}.{key}" if location else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            walk_mutable(child, f"{location}[{index}]")


def canonical_json(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=Path("versions.lock.yaml"))
    parser.add_argument("--json", action="store_true", dest="emit_json")
    args = parser.parse_args()
    try:
        data = load_lock(args.lock)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    payload = canonical_json(data)
    digest = hashlib.sha256(payload).hexdigest()
    if args.emit_json:
        print(payload.decode(), end="")
    else:
        print(f"lock_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
