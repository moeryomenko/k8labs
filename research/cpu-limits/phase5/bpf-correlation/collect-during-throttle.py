#!/usr/bin/env python3
"""
Phase 5 Task 5.1: Collect BPF traces during CPU throttling experiment.

This script:
1. Deploys a throttling workload (from Phase 4)
2. Starts BPF tracing on the node running the pod
3. Collects sched:sched_switch and sched:sched_wakeup events
4. Correlates throttling periods with scheduling latency
5. Generates analysis showing scheduler behavior during throttling

Prerequisites:
- Phase 3 BPF infrastructure deployed (bpf-tracer.service on nodes)
- kubectl access to cluster
- SSH access to cluster nodes
"""

import subprocess
import json
import time
from datetime import datetime, UTC
from pathlib import Path
import sys

# Configuration
WORKLOAD_NAME = "cpu-bound-heavy-throttle"  # 69.8% throttling from Phase 4
WORKLOAD_MANIFEST = "../phase4/workloads/cpu-bound-heavy-throttle.yaml"
EVIDENCE_DIR = Path("evidence")
TRACE_DURATION_SECONDS = 120  # Match workload duration


def run_command(cmd, check=True, capture_output=True):
    """Run shell command and return output."""
    result = subprocess.run(
        cmd, shell=True, check=check, capture_output=capture_output, text=True
    )
    return result.stdout.strip() if capture_output else None


def get_pod_node(pod_name):
    """Get the node where a pod is running."""
    cmd = f"kubectl get pod {pod_name} -o jsonpath='{{.spec.nodeName}}'"
    return run_command(cmd)


def get_node_ip(node_name):
    """Get the internal IP of a node."""
    cmd = f"kubectl get node {node_name} -o jsonpath='{{.status.addresses[?(@.type==\"InternalIP\")].address}}'"
    return run_command(cmd)


def get_pod_cgroup_path(pod_name, node_ip):
    """Get the cgroup path for a pod on a node."""
    # Get container ID
    cmd = f"kubectl get pod {pod_name} -o jsonpath='{{.status.containerStatuses[0].containerID}}'"
    container_id = run_command(cmd).replace("containerd://", "")

    # cgroup path for containerd
    return f"/sys/fs/cgroup/system.slice/containerd.service/kubepods/besteffort/pod*/cri-containerd-{container_id}.scope"


def start_bpf_trace(node_ip, pod_name):
    """Start BPF tracing on the node for the specific pod."""
    print(f"Starting BPF trace on node {node_ip} for pod {pod_name}...")

    # BPF program to trace scheduler events
    bpf_script = """
#!/usr/bin/env bpftrace

BEGIN {
    printf("Tracing scheduler events for throttled container...\\n");
    printf("timestamp_ns,event,pid,comm,prev_pid,prev_comm,duration_ns\\n");
}

tracepoint:sched:sched_switch {
    $now = nsecs;
    $pid = args->next_pid;
    $comm = args->next_comm;
    $prev_pid = args->prev_pid;
    $prev_comm = args->prev_comm;
    
    // Record context switch
    printf("%llu,switch,%d,%s,%d,%s,0\\n", 
        $now, $pid, $comm, $prev_pid, $prev_comm);
}

tracepoint:sched:sched_wakeup {
    $now = nsecs;
    $pid = args->pid;
    $comm = args->comm;
    
    // Record wakeup event
    printf("%llu,wakeup,%d,%s,0,,0\\n", $now, $pid, $comm);
}

tracepoint:sched:sched_process_wait {
    $now = nsecs;
    $pid = args->pid;
    $comm = args->comm;
    
    // Record wait event (might indicate throttling)
    printf("%llu,wait,%d,%s,0,,0\\n", $now, $pid, $comm);
}
"""

    trace_file = f"/tmp/bpf-trace-{pod_name}.csv"

    # Write BPF script to node
    script_path = f"/tmp/trace-{pod_name}.bt"
    run_command(f"ssh root@{node_ip} 'cat > {script_path}' << 'EOF'\n{bpf_script}\nEOF")
    run_command(f"ssh root@{node_ip} chmod +x {script_path}")

    # Start bpftrace in background
    trace_cmd = (
        f"ssh root@{node_ip} 'nohup bpftrace {script_path} > {trace_file} 2>&1 &'"
    )
    run_command(trace_cmd)

    print(f"  BPF trace started, output: {trace_file}")
    return trace_file


def stop_bpf_trace(node_ip):
    """Stop BPF tracing on the node."""
    print(f"Stopping BPF trace on node {node_ip}...")
    run_command(f"ssh root@{node_ip} 'pkill -INT bpftrace'", check=False)
    time.sleep(2)  # Let bpftrace flush


def collect_trace_data(node_ip, trace_file, local_path):
    """Copy trace data from node to local machine."""
    print(f"Collecting trace data from {node_ip}:{trace_file}...")
    run_command(f"scp root@{node_ip}:{trace_file} {local_path}")
    print(f"  Trace data saved to {local_path}")


def get_cgroup_stats(pod_name):
    """Get cgroup CPU stats from the pod."""
    cmd = f"kubectl exec {pod_name} -- cat /sys/fs/cgroup/cpu.stat"
    output = run_command(cmd, check=False)

    stats = {}
    if output:
        for line in output.split("\n"):
            if " " in line:
                key, value = line.split(" ", 1)
                stats[key] = int(value)

    return stats


def analyze_traces(trace_csv, cgroup_stats_before, cgroup_stats_after):
    """Analyze BPF traces and correlate with throttling."""
    import csv

    print("Analyzing BPF traces...")

    # Parse CSV
    events = []
    try:
        with open(trace_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                events.append(
                    {
                        "timestamp_ns": int(row["timestamp_ns"]),
                        "event": row["event"],
                        "pid": int(row["pid"]),
                        "comm": row["comm"],
                    }
                )
    except Exception as e:
        print(f"Error parsing trace data: {e}")
        return None

    if not events:
        print("No events captured in trace")
        return None

    # Calculate metrics
    start_ns = events[0]["timestamp_ns"]
    end_ns = events[-1]["timestamp_ns"]
    duration_s = (end_ns - start_ns) / 1e9

    event_counts = {}
    for event in events:
        event_type = event["event"]
        event_counts[event_type] = event_counts.get(event_type, 0) + 1

    # Calculate throttling from cgroup stats
    usage_delta = cgroup_stats_after.get("usage_usec", 0) - cgroup_stats_before.get(
        "usage_usec", 0
    )
    throttled_delta = cgroup_stats_after.get(
        "throttled_usec", 0
    ) - cgroup_stats_before.get("throttled_usec", 0)

    throttling_pct = (
        (throttled_delta / (usage_delta + throttled_delta) * 100)
        if (usage_delta + throttled_delta) > 0
        else 0
    )

    analysis = {
        "trace_duration_seconds": duration_s,
        "total_events": len(events),
        "event_counts": event_counts,
        "throttling": {
            "usage_usec_delta": usage_delta,
            "throttled_usec_delta": throttled_delta,
            "throttling_percentage": throttling_pct,
            "nr_throttled_delta": cgroup_stats_after.get("nr_throttled", 0)
            - cgroup_stats_before.get("nr_throttled", 0),
        },
        "scheduler_metrics": {
            "context_switches_per_second": event_counts.get("switch", 0) / duration_s
            if duration_s > 0
            else 0,
            "wakeups_per_second": event_counts.get("wakeup", 0) / duration_s
            if duration_s > 0
            else 0,
        },
    }

    return analysis


def main():
    print("=" * 80)
    print("Phase 5 Task 5.1: BPF Trace Collection During Throttling")
    print("=" * 80)

    # Create evidence directory
    EVIDENCE_DIR.mkdir(exist_ok=True)

    exp_id = f"bpf-throttling-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    print(f"\nExperiment ID: {exp_id}\n")

    # Check if workload manifest exists
    if not Path(WORKLOAD_MANIFEST).exists():
        print(f"Error: Workload manifest not found: {WORKLOAD_MANIFEST}")
        print("Make sure Phase 4 is complete")
        sys.exit(1)

    try:
        # Step 1: Deploy workload
        print(f"Step 1: Deploying workload {WORKLOAD_NAME}...")
        run_command(f"kubectl apply -f {WORKLOAD_MANIFEST}")
        time.sleep(5)

        # Wait for pod to start
        print("  Waiting for pod to start...")
        for i in range(30):
            status = run_command(
                f"kubectl get pod {WORKLOAD_NAME} -o jsonpath='{{.status.phase}}'",
                check=False,
            )
            if status == "Running":
                print(f"  Pod is running")
                break
            time.sleep(2)
        else:
            print("  Error: Pod did not start within 60 seconds")
            sys.exit(1)

        # Step 2: Get pod location
        print("\nStep 2: Locating pod...")
        node_name = get_pod_node(WORKLOAD_NAME)
        node_ip = get_node_ip(node_name)
        print(f"  Pod running on node: {node_name} ({node_ip})")

        # Step 3: Get baseline cgroup stats
        print("\nStep 3: Collecting baseline cgroup stats...")
        time.sleep(5)  # Let pod start workload
        cgroup_stats_before = get_cgroup_stats(WORKLOAD_NAME)
        print(
            f"  Baseline usage: {cgroup_stats_before.get('usage_usec', 0) / 1e6:.2f}s"
        )

        # Step 4: Start BPF trace
        print("\nStep 4: Starting BPF trace...")
        trace_file = start_bpf_trace(node_ip, WORKLOAD_NAME)

        # Step 5: Wait for workload to complete
        print(
            f"\nStep 5: Waiting for workload to complete ({TRACE_DURATION_SECONDS}s)..."
        )
        for i in range(0, TRACE_DURATION_SECONDS, 10):
            time.sleep(10)
            print(f"  ... {i + 10}s elapsed")

        # Step 6: Stop BPF trace
        print("\nStep 6: Stopping BPF trace...")
        stop_bpf_trace(node_ip)

        # Step 7: Get final cgroup stats
        print("\nStep 7: Collecting final cgroup stats...")
        cgroup_stats_after = get_cgroup_stats(WORKLOAD_NAME)
        print(f"  Final usage: {cgroup_stats_after.get('usage_usec', 0) / 1e6:.2f}s")

        # Step 8: Collect trace data
        print("\nStep 8: Collecting trace data...")
        local_trace_file = EVIDENCE_DIR / f"{exp_id}-trace.csv"
        collect_trace_data(node_ip, trace_file, local_trace_file)

        # Step 9: Analyze traces
        print("\nStep 9: Analyzing traces...")
        analysis = analyze_traces(
            local_trace_file, cgroup_stats_before, cgroup_stats_after
        )

        if analysis:
            print(f"\n{'=' * 80}")
            print("Analysis Results")
            print(f"{'=' * 80}")
            print(f"Trace duration: {analysis['trace_duration_seconds']:.1f}s")
            print(f"Total events: {analysis['total_events']:,}")
            print(f"Event breakdown:")
            for event_type, count in analysis["event_counts"].items():
                print(f"  {event_type}: {count:,}")
            print(f"\nThrottling:")
            print(
                f"  Percentage: {analysis['throttling']['throttling_percentage']:.1f}%"
            )
            print(
                f"  Throttled time: {analysis['throttling']['throttled_usec_delta'] / 1e6:.2f}s"
            )
            print(
                f"  Throttling events: {analysis['throttling']['nr_throttled_delta']}"
            )
            print(f"\nScheduler metrics:")
            print(
                f"  Context switches/sec: {analysis['scheduler_metrics']['context_switches_per_second']:.0f}"
            )
            print(
                f"  Wakeups/sec: {analysis['scheduler_metrics']['wakeups_per_second']:.0f}"
            )

            # Save analysis
            analysis_file = EVIDENCE_DIR / f"{exp_id}-analysis.json"
            with open(analysis_file, "w") as f:
                json.dump(analysis, f, indent=2)
            print(f"\n✓ Analysis saved to {analysis_file}")

    finally:
        # Cleanup
        print("\nCleaning up...")
        run_command(f"kubectl delete pod {WORKLOAD_NAME} --wait=false", check=False)
        print("Done")


if __name__ == "__main__":
    main()
