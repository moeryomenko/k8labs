"""Contracts for the Kind-backed concurrent workload-cluster workflow."""

from __future__ import annotations

from pathlib import Path

MAKEFILE = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")


def target(name: str) -> str:
    start = MAKEFILE.index(f"{name}:")
    next_target = MAKEFILE.find("\n.PHONY:", start + 1)
    return MAKEFILE[start : next_target if next_target != -1 else None]


def test_multicluster_uses_kind_management_context_for_every_capi_operation() -> None:
    recipe = target("multi-cluster-test")
    assert "$(MAKE) mgmt-up" in recipe
    assert (
        'kubectl --context "$(MGMT_CONTEXT)" apply --server-side -f capi/cluster.yaml'
        in recipe
    )
    assert (
        'kubectl --context "$(MGMT_CONTEXT)" apply --server-side -f "$(MULTI_CLUSTER2_MANIFEST)"'
        in recipe
    )
    assert 'kubectl --context "$(MGMT_CONTEXT)" get secret' in recipe
    assert 'kubectl --context "$(MGMT_CONTEXT)" --cache-dir' in recipe
    assert "capishim" not in recipe.lower()


def test_workload_cluster_wait_uses_capi_available_condition() -> None:
    single_cluster = target("cluster")
    multi_cluster = target("multi-cluster-test")

    assert (
        '--for=condition=Available "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)"'
        in single_cluster
    )
    assert '--for=condition=Ready "cluster.cluster.x-k8s.io/' not in single_cluster
    assert (
        multi_cluster.count('--for=condition=Available "cluster.cluster.x-k8s.io/') == 2
    )
    assert '--for=condition=Ready "cluster.cluster.x-k8s.io/' not in multi_cluster


def test_multicluster_requires_distinct_workload_api_endpoints() -> None:
    recipe = target("multi-cluster-test")
    assert "MULTI_KUBECONFIG_1 := build/kubeconfig.k8labs" in MAKEFILE
    assert "MULTI_KUBECONFIG_2 := build/kubeconfig.k8labs-2" in MAKEFILE
    assert 'if [ "$$url1" = "$$url2" ]; then' in recipe
    assert "port collision" in recipe


def test_multicluster_reclaims_both_workload_clusters() -> None:
    recipe = target("multi-cluster-test")
    assert 'delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)"' in recipe
    assert 'delete "cluster.cluster.x-k8s.io/$(MULTI_CLUSTER2_NAME)"' in recipe
    assert 'wait --for=delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)"' in recipe
    assert (
        'wait --for=delete "cluster.cluster.x-k8s.io/$(MULTI_CLUSTER2_NAME)"' in recipe
    )
    assert "best-effort" in recipe


def test_multicluster_documents_resource_floor() -> None:
    recipe = target("multi-cluster-test")
    assert "~16 CPU" in recipe
    assert "~28 GiB RAM" in recipe
