# Phase 6 Summary: Future Work Topics

**Status:** 6.1 and 6.2 delivered as Phase 7.1 and 7.4; 6.3, 6.4, 6.5, 6.6 not run  
**Date:** October 26, 2026

---

## Completion Status

| Topic | Status | Time | Value | Prerequisites |
|-------|--------|------|-------|---------------|
| 6.1 Latency Testing | ✅ Measured as Phase 7.1 | 30m | ⭐⭐⭐ High | wrk |
| 6.2 BPF Traces | ✅ Measured as Phase 7.4 | 70m | ⭐⭐ Medium | bpftrace in reader pod |
| 6.3 Kernel Tuning | ❌ Not run | 120m | ⭐⭐ Medium | Node SSH, root |
| 6.4 scx Comparison | ❌ Not run | 180m | ⭐ Low | Kernel 6.12+, scx |
| 6.5 Multi-Node | 📋 Design only | 210m | ⭐ Low | Multi-node cluster |
| 6.6 Memory Limits | 📋 Design only | 390m | ⭐⭐⭐ High | Same as Phase 2-4 |

---

## Topic 6.1: Latency Testing (✅ Complete)

### What It Does

Measures **user-visible latency** (P50/P95/P99) under CPU throttling using nginx + wrk.

### Key Finding (measured later as Phase 7.1)

The table that stood here was a projection, not a measurement (it claimed p99
80× worse at a nominal 69.8% throttle level that was never produced). Phase 7.1
measured nginx at a fixed offered rate instead:

| Limit | Throughput | p50 | p99 | Throttled periods |
|-------|-----------|-----|-----|-------------------|
| none | 7216 req/s | 2.04 ms | 13.98 ms | 0% |
| 120m | 3481 req/s | 2.13 ms | 69.43 ms | 99.7% |
| 100m | 2991 req/s | 2.20 ms | 67.76 ms | 95.6% |
| 70m | 2071 req/s | 2.16 ms | 98.87 ms | 75.1% |

Throughput halved; p99 grew about 5×. p50 did not move at all, which is the
point: the damage is in the tail, and the throttle percentage does not rank
configurations (75.1% had the worst p99 of the four).

### Why It Matters

- Users feel latency, not throughput
- P99 SLA violations before throughput degrades
- Validates "no limits" recommendation for latency-sensitive apps

### Implementation

```bash
cd phase6/6.1-latency-testing
./check-wrk.sh
./run-nginx-latency-test.sh
python3 analyze-latency.py
```

**Status:** Ready to run

---

## Topic 6.2: BPF Traces (📋 Design Only)

### What It Does

Correlates CPU throttling with scheduler events using BPF tracing.

### Key Finding (Expected)

**Throttling correlates with `sched_wait` events (r² > 0.8)**

```
nr_throttled ↑ → sched_wait events ↑
throttled_usec ↑ → wakeup-to-run latency ↑
```

### Why It Matters

- Explains **why** throttling hurts latency
- Shows scheduler behavior at kernel level
- Validates Phase 5 kernel tuning recommendations

### Implementation Plan

1. Deploy BPF tracer on node (reuse Phase 3)
2. Run throttled workload
3. Collect `sched:sched_wait`, `sched:sched_wakeup`, `sched:sched_switch`
4. Correlate with cgroup throttling metrics

**Status:** Requires node SSH access

---

## Topic 6.3: Kernel Tuning (📋 Design Only)

### What It Does

Experiments with CFS parameters to reduce throttling impact.

### Key Finding: NOT RUN

No kernel parameter was tuned or measured. The percentage table that stood here
was a hypothesis with invented numbers.

There is also no node-wide bandwidth-slice knob on a cgroup v2 host: the quota
period is per cgroup, the second field of `cpu.max` (kubelet
`--cpu-cfs-quota-period`). Phase 7.5 read it live: a 100m limit produced
`cpu.max = "10000 100000"`.

### Why It Matters

- The measured tail latency came from waiting out the quota period, which no
  scheduler timeslice setting removes
- Removing or right-sizing the CPU limit is the change with measured effect
  (Phase 7.1: p99 69.43 → 13.98 ms)

### Implementation Plan

1. Baseline P99 latency with default kernel
2. Apply latency-sensitive tuning profile
3. Re-measure P99 latency
4. Compare results

**Status:** Requires node SSH + root access

---

## Topic 6.4: scx Comparison (❌ NOT RUN)

### What It Would Do

Compare CFS against a custom scheduler (scx_lavd) for throttled workloads.

### Key Finding

**None. This comparison was never executed**, so no scx throughput or latency
figure exists. Any percentage quoted for scx elsewhere in this research is
wrong; it was a projection.

| Scheduler | Throughput | P99 Latency | Context Switches |
|-----------|------------|-------------|------------------|
| CFS | measured (Phases 2, 4, 7) | measured | measured (7.4) |
| scx_lavd | not run | not run | not run |

> scx is an **sched_ext** BPF scheduler: it replaces the CFS scheduling policy
> for the whole node, not the cgroup quota. It cannot remove CFS bandwidth
> throttling, because `cpu.max` is enforced by the bandwidth controller before
> a task reaches the scheduler. That is reasoning from the kernel design, not a
> measured result.

### Why It Matters

- A scx comparison is the only way to test whether a different scheduling
  policy changes throttled latency, and it has not been tested here

### Implementation Plan

1. Install scx tools
2. Run baseline with CFS
3. Switch to scx_lavd
4. Re-run same workload
5. Compare metrics

**Status:** Requires kernel 6.12+ with CONFIG_SCHED_CLASS_EXT=y

---

## Topic 6.5: Multi-Node Fairness (📋 Design Only)

### What It Does

Tests cross-node CPU fairness in multi-node clusters.

### Key Finding

**Not run.** No multi-node measurement exists; this cluster had a single test
node, so nothing about cross-node fairness is claimed.

### Why It Matters

- Validates Kubernetes scheduler fairness
- Shows impact of node-level contention
- Informs capacity planning

### Implementation Plan

1. Deploy 10 CPU-bound pods across 3 nodes
2. Measure CPU allocation per pod
3. Verify fairness (std dev < 10%)
4. Test under node pressure

**Status:** Requires 3+ node cluster

---

## Topic 6.6: Memory Limits (📋 Design Only)

### What It Does

Parallel research for memory limits (like Phases 1-5 for CPU).

### Key Finding

**Not run.** The comparison below is kernel documentation ("Memory limits are
binary: no throttling, just OOM kill" for the cgroup v2 `memory.max` /
`memory.oom.group` behaviour), not a result from this research. No memory-limit
experiment was executed.

| Aspect | CPU | Memory |
|--------|-----|--------|
| Throttling | Gradual (cpu.max) | None at the limit; OOM kill past memory.max |
| Recovery | Automatic | Container restart |
| Observability | Good | Moderate |

### Why It Matters

- Memory behavior fundamentally different from CPU
- Informs sizing strategy (must be correct, no burst)
- Completes resource limits research

### Implementation Plan

1. Phase 1: Memory-bound workloads
2. Phase 2: QoS comparison
3. Phase 3: OOM event tracking
4. Phase 4: Memory pressure impact
5. Phase 5: Admin guidance

**Status:** Requires 1 week (similar timeline to CPU research)

---

## Priority Recommendation

### Immediate (Recommended)

1. **6.1 Latency Testing** ✅ - Already complete, high value, low cost
2. **6.6 Memory Limits** - High value, completes research, but time-intensive

### Nice to Have

3. **6.2 BPF Traces** - Medium value, needs node access
4. **6.3 Kernel Tuning** - Medium value, needs node access

### Research Only

5. **6.4 scx Comparison** - Low value (niche), high requirements
6. **6.5 Multi-Node** - Low value (edge case), complex setup

---

## Quick Reference

### Topic 6.1 (Complete)
```bash
cd phase6/6.1-latency-testing
./run-nginx-latency-test.sh
```

### Topic 6.2 (Design)
```bash
# Would be:
cd phase6/6.2-bpf-traces
./collect-traces.sh
```

### Topic 6.3 (Design)
```bash
# Would be:
cd phase6/6.3-kernel-tuning
./run-tuning-experiments.sh
```

### Topic 6.4 (Design)
```bash
# Would be:
cd phase6/6.4-scx-comparison
./compare-schedulers.sh
```

### Topic 6.5 (Design)
```bash
# Would be:
cd phase6/6.5-multi-node
./run-fairness-test.sh
```

### Topic 6.6 (Design)
```bash
# Would be:
cd phase6/6.6-memory-limits
python3 run-memory-experiments.py
```

---

## Integration with Core Research

### Extends Phases 1-5

- **6.1:** Adds latency metrics to Phase 4 throttling
- **6.2:** Uses Phase 3 BPF with Phase 4 workloads
- **6.3:** Validates Phase 5 kernel tuning guide
- **6.4:** Tests Phase 5 custom scheduler recommendations
- **6.5:** Multi-node extension of Phase 2 QoS
- **6.6:** Parallel track to CPU research (Phases 1-5)

### Validates Recommendations

- **Request-only pattern:** 6.1 shows P99 impact
- **Kernel tuning:** 6.3 provides empirical targets
- **Custom schedulers:** 6.4 shows when scx helps
- **Memory sizing:** 6.6 shows different behavior

---

## Next Steps

### If You Want Latency Data

Run Topic 6.1:
```bash
cd phase6/6.1-latency-testing
./check-wrk.sh && ./run-nginx-latency-test.sh
```

### If You Have Node Access

Consider Topics 6.2 (BPF) and 6.3 (Kernel Tuning):
- Both provide deep kernel-level insights
- Require SSH + root on nodes
- ~3 hours total

### If You Have Time for Parallel Research

Consider Topic 6.6 (Memory Limits):
- Completes resource limits research
- Same methodology as CPU (Phases 1-5)
- ~1 week effort

---

## Documentation

All topics have complete README.md with:
- Overview and hypothesis
- Expected findings
- Implementation plan
- Integration with core research
- Time estimates

**Read:** `phase6/<topic>/README.md` for details

---

**Summary:** Phase 6 provides 6 extension topics. Topic 6.1 is complete and ready to run. Topics 6.2-6.6 are designed and documented, ready for implementation when needed.
