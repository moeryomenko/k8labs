#!/usr/bin/env python3
"""
Analyze BPF traces and correlate with Phase 2 cgroup metrics.

Usage:
    # Analyze all traces
    python3 analyze-bpf-traces.py

    # Analyze specific workload
    python3 analyze-bpf-traces.py --workload cpu-bound-burstable

    # Generate comparison report
    python3 analyze-bpf-traces.py --compare
"""

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime

PHASE2_EVIDENCE = Path("research/cpu-limits/phase2/evidence")
PHASE3_TRACES = Path("research/cpu-limits/phase3/bpf-traces")
OUTPUT_DIR = Path("research/cpu-limits/phase3/analysis")


@dataclass
class BPFThrottleEvent:
    """Single throttle event from BPF trace."""

    timestamp_ms: int
    comm: str
    pid: int
    throttled_us: int


@dataclass
class BPFTraceSummary:
    """Summary of BPF trace analysis."""

    workload: str
    total_events: int
    total_throttled_us: int
    total_throttled_ms: float
    avg_throttle_us: float
    max_throttle_us: int
    min_throttle_us: int
    events: List[BPFThrottleEvent]

    def to_dict(self):
        return {**asdict(self), "events": [asdict(e) for e in self.events]}


@dataclass
class CgroupMetricsSummary:
    """Summary from Phase 2 cgroup metrics."""

    workload: str
    total_samples: int
    final_throttled_periods: int
    final_throttled_usec: int
    final_usage_usec: int
    avg_usage_percent: float
    max_usage_percent: float


@dataclass
class CorrelatedAnalysis:
    """Correlation between BPF and cgroup metrics."""

    workload: str
    bpf_events: int
    cgroup_throttled_periods: int
    event_discrepancy: Optional[int]  # BPF vs cgroup event count difference
    bpf_throttled_ms: float
    cgroup_throttled_ms: float
    time_discrepancy_ms: Optional[float]
    cpu_usage_percent: float
    throttle_ratio: float  # throttled_time / total_time


def parse_bpf_trace(trace_file: Path) -> BPFTraceSummary:
    """Parse bpftrace output file."""

    events = []
    total_events = 0
    total_throttled_us = 0

    with open(trace_file) as f:
        content = f.read()

    # Parse individual events
    # Format: "TIME(ms) COMM PID THROTTLED(us)"
    # Example: "1234 stress-ng-cpu 12345 10000"
    event_pattern = r"^(\d+)\s+(\S+)\s+(\d+)\s+(\d+)"

    for line in content.splitlines():
        match = re.match(event_pattern, line.strip())
        if match:
            timestamp_ms = int(match.group(1))
            comm = match.group(2)
            pid = int(match.group(3))
            throttled_us = int(match.group(4))

            events.append(
                BPFThrottleEvent(
                    timestamp_ms=timestamp_ms,
                    comm=comm,
                    pid=pid,
                    throttled_us=throttled_us,
                )
            )
            total_throttled_us += throttled_us

    # Parse summary section
    # Format: "Total throttle events: 123"
    summary_match = re.search(r"Total throttle events:\s*(\d+)", content)
    if summary_match:
        total_events = int(summary_match.group(1))
    else:
        total_events = len(events)

    # Calculate statistics
    if events:
        throttle_values = [e.throttled_us for e in events]
        avg_throttle_us = sum(throttle_values) / len(throttle_values)
        max_throttle_us = max(throttle_values)
        min_throttle_us = min(throttle_values)
    else:
        avg_throttle_us = 0
        max_throttle_us = 0
        min_throttle_us = 0

    workload = trace_file.stem.replace("bpf-trace-", "")

    return BPFTraceSummary(
        workload=workload,
        total_events=total_events,
        total_throttled_us=total_throttled_us,
        total_throttled_ms=total_throttled_us / 1000.0,
        avg_throttle_us=avg_throttle_us,
        max_throttle_us=max_throttle_us,
        min_throttle_us=min_throttle_us,
        events=events,
    )


def load_phase2_metrics(workload: str) -> Optional[CgroupMetricsSummary]:
    """Load Phase 2 cgroup metrics for a workload."""

    # Find most recent evidence file for this workload
    evidence_files = list(PHASE2_EVIDENCE.glob(f"{workload}-*.json"))
    if not evidence_files:
        return None

    # Use most recent
    evidence_file = max(evidence_files, key=lambda p: p.stat().st_mtime)

    with open(evidence_file) as f:
        data = json.load(f)

    samples = data.get("samples", [])
    if not samples:
        return None

    # Get final sample
    final = samples[-1]
    cgroup = final.get("cgroup", {})

    # Calculate average CPU usage across samples
    usage_percents = [s.get("cgroup", {}).get("usage_percent", 0) for s in samples]
    avg_usage = sum(usage_percents) / len(usage_percents) if usage_percents else 0
    max_usage = max(usage_percents) if usage_percents else 0

    return CgroupMetricsSummary(
        workload=workload,
        total_samples=len(samples),
        final_throttled_periods=cgroup.get("nr_throttled", 0),
        final_throttled_usec=cgroup.get("throttled_usec", 0),
        final_usage_usec=cgroup.get("usage_usec", 0),
        avg_usage_percent=avg_usage,
        max_usage_percent=max_usage,
    )


def correlate_metrics(
    bpf: BPFTraceSummary, cgroup: CgroupMetricsSummary
) -> CorrelatedAnalysis:
    """Correlate BPF and cgroup metrics."""

    # Calculate discrepancies
    event_discrepancy = None
    if cgroup.final_throttled_periods > 0:
        event_discrepancy = bpf.total_events - cgroup.final_throttled_periods

    cgroup_throttled_ms = cgroup.final_throttled_usec / 1000.0
    time_discrepancy_ms = None
    if cgroup_throttled_ms > 0:
        time_discrepancy_ms = bpf.total_throttled_ms - cgroup_throttled_ms

    # Calculate throttle ratio
    total_time_us = cgroup.final_usage_usec + cgroup.final_throttled_usec
    throttle_ratio = 0
    if total_time_us > 0:
        throttle_ratio = cgroup.final_throttled_usec / total_time_us

    return CorrelatedAnalysis(
        workload=bpf.workload,
        bpf_events=bpf.total_events,
        cgroup_throttled_periods=cgroup.final_throttled_periods,
        event_discrepancy=event_discrepancy,
        bpf_throttled_ms=bpf.total_throttled_ms,
        cgroup_throttled_ms=cgroup_throttled_ms,
        time_discrepancy_ms=time_discrepancy_ms,
        cpu_usage_percent=cgroup.avg_usage_percent,
        throttle_ratio=throttle_ratio,
    )


def analyze_workload(workload: str) -> Optional[Dict]:
    """Analyze both BPF and cgroup data for a workload."""

    # Find BPF trace
    trace_file = PHASE3_TRACES / f"bpf-trace-{workload}.txt"
    if not trace_file.exists():
        print(f"Warning: No BPF trace found for {workload}")
        return None

    # Parse BPF trace
    print(f"Analyzing BPF trace: {trace_file.name}")
    bpf_summary = parse_bpf_trace(trace_file)

    # Load Phase 2 metrics
    cgroup_summary = load_phase2_metrics(workload)
    if not cgroup_summary:
        print(f"Warning: No Phase 2 metrics found for {workload}")
        return {
            "workload": workload,
            "bpf": bpf_summary.to_dict(),
            "cgroup": None,
            "correlation": None,
        }

    # Correlate
    correlation = correlate_metrics(bpf_summary, cgroup_summary)

    return {
        "workload": workload,
        "bpf": bpf_summary.to_dict(),
        "cgroup": asdict(cgroup_summary),
        "correlation": asdict(correlation),
    }


def generate_comparison_report(analyses: List[Dict]) -> str:
    """Generate markdown comparison report."""

    report = ["# BPF Trace Analysis Report", ""]
    report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append("")

    report.append("## Summary")
    report.append("")
    report.append(
        "| Workload | BPF Events | Cgroup Periods | Event Δ | BPF Throttled (ms) | Cgroup Throttled (ms) | Time Δ (ms) | CPU Usage % | Throttle Ratio |"
    )
    report.append(
        "|----------|------------|----------------|---------|---------------------|------------------------|-------------|-------------|----------------|"
    )

    for analysis in analyses:
        if not analysis or not analysis.get("correlation"):
            continue

        corr = analysis["correlation"]
        event_delta = (
            corr["event_discrepancy"]
            if corr["event_discrepancy"] is not None
            else "N/A"
        )
        time_delta = (
            f"{corr['time_discrepancy_ms']:.2f}"
            if corr["time_discrepancy_ms"] is not None
            else "N/A"
        )

        report.append(
            f"| {corr['workload']} "
            f"| {corr['bpf_events']} "
            f"| {corr['cgroup_throttled_periods']} "
            f"| {event_delta} "
            f"| {corr['bpf_throttled_ms']:.2f} "
            f"| {corr['cgroup_throttled_ms']:.2f} "
            f"| {time_delta} "
            f"| {corr['cpu_usage_percent']:.1f} "
            f"| {corr['throttle_ratio']:.3f} |"
        )

    report.append("")
    report.append("## Detailed Analysis")
    report.append("")

    for analysis in analyses:
        if not analysis:
            continue

        workload = analysis["workload"]
        report.append(f"### {workload}")
        report.append("")

        bpf = analysis.get(
            "bpf",
        )
        if bpf:
            report.append("**BPF Trace:**")
            report.append(f"- Total events: {bpf['total_events']}")
            report.append(f"- Total throttled: {bpf['total_throttled_ms']:.2f} ms")
            report.append(f"- Average throttle: {bpf['avg_throttle_us']:.0f} μs")
            report.append(f"- Max throttle: {bpf['max_throttle_us']} μs")
            report.append(f"- Min throttle: {bpf['min_throttle_us']} μs")
            report.append("")

        cgroup = analysis.get("cgroup")
        if cgroup:
            report.append("**Cgroup Metrics:**")
            report.append(f"- Samples: {cgroup['total_samples']}")
            report.append(f"- Throttled periods: {cgroup['final_throttled_periods']}")
            report.append(
                f"- Throttled time: {cgroup['final_throttled_usec'] / 1000:.2f} ms"
            )
            report.append(f"- Total usage: {cgroup['final_usage_usec'] / 1000:.2f} ms")
            report.append(f"- Average CPU: {cgroup['avg_usage_percent']:.1f}%")
            report.append(f"- Max CPU: {cgroup['max_usage_percent']:.1f}%")
            report.append("")

        corr = analysis.get("correlation")
        if corr:
            report.append("**Correlation:**")
            if corr["event_discrepancy"] is not None:
                report.append(
                    f"- Event discrepancy: {corr['event_discrepancy']} (BPF - cgroup)"
                )
            if corr["time_discrepancy_ms"] is not None:
                report.append(
                    f"- Time discrepancy: {corr['time_discrepancy_ms']:.2f} ms"
                )
            report.append(f"- Throttle ratio: {corr['throttle_ratio']:.1%}")
            report.append("")

    report.append("## Interpretation")
    report.append("")
    report.append(
        "**Event Discrepancy**: BPF should report the same or more events than cgroup `nr_throttled`."
    )
    report.append("- Positive Δ: BPF captured more granular events")
    report.append("- Zero Δ: Perfect match")
    report.append("- Negative Δ: Possible race condition or missed BPF events")
    report.append("")
    report.append(
        "**Time Discrepancy**: BPF and cgroup throttled time should match closely."
    )
    report.append("- Small Δ (<100ms): Timing precision differences")
    report.append("- Large Δ: Investigation needed")
    report.append("")
    report.append(
        "**Throttle Ratio**: `throttled_time / (usage_time + throttled_time)`"
    )
    report.append("- High ratio (>0.5): Significant throttling impact")
    report.append("- Low ratio (<0.1): Minimal throttling")
    report.append("")

    return "\n".join(report)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze BPF traces and correlate with cgroup metrics"
    )
    parser.add_argument("--workload", type=str, help="Specific workload to analyze")
    parser.add_argument(
        "--compare", action="store_true", help="Generate comparison report"
    )

    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Find all BPF traces
    trace_files = list(PHASE3_TRACES.glob("bpf-trace-*.txt"))
    if not trace_files:
        print("No BPF traces found. Run collection first:")
        print("  python3 research/cpu-limits/phase3/collect-guest-bpf.py")
        return

    # Analyze workloads
    analyses = []

    if args.workload:
        workloads = [args.workload]
    else:
        workloads = [f.stem.replace("bpf-trace-", "") for f in trace_files]

    for workload in workloads:
        analysis = analyze_workload(workload)
        if analysis:
            analyses.append(analysis)

            # Save individual analysis
            output_file = OUTPUT_DIR / f"{workload}-analysis.json"
            with open(output_file, "w") as f:
                json.dump(analysis, f, indent=2)
            print(f"✓ Analysis saved: {output_file}")

    # Generate comparison report
    if args.compare and analyses:
        report = generate_comparison_report(analyses)
        report_file = OUTPUT_DIR / "comparison-report.md"
        with open(report_file, "w") as f:
            f.write(report)
        print(f"\n✓ Comparison report: {report_file}")

    print(f"\nAnalyzed {len(analyses)} workload(s)")


if __name__ == "__main__":
    main()
