# Kubernetes CPU Requests and Limits: Comprehensive Guide

*Generated: 2026-10-26 22:42:00*

## Executive Summary

This guide demonstrates the real-world impact of Kubernetes CPU requests and limits
on application performance, based on controlled experiments with different workload types
and QoS classes.

### Key Findings

> **Data caveat.** 5 of 14 evidence files contain both a start and an end `cpu.stat` snapshot; only those give a run-level throttle figure. The remaining runs have no end snapshot, so their throttling is reported as unknown. Throughput comes from the stress-ng summary in each run's log and is always measured.

**Run-level throttling where both snapshots exist:**

| Workload (QoS) | `cpu.max` | Throttled periods | Throttled time | Throughput |
|----------------|-----------|-------------------|----------------|------------|
| cpu-bound (besteffort) | `max 100000` | 0/0 (0.0%) | 0.0 s over 323.2 s of CPU | 73.18 cpu ops/s |
| cpu-bound (burstable) | `200000 100000` | 1150/1150 (100.0%) | 22.7 s over 230.1 s of CPU | 51.41 cpu ops/s |
| cpu-bound (burstable) | `200000 100000` | 1167/1178 (99.1%) | 29.8 s over 235.5 s of CPU | 51.27 cpu ops/s |
| hybrid (burstable) | `200000 100000` | 1186/1186 (100.0%) | 138.9 s over 237.3 s of CPU | 382.26 cpu ops/s, 14248.80 io ops/s, 17808 vm ops/s |
| memory-bound (guaranteed) | `100000 100000` | 1170/1172 (99.8%) | 43.6 s over 117.2 s of CPU | 90896.52 vm ops/s |

The workload names, `cpu.max` values, and throughput come from each run's
evidence file (the stress-ng summary in `workload_logs`). The throttled-period
figures are the start-to-end `cpu.stat` deltas. Throughput is not comparable
across workload types: memory-bound "ops" are `--vm` bogo operations, not CPU
operations. Phase 2's cpu-bound runs are also not comparable to Phase 4's,
because Phase 2 ran `--cpu=4` against a 2-CPU quota while Phase 4 used four
workers at much lower quotas.

- **cpu-bound (besteffort)**, `cpu.max` max 100000:
  - 0/0 periods throttled (0.0%), 0.0s throttled over 323.2s of CPU used
  - Performance: 73.18 ops/sec

- **cpu-bound (burstable)**, `cpu.max` 200000 100000:
  - 1150/1150 periods throttled (100.0%), 22.7s throttled over 230.1s of CPU used
  - Performance: 51.41 ops/sec

- **cpu-bound (burstable)**, `cpu.max` 200000 100000:
  - 1167/1178 periods throttled (99.1%), 29.8s throttled over 235.5s of CPU used
  - Performance: 51.27 ops/sec

- **hybrid (burstable)**, `cpu.max` 200000 100000:
  - 1186/1186 periods throttled (100.0%), 138.9s throttled over 237.3s of CPU used
  - Performance: 382.26 ops/sec

- **memory-bound (guaranteed)**, `cpu.max` 100000 100000:
  - 1170/1172 periods throttled (99.8%), 43.6s throttled over 117.2s of CPU used

---

## Table of Contents

1. [Understanding CPU Control in Kubernetes](#understanding-cpu-control)
2. [Experimental Setup](#experimental-setup)
3. [Results by Workload Type](#results-by-workload-type)
4. [Developer Guidelines](#developer-guidelines)
5. [Administrator Guidelines](#administrator-guidelines)
6. [References](#references)

---

## Understanding CPU Control in Kubernetes

### CPU Requests vs Limits

**CPU Requests (`resources.requests.cpu`)**:
- Guarantee: Kubernetes scheduler ensures the node has this much CPU available
- cgroup mapping: Sets `cpu.weight` (priority in CPU scheduler)
- Impact: Determines scheduling placement, not runtime enforcement

**CPU Limits (`resources.limits.cpu`)**:
- Enforcement: Hard cap on CPU usage via CFS quota
- cgroup mapping: Sets `cpu.max` (quota/period)
- Impact: Causes throttling when exceeded, even if CPU is idle

### QoS Classes

| QoS Class | Condition | Behavior |
|-----------|-----------|----------|
| **BestEffort** | No requests or limits | Lowest priority, can use any idle CPU |
| **Burstable** | Requests < Limits | Can burst above request, throttled at limit |
| **Guaranteed** | Requests == Limits | Fixed CPU allocation, hard throttled |

### cgroup v2 CPU Controller

Kubernetes uses cgroup v2 for CPU control:

```
cpu.weight       → CPU shares (request)
cpu.max          → quota/period (limit)
cpu.stat         → usage and throttling counters
```

---

## Experimental Setup

### Workload Types

We tested four workload patterns:

1. **CPU-bound**: Pure computation (stress-ng cpu)
2. **IO-bound**: Disk I/O operations (stress-ng hdd)
3. **Memory-bound**: Memory access patterns (stress-ng vm)
4. **Hybrid**: Mix of CPU, I/O, and memory

### QoS Configurations

Each workload type was tested with different QoS classes:

| Configuration | Workload | Requests | Limits | QoS Class |
|---------------|----------|----------|--------|-----------|
| BestEffort | cpu-bound | - | - | BestEffort |
| Burstable | cpu-bound, hybrid | 500m | 2000m | Burstable |
| Guaranteed | cpu-bound | 2000m | 2000m | Guaranteed |
| Guaranteed | io-bound, memory-bound | 1000m | 1000m | Guaranteed |

Only three of the four workload types have manifests: cpu-bound in all
three QoS classes, memory-bound and io-bound as Guaranteed, and hybrid as
Burstable. The fastest cpu-bound besteffort runs (98.8 and 98.1 ops/s)
lasted 0.3 s and 1.4 s, so they are not comparable in duration to the
120 s runs; treat their rate as a short-sample figure only.

### Metrics Collected

For each experiment:
- cgroup CPU stats (usage, throttling)
- Node CPU stats (/proc/stat)
- Workload performance (stress-ng bogo ops)
- Container logs

---

## Results by Workload Type

### CPU-BOUND Workload

| QoS Class | Duration | cpu.max | Throttled Periods | Performance |
|-----------|----------|---------|-------------------|-------------|
| besteffort | 0.3s | ? | unknown | 98.8 ops/s |
| besteffort | 1.4s | ? | unknown | 98.1 ops/s |
| besteffort | 120.5s | max 100000 | unknown | 73.2 ops/s |
| burstable | 122.2s | 200000 100000 | unknown | 51.3 ops/s |
| burstable | 121.9s | 200000 100000 | 1150/1150 (100.0%) | 51.4 ops/s |
| burstable | 122.6s | 200000 100000 | 1167/1178 (99.1%) | 51.3 ops/s |
| guaranteed | 121.4s | unknown | unknown | 51.5 ops/s |
| guaranteed | 0.3s | ? | unknown | 51.6 ops/s |

#### BESTEFFORT

- Experiment id: `cpu-bound-besteffort-20261026T110703Z`
- No end `cpu.stat` snapshot: run-level throttling unknown

**Performance:**
- Bogo operations: 11854
- Operations/sec: 98.76
- CPU time: 453.9s user + 0.1s system

**Node CPU Usage:**
- User: 0.4%
- System: 0.9%
- Idle: 98.7%

#### BESTEFFORT

- Experiment id: `cpu-bound-besteffort-20261026T112224Z`
- No end `cpu.stat` snapshot: run-level throttling unknown

**Performance:**
- Bogo operations: 11774
- Operations/sec: 98.10
- CPU time: 453.8s user + 0.2s system

**Node CPU Usage:**
- User: 1.0%
- System: 0.0%
- Idle: 99.0%

#### BESTEFFORT

- Experiment id: `cpu-bound-besteffort-20261026T112554Z`
- `cpu.max`: `max 100000`
- Throttled 0 of 0 periods, 0.00s of throttled time

**Performance:**
- Bogo operations: 8783
- Operations/sec: 73.18
- CPU time: 337.0s user + 0.1s system

**Node CPU Usage:**
- User: 98.5%
- System: 0.4%
- Idle: 1.2%

#### BURSTABLE

- Experiment id: `cpu-bound-burstable-20261026T110714Z`
- `cpu.max`: `200000 100000`
- No end `cpu.stat` snapshot: run-level throttling unknown

**Performance:**
- Bogo operations: 6154
- Operations/sec: 51.27
- CPU time: 240.0s user + 0.1s system

**Node CPU Usage:**
- User: 51.5%
- System: 0.2%
- Idle: 48.3%

#### BURSTABLE

- Experiment id: `cpu-bound-burstable-20261026T112235Z`
- `cpu.max`: `200000 100000`
- Throttled 1150 of 1150 periods, 22.67s of throttled time

**Performance:**
- Bogo operations: 6171
- Operations/sec: 51.41
- CPU time: 240.0s user + 0.1s system

**Node CPU Usage:**
- User: 51.2%
- System: 0.3%
- Idle: 48.5%

#### BURSTABLE

- Experiment id: `cpu-bound-burstable-20261026T112810Z`
- `cpu.max`: `200000 100000`
- Throttled 1167 of 1178 periods, 29.78s of throttled time

**Performance:**
- Bogo operations: 6155
- Operations/sec: 51.27
- CPU time: 240.0s user + 0.1s system

**Node CPU Usage:**
- User: 51.0%
- System: 0.3%
- Idle: 48.8%

#### GUARANTEED

- Experiment id: `cpu-bound-guaranteed-20261026T110932Z`
- `cpu.max`: `200000 100000`
- No end `cpu.stat` snapshot: run-level throttling unknown

**Performance:**
- Bogo operations: 6184
- Operations/sec: 51.53
- CPU time: 239.9s user + 0.1s system

**Node CPU Usage:**
- User: 51.5%
- System: 0.2%
- Idle: 48.3%

#### GUARANTEED

- Experiment id: `cpu-bound-guaranteed-20261026T113029Z`
- No end `cpu.stat` snapshot: run-level throttling unknown

**Performance:**
- Bogo operations: 6200
- Operations/sec: 51.65
- CPU time: 239.9s user + 0.1s system

**Node CPU Usage:**
- User: 0.0%
- System: 0.7%
- Idle: 99.3%

---

### HYBRID Workload

| QoS Class | Duration | cpu.max | Throttled Periods | Performance |
|-----------|----------|---------|-------------------|-------------|
| burstable | 121.6s | 200000 100000 | unknown | 394.6 ops/s |
| burstable | 121.3s | 200000 100000 | 1186/1186 (100.0%) | 382.3 ops/s |

#### BURSTABLE

- Experiment id: `hybrid-burstable-20261026T111149Z`
- `cpu.max`: `200000 100000`
- No end `cpu.stat` snapshot: run-level throttling unknown

**Performance:**
- Bogo operations: 47358
- Operations/sec: 394.57
- CPU time: 124.7s user + 0.1s system

**Node CPU Usage:**
- User: 40.6%
- System: 12.4%
- Idle: 47.0%

#### BURSTABLE

- Experiment id: `hybrid-burstable-20261026T113039Z`
- `cpu.max`: `200000 100000`
- Throttled 1186 of 1186 periods, 138.92s of throttled time

**Performance:**
- Bogo operations: 45882
- Operations/sec: 382.26
- CPU time: 120.8s user + 0.1s system

**Node CPU Usage:**
- User: 39.7%
- System: 12.4%
- Idle: 47.6%

---

### IO-BOUND Workload

| QoS Class | Duration | cpu.max | Throttled Periods | Performance |
|-----------|----------|---------|-------------------|-------------|
| guaranteed | 8.4s | 100000 100000 | unknown | N/A |
| guaranteed | 11.3s | 100000 100000 | unknown | N/A |

#### GUARANTEED

- Experiment id: `io-bound-guaranteed-20261026T111404Z`
- `cpu.max`: `100000 100000`
- No end `cpu.stat` snapshot: run-level throttling unknown

**Node CPU Usage:**
- User: 1.1%
- System: 15.9%
- Idle: 82.5%

#### GUARANTEED

- Experiment id: `io-bound-guaranteed-20261026T113256Z`
- `cpu.max`: `100000 100000`
- No end `cpu.stat` snapshot: run-level throttling unknown

**Node CPU Usage:**
- User: 1.1%
- System: 17.9%
- Idle: 80.3%

---

### MEMORY-BOUND Workload

| QoS Class | Duration | cpu.max | Throttled Periods | Performance |
|-----------|----------|---------|-------------------|-------------|
| guaranteed | 122.1s | 100000 100000 | unknown | N/A |
| guaranteed | 122.0s | 100000 100000 | 1170/1172 (99.8%) | N/A |

#### GUARANTEED

- Experiment id: `memory-bound-guaranteed-20261026T111428Z`
- `cpu.max`: `100000 100000`
- No end `cpu.stat` snapshot: run-level throttling unknown

**Node CPU Usage:**
- User: 22.1%
- System: 3.5%
- Idle: 74.4%

#### GUARANTEED

- Experiment id: `memory-bound-guaranteed-20261026T113322Z`
- `cpu.max`: `100000 100000`
- Throttled 1170 of 1172 periods, 43.59s of throttled time

**Node CPU Usage:**
- User: 22.5%
- System: 3.3%
- Idle: 74.2%

---

## Developer Guidelines

### When to Set CPU Limits

**✅ SET LIMITS when:**
- Workload is predictable and bounded
- You need resource guarantees for capacity planning
- Multi-tenancy requires hard isolation
- Cost allocation needs strict enforcement

**❌ AVOID LIMITS when:**
- Workload is bursty (batch jobs, CI/CD)
- Latency-sensitive applications (throttling adds latency)
- Cluster has spare capacity
- You're using HPA/VPA for scaling

### Recommended Patterns

#### Pattern 1: Request-Only (Recommended for most workloads)

```yaml
resources:
  requests:
    cpu: "1000m"
  # No limits - can burst when cluster has capacity
```

**Benefits:**
- No quota, so no throttling periods
- Can use idle CPU
- Better performance for bursty workloads

**Tradeoffs:**
- One pod can consume all CPU
- Harder to predict cluster capacity

#### Pattern 2: Guaranteed (For critical services)

```yaml
resources:
  requests:
    cpu: "2000m"
  limits:
    cpu: "2000m"
```

**Benefits:**
- Predictable performance
- No CPU contention
- QoS class 'Guaranteed'

**Tradeoffs:**
- Wastes CPU when pod is idle
- Gets throttled even if cluster has spare capacity

#### Pattern 3: Burstable (For variable workloads)

```yaml
resources:
  requests:
    cpu: "500m"
  limits:
    cpu: "2000m"
```

**Benefits:**
- Can burst above baseline
- Scheduler uses request for placement

**Tradeoffs:**
- Gets throttled at limit
- May get throttled even with idle CPU

### Sizing Guidelines

1. **Start with monitoring**: Run without limits, measure P95/P99 CPU usage
2. **Set request to P95**: Ensures scheduler finds appropriate nodes
3. **Set limit carefully**: Only if you need hard isolation
4. **Monitor throttling**: Alert on `container_cpu_cfs_throttled_seconds_total`

### Debugging Throttling

Check cgroup stats:

```bash
# Inside container
cat /sys/fs/cgroup/cpu.stat

# Look for:
# nr_throttled - number of times throttled
# throttled_usec - total time throttled (microseconds)
```

Prometheus query:

```promql
rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0
```

---

## Administrator Guidelines

### Cluster-Level CPU Management

#### 1. Kernel Scheduler Configuration

**CFS (Completely Fair Scheduler)** - Default for most workloads

```bash
# Check current scheduler
cat /sys/block/*/queue/scheduler

# Tune CFS parameters (if needed)
sysctl kernel.sched_latency_ns=6000000
sysctl kernel.sched_min_granularity_ns=750000
sysctl kernel.sched_wakeup_granularity_ns=1000000
```

**When to use custom schedulers:**

- **scx_simple**: Low-overhead, single-queue scheduling
- **scx_rusty**: NUMA-aware, better for multi-socket systems
- **scx_lavd**: Latency-sensitive workloads

#### 2. Pod Priority and Preemption

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high-priority
value: 1000
globalDefault: false
description: "High priority workloads"
```

#### 3. Resource Quotas

Enforce namespace-level limits:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: cpu-quota
spec:
  hard:
    requests.cpu: "100"
    limits.cpu: "200"
```

#### 4. LimitRanges

Set defaults for pods without resource specs:

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: cpu-limits
spec:
  limits:
  - default:
      cpu: "500m"
    defaultRequest:
      cpu: "100m"
    type: Container
```

### Monitoring and Alerting

**Key Metrics:**

```promql
# CPU throttling rate
rate(container_cpu_cfs_throttled_seconds_total[5m])

# CPU usage vs request
rate(container_cpu_usage_seconds_total[5m]) /
  on(namespace,pod,container)
  kube_pod_container_resource_requests{resource="cpu"}

# Pods hitting limits
rate(container_cpu_usage_seconds_total[5m]) /
  on(namespace,pod,container)
  kube_pod_container_resource_limits{resource="cpu"}
```

**Alert Rules:**

```yaml
- alert: HighCPUThrottling
  expr: |
    rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0.1
  annotations:
    summary: "Pod {{ $labels.pod }} is being throttled"

- alert: CPURequestTooLow
  expr: |
    rate(container_cpu_usage_seconds_total[5m]) /
    kube_pod_container_resource_requests{resource="cpu"} > 1.2
  annotations:
    summary: "Pod {{ $labels.pod }} using more than 120% of request"
```

---

## References

### Research and Articles

1. [Stop Using CPU Limits - Robusta](https://home.robusta.dev/blog/stop-using-cpu-limits)
2. [Unthrottled: Fixing CPU Limits in the Cloud - Indeed](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)
3. [Why You Should Keep Using CPU Limits - Medium](https://dnastacio.medium.com/why-you-should-keep-using-cpu-limits-on-kubernetes-60c4e50dfc61)
4. [Kubernetes CPU Requests, Limits, and Autoscaling - Sysdig](https://www.sysdig.com/blog/kubernetes-cpu-requests-limits-autoscaling)
5. [The Case for Kubernetes Resource Limits - Kubernetes Blog](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/)

### Official Documentation

- [Kubernetes Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Pod QoS Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/)
- [cgroup v2 Documentation](https://www.kernel.org/doc/Documentation/cgroup-v2.txt)

### Tools

- stress-ng: CPU/memory/IO stress testing
- bpftrace: Kernel tracing for scheduler analysis
- Prometheus + node_exporter: Metrics collection

---

*Experiments conducted: 14 | Date: 2026-10-26*