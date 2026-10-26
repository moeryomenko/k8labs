#!/usr/bin/env python3
"""
Phase 4: Extended experiments with intentional CPU throttling.

This script creates aggressive CPU limits to intentionally trigger throttling,
demonstrating the impact on application performance.
"""

import subprocess
import json
import time
import re
from pathlib import Path
from datetime import datetime

WORKLOADS_DIR = Path("research/cpu-limits/phase4/workloads")
EVIDENCE_DIR = Path("research/cpu-limits/phase4/evidence")
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

# Throttling test configurations
THROTTLING_TESTS = [
    {
        "name": "cpu-bound-heavy-throttle",
        "type": "cpu-bound",
        "qos": "burstable",
        "request": "100m",
        "limit": "500m",
        "expected": "Heavy throttling - 4 workers, only 0.5 CPU",
        "workers": 4,
        "duration": 120,
    },
    {
        "name": "cpu-bound-moderate-throttle",
        "type": "cpu-bound",
        "qos": "burstable",
        "request": "500m",
        "limit": "1500m",
        "expected": "Moderate throttling - 4 workers, 1.5 CPU",
        "workers": 4,
        "duration": 120,
    },
    {
        "name": "cpu-bound-light-throttle",
        "type": "cpu-bound",
        "qos": "burstable",
        "request": "1000m",
        "limit": "3000m",
        "expected": "Light throttling - 4 workers, 3 CPU",
        "workers": 4,
        "duration": 120,
    },
    {
        "name": "cpu-bound-no-throttle",
        "type": "cpu-bound",
        "qos": "burstable",
        "request": "2000m",
        "limit": "4000m",
        "expected": "No throttling - 4 workers, 4 CPU",
        "workers": 4,
        "duration": 120,
    },
]


def generate_workload_manifest(test_config):
    """Generate Pod manifest with specific CPU limits designed to throttle."""

    manifest = f"""apiVersion: v1
kind: Pod
metadata:
  name: {test_config["name"]}
  labels:
    workload-type: {test_config["type"]}
    qos-class: {test_config["qos"]}
    experiment: phase4-throttling
spec:
  restartPolicy: Never
  containers:
  - name: workload
    image: polinux/stress-ng:latest
    command: ["stress-ng"]
    args:
      - "--cpu"
      - "{test_config["workers"]}"
      - "--cpu-method"
      - "ackermann"
      - "--timeout"
      - "{test_config["duration"]}s"
      - "--metrics-brief"
      - "--verbose"
    resources:
      requests:
        cpu: "{test_config["request"]}"
        memory: "128Mi"
      limits:
        cpu: "{test_config["limit"]}"
        memory: "256Mi"
"""

    return manifest


def get_cgroup_stats(pod_name, container_name="workload"):
    """Get cgroup CPU statistics from a running pod."""
    cmd = ["kubectl", "exec", pod_name, "--", "sh", "-c", "cat /sys/fs/cgroup/cpu.stat"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None

        stats = {}
        for line in result.stdout.strip().split("\n"):
            if " " in line:
                key, value = line.split(" ", 1)
                stats[key] = int(value)

        return stats
    except Exception as e:
        print(f"  Error getting cgroup stats: {e}")
        return None


def get_node_cpu_stats():
    """Get node CPU statistics via kubectl exec on any pod."""
    # Try to find a running pod
    cmd = ["kubectl", "get", "pods", "-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return None

    pods = json.loads(result.stdout)
    for pod in pods.get("items", []):
        pod_name = pod["metadata"]["name"]
        if pod["status"]["phase"] == "Running":
            cmd = [
                "kubectl",
                "exec",
                pod_name,
                "--",
                "sh",
                "-c",
                "cat /proc/stat | head -1",
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    parts = result.stdout.strip().split()
                    if parts[0] == "cpu":
                        return {
                            "user": int(parts[1]),
                            "nice": int(parts[2]),
                            "system": int(parts[3]),
                            "idle": int(parts[4]),
                            "iowait": int(parts[5]),
                            "irq": int(parts[6]),
                            "softirq": int(parts[7]),
                        }
            except:
                continue

    return None


def wait_for_pod(pod_name, timeout=60):
    """Wait for pod to reach Running state."""
    print(f"  Waiting for pod {pod_name} to start...")

    start_time = time.time()
    while time.time() - start_time < timeout:
        cmd = ["kubectl", "get", "pod", pod_name, "-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            pod = json.loads(result.stdout)
            phase = pod["status"]["phase"]

            if phase == "Running":
                print(f"  Pod {pod_name} is running")
                return True
            elif phase == "Succeeded":
                print(f"  Pod {pod_name} already completed")
                return True
            elif phase == "Failed":
                print(f"  Pod {pod_name} failed")
                return False

        time.sleep(2)

    print(f"  Timeout waiting for pod {pod_name}")
    return False


def wait_for_completion(pod_name, check_interval=5):
    """Wait for pod to complete and return duration + exit code."""
    print("Waiting for workload completion...")

    start_time = time.time()
    last_report = start_time

    while True:
        cmd = ["kubectl", "get", "pod", pod_name, "-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            pod = json.loads(result.stdout)
            phase = pod["status"]["phase"]

            if phase == "Succeeded" or phase == "Failed":
                duration = time.time() - start_time

                # Get exit code
                exit_code = 0
                container_statuses = pod["status"].get("containerStatuses", [])
                if container_statuses:
                    terminated = (
                        container_statuses[0].get("state", {}).get("terminated", {})
                    )
                    exit_code = terminated.get("exitCode", 0)

                print(
                    f"Workload completed with exit code {exit_code} in {duration:.1f}s"
                )
                return duration, exit_code

        # Report progress every 30 seconds
        now = time.time()
        if now - last_report >= 30:
            elapsed = now - start_time
            print(f"  Still running... ({elapsed:.0f}s elapsed)")
            last_report = now

        time.sleep(check_interval)


def get_pod_logs(pod_name):
    """Get pod logs."""
    cmd = ["kubectl", "logs", pod_name]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def parse_stress_ng(logs):
    """Extract performance metrics from stress-ng logs."""
    if not logs:
        return None

    # Look for: cpu  N  real  usr  sys  bogo/s  bogo/s(tot)
    match = re.search(
        r"cpu\s+(\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)",
        logs,
    )
    if match:
        return {
            "bogo_ops": int(match.group(1)),
            "real_time": float(match.group(2)),
            "user_time": float(match.group(3)),
            "system_time": float(match.group(4)),
            "bogo_ops_per_sec_real": float(match.group(5)),
            "bogo_ops_per_sec_total": float(match.group(6)),
        }
    return None


def run_experiment(test_config):
    """Run a single throttling experiment."""
    exp_id = f"{test_config['name']}-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"

    print(f"\n{'=' * 60}")
    print(f"Experiment: {exp_id}")
    print(f"Expected: {test_config['expected']}")
    print(f"{'=' * 60}")

    # Generate manifest
    manifest = generate_workload_manifest(test_config)
    manifest_path = WORKLOADS_DIR / f"{test_config['name']}.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w") as f:
        f.write(manifest)

    # Apply workload
    print(f"Applying workload: {manifest_path.name}")
    subprocess.run(["kubectl", "apply", "-f", str(manifest_path)], check=True)

    # Wait for pod to start
    if not wait_for_pod(test_config["name"]):
        print(f"❌ Pod failed to start")
        return None

    print(f"  Pod: {test_config['name']}")
    print(f"  Type: {test_config['type']}, QoS: {test_config['qos']}")
    print(
        f"  Resources: request={test_config['request']}, limit={test_config['limit']}"
    )

    # Baseline measurements
    time.sleep(5)  # Let pod stabilize
    print("Collecting baseline...")
    baseline_cgroup = get_cgroup_stats(test_config["name"])
    baseline_node = get_node_cpu_stats()

    # Wait for completion
    duration, exit_code = wait_for_completion(test_config["name"])

    # Final measurements
    print("Collecting final measurements...")
    final_cgroup = get_cgroup_stats(test_config["name"])
    final_node = get_node_cpu_stats()

    # Get logs
    workload_logs = get_pod_logs(test_config["name"])

    # Calculate throttling metrics
    throttling_info = None
    if baseline_cgroup and final_cgroup:
        nr_throttled = final_cgroup.get("nr_throttled", 0) - baseline_cgroup.get(
            "nr_throttled", 0
        )
        throttled_usec = final_cgroup.get("throttled_usec", 0) - baseline_cgroup.get(
            "throttled_usec", 0
        )
        usage_usec = final_cgroup.get("usage_usec", 0) - baseline_cgroup.get(
            "usage_usec", 0
        )

        throttling_pct = (throttled_usec / usage_usec * 100) if usage_usec > 0 else 0

        throttling_info = {
            "nr_throttled": nr_throttled,
            "throttled_usec": throttled_usec,
            "throttled_seconds": throttled_usec / 1_000_000,
            "throttling_percentage": throttling_pct,
            "usage_usec": usage_usec,
        }

        print(f"\n📊 Throttling Analysis:")
        print(f"  Throttled: {nr_throttled} times")
        print(
            f"  Throttled duration: {throttled_usec / 1_000_000:.2f}s ({throttling_pct:.1f}%)"
        )
        print(f"  CPU usage: {usage_usec / 1_000_000:.2f}s")

    # Parse stress-ng performance
    perf_data = parse_stress_ng(workload_logs)
    if perf_data:
        print(f"  Performance: {perf_data['bogo_ops_per_sec_real']:.1f} ops/sec")

    # Build evidence
    evidence = {
        "experiment_id": exp_id,
        "test_config": test_config,
        "workload_type": test_config["type"],
        "qos_class": test_config["qos"],
        "duration_seconds": duration,
        "baseline_cgroup": baseline_cgroup,
        "final_cgroup": final_cgroup,
        "baseline_node_cpu": baseline_node,
        "final_node_cpu": final_node,
        "throttling": throttling_info,
        "performance": perf_data,
        "workload_logs": workload_logs,
        "workload_exit_code": exit_code,
    }

    # Save evidence
    evidence_file = EVIDENCE_DIR / f"{exp_id}.json"
    with open(evidence_file, "w") as f:
        json.dump(evidence, f, indent=2)

    print(f"✓ Evidence written to {evidence_file}")

    # Cleanup
    print(f"Cleaning up pod {test_config['name']}...")
    subprocess.run(
        ["kubectl", "delete", "pod", test_config["name"], "--wait=false"],
        capture_output=True,
    )

    return evidence


def main():
    print("Phase 4: CPU Throttling Experiments")
    print(f"Found {len(THROTTLING_TESTS)} throttling test configurations\n")

    results = []
    for i, test_config in enumerate(THROTTLING_TESTS, 1):
        print(f"\n[{i}/{len(THROTTLING_TESTS)}] Running: {test_config['name']}")

        evidence = run_experiment(test_config)
        if evidence:
            results.append(evidence)

        if i < len(THROTTLING_TESTS):
            print("\nWaiting 10s before next experiment...")
            time.sleep(10)

    # Summary
    print(f"\n{'=' * 60}")
    print(f"Completed {len(results)}/{len(THROTTLING_TESTS)} experiments")
    print(f"Evidence directory: {EVIDENCE_DIR}")
    print(f"{'=' * 60}")

    # Generate summary
    summary = {
        "phase": "phase4-throttling",
        "completed_at": datetime.utcnow().isoformat() + "Z",
        "total_experiments": len(results),
        "experiments": [],
    }

    for evidence in results:
        summary["experiments"].append(
            {
                "experiment_id": evidence["experiment_id"],
                "config": evidence["test_config"]["name"],
                "request": evidence["test_config"]["request"],
                "limit": evidence["test_config"]["limit"],
                "throttling_pct": evidence["throttling"]["throttling_percentage"]
                if evidence.get("throttling")
                else 0,
                "performance": evidence["performance"]["bogo_ops_per_sec_real"]
                if evidence.get("performance")
                else 0,
            }
        )

    summary_file = EVIDENCE_DIR / "throttling-summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nSummary: {summary_file}")

    # Print throttling comparison
    print(f"\n📊 Throttling Comparison:")
    print(
        f"{'Config':<30} {'Request':<10} {'Limit':<10} {'Throttling %':<15} {'Performance'}"
    )
    print("-" * 80)
    for exp in summary["experiments"]:
        print(
            f"{exp['config']:<30} {exp['request']:<10} {exp['limit']:<10} "
            f"{exp['throttling_pct']:>13.1f}%  {exp['performance']:>10.1f} ops/s"
        )


if __name__ == "__main__":
    main()
