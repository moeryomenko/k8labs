# Phase 6.1: Latency Testing Under CPU Throttling

**Status:** not run as designed (wrk2 panics on this cluster). The same question
was answered by Phase 7.1 and Phase 7.4 with measured data; that evidence is
reproduced below and replaces the projections that previously stood here.

---

## Key Question

**"Does CPU throttling make my API feel slow to users?"**

## What Was Measured Instead

Evidence: `evidence/phase7/real-apps/nginx-throttling-20261026T191420Z.json`

nginx serving a 14 KiB page on a 4-vCPU node (`7.3.0-k8labs`), offered a fixed
rate by a Python client (`phase7/lib/loadgen.py`, `--no-keepalive`), because
wrk2 panics on this cluster.

| CPU limit | Throughput | p50 | p99 | Throttled periods |
|-----------|-----------|-----|-----|-------------------|
| none | 7216 req/s | 2.04 ms | 13.98 ms | 0% |
| 120m | 3481 req/s | 2.13 ms | 69.43 ms | 99.7% |
| 100m | 2991 req/s | 2.20 ms | 67.76 ms | 95.6% |
| 70m | 2071 req/s | 2.16 ms | 98.87 ms | 75.1% |

### Kernel view

Evidence: `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`
(bpftrace inside the unprivileged reader pod).

| CPU limit | Throttled | Throughput | p99 | Mean wake-to-run | Max wake-to-run |
|-----------|-----------|-----------|-----|------------------|-----------------|
| none | 0% | 6000 req/s | 4.53 ms | 6.4 µs | 5.49 ms |
| 200m | 96.05% | 4730 req/s | 56.34 ms | 9.6 µs | 6.9 ms |
| 100m | 96.94% | 2588 req/s | 81.99 ms | 13.7 µs | 71.7 ms |

## Analysis

- Throughput roughly halved and p99 grew about 5–7×.
- **p50 did not move** (2.04 → 2.20 ms). Mean-based dashboards show nothing.
- Mean wake-to-run delay barely moved (6.4 → 13.7 µs); the maximum exploded
  (5.49 → 71.7 ms). The damage is in the tail, caused by tasks waiting out the
  100 ms quota period.
- Throttle percentage does not rank configurations: 75.1% throttled produced the
  worst p99 of the four Phase 7.1 runs.

### Why This Matters

1. **User experience:** the slow requests are the ones that cross the quota
   period, and they are invisible to average-latency monitoring.
2. **SLA impact:** a p99 SLA is the metric that breaks; p50 stays inside budget.
3. **Load balancer behavior:** slow pods can be marked unhealthy, moving traffic
   to peers. This was not tested here; treat it as a mechanism, not a result.

---

## Visualizations

Latency by limit, from the Phase 7.1 run above:

```
Limit →        none     120m     100m      70m
P50         │ 2.04ms │ 2.13ms │ 2.20ms │  2.16ms │
P99         │13.98ms │69.43ms │67.76ms │ 98.87ms │
```

## Limitations

- The earlier revision of this document contained a "Expected Results" block
  with invented latency distributions (p99 2 s at 70% throttling, p50 10–20 ms,
  8000 req/s baseline). None of those numbers were ever produced and they have
  been removed.
- The workload is a static page server, so these numbers bound CPU-bound
  request paths. Untested: database and network-bound APIs.
