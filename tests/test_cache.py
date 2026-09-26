from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

SPEC = importlib.util.spec_from_file_location(
    "cache", Path(__file__).parents[1] / "scripts/cache.py"
)
assert SPEC and SPEC.loader
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


def test_cache_validation_checks_artifact_digest(tmp_path: Path) -> None:
    payload = b"artifact"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    lock = {
        "fedora": {"kernel": "7.2.5"},
        "kubernetes": {"version": "v1", "cri_o": "v1", "crun": "1"},
        "cilium": {"version": "v1"},
        "cluster_api": {"version": "v1"},
        "kind": {"version": "v1", "node_image": "kindest/node:v1@sha256:" + "0" * 64},
        "packer": {"version": "v1"},
        "cloud_hypervisor": {"version": "v1"},
        "artifacts": [
            {
                "id": "sample",
                "cache_path": "files/sample",
                "sha256": digest,
                "url": "https://example.test/files/sample",
            }
        ],
    }
    root = tmp_path / "cache"
    directory = cache.write_metadata(lock, root)
    artifact = directory / "files/sample"
    artifact.parent.mkdir()
    artifact.write_bytes(payload)
    assert json.loads((directory / "complete.json").read_text(encoding="utf-8")) == {
        "lock_sha256": cache.lock_digest(lock),
        "artifact_count": 1,
    }
    assert cache.validate_cache(lock, root) == []
    artifact.write_bytes(b"tampered")
    assert any(
        "checksum mismatch" in issue for issue in cache.validate_cache(lock, root)
    )


def test_populate_downloads_and_verifies_artifacts(tmp_path: Path) -> None:
    payload = b"artifact"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    lock = {
        "fedora": {"kernel": "7.2.5"},
        "kubernetes": {"version": "v1", "cri_o": "v1", "crun": "1"},
        "cilium": {"version": "v1"},
        "cluster_api": {"version": "v1"},
        "kind": {"version": "v1", "node_image": "kindest/node:v1@sha256:" + "0" * 64},
        "packer": {"version": "v1"},
        "cloud_hypervisor": {"version": "v1"},
        "artifacts": [
            {
                "id": "sample",
                "cache_path": "files/sample",
                "sha256": digest,
                "url": "https://example.test/files/sample",
            }
        ],
    }
    response = MagicMock()
    response.read.side_effect = [payload, b""]
    response.__enter__.return_value = response

    with patch.object(cache.request, "urlopen", return_value=response):
        directory = cache.populate(lock, tmp_path)

    assert (directory / "files/sample").read_bytes() == payload
    assert cache.validate_cache(lock, tmp_path) == []


def test_prune_preserves_active_lock_only(tmp_path: Path) -> None:
    lock = {
        "fedora": {"kernel": "7.2.5"},
        "kubernetes": {"version": "v1", "cri_o": "v1", "crun": "1"},
        "cilium": {"version": "v1"},
        "cluster_api": {"version": "v1"},
        "kind": {"version": "v1", "node_image": "kindest/node:v1@sha256:" + "0" * 64},
        "packer": {"version": "v1"},
        "cloud_hypervisor": {"version": "v1"},
    }
    locks = tmp_path / "locks"
    active = locks / cache.lock_digest(lock)
    stale = locks / "stale"
    active.mkdir(parents=True)
    stale.mkdir(parents=True)
    removed = cache.prune(lock, tmp_path)
    assert removed == [stale]
    assert active.is_dir()
