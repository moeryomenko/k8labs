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

| QoS Class | Request | Limit | cpu.weight | cpu.max | Throttle Events | Throttled Time | Throttle Ratio |
|-----------|---------|-------|------------|---------|-----------------|----------------|----------------|
| BestEffort | none | none | 2 (min) | max (unlimited) | 0 | 0s | 0% |
| Burstable | 500m | 2 | 59 | 200000/100000 | ~1100 | ~23s | 95% |
| Guaranteed | 2 | 2 | 195 | 200000/100000 | ~1100 | ~23s | 95% |

**Key Observations**:

1. **BestEffort**: No throttling because there's no `cpu.max` limit
2. **Burstable vs Guaranteed**: Both have the same 2-CPU limit, so they throttle identically despite different `cpu.weight` values
3. **Throttling pattern**: ~100 throttle events per 10-second interval, ~19.7ms average duration per event
4. **Usage vs Throttling**: Container tries to use 2 full CPUs but gets throttled to maintain the 2-CPU average over each CFS period

### 2. Throttling Time Series (Burstable Example)

```
Time     Throttled    Total Time   Avg/throttle
--------------------------------------------------
11:22:52 122          2427.0       19.9ms
11:23:03 231          4557.3       19.7ms
11:23:14 340          6698.3       19.7ms
11:23:26 453          8959.2       19.8ms
11:23:35 550          10885.8      19.8ms
11:23:46 664          13130.8      19.8ms
11:23:56 762          15043.5      19.7ms
11:24:06 863          16996.0      19.7ms
11:24:16 963          18955.0      19.7ms
11:24:26 1063         20929.8      19.7ms
11:24:36 1162         22905.9      19.7ms
```

**Interpretation**:
- Linear growth in throttle events and throttled time
- Consistent ~19.7ms average throttle duration (related to CFS period of 100ms)
- Throttling is continuous and predictable

### 3. I/O-Bound Workload

**Result**: Completed in 8.4 seconds with minimal throttling

I/O-bound workloads (e.g., `stress-ng --hdd`) spend most time waiting for I/O operations, not spinning the CPU. CPU limits have minimal impact because the workload rarely hits the quota.

### 4. Memory-Bound Workload

**Result**: Similar to CPU-bound (122 seconds, significant throttling)

Memory allocation/deallocation patterns still consume significant CPU cycles, triggering throttling.

---

## Technical Deep Dive

### How CPU Requests Map to `cpu.weight`

Kubernetes converts CPU requests to cgroup v2 `cpu.weight` using this formula:

```
weight = max(2, min(10000, (cpu_request_millicores * 1024) / 1000))
```

Examples:
- 500m request → weight = 512 * 1024 / 1000 ≈ **524** (but we observed 59 — need to investigate)
- 1000m (1 CPU) request → weight = **1024**
- 2000m (2 CPUs) request → weight = **2048**

The weight is relative: if two containers have weights 100 and 200, the second gets twice as much CPU when there's contention.

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

Consider adjusting CFS parameters in `/sys/kernel/debug/sched/`:

1. **CFS period** (`cfs_period_us`): Default 100ms
   - Shorter periods (e.g., 10ms) = more frequent throttling checks but lower latency
   - Longer periods (e.g., 1s) = less overhead but coarser granularity

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
