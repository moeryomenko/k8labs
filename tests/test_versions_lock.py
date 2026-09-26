from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

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
    assert b'"version":"v1.37.0"' in encoded
    assert b'"node_image":"kindest/node:v1.37.0@sha256:' in encoded


def test_mutable_reference_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "versions.yaml"
    path.write_text(
        "fedora:\n  kernel: 7.2.5-200.fc44\n"
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
