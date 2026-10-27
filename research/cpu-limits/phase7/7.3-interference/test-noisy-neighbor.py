#!/usr/bin/env python3
"""
Phase 7 Topic 7.3: noisy neighbour interference

Measures how a CPU-limited neighbour affects an unrelated latency-sensitive
pod that shares the node.

Each round uses the same number of CPU burners and the same victim:

  unlimited   every burner has no CPU limit
  limited     every burner is capped at 200m

All burners need CPU, so on a node that is oversubscribed by the sum of
their demands the unlimited round forces the victim to compete for cores.
Kubernetes gives all BestEffort pods equal weight, so the victim cannot
escape the competition by priority alone. If the victim's latency improves
once the burners are capped, the cap reduced the interference.

The burn runs as a shell loop inside a node-cached nginx container;
`stress` is not used because its image is absent from the nodes.

Usage:
    python3 test-noisy-neighbor.py [--duration 45] [--rate 3000]

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

EVIDENCE_DIR = (
    Path(__file__).parent.parent.parent / "evidence" / "phase7" / "interference"
)
NAMESPACE = "cpu-limits-research"

NGINX_DIGEST = (
    "docker.io/library/nginx@sha256:"
    "09369da6b10306312cd908661320086bf87fbae1b6b0c49a1f50ba531fef2eab"
)
LOADGEN = Path(__file__).parent.parent / "lib" / "loadgen.py"
LOADGEN_REMOTE = "/tmp/phase7-loadgen.py"

BURN_COMMAND = ["sh", "-c", "while :; do :; done"]
BURNER_COUNT = 4
LIMITED_QUOTA = "200m"

# (name, burner limits, victim request, victim limits)
SCENARIOS = [
    (
        "unlimited",
        None,
        {"cpu": "250m", "memory": "128Mi"},
        {"cpu": "500m", "memory": "256Mi"},
    ),
    (
        "limited",
        {"cpu": LIMITED_QUOTA, "memory": "128Mi"},
        {"cpu": "250m", "memory": "128Mi"},
        {"cpu": "500m", "memory": "256Mi"},
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


def pod_manifest(name, role, node, requests=None, limits=None, command=None):
    container = {
        "name": "app",
        "image": NGINX_DIGEST,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": 80}],
    }
    if requests or limits:
        container["resources"] = {}
        if requests:
            container["resources"]["requests"] = dict(requests)
        if limits:
            container["resources"]["limits"] = dict(limits)
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
        "metadata": {"name": name, "labels": {"phase7": "7.3", "role": role}},
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


def pod_ip(name):
    return kubectl(
        "get", "pod", name, "-n", NAMESPACE, "-o", "jsonpath={.status.podIP}"
    ).stdout.strip()


def cpu_stat(node, pod):
    mapping = na.pod_container_cgroups(node, pod, namespace=NAMESPACE)
    if not mapping:
        return None
    return na.parse_cpu_stat(node.read(f"{next(iter(mapping.values()))}/cpu.stat"))


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


def scenario(node, name, burner_limits, victim_requests, victim_limits, duration, rate):
    print(f"\n{'-' * 78}")
    print(
        f"scenario: {name} ({BURNER_COUNT} burners, limits={burner_limits or 'none'})"
    )
    print(f"{'-' * 78}")

    delete("phase7=7.3")

    victim = f"phase7-victim-{name}"
    applied = apply(
        pod_manifest(victim, "victim", node.node, victim_requests, victim_limits)
    )
    if applied.returncode != 0:
        return {
            "scenario": name,
            "status": "failed",
            "error": applied.stderr.strip()[:300],
        }

    burners = []
    for i in range(BURNER_COUNT):
        burner = f"phase7-noisy-{name}-{i}"
        # Burners request 100m so they are Burstable rather than sharing
        # the victim's BestEffort weight; the cap is what changes.
        applied = apply(
            pod_manifest(
                burner,
                "burner",
                node.node,
                {"cpu": "100m", "memory": "64Mi"},
                burner_limits,
                command=BURN_COMMAND,
            )
        )
        if applied.returncode != 0:
            delete("phase7=7.3")
            return {
                "scenario": name,
                "status": "failed",
                "error": f"{burner}: {applied.stderr.strip()[:300]}",
            }
        burners.append(burner)

    for pod in [victim, *burners]:
        ok, reason = wait_ready(pod)
        if not ok:
            delete("phase7=7.3")
            return {"scenario": name, "status": "failed", "error": f"{pod}: {reason}"}

    if not node.put(str(LOADGEN), LOADGEN_REMOTE):
        delete("phase7=7.3")
        return {
            "scenario": name,
            "status": "failed",
            "error": "could not stage loadgen.py",
        }

    before = {p: cpu_stat(node, p) for p in [victim, *burners]}
    ip = pod_ip(victim)
    load, err = run_load(node, f"http://{ip}:80/", rate, duration)
    after = {p: cpu_stat(node, p) for p in [victim, *burners]}
    delete("phase7=7.3")

    if load is None:
        return {"scenario": name, "status": "failed", "error": err}

    def used(pod):
        b, a = before.get(pod), after.get(pod)
        if not b or not a:
            return None
        return (a.get("usage_usec", 0) - b.get("usage_usec", 0)) / 1e6

    burner_usage = [used(p) for p in burners]
    throttle = []
    for p in burners:
        b, a = before.get(p), after.get(p)
        if b and a:
            periods = a.get("nr_periods", 0) - b.get("nr_periods", 0)
            throttled = a.get("nr_throttled", 0) - b.get("nr_throttled", 0)
            throttle.append(throttled / periods * 100 if periods and throttled else 0.0)

    record = {
        "scenario": name,
        "status": "ok",
        "burners": BURNER_COUNT,
        "burner_limits": burner_limits,
        "burner_cpu_seconds": burner_usage,
        "burner_total_cpu_seconds": (
            round(sum(u for u in burner_usage if u is not None), 2)
            if any(u is not None for u in burner_usage)
            else None
        ),
        "burner_throttle_pct_mean": (
            round(sum(throttle) / len(throttle), 2) if throttle else None
        ),
        "victim_requests": victim_requests,
        "victim_limits": victim_limits,
        "victim_cpu_seconds": used(victim),
        "rate_rps": rate,
        "duration_sec": duration,
        "load": load,
    }
    lat = load["latency_ms"]
    print(
        f"  burners used {record['burner_total_cpu_seconds']}s CPU "
        f"(throttled {record['burner_throttle_pct_mean']}%)"
    )
    print(
        f"  victim: {load['achieved_rps']:7.1f} rps "
        f"p50 {lat['p50']:6.2f} ms p99 {lat['p99']:7.2f} ms "
        f"p99.9 {lat['p99.9']:7.2f} ms max {lat['max']:7.1f} ms"
    )
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=45)
    parser.add_argument("--rate", type=int, default=3000)
    args = parser.parse_args()

    print("=" * 78)
    print("Phase 7 Topic 7.3: noisy neighbour interference")
    print("=" * 78)

    kubectl("create", "namespace", NAMESPACE)
    delete("phase7=7.3")

    node = na.NodeAccess(
        node_name=os.environ.get("PHASE7_NODE") or None, namespace=NAMESPACE
    )
    if not node.available():
        print(f"node access unavailable: {node.describe()}")
        return 1
    print(f"node: {node.node} via {node.describe()['method']}")

    cores = kubectl(
        "get", "node", node.node, "-o", "jsonpath={.status.capacity.cpu}"
    ).stdout.strip()

    results = []
    for name, burner_limits, victim_req, victim_lim in SCENARIOS:
        try:
            results.append(
                scenario(
                    node,
                    name,
                    burner_limits,
                    victim_req,
                    victim_lim,
                    args.duration,
                    args.rate,
                )
            )
        except Exception as exc:  # one broken scenario must not hide the other
            results.append(
                {"scenario": name, "status": "failed", "error": str(exc)[:400]}
            )

    unlimited = next(
        (r for r in results if r["scenario"] == "unlimited" and r["status"] == "ok"),
        None,
    )
    limited = next(
        (r for r in results if r["scenario"] == "limited" and r["status"] == "ok"), None
    )

    comparison = None
    if unlimited and limited:
        u, l = unlimited["load"]["latency_ms"], limited["load"]["latency_ms"]
        comparison = {
            "p50_change_pct": (l["p50"] - u["p50"]) / u["p50"] * 100,
            "p99_change_pct": (l["p99"] - u["p99"]) / u["p99"] * 100,
            "p999_change_pct": (l["p99.9"] - u["p99.9"]) / u["p99.9"] * 100,
            "throughput_change_pct": (
                limited["load"]["achieved_rps"] - unlimited["load"]["achieved_rps"]
            )
            / unlimited["load"]["achieved_rps"]
            * 100,
        }

    payload = {
        "test": "noisy-neighbor",
        "topic": "7.3",
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "node": node.node,
        "node_cores": cores,
        "access_method": node.describe()["method"],
        "workload_image": NGINX_DIGEST,
        "scenarios": results,
        "limited_vs_unlimited": comparison,
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / f"noisy-neighbor-{payload['timestamp']}.json"
    out.write_text(json.dumps(payload, indent=2))

    print()
    if comparison:
        print(
            f"limited vs unlimited: p50 {comparison['p50_change_pct']:+.1f}% "
            f"p99 {comparison['p99_change_pct']:+.1f}% "
            f"p99.9 {comparison['p999_change_pct']:+.1f}% "
            f"throughput {comparison['throughput_change_pct']:+.1f}%"
        )
    print(f"Evidence: {out}")
    ok = all(r["status"] == "ok" for r in results)
    print(f"Result: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
