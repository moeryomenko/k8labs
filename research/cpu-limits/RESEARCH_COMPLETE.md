# Kubernetes CPU Requests/Limits Research: Complete Analysis

**Research Objective:** Provide comprehensive analysis of how Kubernetes CPU requests and limits affect cgroup params and scheduler behavior on running containers.

**Target Audiences:**
- **Developers:** How to set up Pod/container requests and limits
- **Kubernetes Administrators:** How to configure kernel scheduling and when to use custom schedulers

**Date:** October 26, 2026  
**Status:** ✅ Complete

---

## Research Overview

This research demonstrates the complete lifecycle of CPU resource management in Kubernetes through five progressive phases:

1. **Phase 1:** Basic workload simulation and metrics collection
2. **Phase 2:** QoS classes and multi-workload comparison  
3. **Phase 3:** Low-level BPF tracing of kernel scheduler behavior
4. **Phase 4:** CPU throttling analysis with controlled experiments
5. **Phase 5:** Scheduler deep dive, latency testing, and production readiness

---

## Phase Summaries

### Phase 1: Foundations ✅

**Objective:** Establish baseline workload patterns and measurement infrastructure

**Workloads Created:**
- CPU-bound (stress-ng cpu)
- IO-bound (stress-ng hdd)
- Memory-bound (stress-ng vm)
- Hybrid (cpu + memory + io)

**Evidence Collected:**
- cgroup CPU statistics (`cpu.stat`)
- Container performance metrics
- Node-level CPU usage
- Workload execution logs

**Key Deliverables:**
- `research/cpu-limits/phase1/workloads/` - 4 workload manifests
- `research/cpu-limits/phase1/evidence/` - Baseline experiment data
- `research/cpu-limits/phase1/README.md` - Phase documentation

**Status:** ✅ Complete, baseline established

---

### Phase 2: QoS Classes & Performance ✅

**Objective:** Compare Kubernetes QoS classes and their impact on scheduling and performance

**QoS Classes Tested:**
- **BestEffort:** No requests, no limits
- **Burstable:** Requests set, limits higher or absent
- **Guaranteed:** Requests = Limits

**Experiments Run:** 6 configurations across all workload types

**Key Findings:**
- **BestEffort:** fastest short runs (0.3 s, 1.4 s) with no quota written;
  throughput 98.8 and 98.1 ops/s are short-sample figures only
- **Burstable:** 51.41 ops/s with `cpu.max` = `200000 100000`, and every period
  in the run was throttled (1150/1150)
- **Guaranteed:** 51.65 ops/s for cpu-bound with a 2-CPU quota
- **Throttling:** 9 of the 14 runs have no end `cpu.stat` snapshot, so their
  throttling is **unknown**, not zero. In the 5 runs that do have one,
  throttling was near-total (99.1%, 99.8%, 100.0%) wherever a finite quota
  existed, and absent only in the besteffort run with `cpu.max` = `max 100000`

**Evidence:**
```
research/cpu-limits/phase2/evidence/
├── cpu-bound-besteffort-*.json      (cpu.max "max 100000", 0/0 periods, 73.18 ops/s)
├── cpu-bound-burstable-*.json       (1150/1150 periods throttled, 51.41 ops/s)
├── cpu-bound-guaranteed-*.json      (no end snapshot, 51.65 ops/s)
├── hybrid-burstable-*.json          (1186/1186 periods throttled)
├── io-bound-guaranteed-*.json       (no end snapshot)
├── memory-bound-guaranteed-*.json   (1170/1172 periods throttled)
└── summary.json
```

**Key Deliverables:**
- `research/cpu-limits/GUIDE.md` - Comprehensive developer guide
- `research/cpu-limits/phase2/evidence/` - 6 experiment results
- `research/cpu-limits/phase2/README.md` - Phase documentation

**Status:** ✅ Complete, developer guide published

---

### Phase 3: BPF Scheduler Tracing ✅

**Objective:** Instrument kernel scheduler to observe low-level scheduling decisions

**Approach:** Guest-side BPF tracing via systemd service

**BPF Programs Deployed:**
- `sched:sched_switch` - Context switches
- `sched:sched_wakeup` - Thread wakeups
- `sched:sched_process_exec` - Process starts
- `cgroup` attachment for CPU accounting

**Implementation:**
```
research/cpu-limits/phase3/
├── bpf-tracer-daemon.py          (BPF trace collector)
├── bpf-tracer.service            (systemd service)
├── deploy-bpf-tracer.sh          (deployment script)
└── README.md                     (setup instructions)
```

**Deployment Method:**
```bash
# Deploy via SSH to cluster nodes
for node in $(kubectl get nodes -o name); do
  NODE_IP=$(kubectl get $node -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
  ssh root@$NODE_IP "bash -s" < phase3/deploy-bpf-tracer.sh
done
```

**Key Deliverables:**
- Systemd service for continuous BPF tracing
- Integration with Phase 2/4 experiment runners
- Guest-side trace collection (avoids host->VM limitations)

**Status:** ✅ Complete, BPF tracer deployed and operational

---

### Phase 4: CPU Throttling Analysis ✅

**Objective:** Demonstrate impact of CPU throttling on application performance

**Experiment Design:** 4 configurations with identical workload (4 CPU workers), varying limits

| Configuration | Request / Limit | Measured Throughput |
|---------------|-----------------|---------------------|
| Heavy Throttle | 100m / 500m | 19.22 ops/s |
| Moderate Throttle | 500m / 1500m | 53.57 ops/s |
| Light Throttle | 1000m / 3000m | 108.78 ops/s |
| No Throttle | 2000m / 4000m | 139.05 ops/s |

**Results:**

| Configuration | Request | Limit | Performance | vs no-limit |
|---------------|---------|-------|-------------|-------------|
| No Throttle | 2000m | 4000m | 139.05 ops/s | baseline |
| Light Throttle | 1000m | 3000m | 108.78 ops/s | -21.8% |
| Moderate Throttle | 500m | 1500m | 53.57 ops/s | -61.5% |
| Heavy Throttle | 100m | 500m | 19.22 ops/s | **-86.2%** |

Source: `phase4/evidence/throttling-summary.json`.

**Key Insights:**
1. **Throughput loss tracks the CPU cut, but not linearly**
   - The heavy config receives 0.5 core where the 2000m/4000m reference run
     gets up to 4 — an 8x quota reduction — and loses 7.2x throughput
     (139.05 → 19.22 ops/s)
   - The residual is time stopped at the quota boundary with nothing to run

2. **Throttle percentages are not available for these runs**
   - The evidence files captured `nr_periods`/`nr_throttled` in the baseline
     snapshot only; `final_cgroup` and `throttling` are null
   - `throttled_usec / (usage_usec + throttled_usec)` cannot be computed from
     the recorded data, so cite throughput instead
   - Phase 7.1 and 7.4 are where throttle percentages and their latency cost
     were measured

**Evidence:**
```
research/cpu-limits/phase4/evidence/
├── cpu-bound-heavy-throttle-*.json     (19.22 ops/s)
├── cpu-bound-moderate-throttle-*.json  (53.57 ops/s)
├── cpu-bound-light-throttle-*.json     (108.78 ops/s)
├── cpu-bound-no-throttle-*.json        (139.05 ops/s)
└── throttling-summary.json
```

**Key Deliverables:**
- `research/cpu-limits/phase4/THROTTLING_ANALYSIS.md` - Complete throttling analysis
- `research/cpu-limits/phase4/README.md` - Phase documentation
- `research/cpu-limits/phase4/monitoring/` - Prometheus + Grafana configs
- `research/cpu-limits/phase4/evidence/` - 4 throttling experiments

**Status:** ✅ Complete, throttling behavior documented

---

### Phase 5: Scheduler Deep Dive & Production Readiness

**Objective:** Complete the research with kernel-level scheduler analysis and administrator guidance

**Status:** Complete - administrator guides delivered; the BPF and latency
measurements were moved to Phase 6 and Phase 7 and run there.

**Experiments:**

1. **Task 5.1: BPF Trace Collection** - delivered as Phase 7.4
   - Live bpftrace of `sched:sched_wakeup` and `sched:sched_switch` inside an
     unprivileged reader pod, no node SSH required
   - Measured worst wake-to-run wait: 5.5 ms with no limit, 71.7 ms at a 100m limit
   - Evidence: `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`

2. **Task 5.2: Latency-Sensitive Workload** - attempted here, delivered as Phase 7.1
   - The Phase 5 run (`phase5/evidence/latency-impact-20261026T163603Z.json`)
     covered only two configurations and recorded no throttling
     (`nr_throttled` 0); its negative `usage_usec` values show the counter
     delta was read incorrectly, so it is superseded
   - Phase 7.1 measured nginx at a fixed offered rate: p99 13.98 ms with no
     limit rising to 69.43 ms at 120m, while p50 stayed at 2.04 to 2.13 ms

3. **Task 5.3: Kernel Scheduler Parameters** - NOT MEASURED
   - The sysctl tuning material in `phase5/admin-guide/KERNEL_CONFIG.md` and in
     `ADMINISTRATOR_GUIDE.md` is documented guidance, not a result
   - Phase 6 and Phase 7 found the tail latency came from the CPU quota, not
     from scheduler timeslices, so these parameters are not the first thing to
     change

4. **Task 5.4: Custom Scheduler (scx) Comparison** - RUN IN PHASE 7.6
   - `CUSTOM_SCHEDULERS.md` describes the options; Phase 7.6 measured
     `scx_lavd --performance` against CFS on the `7.3.0-k8labs` node. The
     scheduler attached (`state=enabled`, `nr_rejected` 0) and the result was
     not the expected one: `cpu.max` was not enforced at all (12.22 CPU-seconds
     against a 4.0 CPU-second quota) until `--enable-cpu-bw` was added, and
     cgroup weights were not honoured. See `PHASE7_COMPLETE.md` section 7.6

**Administrator Guides:**

- **KERNEL_CONFIG.md** - CFS scheduler documentation, UP/SMP, CPU isolation
- **CUSTOM_SCHEDULERS.md** - When to consider scx (sched-ext), installation, migration strategy
- **PRODUCTION_PATTERNS.md** - Deployment patterns. Its 69.8% and 4.7% throttle
  figures are not present in the Phase 4 evidence, which records throughput;
  see `phase4/THROTTLING_ANALYSIS.md` and `PHASE7_COMPLETE.md` for measured numbers

**Key Deliverables:**
- `research/cpu-limits/phase5/admin-guide/` - 3 administrator guides
- `research/cpu-limits/phase5/evidence/latency-impact-20261026T163603Z.json` -
  the superseded latency attempt
- Superseding deliverables: `ADMINISTRATOR_GUIDE.md`, `PHASE7_COMPLETE.md`,
  `evidence/phase7/`

**Status:** Complete. Scheduler parameters are covered in Phase 6 and Phase 7;
sched_ext was measured in Phase 7.6

---

## Monitoring & Observability

### Prometheus Metrics

Deployed via `kube-prometheus-stack` in the `monitoring` namespace:

**Key Metrics:**
```promql
# Throttling rate
rate(container_cpu_cfs_throttled_seconds_total[5m])

# Throttling percentage
rate(container_cpu_cfs_throttled_seconds_total[5m]) /
rate(container_cpu_cfs_periods_total[5m]) * 100

# Top 10 most throttled containers
topk(10, 
  rate(container_cpu_cfs_throttled_seconds_total[5m]) /
  rate(container_cpu_cfs_periods_total[5m]) * 100
)
```

**Deployment:**
```bash
cd research/cpu-limits/phase4
./deploy-prometheus.sh
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

### Grafana Dashboards

**Throttling Dashboard** (`phase4/monitoring/grafana-dashboard-throttling.json`):
- Throttling percentage by container
- Throttling events count
- CPU usage vs limits
- Performance correlation

**Import:**
1. Open http://localhost:3000
2. Login: admin/prom-operator
3. Import JSON: `monitoring/grafana-dashboard-throttling.json`

### Alert Rules

**Prometheus Alerts** (`phase4/monitoring/prometheus-alerts.yaml`):
- `HighCPUThrottling` - Throttling > 10% for 5m
- `SevereCPUThrottling` - Throttling > 50% for 5m
- `CPULimitExceeded` - Usage > 90% of limit for 5m

---

## Complete File Structure

```
research/cpu-limits/
├── README.md                          # Main research documentation
├── GUIDE.md                           # Developer guide (Phase 2)
├── RESEARCH_COMPLETE.md              # This file - complete summary
├── research-dashboard.html            # Visual summary
│
├── phase1/                            # Foundation
│   ├── README.md
│   ├── workloads/
│   │   ├── cpu-bound.yaml
│   │   ├── io-bound.yaml
│   │   ├── memory-bound.yaml
│   │   └── hybrid.yaml
│   └── evidence/
│       └── (baseline experiment data)
│
├── phase2/                            # QoS Classes
│   ├── README.md
│   ├── workloads/
│   │   ├── cpu-bound-besteffort.yaml
│   │   ├── cpu-bound-burstable.yaml
│   │   ├── cpu-bound-guaranteed.yaml
│   │   ├── hybrid-burstable.yaml
│   │   ├── io-bound-guaranteed.yaml
│   │   └── memory-bound-guaranteed.yaml
│   ├── run-experiments.py
│   └── evidence/
│       ├── (6 experiment results)
│       └── summary.json
│
├── phase3/                            # BPF Tracing
│   ├── README.md
│   ├── bpf-tracer-daemon.py
│   ├── bpf-tracer.service
│   └── deploy-bpf-tracer.sh
│
└── phase4/                            # Throttling Analysis
    ├── README.md
    ├── THROTTLING_ANALYSIS.md        # Complete analysis
    ├── run-throttling-experiments.py
    ├── analyze-throttling-fixed.py
    ├── workloads/
    │   ├── cpu-bound-heavy-throttle.yaml
    │   ├── cpu-bound-moderate-throttle.yaml
    │   ├── cpu-bound-light-throttle.yaml
    │   └── cpu-bound-no-throttle.yaml
    ├── evidence/
    │   ├── (4 throttling experiments)
    │   └── throttling-summary.json
    └── monitoring/
        ├── prometheus-values.yaml
        ├── prometheus-alerts.yaml
        ├── grafana-dashboard-throttling.json
        └── deploy-prometheus.sh
```

---

## Key Research Findings

### For Developers

1. **Understanding CPU Limits**
   - Limits define maximum CPU time per 100ms period
   - Exceeding limits triggers throttling (process suspension)
   - Throttling directly impacts application performance

2. **Setting Requests and Limits**
   ```yaml
   resources:
     requests:
       cpu: "500m"      # Scheduling guarantee
     limits:
       cpu: "1000m"     # Hard cap, triggers throttling if exceeded
   ```

   **Best Practices:**
   - Measure P95/P99 CPU usage in production
   - Set limit = P99 + 20% headroom
   - Or use request-only (no limit) for burst capacity
   - Monitor throttling: alert if > 5%

3. **QoS Class Selection**
   - **Guaranteed:** Requests = Limits (predictable, protected)
   - **Burstable:** Requests < Limits (flexible, can burst)
   - **BestEffort:** No requests/limits (opportunistic, risky)

4. **Avoiding Throttling**
   - Size limits appropriately (see Phase 4 results)
   - Monitor `container_cpu_cfs_throttled_seconds_total`
   - Test under load before production
   - Consider omitting limits for burst workloads

### For Administrators

1. **Kernel Scheduler Configuration**
   - Default: CFS (Completely Fair Scheduler)
   - cgroup CPU controller enforces limits
   - No tuning needed for most workloads

2. **Advanced Scheduling**
   - **CPU pinning:** Use `cpuset` for latency-sensitive apps
   - **CPU isolation:** Boot with `isolcpus=` for dedicated cores
   - **Custom schedulers:** Consider `scx` (sched-ext) for kernel 6.12+

3. **Cluster-Wide Monitoring**
   ```promql
   # Containers with throttling > 10%
   (
     rate(container_cpu_cfs_throttled_seconds_total[5m]) /
     rate(container_cpu_cfs_periods_total[5m]) * 100
   ) > 10
   ```

4. **Resource Quotas**
   - Namespace quotas may force aggressive limits
   - Balance cost control with performance
   - Provide guidance on appropriate limits

---

## Related Research & Industry Perspectives

### Arguments Against CPU Limits

1. **[Stop Using CPU Limits (Robusta)](https://home.robusta.dev/blog/stop-using-cpu-limits)**
   - CPU limits cause unpredictable throttling
   - Recommendation: Use requests only, no limits

2. **[Unthrottled: Fixing CPU Limits (Indeed)](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)**
   - Indeed removed CPU limits from most services
   - Improved performance and reduced incidents

3. **[Kubernetes Faster Services with No CPU Limits](https://erickhun.com/posts/kubernetes-faster-services-no-cpu-limits/)**
   - Removed limits, services got faster
   - No negative impact on cluster stability

### Arguments For CPU Limits

1. **[Why You Should Keep Using CPU Limits (Medium)](https://dnastacio.medium.com/why-you-should-keep-using-cpu-limits-on-kubernetes-60c4e50dfc61)**
   - Limits prevent noisy neighbor problems
   - Cost control in multi-tenant clusters

2. **[The Case for Kubernetes Resource Limits](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/)**
   - Official Kubernetes blog post
   - Limits enable better capacity planning

3. **[Kubernetes Performance (CockroachDB)](https://docs.cockroachlabs.com/docs/stable/kubernetes-performance#resource-requests-and-limits)**
   - Database workloads need careful limit tuning
   - Set limits = requests for predictability

### Our Research Position

**This research demonstrates both sides empirically:**

- **Phase 2:** a finite quota throttled 99.1-100.0% of periods in the runs that
  have an end snapshot; only the besteffort run (`cpu.max` = `max 100000`)
- **Phase 4:** an 8x quota cut cost 7.2x throughput (139.05 to 19.22 ops/sec)
- **Phase 7.1:** A limit below usage raised p99 5x while the median stayed flat
- **Phase 7.3:** Capping the noisy neighbours helped the co-located victim

**Recommendation:** Context-dependent
- **Latency-sensitive:** Request-only (no limits)
- **CPU-bound batch:** limit = request, so no forced wait
- **Multi-tenant:** Limits on the offenders, for noisy neighbour control
- **Always:** Monitor throttling **and** the service's own p99

---

## Next Steps & Extensions

### Completed ✅
- [x] Phase 1: Baseline workloads
- [x] Phase 2: QoS comparison
- [x] Phase 3: BPF tracing infrastructure
- [x] Phase 4: Throttling analysis
- [x] Prometheus + Grafana monitoring
- [x] Developer guide
- [x] Administrator documentation

### Future Enhancements 🚀

1. **Memory Limits Research**
   - OOMKilled behavior
   - Memory throttling (swap)
   - Memory QoS classes

2. **Custom Scheduler Analysis (scx)**
   - Requires kernel 6.12+
   - Compare CFS vs scx_simple vs scx_lavd
   - Scheduler BPF program analysis

3. **Multi-Node Experiments**
   - Cross-node scheduler decisions
   - Node affinity impact
   - Topology-aware scheduling

4. **Real Application Benchmarks**
   - Nginx + wrk load testing
   - PostgreSQL + pgbench
   - Redis + redis-benchmark

5. **Autoscaling Integration**
   - VPA (Vertical Pod Autoscaler) recommendations
   - HPA (Horizontal Pod Autoscaler) behavior
   - Throttling impact on scaling decisions

---

## Usage Instructions

### Run Complete Research Pipeline

```bash
# Phase 1: Baseline
cd research/cpu-limits/phase1
python3 run-experiments.py

# Phase 2: QoS Classes
cd ../phase2
python3 run-experiments.py

# Phase 3: Deploy BPF Tracer
cd ../phase3
./deploy-bpf-tracer.sh

# Phase 4: Throttling Analysis
cd ../phase4
python3 run-throttling-experiments.py
python3 analyze-throttling-fixed.py

# Deploy Monitoring
./deploy-prometheus.sh
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

### View Results

```bash
# Phase 2 Developer Guide
cat research/cpu-limits/GUIDE.md

# Phase 4 Throttling Analysis
cat research/cpu-limits/phase4/THROTTLING_ANALYSIS.md

# Visual Dashboard
open research/cpu-limits/research-dashboard.html

# Experiment Evidence
ls research/cpu-limits/phase*/evidence/
```

---

## Conclusion

This research provides empirical evidence for how Kubernetes CPU requests and limits affect real-world container performance. The key takeaway:

**CPU limits are a powerful tool, but they must be sized correctly.**

- ✅ **A request does not stop throttling** (Phase 2): a 2-CPU quota on 4 workers
  throttled 100% of periods; the request only sets `cpu.weight`
- ⚠️ **A quota below need** (Phase 4): 86.2% throughput loss going from
  2000m/4000m to 100m/500m
- ❌ **A limit below peak on a latency-sensitive service** (Phase 7.1): p99 5x
  worse while the median stays flat

**Always monitor throttling in production, and judge it with the service's own
p99 latency rather than with the throttle percentage alone.**

---

**Research Team:** k8labs  
**Date:** October 26, 2026  
**Repository:** `/research/cpu-limits/`  
**Status:** Phases 1-6 complete; Phase 7 complete, with seven topics measured on
a cluster and 7.8 computed from Phase 4 data
