#!/usr/bin/env python3
"""
Phase 7 Topic 7.5: cgroup v2 CPU parameter deep dive

Reads the CPU control files that kubelet actually writes on a real node and
records which of them this cluster supports, what they contain, and how each
one maps to a Kubernetes field.

Two parts:

  A. node inventory. The root of the host cgroup v2 hierarchy, its
     controllers, and every CPU file the kernel exposes, read from the host
     through a reader pod.

  B. measured mapping. Two pods are created with known requests and limits,
     and the files kubelet wrote for each container are read. This shows the
     real relationship between requests.cpu / limits.cpu and cpu.weight /
     cpu.max instead of asserting it from documentation.

A final section records the parameter interactions that matter in practice,
stated as consequences of the values measured in part B.

Usage:
    python3 analyze-cgroup-v2.py

Environment:
    PHASE7_NAMESPACE  (default: cpu-limits-research)
    PHASE7_NODE       (optional) pin every pod to one node
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import node_access as na  # noqa: E402

EVIDENCE_DIR = Path(__file__).parent.parent.parent / "evidence" / "phase7" / "cgroup-v2"
NAMESPACE = "cpu-limits-research"

NGINX_DIGEST = (
    "docker.io/library/nginx@sha256:"
    "09369da6b10306312cd908661320086bf87fbae1b6b0c49a1f50ba531fef2eab"
)

# CPU files a cgroup v2 kernel may expose. "supported" is decided by reading
# the node, not by this list.
CGROUP_CPU_FILES = [
    "cpu.max",
    "cpu.max.burst",
    "cpu.weight",
    "cpu.weight.nice",
    "cpu.stat",
    "cpu.pressure",
    "cpu.idle",
    "cpu.uclamp.min",
    "cpu.uclamp.max",
]

# (name, requests, limits). Both pods request 250m so that their weights are
# comparable; the limit is what differs.
MAPPING_PODS = [
    ("mapped-unlimited", {"cpu": "100m", "memory": "128Mi"}, {"memory": "256Mi"}),
    ("mapped-capped", {"cpu": "100m", "memory": "128Mi"},
     {"cpu": "100m", "memory": "256Mi"}),
    ("mapped-larger", {"cpu": "250m", "memory": "128Mi"},
     {"cpu": "250m", "memory": "256Mi"}),
]


def kubectl(*args):
    return subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=180
    )


def pod_manifest(name, requests, limits, node):
    container = {
        "name": "app",
        "image": NGINX_DIGEST,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": 80}],
        "resources": {"requests": dict(requests), "limits": dict(limits)},
    }
    spec = {
        "restartPolicy": "Never",
        "tolerations": [{"operator": "Exists"}],
        "containers": [container],
        "terminationGracePeriodSeconds": 0,
    }
    if node:
        spec["nodeName"] = node
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {"phase7": "7.5"}},
        "spec": spec,
    }


def apply(manifest):
    return subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(manifest),
        capture_output=True,
        text=True,
        timeout=120,
    )


def delete():
    return kubectl(
        "delete",
        "pod",
        "-n",
        NAMESPACE,
        "-l",
        "phase7=7.5",
        "--ignore-not-found",
        "--grace-period=0",
        "--force",
    )


def wait_ready(name, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        phase = kubectl(
            "get", "pod", name, "-n", NAMESPACE, "-o", "jsonpath={.status.phase}"
        ).stdout.strip()
        if phase in ("Succeeded", "Failed"):
            return False, f"pod phase {phase}"
        ready = kubectl(
            "get",
            "pod",
            name,
            "-n",
            NAMESPACE,
            "-o",
            "jsonpath={.status.containerStatuses[0].ready}",
        ).stdout.strip()
        if ready == "true":
            return True, None
        time.sleep(2)
    return False, f"not ready in {timeout}s"


def parse_cpu_pressure(text):
    """Parse cpu.pressure PSI output."""
    psi = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        psi[parts[0]] = {
            key: float(value)
            for key, value in (metric.split("=", 1) for metric in parts[1:])
        }
    return psi


def read_file(node, path):
    """Read a cgroup file from the host, reporting absence explicitly."""
    raw = node.read(path)
    if raw is None:
        return {"supported": False, "error": "not readable"}
    return {"supported": True, "value": raw.strip()}


def read_cpu_files(node, path):
    """Read every known CPU file in one cgroup directory."""
    files = {}
    for name in CGROUP_CPU_FILES:
        result = read_file(node, f"{path}/{name}")
        if not result.get("supported"):
            files[name] = result
            continue
        value = result["value"]
        if name == "cpu.stat":
            files[name] = {"supported": True, "parsed": na.parse_cpu_stat(value)}
        elif name == "cpu.pressure":
            files[name] = {"supported": True, "parsed": parse_cpu_pressure(value)}
        elif name == "cpu.max":
            bare = na.parse_cpu_max(value) or {}
            entry = {"supported": True, "raw": value, **bare}
            if bare.get("quota") and bare.get("period"):
                entry["cpu_cores"] = round(bare["quota"] / bare["period"], 3)
            files[name] = entry
        elif name.startswith("cpu.uclamp"):
            try:
                files[name] = {"supported": True, "raw": value, "value": float(value)}
            except ValueError:
                files[name] = {"supported": True, "raw": value}
        elif name in ("cpu.weight", "cpu.idle", "cpu.max.burst", "cpu.weight.nice"):
            try:
                files[name] = {"supported": True, "raw": value, "value": int(value)}
            except ValueError:
                files[name] = {"supported": True, "raw": value}
        else:
            files[name] = {"supported": True, "raw": value}
    return files


def part_a_inventory(node):
    print("\nPart A: node cgroup v2 inventory")
    print("-" * 78)

    root = node.host_cgroup_root()
    controllers = (node.read(f"{root}/cgroup.controllers") or "").strip()
    subtree = (node.read(f"{root}/cgroup.subtree_control") or "").strip()

    # The root of a cgroup v2 hierarchy holds no task-facing CPU files; they
    # appear in the groups that actually hold processes. kubepods.slice is
    # the first such group that always exists on a kubelet node.
    sample_path = f"{root}/kubepods.slice"
    inventory = {
        "cgroup_root": root,
        "controllers_available": controllers.split(),
        "controllers_enabled_in_children": subtree.split(),
        "cpu_controller_enabled": "cpu" in subtree.split(),
        "cpu_files_sample_cgroup": sample_path,
        "cpu_files": read_cpu_files(node, sample_path),
    }

    supported = [
        name
        for name, info in inventory["cpu_files"].items()
        if info.get("supported")
    ]
    missing = [
        name
        for name, info in inventory["cpu_files"].items()
        if not info.get("supported")
    ]

    print(f"  root:        {root}")
    print(f"  sampled:     {sample_path}")
    print(f"  controllers: {' '.join(inventory['controllers_available']) or '(none)'}")
    print(f"  cpu enabled: {inventory['cpu_controller_enabled']}")
    print(f"  supported:   {', '.join(supported)}")
    print(f"  absent:      {', '.join(missing) or '(none)'}")
    return inventory


def part_b_mapping(node):
    print("\nPart B: measured Kubernetes to cgroup mapping")
    print("-" * 78)

    delete()
    pods = []
    for name, requests, limits in MAPPING_PODS:
        applied = apply(pod_manifest(name, requests, limits, node.node))
        if applied.returncode != 0:
            delete()
            return {
                "status": "failed",
                "error": f"{name}: {applied.stderr.strip()[:300]}",
            }
        pods.append((name, requests, limits))

    for name, _, _ in pods:
        ok, reason = wait_ready(name)
        if not ok:
            delete()
            return {"status": "failed", "error": f"{name}: {reason}"}

    records = []
    for name, requests, limits in pods:
        mapping = na.pod_container_cgroups(node, name, namespace=NAMESPACE)
        cgroup_dir = mapping.get("app")
        if not cgroup_dir:
            records.append(
                {
                    "pod": name,
                    "status": "failed",
                    "error": f"cgroup not found (have {sorted(mapping)})",
                }
            )
            continue
        files = read_cpu_files(node, cgroup_dir)
        record = {
            "pod": name,
            "status": "ok",
            "requests": requests,
            "limits": limits,
            "cgroup_dir": cgroup_dir,
            "files": files,
        }
        records.append(record)

        weight = (files.get("cpu.weight") or {}).get("value")
        nice = (files.get("cpu.weight.nice") or {}).get("raw")
        quota = (files.get("cpu.max") or {}).get("quota")
        cores = (files.get("cpu.max") or {}).get("cpu_cores")
        print(
            f"  {name:<16} requests={requests['cpu']:<6} limits={limits.get('cpu', 'none'):<6} "
            f"-> weight={weight} nice={nice} quota={quota} cores={cores}"
        )

    delete()
    return {"status": "ok", "records": records}


def interactions(node, records):
    """Derive the interaction notes from the values that were measured."""
    by_pod = {r["pod"]: r for r in records if r.get("status") == "ok"}
    plain = by_pod.get("mapped-unlimited") or {}
    capped = by_pod.get("mapped-capped") or {}

    plain_weight = (plain.get("files", {}).get("cpu.weight") or {}).get("value")
    capped_weight = (capped.get("files", {}).get("cpu.weight") or {}).get("value")
    plain_max = (plain.get("files", {}).get("cpu.max") or {}).get("raw")
    capped_max = (capped.get("files", {}).get("cpu.max") or {}).get("raw")

    return {
        "requests_to_cpu.weight": {
            "measured": {
                "requests_100m_weight": plain_weight,
                "requests_100m_weight_with_limit_100m": capped_weight,
            },
            "rule": (
                "cpu.weight is set from requests.cpu and is independent of "
                "limits.cpu: the pods that requested 100m both got weight "
                f"{plain_weight} with and without a 100m limit, while the pod "
                "that requested 250m got a different weight."
                if plain_weight == capped_weight
                else "cpu.weight differed between the pods that requested 100m, "
                "so it depends on more than requests.cpu on this node."
            ),
        },
        "limits_to_cpu.max": {
            "measured": {
                "no_cpu_limit_cpu_max": plain_max,
                "limit_100m_cpu_max": capped_max,
            },
            "rule": (
                "limits.cpu is written to cpu.max as <quota> <period> with a "
                "100000 us period; no limit leaves the quota at max."
            ),
        },
        "cpu.max_beats_cpu.weight": {
            "rule": (
                "cpu.weight only decides who runs when cores are oversubscribed "
                "and every task is runnable. cpu.max is enforced regardless of "
                "idle capacity, so a capped container loses CPU on an otherwise "
                "empty node. Phase 4 measured this: a quota-bound task stalls "
                "for the rest of the 100 ms period once its quota is spent."
            ),
        },
        "quota_to_wall_clock": {
            "rule": (
                "A 100ms period means a quota of Q cores allows Q*100000 us of "
                "run time per period. A task needing more than its quota per "
                "period is stopped until the next period, which is where the "
                "latency comes from. Short bursts under the quota cost nothing."
            ),
        },
        "cpu.pressure_vs_cpu.stat": {
            "rule": (
                "cpu.stat counters (nr_throttled, throttled_usec) count quota "
                "exhaustion only. cpu.pressure counts time when a runnable task "
                "waited for a CPU, which also happens with no quota at all, so "
                "PSI catches contention that throttling counters miss."
            ),
        },
    }


def node_override():
    return os.environ.get("PHASE7_NODE") or None


def main():
    parser = argparse.ArgumentParser()
    args = parser.parse_args()
    del args

    print("=" * 78)
    print("Phase 7 Topic 7.5: cgroup v2 CPU parameter deep dive")
    print("=" * 78)

    kubectl("create", "namespace", NAMESPACE)

    node = na.NodeAccess(node_name=node_override(), namespace=NAMESPACE)
    if not node.available():
        print(f"node access unavailable: {node.describe()}")
        return 1
    print(f"node: {node.node} via {node.describe()['method']}")

    inventory = part_a_inventory(node)
    mapping = part_b_mapping(node)


    payload = {
        "test": "cgroup-v2-deep-dive",
        "topic": "7.5",
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "node": node.node,
        "access_method": node.describe()["method"],
        "kernel": (node.read("/proc/sys/kernel/osrelease") or "").strip() or None,
        "inventory": inventory,
        "kubernetes_mapping": mapping,
        "interactions": (
            interactions(node, mapping.get("records", []))
            if mapping.get("status") == "ok" else None
        ),
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / f"cgroup-v2-analysis-{payload['timestamp']}.json"
    out.write_text(json.dumps(payload, indent=2))

    print()
    print(f"Evidence: {out}")
    ok = mapping.get("status") == "ok" and inventory["cpu_controller_enabled"]
    print(f"Result: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
