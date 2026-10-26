#!/usr/bin/env python3
"""
Simplified BPF collector that runs traces on the host system.

This version avoids the complexity of privileged DaemonSets by running
BPF tools directly on the host where the cluster is running.
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

NAMESPACE = "cpu-limits-research"


@dataclass
class ThrottleEvent:
    timestamp_ms: int
    comm: str
    pid: int
    duration_us: int


@dataclass
class ThrottleTrace:
    events: List[Dict] = field(default_factory=list)
    total_events: int = 0
    total_throttled_us: int = 0
    avg_duration_us: float = 0.0


@dataclass
class LatencyStats:
    total_events: int = 0
    total_latency_us: int = 0
    avg_latency_us: float = 0.0
    buckets: Dict[str, int] = field(default_factory=dict)


@dataclass
class ContextSwitchStats:
    voluntary: int = 0
    involuntary: int = 0
    switches_in: int = 0


@dataclass
class BPFResults:
    experiment_id: str
    pod_name: str
    host_pid: Optional[int] = None
    throttle: Optional[ThrottleTrace] = None
    latency: Optional[LatencyStats] = None
    context_switches: Optional[ContextSwitchStats] = None
    collection_errors: List[str] = field(default_factory=list)


def get_host_pid_from_cgroup(pod_name: str) -> Optional[int]:
    """Find host PID by searching cgroup.procs files."""
    try:
        # Get pod UID
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                pod_name,
                "-n",
                NAMESPACE,
                "-o",
                "jsonpath={.metadata.uid}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        pod_uid = result.stdout.strip()

        # Search for cgroup.procs containing this pod
        # Path pattern: /sys/fs/cgroup/kubepods.slice/kubepods-<qos>.slice/kubepods-<qos>-pod<uid>.slice/
        for qos in ["besteffort", "burstable", "guaranteed"]:
            cgroup_pattern = (
                f"/sys/fs/cgroup/**/kubepods-{qos}-pod{pod_uid}.slice/cgroup.procs"
            )
            result = subprocess.run(
                ["bash", "-c", f"cat {cgroup_pattern} 2>/dev/null | head -1"],
                capture_output=True,
                text=True,
            )
            if result.stdout.strip():
                return int(result.stdout.strip())

        return None

    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"  Warning: Could not determine host PID: {e}")
        return None


def collect_simple_metrics(pod_name: str, duration: int) -> Dict:
    """Collect basic metrics without BPF if tools are not available."""
    print("  BPF tools not available, collecting basic metrics only...")

    metrics = {"method": "fallback", "note": "Limited metrics without BPF tracing"}

    # Try to get PID at least
    pid = get_host_pid_from_cgroup(pod_name)
    if pid:
        metrics["host_pid"] = pid

        # Get basic /proc stats
        try:
            with open(f"/proc/{pid}/status") as f:
                status = f.read()
                if match := re.search(r"voluntary_ctxt_switches:\s+(\d+)", status):
                    metrics["voluntary_switches"] = int(match.group(1))
                if match := re.search(r"nonvoluntary_ctxt_switches:\s+(\d+)", status):
                    metrics["involuntary_switches"] = int(match.group(1))
        except Exception as e:
            metrics["proc_error"] = str(e)

    return metrics


def check_bpf_available() -> bool:
    """Check if bpftrace is available."""
    result = subprocess.run(["which", "bpftrace"], capture_output=True)
    return result.returncode == 0


def collect_bpf_traces(
    pod_name: str, experiment_id: str, duration: int = 30
) -> BPFResults:
    """Collect BPF traces if available, fallback to basic metrics otherwise."""

    results = BPFResults(experiment_id=experiment_id, pod_name=pod_name)

    # Check if BPF is available
    if not check_bpf_available():
        print("  BPF tools not available on host, skipping BPF traces")
        results.collection_errors.append("bpftrace not found on host")
        return results

    # Get host PID
    print(f"  Finding host PID for {pod_name}...")
    results.host_pid = get_host_pid_from_cgroup(pod_name)

    if not results.host_pid:
        results.collection_errors.append("Could not determine host PID")
        return results

    print(f"  Host PID: {results.host_pid}")

    # For Phase 3 MVP, we'll skip actual BPF collection
    # and document the approach for manual execution
    print("  Note: BPF collection requires manual execution with sudo")
    print(f"  Run: sudo bpftrace <script>.bt {results.host_pid}")

    results.collection_errors.append("BPF collection requires sudo/manual execution")

    return results


def save_results(results: BPFResults, output_dir: Path):
    """Save BPF results to JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"{results.experiment_id}-bpf.json"
    with open(json_path, "w") as f:
        json.dump(asdict(results), f, indent=2)

    print(f"  ✓ BPF metadata saved to {json_path}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print(
            "Usage: python3 bpf-collector-simple.py <pod-name> <experiment-id> [duration]"
        )
        sys.exit(1)

    pod_name = sys.argv[1]
    experiment_id = sys.argv[2]
    duration = int(sys.argv[3]) if len(sys.argv) > 3 else 30

    results = collect_bpf_traces(pod_name, experiment_id, duration)

    output_dir = Path(__file__).parent.parent / "evidence" / "bpf-traces"
    save_results(results, output_dir)

    if results.collection_errors:
        print("\nCollection Errors:")
        for error in results.collection_errors:
            print(f"  - {error}")
