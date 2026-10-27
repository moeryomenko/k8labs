# Phase 7: Extended Research - Comprehensive Scheduling Analysis

**Goal**: Provide deeper analysis of scheduler behavior, real-world application impact, and production-grade observability

## Motivation

Phases 1-6 established foundational understanding. Phase 7 extends this with:
- Real application workloads (not synthetic stress tests)
- Live BPF tracing during experiments
- Multi-workload interference patterns
- QoS class comparative analysis
- Production incident simulation

## Phase 7 Topics

### 7.1 Real Application Workload Testing
**Duration**: 3-4 hours

Test real-world applications under throttling:
- Web server (nginx) with wrk2 load generator
- Database (PostgreSQL) with pgbench
- Cache (Redis) with redis-benchmark
- Message queue (RabbitMQ) with perf-test

**Metrics**:
- Request latency (P50, P95, P99, P99.9)
- Throughput (requests/sec)
- Error rate
- Tail latency amplification under throttling

### 7.2 QoS Class Comparative Study
**Duration**: 2-3 hours

Compare BestEffort, Burstable, Guaranteed under same load:
- Same workload, 3 QoS configurations
- Measure scheduler fairness
- Identify OOM vs throttling behavior
- Node resource contention scenarios

**Evidence**:
- Scheduling latency per QoS class
- Preemption counts
- QoS-based prioritization effectiveness

### 7.3 Multi-Workload Interference
**Duration**: 2-3 hours

"Noisy neighbor" experiments:
- Latency-sensitive + CPU-intensive on same node
- Multiple throttled pods competing for CPU
- Impact of one runaway process on others

**Key questions**:
- Does throttling protect neighbors?
- What's the blast radius of a CPU-intensive pod?
- How does CFS fairness work with limits?

### 7.4 Live BPF Tracing Integration
**Duration**: 3-4 hours

Real-time BPF collection during experiments:
- bpftrace scripts for scheduler events
- eBPF programs for per-cgroup tracking
- Correlation: cgroup stats ↔ BPF events ↔ app metrics

**Traces**:
- `sched_switch`: Context switch patterns
- `sched_wakeup_new`: New process creation
- `sched_migrate_task`: Cross-CPU migration
- `sched_process_wait`: Wait queue analysis

### 7.5 Cgroup V2 Deep Dive
**Duration**: 2 hours

Detailed cgroup parameter exploration:
- `cpu.weight` vs `cpu.max` interaction
- `cpu.pressure` PSI (Pressure Stall Information)
- `cpu.stat` field-by-field analysis
- `cpu.uclamp.min` / `cpu.uclamp.max` (utilization clamping)

**Output**: Complete reference of all cgroup v2 CPU parameters

### 7.6 Kernel Scheduler Comparison
**Duration**: 3-4 hours

Test same workloads on different kernels:
- Standard Ubuntu kernel (CFS)
- Low-latency kernel (preempt)
- Real-time kernel (PREEMPT_RT)
- Custom scheduler (scx_rusty via sched_ext)

**Metrics**:
- Scheduling latency distribution
- Throughput under load
- Predictability (jitter)

### 7.7 Production Incident Simulation
**Duration**: 2-3 hours

Replicate common production scenarios:
- Sudden traffic spike (burst capacity test)
- Gradual load increase (autoscaling race)
- Pod eviction and rescheduling
- Node drain with CPU-limited pods

**Evidence**: Incident timelines with metrics

### 7.8 Cost-Performance Analysis
**Duration**: 2 hours

Economic impact of CPU limits:
- Wasted capacity (throttled with idle CPU)
- Over-provisioning costs
- Right-sizing recommendations
- TCO calculator based on throttling data

## Expected Outputs

### Extended Evidence Files
```
evidence/phase7/
├── real-apps/
│   ├── nginx-latency-comparison.json
│   ├── postgresql-tps-throttling.json
│   ├── redis-throughput-impact.json
│   └── rabbitmq-message-latency.json
├── qos-comparison/
│   ├── besteffort-vs-burstable-vs-guaranteed.json
│   └── scheduler-fairness-analysis.json
├── interference/
│   ├── noisy-neighbor-impact.json
│   └── multi-pod-throttling.json
├── live-bpf/
│   ├── sched-switch-traces.json
│   ├── context-switch-heatmap.json
│   └── wait-time-distribution.json
├── cgroup-v2/
│   ├── cpu-weight-vs-max-comparison.json
│   ├── psi-pressure-correlation.json
│   └── uclamp-effectiveness.json
├── kernel-comparison/
│   ├── cfs-vs-rt-latency.json
│   └── scx-scheduler-evaluation.json
├── incidents/
│   ├── traffic-spike-timeline.json
│   └── autoscaling-race-analysis.json
└── cost-analysis/
    └── throttling-tco-model.json
```

### Enhanced Guides
- **REAL_WORLD_PLAYBOOK.md**: Production scenarios + solutions
- **CGROUP_V2_REFERENCE.md**: Complete parameter documentation
- **BPF_TRACING_GUIDE.md**: How to trace your own workloads
- **COST_OPTIMIZATION.md**: Financial impact analysis

### Interactive Dashboards
- Grafana dashboard with all Phase 7 metrics
- Live BPF trace visualizations
- QoS comparison panels

## Success Criteria

1. ✅ At least 10 real application workloads tested
2. ✅ Live BPF traces collected during experiments (not simulated)
3. ✅ QoS class behavior fully documented with evidence
4. ✅ Production incident patterns demonstrated
5. ✅ Cost model validated with real cluster data

## Timeline

**Total estimate**: 20-25 hours of experiment runtime + analysis

**Week 1**: Topics 7.1-7.3 (real apps, QoS, interference)  
**Week 2**: Topics 7.4-7.5 (BPF, cgroup deep dive)  
**Week 3**: Topics 7.6-7.8 (kernels, incidents, cost)

## Prerequisites

- Phases 1-6 completed ✅
- Access to k8labs cluster ✅
- BPF tools installed (bpftrace, bcc-tools)
- Real application images available
- Prometheus + Grafana deployed (optional but recommended)

---

**Status**: 📋 PLANNED  
**Next**: Begin with Topic 7.1 (Real Application Workloads)
