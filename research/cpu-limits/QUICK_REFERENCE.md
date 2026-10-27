# Kubernetes CPU Limits - Quick Reference Card

**Research:** k8labs  
**Date:** October 2026  
**Status:** Measured results; Phase 7.6 (kernel comparison) and 7.7 (incidents) included

---

## For Developers: Which Pattern?

| If your workload is... | Use Pattern | Example YAML |
|------------------------|-------------|--------------|
| HTTP API, microservice | Request-Only | `requests: cpu: "1000m"` (no limits) |
| Database, cache | Guaranteed | `requests: cpu: "2000m"` + `limits: cpu: "2000m"` |
| Trading, real-time | CPU Pinning | `requests: cpu: "4"` + `limits: cpu: "4"` (integer) |
| Multi-tenant SaaS / noisy neighbour | Multi-Tenant | `requests: cpu: "500m"` + `limits: cpu: "2000m"` |
| Batch job, ML training | Guaranteed | `requests: cpu: "2000m"` + `limits: cpu: "2000m"` |

---

## Sizing Formula

### Step 1: Measure usage
```promql
# How much CPU the container actually uses
rate(container_cpu_usage_seconds_total{pod="<pod>"}[5m])

# And the quantiles, over a representative window
quantile(0.95, rate(container_cpu_usage_seconds_total{pod="<pod>"}[5m]))
```

You want p50 **and** p95: the p50 shows typical load, the p95 shows the burst
the request has to cover.

### Step 2: Calculate
```
Request-Only Pattern (latency-sensitive):
  request = measured P95
  limit = none

Guaranteed Pattern (CPU-bound, or predictable critical services):
  request = measured P95
  limit = request        (equal: no forced wait)

Multi-Tenant / noisy-neighbour Pattern:
  request = measured need
  limit = well above the pod's own P99 usage
```

Do not size a request below a sustained load; the request sets `cpu.weight`,
and a low weight loses contention. Phase 7.5 measured 100m → weight 17 and
250m → weight 35.

### Step 3: Monitor throttling **and** latency
```promql
# Throttled share of runnable time
100 * rate(container_cpu_cfs_throttled_seconds_total{pod="<pod>"}[5m])
/ (rate(container_cpu_usage_seconds_total{pod="<pod>"}[5m])
   + rate(container_cpu_cfs_throttled_seconds_total{pod="<pod>"}[5m]))
```

There is no universal safe percentage. Phase 7.1 measured a pod throttled in
99.7% of periods while its median latency stayed flat. Judge with the service's
own p99.

---

## Research Findings

CPU-bound throughput, Phase 4 (`phase4/evidence/throttling-summary.json`):

| Configuration | Request | Limit | Ops/sec | vs baseline |
|---------------|---------|-------|---------|-------------|
| No limit | 2000m | 4000m | 139.05 | baseline |
| Light | 1000m | 3000m | 108.78 | -21.8% |
| Moderate | 500m | 1500m | 53.57 | -61.5% |
| **Heavy** | 100m | 500m | **19.22** | **-86.2%** |

nginx latency, Phase 7.1 (`evidence/phase7/real-apps/`):

| Limit | Throughput | p50 | p99 | Throttled |
|-------|-----------|-----|-----|-----------|
| none | 7216 req/s | 2.04 ms | 13.98 ms | 0% |
| 120m | 3481 req/s | 2.13 ms | 69.43 ms | 99.7% |
| 100m | 2991 req/s | 2.20 ms | 67.76 ms | 95.6% |
| 70m | 2071 req/s | 2.16 ms | 98.87 ms | 75.1% |

**Key Insight:** a quota does not slow execution evenly. It stops tasks at the
quota boundary until the next 100 ms period, so the median survives and the
tail collapses. Phase 7.4 measured the wait directly: worst case 5.5 ms with no
limit against 71.7 ms at 100m.

---

## Anti-Patterns (Don't Do This!)

| ❌ Anti-Pattern | ✅ Better Approach |
|----------------|-------------------|
| A limit below observed peak on a latency-sensitive service (7.1: p99 13.98 → 69.43 ms) | Remove the CPU limit; keep the request |
| Capping the victim to "protect" it from a noisy neighbour | Cap the noisy neighbour (7.3) |
| Reading throttle percentage as a severity score | Use it to find candidates, rank by p99 |
| Copy-paste from tutorials (100m, 128Mi) | Measure p95 and size from it |
| Set and forget (no monitoring) | Alert on throttling and track the service's own p99 |
| Same values for all pods | Profile each workload |

---

## For Administrators: Kernel Tuning

### When to Tune

> No scheduler-parameter tuning was measured in this research. The table below
> is documented guidance; the tail latency this research measured came from the
> CPU quota, not from scheduler timeslices. Fix the quota first.

| Symptom | Parameter | Direction |
|---------|-----------|-----------|
| High P99 latency, quota not binding | `sched_latency_ns` | Reduce: 6ms → 3ms |
| Excessive context switches | `sched_min_granularity_ns` | Increase: 0.75ms → 2ms |
| Real-time requirements | Boot params | Add `isolcpus=2-7` |

### Production Tuning Presets

**Latency-Sensitive (APIs, DBs):**
```bash
# Documented starting point, not a measured optimum
echo 3000000 > /proc/sys/kernel/sched_latency_ns
```

**Throughput (Batch, ML):**
```bash
# Documented starting point, not a measured optimum
echo 24000000 > /proc/sys/kernel/sched_latency_ns
```

**Balanced (Multi-Tenant):**
```bash
# Use kernel defaults (no tuning)
```

---

## Custom Schedulers (scx)

### Should You Use scx?

✅ **Yes, consider for:**
- Real-time/trading systems with a measured scheduler-side latency problem
- HPC with gang scheduling
- Research and experimentation

❌ **No, stick with CFS for:**
- General-purpose Kubernetes (CFS works well)
- Multi-tenant clusters (less proven fairness)
- Compliance-sensitive workloads (new tech)

> Measured in Phase 7.6 on `7.3.0-k8labs`: `scx_lavd --performance` did not
> enforce `cpu.max` at all (12.22 CPU-seconds consumed against a 4.0
> CPU-second quota) until `--enable-cpu-bw`, which the scheduler calls highly
> experimental, was added. It also gave the four-thread group 3.4-4.5 times the
> CPU of a single thread whose cgroup weight was four times higher. Do not
> attach it to a cluster that depends on those two mechanisms.

### Requirements
- Linux kernel **6.12+** with `CONFIG_SCHED_CLASS_EXT=y`
- scx tools installed

---

## Essential Monitoring

### 4 Queries Every Cluster Needs

```promql
# 1. Throttled share of runnable time, in percent. Find candidates; rank by p99.
100 * rate(container_cpu_cfs_throttled_seconds_total[5m])
/ (rate(container_cpu_usage_seconds_total[5m])
   + rate(container_cpu_cfs_throttled_seconds_total[5m]))

# 2. CPU usage vs request
rate(container_cpu_usage_seconds_total[5m]) /
  kube_pod_container_resource_requests{resource="cpu"}

# 3. CPU usage vs limit
rate(container_cpu_usage_seconds_total[5m]) /
  kube_pod_container_resource_limits{resource="cpu"}

# 4. Throttled periods per second
rate(container_cpu_cfs_throttled_periods_total[5m])
```

### Critical Alert

```yaml
- alert: HighCPUThrottling
  expr: |
    rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0.1
  for: 5m
  annotations:
    summary: "CPU throttling detected"
```

The 0.1 (10%) threshold is an operational default, not a measured boundary.
Phase 7.1 measured heavy throttling that cost 5x on p99 and heavy throttling
that cost 2x, so the number alone does not tell you the impact.

---

## QoS Classes Explained

| QoS Class | Requests | Limits | cgroup weight (measured) | OOM Priority |
|-----------|----------|--------|--------------------------|--------------|
| **Guaranteed** | Set | Set (same) | 100 (nice 0) in Phase 7.2 | Protected |
| **Burstable** | Set | Higher or none | 59 (nice 2) for a 500m request | Medium |
| **BestEffort** | None | None | 1 (nice 19) | Killed first |

The weights are real values read from the pod cgroups in Phase 7.2. A single
Guaranteed pod is not automatically weight 100 if the request is small; the
weight follows `requests.cpu`, and Phase 7.5 measured 100m → 17.

### When to Use Each

- **Guaranteed:** Databases, critical services (predictable, no forced wait)
- **Burstable:** APIs, microservices (the normal case)
- **BestEffort:** only where weight 1 is acceptable. Phase 7.2 measured p99
  65.44 ms for BestEffort against 31.28 ms for Burstable under contention.

---

## Decision Tree

```
Is the workload latency-sensitive (has an SLO)?
├─ Yes → Request-Only Pattern (RECOMMENDED)
│   └─ request = measured P95
│   └─ no CPU limit
│
└─ No
    ├─ Is it CPU-bound batch/throughput work?
    │   └─ Guaranteed Pattern: request = limit = measured need
    │
    └─ Is it a known noisy neighbour or untrusted code?
        └─ Multi-Tenant Pattern
            └─ request = measured need
            └─ limit = well above its own P99 usage
```

---

## Checklist for Production

### Developers
- [ ] Measure the workload's CPU usage over a representative period
- [ ] Record P50 and P95 usage
- [ ] Set `requests.cpu` from P95
- [ ] Leave `limits.cpu` off unless the pod needs a hard bound
- [ ] Set up throttling alerts **and** track the service's p99
- [ ] Document the decision next to the manifest
- [ ] Review when traffic patterns change

### Administrators
- [ ] Deploy monitoring (Prometheus + Grafana)
- [ ] Set resource quotas per namespace
- [ ] Document cluster CPU capacity
- [ ] Review throttling alerts weekly
- [ ] Tune kernel only if needed (measure first)
- [ ] Plan for custom scheduler only if CFS proven insufficient

---

## Further Reading

| Document | Purpose | Lines |
|----------|---------|-------|
| `FINAL_SUMMARY.md` | Complete research (5 phases) | 800+ |
| `GUIDE.md` | Developer/admin guide (Phase 2) | full guide |
| `phase4/THROTTLING_ANALYSIS.md` | Throttling impact | 300+ |
| `phase5/admin-guide/KERNEL_CONFIG.md` | CFS tuning | 350+ |
| `phase5/admin-guide/CUSTOM_SCHEDULERS.md` | scx guide | 350+ |
| `phase5/admin-guide/PRODUCTION_PATTERNS.md` | 5 patterns | 600+ |

---

## Support

**Questions?** See `FINAL_SUMMARY.md` for complete research  
**Issues?** Open a GitHub issue  
**Research Team:** k8labs  
**Date:** October 2026

---

**Print this card and keep it at your desk!**
