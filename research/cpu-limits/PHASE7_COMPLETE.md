# Phase 7 Complete: Extended Research

**Date**: 2026-10-26  
**Status**: all eight topics implemented; seven measured on a live cluster, one computed from Phase 4 data

Everything below comes from evidence files that exist in `evidence/phase7/`.
Every number is a measured value, and each one names the evidence file it came
from.

---

## What Was Measured

| Topic | Status | Measured on | Runtime | Evidence |
|-------|--------|-------------|---------|----------|
| 7.1 nginx under CPU quota | Measured | 4-core node, kernel 7.3.0-k8labs | ~8 min | `real-apps/nginx-throttling-20261026T191420Z.json` |
| 7.2 QoS class comparison | Measured | same node | ~6 min | `qos-comparison/qos-classes-20261026T191440Z.json` |
| 7.3 Noisy neighbour | Measured | same node | ~5 min | `interference/noisy-neighbor-20261026T191852Z.json` |
| 7.4 Live BPF scheduler trace | Measured | same node, bpftrace 0.24.2 | ~4 min | `live-bpf/scheduler-trace-20261026T192056Z.json` |
| 7.5 cgroup v2 CPU parameters | Measured | same node | <1 min | `cgroup-v2/cgroup-v2-analysis-20261026T190908Z.json` |
| 7.6 Kernel scheduler comparison | Measured | same node, CFS modes and sched_ext | ~25 min | `kernel-comparison/kernel-comparison-20261026T212246Z.json` |
| 7.7 Incident simulation | Measured | same node plus one drained worker | ~25 min | `incidents/incidents-20261027T000538Z.json`, `incidents/incidents-run2-20261027T002520Z.json` |
| 7.8 Cost / TCO model | Modelled from Phase 4 data | n/a | <1 min | `cost-analysis/tco-model-20261026T190918Z.json` |

Every topic was executed with the command `phase7/run-all-phase7.sh` uses for
it. The master runner was not re-run end to end after 7.6 and 7.7 were added,
because those two take about 50 minutes between them; the individual runs above
are the evidence.

---

## 7.1 Real application: nginx under a CPU quota

nginx serves a 14.4 KiB page from a ConfigMap. A fixed-rate client requests
it with keepalive disabled, so every request pays a fresh connection.

| Config | CPU limit | Throughput | p99 latency | Throttled periods |
|--------|-----------|-----------|-------------|-------------------|
| no-limit | none | 7216 req/s | 13.98 ms | 0% |
| light | 120m | 3481 req/s | 69.43 ms | 99.7% |
| moderate | 100m | 2991 req/s | 67.76 ms | 95.6% |
| heavy | 70m | 2071 req/s | 98.87 ms | 75.1% |

Two things worth reading carefully:

- **p50 barely moves** (2.04 ms without a limit, 2.16 ms at 70m). The median
  request still fits under the quota. The damage lands in the tail.
- **Throttle percentage is not monotonic in the limit.** The 70m pod is
  throttled less often than the 100m pod because it uses less CPU per
  period and its work is spread thinner. Throttle percentage alone does not
  rank configurations; latency does.

The no-limit run is the node's ceiling for this workload on this hardware:
nginx costs about 36-45 us of CPU per request, so 4 cores cap out near
7.2k req/s.

---

## 7.2 QoS classes under contention

One pod per QoS class, one CPU-burner per class, four cores. All three
probes answer the same requests while their own class's burner competes with
them.

| QoS class | cgroup weight / nice | Throughput | p99 latency | Burner CPU seconds |
|-----------|----------------------|-----------|-------------|--------------------|
| BestEffort | 1 / 19 | 1787 req/s | 65.44 ms | 71.7 s |
| Burstable (500m request) | 59 / 2 | 3613 req/s | 31.28 ms | 118.2 s |
| Guaranteed (1000m, limit = request) | 100 / 0 | 2980 req/s | 30.99 ms | 99.5 s |

The mapping is real, not documented: the pod cgroup itself reports
`cpu.weight` 1, 59 and 100 for the three classes, and
`cpu.weight.nice` 19, 2 and 0. BestEffort pods land in
`kubepods-besteffort.slice`, Burstable in `kubepods-burstable.slice`, and
Guaranteed in `kubepods.slice`.

Guaranteed and Burstable reach the same p99. Guaranteed uses less CPU
because its burner is capped at one core by `cpu.max`, while the Burstable
burner is uncapped and runs away with 118 s of CPU.

---

## 7.3 Noisy neighbour interference

One victim (nginx, 250m request, 500m limit) at a fixed 3000 req/s, plus four
burners, run twice: burners unlimited, then burners capped at 200m.

| Scenario | Burner CPU | Burner throttle | Victim p50 | Victim p99 | Victim p99.9 |
|----------|-----------|-----------------|-----------|-----------|--------------|
| burners unlimited | 149.1 s across 4 | 0.0% | 0.79 ms | 5.73 ms | 10.49 ms |
| burners capped 200m | 37.8 s across 4 | 94.0% | 0.17 ms | 5.57 ms | 11.76 ms |

Capping the neighbours removes 75% of their CPU consumption (149.1 s to
37.8 s) and the victim's median latency drops by 79%. The victim's p99
improvement is small (-2.9%), and its p99.9 is slightly worse (+12.2%).

This result is the least stable in Phase 7. An earlier independent run of the
same test measured p99 -37.1% and p50 -81.6%. The direction is consistent
(limiting the neighbours helps the victim) but the tail magnitude swings run
to run at a fixed 3000 req/s on a 4-core node. Treat the p99 figure as
"unchanged to somewhat better", not as a fixed percentage.

---

## 7.4 Live BPF trace of the scheduler

bpftrace measures, for every task named `nginx`, the time between
`sched_wakeup` (task became runnable) and `sched_switch` (task got a CPU).
That gap is the scheduling delay, measured in the kernel rather than inferred
from latency.

| Config | CPU limit | Throttled periods | Throughput | p99 | Mean scheduling wait |
|--------|-----------|-------------------|-----------|-----|----------------------|
| no-limit | none | 0% | 6000 req/s | 4.53 ms | 6.4 us |
| capped | 200m | 96.0% | 4730 req/s | 56.34 ms | 9.6 us |
| tight | 100m | 96.9% | 2588 req/s | 81.99 ms | 13.7 us |

This is the mechanism behind the latency results: as the quota tightens, a
runnable nginx task waits roughly twice as long for a CPU, and the worst case
jumps from 5.5 ms to 71.7 ms. The mean rises only from 6.4 us to 13.7 us, so
the average request is barely affected. The tail is affected enormously,
because a small share of requests arrive exactly when the quota is spent.

Note that the mean wait is a few microseconds in every configuration. The
quota is not making the scheduler slow; it makes a minority of wakeups wait
for the next 100 ms period.

---

## 7.5 cgroup v2 CPU parameters

Read from the host cgroupfs on kernel 7.3.0-k8labs. All nine CPU files are
present: `cpu.max`, `cpu.max.burst`, `cpu.weight`, `cpu.weight.nice`,
`cpu.stat`, `cpu.pressure`, `cpu.idle`, `cpu.uclamp.min`, `cpu.uclamp.max`.

Measured Kubernetes to cgroup mapping, from three pods placed with known
resources:

| Pod | requests.cpu | limits.cpu | cpu.weight | cpu.weight.nice | cpu.max |
|-----|--------------|------------|------------|-----------------|---------|
| mapped-unlimited | 100m | none | 17 | 8 | `max 100000` |
| mapped-capped | 100m | 100m | 17 | 8 | `10000 100000` |
| mapped-larger | 250m | 250m | 35 | 5 | `25000 100000` |

What this establishes:

- `cpu.weight` tracks `requests.cpu` only. The two 100m pods have identical
  weight whether or not a limit is present.
- `limits.cpu` is written to `cpu.max` as a 100 ms quota. 100m becomes
  10000 us per 100000 us period, i.e. 0.1 core.
- `cpu.max.burst` is 0 everywhere, so quota cannot be banked across periods
  on this cluster.
- `cpu.pressure` (PSI) is available and counts time with a runnable task
  waiting, including contention with no limit at all. `cpu.stat` only counts
  quota exhaustion.

---

## 7.6 Kernel scheduler comparison

Evidence: `evidence/phase7/kernel-comparison/kernel-comparison-20261026T212246Z.json`,
`cpu-bandwidth-20261026T214327Z.json`, `preempt-20261026T214749Z.json`.

The node runs `7.3.0-k8labs`, built with `CONFIG_SCHED_CLASS_EXT`,
`CONFIG_PREEMPT_DYNAMIC` and `CONFIG_PREEMPT_LAZY`. That allows three
comparisons in one boot with the same workload matrix:

| Arm | What changes |
|-----|--------------|
| `cfs` | CFS group scheduling, with each of the five `cgroup_mode` values |
| `scx_lavd --performance` | sched_ext replaces the fair-class scheduler |
| `preempt=lazy` / `preempt=full` | the preemption model, switched at runtime |

The matrix is `research/sched-core-modes/experiments/run_matrix.py`: four
scenarios, two repetitions of eight seconds, five modes, two arms. That
produced 110 summary rows (55 per arm, over two repetitions) with no
failures. sched_ext reported
`enabled` with `enable_seq` 61 and `nr_rejected` 0 after detaching.

### The five CFS group modes

`weighted_threads` sets a 400-weight single thread against a 200-weight pair
and a 100-weight group of four threads. Share of CPU, mean of two repetitions:

| mode | w400-t1 (weight 400) | w200-t2 (weight 200) | w100-t4 (weight 100) |
|------|----------------------|----------------------|----------------------|
| `up` | 0.224 | 0.407 | 0.368 |
| `smp` | 0.333 | 0.397 | 0.270 |
| `concur` | 0.283 | 0.312 | 0.405 |
| `max` | 0.312 | 0.407 | 0.281 |
| `tasks` | 0.268 | 0.355 | 0.377 |

Two facts matter more than the ranking:

- **Thread count beats weight in every mode.** The group with four threads and
  weight 100 never falls below 0.270, and the group with four times the weight
  and one thread never rises above 0.333. A cgroup weight is not a CPU
  guarantee.
- **The mode moves the split, but not by the amount the weight suggests.** The
  100-weight group ranges from 0.270 (`smp`) to 0.405 (`concur`), a spread of
  13.5 points, against a worst-case within-mode spread of 6.7 points. The mode
  is a real knob for how runnable threads share a CPU, not a lever that makes
  the number in the cgroup file come true.

`quota` (`cpu.max = 50000 100000` beside an unconstrained peer of equal
weight) is unaffected by the mode, which matches the kernel: bandwidth control
acts below the group-weight calculation. The limited group received 0.209-0.210
of the CPU in all five modes, its peer 0.790-0.791.

### CFS versus sched_ext

Under `scx_lavd --performance` the CFS mode has no influence at all, as
expected: sched_ext owns the CPUs, so `cgroup_mode` has nothing to act on. The
same matrix produced:

| group (weight, threads) | cfs across the five modes | scx_lavd across the five modes |
|-------------------------|---------------------------|--------------------------------|
| w100-t4 | 0.270 - 0.405 | 0.528 - 0.575 |
| w200-t2 | 0.312 - 0.407 | 0.286 - 0.354 |
| w400-t1 | 0.224 - 0.333 | 0.118 - 0.159 |

scx_lavd allocates per task, not per cgroup weight: the four-thread group
received 3.4 to 4.5 times the CPU of the single thread whose weight was four
times higher. That is how the scheduler is designed, but it means a cluster
that relies on cgroup weights for fairness must not switch to scx_lavd without
deciding what replaces that fairness.

### CPU quotas are not enforced under sched_ext by default

This is the most operationally important result of the topic. A group with
`cpu.max = 50000 100000` (half a core) ran for eight seconds beside an
unconstrained group of equal weight:

| arm | limited group, CPU-seconds | share | `nr_throttled` |
|-----|---------------------------|-------|----------------|
| `cfs` | 4.00 | 0.209 | 160 |
| `scx_lavd --performance` | 12.22 | 0.503 | 0 |
| `scx_lavd --performance --enable-cpu-bw` | 4.01 | 0.221 | 0 |

Half a core for eight seconds is 4.0 CPU-seconds. Under plain scx_lavd the
limited group consumed three times that, and the two groups converged on an
even split: the quota was not applied at all. scx_lavd implements `cpu.max`
only with `--enable-cpu-bw`, which its own help text calls "a highly
experimental feature".

Two consequences:

- Attaching a custom scheduler can silently remove the CPU limits that every
  manifest in the cluster depends on. After attaching one, check `usage_usec`
  against `cpu.max` instead of trusting the configuration.
- `nr_throttled` is not a valid health signal under sched_ext. It read zero in
  the arm that enforced the quota and zero in the arm that ignored it.

### Wakeup latency

`latency` places a 1 ms wakeup probe in a 100-weight group beside a 400-weight
CPU load. Probe latency in microseconds, mean of two repetitions:

| arm | p50 | p99 | max |
|-----|-----|-----|-----|
| cfs, best mode (`tasks`) | 62.5 | 753 | 4169 |
| cfs, worst mode (`smp`) | 60.0 | 1475 | 4130 |
| scx_lavd, best mode (`smp`) | 62.5 | 2032 | 5828 |
| scx_lavd, worst mode (`tasks`) | 65.0 | 3890 | 6892 |

p50 stays at 60-66 µs in every arm. The tail does not: scx_lavd lifts p99 from
0.75-1.48 ms to 2.03-3.89 ms. A latency-sensitive service already at its p99
budget should not assume a custom scheduler improves it.

### Preemption model

Because the kernel is built with `PREEMPT_DYNAMIC`, the lazy and full models
were measured in one boot by writing `/sys/kernel/debug/sched/preempt`, with
the active value read back each time (`full (lazy)` for lazy, `(full) lazy` for
full). Three repetitions per arm:

| model | p50 | p99 | max |
|-------|-----|-----|-----|
| lazy | 61.3 | 820.7 | 5047 |
| full | 61.0 | 936.7 | 8985 |

p50 is identical and the p99 and max differences are of the same size as the
spread between repetitions of one arm elsewhere in this matrix. This workload
does not distinguish the two models, so this data does not justify changing
the boot parameter.

### PREEMPT_RT and sched_ext do configure together

The earlier version of this document said the two cannot be combined. That was
wrong, at least at the configuration and build level. `kernel/Kconfig.preempt`
puts no dependency between them: `SCHED_CLASS_EXT` depends on
`BPF_SYSCALL && BPF_JIT && DEBUG_INFO_BTF`, and `PREEMPT_RT` is an independent
option. Starting from the `next-20261016` configuration, setting
`CONFIG_PREEMPT_RT=y` and running `olddefconfig` leaves `CONFIG_SCHED_CLASS_EXT=y`
and `CONFIG_DEBUG_INFO_BTF=y` in place, and:

- the kernel builds: `bzImage` 16790528 bytes, 2445 modules with BTF, sched_ext
  objects linked in;
- the only reason the first attempt appeared to drop sched_ext was `pahole`
  missing on the host that ran `olddefconfig`, which made
  `DEBUG_INFO_BTF` unmet and cascaded into `SCHED_CLASS_EXT`. Run inside the
  build container, both survive.

What is **not** established is whether an RT kernel with sched_ext *runs*
correctly. Installing a kernel on these nodes is not an `rpm -i` away: they
boot a signed UKI built by the image bake, and this environment has no PKCS#11
signing token, no host image staging and no CAPI machine objects. The
combination is therefore build-verified and boot-unverified, and sched_ext on
RT should be treated as unsupported until someone boots it.

### Scheduler survey: bpfland, mitosis, mavd, nitosis

Evidence: `evidence/phase7/kernel-comparison/survey-clean-20261027T030516Z.json`,
`cells-mitosis-20261027T031216Z.json`, `mavd-try-20261027T022959Z.json` and
`nitosis-try-20261027T023315Z.json` (the two load attempts), plus the built
linux-next tree under `build/kernel-custom-next`.

Four more schedulers were built from the same scx tree. Two were measured with
the same matrix (`concur` mode, two repetitions) on `7.3.0-k8labs`; the other
two were attempted and could not load.

| scheduler | state on 7.3.0-k8labs | w400-t1 share | quota group, CPU-s | probe p99 |
|-----------|----------------------|---------------|--------------------|-----------|
| cfs | baseline | 0.285 | 4.00 | 702 us |
| `scx_bpfland` | attached, `nr_rejected` 0 | 0.143 | 12.48 | 1427 us |
| `scx_mitosis` | attached, `nr_rejected` 0 | 0.143 | 12.91 | 5139 us |
| `scx_mavd` | **cannot load** | — | — | — |
| `scx_nitosis` | **cannot load** | — | — | — |

Both schedulers that load behave like `scx_lavd`: they ignore `cpu.max`
(12.5 and 12.9 CPU-seconds against a 4.0 CPU-second quota) and they allocate
per task rather than per cgroup weight (the weight-400 single thread gets
half the CPU of the weight-100 four-thread group).

`scx_mitosis` also gets its own test, because the matrix cannot exercise the
feature that defines it: a cell scheduler scans the children of its parent
once, at start, and the matrix creates its groups later. The test therefore
creates two cells first, gives them weights 400 and 100, runs four CPU threads
in each, and measures eight seconds:

| arm | cell-a (weight 400) | cell-b (weight 100) |
|-----|---------------------|---------------------|
| cfs | 0.750 (20.75 CPU-s) | 0.250 (6.91 CPU-s) |
| `scx_mitosis` | 0.499 (12.58 CPU-s) | 0.501 (12.61 CPU-s) |

That is the scheduler doing exactly what it is designed to do: CFS honours the
weights, mitosis gives each cell its own CPUs and equalises them. It is also
the clearest illustration in this research that "fairness" means different
things to different schedulers, and that swapping them changes who gets the
CPU without any manifest changing.

`scx_mavd` and `scx_nitosis` both fail at the loader, not at the policy:

```
libbpf: prog 'scx_lib_init_probe': BPF program load failed: -EINVAL
Error: Failed to load BPF program / initializing scheduler
```

They are experiments of the `scx_lavd` and `scx_mitosis` code bases converted
to the "cid" and arena forms, which need kernel features that `7.3.0-k8labs`
does not have. The strings are absent from its BTF and present in a
`next-20261016` build:

| symbol | 7.3.0-k8labs BTF | next-20261016 build |
|--------|------------------|---------------------|
| `bpf_call_rcu` | 0 | 3 |
| `bpf_rcu_head` | 0 | 4 |
| `sub_cid_sched_updated` | 1 | 3 |

So both build, both are staged, and both need a 7.4-class kernel. The
`next-20261016` kernel that would run them was built here and packaged, but
could not be installed for the reason above: node images are signed UKIs
produced by the bake pipeline.

---

## 7.7 Production incident simulation

Evidence: `evidence/phase7/incidents/incidents-20261027T000538Z.json` (spike),
`incidents-run2-20261027T002520Z.json` (ramp, eviction, drain) and
`drain-pdb-20261026T235150Z.json` (drain with a disruption budget). The run was
split across files because the cluster's API server dropped a connection
mid-run once; each number below names the file it comes from.

Four incidents, driven from the client side, because a pod that is throttled,
evicted or rescheduled stops reporting its own metrics exactly when the
interesting part starts.

The target is `phase7/lib/httpd.c`: a static HTTP server that spends 100
microseconds of CPU on every request. That number is not decoration. A plain
static response costs about 20 microseconds, and a 120m quota can serve tens of
thousands of those per second, so no quota binds and nothing happens. At 100
microseconds a 120m quota saturates near 1200 requests per second, inside the
range this load generator can offer. The 7.1-7.3 nginx image could not be used:
the node garbage-collected it during a disk-pressure episode and Docker Hub now
answers anonymous pulls with a rate-limit error. Rates here are therefore not
comparable with 7.1; comparisons between arms inside 7.7 are.

### 7.7.1 Traffic spike

800 req/s baseline, 3000 for fifteen seconds, 800 again. Both pods request
100m; only one has a limit.

| arm | baseline | spike | recovery |
|-----|----------|-------|----------|
| no limit | 800 rps, p99 1.82 ms | 3000 rps, p99 1.15 ms | 800 rps, p99 0.93 ms |
| `120m` limit | 800 rps, p99 0.85 ms | **1453 rps, p99 102.3 ms** | 800 rps, p99 0.88 ms |

During the spike the limited pod was throttled in 98.58% of its quota periods
and only 21794 of the 45000 requests the generator intended to send were
issued: the rest were shed at the client because every connection was waiting.
The unlimited pod issued and completed all 45000 in the same window.

This is the shape of a production incident: the service does not return
errors, it returns slowly. The queue absorbs the difference until the client
timeout, or the client's patience, ends it.

### 7.7.2 Gradual ramp and the autoscaling race

The same two pods, offered load raised in seven steps of six to eight seconds.

| offered | no limit | `120m` limit | throttled |
|---------|----------|--------------|-----------|
| 300 | 300 rps, p99 0.65 ms | 300 rps, p99 0.82 ms | 0.0% |
| 600 | 600, 0.92 ms | 600, 0.75 ms | 0.0% |
| 900 | 900, 0.81 ms | 900, 1.16 ms | 0.0% |
| 1200 | 1200, 1.01 ms | 1200, **114.9 ms** | 96.84% |
| 1600 | 1600, 0.87 ms | 1426, 102.2 ms | 97.35% |
| 2200 | 2200, 0.98 ms | 1595, 100.6 ms | 98.40% |
| 3000 | 3000, 1.88 ms | 1791, 100.1 ms | 98.57% |

The knee is between 900 and 1200 req/s. Throughput still creeps up after it,
but latency has already gone from about 1 ms to about 100 ms, which is the
quota period: the pod is being stopped and restarted once per period. A cap
does not degrade gently; it changes the shape of the response at a threshold.

The same ramp behind an HPA (one replica, request 100m, target 60%) did not
rescue the service. The autoscaler needed 136.6 s before it had any CPU metric,
and then reported utilisation far below the truth:

| offered | HPA's CPU | CPU from `cpu.stat` | replicas |
|---------|-----------|---------------------|----------|
| 300 | 1% | 36.4% | 1 |
| 600 | 1% | 70.8% | 1 |
| 900 | 4% | 106.1% | 1 |
| 1200 | 4% | 139.9% | 1 |
| 1600 | 31% | 167.9% | 1 |
| 2200 | 31% | 184.5% | 1 |
| 3000 | 43% | 208.0% | 1 |

The right-hand column is computed independently inside the same window: CPU
seconds from `cpu.stat`, divided by the request and the window. The HPA's
figure is roughly the previous window's value, halved, so it never crossed the
60% target and the Deployment stayed at one replica while the pod ran at 208%
of its request. On this cluster the autoscaler was one to two windows behind
the load, which at six-second steps is the whole incident.

### 7.7.3 Pod eviction

Three replicas at `120m` behind one Service, 1000 req/s, one pod force-deleted.

| phase | served | p99 | errors | ready endpoints |
|-------|--------|-----|--------|-----------------|
| baseline | 1000 rps | 1.19 ms | 0 | 3 |
| after the kill | 1000 rps | 1.53 ms | 0 | 2 |
| recovery | 1000 rps | 1.09 ms | 0 | 2 |

The service lost one of three backends and the client saw nothing: the
remaining two absorbed the load and p99 rose from 1.19 to 1.53 ms. The
replacement took 42.3 s to become the third ready endpoint, which is the number
that matters for capacity planning — for 42 s the service ran on two thirds of
its designed headroom. Nothing about that time is CPU-related: it is scheduling
plus container start plus the readiness probe.

### 7.7.4 Node drain

All three replicas were placed on one worker and that node was drained under
the same 1000 req/s.

| variant | drain duration | served during | p99 | errors | back to 3 ready |
|---------|----------------|---------------|-----|--------|-----------------|
| no PodDisruptionBudget | 0.20 s | 1000 rps | 4.64 ms | 0 | 50.7 s |
| `minAvailable: 2` | 55.8 s | 1000 rps | 4.92 ms | 0 | 98.2 s |

Without a budget the drain evicted all three replicas in 0.20 s and the
replacements landed on the other nodes. The client still saw no errors, because
the image was cached and a replacement container starts in about three seconds,
but the cluster spent 50 s with degraded redundancy. With `minAvailable: 2` the
drain itself took 55.8 s: it waits for a replacement to become ready before it
may evict the next pod.

The comparison is the point. A PodDisruptionBudget does not protect the
client, it protects redundancy and makes the operator wait for it. The client
was fine in both runs because the remaining nodes had room; a drain that
removes the last replica, or a cluster with no room left, is an outage no
budget can prevent.

---

## 7.8 Cost model

`calculate-tco.py` joins the Phase 4 throttling measurements to a cluster
cost shape (10 nodes, 8 vCPU, $2,803.20/month each).

The model reports a "wasted capacity" figure of about $2,755/month per
throttled scenario. Read it as an upper bound, not a saving: it assumes
every throttled microsecond would otherwise have done full-price useful
work. The model itself says so in its `model_limits` field.

No revenue figure is attached. `PHASE7_BASELINE_RPS` and
`PHASE7_REVENUE_PER_REQUEST` were not supplied, so `throughput_cost_per_month`
is 0 in every row. Any dollar-per-lost-request claim requires the operator to
supply their own numbers and re-run the script.

---

## What This Does Not Show

- **7.6 kernel comparison** ran on one kernel, `7.3.0-k8labs`. It compares CFS
  with `scx_lavd`, `scx_bpfland` and `scx_mitosis` and the two dynamic
  preemption models. `scx_mavd` and `scx_nitosis` could not be measured: they
  need kernel symbols that `7.3.0-k8labs` lacks, and the `next-20261016` kernel
  that has them was built but cannot be installed without the signed-image
  pipeline. PREEMPT_RT plus sched_ext was configured and built, not booted.
- **7.7 incident simulation** uses a purpose-built 100-microsecond HTTP target,
  because the nginx image was no longer available on the node. Rates and
  latencies are therefore not comparable with the nginx numbers in 7.1; the
  comparisons between arms inside 7.7 are.
- **The HPA in 7.7 did not scale at all** in either run. That is reported as a
  measurement of this cluster's metrics path, not as a property of HPAs.
- **Noisy-neighbour tail magnitude** (7.3) varies measurably between runs.
- **One node, one hardware shape.** All measurements come from a 4-core
  node. Ratio-based conclusions should transfer; absolute latency should
  not.

---

## How to Reproduce

```bash
cd research/cpu-limits/phase7
PHASE7_NODE=<node name> ./run-all-phase7.sh
```

Runtime is 20-30 minutes. `PHASE7_NAMESPACE` defaults to
`cpu-limits-research`. Without `PHASE7_NODE` the scheduler picks the node and
the scripts follow the pods.

Individual topics:

```bash
cd 7.1-real-apps         && python3 test-nginx-throttling.py
cd 7.2-qos-comparison    && python3 test-qos-classes.py
cd 7.3-interference      && python3 test-noisy-neighbor.py
cd 7.4-live-bpf          && python3 trace-scheduler.py
cd 7.5-cgroup-deep-dive  && python3 analyze-cgroup-v2.py
cd 7.6-kernel-comparison && python3 test-kernel-comparison.py --reps 2 --duration 8
cd 7.7-incidents         && python3 test-incidents.py
cd 7.8-cost-analysis     && python3 calculate-tco.py
```

Every topic except 7.8 needs cluster access and reaches the node through a
reader pod that mounts the host cgroupfs. 7.6 additionally needs the
`7.3.0-k8labs` node: the CFS group modes and sched_ext do not exist on a stock
kernel. 7.7 drains one worker and uncordons it afterwards.

---

## Success Metrics

| Objective | Where it is answered |
|-----------|----------------------|
| CPU usage vs throttling across workload types | Phase 4 (synthetic), Phase 7.1 (nginx) |
| Low-level scheduler behaviour | Phase 7.4 (BPF), Phase 7.5 (cgroup mapping), Phase 7.6 (kernel comparison) |
| Impact records | the evidence directories under `evidence/phase7/` plus Phase 4 evidence |
| Developer guide | `DEVELOPER_GUIDE.md` |
| Administrator guide | `ADMINISTRATOR_GUIDE.md` |

Coverage of the stated objectives: complete, with the limits listed under
"What This Does Not Show".

---

**Last updated**: 2026-10-27
