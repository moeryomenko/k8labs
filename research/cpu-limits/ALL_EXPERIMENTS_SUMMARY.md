# Kubernetes CPU Limits Research: All Experiments

**Date:** 2026-10-27
**Scope:** Phases 1-7
**Runtime platform:** Kubernetes on cgroup v2, with measured runs on
`7.3.0-k8labs` where stated

## Executive summary

The experiments support one main operational rule:

> Set CPU requests from measured demand. Add CPU limits only when a hard cap
> the workload needs a hard cap. Treat a limit as a latency and throughput
> control.

The evidence shows:

- Requests affect placement and cgroup weight.
- Limits create CPU quotas.
- A quota can reduce throughput and increase tail latency while the median
  remains almost unchanged.
- A limit can protect a victim from a noisy neighbour.
- A scheduler swap can change cgroup semantics. `scx_bpfland` and
  `scx_mitosis` did not enforce `cpu.max` in the measured runs.
- `scx_mitosis` used cell isolation instead of CFS weight fairness.
- PREEMPT_RT and sched_ext configure and compile together. The combination
  was not boot-tested.

## Research map

```text
Kubernetes manifest
        |
        v
  request and limit
        |
        +--------------------+
        |                    |
        v                    v
  scheduler placement     cgroup v2 controls
  cpu.weight              cpu.max quota
        |                    |
        +---------+----------+
                  |
                  v
       CPU run time and throttling
                  |
        +---------+----------+
        |                    |
        v                    v
   throughput             tail latency
        |                    |
        +---------+----------+
                  |
                  v
       production outcome
```

## Phase results

### Phase 1: baseline workloads

**State:** measured and implemented.

The project created four workload types:

1. CPU-bound workload using `stress-ng --cpu --cpu-method ackermann`.
2. I/O-bound workload using `stress-ng --hdd`.
3. Memory-bound workload using `stress-ng --vm`.
4. Hybrid workload combining CPU, I/O, and memory work.

These workloads provided repeatable inputs for later phases.

### Phase 2: QoS classes

**State:** measured.

The runs compared BestEffort, Burstable, and Guaranteed pods.

| Workload | QoS | Request | Limit | Recorded result |
|---|---|---:|---:|---:|
| CPU-bound | BestEffort | none | none | 0.3 s on an idle cluster |
| CPU-bound | Burstable | 1000m | 2000m | 121.4 s |
| CPU-bound | Guaranteed | 2000m | 2000m | 121.4 s |
| Hybrid | Burstable | 1000m | 2000m | 121.6 s |
| I/O-bound | Guaranteed | 1000m | 1000m | 8.4 s |
| Memory-bound | Guaranteed | 1000m | 1000m | 122.1 s |

Finite quotas throttled almost every period in runs with an end snapshot:

- CPU-bound Burstable: 1150 of 1150 periods.
- Hybrid: 1186 of 1186 periods.
- Memory-bound: 1170 of 1172 periods.

Nine of fourteen runs had no end snapshot. Their throttling state is unknown.

The run with `cpu.max = max 100000` showed no throttled period.

### Phase 3: BPF tracing infrastructure

**State:** implemented and tested.

The project added a systemd tracing service. It collects:

- `sched:sched_switch`
- `sched:sched_wakeup`
- `sched:sched_wait`

An annotation enables collection for a pod:

```text
kubectl annotate pod POD_NAME bpf-trace=enabled
```

### Phase 4: throttling impact

**State:** measured.

The CPU-bound workload ran with four quota settings.

| Configuration | Request | Limit | Throughput | Change from baseline |
|---|---:|---:|---:|---:|
| Baseline (no CPU quota) | 2000m | none | 139.05 ops/s | baseline |
| Light | 1000m | 3000m | 108.78 ops/s | -21.8% |
| Moderate | 500m | 1500m | 53.57 ops/s | -61.5% |
| Heavy | 100m | 500m | 19.22 ops/s | -86.2% |

The heavy case reached 19.22 ops/s, 86.2% below the no-quota baseline.
The relation was not linear because the workload waited after reaching the
quota boundary.

```text
Throughput
139.05 |##############################  no CPU quota
108.78 |####################          light
 53.57 |############                  moderate
 19.22 |####                          heavy
       +--------------------------------------
        0        50       100       150 ops/s
```

### Phase 5: production guidance

**State:** documented, with some measurements supplied by later phases.

The project documented five deployment patterns:

1. Request-only for most services.
2. Guaranteed for critical services.
3. CPU pinning for dedicated low-latency workloads.
4. Multi-tenant caps for noisy or untrusted workloads.
5. Batch or background workloads with a measured bound.

The guides recommend sizing requests from measured P95 usage. They recommend
removing a CPU limit for latency-sensitive services unless isolation requires a
hard cap.

### Phase 6: planned extensions

**State:** partially superseded by Phase 7.

- Latency testing became Phase 7.1.
- BPF correlation became Phase 7.4.
- Kernel tuning, multi-node fairness, and memory-limit experiments remain
  design-only or unrun.

### Phase 7.1: real application latency

**State:** measured with nginx and a fixed-rate client.

| Pod CPU policy | Throughput | p99 latency |
|---|---:|---:|
| No limit | 7216 req/s | 13.98 ms |
| 120m limit | 3481 req/s | 69.43 ms |
| 70m limit | 2071 req/s | 98.87 ms |

The median changed little while the tail became much worse. At 120m, the cgroup
throttled the workload in 99.7% of periods. A throttling percentage identifies
a candidate. The service p99 shows user impact.

### Phase 7.2: QoS comparison under contention

**State:** measured.

The measured cgroup values were:

| QoS class | `cpu.weight` | nice value | p99 latency |
|---|---:|---:|---:|
| BestEffort | 1 | 19 | 65.44 ms |
| Burstable | 59 | 2 | 31.28 ms |
| Guaranteed | 100 | 0 | 30.99 ms |

BestEffort had the highest p99 in the contention test.

### Phase 7.3: noisy neighbour

**State:** measured.

Capping four CPU burners at 200m reduced their CPU time from 149.1 s to
37.8 s. This is a 75% reduction. The victim's p50 improved by 79%. The p99
change was -2.9% in the cited clean run.

```text
Without cap:  noisy burners  ############################ 149.1 s
With 200m:   noisy burners  ########                      37.8 s
                           victim receives more CPU time
```

This supports a narrow use of limits: cap the offender when protecting other
workloads has higher value than preserving the offender's burst capacity.

### Phase 7.4: live BPF scheduler trace

**State:** measured.

The trace measured wake-to-run delay as the limit tightened:

| CPU policy | Mean wait | Maximum wait |
|---|---:|---:|
| No limit | 6.4 us | 5.5 ms |
| Intermediate limit | 9.6 us | not used as headline |
| Tight limit | 13.7 us | 71.7 ms |

The mean remained small. The maximum exposed quota-period stalls.

```text
task wakes -> runnable queue -> CPU quota available -> task runs
                         |
                         +-- quota exhausted: wait until next period
```

### Phase 7.5: cgroup v2 parameters

**State:** measured on a live node.

The Kubernetes-to-cgroup mapping included:

| Kubernetes value | cgroup v2 value |
|---|---|
| 100m request | `cpu.weight = 17`, nice 8 |
| 100m limit | `cpu.max = 10000 100000` |
| CPU burst configuration | `cpu.max.burst = 0` |

The request and limit use different mechanisms. The request affects relative
weight. The limit creates a quota.

### Phase 7.6: scheduler comparison

**State:** measured on `7.3.0-k8labs`.

The CFS matrix used five modes: `up`, `smp`, `concur`, `max`, and `tasks`.
The sched_ext survey used `concur`. Its clean summary reports results for
`scx_lavd`, `scx_bpfland`, and `scx_mitosis`.

| Scheduler | Quota group CPU time | Weight-400 single-thread share | Probe p99 |
|---|---:|---:|---:|
| CFS | 4.001 CPU-s | 0.285 | 702 us |
| `scx_lavd` | 12.224 CPU-s | 0.143 | not in clean survey |
| `scx_bpfland` | 12.484 CPU-s | not in clean survey | 1427 us |
| `scx_mitosis` | 12.906 CPU-s | not in clean survey | 5139 us |

The quota was 4.0 CPU-s. All three sched_ext schedulers exceeded it. The
weighted-thread scenario showed that thread count affected CPU share. For
example, under CFS the four-thread, weight-100 group received 37.7%, while the
single-thread, weight-400 group received 28.5%. The two-thread, weight-100
limited group received 4.001 CPU-s under CFS and 12.484 CPU-s under
`scx_bpfland` in this survey.

`scx_mitosis` received a separate cell test:

| Scheduler | Cell A, weight 400 | Cell B, weight 100 |
|---|---:|---:|
| CFS | 0.750, 20.745 CPU-s | 0.250, 6.906 CPU-s |
| `scx_mitosis` | 0.499, 12.580 CPU-s | 0.501, 12.609 CPU-s |

```text
CFS:          cell A 75%  #######################
              cell B 25%  ########

scx_mitosis:  cell A 50%  ###############
              cell B 50%  ###############
```

`scx_mavd` and `scx_nitosis` built but did not load on this kernel. Their BPF
loader returned `-EINVAL`. The node BTF did not contain the required
`bpf_call_rcu` and `bpf_rcu_head` symbols. The build team compiled a
`next-20261016` kernel containing the symbols. No one installed or booted it.

PREEMPT_RT and sched_ext also configure and compile together when `pahole` is
available. The combination was not boot-tested. A host without `pahole` can
drop BTF during `olddefconfig`, which then drops sched_ext. That is a build
environment failure, not a Kconfig incompatibility.

### Phase 7.7: production incidents

**State:** measured.

The incident runs showed two different failure modes:

```text
traffic spike
     |
     +--> 120m CPU limit --> 1453 / 3000 req/s, p99 102 ms
     |
     +--> no limit -------> 3000 / 3000 req/s, p99 1.15 ms

node drain
     |
     +--> one pod lost --> no client errors
                         --> replacement took 42 s
```

In one incident run, the HPA did not scale in this cluster. That result
describes this metrics path, not all Kubernetes HPA deployments.

### Phase 7.8: cost model

**State:** modelled, not measured as an invoice.

The model estimated an upper bound of about **$2,755 per month** in wasted
capacity for the throttled scenario. It did not include a supplied revenue
model. Treat this as a sensitivity model, not a production cost measurement.

## Decision flow

```text
Measure CPU usage and service p99
              |
              v
      Is a hard cap required?
          /             \
        no               yes
        |                 |
        v                 v
  Request only      Is isolation the goal?
                        /        \
                      no          yes
                      |            |
                      v            v
              Size limit above   Add limit and
              own p99 demand     monitor victim p99
```

Use CFS as the baseline. Test any sched_ext scheduler against the workload's
actual requirements. Do not assume that `cpu.max` or cgroup weights retain
their CFS meaning after a scheduler swap.

## Evidence index

| Area | Evidence |
|---|---|
| Phase 4 throughput | `phase4/evidence/throttling-summary.json` |
| Phase 7 results | `evidence/phase7/` |
| Scheduler survey | `evidence/phase7/kernel-comparison/survey-clean-20261027T030516Z.json` and `evidence/phase7/kernel-comparison/cpu-bandwidth-20261026T214327Z.json` |
| Mitosis cells | `evidence/phase7/kernel-comparison/cells-mitosis-20261027T031216Z.json` |
| PREEMPT_RT build check | `evidence/phase7/kernel-comparison/preempt-rt-config.md` |
| Full phase status | `phase7/STATUS.md` |

Values marked as modelled or documented are not runtime measurements.
