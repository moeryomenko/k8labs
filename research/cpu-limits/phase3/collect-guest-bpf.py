#!/usr/bin/env python3
"""
Orchestrate BPF trace collection on Kubernetes guest VMs.

This script:
1. Deploys workloads to the cluster
2. Identifies which node they run on
3. Provides commands to SSH into the node and run BPF traces
4. Correlates BPF output with cgroup metrics

Usage:
    # Interactive mode: guides you through collection
    python3 collect-guest-bpf.py

    # Generate trace commands only
    python3 collect-guest-bpf.py --dry-run

    # Analyze existing trace files
    python3 collect-guest-bpf.py --analyze
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

NAMESPACE = "cpu-limits-research"
WORKLOAD_DIR = Path("research/cpu-limits/phase2/workloads")
EVIDENCE_DIR = Path("research/cpu-limits/phase3/bpf-traces")


def kubectl(*args) -> str:
    """Run kubectl command and return output."""
    result = subprocess.run(
        ["kubectl"] + list(args), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def get_pod_info(pod_name: str) -> Dict[str, str]:
    """Get pod UID, node, and QoS class."""
    cmd_template = (
        f"kubectl get pod {pod_name} -n {NAMESPACE} -o jsonpath='{{{{.{{}}}}}}"
    )

    uid = kubectl(
        "get", "pod", pod_name, "-n", NAMESPACE, "-o", "jsonpath={.metadata.uid}"
    )
    node = kubectl(
        "get", "pod", pod_name, "-n", NAMESPACE, "-o", "jsonpath={.spec.nodeName}"
    )
    qos = kubectl(
        "get", "pod", pod_name, "-n", NAMESPACE, "-o", "jsonpath={.status.qosClass}"
    )
    node_ip = kubectl(
        "get",
        "node",
        node,
        "-o",
        "jsonpath={.status.addresses[?(@.type=='InternalIP')].address}",
    )

    return {
        "uid": uid,
        "node": node,
        "node_ip": node_ip,
        "qos": qos.lower() if qos else "burstable",
    }


def generate_bpf_script(pod_info: Dict[str, str], trace_type: str = "throttle") -> str:
    """Generate bpftrace script for given trace type."""

    if trace_type == "throttle":
        return """#!/usr/bin/env bpftrace

BEGIN {
  printf("Tracing CPU throttle events... Hit Ctrl-C to stop.\\n");
  printf("%-12s %-16s %-8s %-12s\\n", "TIME(ms)", "COMM", "PID", "THROTTLED(us)");
}

tracepoint:sched:sched_cfs_quota_throttled {
  printf("%-12d %-16s %-8d %-12d\\n",
    elapsed / 1000000,
    args->comm,
    args->pid,
    args->throttled_usec
  );
  @throttle_events++;
  @total_throttled_us = @total_throttled_us + args->throttled_usec;
}

END {
  printf("\\nSummary:\\n");
  printf("  Total throttle events: %d\\n", @throttle_events);
  printf("  Total throttled time: %d us (%.2f ms)\\n",
    @total_throttled_us, @total_throttled_us / 1000.0);
}
"""

    elif trace_type == "runtime":
        return """#!/usr/bin/env bpftrace

BEGIN {
  printf("Tracing scheduler runtime... Hit Ctrl-C to stop.\\n");
}

tracepoint:sched:sched_stat_runtime {
  @runtime_us[args->comm] = hist(args->runtime / 1000);
  @total_runtime_us = @total_runtime_us + args->runtime;
}

END {
  printf("\\nRuntime distribution by process:\\n");
  print(@runtime_us);
  printf("\\nTotal runtime: %d us (%.2f ms)\\n",
    @total_runtime_us, @total_runtime_us / 1000.0);
}
"""

    else:
        raise ValueError(f"Unknown trace type: {trace_type}")


def generate_node_commands(
    pod_name: str, pod_info: Dict[str, str], duration: int = 30
) -> List[str]:
    """Generate shell commands to run on the Kubernetes node."""

    uid = pod_info["uid"]
    qos = pod_info["qos"]

    commands = [
        "# === BPF Trace Collection on Kubernetes Node ===",
        f"# Pod: {pod_name}",
        f"# Node: {pod_info['node']}",
        f"# UID: {uid}",
        f"# QoS: {qos}",
        "",
        "# 1. Install bpftrace if needed",
        "sudo dnf install -y bpftrace",
        "",
        "# 2. Find container PID",
        f'CGROUP_PATH="/sys/fs/cgroup/kubepods.slice/kubepods-{qos}.slice/kubepods-{qos}-pod{uid}.slice"',
        "CONTAINER_PID=$(cat ${CGROUP_PATH}/cgroup.procs | head -1)",
        'echo "Container PID: $CONTAINER_PID"',
        "ps -p $CONTAINER_PID -o pid,comm,cmd",
        "",
        "# 3. Run throttle trace",
        f"sudo timeout {duration}s bpftrace -e '",
        "tracepoint:sched:sched_cfs_quota_throttled {",
        '  printf("%s[%d] throttled for %d us\\n",',
        "    args->comm, args->pid, args->throttled_usec);",
        "  @throttle_events++;",
        "  @total_throttled_us += args->throttled_usec;",
        "}",
        "END {",
        '  printf("\\nTotal throttle events: %d\\n", @throttle_events);',
        '  printf("Total throttled time: %d us\\n", @total_throttled_us);',
        "}",
        f"' > /tmp/bpf-trace-{pod_name}.txt",
        "",
        "# 4. Monitor cgroup stats (run in separate terminal)",
        "while true; do",
        '  echo "$(date +%s) $(cat ${CGROUP_PATH}/cpu.stat)"',
        "  sleep 1",
        f"done > /tmp/cgroup-stats-{pod_name}.txt",
    ]

    return commands


def print_collection_guide(pod_name: str, workload_file: Path):
    """Print interactive guide for BPF trace collection."""

    print(f"\n{'=' * 70}")
    print(f"BPF Trace Collection Guide: {pod_name}")
    print(f"{'=' * 70}\n")

    # Step 1: Deploy workload
    print("Step 1: Deploy workload")
    print(f"  kubectl apply -f {workload_file}")
    print()

    # Wait for pod to be ready
    print("Step 2: Wait for pod to start")
    print(
        f"  kubectl wait --for=condition=ready pod/{pod_name} -n {NAMESPACE} --timeout=30s"
    )
    print()

    try:
        # Get pod info
        print("Step 3: Get pod information")
        pod_info = get_pod_info(pod_name)
        print(f"  Pod UID: {pod_info['uid']}")
        print(f"  Node: {pod_info['node']}")
        print(f"  Node IP: {pod_info['node_ip']}")
        print(f"  QoS: {pod_info['qos']}")
        print()

        # Generate and display node commands
        print("Step 4: SSH into node and run BPF trace")
        print(f"  ssh fedora@{pod_info['node_ip']}")
        print()
        print("  Then run these commands on the node:")
        print()

        commands = generate_node_commands(pod_name, pod_info)
        for cmd in commands:
            print(f"  {cmd}")

        print()
        print("Step 5: After trace completes, copy results back")
        print(
            f"  scp fedora@{pod_info['node_ip']}:/tmp/bpf-trace-{pod_name}.txt {EVIDENCE_DIR}/"
        )
        print(
            f"  scp fedora@{pod_info['node_ip']}:/tmp/cgroup-stats-{pod_name}.txt {EVIDENCE_DIR}/"
        )
        print()

        # Save commands to file
        script_file = EVIDENCE_DIR / f"collect-{pod_name}.sh"
        script_file.parent.mkdir(parents=True, exist_ok=True)
        with open(script_file, "w") as f:
            f.write("#!/bin/bash\\n")
            f.write(f"# BPF trace collection for {pod_name}\\n")
            f.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\\n\\n")
            f.write("\\n".join(commands))

        print(f"Commands saved to: {script_file}")
        print()

    except subprocess.CalledProcessError as e:
        print(f"Error: Could not get pod info. Is the pod running?")
        print(f"  {e}")
        return


def main():
    parser = argparse.ArgumentParser(
        description="Orchestrate BPF trace collection on Kubernetes nodes"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Generate commands without deploying"
    )
    parser.add_argument(
        "--analyze", action="store_true", help="Analyze existing trace files"
    )
    parser.add_argument("--pod", type=str, help="Specific pod name to trace")
    parser.add_argument(
        "--workload", type=Path, help="Specific workload file to deploy"
    )

    args = parser.parse_args()

    if args.analyze:
        print("Trace analysis not implemented yet")
        return

    # Use specified workload or default to cpu-bound-burstable
    if args.workload:
        workload_file = args.workload
        pod_name = args.pod or workload_file.stem
    else:
        workload_file = WORKLOAD_DIR / "cpu-bound-burstable.yaml"
        pod_name = "cpu-bound-burstable"

    if not workload_file.exists():
        print(f"Error: Workload file not found: {workload_file}")
        sys.exit(1)

    print_collection_guide(pod_name, workload_file)


if __name__ == "__main__":
    main()
