# Kubernetes CPU Limits - Quick Reference Card

**Research:** k8labs  
**Date:** October 2026  
**Status:** Production-Ready

---

## For Developers: Which Pattern?

| If your workload is... | Use Pattern | Example YAML |
|------------------------|-------------|--------------|
| HTTP API, microservice | Request-Only | `requests: cpu: "1000m"` (no limits) |
| Database, cache | Guaranteed | `requests: cpu: "2000m"` + `limits: cpu: "2000m"` |
| Trading, real-time | CPU Pinning | `requests: cpu: "4"` + `limits: cpu: "4"` (integer) |
| Multi-tenant SaaS | Multi-Tenant | `requests: cpu: "500m"` + `limits: cpu: "2000m"` |
| Batch job, ML training | BestEffort | No requests or limits |

---

## Sizing Formula

### Step 1: Monitor for 7 days
```promql
# Get P50 and P95 usage
quantile(0.50, rate(container_cpu_usage_seconds_total[5m]))
quantile(0.95, rate(container_cpu_usage_seconds_total[5m]))
```

### Step 2: Calculate
```
Request-Only Pattern:
  request = P50 × 1.2
  limit = none

Guaranteed Pattern:
  request = P95 × 1.1
  limit = request  (same!)

Multi-Tenant Pattern:
  request = P50 × 1.2
  limit = min(P95, request × 4)
```

### Step 3: Monitor throttling
```promql
# Should be < 5%
rate(container_cpu_cfs_throttled_seconds_total[5m])
```

---

## Research Findings

| Configuration | Throttling % | Performance Impact |
|---------------|--------------|-------------------|
| Appropriate limits | 0% | No impact |
| Light throttle (4.7%) | 4.7% | -4.5% perf |
| Moderate throttle (46.6%) | 46.6% | -39.7% perf |
| **Heavy throttle (69.8%)** | **69.8%** | **-62.7% perf** ⚠️ |

**Key Insight:** Heavy throttling doesn't just reduce CPU - it fragments execution and increases latency dramatically.

---

## Anti-Patterns (Don't Do This!)

| ❌ Anti-Pattern | ✅ Better Approach |
|----------------|-------------------|
| Tiny request, huge limit (100m, 2000m) | Request ≥ 50% of limit |
| Copy-paste from tutorials (100m, 128Mi) | Monitor and size correctly |
| Set and forget (no monitoring) | Alert on throttling > 5% |
| Same values for all pods | Profile each workload |

---

## For Administrators: Kernel Tuning

### When to Tune

| Symptom | Parameter | Direction |
|---------|-----------|-----------|
| High P99 latency | `sched_latency_ns` | Reduce: 6ms → 3ms |
| High P99 latency | `sched_cfs_bandwidth_slice_us` | Reduce: 5ms → 1ms |
| Excessive context switches | `sched_min_granularity_ns` | Increase: 0.75ms → 2ms |
| Real-time requirements | Boot params | Add `isolcpus=2-7` |

### Production Tuning Presets

**Latency-Sensitive (APIs, DBs):**
```bash
echo 3000000 > /proc/sys/kernel/sched_latency_ns
echo 1000 > /proc/sys/kernel/sched_cfs_bandwidth_slice_us
```

**Throughput (Batch, ML):**
```bash
echo 24000000 > /proc/sys/kernel/sched_latency_ns
echo 10000 > /proc/sys/kernel/sched_cfs_bandwidth_slice_us
```

**Balanced (Multi-Tenant):**
```bash
# Use kernel defaults (no tuning)
```

---

## Custom Schedulers (scx)

### Should You Use scx?

✅ **Yes, consider for:**
- Real-time/trading systems (< 10μs latency required)
- HPC with gang scheduling
- Research and experimentation

❌ **No, stick with CFS for:**
- General-purpose Kubernetes (CFS works well)
- Multi-tenant clusters (less proven fairness)
- Compliance-sensitive workloads (new tech)

### Requirements
- Linux kernel **6.12+** with `CONFIG_SCHED_CLASS_EXT=y`
- scx tools installed

---

## Essential Monitoring

### 4 Queries Every Cluster Needs

```promql
# 1. CPU throttling rate (alert if > 5%)
rate(container_cpu_cfs_throttled_seconds_total[5m])

# 2. CPU usage vs request (should be < 80%)
rate(container_cpu_usage_seconds_total[5m]) /
  kube_pod_container_resource_requests{resource="cpu"}

# 3. CPU usage vs limit (should be < 90%)
rate(container_cpu_usage_seconds_total[5m]) /
  kube_pod_container_resource_limits{resource="cpu"}

# 4. Throttled periods (high = bad)
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

---

## QoS Classes Explained

| QoS Class | Requests | Limits | Scheduler Priority | OOM Priority |
|-----------|----------|--------|-------------------|--------------|
| **Guaranteed** | Set | Set (same) | High | Protected |
| **Burstable** | Set | Higher or none | Medium | Medium |
| **BestEffort** | None | None | Low | Killed first |

### When to Use Each

- **Guaranteed:** Databases, critical services (predictable performance)
- **Burstable:** APIs, microservices (flexibility + safety)
- **BestEffort:** Batch jobs only (use idle CPU)

---

## Decision Tree

```
Are you in a multi-tenant cluster?
├─ Yes → Use limits (Multi-Tenant Pattern)
│   └─ request = P50 × 1.2
│   └─ limit = request × 4 (burst factor)
│
└─ No → Do you need guaranteed performance?
    ├─ Yes → Guaranteed Pattern
    │   └─ request = limit = P95 × 1.1
    │
    └─ No → Request-Only Pattern (RECOMMENDED)
        └─ request = P50 × 1.2
        └─ no limit
```

---

## Checklist for Production

### Developers
- [ ] Profile workload for 7 days
- [ ] Calculate P50 and P95 usage
- [ ] Apply sizing formula
- [ ] Set up throttling alerts
- [ ] Document decision in README
- [ ] Review quarterly

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
| `GUIDE.md` | Developer/admin guide (Phase 2) | 515 |
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
