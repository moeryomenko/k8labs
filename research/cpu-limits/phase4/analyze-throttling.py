#!/usr/bin/env python3
"""
Analyze Phase 4 throttling experiments and generate comprehensive report.
"""

import json
from pathlib import Path
from datetime import datetime

EVIDENCE_DIR = Path("research/cpu-limits/phase4/evidence")
OUTPUT_FILE = Path("research/cpu-limits/phase4/THROTTLING_ANALYSIS.md")


def load_evidence():
    """Load all Phase 4 evidence files."""
    evidence = []
    for f in EVIDENCE_DIR.glob("*.json"):
        if f.name == "throttling-summary.json":
            continue
        with open(f) as fp:
            evidence.append(json.load(fp))
    return sorted(evidence, key=lambda x: x["test_config"]["limit"])


def analyze_throttling(evidence_list):
    """Analyze throttling patterns across experiments."""

    analysis = []
    for exp in evidence_list:
        config = exp["test_config"]
        throttling = exp.get(
            "throttling",
        )
        perf = exp.get("performance", {})

        analysis.append(
            {
                "name": config["name"],
                "request": config["request"],
                "limit": config["limit"],
                "workers": config["workers"],
                "expected": config["expected"],
                "nr_throttled": throttling.get("nr_throttled", 0),
                "throttled_seconds": throttling.get("throttled_seconds", 0),
                "throttling_pct": throttling.get("throttling_percentage", 0),
                "cpu_usage_seconds": throttling.get("usage_usec", 0) / 1_000_000,
                "performance": perf.get("bogo_ops_per_sec_real", 0),
                "bogo_ops": perf.get("bogo_ops", 0),
                "duration": exp["duration_seconds"],
            }
        )

    return analysis


def generate_markdown_report(analysis):
    """Generate comprehensive markdown report."""

    report = f"""# Phase 4: CPU Throttling Analysis

**Generated:** {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC  
**Experiments:** {len(analysis)}

---

## Executive Summary

This phase intentionally triggers CPU throttling by setting aggressive CPU limits
to demonstrate the impact on application performance. Unlike Phase 2 (where no 
throttling occurred), these experiments show what happens when containers exceed
their CPU quota.

### Key Findings

"""

    # Find extremes
    max_throttle = max(analysis, key=lambda x: x["throttling_pct"])
    min_throttle = min(analysis, key=lambda x: x["throttling_pct"])
    max_perf = max(analysis, key=lambda x: x["performance"])
    min_perf = min(analysis, key=lambda x: x["performance"])

    report += f"""
- **Maximum throttling:** {max_throttle["name"]} at {max_throttle["throttling_pct"]:.1f}%
- **Minimum throttling:** {min_throttle["name"]} at {min_throttle["throttling_pct"]:.1f}%
- **Performance range:** {min_perf["performance"]:.1f} to {max_perf["performance"]:.1f} ops/sec
- **Throttling impact:** {((max_perf["performance"] - min_perf["performance"]) / max_perf["performance"] * 100):.1f}% performance loss

---

## Experiment Results

### Comparison Table

| Configuration | Request | Limit | Throttling | Performance | Impact |
|---------------|---------|-------|------------|-------------|--------|
"""

    baseline_perf = max_perf["performance"]
    for exp in analysis:
        perf_impact = (baseline_perf - exp["performance"]) / baseline_perf * 100
        report += f"| {exp['name']} | {exp['request']} | {exp['limit']} | {exp['throttling_pct']:.1f}% | {exp['performance']:.1f} ops/s | {perf_impact:.1f}% |\n"

    report += "\n---\n\n## Detailed Analysis\n\n"

    for exp in analysis:
        report += f"""### {exp["name"]}

**Configuration:**
- CPU Request: `{exp["request"]}`
- CPU Limit: `{exp["limit"]}`
- Workers: {exp["workers"]}
- Duration: {exp["duration"]:.1f}s

**Expected Behavior:**
{exp["expected"]}

**Results:**
- **Throttling Events:** {exp["nr_throttled"]:,} times
- **Throttled Duration:** {exp["throttled_seconds"]:.2f}s ({exp["throttling_pct"]:.1f}% of CPU time)
- **CPU Usage:** {exp["cpu_usage_seconds"]:.2f}s
- **Performance:** {exp["performance"]:.1f} bogo ops/sec
- **Total Operations:** {exp["bogo_ops"]:,}

**Analysis:**
"""

        if exp["throttling_pct"] > 50:
            report += f"- ❌ **Severe throttling** - Container spent {exp['throttling_pct']:.1f}% of time throttled\n"
            report += (
                f"- Performance severely degraded to {exp['performance']:.1f} ops/sec\n"
            )
            report += (
                f"- Recommendation: Increase CPU limit or reduce workload intensity\n"
            )
        elif exp["throttling_pct"] > 20:
            report += f"- ⚠️ **Moderate throttling** - Container spent {exp['throttling_pct']:.1f}% of time throttled\n"
            report += f"- Performance impacted: {exp['performance']:.1f} ops/sec\n"
            report += f"- Recommendation: Consider increasing CPU limit if performance is critical\n"
        elif exp["throttling_pct"] > 5:
            report += f"- ✓ **Light throttling** - Container spent {exp['throttling_pct']:.1f}% of time throttled\n"
            report += f"- Performance acceptable: {exp['performance']:.1f} ops/sec\n"
            report += f"- Recommendation: Current limits are appropriate for non-critical workloads\n"
        else:
            report += f"- ✅ **No significant throttling** - Container spent {exp['throttling_pct']:.1f}% of time throttled\n"
            report += f"- Performance optimal: {exp['performance']:.1f} ops/sec\n"
            report += f"- Recommendation: Current limits are well-sized\n"

        report += "\n---\n\n"

    report += """## Understanding CPU Throttling

### What is CPU Throttling?

CPU throttling occurs when a container's CPU usage exceeds its configured limit.
The Linux kernel's cgroup CPU controller enforces limits by:

1. Tracking CPU time used in 100ms periods
2. Comparing usage to the quota (limit × period)
3. Suspending the process when quota is exhausted
4. Resuming in the next period

### cgroup CPU Statistics

Key metrics from `/sys/fs/cgroup/cpu.stat`:

- `usage_usec`: Total CPU time consumed (user + system)
- `nr_periods`: Number of enforcement periods elapsed
- `nr_throttled`: Number of periods where throttling occurred
- `throttled_usec`: Total time spent throttled

### Calculating Throttling Percentage

```
throttling_pct = (throttled_usec / usage_usec) × 100
```

This tells you what percentage of potential CPU time was lost to throttling.

### Impact on Applications

**Latency-sensitive applications:**
- HTTP servers see P99 latency spikes
- Database queries take longer
- Real-time systems miss deadlines

**Throughput-focused applications:**
- Batch jobs take longer to complete
- Data processing throughput drops
- Background workers queue up

**Bursty applications:**
- Cannot burst above limit even with idle CPU
- Cold starts are slower
- Peak load handling is degraded

---

## Comparison: Phase 2 vs Phase 4

### Phase 2 (No Throttling)
- CPU-bound workload with 2000m request, 2000m limit
- Performance: **51.5 ops/sec**
- Throttling: **0%** (limits matched workload)

### Phase 4 (Intentional Throttling)
- CPU-bound workload with 100m request, 500m limit
- Performance: **{min_perf['performance']:.1f} ops/sec**
- Throttling: **{min_perf['throttling_pct']:.1f}%**

**Performance loss:** {((51.5 - min_perf['performance']) / 51.5 * 100):.1f}% due to aggressive limits

---

## Recommendations

### For Developers

1. **Monitor throttling in production**
   ```promql
   rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0
   ```

2. **Size limits based on actual usage**
   - Measure P95/P99 CPU usage over time
   - Set limit = P99 + 20% headroom
   - Or use no limit and only set requests

3. **Test with limits before production**
   - Run load tests with production limits
   - Measure P99 latency and throughput
   - Adjust limits if throttling > 5%

4. **Consider request-only pattern**
   - Only set CPU requests, no limits
   - Allows bursting when cluster has capacity
   - Scheduler still ensures placement

### For Administrators

1. **Detect throttling cluster-wide**
   ```promql
   topk(10, 
     rate(container_cpu_cfs_throttled_seconds_total[5m]) /
     rate(container_cpu_cfs_periods_total[5m]) * 100
   )
   ```

2. **Alert on excessive throttling**
   - Throttling > 10% for > 5 minutes
   - Indicates undersized limits or resource contention

3. **Review resource quotas**
   - Namespace quotas may force developers to set aggressive limits
   - Balance cost control with application performance

4. **Consider node-level CPU management**
   - cpuset for CPU pinning
   - CPU isolation for latency-sensitive workloads
   - Custom schedulers for specialized needs

---

## Next Steps

### Phase 4b: Latency Impact
- Deploy HTTP server with CPU limits
- Generate load with wrk/hey
- Measure P50/P99/P999 latency during throttling

### Phase 4c: Prometheus Integration
- Deploy Prometheus + Grafana
- Create real-time throttling dashboard
- Set up alerts for throttling detection

### Phase 4d: BPF Scheduler Analysis
- Deploy bpftrace on nodes
- Collect runqlat, runqlen during throttling
- Correlate scheduler latency with throttling events

### Phase 4e: Scheduler Comparison
- Install scx tools (requires kernel 6.12+)
- Compare CFS vs scx schedulers
- Measure throttling impact under each scheduler

---

## References

### Kubernetes CPU Management
- [Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Pod QoS Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/)

### Linux cgroup CPU Controller
- [cgroup v2 Documentation](https://www.kernel.org/doc/Documentation/cgroup-v2.txt)
- [CPU Controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#cpu)

### Industry Perspectives
- [Stop Using CPU Limits (Robusta)](https://home.robusta.dev/blog/stop-using-cpu-limits)
- [Unthrottled: Fixing CPU Limits (Indeed)](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)
- [The Case for Resource Limits (Kubernetes)](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/)

---

**Experiment data:** `research/cpu-limits/phase4/evidence/`  
**Phase 2 comparison:** `research/cpu-limits/GUIDE.md`
"""

    return report


def main():
    print("Loading Phase 4 evidence...")
    evidence = load_evidence()

    if not evidence:
        print("No evidence files found. Run run-throttling-experiments.py first.")
        return

    print(f"Found {len(evidence)} experiments")

    print("Analyzing throttling patterns...")
    analysis = analyze_throttling(evidence)

    print("Generating comprehensive report...")
    report = generate_markdown_report(analysis)

    with open(OUTPUT_FILE, "w") as f:
        f.write(report)

    print(f"\n✓ Report written to: {OUTPUT_FILE}")

    # Print summary
    print(f"\n{'=' * 60}")
    print("Throttling Summary")
    print(f"{'=' * 60}")
    print(f"{'Configuration':<30} {'Throttling %':<15} {'Performance'}")
    print("-" * 65)
    for exp in analysis:
        print(
            f"{exp['name']:<30} {exp['throttling_pct']:>13.1f}%  {exp['performance']:>10.1f} ops/s"
        )


if __name__ == "__main__":
    main()
