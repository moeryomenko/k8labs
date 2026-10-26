#!/usr/bin/env python3
"""
Generate comprehensive CPU requests/limits guide from Phase 2 evidence.
"""

import json
import os
from pathlib import Path
from datetime import datetime
import re

EVIDENCE_DIR = Path("research/cpu-limits/phase2/evidence")
OUTPUT_FILE = Path("research/cpu-limits/GUIDE.md")


def load_evidence():
    """Load all evidence JSON files."""
    evidence = []
    for f in EVIDENCE_DIR.glob("*.json"):
        if f.name == "summary.json":
            continue
        with open(f) as fp:
            data = json.load(fp)
            evidence.append(data)
    return sorted(evidence, key=lambda x: (x["workload_type"], x["qos_class"]))


def parse_stress_ng_output(logs):
    """Extract bogo ops and timing from stress-ng output."""
    if not logs:
        return None

    # Extract the summary line
    # stress-ng: info:  [1] cpu               11854    120.02    453.95      0.14        98.76        26.10
    match = re.search(
        r"cpu\s+(\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)",
        logs,
    )
    if match:
        return {
            "bogo_ops": int(match.group(1)),
            "real_time": float(match.group(2)),
            "usr_time": float(match.group(3)),
            "sys_time": float(match.group(4)),
            "bogo_ops_per_sec_real": float(match.group(5)),
            "bogo_ops_per_sec_cpu": float(match.group(6)),
        }
    return None


def calculate_cpu_delta(baseline, final):
    """Calculate CPU time delta."""
    if not baseline or not final:
        return None

    delta = {}
    for key in ["user", "nice", "system", "idle", "iowait"]:
        delta[key] = final.get(key, 0) - baseline.get(key, 0)

    total = sum(delta.values())
    if total == 0:
        return None

    return {
        "user_pct": (delta["user"] / total) * 100,
        "system_pct": (delta["system"] / total) * 100,
        "idle_pct": (delta["idle"] / total) * 100,
        "iowait_pct": (delta["iowait"] / total) * 100,
        "total_ticks": total,
    }


def analyze_experiment(exp):
    """Analyze a single experiment."""
    stress_ng = parse_stress_ng_output(exp.get("workload_logs"))
    cpu_delta = calculate_cpu_delta(
        exp.get("baseline_node_cpu"), exp.get("final_node_cpu")
    )

    baseline_cgroup = exp.get("baseline_cgroup") or {}
    final_cgroup = exp.get("final_cgroup") or {}

    throttled_periods = final_cgroup.get("nr_throttled", 0) - baseline_cgroup.get(
        "nr_throttled", 0
    )
    throttled_time_us = final_cgroup.get("throttled_usec", 0) - baseline_cgroup.get(
        "throttled_usec", 0
    )

    return {
        "experiment_id": exp["experiment_id"],
        "workload_type": exp["workload_type"],
        "qos_class": exp["qos_class"],
        "duration": exp["duration_seconds"],
        "stress_ng": stress_ng,
        "cpu_delta": cpu_delta,
        "throttled_periods": throttled_periods,
        "throttled_time_us": throttled_time_us,
        "throttled_time_sec": throttled_time_us / 1_000_000 if throttled_time_us else 0,
        "exit_code": exp.get("workload_exit_code"),
    }


def generate_markdown(evidence):
    """Generate comprehensive guide in Markdown."""

    analyzed = [analyze_experiment(exp) for exp in evidence]

    md = []
    md.append("# Kubernetes CPU Requests and Limits: Comprehensive Guide")
    md.append("")
    md.append(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    md.append("")
    md.append("## Executive Summary")
    md.append("")
    md.append(
        "This guide demonstrates the real-world impact of Kubernetes CPU requests and limits"
    )
    md.append(
        "on application performance, based on controlled experiments with different workload types"
    )
    md.append("and QoS classes.")
    md.append("")
    md.append("### Key Findings")
    md.append("")

    # Find throttled workloads
    throttled = [a for a in analyzed if a["throttled_periods"] > 0]
    if throttled:
        md.append("**CPU Throttling Observed:**")
        md.append("")
        for a in throttled:
            pct = (
                (a["throttled_time_sec"] / a["duration"]) * 100
                if a["duration"] > 0
                else 0
            )
            md.append(f"- **{a['workload_type']} ({a['qos_class']})**:")
            md.append(f"  - Throttled {a['throttled_periods']} times")
            md.append(
                f"  - Total throttled time: {a['throttled_time_sec']:.1f}s ({pct:.1f}% of runtime)"
            )
            if a["stress_ng"]:
                md.append(
                    f"  - Performance: {a['stress_ng']['bogo_ops_per_sec_real']:.2f} ops/sec"
                )
            md.append("")

    md.append("---")
    md.append("")
    md.append("## Table of Contents")
    md.append("")
    md.append(
        "1. [Understanding CPU Control in Kubernetes](#understanding-cpu-control)"
    )
    md.append("2. [Experimental Setup](#experimental-setup)")
    md.append("3. [Results by Workload Type](#results-by-workload-type)")
    md.append("4. [Developer Guidelines](#developer-guidelines)")
    md.append("5. [Administrator Guidelines](#administrator-guidelines)")
    md.append("6. [References](#references)")
    md.append("")
    md.append("---")
    md.append("")

    # Section 1: Understanding CPU Control
    md.append("## Understanding CPU Control in Kubernetes")
    md.append("")
    md.append("### CPU Requests vs Limits")
    md.append("")
    md.append("**CPU Requests (`resources.requests.cpu`)**:")
    md.append(
        "- Guarantee: Kubernetes scheduler ensures the node has this much CPU available"
    )
    md.append("- cgroup mapping: Sets `cpu.weight` (priority in CPU scheduler)")
    md.append("- Impact: Determines scheduling placement, not runtime enforcement")
    md.append("")
    md.append("**CPU Limits (`resources.limits.cpu`)**:")
    md.append("- Enforcement: Hard cap on CPU usage via CFS quota")
    md.append("- cgroup mapping: Sets `cpu.max` (quota/period)")
    md.append("- Impact: Causes throttling when exceeded, even if CPU is idle")
    md.append("")
    md.append("### QoS Classes")
    md.append("")
    md.append("| QoS Class | Condition | Behavior |")
    md.append("|-----------|-----------|----------|")
    md.append(
        "| **BestEffort** | No requests or limits | Lowest priority, can use any idle CPU |"
    )
    md.append(
        "| **Burstable** | Requests < Limits | Can burst above request, throttled at limit |"
    )
    md.append(
        "| **Guaranteed** | Requests == Limits | Fixed CPU allocation, hard throttled |"
    )
    md.append("")
    md.append("### cgroup v2 CPU Controller")
    md.append("")
    md.append("Kubernetes uses cgroup v2 for CPU control:")
    md.append("")
    md.append("```")
    md.append("cpu.weight       → CPU shares (request)")
    md.append("cpu.max          → quota/period (limit)")
    md.append("cpu.stat         → usage and throttling counters")
    md.append("```")
    md.append("")
    md.append("---")
    md.append("")

    # Section 2: Experimental Setup
    md.append("## Experimental Setup")
    md.append("")
    md.append("### Workload Types")
    md.append("")
    md.append("We tested four workload patterns:")
    md.append("")
    md.append("1. **CPU-bound**: Pure computation (stress-ng cpu)")
    md.append("2. **IO-bound**: Disk I/O operations (stress-ng hdd)")
    md.append("3. **Memory-bound**: Memory access patterns (stress-ng vm)")
    md.append("4. **Hybrid**: Mix of CPU, I/O, and memory")
    md.append("")
    md.append("### QoS Configurations")
    md.append("")
    md.append("Each workload type was tested with different QoS classes:")
    md.append("")
    md.append("| Configuration | Requests | Limits | QoS Class |")
    md.append("|---------------|----------|--------|-----------|")
    md.append("| BestEffort | - | - | BestEffort |")
    md.append("| Burstable | 500m | 2000m | Burstable |")
    md.append("| Guaranteed | 2000m | 2000m | Guaranteed |")
    md.append("")
    md.append("### Metrics Collected")
    md.append("")
    md.append("For each experiment:")
    md.append("- cgroup CPU stats (usage, throttling)")
    md.append("- Node CPU stats (/proc/stat)")
    md.append("- Workload performance (stress-ng bogo ops)")
    md.append("- Container logs")
    md.append("")
    md.append("---")
    md.append("")

    # Section 3: Results
    md.append("## Results by Workload Type")
    md.append("")

    # Group by workload type
    by_type = {}
    for a in analyzed:
        wtype = a["workload_type"]
        if wtype not in by_type:
            by_type[wtype] = []
        by_type[wtype].append(a)

    for wtype, experiments in sorted(by_type.items()):
        md.append(f"### {wtype.upper()} Workload")
        md.append("")

        # Summary table
        md.append(
            "| QoS Class | Duration | Throttled Periods | Throttled Time | Performance |"
        )
        md.append(
            "|-----------|----------|-------------------|----------------|-------------|"
        )

        for exp in experiments:
            qos = exp["qos_class"]
            dur = f"{exp['duration']:.1f}s"
            throttled_periods = exp["throttled_periods"]
            throttled_time = (
                f"{exp['throttled_time_sec']:.1f}s" if throttled_periods > 0 else "-"
            )

            if exp["stress_ng"]:
                perf = f"{exp['stress_ng']['bogo_ops_per_sec_real']:.1f} ops/s"
            else:
                perf = "N/A"

            md.append(
                f"| {qos} | {dur} | {throttled_periods} | {throttled_time} | {perf} |"
            )

        md.append("")

        # Detailed analysis
        for exp in experiments:
            md.append(f"#### {exp['qos_class'].upper()}")
            md.append("")

            if exp["throttled_periods"] > 0:
                pct = (
                    (exp["throttled_time_sec"] / exp["duration"]) * 100
                    if exp["duration"] > 0
                    else 0
                )
                md.append(f"**⚠️ Throttling Detected:**")
                md.append(f"- Throttled {exp['throttled_periods']} times")
                md.append(
                    f"- Total throttled time: {exp['throttled_time_sec']:.1f}s ({pct:.1f}% of runtime)"
                )
                md.append("")

            if exp["stress_ng"]:
                sn = exp["stress_ng"]
                md.append(f"**Performance:**")
                md.append(f"- Bogo operations: {sn['bogo_ops']}")
                md.append(f"- Operations/sec: {sn['bogo_ops_per_sec_real']:.2f}")
                md.append(
                    f"- CPU time: {sn['usr_time']:.1f}s user + {sn['sys_time']:.1f}s system"
                )
                md.append("")

            if exp["cpu_delta"]:
                cd = exp["cpu_delta"]
                md.append(f"**Node CPU Usage:**")
                md.append(f"- User: {cd['user_pct']:.1f}%")
                md.append(f"- System: {cd['system_pct']:.1f}%")
                md.append(f"- Idle: {cd['idle_pct']:.1f}%")
                md.append("")

        md.append("---")
        md.append("")

    # Section 4: Developer Guidelines
    md.append("## Developer Guidelines")
    md.append("")
    md.append("### When to Set CPU Limits")
    md.append("")
    md.append("**✅ SET LIMITS when:**")
    md.append("- Workload is predictable and bounded")
    md.append("- You need resource guarantees for capacity planning")
    md.append("- Multi-tenancy requires hard isolation")
    md.append("- Cost allocation needs strict enforcement")
    md.append("")
    md.append("**❌ AVOID LIMITS when:**")
    md.append("- Workload is bursty (batch jobs, CI/CD)")
    md.append("- Latency-sensitive applications (throttling adds latency)")
    md.append("- Cluster has spare capacity")
    md.append("- You're using HPA/VPA for scaling")
    md.append("")
    md.append("### Recommended Patterns")
    md.append("")
    md.append("#### Pattern 1: Request-Only (Recommended for most workloads)")
    md.append("")
    md.append("```yaml")
    md.append("resources:")
    md.append("  requests:")
    md.append('    cpu: "1000m"')
    md.append("  # No limits - can burst when cluster has capacity")
    md.append("```")
    md.append("")
    md.append("**Benefits:**")
    md.append("- No throttling")
    md.append("- Can use idle CPU")
    md.append("- Better performance for bursty workloads")
    md.append("")
    md.append("**Tradeoffs:**")
    md.append("- One pod can consume all CPU")
    md.append("- Harder to predict cluster capacity")
    md.append("")
    md.append("#### Pattern 2: Guaranteed (For critical services)")
    md.append("")
    md.append("```yaml")
    md.append("resources:")
    md.append("  requests:")
    md.append('    cpu: "2000m"')
    md.append("  limits:")
    md.append('    cpu: "2000m"')
    md.append("```")
    md.append("")
    md.append("**Benefits:**")
    md.append("- Predictable performance")
    md.append("- No CPU contention")
    md.append("- QoS class 'Guaranteed'")
    md.append("")
    md.append("**Tradeoffs:**")
    md.append("- Wastes CPU when pod is idle")
    md.append("- Gets throttled even if cluster has spare capacity")
    md.append("")
    md.append("#### Pattern 3: Burstable (For variable workloads)")
    md.append("")
    md.append("```yaml")
    md.append("resources:")
    md.append("  requests:")
    md.append('    cpu: "500m"')
    md.append("  limits:")
    md.append('    cpu: "2000m"')
    md.append("```")
    md.append("")
    md.append("**Benefits:**")
    md.append("- Can burst above baseline")
    md.append("- Scheduler uses request for placement")
    md.append("")
    md.append("**Tradeoffs:**")
    md.append("- Gets throttled at limit")
    md.append("- May get throttled even with idle CPU")
    md.append("")
    md.append("### Sizing Guidelines")
    md.append("")
    md.append(
        "1. **Start with monitoring**: Run without limits, measure P95/P99 CPU usage"
    )
    md.append("2. **Set request to P95**: Ensures scheduler finds appropriate nodes")
    md.append("3. **Set limit carefully**: Only if you need hard isolation")
    md.append(
        "4. **Monitor throttling**: Alert on `container_cpu_cfs_throttled_seconds_total`"
    )
    md.append("")
    md.append("### Debugging Throttling")
    md.append("")
    md.append("Check cgroup stats:")
    md.append("")
    md.append("```bash")
    md.append("# Inside container")
    md.append("cat /sys/fs/cgroup/cpu.stat")
    md.append("")
    md.append("# Look for:")
    md.append("# nr_throttled - number of times throttled")
    md.append("# throttled_usec - total time throttled (microseconds)")
    md.append("```")
    md.append("")
    md.append("Prometheus query:")
    md.append("")
    md.append("```promql")
    md.append("rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0")
    md.append("```")
    md.append("")
    md.append("---")
    md.append("")

    # Section 5: Administrator Guidelines
    md.append("## Administrator Guidelines")
    md.append("")
    md.append("### Cluster-Level CPU Management")
    md.append("")
    md.append("#### 1. Kernel Scheduler Configuration")
    md.append("")
    md.append("**CFS (Completely Fair Scheduler)** - Default for most workloads")
    md.append("")
    md.append("```bash")
    md.append("# Check current scheduler")
    md.append("cat /sys/block/*/queue/scheduler")
    md.append("")
    md.append("# Tune CFS parameters (if needed)")
    md.append("sysctl kernel.sched_latency_ns=6000000")
    md.append("sysctl kernel.sched_min_granularity_ns=750000")
    md.append("sysctl kernel.sched_wakeup_granularity_ns=1000000")
    md.append("```")
    md.append("")
    md.append("**When to use custom schedulers:**")
    md.append("")
    md.append("- **scx_simple**: Low-overhead, single-queue scheduling")
    md.append("- **scx_rusty**: NUMA-aware, better for multi-socket systems")
    md.append("- **scx_lavd**: Latency-sensitive workloads")
    md.append("")
    md.append("#### 2. Pod Priority and Preemption")
    md.append("")
    md.append("```yaml")
    md.append("apiVersion: scheduling.k8s.io/v1")
    md.append("kind: PriorityClass")
    md.append("metadata:")
    md.append("  name: high-priority")
    md.append("value: 1000")
    md.append("globalDefault: false")
    md.append('description: "High priority workloads"')
    md.append("```")
    md.append("")
    md.append("#### 3. Resource Quotas")
    md.append("")
    md.append("Enforce namespace-level limits:")
    md.append("")
    md.append("```yaml")
    md.append("apiVersion: v1")
    md.append("kind: ResourceQuota")
    md.append("metadata:")
    md.append("  name: cpu-quota")
    md.append("spec:")
    md.append("  hard:")
    md.append('    requests.cpu: "100"')
    md.append('    limits.cpu: "200"')
    md.append("```")
    md.append("")
    md.append("#### 4. LimitRanges")
    md.append("")
    md.append("Set defaults for pods without resource specs:")
    md.append("")
    md.append("```yaml")
    md.append("apiVersion: v1")
    md.append("kind: LimitRange")
    md.append("metadata:")
    md.append("  name: cpu-limits")
    md.append("spec:")
    md.append("  limits:")
    md.append("  - default:")
    md.append('      cpu: "500m"')
    md.append("    defaultRequest:")
    md.append('      cpu: "100m"')
    md.append("    type: Container")
    md.append("```")
    md.append("")
    md.append("### Monitoring and Alerting")
    md.append("")
    md.append("**Key Metrics:**")
    md.append("")
    md.append("```promql")
    md.append("# CPU throttling rate")
    md.append("rate(container_cpu_cfs_throttled_seconds_total[5m])")
    md.append("")
    md.append("# CPU usage vs request")
    md.append("rate(container_cpu_usage_seconds_total[5m]) /")
    md.append("  on(namespace,pod,container)")
    md.append('  kube_pod_container_resource_requests{resource="cpu"}')
    md.append("")
    md.append("# Pods hitting limits")
    md.append("rate(container_cpu_usage_seconds_total[5m]) /")
    md.append("  on(namespace,pod,container)")
    md.append('  kube_pod_container_resource_limits{resource="cpu"}')
    md.append("```")
    md.append("")
    md.append("**Alert Rules:**")
    md.append("")
    md.append("```yaml")
    md.append("- alert: HighCPUThrottling")
    md.append("  expr: |")
    md.append("    rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0.1")
    md.append("  annotations:")
    md.append('    summary: "Pod {{ $labels.pod }} is being throttled"')
    md.append("")
    md.append("- alert: CPURequestTooLow")
    md.append("  expr: |")
    md.append("    rate(container_cpu_usage_seconds_total[5m]) /")
    md.append('    kube_pod_container_resource_requests{resource="cpu"} > 1.2')
    md.append("  annotations:")
    md.append('    summary: "Pod {{ $labels.pod }} using more than 120% of request"')
    md.append("```")
    md.append("")
    md.append("---")
    md.append("")

    # Section 6: References
    md.append("## References")
    md.append("")
    md.append("### Research and Articles")
    md.append("")
    md.append(
        "1. [Stop Using CPU Limits - Robusta](https://home.robusta.dev/blog/stop-using-cpu-limits)"
    )
    md.append(
        "2. [Unthrottled: Fixing CPU Limits in the Cloud - Indeed](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)"
    )
    md.append(
        "3. [Why You Should Keep Using CPU Limits - Medium](https://dnastacio.medium.com/why-you-should-keep-using-cpu-limits-on-kubernetes-60c4e50dfc61)"
    )
    md.append(
        "4. [Kubernetes CPU Requests, Limits, and Autoscaling - Sysdig](https://www.sysdig.com/blog/kubernetes-cpu-requests-limits-autoscaling)"
    )
    md.append(
        "5. [The Case for Kubernetes Resource Limits - Kubernetes Blog](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/)"
    )
    md.append("")
    md.append("### Official Documentation")
    md.append("")
    md.append(
        "- [Kubernetes Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)"
    )
    md.append(
        "- [Pod QoS Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/)"
    )
    md.append(
        "- [cgroup v2 Documentation](https://www.kernel.org/doc/Documentation/cgroup-v2.txt)"
    )
    md.append("")
    md.append("### Tools")
    md.append("")
    md.append("- stress-ng: CPU/memory/IO stress testing")
    md.append("- bpftrace: Kernel tracing for scheduler analysis")
    md.append("- Prometheus + node_exporter: Metrics collection")
    md.append("")
    md.append("---")
    md.append("")
    md.append(
        f"*Experiments conducted: {len(evidence)} | Date: {datetime.now().strftime('%Y-%m-%d')}*"
    )

    return "\n".join(md)


def main():
    print("Loading evidence...")
    evidence = load_evidence()
    print(f"Found {len(evidence)} experiments")

    print("Generating comprehensive guide...")
    markdown = generate_markdown(evidence)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(markdown)

    print(f"\n✓ Guide written to: {OUTPUT_FILE}")
    print(f"  {len(markdown.splitlines())} lines")


if __name__ == "__main__":
    main()
