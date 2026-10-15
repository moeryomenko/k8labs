#!/usr/bin/env python3
"""Validate and maintain a lock-keyed k8labs artifact and OCI-image cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib import error, parse, request

try:
    from lock import canonical_json, load_lock
except ModuleNotFoundError:
    from scripts.lock import canonical_json, load_lock


HEX_DIGEST_LENGTH = 64
OCI_LAYOUT_VERSION = 1


def lock_digest(lock: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(lock)).hexdigest()


def cache_dir(lock: dict[str, Any], root: Path) -> Path:
    return root / "locks" / lock_digest(lock)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == HEX_DIGEST_LENGTH
        and all(char in "0123456789abcdef" for char in value.lower())
    )


def _safe_cache_path(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{location} must stay inside the cache")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_inventory(lock: dict[str, Any], group: str) -> list[dict[str, Any]]:
    artifacts = lock.get(group, [])
    if not isinstance(artifacts, list):
        raise TypeError(f"lock.{group} must be a list")
    result: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts):
        location = f"lock.{group}[{index}]"
        if not isinstance(artifact, dict):
            raise TypeError(f"{location} must be a mapping")
        artifact_id = artifact.get("id")
        relative_path = _safe_cache_path(
            artifact.get("cache_path"), f"{location}.cache_path"
        )
        digest = artifact.get("sha256")
        url = artifact.get("url")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError(f"{location}.id must be a non-empty string")
        if not _is_sha256(digest):
            raise ValueError(f"{location}.sha256 must be a hexadecimal SHA-256 digest")
        if not isinstance(url, str) or parse.urlparse(url).scheme != "https":
            raise ValueError(f"{location}.url must use HTTPS")
        result.append(
            {
                "id": artifact_id,
                "cache_path": relative_path,
                "sha256": digest.lower(),
                "url": url,
            }
        )
    return result


def inventory(lock: dict[str, Any]) -> list[dict[str, Any]]:
    """Return artifacts required by the normal management/bootstrap cache."""
    return _artifact_inventory(lock, "artifacts")


def guest_upgrade_inventory(lock: dict[str, Any]) -> list[dict[str, Any]]:
    """Return version-specific guest artifacts, separate from bootstrap inputs."""
    return _artifact_inventory(lock, "guest_upgrade_artifacts")


def kubernetes_upgrade_inventory(lock: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only kubelet, control-plane, and CRI-O artifacts in the stage matrix."""
    inventory(lock)
    guest_upgrade_inventory(lock)
    normal_artifacts = lock.get("artifacts", [])
    guest_artifacts = lock.get("guest_upgrade_artifacts", [])
    if not isinstance(normal_artifacts, list) or not isinstance(guest_artifacts, list):
        raise TypeError("lock artifact groups must be lists")
    matrix = lock.get("kubernetes_upgrade_matrix")
    if not isinstance(matrix, list) or not matrix:
        raise ValueError("lock.kubernetes_upgrade_matrix must be a non-empty list")

    by_id = {
        artifact["id"]: artifact
        for artifact in normal_artifacts + guest_artifacts
        if isinstance(artifact, dict) and isinstance(artifact.get("id"), str)
    }
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, stage in enumerate(matrix):
        if not isinstance(stage, dict):
            raise TypeError(
                f"lock.kubernetes_upgrade_matrix[{index}] must be a mapping"
            )
        for field in ("kubelet_artifact", "control_plane_artifact", "cri_o_artifact"):
            artifact_id = stage.get(field)
            if not isinstance(artifact_id, str) or artifact_id not in by_id:
                raise ValueError(
                    f"lock.kubernetes_upgrade_matrix[{index}].{field} must reference a locked artifact"
                )
            if artifact_id not in seen_ids:
                selected.append(by_id[artifact_id])
                seen_ids.add(artifact_id)
    return _artifact_inventory({"matrix_artifacts": selected}, "matrix_artifacts")


def oci_inventory(lock: dict[str, Any]) -> list[dict[str, Any]]:
    images = lock.get("oci_images", [])
    if not isinstance(images, list):
        raise TypeError("lock.oci_images must be a list")
    result: list[dict[str, Any]] = []
    for index, image in enumerate(images):
        location = f"lock.oci_images[{index}]"
        if not isinstance(image, dict):
            raise TypeError(f"{location} must be a mapping")
        image_id = image.get("id")
        relative_path = _safe_cache_path(
            image.get("cache_path"), f"{location}.cache_path"
        )
        reference = image.get("reference")
        platform = image.get("platform")
        if not isinstance(image_id, str) or not image_id:
            raise ValueError(f"{location}.id must be a non-empty string")
        if not isinstance(reference, str) or "@sha256:" not in reference:
            raise ValueError(f"{location}.reference must be pinned by a sha256 digest")
        repository, separator, digest = reference.rpartition("@sha256:")
        if not repository or not separator or not _is_sha256(digest):
            raise ValueError(f"{location}.reference must end with a full sha256 digest")
        repository_path, separator, repository_name = repository.rpartition("/")
        if ":" in repository_name:
            repository_name = repository_name.rsplit(":", 1)[0]
        repository = (
            f"{repository_path}/{repository_name}" if separator else repository_name
        )
        if not isinstance(platform, str) or platform.count("/") != 1:
            raise ValueError(f"{location}.platform must be OS/architecture")
        result.append(
            {
                "id": image_id,
                "cache_path": relative_path,
                "reference": f"{repository}@sha256:{digest.lower()}",
                "manifest_digest": f"sha256:{digest.lower()}",
                "platform": platform,
            }
        )
    return result


def _read_json(path: Path, description: str, problems: list[str]) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        problems.append(f"invalid {description}: {exc}")
        return None


def _validate_blob(
    root: Path, digest: object, description: str, problems: list[str]
) -> Path | None:
    if (
        not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or not _is_sha256(digest[7:])
    ):
        problems.append(f"invalid OCI digest for {description}: {digest!r}")
        return None
    blob = root / "blobs" / "sha256" / digest[7:]
    if not blob.is_file():
        problems.append(f"missing OCI blob for {description}: {digest}")
        return None
    if _sha256_file(blob) != digest[7:]:
        problems.append(f"checksum mismatch for OCI blob {description}: {digest}")
        return None
    return blob


def validate_oci_layout(item: dict[str, Any], directory: Path) -> list[str]:
    problems: list[str] = []
    layout = _read_json(
        directory / "oci-layout", f"OCI layout marker for {item['id']}", problems
    )
    if layout != {"imageLayoutVersion": "1.0.0"}:
        problems.append(f"invalid OCI layout marker for {item['id']}")
    index = _read_json(
        directory / "index.json", f"OCI index for {item['id']}", problems
    )
    if not isinstance(index, dict):
        return problems
    descriptors = index.get("manifests")
    if (
        not isinstance(descriptors, list)
        or len(descriptors) != 1
        or not isinstance(descriptors[0], dict)
    ):
        problems.append(f"OCI index for {item['id']} must contain exactly one manifest")
        return problems
    descriptor = descriptors[0]
    digest = descriptor.get("digest")
    if digest != item["manifest_digest"]:
        problems.append(
            f"OCI manifest digest mismatch for {item['id']}: "
            f"got {digest!r}, want {item['manifest_digest']}"
        )
    manifest_path = _validate_blob(
        directory, digest, f"manifest {item['id']}", problems
    )
    if manifest_path is None:
        return problems
    manifest = _read_json(manifest_path, f"OCI manifest for {item['id']}", problems)
    if not isinstance(manifest, dict):
        return problems
    config = manifest.get("config")
    if not isinstance(config, dict):
        problems.append(f"OCI manifest for {item['id']} has no config descriptor")
        return problems
    config_path = _validate_blob(
        directory, config.get("digest"), f"config {item['id']}", problems
    )
    for layer_index, layer in enumerate(manifest.get("layers", [])):
        if not isinstance(layer, dict):
            problems.append(
                f"invalid OCI layer descriptor {layer_index} for {item['id']}"
            )
            continue
        _validate_blob(
            directory,
            layer.get("digest"),
            f"layer {layer_index} for {item['id']}",
            problems,
        )
    if config_path is None:
        return problems
    config_data = _read_json(config_path, f"OCI config for {item['id']}", problems)
    if not isinstance(config_data, dict):
        return problems
    if f"{config_data.get('os')}/{config_data.get('architecture')}" != item["platform"]:
        problems.append(
            f"OCI platform mismatch for {item['id']}: "
            f"got {config_data.get('os')}/{config_data.get('architecture')}, want {item['platform']}"
        )
    return problems


def validate_cache(lock: dict[str, Any], root: Path) -> list[str]:
    directory = cache_dir(lock, root)
    problems: list[str] = []
    if not directory.is_dir():
        return [f"cache directory is missing: {directory}"]
    for name in (
        "lock.json",
        "checksums.txt",
        "artifacts.json",
        "oci-images.json",
        "complete.json",
    ):
        if not (directory / name).is_file():
            problems.append(f"missing cache metadata: {name}")
    if problems:
        return problems
    if _read_json(directory / "lock.json", "lock.json", problems) != lock:
        problems.append("lock.json does not match the active lock")
    expected_artifacts = inventory(lock)
    if (
        _read_json(directory / "artifacts.json", "artifacts.json", problems)
        != expected_artifacts
    ):
        problems.append("artifacts.json does not match the active lock inventory")
    expected_images = oci_inventory(lock)
    if (
        _read_json(directory / "oci-images.json", "oci-images.json", problems)
        != expected_images
    ):
        problems.append("oci-images.json does not match the active lock inventory")
    checksums = (directory / "checksums.txt").read_text(encoding="utf-8").splitlines()
    expected_lines = [
        f"{item['sha256']}  {item['cache_path']}" for item in expected_artifacts
    ]
    if checksums != expected_lines:
        problems.append("checksums.txt does not match the active lock inventory")
    for item in expected_artifacts:
        artifact = directory / item["cache_path"]
        if not artifact.is_file():
            problems.append(f"missing artifact: {item['id']} ({item['cache_path']})")
        elif _sha256_file(artifact) != item["sha256"]:
            problems.append(f"checksum mismatch: {item['id']} ({item['cache_path']})")
    for item in expected_images:
        problems.extend(validate_oci_layout(item, directory / item["cache_path"]))
    complete = _read_json(directory / "complete.json", "complete.json", problems)
    if (
        not isinstance(complete, dict)
        or complete.get("lock_sha256") != lock_digest(lock)
        or complete.get("artifact_count") != len(expected_artifacts)
        or complete.get("oci_image_count") != len(expected_images)
    ):
        problems.append("complete.json does not describe the active lock")
    return problems


def _write_json(path: Path, data: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_metadata(lock: dict[str, Any], root: Path) -> Path:
    directory = cache_dir(lock, root)
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = inventory(lock)
    images = oci_inventory(lock)
    temporary = directory / ".lock.json.tmp"
    temporary.write_bytes(canonical_json(lock))
    os.replace(temporary, directory / "lock.json")
    _write_json(directory / "artifacts.json", artifacts)
    _write_json(directory / "oci-images.json", images)
    checksums = "".join(
        f"{item['sha256']}  {item['cache_path']}\n" for item in artifacts
    )
    temporary = directory / ".checksums.txt.tmp"
    temporary.write_text(checksums, encoding="utf-8")
    os.replace(temporary, directory / "checksums.txt")
    _write_json(
        directory / "complete.json",
        {
            "schema_version": OCI_LAYOUT_VERSION,
            "lock_sha256": lock_digest(lock),
            "artifact_count": len(artifacts),
            "oci_image_count": len(images),
        },
    )
    return directory


def _download(item: dict[str, Any], directory: Path) -> None:
    destination = directory / item["cache_path"]
    if destination.is_file() and _sha256_file(destination) == item["sha256"]:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    digest = hashlib.sha256()
    try:
        with (
            request.urlopen(item["url"], timeout=60) as response,
            temporary.open("wb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
    except (OSError, error.URLError) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"failed to download {item['id']}: {exc}") from exc
    if digest.hexdigest() != item["sha256"]:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch while downloading {item['id']}")
    os.replace(temporary, destination)


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"command failed ({' '.join(command)}): {detail}") from exc


def populate_oci_image(item: dict[str, Any], directory: Path) -> None:
    destination = directory / item["cache_path"]
    if destination.is_dir() and not validate_oci_layout(item, destination):
        return
    stage_parent = directory / ".staging"
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{item['id']}-", dir=stage_parent)) / "layout"
    try:
        os_name, architecture = item["platform"].split("/", 1)
        _run(
            [
                "skopeo",
                "copy",
                "--override-os",
                os_name,
                "--override-arch",
                architecture,
                f"docker://{item['reference']}",
                f"oci:{stage}:{item['id']}",
            ]
        )
        problems = validate_oci_layout(item, stage)
        if problems:
            raise RuntimeError("; ".join(problems))
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, destination)
    finally:
        shutil.rmtree(stage.parent, ignore_errors=True)


def populate(lock: dict[str, Any], root: Path) -> Path:
    """Populate every file and OCI image entry before publishing completion metadata."""
    directory = cache_dir(lock, root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "complete.json").unlink(missing_ok=True)
    for item in inventory(lock):
        _download(item, directory)
    for item in oci_inventory(lock):
        populate_oci_image(item, directory)
    return write_metadata(lock, root)


def validate_guest_upgrade_cache(lock: dict[str, Any], root: Path) -> list[str]:
    """Check stage-specific guest artifacts without requiring them for bootstrap."""
    directory = cache_dir(lock, root)
    problems: list[str] = []
    for item in guest_upgrade_inventory(lock):
        artifact = directory / item["cache_path"]
        if not artifact.is_file():
            problems.append(
                f"missing guest upgrade artifact: {item['id']} ({item['cache_path']})"
            )
        elif _sha256_file(artifact) != item["sha256"]:
            problems.append(f"checksum mismatch: {item['id']} ({item['cache_path']})")
    return problems


def validate_kubernetes_upgrade_cache(lock: dict[str, Any], root: Path) -> list[str]:
    """Check only the kubelet, control-plane, and CRI-O matrix artifacts."""
    directory = cache_dir(lock, root)
    problems: list[str] = []
    for item in kubernetes_upgrade_inventory(lock):
        artifact = directory / item["cache_path"]
        if not artifact.is_file():
            problems.append(
                f"missing Kubernetes upgrade artifact: {item['id']} ({item['cache_path']})"
            )
        elif _sha256_file(artifact) != item["sha256"]:
            problems.append(f"checksum mismatch: {item['id']} ({item['cache_path']})")
    return problems


def populate_guest_upgrade_artifacts(lock: dict[str, Any], root: Path) -> Path:
    """Download only stage-specific guest artifacts after verifying their locked hashes."""
    directory = cache_dir(lock, root)
    for item in guest_upgrade_inventory(lock):
        _download(item, directory)
    return directory


def populate_kubernetes_upgrade_artifacts(lock: dict[str, Any], root: Path) -> Path:
    """Download only the locked kubelet, control-plane, and CRI-O stage artifacts."""
    directory = cache_dir(lock, root)
    for item in kubernetes_upgrade_inventory(lock):
        _download(item, directory)
    return directory


def prune(lock: dict[str, Any], root: Path) -> list[Path]:
    locks = root / "locks"
    active = lock_digest(lock)
    removed: list[Path] = []
    if not locks.is_dir():
        return removed
    for candidate in locks.iterdir():
        if candidate.is_dir() and candidate.name != active:
            shutil.rmtree(candidate)
            removed.append(candidate)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=Path("versions.lock.yaml"))
    parser.add_argument("--cache-root", type=Path, default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--populate", action="store_true")
    parser.add_argument("--check-upgrade-artifacts", action="store_true")
    parser.add_argument("--populate-upgrade-artifacts", action="store_true")
    parser.add_argument("--write-metadata", action="store_true")
    parser.add_argument("--prune", action="store_true")
    args = parser.parse_args()
    lock = load_lock(args.lock)
    root = (
        args.cache_root
        or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "k8labs"
    )
    if args.populate:
        if os.environ.get("OFFLINE", "0") == "1":
            parser.error("OFFLINE=1 forbids cache population from the network")
        try:
            print(populate(lock, root))
        except RuntimeError as exc:
            parser.error(str(exc))
    if args.populate_upgrade_artifacts:
        if os.environ.get("OFFLINE", "0") == "1":
            parser.error("OFFLINE=1 forbids Kubernetes upgrade artifact downloads")
        try:
            print(populate_kubernetes_upgrade_artifacts(lock, root))
        except RuntimeError as exc:
            parser.error(str(exc))
    elif args.write_metadata:
        print(write_metadata(lock, root))
    if args.prune:
        for path in prune(lock, root):
            print(f"removed {path}")
    normal_check = args.check or not (
        args.write_metadata
        or args.prune
        or args.populate_upgrade_artifacts
        or args.check_upgrade_artifacts
    )
    if normal_check:
        problems = validate_cache(lock, root)
        if problems:
            for problem in problems:
                print(f"ERROR: {problem}")
            return 1
        print(f"artifact cache ready: {cache_dir(lock, root)}")
    if args.check_upgrade_artifacts:
        problems = validate_kubernetes_upgrade_cache(lock, root)
        if problems:
            for problem in problems:
                print(f"ERROR: {problem}")
            return 1
        print(f"guest upgrade artifacts ready: {cache_dir(lock, root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
