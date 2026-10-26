# Phase 5: Scheduler Deep Dive & Production Readiness

**Status:** 🚧 In Progress  
**Objective:** Complete the research with kernel-level scheduler analysis, BPF trace correlation, and administrator guidance

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

**Approach:**
1. Use Phase 4 heavy-throttle workload (69.8% throttling)
2. Start BPF tracer before workload
3. Collect `sched:sched_switch`, `sched:sched_wakeup` events
4. Correlate throttling periods with scheduling delays

**Expected Output:**
- BPF trace JSON with timestamps
- Scheduler latency histogram
- Correlation: throttling events → wakeup delays

**Script:** `bpf-correlation/collect-during-throttle.py`

---

### Experiment 5.2: Latency-Sensitive Workload

**Objective:** Demonstrate throttling impact on HTTP latency (P50/P95/P99)

**Approach:**
1. Deploy nginx with varying CPU limits
2. Generate constant load with `wrk` (1000 req/s)
3. Measure latency distribution during throttling

**Configurations:**
- No throttle (4000m limit) - baseline
- Light throttle (3000m limit)
- Heavy throttle (500m limit)

**Expected Output:**
```
Configuration    | P50    | P95    | P99    | Throttling %
-----------------|--------|--------|--------|-------------
No throttle      | 2ms    | 5ms    | 8ms    | 0%
Light throttle   | 3ms    | 12ms   | 25ms   | 4.7%
Heavy throttle   | 45ms   | 250ms  | 500ms  | 69.8%
```

**Script:** `experiments/latency-sensitive/run-latency-test.py`

---

### Experiment 5.3: Kernel Scheduler Parameters

**Objective:** Test CFS tuning impact on throttled workloads

**Parameters to test:**
```bash
# CFS bandwidth control
/proc/sys/kernel/sched_cfs_bandwidth_slice_us    # default: 5000
/proc/sys/kernel/sched_min_granularity_ns        # default: 750000
/proc/sys/kernel/sched_latency_ns                # default: 6000000
/proc/sys/kernel/sched_wakeup_granularity_ns     # default: 1000000

# SMP scheduling
/proc/sys/kernel/sched_migration_cost_ns         # default: 500000
/proc/sys/kernel/sched_nr_migrate                # default: 32
```

**Approach:**
1. Run heavy-throttle workload with default params
2. Tune CFS bandwidth slice (5ms → 10ms)
3. Measure throttling behavior change

**Expected Finding:** Larger bandwidth slice = fewer throttling events but longer delays

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

**Expected Finding:** scx_lavd may reduce latency for throttled workloads

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
   - **Pros:** No throttling, can burst
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

### Task 5.1: Collect BPF Traces ✅ Infrastructure Ready
```bash
cd phase5/bpf-correlation
python3 collect-during-throttle.py
```

**What it does:**
1. Deploy Phase 4 heavy-throttle workload
2. Start BPF tracer (using Phase 3 infrastructure)
3. Collect traces for entire workload duration
4. Correlate cgroup throttling events with scheduler latency

**Output:** `evidence/bpf-traces-throttling.json`

---

### Task 5.2: Latency Testing ⏸️ Ready to Implement
```bash
cd phase5/experiments/latency-sensitive
python3 run-latency-test.py
```

**What it does:**
1. Deploy nginx with 3 different CPU limits
2. Run `wrk` load generator (1000 req/s for 2 minutes each)
3. Capture P50/P95/P99 latency from wrk output
4. Correlate with Prometheus throttling metrics

**Output:** `evidence/latency-impact.json`

---

### Task 5.3: Scheduler Parameters ⏸️ Requires Node Access
```bash
cd phase5/experiments/scheduler-params
./test-cfs-bandwidth.sh
```

**What it does:**
1. Record baseline CFS parameters
2. Run throttling test with default params
3. Tune `sched_cfs_bandwidth_slice_us` to 10ms
4. Run throttling test again
5. Compare results

**Output:** `evidence/scheduler-params-comparison.json`

---

### Task 5.4: scx Comparison ⏸️ Conditional (Kernel 6.12+)
```bash
cd phase5/experiments/scx-comparison
./install-scx.sh
python3 compare-schedulers.py
```

**What it does:**
1. Check kernel version and scx support
2. Install scx tools if available
3. Run Phase 4 experiments under CFS, scx_simple, scx_lavd
4. Compare throttling %, performance, latency

**Output:** `evidence/scheduler-comparison.json`

---

## Dependencies

### Required
- ✅ Phase 3 BPF infrastructure (deployed)
- ✅ Phase 4 throttling workloads (ready)
- ✅ Prometheus + Grafana (ready to deploy)
- ⏸️ Node SSH access (for kernel parameter tuning)

### Optional
- ⏸️ Kernel 6.12+ with `CONFIG_SCHED_CLASS_EXT=y` (for scx)
- ⏸️ wrk HTTP benchmarking tool
- ⏸️ nginx Docker image

---

## Success Criteria

Phase 5 is complete when:

- [x] Infrastructure ready (BPF, Prometheus)
- [ ] Task 5.1: BPF traces collected during throttling
- [ ] Task 5.2: Latency impact demonstrated (P50/P95/P99)
- [ ] Task 5.3: Scheduler parameters tested
- [ ] Task 5.4: scx comparison (if available) OR documented as unavailable
- [ ] Admin guides written: KERNEL_CONFIG.md, CUSTOM_SCHEDULERS.md, PRODUCTION_PATTERNS.md
- [ ] Updated RESEARCH_COMPLETE.md to include Phase 5 findings

---

## Timeline Estimate

- **Task 5.1 (BPF):** 2-3 hours (automation exists)
- **Task 5.2 (Latency):** 3-4 hours (new workload + analysis)
- **Task 5.3 (Params):** 2-3 hours (if node access available)
- **Task 5.4 (scx):** 4-6 hours (if kernel supports) OR 30 minutes (document unavailable)
- **Documentation:** 4-6 hours (3 admin guides)

**Total:** 15-22 hours of work

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

### Start with Task 5.1 (BPF Traces)

This is the highest-priority gap - BPF infrastructure is deployed but traces haven't been collected during actual throttling:

```bash
cd research/cpu-limits/phase5
python3 bpf-correlation/collect-during-throttle.py
```

This will give you the low-level scheduler behavior during throttling events.

---

**Next Steps:** Should I proceed with implementing Task 5.1 (BPF trace collection)?
