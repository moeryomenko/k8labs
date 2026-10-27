# Phase 5 Extension: Implementation Summary

## What Was Done

Phase 5 extends the Kubernetes CPU research to address gaps in the comprehensive requirements:

### 1. Infrastructure Created

#### Experiment Scripts
- **BPF Trace Collection** (`bpf-correlation/collect-during-throttle.py`)
  - Collects `sched:sched_switch` and `sched:sched_wakeup` events during throttling
  - Correlates kernel scheduler latency with cgroup throttling metrics
  - Auto-deploys Phase 4 heavy-throttle workload and captures traces
  
- **Latency Testing** (`experiments/latency-sensitive/run-latency-test.py`)
  - Measures P50/P95/P99 HTTP latency under different throttling levels
  - Uses nginx + a fixed-rate Python client (`wrk`/`wrk2` are unusable here)
  - The Phase 5 run recorded two configurations and no throttling, so it is
    superseded by Phase 7.1, which measured four quota settings
  - Answers: "What is the latency impact, not just throughput?"

#### Administrator Guides (Complete Documentation)
- **KERNEL_CONFIG.md** (300+ lines)
  - Complete CFS scheduler parameter reference
  - UP vs SMP scheduling
  - CPU isolation (`isolcpus`, `cpuset`)
  - Production tuning examples for 3 scenarios:
    1. Latency-sensitive microservices
    2. Batch/ML workloads
    3. Multi-tenant clusters
  
- **CUSTOM_SCHEDULERS.md** (300+ lines)
  - What is sched-ext (scx)?
  - When to use custom schedulers (and when NOT to)
  - Available schedulers: scx_simple, scx_rusty, scx_lavd, scx_nest
  - Installation and setup instructions
  - Migration strategy (test → canary → gradual rollout)
  - Monitoring and debugging
  
- **PRODUCTION_PATTERNS.md** (pending - to be created)
  - Request-only pattern (recommended)
  - Guaranteed pattern (critical services)
  - CPU pinning pattern (ultra-low latency)
  - Multi-tenant pattern (cost control)

#### Automation
- **run-phase5.sh** - Interactive script to run Phase 5 experiments
  - Checks prerequisites (kubectl, cluster access, Python)
  - Validates Phase 4 completion
  - Guides user through experiment selection
  - Checks for advanced capabilities (kernel 6.12+, SSH access)

### 2. Gaps Filled

| Gap | Phase 5 Solution | Status |
|-----|------------------|--------|
| **BPF traces not collected** | Task 5.1 script ready | ✅ Ready to run |
| **Latency (P50/P95/P99) missing** | Task 5.2 script ready | ✅ Ready to run |
| **Kernel config docs missing** | KERNEL_CONFIG.md written | ✅ Complete |
| **Custom scheduler (scx) not tested** | Task 5.4 script + guide ready | ✅ Ready (if kernel supports) |
| **Production guidance missing** | Admin guides written | ✅ Complete |

### 3. Actual Findings

The tasks were later run as Phase 7.1 and Phase 7.4. The estimates previously
printed in this section (a ~500 ms p99 at heavy throttle, ~8 ms with no limit)
were predictions rather than results.

**Task 5.1 (BPF traces), delivered as Phase 7.4:**

| Limit | Throttled | Mean wake-to-run | Worst wake-to-run |
|-------|-----------|------------------|-------------------|
| none | 0% | 6.4 us | 5.5 ms |
| 200m | 96.0% | 9.6 us | 6.9 ms |
| 100m | 96.9% | 13.7 us | 71.7 ms |

The mean stays in microseconds, so the scheduler is not slow. The worst case is
consistent with a task waiting out the remainder of a 100 ms quota period.

**Task 5.2 (latency testing), delivered as Phase 7.1:**

```
Limit    | p50    | p99     | Throttled
---------|--------|---------|----------
none     | 2.04ms | 13.98ms | 0%
120m     | 2.13ms | 69.43ms | 99.7%
100m     | 2.20ms | 67.76ms | 95.6%
70m      | 2.16ms | 98.87ms | 75.1%
```

**Key insight:** p99 degrades about 5-7x while p50 does not move at all, so
mean-based dashboards show nothing. The measured mechanism also confirms that
throttle percentage does not rank configurations: 75.1% throttled had the worst
p99 of the four.

**Task 5.3 (Kernel parameter tuning): NOT RUN**

- No parameter change was measured. There is also no node-wide
  `sched_cfs_bandwidth_slice_us` to tune on a cgroup v2 host: the quota period
  is the second field of each cgroup's `cpu.max`, set by kubelet's
  `cpuCFSQuotaPeriod` (default 100 ms). Phase 7.5 read `cpu.max` = `10000 100000`
  from a pod limited to 100m, with `cpu.max.burst` = 0.
- The claim that shortening the slice would cut p99 spikes was a hypothesis, not
  a result. Phase 7.4 shows the tail comes from waiting out the 100 ms period.

**Task 5.4 (sched_ext comparison): RUN AS PHASE 7.6**

- `scx_lavd --performance` was built and attached on `7.3.0-k8labs` and
  compared with CFS on the same workload matrix.
- The result contradicted the expectation recorded here: a throttled cgroup
  was *not* parked by the bandwidth controller, because the custom scheduler
  did not apply `cpu.max` at all (12.22 CPU-s against a 4.0 CPU-s quota).
  `--enable-cpu-bw` restored enforcement (4.01 CPU-s). See
  `PHASE7_COMPLETE.md` section 7.6.
- CFS is therefore sufficient to explain everything measured here.

---

## File Structure

```
phase5/
├── README.md                           # Phase 5 overview (complete)
├── run-phase5.sh                       # Interactive experiment runner (complete)
│
├── bpf-correlation/
│   └── collect-during-throttle.py      # Task 5.1 (ready to run)
│
├── experiments/
│   ├── latency-sensitive/
│   │   └── run-latency-test.py         # Task 5.2 (ready to run)
│   ├── scheduler-params/               # Task 5.3 (placeholder)
│   └── scx-comparison/                 # Task 5.4 (placeholder)
│
├── admin-guide/
│   ├── KERNEL_CONFIG.md                # Complete (300+ lines)
│   ├── CUSTOM_SCHEDULERS.md            # Complete (300+ lines)
│   └── PRODUCTION_PATTERNS.md          # Pending
│
└── evidence/
    └── (experiment results will go here)
```

---

## How to Run

### Prerequisites Check
```bash
cd research/cpu-limits/phase5
./run-phase5.sh
# Select option 4 to check system capabilities
```

### Run BPF Trace Collection (Task 5.1)
```bash
cd bpf-correlation
python3 collect-during-throttle.py
```

**Requirements:**
- SSH access to cluster nodes
- bpftrace installed on nodes
- Phase 4 completed

**Output:**
- `evidence/bpf-throttling-*.csv` - Raw trace data
- `evidence/bpf-throttling-*-analysis.json` - Analysis with scheduler metrics

---

### Run Latency Testing (Task 5.2)
```bash
cd experiments/latency-sensitive
python3 run-latency-test.py
```

**Requirements:**
- Cluster access (kubectl)
- wrk Docker image available
- ~20 minutes runtime

**Output:**
- `evidence/latency-impact-*.json` - P50/P95/P99 for 3 configurations
- Comparison table printed to console

---

### Run Both (Recommended)
```bash
./run-phase5.sh
# Select option 3
```

---

## Documentation Status

| Document | Status | Lines | Coverage |
|----------|--------|-------|----------|
| phase5/README.md | ✅ Complete | 300+ | Objectives, experiments, success criteria |
| admin-guide/KERNEL_CONFIG.md | ✅ Complete | 350+ | CFS params, UP/SMP, CPU isolation, tuning |
| admin-guide/CUSTOM_SCHEDULERS.md | ✅ Complete | 350+ | scx overview, when to use, migration |
| admin-guide/PRODUCTION_PATTERNS.md | ⏸️ Pending | - | Real-world deployment patterns |

**Total documentation added:** ~1000+ lines of administrator guidance

---

## Integration with Existing Research

Phase 5 builds on:
- **Phase 2:** Uses burstable workload as baseline for comparison
- **Phase 3:** BPF infrastructure deployed, now used to collect traces
- **Phase 4:** Uses the heavy-throttle config (500m limit, 19.22 ops/sec) as the
  throttled workload

Phase 5 delivered:
- ✅ **BPF traces** - done as Phase 7.4 (Task 5.1)
- ✅ **Latency impact** - done as Phase 7.1 (Task 5.2)
- ✅ **Kernel docs** - CFS and scheduler documentation
- ⚠️ **Custom schedulers** - documentation only; sched_ext was never installed
  or benchmarked (Task 5.4 not run)
- ✅ **Production guidance** - admin guides for real-world use

---

## What's Next

### Remaining gaps

1. **Task 5.3** (kernel scheduler parameters) - not run
2. **Task 5.4** (sched_ext comparison) - run as Phase 7.6
3. **Phase 7.7** (incident simulation) - run

### State of the research

- ✅ 4 workload types tested (CPU, IO, memory, hybrid)
- ✅ 3 QoS classes compared (BestEffort, Burstable, Guaranteed), with weights
  read from pod cgroups
- ✅ Throttling behaviour measured with throughput, throttle percentage and
  latency (Phases 4, 7.1, 7.4)
- ✅ BPF traces collected and analysed (Phase 7.4)
- ✅ Latency impact measured (p50/p99, Phase 7.1)
- ⚠️ Kernel schedulers documented, not compared
- ✅ Production guidance provided (admin guides)

---

## Success Criteria

Phase 5 is **successful** when:

- [x] Phase 5 infrastructure created
- [x] Admin guides written (KERNEL_CONFIG.md, CUSTOM_SCHEDULERS.md)
- [ ] Task 5.1 executed (BPF traces collected)
- [ ] Task 5.2 executed (Latency impact measured)
- [ ] RESEARCH_COMPLETE.md updated with Phase 5 findings
- [ ] Evidence published (BPF traces, latency results)

**Current status:** 3/6 ✅ (50% complete)

**Remaining work:** Run experiments (Tasks 5.1 and 5.2), document findings

---

**Date:** October 26, 2026  
**Author:** k8labs Research Team  
**Status:** Phase 5 Infrastructure Complete, Experiments Ready to Run
