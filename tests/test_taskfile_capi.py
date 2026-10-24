"""Static contracts for the Kind-based Cluster API management lifecycle."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
TASKFILE = yaml.safe_load((ROOT / "Taskfile.yml").read_text(encoding="utf-8"))
TASKS = TASKFILE["tasks"]
VARS = TASKFILE["vars"]
KIND_CONFIG = (ROOT / "kind.yaml").read_text(encoding="utf-8")
BAKE_CONTAINERFILE = (ROOT / "bake" / "Containerfile").read_text(encoding="utf-8")


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


def test_management_lifecycle_targets_use_the_kind_context() -> None:
    assert "k8labs-mgmt" in VARS["MGMT_CLUSTER_NAME"]
    assert "kind-" in VARS["MGMT_CONTEXT"]
    for name in (
        "cluster-up",
        "addons-up",
        "cluster-down",
        "cluster-clean",
        "kubeconfig",
    ):
        assert 'kubectl --context "{{.MGMT_CONTEXT}}"' in command(name)


def test_kind_bootstrap_loads_verified_images_without_lock_parsing_at_make_time() -> (
    None
):
    recipe = command("kind-up")
    assert "load_management_images.py" in recipe
    assert (
        'kind create cluster --name "{{.MGMT_CLUSTER_NAME}}" --config kind.yaml'
        in recipe
    )
    assert 'kind export kubeconfig --name "{{.MGMT_CLUSTER_NAME}}"' in recipe
    assert (
        'kubectl config get-contexts -o name | grep -qx "{{.MGMT_CONTEXT}}"' in recipe
    )
    assert "yaml.safe_load" not in recipe
    assert "$(shell " not in recipe
    assert "kindest/node:v1.37.0@sha256:" in KIND_CONFIG


def test_bake_container_fetches_and_verifies_the_locked_plugin_commit() -> None:
    assert "ARG PACKER_PLUGIN_REF" in BAKE_CONTAINERFILE
    assert 'git fetch --depth 1 origin "${PACKER_PLUGIN_REF}"' in BAKE_CONTAINERFILE
    assert 'test "$(git rev-parse HEAD)" = "${PACKER_PLUGIN_REF}"' in BAKE_CONTAINERFILE
    assert "git clone --depth 1 --branch" not in BAKE_CONTAINERFILE


def test_management_bootstrap_uses_separate_host_agent_and_provider_manager() -> None:
    recipe = command("mgmt-bootstrap")
    assert "task: kind-up" in recipe
    assert "task: host-agent-up" in recipe
    assert "load_management_images.py" in recipe
    assert "bootstrap_management.py" in recipe
    check = command("mgmt-check")
    assert "deployment/controller-manager" in check
    assert "host_agent.py check" in check


def test_management_teardown_is_guarded_and_preserves_host_agent_state() -> None:
    down = command("kind-down")
    assert "MGMT_FORCE" in down
    assert "get clusters.cluster.x-k8s.io" in down
    assert 'kind delete cluster --name "{{.MGMT_CLUSTER_NAME}}"' in down
    assert "task: kind-down" in command("mgmt-down")


def test_retired_capishim_contract_is_absent_from_management_lifecycle() -> None:
    lifecycle = "\n".join(
        command(name)
        for name in (
            "kind-up",
            "kind-down",
            "mgmt-bootstrap",
            "mgmt-check",
            "mgmt-down",
        )
    )
    assert "capishim" not in lifecycle.lower()
    assert "kubeconfig" not in command("cluster-up")


def test_full_cluster_gate_waits_for_all_ready_nodes_before_smoke() -> None:
    recipe = command("cluster")
    assert "wait --for=condition=Ready nodes --all" in recipe
    assert "node_count" in recipe and "-ne 3" in recipe
    assert recipe.index('"{{.TASK_EXE}}" kubeconfig') < recipe.index("node_count")
    assert recipe.index("node_count") < recipe.index('"{{.TASK_EXE}}" smoke-test')
