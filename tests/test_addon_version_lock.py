from __future__ import annotations

import base64
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
MAX_SECRET_PAYLOAD = 921_600
sys.path.insert(0, str(ROOT / "scripts"))

import generate_capi_addons as gen


def test_cilium_manifests_match_locked_cilium_and_gateway_api_versions() -> None:
    lock = yaml.safe_load((ROOT / "versions.lock.yaml").read_text(encoding="utf-8"))
    version_note = (ROOT / "cilium/install/VERSION").read_text(encoding="utf-8")
    workloads = (ROOT / "cilium/install/04-workloads.yaml").read_text(encoding="utf-8")

    cilium_version = lock["cilium"]["version"]
    gateway_api_version = lock["cilium"]["gateway_api"]
    assert f"Cilium {cilium_version} (chart {cilium_version})" in version_note
    assert f"Gateway API CRDs {gateway_api_version}" in version_note

    datapath_images = set(
        re.findall(
            r"quay\.io/cilium/(?:cilium|operator-generic):(v\d+\.\d+\.\d+)@sha256:",
            workloads,
        )
    )
    assert datapath_images == {cilium_version}, (
        f"Cilium and operator image versions {sorted(datapath_images)} do not "
        f"match lock version {cilium_version}"
    )


def test_cilium_install_yaml_entries_fit_single_secret_limit() -> None:
    install = ROOT / "cilium" / "install"
    oversized = [
        (path.relative_to(ROOT).as_posix(), len(base64.b64encode(path.read_bytes())))
        for path in sorted(install.rglob("*.yaml"))
        if len(base64.b64encode(path.read_bytes())) > MAX_SECRET_PAYLOAD
    ]

    assert not oversized, (
        f"Cilium source files must fit as individual Secret data values: {oversized}"
    )


def test_cilium_v132_recovery_bridge_is_pinned() -> None:
    version_note = (ROOT / "cilium/install/VERSION").read_text(encoding="utf-8")
    required_pins = (
        "v1.19.8",
        "6974e74dcc01eab39385b8b8840df483ae314062c60a9e1b290df4cd38281fe0",
        "7c997f9c15d348d5ea4abba67e229905055acc27235f542a5b608ea07088979c",
        "e9f7ee1f2f3a41e44339612e3b6b88170bdde7679c9c9461f287bb27702a8ecf",
        "786ec9bb1a9344435e3e3f994bc4ed3a4a85afa6a314e98681af241f8be79833",
        "af7382699576b9e65e9184efa52eeca0b58aea70ad6e511bf260c91d9f740463",
    )

    missing = [pin for pin in required_pins if pin not in version_note]
    assert not missing, f"Cilium v1.32 recovery pins missing from VERSION: {missing}"


def test_generated_cilium_secrets_fit_default_payload_limit(tmp_path: Path) -> None:
    generated = gen.generate_all(ROOT, tmp_path, update_tracked_crs=False)
    for secret_path in generated["cilium"]:
        secret = yaml.safe_load(secret_path.read_text(encoding="utf-8"))
        encoded_size = sum(len(value) for value in secret["data"].values())
        assert encoded_size <= MAX_SECRET_PAYLOAD, (
            f"{secret_path.name} contains {encoded_size} encoded bytes, "
            f"over the {MAX_SECRET_PAYLOAD}-byte payload limit"
        )


def test_gateway_api_crds_are_unique_and_keep_experimental_grpc_schema() -> None:
    install = ROOT / "cilium" / "install"
    gateway_crds: dict[str, dict[str, object]] = {}
    duplicates: list[str] = []
    admission_policies: set[tuple[str, str]] = set()

    for path in sorted(install.rglob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(document, dict):
                continue
            metadata = document.get("metadata", {})
            name = metadata.get("name", "")
            if document.get("kind") in {
                "ValidatingAdmissionPolicy",
                "ValidatingAdmissionPolicyBinding",
            } and name.endswith(".gateway.networking.k8s.io"):
                admission_policies.add((document["kind"], name))
            if document.get("kind") != "CustomResourceDefinition":
                continue
            if not name.endswith(".gateway.networking.k8s.io"):
                continue
            if name in gateway_crds:
                duplicates.append(name)
            gateway_crds[name] = document

    assert not duplicates, f"duplicate Gateway API CRD documents: {duplicates}"
    assert set(gateway_crds) == {
        "backendtlspolicies.gateway.networking.k8s.io",
        "gatewayclasses.gateway.networking.k8s.io",
        "gateways.gateway.networking.k8s.io",
        "grpcroutes.gateway.networking.k8s.io",
        "httproutes.gateway.networking.k8s.io",
        "listenersets.gateway.networking.k8s.io",
        "referencegrants.gateway.networking.k8s.io",
        "tcproutes.gateway.networking.k8s.io",
        "tlsroutes.gateway.networking.k8s.io",
        "udproutes.gateway.networking.k8s.io",
    }
    assert admission_policies == {
        ("ValidatingAdmissionPolicy", "safe-upgrades.gateway.networking.k8s.io"),
        (
            "ValidatingAdmissionPolicyBinding",
            "safe-upgrades.gateway.networking.k8s.io",
        ),
    }
    grpc = gateway_crds["grpcroutes.gateway.networking.k8s.io"]
    annotations = grpc["metadata"].get("annotations", {})
    assert annotations.get("gateway.networking.k8s.io/channel") == "experimental"


def test_sysext_manifest_versions_match_immutable_lock() -> None:
    lock = yaml.safe_load((ROOT / "versions.lock.yaml").read_text(encoding="utf-8"))
    manifest = yaml.safe_load(
        (ROOT / "extensions/manifest.yaml").read_text(encoding="utf-8")
    )
    sysexts = {entry["name"]: entry for entry in manifest["sysexts"]}

    assert sysexts["kubelet"]["version"] == lock["kubernetes"]["version"]
    assert sysexts["kubernetes-cp"]["version"] == lock["kubernetes"]["version"]
    assert sysexts["cri-o"]["version"] == lock["kubernetes"]["cri_o"]
    assert sysexts["crun"]["version"] == lock["kubernetes"]["crun"]
    assert sysexts["cni"]["version"] == lock["kubernetes"]["cni_plugins"]


def test_sysext_manifest_sources_bind_to_immutable_artifacts() -> None:
    lock = yaml.safe_load((ROOT / "versions.lock.yaml").read_text(encoding="utf-8"))
    manifest = yaml.safe_load(
        (ROOT / "extensions/manifest.yaml").read_text(encoding="utf-8")
    )
    artifacts = {
        artifact["id"]: artifact
        for artifact in lock["artifacts"] + lock["guest_upgrade_artifacts"]
    }

    for sysext in manifest["sysexts"]:
        for source in sysext.get("sources", []):
            artifact_id = source.get("artifact_id")
            assert artifact_id in artifacts, (
                f"{sysext['name']} source must reference a locked artifact"
            )
            artifact = artifacts[artifact_id]
            assert artifact["version"] == sysext["version"]
            assert artifact["url"] == source["url"]
            assert re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])


def test_sysext_downloader_uses_manifest_kubelet_version() -> None:
    lock = yaml.safe_load((ROOT / "versions.lock.yaml").read_text(encoding="utf-8"))
    artifact = next(
        item
        for item in lock["artifacts"] + lock["guest_upgrade_artifacts"]
        if item["id"] == "kubelet-v1.37.0"
    )
    result = subprocess.run(
        ["sh", str(ROOT / "extensions/download-sysexts.sh"), "--dry-run", "kubelet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"/{lock['kubernetes']['version']}/bin/linux/amd64/kubelet" in result.stdout
    assert artifact["sha256"] in result.stdout
    assert "/v1.32.13/" not in result.stdout
