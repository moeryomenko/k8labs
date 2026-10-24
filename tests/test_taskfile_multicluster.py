"""Contracts for the Kind-backed concurrent workload-cluster workflow."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
TASKFILE = yaml.safe_load((ROOT / "Taskfile.yml").read_text(encoding="utf-8"))
TASKS = TASKFILE["tasks"]
VARS = TASKFILE["vars"]


def command(name: str) -> str:
    """Flatten a task's command strings and task references."""
    parts: list[str] = []
    task = TASKS[name]
    if "cmd" in task:
        parts.append(str(task["cmd"]))
    for entry in task.get("cmds", []):
        if isinstance(entry, str):
            parts.append(entry)
        elif isinstance(entry, dict):
            if "task" in entry:
                parts.append(f"task: {entry['task']}")
            if "cmd" in entry:
                parts.append(str(entry["cmd"]))
    return "\n".join(parts)


def test_multicluster_uses_kind_management_context_for_every_capi_operation() -> None:
    recipe = command("multi-cluster-test")
    assert "task: mgmt-up" in recipe
    assert (
        'kubectl --context "{{.MGMT_CONTEXT}}" apply --server-side -f capi/cluster.yaml'
        in recipe
    )
    assert (
        'kubectl --context "{{.MGMT_CONTEXT}}" apply --server-side -f "{{.MULTI_CLUSTER2_MANIFEST}}"'
        in recipe
    )
    assert 'kubectl --context "{{.MGMT_CONTEXT}}" get secret' in recipe
    assert 'kubectl --context "{{.MGMT_CONTEXT}}" --cache-dir' in recipe
    assert "capishim" not in recipe.lower()


def test_workload_cluster_wait_uses_capi_available_condition() -> None:
    single_cluster = command("cluster")
    multi_cluster = command("multi-cluster-test")

    assert (
        '--for=condition=Available "cluster.cluster.x-k8s.io/{{.CLUSTER_NAME}}"'
        in single_cluster
    )
    assert '--for=condition=Ready "cluster.cluster.x-k8s.io/' not in single_cluster
    assert (
        multi_cluster.count('--for=condition=Available "cluster.cluster.x-k8s.io/') == 2
    )
    assert '--for=condition=Ready "cluster.cluster.x-k8s.io/' not in multi_cluster


def test_multicluster_requires_distinct_workload_api_endpoints() -> None:
    recipe = command("multi-cluster-test")
    assert VARS["MULTI_KUBECONFIG_1"] == "build/kubeconfig.k8labs"
    assert VARS["MULTI_KUBECONFIG_2"] == "build/kubeconfig.k8labs-2"
    assert 'if [ "$url1" = "$url2" ]; then' in recipe
    assert "port collision" in recipe


def test_multicluster_reclaims_both_workload_clusters() -> None:
    recipe = command("multi-cluster-test")
    assert 'delete "cluster.cluster.x-k8s.io/{{.CLUSTER_NAME}}"' in recipe
    assert 'delete "cluster.cluster.x-k8s.io/{{.MULTI_CLUSTER2_NAME}}"' in recipe
    assert 'wait --for=delete "cluster.cluster.x-k8s.io/{{.CLUSTER_NAME}}"' in recipe
    assert (
        'wait --for=delete "cluster.cluster.x-k8s.io/{{.MULTI_CLUSTER2_NAME}}"'
        in recipe
    )
    assert "best-effort" in recipe


def test_multicluster_documents_resource_floor() -> None:
    summary = TASKS["multi-cluster-test"]["summary"]
    assert "~16 CPU" in summary
    assert "~28 GiB RAM" in summary
