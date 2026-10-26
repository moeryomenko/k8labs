#!/usr/bin/env python3
"""Analyze experiment results and generate a comparison report."""

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


def extract_stress_ng_stats(logs: str) -> dict[str, Any]:
    """Extract metrics from stress-ng logs."""
    stats = {}

    # Find the summary line: "cpu  11854  120.02  453.95  0.14  98.76  26.10"
    match = re.search(
        r"cpu\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", logs
    )

    if match:
        stats["bogo_ops"] = int(match.group(1))
        stats["real_time_s"] = float(match.group(2))
        stats["usr_time_s"] = float(match.group(3))
        stats["sys_time_s"] = float(match.group(4))
        stats["bogo_ops_per_s_real"] = float(match.group(5))
        stats["bogo_ops_per_s_cpu"] = float(match.group(6))

    return stats


def calculate_cpu_delta(
    baseline: dict[str, int], final: dict[str, int]
) -> dict[str, int]:
    """Calculate CPU time deltas between baseline and final."""
    if not baseline or not final:
        return {}

    return {key: final[key] - baseline.get(key, 0) for key in final.keys()}


def analyze_evidence(evidence_dir: Path) -> None:
    """Analyze all evidence files and generate comparison."""

    # Group by workload type
    by_type: dict[str, list[dict]] = defaultdict(list)

    for evidence_file in sorted(evidence_dir.glob("*.json")):
        with open(evidence_file) as f:
            data = json.load(f)

        # Extract stress-ng stats if available
        if data.get("workload_logs"):
            data["stress_ng_stats"] = extract_stress_ng_stats(data["workload_logs"])

        # Calculate CPU deltas
        if data.get("baseline_node_cpu") and data.get("final_node_cpu"):
            data["node_cpu_delta"] = calculate_cpu_delta(
                data["baseline_node_cpu"], data["final_node_cpu"]
            )

        by_type[data["workload_type"]].append(data)

    # Print comparison
    print("=" * 80)
    print("CPU Limits Experiment Results")
    print("=" * 80)

    for workload_type, experiments in sorted(by_type.items()):
        print(f"\n{workload_type.upper()} Workload")
        print("-" * 80)

        # Sort by QoS class
        qos_order = {"besteffort": 0, "burstable": 1, "guaranteed": 2}
        experiments.sort(key=lambda x: qos_order.get(x["qos_class"], 99))

        for exp in experiments:
            qos = exp["qos_class"]
            exp_id = exp["experiment_id"]

            print(f"\n  {qos.upper()}")
            print(f"  Experiment: {exp_id}")
            print(f"  Duration: {exp.get('duration_seconds', 0):.1f}s")
            print(f"  Exit code: {exp.get('workload_exit_code', 'N/A')}")

            # stress-ng stats
            if "stress_ng_stats" in exp:
                stats = exp["stress_ng_stats"]
                print(f"  Bogo ops: {stats.get('bogo_ops', 'N/A')}")
                print(f"  Real time: {stats.get('real_time_s', 'N/A')}s")
                print(f"  User time: {stats.get('usr_time_s', 'N/A')}s")
                print(f"  System time: {stats.get('sys_time_s', 'N/A')}s")
                print(f"  Ops/s (real): {stats.get('bogo_ops_per_s_real', 'N/A'):.2f}")
                print(f"  Ops/s (cpu): {stats.get('bogo_ops_per_s_cpu', 'N/A'):.2f}")

            # Node CPU delta
            if "node_cpu_delta" in exp:
                delta = exp["node_cpu_delta"]
                total_delta = sum(delta.values())
                print(f"  Node CPU delta (ticks):")
                for key, value in delta.items():
                    pct = (value / total_delta * 100) if total_delta > 0 else 0
                    print(f"    {key:8s}: {value:8d} ({pct:5.1f}%)")

            # cgroup stats
            if exp.get("final_cgroup"):
                cg = exp["final_cgroup"]
                print(f"  CFS stats:")
                print(f"    nr_periods: {cg.get('nr_periods', 'N/A')}")
                print(f"    nr_throttled: {cg.get('nr_throttled', 'N/A')}")
                print(f"    throttled_time: {cg.get('throttled_time', 'N/A')}")

    print("\n" + "=" * 80)

    # Summary comparison
    print("\nSummary: Bogo Ops/s (Real Time) by QoS Class")
    print("-" * 80)

    for workload_type, experiments in sorted(by_type.items()):
        print(f"\n{workload_type}:")
        for exp in experiments:
            qos = exp["qos_class"]
            stats = exp.get("stress_ng_stats", {})
            ops_per_s = stats.get("bogo_ops_per_s_real", 0)
            print(f"  {qos:12s}: {ops_per_s:8.2f} ops/s")


if __name__ == "__main__":
    evidence_dir = Path("research/cpu-limits/phase2/evidence")
    analyze_evidence(evidence_dir)
