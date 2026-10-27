# Kubernetes CPU Limits Research

**Comprehensive empirical analysis of how Kubernetes CPU requests and limits affect cgroup parameters, scheduler behavior, and application performance**

[![Research Status](https://img.shields.io/badge/status-investigation%20complete-brightgreen)]()
[![Evidence Files](https://img.shields.io/badge/evidence%20files-32%20JSON-green)]()
[![Documentation](https://img.shields.io/badge/docs-comprehensive-brightgreen)]()
[![Phase 7](https://img.shields.io/badge/phase%207-6%20of%208%20measured-blue)]()

---

## 📖 Table of Contents

- [Overview](#overview)
- [Key Findings](#key-findings)
- [Quick Start](#quick-start)
- [Research Phases](#research-phases)
- [Documentation](#documentation)
- [Evidence & Reproducibility](#evidence--reproducibility)
- [How to Use This Research](#how-to-use-this-research)
- [Contributing](#contributing)

---

## Overview

This research project provides **empirical, reproducible evidence** of how Kubernetes CPU limits affect:
- Application performance (latency, throughput, error rates)
- Linux kernel scheduler behavior (context switches, wait time, throttling)
- Cgroup v2 parameters (quota, periods, throttled time)
- Production operations (cost, capacity planning, incident response)

**Motivation**: Conflicting guidance exists on whether to use CPU limits. This research provides data-driven recommendations.

### What Makes This Different

✅ **Empirical**: measured on a live cluster; every number cites an evidence file  
✅ **Comprehensive**: 7 phases covering synthetic workloads through real applications  
✅ **Reproducible**: all scripts, manifests and a single-command runner included  
✅ **Production-focused**: guides for developers and administrators  
✅ **Low-level**: bpftrace captures the actual wake-to-run delay under a quota  

**Scope note**: everything claimed as measured was executed on one 4-core node
running `7.3.0-k8labs`, plus one drained 2-core worker for 7.7. Every figure
names its evidence file.

---

## Key Findings

### 🎯 Main Insight

**CPU throttling is not gradual, and it attacks the tail.** When a container
exhausts its quota:

1. Its tasks are **prevented from running** until the next period starts
2. They wait **up to 100 ms** (the quota period), even on an idle node
3. The **median** request barely changes while **p99** collapses

Measured on nginx at a fixed offered rate (`evidence/phase7/real-apps/nginx-throttling-20261026T191420Z.json`):

| CPU limit | Throughput | p50 | p99 | Throttled periods |
|-----------|-----------|-----|-----|-------------------|
| none | 7216 req/s | 2.04 ms | 13.98 ms | 0% |
| 120m | 3481 req/s | 2.13 ms | 69.43 ms | 99.7% |
| 100m | 2991 req/s | 2.20 ms | 67.76 ms | 95.6% |
| 70m | 2071 req/s | 2.16 ms | 98.87 ms | 75.1% |

Note the last two rows: the less-throttled configuration had the **worse**
latency. Throttle percentage finds candidates; latency ranks them.

### 📊 Throughput Impact (CPU-bound, Phase 4)

| Config | requests | limits | ops/sec | Loss |
|--------|----------|--------|---------|------|
| none | 2000m | 4000m | 139.05 | baseline |
| light | 1000m | 3000m | 108.78 | -21.8% |
| moderate | 500m | 1500m | 53.57 | -61.5% |
| heavy | 100m | 500m | 19.22 | -86.2% |

`phase4/evidence/throttling-summary.json`. The throughput loss tracks the quota
cut: heavy (0.5 core) against the 2000m/4000m reference (up to 4 cores) is an
8x quota reduction and a 7.2x throughput loss (139.05 → 19.22 ops/s). The
relation is not linear; the residual is forced wait with nothing to run.

### 🔬 Scheduler Behaviour (Phase 7.4)

bpftrace measured wake-to-run delay for nginx tasks
(`evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`):

| Limit | Throttled | Mean wait | Worst wait | p99 |
|-------|-----------|-----------|------------|-----|
| none | 0% | 6.4 us | 5.5 ms | 4.53 ms |
| 200m | 96.0% | 9.6 us | 6.9 ms | 56.34 ms |
| 100m | 96.9% | 13.7 us | 71.7 ms | 81.99 ms |

The scheduler is not slow — the mean stays in microseconds. A minority of
wakeups wait out the remainder of a 100 ms period, and those are the slow
requests.

### 💡 Recommendations

**For Developers**:
- ✅ **Set `requests.cpu`** from measured P95 usage (drives scheduling and `cpu.weight`)
- ❌ **Do not set `limits.cpu`** on latency-sensitive workloads
- ⚠️ Where you do need a limit (untrusted tenants, batch):
  - Cap the pod that *causes* the contention, not the one that suffers
  - For CPU-bound batch, set limit = request so no forced wait occurs

**For Administrators**:
- ✅ Leave default kernel scheduler parameters alone (nothing was measured that justifies changing them)
- ✅ Monitor throttling via Prometheus (`container_cpu_cfs_throttled_seconds_total`) **and** `cpu.pressure`
- ✅ Alert on throttle percentage to find candidates, but gate any action on the service's own p99
- ⚠️ Treat custom schedulers (sched_ext) as documentation, not a measured option

---

## Quick Start

### For Developers

**Read this first**: [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md)

Quick decision tree:
```yaml
Is your workload latency-sensitive (API, user-facing)?
├─ YES → Set requests only, no CPU limit
└─ NO → Is it batch/background?
     ├─ YES → Set limits = requests (or omit limits)
     └─ NO → Set requests only, watch p99 and throttling
```

**Example (production API)**:
```yaml
resources:
  requests:
    cpu: "1000m"      # P95 observed usage
    memory: "512Mi"
  # No CPU limit - allow bursting
  limits:
    memory: "1Gi"     # Memory limit is fine
```

### For Administrators

**Read this first**: [`ADMINISTRATOR_GUIDE.md`](ADMINISTRATOR_GUIDE.md)

**Quick setup**:
1. Deploy Prometheus + Grafana
2. Import dashboard: `evidence/prometheus-integration/grafana-dashboard.json`
3. Apply alerts: `kubectl apply -f evidence/prometheus-integration/prometheus-alerts.yaml`
4. Monitor cluster for 1 week
5. Audit pods with sustained throttling and check whether their service has a latency SLO

**PromQL to find problematic pods**:
```promql
100 * rate(container_cpu_cfs_throttled_seconds_total[5m])
/ (rate(container_cpu_usage_seconds_total[5m])
   + rate(container_cpu_cfs_throttled_seconds_total[5m]))
> 25
```

### For Researchers

**Reproduce experiments**:
```bash
# Clone repository
git clone https://github.com/your-org/k8labs
cd k8labs/research/cpu-limits

# Phase 4: Throttling experiments (6-8 minutes)
cd phase4/experiments
python3 run-throttling-experiments.py

# Phase 6: BPF correlation (< 1 minute, theoretical)
cd ../../phase6/6.2-bpf-correlation
python3 analyze-scheduler-correlation.py

# Phase 7: Real app testing (6-8 minutes)
cd ../../phase7/7.1-real-apps
python3 test-nginx-throttling.py

# All evidence stored in research/cpu-limits/evidence/
```

---

## Research Phases

### ✅ Phase 1: Foundation (Complete)
- Workload generators: cpu-bound, io-bound, memory-bound, hybrid
- Cgroup metrics collection infrastructure
- Evidence framework (JSON format)

**Key output**: `phase1/generators/` — stress-ng based workload scripts

### ✅ Phase 2: Workload Characterization (Complete)
- 6 workload manifests (BestEffort, Burstable, Guaranteed QoS)
- Baseline performance profiles
- Duration analysis: 0.3s to 122s

**Key output**: `evidence/summary.json` — baseline metrics for all workload types

### ✅ Phase 3: BPF Tracing Infrastructure (Complete)
- systemd service for automatic BPF collection
- Scheduler event capture (sched_switch, sched_wakeup, sched_stat_*)
- Integration with cgroup stats

**Key output**: `phase3/bpf-tracing-service/` — systemd unit files

### ✅ Phase 4: Throttling Experiments (Complete)
- 4 configurations: no-throttle, light, moderate, heavy
- Performance baseline: 139.05 ops/sec (no limit)
- Worst case: 19.22 ops/sec (86.2% loss)

**Key output**: `phase4/evidence/` — 4 run files plus `throttling-summary.json`

### ✅ Phase 5: Production Patterns (Complete)
- Latency testing framework (P50/P95/P99)
- Production deployment patterns
- Decision trees for limit selection

**Key output**: `phase5/PRODUCTION_PATTERNS.md`

### ✅ Phase 6: Low-Level Analysis (Complete)
- BPF correlation analysis (scheduler modeling)
- Prometheus integration (queries, dashboard, alerts)
- Comprehensive interpretation (severity, amplification)

**Key output**: 
- `evidence/bpf-correlation-analysis.json`
- `evidence/prometheus-integration/`

### ✅ Phase 7: Extended Research (all eight topics; seven measured)

**Measured** (evidence in `evidence/phase7/`):

| Topic | What it established |
|-------|---------------------|
| 7.1 Real application (nginx) | p99 13.98 -> 69.43 ms at 120m; p50 flat at 2.04 -> 2.16 ms |
| 7.2 QoS class comparison | Weights 1/59/100 and nice 19/2/0 read from pod cgroups; BestEffort p99 65.44 ms vs Burstable 31.28 ms |
| 7.3 Noisy neighbour | Capping offenders cut their CPU 75%; victim p50 -79%, p99 -2.9% |
| 7.4 Live BPF scheduler trace | Mean wake-to-run 6.4 -> 13.7 us, worst case 5.5 -> 71.7 ms |
| 7.5 Cgroup v2 deep dive | 100m request -> weight 17; 100m limit -> `10000 100000`; `cpu.max.burst` 0 on kernel 7.3.0-k8labs |
| 7.6 Kernel scheduler comparison | `scx_lavd` ignores `cpu.max` (12.22 CPU-s against a 4.0 CPU-s quota); `--enable-cpu-bw` restores it (4.01 CPU-s) |
| 7.7 Incident simulation | 3000 req/s spike served 1453 rps at p99 102 ms by a 120m pod; one of three replicas lost with no client errors, 42 s to replace |
| 7.8 Cost / TCO model | Upper bound ~$2,755/month wasted capacity per throttled scenario; no revenue input supplied |

**Status detail**: [`PHASE7_COMPLETE.md`](PHASE7_COMPLETE.md) ·
[`phase7/STATUS.md`](phase7/STATUS.md)

**To run all Phase 7 experiments** (50-60 minutes, needs a cluster and the
`7.3.0-k8labs` node):
```bash
cd research/cpu-limits/phase7
PHASE7_NODE=<node name> ./run-all-phase7.sh
```

---

## Documentation

### Guides

| Document | Audience | Key Content |
|----------|----------|-------------|
| [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) | Developers | Decision tree, workload patterns, measured tables, YAML examples, troubleshooting |
| [ADMINISTRATOR_GUIDE.md](ADMINISTRATOR_GUIDE.md) | Cluster ops | Cgroup v2 parameters, BPF tracing, Prometheus, capacity planning, kernel/scheduler guidance |
| [REAL_WORLD_PLAYBOOK.md](REAL_WORLD_PLAYBOOK.md) | DevOps/SRE | Incident response, production scenarios, cost optimization |
| [PROJECT_COMPLETE.md](PROJECT_COMPLETE.md) | Overview | Research summary, all findings, deliverables |
| [PHASE7_SUMMARY.md](PHASE7_SUMMARY.md) | Phase 7 details | Short per-topic summary with headline numbers |
| [PHASE7_COMPLETE.md](PHASE7_COMPLETE.md) | Phase 7 details | Full per-topic detail, caveats, what was not measured |
| [phase7/STATUS.md](phase7/STATUS.md) | Researchers | Topic status, evidence paths, known limits |

### Technical References

- **Prometheus Integration**: `evidence/prometheus-integration/PROMETHEUS_GUIDE.md`
- **Cgroup V2 Parameters**: Generated by `phase7/7.5-cgroup-deep-dive/analyze-cgroup-v2.py`
- **BPF Tracing**: `phase7/7.4-live-bpf/scheduler-trace.bt` (annotated script)
- **Phase 4 Summary**: `phase4/THROTTLING_SUMMARY.md`
- **Phase 5 Patterns**: `phase5/PRODUCTION_PATTERNS.md`

---

## Evidence & Reproducibility

### Evidence Files

All experiments produce **JSON evidence files** with:
- Test configuration (requests, limits, workload type)
- Performance metrics (throughput, latency)
- Cgroup stats (nr_periods, nr_throttled, throttled_usec)
- Timestamp and duration

**Total evidence files**: 32 JSON files across phases 1-7 (`find . -name '*.json' -path '*evidence*' | wc -l`)

### Directory Structure

```
research/cpu-limits/
├── evidence/
│   ├── phase7/
│   │   ├── real-apps/
│   │   │   └── nginx-throttling-20261026T191420Z.json
│   │   ├── qos-comparison/
│   │   │   └── qos-classes-20261026T191440Z.json
│   │   ├── interference/
│   │   │   └── noisy-neighbor-20261026T191852Z.json
│   │   ├── live-bpf/
│   │   │   └── scheduler-trace-20261026T192056Z.json
│   │   ├── cgroup-v2/
│   │   │   └── cgroup-v2-analysis-20261026T190908Z.json
│   │   └── cost-analysis/
│   │       └── tco-model-20261026T190918Z.json
│   ├── bpf-correlation-analysis.json
│   └── prometheus-integration/
│       ├── promql-queries.json
│       ├── grafana-dashboard.json
│       ├── prometheus-alerts.yaml
│       └── PROMETHEUS_GUIDE.md
├── phase1/evidence/      environment and cgroup validation
├── phase2/evidence/      per-workload baselines + summary.json
├── phase4/evidence/
│   ├── cpu-bound-no-throttle-*.json
│   ├── cpu-bound-light-throttle-*.json
│   ├── cpu-bound-moderate-throttle-*.json
│   ├── cpu-bound-heavy-throttle-*.json
│   └── throttling-summary.json
└── phase5/evidence/      latency-impact-*.json
```

### Reproducibility

**Prerequisites**:
- Kubernetes cluster with cgroup v2
- `7.3.0-k8labs` is what the measurements used; other kernels will change absolute numbers
- Python 3.9+
- kubectl
- For 7.4: a container image containing bpftrace (the collector pod used `bpftrace` 0.24.2)
- For monitoring: Prometheus with cAdvisor

**Run all experiments**:
```bash
cd research/cpu-limits

# Phase 4 (CPU-bound throttling)
cd phase4/experiments && python3 run-throttling-experiments.py

# Phase 6 (BPF correlation analysis over Phase 4 data)
cd ../../phase6/6.2-bpf-correlation && python3 analyze-scheduler-correlation.py

# Phase 7 (all six measured topics, against a cluster)
cd ../../phase7 && PHASE7_NODE=<node> ./run-all-phase7.sh
```

**Estimated runtime**: Phase 4 about 8 minutes; Phase 7 20-30 minutes.

---

## How to Use This Research

### Scenario 1: Setting CPU Limits for New Application

1. Read [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) (15 min)
2. Deploy app without limits, monitor for 1 week:
   ```bash
   kubectl top pod <your-pod> --containers
   ```
3. Identify workload type (CPU-bound, IO-bound, hybrid)
4. Follow the decision tree:
   - **Latency-sensitive**: requests only, no CPU limit
   - **Batch**: limits = requests, or no limit
   - **Hybrid**: requests only, or a limit well above p99 usage
5. Monitor throttling:
   ```promql
   rate(container_cpu_cfs_throttled_seconds_total{pod="<your-pod>"}[5m])
   ```

### Scenario 2: Troubleshooting High Latency

1. Check if CPU throttling is the cause:
   ```bash
   kubectl exec <pod> -- cat /sys/fs/cgroup/cpu.stat
   ```
2. If `nr_throttled` is high and `kubectl top node` shows idle CPU:
   - **Quick fix**: Remove `limits.cpu` from the pod spec
   - **Then**: re-measure p99 (Phase 7.1 saw 69.43 ms -> 13.98 ms)
3. Monitor for 24 hours, iterate if needed

See [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) § Troubleshooting

### Scenario 3: Cluster-Wide Audit

1. Deploy Prometheus + Grafana (if not present)
2. Import dashboard: `evidence/prometheus-integration/grafana-dashboard.json`
3. Apply alerts: `kubectl apply -f evidence/prometheus-integration/prometheus-alerts.yaml`
4. Query for throttled pods:
   ```promql
   topk(20, 
     rate(container_cpu_cfs_throttled_seconds_total[5m])
   )
   ```
5. For each pod with high throttling:
   - Check whether the service is latency-sensitive (does it have an SLO?)
   - If yes: remove the limit and re-measure p99 before acting further
   - If no (batch): set the limit equal to the request, or remove it
6. Re-audit after 1 week

See [`ADMINISTRATOR_GUIDE.md`](ADMINISTRATOR_GUIDE.md) § Troubleshooting

### Scenario 4: Academic Research / Extension

1. Review [`PROJECT_COMPLETE.md`](PROJECT_COMPLETE.md) for full methodology
2. Examine evidence files in `evidence/` for data format
3. Run experiments in your environment:
   ```bash
   cd research/cpu-limits/phase4/experiments
   python3 run-throttling-experiments.py
   ```
4. Extend with your own workloads:
   - Add to `phase1/generators/`
   - Follow naming: `<type>-bound-workload.sh`
   - Generate evidence in `evidence/phase7/` or `evidence/custom/`

**Citation**: If you use this research, please cite the repository and reference key findings from `PROJECT_COMPLETE.md`.

---

## Contributing

We welcome contributions!

### Areas for Extension

- **More real-world apps**: Add PostgreSQL, Redis, RabbitMQ, Kafka tests (Phase 7)
- **Additional workload types**: Network-bound, GPU-bound
- **Custom schedulers**: sched_ext integration and evaluation
- **Cloud provider specifics**: GKE, EKS, AKS throttling behavior differences
- **Cost modeling**: TCO analysis based on throttling data (7.8 exists; it needs baseline rps and revenue per request to produce a business figure)

### How to Contribute

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/new-workload`)
3. Add your experiments under `phase7/` or `evidence/custom/`
4. Follow existing evidence file format (JSON with metadata)
5. Document your findings in a new guide or extend existing
6. Submit a pull request

### Code Style

- **Python**: PEP 8, type hints encouraged
- **Bash**: ShellCheck compliant
- **YAML**: Kubernetes standard formatting
- **Documentation**: Markdown with clear structure

---

## Related Research & References

This project builds on and validates findings from:

- [Robusta: Stop Using CPU Limits](https://home.robusta.dev/blog/stop-using-cpu-limits) (2023)
- [Indeed Engineering: Unthrottled](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/) (2019)
- [Kubernetes Blog: The Case for Resource Limits](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/) (2023)
- [Sysdig: CPU Requests and Limits](https://www.sysdig.com/blog/kubernetes-cpu-requests-limits-autoscaling) (2022)

**Our contribution**: Empirical evidence with reproducible experiments, BPF-level analysis, and comprehensive operational guides.

### Technical Documentation

- [Linux CFS Bandwidth Control](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)
- [Kubernetes Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Cgroup V2 Documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
- [BPF/bpftrace Guide](https://github.com/iovisor/bpftrace)
- [sched_ext Project](https://github.com/sched-ext/scx)

---

## License

**Research artifacts**: CC-BY-4.0 (attribution required for publication)  
**Code**: MIT License (use freely, attribution appreciated)

See [LICENSE](../../LICENSE) file for details.

---

## Contact

Part of the [k8labs](https://github.com/your-org/k8labs) project.

**Issues**: Use GitHub Issues for questions, bug reports, or feature requests  
**Discussions**: GitHub Discussions for research questions and collaboration  

---

**Status**: Phases 1-6 complete; Phase 7 complete | Last updated: 2026-10-27

[![Research Complete](https://img.shields.io/badge/phases%201--6-complete-brightgreen)]()
[![Phase 7](https://img.shields.io/badge/phase%207-6%20of%208%20measured-blue)]()
