# Real-World Production Playbook

**Symptoms, diagnosis, and fixes, backed by the Phase 7 measurements**

---

## Table of Contents

1. [How To Read This](#how-to-read-this)
2. [Incident: High API Latency](#incident-high-api-latency)
3. [Incident: Database Performance Degradation](#incident-database-performance-degradation)
4. [Incident: Batch Job Stuck](#incident-batch-job-stuck)
5. [Scenario: Traffic Spike](#scenario-traffic-spike)
6. [Scenario: Multi-Tenant Isolation](#scenario-multi-tenant-isolation)
7. [Scenario: Cost Optimization](#scenario-cost-optimization)
8. [Cheat Sheet](#cheat-sheet)

---

## How To Read This

Each scenario states what was **measured** in Phase 7 and what is
**reasoning** that goes beyond the measurements. The measured figures come
from one 4-core node running kernel 7.3.0-k8labs with nginx and synthetic CPU
burners. They reproduce the mechanisms; the absolute milliseconds and
dollars in your cluster will differ. Verify with your own numbers before
quoting them.

The single most reliable finding: a CPU quota leaves the median request
alone and ruins the tail. Phase 7.1 measured p50 going from 2.04 ms to
2.16 ms while p99 went from 13.98 ms to 98.87 ms.

---

## Incident: High API Latency

### Symptoms
- p99 latency up sharply, median latency normal
- No application or database change
- Average CPU usage looks fine in dashboards

### Diagnosis

**Step 1: Read throttling from the container's own cgroup**

```bash
POD=$(kubectl get pod -l app=api -o jsonpath='{.items[0].metadata.name}')
kubectl exec "$POD" -- cat /sys/fs/cgroup/cpu.stat
```

```
nr_periods 12000
nr_throttled 8400
throttled_usec 8400000000
```

Everything is in microseconds: 8.4e9 usec is 8400 seconds of forced wait
across the container's lifetime.

**Step 2: Compute the throttle percentage**

```
throttle_pct = nr_throttled / nr_periods * 100
```

**Step 3: Confirm with Prometheus**

```promql
100 * rate(container_cpu_cfs_throttled_seconds_total{pod="$POD"}[5m])
/ (rate(container_cpu_usage_seconds_total{pod="$POD"}[5m])
   + rate(container_cpu_cfs_throttled_seconds_total{pod="$POD"}[5m]))
```

### What the measurements say

Phase 7.1 (nginx, 14.4 KiB page, fixed-rate client):

| CPU limit | Throughput | p50 | p99 | Throttled periods |
|-----------|-----------|-----|-----|-------------------|
| none | 7216 req/s | 2.04 ms | 13.98 ms | 0% |
| 120m | 3481 req/s | 2.13 ms | 69.43 ms | 99.7% |
| 100m | 2991 req/s | 2.20 ms | 67.76 ms | 95.6% |
| 70m | 2071 req/s | 2.16 ms | 98.87 ms | 75.1% |

Two cautions against the intuitive reading:

- **The median did not move.** p50 stayed within 0.2 ms across every
  configuration. If your dashboard shows average latency, you will not see
  this problem. Look at p99.
- **A high throttle percentage does not mean the worst latency.** The 70m
  pod was throttled less often (75.1%) than the 100m pod (95.6%) but had the
  worst p99. Use throttle percentage to find candidates, then rank by latency.

### Fix

Remove the CPU limit, keep the request:

```yaml
resources:
  requests:
    cpu: "1000m"
  limits:
    memory: "1Gi"
    # no cpu limit
```

Phase 7.4 shows why this works at the kernel level. With a 100m limit, nginx
tasks waited a mean of 13.7 us to be scheduled after waking, against 6.4 us
with no limit, and the worst wait went from 5.5 ms to 71.7 ms. Those long
waits are requests that arrived just after the quota ran out and had to wait
for the next 100 ms period.

Reasoning that goes beyond the measurement: the p99 you observe after
removing the limit should approach the no-limit row above. It will not
necessarily reach 13.98 ms, because the node's spare capacity and your
traffic mix differ.

---

## Incident: Database Performance Degradation

### Symptoms
- Query latency up 2-3x
- Connection pool saturation
- CPU pinned at the limit but the node has idle cores

### Diagnosis

```bash
kubectl get pod postgres-0 -o jsonpath='{.spec.nodeName}'
kubectl get node <node> -o jsonpath='{.status.allocatable.cpu}'
kubectl get pod postgres-0 -o jsonpath='{.spec.containers[0].resources}'
kubectl exec postgres-0 -- cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat
```

If `cpu.max` is `200000 100000` while the node has far more allocatable CPU,
the database is capped at two cores regardless of idle capacity. This is the
7.5 finding directly: `cpu.max` is enforced whether or not the node is busy,
while `cpu.weight` only decides who runs when the node is oversubscribed.

### What the measurements say

- 7.5: a 2000m limit becomes a 200000 us quota per 100 ms period. Idle
  capacity elsewhere on the node is invisible to the container.
- 7.3: capping competing CPU burners at 200m cut their consumption by 75%
  (149.1 s to 37.8 s) and the co-located victim's median latency dropped by
  79%. Limits on the *offending* pods help the *victim*. Limits on the victim
  do not.

### Fix

```yaml
resources:
  requests:
    cpu: "4000m"
    memory: "8Gi"
  limits:
    memory: "16Gi"
    # no cpu limit, or raise it well above observed peak
```

No Phase 7 experiment tested a database engine. The recommendation transfers
the measured mechanism (quota enforced against idle capacity) rather than a
measured database result.

---

## Incident: Batch Job Stuck

### Symptoms
- CPU-bound job running far slower than on a VM
- CPU shows 100% of limit, throughput low

### Diagnosis

```bash
POD=$(kubectl get pod -l job-name=encode -o jsonpath='{.items[0].metadata.name}')
kubectl exec "$POD" -- cat /sys/fs/cgroup/cpu.stat
```

### What the measurements say

Phase 4 (bogo-ops CPU benchmark, synthetic) measured throughput under four
limits:

| Config | requests | limits | ops/sec | Loss vs no limit |
|--------|----------|--------|---------|------------------|
| no limit | 2000m | 4000m | 139.05 | baseline |
| light | 1000m | 3000m | 108.78 | -21.8% |
| moderate | 500m | 1500m | 53.57 | -61.5% |
| heavy | 100m | 500m | 19.22 | -86.2% |

The loss is far larger than the quota reduction. The heavy config gets 0.5
core where the 2000m/4000m reference run gets up to 4, an 8x quota cut, and
throughput falls 7.2x (139.05 → 19.22 ops/s). The relation is not linear; the
residual loss is the forced wait until the next quota period, which a CPU-bound
task cannot fill with anything else.

Phase 7.4 shows the same mechanism from the scheduler side: worst-case
wake-to-run wait rose from 5.5 ms without a limit to 71.7 ms at 100m.

### Fix

For a CPU-bound batch job, raising the limit is the direct fix:

```yaml
resources:
  requests:
    cpu: "4000m"
  limits:
    cpu: "4000m"     # or remove the limit entirely
```

Setting limit = request (Guaranteed) removes the throttling for this case
because the quota then matches the reserved CPU. Phase 7.2 measured exactly
this: the Guaranteed probe's burner ran 99.5 s of CPU inside a one-core
quota without the tail collapse that an under-provisioned limit produces.

Reasoning beyond the measurement: batch jobs tolerate tail latency poorly
only if they have many small units of work. A single long-lived encode loses
throughput but no user-visible latency. A job made of thousands of short
tasks behaves like the 7.1 server and needs the tail fixed.

---

## Scenario: Traffic Spike

### Situation
- A known spike is coming (sale, launch, batch deadline)
- Latency-sensitive pods currently carry CPU limits

### Preparation

**Step 1: Remove CPU limits on the latency-sensitive pods**

```bash
for DEPLOY in api-gateway user-service product-service; do
  kubectl patch deployment "$DEPLOY" --type=json \
    -p='[{"op": "remove", "path": "/spec/template/spec/containers/0/resources/limits/cpu"}]'
done
```

**Step 2: Raise requests only if the HPA or scheduler needs it**

Requests drive scheduling and HPA utilisation, not the quota. Raise them
when the node is likely to be oversubscribed, so the scheduler places pods on
nodes with real headroom.

**Step 3: Pre-scale**

```bash
kubectl scale deployment api-gateway --replicas=20
```

**Step 4: Watch p99 and throttling together**

```promql
histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[1m])) by (le, pod))
```

```promql
rate(container_cpu_cfs_throttled_seconds_total{namespace="production"}[1m]) > 0
```

### What the measurements say

Phase 7.1 is the closest available evidence: at a fixed offered rate, an
unlimited nginx pod sustained 7216 req/s while a 120m limit capped it at
3481 req/s, half the throughput with roughly 5x the p99. Under a spike the
unlimited pod uses whatever the node has left; the limited pod is stopped at
its quota no matter how idle the node is.

No Phase 7 experiment simulated a live spike, an HPA race, or a node drain.
Those scenarios are not measured here.

---

## Scenario: Multi-Tenant Isolation

### Situation
- Shared cluster, several teams
- One team's CPU-heavy pods degrade another team's latency-sensitive service

### What the measurements say

Phase 7.2, one probe and one burner per QoS class, four cores:

| Class | weight / nice | Throughput | p99 | Burner CPU |
|-------|---------------|-----------|-----|------------|
| BestEffort | 1 / 19 | 1787 req/s | 65.44 ms | 71.7 s |
| Burstable (500m request) | 59 / 2 | 3613 req/s | 31.28 ms | 118.2 s |
| Guaranteed (1000m) | 100 / 0 | 2980 req/s | 30.99 ms | 99.5 s |

The weights are real values read from the pod cgroups, not documentation.
A BestEffort pod gets weight 1 against weight 59 for a modest Burstable
request, which is why its p99 is more than double.

Phase 7.3: capping four noisy burners at 200m reduced their CPU consumption
by 75% and cut the victim's median latency by 79%. The p99 improved by only
2.9% in that run, and p99.9 got slightly worse (+12.2%); an earlier run of
the same test gave p99 -37.1%. The victim's p50 improvement is reliable; the
tail improvement is not, at this scale.

### Recommendation

**Limit the noisy pods, not the sensitive ones.** Raising a request on the
offending pod raises its `cpu.weight` and makes it take more CPU under
contention; capping it with `cpu.max` bounds what it can take.

```yaml
# CPU-heavy team's workload: cap it
resources:
  requests:
    cpu: "500m"
  limits:
    cpu: "2000m"
```

```yaml
# Latency-sensitive service: reserve, do not cap
resources:
  requests:
    cpu: "2000m"
  limits:
    memory: "2Gi"
```

Enforce with a LimitRange in the noisy team's namespace, and leave the
latency-sensitive namespace without a default CPU limit.

---

## Scenario: Cost Optimization

### Situation
- Compute spend must come down without losing capacity

### Step 1: Find throttled workloads

```promql
topk(20,
  100 * rate(container_cpu_cfs_throttled_seconds_total[5m])
  / (rate(container_cpu_usage_seconds_total[5m])
     + rate(container_cpu_cfs_throttled_seconds_total[5m]))
)
```

### Step 2: Find over-provisioned requests

```promql
(rate(container_cpu_usage_seconds_total[30m])
 / on(pod,namespace) group_left kube_pod_container_resource_requests{resource="cpu"}
) < 0.5
```

### Step 3: Act on the measurement, not on the percentage alone

| Workload | Throttled | Action |
|----------|-----------|--------|
| Latency-sensitive | any | Remove the limit; 7.1 shows the tail cost |
| Latency-sensitive | 0 | Leave it, but check requests against P95 |
| Batch, CPU-bound | high | Raise limit toward request, or remove it; Phase 4 shows throughput recovers |
| Batch, CPU-bound | low | Keep; the tail does not matter here |
| Dev / staging | any | Limits for isolation are fine |

### What the cost model actually says

`phase7/7.8-cost-analysis/calculate-tco.py` produces an upper bound of about
$2,755/month per throttled scenario on a 10-node, 8 vCPU, $2,803.20/month
shape. The model's own `model_limits` field states the assumption: it counts
every throttled period as if it would otherwise have done full-price work.
That is not money you automatically recover.

The model reports **zero** throughput cost, because no baseline rps or
revenue per request was supplied. To make the model meaningful, supply both:

```bash
PHASE7_BASELINE_RPS=<your rps> \
PHASE7_REVENUE_PER_REQUEST=<your value> \
python3 calculate-tco.py
```

Right-sizing requests is the saving this research can defend: 7.5 shows a
250m request costs weight 35 against 17 for 100m, so an inflated request
takes more CPU from neighbours under contention without giving the app more
quota.

---

## Cheat Sheet

| Symptom | Check | Likely cause | Fix |
|---------|-------|--------------|-----|
| p99 high, p50 normal | `cpu.stat` in the pod cgroup | CPU limit | Remove the limit |
| CPU at limit, node idle | `cpu.max` | CPU limit | Remove or raise the limit |
| One team starves another | `cpu.weight` per pod cgroup | Noisy pod with a high weight and no cap | Cap the noisy pod |
| Batch job far slower than the CPU ratio suggests | `cpu.stat`, Phase 4 table | Quota forced wait | Limit = request, or no limit |
| Cost high, CPU utilisation low | requests vs P95 usage | Over-provisioned requests | Right-size requests |

**The rule the data supports**: set requests for scheduling and fairness,
avoid CPU limits on latency-sensitive workloads, and put limits on the pods
that misbehave rather than on the pods that suffer.

---

**Based on**: Phases 1-7. Measured figures cite their evidence file in
`PHASE7_COMPLETE.md`.

**Last updated**: 2026-10-26
