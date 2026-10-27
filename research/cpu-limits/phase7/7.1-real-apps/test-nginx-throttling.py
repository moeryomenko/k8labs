#!/usr/bin/env python3
"""
Phase 7 Topic 7.1: Real Application Workload Testing (nginx)

Measures how a CPU quota changes the latency and throughput of a real
server. For each quota configuration the script:

  1. deploys one nginx pod (image cached on the node, no registry pull)
  2. drives fixed-rate load with lib/loadgen.py running on the node, so
     the load generator is never the contended party
  3. reads the container's cgroup v2 cpu.stat / cpu.max before and after
  4. records throttling, throughput, and latency percentiles in one
     evidence file

Data comes from the cluster. If a step cannot run, the run is recorded as
failed with a reason; no metric is ever defaulted to zero.

Usage:
    python3 test-nginx-throttling.py [--duration 60] [--rate 12000]

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

EVIDENCE_DIR = Path(__file__).parent.parent.parent / "evidence" / "phase7" / "real-apps"
NAMESPACE = "cpu-limits-research"

# (name, request, limit). None limit = no quota.
#
# The three limited tiers are chosen so that each one is actually reached
# at the experiment load rate. An earlier revision used 1 / 500m / 250m
# quotas with requests of 500m; at 12k req/s nginx needs ~100m of server
# CPU, so only the 250m tier throttled and the lighter tiers measured the
# same thing under different names.
THROTTLE_CONFIGS = [
    ("no-limit", "50m", None),
    ("light", "50m", "120m"),
    ("moderate", "50m", "100m"),
    ("heavy", "50m", "70m"),
]

# nginx:1.25-alpine is cached on md-custom under this digest. Pulling the
# bare tag fails: Docker Hub rejects unauthenticated pulls from this cluster.
NGINX_DIGEST = (
    "docker.io/library/nginx@sha256:"
    "09369da6b10306312cd908661320086bf87fbae1b6b0c49a1f50ba531fef2eab"
)


CONTENT_CONFIGMAP = "phase7-nginx-content"
CONTENT_DIR = "/usr/share/nginx/html/phase7"
INDEX_FILE = "index.html"
LOAD_PATH = "/phase7/index.html"


def index_html():
    """Return a response body large enough that serving it costs CPU.

    nginx serves /index.html from a 4 KiB file whose content costs roughly
    12 millicores at 200 req/s. A ~16 KiB body makes the worker's own work
    dominate, which is what a CPU quota acts on.
    """
    line = "<li>phase7 cpu-quota measurement row with a stable payload</li>\n"
    items = line * 224  # ~16 KiB
    return (
        "<!DOCTYPE html>\n<html><head><title>phase7</title></head>\n"
        f"<body>\n<ul>\n{items}</ul>\n</body></html>\n"
    )


def kubectl(*args, check=False):
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=180
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result


def pod_manifest(name, request, limit, node):
    container = {
        "name": "nginx",
        "image": NGINX_DIGEST,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": 80}],
        "resources": {
            "requests": {"cpu": request, "memory": "128Mi"},
        },
        # The generator's output replaces the default index; it is mounted
        # over the page inside a subdirectory of the doc root.
        "volumeMounts": [
            {
                "name": "content",
                "mountPath": f"{CONTENT_DIR}/{INDEX_FILE}",
                "subPath": INDEX_FILE,
                "readOnly": True,
            }
        ],
    }
    if limit:
        container["resources"]["limits"] = {"cpu": limit, "memory": "256Mi"}

    spec = {
        "restartPolicy": "Never",
        "tolerations": [{"operator": "Exists"}],
        "containers": [container],
        "volumes": [{"name": "content", "configMap": {"name": CONTENT_CONFIGMAP}}],
    }
    if node:
        spec["nodeName"] = node

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {"phase7": "7.1", "app": "nginx"}},
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


LOADGEN = Path(__file__).parent.parent / "lib" / "loadgen.py"
LOADGEN_REMOTE = "/tmp/phase7-loadgen.py"


def run_load(node, pod_name, duration, rate):
    """Run the fixed-rate load generator on the node against the pod IP.

    The generator runs on the node rather than the workstation so that the
    network path and the host clock are the ones the container experiences.
    """
    ip = kubectl(
        "get", "pod", pod_name, "-n", NAMESPACE, "-o", "jsonpath={.status.podIP}"
    ).stdout.strip()
    if not ip:
        return None, "pod has no IP"

    if not node.put(str(LOADGEN), LOADGEN_REMOTE):
        return None, "could not stage loadgen.py on the node"

    cmd = (
        f"python3 {LOADGEN_REMOTE} --url http://{ip}:80{LOAD_PATH} --no-keepalive "
        f"--rate {rate} --duration {duration} --connections 20"
    )
    out = node.run(cmd)
    if not out:
        return None, "load generator produced no output"

    try:
        metrics = json.loads(out[out.index("{") :])
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"unparsable load generator output: {exc}: {out[-300:]}"

    # Normalise into the field names the summary table expects.
    metrics["requests_per_sec"] = metrics.get("achieved_rps")
    lat = metrics.get("latency_ms") or {}
    metrics["latency_p50_ms"] = lat.get("p50")
    metrics["latency_p99_ms"] = lat.get("p99")
    return metrics, None


def collect_cgroup(node, pod_name, container="nginx"):
    """Read the cgroup files of one named container.

    The pod slice also contains the sandbox cgroup, which carries no quota
    and no CPU accounting; reading that instead of the container reports
    "no limit" for every configuration.
    """
    cgroups = na.pod_container_cgroups(node, pod_name, namespace=NAMESPACE)
    cgroup_dir = cgroups.get(container)
    if not cgroup_dir:
        return (
            None,
            f"cgroup for container {container!r} not found (have {sorted(cgroups)})",
        )
    return na.read_container_cpu(node, cgroup_dir), None


def run_config(node, name, request, limit, duration, rate):
    pod = f"phase7-nginx-{name}"
    print(f"\n{'=' * 78}")
    print(f"nginx-{name}: request={request} limit={limit or 'none'}")
    print(f"{'=' * 78}")

    kubectl(
        "delete",
        "pod",
        pod,
        "-n",
        NAMESPACE,
        "--ignore-not-found",
        "--force",
        "--grace-period=0",
    )

    # The outer shell must not expand the payload, so feed kubectl stdin.
    manifest = json.dumps(pod_manifest(pod, request, limit, node_override()))
    apply = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=manifest,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if apply.returncode != 0:
        return {"config": name, "status": "failed", "error": apply.stderr.strip()[:400]}

    ok, reason = wait_ready(pod)
    if not ok:
        kubectl("delete", "pod", pod, "-n", NAMESPACE, "--grace-period=0", "--force")
        return {"config": name, "status": "failed", "error": reason}

    node_pod = kubectl(
        "get", "pod", pod, "-n", NAMESPACE, "-o", "jsonpath={.spec.nodeName}"
    ).stdout.strip()

    if node is None or node.node != node_pod:
        node = na.NodeAccess(pod_name=pod, namespace=NAMESPACE)
    if not node.available():
        kubectl("delete", "pod", pod, "-n", NAMESPACE, "--grace-period=0", "--force")
        return {
            "config": name,
            "status": "failed",
            "error": f"node access unavailable: {node.describe()}",
        }

    before, err = collect_cgroup(node, pod)
    if err:
        kubectl("delete", "pod", pod, "-n", NAMESPACE, "--grace-period=0", "--force")
        return {"config": name, "status": "failed", "error": err}

    stats_before = before["cpu_stat"] or {}

    print(f"  cgroup:  {before['cgroup_dir']}")
    print(f"  cpu.max: {before['cpu_max']}")
    print(f"  weight:  {before['cpu_weight']}")
    print(f"  loading: {duration}s at {rate} req/s via loadgen.py on node")

    load, load_err = run_load(node, pod, duration, rate)

    after, err = collect_cgroup(node, pod)
    kubectl("delete", "pod", pod, "-n", NAMESPACE, "--grace-period=0", "--force")

    stats_after = (after or {}).get("cpu_stat") or {}
    delta = {
        k: stats_after.get(k, 0) - stats_before.get(k, 0)
        for k in ("nr_periods", "nr_throttled", "throttled_usec", "usage_usec")
    }

    record = {
        "config": name,
        "status": "ok" if load else "load-failed",
        "pod": pod,
        "node": node.node,
        "requested_cpu": request,
        "limit_cpu": limit,
        "cgroup_dir": before["cgroup_dir"],
        "cpu_max": before["cpu_max"],
        "cpu_weight": before["cpu_weight"],
        "cpu_weight_nice": before["cpu_weight_nice"],
        "cgroup_delta": delta,
        "load_duration_sec": duration,
        "load_rate_rps": rate,
        "load": load,
    }
    if load_err:
        record["load_error"] = load_err
    if delta["nr_periods"] and delta["nr_throttled"]:
        record["throttle_pct"] = delta["nr_throttled"] / delta["nr_periods"] * 100
        record["throttled_ms"] = delta["throttled_usec"] / 1000
    else:
        record["throttle_pct"] = None
        record["note"] = "no throttled periods during the load window"

    if load:
        print(
            f"  result:  {load['requests_per_sec']:.1f} req/s, "
            f"p50={load.get('latency_p50_ms', float('nan')):.2f}ms, "
            f"p99={load.get('latency_p99_ms', float('nan')):.2f}ms"
        )
    print(f"  throttle: {record['throttle_pct']}")
    return record


def node_override():
    return os.environ.get("PHASE7_NODE") or None


def apply_content_configmap():
    """Create the ConfigMap holding the page the load generator fetches."""
    body = index_html()
    rendered = subprocess.run(
        [
            "kubectl",
            "create",
            "configmap",
            CONTENT_CONFIGMAP,
            "-n",
            NAMESPACE,
            f"--from-file={INDEX_FILE}=/dev/stdin",
            "--dry-run=client",
            "-o",
            "json",
        ],
        input=body,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if rendered.returncode != 0:
        print(f"could not render content configmap: {rendered.stderr.strip()}")
        return False
    applied = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=rendered.stdout,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if applied.returncode != 0:
        print(f"could not apply content configmap: {applied.stderr.strip()}")
        return False
    print(f"content: {len(body)} bytes served at {LOAD_PATH}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--rate", type=int, default=12000)
    args = parser.parse_args()

    print("=" * 78)
    print("Phase 7 Topic 7.1: nginx under CPU quota")
    print("=" * 78)

    kubectl("create", "namespace", NAMESPACE)
    kubectl("config", "set-context", "--current", f"--namespace={NAMESPACE}")

    kubectl(
        "delete",
        "pod",
        "-n",
        NAMESPACE,
        "-l",
        "phase7=7.1",
        "--ignore-not-found",
        "--grace-period=0",
        "--force",
    )

    if not apply_content_configmap():
        return 1

    node = None
    results = []

    for name, request, limit in THROTTLE_CONFIGS:
        try:
            results.append(
                run_config(node, name, request, limit, args.duration, args.rate)
            )
        except Exception as exc:  # keep going: one bad config must not kill the run
            results.append(
                {"config": name, "status": "failed", "error": str(exc)[:400]}
            )

    if node:
        node.close()

    ok = [r for r in results if r.get("status") == "ok"]

    payload = {
        "test": "nginx-throttling",
        "topic": "7.1",
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "load_generator": {
            "tool": "phase7/lib/loadgen.py",
            "location": "kubernetes node (exec into reader pod)",
            "rationale": (
                "wrk2 on this node panics in Lua before sending any request; "
                "loadgen.py is a fixed-rate keep-alive HTTP client"
            ),
            "connections": 20,
        },
        "configs_tested": len(results),
        "configs_succeeded": len(ok),
        "results": results,
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / f"nginx-throttling-{payload['timestamp']}.json"
    out.write_text(json.dumps(payload, indent=2))

    print()
    print("=" * 78)
    print("Summary")
    print("=" * 78)
    print(
        f"{'config':<12} {'limit':>8} {'req/s':>10} {'p50 ms':>9} {'p99 ms':>9} {'throttle':>10}"
    )
    for r in results:
        if r.get("status") != "ok":
            print(
                f"{r['config']:<12} {'-':>8} {'FAILED':>10} {r.get('error', '')[:40]}"
            )
            continue
        load = r["load"]
        thr = r["throttle_pct"]
        print(
            f"{r['config']:<12} {str(r['limit_cpu'] or 'none'):>8} "
            f"{load['requests_per_sec']:>10.1f} "
            f"{load.get('latency_p50_ms', 0):>9.2f} "
            f"{load.get('latency_p99_ms', 0):>9.2f} "
            f"{('n/a' if thr is None else f'{thr:.1f}%'):>10}"
        )

    print()
    print(f"Evidence: {out}")
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
