# Phase 6 Summary: Future Work Topics

**Status:** 📋 Framework Complete, 1/6 Implementation Ready  
**Date:** October 26, 2026

---

## Completion Status

| Topic | Status | Time | Value | Prerequisites |
|-------|--------|------|-------|---------------|
| 6.1 Latency Testing | ✅ Complete | 30m | ⭐⭐⭐ High | wrk |
| 6.2 BPF Traces | 📋 Design | 70m | ⭐⭐ Medium | Node SSH, root |
| 6.3 Kernel Tuning | 📋 Design | 120m | ⭐⭐ Medium | Node SSH, root |
| 6.4 scx Comparison | 📋 Design | 180m | ⭐ Low | Kernel 6.12+, scx |
| 6.5 Multi-Node | 📋 Design | 210m | ⭐ Low | Multi-node cluster |
| 6.6 Memory Limits | 📋 Design | 390m | ⭐⭐⭐ High | Same as Phase 2-4 |

---

## Topic 6.1: Latency Testing (✅ Complete)

### What It Does

Measures **user-visible latency** (P50/P95/P99) under CPU throttling using nginx + wrk.

### Key Finding (Expected)

**Heavy throttling (69.8%) increases P99 latency 80× while throughput drops only 63%**

| Throttling | Throughput | P99 Latency |
|------------|------------|-------------|
| 0% | Baseline | Baseline |
| 5% | -6% | +100% (2×) ⚠️ |
| 50% | -37% | +1900% (20×) ⚠️⚠️ |
| 70% | -63% | +7900% (80×) ⚠️⚠️⚠️ |

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

### Key Finding (Expected)

**Tuned parameters reduce P99 by 30-50%**

| Parameter | Default | Tuned | P99 Impact |
|-----------|---------|-------|------------|
| sched_latency_ns | 6ms | 3ms | -30% |
| sched_cfs_bandwidth_slice_us | 5ms | 1ms | -20% |
| Combined | - | - | -45% |

### Why It Matters

- Shows kernel tuning can partially offset throttling
- Validates Phase 5 admin guide recommendations
- Provides empirical tuning targets

### Implementation Plan

1. Baseline P99 latency with default kernel
2. Apply latency-sensitive tuning profile
3. Re-measure P99 latency
4. Compare results

**Status:** Requires node SSH + root access

---

## Topic 6.4: scx Comparison (📋 Design Only)

### What It Does

Compares CFS vs custom scheduler (scx_lavd) for throttled workloads.

### Key Finding (Expected)

**scx_lavd: -5% throughput, -40% P99 latency vs CFS**

| Scheduler | Throughput | P99 Latency | Context Switches |
|-----------|------------|-------------|------------------|
| CFS | Baseline | Baseline | Baseline |
| scx_lavd | -5% | -40% | -20% |

### Why It Matters

- Shows custom schedulers trade throughput for latency
- Validates Phase 5 "when to use scx" guidance
- Provides data for scx adoption decision

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

### Key Finding (Expected)

**Kubernetes scheduler achieves fair CPU distribution across nodes**

```
Expected: Equal CPU per pod across nodes
Actual: May vary by node CPU pressure
```

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

### Key Finding (Expected)

**Memory limits are binary: no throttling, just OOM kill**

| Aspect | CPU | Memory |
|--------|-----|--------|
| Throttling | Gradual | None (OOM kill) |
| Recovery | Automatic | Manual restart |
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
