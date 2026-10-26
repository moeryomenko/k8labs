#!/usr/bin/env python3
"""Phase 1 cgroup validation script.

Validates that Kubernetes CPU requests and limits correctly propagate to cgroup
v2 parameters for running containers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class ContainerCgroup:
    """cgroup v2 parameters for a single container."""

    pod_name: str
    namespace: str
    container_name: str
    container_id: str
    cgroup_path: str

    # Resource specification from Kubernetes
    requests_cpu: Optional[str]
    limits_cpu: Optional[str]

    # Actual cgroup v2 values
    cpu_weight: Optional[int]
    cpu_max_quota: Optional[int]
    cpu_max_period: Optional[int]
    cpu_uclamp_min: Optional[int]
    cpu_uclamp_max: Optional[int]

    # Current usage
    usage_usec: Optional[int]
    user_usec: Optional[int]
    system_usec: Optional[int]
    nr_periods: Optional[int]
    nr_throttled: Optional[int]
    throttled_usec: Optional[int]

    # Pressure
    pressure_some_avg10: Optional[float]
    pressure_some_avg60: Optional[float]
    pressure_some_avg300: Optional[float]


def _exec(cmd: list[str]) -> tuple[int, str, str]:
    """Execute kubectl command."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def _exec_in_observer(cmd: list[str]) -> tuple[int, str, str]:
    """Execute command in the observer pod."""
    # Get current observer pod
    result = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "-n",
            "linux-sched",
            "get",
            "pod",
            "-l",
            "app.kubernetes.io/name=linux-sched-observer",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return 1, "", "Observer pod not found"

    pod_name = result.stdout.strip()

    result = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "-n",
            "linux-sched",
            "exec",
            pod_name,
            "--",
            *cmd,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _read_observer_file(path: str) -> Optional[str]:
    """Read a file from the observer pod."""
    rc, stdout, _ = _exec_in_observer(["cat", path])
    return stdout.strip() if rc == 0 else None


def get_pod_containers() -> list[dict]:
    """Get all containers in the custom worker node."""
    rc, stdout, stderr = _exec(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "get",
            "pods",
            "--all-namespaces",
            "--field-selector",
            "spec.nodeName=k8labs-md-custom-4vcpu-qq498-zlz6p-6n655",
            "-o",
            "json",
        ]
    )

    if rc != 0:
        print(f"Failed to get pods: {stderr}", file=sys.stderr)
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        print(f"Failed to parse pod JSON: {e}", file=sys.stderr)
        return []

    containers = []
    for pod in data.get("items", []):
        pod_name = pod["metadata"]["name"]
        namespace = pod["metadata"]["namespace"]
        spec = pod.get("spec", {})
        status = pod.get("status", {})

        # Get container specs and statuses
        container_specs = {c["name"]: c for c in spec.get("containers", [])}
        container_statuses = {c["name"]: c for c in status.get("containerStatuses", [])}

        for name, container_spec in container_specs.items():
            container_status = container_statuses.get(name, {})
            container_id = container_status.get("containerID", "")

            # Extract short container ID (remove cri-o:// prefix)
            if container_id.startswith("cri-o://"):
                container_id = container_id[8:]

            containers.append(
                {
                    "pod_name": pod_name,
                    "namespace": namespace,
                    "container_name": name,
                    "container_id": container_id,
                    "resources": container_spec.get("resources", {}),
                }
            )

    return containers


def parse_cpu_stat(content: str) -> dict:
    """Parse cpu.stat content."""
    stats = {}
    for line in content.split("\n"):
        if not line:
            continue
        key, value = line.split(None, 1)
        stats[key] = int(value)
    return stats


def parse_cpu_pressure(content: str) -> dict:
    """Parse cpu.pressure content."""
    pressure = {}
    for line in content.split("\n"):
        if not line:
            continue
        if line.startswith("some"):
            parts = line.split()
            for part in parts[1:]:
                key, value = part.split("=")
                pressure[f"some_{key}"] = float(value)
        elif line.startswith("full"):
            parts = line.split()
            for part in parts[1:]:
                key, value = part.split("=")
                pressure[f"full_{key}"] = float(value)
    return pressure


def inspect_container_cgroup(container: dict) -> Optional[ContainerCgroup]:
    """Inspect cgroup v2 parameters for a container."""
    pod_name = container["pod_name"]
    namespace = container["namespace"]
    container_name = container["container_name"]
    container_id = container["container_id"]

    if not container_id:
        return None

    # Find cgroup path
    # Try to find the cgroup by container ID
    rc, stdout, _ = _exec_in_observer(
        [
            "find",
            "/sys/fs/cgroup/kubepods.slice",
            "-name",
            f"*{container_id[:12]}*",
            "-type",
            "d",
        ]
    )

    if rc != 0 or not stdout.strip():
        return None

    # Take first match
    cgroup_path = stdout.strip().split("\n")[0]

    # Read cpu.weight
    cpu_weight_str = _read_observer_file(f"{cgroup_path}/cpu.weight")
    cpu_weight = int(cpu_weight_str) if cpu_weight_str else None

    # Read cpu.max
    cpu_max_str = _read_observer_file(f"{cgroup_path}/cpu.max")
    cpu_max_quota = None
    cpu_max_period = None
    if cpu_max_str:
        parts = cpu_max_str.split()
        if parts[0] != "max":
            cpu_max_quota = int(parts[0])
        cpu_max_period = int(parts[1]) if len(parts) > 1 else None

    # Read cpu.uclamp.min and cpu.uclamp.max
    cpu_uclamp_min_str = _read_observer_file(f"{cgroup_path}/cpu.uclamp.min")
    cpu_uclamp_min = None
    if cpu_uclamp_min_str and cpu_uclamp_min_str != "max":
        cpu_uclamp_min = int(float(cpu_uclamp_min_str))

    cpu_uclamp_max_str = _read_observer_file(f"{cgroup_path}/cpu.uclamp.max")
    cpu_uclamp_max = None
    if cpu_uclamp_max_str and cpu_uclamp_max_str != "max":
        cpu_uclamp_max = int(float(cpu_uclamp_max_str))

    # Read cpu.stat
    cpu_stat_str = _read_observer_file(f"{cgroup_path}/cpu.stat")
    cpu_stat = parse_cpu_stat(cpu_stat_str) if cpu_stat_str else {}

    # Read cpu.pressure
    cpu_pressure_str = _read_observer_file(f"{cgroup_path}/cpu.pressure")
    cpu_pressure = parse_cpu_pressure(cpu_pressure_str) if cpu_pressure_str else {}

    # Get Kubernetes resource spec
    resources = container.get("resources", {})
    requests = resources.get("requests", {})
    limits = resources.get("limits", {})

    return ContainerCgroup(
        pod_name=pod_name,
        namespace=namespace,
        container_name=container_name,
        container_id=container_id,
        cgroup_path=cgroup_path,
        requests_cpu=requests.get("cpu"),
        limits_cpu=limits.get("cpu"),
        cpu_weight=cpu_weight,
        cpu_max_quota=cpu_max_quota,
        cpu_max_period=cpu_max_period,
        cpu_uclamp_min=int(cpu_uclamp_min) if cpu_uclamp_min is not None else None,
        cpu_uclamp_max=int(cpu_uclamp_max) if cpu_uclamp_max is not None else None,
        usage_usec=cpu_stat.get("usage_usec"),
        user_usec=cpu_stat.get("user_usec"),
        system_usec=cpu_stat.get("system_usec"),
        nr_periods=cpu_stat.get("nr_periods"),
        nr_throttled=cpu_stat.get("nr_throttled"),
        throttled_usec=cpu_stat.get("throttled_usec"),
        pressure_some_avg10=cpu_pressure.get("some_avg10"),
        pressure_some_avg60=cpu_pressure.get("some_avg60"),
        pressure_some_avg300=cpu_pressure.get("some_avg300"),
    )


def main() -> int:
    """Validate container cgroups."""
    print("Validating container cgroup propagation...")

    containers = get_pod_containers()
    print(f"Found {len(containers)} containers on research node")

    cgroup_data = []
    errors = []

    for container in containers:
        cgroup = inspect_container_cgroup(container)
        if cgroup:
            cgroup_data.append(cgroup)
        else:
            errors.append(
                f"Could not resolve cgroup for {container['namespace']}/{container['pod_name']}/{container['container_name']}"
            )

    # Write results
    out_dir = Path("research/cpu-limits/phase1/evidence")
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / "cgroup-validation.json"
    with out_file.open("w") as f:
        json.dump([asdict(cg) for cg in cgroup_data], f, indent=2)

    print(f"\n✓ Validated {len(cgroup_data)} container cgroups")
    print(f"  Output: {out_file}")

    if errors:
        print(f"\n⚠ Warnings:")
        for error in errors:
            print(f"  - {error}")

    # Print summary
    print("\nSummary:")
    for cg in cgroup_data:
        print(f"\n{cg.namespace}/{cg.pod_name}/{cg.container_name}")
        print(
            f"  Request: {cg.requests_cpu or 'none'}, Limit: {cg.limits_cpu or 'none'}"
        )
        print(f"  cpu.weight: {cg.cpu_weight}")
        print(f"  cpu.max: {cg.cpu_max_quota or 'max'} {cg.cpu_max_period or 100000}")
        if cg.nr_throttled and cg.nr_throttled > 0:
            print(
                f"  ⚠ Throttled: {cg.nr_throttled}/{cg.nr_periods} periods, {cg.throttled_usec}μs"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
