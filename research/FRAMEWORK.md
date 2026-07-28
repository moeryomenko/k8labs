# CPU Parameter Selection Framework for Kubernetes Pods

## Overview

Kubernetes uses a two-tier model for CPU resource management, exposing two knobs that control different aspects of scheduling and execution:

- **CPU requests** — Used by the kubelet scheduler to decide which node has enough capacity. At the cgroup level, requests are translated to `cpu.weight` via the `CpuShares` mechanism (1024 per core). During CPU contention, the kernel distributes time proportionally to these weights. A higher request means a larger share when the CPU is oversubscribed.

- **CPU limits** — Enforced via the Completely Fair Scheduler (CFS) quota mechanism at the cgroup v2 level (`cpu.max`). This sets an absolute ceiling on CPU time per period. When a pod exceeds its quota within a period, it is throttled — forced to wait until the next period. Limits protect the node from runaway pods but introduce throttling latency.

### The Relationship

```
request  ──>  cpu.weight  ──>  proportional share during contention
limit    ──>  cpu.max     ──>  absolute cap via CFS throttling
```

When `request == limit`, the pod gets **Guaranteed QoS**, and the CFS quota is set to match. When `request < limit`, the pod is **Burstable QoS** — it gets a baseline reservation (weight) but can burst up to the limit.

A critical implementation detail: in cgroup v2 with crun as the OCI runtime, when `request == limit`, the CFS quota can be disabled entirely (set to `max`), meaning **no throttling occurs**. This is the key insight for latency-sensitive workloads.

---

## Workload Archetypes

### Type 1: Latency-Sensitive Web Services

- **Goal**: Minimize tail latency (p99)
- **Strategy**: Guaranteed QoS (request == limit)
- **Sizing**: Steady-state CPU + 20% headroom for bursts
- **Example**: request=500m, limit=500m for a service using ~400m
- **Why**: Avoids throttling entirely; CFS quota is disabled when request == limit with cpu.weight = shares
- **Tradeoff**: Lower resource utilization; can leave CPU idle

**Best for**: API servers, web backends, database query handlers, gRPC services, real-time applications.

### Type 2: CPU-Bound Batch Jobs

- **Goal**: Maximize throughput
- **Strategy**: High request, limit = request + burst allowance
- **Sizing**: request = expected average, limit = max sustainable
- **Example**: request=1500m, limit=2000m on a 2-vCPU node
- **Why**: Throttling is acceptable if throughput is the goal
- **Tradeoff**: Can cause CPU starvation for other pods; use with pod priority

**Best for**: Data processing, CI/CD pipelines, batch analytics, video encoding, scientific computing.

### Type 3: Co-located Workloads

- **Goal**: Protect latency-sensitive app from noisy neighbors
- **Strategy**: Latency-sensitive gets Guaranteed QoS; batch gets Burstable
- **Sizing**: LS request = steady-state + margin; batch request = whatever remains
- **Example**: LS: 500m/500m, Batch: 1000m/2000m on a 2-vCPU node (total requests = 1500m)
- **Why**: High cpu.weight for LS ensures preferential scheduling during contention
- **Tradeoff**: Batch job throughput suffers during contention — intentional

**Best for**: Mixed-workload nodes, serving + offline processing, edge nodes with variable load.

### Type 4: HTTP API Server

- **Goal**: Balance throughput and tail latency across variable endpoints
- **Strategy**: Guaranteed QoS for latency-critical endpoints; Burstable for background processing
- **Sizing**: request = P95 CPU across all endpoints, limit = P99 + 20% headroom
- **Example**: request=500m, limit=1000m for a service handling mixed CRUD + compute endpoints
- **Why**: CPU intensity varies by endpoint — authentication is lightweight, report generation is heavy. Request-rate-driven profile means burst allowance is essential.
- **Tradeoff**: Setting limits too low causes throttling during traffic spikes; setting too high wastes resources

**Best for**: REST APIs, GraphQL servers, gRPC endpoints with mixed handler complexity.

### Type 5: Database Workload

- **Goal**: Absorb periodic checkpoint spikes without throttling
- **Strategy**: Guaranteed QoS with headroom for checkpoints
- **Sizing**: request = steady-state query processing, limit = steady-state + checkpoint overhead
- **Example**: request=1000m, limit=2000m for a database with 800m steady-state and 400m checkpoint spikes
- **Why**: Checkpoint spikes are periodic and unavoidable — throttling during a spike delays writes and increases p99 latency
- **Tradeoff**: Over-provisioning sits idle between checkpoints; under-provisioning causes cascading latency under load

**Best for**: OLTP databases, in-memory caches (Redis, Memcached), persistent queues (Kafka, Pulsar).

### Type 6: Combined Stack

- **Goal**: End-to-end latency predictability across a multi-tier service mesh
- **Strategy**: Guaranteed QoS at every tier with coordinated sizing
- **Sizing**: Each tier sized independently, then validated end-to-end with synthetic load
- **Example**: Frontend (200m/500m) → Middleware (500m/1000m) → Database (1000m/2000m)
- **Why**: A throttled middle tier amplifies latency spikes — queued requests compound across tiers. Multi-tier CPU profiles with service communication overhead mean each hop introduces scheduling delay.
- **Tradeoff**: Higher resource cost for predictable performance; requires load testing across all tiers

**Best for**: Microservice architectures with strict SLOs, real-time data pipelines, event-driven systems.

---

## Quantitative Thresholds

### Throttling Regions (from experiment data)

> **(Validated by experiments — see CPU-PARAMETER-GUIDE.md for full analysis)**

| Region | Limit Utilization | Throttling Impact | Recommendation |
|--------|------------------|-------------------|----------------|
| Safe | < 80% of limit | < 0.1% periods throttled | Acceptable for all workloads |
| Caution | 80–95% of limit | 0.1–50% periods throttled | Acceptable for batch, avoid for latency-sensitive |
| Dangerous | > 95% of limit | > 50% periods throttled | Redesign: increase limit or reduce request |

### Experimental Validation Summary

These thresholds are validated by 50+ experiment runs on a 3-node Fedora 44 cluster (Kernel 7.1, CRI-O, crun, Cilium, cgroup v2):

- **Baseline (no limits)**: 0% throttling, ~246M usec CPU over 120s, 2 stress-ng threads at 200% total.
- **100m limit**: 100% throttling, ~13M usec CPU (limit fully saturated), 94.6% of wall time spent throttled.
- **250m limit**: 99.97% throttling, ~32.7M usec CPU, throttled time exceeds wall time (multi-period overlap).
- **500m limit**: 100% throttling, ~65.4M usec CPU, throttled time ratio of 1.13.
- **750m limit**: 99.7% throttling, ~98.5M usec CPU, throttled time drops to 19.6% of wall time.
- **1000m limit**: 99.97% throttling, ~128.4M usec CPU, throttled time ratio of 48.1%.
- **1500m limit**: 93.9% throttling, ~190.9M usec CPU, throttled time ratio of 20.6%.
- **100m/1800m request/limit**: 73.1% throttling, ~225.8M usec CPU (near baseline), throttled time only 3.6%.
- **Light workload (cpu-burner)**: 0% throttling across all 8 request/limit configs.
- **Co-located**: LS pod (200m/500m) zero throttling, batch (1000m/2000m) only 4% throttled with cpu.weight=29 vs 100 for LS and batch respectively.

### crun Conversion: CpuShares → cpu.weight

The kubelet converts milliCPU requests to CpuShares using the formula:

```
CpuShares = (milliCPU / 1000) * 1024
```

The crun OCI runtime then converts CpuShares to the cgroup v2 `cpu.weight` value using a logarithmic formula. The theoretical mapping (to be validated experimentally):

| milliCPU | CpuShares | cpu.weight (approx) |
|----------|-----------|---------------------|
| 100m     | 102       | 5                   |
| 250m     | 256       | 10                  |
| 500m     | 512       | 20                  |
| 1000m    | 1024      | 100                 |
| 2000m    | 2048      | ~500                |

The cpu.weight values are approximate because crun applies a non-linear mapping. Run `make experiment-baseline` and check cgroup data to observe the actual values on your kernel/runtime combination.

---

## Decision Flowchart

```
                    ┌────────────────────────────┐
                    │  Do you know steady-state   │
                    │  CPU usage for this pod?    │
                    └────────────┬───────────────┘
                                 │
                ┌──── YES ───────┴────── NO ────┐
                │                               │
                ▼                               ▼
    ┌─────────────────────┐         ┌─────────────────────┐
    │ Is p99 latency      │         │ Run a profiling     │
    │ critical?           │         │ pod first (see      │
    └──────┬──────────────┘         │ Profiling Method.)  │
           │                       └─────────────────────┘
     ┌─────┴─────┐                           │
     │           │                           ▼
    YES          NO              ┌─────────────────────────┐
     │           │               │ Measure for 24h with   │
     ▼           ▼               │ metrics-server or      │
  ┌────────┐ ┌────────┐         │ Prometheus             │
  │Guaran- │ │Burst-  │         │ Use P95 as request,    │
  │teed    │ │able    │         │ P99 as limit           │
  │QoS:    │ │QoS:    │         └─────────────────────────┘
  │req=    │ │req=    │                     │
  │limit=  │ │steady  │                     ▼
  │steady+ │ │limit=  │         (use derived values below)
  │20%     │ │max     │
  └────────┘ └────────┘
```

---

## Profiling Methodology

Before setting CPU parameters, you need to understand the actual CPU consumption of your workload. Follow this methodology:

### Step 1: Deploy without resource limits

Deploy the workload with no CPU requests or limits. This gives you the natural CPU consumption pattern without any throttling or scheduling constraints.

```yaml
resources: {}
```

### Step 2: Monitor for 24 hours

Use `kubectl top` or Prometheus to record CPU usage over a full business cycle or at least 24 hours under representative production traffic. A shorter window may miss important variance.

```bash
# Live monitoring with kubectl top
kubectl top pod <pod-name> --containers

# Or set up Prometheus with a query like:
# rate(container_cpu_usage_seconds_total{namespace="...", pod="..."}[5m])
```

### Step 3: Record percentiles

Collect CPU usage data and compute the following percentiles:

| Percentile | Use as                  | Rationale                                      |
|------------|-------------------------|------------------------------------------------|
| P50 (median) | Minimum request       | Ensures baseline allocation                    |
| P95        | Recommended request     | Covers 95% of operating points                 |
| P99        | Recommended limit       | Covers near-peak usage (unless Guaranteed QoS) |

### Step 4: Apply the decision flowchart

With steady-state data available, use the decision flowchart above to select the appropriate QoS strategy.

### Step 5: Validate in staging

Before rolling to production, validate the parameters in a staging environment:

1. Run the workload under expected peak load
2. Use `cgroup-watch.sh` to observe throttling:
   ```bash
   research/bin/cgroup-watch.sh <pod-name> --interval 5 --count 60
   ```
3. Check if `nr_throttled` increases over the measurement window
4. If throttling exceeds acceptable thresholds, increase request or limit

---

## Tradeoff Summary

| Strategy                     | Utilization | Latency Stability | Throughput | Throttle Risk |
|------------------------------|-------------|-------------------|------------|---------------|
| No requests/limits           | High        | Poor              | High       | None          |
| BestEffort QoS               | High        | Poor              | Variable   | None          |
| Burstable, low request       | High        | Moderate          | Good       | Low           |
| Burstable, high limit        | Moderate    | Good              | Good       | Moderate      |
| Guaranteed QoS               | Low         | Excellent         | Good       | None          |

### When to choose each strategy

- **No requests/limits**: Dev/test environments only. Risk of resource starvation and noisy-neighbor problems in production.
- **BestEffort QoS** (no requests, no limits): Non-critical batch workloads, CI/CD runners, background tasks that can be preempted.
- **Burstable, low request** (request << limit): When you want to overcommit the node and accept some throttling. Works for workloads with variable CPU usage and tolerance for latency spikes.
- **Burstable, high limit** (request close to limit): When you want near-guaranteed performance but keep the option to burst. Good for workloads with occasional spikes.
- **Guaranteed QoS** (request == limit): Latency-critical production services, database pods, API gateways, and any workload where tail latency matters.

---

## Key Insights

1. **Guaranteed QoS eliminates throttling in cgroup v2 with crun.** When `request == limit`, the CFS quota is effectively disabled. This is the single most important finding for latency-sensitive workloads.

2. **CPU requests protect during contention, not during idle.** Without contention, a pod with 100m request can use all available CPU. The request only matters when multiple pods compete for CPU time.

3. **Throttling is not always bad.** For CPU-bound batch workloads, some throttling may be an acceptable tradeoff for higher overall node utilization. The key is understanding your workload's tolerance.

4. **The difference between request and limit determines throttling behavior.** A large gap means more time spent at the limit, more throttling, and more variable performance. Small gaps mean stable performance but less flexibility.

5. **Always validate with representative load.** Theoretical mappings (like cpu.weight values) depend on the specific kernel version, OCI runtime, and cgroup mode. Run experiments to get real numbers for your environment.

---

## CPU Manager Policy

Kubernetes offers two CPU manager policies that affect how CPU is allocated to pods:

### static vs none

- **none** (default): CPU affinity is managed by the kernel scheduler. Pods can run on any available CPU. Best for general-purpose workloads.
- **static**: The kubelet pins containers in Guaranteed QoS pods to dedicated CPU cores. This reduces context switching and cache thrashing at the cost of reduced flexibility.

### When to Use Each

| Policy | Use Case | Rationale |
|--------|----------|-----------|
| none | General-purpose, batch, burstable workloads | Kernel scheduler can optimize for throughput and utilization |
| static | Latency-critical, Guaranteed QoS pods | Dedicated cores eliminate scheduling jitter and cache interference |

### Combined with Guaranteed QoS

For latency-critical workloads, combine **static** CPU manager with **Guaranteed QoS** (request == limit). This gives the pod both:

1. **Exclusive CPU cores** (no co-scheduling contention)
2. **No CFS throttling** (quota disabled when request == limit with crun)

### Caveat: 2-vCPU Nodes

On 2-vCPU nodes, the static policy offers **limited benefit** because:

- There are only 2 cores to pin — a single Guaranteed QoS pod with 2 CPU cores takes the entire node
- The kernel's EEVDF scheduler on modern kernels efficiently handles fast core switching with low overhead
- Static pinning can paradoxically worsen latency if the pinned core is handling interrupts

**Recommendation**: Use static CPU manager on nodes with 4+ vCPUs for latency-critical workloads. On 2-vCPU nodes, prefer the default `none` policy with Guaranteed QoS.

### Switching and Verification

```bash
# Check current CPU manager policy
kubectl get configmap -n kube-system kubelet-config -o json \
  | jq '.data.kubelet' | jq '.cpuManagerPolicy'

# Switch to static policy (edit kubelet configuration)
# See research/scripts/configure-cpu-manager.sh

# Verify static policy is working
# Pods with Guaranteed QoS should show cpu pinning in cgroup
research/scripts/cgroup-observe.sh <pod-name> | jq '.cpuset.cpus'
```

After switching, verify that Guaranteed QoS pods have `cpuset.cpus` set to dedicated cores and not the full node range.

---

## EEVDF Scheduler Considerations

### How EEVDF Works

The Earliest Eligible Virtual Deadline First (EEVDF) scheduler, introduced in Linux 6.6, replaces the Completely Fair Scheduler (CFS) as the default `SCHED_OTHER` / `SCHED_NORMAL` scheduler. Key concepts:

- **Eligibility ordering**: Tasks are ordered by their virtual runtime (vruntime). The task with the smallest vruntime is eligible to run next.
- **Deadline-based scheduling**: Each task receives a deadline proportional to its weight. The scheduler picks the eligible task with the earliest deadline.
- **Slice allocation**: CPU time is allocated in slices. A task's slice is `base_slice * (weight / sum_of_weights)`. When a task consumes its slice, it gets a new deadline: `now + (slice * weight)`.
- **Preemption**: A task is preempted when a newly-eligible task has an earlier deadline. This provides bounded latency guarantees.

### Interaction with cgroup cpu.weight

EEVDF inherits the cgroup v2 `cpu.weight` mechanism from CFS. The weight controls the proportion of CPU time a cgroup receives:

- `cpu.weight` values range from 1 to 10000 (default 100)
- Higher weight = larger slice = more CPU time
- The crun OCI runtime converts Kubernetes CPU requests to `cpu.weight` (same formula as CFS)

In EEVDF, weight directly determines slice size: a task with `cpu.weight=200` gets twice the slice of a task with `cpu.weight=100`, all else being equal. This is equivalent to the proportional fairness of CFS but with tighter latency bounds.

### CFS Quota (cpu.max) in EEVDF

The cgroup v2 `cpu.max` interface is **unchanged** from CFS. It still enforces an absolute CPU time limit per period:

```
cpu.max = <quota> <period>
```

Example: `50000 100000` = 50ms CPU time per 100ms period = 0.5 CPUs.

EEVDF implements the same throttling mechanism: when a cgroup exceeds its quota within a period, it is throttled until the next period. **All throttling analysis from CFS experiments applies equally to EEVDF.**

Key difference: EEVDF's deadline-based scheduling provides more consistent latency within the quota window, but the throttling boundary itself behaves identically.

### Scheduler Tunables

EEVDF exposes several tunables via `/sys/kernel/debug/sched/`:

| Tunable | Description | Default | Impact |
|---------|-------------|---------|--------|
| `base_slice_ns` | Base time slice for a weight-100 task | 3,000,000 (3ms) | Smaller = more context switches, lower latency; larger = higher throughput |
| `migration_cost_ns` | Cost estimate for task migration between CPUs | 500,000 (500us) | Higher = less migration, potential load imbalance |
| `nr_migrate` | Max tasks to migrate in a single balance pass | 32 | Higher = faster load balancing, more overhead |

These tunables can be adjusted dynamically:

```bash
# Check current values
cat /sys/kernel/debug/sched/base_slice_ns
cat /sys/kernel/debug/sched/migration_cost_ns
cat /sys/kernel/debug/sched/nr_migrate

# Adjust (example: reduce base slice for lower latency)
echo 2000000 > /sys/kernel/debug/sched/base_slice_ns
```

### Reference

For detailed experimental results on EEVDF behavior across workload types, including vruntime trajectories, slice distributions, and deadline drift analysis, see [EEVDF-DEEP-DIVE.md](EEVDF-DEEP-DIVE.md).
