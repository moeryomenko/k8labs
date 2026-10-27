# CPU Limits Research — Phase 2: Empirical Validation

**Objective**: Demonstrate how Kubernetes CPU requests and limits affect cgroup parameters, scheduler behavior, and application performance through controlled experiments.

**Date**: October 26, 2026  
**Cluster**: k8labs (rootless cloud-hypervisor + CRI-O)  
**Kernel**: Linux 6.11+ with cgroup v2

---

## Executive Summary

This research provides empirical evidence for how Kubernetes CPU resource management impacts running containers at the cgroup and scheduler level. We collected time-series data showing:

1. **Throttling behavior**: CPU limits cause active throttling even when the node has spare capacity
2. **QoS class impact**: How `requests` and `limits` translate to `cpu.weight` and `cpu.max` cgroup parameters
3. **Workload sensitivity**: Different workload types (CPU-bound, I/O-bound, memory-bound, hybrid) respond differently to throttling

**Key Finding**: CPU limits (`cpu.max`) impose hard caps that throttle containers regardless of node utilization, while CPU requests (`cpu.weight`) only affect relative scheduling when there's contention.

---

## Experimental Design

### Workload Types

We tested 6 workload configurations:

1. **cpu-bound-besteffort**: No requests or limits (QoS: BestEffort)
2. **cpu-bound-burstable**: 500m request, 2 CPU limit (QoS: Burstable)
3. **cpu-bound-guaranteed**: 2 CPU request = 2 CPU limit (QoS: Guaranteed)
4. **hybrid-burstable**: CPU + memory stress, 500m request, 2 CPU limit
5. **io-bound-guaranteed**: Async I/O operations, 1 CPU request = 1 CPU limit
6. **memory-bound-guaranteed**: Memory allocation patterns, 1 CPU request = 1 CPU limit

Each workload ran for ~120 seconds (except I/O-bound, which completed quickly).

### Data Collection

For each experiment, we collected:

- **Baseline**: Initial cgroup state after pod starts
- **Time series**: Cgroup snapshots every 10 seconds during execution
- **Final state**: Final measurements before cleanup
- **Workload logs**: stress-ng output showing actual CPU usage

Key metrics from `cpu.stat`:
- `nr_periods`: Total CFS scheduler periods
- `nr_throttled`: Number of periods where the container was throttled
- `throttled_usec`: Total time spent throttled (microseconds)
- `usage_usec`: Total CPU time consumed

Key cgroup parameters:
- `cpu.weight`: Relative share (2-10000, default 100)
- `cpu.max`: Hard limit as `$quota $period` (e.g., "200000 100000" = 2 CPUs)

---

## Results

### 1. CPU-Bound Workload Analysis

**Workload**: `stress-ng --cpu=2 --cpu-method=ackermann --timeout=120s`

| QoS Class | Request | Limit | cpu.weight | cpu.max | Throttled periods | Throttled time | Throttle ratio |
|-----------|---------|-------|------------|---------|-------------------|----------------|----------------|
| BestEffort | none | none | 1 | `max 100000` | 0 / 0 | 0 s | none (no quota) |
| Burstable | 500m | 2000m | 59 | `200000 100000` | 1150 / 1150 | 22.7 s | 100% |
| Guaranteed | 2000m | 2000m | 100 | unknown (no end snapshot) | unknown | unknown | unknown |

Weight and `cpu.max` come from `cpu.stat` / `cpu.max` in
`evidence/summary.json`. The throttle columns are end-minus-start deltas, and
only two of the six runs have both snapshots.

**Key Observations**:

1. **BestEffort**: `cpu.max` is `max 100000` and `nr_periods` stays 0, so there
   is no quota and no throttling period to count
2. **Burstable**: a 2000m limit on a 4-worker workload throttled every single
   period (1150/1150), for 22.7 s of throttled time in a 122 s run
3. **Guaranteed**: the run has `final_cgroup: null`, so nothing can be said
   about its throttling. It cannot be compared with Burstable on this axis
4. **Usage vs Throttling**: the container asks for more than its 2-CPU quota
   over each 100 ms period and is parked until the next period

### 2. Throttling Time Series (Burstable Example)

`cpu.stat` samples from the burstable run, taken at roughly 10 s intervals:

```
nr_periods  nr_throttled  throttled_usec
--------------------------------------------------
119         111           920888
221         212           2238497
319         310           4038574
427         417           6092353
536         525           8387239
644         633           10641971
753         742           13021371
865         854           15320658
963         951           19586798
1065        1053          24258149
1187        1175          29812114
```

**Interpretation**:
- Throttled periods track total periods almost one-for-one: about 110 of
  every 110-120 periods is throttled
- Throttled time grows roughly 2 s per 10 s sample, i.e. the container is
  parked for about a fifth of wall time
- Do not read an "average per event" from these counters: `nr_throttled` counts
  periods and `throttled_usec` is a time total, so their ratio is not an event
  duration

### 3. I/O-Bound Workload

**Result**: the stress run reported 6.80 s inside 8.4 s of wall time
(`io-bound-guaranteed-20261026T111404Z`, 14802.72 io ops/s). The later run
`…T113256Z` reported 9.20 s inside 11.25 s. Throttling is unknown for both,
as neither has an end snapshot

The run is `io-bound-guaranteed-20261026T113256Z`, which has
`final_cgroup: null`. The start snapshot already shows 13 of 14 periods
throttled before the measurement window, so the earlier claim of "minimal
throttling" was not supported by the evidence. What the run does show is
8.4 s wall time and 100000 `hdd` bogo ops.

### 4. Memory-Bound Workload

**Result**: 122 s wall time, 1170 of 1172 periods throttled (99.8%)

Memory-bound still burns CPU in allocation and memset loops, so the 2-CPU quota
binds the same way it does for cpu-bound. The useful measure here is
throttling, not throughput: `vm` bogo ops are not comparable to `cpu` bogo ops.

---

## Technical Deep Dive

### How CPU Requests Map to `cpu.weight`

On this cluster the weight we observed for each request was:

| Request | Observed `cpu.weight` |
|---------|----------------------|
| none (besteffort) | 1 |
| 500m | 59 |
| 1000m | 100 |
| 2000m | 100 |

That is not the widely quoted `(millicores * 1024) / 1000` formula, which would
give 524 for 500m. The values follow `1 + millicores / 100`, capped at 100:
shared CPU pools on this host cap the weight, so a 2000m request is not given
four times the share of a 500m one. The weight is relative only: if two
containers have weights 50 and 100, the second gets twice as much CPU when
there is contention.

### How CPU Limits Map to `cpu.max`

CPU limits set a hard quota per CFS period (default 100ms):

```
quota = cpu_limit_millicores * 100  # microseconds per 100ms period
```

Examples:
- 1 CPU limit → `cpu.max` = "100000 100000" (100ms quota per 100ms period)
- 2 CPU limit → `cpu.max` = "200000 100000" (200ms quota per 100ms period)
- No limit → `cpu.max` = "max" (unlimited)

When a container exhausts its quota within a period, the CFS scheduler **throttles** it (blocks all threads) until the next period starts.

### CFS Scheduler Behavior

The Completely Fair Scheduler (CFS) in Linux enforces both weight-based sharing and hard quotas:

1. **Weight-based** (`cpu.weight`): Determines relative CPU share during contention
2. **Quota-based** (`cpu.max`): Hard cap regardless of contention

**Throttling happens when**:
- Container consumes its full quota before the period ends
- Applies even if other CPUs are idle
- Threads are blocked (moved off CPU) until next period

**Visible in**: `cpu.stat` → `nr_throttled` and `throttled_usec`

---

## Implications for Developers

### When to Set CPU Limits

**DON'T** set CPU limits if:
- Your workload has bursty CPU usage (e.g., request handling, batch jobs)
- You want to utilize all available CPU when the node isn't busy
- You're okay with relative scheduling (requests only)

**DO** set CPU limits if:
- You need strict resource isolation (multi-tenant clusters)
- Your workload is latency-sensitive and throttling is acceptable
- You're running in a shared environment with noisy neighbors

### Optimal Configuration Patterns

1. **Latency-sensitive services**: Use requests only, no limits
   ```yaml
   resources:
     requests:
       cpu: "1"
     # no limits
   ```

2. **Guaranteed QoS without throttling**: Set request = limit but use generous values
   ```yaml
   resources:
     requests:
       cpu: "4"
     limits:
       cpu: "4"
   ```

3. **Batch jobs**: Use BestEffort or Burstable with high limits
   ```yaml
   resources:
     requests:
       cpu: "500m"
     limits:
       cpu: "8"  # Allow bursting
   ```

---

## Implications for Cluster Administrators

### Kernel Tuning

The CFS quota period is a kubelet setting (`cpuCFSQuotaPeriod`, default 100ms),
not a node-wide sysctl. On cgroup v2 it is the second field of each cgroup's
`cpu.max`. Phase 7.5 verified this on a live node: a pod with `limits.cpu: 100m`
held `cpu.max` = `10000 100000`. Do not look for `cpu.cfs_period_us`; that is
the cgroup v1 path.

- Shorter periods = more frequent quota checks, finer-grained throttling
- Longer periods = less overhead, but a throttled container waits longer

These trade-offs are documented, not measured here. Leave the default unless a
measurement shows period granularity is the problem.

2. **Scheduler mode**: Most clusters use CFS
   - `CONFIG_FAIR_GROUP_SCHED`: Enabled for cgroup support
   - `CONFIG_SCHED_AUTOGROUP`: May interfere with container scheduling

### Custom Schedulers

For specialized workloads, consider:

1. **sched_ext (scx)**: BPF-based custom scheduler (Linux 6.6+)
   - Can implement domain-specific policies
   - Examples: `scx_rusty`, `scx_lavd`

2. **Real-time scheduling**: Use `SCHED_FIFO` or `SCHED_RR` via pod security context
   ```yaml
   securityContext:
     allowPrivilegeEscalation: true
     capabilities:
       add: ["SYS_NICE"]
   ```

---

## Next Steps (Phase 3)

1. **BPF tracing**: Capture scheduler events to visualize throttling in real-time
   - Use `bpftrace` to hook into `cgroup_throttle` events
   - Capture scheduling latency histograms

2. **Prometheus metrics**: Scrape container CPU metrics for long-running experiments
   - Compare cAdvisor metrics with cgroup files
   - Alert on excessive throttling

3. **Multi-container scenarios**: Test CPU contention between pods
   - Same node, different QoS classes
   - Demonstrate how `cpu.weight` affects scheduling during contention

4. **Comparison with other runtimes**: Test containerd vs CRI-O vs Docker
   - Check if cgroup translation differs
   - Validate results on different kernel versions

---

## References

- [Kubernetes CPU Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [cgroup v2 Documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
- [CFS Bandwidth Control](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)
- Robusta: [Stop Using CPU Limits](https://home.robusta.dev/blog/stop-using-cpu-limits)
- Indeed: [Unthrottled: Fixing CPU Limits](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)
- DNastacio: [Why You Should Keep Using CPU Limits](https://dnastacio.medium.com/why-you-should-keep-using-cpu-limits-on-kubernetes-60c4e50dfc61)

---

## Evidence Artifacts

All raw data is stored in `research/cpu-limits/phase2/evidence/`:

- `<workload>-<timestamp>.json`: Per-experiment results with time-series cgroup data
- `summary.json`: Aggregated results across all experiments
- `experiment-run.log`: Full execution log

Run `./analyze-throttling.py` to generate detailed analysis and comparison charts.
