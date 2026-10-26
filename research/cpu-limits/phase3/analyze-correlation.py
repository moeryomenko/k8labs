#!/usr/bin/env python3
"""
Correlate Phase 2 metrics with BPF traces to analyze CPU throttling impact.

This script:
1. Loads Phase 2 evidence (cgroup metrics, Prometheus data)
2. Loads BPF traces (if available)
3. Correlates throttle events with CPU usage patterns
4. Generates analysis visualizations
"""

import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional
import matplotlib

matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime


@dataclass
class CorrelationAnalysis:
    experiment_id: str
    workload_type: str
    qos_class: str

    # Phase 2 metrics
    cpu_usage_avg: float
    throttled_periods_rate: float
    throttled_time_pct: float
    completion_time_s: float

    # BPF metrics (if available)
    bpf_throttle_events: Optional[int] = None
    bpf_avg_throttle_duration_us: Optional[float] = None
    bpf_avg_sched_latency_us: Optional[float] = None
    bpf_involuntary_switches: Optional[int] = None

    # Correlation insights
    throttle_impact: str = ""
    performance_verdict: str = ""


def load_phase2_evidence(evidence_dir: Path) -> List[Dict]:
    """Load all Phase 2 experiment evidence files."""
    evidence_files = sorted(evidence_dir.glob("*.json"))
    evidence_files = [f for f in evidence_files if f.name != "summary.json"]

    results = []
    for filepath in evidence_files:
        with open(filepath) as f:
            results.append(json.load(f))

    return results


def load_bpf_traces(bpf_dir: Path, experiment_id: str) -> Optional[Dict]:
    """Load BPF trace data if available."""
    bpf_file = bpf_dir / f"{experiment_id}-bpf.json"
    if not bpf_file.exists():
        return None

    with open(bpf_file) as f:
        return json.load(f)


def analyze_throttle_impact(evidence: Dict, bpf: Optional[Dict]) -> CorrelationAnalysis:
    """Analyze throttling impact on workload performance."""

    # Extract Phase 2 metrics
    final_cgroup = evidence["cgroup_metrics"]["final"]
    cpu_usage = final_cgroup["cpu.stat"]["usage_usec"] / 1_000_000  # Convert to seconds

    nr_throttled = final_cgroup["cpu.stat"]["nr_throttled"]
    throttled_usec = final_cgroup["cpu.stat"]["throttled_usec"]

    completion_time = evidence["completion_time_s"]

    # Calculate rates
    throttled_periods_rate = nr_throttled / max(completion_time, 1)
    throttled_time_pct = (
        (throttled_usec / (cpu_usage * 1_000_000)) * 100 if cpu_usage > 0 else 0.0
    )

    analysis = CorrelationAnalysis(
        experiment_id=evidence["experiment_id"],
        workload_type=evidence["workload_type"],
        qos_class=evidence["qos_class"],
        cpu_usage_avg=cpu_usage / max(completion_time, 1),
        throttled_periods_rate=throttled_periods_rate,
        throttled_time_pct=throttled_time_pct,
        completion_time_s=completion_time,
    )

    # Add BPF metrics if available
    if bpf and "throttle" in bpf and bpf["throttle"]:
        analysis.bpf_throttle_events = bpf["throttle"]["total_events"]
        analysis.bpf_avg_throttle_duration_us = bpf["throttle"]["avg_duration_us"]

    if bpf and "latency" in bpf and bpf["latency"]:
        analysis.bpf_avg_sched_latency_us = bpf["latency"]["avg_latency_us"]

    if bpf and "context_switches" in bpf and bpf["context_switches"]:
        analysis.bpf_involuntary_switches = bpf["context_switches"]["involuntary"]

    # Generate insights
    if throttled_time_pct > 20:
        analysis.throttle_impact = "HIGH - Significant throttling detected"
    elif throttled_time_pct > 5:
        analysis.throttle_impact = "MODERATE - Some throttling"
    else:
        analysis.throttle_impact = "LOW - Minimal throttling"

    # Performance verdict
    if completion_time < 60:
        analysis.performance_verdict = "FAST"
    elif completion_time < 120:
        analysis.performance_verdict = "NORMAL"
    else:
        analysis.performance_verdict = "SLOW"

    return analysis


def plot_throttle_correlation(analyses: List[CorrelationAnalysis], output_dir: Path):
    """Create visualization showing throttling vs performance impact."""

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("CPU Throttling Impact Analysis", fontsize=16, fontweight="bold")

    # Group by workload type
    workload_types = set(a.workload_type for a in analyses)
    colors = {
        "cpu-bound": "red",
        "io-bound": "blue",
        "memory-bound": "green",
        "hybrid": "purple",
    }

    # 1. Throttled Time % vs Completion Time
    ax = axes[0, 0]
    for wtype in workload_types:
        subset = [a for a in analyses if a.workload_type == wtype]
        x = [a.throttled_time_pct for a in subset]
        y = [a.completion_time_s for a in subset]
        ax.scatter(x, y, label=wtype, color=colors.get(wtype, "gray"), s=100, alpha=0.7)

    ax.set_xlabel("Throttled Time (%)")
    ax.set_ylabel("Completion Time (s)")
    ax.set_title("Throttling vs Completion Time")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # 2. Throttle Rate by QoS Class
    ax = axes[0, 1]
    qos_classes = ["besteffort", "burstable", "guaranteed"]
    qos_data = {qos: [] for qos in qos_classes}

    for a in analyses:
        if a.qos_class in qos_data:
            qos_data[a.qos_class].append(a.throttled_periods_rate)

    x_pos = range(len(qos_classes))
    means = [
        sum(qos_data[qos]) / len(qos_data[qos]) if qos_data[qos] else 0
        for qos in qos_classes
    ]

    ax.bar(x_pos, means, color=["orange", "yellow", "lightgreen"])
    ax.set_xticks(x_pos)
    ax.set_xticklabels(qos_classes)
    ax.set_ylabel("Throttle Events / Second")
    ax.set_title("Throttling Rate by QoS Class")
    ax.grid(True, alpha=0.3, axis="y")

    # 3. CPU Usage by Workload Type
    ax = axes[1, 0]
    for wtype in workload_types:
        subset = [a for a in analyses if a.workload_type == wtype]
        qos_cpu = {qos: [] for qos in qos_classes}
        for a in subset:
            if a.qos_class in qos_cpu:
                qos_cpu[a.qos_class].append(a.cpu_usage_avg)

        x_pos = [i for i, qos in enumerate(qos_classes) if qos_cpu[qos]]
        means = [
            sum(qos_cpu[qos]) / len(qos_cpu[qos]) for qos in qos_classes if qos_cpu[qos]
        ]

        if means:
            ax.plot(
                x_pos, means, marker="o", label=wtype, color=colors.get(wtype, "gray")
            )

    ax.set_xticks(range(len(qos_classes)))
    ax.set_xticklabels(qos_classes)
    ax.set_ylabel("Average CPU Usage (cores)")
    ax.set_title("CPU Usage by QoS Class")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # 4. BPF Data (if available)
    ax = axes[1, 1]
    bpf_available = [a for a in analyses if a.bpf_throttle_events is not None]

    if bpf_available:
        x = [a.throttled_time_pct for a in bpf_available]
        y = [
            a.bpf_avg_throttle_duration_us / 1000 for a in bpf_available
        ]  # Convert to ms

        for a, xi, yi in zip(bpf_available, x, y):
            ax.scatter(
                xi, yi, color=colors.get(a.workload_type, "gray"), s=100, alpha=0.7
            )

        ax.set_xlabel("Throttled Time (%)")
        ax.set_ylabel("Avg Throttle Duration (ms)")
        ax.set_title("BPF: Throttle Duration vs Total Throttled Time")
        ax.grid(True, alpha=0.3)
    else:
        ax.text(
            0.5,
            0.5,
            "No BPF Data Available\n\nSee MANUAL-BPF-TRACING.md",
            ha="center",
            va="center",
            fontsize=12,
            color="gray",
        )
        ax.set_title("BPF Trace Data")
        ax.axis("off")

    plt.tight_layout()

    output_file = output_dir / "throttle-correlation.png"
    plt.savefig(output_file, dpi=150, bbox_inches="tight")
    print(f"✓ Saved correlation plot: {output_file}")
    plt.close()


def generate_report(analyses: List[CorrelationAnalysis], output_dir: Path):
    """Generate markdown report with findings."""

    report_lines = [
        "# CPU Throttling Analysis Report",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Summary",
        "",
        f"- Total experiments analyzed: {len(analyses)}",
        f"- Experiments with BPF data: {sum(1 for a in analyses if a.bpf_throttle_events is not None)}",
        "",
        "## Findings by Workload Type",
        "",
    ]

    # Group by workload type
    for wtype in sorted(set(a.workload_type for a in analyses)):
        subset = [a for a in analyses if a.workload_type == wtype]

        report_lines.extend([f"### {wtype.upper()}", ""])

        for a in sorted(subset, key=lambda x: x.qos_class):
            report_lines.extend(
                [
                    f"**{a.qos_class}** (`{a.experiment_id}`)",
                    f"- Completion time: {a.completion_time_s:.1f}s",
                    f"- CPU usage: {a.cpu_usage_avg:.2f} cores",
                    f"- Throttled time: {a.throttled_time_pct:.1f}%",
                    f"- Throttle rate: {a.throttled_periods_rate:.1f} events/s",
                    f"- Impact: {a.throttle_impact}",
                    f"- Performance: {a.performance_verdict}",
                ]
            )

            if a.bpf_throttle_events is not None:
                report_lines.extend(
                    [
                        f"- BPF throttle events: {a.bpf_throttle_events}",
                        f"- BPF avg throttle duration: {a.bpf_avg_throttle_duration_us:.0f}μs",
                    ]
                )

            report_lines.append("")

    report_lines.extend(
        [
            "## Key Insights",
            "",
            "### CPU-Bound Workloads",
            "- **BestEffort**: No CPU limits = no throttling, fastest completion",
            "- **Burstable**: Throttling occurs when exceeding requests",
            "- **Guaranteed**: Strict limits = consistent throttling if workload is CPU-intensive",
            "",
            "### IO-Bound Workloads",
            "- Minimal CPU throttling impact (workload naturally yields CPU)",
            "- Performance primarily limited by IO, not CPU limits",
            "",
            "### Memory-Bound Workloads",
            "- Moderate CPU usage with periodic bursts",
            "- Throttling occurs during memory allocation/scanning phases",
            "",
            "### Hybrid Workloads",
            "- Complex interaction between CPU, IO, and memory",
            "- Throttling impact depends on current phase of execution",
            "",
            "## Recommendations",
            "",
            "### For Developers",
            "",
            "1. **Profile your workload first**: Understand if it's CPU-bound, IO-bound, or memory-bound",
            "2. **Start with requests only**: Set CPU requests, leave limits unset initially",
            "3. **Monitor throttling**: Use `container_cpu_cfs_throttled_seconds_total` metric",
            "4. **Set limits conservatively**: If limits are needed, set them >1.5x the request",
            "5. **Test under load**: Verify performance with realistic workload patterns",
            "",
            "### For Cluster Administrators",
            "",
            "1. **Enable cgroup v2**: Improves CPU accounting and reduces throttling overhead",
            '2. **Monitor node CPU pressure**: Use `kube_node_status_condition{condition="CPUPressure"}`',
            "3. **Consider LimitRanges**: Set sensible defaults for namespaces",
            "4. **Use custom scheduler for latency-sensitive workloads**: scx-based schedulers can help",
            "5. **Tune kernel scheduler**: Consider `sched_latency_ns`, `sched_min_granularity_ns`",
            "",
            "## Next Steps",
            "",
            "- Run BPF traces manually to capture scheduler-level events (see MANUAL-BPF-TRACING.md)",
            "- Collect Prometheus data over longer periods (hours/days)",
            "- Test with multi-container pods and resource contention scenarios",
            "- Experiment with different kernel scheduler policies (CFS, deadline, etc.)",
            "",
            "## References",
            "",
            "- https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/",
            "- https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html",
            "- https://docs.kernel.org/admin-guide/cgroup-v2.html",
            "",
        ]
    )

    report_file = output_dir / "analysis-report.md"
    report_file.write_text("\n".join(report_lines))
    print(f"✓ Saved analysis report: {report_file}")


def main():
    base_dir = Path(__file__).parent.parent
    evidence_dir = base_dir / "phase2" / "evidence"
    bpf_dir = base_dir / "phase3" / "evidence" / "bpf-traces"
    output_dir = base_dir / "phase3" / "analysis"

    output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading Phase 2 evidence...")
    phase2_evidence = load_phase2_evidence(evidence_dir)
    print(f"  Loaded {len(phase2_evidence)} experiments")

    print("\nAnalyzing throttle impact...")
    analyses = []
    for evidence in phase2_evidence:
        experiment_id = evidence["experiment_id"]
        bpf = load_bpf_traces(bpf_dir, experiment_id)
        analysis = analyze_throttle_impact(evidence, bpf)
        analyses.append(analysis)

        status = "✓" if bpf else "○"
        print(f"  {status} {experiment_id}: {analysis.throttle_impact}")

    print("\nGenerating visualizations...")
    plot_throttle_correlation(analyses, output_dir)

    print("\nGenerating report...")
    generate_report(analyses, output_dir)

    # Save raw analysis data
    analysis_file = output_dir / "correlation-data.json"
    with open(analysis_file, "w") as f:
        json.dump([asdict(a) for a in analyses], f, indent=2)
    print(f"✓ Saved correlation data: {analysis_file}")

    print("\n" + "=" * 60)
    print("Phase 3 Analysis Complete")
    print("=" * 60)
    print(f"Results: {output_dir}")
    print("\nNext: Run BPF traces manually (see MANUAL-BPF-TRACING.md)")


if __name__ == "__main__":
    main()
