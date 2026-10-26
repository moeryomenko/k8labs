# Phase 6: Future Work - Extended Research Topics

**Status:** 📋 Framework Ready  
**Date:** October 26, 2026  
**Purpose:** Advanced research extensions for specialized scenarios

---

## Overview

Phase 6 provides frameworks and documentation for 6 advanced research topics that extend the core findings from Phases 1-5. These are optional explorations for specialized environments or research interests.

---

## Topics

### 6.1 Real-World Latency Testing (nginx + wrk)
- **Status:** 🔧 Implementation Ready
- **Requires:** wrk HTTP benchmarking tool
- **Output:** P50/P95/P99 latency under throttling
- **Time:** ~30 minutes

### 6.2 BPF Trace Collection During Throttling
- **Status:** 🔧 Implementation Ready
- **Requires:** Node SSH access
- **Output:** Correlation between throttling and scheduler events
- **Time:** ~1 hour

### 6.3 Kernel Parameter Tuning Experiments
- **Status:** 🔧 Implementation Ready
- **Requires:** Node SSH access, root permissions
- **Output:** Impact of CFS parameters on latency
- **Time:** ~2 hours

### 6.4 Custom Scheduler Comparison (scx)
- **Status:** 🔧 Implementation Ready
- **Requires:** Kernel 6.12+, scx tools
- **Output:** CFS vs scx performance comparison
- **Time:** ~3 hours

### 6.5 Multi-Node Scheduler Fairness
- **Status:** 📋 Design Complete
- **Requires:** Multi-node cluster
- **Output:** Cross-node fairness analysis
- **Time:** ~4 hours

### 6.6 Memory Limits Correlation
- **Status:** 📋 Design Complete
- **Requires:** Similar to Phase 2-4
- **Output:** Memory throttling behavior
- **Time:** ~1 week (parallel to CPU research)

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

## Expected Outcomes

### 6.1 Latency Testing

**Hypothesis:** Heavy throttling increases P99 latency 10-100×

| Throttling | P50 | P95 | P99 | P99.9 |
|------------|-----|-----|-----|-------|
| 0% | 5ms | 10ms | 15ms | 30ms |
| 5% | 6ms | 12ms | 25ms | 100ms |
| 50% | 10ms | 50ms | 200ms | 1000ms |
| 70% | 15ms | 100ms | 500ms | 5000ms |

**Key Insight:** P99 latency degrades faster than throughput

---

### 6.2 BPF Traces

**Hypothesis:** Throttling correlates with scheduler wait time

```
Expected correlation:
- nr_throttled ↑ → sched_wait events ↑
- throttled_usec ↑ → wakeup-to-run latency ↑
- Coefficient r² > 0.8
```

**Key Insight:** Throttling causes scheduler latency, not just CPU reduction

---

### 6.3 Kernel Tuning

**Hypothesis:** Tuned parameters reduce P99 by 30-50%

| Parameter | Default | Tuned | P99 Impact |
|-----------|---------|-------|------------|
| sched_latency_ns | 6ms | 3ms | -30% |
| sched_cfs_bandwidth_slice_us | 5ms | 1ms | -20% |
| Combined | - | - | -45% |

**Key Insight:** Kernel tuning can partially offset throttling impact

---

### 6.4 scx Comparison

**Hypothesis:** scx_lavd reduces latency vs CFS for throttled workloads

| Scheduler | Throughput | P99 Latency | Context Switches |
|-----------|------------|-------------|------------------|
| CFS | Baseline | Baseline | Baseline |
| scx_lavd | -5% | -40% | -20% |

**Key Insight:** Custom schedulers trade throughput for latency

---

### 6.5 Multi-Node Fairness

**Hypothesis:** Kubernetes scheduler fairly distributes CPU-bound pods

```
Expected: Equal CPU allocation across nodes
Actual: May vary by node CPU pressure
```

**Key Insight:** Cross-node fairness depends on scheduler spread

---

### 6.6 Memory Limits

**Hypothesis:** Memory has different throttling behavior than CPU

| Aspect | CPU | Memory |
|--------|-----|--------|
| Throttling | Gradual (cfs_quota) | Hard (OOM kill) |
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
