"""Focused tests for custom-kernel image staging and rollback mapping."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("host_agent", SCRIPTS / "host_agent.py")
assert SPEC and SPEC.loader
host_agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = host_agent
SPEC.loader.exec_module(host_agent)

REPO_ROOT = Path(__file__).parents[1]


def test_stage_artifacts_stages_two_images_under_distinct_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    build = tmp_path / "build"
    build.mkdir()
    (build / "k8labs-base.qcow2").write_bytes(b"base-image")
    (build / "k8labs-custom-kernel.qcow2").write_bytes(b"custom-kernel-image")
    (build / "CLOUDHV.fd").write_bytes(b"firmware")

    paths = host_agent.stage_artifacts(tmp_path / "agent")
    manifest = json.loads(Path(paths["manifest"]).read_text(encoding="utf-8"))

    assert set(manifest["images"]) == {
        host_agent.BASE_IMAGE_REFERENCE,
        host_agent.CUSTOM_KERNEL_IMAGE_REFERENCE,
    }
    for key, entry in manifest["images"].items():
        assert entry["sha256"] == host_agent.sha256(Path(entry["path"]))
        assert Path(entry["path"]).is_file()


def test_exact_hash_enforcement_detects_mismatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    build = tmp_path / "build"
    build.mkdir()
    (build / "k8labs-base.qcow2").write_bytes(b"base-image")
    (build / "k8labs-custom-kernel.qcow2").write_bytes(b"custom-kernel-image")
    (build / "CLOUDHV.fd").write_bytes(b"firmware")

    paths = host_agent.stage_artifacts(tmp_path / "agent")
    root = tmp_path / "agent"
    assert host_agent.verify_staged_artifacts(root) == []

    manifest_path = root / "state" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_key = sorted(manifest["images"])[0]
    manifest["images"][first_key]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    problems = host_agent.verify_staged_artifacts(root)
    assert problems
    assert "hash mismatch" in problems[0]


def test_cluster_mapping_uses_custom_artifact_with_rollback_base() -> None:
    docs = yaml.safe_load_all((REPO_ROOT / "capi" / "cluster.yaml").read_text(encoding="utf-8"))
    image_paths = []
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "HypervisorClusterTemplate":
            for image in doc["spec"]["template"]["spec"]["images"]:
                image_paths.append(image["path"])
    assert image_paths == [host_agent.CUSTOM_KERNEL_IMAGE_REFERENCE]
    assert host_agent.BASE_IMAGE_REFERENCE != host_agent.CUSTOM_KERNEL_IMAGE_REFERENCE
