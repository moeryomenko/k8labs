from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

SPEC = importlib.util.spec_from_file_location(
    "cache", Path(__file__).parents[1] / "scripts/cache.py"
)
assert SPEC and SPEC.loader
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


def base_lock() -> dict[str, object]:
    return {
        "fedora": {"kernel": "7.2.10"},
        "kubernetes": {"version": "v1", "cri_o": "v1", "crun": "1"},
        "cilium": {"version": "v1"},
        "cluster_api": {"version": "v1"},
        "kind": {"version": "v1", "node_image": "kindest/node:v1@sha256:" + "0" * 64},
        "packer": {"version": "v1"},
        "cloud_hypervisor": {"version": "v1"},
    }


def write_blob(layout: Path, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    blob = layout / "blobs" / "sha256" / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(payload)
    return f"sha256:{digest}"


def write_oci_layout(directory: Path, item: dict[str, str]) -> None:
    directory.mkdir(parents=True)
    (directory / "oci-layout").write_text(
        '{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8"
    )
    config_digest = write_blob(
        directory,
        json.dumps(
            {"os": "linux", "architecture": "amd64"}, separators=(",", ":")
        ).encode(),
    )
    layer_digest = write_blob(directory, b"layer")
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": 1,
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": layer_digest,
                    "size": 1,
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    manifest_digest = write_blob(directory, manifest)
    assert manifest_digest == item["manifest_digest"]
    (directory / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": manifest_digest,
                        "size": len(manifest),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_cache_validation_checks_artifact_digest(tmp_path: Path) -> None:
    payload = b"artifact"
    digest = hashlib.sha256(payload).hexdigest()
    lock = base_lock() | {
        "artifacts": [
            {
                "id": "sample",
                "cache_path": "files/sample",
                "sha256": digest,
                "url": "https://example.test/files/sample",
            }
        ]
    }
    root = tmp_path / "cache"
    directory = cache.write_metadata(lock, root)
    artifact = directory / "files/sample"
    artifact.parent.mkdir()
    artifact.write_bytes(payload)
    assert json.loads((directory / "complete.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "lock_sha256": cache.lock_digest(lock),
        "artifact_count": 1,
        "oci_image_count": 0,
    }
    assert cache.validate_cache(lock, root) == []
    artifact.write_bytes(b"tampered")
    assert any(
        "checksum mismatch" in issue for issue in cache.validate_cache(lock, root)
    )


def test_oci_layout_validation_rejects_tampering_and_wrong_platform(
    tmp_path: Path,
) -> None:
    manifest = (
        b'{"schemaVersion":2,"config":{"digest":"sha256:'
        + b"0" * 64
        + b'"},"layers":[]}'
    )
    digest = hashlib.sha256(manifest).hexdigest()
    lock = base_lock() | {
        "oci_images": [
            {
                "id": "manager",
                "cache_path": "oci/manager",
                "reference": f"registry.test/manager@sha256:{digest}",
                "platform": "linux/amd64",
            }
        ]
    }
    root = tmp_path / "cache"
    directory = cache.write_metadata(lock, root)
    item = cache.oci_inventory(lock)[0]
    # The helper builds a manifest whose digest is controlled by its serialized payload.
    # Replace its inventory digest with the manifest it creates for this isolated layout test.
    layout = directory / item["cache_path"]
    layout.mkdir(parents=True)
    (layout / "oci-layout").write_text(
        '{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8"
    )
    config = write_blob(layout, b'{"os":"linux","architecture":"amd64"}')
    layer = write_blob(layout, b"layer")
    actual_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "config": {"digest": config},
            "layers": [{"digest": layer}],
        },
        separators=(",", ":"),
    ).encode()
    actual_digest = write_blob(layout, actual_manifest)
    item["manifest_digest"] = actual_digest
    item["reference"] = f"registry.test/manager@{actual_digest}"
    (layout / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": [{"digest": actual_digest}]})
        + "\n",
        encoding="utf-8",
    )
    assert cache.validate_oci_layout(item, layout) == []
    (layout / "blobs" / "sha256" / actual_digest[7:]).write_bytes(b"tampered")
    assert any(
        "checksum mismatch" in issue
        for issue in cache.validate_oci_layout(item, layout)
    )


def test_populate_downloads_and_verifies_artifacts(tmp_path: Path) -> None:
    payload = b"artifact"
    digest = hashlib.sha256(payload).hexdigest()
    lock = base_lock() | {
        "artifacts": [
            {
                "id": "sample",
                "cache_path": "files/sample",
                "sha256": digest,
                "url": "https://example.test/files/sample",
            }
        ]
    }
    response = MagicMock()
    response.read.side_effect = [payload, b""]
    response.__enter__.return_value = response
    with patch.object(cache.request, "urlopen", return_value=response):
        directory = cache.populate(lock, tmp_path)
    assert (directory / "files/sample").read_bytes() == payload
    assert cache.validate_cache(lock, tmp_path) == []


def test_guest_upgrade_artifacts_have_a_separate_verified_cache(tmp_path: Path) -> None:
    payload = b"guest upgrade binary archive"
    digest = hashlib.sha256(payload).hexdigest()
    lock = base_lock() | {
        "artifacts": [],
        "guest_upgrade_artifacts": [
            {
                "id": "kubelet-v1.32.13",
                "component": "kubelet",
                "version": "v1.32.13",
                "cache_path": "guest-upgrade/v1.32.13/kubelet",
                "sha256": digest,
                "url": "https://example.test/kubelet",
            }
        ],
    }
    response = MagicMock()
    response.read.side_effect = [payload, b""]
    response.__enter__.return_value = response

    with patch.object(cache.request, "urlopen", return_value=response):
        directory = cache.populate_guest_upgrade_artifacts(lock, tmp_path)

    artifact = directory / "guest-upgrade/v1.32.13/kubelet"
    assert artifact.read_bytes() == payload
    assert cache.validate_guest_upgrade_cache(lock, tmp_path) == []
    artifact.write_bytes(b"tampered guest upgrade artifact")
    assert any(
        "checksum mismatch" in problem
        for problem in cache.validate_guest_upgrade_cache(lock, tmp_path)
    )


def test_guest_upgrade_artifacts_do_not_enter_management_inventory() -> None:
    lock = base_lock() | {
        "artifacts": [
            {
                "id": "management-manifest",
                "cache_path": "manifests/management.yaml",
                "sha256": "a" * 64,
                "url": "https://example.test/management.yaml",
            }
        ],
        "guest_upgrade_artifacts": [
            {
                "id": "guest-server",
                "component": "kubernetes-control-plane",
                "version": "v1.32.13",
                "cache_path": "guest-upgrade/v1.32.13/server.tar.gz",
                "sha256": "b" * 64,
                "url": "https://example.test/server.tar.gz",
            }
        ],
    }

    assert [item["id"] for item in cache.inventory(lock)] == ["management-manifest"]
    assert [item["id"] for item in cache.guest_upgrade_inventory(lock)] == [
        "guest-server"
    ]


def test_kubernetes_upgrade_inventory_selects_matrix_only() -> None:
    lock = yaml.safe_load(
        (Path(__file__).parents[1] / "versions.lock.yaml").read_text(encoding="utf-8")
    )

    stages = cache.kubernetes_upgrade_inventory(lock)
    stage_ids = {item["id"] for item in stages}

    assert len(stages) == 18
    assert "kubelet-v1.32.13" in stage_ids
    assert "kubernetes-server-v1.37.0" in stage_ids
    assert "cri-o-v1.37.0" in stage_ids
    assert len(cache.inventory(lock)) == 3


def test_kubernetes_upgrade_population_does_not_download_shared_guest_artifacts(
    tmp_path: Path,
) -> None:
    lock = yaml.safe_load(
        (Path(__file__).parents[1] / "versions.lock.yaml").read_text(encoding="utf-8")
    )
    downloaded: list[str] = []

    def record_download(item: dict[str, object], _directory: Path) -> None:
        downloaded.append(str(item["id"]))

    with patch.object(cache, "_download", side_effect=record_download):
        cache.populate_kubernetes_upgrade_artifacts(lock, tmp_path)

    assert len(downloaded) == 18
    assert "kubelet-v1.32.13" in downloaded
    assert "kubernetes-server-v1.37.0" in downloaded
    assert "cri-o-v1.37.0" in downloaded
    assert "crun-1.29.1" not in downloaded
    assert "etcd-v3.5.17" not in downloaded


def test_oci_inventory_rejects_tag_only_reference() -> None:
    lock = base_lock() | {
        "oci_images": [
            {
                "id": "manager",
                "cache_path": "oci/manager",
                "reference": "registry.test/manager:latest",
                "platform": "linux/amd64",
            }
        ]
    }
    try:
        cache.oci_inventory(lock)
    except ValueError as error:
        assert "sha256" in str(error)
    else:
        raise AssertionError("tag-only image references must be rejected")


def test_prune_preserves_active_lock_only(tmp_path: Path) -> None:
    lock = base_lock()
    locks = tmp_path / "locks"
    active = locks / cache.lock_digest(lock)
    stale = locks / "stale"
    active.mkdir(parents=True)
    stale.mkdir(parents=True)
    removed = cache.prune(lock, tmp_path)
    assert removed == [stale]
    assert active.is_dir()
