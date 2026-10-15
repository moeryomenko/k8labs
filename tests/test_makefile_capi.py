"""Static contracts for the Kind-based Cluster API management lifecycle."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
KIND_CONFIG = (ROOT / "kind.yaml").read_text(encoding="utf-8")
BAKE_CONTAINERFILE = (ROOT / "bake" / "Containerfile").read_text(encoding="utf-8")


def target(name: str) -> str:
    start = MAKEFILE.index(f"{name}:")
    next_target = MAKEFILE.find("\n.PHONY:", start + 1)
    return MAKEFILE[start : next_target if next_target != -1 else None]


def test_management_lifecycle_targets_use_the_kind_context() -> None:
    assert "MGMT_CLUSTER_NAME ?= k8labs-mgmt" in MAKEFILE
    assert "MGMT_CONTEXT ?= kind-$(MGMT_CLUSTER_NAME)" in MAKEFILE
    for name in (
        "cluster-up",
        "addons-up",
        "cluster-down",
        "cluster-clean",
        "kubeconfig",
    ):
        assert 'kubectl --context "$(MGMT_CONTEXT)"' in target(name)


def test_kind_bootstrap_loads_verified_images_without_lock_parsing_at_make_time() -> (
    None
):
    recipe = target("kind-up")
    assert "load_management_images.py" in recipe
    assert (
        'kind create cluster --name "$(MGMT_CLUSTER_NAME)" --config kind.yaml' in recipe
    )
    assert 'kind export kubeconfig --name "$(MGMT_CLUSTER_NAME)"' in recipe
    assert 'kubectl config get-contexts -o name | grep -qx "$(MGMT_CONTEXT)"' in recipe
    assert "yaml.safe_load" not in recipe
    assert "$(shell " not in recipe
    assert "kindest/node:v1.37.0@sha256:" in KIND_CONFIG


def test_bake_container_fetches_and_verifies_the_locked_plugin_commit() -> None:
    assert "ARG PACKER_PLUGIN_REF" in BAKE_CONTAINERFILE
    assert 'git fetch --depth 1 origin "${PACKER_PLUGIN_REF}"' in BAKE_CONTAINERFILE
    assert 'test "$(git rev-parse HEAD)" = "${PACKER_PLUGIN_REF}"' in BAKE_CONTAINERFILE
    assert "git clone --depth 1 --branch" not in BAKE_CONTAINERFILE


def test_management_bootstrap_uses_separate_host_agent_and_provider_manager() -> None:
    recipe = target("mgmt-bootstrap")
    assert "kind-up host-agent-up" in recipe
    assert "load_management_images.py" in recipe
    assert "bootstrap_management.py" in recipe
    check = target("mgmt-check")
    assert "deployment/controller-manager" in check
    assert "host_agent.py check" in check


def test_management_teardown_is_guarded_and_preserves_host_agent_state() -> None:
    down = target("kind-down")
    assert "MGMT_FORCE" in down
    assert "get clusters.cluster.x-k8s.io" in down
    assert 'kind delete cluster --name "$(MGMT_CLUSTER_NAME)"' in down
    assert "mgmt-down: kind-down" in MAKEFILE


def test_retired_capishim_contract_is_absent_from_management_lifecycle() -> None:
    lifecycle = "\n".join(
        target(name)
        for name in (
            "kind-up",
            "kind-down",
            "mgmt-bootstrap",
            "mgmt-check",
            "mgmt-down",
        )
    )
    assert "capishim" not in lifecycle.lower()
    assert "kubeconfig" not in target("cluster-up")


def test_full_cluster_gate_waits_for_four_ready_nodes_before_smoke() -> None:
    recipe = target("cluster")
    assert "wait --for=condition=Ready nodes --all" in recipe
    assert "node_count" in recipe and "-ne 4" in recipe
    assert recipe.index("$(MAKE) kubeconfig") < recipe.index("node_count")
    assert recipe.index("node_count") < recipe.index("$(MAKE) smoke-test")
