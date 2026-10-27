#!/usr/bin/env python3
"""
Node access for Phase 7 experiments.

Phase 7 needs numbers that only the Kubernetes node holds: cgroup CPU
accounting, cgroup v2 files, PSI, and load-generator binaries. The
workstation does not have them, so every Phase 7 script either obtains
them through this module or records that they were unavailable.

Two properties are deliberate:

* Nothing is fabricated. A missing value is None plus a reason, never 0.
* Absence of a CPU limit is not "0% throttled". It is "quota not
  applicable". Conflating the two is what made the first draft of the
  cost model report $271M/year.

Detection order:
  1. $PHASE7_NODE_SSH      host-reachable nodes (specify the node you need)
  2. exec into a pod that already mounts the host cgroupfs
                           reads /host/sys/fs/cgroup, which is what the
                           cpu-limits-collector DaemonSet-style pod does
  3. unavailable

Path 2 exists because this cluster rejects privileged pods outright
(container create fails on `mknodat /dev/loop0`), so a freshly created
helper pod cannot be used to reach the host namespace.
"""

import json
import os
import shlex
import subprocess

# Pods already running in the cluster that mount host cgroupfs read-only.
# Override with PHASE7_READER_POD="namespace/name".
DEFAULT_READER_NS = "cpu-limits"
DEFAULT_READER_POD = "cpu-limits-collector-fgpd7"

HOST_CGROUP = "/host/sys/fs/cgroup"


class NodeAccess:
    def __init__(self, pod_name=None, namespace=None, node_name=None, timeout=120):
        self.pod_name = pod_name
        self.namespace = namespace
        self.timeout = timeout
        self.method = "unavailable"
        self.node = node_name
        self.detail = None
        self._ssh_prefix = None
        self._reader_ns = None
        self._reader_pod = None

        if pod_name:
            self.node = self._kubectl(
                "get", "pod", pod_name, "-o", "jsonpath={.spec.nodeName}"
            )

        ssh = os.environ.get("PHASE7_NODE_SSH", "").strip()
        if ssh:
            prefix = shlex.split(ssh)
            check = subprocess.run([*prefix, "true"], capture_output=True, timeout=30)
            if check.returncode == 0:
                self.method = "ssh"
                self._ssh_prefix = prefix
                return
            self.detail = "PHASE7_NODE_SSH set but unreachable"

        reason = self._find_reader_pod()
        if reason is None:
            self.method = "reader-pod"
            return
        if self.detail is None:
            self.detail = reason

    # -- kubectl helpers ---------------------------------------------------

    def _kubectl(self, *args):
        try:
            result = subprocess.run(
                ["kubectl", *args], capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            return ""
        return result.stdout.strip()

    def _find_reader_pod(self):
        """Locate a running pod that exposes the host cgroupfs.

        Returns None on success, or a reason string on failure.
        """
        override = os.environ.get("PHASE7_READER_POD", "").strip()
        candidates = []
        if override:
            ns, _, name = override.partition("/")
            candidates.append((ns or DEFAULT_READER_NS, name))
        candidates.append((DEFAULT_READER_NS, DEFAULT_READER_POD))

        pods = (
            json.loads(self._kubectl("get", "pods", "-A", "-o", "json") or "{}").get(
                "items", []
            )
            if self._kubectl("get", "pods", "-A", "-o", "json")
            else []
        )

        # Any running pod holding a hostPath mount of /sys/fs/cgroup works.
        # When a target node is set, only readers on that node are useful.
        for pod in pods:
            if pod.get("status", {}).get("phase") != "Running":
                continue
            if self.node and pod.get("spec", {}).get("nodeName") != self.node:
                continue
            host_cgroup = any(
                "cgroup" in (v.get("hostPath", {}).get("path") or "")
                for v in pod.get("spec", {}).get("volumes", [])
            )
            if not host_cgroup:
                continue
            key = (pod["metadata"]["namespace"], pod["metadata"]["name"])
            if key not in candidates:
                candidates.append(key)

        if self.node:
            candidates = [
                (ns, name)
                for ns, name in candidates
                if self._pod_node(ns, name) == self.node
            ]

        for ns, name in candidates:
            info = json.loads(
                self._kubectl("get", "pod", "-n", ns, name, "-o", "json") or "{}"
            )
            if not info or info.get("status", {}).get("phase") != "Running":
                continue
            mounts = [
                m["mountPath"]
                for c in info.get("spec", {}).get("containers", [])
                for m in c.get("volumeMounts", [])
            ]
            root = next((m for m in mounts if m.endswith("sys/fs/cgroup")), None)
            if root is None:
                continue
            probe = self._exec(ns, name, f"cat {root}/cgroup.controllers 2>&1")
            if "cpu" in probe:
                self._reader_ns = ns
                self._reader_pod = name
                self.node = info["spec"].get("nodeName")
                self._host_cgroup = root
                return None

        if self.node:
            reason = self._create_reader(self.node)
            if reason is None:
                return None
            return reason

        return (
            f"no running pod exposes host cgroupfs "
            f"(looked at {len(candidates)} candidate pods)"
        )

    def _pod_node(self, ns, name):
        return self._kubectl(
            "get", "pod", "-n", ns, name, "-o", "jsonpath={.spec.nodeName}"
        )

    def _create_reader(self, node):
        """Create a minimal, unprivileged cgroup reader on `node`.

        Only a read-only hostPath and DAC_READ_SEARCH are needed. Privileged
        pods are rejected by this cluster's container runtime, so this must
        stay unprivileged.
        """
        name = "phase7-reader"
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name},
            "spec": {
                "nodeName": node,
                "hostPID": True,
                "restartPolicy": "Never",
                "tolerations": [{"operator": "Exists"}],
                "containers": [
                    {
                        "name": "reader",
                        "image": "fedora:40",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sleep", "infinity"],
                        "securityContext": {
                            "runAsUser": 0,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"add": ["DAC_READ_SEARCH"]},
                        },
                        "volumeMounts": [
                            {
                                "name": "cgroup",
                                "mountPath": "/host/sys/fs/cgroup",
                                "readOnly": True,
                            }
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "cgroup",
                        "hostPath": {"path": "/sys/fs/cgroup", "type": "Directory"},
                    }
                ],
            },
        }
        self._kubectl(
            "delete", "pod", name, "--ignore-not-found", "--force", "--grace-period=0"
        )
        apply = subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(manifest),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if apply.returncode != 0:
            return f"reader pod rejected: {apply.stderr.strip()[:200]}"

        subprocess.run(
            ["kubectl", "wait", "--for=condition=Ready", name, "--timeout=150s"],
            capture_output=True,
            timeout=170,
        )
        phase = self._kubectl("get", "pod", name, "-o", "jsonpath={.status.phase}")
        if phase != "Running":
            events = self._kubectl(
                "get",
                "events",
                "--field-selector",
                f"involvedObject.name={name}",
                "-o",
                "jsonpath={.items[*].message}",
            )
            return f"reader pod on {node} not running: {events[:200] or phase}"

        probe = self._exec(
            self._reader_ns, name, "cat /host/sys/fs/cgroup/cgroup.controllers"
        )
        if "cpu" not in probe:
            return f"reader pod cannot read host cgroupfs: {probe[:200]}"

        self._reader_ns = self.namespace or os.environ.get(
            "PHASE7_READER_NS", "default"
        )
        self._reader_pod = name
        self._host_cgroup = "/host/sys/fs/cgroup"
        return None

    def _exec(self, ns, name, command):
        return self._kubectl("exec", "-n", ns, name, "--", "sh", "-c", command)

    # -- public API --------------------------------------------------------

    def available(self):
        return self.method != "unavailable"

    def read(self, path):
        """Return host file text, or None.

        `path` is a host path such as /sys/fs/cgroup/kubepods.slice.
        """
        if self.method == "ssh":
            result = subprocess.run(
                [*self._ssh_prefix, f"cat {path}"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return result.stdout if result.returncode == 0 else None
        if self.method == "reader-pod":
            out = self._exec(
                self._reader_ns,
                self._reader_pod,
                f"cat {self._to_pod_path(path)} 2>/dev/null",
            )
            return out or None
        return None

    def run(self, command):
        """Run a shell command with host absolute paths, return stdout or None."""
        if self.method == "ssh":
            result = subprocess.run(
                [*self._ssh_prefix, command],
                capture_output=True,
                text=True,
                timeout=300,
            )
            return result.stdout if result.returncode == 0 else None
        if self.method == "reader-pod":
            return self._exec(
                self._reader_ns, self._reader_pod, self._rewrite_paths(command)
            )
        return None

    def _to_pod_path(self, path):
        if path.startswith("/sys/fs/cgroup"):
            return self._host_cgroup + path[len("/sys/fs/cgroup") :]
        return path

    def _rewrite_paths(self, command):
        # Commands are written against host paths; remap the cgroupfs prefix.
        # Rewriting an already-remapped path would produce /host/host/...,
        # so guard against it.
        already = self._host_cgroup
        return (
            command.replace(already, "\x00")
            .replace("/sys/fs/cgroup", already)
            .replace("\x00", already)
        )

    def put(self, local_path, remote_path):
        """Copy a local file into the reader pod (not the host)."""
        if self.method == "ssh":
            result = subprocess.run(
                ["scp", "-q", local_path, f"{self._ssh_host}:{remote_path}"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.returncode == 0
        if self.method == "reader-pod":
            result = subprocess.run(
                [
                    "kubectl",
                    "cp",
                    local_path,
                    f"{self._reader_ns}/{self._reader_pod}:{remote_path}",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.returncode == 0
        return False

    def host_cgroup_root(self):
        return HOST_CGROUP if self.method != "reader-pod" else self._host_cgroup

    def describe(self):
        return {
            "method": self.method,
            "node": self.node,
            "reader_pod": (
                f"{self._reader_ns}/{self._reader_pod}" if self._reader_pod else None
            ),
            "detail": self.detail,
        }

    def close(self):
        """Nothing to tear down: the reader pod is a long-lived cluster pod."""
        return None


def kubectl_json(*args):
    result = subprocess.run(
        ["kubectl", *args, "-o", "json"], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def pod_container_cgroups(node, pod_name, namespace=None):
    """
    Map each pod container name to its cgroup directory on the node.

    A pod slice holds two kinds of cgroup: the pod sandbox (no suffix, no
    quota, no CPU accounting) and one crio-<container-id>.scope per
    container. Reading the sandbox instead of the container silently yields
    quota=None and usage 0, so the container ID comes from the pod status
    and is matched against the directory name.

    Returns {container_name: cgroup_dir}; missing containers are absent.
    """
    ns_args = ["-n", namespace] if namespace else []
    pod = kubectl_json("get", "pod", pod_name, *ns_args)
    token = pod["metadata"]["uid"].replace("-", "_")
    root = node.host_cgroup_root()
    # The pod slice is one level below kubepods.slice. BestEffort and
    # Guaranteed pods get a slice directly there (kubepods-pod<uid>.slice),
    # while Burstable pods sit inside kubepods-burstable.slice.
    out = node.run(
        f"ls -d {root}/kubepods.slice/*pod{token}.slice "
        f"{root}/kubepods.slice/*/*pod{token}.slice 2>/dev/null"
    )
    pod_dirs = [d.strip() for d in (out or "").splitlines() if d.strip()]
    if not pod_dirs:
        return {}

    listing = node.run(f"ls -d {pod_dirs[0]}/*/ 2>/dev/null") or ""
    dirs = [d.rstrip("/") for d in listing.splitlines() if d.strip()]

    mapping = {}
    for status in pod.get("status", {}).get("containerStatuses") or []:
        cid = (status.get("containerID") or "").split("://")[-1]
        if not cid:
            continue
        match = next(
            (d for d in dirs if d.rsplit("/", 1)[-1].startswith(f"crio-{cid}")),
            None,
        )
        if match:
            mapping[status["name"]] = match
    return mapping


def pod_cgroup_dirs(node, pod_name, namespace=None):
    """
    Resolve the cgroup directories of a pod's containers on the node.

    systemd driver layout:
      kubepods.slice/kubepods-<qos>.slice/kubepods-<qos>-pod<uid>.slice/crio-<id>
    The pod slice name embeds the UID with '-' replaced by '_'.

    Sandbox cgroups are included here for completeness; use
    pod_container_cgroups() when the container's own quota matters.
    """
    ns_args = ["-n", namespace] if namespace else []
    pod = kubectl_json("get", "pod", pod_name, *ns_args)
    token = pod["metadata"]["uid"].replace("-", "_")
    root = node.host_cgroup_root()
    # Match the pod slice by its full UID token; a bare '*pod*' glob also
    # returns every other pod on the node.
    # The pod slice is one level below kubepods.slice. BestEffort and
    # Guaranteed pods get a slice directly there (kubepods-pod<uid>.slice),
    # while Burstable pods sit inside kubepods-burstable.slice.
    out = node.run(
        f"ls -d {root}/kubepods.slice/*pod{token}.slice "
        f"{root}/kubepods.slice/*/*pod{token}.slice 2>/dev/null"
    )
    pod_dirs = [d.strip() for d in (out or "").splitlines() if d.strip()]
    if not pod_dirs:
        return []
    # Container cgroups live directly under the pod slice.
    listing = node.run(f"ls -d {pod_dirs[0]}/crio-* 2>/dev/null")
    return sorted(l.strip() for l in (listing or "").splitlines() if l.strip())


def parse_cpu_stat(text):
    """Parse cgroup v2 cpu.stat into {key: int}."""
    if not text:
        return None
    stats = {}
    for line in text.strip().splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                stats[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return stats


def parse_cpu_max(text):
    """Parse 'cpu.max' -> {'quota': int|None, 'period': int}. None = unlimited."""
    if not text:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    quota = None if parts[0] == "max" else int(parts[0])
    return {"quota": quota, "period": int(parts[1])}


def throttle_pct(stats):
    """
    Throttled-period ratio in percent, or None when never throttled.

    An unlimited container still records nr_periods (integer max quota) but
    never records a throttled period, so nr_throttled == 0 means "no
    throttling occurred", not "0.0% of a limit was hit". Callers that need a
    number must handle None explicitly.
    """
    if not stats:
        return None
    periods = stats.get("nr_periods", 0)
    if periods == 0 or stats.get("nr_throttled", 0) == 0:
        return None
    return stats["nr_throttled"] / periods * 100


def read_container_cpu(node, cgroup_dir):
    """Read cpu.max/cpu.stat/cpu.weight/cpu.pressure from one container cgroup."""
    return {
        "cgroup_dir": cgroup_dir,
        "cpu_max": parse_cpu_max(node.read(f"{cgroup_dir}/cpu.max")),
        "cpu_stat": parse_cpu_stat(node.read(f"{cgroup_dir}/cpu.stat")),
        "cpu_weight": _int_or_none(node.read(f"{cgroup_dir}/cpu.weight")),
        "cpu_weight_nice": _int_or_none(node.read(f"{cgroup_dir}/cpu.weight.nice")),
        "cpu_pressure": (node.read(f"{cgroup_dir}/cpu.pressure") or "").strip() or None,
    }


def _int_or_none(text):
    if text and text.strip().isdigit():
        return int(text.strip())
    return None
