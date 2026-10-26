# Phase 4: Extended Research Plan

**Objective:** Complete the research with BPF traces, Prometheus metrics, throttling analysis, and scheduler comparison.

---

## What's Missing from Current Research

### ❌ Not Yet Delivered

1. **BPF Scheduler Traces**
   - Low-level scheduler behavior (runqlat, runqlen, sched_switch)
   - Context switch analysis
   - Correlation with throttling events

2. **Prometheus Metrics**
   - Real-time monitoring setup
   - Grafana dashboards
   - Alert rules for throttling

3. **Intentional Throttling**
   - Current experiments showed NO throttling (limits were appropriate)
   - Need to demonstrate throttling impact by setting aggressive limits

4. **Kernel Scheduler Comparison**
   - CFS (default) baseline
   - scx custom schedulers (scx_simple, scx_rusty, scx_lavd)
   - Performance comparison across workload types

---

## Phase 4 Implementation Plan

### Step 1: Deploy BPF Tracer on Nodes ✅ (Infrastructure exists)

**Approach:** systemd service on Kubernetes nodes (guest VMs)

**What to do:**
```bash
# SSH into each Kubernetes node
ssh <node-ip>

# Install BPF tools
sudo apt install -y bpftrace bcc-tools linux-tools-generic

# Deploy systemd service
sudo cp phase3/bpf-tracer.service /etc/systemd/system/
sudo cp phase3/bpf-tracer-daemon.py /usr/local/bin/
sudo chmod +x /usr/local/bin/bpf-tracer-daemon.py

# Start service
sudo systemctl daemon-reload
sudo systemctl enable bpf-tracer
sudo systemctl start bpf-tracer
```

**Triggers:**
- Annotate pods with `bpf-trace=enabled`
- Daemon collects runqlat, runqlen, sched_switch
- Writes traces to `/var/log/bpf-traces/<pod-name>/`

---

### Step 2: Deploy Prometheus + Grafana

**Components:**
- prometheus-operator
- kube-state-metrics (CPU request/limit metadata)
- node-exporter (node CPU stats)
- cadvisor (container CPU stats)

**Manifests to create:**
```
phase4/monitoring/
├── namespace.yaml
├── prometheus-operator.yaml
├── prometheus.yaml
├── servicemonitor-pods.yaml
├── grafana.yaml
└── dashboards/
    ├── cpu-requests-limits.json
    └── cpu-throttling.json
```

**Key metrics:**
```promql
# CPU usage
rate(container_cpu_usage_seconds_total[5m])

# CPU throttling
rate(container_cpu_cfs_throttled_seconds_total[5m])

# CPU request utilization
rate(container_cpu_usage_seconds_total[5m]) / 
  kube_pod_container_resource_requests{resource="cpu"}

# CPU limit utilization
rate(container_cpu_usage_seconds_total[5m]) / 
  kube_pod_container_resource_limits{resource="cpu"}
```

---

### Step 3: Create Throttling Experiments

**Goal:** Intentionally trigger CPU throttling to demonstrate impact

**New workload manifests:**
```yaml
# cpu-bound-aggressive-limit.yaml
resources:
  requests:
    cpu: "100m"
  limits:
    cpu: "500m"  # Too low for 4-worker stress-ng
# Expected: Heavy throttling

# cpu-bound-moderate-limit.yaml
resources:
  requests:
    cpu: "500m"
  limits:
    cpu: "1500m"
# Expected: Some throttling under load

# latency-sensitive-limited.yaml
# HTTP server with CPU limit
# Expected: P99 latency spikes during throttling
```

**Test matrix:**
| Workload | Request | Limit | Expected Throttling |
|----------|---------|-------|---------------------|
| cpu-bound-4workers | 100m | 500m | Heavy (80%+ time) |
| cpu-bound-4workers | 500m | 1500m | Moderate (20-40% time) |
| cpu-bound-4workers | 1000m | 2000m | None (Phase 2 baseline) |
| http-server-wrk | 250m | 500m | Latency spikes in P99 |

**Metrics to collect:**
- `nr_throttled` - count
- `throttled_usec` - total time
- Throttling % = (throttled_usec / duration_usec) * 100
- Application performance degradation

---

### Step 4: Kernel Scheduler Comparison

**Goal:** Compare CFS (default) vs scx custom schedulers

**Prerequisites:**
- Linux 6.12+ with CONFIG_SCHED_CLASS_EXT
- scx tools installed

**Install scx:**
```bash
# On each node
git clone https://github.com/sched-ext/scx.git
cd scx
make
sudo make install
```

**Schedulers to test:**

1. **CFS (baseline)** - Default Completely Fair Scheduler
2. **scx_simple** - Simple FIFO scheduler
3. **scx_rusty** - NUMA-aware, load-balanced
4. **scx_lavd** - Latency-aware, optimized for interactive

**Test each scheduler with same workloads:**
```bash
# Switch scheduler
sudo scx_rusty &

# Run experiment
kubectl apply -f cpu-bound-burstable.yaml

# Collect metrics
# - Workload performance
# - cgroup throttling
# - BPF scheduler traces (runqlat, runqlen)
# - Prometheus metrics

# Stop scheduler (returns to CFS)
sudo pkill scx_rusty
```

**Comparison table:**
| Scheduler | CPU-bound | IO-bound | Memory-bound | Hybrid | Latency-sensitive |
|-----------|-----------|----------|--------------|--------|-------------------|
| CFS | baseline | baseline | baseline | baseline | baseline |
| scx_simple | ? | ? | ? | ? | ? |
| scx_rusty | ? | ? | ? | ? | ? |
| scx_lavd | ? | ? | ? | ? | ? |

---

### Step 5: Correlation Analysis

**Tool:** `phase4/analyze-correlation.py`

**Inputs:**
- cgroup CPU stats (Phase 2 evidence)
- BPF scheduler traces (Phase 4)
- Prometheus time-series data

**Analysis:**
1. **Throttling vs Scheduler Latency**
   - Does throttling increase runqlat?
   - How does runqlen grow during throttling?

2. **QoS Class vs Scheduler Fairness**
   - Are Guaranteed pods getting consistent CPU time?
   - Are BestEffort pods starved under load?

3. **Scheduler Comparison**
   - Which scheduler minimizes throttling impact?
   - Which scheduler is best for each workload type?

**Output:** Enhanced GUIDE.md with:
- BPF trace examples
- Prometheus query cookbook
- Scheduler tuning recommendations

---

## Phase 4 Deliverables

### 1. Extended Evidence
```
phase4/evidence/
├── throttling/
│   ├── cpu-bound-aggressive-*.json
│   ├── cpu-bound-moderate-*.json
│   └── latency-sensitive-*.json
├── bpf-traces/
│   ├── cpu-bound-burstable-runqlat.txt
│   ├── cpu-bound-burstable-runqlen.txt
│   └── cpu-bound-burstable-sched-switch.txt
└── scheduler-comparison/
    ├── cfs-*.json
    ├── scx_simple-*.json
    ├── scx_rusty-*.json
    └── scx_lavd-*.json
```

### 2. Monitoring Stack
```
phase4/monitoring/
├── prometheus/
│   └── (manifests)
├── grafana/
│   └── dashboards/
│       ├── cpu-throttling.json
│       └── scheduler-comparison.json
└── alerts/
    └── cpu-alerts.yaml
```

### 3. Updated GUIDE.md
New sections:
- "Understanding CPU Throttling" (with real examples)
- "BPF-Based Scheduler Analysis"
- "Prometheus Monitoring Setup"
- "Kernel Scheduler Tuning"
- "When to Use Custom Schedulers (scx)"

### 4. Correlation Dashboard
```
phase4/correlation-dashboard.html
```
Visual analysis of:
- Throttling events vs scheduler latency
- QoS class vs fairness
- Scheduler performance comparison

---

## Implementation Order

### Week 1: Throttling Experiments
1. Create aggressive-limit workload manifests
2. Run experiments to trigger throttling
3. Collect cgroup stats showing throttling

### Week 2: BPF Tracing
1. Deploy systemd BPF tracer on nodes
2. Re-run Phase 2 workloads with BPF enabled
3. Collect runqlat, runqlen, sched_switch traces

### Week 3: Prometheus Monitoring
1. Deploy prometheus-operator stack
2. Create ServiceMonitors for pod metrics
3. Build Grafana dashboards

### Week 4: Scheduler Comparison
1. Install scx tools on nodes
2. Run same workloads with each scheduler
3. Compare performance and throttling behavior

### Week 5: Analysis & Documentation
1. Run correlation analysis
2. Update GUIDE.md with new sections
3. Generate enhanced dashboard

---

## Success Criteria

### ✅ Complete when:

1. **Throttling demonstrated:**
   - At least 3 experiments showing >10% throttled time
   - Performance degradation quantified
   - cgroup stats show `nr_throttled > 0`

2. **BPF traces collected:**
   - runqlat histograms for each workload type
   - runqlen showing queue depth
   - sched_switch events correlated with throttling

3. **Prometheus deployed:**
   - All pod CPU metrics scraped
   - Dashboards showing real-time throttling
   - Alerts configured for throttling detection

4. **Schedulers compared:**
   - Same workload run on CFS, scx_simple, scx_rusty, scx_lavd
   - Performance comparison table
   - Recommendations for each workload type

5. **Guide updated:**
   - "Understanding CPU Throttling" section
   - BPF trace examples
   - Prometheus monitoring setup
   - Scheduler tuning guide

---

## Open Questions

1. **Node access:** How to SSH into Kubernetes VMs?
   - Need IP addresses and SSH keys
   - Or: kubectl debug with host network?

2. **scx availability:** Is kernel 6.12+ with CONFIG_SCHED_CLASS_EXT available?
   - Check: `uname -r` and `zcat /proc/config.gz | grep CONFIG_SCHED_CLASS_EXT`
   - Fallback: Document but don't test if unavailable

3. **Load generation:** For latency-sensitive tests:
   - Deploy HTTP server (nginx/golang)
   - Use wrk/hey for load generation
   - Measure P50/P99/P999 latency

---

## Next Steps

**Ready to proceed?** Choose starting point:

A. **Deploy BPF tracer** (requires node SSH access)
B. **Create throttling experiments** (can do immediately)
C. **Deploy Prometheus stack** (foundation for monitoring)
D. **Plan scheduler comparison** (requires kernel check)

Which would you like to start with?
