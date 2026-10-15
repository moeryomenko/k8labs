from __future__ import annotations

from pathlib import Path

import yaml


def test_management_kind_uses_default_cni_without_host_bridges() -> None:
    config = yaml.safe_load(
        (Path(__file__).parents[1] / "kind.yaml").read_text(encoding="utf-8")
    )
    networking = config.get("networking", {})
    assert networking.get("disableDefaultCNI") is not True
    assert config["name"] == "k8labs-mgmt"
    node = config["nodes"][0]
    assert node["image"].startswith("kindest/node:v1.37.0@sha256:")
    assert not node.get("extraMounts")
    assert "kubeadmConfigPatches" not in node
    forbidden = ("/dev/kvm", "/run/user", "k8netd", "/host-state", "systemd")
    assert not any(
        forbidden_value in str(config)
        for forbidden_value in forbidden
    )
