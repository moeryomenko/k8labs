#!/usr/bin/env python3 -u
"""Phase 2 experiment runner.

Orchestrates workload execution and evidence collection for CPU limits research.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class ExperimentRun:
    """Single experiment run metadata and results."""

    experiment_id: str
    workload_name: str
    workload_type: str
    qos_class: str
    start_time: str
    end_time: Optional[str]
    duration_seconds: Optional[float]

    # Pre-run baseline
    baseline_cgroup: Optional[dict]
    baseline_node_cpu: Optional[dict]

    # Post-run measurements
    final_cgroup: Optional[dict]
    final_node_cpu: Optional[dict]

    # Time series of cgroup snapshots during execution
    cgroup_samples: Optional[list[dict]]

    # BPF trace artifacts
    bpf_runqlat_output: Optional[str]
    bpf_runqlen_output: Optional[str]
    bpf_sched_switch_output: Optional[str]

    # Workload output
    workload_logs: Optional[str]
    workload_exit_code: Optional[int]

    # Prometheus scrape (if available)
    prometheus_metrics: Optional[dict]


def _exec(cmd: list[str]) -> tuple[int, str, str]:
    """Execute command."""
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


def get_node_cpu_usage() -> dict:
    """Get current node CPU usage from /proc/stat."""
    rc, stdout, _ = _exec_in_observer(["cat", "/proc/stat"])
    if rc != 0:
        return {}

    # Parse first line (aggregate CPU)
    first_line = stdout.split("\n")[0]
    parts = first_line.split()
    if parts[0] != "cpu":
        return {}

    return {
        "user": int(parts[1]),
        "nice": int(parts[2]),
        "system": int(parts[3]),
        "idle": int(parts[4]),
        "iowait": int(parts[5]),
        "irq": int(parts[6]),
        "softirq": int(parts[7]),
        "steal": int(parts[8]) if len(parts) > 8 else 0,
    }


def get_container_cgroup(
    pod_name: str, namespace: str, container_name: str
) -> Optional[dict]:
    """Get cgroup parameters for a specific container."""
    # Get container ID
    rc, stdout, _ = _exec(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "-n",
            namespace,
            "get",
            "pod",
            pod_name,
            "-o",
            f"jsonpath={{.status.containerStatuses[?(@.name=='{container_name}')].containerID}}",
        ]
    )

    if rc != 0 or not stdout:
        return None

    container_id = stdout.strip()
    if container_id.startswith("cri-o://"):
        container_id = container_id[8:]

    # Find cgroup path
    rc, stdout, _ = _exec_in_observer(
        [
            "find",
            "/host-sys/fs/cgroup/kubepods.slice",
            "-name",
            f"*{container_id[:12]}*",
            "-type",
            "d",
        ]
    )

    if rc != 0 or not stdout.strip():
        return None

    cgroup_path = stdout.strip().split("\n")[0]

    # Read cgroup files
    def read_file(path: str) -> Optional[str]:
        rc, content, _ = _exec_in_observer(["cat", path])
        return content.strip() if rc == 0 else None

    cpu_weight = read_file(f"{cgroup_path}/cpu.weight")
    cpu_max = read_file(f"{cgroup_path}/cpu.max")
    cpu_stat = read_file(f"{cgroup_path}/cpu.stat")
    cpu_pressure = read_file(f"{cgroup_path}/cpu.pressure")

    return {
        "container_id": container_id,
        "cgroup_path": cgroup_path,
        "cpu_weight": cpu_weight,
        "cpu_max": cpu_max,
        "cpu_stat": cpu_stat,
        "cpu_pressure": cpu_pressure,
    }


def apply_workload(workload_file: Path) -> tuple[str, str, str, str]:
    """Apply workload manifest and return pod details."""
    rc, stdout, stderr = _exec(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "apply",
            "-f",
            str(workload_file),
        ]
    )

    if rc != 0:
        raise RuntimeError(f"Failed to apply workload: {stderr}")

    # Extract pod name from kubectl output (e.g., "pod/cpu-bound-besteffort created" or "pod/name unchanged")
    # Handle multi-pod manifests by taking the first pod
    first_line = stdout.strip().split("\n")[0]
    if "/" in first_line:
        pod_name = first_line.split("/")[1].split()[0]
    else:
        raise RuntimeError(f"Could not extract pod name from: {stdout}")

    # Wait for pod to be running or completed
    print(f"  Waiting for pod {pod_name} to start...")
    for _ in range(60):
        rc, phase_out, _ = _exec(
            [
                "kubectl",
                "--kubeconfig",
                "build/kubeconfig",
                "-n",
                "cpu-limits-research",
                "get",
                "pod",
                pod_name,
                "-o",
                "jsonpath={.status.phase}",
            ]
        )
        phase = phase_out.strip()
        if phase in ("Running", "Succeeded", "Failed"):
            break
        time.sleep(1)

    # Get pod details
    rc, stdout, _ = _exec(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "-n",
            "cpu-limits-research",
            "get",
            "pod",
            pod_name,
            "-o",
            "json",
        ]
    )

    if rc != 0:
        raise RuntimeError(f"Failed to get pod details for {pod_name}")

    pod_data = json.loads(stdout)
    labels = pod_data["metadata"].get("labels", {})

    return (
        pod_name,
        labels.get("workload-type", "unknown"),
        labels.get("qos-class", "unknown"),
        pod_data["spec"]["containers"][0]["name"],
    )


def wait_for_pod_completion(
    pod_name: str, namespace: str, timeout: int = 180
) -> tuple[int, str]:
    """Wait for pod to complete and return exit code and logs."""
    start = time.time()
    while time.time() - start < timeout:
        rc, stdout, _ = _exec(
            [
                "kubectl",
                "--kubeconfig",
                "build/kubeconfig",
                "-n",
                namespace,
                "get",
                "pod",
                pod_name,
                "-o",
                "jsonpath={.status.phase}",
            ]
        )

        phase = stdout.strip()
        if phase in ("Succeeded", "Failed"):
            break

        time.sleep(1)

    # Get logs
    rc, logs, _ = _exec(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "-n",
            namespace,
            "logs",
            pod_name,
        ]
    )

    # Get exit code
    rc, stdout, _ = _exec(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "-n",
            namespace,
            "get",
            "pod",
            pod_name,
            "-o",
            "jsonpath={.status.containerStatuses[0].state.terminated.exitCode}",
        ]
    )

    exit_code = int(stdout.strip()) if stdout.strip() else None

    return exit_code, logs


def run_experiment(workload_file: Path, evidence_dir: Path) -> ExperimentRun:
    """Run a single experiment."""
    experiment_id = (
        f"{workload_file.stem}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    print(f"\n{'=' * 60}", flush=True)
    print(f"Experiment: {experiment_id}", flush=True)
    print(f"{'=' * 60}", flush=True)

    # Apply workload
    print(f"Applying workload: {workload_file.name}", flush=True)
    pod_name, workload_type, qos_class, container_name = apply_workload(workload_file)
    print(f"  Pod: {pod_name}", flush=True)
    print(f"  Type: {workload_type}, QoS: {qos_class}", flush=True)

    start_time = datetime.now(timezone.utc).isoformat()

    # Wait for container to actually start (pod phase=Running doesn't guarantee containerStatus is populated)
    print("Waiting for container to start...", flush=True)
    for _ in range(30):
        rc, stdout, _ = _exec(
            [
                "kubectl",
                "--kubeconfig",
                "build/kubeconfig",
                "-n",
                "cpu-limits-research",
                "get",
                "pod",
                pod_name,
                "-o",
                "jsonpath={.status.containerStatuses[0].containerID}",
            ]
        )
        if rc == 0 and stdout.strip():
            break
        time.sleep(1)

    # Collect baseline
    print("Collecting baseline...", flush=True)
    baseline_cgroup = get_container_cgroup(
        pod_name, "cpu-limits-research", container_name
    )
    baseline_node_cpu = get_node_cpu_usage()

    # Start BPF traces in background
    print("Starting BPF traces...", flush=True)
    # Note: These would run in background, capturing data during the workload
    # For now, we'll collect post-run

    # Collect cgroup samples during workload execution
    print("Monitoring workload...", flush=True)
    cgroup_samples = []
    sample_interval = 10  # seconds
    last_sample = time.time()

    # Poll for completion and collect samples
    while True:
        rc, stdout, _ = _exec(
            [
                "kubectl",
                "--kubeconfig",
                "build/kubeconfig",
                "-n",
                "cpu-limits-research",
                "get",
                "pod",
                pod_name,
                "-o",
                "jsonpath={.status.phase}",
            ]
        )

        phase = stdout.strip()
        if phase in ("Succeeded", "Failed"):
            break

        # Collect sample if interval elapsed
        now = time.time()
        if now - last_sample >= sample_interval:
            sample = get_container_cgroup(
                pod_name, "cpu-limits-research", container_name
            )
            if sample:  # Only append if we successfully got the data
                sample["timestamp"] = datetime.now(timezone.utc).isoformat()
                cgroup_samples.append(sample)
            last_sample = now

        time.sleep(1)

    # Get final pod status
    exit_code, logs = wait_for_pod_completion(
        pod_name, "cpu-limits-research", timeout=5
    )

    end_time = datetime.now(timezone.utc).isoformat()
    duration = (
        datetime.fromisoformat(end_time) - datetime.fromisoformat(start_time)
    ).total_seconds()

    print(
        f"Workload completed with exit code {exit_code} in {duration:.1f}s ({len(cgroup_samples)} samples)",
        flush=True,
    )

    # Collect final measurements (may fail if cgroup already cleaned up)
    print("Collecting final measurements...", flush=True)
    final_cgroup = None  # Will be the last sample or None
    if cgroup_samples:
        final_cgroup = cgroup_samples[-1]  # Use last sample as "final"
    final_node_cpu = get_node_cpu_usage()

    # Collect BPF traces (post-run for now)
    # TODO: BPF tools hang waiting for events - need to revisit
    print("Skipping BPF traces for now...", flush=True)
    runqlat_out = ""
    runqlen_out = ""
    # # runqlat
    # rc, runqlat_out, _ = _exec_in_observer(
    #     [
    #         "bpf-runqlat",
    #         "-m",
    #         "5",
    #     ]
    # )

    # # runqlen
    # rc, runqlen_out, _ = _exec_in_observer(
    #     [
    #         "bpf-runqlen",
    #         "-T",
    #         "5",
    #     ]
    # )

    # Create experiment result
    experiment = ExperimentRun(
        experiment_id=experiment_id,
        workload_name=pod_name,
        workload_type=workload_type,
        qos_class=qos_class,
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration,
        baseline_cgroup=baseline_cgroup,
        baseline_node_cpu=baseline_node_cpu,
        final_cgroup=final_cgroup,
        final_node_cpu=final_node_cpu,
        cgroup_samples=cgroup_samples if cgroup_samples else None,
        bpf_runqlat_output=runqlat_out if runqlat_out else None,
        bpf_runqlen_output=runqlen_out if runqlen_out else None,
        bpf_sched_switch_output=None,  # Would require bpftrace script
        workload_logs=logs,
        workload_exit_code=exit_code,
        prometheus_metrics=None,  # Would require Prometheus query
    )

    # Write experiment data
    exp_file = evidence_dir / f"{experiment_id}.json"
    with exp_file.open("w") as f:
        json.dump(asdict(experiment), f, indent=2)

    print(f"✓ Evidence written to {exp_file}", flush=True)

    # Cleanup pod
    print(f"Cleaning up pod {pod_name}...", flush=True)
    _exec(
        [
            "kubectl",
            "--kubeconfig",
            "build/kubeconfig",
            "-n",
            "cpu-limits-research",
            "delete",
            "pod",
            pod_name,
            "--wait=false",
        ]
    )

    return experiment


def main() -> int:
    """Run all Phase 2 experiments."""
    workload_dir = Path("research/cpu-limits/phase2/workloads")
    evidence_dir = Path("research/cpu-limits/phase2/evidence")
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # Find all workload manifests (skip contention-scenario, it has its own runner)
    workloads = sorted(
        [w for w in workload_dir.glob("*.yaml") if "contention" not in w.name]
    )
    print(f"Found {len(workloads)} workload manifests")

    experiments = []
    for workload_file in workloads:
        try:
            experiment = run_experiment(workload_file, evidence_dir)
            experiments.append(experiment)

            # Wait between experiments
            print("Waiting 10s before next experiment...")
            time.sleep(10)

        except Exception as e:
            print(f"✗ Experiment failed: {e}", file=sys.stderr)
            continue

    # Write summary
    summary_file = evidence_dir / "summary.json"
    with summary_file.open("w") as f:
        json.dump([asdict(exp) for exp in experiments], f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"Completed {len(experiments)} experiments")
    print(f"Summary: {summary_file}")
    print(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
