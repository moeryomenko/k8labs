# Kubernetes CPU Limits: Administrator Guide

**Cluster operators' guide to CPU scheduling, cgroup parameters, kernel tuning,
and custom schedulers. Measurements are cited to evidence; documentation-only
sections are labelled as such.**

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Kernel Scheduler Fundamentals](#kernel-scheduler-fundamentals)
3. [CFS Bandwidth Control](#cfs-bandwidth-control)
4. [Tuning Scheduler Parameters](#tuning-scheduler-parameters)
5. [Scheduler Selection (UP vs SMP)](#scheduler-selection-up-vs-smp)
6. [Custom Schedulers (sched_ext)](#custom-schedulers-sched_ext)
7. [Observability and Monitoring](#observability-and-monitoring)
8. [Capacity Planning](#capacity-planning)
9. [Troubleshooting](#troubleshooting)

---

## Executive Summary

### What Was Measured

All numbers below come from evidence files in this repository. Every measured
run used one 4-core node running `7.3.0-k8labs` unless stated otherwise.

| Finding | Measured value | Evidence |
|---------|----------------|----------|
| CPU-bound throughput loss at a 500m limit | 139.05 -> 19.22 ops/sec (-86.2%) | `phase4/evidence/throttling-summary.json` |
| p99 latency, nginx with a 120m limit | 13.98 ms -> 69.43 ms | `evidence/phase7/real-apps/nginx-throttling-20261026T191420Z.json` |
| p50 latency, same run | 2.04 ms -> 2.16 ms (unchanged) | same file |
| Mean wake-to-run wait at a 100m limit | 6.4 us -> 13.7 us, worst 5.5 ms -> 71.7 ms | `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json` |
| BestEffort vs Burstable p99 under contention | 65.44 ms vs 31.28 ms | `evidence/phase7/qos-comparison/qos-classes-20261026T191440Z.json` |
| Capping noisy neighbours | Their CPU use fell 75% (149.1 s -> 37.8 s); victim p50 -79% | `evidence/phase7/interference/noisy-neighbor-20261026T191852Z.json` |
| requests.cpu -> `cpu.weight` | 100m -> 17, 250m -> 35 | `evidence/phase7/cgroup-v2/cgroup-v2-analysis-20261026T190908Z.json` |
| `cpu.max` under `scx_lavd` | ignored (12.22 CPU-s against a 4.0 CPU-s quota) unless `--enable-cpu-bw` | `evidence/phase7/kernel-comparison/cpu-bandwidth-20261026T214327Z.json` |
| 3000 req/s spike on a 120m pod | 1453 req/s served at p99 102.3 ms; the unlimited pod served 3000 at p99 1.15 ms | `evidence/phase7/incidents/incidents-20261027T000538Z.json` |

The pattern that matters operationally: **a CPU limit leaves the median alone
and destroys the tail.** It does not degrade evenly, so averages and average
CPU utilisation will not show the problem.

### Default Stance for Cluster Ops

**For most clusters**: let applications burst. Set pod `requests.cpu` for
bin-packing and fairness, and avoid `limits.cpu` except where it buys
something concrete.

**Cases where a CPU limit is worth its cost** (reasoning, consistent with the
measured mechanism):

- Multi-tenant clusters with untrusted or noisy workloads
- Enforcing departmental quotas so one team cannot exhaust a node
- Running code you do not control
- Batch jobs, where the limit equals the request so no forced wait occurs

Throttle percentages are a screening signal, not a severity score. Phase 7.1
measured the 70m configuration throttled in 75.1% of periods with the *worst*
p99 (98.87 ms), while the 100m configuration was throttled in 95.6% of periods
with a better p99 (67.76 ms). Rank candidates by latency.

---

## Kernel Scheduler Fundamentals

### CFS (Completely Fair Scheduler)

Kubernetes CPU limits are implemented by **CFS bandwidth control**. On a
cgroup v2 host the interface is `cpu.max`:

```
cpu.max = "<quota> <period>"     # microseconds
period  = 100000                 # 100 ms, kubelet default
quota   = limits.cpu * 100000

Example: limits.cpu = "2.5" -> "250000 100000"
```

This was verified on the test node, not assumed. Phase 7.5 measured a pod with
`limits.cpu: 100m` holding `cpu.max` = `10000 100000`, and a pod with
`limits.cpu: 250m` holding `25000 100000`.
Evidence: `evidence/phase7/cgroup-v2/cgroup-v2-analysis-20261026T190908Z.json`.

**How it works**:

1. A container's tasks accumulate CPU time during the period
2. When the quota is exhausted the cgroup is **throttled**
3. Tasks are prevented from running until the next period begins
4. The quota is replenished and they run again

**Critical**: throttling is **all-or-nothing**. There is no gradual slowdown;
the cgroup is stopped for the remainder of the period.

Phase 7.4 measured the consequence directly with bpftrace, as the delay between
`sched_wakeup` and `sched_switch` for tasks named `nginx`:

| Limit | Throttled periods | Mean wait | Worst wait |
|-------|-------------------|-----------|------------|
| none | 0% | 6.4 us | 5.5 ms |
| 200m | 96.0% | 9.6 us | 6.9 ms |
| 100m | 96.9% | 13.7 us | 71.7 ms |

The mean barely moves. A minority of wakeups land just after the quota is spent
and wait out the rest of the period, which is why the tail explodes while the
average looks healthy.

### Scheduler Parameters (sysctl)

Inspect the live values on your nodes before assuming the documented defaults:

```bash
sysctl kernel.sched_latency_ns \
       kernel.sched_min_granularity_ns \
       kernel.sched_wakeup_granularity_ns
```

Common upstream defaults are `sched_latency_ns` 6 ms,
`sched_min_granularity_ns` 0.75 ms and `sched_wakeup_granularity_ns` 1 ms, but
distribution and kernel version change them (newer kernels scale them by CPU
count). Treat the numbers `sysctl` prints as authoritative.

**Meanings**:

- **`sched_latency_ns`**: target time for all runnable tasks to run once.
  Lower is more responsive, at higher overhead.
- **`sched_min_granularity_ns`**: minimum time slice per task; bounds context
  switching.
- **`sched_wakeup_granularity_ns`**: how readily a waking task preempts the
  current one.

### When to Tune

**Defaults are right for almost all clusters.** No experiment in this
repository tuned these parameters, so none of this section is a measured
result.

Consider tuning only if a measurement shows a problem:

- A specific workload misses a latency SLO that removing CPU limits did not fix
- Profiling shows scheduler overhead itself, not quota throttling, is the cost
- You have a dedicated batch or HPC pool with known, uniform workloads

Before tuning, confirm the problem is not the quota. Phase 4 and 7.1 show that
throttling, not the scheduler's timeslices, is the usual cause of tail latency
on Kubernetes.

---

## CFS Bandwidth Control

### Checking Current Settings

cgroup v2 on a Kubernetes node keeps pods under `kubepods.slice`. The exact
path depends on the QoS class, and the pod UID appears in the directory name
**with hyphens intact**:

```
Burstable:  /sys/fs/cgroup/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod<uid>.slice/
BestEffort: /sys/fs/cgroup/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod<uid>.slice/
Guaranteed: /sys/fs/cgroup/kubepods.slice/kubepods-pod<uid>.slice/
```

Inside the pod slice each container has its own `crio-<container-id>.scope`.
**Read the container scope, not the sandbox.** The sandbox (pause) container
does not run your workload, so its counters stay near zero and will mislead
you. Phase 7's collector reads container scopes for exactly this reason.

```bash
POD=$(kubectl get pod <pod-name> -n <ns> -o jsonpath='{.metadata.uid}')

# Find the pod slice regardless of QoS class
SLICE=$(find /sys/fs/cgroup/kubepods.slice -maxdepth 2 -name "*pod${POD}.slice" | head -1)

# Container scopes inside it
ls "$SLICE"

# Quota and period
cat "$SLICE"/crio-*/.cpu.max
# 10000 100000   = 0.1 core
# max 100000     = no limit

# Throttling counters
cat "$SLICE"/crio-*/.cpu.stat
# nr_periods 697
# nr_throttled 666
# throttled_usec 35461040
```

`throttled_usec` is microseconds of forced wait. Above, 35461040 us is 35.5
seconds across the container's lifetime.

**Throttle percentage**:

```
throttle_pct = nr_throttled / nr_periods * 100
```

Use it to find candidates. Then look at latency to decide.

### The Other CPU Files

Phase 7.5 confirmed all nine CPU control files exist on the host
`kubepods.slice` with kernel `7.3.0-k8labs`:

`cpu.max`, `cpu.max.burst`, `cpu.weight`, `cpu.weight.nice`, `cpu.stat`,
`cpu.pressure`, `cpu.idle`, `cpu.uclamp.min`, `cpu.uclamp.max`.

Measured behaviour:

| File | What Kubernetes puts there |
|------|----------------------------|
| `cpu.weight` | From `requests.cpu` only: 100m -> 17, 250m -> 35 |
| `cpu.weight.nice` | The nice-value mirror of the weight: 100m -> 8, 250m -> 5 |
| `cpu.max` | From `limits.cpu`: 100m -> `10000 100000`; no limit -> `max 100000` |
| `cpu.max.burst` | 0 — quota cannot be banked across periods |
| `cpu.pressure` | PSI; counts runnable-but-waiting time, including contention with no limit |

`cpu.weight` ignores `limits.cpu`: the two pods requesting 100m had weight 17
with and without a 100m limit. If you want a pod to get a bigger share of a
contended node, raise its **request**. If you want to bound what it can take,
set a **limit**.

`cpu.pressure` is often the more useful signal because it fires during
contention as well as throttling; `cpu.stat` only counts quota exhaustion.

### Global Bandwidth Tuning (Advanced)

The quota period is set per cgroup, not globally. On cgroup v2 the value lives
in `cpu.max` itself, so there is no node-wide `cpu.cfs_period_us` file to
write. (That file is the cgroup **v1** interface. Do not look for it on a v2
node; Phase 7.5's inventory of the v2 host shows only the v2 names above.)

**For Kubernetes**, the period is a kubelet setting:

```yaml
# /var/lib/kubelet/config.yaml
cpuCFSQuotaPeriod: 100ms   # default
```

Changing it means restarting kubelet on every node and re-measuring.

**Trade-off** (mechanism, not measured here): a longer period allows longer
bursts before throttling, but a throttled container then waits longer for
replenishment. With a 1 s period the worst-case wait is 1 s instead of 100 ms.
Given the 71.7 ms worst-case wait Phase 7.4 already measured at 100 ms, a
longer period makes the tail worse, not better.

**Recommendation**: leave it at the default unless you have measured that
100 ms granularity is the problem.

---

## Tuning Scheduler Parameters

Neither profile below was measured here. They are documented starting points,
not results. Validate any change with the procedure in
[Validation](#validation).

### Latency-Optimized (Interactive Workloads)

```bash
cat > /etc/sysctl.d/99-k8s-latency.conf <<'EOF'
kernel.sched_latency_ns = 4000000
kernel.sched_min_granularity_ns = 500000
kernel.sched_wakeup_granularity_ns = 500000
EOF
sysctl --system
```

**Expected effect**: finer timeslices, more context switches, lower
wake-to-run latency.

**When to consider it**: user-facing API clusters that still miss SLOs after
CPU limits are removed.

### Throughput-Optimized (Batch Workloads)

```bash
cat > /etc/sysctl.d/99-k8s-throughput.conf <<'EOF'
kernel.sched_latency_ns = 12000000
kernel.sched_min_granularity_ns = 1500000
kernel.sched_wakeup_granularity_ns = 2000000
EOF
sysctl --system
```

**Expected effect**: longer timeslices, fewer context switches, higher
throughput per core.

**When to consider it**: dedicated ML training, encoding farms, data
warehouses with uniform CPU-bound pods.

### Validation

Measure the thing you changed, before and after.

**Context switch rate**:

```bash
# Requires bpftrace and root; 5 s sample
sudo bpftrace -e 'tracepoint:sched:sched_switch { @[comm] = count(); } interval:s:5 { exit(); }'
```

**Wake-to-run latency for a specific process** — the same shape as the Phase
7.4 probe, which is the one measurement here that directly reflects scheduler
responsiveness:

```bash
sudo bpftrace -e '
tracepoint:sched:sched_wakeup /comm == "nginx"/ { @start[args->pid] = nsecs; }
tracepoint:sched:sched_switch /@start[args->next_pid] != 0/ {
  @us = hist((nsecs - @start[args->next_pid]) / 1000);
  delete(@start[args->next_pid]);
}'
```

**Application latency**: measure p50 and p99 from a client at a fixed offered
rate. If you only look at the mean you will miss the effect that matters; in
Phase 7.1 the mean tracking p50 hid a 7x p99 increase.

---

## Scheduler Selection (UP vs SMP)

> **Measured, on one kernel.** Phase 7.6 ran the same workload matrix on
> `7.3.0-k8labs` under CFS, under `scx_lavd --performance` and under both
> dynamic preemption models. What it did **not** do is compare different
> kernels: no distribution kernel, no PREEMPT_RT (which cannot be combined with
> sched_ext) and no other sched_ext scheduler was measured.

### Kernel Scheduler Variants

Linux supports several scheduler configurations, chosen at **compile time**
plus runtime parameters:

1. **UP (uniprocessor)**: single-CPU scheduler, no cross-CPU balancing
2. **SMP (symmetric multiprocessing)**: the normal multi-core kernel with CFS
3. **PREEMPT variants**: voluntary/full preemption change worst-case latency
4. **PREEMPT_RT**: hard real-time, near-deterministic
5. **sched_ext**: BPF-programmable scheduler class (Linux 6.12+ upstream)

**Check the current kernel**:

```bash
uname -a
# "SMP" in the version string -> SMP build
# "PREEMPT_RT" -> real-time kernel
# "PREEMPT_DYNAMIC" -> preemption mode selectable at boot

# Preemption mode actually in use
cat /sys/kernel/debug/sched/preempt 2>/dev/null

# Build configuration (if /boot/config-* is present)
grep -E 'CONFIG_PREEMPT|CONFIG_HZ_|CONFIG_SCHED_CLASS_EXT' /boot/config-$(uname -r)
```

### When Each Fits

| Cluster type | Kernel | Reasoning |
|--------------|--------|-----------|
| General-purpose | SMP, PREEMPT_DYNAMIC | Well tested; the node measured here runs this |
| Latency-critical | SMP with full preemption or low-latency build | Lower worst-case scheduling delay — but see the measurement below |
| Batch / HPC | SMP, throughput-tuned | Maximise useful CPU time per core |
| Edge / single-core | UP build | Avoids SMP overhead |
| Hard real-time | PREEMPT_RT | Bounded preemption |

**Worth stating plainly**: on Kubernetes, the largest tail-latency contributor
measured in this research was the CPU **quota** (Phase 7.1, 7.4), not the
scheduler build. A real-time kernel does not help a throttled container, and
replacing the scheduler is a large change to make before removing a CPU limit.

**The preemption model made no measurable difference here.** Phase 7.6 measured
a 1 ms wakeup probe beside a CPU-hogging neighbour under both models, switching
them at runtime through `/sys/kernel/debug/sched/preempt`:

| model | p50 | p99 | max |
|-------|-----|-----|-----|
| lazy | 61.3 us | 820.7 us | 5047 us |
| full | 61.0 us | 936.7 us | 8985 us |

p50 is identical and the tail differences are the size of the run-to-run
spread. A boot-parameter change is not justified by this workload. Read the
file as `(full) lazy` when full is active and `full (lazy)` when lazy is
active: the active model is the parenthesised one.

### Low-Latency Kernel (Ubuntu)

```bash
apt install linux-image-lowlatency linux-headers-lowlatency
update-grub
reboot
uname -r | grep lowlatency
```

This enables full preemption and a 1000 Hz tick, which shortens worst-case
scheduling delay at a small throughput cost.

### Real-Time Kernel (PREEMPT_RT)

```bash
apt install linux-image-rt-amd64   # availability depends on release
```

**Trade-offs** (documented, not measured here): better bounded latency, lower
throughput, narrower driver support.

Use it only for genuine hard deadlines (industrial control, telco, audio), not
to rescue a workload that is simply being throttled.

---

## Custom Schedulers (sched_ext)

> **Measured on `7.3.0-k8labs`, with `scx_lavd` 1.1.4.** The scheduler was
> built from the scx tree and attached with `--performance`; the kernel
> reported `state=enabled`, `enable_seq` 61 and `nr_rejected` 0. Read the
> warnings below before doing the same on a cluster that uses CPU limits.

### What sched_ext Is

sched_ext lets a **BPF program implement the scheduling policy**. It was
merged upstream in Linux 6.12. Policies can target workload-specific behaviour,
multi-tenant fairness beyond CFS weights, power-aware placement, or
co-scheduling with accelerators.

Examples from the upstream project: `scx_rusty` (topology-aware,
Meta), `scx_lavd` (latency-criticality aware, Valve), `scx_central`
(centralised decisions).

### Checking Support

```bash
uname -r                                  # needs 6.12+, or a patched vendor kernel
zgrep SCHED_CLASS_EXT /proc/config.gz     # CONFIG_SCHED_CLASS_EXT=y

git clone https://github.com/sched-ext/scx
cd scx && make && sudo make install
```

Note that vendor kernels ship sched_ext at different versions than upstream,
and the scheduler runs against a versioned BPF interface. Check
`/sys/kernel/sched_ext/` and the project's compatibility table rather than
assuming a newer kernel is enough.

### Running a Scheduler

```bash
sudo scx_rusty --help      # every scheduler documents its own knobs
sudo scx_rusty             # Ctrl-C returns the system to CFS
```

sched_ext schedulers can be enabled and disabled at runtime, so a failed
policy does not require a reboot. Do test that failover on a non-production
node first.

### What `scx_lavd` Does To Kubernetes Guarantees (Measured)

Two results from Phase 7.6 matter more than any tuning advice, because both are
silent: nothing in the cluster reports an error when they happen.

**1. It does not enforce `cpu.max` by default.** A cgroup with a half-core
quota (`50000 100000`) ran for eight seconds beside an unconstrained peer:

| scheduler | limited group, CPU-seconds | share | `nr_throttled` |
|-----------|---------------------------|-------|----------------|
| CFS | 4.00 | 0.209 | 160 |
| `scx_lavd --performance` | 12.22 | 0.503 | 0 |
| `scx_lavd --performance --enable-cpu-bw` | 4.01 | 0.221 | 0 |

Half a core for eight seconds is 4.0 CPU-seconds. Under plain `scx_lavd` the
"limited" group consumed three times that and the two groups converged on an
even split. The quota is ignored unless the scheduler is started with
`--enable-cpu-bw`, which its own help text calls "a highly experimental
feature".

**2. `nr_throttled` stops being a health signal.** It read zero both in the arm
that enforced the quota and in the arm that ignored it. After attaching a
sched_ext scheduler, check `usage_usec` against `cpu.max` instead of trusting
`cpu.stat`.

**3. cgroup weights are not honoured.** The same matrix gave the group with
four threads and weight 100 between 0.528 and 0.575 of the CPU, and the group
with one thread and weight 400 between 0.118 and 0.159 — 3.4 to 4.5 times less
CPU for four times the weight. The scheduler allocates per task, not per cgroup
weight. Under CFS the weight-400 group never exceeded 0.333, but the four-thread
group never fell below 0.270 either, so neither scheduler turns weight into a
guarantee.

**4. The tail latency got worse, not better.** With a 1 ms wakeup probe beside a
CPU load, CFS measured p99 753-1475 us and `scx_lavd` measured p99 2032-3890 us;
p50 was unchanged at 60-66 us. A latency-critical service at its p99 budget
should not expect an improvement.

If a workload depends on either quota enforcement or cgroup fairness, keep CFS
or budget for the work of re-implementing both.

### When To Consider It

Only if all of these hold:

- Default CFS with correct requests and no CPU limits still misses an SLO
- You do not rely on `cpu.max` to bound a tenant, or you accept
  `--enable-cpu-bw` and its experimental status
- You do not rely on cgroup weight for fairness between tenants
- You have measured the remaining latency and know it is scheduler-side
  (wake-to-run delay, not quota throttling — Phase 7.4's probe tells you which)
- You have kernel and BPF expertise on staff
- You accept running a non-default scheduler in production

**Warning**: sched_ext is new. A bug in the policy affects every task on the
node, including kubelet and the container runtime.

---

## Observability and Monitoring

### Node-Level Metrics

```bash
# Aggregate node cgroup
cat /sys/fs/cgroup/kubepods.slice/cpu.stat

# Throttling per pod container scope, with the pod name for context
for scope in /sys/fs/cgroup/kubepods.slice/*pod*/crio-*; do
  printf '%s ' "$scope"
  awk '/nr_throttled|nr_periods/ {printf "%s=%s ", $1, $2}' "$scope/cpu.stat"
  echo
done
```

**BPF tracing of scheduling delay** — this is the probe used for Phase 7.4,
and it is what distinguishes quota throttling from general contention:

```bash
sudo bpftrace -e '
tracepoint:sched:sched_wakeup /comm == "myapp"/ { @start[args->pid] = nsecs; }
tracepoint:sched:sched_switch /@start[args->next_pid] != 0/ {
  @us = hist((nsecs - @start[args->next_pid]) / 1000);
  delete(@start[args->next_pid]);
}'
```

A histogram concentrated near zero with a small second cluster near 100 ms is
the signature of quota throttling.

### Prometheus Integration

Deploy Prometheus with cAdvisor to track:

```promql
# Throttled time as a share of runnable time (percent)
100 * rate(container_cpu_cfs_throttled_seconds_total[5m])
/ (rate(container_cpu_usage_seconds_total[5m])
   + rate(container_cpu_cfs_throttled_seconds_total[5m]))

# Node context switch rate
rate(node_context_switches_total[5m])

# CPU pressure (PSI), cgroup v2
rate(container_cpu_pressure_waiting_seconds_total[5m])
```

**Alerting rules**: `evidence/prometheus-integration/prometheus-alerts.yaml`
contains the rules shipped with this research. The thresholds there
(`HighCPUThrottling` at 25% for 5 minutes, `CriticalCPUThrottling` at 50%) are
operational judgement, not measured boundaries. Phase 7.1 showed a
configuration throttled in 95.6% of periods that still served 2991 req/s, so
throttle percentage alone is a weak alert. Pair any throttle alert with the
service's own p99 SLO.

### Grafana

Import `evidence/prometheus-integration/grafana-dashboard.json`.

Panels: CPU usage vs request, throttling rate, throttle percentage, context
switch rate.

---

## Capacity Planning

### Calculating Cluster CPU Capacity

```bash
# Total allocatable CPU (cores)
kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.cpu}{"\n"}{end}' \
  | sed 's/m$//' | awk '{sum += ($0 < 100 ? $0 * 1000 : $0)} END {print sum/1000 " cores"}'

# Total requested (millicores)
kubectl get pods --all-namespaces \
  -o jsonpath='{range .items[*]}{.spec.containers[*].resources.requests.cpu}{"\n"}{end}' \
  | sed 's/m$//' | awk 'NF {sum += $1} END {print sum " millicores requested"}'
```

Note that `allocatable` is below `capacity` (kube-reserved and system-reserved
are already deducted) and that this sum treats a bare `"1"` as 1 millicore.
Use the allocatable figure for headroom planning.

### Overcommit Strategy

Kubernetes allows the sum of requests to exceed capacity because pods rarely
use their requests simultaneously. Requests, not limits, drive scheduling.

Reasonable tiers as a starting policy:

| Cluster type | sum(requests) / capacity |
|--------------|--------------------------|
| Regulated / financial | <= 0.8 |
| General SaaS | <= 1.2 |
| Batch / dev | <= 2.0 |

These ratios are starting policies, not measured optima. What the research
does establish: overcommit becomes hazardous when a pod's **weight** is high
and its workload is bursty, because weight is what CFS uses to arbitrate
contention. Phase 7.2 measured BestEffort (weight 1) reaching p99 65.44 ms
against Burstable (weight 59) at 31.28 ms on the same node under contention.

Limits may sum far above capacity. That is the normal case, and it is exactly
why Phase 7 recommends limits only where you want a hard bound.

### Preventing Overload

ResourceQuota per team, plus a LimitRange for defaults:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: compute-quota
  namespace: team-a
spec:
  hard:
    requests.cpu: "50"
    limits.cpu: "100"
```

Prefer `requests.cpu` quotas for fairness; a `limits.cpu` quota encourages
every team to set limits just to satisfy it, which is the opposite of what the
measurements support. If you must quota limits, leave the latency-sensitive
namespace without a default CPU limit.

---

## Troubleshooting

### Issue 1: Pods Stuck in Pending

```
0/10 nodes are available: 10 Insufficient cpu.
```

**Diagnosis**:

```bash
kubectl describe node <node> | grep -A8 "Allocated resources"
kubectl describe pod <pod-name> | grep -A3 "Requests"
```

**Solutions**:

1. Lower pod requests if they are over-estimated
2. Add nodes, or enable the Cluster Autoscaler
3. Remove idle pods to free capacity

Lowering a **limit** never fixes this. Only requests affect placement.

### Issue 2: High Latency Despite Available CPU

This is the signature failure mode measured in Phase 7: a quota enforced on an
otherwise idle node.

```bash
kubectl exec <pod> -- cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat
```

If `cpu.stat` shows a high `nr_throttled` while `kubectl top node` shows idle
cores, the limit is the cause.

**Solution**: remove `limits.cpu` from the pod spec and re-measure p99. Phase
7.1 measured p99 falling from 69.43 ms to 13.98 ms when the limit was removed
on the same workload at a fixed offered rate.

### Issue 3: Noisy Neighbour

One pod consuming CPU and starving others.

**Check**: who is taking CPU, and at what weight?

```bash
kubectl top pods -A --sort-by=cpu

# Per-container weight, from the node. Higher weight = larger share
# under contention. 100m maps to weight 17, 250m to 35 (Phase 7.5).
for scope in /sys/fs/cgroup/kubepods.slice/*pod*/crio-*; do
  printf '%s weight=' "$scope"
  cat "$scope/cpu.weight" 2>/dev/null
done
```

**Solutions**, in order of measured effectiveness:

1. **Cap the offending pod** (`limits.cpu`). Phase 7.3 measured capping four
   burners at 200m cutting their CPU use by 75% and the co-located victim's
   median latency by 79%.
2. **Move it to a dedicated node pool** with taints and tolerations.
3. **Use PriorityClass** to protect critical pods from preemption:

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high-priority
value: 1000
globalDefault: false
```

Do not solve a noisy neighbour by capping the victim. The victim's own limit
is what produces its tail, and Phase 7.3 shows the gain comes from bounding the
offender.

### Issue 4: Slow Pod Scheduling

The scheduler taking seconds to place pods. This is **kube-scheduler**
latency, unrelated to CFS throttling.

**Check**:

```bash
kubectl get events --sort-by='.lastTimestamp' | grep Scheduled
```

**Solutions**:

1. Reduce the number of nodes the scheduler must consider (node pools)
2. Use pod affinity/anti-affinity to narrow placement, with care
3. Raise `percentageOfNodesToScore`, which is now automatic by default for
   large clusters

Tuning `kernel.sched_*` sysctls will not affect this.

---

## Summary

### Quick Reference

| Task | Command |
|------|---------|
| Node capacity | `kubectl describe node <node>` |
| Pod requests | `kubectl describe pod <pod>` |
| Quota | `cat /sys/fs/cgroup/kubepods.slice/*pod<uid>*/crio-*/cpu.max` |
| Throttling | `cat /sys/fs/cgroup/kubepods.slice/*pod<uid>*/crio-*/cpu.stat` |
| Contention (not just quota) | `cat /sys/fs/cgroup/kubepods.slice/*pod<uid>*/crio-*/cpu.pressure` |
| Scheduler params | `sysctl -a \| grep '^kernel.sched_'` |
| Wake-to-run delay | Phase 7.4 probe, `phase7/7.4-live-bpf/scheduler-trace.bt` |

### Decision Matrix

| Scenario | Measured basis | Action |
|----------|----------------|--------|
| General-purpose cluster | 7.1: limit cost p99 13.98 -> 69.43 ms | Default settings; requests only |
| Latency-sensitive service | 7.4: worst wait 5.5 -> 71.7 ms | No CPU limit |
| Multi-tenant with untrusted code | 7.3: capping offenders cut victim p50 79% | Cap the offenders, not the victims |
| Batch / HPC | Phase 4: limit below need costs throughput | Limit = request, or no limit |
| BestEffort workloads | 7.2: weight 1 vs 59, p99 65.44 vs 31.28 ms | Accept, or give them a request |
| Hard real-time | not measured | RT kernel, dedicated pool |

### References

- Phase 4 throttling data: `phase4/evidence/throttling-summary.json`
- Phase 7 full detail: `PHASE7_COMPLETE.md`
- Phase 7 cgroup mapping: `evidence/phase7/cgroup-v2/cgroup-v2-analysis-20261026T190908Z.json`
- Phase 7 BPF trace: `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`
- Prometheus configs: `evidence/prometheus-integration/`
- [Linux CFS Bandwidth Control](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)
- [Cgroup v2 CPU controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
- [sched_ext project](https://github.com/sched-ext/scx)
- [Kubernetes Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)

---

**Last updated**: 2026-10-26
