#!/usr/bin/env python3
"""
Analyze CPU throttling data from experiments.

Generates:
1. Throttling time series plots for each experiment
2. Summary statistics comparing QoS classes
3. Analysis of throttling patterns across workload types
"""

import json
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import sys


@dataclass
class ThrottlingSample:
    """Single cgroup throttling measurement."""

    timestamp: str
    nr_periods: int
    nr_throttled: int
    throttled_usec: int
    cpu_weight: Optional[int]
    cpu_max: Optional[str]

    @property
    def throttled_ms(self) -> float:
        """Throttled time in milliseconds."""
        return self.throttled_usec / 1000

    @property
    def throttle_ratio(self) -> float:
        """Fraction of periods that were throttled."""
        return self.nr_throttled / self.nr_periods if self.nr_periods > 0 else 0

    @property
    def avg_throttle_ms(self) -> float:
        """Average throttle duration per event in milliseconds."""
        return self.throttled_ms / self.nr_throttled if self.nr_throttled > 0 else 0


def parse_cpu_stat(cpu_stat_str: str) -> dict:
    """Parse cpu.stat cgroup file content."""
    if not cpu_stat_str:
        return {}

    result = {}
    for line in cpu_stat_str.split("\\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 2:
            key, value = parts
            result[key] = int(value)
    return result


def extract_throttling_samples(evidence_file: Path) -> list[ThrottlingSample]:
    """Extract throttling time series from experiment evidence."""
    with open(evidence_file) as f:
        data = json.load(f)

    samples = []
    for sample_data in data.get("cgroup_samples", []):
        cpu_stat = parse_cpu_stat(sample_data.get("cpu_stat", ""))
        if not cpu_stat:
            continue

        samples.append(
            ThrottlingSample(
                timestamp=sample_data["timestamp"],
                nr_periods=cpu_stat.get("nr_periods", 0),
                nr_throttled=cpu_stat.get("nr_throttled", 0),
                throttled_usec=cpu_stat.get("throttled_usec", 0),
                cpu_weight=sample_data.get("cpu_weight"),
                cpu_max=sample_data.get("cpu_max"),
            )
        )

    return samples


def analyze_experiment(evidence_file: Path):
    """Print analysis of a single experiment."""
    with open(evidence_file) as f:
        data = json.load(f)

    workload = data["workload_name"]
    workload_type = data["workload_type"]
    qos_class = data["qos_class"]
    duration = data["duration_seconds"]

    samples = extract_throttling_samples(evidence_file)

    print(f"\n{'=' * 70}")
    print(f"Experiment: {workload}")
    print(f"Type: {workload_type}, QoS: {qos_class}, Duration: {duration:.1f}s")
    print(f"{'=' * 70}")

    if not samples:
        print("⚠ No cgroup samples collected")
        return

    print(f"\nCollected {len(samples)} samples over {duration:.1f}s")

    # First and last sample comparison
    first, last = samples[0], samples[-1]
    total_throttled = last.nr_throttled - first.nr_throttled
    total_throttled_ms = last.throttled_ms - first.throttled_ms
    avg_throttle_ms = total_throttled_ms / total_throttled if total_throttled > 0 else 0

    print(f"\nThrottling Summary:")
    print(f"  Total throttle events: {total_throttled}")
    print(
        f"  Total throttled time: {total_throttled_ms:.1f}ms ({total_throttled_ms / 1000:.2f}s)"
    )
    print(f"  Average per event: {avg_throttle_ms:.2f}ms")
    print(f"  Throttle ratio: {last.throttle_ratio:.1%} of periods")

    if first.cpu_weight:
        print(f"\nCgroup Configuration:")
        print(f"  cpu.weight: {first.cpu_weight}")
        print(f"  cpu.max: {first.cpu_max}")

    # Time series evolution
    print(f"\nThrottling Evolution:")
    print(f"  {'Time':<12} {'Events':<10} {'Total (ms)':<12} {'Rate':<10}")
    print(f"  {'-' * 50}")

    for i, sample in enumerate(samples):
        if i == 0:
            continue  # Skip first sample (baseline)

        prev = samples[i - 1]
        delta_events = sample.nr_throttled - prev.nr_throttled
        delta_ms = sample.throttled_ms - prev.throttled_ms
        ts = sample.timestamp.split("T")[1][:8]

        print(
            f"  {ts:<12} {delta_events:<10} {delta_ms:<12.1f} {delta_events / 10:.1f}/s"
        )


def compare_qos_classes(evidence_dir: Path):
    """Compare throttling across QoS classes for same workload type."""
    experiments_by_type = {}

    for evidence_file in evidence_dir.glob("*.json"):
        if evidence_file.name == "summary.json":
            continue

        with open(evidence_file) as f:
            data = json.load(f)

        workload_type = data["workload_type"]
        qos_class = data["qos_class"]

        if workload_type not in experiments_by_type:
            experiments_by_type[workload_type] = {}

        samples = extract_throttling_samples(evidence_file)
        if samples:
            first, last = samples[0], samples[-1]
            total_throttled = last.nr_throttled - first.nr_throttled
            total_throttled_ms = last.throttled_ms - first.throttled_ms

            experiments_by_type[workload_type][qos_class] = {
                "duration": data["duration_seconds"],
                "samples": len(samples),
                "throttle_events": total_throttled,
                "throttled_ms": total_throttled_ms,
                "throttle_ratio": last.throttle_ratio,
            }

    print(f"\n{'=' * 70}")
    print("QoS Class Comparison")
    print(f"{'=' * 70}")

    for workload_type, qos_data in sorted(experiments_by_type.items()):
        print(f"\n{workload_type.upper()}:")
        print(f"  {'QoS':<15} {'Events':<12} {'Time (s)':<12} {'Ratio':<10}")
        print(f"  {'-' * 55}")

        for qos_class in ["besteffort", "burstable", "guaranteed"]:
            if qos_class in qos_data:
                stats = qos_data[qos_class]
                print(
                    f"  {qos_class:<15} "
                    f"{stats['throttle_events']:<12} "
                    f"{stats['throttled_ms'] / 1000:<12.2f} "
                    f"{stats['throttle_ratio']:<10.1%}"
                )


def main():
    """Main entry point."""
    evidence_dir = Path("research/cpu-limits/phase2/evidence")

    if not evidence_dir.exists():
        print(f"Evidence directory not found: {evidence_dir}")
        sys.exit(1)

    # Analyze each experiment
    for evidence_file in sorted(evidence_dir.glob("*.json")):
        if evidence_file.name == "summary.json":
            continue
        analyze_experiment(evidence_file)

    # Compare QoS classes
    compare_qos_classes(evidence_dir)


if __name__ == "__main__":
    main()
