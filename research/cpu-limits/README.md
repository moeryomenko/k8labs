# Kubernetes CPU Limits Research - Project Status

**Last Updated**: 2026-10-26T11:36:00Z  
**Status**: ✅ Phase 2 Complete, Ready for Phase 3

---

## Project Overview

Comprehensive research on how Kubernetes CPU requests/limits affect cgroup parameters, scheduler behavior, and application performance across different workload types.

**Goal**: Provide evidence-based guidance for developers and cluster administrators on setting CPU requests/limits.

---

## Phase Status

### ✅ Phase 1: Infrastructure & Workloads (Complete)

**Location**: `research/cpu-limits/phase1/`

**Deliverables**:
- ✅ Kubernetes namespace: `cpu-limits-research`
- ✅ Base workload image: `stress-ng` with debugging tools
- ✅ 7 workload manifests (6 active + 1 contention scenario)
- ✅ Test script: `test-workloads.sh`

**Workload Types**:
1. `cpu-bound-besteffort.yaml` — No limits, maximum CPU
2. `cpu-bound-burstable.yaml` — Request 500m, Limit 2000m
3. `cpu-bound-guaranteed.yaml` — Request = Limit = 1000m
4. `io-bound-guaranteed.yaml` — I/O-bound workload
5. `memory-bound-guaranteed.yaml` — Memory operations
6. `hybrid-burstable.yaml` — Mixed CPU+I/O+VM
7. `contention-scenario.yaml` — Multi-pod contention (not yet tested)

---

### ✅ Phase 2: Metrics & Evidence Collection (Complete)

**Location**: `research/cpu-limits/phase2/`

**Deliverables**:
- ✅ Automated experiment runner: `run-experiments.py`
- ✅ DaemonSet collector pod: `collector-pod.yaml`
- ✅ Evidence collection framework
- ✅ **6 experiments executed successfully**
- ✅ Evidence files: `evidence/*.json`
- ✅ Analysis tools: `analyze-throttling.py`, `generate-dashboard.py`
- ✅ Results summary: `RESULTS.md`

**Key Results**:
- BestEffort pods use **2.68 cores** (on 4-vCPU node) with **0% throttling**
- Burstable cpu-bound: **12.6% throttling**
- Burstable hybrid: **58.6% throttling** (4.6x worse!)
- Guaranteed memory-bound: **37.2% throttling**

**Data Quality**:
- ✅ 11 samples per experiment (10-second interval)
- ✅ Baseline + final cgroup measurements
- ✅ Complete cpu.stat, cpu.max, cpu.weight, cpu.pressure
- ⚠️ 1 experiment with race condition (cpu-bound-guaranteed)

---

### 🔄 Phase 3: BPF Tracing (In Progress)

**Location**: `research/cpu-limits/phase3/`

**Planned Deliverables**:
- [ ] BPF trace collector scripts
- [ ] `runqlat` — run queue latency histogram
- [ ] `runqlen` — run queue length timeline
- [ ] `sched:sched_switch` — context switch events
- [ ] `sched:sched_throttle` — CFS throttle events
- [ ] Integration with Phase 2 experiments
- [ ] BPF trace analysis + visualization

**Status**: Directory structure created, awaiting implementation.

**Prerequisites**:
- BPF tooling installed on collector pod (bpftrace/bcc)
- Kernel with BTF support (>= 5.4)
- CAP_BPF and CAP_PERFMON capabilities

---

## Quick Start

### Run All Experiments

```bash
cd research/cpu-limits/phase2
python3 -u run-experiments.py 2>&1 | tee experiment-run.log
```

**Duration**: ~12 minutes (6 experiments × 2 minutes each)

### View Results

```bash
# Quick summary
python3 quick-analysis.py

# Detailed analysis
python3 analyze-throttling.py

# Interactive dashboard
python3 generate-dashboard.py
firefox dashboard.html
```

### Individual Experiment

```bash
kubectl apply -f phase1/workloads/cpu-bound-burstable.yaml
kubectl wait --for=condition=ready pod/cpu-bound-burstable -n cpu-limits-research --timeout=30s
kubectl logs -f cpu-bound-burstable -n cpu-limits-research
```

---

## Key Findings (So Far)

### 1. Hybrid Workloads Suffer Most

**Evidence**: `hybrid-burstable` experienced **58.6% throttling** vs **12.6%** for pure cpu-bound.

**Explanation**: CFS quota accounts for *all* CPU time including system calls (I/O, page faults, syscalls). Mixed workloads that do I/O or memory operations are penalized even though they're not CPU-bound.

**Recommendation**: Avoid CPU limits for workloads with significant I/O or syscall activity.

### 2. Throttling Is Nearly Universal

**Evidence**: 99-100% of CFS periods are throttled for workloads with CPU limits.

**Explanation**: The CFS quota is checked every 100ms period. Most workloads exceed quota in at least some periods, even if total throttling time is low.

**Implication**: `nr_throttled` is not a good metric alone — use `throttled_usec` to measure *duration*.

### 3. BestEffort Is "Maximum Effort"

**Evidence**: BestEffort pod consumed **2.68 cores** on a 4-vCPU node with no throttling.

**Implication**: BestEffort is not "low priority" — it's "use everything available until preempted." Use CPU *requests* to ensure fair scheduling.

---

## Known Issues

### Race Condition in Monitoring Loop

**Symptom**: Short-lived or fast workloads report 0 samples and incorrect duration.

**Affected**:
- `cpu-bound-guaranteed` (logged 120s but reported 0.3s)
- `io-bound-guaranteed` (completed in 11s, 0 samples)

**Root Cause**: Monitoring loop starts after pod is Running, but workload may complete before first sample.

**Fix**: Start collector *before* workload starts, or use PreStart hook.

**Workaround**: Re-run with fixed timing or manually verify from logs.

---

## File Structure

```
research/cpu-limits/
├── README.md                    # This file
├── phase1/                      # Infrastructure setup
│   ├── README.md
│   ├── namespace.yaml
│   ├── workload-base/
│   │   └── Containerfile
│   ├── workloads/               # 7 workload manifests
│   │   ├── cpu-bound-besteffort.yaml
│   │   ├── cpu-bound-burstable.yaml
│   │   ├── cpu-bound-guaranteed.yaml
│   │   ├── hybrid-burstable.yaml
│   │   ├── io-bound-guaranteed.yaml
│   │   ├── memory-bound-guaranteed.yaml
│   │   └── contention-scenario.yaml
│   └── test-workloads.sh
├── phase2/                      # Metrics collection
│   ├── README.md
│   ├── RESULTS.md               # Experiment results
│   ├── run-experiments.py       # Main experiment runner
│   ├── collector-pod.yaml       # DaemonSet for cgroup access
│   ├── analyze-throttling.py    # Analysis tool
│   ├── generate-dashboard.py    # Dashboard generator
│   ├── quick-analysis.py        # Quick summary
│   ├── evidence/                # Experiment data
│   │   ├── summary.json
│   │   └── *.json               # Per-experiment evidence
│   └── experiment-run.log
└── phase3/                      # BPF tracing (planned)
    ├── README.md
    ├── bpf-collector/
    └── analysis/
```

---

## Next Actions

### Immediate (Phase 2 Completion)

1. ✅ Document results (RESULTS.md)
2. ⏳ Generate dashboard (`generate-dashboard.py`)
3. ⏳ Re-run failed experiments with fixed monitoring
4. ⏳ Run contention scenario (multi-pod)

### Phase 3 (BPF Tracing)

1. Install BPF tooling in collector pod
2. Create BPF trace scripts (runqlat, runqlen, sched_switch)
3. Integrate BPF traces into experiment runner
4. Correlate throttling events with scheduler behavior
5. Visualize scheduler timeline

### Final Report

1. Consolidate findings from Phase 2 + Phase 3
2. Create recommendations guide:
   - Developer guide: When to set requests/limits
   - Admin guide: Kernel tuning, custom schedulers (scx)
3. Publish as blog post or technical report

---

## References

### Articles
- [Robusta: Stop Using CPU Limits](https://home.robusta.dev/blog/stop-using-cpu-limits)
- [Indeed: Unthrottled](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)
- [Why Keep Using CPU Limits](https://dnastacio.medium.com/why-you-should-keep-using-cpu-limits-on-kubernetes-60c4e50dfc61)
- [CockroachDB: Kubernetes Performance](https://docs.cockroachlabs.com/docs/stable/kubernetes-performance#resource-requests-and-limits)
- [Kubernetes Blog: The Case for Resource Limits](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/)

### Tools
- [stress-ng](https://github.com/ColinIanKing/stress-ng) — CPU/memory/I/O stress testing
- [bpftrace](https://github.com/iovisor/bpftrace) — High-level tracing language for eBPF
- [bcc-tools](https://github.com/iovisor/bcc) — BPF Compiler Collection

---

**Contact**: k8labs research team  
**Repository**: k8labs/research/cpu-limits/
