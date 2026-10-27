#!/usr/bin/env python3
"""
Phase 6 Topic 6.2: BPF-Throttling Correlation

Collects BPF scheduler traces during CPU throttling experiments and correlates
with cgroup throttling metrics.

This shows the low-level scheduler behavior that causes performance degradation.
"""

import subprocess
import json
import time
import sys
from pathlib import Path
from datetime import datetime

EVIDENCE_DIR = Path(__file__).parent.parent.parent / "evidence"
WORKLOADS_DIR = Path(__file__).parent.parent.parent / "phase4" / "workloads"

BPF_SCRIPT = """
#!/usr/bin/env bpftrace

/*
 * Trace scheduler events for CPU throttling correlation
 * 
 * Events:
 * - sched:sched_switch: Context switches
 * - sched:sched_wakeup: Process wakeup
 * - sched:sched_wait_task: Waiting time
 * - sched:sched_stat_runtime: Actual runtime
 */

BEGIN {
    printf("Tracing scheduler events...\\n");
}

/* Track context switches per process */
tracepoint:sched:sched_switch {
    @switch_count[args->next_comm] = count();
    @switch_per_sec = count();
}

/* Track wakeup latency */
tracepoint:sched:sched_wakeup {
    @wakeups[args->comm] = count();
}

/* Track wait time (time spent not running) */
tracepoint:sched:sched_stat_wait {
    @wait_time_us[args->comm] = sum(args->delay / 1000);
    @wait_count[args->comm] = count();
}

/* Track actual CPU runtime */
tracepoint:sched:sched_stat_runtime {
    @runtime_us[args->comm] = sum(args->runtime / 1000);
}

/* Every 5 seconds, print summary */
interval:s:5 {
    time("%H:%M:%S ");
    printf("switches/sec=%ld\\n", @switch_per_sec);
    clear(@switch_per_sec);
}

END {
    printf("\\n=== Context Switches ===\\n");
    print(@switch_count);
    
    printf("\\n=== Wakeups ===\\n");
    print(@wakeups);
    
    printf("\\n=== Wait Time (us) ===\\n");
    print(@wait_time_us);
    
    printf("\\n=== Wait Count ===\\n");
    print(@wait_count);
    
    printf("\\n=== Runtime (us) ===\\n");
    print(@runtime_us);
    
    /* Clean up */
    clear(@switch_count);
    clear(@switch_per_sec);
    clear(@wakeups);
    clear(@wait_time_us);
    clear(@wait_count);
    clear(@runtime_us);
}
"""


def check_bpftrace():
    """Check if bpftrace is available"""
    try:
        result = subprocess.run(
            ["bpftrace", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print(f"✓ bpftrace found: {result.stdout.strip()}")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    print("✗ bpftrace not found")
    print("  Install: sudo apt install bpftrace (Ubuntu/Debian)")
    print("  Or: https://github.com/iovisor/bpftrace/blob/master/INSTALL.md")
    return False


def run_bpf_trace(duration_seconds, output_file):
    """Run BPF trace for specified duration"""
    script_file = Path("/tmp/throttle-trace.bt")
    script_file.write_text(BPF_SCRIPT)

    print(f"  Starting BPF trace for {duration_seconds}s...")

    try:
        # Run bpftrace (requires root)
        result = subprocess.run(
            ["sudo", "bpftrace", str(script_file)],
            capture_output=True,
            text=True,
            timeout=duration_seconds + 10,
        )

        output_file.write_text(result.stdout)
        print(f"  ✓ BPF trace saved to {output_file}")
        return True

    except subprocess.TimeoutExpired:
        print(f"  ✗ BPF trace timed out")
        return False
    except PermissionError:
        print(f"  ✗ BPF trace requires root (sudo)")
        return False


def get_cgroup_stats(pod_name):
    """Get cgroup CPU throttling stats for a pod"""
    try:
        # Get pod UID
        result = subprocess.run(
            ["kubectl", "get", "pod", pod_name, "-o", "jsonpath={.metadata.uid}"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            return None

        pod_uid = result.stdout.strip()

        # Read cgroup stats from node (this is a simplified approach)
        # In production, you'd need to SSH to the node or use a daemonset
        cgroup_base = f"/sys/fs/cgroup/kubepods.slice/kubepods-pod{pod_uid.replace('-', '_')}.slice"

        stats = {}

        # Try to read cpu.stat
        cpu_stat_file = Path(cgroup_base) / "cpu.stat"
        if cpu_stat_file.exists():
            content = cpu_stat_file.read_text()
            for line in content.strip().split("\n"):
                key, value = line.split()
                stats[key] = int(value)

        return stats

    except Exception as e:
        print(f"  Warning: Could not read cgroup stats: {e}")
        return None


def run_experiment(workload_file, trace_duration=60):
    """Run a single throttling experiment with BPF tracing"""
    workload_name = workload_file.stem
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    print(f"\n{'=' * 80}")
    print(f"Experiment: {workload_name}")
    print(f"{'=' * 80}")

    # Apply workload
    print(f"  Applying workload: {workload_file.name}")
    subprocess.run(["kubectl", "apply", "-f", str(workload_file)], capture_output=True)

    # Wait for pod to start
    pod_name = workload_name
    print(f"  Waiting for pod {pod_name} to start...")

    for _ in range(30):
        result = subprocess.run(
            ["kubectl", "get", "pod", pod_name, "-o", "jsonpath={.status.phase}"],
            capture_output=True,
            text=True,
        )

        if result.stdout == "Running":
            print(f"  ✓ Pod started")
            break

        time.sleep(2)
    else:
        print(f"  ✗ Pod did not start")
        return None

    # Get baseline cgroup stats
    time.sleep(5)
    stats_before = get_cgroup_stats(pod_name)

    # Start BPF trace
    bpf_output = EVIDENCE_DIR / f"bpf-trace-{workload_name}-{timestamp}.txt"
    bpf_success = run_bpf_trace(trace_duration, bpf_output)

    # Get final cgroup stats
    stats_after = get_cgroup_stats(pod_name)

    # Calculate deltas
    throttling_stats = {}
    if stats_before and stats_after:
        for key in ["nr_periods", "nr_throttled", "throttled_usec"]:
            if key in stats_after and key in stats_before:
                throttling_stats[key] = stats_after[key] - stats_before[key]

        # Calculate throttling percentage
        if throttling_stats.get("nr_periods", 0) > 0:
            throttle_pct = (
                throttling_stats.get("nr_throttled", 0)
                / throttling_stats["nr_periods"]
                * 100
            )
            throttling_stats["throttle_pct"] = round(throttle_pct, 1)

    # Wait for workload completion
    print(f"  Waiting for workload completion...")
    for _ in range(120):
        result = subprocess.run(
            ["kubectl", "get", "pod", pod_name, "-o", "jsonpath={.status.phase}"],
            capture_output=True,
            text=True,
        )

        if result.stdout in ["Succeeded", "Failed"]:
            break

        time.sleep(1)

    # Parse BPF output
    bpf_metrics = parse_bpf_output(bpf_output) if bpf_success else {}

    # Build evidence
    evidence = {
        "workload": workload_name,
        "timestamp": timestamp,
        "trace_duration_seconds": trace_duration,
        "throttling_stats": throttling_stats,
        "bpf_metrics": bpf_metrics,
        "bpf_trace_file": str(bpf_output) if bpf_success else None,
    }

    # Write evidence
    evidence_file = EVIDENCE_DIR / f"bpf-correlation-{workload_name}-{timestamp}.json"
    with open(evidence_file, "w") as f:
        json.dump(evidence, f, indent=2)

    print(f"  ✓ Evidence written to {evidence_file}")

    # Cleanup
    subprocess.run(
        ["kubectl", "delete", "pod", pod_name, "--ignore-not-found"],
        capture_output=True,
    )

    return evidence


def parse_bpf_output(bpf_file):
    """Parse BPF trace output to extract key metrics"""
    if not bpf_file.exists():
        return {}

    content = bpf_file.read_text()
    metrics = {}

    # Extract switches per second (average)
    switches = []
    for line in content.split("\n"):
        if "switches/sec=" in line:
            try:
                value = int(line.split("=")[1])
                switches.append(value)
            except:
                pass

    if switches:
        metrics["avg_switches_per_sec"] = sum(switches) / len(switches)
        metrics["max_switches_per_sec"] = max(switches)

    # Extract total counts (simplified parsing)
    # In production, parse the full histogram output

    return metrics


def main():
    print("=" * 80)
    print("Phase 6 Topic 6.2: BPF-Throttling Correlation")
    print("=" * 80)
    print()
    print("This experiment correlates CPU throttling with scheduler behavior.")
    print("It requires:")
    print("  - bpftrace installed")
    print("  - sudo/root access")
    print("  - Phase 4 workloads available")
    print()

    # Check prerequisites
    if not check_bpftrace():
        print("\n✗ Prerequisites not met")
        sys.exit(1)

    # Ensure evidence directory exists
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    # Find Phase 4 workloads
    workloads = sorted(WORKLOADS_DIR.glob("cpu-bound-*.yaml"))

    if not workloads:
        print(f"\n✗ No workloads found in {WORKLOADS_DIR}")
        sys.exit(1)

    print(f"\nFound {len(workloads)} workload(s)")
    print()

    # Run experiments
    results = []
    for workload_file in workloads:
        evidence = run_experiment(workload_file, trace_duration=60)
        if evidence:
            results.append(evidence)

        # Wait between experiments
        print("\n  Waiting 10s before next experiment...")
        time.sleep(10)

    # Write summary
    summary_file = EVIDENCE_DIR / "bpf-correlation-summary.json"
    with open(summary_file, "w") as f:
        json.dump(
            {
                "test": "bpf-throttling-correlation",
                "timestamp": datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
                "experiments": len(results),
                "results": results,
            },
            f,
            indent=2,
        )

    print("\n" + "=" * 80)
    print(f"Completed {len(results)} experiments")
    print(f"Summary: {summary_file}")
    print("=" * 80)


if __name__ == "__main__":
    main()
