# Phase 6: Future Work - Extended Research Topics

**Status:** partly superseded by Phase 7. Topic 6.1 was answered by Phase 7.1 and
7.4 with measured data; 6.2 by Phase 7.4; 6.3 and 6.4 were not run.
**Date:** October 26, 2026  
**Purpose:** Advanced research extensions for specialized scenarios

---

## Overview

Phase 6 provides frameworks and documentation for 6 advanced research topics that extend the core findings from Phases 1-5. These are optional explorations for specialized environments or research interests.

---

## Topics

### 6.1 Real-World Latency Testing (nginx + wrk)
- **Status:** ✅ Measured as Phase 7.1 (wrk2 panics on this cluster; a fixed-rate
  Python client was used instead)
- **Requires:** wrk HTTP benchmarking tool
- **Output:** P50/P95/P99 latency under throttling

### 6.2 BPF Trace Collection During Throttling
- **Status:** ✅ Measured as Phase 7.4 (bpftrace in an unprivileged reader pod, no
  node SSH needed)
- **Output:** Correlation between throttling and scheduler events

### 6.3 Kernel Parameter Tuning Experiments
- **Status:** ❌ NOT RUN
- **Requires:** Node SSH access, root permissions
- **Output:** Impact of CFS parameters on latency

### 6.4 Custom Scheduler Comparison (scx)
- **Status:** ✅ Run as Phase 7.6 (`scx_lavd` against CFS on `7.3.0-k8labs`)
- **Requires:** Kernel 6.12+, scx tools
- **Output:** CFS vs scx performance comparison

### 6.5 Multi-Node Scheduler Fairness
- **Status:** 📋 Design only, not run
- **Requires:** Multi-node cluster
- **Output:** Cross-node fairness analysis

### 6.6 Memory Limits Correlation
- **Status:** 📋 Design only, not run
- **Requires:** Similar to Phase 2-4
- **Output:** Memory throttling behavior

---

## Quick Start

### Check Prerequisites

```bash
cd phase6
./check-prerequisites.sh
```

### Run Individual Topics

```bash
# Topic 6.1: Latency testing
cd 6.1-latency-testing
./run-nginx-latency-test.sh

# Topic 6.2: BPF traces
cd 6.2-bpf-traces
./collect-traces.sh

# Topic 6.3: Kernel tuning
cd 6.3-kernel-tuning
./run-tuning-experiments.sh

# Topic 6.4: scx comparison
cd 6.4-scx-comparison
./compare-schedulers.sh

# Topic 6.5: Multi-node fairness
cd 6.5-multi-node
./run-fairness-test.sh

# Topic 6.6: Memory limits
cd 6.6-memory-limits
python3 run-memory-experiments.py
```

---

## Directory Structure

```
phase6/
├── README.md                           ← You are here
├── check-prerequisites.sh              ← Prerequisite checker
│
├── 6.1-latency-testing/                ← Real-world latency (nginx+wrk)
│   ├── README.md
│   ├── run-nginx-latency-test.sh
│   ├── manifests/
│   ├── wrk-scripts/
│   └── analyze-latency.py
│
├── 6.2-bpf-traces/                     ← BPF correlation during throttling
│   ├── README.md
│   ├── collect-traces.sh
│   ├── bpf-programs/
│   └── analyze-correlation.py
│
├── 6.3-kernel-tuning/                  ← Kernel parameter experiments
│   ├── README.md
│   ├── run-tuning-experiments.sh
│   ├── tuning-profiles/
│   └── analyze-impact.py
│
├── 6.4-scx-comparison/                 ← Custom scheduler testing
│   ├── README.md
│   ├── compare-schedulers.sh
│   ├── scx-configs/
│   └── analyze-comparison.py
│
├── 6.5-multi-node/                     ← Multi-node fairness
│   ├── README.md
│   ├── run-fairness-test.sh
│   ├── manifests/
│   └── analyze-fairness.py
│
└── 6.6-memory-limits/                  ← Memory throttling research
    ├── README.md
    ├── run-memory-experiments.py
    ├── workloads/
    └── generate-memory-guide.py
```

---

## Implementation Priority

### High Value (Recommended)

1. **6.1 Latency Testing** - Demonstrates user-visible impact
2. **6.6 Memory Limits** - Parallel research track

### Medium Value (Nice to Have)

3. **6.2 BPF Traces** - Deep scheduler visibility
4. **6.3 Kernel Tuning** - Optimization opportunities

### Low Value (Research Only)

5. **6.4 scx Comparison** - Niche use case
6. **6.5 Multi-Node** - Complex setup

---

## Measured Outcomes

### 6.1 Latency Testing (measured as Phase 7.1)

Evidence: `evidence/phase7/real-apps/nginx-throttling-20261026T191420Z.json`

| CPU limit | Throughput | p50 | p99 |
|-----------|-----------|-----|-----|
| none | 7216 req/s | 2.04 ms | 13.98 ms |
| 120m | 3481 req/s | 2.13 ms | 69.43 ms |
| 100m | 2991 req/s | 2.20 ms | 67.76 ms |
| 70m | 2071 req/s | 2.16 ms | 98.87 ms |

**Key Insight:** p99 degrades 5-7× while p50 does not move at all. The
`| Throttling | P50 | P95 | P99 | P99.9 |` table that used to stand here was a
projection and has been removed; p95 was not recorded.

---

### 6.2 BPF Traces (measured as Phase 7.4)

Evidence: `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`

**Hypothesis:** throttling correlates with scheduler wait time

Measured wake-to-run delay (`sched:sched_wakeup` to `sched_switch`):

| CPU limit | Throttled | Mean | Max |
|-----------|-----------|------|-----|
| none | 0% | 6.4 µs | 5.49 ms |
| 200m | 96.05% | 9.6 µs | 6.9 ms |
| 100m | 96.94% | 13.7 µs | 71.7 ms |

**Key Insight:** throttling inflates the *maximum* delay by an order of
magnitude while the mean barely moves. The `r² > 0.8` correlation that used to
stand here was a projection and was removed; no regression was fitted.

---

### 6.3 Kernel Tuning

**Status: NOT RUN.** No parameter was tuned or measured, and the impact
percentages previously listed here were never produced.

**Note on scope:** on cgroup v2 there is no node-wide bandwidth-slice setting.
The quota period is the second field of each cgroup's `cpu.max`, written from
kubelet's `--cpu-cfs-quota-period`. Phase 7.5 verified `cpu.max = "10000 100000"`
for a 100m limit on the test node.

**Key Insight (measured):** the tail latency came from waiting out the quota
period. Phase 7.1: p99 69.43 ms at 120m versus 13.98 ms with no limit, p50 flat.

---

### 6.4 scx Comparison (❌ NOT RUN)

**Hypothesis (untested):** a sched_ext scheduler changes latency for throttled
workloads

| Scheduler | Throughput | P99 Latency | Context Switches |
|-----------|------------|-------------|------------------|
| CFS | measured elsewhere | measured elsewhere | measured (7.4) |
| scx_lavd | not run | not run | not run |

**Key Insight:** none - no run exists. The `-5% / -40% / -20%` row that used to
stand here was a projection, not data.

---

### 6.5 Multi-Node Fairness (📋 NOT RUN)

**Hypothesis (untested):** Kubernetes spreads CPU-bound pods evenly across nodes

**Key Insight:** none. The test environment had one node, so no cross-node
fairness was measured.

---

### 6.6 Memory Limits

**Hypothesis:** Memory has different throttling behavior than CPU

| Aspect | CPU | Memory |
|--------|-----|--------|
| Throttling | Gradual (cpu.max quota) | None at the limit; OOM kill past memory.max |
| Recovery | Automatic | Manual (restart) |
| Observability | Good (metrics) | Moderate (events) |

**Key Insight:** Memory limits are binary (kill vs no kill)

---

## Prerequisites by Topic

### 6.1 Latency Testing
- ✅ Kubernetes cluster
- ✅ kubectl access
- ⚠️ wrk tool (`apt install wrk` or `brew install wrk`)

### 6.2 BPF Traces
- ✅ Kubernetes cluster
- ⚠️ Node SSH access
- ⚠️ bpftrace installed on nodes
- ⚠️ Root/sudo permissions

### 6.3 Kernel Tuning
- ✅ Kubernetes cluster
- ⚠️ Node SSH access
- ⚠️ Root/sudo permissions
- ⚠️ Ability to modify /proc/sys/kernel

### 6.4 scx Comparison
- ⚠️ Kernel 6.12+ with CONFIG_SCHED_CLASS_EXT=y
- ⚠️ scx tools installed
- ⚠️ Node access to load schedulers

### 6.5 Multi-Node Fairness
- ⚠️ Multi-node cluster (3+ nodes)
- ✅ kubectl access
- ✅ Pod anti-affinity

### 6.6 Memory Limits
- ✅ Kubernetes cluster
- ✅ kubectl access
- ✅ cAdvisor metrics

---

## Time Estimates

| Topic | Setup | Execution | Analysis | Total |
|-------|-------|-----------|----------|-------|
| 6.1 Latency | 10m | 15m | 10m | ~35m |
| 6.2 BPF | 20m | 30m | 20m | ~70m |
| 6.3 Tuning | 30m | 60m | 30m | ~120m |
| 6.4 scx | 60m | 90m | 30m | ~180m |
| 6.5 Multi-Node | 45m | 120m | 45m | ~210m |
| 6.6 Memory | 30m | 300m | 60m | ~390m |

---

## Integration with Phases 1-5

### Builds On

- **Phase 2:** Same QoS methodology
- **Phase 3:** BPF infrastructure
- **Phase 4:** Throttling workloads
- **Phase 5:** Admin guides

### Extends

- **6.1:** Adds latency metrics to Phase 4 throttling
- **6.2:** Uses Phase 3 BPF with Phase 4 workloads
- **6.3:** Implements Phase 5 kernel tuning recommendations
- **6.4:** Tests Phase 5 custom scheduler guide
- **6.5:** Multi-node extension of Phase 2
- **6.6:** Parallel track to CPU research

---

## Citation

If you use these methodologies:

```
k8labs Kubernetes CPU Research - Phase 6: Future Work
October 2026
https://github.com/<org>/k8labs/tree/main/research/cpu-limits/phase6
```

---

## Support

**Status:** Framework ready, implementation on demand  
**Questions:** Open a GitHub issue  
**Contributions:** Pull requests welcome

Choose your topics and dive in! 🚀
