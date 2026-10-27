# Phase 4: Extended CPU Limits Research

**Status:** Complete - 4 throttling runs recorded in `phase4/evidence/`. BPF
tracing was delivered later as Phase 7.4 and the scheduler comparison as Phase
7.6, both run.

**Focus:** Throttling analysis, Prometheus monitoring, BPF tracing, scheduler comparison

---

## Objectives

Extend Phase 1-2 research to include:

1. **Intentional CPU throttling** - Demonstrate throttling impact with aggressive limits
2. **Prometheus metrics** - Real-time monitoring and alerting
3. **BPF scheduler traces** - Low-level kernel scheduling visibility
4. **Scheduler comparison** - CFS vs scx custom schedulers

---

## Phase 4 Components

### 4a: Throttling Experiments ✅ (Running)

**Goal:** Trigger CPU throttling to demonstrate performance impact

**Configurations (all four measured):**
| Name | Request | Limit | Measured Throughput |
|------|---------|-------|---------------------|
| cpu-bound-heavy-throttle | 100m | 500m | 19.22 ops/s |
| cpu-bound-moderate-throttle | 500m | 1500m | 53.57 ops/s |
| cpu-bound-light-throttle | 1000m | 3000m | 108.78 ops/s |
| cpu-bound-no-throttle | 2000m | 4000m | 139.05 ops/s |

**What's collected:**
- `nr_throttled` - count of throttling events
- `throttled_usec` - total time throttled
- Throughput from the stress-ng summary
- The evidence files have no end-of-run `cpu.stat` snapshot
  (`final_cgroup: null`), so no throttle percentage is reported for these runs

**Run experiments:**
```bash
cd phase4
python3 run-throttling-experiments.py

# Analyze results
python3 analyze-throttling.py

# View report
cat THROTTLING_ANALYSIS.md
```

---

### 4b: Prometheus Monitoring ⏸️ (Ready to deploy)

**Goal:** Deploy production-grade monitoring stack

**Components:**
- prometheus-operator
- kube-state-metrics (CPU request/limit metadata)
- node-exporter (node CPU stats)
- Grafana (dashboards)

**Key metrics:**
```promql
# CPU throttling rate
rate(container_cpu_cfs_throttled_seconds_total[5m])

# Throttling percentage
rate(container_cpu_cfs_throttled_seconds_total[5m]) /
  rate(container_cpu_cfs_periods_total[5m]) * 100

# CPU usage vs request
rate(container_cpu_usage_seconds_total[5m]) /
  kube_pod_container_resource_requests{resource="cpu"}

# CPU usage vs limit
rate(container_cpu_usage_seconds_total[5m]) /
  kube_pod_container_resource_limits{resource="cpu"}
```

**Deploy:**
```bash
cd phase4
./deploy-prometheus.sh

# Access Grafana
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80

# Import dashboard
# Open http://localhost:3000
# Import monitoring/grafana-dashboard-throttling.json
```

**Alert rules:** See `monitoring/cpu-alerts.yaml`

---

### 4c: BPF Scheduler Traces ⏸️ (Node access required)

**Goal:** Capture low-level kernel scheduler behavior during throttling

**What to trace:**
- **runqlat** - Scheduler latency (time tasks wait in run queue)
- **runqlen** - Run queue length (scheduling contention)
- **sched_switch** - Context switches between tasks

**Prerequisites:**
- SSH access to Kubernetes nodes
- bpftrace installed on nodes
- Linux kernel with BPF support

**Deploy:**
```bash
# SSH into each node
ssh root@<node-ip>

# Install BPF tools
sudo apt install -y bpftrace bcc-tools linux-tools-generic

# Copy systemd service
sudo cp phase3/bpf-tracer.service /etc/systemd/system/
sudo cp phase3/bpf-tracer-daemon.py /usr/local/bin/
sudo chmod +x /usr/local/bin/bpf-tracer-daemon.py

# Start service
sudo systemctl daemon-reload
sudo systemctl enable bpf-tracer
sudo systemctl start bpf-tracer

# Check status
sudo systemctl status bpf-tracer
sudo journalctl -u bpf-tracer -f
```

**Trigger tracing:**
```bash
# Annotate pod to enable tracing
kubectl annotate pod <pod-name> bpf-trace=enabled

# View traces
kubectl exec <pod-name> -- cat /var/log/bpf-traces/*.txt
```

**Analysis:**
```bash
python3 phase3/analyze-bpf.py
```

---

### 4d: Kernel Scheduler Comparison ⏸️ (Kernel 6.12+ required)

**Goal:** Compare default CFS scheduler with scx custom schedulers

**Schedulers to test:**
- **CFS** (Completely Fair Scheduler) - Default Linux scheduler
- **scx_simple** - Simple FIFO, minimal overhead
- **scx_rusty** - NUMA-aware, load-balanced
- **scx_lavd** - Latency-aware for interactive workloads

**Prerequisites:**
```bash
# Check kernel version (need 6.12+)
uname -r

# Check scx support
zcat /proc/config.gz | grep CONFIG_SCHED_CLASS_EXT

# Install scx tools
git clone https://github.com/sched-ext/scx.git
cd scx
make
sudo make install
```

**Run comparison:**
```bash
# For each scheduler:

# 1. Start scheduler
sudo scx_rusty &

# 2. Run same workload
kubectl apply -f phase2/workloads/cpu-bound-burstable.yaml

# 3. Collect metrics
python3 run-throttling-experiments.py --scheduler=scx_rusty

# 4. Stop scheduler (returns to CFS)
sudo pkill scx_rusty
```

**Compare results:**
```bash
python3 analyze-scheduler-comparison.py
```

---

## Current Status

### ✅ Completed
- Phase 4 directory structure
- Throttling experiment scripts
- 4 throttling runs, results in `phase4/evidence/`
- Prometheus deployment scripts, Grafana dashboards, alert rules (configs only;
  no Prometheus was deployed, so there are no captured metric snapshots)
- Analysis tools

### Superseded
- BPF tracing: delivered as Phase 7.4 with bpftrace inside an unprivileged
  reader pod, so node SSH was not needed
  (`evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`)

### Run Later
- Scheduler comparison: Phase 7.6, on `7.3.0-k8labs`
  (`evidence/phase7/kernel-comparison/`)

### Corrections in this phase
The runs captured `nr_periods`/`nr_throttled` in a baseline snapshot only, with
`final_cgroup` and `throttling` null, so Phase 4 reports throughput, not throttle
percentages. See `THROTTLING_ANALYSIS.md` for the details and for the measured
throttle percentages from Phase 7.1 and 7.4.

### Deliverable status
`THROTTLING_ANALYSIS.md` reports throughput, 139.05 to 19.22 ops/sec. That is
what the evidence supports.

---

## Expected Deliverables

### 1. THROTTLING_ANALYSIS.md
Measured report showing:
- Throughput per request/limit configuration (no throttle percentage - the
  evidence does not contain a post-run cgroup snapshot)
- Why throughput loss can exceed the CPU cut
- Recommendations for sizing limits

### 2. Prometheus Dashboards
- Real-time throttling visualization
- CPU request/limit utilization
- Alert rule integration

### 3. BPF Trace Analysis (if node access available)
- Scheduler latency histograms
- Queue depth during throttling
- Context switch patterns

### 4. Scheduler Comparison Report (if kernel supports scx)
- Performance comparison table
- Throttling behavior under each scheduler
- Recommendations for workload types

### 5. Updated GUIDE.md
New sections:
- "Understanding CPU Throttling" (with real examples)
- "Prometheus Monitoring Setup"
- "BPF-Based Scheduler Analysis" (if available)
- "Kernel Scheduler Tuning"
- "When to Use Custom Schedulers"

---

## How Phase 4 Extends Phase 2

### Phase 2: Baseline (measured)
- CPU-bound with 2000m request, 2000m limit
- Performance: 51.5 ops/sec (`dispatching hogs: 4 cpu`)
- Throttling: 100.0% of periods in the one cpu-bound burstable run that has
  both snapshots (1150/1150, 29.8 s of throttled time)

### Phase 4a: Deliberate Shrinking Quota (measured)
- CPU-bound with 100m-4000m limits, 4 workers
- Performance: 139.05 / 108.78 / 53.57 / 19.22 ops/s as the quota shrinks
- Throttling: not captured; the evidence has no end `cpu.stat` snapshot
- **Demonstrates:** throughput loss from undersized limits (-86.2% at 100m/500m)

### Phase 4b: Real-time Monitoring
- Prometheus scrapes every pod
- Dashboards show throttling in real-time
- Alerts fire on excessive throttling
- **Demonstrates:** Production monitoring patterns

### Phase 4c: Low-level Scheduler View
- Delivered as Phase 7.4: bpftrace in an unprivileged reader pod
- Wake-to-run delay against a shrinking CPU limit
- **Demonstrates:** the tail grows from 5.5 ms to 71.7 ms

### Phase 4d: Scheduler Alternatives
- ✅ RUN AS PHASE 7.6 on `7.3.0-k8labs`
- CFS against `scx_lavd --performance`: the quota was not enforced under the
  custom scheduler (12.22 CPU-s against a 4.0 CPU-s quota) until
  `--enable-cpu-bw`; see `PHASE7_COMPLETE.md` section 7.6

---

## Prerequisites

### Required
- Kubernetes cluster (already available)
- kubectl access
- Python 3
- stress-ng container images

### Optional (for full research)
- Helm (for Prometheus deployment)
- SSH access to Kubernetes nodes (for BPF tracing)
- Linux 6.12+ with CONFIG_SCHED_CLASS_EXT (for scx comparison)

---

## Usage

### Quick Start (Throttling Only)
```bash
cd phase4
python3 run-throttling-experiments.py
python3 analyze-throttling.py
cat THROTTLING_ANALYSIS.md
```

### Full Stack (With Monitoring)
```bash
cd phase4

# Deploy Prometheus
./deploy-prometheus.sh

# Run experiments
python3 run-throttling-experiments.py

# Access Grafana
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80

# Import dashboard: monitoring/grafana-dashboard-throttling.json

# Analyze
python3 analyze-throttling.py
```

### With BPF Tracing (Node Access Required)
```bash
# 1. Deploy BPF tracer on each node
ssh root@<node-ip>
sudo cp phase3/bpf-tracer.service /etc/systemd/system/
sudo cp phase3/bpf-tracer-daemon.py /usr/local/bin/
sudo systemctl enable --now bpf-tracer

# 2. Run experiments with BPF annotation
kubectl apply -f phase4/workloads/cpu-bound-heavy-throttle.yaml
kubectl annotate pod cpu-bound-heavy-throttle bpf-trace=enabled

# 3. Collect and analyze traces
python3 phase3/analyze-bpf.py
```

---

## Troubleshooting

### Experiments fail to start
- Check cluster has sufficient capacity
- Verify stress-ng image is available
- Check pod events: `kubectl describe pod <pod-name>`

### No throttling observed
- The manifest probably has no CPU limit: check `cpu.max` in the container's
  cgroup; `max 100000` means unbounded, so `nr_throttled` stays 0
- Try a tighter limit (e.g., 100m for 4-worker stress-ng)
- Verify cgroup v2: `stat -f -c %T /sys/fs/cgroup`

### Prometheus not scraping pods
- Check ServiceMonitor: `kubectl get servicemonitor -n monitoring`
- Verify kube-state-metrics is running
- Check Prometheus targets: http://localhost:9090/targets

### BPF traces not collected
- Verify bpftrace is installed: `which bpftrace`
- Check systemd service: `systemctl status bpf-tracer`
- Verify pod annotation: `kubectl get pod <pod-name> -o yaml | grep annotations`

---

## Next Steps

1. **Complete throttling experiments** (~10 min remaining)
2. **Generate throttling analysis report**
3. **Deploy Prometheus stack** (if Helm available)
4. **Create correlation dashboard**
5. **Update main GUIDE.md** with Phase 4 findings

---

## Files

```
phase4/
├── README.md                           # This file
├── PLAN.md                             # Detailed implementation plan
├── THROTTLING_ANALYSIS.md              # Generated report
├── run-throttling-experiments.py       # Experiment runner
├── analyze-throttling.py               # Report generator
├── deploy-prometheus.sh                # Monitoring stack deployment
├── throttling-experiments.log          # Run log
├── workloads/                          # Pod manifests
│   ├── cpu-bound-heavy-throttle.yaml
│   ├── cpu-bound-moderate-throttle.yaml
│   ├── cpu-bound-light-throttle.yaml
│   └── cpu-bound-no-throttle.yaml
├── evidence/                           # Experiment results
│   ├── *.json
│   └── throttling-summary.json
└── monitoring/                         # Prometheus + Grafana
    ├── grafana-dashboard-throttling.json
    └── cpu-alerts.yaml
```

---

## References

### Phase Dependencies
- **Phase 1:** Workload manifests and QoS patterns
- **Phase 2:** QoS comparison runs (near-total throttling wherever a quota existed)
- **Phase 3:** BPF tracer infrastructure
- **Phase 4:** Extensions (this phase)

### External Resources
- [Kubernetes Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [cgroup v2 CPU Controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#cpu)
- [Prometheus Operator](https://prometheus-operator.dev/)
- [sched_ext (scx)](https://github.com/sched-ext/scx)
