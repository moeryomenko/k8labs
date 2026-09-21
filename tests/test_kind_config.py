from __future__ import annotations

from pathlib import Path

import yaml


def test_management_kind_uses_default_cni() -> None:
    config = yaml.safe_load(
        (Path(__file__).parents[1] / "kind.yaml").read_text(encoding="utf-8")
    )
    networking = config.get("networking", {})
    assert networking.get("disableDefaultCNI") is not True
    assert config["nodes"][0]["image"].startswith("kindest/node:v1.37.0@sha256:")
    mounts = {mount["containerPath"] for mount in config["nodes"][0]["extraMounts"]}
    assert {
        "/dev/kvm",
        "/run/user/1000/bus",
        "/run/user/1000/k8snet",
        "/host-state",
    } <= mounts
