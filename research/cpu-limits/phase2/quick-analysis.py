#!/usr/bin/env python3
"""
Quick analysis of throttling data from Phase 2 experiments
"""

import json
from pathlib import Path


def parse_cpu_stat(stat_str):
    """Parse cpu.stat format into dict"""
    result = {}
    for line in stat_str.strip().split("\n"):
        parts = line.split()
        if len(parts) == 2:
            key, value = parts
            result[key] = int(value)
    return result


def main():
    evidence_dir = Path(__file__).parent / "evidence"
    summary_file = evidence_dir / "summary.json"

    with open(summary_file) as f:
        experiments = json.load(f)

    print("=" * 70)
    print("CPU LIMITS RESEARCH - PHASE 2 RESULTS")
    print("=" * 70)
    print()

    for exp in experiments:
        name = exp["workload_name"]
        qos = exp["qos_class"]
        workload_type = exp["workload_type"]
        duration = exp["duration_seconds"]

        print(f"Experiment: {name}")
        print(f"  QoS Class: {qos}")
        print(f"  Workload Type: {workload_type}")
        print(f"  Duration: {duration:.1f}s")

        # Check if we have cgroup data
        if exp.get("final_cgroup") and exp.get("baseline_cgroup"):
            baseline_stat = parse_cpu_stat(exp["baseline_cgroup"]["cpu_stat"])
            final_stat = parse_cpu_stat(exp["final_cgroup"]["cpu_stat"])

            # CPU usage
            cpu_used_us = final_stat["usage_usec"] - baseline_stat["usage_usec"]
            cpu_used_s = cpu_used_us / 1_000_000

            # Throttling
            throttled_us = final_stat.get("throttled_usec", 0) - baseline_stat.get(
                "throttled_usec", 0
            )
            throttled_s = throttled_us / 1_000_000

            # Periods
            nr_periods = final_stat.get("nr_periods", 0) - baseline_stat.get(
                "nr_periods", 0
            )
            nr_throttled = final_stat.get("nr_throttled", 0) - baseline_stat.get(
                "nr_throttled", 0
            )

            print(
                f"  CPU Used: {cpu_used_s:.1f}s ({cpu_used_s / duration:.2f}x vCPU equivalent)"
            )

            if nr_periods > 0:
                print(
                    f"  Throttled: {throttled_s:.1f}s ({throttled_s / cpu_used_s * 100:.1f}% of CPU time)"
                )
                print(
                    f"  Throttle Events: {nr_throttled}/{nr_periods} periods ({nr_throttled / nr_periods * 100:.1f}%)"
                )
            else:
                print(f"  Throttled: NO LIMIT (besteffort)")

            # CPU limit
            cpu_max = exp["final_cgroup"].get("cpu_max", "max 100000")
            if "max" not in cpu_max:
                quota, period = cpu_max.split()
                limit_cores = int(quota) / int(period)
                print(f"  CPU Limit: {limit_cores:.1f} cores")
            else:
                print(f"  CPU Limit: unlimited")

            # Samples
            samples = len(exp.get("cgroup_samples", []))
            print(f"  Samples: {samples}")
        else:
            print(f"  ⚠️  No cgroup data (race condition)")

        print()

    print("=" * 70)
    print("KEY FINDINGS:")
    print("=" * 70)
    print()

    # Find besteffort experiment
    besteffort = next((e for e in experiments if e["qos_class"] == "besteffort"), None)
    burstable = [e for e in experiments if e["qos_class"] == "burstable"]
    guaranteed = [
        e
        for e in experiments
        if e["qos_class"] == "guaranteed" and e.get("final_cgroup")
    ]

    if besteffort and besteffort.get("final_cgroup"):
        baseline_stat = parse_cpu_stat(besteffort["baseline_cgroup"]["cpu_stat"])
        final_stat = parse_cpu_stat(besteffort["final_cgroup"]["cpu_stat"])
        cpu_used_s = (
            final_stat["usage_usec"] - baseline_stat["usage_usec"]
        ) / 1_000_000
        print(
            f"1. BestEffort pods use maximum available CPU ({cpu_used_s:.1f}s over {besteffort['duration_seconds']:.1f}s)"
        )
        print(
            f"   → {cpu_used_s / besteffort['duration_seconds']:.2f}x vCPU equivalent"
        )
        print()

    if burstable:
        print("2. Burstable pods with CPU limits experience significant throttling:")
        for exp in burstable:
            if exp.get("final_cgroup"):
                baseline_stat = parse_cpu_stat(exp["baseline_cgroup"]["cpu_stat"])
                final_stat = parse_cpu_stat(exp["final_cgroup"]["cpu_stat"])
                throttled_us = final_stat.get("throttled_usec", 0) - baseline_stat.get(
                    "throttled_usec", 0
                )
                cpu_used_us = final_stat["usage_usec"] - baseline_stat["usage_usec"]
                throttle_pct = throttled_us / cpu_used_us * 100
                print(
                    f"   - {exp['workload_name']}: {throttle_pct:.1f}% of CPU time throttled"
                )
        print()

    if guaranteed:
        print("3. Guaranteed pods have predictable but constrained performance:")
        for exp in guaranteed:
            baseline_stat = parse_cpu_stat(exp["baseline_cgroup"]["cpu_stat"])
            final_stat = parse_cpu_stat(exp["final_cgroup"]["cpu_stat"])
            cpu_used_s = (
                final_stat["usage_usec"] - baseline_stat["usage_usec"]
            ) / 1_000_000
            throttled_us = final_stat.get("throttled_usec", 0) - baseline_stat.get(
                "throttled_usec", 0
            )
            throttled_s = throttled_us / 1_000_000
            cpu_max = exp["final_cgroup"].get("cpu_max", "max 100000")
            if "max" not in cpu_max:
                quota, period = cpu_max.split()
                limit_cores = int(quota) / int(period)
                print(
                    f"   - {exp['workload_name']}: {cpu_used_s:.1f}s used, {throttled_s:.1f}s throttled (limit: {limit_cores:.1f} cores)"
                )
        print()

    print("=" * 70)
    print("NEXT STEPS:")
    print("=" * 70)
    print()
    print("1. Run analyze-throttling.py for detailed throttling patterns")
    print("2. Run generate-dashboard.py for interactive visualization")
    print("3. Enable BPF tracing in Phase 3 for scheduler insights")
    print()


if __name__ == "__main__":
    main()
