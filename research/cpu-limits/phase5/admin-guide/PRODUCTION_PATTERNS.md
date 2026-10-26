# Production Deployment Patterns for Kubernetes CPU Resources

**Audience:** Kubernetes administrators and platform engineers  
**Objective:** Real-world patterns for setting CPU requests and limits based on workload type

---

## Table of Contents

1. [Pattern Selection Guide](#pattern-selection-guide)
2. [Pattern 1: Request-Only (Recommended)](#pattern-1-request-only-recommended)
3. [Pattern 2: Guaranteed Resources](#pattern-2-guaranteed-resources)
4. [Pattern 3: CPU Pinning (Ultra-Low Latency)](#pattern-3-cpu-pinning-ultra-low-latency)
5. [Pattern 4: Multi-Tenant with Limits](#pattern-4-multi-tenant-with-limits)
6. [Pattern 5: Batch/Background Jobs](#pattern-5-batchbackground-jobs)
7. [Anti-Patterns to Avoid](#anti-patterns-to-avoid)

---

## Pattern Selection Guide

**Decision Tree:**

```
Is latency-sensitive (API, DB, real-time)?
├─ YES → Pattern 1 (Request-Only) or Pattern 3 (CPU Pinning)
└─ NO
    ├─ Critical service (payments, auth)?
    │   └─ YES → Pattern 2 (Guaranteed)
    └─ Multi-tenant with noisy neighbors?
        ├─ YES → Pattern 4 (Multi-Tenant)
        └─ NO → Pattern 5 (Batch/Background)
```

**Quick Reference:**

| Workload Type | Pattern | Requests | Limits | Rationale |
|---------------|---------|----------|--------|-----------|
| HTTP API | Request-Only | ✅ | ❌ | Avoid throttling, allow bursting |
| Database | Guaranteed | ✅ | ✅ (same) | Predictable performance |
| Trading/Real-time | CPU Pinning | ✅ | ✅ (same) | Minimal scheduler overhead |
| Multi-tenant apps | Multi-Tenant | ✅ | ✅ (2-4× req) | Isolation, noisy neighbor protection |
| Batch/ML jobs | BestEffort/Burstable | ❌ or ✅ | ❌ | Use idle CPU, low priority |

---

## Pattern 1: Request-Only (Recommended)

### Overview

Set CPU requests but **no limits**. Pods get guaranteed minimum CPU but can burst to use idle capacity.

**From our research:**
- Phase 2: Appropriate limits → 0% throttling
- Phase 4: Aggressive limits → 69.8% throttling, 62.7% perf loss
- **Conclusion:** If you don't need hard isolation, skip limits

### Configuration

```yaml
resources:
  requests:
    cpu: "1000m"  # Guaranteed minimum
    memory: "512Mi"
  # No limits - can burst
```

### When to Use

✅ **Use for:**
- HTTP APIs and microservices
- Databases (if no noisy neighbors)
- Any latency-sensitive workload
- Clusters with good monitoring

❌ **Don't use for:**
- Multi-tenant platforms (need isolation)
- Untrusted workloads (need containment)
- Strict cost control requirements

### Pros and Cons

**Pros:**
- ✅ No CPU throttling
- ✅ Can burst during traffic spikes
- ✅ Better P99 latency
- ✅ Simpler to configure (size request to P50, not P99)

**Cons:**
- ❌ No hard isolation (noisy neighbor possible)
- ❌ Node can be overcommitted (if many pods burst)
- ❌ Harder to predict node capacity

### Sizing Guide

```bash
# Request = P50 CPU usage + 20% buffer
# Example: P50 = 800m → Request = 1000m

# Monitor over 7 days
kubectl top pod <pod-name> --containers

# P50 from Prometheus
quantile(0.5, rate(container_cpu_usage_seconds_total[5m]))

# Add 20% buffer
request = P50 * 1.2
```

### Example: HTTP API

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-server
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: api
        image: my-api:v1
        resources:
          requests:
            cpu: "1000m"      # P50 usage + 20%
            memory: "512Mi"
          # No CPU limit
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 10
```

### Monitoring

```promql
# Ensure no throttling (should be 0)
rate(container_cpu_cfs_throttled_seconds_total{pod="api-server-*"}[5m])

# CPU usage vs request
container_cpu_usage_seconds_total / 
  kube_pod_container_resource_requests_cpu_cores

# Alert if usage > 80% of request consistently
```

---

## Pattern 2: Guaranteed Resources

### Overview

Set CPU requests **equal to** limits. Pods get QoS class `Guaranteed` and predictable CPU allocation.

**From our research:**
- Phase 2: Guaranteed QoS had lowest variance
- Suitable for critical services requiring consistency

### Configuration

```yaml
resources:
  requests:
    cpu: "2000m"
    memory: "2Gi"
  limits:
    cpu: "2000m"      # Same as request
    memory: "2Gi"     # Same as request
```

### When to Use

✅ **Use for:**
- Databases (PostgreSQL, MySQL, Redis)
- Critical services (auth, payments, billing)
- Stateful workloads
- Services with strict SLAs

❌ **Don't use for:**
- Bursty workloads (can't burst past limit)
- Cost-sensitive batch jobs (wastes idle CPU)

### Pros and Cons

**Pros:**
- ✅ QoS `Guaranteed` (last to be evicted)
- ✅ Predictable performance
- ✅ No CPU throttling if sized correctly
- ✅ OOM killer protection (if memory also guaranteed)

**Cons:**
- ❌ Cannot burst past limit
- ❌ Requires accurate sizing (P95, not P50)
- ❌ Lower node utilization (reserved capacity)

### Sizing Guide

```bash
# Request/Limit = P95 CPU usage + 10% buffer
# Example: P95 = 1800m → Request/Limit = 2000m

# P95 from Prometheus
quantile(0.95, rate(container_cpu_usage_seconds_total[5m]))

# Add 10% buffer (smaller than request-only since no bursting)
limit = P95 * 1.1
request = limit  # Same!
```

### Example: PostgreSQL Database

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
spec:
  serviceName: postgres
  replicas: 1
  template:
    spec:
      containers:
      - name: postgres
        image: postgres:16
        resources:
          requests:
            cpu: "2000m"
            memory: "4Gi"
          limits:
            cpu: "2000m"      # Same
            memory: "4Gi"     # Same
        volumeMounts:
        - name: data
          mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 100Gi
```

### Monitoring

```promql
# QoS class (should be Guaranteed)
kube_pod_status_qos_class{qos_class="guaranteed"}

# CPU usage should stay below limit
container_cpu_usage_seconds_total / 
  kube_pod_container_resource_limits_cpu_cores < 0.95

# Alert if approaching limit (>90%)
```

---

## Pattern 3: CPU Pinning (Ultra-Low Latency)

### Overview

Dedicate exclusive CPUs to pods using CPU Manager. Minimizes context switches and scheduler overhead.

**From our research:**
- Reduces scheduling latency by 10-100× for real-time workloads
- Requires kernel tuning (`isolcpus`)

### Configuration

```yaml
# kubelet config
cpuManagerPolicy: static
reservedSystemCPUs: "0,1"  # Reserve for system daemons

---
# Pod manifest
resources:
  requests:
    cpu: "4"              # Must be integer
    memory: "8Gi"
  limits:
    cpu: "4"              # Same (required for static policy)
    memory: "8Gi"
```

### When to Use

✅ **Use for:**
- Trading systems (order execution)
- Real-time audio/video processing
- High-frequency data processing
- Soft real-time requirements (< 10μs latency)

❌ **Don't use for:**
- Most web applications (overkill)
- Batch workloads (waste of resources)
- Clusters with high pod density

### Prerequisites

**Node configuration:**
```bash
# Boot parameter (isolate CPUs 2-15)
GRUB_CMDLINE_LINUX="isolcpus=2-15 nohz_full=2-15"

# kubelet flags
--cpu-manager-policy=static
--reserved-cpus=0,1
--system-reserved=cpu=1000m,memory=2Gi
--kube-reserved=cpu=1000m,memory=2Gi

# Restart kubelet
systemctl restart kubelet
```

**Node labels:**
```bash
kubectl label node worker-1 node.kubernetes.io/isolated=true
```

### Pros and Cons

**Pros:**
- ✅ Exclusive CPU access (no sharing)
- ✅ Minimal context switches
- ✅ Predictable latency (< 10μs jitter)
- ✅ No noisy neighbors

**Cons:**
- ❌ Low node utilization (reserved CPUs idle when pod not scheduled)
- ❌ Complex setup (kernel params, kubelet config)
- ❌ Only works for Guaranteed QoS with integer CPUs
- ❌ Not portable across clusters

### Example: Trading Application

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: trading-engine
spec:
  replicas: 1
  template:
    spec:
      nodeSelector:
        node.kubernetes.io/isolated: "true"
      containers:
      - name: engine
        image: trading-engine:v1
        resources:
          requests:
            cpu: "8"          # Integer CPUs
            memory: "16Gi"
          limits:
            cpu: "8"          # Same
            memory: "16Gi"
        env:
        - name: GOMAXPROCS
          value: "8"          # Match CPU count
```

### Monitoring

```bash
# Verify CPU pinning
kubectl exec trading-engine-* -- taskset -cp 1
# Output: pid 1's current affinity list: 2,3,4,5,6,7,8,9

# Check context switches (should be low)
kubectl exec trading-engine-* -- cat /proc/1/status | grep voluntary_ctxt_switches
```

---

## Pattern 4: Multi-Tenant with Limits

### Overview

Set moderate limits (2-4× requests) to allow bursting while protecting against noisy neighbors.

**From our research:**
- Phase 4: 4× limit (request=500m, limit=2000m) had only 4.7% throttling
- Good balance between isolation and flexibility

### Configuration

```yaml
resources:
  requests:
    cpu: "500m"       # Guaranteed minimum
    memory: "512Mi"
  limits:
    cpu: "2000m"      # 4× request (burst factor)
    memory: "1Gi"     # 2× request
```

### When to Use

✅ **Use for:**
- SaaS platforms (multiple customers)
- Shared development clusters
- Untrusted workloads
- Cost control requirements

❌ **Don't use for:**
- Single-tenant clusters (unnecessary limits)
- Ultra-latency-sensitive workloads (throttling risk)

### Burst Factor Guide

```
Burst Factor = Limit / Request

1× (no burst) → Pattern 2 (Guaranteed)
2× (tight)    → Occasional bursts, some throttling possible
4× (balanced) → Good for most multi-tenant (Phase 4: 4.7% throttling)
8× (loose)    → Approaching request-only, minimal isolation
```

### Sizing Guide

```bash
# Request = P50 usage
# Limit = P95 usage (or 2-4× request, whichever is smaller)

request = P50_cpu
limit = min(P95_cpu, request * 4)
```

### Example: Multi-Tenant SaaS

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tenant-app
  namespace: tenant-abc
spec:
  replicas: 2
  template:
    spec:
      containers:
      - name: app
        image: saas-app:v1
        resources:
          requests:
            cpu: "500m"
            memory: "512Mi"
          limits:
            cpu: "2000m"      # 4× burst
            memory: "1Gi"
        env:
        - name: TENANT_ID
          value: "abc"
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: tenant-abc-quota
  namespace: tenant-abc
spec:
  hard:
    requests.cpu: "4"         # Max 8 pods at 500m each
    requests.memory: "8Gi"
    limits.cpu: "16"
    limits.memory: "16Gi"
```

### Monitoring

```promql
# Throttling rate per namespace
sum by (namespace) (
  rate(container_cpu_cfs_throttled_seconds_total[5m])
)

# Alert if throttling > 10% for > 5 minutes
rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0.1

# Namespace CPU usage vs quota
sum by (namespace) (container_cpu_usage_seconds_total) /
  kube_resourcequota{resource="requests.cpu"}
```

---

## Pattern 5: Batch/Background Jobs

### Overview

Use BestEffort or Burstable with minimal requests. Jobs use idle CPU, yielding to higher-priority workloads.

**From our research:**
- Phase 2: BestEffort completed in 0.3s (plenty of idle CPU)
- Suitable for non-critical, deadline-flexible work

### Configuration

**Option A: BestEffort (No Requests/Limits)**
```yaml
resources: {}  # No requests or limits
```

**Option B: Burstable (Small Request, No Limit)**
```yaml
resources:
  requests:
    cpu: "100m"       # Minimal guarantee
    memory: "256Mi"
  # No limits
```

### When to Use

✅ **Use for:**
- ML model training (deadline-flexible)
- Data processing pipelines
- Log aggregation
- Backup jobs
- CI/CD builds

❌ **Don't use for:**
- Critical services
- User-facing APIs
- Deadline-sensitive jobs

### Pros and Cons

**Pros:**
- ✅ Uses idle CPU (no waste)
- ✅ Cost-effective (low/no reservation)
- ✅ Can scale out during low-traffic hours

**Cons:**
- ❌ First to be evicted (BestEffort)
- ❌ Unpredictable performance (depends on cluster load)
- ❌ May be starved if cluster busy

### Example: ML Training Job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: train-model
spec:
  completions: 1
  parallelism: 4
  template:
    spec:
      restartPolicy: OnFailure
      containers:
      - name: trainer
        image: ml-trainer:v1
        resources:
          requests:
            cpu: "2000m"      # Prefer 2 CPUs, but not guaranteed
            memory: "4Gi"
          # No limits - use all available CPU
        command:
        - python3
        - train.py
        - --epochs=100
      # Allow preemption by higher-priority pods
      priorityClassName: low-priority
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: low-priority
value: 100  # Low priority (default is 0, system-critical is 2000000000)
globalDefault: false
```

### Monitoring

```promql
# Job completion time
job_completion_time_seconds{job="train-model"}

# CPU usage (may vary widely)
container_cpu_usage_seconds_total{pod=~"train-model-.*"}

# Check for evictions
kube_pod_status_reason{reason="Evicted"}
```

---

## Anti-Patterns to Avoid

### ❌ Anti-Pattern 1: Limit Without Request

```yaml
# BAD: Limit without request
resources:
  limits:
    cpu: "1000m"
  # No request specified
```

**Problem:**
- Request defaults to limit (becomes Guaranteed QoS)
- You probably wanted Burstable, not Guaranteed
- Wastes resources if workload is usually idle

**Fix:**
```yaml
resources:
  requests:
    cpu: "200m"       # Explicit small request
  limits:
    cpu: "1000m"      # Can burst 5×
```

---

### ❌ Anti-Pattern 2: Tiny Request, Huge Limit

```yaml
# BAD: 100× burst factor
resources:
  requests:
    cpu: "10m"
  limits:
    cpu: "1000m"      # 100× request
```

**Problem:**
- Poor scheduling decisions (scheduler only sees 10m)
- All pods fit on one node, then throttle heavily
- Phase 4 showed: heavy throttling = 62.7% perf loss

**Fix:**
```yaml
resources:
  requests:
    cpu: "500m"       # Realistic minimum
  limits:
    cpu: "2000m"      # 4× burst (sane)
```

---

### ❌ Anti-Pattern 3: Copy-Paste from Docs

```yaml
# BAD: Example values from tutorials
resources:
  requests:
    cpu: "100m"       # Way too small for real app
    memory: "128Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"
```

**Problem:**
- Tutorial values are minimal examples
- Real apps need more
- Results in throttling and OOMKills

**Fix:**
- Measure your app's actual usage
- Use P50 for requests, P95 for limits

---

### ❌ Anti-Pattern 4: No Monitoring

```yaml
# BAD: Set and forget
resources:
  requests:
    cpu: "1000m"
  limits:
    cpu: "2000m"
# No Prometheus alerts, no dashboard
```

**Problem:**
- Throttling goes unnoticed
- Users complain about "slow API"
- No data to inform tuning

**Fix:**
```yaml
# Deploy monitoring stack
helm install prometheus prometheus-community/kube-prometheus-stack

# Add alerts
- alert: HighCPUThrottling
  expr: rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0.1
  for: 5m
```

---

## Summary Table

| Pattern | Requests | Limits | QoS | Use Case | Throttling Risk |
|---------|----------|--------|-----|----------|----------------|
| Request-Only | ✅ | ❌ | Burstable | HTTP APIs, DBs | Low |
| Guaranteed | ✅ | ✅ (same) | Guaranteed | Critical services | Low (if sized right) |
| CPU Pinning | ✅ (int) | ✅ (same) | Guaranteed | Real-time, trading | Very Low |
| Multi-Tenant | ✅ | ✅ (2-4×) | Burstable | SaaS platforms | Moderate |
| Batch/Background | ❌ or small | ❌ | BestEffort | ML training, jobs | High (acceptable) |

---

## Decision Checklist

Before deploying, ask:

- [ ] What is the workload type? (API, DB, batch, real-time)
- [ ] Is latency critical? (P99 < 100ms?)
- [ ] Is this multi-tenant? (noisy neighbor risk?)
- [ ] What is the baseline CPU usage? (P50, P95)
- [ ] Is bursting required? (traffic spikes?)
- [ ] Are there SLAs? (99.9% uptime?)
- [ ] Is monitoring in place? (throttling alerts?)

---

## References

- Phase 2 Results: `research/cpu-limits/phase2/evidence/`
- Phase 4 Throttling Analysis: `research/cpu-limits/phase4/THROTTLING_ANALYSIS.md`
- Kernel Tuning: `research/cpu-limits/phase5/admin-guide/KERNEL_CONFIG.md`
- Prometheus Alerts: `research/cpu-limits/phase4/monitoring/prometheus-alerts.yaml`

---

**Date:** October 26, 2026  
**Status:** ✅ Complete  
**Research Phase:** Phase 5 - Production Readiness
