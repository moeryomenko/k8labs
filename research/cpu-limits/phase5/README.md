# Phase 5: Scheduler Deep Dive & Production Readiness

**Status:** Complete - administrator guides delivered. The BPF and latency
measurements were carried out later as Phase 7.4 and Phase 7.1; the
scheduler-parameter and sched_ext experiments were not run.

**Objective:** Kernel-level scheduler documentation, BPF trace correlation, and
administrator guidance

---

## Objectives

This phase fills the remaining gaps to meet the comprehensive research requirements:

1. **Collect actual BPF traces** during throttling experiments
2. **Correlate throttling with scheduler latency**
3. **Document kernel scheduler configuration** (CFS parameters, UP/SMP)
4. **Test custom schedulers** (scx) if kernel 6.12+ available
5. **Demonstrate production impact** (P50/P95/P99 latency, not just throughput)

---

## Phase 5 Structure

```
phase5/
├── README.md                          # This file
├── experiments/
│   ├── latency-sensitive/             # HTTP server under throttling
│   │   ├── nginx-deployment.yaml
│   │   ├── wrk-load-generator.yaml
│   │   └── run-latency-test.py
│   │
│   ├── scheduler-params/              # CFS tuning experiments
│   │   ├── test-cfs-bandwidth.sh
│   │   ├── test-sched-latency.sh
│   │   └── baseline-vs-tuned.py
│   │
│   └── scx-comparison/                # Custom scheduler (if available)
│       ├── install-scx.sh
│       ├── test-scx-simple.sh
│       └── compare-schedulers.py
│
├── bpf-correlation/
│   ├── collect-during-throttle.py     # BPF + cgroup correlation
│   ├── analyze-sched-latency.py       # Scheduler latency from BPF
│   └── visualize-traces.py            # Generate flame graphs
│
├── admin-guide/
│   ├── KERNEL_CONFIG.md               # CFS, UP/SMP, scheduler params
│   ├── CUSTOM_SCHEDULERS.md           # When to use scx
│   └── PRODUCTION_PATTERNS.md         # Real-world deployment patterns
│
└── evidence/
    └── (experiment results, BPF traces)
```

---

## Experiments Plan

### Experiment 5.1: BPF Trace Collection During Throttling

**Objective:** Capture scheduler events during CPU throttling

**Delivered as Phase 7.4.** bpftrace now runs inside an unprivileged reader pod
with tracefs visible, so no node SSH is needed.

**Approach:**
1. Run nginx with a known CPU limit
2. Trace `sched:sched_wakeup` and `sched:sched_switch` for nginx tasks
3. Record the wake-to-run interval and the cgroup throttle counters together

**Measured:**

| Limit | Throttled periods | Mean wait | Worst wait |
|-------|-------------------|-----------|------------|
| none | 0% | 6.4 us | 5.5 ms |
| 200m | 96.0% | 9.6 us | 6.9 ms |
| 100m | 96.9% | 13.7 us | 71.7 ms |

Evidence: `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`
Script: `phase7/7.4-live-bpf/`

---

### Experiment 5.2: Latency-Sensitive Workload

**Objective:** Demonstrate throttling impact on HTTP latency (P50/P95/P99)

**Delivered as Phase 7.1.** The Phase 5 attempt
(`phase5/evidence/latency-impact-20261026T163603Z.json`) covered two
configurations, recorded no throttling, and logged negative `usage_usec`
values, so it is superseded rather than reported.

**Actual method:**
1. Run nginx from a cached image, serving a 14.4 KiB page
2. Drive it at a fixed offered rate with a Python client (`wrk2` panics on this
   kernel)
3. Record throughput, p50 and p99 with the cgroup counters for the run

**Measured:**

```
Limit    | Throughput  | p50    | p99     | Throttled
---------|-------------|--------|---------|----------
none     | 7216 req/s  | 2.04ms | 13.98ms | 0%
120m     | 3481 req/s  | 2.13ms | 69.43ms | 99.7%
100m     | 2991 req/s  | 2.20ms | 67.76ms | 95.6%
70m      | 2071 req/s  | 2.16ms | 98.87ms | 75.1%
```

Evidence: `evidence/phase7/real-apps/nginx-throttling-20261026T191420Z.json`

---

### Experiment 5.3: Kernel Scheduler Parameters - RUN AS PHASE 7.6

**Objective:** Test CFS tuning impact on throttled workloads

**Status:** no parameter was tuned or measured. There is also no node-wide
bandwidth-slice knob on a cgroup v2 host; the quota period is the second field of
each cgroup's `cpu.max`, written from kubelet's `cpuCFSQuotaPeriod`. See
`admin-guide/KERNEL_CONFIG.md`.

**Parameters that would be involved:**
```bash
# CFS scheduling (node-wide, cgroup v2)
/proc/sys/kernel/sched_min_granularity_ns        # default: 750000
/proc/sys/kernel/sched_latency_ns                # default: 6000000
/proc/sys/kernel/sched_wakeup_granularity_ns     # default: 1000000

# SMP scheduling
/proc/sys/kernel/sched_migration_cost_ns         # default: 500000
/proc/sys/kernel/sched_nr_migrate                # default: 32

# Throttling period: per cgroup, in cpu.max (kubelet --cpu-cfs-quota-period)
```

**Approach, if you run it:**
1. Run the Phase 7.1 nginx workload with default parameters
2. Change one parameter at a time
3. Record p50/p99 and the cgroup `cpu.stat` throttle counters for each run

**Hypothesis (unverified):** a larger period means fewer throttle events but
longer waits when a throttle does happen.

**Script:** `experiments/scheduler-params/test-cfs-bandwidth.sh`

---

### Experiment 5.4: Custom Scheduler (scx) Comparison

**Objective:** Compare CFS vs scx schedulers for throttled workloads

**Prerequisites:**
- Kernel 6.12+ with `CONFIG_SCHED_CLASS_EXT=y`
- scx tools installed

**Schedulers to test:**
1. **CFS** (default) - Completely Fair Scheduler
2. **scx_simple** - Round-robin per-CPU
3. **scx_rusty** - Load balancing across domains
4. **scx_lavd** - Latency-aware

**Approach:**
1. Run Phase 4 throttling experiments under each scheduler
2. Compare throttling %, performance, scheduling latency

**Expected Finding (hypothesis only, not tested):** a latency-aware sched_ext
scheduler might reduce scheduling delay for throttled workloads. No run exists.

**Script:** `experiments/scx-comparison/compare-schedulers.py`

---

## Administrator Guides

### KERNEL_CONFIG.md

**Contents:**
1. **CFS Scheduler Parameters**
   - When to tune `sched_latency_ns`
   - Impact of bandwidth slice on throttling
   - Trade-offs: fairness vs latency

2. **UP vs SMP Scheduling**
   - Uniprocessor (UP) build - no SMP overhead
   - SMP scheduling domains
   - CPU affinity and isolation

3. **cgroup CPU Controller**
   - `cpu.max` (quota/period) - how limits work
   - `cpu.weight` (shares) - how requests work
   - `cpu.uclamp.min/max` - utilization clamping

4. **Production Patterns**
   - Isolate latency-sensitive pods with `cpuset`
   - Use `isolcpus=` boot parameter
   - Reserve CPUs for system daemons

---

### CUSTOM_SCHEDULERS.md

**Contents:**
1. **When to Use Custom Schedulers**
   - Default CFS works for 95% of workloads
   - Consider scx for:
     - Extreme latency requirements (real-time)
     - Specialized workloads (HPC, gaming)
     - Research and experimentation

2. **scx Scheduler Overview**
   - BPF-based extensible scheduling
   - Available schedulers: simple, rusty, lavd, nest
   - Installation and configuration

3. **Performance Comparison**
   - Results from Experiment 5.4
   - When scx improves throttling behavior
   - Trade-offs and limitations

4. **Migration Guide**
   - Testing scx in non-production
   - Gradual rollout strategy
   - Monitoring and rollback

---

### PRODUCTION_PATTERNS.md

**Contents:**
1. **Request-Only Pattern** (Recommended)
   ```yaml
   resources:
     requests:
       cpu: "1000m"
     # No limits - allows bursting
   ```
   - **Use for:** Most production workloads
   - **Pros:** No quota, so no throttling periods; can burst
   - **Cons:** No hard isolation

2. **Guaranteed Pattern** (Critical Services)
   ```yaml
   resources:
     requests:
       cpu: "2000m"
     limits:
       cpu: "2000m"
   ```
   - **Use for:** Databases, critical services
   - **Pros:** Predictable, QoS protection
   - **Cons:** Cannot burst

3. **CPU Pinning Pattern** (Ultra-Low Latency)
   ```yaml
   spec:
     containers:
     - resources:
         requests:
           cpu: "4"
         limits:
           cpu: "4"
     nodeSelector:
       node.kubernetes.io/isolated: "true"
   ```
   - **Use for:** Trading systems, real-time apps
   - **Requires:** `isolcpus=` on nodes
   - **Pros:** Minimal scheduler overhead
   - **Cons:** Reduced cluster utilization

4. **Multi-Tenant Pattern** (Cost Control)
   ```yaml
   resources:
     requests:
       cpu: "500m"
     limits:
       cpu: "2000m"
   ```
   - **Use for:** Multi-tenant platforms
   - **Pros:** Cost isolation, noisy neighbor protection
   - **Cons:** Throttling during bursts

---

## Implementation Tasks

### Task 5.1: Collect BPF Traces - delivered as Phase 7.4

```bash
cd phase7 && PHASE7_NODE=<node-name> ./run-all-phase7.sh
```

**What it does:**
1. Deploy nginx with a known CPU limit
2. Start bpftrace inside the unprivileged reader pod
3. Record the wake-to-run interval for nginx tasks
4. Read the same cgroup's throttle counters for the same run

**Results:** `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`

---

### Task 5.2: Latency Testing - delivered as Phase 7.1
```bash
cd phase5/experiments/latency-sensitive
python3 run-latency-test.py
```

**What it does:**
1. Deploy nginx with several CPU limits
2. Drive it at a fixed offered rate from a Python client
3. Capture p50 and p99 latency alongside the cgroup throttle counters

**Status:** superseded by Phase 7.1. The Phase 5 run
(`evidence/latency-impact-20261026T163603Z.json`) recorded two configurations
and no throttling, so it is kept only as a record of the attempt.

---

### Task 5.3: Scheduler Parameters - not run
```bash
cd phase5/experiments/scheduler-params
./test-cfs-bandwidth.sh
```

**What it does:**
1. Record baseline CFS parameters
2. Run throttling test with default params
3. Change the quota period (kubelet `cpuCFSQuotaPeriod`; on cgroup v2 it is the
   second field of `cpu.max` - there is no node-wide
   `sched_cfs_bandwidth_slice_us` or `cpu.cfs_period_us` to tune)
4. Run throttling test again
5. Compare results

**Output:** `evidence/scheduler-params-comparison.json`

---

### Task 5.4: scx Comparison - not run
```bash
cd phase5/experiments/scx-comparison
./install-scx.sh
python3 compare-schedulers.py
```

**What it does:**
1. Check kernel version and scx support
2. Install scx tools if available
3. Run Phase 4 experiments under CFS, scx_simple, scx_lavd
4. Compare throughput and latency

**Status:** not run. The test node ran `7.3.0-k8labs`; sched_ext was never
installed, so `CUSTOM_SCHEDULERS.md` is documentation only.

**Output:** `evidence/scheduler-comparison.json`

---

## Dependencies

### Required
- ✅ Phase 3 BPF infrastructure (deployed)
- ✅ Phase 4 throttling workloads (ready)
- ✅ Prometheus + Grafana (ready to deploy)
- ❌ Node SSH access - not needed; Phase 7 uses an unprivileged reader pod

### Optional
- ⏸️ Kernel 6.12+ with `CONFIG_SCHED_CLASS_EXT=y` (for scx; not available here)
- ✅ nginx (multi-arch digest, pre-pulled)
- ❌ `wrk`/`wrk2` - both unusable on this kernel; replaced by a Python client

---

## Success Criteria

- [x] Infrastructure ready (BPF, Prometheus configs)
- [x] Task 5.1: BPF traces collected during throttling - done as Phase 7.4
- [x] Task 5.2: Latency impact demonstrated (p50/p99) - done as Phase 7.1
- [ ] Task 5.3: Scheduler parameters tested - **not run**
- [ ] Task 5.4: scx comparison - **not run** (kernel has no sched_ext here)
- [x] Admin guides written: KERNEL_CONFIG.md, CUSTOM_SCHEDULERS.md, PRODUCTION_PATTERNS.md
- [x] Findings recorded in `PHASE7_COMPLETE.md` and `RESEARCH_COMPLETE.md`

Tasks 5.3 and 5.4 are declared gaps. The tail-latency cost this research
measured came from the CPU quota, not from scheduler timeslices, so neither is
a prerequisite for the guides.

---

## Effort

Measured work: Phase 7 runs all eight topics in 50-60 minutes
(`phase7/run-all-phase7.sh`). Writing the admin guides and the Phase 7 analysis
took the bulk of the time.

Remaining (not run): a scheduler-parameter A/B test and a sched_ext comparison,
each needing nodes with different kernels.

---

## Getting Started

### Check Prerequisites

```bash
# Check kernel version
uname -r

# Check scx support (kernel 6.12+)
grep CONFIG_SCHED_CLASS_EXT /boot/config-$(uname -r)

# Check BPF tools
which bpftrace
which perf

# Check node access
kubectl get nodes -o wide
# Can you SSH to these IPs?
```

### Reproducing the BPF measurement

The Phase 7.4 probe is the maintained version. It runs bpftrace inside an
unprivileged reader pod, so node SSH is not required:

```bash
cd research/cpu-limits/phase7
PHASE7_NODE=<node-name> ./run-all-phase7.sh    # includes 7.4
```

It prints the wake-to-run distribution for nginx tasks and the cgroup throttle
counters from the same run, which is what separates quota throttling from
ordinary contention.

---

**Next Steps:** the declared gaps are the scheduler-parameter test (Task 5.3)
and the sched_ext comparison (Task 5.4). Neither changes the guides' advice:
remove or right-size the quota before replacing the scheduler.
