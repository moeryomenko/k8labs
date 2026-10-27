# Phase 4: CPU Throttling Analysis

**Generated:** 2026-10-26  
**Experiments:** 4  
**Evidence:** `phase4/evidence/` and `phase4/evidence/throttling-summary.json`

---

## Executive Summary

This phase deliberately undersizes CPU limits on a CPU-bound workload to measure
what a quota costs in throughput. Every run used 4 CPU workers for about 120 s;
only the request and limit changed.

### Key Findings

| Configuration | Request | Limit | Bogo ops/s | vs no-limit |
|---------------|---------|-------|-----------|-------------|
| cpu-bound-no-throttle | 2000m | 4000m | 139.05 | baseline |
| cpu-bound-light-throttle | 1000m | 3000m | 108.78 | -21.8% |
| cpu-bound-moderate-throttle | 500m | 1500m | 53.57 | -61.5% |
| cpu-bound-heavy-throttle | 100m | 500m | 19.22 | -86.2% |

**Throughput loss tracks the CPU cut, but not linearly.** The heavy config
receives 0.5 core where the 2000m/4000m reference run gets up to 4 — an 8x quota
reduction — and loses 7.2x throughput (139.05 / 19.22). The residual is time
stopped at the quota boundary with nothing else runnable to fill it. The ratio
between the two figures is arithmetic on measured throughput; the throttle
percentage itself was not captured in these runs.

### What this file does not report

Phase 4 evidence records **throughput**, not throttle percentages. In all four
files `final_cgroup` is `null` and `throttling` is `null`; only a baseline
`cpu.stat` snapshot was captured. Throttle percentage therefore cannot be
derived from these runs, and earlier revisions of this file that quoted 69.8%,
33.2% and 4.7% were not reproducible from the evidence.

Throttle percentages *were* measured later, together with latency:

- `evidence/phase7/real-apps/nginx-throttling-20261026T191420Z.json` — 0%,
  99.7%, 95.6% and 75.1% for four quota settings on nginx
- `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json` —
  96.0% and 96.9% alongside wake-to-run delays

### Why there is no Phase 2 comparison column

Phase 2 and Phase 4 both ran cpu-bound workloads with 4 stressors
(`dispatching hogs: 4 cpu`), but under different scripts and stress mixes. A
cross-phase throughput ratio would mix the quota change with the workload
change, so all comparisons here are computed within Phase 4.

---

## Experiment Results

### cpu-bound-no-throttle

- Request `2000m`, limit `4000m`, 4 workers, 120 s
- `performance.bogo_ops_per_sec_real` = **139.05**
- Baseline `cpu.stat`: `nr_periods` 69, `nr_throttled` 0, `throttled_usec` 0

### cpu-bound-light-throttle

- Request `1000m`, limit `3000m`, 4 workers, 120 s
- `performance.bogo_ops_per_sec_real` = **108.78** (-21.8%)
- Baseline `cpu.stat`: `nr_periods` 58, `nr_throttled` 57, `throttled_usec` 868246

### cpu-bound-moderate-throttle

- Request `500m`, limit `1500m`, 4 workers, 120 s
- `performance.bogo_ops_per_sec_real` = **53.57** (-61.5%)
- Baseline `cpu.stat`: `nr_periods` 73, `nr_throttled` 72, `throttled_usec` 5385634

### cpu-bound-heavy-throttle

- Request `100m`, limit `500m`, 4 workers, 120 s
- `performance.bogo_ops_per_sec_real` = **19.22** (-86.2%)
- Baseline `cpu.stat`: `nr_periods` 58, `nr_throttled` 57, `throttled_usec` 6650045

The `nr_throttled`/`nr_periods` pairs above come from the baseline snapshot, so
they describe the container's state at the start of the run rather than the run
itself. Treat them as an indication that these containers were already hitting
their quota, not as the run's throttle rate. The throughput figures are the
measurements.

---

## Understanding CPU Throttling

### What is CPU Throttling?

A container that exceeds its CPU limit is stopped by CFS bandwidth control. On a
cgroup v2 host the interface is `cpu.max`, holding a quota and a period in
microseconds:

```
cpu.max = "10000 100000"    # limits.cpu: 100m, 100 ms period
cpu.max = "max 100000"      # no limit
```

Kubernetes sets the quota from `limits.cpu`; kubelet sets the period
(`cpuCFSQuotaPeriod`, default 100 ms). Phase 7.5 verified both forms on a live
node.

### cgroup CPU Statistics

From `/sys/fs/cgroup/cpu.stat`:

- `usage_usec`: CPU time actually consumed
- `throttled_usec`: time the cgroup was throttled
- `nr_periods`: enforcement periods elapsed
- `nr_throttled`: periods in which the cgroup was throttled

### Reading These Counters

`nr_throttled / nr_periods` gives the fraction of periods that were throttled.
`throttled_usec` is a duration, so do not divide it by `usage_usec` and call the
result a percentage of "desired CPU time" — the two counters measure different
things (elapsed time versus consumed CPU time) and the ratio is not bounded by
100% in a meaningful way.

Use the counters to find candidates, then look at the service's own latency.
Phase 7.1 measured a pod throttled in 75.1% of periods with p99 98.87 ms next to
one throttled in 95.6% of periods with p99 67.76 ms, so the percentage does not
rank configurations by harm.

### Impact on Applications

Measured in this repository:

- **Tail latency**: p99 13.98 → 69.43 ms on nginx at a 120m limit, with p50
  unchanged (Phase 7.1)
- **Wake-to-run delay**: worst case 5.5 → 71.7 ms (Phase 7.4)
- **Throughput**: 139.05 → 19.22 ops/sec for a CPU-bound task (this phase)

Documented, not measured here:

- Cold starts and burst handling are slower, because a quota cannot be exceeded
  even when the node is idle

---

## Recommendations

### For Developers

1. **Watch throttling and latency together**
   ```promql
   rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0
   ```

2. **Size requests from measured usage**
   - Measure P95 CPU usage over a representative period
   - `requests.cpu` drives scheduling and `cpu.weight`
   - Leave `limits.cpu` off for latency-sensitive workloads

3. **For CPU-bound batch work, set limit = request**
   - The quota then matches the reservation and no forced wait occurs
   - Phase 4's losses come from limits *below* need

4. **Test with production limits**
   - Measure both p99 latency and throughput before shipping

### For Administrators

1. **Find throttled containers cluster-wide**
   ```promql
   topk(10,
     rate(container_cpu_cfs_throttled_periods_total[5m]) /
     rate(container_cpu_cfs_periods_total[5m]) * 100
   )
   ```

2. **Alert, but judge with latency**
   - Sustained throttling is a signal to investigate, not a severity score
   - Pair any throttle alert with the service's p99 SLO

3. **Review resource quotas**
   - A `limits.cpu` quota pushes every team to set limits they do not need
   - Prefer `requests.cpu` quotas

4. **Cap the offenders, not the victims**
   - Phase 7.3 measured capping four burners at 200m cutting their CPU use by
     75% and improving the co-located victim's p50 by 79%

---

## Next Steps

Phase 5's Prometheus and Grafana material lives in
`evidence/prometheus-integration/` and `phase4/monitoring/`. No Prometheus
instance was deployed for this research, so those files are configuration, not
captured metrics.

Measured follow-up work is in `PHASE7_COMPLETE.md`:

- 7.1 nginx under four quota settings, with p50/p99
- 7.4 live bpftrace of wake-to-run delay
- 7.5 the Kubernetes → cgroup v2 mapping, read from pod cgroups

Run later, outside this phase: the kernel scheduler comparison (Phase 7.6) and
the incident simulation (Phase 7.7), both in `PHASE7_COMPLETE.md`.

---

## References

### Kubernetes CPU Management
- [Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Pod QoS Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/)

### Linux cgroup CPU Controller
- [cgroup v2 CPU controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#cpu)
- [CFS bandwidth control](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)

### Industry Perspectives
- [Stop Using CPU Limits (Robusta)](https://home.robusta.dev/blog/stop-using-cpu-limits)
- [Unthrottled: Fixing CPU Limits (Indeed)](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)
- [The Case for Resource Limits (Kubernetes)](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/)

---

**Experiment data:** `phase4/evidence/`  
**Throttle percentages and latency:** `PHASE7_COMPLETE.md`  
**Developer guide:** `DEVELOPER_GUIDE.md`
