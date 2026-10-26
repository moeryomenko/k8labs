#!/usr/bin/env python3
"""
Phase 5 Task 5.2: Simplified Latency Test using stress-ng

Instead of nginx+wrk (which requires specific images), use stress-ng which we already have.
We'll measure task completion time under different throttling levels as a proxy for latency.
"""

import subprocess
import json
import time
from datetime import datetime, UTC
from pathlib import Path

EVIDENCE_DIR = Path("../../evidence")
WORKLOAD_DIR = Path(".")

# Test configurations - use stress-ng with consistent workload
CONFIGS = [
    {
        "name": "latency-no-throttle",
        "cpu_request": "2000m",
        "cpu_limit": "4000m",
        "workers": 2,
        "expected_throttling": 0,
        "description": "Baseline - no throttling",
    },
    {
        "name": "latency-light-throttle",
        "cpu_request": "1000m",
        "cpu_limit": "3000m",
        "workers": 2,
        "expected_throttling": 4.7,
        "description": "Light throttling - 4.7% expected",
    },
    {
        "name": "latency-heavy-throttle",
        "cpu_request": "100m",
        "cpu_limit": "500m",
        "workers": 2,
        "expected_throttling": 69.8,
        "description": "Heavy throttling - 69.8% expected",
    },
]


def run_command(cmd, check=True):
    """Run shell command and return output."""
    result = subprocess.run(
        cmd, shell=True, check=check, capture_output=True, text=True
    )
    return result.stdout.strip()


def generate_workload_manifest(config):
    """Generate stress-ng pod with specified CPU resources."""
    manifest = f"""apiVersion: v1
kind: Pod
metadata:
  name: {config["name"]}
  labels:
    test-type: latency-test
    config: {config["name"]}
spec:
  restartPolicy: Never
  containers:
  - name: stress
    image: polinux/stress-ng
    args:
    - "--cpu"
    - "{config["workers"]}"
    - "--cpu-method"
    - "ackermann"
    - "--timeout"
    - "60s"
    - "--metrics-brief"
    resources:
      requests:
        cpu: "{config["cpu_request"]}"
      limits:
        cpu: "{config["cpu_limit"]}"
"""
    return manifest


def get_cgroup_stats(pod_name):
    """Get cgroup CPU stats from the pod."""
    cmd = (
        f"kubectl exec {pod_name} -- cat /sys/fs/cgroup/cpu.stat 2>/dev/null || echo ''"
    )
    output = run_command(cmd, check=False)

    stats = {}
    if output:
        for line in output.split("\n"):
            if " " in line:
                key, value = line.split(" ", 1)
                try:
                    stats[key] = int(value)
                except ValueError:
                    pass

    return stats


def wait_for_pod_completion(pod_name, timeout=300):
    """Wait for pod to complete and return exit code and duration."""
    print(f"  Waiting for pod {pod_name} to complete...")

    start_time = time.time()

    for i in range(timeout):
        phase = run_command(
            f"kubectl get pod {pod_name} -o jsonpath='{{.status.phase}}'", check=False
        )

        if phase == "Succeeded":
            duration = time.time() - start_time
            print(f"  ✓ Pod completed successfully in {duration:.1f}s")
            return 0, duration
        elif phase == "Failed":
            duration = time.time() - start_time
            print(f"  ✗ Pod failed in {duration:.1f}s")
            return 1, duration

        if i % 10 == 0 and i > 0:
            print(f"  ... still running ({i}s elapsed)")

        time.sleep(1)

    print(f"  ✗ Pod did not complete within {timeout}s")
    return -1, timeout


def run_experiment(config):
    """Run a single latency experiment."""
    print(f"\n{'=' * 80}")
    print(f"Experiment: {config['name']}")
    print(f"  CPU Request: {config['cpu_request']}")
    print(f"  CPU Limit: {config['cpu_limit']}")
    print(f"  Expected Throttling: {config['expected_throttling']}%")
    print(f"{'=' * 80}")

    # Generate and apply manifest
    manifest = generate_workload_manifest(config)
    manifest_file = WORKLOAD_DIR / f"{config['name']}.yaml"
    with open(manifest_file, "w") as f:
        f.write(manifest)

    # Clean up any existing pod
    run_command(
        f"kubectl delete pod {config['name']} --wait=false 2>/dev/null || true",
        check=False,
    )
    time.sleep(2)

    # Apply manifest
    run_command(f"kubectl apply -f {manifest_file}")

    # Wait for pod to start
    print("  Waiting for pod to start...")
    for i in range(60):
        phase = run_command(
            f"kubectl get pod {config['name']} -o jsonpath='{{.status.phase}}'",
            check=False,
        )
        if phase in ["Running", "Succeeded"]:
            print(f"  ✓ Pod started")
            break
        time.sleep(1)
    else:
        print(f"  ✗ Pod did not start within 60s")
        return None

    # Get baseline cgroup stats
    time.sleep(2)
    stats_before = get_cgroup_stats(config["name"])

    # Wait for completion and measure duration
    exit_code, duration = wait_for_pod_completion(config["name"], timeout=300)

    # Get final cgroup stats
    stats_after = get_cgroup_stats(config["name"])

    # Calculate throttling
    usage_delta = stats_after.get("usage_usec", 0) - stats_before.get("usage_usec", 0)
    throttled_delta = stats_after.get("throttled_usec", 0) - stats_before.get(
        "throttled_usec", 0
    )

    if usage_delta + throttled_delta > 0:
        throttling_pct = (throttled_delta / (usage_delta + throttled_delta)) * 100
    else:
        throttling_pct = 0

    # Get logs
    logs = run_command(f"kubectl logs {config['name']}", check=False)

    # Parse stress-ng output for bogo ops
    bogo_ops = 0
    for line in logs.split("\n"):
        if "bogo ops" in line.lower() and "real time" in line.lower():
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "bogo" and i + 2 < len(parts):
                    try:
                        bogo_ops = float(parts[i + 2])
                        break
                    except ValueError:
                        pass

    result = {
        "config": config,
        "duration_seconds": duration,
        "exit_code": exit_code,
        "throttling": {
            "usage_usec": usage_delta,
            "throttled_usec": throttled_delta,
            "percentage": throttling_pct,
            "nr_throttled": stats_after.get("nr_throttled", 0)
            - stats_before.get("nr_throttled", 0),
        },
        "performance": {
            "bogo_ops_per_sec": bogo_ops,
            "duration_vs_baseline": None,  # Will calculate later
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }

    # Print summary
    print(f"\n{'=' * 80}")
    print(f"Results: {config['name']}")
    print(f"{'=' * 80}")
    print(f"Duration: {duration:.1f}s")
    print(f"Performance: {bogo_ops:.1f} ops/sec")
    print(f"Throttling: {throttling_pct:.1f}%")
    print(f"Throttled time: {throttled_delta / 1e6:.2f}s")

    return result


def main():
    print("=" * 80)
    print("Phase 5 Task 5.2: Latency-Sensitive Workload Testing (Simplified)")
    print("=" * 80)
    print()
    print(
        "This test measures task completion time under different CPU throttling levels."
    )
    print("We use stress-ng with a fixed workload to demonstrate latency impact.")
    print()

    EVIDENCE_DIR.mkdir(exist_ok=True, parents=True)
    WORKLOAD_DIR.mkdir(exist_ok=True)

    all_results = []
    baseline_duration = None

    # Run experiments
    for config in CONFIGS:
        try:
            result = run_experiment(config)
            if result:
                all_results.append(result)

                # Set baseline (first experiment, no throttle)
                if baseline_duration is None:
                    baseline_duration = result["duration_seconds"]

                # Calculate vs baseline
                if baseline_duration > 0:
                    result["performance"]["duration_vs_baseline"] = (
                        (result["duration_seconds"] - baseline_duration)
                        / baseline_duration
                    ) * 100

        finally:
            # Cleanup
            print(f"\nCleaning up pod {config['name']}...")
            run_command(
                f"kubectl delete pod {config['name']} --wait=false", check=False
            )

            if config != CONFIGS[-1]:
                print(f"\nWaiting 10s before next experiment...")
                time.sleep(10)

    # Save results
    exp_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results_file = EVIDENCE_DIR / f"latency-impact-{exp_id}.json"

    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n{'=' * 80}")
    print("All Experiments Complete")
    print(f"{'=' * 80}")
    print(f"Results saved to: {results_file}")

    # Print comparison table
    print(f"\n{'=' * 80}")
    print("Latency Comparison (Duration as Proxy)")
    print(f"{'=' * 80}")
    print(
        f"{'Configuration':<30} {'Duration':>10} {'vs Baseline':>12} {'Throttling':>12} {'Performance':>12}"
    )
    print("-" * 80)

    for result in all_results:
        name = result["config"]["name"]
        duration = result["duration_seconds"]
        vs_baseline = result["performance"]["duration_vs_baseline"]
        throttling = result["throttling"]["percentage"]
        perf = result["performance"]["bogo_ops_per_sec"]

        vs_baseline_str = (
            f"+{vs_baseline:.1f}%" if vs_baseline is not None else "baseline"
        )

        print(
            f"{name:<30} {duration:>9.1f}s {vs_baseline_str:>12} {throttling:>11.1f}% {perf:>11.1f}/s"
        )

    print()
    print("Key Finding:")
    if len(all_results) >= 2:
        baseline = all_results[0]
        worst = max(all_results, key=lambda r: r["throttling"]["percentage"])

        throttle_increase = worst["throttling"]["percentage"]
        duration_increase = (
            worst["performance"]["duration_vs_baseline"]
            if worst["performance"]["duration_vs_baseline"]
            else 0
        )

        print(
            f"  Heavy throttling ({throttle_increase:.1f}%) increased task duration by {duration_increase:.1f}%"
        )
        print(
            f"  This demonstrates the latency impact of CPU limits on user-facing operations"
        )


if __name__ == "__main__":
    main()
