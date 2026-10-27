#!/usr/bin/env python3
"""
Phase 7 Topic 7.7: production incident simulation

Four incidents, each measured from the client side while the cluster does the
thing that operators actually page for:

  spike      a sudden traffic spike against a 120m-limited pod and against an
             unlimited pod: what is served, what times out, how much is
             throttled, and how long recovery takes
  ramp       a gradual load increase against both arms, to find the offered
             rate at which a capped pod stops keeping up, followed by the same
             ramp behind an HPA that can add replicas
  eviction   one of three replicas is deleted under load: what the service
             sees while the replacement starts, and how long that takes
  drain      a node is cordoned and drained under load: drain duration, where
             the replicas land, and the client-side cost

The measurements are client-side on purpose. A throttled pod's own metrics
history stops exactly when it is evicted or rescheduled, so the load
generator is the only witness that survives the incident. Per-pod cgroup
counters are recorded where the pod is still on the node that the reader pod
mounts, and are reported as absent where it is not.

The load generator runs in the reader pod that mounts the node's cgroupfs
(`phase7/lib/node_access.py`). Nothing is fabricated: a missing sample is
null with a reason, never 0.

Usage:
    python3 test-incidents.py [--scenarios spike,ramp,eviction,drain]
                              [--drain-node NAME]

Environment:
    PHASE7_NAMESPACE  (default: cpu-limits-research)
    PHASE7_NODE       (default: the node running 7.3.0-k8labs)
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import node_access as na  # noqa: E402

RESEARCH = Path(__file__).parent.parent.parent
EVIDENCE_DIR = RESEARCH / "evidence" / "phase7" / "incidents"
NAMESPACE = "cpu-limits-research"

# The 7.1-7.3 experiments used the nginx image above, but the node garbage
# collected it during a disk-pressure episode and Docker Hub now answers
# anonymous pulls with a rate-limit error. The incident scenarios measure the
# shape of the response rather than nginx's absolute throughput, so the target
# is phase7/lib/httpd.c instead: a static server that returns a fixed body and
# keeps the connection alive. It needs no registry, because the only image
# involved is fedora:44, which is already on every node, and the binary is
# placed on the node once and mounted read-only into every replica.
WORKLOAD_IMAGE = "docker.io/library/fedora:44"
HTTPD = Path(__file__).parent.parent / "lib" / "httpd.c"
HTTPD_MOUNT = "/srv"
HTTPD_CONFIGMAP = "phase7-httpd"
# Every request burns 100 microseconds of CPU inside the server, so a 120m
# quota (120 ms per second) saturates at about 1200 requests per second. The
# plain static response cost 20 us, which no plausible quota on this node can
# bind, and an incident that cannot happen measures nothing.
HTTPD_WORK_US = 100
LOADGEN = Path(__file__).parent.parent / "lib" / "loadgen.py"
LOADGEN_REMOTE = "/tmp/phase7-loadgen.py"

# 120m is the limit the 7.1 nginx experiment used, so the spike and ramp
# incidents can be read against those numbers.
LIMITED = "120m"
UNLIMITED = None
# 100m of request per replica is what the HPA divides by: at a 60% target the
# Deployment is asked to keep every pod near 60m, which is a fifth of a core
# on this node.
HPA_REQUEST = "100m"
HPA_LIMIT = "120m"
HPA_TARGET = 60

LABEL = "phase7=7.7"


def kubectl(*args, stdin=None, timeout=240):
    return subprocess.run(
        ["kubectl", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def apply(manifest):
    return kubectl("apply", "-f", "-", stdin=json.dumps(manifest))


def apply_server_side(manifest):
    """Server-side apply, for objects too large for the last-applied annotation.

    kubectl apply records the whole object in an annotation capped at
    256 KiB, which a half-megabyte ConfigMap exceeds. Server-side apply keeps
    its bookkeeping in managedFields instead.
    """
    return kubectl("apply", "--server-side", "-f", "-", stdin=json.dumps(manifest))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def cleanup():
    """Remove every object a previous incident run may have left behind."""
    kubectl(
        "delete",
        "deploy,svc,hpa,pdb,pod,configmap",
        "-n",
        NAMESPACE,
        "-l",
        LABEL,
        "--ignore-not-found",
        "--wait=false",
        timeout=180,
    )


def publish_httpd(build_dir):
    """Build the server and publish it as two ConfigMaps.

    A hostPath does not work here: SELinux labels a file under /var/tmp as
    tmp_t and a container may neither read nor execute it ("exists but it is
    not executable: Operation not permitted", and a plain `cp` is denied just
    the same). Relabelling needs CAP_MAC_ADMIN, and asking for seLinuxOptions
    type spc_t is rejected by this cluster's container runtime. A ConfigMap
    volume is labelled container_file_t, so the only thing a pod needs is cat
    and chmod, which every image has.

    The static binary is 907920 bytes, and base64 of that exceeds the 1 MiB
    per-object limit, so it is split into two ConfigMaps that the init
    container concatenates.
    """
    built = build_dir / "httpd"
    result = subprocess.run(
        ["gcc", "-O2", "-static", "-pthread", "-o", str(built), str(HTTPD)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"static build of httpd failed: {result.stderr.strip()[:300]}"

    payload = built.read_bytes()
    middle = len(payload) // 2
    chunks = [payload[:middle], payload[middle:]]
    for index, chunk in enumerate(chunks):
        name = f"{HTTPD_CONFIGMAP}-{index}"
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "labels": {"phase7": "7.7"}},
            "binaryData": {"part": base64.b64encode(chunk).decode("ascii")},
        }
        applied = apply_server_side(manifest)
        if applied.returncode != 0:
            return f"ConfigMap {name} rejected: {applied.stderr.strip()[:300]}"
    sizes = [len(chunk) for chunk in chunks]
    return None if len(payload) else f"empty binary: {sizes}"


def service(name):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "labels": {"phase7": "7.7"}},
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": name},
            "ports": [{"port": 80, "targetPort": 80}],
        },
    }


def workload_container(requests, limits=None):
    """The httpd container: no registry, fixed body, keep-alive."""
    resources = {"requests": dict(requests)}
    if limits:
        resources["limits"] = dict(limits)
    return {
        "name": "app",
        "image": WORKLOAD_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "command": [
            f"{HTTPD_MOUNT}/httpd",
            "--port",
            "80",
            "--work-us",
            str(HTTPD_WORK_US),
        ],
        "ports": [{"containerPort": 80}],
        "resources": resources,
        "volumeMounts": [{"name": "work", "mountPath": HTTPD_MOUNT}],
    }


def staging_container():
    """Assemble the server from the two ConfigMaps into an emptyDir."""
    return {
        "name": "stage",
        "image": WORKLOAD_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "command": [
            "sh",
            "-c",
            "cat /cm/0/part /cm/1/part > "
            f"{HTTPD_MOUNT}/httpd && chmod 0755 {HTTPD_MOUNT}/httpd",
        ],
        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
        "volumeMounts": [
            {"name": "cm0", "mountPath": "/cm/0", "readOnly": True},
            {"name": "cm1", "mountPath": "/cm/1", "readOnly": True},
            {"name": "work", "mountPath": HTTPD_MOUNT},
        ],
    }


def workload_volumes():
    return [
        {"name": "cm0", "configMap": {"name": f"{HTTPD_CONFIGMAP}-0"}},
        {"name": "cm1", "configMap": {"name": f"{HTTPD_CONFIGMAP}-1"}},
        {"name": "work", "emptyDir": {}},
    ]


def deployment(name, replicas, requests, limits, node=None, prefer_node=None):
    container = workload_container(requests, limits)
    container.update(
        {
            # A probe makes "ready" mean "can answer", which is what the eviction
            # and drain scenarios time.
            "readinessProbe": {
                "httpGet": {"path": "/", "port": 80},
                "initialDelaySeconds": 1,
                "periodSeconds": 2,
            },
        }
    )
    spec = {
        "replicas": replicas,
        "selector": {"matchLabels": {"app": name}},
        "template": {
            "metadata": {"labels": {"app": name, "phase7": "7.7"}},
            "spec": {
                "terminationGracePeriodSeconds": 0,
                "initContainers": [staging_container()],
                "containers": [container],
                "volumes": workload_volumes(),
            },
        },
    }
    if node:
        spec["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": node}
    if prefer_node:
        # A required selector would pin the replacement pods to the node that
        # is being drained, turning the drain into a permanent scheduling
        # failure. A preference puts the replicas there first and still lets
        # the scheduler move them once the node is cordoned.
        spec["template"]["spec"]["affinity"] = {
            "nodeAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "preference": {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": [prefer_node],
                                }
                            ]
                        },
                    }
                ]
            }
        }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": {"phase7": "7.7"}},
        "spec": spec,
    }


def pod_manifest(name, requests, limits, node):
    # Only a pod with no limits at all is the unlimited arm. Filling limits
    # with the requests would make it Guaranteed and change its weight.
    container = workload_container(requests, limits)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {"phase7": "7.7", "role": "target"}},
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0,
            "initContainers": [staging_container()],
            "containers": [container],
            "volumes": workload_volumes(),
        },
    }


def pdb(name, min_available):
    """Keep at least min_available replicas up while the node is drained.

    kubectl drain honours a PodDisruptionBudget, so with three replicas and
    minAvailable 2 it must wait for a replacement before it may evict the
    second pod. Without one, a drain takes every replica at once and the
    service is down until the replacements are scheduled.
    """
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": name, "labels": {"phase7": "7.7"}},
        "spec": {
            "minAvailable": min_available,
            "selector": {"matchLabels": {"app": name}},
        },
    }


def hpa(name, minimum, maximum):
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": name, "labels": {"phase7": "7.7"}},
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": name,
            },
            "minReplicas": minimum,
            "maxReplicas": maximum,
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": HPA_TARGET,
                        },
                    },
                }
            ],
        },
    }


def wait_pod_ready(name, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
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
        phase = kubectl(
            "get", "pod", name, "-n", NAMESPACE, "-o", "jsonpath={.status.phase}"
        ).stdout.strip()
        if phase in ("Failed", "Succeeded"):
            return False, f"pod phase {phase}"
        time.sleep(1)
    return False, f"{name} not ready in {timeout}s"


def wait_deployment_ready(name, replicas, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = kubectl(
            "get",
            "deployment",
            name,
            "-n",
            NAMESPACE,
            "-o",
            "json",
        ).stdout
        try:
            data = json.loads(status)
        except (ValueError, json.JSONDecodeError):
            time.sleep(1)
            continue
        if data.get("status", {}).get("readyReplicas", 0) >= replicas:
            return True, time.time()
        time.sleep(1)
    return False, time.time()


def pod_ip(name):
    return kubectl(
        "get", "pod", name, "-n", NAMESPACE, "-o", "jsonpath={.status.podIP}"
    ).stdout.strip()


def service_ip(name):
    deadline = time.time() + 60
    while time.time() < deadline:
        ip = kubectl(
            "get",
            "service",
            name,
            "-n",
            NAMESPACE,
            "-o",
            "jsonpath={.spec.clusterIP}",
        ).stdout.strip()
        if ip and ip != "None":
            return ip
        time.sleep(1)
    return ""


def ready_endpoints(name, port=80):
    out = kubectl(
        "get",
        "endpoints",
        name,
        "-n",
        NAMESPACE,
        "-o",
        f"jsonpath={{.subsets[*].addresses[*].ip}}",
    ).stdout.strip()
    return len(out.split()) if out else 0


def deployment_state(name):
    out = kubectl("get", "deployment", name, "-n", NAMESPACE, "-o", "json").stdout
    try:
        status = json.loads(out).get("status", {})
    except (ValueError, json.JSONDecodeError):
        return {}
    return {
        "replicas": status.get("replicas", 0),
        "ready": status.get("readyReplicas", 0),
        "available": status.get("availableReplicas", 0),
        "updated": status.get("updatedReplicas", 0),
    }


def hpa_state(name):
    out = kubectl("get", "hpa", name, "-n", NAMESPACE, "-o", "json").stdout
    try:
        data = json.loads(out)
    except (ValueError, json.JSONDecodeError):
        return {}
    status = data.get("status", {})
    util = None
    for metric in status.get("currentMetrics", []) or []:
        if (
            metric.get("type") == "Resource"
            and metric.get("resource", {}).get("name") == "cpu"
        ):
            util = metric["resource"].get("current", {}).get("averageUtilization")
    return {
        "current_replicas": status.get("currentReplicas"),
        "desired_replicas": status.get("desiredReplicas"),
        "current_cpu_utilization_pct": util,
        "conditions": [
            {
                "type": c.get("type"),
                "reason": c.get("reason"),
                "message": c.get("message"),
            }
            for c in status.get("conditions", [])
        ],
    }


def pod_nodes(selector):
    out = kubectl(
        "get",
        "pods",
        "-n",
        NAMESPACE,
        "-l",
        selector,
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}={.spec.nodeName}={.status.phase};{end}",
    ).stdout.strip()
    result = {}
    for chunk in out.split(";"):
        if not chunk:
            continue
        parts = chunk.split("=")
        if len(parts) == 3:
            result[parts[0]] = {"node": parts[1], "phase": parts[2]}
    return result


def load(node, url, rate, duration, connections=20):
    """One fixed-rate window; returns the load generator's JSON or an error."""
    out = node.run(
        f"python3 {LOADGEN_REMOTE} --url {url} --no-keepalive "
        f"--rate {rate} --duration {duration} --connections {connections}"
    )
    if not out:
        return None, "load generator produced no output"
    try:
        return json.loads(out[out.index("{") :]), None
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"unparsable load generator output: {exc}: {out[-200:]}"


def fmt(value, spec=".1f"):
    """Format a possibly missing measurement without pretending it is zero."""
    return format(value, spec) if isinstance(value, (int, float)) else "n/a"


def window_summary(load_result):
    if not load_result:
        return None
    lat = load_result["latency_ms"]
    dispatched = load_result.get("dispatched") or 0
    errors = load_result.get("errors") or 0
    return {
        "offered_rps": load_result.get("target_rate_rps"),
        "achieved_rps": load_result["achieved_rps"],
        "p50_ms": lat["p50"],
        "p99_ms": lat["p99"],
        "p999_ms": lat["p99.9"],
        "max_ms": lat["max"],
        "dispatched": dispatched,
        "completed": load_result.get("completed"),
        "errors": errors,
        "error_rate_pct": round(errors / dispatched * 100, 2) if dispatched else None,
        "error_kinds": load_result.get("error_kinds"),
    }


def cgroup_usage(node, pod_names):
    """CPU seconds and throttling per pod, for pods still on the reader node."""
    out = {}
    for name in pod_names:
        mapping = na.pod_container_cgroups(node, name, namespace=NAMESPACE)
        if not mapping:
            out[name] = None
            continue
        path = f"{next(iter(mapping.values()))}/cpu.stat"
        stats = na.parse_cpu_stat(node.read(path))
        out[name] = {
            "usage_usec": stats.get("usage_usec"),
            "nr_periods": stats.get("nr_periods"),
            "nr_throttled": stats.get("nr_throttled"),
            "throttled_usec": stats.get("throttled_usec"),
        }
    return out


def deployment_pods(name):
    out = kubectl(
        "get",
        "pods",
        "-n",
        NAMESPACE,
        "-l",
        f"app={name}",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    ).stdout
    return out.split()


def usage_delta(before, after):
    if not before or not after:
        return None
    periods = (after.get("nr_periods") or 0) - (before.get("nr_periods") or 0)
    throttled = (after.get("nr_throttled") or 0) - (before.get("nr_throttled") or 0)
    return {
        "cpu_seconds": (
            (after.get("usage_usec") or 0) - (before.get("usage_usec") or 0)
        )
        / 1e6,
        "nr_periods": periods,
        "nr_throttled": throttled,
        "throttle_pct": round(throttled / periods * 100, 2) if periods else None,
        "throttled_seconds": (
            (after.get("throttled_usec") or 0) - (before.get("throttled_usec") or 0)
        )
        / 1e6,
    }


# ---------------------------------------------------------------------------
# 7.7.1 traffic spike
# ---------------------------------------------------------------------------

SPIKE_WINDOWS = [
    ("baseline", 800, 8),
    ("spike", 3000, 15),
    ("recovery", 800, 10),
]


def scenario_spike(node, args):
    """Baseline, spike, recovery for an unlimited and a 120m-capped pod."""
    print("\n" + "-" * 78)
    print("incident: traffic spike (800 -> 3000 -> 800 rps)")
    print("-" * 78)

    if not node.put(str(LOADGEN), LOADGEN_REMOTE):
        return {
            "scenario": "spike",
            "status": "failed",
            "error": "could not stage loadgen.py",
        }

    results = []
    for arm_name, limit in (("unlimited", UNLIMITED), ("limited", LIMITED)):
        pod = f"phase7-spike-{arm_name}"
        # Both arms request the same 100m, so their fair-share weight is
        # equal and the cap is the only difference between them.
        requests = {"cpu": "100m", "memory": "128Mi"}
        applied = apply(
            pod_manifest(
                pod,
                requests,
                {"cpu": limit, "memory": "256Mi"} if limit else None,
                node.node,
            )
        )
        if applied.returncode != 0:
            results.append(
                {
                    "arm": arm_name,
                    "status": "failed",
                    "error": applied.stderr.strip()[:300],
                }
            )
            continue
        ready, reason = wait_pod_ready(pod)
        if not ready:
            results.append({"arm": arm_name, "status": "failed", "error": reason})
            continue

        ip = pod_ip(pod)
        url = f"http://{ip}:80/"
        windows = []
        for phase, offered, duration in SPIKE_WINDOWS:
            before = cgroup_usage(node, [pod]).get(pod)
            started = now_iso()
            load_result, error = load(node, url, offered, duration)
            after = cgroup_usage(node, [pod]).get(pod)
            windows.append(
                {
                    "phase": phase,
                    "offered_rps": offered,
                    "duration_s": duration,
                    "started": started,
                    "load": window_summary(load_result),
                    "error": error,
                    "cpu": usage_delta(before, after),
                }
            )
            if load_result:
                lat = load_result["latency_ms"]
                print(
                    f"  {arm_name:9} {offered:5} rps -> "
                    f"{fmt(load_result['achieved_rps'], '7.1f')} rps "
                    f"p99 {fmt(lat['p99'], '7.2f')} ms errors {load_result['errors']}"
                )
        results.append(
            {
                "arm": arm_name,
                "status": "ok",
                "limit": limit,
                "pod": pod,
                "windows": windows,
            }
        )
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

    return {"scenario": "spike", "status": "ok", "arms": results}


# ---------------------------------------------------------------------------
# 7.7.2 gradual ramp + autoscaling race
# ---------------------------------------------------------------------------

RAMP_STEPS = [
    (300, 6),
    (600, 6),
    (900, 6),
    (1200, 8),
    (1600, 8),
    (2200, 8),
    (3000, 8),
]


def scenario_ramp(node, args):
    """Find the knee of each arm, then run the same ramp behind an HPA."""
    print("\n" + "-" * 78)
    print("incident: gradual load increase and autoscaling race")
    print("-" * 78)

    if not node.put(str(LOADGEN), LOADGEN_REMOTE):
        return {
            "scenario": "ramp",
            "status": "failed",
            "error": "could not stage loadgen.py",
        }

    arms = []
    for arm_name, limit in (("unlimited", UNLIMITED), ("limited", LIMITED)):
        pod = f"phase7-ramp-{arm_name}"
        # Both arms request the same 100m, so their fair-share weight is
        # equal and the cap is the only difference between them.
        requests = {"cpu": "100m", "memory": "128Mi"}
        applied = apply(
            pod_manifest(
                pod,
                requests,
                {"cpu": limit, "memory": "256Mi"} if limit else None,
                node.node,
            )
        )
        if applied.returncode != 0:
            arms.append(
                {
                    "arm": arm_name,
                    "status": "failed",
                    "error": applied.stderr.strip()[:300],
                }
            )
            continue
        ready, reason = wait_pod_ready(pod)
        if not ready:
            arms.append({"arm": arm_name, "status": "failed", "error": reason})
            continue

        url = f"http://{pod_ip(pod)}:80/"
        steps = []
        knee = None
        for offered, duration in RAMP_STEPS:
            before = cgroup_usage(node, [pod]).get(pod)
            load_result, error = load(node, url, offered, duration)
            after = cgroup_usage(node, [pod]).get(pod)
            entry = {
                "offered_rps": offered,
                "duration_s": duration,
                "load": window_summary(load_result),
                "error": error,
                "cpu": usage_delta(before, after),
            }
            steps.append(entry)
            if load_result:
                # A pod with no quota has no throttling periods at all, which
                # is different from being throttled 0% of the time.
                throttle = (entry["cpu"] or {}).get("throttle_pct")
                throttle_text = "n/a" if throttle is None else f"{throttle}%"
                print(
                    f"  {arm_name:9} {offered:5} rps -> "
                    f"{fmt(load_result['achieved_rps'], '7.1f')} rps "
                    f"p99 {fmt(load_result['latency_ms']['p99'], '7.2f')} ms "
                    f"throttled {throttle_text}"
                )
                if knee is None and load_result["achieved_rps"] < offered * 0.95:
                    knee = {
                        "offered_rps": offered,
                        "achieved_rps": load_result["achieved_rps"],
                        "p99_ms": load_result["latency_ms"]["p99"],
                    }
            if error:
                steps[-1]["status"] = "failed"
        arms.append(
            {
                "arm": arm_name,
                "status": "ok",
                "limit": limit,
                "pod": pod,
                "steps": steps,
                "knee": knee,
            }
        )
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

    # Autoscaling race: replicas are added while the load ramps, and the
    # question is whether they arrive before the client notices.
    hpa_result = None
    name = "phase7-hpa"
    metrics = kubectl("get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes")
    if metrics.returncode != 0:
        hpa_result = {
            "status": "skipped",
            "reason": "metrics.k8s.io is unavailable, so an HPA cannot compute a target",
            "error": metrics.stderr.strip()[:200],
        }
    else:
        applied = apply(
            deployment(
                "phase7-hpa",
                1,
                {"cpu": HPA_REQUEST, "memory": "128Mi"},
                {"cpu": HPA_LIMIT, "memory": "256Mi"},
                node=node.node,
            )
        )
        if applied.returncode != 0:
            hpa_result = {"status": "failed", "error": applied.stderr.strip()[:300]}
        else:
            apply(service(name))
            apply(hpa(name, 1, 6))
            ready, _ = wait_deployment_ready(name, 1)
            metric_started = time.time()
            metric_ready = None
            while time.time() - metric_started < 180:
                if hpa_state(name).get("current_cpu_utilization_pct") is not None:
                    metric_ready = round(time.time() - metric_started, 1)
                    break
                time.sleep(2)
            print(f"  hpa metric available after {metric_ready}s")
            ip = service_ip(name)
            timeline = []
            if ready and ip:
                url = f"http://{ip}:80/"
                ramp_start = time.time()
                for offered, duration in RAMP_STEPS:
                    # The HPA's own utilization comes from metrics-server; the
                    # cgroup counters give the same ratio independently, so a
                    # lagging metric is visible instead of inferred.
                    pods = deployment_pods(name)
                    before = cgroup_usage(node, pods)
                    load_result, error = load(node, url, offered, duration)
                    after = cgroup_usage(node, pods)
                    cpu_seconds = 0.0
                    measured = 0
                    for pod_name in pods:
                        delta = usage_delta(before.get(pod_name), after.get(pod_name))
                        if delta:
                            cpu_seconds += delta["cpu_seconds"]
                            measured += 1
                    request_cores = float(str(HPA_REQUEST).rstrip("m")) / 1000
                    timeline.append(
                        {
                            "t_s": round(time.time() - ramp_start, 1),
                            "offered_rps": offered,
                            "duration_s": duration,
                            "load": window_summary(load_result),
                            "error": error,
                            "deployment": deployment_state(name),
                            "hpa": hpa_state(name),
                            "ready_endpoints": ready_endpoints(name),
                            "pods_measured": measured,
                            "cgroup_cpu_seconds": round(cpu_seconds, 3),
                            "cgroup_utilization_pct": round(
                                cpu_seconds
                                / (duration * measured * request_cores)
                                * 100,
                                1,
                            )
                            if measured
                            else None,
                        }
                    )
                    if load_result:
                        dep = timeline[-1]["deployment"]
                        print(
                            f"  hpa       {offered:5} rps -> "
                            f"{fmt(load_result['achieved_rps'], '7.1f')} rps "
                            f"p99 {fmt(load_result['latency_ms']['p99'], '7.2f')} ms "
                            f"replicas {dep.get('ready')}/{dep.get('replicas')} "
                            f"hpa {timeline[-1]['hpa'].get('desired_replicas')} "
                            f"cpu {timeline[-1]['hpa'].get('current_cpu_utilization_pct')}%"
                        )
                hpa_result = {
                    "status": "ok",
                    "metric_ready_seconds": metric_ready,
                    "request": HPA_REQUEST,
                    "limit": HPA_LIMIT,
                    "target_utilization_pct": HPA_TARGET,
                    "timeline": timeline,
                }
            else:
                hpa_result = {
                    "status": "failed",
                    "error": "deployment or service never became ready",
                }
        kubectl(
            "delete",
            "deploy,svc,hpa",
            "-n",
            NAMESPACE,
            "-l",
            LABEL,
            "--ignore-not-found",
            "--wait=false",
        )

    return {"scenario": "ramp", "status": "ok", "arms": arms, "hpa": hpa_result}


# ---------------------------------------------------------------------------
# 7.7.3 pod eviction
# ---------------------------------------------------------------------------


def scenario_eviction(node, args):
    """Delete one of three replicas under load and watch the service absorb it."""
    print("\n" + "-" * 78)
    print("incident: pod eviction (3 replicas, 1000 rps, one replica killed)")
    print("-" * 78)

    if not node.put(str(LOADGEN), LOADGEN_REMOTE):
        return {
            "scenario": "eviction",
            "status": "failed",
            "error": "could not stage loadgen.py",
        }

    name = "phase7-evict"
    applied = apply(
        deployment(
            name,
            3,
            {"cpu": LIMITED, "memory": "128Mi"},
            {"cpu": LIMITED, "memory": "256Mi"},
            node=node.node,
        )
    )
    if applied.returncode != 0:
        return {
            "scenario": "eviction",
            "status": "failed",
            "error": applied.stderr.strip()[:300],
        }
    apply(service(name))
    ready, _ = wait_deployment_ready(name, 3)
    ip = service_ip(name)
    if not ready or not ip:
        kubectl(
            "delete", "deploy,svc", "-n", NAMESPACE, "-l", LABEL, "--ignore-not-found"
        )
        return {
            "scenario": "eviction",
            "status": "failed",
            "error": "3 replicas never became ready",
        }

    pods = pod_nodes(f"app={name}")
    victim = sorted(pods)[0]
    victim_cgroup = cgroup_usage(node, [victim]).get(victim)
    url = f"http://{ip}:80/"

    windows = []
    baseline, error = load(node, url, 1000, 10)
    windows.append(
        {
            "phase": "baseline",
            "t": now_iso(),
            "load": window_summary(baseline),
            "error": error,
            "ready_endpoints": ready_endpoints(name),
        }
    )
    if baseline:
        print(
            f"  baseline 1000 rps -> {fmt(baseline['achieved_rps'])} rps "
            f"p99 {fmt(baseline['latency_ms']['p99'], '.2f')} ms"
        )

    killed_at = time.time()
    kubectl("delete", "pod", victim, "-n", NAMESPACE, "--force", "--grace-period=0")
    print(f"  killed {victim}")

    # Two windows inside the outage, then a recovery window.
    for label, duration in (("outage", 15), ("recovery", 20)):
        started = now_iso()
        load_result, error = load(node, url, 1000, duration)
        windows.append(
            {
                "phase": label,
                "t": started,
                "load": window_summary(load_result),
                "error": error,
                "ready_endpoints": ready_endpoints(name),
                "deployment": deployment_state(name),
                "elapsed_since_kill_s": round(time.time() - killed_at - duration, 2),
            }
        )
        if load_result:
            print(
                f"  {label:9} 1000 rps -> {fmt(load_result['achieved_rps'])} rps "
                f"p99 {fmt(load_result['latency_ms']['p99'], '.2f')} ms "
                f"errors {load_result['errors']}"
            )

    # Time from the kill to three ready endpoints, taken from the pods the
    # deployment reports; the load windows above are coarser than this.
    recovered, _ = wait_deployment_ready(name, 3, timeout=180)
    survived = time.time() - killed_at

    record = {
        "scenario": "eviction",
        "status": "ok",
        "victim": victim,
        "victim_cgroup": victim_cgroup,
        "windows": windows,
        "recovered_to_3_ready": recovered,
        "seconds_to_recover": round(survived, 2),
        "final_deployment": deployment_state(name),
        "pods_after": pod_nodes(f"app={name}"),
    }
    kubectl(
        "delete",
        "deploy,svc",
        "-n",
        NAMESPACE,
        "-l",
        LABEL,
        "--ignore-not-found",
        "--wait=false",
    )
    return record


# ---------------------------------------------------------------------------
# 7.7.4 node drain
# ---------------------------------------------------------------------------


def scenario_drain(node, args):
    """Cordon and drain a node while three limited replicas serve traffic."""
    print("\n" + "-" * 78)
    print(f"incident: node drain ({args.drain_node})")
    print("-" * 78)

    drain_node = args.drain_node
    if not drain_node:
        return {
            "scenario": "drain",
            "status": "skipped",
            "reason": "no --drain-node given and none could be chosen",
        }
    if drain_node == node.node:
        return {
            "scenario": "drain",
            "status": "skipped",
            "reason": (
                "the load generator runs on the same node, so draining it would "
                "remove the only witness; pass --drain-node for another node"
            ),
        }

    if not node.put(str(LOADGEN), LOADGEN_REMOTE):
        return {
            "scenario": "drain",
            "status": "failed",
            "error": "could not stage loadgen.py",
        }

    name = "phase7-drain"
    # The replicas are attracted to the drain node but are not pinned to it,
    # and the placement is verified. Draining a node that holds one of three
    # replicas measures something different from draining the node that holds
    # all three, and a required selector would keep the replacements off every
    # other node, turning the drain into a permanent scheduling failure.
    ready = False
    before_pods = {}
    for attempt in range(3):
        kubectl(
            "delete",
            "deploy,svc",
            "-n",
            NAMESPACE,
            "-l",
            LABEL,
            "--ignore-not-found",
            "--wait=true",
            timeout=180,
        )
        applied = apply(
            deployment(
                name,
                3,
                {"cpu": LIMITED, "memory": "128Mi"},
                {"cpu": LIMITED, "memory": "256Mi"},
                prefer_node=drain_node,
            )
        )
        if applied.returncode != 0:
            return {
                "scenario": "drain",
                "status": "failed",
                "error": applied.stderr.strip()[:300],
            }
        apply(service(name))
        ready, _ = wait_deployment_ready(name, 3, timeout=300)
        before_pods = pod_nodes(f"app={name}")
        placed = [info for info in before_pods.values() if info["node"] == drain_node]
        if ready and len(placed) == 3:
            break
        print(f"  placement attempt {attempt + 1}: {len(placed)}/3 on {drain_node}")
    ip = service_ip(name)
    if not ready or not ip:
        kubectl(
            "delete", "deploy,svc", "-n", NAMESPACE, "-l", LABEL, "--ignore-not-found"
        )
        kubectl("uncordon", drain_node)
        return {
            "scenario": "drain",
            "status": "failed",
            "error": f"3 replicas never became ready: {before_pods}",
        }

    if args.drain_pdb:
        applied = apply(pdb(name, 2))
        if applied.returncode != 0:
            return {
                "scenario": "drain",
                "status": "failed",
                "error": f"PDB rejected: {applied.stderr.strip()[:300]}",
            }

    windows = []
    baseline, error = load(node, f"http://{ip}:80/", 1000, 10)
    windows.append(
        {"phase": "baseline", "load": window_summary(baseline), "error": error}
    )
    if baseline:
        print(
            f"  baseline 1000 rps -> {fmt(baseline['achieved_rps'])} rps "
            f"p99 {fmt(baseline['latency_ms']['p99'], '.2f')} ms"
        )

    started = time.time()
    cordon = kubectl("cordon", drain_node)
    drain = kubectl(
        "drain",
        drain_node,
        "--ignore-daemonsets",
        "--delete-emptydir-data",
        "--force",
        "--timeout=240s",
        timeout=300,
    )
    drain_seconds = round(time.time() - started, 2)
    print(f"  drain finished in {drain_seconds}s rc={drain.returncode}")

    load_result, error = load(node, f"http://{ip}:80/", 1000, 25)
    windows.append(
        {
            "phase": "during-recovery",
            "load": window_summary(load_result),
            "error": error,
            "elapsed_since_cordon_s": round(time.time() - started - 25, 2),
        }
    )
    if load_result:
        print(
            f"  during    1000 rps -> {fmt(load_result['achieved_rps'])} rps "
            f"p99 {fmt(load_result['latency_ms']['p99'], '.2f')} ms "
            f"errors {load_result['errors']}"
        )

    recovered, _ = wait_deployment_ready(name, 3, timeout=300)
    recovery_seconds = round(time.time() - started, 2)
    after_pods = pod_nodes(f"app={name}")

    uncordon = kubectl("uncordon", drain_node)
    scheduled_elsewhere = all(
        info["node"] != drain_node for info in after_pods.values()
    )

    record = {
        "scenario": "drain",
        "status": "ok",
        "drain_node": drain_node,
        "reader_node": node.node,
        "pdb_min_available": 2 if args.drain_pdb else None,
        "cordon_rc": cordon.returncode,
        "drain_rc": drain.returncode,
        "drain_stdout": drain.stdout.strip()[-1200:],
        "drain_stderr": drain.stderr.strip()[-600:],
        "drain_seconds": drain_seconds,
        "windows": windows,
        "recovered_to_3_ready": recovered,
        "seconds_to_recover": recovery_seconds,
        "pods_before": before_pods,
        "pods_after": after_pods,
        "all_replicas_left_the_node": scheduled_elsewhere,
        "uncordon_rc": uncordon.returncode,
    }
    kubectl(
        "delete",
        "deploy,svc",
        "-n",
        NAMESPACE,
        "-l",
        LABEL,
        "--ignore-not-found",
        "--wait=false",
    )
    return record


SCENARIOS = {
    "spike": scenario_spike,
    "ramp": scenario_ramp,
    "eviction": scenario_eviction,
    "drain": scenario_drain,
}


def choose_drain_node(reader_node):
    """Pick the empty worker node that is not the reader's node.

    Draining must not evict infrastructure the measurement depends on: one
    worker on this cluster carries CoreDNS and metrics-server, and an HPA
    without metrics-server stops being an autoscaler. The node with the fewest
    non-DaemonSet pods is therefore the safest candidate; DaemonSet pods are
    ignored by the drain itself.
    """
    out = kubectl("get", "nodes", "-o", "json").stdout
    try:
        items = json.loads(out)["items"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return ""

    candidates = []
    for item in items:
        name = item["metadata"]["name"]
        if name == reader_node:
            continue
        labels = item["metadata"].get("labels", {})
        if "node-role.kubernetes.io/control-plane" in labels:
            continue
        conditions = {
            c["type"]: c["status"] for c in item["status"].get("conditions", [])
        }
        if conditions.get("Ready") != "True":
            continue
        if item.get("spec", {}).get("unschedulable"):
            continue
        candidates.append(name)

    def workload_pods(node_name):
        pods = kubectl(
            "get",
            "pods",
            "-A",
            "--field-selector",
            f"spec.nodeName={node_name}",
            "-o",
            "json",
        ).stdout
        try:
            entries = json.loads(pods)["items"]
        except (ValueError, KeyError, json.JSONDecodeError):
            return 1000
        count = 0
        for entry in entries:
            owners = entry["metadata"].get("ownerReferences") or []
            if any(owner.get("kind") == "DaemonSet" for owner in owners):
                continue
            if entry.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            count += 1
        return count

    return min(candidates, key=workload_pods) if candidates else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--drain-node", default="")
    parser.add_argument(
        "--drain-pdb",
        action="store_true",
        help="protect the drain with a PodDisruptionBudget",
    )
    parser.add_argument("--label", default="incidents")
    args = parser.parse_args()

    names = [name for name in args.scenarios.split(",") if name]
    for name in names:
        if name not in SCENARIOS:
            raise SystemExit(f"unknown scenario: {name}")

    print("=" * 78)
    print("Phase 7 Topic 7.7: production incident simulation")
    print("=" * 78)

    kubectl("create", "namespace", NAMESPACE)
    cleanup()

    node = na.NodeAccess(
        node_name=os.environ.get("PHASE7_NODE") or None, namespace=NAMESPACE
    )
    if not node.available():
        print(f"node access unavailable: {node.describe()}")
        return 1
    print(f"reader: {node.node} via {node.describe()['method']}")

    if not args.drain_node:
        args.drain_node = choose_drain_node(node.node)

    with tempfile.TemporaryDirectory(prefix="phase7-7.7-") as tmp:
        error = publish_httpd(Path(tmp))
    if error:
        print(f"FAILED: {error}")
        return 1
    print(f"workload: {HTTPD_MOUNT}/httpd assembled from {HTTPD_CONFIGMAP}-0/1")

    results = []
    for name in names:
        try:
            results.append(SCENARIOS[name](node, args))
        except Exception as exc:  # one broken incident must not hide the others
            results.append(
                {"scenario": name, "status": "failed", "error": str(exc)[:500]}
            )
        finally:
            cleanup()

    payload = {
        "test": "incidents",
        "topic": "7.7",
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "reader_node": node.node,
        "reader_method": node.describe()["method"],
        "drain_node": args.drain_node,
        "workload_image": WORKLOAD_IMAGE,
        "workload_server": str(HTTPD),
        "httpd_configmaps": [f"{HTTPD_CONFIGMAP}-0", f"{HTTPD_CONFIGMAP}-1"],
        "scenarios": results,
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / f"{args.label}-{payload['timestamp']}.json"
    out.write_text(json.dumps(payload, indent=2))

    print()
    print(f"Evidence: {out}")
    failed = [r for r in results if r.get("status") == "failed"]
    for failure in failed:
        print(f"FAILED: {failure['scenario']}: {failure.get('error')}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
