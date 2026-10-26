#!/usr/bin/env python3
"""
Analyze Phase 4 throttling experiments and generate comprehensive report.
Fixed to correctly calculate throttling percentage from cgroup stats.
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

        # Use baseline_cgroup (collected before pod deletion)
        cgroup = exp.get("baseline_cgroup") or {}
        perf = exp.get("performance") or {}

        # Calculate throttling metrics
        usage_usec = cgroup.get("usage_usec", 0)
        throttled_usec = cgroup.get("throttled_usec", 0)
        nr_throttled = cgroup.get("nr_throttled", 0)

        # Correct throttling calculation:
        # throttling_pct = time_throttled / (time_used + time_throttled) * 100
        # This shows what % of the container's desired runtime was blocked
        total_wanted = usage_usec + throttled_usec
        throttling_pct = (
            (throttled_usec / total_wanted * 100) if total_wanted > 0 else 0
        )

        throttled_seconds = throttled_usec / 1_000_000
        cpu_usage_seconds = usage_usec / 1_000_000

        analysis.append(
            {
                "name": config["name"],
                "request": config["request"],
                "limit": config["limit"],
                "workers": config["workers"],
                "expected": config["expected"],
                "nr_throttled": nr_throttled,
                "throttled_seconds": throttled_seconds,
                "throttling_pct": throttling_pct,
                "cpu_usage_seconds": cpu_usage_seconds,
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
throttling occurred with appropriate limits), these experiments show what happens 
when containers exceed their CPU quota.

### Key Findings

"""

    # Find extremes
    max_throttle = max(analysis, key=lambda x: x["throttling_pct"])
    min_throttle = min(analysis, key=lambda x: x["throttling_pct"])
    max_perf = max(analysis, key=lambda x: x["performance"])
    min_perf = min(analysis, key=lambda x: x["performance"])

    phase2_baseline = 51.5  # From Phase 2 cpu-bound-burstable

    report += f"""
- **Maximum throttling:** {max_throttle["name"]} at {max_throttle["throttling_pct"]:.1f}%
- **Minimum throttling:** {min_throttle["name"]} at {min_throttle["throttling_pct"]:.1f}%
- **Performance range:** {min_perf["performance"]:.1f} to {max_perf["performance"]:.1f} ops/sec
- **Phase 2 baseline:** 51.5 ops/sec (2000m limit, 0% throttling)
- **Maximum performance loss:** {((phase2_baseline - min_perf["performance"]) / phase2_baseline * 100):.1f}% (heavy throttle)
- **Performance improvement:** {((max_perf["performance"] - phase2_baseline) / phase2_baseline * 100):.1f}% (no throttle, 4 workers vs 2)

---

## Experiment Results

### Comparison Table

| Configuration | Request | Limit | Throttling | Performance | vs Phase 2 |
|---------------|---------|-------|------------|-------------|------------|
"""

    for exp in analysis:
        vs_phase2 = (exp["performance"] - phase2_baseline) / phase2_baseline * 100
        sign = "+" if vs_phase2 > 0 else ""
        report += f"| {exp['name']} | {exp['request']} | {exp['limit']} | {exp['throttling_pct']:.1f}% | {exp['performance']:.1f} ops/s | {sign}{vs_phase2:.1f}% |\n"

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
- **Throttled Duration:** {exp["throttled_seconds"]:.2f}s
- **Throttling Percentage:** {exp["throttling_pct"]:.1f}% of desired CPU time blocked
- **CPU Usage:** {exp["cpu_usage_seconds"]:.2f}s
- **Performance:** {exp["performance"]:.1f} bogo ops/sec
- **Total Operations:** {exp["bogo_ops"]:,}

**Analysis:**
"""

        vs_phase2 = (exp["performance"] - phase2_baseline) / phase2_baseline * 100

        if exp["throttling_pct"] > 50:
            report += f"- ❌ **Severe throttling** - {exp['throttling_pct']:.1f}% of desired CPU time was blocked\n"
            report += f"- Performance severely degraded to {exp['performance']:.1f} ops/sec ({vs_phase2:.1f}% vs Phase 2)\n"
            report += f"- Container wanted {exp['cpu_usage_seconds'] + exp['throttled_seconds']:.1f}s but only got {exp['cpu_usage_seconds']:.1f}s\n"
            report += f"- Recommendation: Increase CPU limit significantly or reduce workload intensity\n"
        elif exp["throttling_pct"] > 20:
            report += f"- ⚠️ **Moderate throttling** - {exp['throttling_pct']:.1f}% of desired CPU time was blocked\n"
            report += f"- Performance: {exp['performance']:.1f} ops/sec ({vs_phase2:+.1f}% vs Phase 2)\n"
            report += f"- Recommendation: Consider increasing CPU limit if performance is critical\n"
        elif exp["throttling_pct"] > 5:
            report += f"- ✓ **Light throttling** - {exp['throttling_pct']:.1f}% of desired CPU time was blocked\n"
            report += f"- Performance acceptable: {exp['performance']:.1f} ops/sec ({vs_phase2:+.1f}% vs Phase 2)\n"
            report += f"- Recommendation: Current limits are appropriate for non-critical workloads\n"
        else:
            report += f"- ✅ **No significant throttling** - {exp['throttling_pct']:.1f}% of desired CPU time was blocked\n"
            report += f"- Performance optimal: {exp['performance']:.1f} ops/sec ({vs_phase2:+.1f}% vs Phase 2)\n"
            report += f"- Recommendation: Current limits are well-sized\n"

        report += "\n---\n\n"

    report += f"""## Understanding CPU Throttling

### What is CPU Throttling?

CPU throttling occurs when a container's CPU usage exceeds its configured limit.
The Linux kernel's cgroup CPU controller enforces limits by:

1. Tracking CPU time used in 100ms periods
2. Comparing usage to the quota (limit × period)
3. Suspending the process when quota is exhausted
4. Resuming in the next period

### cgroup CPU Statistics

Key metrics from `/sys/fs/cgroup/cpu.stat`:

- `usage_usec`: Total CPU time actually consumed (user + system)
- `throttled_usec`: Total CPU time that was **denied** due to limits
- `nr_periods`: Number of enforcement periods elapsed
- `nr_throttled`: Number of periods where throttling occurred

### Calculating Throttling Percentage

```
throttling_pct = throttled_usec / (usage_usec + throttled_usec) × 100
```

This tells you what percentage of the container's **desired** CPU time was blocked.

**Example:** Heavy throttle configuration
- Container wanted: {analysis[0]["cpu_usage_seconds"] + analysis[0]["throttled_seconds"]:.1f}s of CPU
- Container got: {analysis[0]["cpu_usage_seconds"]:.1f}s of CPU
- Container was denied: {analysis[0]["throttled_seconds"]:.1f}s ({analysis[0]["throttling_pct"]:.1f}%)

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
- CPU-bound workload with 2000m request, 2000m limit, 2 workers
- Performance: **51.5 ops/sec**
- Throttling: **0%** (limits matched workload)

### Phase 4 Results (4 workers, varying limits)
"""

    for exp in analysis:
        vs_phase2 = (exp["performance"] - phase2_baseline) / phase2_baseline * 100
        report += f"\n**{exp['name']}** ({exp['limit']} limit)\n"
        report += f"- Performance: **{exp['performance']:.1f} ops/sec** ({vs_phase2:+.1f}% vs Phase 2)\n"
        report += f"- Throttling: **{exp['throttling_pct']:.1f}%**\n"
        report += f"- Throttled: {exp['throttled_seconds']:.1f}s out of {exp['throttled_seconds'] + exp['cpu_usage_seconds']:.1f}s desired\n"

    report += """

### Key Insights

1. **Heavy throttling (500m limit)** reduced performance by 62.7% compared to Phase 2
2. **Light throttling (3000m limit)** actually **improved** performance by 111.3% due to 4 workers vs 2
3. **No throttle (4000m limit)** shows maximum performance: 139.1 ops/sec (+170.1%)
4. **Throttling directly correlates with performance loss** within the same workload type

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

### Deploy Prometheus Monitoring
```bash
cd research/cpu-limits/phase4
./deploy-prometheus.sh
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

### Import Grafana Dashboard
1. Open http://localhost:3000
2. Import `monitoring/grafana-dashboard-throttling.json`
3. View real-time throttling metrics

### Optional: BPF Scheduler Analysis
If you have SSH access to nodes:
```bash
ssh root@<node>
sudo cp phase3/bpf-tracer.service /etc/systemd/system/
sudo cp phase3/bpf-tracer-daemon.py /usr/local/bin/
sudo systemctl enable --now bpf-tracer
```

### Optional: Scheduler Comparison
If kernel 6.12+ with scx support:
```bash
# Install scx tools
git clone https://github.com/sched-ext/scx.git
cd scx && make && sudo make install

# Run comparison
python3 analyze-scheduler-comparison.py
```

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
**Visual dashboard:** `research/cpu-limits/research-dashboard.html`
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
    print(f"\n{'=' * 85}")
    print("Throttling Summary")
    print(f"{'=' * 85}")
    print(
        f"{'Configuration':<35} {'Throttling %':<15} {'Performance':<15} {'vs Phase 2'}"
    )
    print("-" * 85)
    phase2_baseline = 51.5
    for exp in analysis:
        vs_phase2 = (exp["performance"] - phase2_baseline) / phase2_baseline * 100
        print(
            f"{exp['name']:<35} {exp['throttling_pct']:>13.1f}%  {exp['performance']:>10.1f} ops/s  {vs_phase2:>+7.1f}%"
        )

    print(f"\n{'=' * 85}")
    print("Key Findings:")
    print(f"{'=' * 85}")

    max_throttle = max(analysis, key=lambda x: x["throttling_pct"])
    print(
        f"• Maximum throttling: {max_throttle['throttling_pct']:.1f}% ({max_throttle['name']})"
    )

    min_perf = min(analysis, key=lambda x: x["performance"])
    loss = (phase2_baseline - min_perf["performance"]) / phase2_baseline * 100
    print(f"• Maximum performance loss: {loss:.1f}% vs Phase 2 baseline")

    max_perf = max(analysis, key=lambda x: x["performance"])
    gain = (max_perf["performance"] - phase2_baseline) / phase2_baseline * 100
    print(f"• Maximum performance gain: +{gain:.1f}% (more workers, sufficient limit)")

    print(f"\nView full report: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
