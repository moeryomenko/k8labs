"""Submit one placement burst and emit pods.csv rows.

Used by ``run-placement.sh``. For the resource-scoring matrices this submits a
capacity-boundary burst (all Pods fit except the final three); for the
topology-spread matrix it submits a fixed burst with a zone spread constraint.

Feasibility is computed from *remaining* allocatable capacity (allocatable minus
the requests of Pods already placed on each node), not total allocatable.

The emitted columns match ``placement_analysis.read_pods``:
``block_id,profile,pod,latency_ms,unschedulable,correct``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
KUBECONFIG = PROJECT_ROOT / "build" / "kubeconfig"
POD_CPU_MILLIS = 500
POD_MEMORY_KI = 256 * 1024
IMAGE = "docker.io/library/nginx:1.27.3-alpine"
CAPACITY_MATRICES = {"least-allocated", "most-allocated"}
EXCESS = 3


def kubectl(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(KUBECONFIG), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def parse_cpu_millis(value: str) -> int:
    value = value.strip()
    if value.endswith("m"):
        return int(value[:-1])
    return int(value) * 1000


def parse_memory_ki(value: str) -> int:
    value = value.strip()
    scale = {"Ki": 1, "Mi": 1024, "Gi": 1024 * 1024}
    for suffix, factor in scale.items():
        if value.endswith(suffix):
            return int(value[: -len(suffix)]) * factor
    return int(value) // 1024


def worker_nodes() -> list[dict]:
    data = json.loads(kubectl("get", "nodes", "-o", "json"))
    return [
        node
        for node in data["items"]
        if "node-role.kubernetes.io/control-plane" not in node["metadata"]["labels"]
    ]


def existing_usage_by_node() -> dict[str, tuple[int, int]]:
    """Requested (cpu millis, memory Ki) already placed on each node."""

    data = json.loads(
        kubectl(
            "get",
            "pods",
            "-A",
            "--field-selector=status.phase!=Succeeded,status.phase!=Failed",
            "-o",
            "json",
        )
    )
    usage: dict[str, list[int]] = {}
    for pod in data["items"]:
        node = pod.get("spec", {}).get("nodeName")
        if not node:
            continue
        for container in pod["spec"].get("containers", []):
            requests = container.get("resources", {}).get("requests", {}) or {}
            cpu = requests.get("cpu")
            memory = requests.get("memory")
            bucket = usage.setdefault(node, [0, 0])
            if cpu:
                bucket[0] += parse_cpu_millis(cpu)
            if memory:
                bucket[1] += parse_memory_ki(memory)
    return {node: (b[0], b[1]) for node, b in usage.items()}


def ensure_zone_labels(nodes: list[dict], topology_key: str) -> dict[str, str]:
    """Assign one synthetic domain per worker so spread constraints can apply."""

    zones = ["zone-a", "zone-b", "zone-c"]
    mapping: dict[str, str] = {}
    for index, node in enumerate(sorted(nodes, key=lambda n: n["metadata"]["name"])):
        zone = zones[index % len(zones)]
        name = node["metadata"]["name"]
        kubectl("label", "node", name, f"{topology_key}={zone}", "--overwrite")
        mapping[name] = zone
    return mapping


def capacity_fit(nodes: list[dict], usage: dict[str, tuple[int, int]]) -> int:
    """Slot count that fits in the REMAINING capacity of the workers."""

    total = 0
    for node in nodes:
        name = node["metadata"]["name"]
        alloc = node["status"]["allocatable"]
        used_cpu, used_mem = usage.get(name, (0, 0))
        free_cpu = parse_cpu_millis(alloc["cpu"]) - used_cpu
        free_mem = parse_memory_ki(alloc["memory"]) - used_mem
        total += max(min(free_cpu // POD_CPU_MILLIS, free_mem // POD_MEMORY_KI), 0)
    return total


def pod_manifest(block: int, index: int) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"sched-probe-b{block}-{index}",
            "labels": {"app": "kube-sched-probe"},
        },
        "spec": {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0,
            "containers": [
                {
                    "name": "probe",
                    "image": IMAGE,
                    "command": ["sleep", "600"],
                    "resources": {
                        "requests": {"cpu": f"{POD_CPU_MILLIS}m", "memory": "256Mi"}
                    },
                }
            ],
        },
    }


def iso_to_epoch(value: str) -> float:
    return (
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def wait_scheduled(names: set[str], timeout_s: int) -> tuple[dict[str, dict], set[str]]:
    """Wait until every submitted Pod is scheduled, or the timeout expires.

    A Pod missing from the list response is still pending observation, never
    "scheduled" and never "unschedulable": conflating the two turns an API
    hiccup into fabricated placement data.
    """

    deadline = time.time() + timeout_s
    observed: dict[str, dict] = {}
    while True:
        data = json.loads(
            kubectl("get", "pods", "-l", "app=kube-sched-probe", "-o", "json")
        )
        observed = {p["metadata"]["name"]: p for p in data["items"]}
        missing = names - set(observed)
        pending = {
            name
            for name in names & set(observed)
            if not observed[name].get("spec", {}).get("nodeName")
        }
        if not missing and not pending:
            return observed, set()
        if time.time() >= deadline:
            return observed, missing | pending
        time.sleep(2)


def emit(block: int, profile: str, rows: list[tuple]) -> None:
    for pod, latency, unschedulable, correct in rows:
        print(
            f"{block},{profile},{pod},{latency:.3f},{int(unschedulable)},{int(correct)}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--block", required=True, type=int)
    parser.add_argument("--burst", required=True, type=int)
    parser.add_argument("--timeout", required=True, type=int)
    parser.add_argument("--topology-key", required=True)
    parser.add_argument("--max-skew", required=True, type=int)
    args = parser.parse_args(argv)

    nodes = worker_nodes()
    if not nodes:
        raise SystemExit("no worker nodes found")

    topology_key = ""
    if args.matrix in CAPACITY_MATRICES:
        fit = capacity_fit(nodes, existing_usage_by_node())
        total = fit + EXCESS
    else:
        fit = None
        total = args.burst
        topology_key = args.topology_key
        ensure_zone_labels(nodes, topology_key)

    created: dict[str, float] = {}
    order: list[str] = []
    for index in range(total):
        manifest = pod_manifest(args.block, index)
        if topology_key:
            manifest["spec"]["topologySpreadConstraints"] = [
                {
                    "maxSkew": args.max_skew,
                    "topologyKey": topology_key,
                    "whenUnsatisfiable": "DoNotSchedule",
                    "labelSelector": {"matchLabels": {"app": "kube-sched-probe"}},
                }
            ]
        name = manifest["metadata"]["name"]
        created[name] = time.time()
        subprocess.run(
            ["kubectl", "--kubeconfig", str(KUBECONFIG), "apply", "-f", "-"],
            input=json.dumps(manifest),
            text=True,
            check=True,
            capture_output=True,
        )
        order.append(name)

    observed, unresolved = wait_scheduled(set(created), args.timeout)
    if unresolved:
        raise SystemExit(
            "placement burst unresolved; Pods not scheduled within "
            f"{args.timeout}s: {sorted(unresolved)}"
        )

    rows: list[tuple] = []
    for idx, name in enumerate(order):
        pod = observed[name]
        node_name = pod.get("spec", {}).get("nodeName") or ""
        unschedulable = not node_name
        latency = 0.0
        for condition in pod.get("status", {}).get("conditions", []):
            if condition.get("type") == "PodScheduled" and node_name:
                latency = max(
                    iso_to_epoch(condition["lastTransitionTime"]) - created[name],
                    0.0,
                )
                if latency == 0.0:
                    # Pods created in the same second as their schedule event.
                    latency = 0.001
        if fit is not None:
            should_fit = idx < fit
            correct = (should_fit and not unschedulable) or (
                not should_fit and unschedulable
            )
        else:
            correct = not unschedulable
        rows.append((name, latency, unschedulable, correct))

    if fit is not None:
        scheduled = sum(1 for r in rows if not r[2])
        if scheduled != fit:
            raise SystemExit(
                f"capacity mismatch: expected {fit} scheduled Pods, observed "
                f"{scheduled}; the capacity model is wrong for this cluster state"
            )

    emit(args.block, args.matrix, rows)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
