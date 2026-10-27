# Kubernetes CPU Limits: Developer Guide

**How to set CPU requests and limits, from measurements on a live cluster**

## TL;DR

```
Is your workload latency-sensitive (web API, user-facing)?
├─ YES → Request for scheduling. Do NOT set a CPU limit.
└─ NO → Is it batch / background / throughput work?
     ├─ YES → Set requests = what you need, limits = requests (or omit limits)
     └─ NO → Requests only, watch p99 and throttle counters

Default starting point:
  requests.cpu: <P95 observed usage>
  limits.cpu:   <not set>
```

The measurement behind that rule: on an nginx serving a 14.4 KiB page, the
median request was unaffected by a CPU limit (2.04 ms unlimited, 2.16 ms at a
70m limit) while p99 went from 13.98 ms to 98.87 ms. A limit does not slow
your application down evenly. It hits the requests that arrive when the
quota is spent.

---

## Background

A Kubernetes CPU limit is enforced by CFS bandwidth control as a quota per
100 ms period. When the quota is used up, the container's tasks stop running
until the next period starts. The node may be completely idle; the quota is
still enforced.

That produces three separate effects, which Phase 4 and Phase 7 measured
independently:

1. **Throughput loss larger than the quota cut** (Phase 4, CPU-bound).
2. **Tail latency blow-up with an unchanged median** (Phase 7.1, nginx).
3. **Longer time between "ready to run" and "running"** (Phase 7.4, BPF).

### Phase 4: throughput under a CPU-bound workload

bogo-ops benchmark, no IO, one container per run:

| Config | requests | limits | ops/sec | Loss |
|--------|----------|--------|---------|------|
| none | 2000m | 4000m | 139.05 | baseline |
| light | 1000m | 3000m | 108.78 | -21.8% |
| moderate | 500m | 1500m | 53.57 | -61.5% |
| heavy | 100m | 500m | 19.22 | -86.2% |

The loss is larger than the quota reduction alone. Heavy (100m/500m) gets 0.5
core while the 2000m/4000m reference run gets up to 4, an 8x quota cut, and
loses 7.2x throughput (139.05 → 19.22 ops/s). Throttling removes more than
proportionally: throughput is not linear in quota, and the remaining time lost
is the forced wait until the next quota period.

### Phase 7.1: latency under a real web server

nginx, 14.4 KiB page, fixed-rate client, keepalive disabled:

| CPU limit | Throughput | p50 | p99 | Throttled periods |
|-----------|-----------|-----|-----|-------------------|
| none | 7216 req/s | 2.04 ms | 13.98 ms | 0% |
| 120m | 3481 req/s | 2.13 ms | 69.43 ms | 99.7% |
| 100m | 2991 req/s | 2.20 ms | 67.76 ms | 95.6% |
| 70m | 2071 req/s | 2.16 ms | 98.87 ms | 75.1% |

Read this before choosing a limit:

- **p50 is flat.** If you monitor averages, you will not see the problem.
- **Throttle percentage does not rank configurations.** The 70m pod was
  throttled less often than the 100m pod and was worse on latency.
- **Half the request rate and 5x the p99** is what a 120m limit bought here.
  A quota that looks generous in the manifest can be far below what the
  service needs.

### Phase 7.4: what the kernel actually does

bpftrace measured the time between a task becoming runnable
(`sched_wakeup`) and a CPU picking it up (`sched_switch`), for nginx only:

| Config | Limit | Throttled | Throughput | p99 | Mean wait | Worst wait |
|--------|-------|-----------|-----------|-----|-----------|------------|
| no-limit | none | 0% | 6000 req/s | 4.53 ms | 6.4 us | 5.5 ms |
| capped | 200m | 96.0% | 4730 req/s | 56.34 ms | 9.6 us | 6.9 ms |
| tight | 100m | 96.9% | 2588 req/s | 81.99 ms | 13.7 us | 71.7 ms |

The scheduler is not slow: the mean wait stays under 15 us in every case. A
minority of wakeups wait for the next quota period, and those are your slow
requests.

---

## The Three Workload Patterns

### 1. CPU-bound (compute-heavy)

**Examples**: video encoding, ML training, data processing, compression

**Measured** (Phase 4): a heavy limit cost 86.2% of throughput, against an 8x
quota cut from the 2000m/4000m reference run to 100m/500m.

**Recommendation**:

```yaml
resources:
  requests:
    cpu: "2000m"
  limits:
    cpu: "2000m"     # equal to the request: no forced wait
```

Use a limit here when you need one, but set it equal to the request. A
CPU-bound task has no idle time to absorb a smaller quota, so any gap
between request and limit is pure throughput loss. Phase 7.2 confirmed the
pattern from the other direction: a burner inside a one-core Guaranteed
quota ran 99.5 s of CPU in the window without the tail collapse that an
under-provisioned limit causes.

### 2. IO-bound (network, disk, APIs)

**Examples**: API servers, proxy/gateway, databases, file processing

**Measured** (Phase 7.1): a limit halves throughput and multiplies p99 by 5x
while p50 stays flat.

**Recommendation**:

```yaml
resources:
  requests:
    cpu: "500m"      # P95 observed usage
  limits:
    memory: "1Gi"    # memory limits are fine
    # no cpu limit
```

**Why**: IO wait does not consume quota, so a bursty service can sit far
below its request most of the time and then need several times the request
when responses arrive. A CPU limit turns each such burst into a wait for the
next 100 ms period.

### 3. Hybrid (compute plus IO)

**Examples**: web applications with heavy business logic, queue workers

**Measured**: Phase 2 characterisation and Phase 7.1 together show the worst
of both. Both phases are needed: the server is IO-driven between requests and
CPU-bound while serving them.

**Recommendation**:

```yaml
resources:
  requests:
    cpu: "1000m"     # P95 observed usage
  limits:
    memory: "1Gi"
    # no cpu limit unless the pod is a known noisy neighbour
```

If you must cap it (shared node, untrusted code), set the limit well above
the observed p99 CPU usage and treat any sustained throttling as a defect
rather than a normal state.

---

## How To Set Your Values

### Step 1: Measure baseline

Deploy without a CPU limit for one week.

```bash
kubectl top pod <pod> --containers
```

```promql
rate(container_cpu_usage_seconds_total{pod="<pod>"}[5m])
```

You need **P50**, **P95**, and **P99** of CPU usage. Average usage alone
will under-size the request and hide the bursts that matter.

### Step 2: Choose the request

```
requests.cpu = P95 observed usage, rounded up
```

The request is what the scheduler reserves and what sets the pod's
`cpu.weight` under contention. Phase 7.5 measured the mapping: 100m request
produced weight 17 and nice 8, 250m request produced weight 35 and nice 5.
Raising the request raises your share of a contended node.

### Step 3: Decide on the limit

| Workload type | Limit |
|---------------|-------|
| Latency-sensitive | **not set** |
| CPU-bound batch | equal to the request |
| Hybrid / worker | not set, or well above p99 usage |
| Untrusted multi-tenant | set, and accept the tail cost |

### Step 4: Deploy and monitor p99 and throttling together

```promql
# throttled time as a share of runnable time
100 * rate(container_cpu_cfs_throttled_seconds_total{pod="<pod>"}[5m])
/ (rate(container_cpu_usage_seconds_total{pod="<pod>"}[5m])
   + rate(container_cpu_cfs_throttled_seconds_total{pod="<pod>"}[5m]))
```

Use this to find candidates, not to judge them. In Phase 7.1 the
least-throttled configuration had the worst p99. Always pair it with your own
latency p99 and ask whether your SLOs hold.

---

## Real-World Examples

### Example 1: Web API (latency-sensitive)

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: api-server
spec:
  containers:
  - name: api
    image: myapi:latest
    resources:
      requests:
        cpu: "1000m"      # P95 observed: 800m, rounded up
        memory: "512Mi"
      limits:
        memory: "1Gi"
        # no cpu limit
```

### Example 2: Batch job (throughput)

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: video-encoder
spec:
  template:
    spec:
      containers:
      - name: encoder
        image: ffmpeg:latest
        resources:
          requests:
            cpu: "2000m"
            memory: "2Gi"
          limits:
            cpu: "2000m"     # equal to request: no forced wait
            memory: "4Gi"
```

### Example 3: Queue worker (hybrid)

```yaml
spec:
  containers:
  - name: worker
    resources:
      requests:
        cpu: "500m"
        memory: "256Mi"
      limits:
        memory: "512Mi"
        # no cpu limit
```

### Example 4: Known noisy neighbour (cap it)

Limit the pod that misbehaves, not the pod that suffers. Phase 7.3 measured
this: capping four CPU burners at 200m cut their consumption by 75%
(149.1 s to 37.8 s of CPU) and the co-located latency-sensitive pod's median
latency fell 79%.

```yaml
spec:
  containers:
  - name: batch-fanout
    resources:
      requests:
        cpu: "500m"
      limits:
        cpu: "2000m"     # bounded blast radius
```

---

## Troubleshooting

### Symptom: high p99, normal p50

```bash
kubectl exec <pod> -- cat /sys/fs/cgroup/cpu.stat
```

```
nr_periods 4000
nr_throttled 2800
throttled_usec 2800000000
```

`throttled_usec` is microseconds: 2.8e9 is 2800 seconds of forced wait.
If p99 is your problem and this counter is rising, remove the CPU limit
first and re-measure.

### Symptom: pods getting evicted

That is memory, not CPU. CPU throttling does not evict pods.

```bash
kubectl describe pod <pod> | grep -A5 "Last State"
```

`OOMKilled` means raise the memory limit.

### Symptom: pod cannot be scheduled

```
0/3 nodes available: insufficient cpu
```

The request is too high for the cluster. Lower the request, add nodes, or
enable the Cluster Autoscaler. Lowering a CPU *limit* never fixes this; only
requests affect placement.

---

## Anti-Patterns

### Setting the limit far below the request's traffic needs

A limit that looks reasonable against average usage will throttle at every
peak. Phase 7.1 measured a 120m limit producing 99.7% throttled periods on a
service that needed most of a core at its offered rate.

### Reading throttle percentage as a quality score

Phase 7.1: the 70m configuration was throttled in 75.1% of periods and the
100m in 95.6%, and the 70m one had the worse p99. Throttle percentage tells
you a quota binds. Latency tells you whether it hurts.

### Assuming `millicores` means threads

`4000m` is 4 core-seconds of runtime per 100 ms period, not four threads. A
single-threaded service cannot use more than one core however high the value.

### Guessing values

Set requests from measured P95 and validate the limit with p99. Guessed
values are either too low (throttling) or too high (wasted reservation).

---

## Summary

| Scenario | Request | Limit |
|----------|---------|-------|
| Production API | P95 usage | none |
| Batch job | measured need | equal to request |
| Queue worker | P95 usage | none |
| Known noisy neighbour | measured need | bounded, above its own p99 |
| Dev / test | small | none, or generous |

**Golden rule**: request for scheduling and fairness, and leave CPU
unlimited on anything with a latency SLO. Put limits on the pods that take
CPU from others.

---

## Further Reading

- Full Phase 7 results: `PHASE7_COMPLETE.md`
- Production scenarios: `REAL_WORLD_PLAYBOOK.md`
- Cgroup parameter detail: `evidence/phase7/cgroup-v2/`
- Phase 4 throughput data: `phase4/evidence/throttling-summary.json`
- Phase 7.4 scheduler trace: `evidence/phase7/live-bpf/`
- [Linux CFS Bandwidth Control](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)
- [Kubernetes Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
