# Phase 7 Summary: Extended Research

**Date**: 2026-10-26  
**Status**: all eight topics done; seven measured on the cluster, 7.8 computed from Phase 4 data

`PHASE7_COMPLETE.md` holds the full per-topic detail. This file is the short
version: what each topic measured, the headline number, and where the
evidence lives.

All measurements came from one 4-core node running kernel 7.3.0-k8labs; 7.7
also drained one 2-core worker. Each topic was executed with the command
`phase7/run-all-phase7.sh` uses for it.

---

## Results At A Glance

| Topic | Question | Answer | Evidence |
|-------|----------|--------|----------|
| 7.1 | Does a CPU quota hurt a real web server? | p99 13.98 ms -> 69.43 ms at a 120m limit; throughput 7216 -> 3481 req/s | `real-apps/nginx-throttling-20261026T191420Z.json` |
| 7.2 | Does QoS class change fairness? | BestEffort weight 1 / nice 19, Burstable 59 / 2, Guaranteed 100 / 0; BestEffort p99 65.44 ms vs 31.28 ms Burstable | `qos-comparison/qos-classes-20261026T191440Z.json` |
| 7.3 | Do limits on neighbours protect a victim? | Capping neighbours cut their CPU 75% (149.1 s -> 37.8 s); victim p50 -79%, p99 -2.9% | `interference/noisy-neighbor-20261026T191852Z.json` |
| 7.4 | What does throttling do to the scheduler? | Mean wake-to-run wait 6.4 us -> 13.7 us; worst case 5.5 ms -> 71.7 ms | `live-bpf/scheduler-trace-20261026T192056Z.json` |
| 7.5 | How does Kubernetes map to cgroups? | 100m request -> weight 17 / nice 8; 100m limit -> `10000 100000` | `cgroup-v2/cgroup-v2-analysis-20261026T190908Z.json` |
| 7.6 | Does the scheduler change the answer? | `scx_lavd` ignores `cpu.max` (12.22 CPU-s against a 4.0 CPU-s quota) and inverts cgroup weights; `--enable-cpu-bw` restores the quota (4.01 CPU-s) | `kernel-comparison/kernel-comparison-20261026T212246Z.json`, `kernel-comparison/cpu-bandwidth-20261026T214327Z.json` |
| 7.7 | What does an incident look like? | A spike of 3000 req/s got 1453 rps at p99 102 ms from a 120m pod; losing one of three replicas cost no errors and took 42 s to replace | `incidents/incidents-20261027T000538Z.json`, `incidents/incidents-run2-20261027T002520Z.json` |
| 7.8 | What does throttling cost? | Upper bound ~$2,755/month wasted capacity per throttled scenario; no revenue model supplied | `cost-analysis/tco-model-20261026T190918Z.json` |

---

## The One Result To Remember

Across every experiment the **median** request survived the quota and the
**tail** did not.

- 7.1: p50 2.04 ms -> 2.16 ms from no limit to 70m; p99 13.98 ms -> 98.87 ms.
- 7.4: mean scheduling wait 6.4 us -> 13.7 us; worst case 5.5 ms -> 71.7 ms.

A quota does not slow the scheduler down. It makes a minority of wakeups wait
for the next 100 ms period, and those wakeups carry the slow requests.

---

## Topics

### 7.1 Real application (nginx)

Fixed-rate client, keepalive disabled, 14.4 KiB page.

| Config | Limit | Throughput | p99 | Throttled |
|--------|-------|-----------|-----|-----------|
| no-limit | none | 7216 req/s | 13.98 ms | 0% |
| light | 120m | 3481 req/s | 69.43 ms | 99.7% |
| moderate | 100m | 2991 req/s | 67.76 ms | 95.6% |
| heavy | 70m | 2071 req/s | 98.87 ms | 75.1% |

Throttle percentage is not monotonic in the limit here: the 70m pod is
throttled less often than the 100m pod. Latency ranks the configurations;
throttle percentage does not.

### 7.2 QoS classes

One probe and one CPU burner per class, four cores, all three probes under
load at once.

| Class | weight / nice | Throughput | p99 | Burner CPU |
|-------|---------------|-----------|-----|------------|
| BestEffort | 1 / 19 | 1787 req/s | 65.44 ms | 71.7 s |
| Burstable | 59 / 2 | 3613 req/s | 31.28 ms | 118.2 s |
| Guaranteed | 100 / 0 | 2980 req/s | 30.99 ms | 99.5 s |

Guaranteed and Burstable match on p99. Guaranteed uses less CPU because its
burner is capped at one core.

### 7.3 Noisy neighbour

Victim at 3000 req/s plus four burners, run with the burners unlimited and
then capped at 200m.

| Scenario | Burner CPU | Victim p50 | Victim p99 |
|----------|-----------|-----------|-----------|
| unlimited | 149.1 s | 0.79 ms | 5.73 ms |
| burners capped 200m | 37.8 s | 0.17 ms | 5.57 ms |

Limiting the neighbours removed 75% of their CPU use and cut the victim's
median latency by 79%. The p99 gain is small (-2.9%) and the p99.9 is
slightly worse (+12.2%) in this run; an earlier run of the same test measured
p99 -37.1%. The tail number is not stable run to run.

### 7.4 Live BPF scheduler trace

bpftrace 0.24.2 measures wake-to-run delay for tasks named `nginx`.

| Config | Limit | Throttled | Throughput | p99 | Mean wait | Max wait |
|--------|-------|-----------|-----------|-----|-----------|----------|
| no-limit | none | 0% | 6000 req/s | 4.53 ms | 6.4 us | 5.5 ms |
| capped | 200m | 96.0% | 4730 req/s | 56.34 ms | 9.6 us | 6.9 ms |
| tight | 100m | 96.9% | 2588 req/s | 81.99 ms | 13.7 us | 71.7 ms |

### 7.5 cgroup v2 parameters

All nine CPU files present on the host. Measured mapping:

| requests.cpu | limits.cpu | cpu.weight | cpu.weight.nice | cpu.max |
|--------------|------------|------------|-----------------|---------|
| 100m | none | 17 | 8 | `max 100000` |
| 100m | 100m | 17 | 8 | `10000 100000` |
| 250m | 250m | 35 | 5 | `25000 100000` |

`cpu.weight` follows the request and ignores the limit. `cpu.max` carries
the limit as a 100 ms quota. `cpu.max.burst` is 0, so quota does not carry
across periods.

### 7.8 Cost model

Joined to a 10-node, 8 vCPU, $2,803.20/month-per-node cost shape. Reports
about $2,755/month of "wasted capacity" per throttled scenario, which the
model itself names an upper bound. With no baseline rps or revenue-per-request
supplied, the throughput cost is 0. Supply those numbers to make it a
business figure.

---

## Limits Of This Phase

- **7.6 Kernel comparison** ran on one kernel. It compares CFS with
  `scx_lavd` and the two dynamic preemption models, not with a distribution
  kernel, PREEMPT_RT, or other sched_ext schedulers.
- **7.7 Incident simulation** uses a purpose-built 100-microsecond HTTP target
  rather than nginx, so its rates are not comparable with 7.1. The HPA in the
  autoscaling race never scaled, because this cluster's metrics path lagged the
  load by one to two windows.

---

## How to Reproduce

```bash
cd research/cpu-limits/phase7
PHASE7_NODE=<node name> ./run-all-phase7.sh    # 50-60 minutes, 8 topics
```

---

**Last updated**: 2026-10-26
