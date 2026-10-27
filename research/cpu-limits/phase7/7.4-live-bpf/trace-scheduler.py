#!/usr/bin/env python3
"""
Phase 7 Topic 7.4: live BPF scheduling trace under CPU quota

The cgroup counters show that throttling happened. This experiment shows
what it does to the scheduler: when a task becomes runnable, how long does
it wait before a CPU runs it?

Method:

  1. deploy one nginx pod with a known CPU limit and drive load at it
  2. while the load runs, attach a bpftrace program (scheduler-trace.bt) on
     the node that measures, for tasks named "nginx":
       - sched_wakeup  : the task became runnable
       - sched_switch  : the task got or lost a CPU
     The wait between those two events is the scheduling delay.
  3. repeat for a second limit

The same trace runs for both limits, so any difference in wait time comes
from the quota. A run with no limit is included as the control.

bpftrace must run with tracing privileges. The reader pod is unprivileged,
but its container image ships bpftrace and the pod already sees tracefs,
which is what makes this measurement possible without a privileged pod.

Usage:
    python3 trace-scheduler.py [--duration 30] [--rate 6000]

Environment:
    PHASE7_NAMESPACE  (default: cpu-limits-research)
    PHASE7_NODE       (optional) pin the pod to one node
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

EVIDENCE_DIR = Path(__file__).parent.parent.parent / "evidence" / "phase7" / "live-bpf"
NAMESPACE = "cpu-limits-research"

NGINX_DIGEST = (
    "docker.io/library/nginx@sha256:"
    "09369da6b10306312cd908661320086bf87fbae1b6b0c49a1f50ba531fef2eab"
)
LOADGEN = Path(__file__).parent.parent / "lib" / "loadgen.py"
LOADGEN_REMOTE = "/tmp/phase7-loadgen.py"
TRACE_LOCAL = Path(__file__).parent / "scheduler-trace.bt"
TRACE_REMOTE = "/tmp/phase7-scheduler-trace.bt"

# Traffic costs the server about 36 us of CPU per request, so 6000 req/s
# needs roughly 0.2 cores: close to the caps below but not far above them.
CONFIGS = [
    ("no-limit", "200m", None),
    ("capped", "200m", "200m"),
    ("tight", "100m", "100m"),
]


def kubectl(*args):
    return subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=180
    )


def pod_manifest(name, request, limit, node):
    container = {
        "name": "nginx",
        "image": NGINX_DIGEST,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": 80}],
        "resources": {"requests": {"cpu": request, "memory": "128Mi"}},
    }
    if limit:
        container["resources"]["limits"] = {"cpu": limit, "memory": "256Mi"}
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
        "metadata": {"name": name, "labels": {"phase7": "7.4"}},
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
        "phase7=7.4",
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


def pod_ip(name):
    return kubectl(
        "get", "pod", name, "-n", NAMESPACE, "-o", "jsonpath={.status.podIP}"
    ).stdout.strip()


def parse_trace(output):
    """Read the JSON line the trace prints from its END block."""
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line), None
            except json.JSONDecodeError as exc:
                return None, f"unparsable trace JSON: {exc}"
    return None, f"no JSON in trace output: {(output or '')[-300:]}"


def trace_and_load(node, url, rate, duration):
    """Run bpftrace and the load generator at the same time on the node.

    bpftrace runs in the background and is interrupted with SIGINT so that
    its END block runs; the load generator runs in the foreground.
    """
    inner = (
        f"bpftrace {TRACE_REMOTE} nginx > /tmp/phase7-trace.out 2>&1 & "
        f"TRACE_PID=$!; sleep 2; "
        f"python3 {LOADGEN_REMOTE} --url {url} --no-keepalive "
        f"--rate {rate} --duration {duration} --connections 20 "
        f"> /tmp/phase7-load.out 2>&1; "
        f"kill -INT $TRACE_PID; wait $TRACE_PID; "
        f"cat /tmp/phase7-load.out; echo '---TRACE---'; cat /tmp/phase7-trace.out"
    )
    # node.run already executes the command through `sh -c`, so the
    # script is passed as-is rather than wrapped in a second shell.
    out = node.run(inner)
    if not out:
        return None, None, "node produced no output"
    if "---TRACE---" not in out:
        return None, None, f"split marker missing: {out[-300:]}"
    load_text, trace_text = out.split("---TRACE---", 1)

    # The load generator prints one indented JSON object, which spans many
    # lines, so parse from its opening brace rather than per line.
    load = None
    brace = load_text.find("{")
    if brace != -1:
        try:
            load = json.loads(load_text[brace:])
        except json.JSONDecodeError as exc:
            return None, None, f"unparsable load generator output: {exc}"
    trace, err = parse_trace(trace_text)
    if load is None:
        return None, trace, "load generator produced no JSON"
    return load, trace, err


def scenario(node, name, request, limit, duration, rate):
    print(f"\n{'-' * 78}")
    print(f"config: {name} (request={request} limit={limit or 'none'})")
    print(f"{'-' * 78}")

    delete()
    pod = f"phase7-trace-{name}"
    applied = apply(pod_manifest(pod, request, limit, node.node))
    if applied.returncode != 0:
        return {
            "config": name,
            "status": "failed",
            "error": applied.stderr.strip()[:300],
        }
    ok, reason = wait_ready(pod)
    if not ok:
        delete()
        return {"config": name, "status": "failed", "error": reason}

    mapping = na.pod_container_cgroups(node, pod, namespace=NAMESPACE)
    cgroup_dir = mapping.get("nginx")
    before = None
    if cgroup_dir:
        before = na.parse_cpu_stat(node.read(f"{cgroup_dir}/cpu.stat"))

    ip = pod_ip(pod)
    load, trace, err = trace_and_load(node, f"http://{ip}:80/", rate, duration)

    after = None
    if cgroup_dir:
        after = na.parse_cpu_stat(node.read(f"{cgroup_dir}/cpu.stat"))
    delete()

    delta = None
    if before and after:
        delta = {
            key: after.get(key, 0) - before.get(key, 0)
            for key in ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")
        }

    record = {
        "config": name,
        "status": "ok" if load is not None else "failed",
        "request": request,
        "limit": limit,
        "cgroup_dir": cgroup_dir,
        "cgroup_delta": delta,
        "throttle_pct": na.throttle_pct(delta) if delta else None,
        "rate_rps": rate,
        "duration_sec": duration,
        "load": load,
        "trace": trace,
    }
    if err:
        record["error"] = err

    lat = (load or {}).get("latency_ms") or {}
    print(f"  throttle: {record['throttle_pct']}%")
    if load:
        print(
            f"  load:  {load['achieved_rps']:7.1f} rps "
            f"p50 {lat.get('p50', 0):6.2f} ms p99 {lat.get('p99', 0):7.2f} ms"
        )
    if trace:
        print(
            f"  trace: {trace['wakeups']} wakeups, "
            f"{trace['switches_in']} switches in, mean wait "
            f"{trace['wait_mean_us']} us, max {trace['wait_max_us']} us"
        )
    elif err:
        print(f"  trace: {err}")
    return record


def node_override():
    return os.environ.get("PHASE7_NODE") or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--rate", type=int, default=6000)
    args = parser.parse_args()

    print("=" * 78)
    print("Phase 7 Topic 7.4: live BPF scheduling trace")
    print("=" * 78)

    kubectl("create", "namespace", NAMESPACE)
    delete()

    node = na.NodeAccess(node_name=node_override(), namespace=NAMESPACE)
    if not node.available():
        print(f"node access unavailable: {node.describe()}")
        return 1
    print(f"node: {node.node} via {node.describe()['method']}")

    for local, remote, label in (
        (TRACE_LOCAL, TRACE_REMOTE, "trace program"),
        (LOADGEN, LOADGEN_REMOTE, "load generator"),
    ):
        if not node.put(str(local), remote):
            print(f"could not stage the {label} on the node")
            return 1

    version = node.run("bpftrace --version 2>&1 | head -1") or ""
    print(f"bpftrace: {version.strip() or '(version unknown)'}")

    results = []
    for name, request, limit in CONFIGS:
        try:
            results.append(
                scenario(node, name, request, limit, args.duration, args.rate)
            )
        except Exception as exc:  # one bad config must not hide the others
            results.append(
                {"config": name, "status": "failed", "error": str(exc)[:400]}
            )

    payload = {
        "test": "scheduler-trace",
        "topic": "7.4",
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "node": node.node,
        "access_method": node.describe()["method"],
        "kernel": (node.read("/proc/sys/kernel/osrelease") or "").strip() or None,
        "bpftrace_version": version.strip() or None,
        "trace_program": TRACE_LOCAL.name,
        "traced_comm": "nginx",
        "workload_image": NGINX_DIGEST,
        "results": results,
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / f"scheduler-trace-{payload['timestamp']}.json"
    out.write_text(json.dumps(payload, indent=2))

    print()
    for r in results:
        if r["status"] != "ok" or not r.get("trace"):
            continue
        print(
            f"  {r['config']:<9} throttle {str(r['throttle_pct']):>6}% "
            f"| mean wait {r['trace']['wait_mean_us']} us "
            f"max {r['trace']['wait_max_us']} us"
        )
    print(f"Evidence: {out}")

    ok = all(r["status"] == "ok" and r.get("trace") for r in results)
    print(f"Result: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
