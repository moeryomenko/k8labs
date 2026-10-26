#!/usr/bin/env python3
"""
BPF trace collector and parser for CPU limits research.

Runs BPF traces via the bpf-collector DaemonSet and parses the output
into structured JSON for analysis.
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

NAMESPACE = "cpu-limits-research"
COLLECTOR_POD = "bpf-collector"


@dataclass
class ThrottleEvent:
    """Single CFS throttle event."""

    timestamp_ms: int
    comm: str
    pid: int
    duration_us: int


@dataclass
class ThrottleTrace:
    """Aggregated throttle trace data."""

    events: List[ThrottleEvent] = field(default_factory=list)
    total_events: int = 0
    total_throttled_us: int = 0
    avg_duration_us: float = 0.0
    max_duration_us: int = 0


@dataclass
class LatencyHistogram:
    """Scheduling latency histogram."""

    buckets: Dict[str, int] = field(default_factory=dict)
    total_events: int = 0
    total_latency_us: int = 0
    avg_latency_us: float = 0.0


@dataclass
class ContextSwitchStats:
    """Context switch statistics."""

    voluntary: int = 0
    involuntary: int = 0
    switches_in: int = 0
    total_switches_out: int = 0


@dataclass
class BPFTraceResults:
    """Complete BPF trace results for an experiment."""

    experiment_id: str
    pod_name: str
    host_pid: Optional[int] = None
    throttle: Optional[ThrottleTrace] = None
    latency: Optional[LatencyHistogram] = None
    context_switches: Optional[ContextSwitchStats] = None
    raw_output: Dict[str, str] = field(default_factory=dict)


def get_collector_pod() -> str:
    """Find the bpf-collector pod on the same node as the target pod."""
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            NAMESPACE,
            "-l",
            "app=bpf-collector",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_host_pid(pod_name: str) -> Optional[int]:
    """Get host PID for the main container process."""
    try:
        # Get container ID
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                pod_name,
                "-n",
                NAMESPACE,
                "-o",
                "jsonpath={.status.containerStatuses[0].containerID}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        container_id = result.stdout.strip().split("//")[-1][:12]

        # Find PID via cgroup
        collector = get_collector_pod()
        result = subprocess.run(
            [
                "kubectl",
                "exec",
                "-n",
                NAMESPACE,
                collector,
                "--",
                "bash",
                "-c",
                f"grep -l {container_id} /host-proc/*/cgroup 2>/dev/null | "
                f"head -1 | cut -d/ -f3",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        pid_str = result.stdout.strip()
        return int(pid_str) if pid_str else None

    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"Warning: Could not determine host PID: {e}")
        return None


def run_bpf_trace(script_name: str, duration_sec: int, args: List[str] = None) -> str:
    """Run a BPF trace script via the collector pod."""
    collector = get_collector_pod()
    args = args or []

    # Build bpftrace command
    cmd = [
        "timeout",
        str(duration_sec),
        "/tools/bpftrace",
        f"/bpf-traces/{script_name}",
        *args,
    ]

    print(f"  Running BPF trace: {script_name} (timeout={duration_sec}s)")

    try:
        result = subprocess.run(
            [
                "kubectl",
                "exec",
                "-n",
                NAMESPACE,
                collector,
                "--",
                "bash",
                "-c",
                " ".join(cmd),
            ],
            capture_output=True,
            text=True,
            timeout=duration_sec + 5,
        )
        return result.stdout + result.stderr

    except subprocess.TimeoutExpired:
        print(f"  BPF trace {script_name} timed out (expected)")
        return ""


def parse_throttle_trace(output: str) -> ThrottleTrace:
    """Parse throttle-events.bt output."""
    trace = ThrottleTrace()

    # Parse individual events
    event_pattern = re.compile(r"(\d+)\s+(\S+)\s+(\d+)\s+(\d+)")
    for match in event_pattern.finditer(output):
        trace.events.append(
            ThrottleEvent(
                timestamp_ms=int(match.group(1)),
                comm=match.group(2),
                pid=int(match.group(3)),
                duration_us=int(match.group(4)),
            )
        )

    # Parse summary
    if match := re.search(r"Total throttle events:\s+(\d+)", output):
        trace.total_events = int(match.group(1))

    if match := re.search(r"Total throttled time:\s+(\d+)\s+us", output):
        trace.total_throttled_us = int(match.group(1))

    if trace.events:
        durations = [e.duration_us for e in trace.events]
        trace.avg_duration_us = sum(durations) / len(durations)
        trace.max_duration_us = max(durations)

    return trace


def parse_latency_histogram(output: str) -> LatencyHistogram:
    """Parse sched-latency.bt histogram output."""
    hist = LatencyHistogram()

    # Parse histogram buckets
    bucket_pattern = re.compile(r"\[(\d+)(?:,\s*(\d+))?\)\s+\d+\s+\|@+\|\s+(\d+)")
    for match in bucket_pattern.finditer(output):
        low = match.group(1)
        high = match.group(2) or "inf"
        count = int(match.group(3))
        hist.buckets[f"[{low}-{high})"] = count

    # Parse summary
    if match := re.search(r"Total scheduling events:\s+(\d+)", output):
        hist.total_events = int(match.group(1))

    if match := re.search(r"Total latency:\s+(\d+)\s+us", output):
        hist.total_latency_us = int(match.group(1))

    if match := re.search(r"Average latency:\s+(\d+)\s+us", output):
        hist.avg_latency_us = float(match.group(1))

    return hist


def parse_context_switches(output: str) -> ContextSwitchStats:
    """Parse context-switches.bt output."""
    stats = ContextSwitchStats()

    if match := re.search(r"Voluntary switches.*?:\s+(\d+)", output):
        stats.voluntary = int(match.group(1))

    if match := re.search(r"Involuntary switches.*?:\s+(\d+)", output):
        stats.involuntary = int(match.group(1))

    if match := re.search(r"Switches in.*?:\s+(\d+)", output):
        stats.switches_in = int(match.group(1))

    stats.total_switches_out = stats.voluntary + stats.involuntary

    return stats


def collect_bpf_traces(
    pod_name: str, experiment_id: str, duration_sec: int = 120
) -> BPFTraceResults:
    """Collect all BPF traces for a workload."""
    results = BPFTraceResults(experiment_id=experiment_id, pod_name=pod_name)

    # Get host PID
    print(f"  Resolving host PID for {pod_name}...")
    results.host_pid = get_host_pid(pod_name)

    if not results.host_pid:
        print("  Warning: Could not determine host PID, skipping PID-specific traces")
        return results

    pid_str = str(results.host_pid)

    # Run traces in parallel (TODO: implement parallel execution)
    print(f"  Collecting BPF traces for PID {results.host_pid}...")

    # 1. Throttle events
    output = run_bpf_trace("throttle-events.bt", duration_sec)
    results.raw_output["throttle"] = output
    results.throttle = parse_throttle_trace(output)

    # 2. Scheduling latency
    output = run_bpf_trace("sched-latency.bt", duration_sec, [pid_str])
    results.raw_output["latency"] = output
    results.latency = parse_latency_histogram(output)

    # 3. Context switches
    output = run_bpf_trace("context-switches.bt", duration_sec, [pid_str])
    results.raw_output["context_switches"] = output
    results.context_switches = parse_context_switches(output)

    return results


def save_trace_results(results: BPFTraceResults, output_dir: Path):
    """Save BPF trace results to JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save structured data
    json_path = output_dir / f"{results.experiment_id}-bpf.json"
    with open(json_path, "w") as f:
        json.dump(asdict(results), f, indent=2)

    print(f"  ✓ BPF traces saved to {json_path}")

    # Save raw output
    for trace_type, output in results.raw_output.items():
        raw_path = output_dir / f"{results.experiment_id}-{trace_type}.txt"
        with open(raw_path, "w") as f:
            f.write(output)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python3 bpf-collector.py <pod-name> <experiment-id> [duration]")
        sys.exit(1)

    pod_name = sys.argv[1]
    experiment_id = sys.argv[2]
    duration = int(sys.argv[3]) if len(sys.argv) > 3 else 120

    results = collect_bpf_traces(pod_name, experiment_id, duration)

    output_dir = Path(__file__).parent.parent / "evidence" / "bpf-traces"
    save_trace_results(results, output_dir)

    print("\nBPF Trace Summary:")
    if results.throttle:
        print(f"  Throttle events: {results.throttle.total_events}")
        print(f"  Total throttled: {results.throttle.total_throttled_us / 1000:.1f} ms")

    if results.latency:
        print(f"  Scheduling events: {results.latency.total_events}")
        print(f"  Avg latency: {results.latency.avg_latency_us:.1f} µs")

    if results.context_switches:
        print(f"  Context switches: {results.context_switches.total_switches_out}")
        print(f"    Voluntary: {results.context_switches.voluntary}")
        print(f"    Involuntary: {results.context_switches.involuntary}")
