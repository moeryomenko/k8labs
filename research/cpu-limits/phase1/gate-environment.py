#!/usr/bin/env python3
"""Phase 1 environment and capability gate.

Validates that the research environment meets all prerequisites for CPU
request/limit experiments, including cgroup v2, BPF instrumentation, scheduler
modes, and tool availability.

Returns machine-readable environment metadata and fails closed when required
capabilities are missing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class KernelMode:
    """Kernel scheduler and capacity configuration."""

    smp: bool  # CONFIG_SMP=y
    preempt_model: str  # PREEMPT_DYNAMIC, PREEMPT_VOLUNTARY, etc.
    online_cpus: list[int]
    max_cpus: int  # CONFIG_NR_CPUS
    sched_ext_state: str  # disabled, enabled, ops name
    sched_ext_available: bool


@dataclass
class CgroupCapability:
    """cgroup v2 controllers and CPU bandwidth support."""

    cgroup_version: int
    cpu_controller: bool
    cpu_max_available: bool
    cpu_weight_available: bool
    cpu_stat_available: bool
    cpu_pressure_available: bool
    cpu_uclamp_available: bool


@dataclass
class BPFCapability:
    """BPF and tracing instrumentation."""

    btf_vmlinux: bool
    tracefs_mounted: bool
    bpftrace_version: Optional[str]
    bpftool_version: Optional[str]
    perf_version: Optional[str]
    runqlat_available: bool
    runqlen_available: bool
    sched_tracepoints: list[str]


@dataclass
class NodeEnvironment:
    """Complete node environment metadata."""

    node_name: str
    kernel_release: str
    kernel_version: str  # full /proc/version
    boot_id: str
    cpu_topology: str  # /sys/devices/system/cpu/online
    kubernetes_version: str
    kubelet_version: str
    crio_version: str
    kernel_mode: KernelMode
    cgroup: CgroupCapability
    bpf: BPFCapability
    observer_pod: str
    observer_image: str


def _exec_in_observer(cmd: list[str]) -> tuple[int, str, str]:
    """Execute command in the observer pod."""
    # Get current observer pod name
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


def _check_observer_command(cmd: list[str]) -> bool:
    """Check if a command exists in the observer."""
    rc, _, _ = _exec_in_observer(["sh", "-c", f"command -v {cmd[0]} >/dev/null 2>&1"])
    return rc == 0


def _get_command_version(cmd: list[str]) -> Optional[str]:
    """Get version string from a command."""
    rc, stdout, stderr = _exec_in_observer(cmd)
    if rc != 0:
        return None
    output = (stdout + stderr).strip()
    # Extract first line that looks like a version
    for line in output.split("\n"):
        if any(c.isdigit() for c in line):
            return line.strip()
    return output.split("\n")[0] if output else None


def check_kernel_mode() -> KernelMode:
    """Detect kernel scheduler mode and CPU capacity."""
    version = _read_observer_file("/proc/version") or ""
    online = _read_observer_file("/sys/devices/system/cpu/online") or "0"

    # Parse online CPUs
    online_cpus = []
    for part in online.split(","):
        if "-" in part:
            start, end = part.split("-")
            online_cpus.extend(range(int(start), int(end) + 1))
        else:
            online_cpus.append(int(part))

    # Check SMP
    smp = "SMP" in version

    # Extract preempt model
    preempt = "UNKNOWN"
    if "PREEMPT_DYNAMIC" in version:
        preempt = "PREEMPT_DYNAMIC"
    elif "PREEMPT" in version:
        preempt = "PREEMPT"
    elif "PREEMPT_VOLUNTARY" in version:
        preempt = "PREEMPT_VOLUNTARY"

    # Check sched_ext
    sched_ext_state = (
        _read_observer_file("/sys/kernel/sched_ext/state") or "unavailable"
    )
    sched_ext_available = sched_ext_state != "unavailable"

    # Max CPUs - try multiple sources
    max_cpus = len(online_cpus)  # fallback
    possible = _read_observer_file("/sys/devices/system/cpu/possible")
    if possible and "-" in possible:
        _, end = possible.rsplit("-", 1)
        max_cpus = int(end) + 1

    return KernelMode(
        smp=smp,
        preempt_model=preempt,
        online_cpus=online_cpus,
        max_cpus=max_cpus,
        sched_ext_state=sched_ext_state.strip(),
        sched_ext_available=sched_ext_available,
    )


def check_cgroup() -> CgroupCapability:
    """Validate cgroup v2 and CPU controller."""
    controllers = _read_observer_file("/sys/fs/cgroup/cgroup.controllers") or ""

    # Check specific CPU files
    cpu_max = _read_observer_file("/sys/fs/cgroup/kubepods.slice/cpu.max") is not None
    cpu_weight = (
        _read_observer_file("/sys/fs/cgroup/kubepods.slice/cpu.weight") is not None
    )
    cpu_stat = _read_observer_file("/sys/fs/cgroup/kubepods.slice/cpu.stat") is not None
    cpu_pressure = (
        _read_observer_file("/sys/fs/cgroup/kubepods.slice/cpu.pressure") is not None
    )

    # Check uclamp
    rc, _, _ = _exec_in_observer(
        ["test", "-f", "/sys/fs/cgroup/kubepods.slice/cpu.uclamp.min"]
    )
    cpu_uclamp = rc == 0

    return CgroupCapability(
        cgroup_version=2,
        cpu_controller="cpu" in controllers,
        cpu_max_available=cpu_max,
        cpu_weight_available=cpu_weight,
        cpu_stat_available=cpu_stat,
        cpu_pressure_available=cpu_pressure,
        cpu_uclamp_available=cpu_uclamp,
    )


def check_bpf() -> BPFCapability:
    """Validate BPF and tracing tools."""
    # Check BTF by testing file existence
    rc, _, _ = _exec_in_observer(["test", "-f", "/sys/kernel/btf/vmlinux"])
    btf = rc == 0

    rc, _, _ = _exec_in_observer(["test", "-d", "/sys/kernel/tracing"])
    tracefs = rc == 0

    bpftrace_ver = _get_command_version(["bpftrace", "--version"])
    bpftool_ver = _get_command_version(["bpftool", "version"])
    perf_ver = _get_command_version(["perf", "--version"])

    runqlat = _check_observer_command(["bpf-runqlat"])
    runqlen = _check_observer_command(["bpf-runqlen"])

    # List sched tracepoints
    rc, stdout, _ = _exec_in_observer(
        [
            "find",
            "/sys/kernel/tracing/events/sched",
            "-maxdepth",
            "1",
            "-type",
            "d",
            "-printf",
            "%f\\n",
        ]
    )
    tracepoints = [tp for tp in stdout.split("\n") if tp and tp != "sched"]

    return BPFCapability(
        btf_vmlinux=btf,
        tracefs_mounted=tracefs,
        bpftrace_version=bpftrace_ver,
        bpftool_version=bpftool_ver,
        perf_version=perf_ver,
        runqlat_available=runqlat,
        runqlen_available=runqlen,
        sched_tracepoints=sorted(tracepoints),
    )


def check_environment() -> tuple[NodeEnvironment, list[str]]:
    """Collect complete environment metadata and validate prerequisites."""
    errors = []

    # Get node name
    result = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "get",
            "nodes",
            "-l",
            "linux-sched/research=observer",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout:
        errors.append("Could not find observer node")
        node_name = "unknown"
    else:
        node_name = result.stdout.strip()

    # Get observer pod
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
    observer_pod = result.stdout.strip() if result.returncode == 0 else "unknown"

    # Get versions
    kernel_release = _read_observer_file("/proc/sys/kernel/osrelease") or "unknown"
    kernel_version = _read_observer_file("/proc/version") or "unknown"
    boot_id = _read_observer_file("/proc/sys/kernel/random/boot_id") or "unknown"
    cpu_topology = _read_observer_file("/sys/devices/system/cpu/online") or "unknown"

    # Kubernetes versions
    result = subprocess.run(
        ["kubectl", "--kubeconfig", "build/kubeconfig", "version", "-o", "json"],
        capture_output=True,
        text=True,
    )
    k8s_version = "unknown"
    if result.returncode == 0:
        try:
            ver_data = json.loads(result.stdout)
            k8s_version = ver_data.get("serverVersion", {}).get("gitVersion", "unknown")
        except json.JSONDecodeError:
            pass

    # Get kubelet and crio versions from node
    kubelet_ver = "unknown"
    crio_ver = "unknown"

    kernel_mode = check_kernel_mode()
    cgroup = check_cgroup()
    bpf = check_bpf()

    # Validate required capabilities
    if not kernel_mode.smp:
        errors.append(
            "Current kernel is not SMP; UP mode requires explicit custom kernel"
        )

    if not cgroup.cpu_controller:
        errors.append("cgroup v2 CPU controller not available")

    if not cgroup.cpu_max_available:
        errors.append("cgroup v2 cpu.max not available")

    if not cgroup.cpu_stat_available:
        errors.append("cgroup v2 cpu.stat not available")

    if not bpf.btf_vmlinux:
        errors.append("BTF vmlinux not available")

    if not bpf.tracefs_mounted:
        errors.append("tracefs not mounted")

    if not bpf.bpftrace_version:
        errors.append("bpftrace not available")

    if not bpf.bpftool_version:
        errors.append("bpftool not available")

    if not bpf.runqlat_available:
        errors.append("bpf-runqlat not available")

    if not bpf.runqlen_available:
        errors.append("bpf-runqlen not available")

    # Check for key scheduler tracepoints
    required_tracepoints = {"sched_switch", "sched_wakeup", "sched_stat_runtime"}
    missing = required_tracepoints - set(bpf.sched_tracepoints)
    if missing:
        errors.append(
            f"Missing required scheduler tracepoints: {', '.join(sorted(missing))}"
        )

    env = NodeEnvironment(
        node_name=node_name,
        kernel_release=kernel_release,
        kernel_version=kernel_version,
        boot_id=boot_id,
        cpu_topology=cpu_topology,
        kubernetes_version=k8s_version,
        kubelet_version=kubelet_ver,
        crio_version=crio_ver,
        kernel_mode=kernel_mode,
        cgroup=cgroup,
        bpf=bpf,
        observer_pod=observer_pod,
        observer_image="docker.io/library/fedora:44",
    )

    return env, errors


def main() -> int:
    """Run environment gate."""
    print("Checking CPU limits research environment...")

    env, errors = check_environment()

    # Write environment metadata
    out_dir = Path("research/cpu-limits/phase1/evidence")
    out_dir.mkdir(parents=True, exist_ok=True)

    env_file = out_dir / "environment.json"
    with env_file.open("w") as f:
        json.dump(asdict(env), f, indent=2)

    print(f"\n✓ Environment metadata written to {env_file}")

    # Print summary
    print(f"\nNode: {env.node_name}")
    print(f"Kernel: {env.kernel_release}")
    print(
        f"Mode: {'SMP' if env.kernel_mode.smp else 'UP'}, {env.kernel_mode.preempt_model}"
    )
    print(
        f"CPUs: {len(env.kernel_mode.online_cpus)} online, {env.kernel_mode.max_cpus} max"
    )
    print(f"sched_ext: {env.kernel_mode.sched_ext_state}")
    print(f"cgroup v2: {'✓' if env.cgroup.cpu_controller else '✗'}")
    print(
        f"BPF tools: bpftrace={env.bpf.bpftrace_version or 'missing'}, bpftool={env.bpf.bpftool_version or 'missing'}"
    )
    print(
        f"Run queue tools: runqlat={'✓' if env.bpf.runqlat_available else '✗'}, runqlen={'✓' if env.bpf.runqlen_available else '✗'}"
    )

    if errors:
        print("\n✗ Gate FAILED:\n")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("\n✓ Environment gate PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
