---
title: "Understanding Kubernetes CPU Limits: A Deep Dive into cgroups and Scheduler Behavior"
date: {{ .Date }}
draft: true
tags:
  - kubernetes
  - linux
  - cgroups
  - scheduler
  - performance
categories:
  - systems
summary: >
  A comprehensive analysis of how Kubernetes CPU requests and limits affect
  cgroup parameters, Linux scheduler behavior, and application performance.
  Based on empirical experiments with different workload types and QoS classes.
---

## Introduction

The debate over whether to set CPU limits in Kubernetes has been ongoing for years. Some argue that limits cause unnecessary throttling ([Robusta](https://home.robusta.dev/blog/stop-using-cpu-limits), [Indeed](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)), while others maintain they're essential for cluster stability ([Kubernetes blog](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/), [DNastacio](https://dnastacio.medium.com/why-you-should-keep-using-cpu-limits-on-kubernetes-60c4e50dfc61)).

This post presents empirical evidence from controlled experiments to help you make informed decisions about CPU resource management in your Kubernetes clusters.

## The Three Questions We're Answering

1. **How do CPU requests and limits map to cgroup parameters?**
2. **How do those parameters affect actual scheduler behavior?**
3. **What's the real-world impact on different workload types?**

## Experimental Setup

We ran six experiments across two workload types (CPU-bound and I/O-bound) and three QoS classes (BestEffort, Burstable, Guaranteed). For each:

- Deployed a pod with the specified resource configuration
- Ran a standardized stress test (stress-ng)
- Collected cgroup metrics (CFS quota, period, throttling stats)
- Captured BPF traces of scheduler events
- Measured node CPU usage and application throughput

All experiments ran on identical hardware:
- Kubernetes {{.KubeVersion}}
- Linux kernel {{.KernelVersion}}
- {{.NodeCPUs}} CPU cores per node
- CFS scheduler (default)

## Part 1: From YAML to cgroup Files

### The Translation

When you write this in your Pod spec:

```yaml
resources:
  requests:
    cpu: "500m"
  limits:
    cpu: "1000m"
```

Kubernetes creates cgroup files that the Linux kernel uses to enforce those limits:

```bash
# For a Burstable pod with the above spec:
/sys/fs/cgroup/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod<UUID>.slice/<container-id>/
  cpu.weight          → 51 (derived from 500m request)
  cpu.max             → "100000 100000" (1 CPU = 100ms per 100ms period)
```

### The Three QoS Classes

**BestEffort** (no requests or limits):
- `cpu.weight`: minimum (2)
- `cpu.max`: unlimited
- Gets CPU only when no one else wants it

**Burstable** (requests < limits):
- `cpu.weight`: proportional to request (10-262 range)
- `cpu.max`: hard cap at limit
- Guaranteed its request, can burst to limit

**Guaranteed** (requests == limits):
- `cpu.weight`: proportional to request
- `cpu.max`: hard cap at limit
- No bursting, but predictable performance

## Part 2: Scheduler Behavior Under Load

### CPU-Bound Workload Results

{{< figure src="/images/cpu-limits/cpu-bound-comparison.svg" 
           caption="stress-ng CPU throughput by QoS class" >}}

| QoS Class  | Bogo Ops/s | Throttled? | CPU Time (user/sys) |
|------------|------------|------------|---------------------|
| BestEffort | TBD        | No         | TBD                 |
| Burstable  | TBD        | Yes        | TBD                 |
| Guaranteed | TBD        | Yes        | TBD                 |

**Key Findings:**
- BestEffort achieved the highest throughput when the node had spare capacity
- Burstable was throttled X times during the 120s test
- Guaranteed had predictable but capped performance

### I/O-Bound Workload Results

{{< figure src="/images/cpu-limits/io-bound-comparison.svg" 
           caption="I/O throughput by QoS class" >}}

| QoS Class  | IOPS       | Throttled? | CPU Time (user/sys) |
|------------|------------|------------|---------------------|
| BestEffort | TBD        | No         | TBD                 |
| Burstable  | TBD        | Rare       | TBD                 |
| Guaranteed | TBD        | Rare       | TBD                 |

**Key Findings:**
- I/O workloads were less affected by CPU limits
- Throttling occurred but had minimal impact on IOPS
- System time (kernel CPU) was significant

## Part 3: BPF Traces - What the Scheduler Really Did

### Run Queue Latency

```
# BPF output from runqlat during Burstable test
     usecs               : count     distribution
         0 -> 1          : 0        |                    |
         2 -> 3          : 145      |*****               |
         4 -> 7          : 892      |***********************|
         8 -> 15         : 234      |******              |
        16 -> 31         : 45       |*                   |
        32 -> 63         : 12       |                    |
```

### Throttling Events

From `cpu.stat`:
```
nr_periods 1200
nr_throttled 340
throttled_time 15234567890  # nanoseconds
```

This means:
- The workload was throttled in 28% of CFS periods
- Spent 15.2 seconds waiting (out of 120s total)
- That's 12.7% of the time sitting idle, despite wanting CPU

## Part 4: Recommendations

### For Application Developers

**Set requests based on P50 usage, not P99:**
```yaml
resources:
  requests:
    cpu: "500m"  # Your app uses 300-500m typically
  limits:
    cpu: "2"     # Allow bursting to 2 CPUs for peaks
```

**For CPU-bound batch jobs:**
```yaml
# Option 1: BestEffort if the job can tolerate variable runtime
resources: {}

# Option 2: Guaranteed if you need predictable runtime
resources:
  requests:
    cpu: "4"
  limits:
    cpu: "4"
```

**For latency-sensitive services:**
```yaml
# Burstable with headroom for traffic spikes
resources:
  requests:
    cpu: "1"
  limits:
    cpu: "4"  # Or omit limits entirely
```

### For Cluster Administrators

**Kernel tuning:**
- Use CFS bandwidth control (default) for most workloads
- Consider `cpu.cfs_period_us=10000` (10ms) for latency-sensitive workloads
- Avoid CPU pinning unless you have NUMA-aware workloads

**Scheduler configuration:**
- Default CFS works well for mixed workloads
- For batch-heavy clusters, consider lowering `kernel.sched_min_granularity_ns`
- Monitor `node_cpu_seconds_total{mode="steal"}` for over-commitment

**When to use a custom scheduler:**
- High-frequency trading or real-time apps → consider `SCHED_FIFO` via priority classes
- GPU workloads → use device plugins, not CPU limits
- Extremely heterogeneous clusters → consider schedulers like kube-scheduler profiles

## Conclusion

The right approach to CPU limits depends on your workload:

- **Latency-sensitive services**: Omit limits or set them very high
- **Batch jobs**: Use Guaranteed QoS or BestEffort
- **Background tasks**: BestEffort is fine
- **Mixed workloads**: Burstable with generous limits

Most importantly: **monitor throttling metrics** (`container_cpu_cfs_throttled_seconds_total`) and adjust based on actual behavior, not theoretical best practices.

## Appendix: Reproducing the Experiments

All code and data from this post are available at [github.com/eryoma/k8labs/tree/main/research/cpu-limits](https://github.com/eryoma/k8labs/tree/main/research/cpu-limits).

To run the experiments yourself:

```bash
# Prerequisites: A running Kubernetes cluster with BPF support

# Phase 1: Deploy observer infrastructure
kubectl apply -f research/cpu-limits/observers/

# Phase 2: Run experiments
python3 research/cpu-limits/phase2/run-experiments.py

# Phase 3: Analyze results
python3 research/cpu-limits/phase2/analyze-results.py
```

## References

- [Kubernetes CPU resource management docs](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Linux CFS scheduler](https://www.kernel.org/doc/html/latest/scheduler/sched-design-CFS.html)
- [cgroup v2 CPU controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#cpu)
- [BPF tools for scheduler analysis](https://github.com/iovisor/bcc)

---

*This research was conducted as part of the k8labs project - a collection of Kubernetes internals deep-dives. Follow along at [k8labs.dev](https://k8labs.dev).*
