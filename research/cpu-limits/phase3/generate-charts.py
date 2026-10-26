#!/usr/bin/env python3
"""Generate visualization charts from experiment results."""

import json
import re
from pathlib import Path
from typing import Any

try:
    import matplotlib.pyplot as plt
    import matplotlib

    matplotlib.use("Agg")  # Non-interactive backend
except ImportError:
    print("matplotlib not installed - install with: pip install matplotlib")
    exit(1)


def extract_stress_ng_stats(logs: str) -> dict[str, Any]:
    """Extract metrics from stress-ng logs."""
    stats = {}

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


def load_experiments(evidence_dir: Path) -> dict[str, list[dict]]:
    """Load and parse all experiment evidence files."""
    by_type: dict[str, list[dict]] = {}

    for evidence_file in sorted(evidence_dir.glob("*.json")):
        with open(evidence_file) as f:
            data = json.load(f)

        if data.get("workload_logs"):
            data["stress_ng_stats"] = extract_stress_ng_stats(data["workload_logs"])

        workload_type = data["workload_type"]
        if workload_type not in by_type:
            by_type[workload_type] = []
        by_type[workload_type].append(data)

    # Sort each type by QoS class
    qos_order = {"besteffort": 0, "burstable": 1, "guaranteed": 2}
    for experiments in by_type.values():
        experiments.sort(key=lambda x: qos_order.get(x["qos_class"], 99))

    return by_type


def generate_throughput_comparison(
    experiments: dict[str, list[dict]], output_dir: Path
):
    """Generate bar charts comparing throughput across QoS classes."""

    for workload_type, exps in experiments.items():
        qos_classes = []
        throughputs = []

        for exp in exps:
            stats = exp.get("stress_ng_stats", {})
            ops_per_s = stats.get("bogo_ops_per_s_real", 0)

            if ops_per_s > 0:
                qos_classes.append(exp["qos_class"].title())
                throughputs.append(ops_per_s)

        if not throughputs:
            continue

        # Create bar chart
        fig, ax = plt.subplots(figsize=(10, 6))
        bars = ax.bar(qos_classes, throughputs, color=["#38BDF8", "#F97316", "#4FD1C5"])

        ax.set_xlabel("QoS Class", fontsize=12, fontweight="bold")
        ax.set_ylabel("Throughput (bogo ops/s)", fontsize=12, fontweight="bold")
        ax.set_title(
            f"{workload_type.title()} Workload: Throughput by QoS Class",
            fontsize=14,
            fontweight="bold",
            pad=20,
        )
        ax.grid(axis="y", alpha=0.3, linestyle="--")

        # Add value labels on bars
        for bar in bars:
            height = bar.get_height()
            ax.text(
                bar.get_x() + bar.get_width() / 2.0,
                height,
                f"{height:.1f}",
                ha="center",
                va="bottom",
                fontweight="bold",
            )

        plt.tight_layout()
        output_file = output_dir / f"{workload_type}-throughput.png"
        plt.savefig(output_file, dpi=150, bbox_inches="tight")
        print(f"Generated: {output_file}")
        plt.close()


def generate_cpu_time_breakdown(experiments: dict[str, list[dict]], output_dir: Path):
    """Generate stacked bar charts showing user vs system CPU time."""

    for workload_type, exps in experiments.items():
        qos_classes = []
        usr_times = []
        sys_times = []

        for exp in exps:
            stats = exp.get("stress_ng_stats", {})
            usr = stats.get("usr_time_s", 0)
            sys = stats.get("sys_time_s", 0)

            if usr > 0 or sys > 0:
                qos_classes.append(exp["qos_class"].title())
                usr_times.append(usr)
                sys_times.append(sys)

        if not usr_times:
            continue

        # Create stacked bar chart
        fig, ax = plt.subplots(figsize=(10, 6))

        x = range(len(qos_classes))
        width = 0.6

        p1 = ax.bar(x, usr_times, width, label="User Time", color="#38BDF8")
        p2 = ax.bar(
            x, sys_times, width, bottom=usr_times, label="System Time", color="#F97316"
        )

        ax.set_xlabel("QoS Class", fontsize=12, fontweight="bold")
        ax.set_ylabel("CPU Time (seconds)", fontsize=12, fontweight="bold")
        ax.set_title(
            f"{workload_type.title()} Workload: CPU Time Breakdown",
            fontsize=14,
            fontweight="bold",
            pad=20,
        )
        ax.set_xticks(x)
        ax.set_xticklabels(qos_classes)
        ax.legend()
        ax.grid(axis="y", alpha=0.3, linestyle="--")

        plt.tight_layout()
        output_file = output_dir / f"{workload_type}-cpu-time.png"
        plt.savefig(output_file, dpi=150, bbox_inches="tight")
        print(f"Generated: {output_file}")
        plt.close()


def generate_throttling_chart(experiments: dict[str, list[dict]], output_dir: Path):
    """Generate chart showing throttling statistics."""

    for workload_type, exps in experiments.items():
        qos_classes = []
        throttle_pcts = []

        for exp in exps:
            cg = exp.get("final_cgroup")
            if not cg:
                continue

            nr_periods = cg.get("nr_periods", 0)
            nr_throttled = cg.get("nr_throttled", 0)

            if nr_periods > 0:
                pct = (nr_throttled / nr_periods) * 100
                qos_classes.append(exp["qos_class"].title())
                throttle_pcts.append(pct)

        if not throttle_pcts:
            continue

        # Create bar chart
        fig, ax = plt.subplots(figsize=(10, 6))
        bars = ax.bar(
            qos_classes, throttle_pcts, color=["#38BDF8", "#F97316", "#4FD1C5"]
        )

        ax.set_xlabel("QoS Class", fontsize=12, fontweight="bold")
        ax.set_ylabel("Throttled Periods (%)", fontsize=12, fontweight="bold")
        ax.set_title(
            f"{workload_type.title()} Workload: CPU Throttling",
            fontsize=14,
            fontweight="bold",
            pad=20,
        )
        ax.grid(axis="y", alpha=0.3, linestyle="--")
        ax.set_ylim(0, 100)

        # Add value labels
        for bar in bars:
            height = bar.get_height()
            ax.text(
                bar.get_x() + bar.get_width() / 2.0,
                height,
                f"{height:.1f}%",
                ha="center",
                va="bottom",
                fontweight="bold",
            )

        plt.tight_layout()
        output_file = output_dir / f"{workload_type}-throttling.png"
        plt.savefig(output_file, dpi=150, bbox_inches="tight")
        print(f"Generated: {output_file}")
        plt.close()


def main():
    evidence_dir = Path("research/cpu-limits/phase2/evidence")
    output_dir = Path("research/cpu-limits/phase3/images")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading experiment data...")
    experiments = load_experiments(evidence_dir)

    if not experiments:
        print("No experiment data found!")
        return

    print(f"Found {sum(len(e) for e in experiments.values())} experiments")

    print("\nGenerating charts...")
    generate_throughput_comparison(experiments, output_dir)
    generate_cpu_time_breakdown(experiments, output_dir)
    generate_throttling_chart(experiments, output_dir)

    print(f"\nAll charts saved to: {output_dir}")


if __name__ == "__main__":
    main()
