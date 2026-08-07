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

A measured implementation detail (Fedora 44, kernel 7.1, crun, cgroup v2): when `request == limit` the kubelet/crun set `cpu.max` quota exactly equal to the request (`100m/100m` -> `10000 100000`), **not** `max`. A workload that saturates its quota is therefore throttled in >= 98% of periods (measured: 100/100 -> 0.999, 500/500 -> 0.996, 1000/1000 -> 0.980 throttling ratio). Throttling disappears only when the limit is at/above the workload's real demand (limit=2000m on a 2-vCPU node -> 0 throttling regardless of request). The claim that `request == limit` disables CFS quota is NOT supported by this cluster's measurements.

---

## Workload Archetypes

### Type 1: Latency-Sensitive Web Services

- **Goal**: Minimize tail latency (p99)
- **Strategy**: Guaranteed QoS (request == limit)
- **Sizing**: Steady-state CPU + 20% headroom for bursts
- **Example**: request=500m, limit=500m for a service using ~400m
- **Why**: Keeps usage far below the enforced quota, so the workload is never throttled (measured: a 500m/500m API pod using ~1.5% CPU saw 0 throttled periods with batch co-located)
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

> **(Validated by experiments — Family B request×limit matrix, CPU-saturating stress-ng on a 2-vCPU node)**

| Region | Limit vs demand | Measured throttling ratio (Family B) | Recommendation |
|--------|------------------|--------------------------------------|----------------|
| Safe | limit >= demand (>= node capacity) | 0.0003 (100m/2000m), 0.0 (500m/2000m), 0.0 (1000m/2000m) | Acceptable for all workloads |
| Caution | limit slightly below demand | **not observed** — throttling is bimodal for a saturating workload; retained as approximate guidance only | -- |
| Dangerous | limit < demand | >= 0.98 for every limit <= 1000m: 100/100 0.999, 100/250 0.998, 100/500 0.9997 (max), 100/1000 0.983, 500/500 0.996, 500/1000 0.986, 1000/1000 0.980 | Redesign: increase limit or reduce request |

Measured facts that update the thresholds:

- **`request == limit` does not escape the Dangerous region.** Quota is enforced at the request value (100m/100m -> `cpu.max=10000`), so a workload that saturates its quota is throttled in ~98-100% of periods. The old assumption that Guaranteed QoS disables throttling is **not validated**; what protects a pod is usage well below the limit.
- **Throttling onset is demand-driven, not request-driven.** With a saturating workload the throttling ratio is >= 0.98 for every limit <= 1000m and 0 for limit=2000m, regardless of the request (100m vs 1000m). A limit at/above actual CPU demand eliminates throttling entirely.
- The previous 80%/95% limit-utilization bands were never measured; treat them as approximate until a non-saturating workload matrix is run.

### Experimental Validation Summary

These thresholds are validated by 50+ experiment runs on a 3-node Fedora 44 cluster (Kernel 7.1, CRI-O, crun, Cilium, cgroup v2), plus the six-family matrix (weight-share, request×limit, QoS hierarchy, latency interference, cpu-burst, tunables):

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

six-family validated numbers (54+45+18+24+6+18 summary rows, data-integrity verified):

- **Weight-share (A)** — proportional-share model `weight_i / Σweight_j` **validated within ~5 percentage points** (weight-share-analyze.py output, 15 rows = 5 cells x 3 pods). Measured achieved vs theoretical per ratio: 1:1 (500/500) 0.488/0.496, (800/800) 0.489/0.497; 1:4 (250/1000) 0.211/0.257; 1:5 (100/500) 0.173/0.221; 1:10 (100/1000) 0.103/0.144 for the low-weight pod (a), with the high-weight pod (b) correspondingly +0.03 above its model share. **ratio_error range −0.048..+0.034 (max |err| = 0.048 at the 1:5 cell)**. The low-weight pod systematically underachieves by 4-5 pp at wide ratios; the weight-1 BestEffort pod overachieves (0.019-0.034 achieved vs 0.006-0.013 model) — EEVDF minimum-share granularity. The 1:1 cells match within <1 pp.
- **Request×limit matrix (B)** — throttling is bimodal for a saturating workload: ratio >= 0.98 for every limit <= 1000m (including quota==request cells: 100/100 0.999, 500/500 0.996, 1000/1000 0.980), and 0.0 for limit=2000m (node capacity) regardless of request. Max ratio 0.9997 at 100m/500m (heatmap CSV). **Guaranteed QoS does not disable throttling when the workload saturates its quota.**
- **QoS hierarchy (C)** — two-level weight fact documented in the conversion table below. Achieved share with co-located classes (qos-analyze.py output, 6 rows incl. guaranteed): guaranteed 500m/500m 29.5% (throttled 3023/4000 periods, quota-capped at 0.5 core; pod-slice weight 20), burstable 500m/2000m 66.5% (pod-slice weight 20), besteffort 4.1% (weight 1); guaranteed 1000m/1000m 56.2% (throttled 2693/4013, pod-slice weight 39), burstable 250m/1000m 41.0% (pod-slice weight 10), besteffort 2.8%. The limit cap, not the hierarchy weight, dominates achieved share when limits differ.
- **Latency interference (D)** — p50/p95/p99 (ms) per LS config with batch co-located (latency-analyze.py output, 4 cells):

  | LS config | p50 | p95 | p99 |
  |---|---|---|---|
  | Guaranteed 500/500 | 32.0 | 58.7 | 79.8 |
  | Burstable 250/1000 | 32.0 | 59.7 | 80.7 |
  | Burstable 500/1000 | 32.0 | 57.4 | 77.8 |
  | BestEffort (no req/limit) | 36.7 | 85.3 | **132.2** |

  **BestEffort penalty: p99 132.2ms, +66% over Guaranteed (79.8ms)** purely from weight-1 scheduling (LS pods were idle, ~1.5% CPU, 0 throttled periods). Correlation with throttled time is weakly negative and not meaningful (p50 -0.33, p95 -0.39, p99 -0.37; throttled_usec ~0 across all cells, so the correlation is driven by noise).
- **cpu-burst (E)** — with `cpu.max.burst` applied at 25000 (== quota; burst=100000 was rejected EINVAL by the kernel since burst > quota), throttling is **eliminated**: mean nr_throttled 105 -> 0, throttled_usec 5.28M -> 0 on the same 250m-limit workload.
- **Tunables under contention (F)** — p99 mean (ms) per set (tunables-analyze.py output; n=3 each, slice columns n/a — the dataset has no `eevdf-slices.csv`, so the p99-only significance path was used):

  | Tunable set | base_slice_ns applied | mean_p99 (ms) | std_p99 (ms) |
  |---|---|---|---|
  | default | 1400000 | 87.3 | 0.6 |
  | base-slice-low | 1000000 | 85.0 | 1.1 |
  | base-slice-high | 10000000 | 82.7 | 2.9 |

  **Significance verdict (per the pinned slice-optional rule): BOTH tunable changes are significant** — base-slice-high diff_p99 = −4.7ms vs noise_threshold 2.9ms (significant), base-slice-low diff_p99 = −2.3ms vs noise_threshold 1.1ms (significant). Both lower p99 than default; a larger base_slice (10ms) gave the largest reduction (−4.7ms).

### crun Conversion: CpuShares → cpu.weight

The kubelet converts milliCPU requests to CpuShares using the formula:

```
CpuShares = (milliCPU / 1000) * 1024
```

The crun OCI runtime then converts CpuShares to the cgroup v2 `cpu.weight` value using a logarithmic formula. **Measured mapping** (read from the summary `cpu_weight` column across families A/B/C/E and the earlier throttling-limits family):

| milliCPU | CpuShares | cpu.weight (measured) |
|----------|-----------|-----------------------|
| none     | --        | 1                     |
| 100m     | 102       | 17                    |
| 200m     | 205       | 29                    |
| 250m     | 256       | 35                    |
| 500m     | 512       | 59                    |
| 750m     | 768       | 80                    |
| 800m     | 819       | 84                    |
| 1000m    | 1024      | 100                   |
| 1500m    | 1536      | 138                   |

Notes on the measured mapping:

- The earlier "approx" values (100m->5, 250m->10, 500m->20) were **wrong**; the measured crun conversion is roughly `weight ≈ 0.1 * milliCPU` (100m->17, 1000m->100, 1500m->138).
- **1800m -> 160 is PROBE-DERIVED, not experiment-validated**: no request=1800m cell exists in any dataset (the only 1800m row is `request=100m-limit=1800m`, whose weight 17 comes from the 100m request). The 160 value was extrapolated from the probe kernel formula, not measured. Do not cite it as an experimental result; nearest measured value is 1500m -> 138.
- The besteffort/no-request floor is weight=1 (not 0), which is why BestEffort pods still receive CPU under contention.

#### Two-level weight fact (measured, Family C snapshots)

The same request maps to different `cpu.weight` values at different levels of the cgroup hierarchy:

| Level | cgroup | 500m request | 1000m request |
|-------|--------|--------------|---------------|
| Container | pod container | 59 | 100 |
| Pod slice | `kubepods-pod<uid>.slice` (Guaranteed direct) | 20 | 39 |
| Pod slice | `kubepods-burstable-pod<uid>.slice` | 20 | -- (250m -> 10) |
| QoS slice | `kubepods-burstable.slice` | 28 (20 + 4 + 4 idle pods) | 18 (10 + 4 + 4) |
| QoS slice | `kubepods-besteffort.slice` | 1 | 1 |
| Root | `kubepods.slice` | 79 | 79 |

EEVDF distributes time hierarchically: first among `kubepods.slice` children, then inside each QoS slice among pod slices, then inside the pod slice among containers. The pod-slice weights are ~2.5-3x lower than the container weights, so a pod's effective share depends on the weights of its sibling pods at every level, not only on its container weight.

### Validated Interaction Findings

Six families (weight-share, request×limit, QoS hierarchy, latency interference, cpu-burst, tunables) were run on the 3-node cluster and data-integrity verified. The quantitative conclusions:

1. **The proportional-share model holds.** Under pure contention (requests only, no limits), achieved CPU share tracks `weight_i / Σweight_j` within ~5 pp; 1:1 cells match within <1 pp. The model slightly over-predicts the low-weight pod (by 4-5 pp at 1:4/1:5/1:10 ratios) and under-predicts BestEffort (weight-1) pods, which always receive at least a small share.
2. **Limits override weights.** Once a limit is below demand, achieved share is set by the quota cap (throttling), not by the hierarchy weight — a guaranteed 500m/500m pod achieved 29.5% while a burstable 500m/2000m pod achieved 66.5% in the same cell. Guaranteed QoS protects latency only when usage is far below the (enforced) quota.
3. **Throttling is bimodal for saturating workloads.** Ratio >= 0.98 whenever limit < demand, 0 when limit >= demand. There is no measured middle band; the Caution region is approximate.
4. **`cpu.max.burst` eliminates throttling when applied** (burst=25000 == quota; burst > quota is rejected EINVAL). This is the only tested mechanism that removes throttling without raising the limit.
5. **BestEffort co-location costs ~66% p99 latency** (132.2ms vs 77.8-80.7ms for requested pods) even when the LS pod is idle, purely from weight-1 scheduling.
6. **EEVDF tunables moved p99 by <= 5ms and BOTH changes were significant under the pinned slice-optional rule** (82.7-87.3ms across default/low/high base_slice; base-slice-high diff −4.7ms > noise 2.9ms, base-slice-low diff −2.3ms > noise 1.1ms). Slice-duration columns are n/a — the dataset lacks `eevdf-slices.csv`, so the p99-only significance path was used.

### Validated Interaction Findings — Multi-CPU validation

The 4-vCPU validation re-ran the node-size-dependent families on the single
4-CPU worker `w2` (same kernel/day as the 2-vCPU runs). All numbers
below are traced to the analyzer outputs staged under
`research/analysis/output/v08/` (clean per-family dirs, per-family CSVs,
`report-input/`, `cpu-count-compare/`; `interaction-report.md` and
`multi-cpu-validation.md` in `research/analysis/output/`).

**Weight-share on 4-CPU does NOT show reduced ratio error — the granularity
hypothesis is not supported by the same-cell comparison.** Mean |ratio_error|
is **0.022 on 2-CPU vs 0.115 on 4-CPU** for the same request cells
(`cpu-count-compare.csv`; verdict over the 5 cells present in both runs). The
4-CPU error growth is driven by the low-weight pods: the same request cells
occupy only ~2 of 4 CPUs, so demand does not saturate the 4-CPU node and the
low-weight pod runs on idle capacity (e.g. the 100m pod achieved 0.359 share
vs 0.144 model at 100/1000) while the high-weight pod under-achieves (0.514 vs
0.847). Because the 2-CPU and 4-CPU runs used different workers (w1 vs w2) and
a single 4-CPU node was measured, the 2-vs-4 difference cannot be cleanly
attributed to vCPU count alone (see the node-size caveat below).

| cell ratio | error_2cpu | error_4cpu | delta |
|---|---|---|---|
| 100/1000 | 0.0273 | 0.2223 | +0.195 |
| 100/500 | 0.0319 | 0.1708 | +0.139 |
| 250/1000 | 0.0307 | 0.1505 | +0.120 |
| 500/500 | 0.0115 | 0.0170 | +0.005 |
| 800/800 | 0.0094 | 0.0124 | +0.003 |
| 200/500 | (no 2-CPU data) | 0.0999 | n/a |

**Scaled 4-vCPU block** (requests scaled to the 4-CPU budget,
`cpu-count-4v-scaled.csv`): mean |ratio_error| **0.093** — lower than the
same-cell 4-CPU run (0.115) but still ~4x the 2-CPU 0.022; scaling demand
toward node capacity reduces but does not eliminate the gap.

| cell ratio | error_scaled |
|---|---|
| 1000/1000 | 0.0111 |
| 1500/1500 | 0.0094 |
| 500/1000 | 0.0828 |
| 750/1500 | 0.0860 |
| 600/3000 | 0.1775 |
| 500/3000 | 0.1911 |

**Node-size-dependent families on the 4-CPU worker (w2)** — these differ
materially from the 2-CPU results and should be read as the 4-CPU
measurements:

- **QoS hierarchy (`qos-summary.csv`)** — cell 1 (guaranteed 500m/500m,
  burstable 500m/2000m, besteffort): achieved 14.5% / 49.8% / **35.7%**
  (2-CPU: 29.5% / 66.5% / 4.1%); cell 2 (guaranteed 1000m/1000m, burstable
  250m/1000m, besteffort): 31.6% / 31.3% / **37.1%** (2-CPU: 56.2% / 41.0% /
  2.8%). On 4-CPU with sub-capacity demand the weight-1 BestEffort pod
  receives a large share of idle CPU; the quota-capped guaranteed pod stays
  below its weight share.
- **Latency interference (`latency-summary.csv`)** — p50/p95/p99 (ms) are
  lower and flat across LS configs: Guaranteed 500/500 27.0/45.7/56.0,
  Burstable 250/1000 26.7/45.3/54.7, Burstable 500/1000 26.3/44.7/54.3,
  BestEffort 27.0/46.0/55.3. **The 2-CPU BestEffort p99 penalty (+66%) does
  NOT reproduce on 4-CPU** — idle capacity absorbs the weight-1 neighbor.
  Correlation p99_vs_throttled_usec is +0.95, but the LS pods never throttle
  and throttled_usec is dominated by the batch pod, so treat the correlation
  as spurious.
- **Request×limit heatmap (`heatmap-throttling_ratio.csv`)** — bimodal
  throttling reproduces: ratio >= 0.99 for every limit <= 1000m (max 0.9993
  at 100m/500m), but limit=2000m is **not** 0.0 as on 2-CPU (0.003-0.015): on
  the 4-CPU node 2000m is half of node capacity, not capacity itself, so a
  saturating workload at that quota still sees small residual throttling.
  This refines the "throttling eliminated at limit=2000m" finding to
  "eliminated when the limit reaches the workload's real demand".

**Node-size caveat (updated by the 4-vCPU validation)** — the six-family matrix
was run on 2-vCPU workers. The 4-vCPU validation re-ran weight-share (same
cells + scaled), request×limit, QoS hierarchy, and latency interference on the
single 4-CPU worker `w2`; weight-share now has explicit 4-CPU validation
(above). The re-run families carry a single-node, same-kernel/day caveat.
`cpu-burst` and `tunables-contention` were NOT re-run on 4-CPU: they are
node-size-independent by design (per-cgroup quota burst and global EEVDF
tunables), so their 2-vCPU measurements remain authoritative. The 2-CPU
weight-share data also lacks the 200/500 cell (its 6th config cell was a
duplicate 500m/500m label pre-D05), so the 2-vs-4 comparison covers the 5
cells present in both runs; the 4-CPU side has 6 unique cells.

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

1. **Guaranteed QoS (request == limit) does NOT disable the CFS quota on this cluster.** Measured `cpu.max` equals the request (`100m/100m` -> `10000 100000`), and a workload that saturates its quota is throttled in >= 98% of periods (Family B). Guaranteed QoS still benefits latency-sensitive workloads whose usage stays far below the quota: the 500m/500m API pod used ~1.5% CPU and was never throttled even with a CPU-bound neighbor.

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
| `base_slice_ns` | Base time slice for a weight-100 task | 3,000,000 (3ms) in kernel docs; **measured cluster default 1,400,000 (1.4ms)** | Smaller = more context switches, lower latency; larger = higher throughput |
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
