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

---

## Quantitative Thresholds

### Throttling Regions (from experiment data)

> **(TO BE UPDATED with experimental data after running experiments)**

| Region | Limit Utilization | Throttling Impact | Recommendation |
|--------|------------------|-------------------|----------------|
| Safe | < 60% of limit | < 0.1% periods throttled | Acceptable for all workloads |
| Caution | 60–80% of limit | 0.1–5% periods throttled | Acceptable for batch, avoid for latency-sensitive |
| Dangerous | > 80% of limit | > 5% periods throttled | Redesign: increase limit or reduce request |

These thresholds are preliminary estimates. The region boundaries will be refined based on experimental data from the throttling-baseline, throttling-limits, and throttling-request-limit experiments.

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
