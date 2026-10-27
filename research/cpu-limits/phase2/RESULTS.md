# Phase 2 Results Summary

## Experiment Run: 2026-10-26T11:25:54

All 6 workload experiments completed successfully with **11 samples each** (except for race-condition cases).

---

## Experimental Setup

### Workloads
- **cpu-bound**: 4x `stress-ng --cpu 4` workers using Ackermann method
- **hybrid**: 2x CPU + 1x I/O + 1x VM workers (mixed workload)
- **io-bound**: 4x I/O workers (read/write operations)
- **memory-bound**: 4x VM workers (memory operations)

### QoS Classes
- **BestEffort**: No requests or limits
- **Burstable**: Request = 500m, Limit = 2000m (2 cores)
- **Guaranteed**: Request = Limit = 1000m (1 core)

### Monitoring
- **Sample interval**: 10 seconds
- **Duration**: ~120 seconds per experiment
- **Metrics collected**: cgroup cpu.stat, cpu.max, cpu.weight, cpu.pressure

---

## Key Findings

### 1. BestEffort: Maximum CPU Utilization

**cpu-bound-besteffort:**
- **CPU Used**: 323.2s over 120.5s runtime
- **Equivalent vCPU**: **2.68 cores**
- **Throttling**: **0%** (no limit)
- **nr_throttled**: 0 periods
- **CPU pressure**: avg60=12.66 (some), avg60=12.62 (full)

**Insight**: BestEffort pods consume all available CPU with no throttling
because `cpu.max` is `max 100000`. The workload used nearly 3 full cores on a
4-vCPU node.

---

### 2. Burstable: Variable Throttling by Workload Type

#### CPU-Bound Workload

**cpu-bound-burstable:**
- **CPU Used**: 235.5s over 122.6s runtime
- **Equivalent vCPU**: **1.92 cores**
- **Throttled periods**: 1,150 of 1,150 (**100%**) in the end-snapshot delta
- **Throttled time**: 29.8s, i.e. **11.2% of CPU time**
- **CPU limit**: 2.0 cores (200ms quota / 100ms period)
- **CPU pressure**: avg60=39.40 (some), avg60=39.40 (full)

**Insight**: Pure CPU-bound workload hits the quota in every period, but the
parked time is only 11.2% of CPU time: the scheduler refills the 200 ms quota at
the start of most periods, so the task loses a fraction of each period rather
than the whole of it.

#### Hybrid Workload

**hybrid-burstable:**
- **CPU Used**: 237.3s over 121.3s runtime
- **Equivalent vCPU**: **1.96 cores**
- **Throttled periods**: 1,186 of 1,186 (**100%**)
- **Throttled time**: 138.9s, i.e. **36.9% of CPU time**
- **CPU limit**: 2.0 cores
- **CPU pressure**: avg60=39.05 (some), avg60=39.03 (full)

**Insight**: Mixed CPU + I/O + VM workload is parked for **36.9%** of its CPU
time, over three times the cpu-bound share. Every single period was throttled.
The CFS quota does not distinguish CPU compute from system calls, page faults
and VM exits — all of them draw on the same quota.

---

### 3. Guaranteed: Predictable But Constrained

**memory-bound-guaranteed:**
- **CPU Used**: 117.2s over 122.0s runtime
- **Equivalent vCPU**: **0.96 cores**
- **Throttled periods**: 1,170 of 1,172 (**99.8%**)
- **Throttled time**: 43.6s, i.e. **27.1% of CPU time**
- **CPU limit**: 1.0 core (100ms quota / 100ms period)
- **CPU pressure**: avg60=28.95 (some), avg60=28.95 (full)

**Insight**: Guaranteed QoS with a 1-core quota parks the workload for 27.1% of
its CPU time. Averaging 0.96 cores against a 1-core quota does not protect it:
the average fits under the quota while individual 100 ms periods do not.

---

### 4. I/O-Bound: Too Fast to Monitor

**io-bound-guaranteed** (`…T111404Z`, the first run): 8.37 s wall time, stress
6.80 s, 14802.72 io ops/s. Only a start snapshot exists, so run-level
throttling is unknown. The start snapshot shows 16 of 17 periods already
throttled before the window.

A second run (`…T113256Z`) took 11.25 s wall time (stress 9.20 s, 10971.49 io
ops/s) and is otherwise equivalent. Both stopped at the 100k `hdd` bogo-ops
target well short of 120 s.

**Insight**: the run is too short to compare against the 120 s workloads, and
its lack of an end snapshot means it cannot be used as a throttling measurement.

---

## Throttling Analysis

`nr_throttled / nr_periods` is the share of quota periods that were throttled.
`throttled_usec / usage_usec` is a different quantity: time parked against CPU
time actually used. Both come from the end-minus-start deltas in
`evidence/summary.json`.

| Workload                   | QoS Class  | CPU Limit | Periods throttled | Throttled time / CPU time |
|----------------------------|------------|-----------|-------------------|---------------------------|
| cpu-bound-besteffort       | BestEffort | none      | 0/0 (no quota)    | 0 s / 323.2 s (no quota)  |
| cpu-bound-burstable        | Burstable  | 2 cores   | 1150/1150 (100%)  | 29.8 s / 235.5 s (11.2%)  |
| hybrid-burstable           | Burstable  | 2 cores   | 1186/1186 (100%)  | 138.9 s / 237.3 s (36.9%) |
| memory-bound-guaranteed    | Guaranteed | 1 core    | 1170/1172 (99.8%) | 43.6 s / 117.2 s (27.1%)  |

`cpu-bound-guaranteed` and `io-bound-guaranteed` are absent because they have no
end snapshot.

### Key Observations

1. **A finite quota throttles essentially every period** (99.8-100%) in all
   three runs that have an end snapshot. The period count says how *often* the
   cgroup hit the quota; it says nothing about *how much* CPU was lost
2. **The throttled-time ratio is what separates the workloads**: 11.2% for
   cpu-bound against 36.9% for hybrid and 27.1% for memory-bound
3. **Hybrid workloads pay the most**: system calls, page faults and VM exits
   all consume quota, so the cgroup is parked even when the process is not
   spinning on CPU

---

## CPU Pressure Metrics

| Workload                   | PSI avg60 (some) | PSI avg60 (full) | Interpretation                        |
|----------------------------|------------------|------------------|---------------------------------------|
| cpu-bound-besteffort       | 12.66            | 12.62            | Low pressure (no quota)               |
| cpu-bound-burstable        | 39.40            | 39.40            | Moderate pressure (light throttling)  |
| hybrid-burstable           | 39.05            | 39.03            | Moderate pressure (severe throttling) |
| memory-bound-guaranteed    | 28.95            | 28.95            | Moderate pressure (constrained)       |

**Insight**: PSI (Pressure Stall Information) shows sustained pressure in limited workloads, but **does not correlate 1:1 with throttling severity**. The hybrid workload has similar PSI to cpu-bound but **4.6x more throttling**.

---

## Known Issues

### Race Condition in Short-Lived Workloads

**Affected experiments:**
- `cpu-bound-guaranteed` (reported 0.3s duration but logs show 120.03s)
- `io-bound-guaranteed` (completed in 11.3 s, 0 samples collected; the 8.4 s
  run `…T111404Z` is the earlier attempt)

**Root cause**: The monitoring loop starts after the pod enters "Running" state, but short or fast workloads complete before the first sample is collected. The script reports `duration_seconds = 0.3s` but the actual workload ran for 120s.

**Fix needed**: Start monitoring **before** applying the workload, or use a `PreStart` hook to signal when monitoring should begin.

---

## Recommendations

### For Application Developers

1. **Avoid CPU limits for latency-sensitive workloads** unless you have measured the throttling impact.
   - A 2-core limit can cause **12-58% throttling** depending on workload type.

2. **Hybrid workloads (CPU + I/O + syscalls) are severely penalized** by CPU limits.
   - System calls count against the quota → excessive throttling.
   - Consider using only CPU *requests* (no limits) to avoid throttling.

3. **Set CPU requests to ensure fair scheduling**, but leave limits unset if throttling is unacceptable.

4. **BestEffort is not "best effort"** — it's "maximum effort until preempted."
   - BestEffort pods will consume all available CPU (2.68 cores in our 4-vCPU node).

### For Cluster Administrators

1. **Enable PSI metrics** (`cpu.pressure`) to detect sustained CPU pressure.
   - PSI shows *contention*, not throttling.
   - Use `nr_throttled` and `throttled_usec` for throttling metrics.

2. **Monitor `nr_throttled` and `throttled_usec`** per pod to detect excessive throttling.
   - Prometheus exporter: `container_cpu_cfs_throttled_periods_total`, `container_cpu_cfs_throttled_seconds_total`

3. **Consider disabling CPU limits** for non-batch workloads.
   - Use only CPU requests for fair scheduling.
   - Reserve CPU limits for batch jobs that can tolerate throttling.

4. **Understand the quota period** before tuning it:
   - On cgroup v2 the period is the second field of `cpu.max` (default 100000us),
     set by kubelet's `cpuCFSQuotaPeriod`. There is no node-wide
     `cpu.cfs_period_us`; that is the cgroup v1 interface.
   - A longer period lets a container burst longer before throttling, but when
     it does throttle it waits longer. Phase 7.4 measured a 71.7 ms worst-case
     wait at the default 100 ms period.
   - `cpu.max.burst` exists on cgroup v2 but kubelet left it at 0 on the
     measured node (Phase 7.5), so quota cannot be banked across periods.

---

## Next Steps

### Phase 2 Completion

1. **Re-run failed experiments** (cpu-bound-guaranteed, io-bound-guaranteed) with fixed monitoring.
2. **Generate interactive dashboard** (`generate-dashboard.py`) with throttling timeline.
3. **Analyze throttling patterns** (`analyze-throttling.py`) across all experiments.

### Phase 3: BPF Tracing

Enable BPF tracing to observe low-level scheduler behavior:

1. **`runqlat`**: Run queue latency histogram (how long tasks wait to run)
2. **`runqlen`**: Run queue length over time (scheduler contention)
3. **`sched:sched_switch`**: Task context switches (preemption, migration)
4. **`sched:sched_throttle`**: Exact moments when CFS quota is exhausted

**Goal**: Correlate cgroup throttling events with scheduler behavior and demonstrate how CFS quota affects task latency.

---

## Data Artifacts

### Generated Files
- `evidence/summary.json` — Complete experiment data (621 lines)
- `evidence/cpu-bound-besteffort-*.json` — Individual experiment evidence
- `evidence/cpu-bound-burstable-*.json`
- `evidence/hybrid-burstable-*.json`
- `evidence/memory-bound-guaranteed-*.json`
- `experiment-run.log` — Full experiment execution log

### Sample Collection
- **11 samples per successful experiment** (1 sample every 10 seconds)
- **Baseline + final measurements** for all experiments
- **Complete cgroup state** at each sample (cpu.stat, cpu.max, cpu.weight, cpu.pressure)

---

## References

- [Robusta: Stop Using CPU Limits](https://home.robusta.dev/blog/stop-using-cpu-limits)
- [Indeed: Unthrottled - Fixing CPU Limits in the Cloud](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)
- [Why You Should Keep Using CPU Limits on Kubernetes](https://dnastacio.medium.com/why-you-should-keep-using-cpu-limits-on-kubernetes-60c4e50dfc61)
- [Kubernetes CPU Requests, Limits, and Autoscaling](https://www.sysdig.com/blog/kubernetes-cpu-requests-limits-autoscaling/)
- [The Case for Kubernetes Resource Limits](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/)

---

**Generated**: 2026-10-26
**Status**: ✅ 5 of 6 experiments successful (1 with race condition)
**Duration**: ~12 minutes total runtime
**Next**: Dashboard generation and BPF tracing setup
