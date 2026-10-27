#!/usr/bin/env python3
"""
Phase 7 Topic 7.2: QoS class comparison

Kubernetes derives a QoS class from a pod's resources, and kubelet turns
that class into two things that matter here:

  * the cgroup slice the pod lands in (kubepods-besteffort.slice,
    kubepods-burstable.slice, kubepods-guaranteed.slice)
  * cpu.weight on the container, which is how the CFS scheduler divides
    CPU between runnable tasks when cores are oversubscribed

This experiment runs two parts on one node:

  A. control-plane inspection (no load). For each QoS class, record the
     slice, cpu.max, cpu.weight, and cpu.weight.nice that kubelet actually
     wrote. This is the mapping the rest of the study depends on.

  B. contention. All three classes run the same CPU burn at the same time
     on a node with only 2 usable cores, and nginx is pinned to each class
     so that a load generator measures the latency each class experiences
     while the node is oversubscribed.

The burn is a shell loop executed inside a node-cached nginx container.
`stress` was not used because its image is not present on the nodes and
Docker Hub rejects anonymous pulls from this cluster.

Usage:
    python3 test-qos-classes.py [--duration 60] [--rate 4000]

Environment:
    PHASE7_NAMESPACE  (default: cpu-limits-research)
    PHASE7_NODE       (optional) pin every pod to one node
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import node_access as na  # noqa: E402

EVIDENCE_DIR = (
    Path(__file__).parent.parent.parent / "evidence" / "phase7" / "qos-comparison"
)
NAMESPACE = "cpu-limits-research"

NGINX_DIGEST = (
    "docker.io/library/nginx@sha256:"
    "09369da6b10306312cd908661320086bf87fbae1b6b0c49a1f50ba531fef2eab"
)
LOADGEN = Path(__file__).parent.parent / "lib" / "loadgen.py"
LOADGEN_REMOTE = "/tmp/phase7-loadgen.py"

# A CPU burner that needs no extra image: nginx ships /bin/sh.
BURN_COMMAND = ["sh", "-c", "while :; do :; done"]

# (name, requests, limits). The QoS class is whatever Kubernetes derives.
QOS_CONFIGS = [
    ("besteffort", None, None),
    ("burstable", {"cpu": "500m", "memory": "128Mi"}, {"memory": "512Mi"}),
    (
        "guaranteed",
        {"cpu": "1000m", "memory": "512Mi"},
        {"cpu": "1000m", "memory": "512Mi"},
    ),
]


def kubectl(*args):
    return subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=180
    )


def apply(manifest):
    return subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(manifest),
        capture_output=True,
        text=True,
        timeout=120,
    )


def delete(selector):
    return kubectl(
        "delete",
        "pod",
        "-n",
        NAMESPACE,
        "-l",
        selector,
        "--ignore-not-found",
        "--grace-period=0",
        "--force",
    )


def pod_manifest(name, requests, limits, node, role, container_name=None, command=None):
    container = {
        "name": container_name or "app",
        "image": NGINX_DIGEST,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": 80}],
    }
    resources = {}
    if requests:
        resources["requests"] = dict(requests)
    if limits:
        resources["limits"] = dict(limits)
    if resources:
        container["resources"] = resources
    if command:
        container["command"] = command

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
        "metadata": {
            "name": name,
            "labels": {"phase7": "7.2", "role": role},
        },
        "spec": spec,
    }


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
    events = kubectl(
        "get",
        "events",
        "-n",
        NAMESPACE,
        "--field-selector",
        f"involvedObject.name={name}",
        "-o",
        "jsonpath={.items[*].message}",
    ).stdout.strip()
    return False, f"not ready in {timeout}s: {events[:300]}"


def qos_class(name):
    return kubectl(
        "get", "pod", name, "-n", NAMESPACE, "-o", "jsonpath={.status.qosClass}"
    ).stdout.strip()


def update_node_access(node, pod_name):
    """Repoint node access at the reader on the pod's node.

    Only one node carries the collector pod, so a reader is created on any
    other node the first time it is needed.
    """
    pod_node = kubectl(
        "get", "pod", pod_name, "-n", NAMESPACE, "-o", "jsonpath={.spec.nodeName}"
    ).stdout.strip()
    if node.available() and node.node == pod_node:
        return node, None
    candidate = na.NodeAccess(node_name=pod_node, namespace=NAMESPACE)
    if not candidate.available():
        return node, (
            f"no reader on {pod_node}: {candidate.describe().get('detail')}"
        )
    return candidate, None


def inspect(node, name):
    """Read the cgroup state kubelet created for the named container."""
    pod = na.kubectl_json("get", "pod", name, "-n", NAMESPACE)
    mapping = na.pod_container_cgroups(node, name, namespace=NAMESPACE)
    container = pod["spec"]["containers"][0]["name"]
    cgroup_dir = mapping.get(container)
    if not cgroup_dir:
        return None, f"cgroup for {container!r} not found (have {sorted(mapping)})"
    info = na.read_container_cpu(node, cgroup_dir)

    # The pod slice is the direct parent of the container cgroup in every
    # layout, and the QoS slice is its parent. Burstable pods get a slice
    # of their own (kubepods-burstable.slice); BestEffort and Guaranteed
    # pods sit directly in kubepods.slice, and kubelet marks their class in
    # a label instead (recorded as pod_qos_label).
    parts = cgroup_dir.rstrip("/").split("/")
    pod_slice = parts[-2] if len(parts) >= 2 else None
    qos_slice = parts[-3] if len(parts) >= 3 else None
    info["pod_slice"] = pod_slice
    info["qos_slice"] = qos_slice
    info["pod_qos"] = pod["status"].get("qosClass")
    info["pod_qos_label"] = (
        pod["metadata"].get("labels", {}).get("io.kubernetes.pod.qos") or None
    )
    return info, None


def stage_loadgen(node):
    return node.put(str(LOADGEN), LOADGEN_REMOTE)


def run_load(node, url, rate, duration):
    out = node.run(
        f"python3 {LOADGEN_REMOTE} --url {url} --no-keepalive "
        f"--rate {rate} --duration {duration} --connections 20"
    )
    if not out:
        return None, "load generator produced no output"
    try:
        return json.loads(out[out.index("{") :]), None
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"unparsable load generator output: {exc}: {out[-300:]}"


def pod_ip(name):
    return kubectl(
        "get", "pod", name, "-n", NAMESPACE, "-o", "jsonpath={.status.podIP}"
    ).stdout.strip()


def cpu_usage(node, name):
    mapping = na.pod_container_cgroups(node, name, namespace=NAMESPACE)
    if not mapping:
        return None
    cgroup_dir = next(iter(mapping.values()))
    return na.parse_cpu_stat(node.read(f"{cgroup_dir}/cpu.stat"))


def part_a_control_plane(node):
    """Record the cgroup mapping kubelet creates for each QoS class.

    Returns (results, node): the node may be rebound if the scheduler places
    a pod somewhere other than the requested node.
    """
    print("\nPart A: control-plane mapping (no load)")
    print("-" * 78)
    results = []
    for name, requests, limits in QOS_CONFIGS:
        pod = f"phase7-qos-{name}"
        delete("phase7=7.2")
        applied = apply(pod_manifest(pod, requests, limits, node.node, "control"))
        if applied.returncode != 0:
            results.append(
                {
                    "class": name,
                    "status": "failed",
                    "error": applied.stderr.strip()[:300],
                }
            )
            continue
        ok, reason = wait_ready(pod)
        if not ok:
            results.append({"class": name, "status": "failed", "error": reason})
            delete("phase7=7.2")
            continue

        node, err = update_node_access(node, pod)
        if err:
            results.append({"class": name, "status": "failed", "error": err})
            delete("phase7=7.2")
            continue
        info, err = inspect(node, pod)
        reported = qos_class(pod)
        delete("phase7=7.2")
        if err:
            results.append({"class": name, "status": "failed", "error": err})
            continue

        record = {
            "class": name,
            "status": "ok",
            "reported_qos": reported,
            "derived_qos": info["pod_qos"],
            "requests": requests,
            "limits": limits,
            "qos_slice": info["qos_slice"],
            "pod_qos_label": info["pod_qos_label"],
            "pod_slice": info["pod_slice"],
            "cpu_max": info["cpu_max"],
            "cpu_weight": info["cpu_weight"],
            "cpu_weight_nice": info["cpu_weight_nice"],
        }
        results.append(record)

        quota = record["cpu_max"]["quota"] if record["cpu_max"] else None
        print(
            f"  {name:<11} qos={record['reported_qos'] or '(pending)':<11} "
            f"slice={str(record['qos_slice']).replace('kubepods-', ''):<24} "
            f"quota={str(quota):>7} weight={record['cpu_weight']} "
            f"nice={record['cpu_weight_nice']}"
        )
    return results, node


def part_b_contention(node, duration, rate):
    """Run all three classes at once and measure each one's latency."""
    print("\nPart B: contention (all classes on one node, simultaneous)")
    print("-" * 78)

    cores = kubectl(
        "get", "node", node.node, "-o", "jsonpath={.status.capacity.cpu}"
    ).stdout.strip()

    delete("phase7=7.2")

    # One burner per class, plus a latency probe (nginx serving normally)
    # pinned to the same class as the burner it sits next to.
    burners = {}
    probes = {}
    for name, requests, limits in QOS_CONFIGS:
        burner = f"phase7-qos-{name}-burn"
        probe = f"phase7-qos-{name}-probe"
        for pod, role, command in (
            (burner, "burn", BURN_COMMAND),
            (probe, "probe", None),
        ):
            applied = apply(
                pod_manifest(pod, requests, limits, node.node, role, command=command)
            )
            if applied.returncode != 0:
                return {
                    "status": "failed",
                    "error": f"{pod}: {applied.stderr.strip()[:300]}",
                }
        burners[name] = burner
        probes[name] = probe

    for pod in [*burners.values(), *probes.values()]:
        ok, reason = wait_ready(pod)
        if not ok:
            delete("phase7=7.2")
            return {"status": "failed", "error": f"{pod}: {reason}"}

    if not stage_loadgen(node):
        delete("phase7=7.2")
        return {"status": "failed", "error": "could not stage loadgen.py"}

    node, err = update_node_access(node, next(iter(burners.values())))
    if err:
        delete("phase7=7.2")
        return {"status": "failed", "error": err}

    before = {name: cpu_usage(node, pod) for name, pod in burners.items()}
    before_stats = {name: inspect(node, pod)[0] for name, pod in probes.items()}

    # Warm-up reading. The burners started before this point, so it is not
    # a no-contention baseline; it only shows the probes before all three
    # are driven at once.
    base_loads = {}
    for name, pod in probes.items():
        ip = pod_ip(pod)
        load, err = run_load(node, f"http://{ip}:80/", rate, min(duration, 20))
        base_loads[name] = {"load": load, "error": err}

    # Burners have been running since they were created, so every reading
    # from here on includes their contention.
    results = {}
    errors = {}

    def drive(name):
        ip = pod_ip(probes[name])
        load, err = run_load(node, f"http://{ip}:80/", rate, duration)
        results[name] = load
        if err:
            errors[name] = err

    threads = [
        threading.Thread(target=drive, args=(name,), daemon=True) for name in probes
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    after = {name: cpu_usage(node, pod) for name, pod in burners.items()}
    delete("phase7=7.2")

    records = []
    for name in probes:
        load = results.get(name)
        rec = {
            "class": name,
            "burner_cpu_seconds": (
                (
                    (after[name] or {}).get("usage_usec", 0)
                    - (before[name] or {}).get("usage_usec", 0)
                )
                / 1e6
                if after.get(name) and before.get(name)
                else None
            ),
            "burner_cpu_max": (before_stats.get(name) or {}).get("cpu_max"),
            "probe_cgroup": (before_stats.get(name) or {}).get("cgroup_dir"),
            "warmup_reading_not_baseline": base_loads.get(name, {}).get("load"),
            "under_contention": load,
        }
        if errors.get(name):
            rec["error"] = errors[name]
        records.append(rec)

        base = rec["warmup_reading_not_baseline"]
        cont = load
        print(
            f"  {name:<11} burner_cpu={('%.1f' % rec['burner_cpu_seconds']) if rec['burner_cpu_seconds'] is not None else 'n/a':>6}s "
            f"| warmup {base['achieved_rps'] if base else 0:7.1f} rps "
            f"| contended {cont['achieved_rps'] if cont else 0:7.1f} rps "
            f"p99 {(cont['latency_ms']['p99'] if cont else 0):7.2f} ms"
        )

    return {
        "status": "ok",
        "node_cores": cores,
        "burners_per_class": 1,
        "probes_per_class": 1,
        "burner_workload": "single-threaded busy loop in an nginx container",
        "contention_ratio": f"{len(burners)} CPU-bound pods on {cores} cores",
        "rate_rps": rate,
        "duration_sec": duration,
        "records": records,
    }


def node_override():
    return os.environ.get("PHASE7_NODE") or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--rate", type=int, default=4000)
    args = parser.parse_args()

    print("=" * 78)
    print("Phase 7 Topic 7.2: QoS class comparison")
    print("=" * 78)

    kubectl("create", "namespace", NAMESPACE)
    delete("phase7=7.2")

    node = na.NodeAccess(node_name=node_override(), namespace=NAMESPACE)
    if not node.available():
        print(f"node access unavailable: {node.describe()}")
        return 1

    print(f"node: {node.node} via {node.describe()['method']}")

    control_plane, node = part_a_control_plane(node)
    payload = {
        "test": "qos-classes",
        "topic": "7.2",
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "node": node.node,
        "access_method": node.describe()["method"],
        "workload_image": NGINX_DIGEST,
        "control_plane": control_plane,
        "contention": part_b_contention(node, args.duration, args.rate),
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / f"qos-classes-{payload['timestamp']}.json"
    out.write_text(json.dumps(payload, indent=2))

    ok = (
        all(r.get("status") == "ok" for r in control_plane)
        and payload["contention"].get("status") == "ok"
    )
    print()
    print(f"Evidence: {out}")
    print(f"Result: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
