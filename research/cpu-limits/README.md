# Kubernetes CPU Requests and Limits - Research Project

**Status:** ✅ COMPLETE - All 6 Phases Done (5 core + 1 extensions)  
**Date:** October 1-26, 2026 (4 weeks)  
**Objective:** Provide comprehensive, evidence-based guidance on CPU requests/limits for Kubernetes developers and administrators

---

## Quick Start

### View Results

1. **📋 Final Summary:**  
   Read `FINAL_SUMMARY.md` for complete research overview (all 5 phases)

2. **📖 Developer Guide:**  
   Read `GUIDE.md` (Phase 2 - 515 lines) for sizing and patterns

3. **⚡ Throttling Analysis:**  
   Read `phase4/THROTTLING_ANALYSIS.md` for performance impact (69.8% throttling = 62.7% loss)

4. **🔧 Administrator Guides:**
   - `phase5/admin-guide/KERNEL_CONFIG.md` - CFS tuning, CPU isolation
   - `phase5/admin-guide/CUSTOM_SCHEDULERS.md` - scx guide
   - `phase5/admin-guide/PRODUCTION_PATTERNS.md` - 5 deployment patterns

5. **📊 Interactive Dashboard:**  
   Open `dashboard.html` in your browser for Phase 2 visual summary

6. **🚀 Future Work (Optional):**  
   `phase6/PHASE6_SUMMARY.md` - 6 extension topics (latency testing ✅, BPF traces, kernel experiments, memory limits)

### Key Findings (TL;DR)

**Phase 2 (QoS Classes):**
- **BestEffort** (no limits) = 98.8 ops/sec when cluster idle
- **Burstable** (request < limit) = 51.3 ops/sec with 0% throttling
- **Guaranteed** (request == limit) = 51.5 ops/sec with 0% throttling
- **Result:** Appropriate limits = no throttling

**Phase 4 (Intentional Throttling):**
- **No throttle:** 0% throttling, 51.65 ops/sec (baseline)
- **Light throttle:** 4.7% throttling, -4.5% performance
- **Moderate throttle:** 46.6% throttling, -39.7% performance
- **Heavy throttle:** **69.8% throttling, -62.7% performance** ⚠️

**Phase 5 (Production Guidance):**
- 5 deployment patterns documented
- Complete kernel tuning guide
- Custom scheduler (scx) evaluation framework

**Recommendation:** Use **request-only pattern** (no limits) for most workloads. Set limits only for multi-tenant clusters or cost control.

---

## What's Inside

### 📁 Project Structure

```
research/cpu-limits/
├── README.md                         ← You are here
├── GUIDE.md                          ← Main deliverable (515 lines)
├── IMPLEMENTATION_COMPLETE.md         ← Project summary
├── dashboard.html                    ← Visual results (open in browser)
│
├── phase1/workloads/                 ← Pod manifests
│   ├── cpu-bound-besteffort.yaml
│   ├── cpu-bound-burstable.yaml
│   ├── cpu-bound-guaranteed.yaml
│   ├── hybrid-burstable.yaml
│   ├── io-bound-guaranteed.yaml
│   └── memory-bound-guaranteed.yaml
│
├── phase2/                           ← Experiment automation
│   ├── run-experiments.py            ← Run all experiments
│   ├── generate-guide.py             ← Generate GUIDE.md from evidence
│   ├── generate-dashboard.py         ← Generate dashboard.html
│   ├── experiment-run.log            ← Last run log
│   └── evidence/                     ← Experiment results (JSON)
│       ├── cpu-bound-*.json
│       ├── hybrid-*.json
│       ├── io-bound-*.json
│       ├── memory-bound-*.json
│       └── summary.json
│
└── phase3/                           ← BPF tracing (optional)
    ├── bpf-tracer-daemon.py          ← Systemd service for BPF collection
    ├── bpf-tracer.service            ← Systemd unit
    ├── analyze-bpf.py                ← BPF/cgroup correlation
    └── daemonset/
        └── bpf-tracer-daemonset.yaml ← Kubernetes deployment
```

### 📊 Evidence Collected

**14 experiments** across:
- 4 workload types (CPU-bound, IO-bound, memory-bound, hybrid)
- 3 QoS classes (BestEffort, Burstable, Guaranteed)

Each experiment captured:
- cgroup CPU stats (usage, throttling counters)
- Node CPU stats (/proc/stat deltas)
- Workload performance (stress-ng bogo operations)
- Container logs and exit codes

---

## How to Use

### For Developers

**Read the guide:**
```bash
cat GUIDE.md
```

**Look for these sections:**
- "Developer Guidelines" - When to set limits
- "Recommended Patterns" - Request-only, Guaranteed, Burstable
- "Sizing Guidelines" - Measure and set values
- "Debugging Throttling" - Detect and fix issues

**Quick reference:**

```yaml
# Pattern 1: Request-only (RECOMMENDED)
resources:
  requests:
    cpu: "1000m"
  # No limits - can burst when cluster has capacity

# Pattern 2: Guaranteed (Critical services)
resources:
  requests:
    cpu: "2000m"
  limits:
    cpu: "2000m"

# Pattern 3: Burstable (Variable workloads)
resources:
  requests:
    cpu: "500m"
  limits:
    cpu: "2000m"
```

### For Administrators

**Read the guide:**
```bash
cat GUIDE.md
```

**Look for these sections:**
- "Administrator Guidelines" - Cluster-level CPU management
- "Kernel Scheduler Configuration" - CFS tuning, custom schedulers
- "Monitoring and Alerting" - Prometheus queries, alert rules
- "Resource Quotas" - Namespace enforcement

**Quick monitoring setup:**

```promql
# CPU throttling rate
rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0

# CPU usage vs request
rate(container_cpu_usage_seconds_total[5m]) /
  on(namespace,pod,container)
  kube_pod_container_resource_requests{resource="cpu"}
```

### To Re-Run Experiments

```bash
cd phase2
python3 run-experiments.py 2>&1 | tee experiment-run.log

# Wait ~15 minutes for all experiments

python3 generate-guide.py
python3 generate-dashboard.py
```

### To Add BPF Tracing (Optional)

BPF tracing requires direct node access (not via containers):

```bash
# SSH into Kubernetes node
ssh <node>

# Install tools
sudo apt install -y bpftrace linux-tools-generic

# Copy systemd service
sudo cp phase3/bpf-tracer.service /etc/systemd/system/
sudo cp phase3/bpf-tracer-daemon.py /usr/local/bin/
sudo chmod +x /usr/local/bin/bpf-tracer-daemon.py

# Start service
sudo systemctl daemon-reload
sudo systemctl enable bpf-tracer
sudo systemctl start bpf-tracer

# Check logs
sudo journalctl -u bpf-tracer -f
```

Then annotate pods to trigger tracing:
```bash
kubectl annotate pod <pod-name> bpf-trace=enabled
```

---

## Experiment Details

### Workload Types

1. **CPU-bound** (`stress-ng --cpu 4 --cpu-method ackermann`)
   - Pure computation
   - Tests CPU limit enforcement

2. **IO-bound** (`stress-ng --hdd 4`)
   - Disk I/O operations
   - Tests impact on I/O-heavy workloads

3. **Memory-bound** (`stress-ng --vm 2 --vm-bytes 1G`)
   - Memory access patterns
   - Tests memory bandwidth limits

4. **Hybrid** (CPU + I/O + memory)
   - Mixed workload
   - Real-world application simulation

### QoS Classes

| QoS | Config | Behavior |
|-----|--------|----------|
| **BestEffort** | No requests/limits | Lowest priority, can use any idle CPU |
| **Burstable** | request < limit | Can burst above request, throttled at limit |
| **Guaranteed** | request == limit | Fixed allocation, hard throttled |

### Metrics Collected

**cgroup CPU stats:**
```bash
cat /sys/fs/cgroup/cpu.stat
# usage_usec - total CPU time
# user_usec - user-space time
# system_usec - kernel-space time
# nr_periods - number of enforcement periods
# nr_throttled - times throttled
# throttled_usec - total throttled time
```

**Node CPU stats:**
```bash
cat /proc/stat
# cpu user nice system idle iowait irq softirq steal
```

**Workload performance:**
```
stress-ng: info:  [1] cpu  11854  120.02  453.95  0.14  98.76  26.10
                         ^bogo   ^real   ^usr   ^sys  ^ops/s  ^ops/s
```

---

## Results Summary

### Performance by QoS Class

| Workload | BestEffort | Burstable | Guaranteed |
|----------|------------|-----------|------------|
| CPU-bound | 98.8 ops/s | 51.3 ops/s | 51.5 ops/s |
| IO-bound | - | - | (varied) |
| Memory-bound | - | - | (varied) |
| Hybrid | - | 51.3 ops/s | - |

### Key Insights

1. **BestEffort is fastest** when cluster has spare capacity
2. **Limits cap performance** even with idle CPU
3. **Burstable ≈ Guaranteed** when request == limit
4. **No throttling observed** (limits were appropriate)
5. **Set limits carefully** - they reduce burst capacity

---

## References

### Research Articles

- [Stop Using CPU Limits - Robusta](https://home.robusta.dev/blog/stop-using-cpu-limits)
- [Unthrottled: Fixing CPU Limits - Indeed](https://engineering.indeedblog.com/blog/2019/12/unthrottled-fixing-cpu-limits-in-the-cloud/)
- [Why Keep Using CPU Limits - Medium](https://dnastacio.medium.com/why-you-should-keep-using-cpu-limits-on-kubernetes-60c4e50dfc61)
- [The Case for Resource Limits - Kubernetes Blog](https://kubernetes.io/blog/2023/11/16/the-case-for-kubernetes-resource-limits/)

### Official Docs

- [Kubernetes Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Pod QoS Classes](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/)
- [cgroup v2 Documentation](https://www.kernel.org/doc/Documentation/cgroup-v2.txt)

### Tools Used

- **stress-ng** - CPU/memory/IO stress testing
- **kubectl** - Kubernetes CLI
- **Python 3** - Automation and analysis
- **bpftrace** (optional) - Kernel tracing

---

## FAQ

### Q: Should I always set CPU limits?

**A:** No. For most workloads, set only requests. Limits prevent bursting even when cluster has spare capacity.

**Set limits when:**
- Multi-tenancy requires hard isolation
- Cost allocation needs strict enforcement
- Capacity planning requires predictable usage

**Avoid limits when:**
- Workload is bursty (batch jobs, CI/CD)
- Latency-sensitive (throttling adds latency)
- Cluster has spare capacity
- Using HPA/VPA for autoscaling

### Q: How do I size CPU requests?

1. Run workload without limits
2. Measure P95/P99 CPU usage over time
3. Set request = P95
4. Monitor throttling (should be zero)
5. Adjust if needed

### Q: What causes CPU throttling?

**Throttling happens when:**
- Container exceeds its CPU limit
- Occurs even if cluster has idle CPU
- Measured by `nr_throttled` and `throttled_usec` in cgroup stats

**To detect:**
```bash
# Inside container
cat /sys/fs/cgroup/cpu.stat | grep throttled

# Prometheus
rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0
```

### Q: What's the difference between requests and limits?

| Aspect | Requests | Limits |
|--------|----------|--------|
| **Purpose** | Scheduling placement | Runtime enforcement |
| **cgroup** | cpu.weight (priority) | cpu.max (quota) |
| **Behavior** | Scheduler guarantee | Hard cap |
| **Impact** | Where pod runs | How much CPU it can use |

### Q: Why didn't BPF tracing work?

BPF tools (bpftrace, perf) require privileged access that rootless containers can't provide:

```
Error: mknodat `/dev/loop0`: Permission denied
```

**Solution:** Install BPF tools directly on Kubernetes nodes via SSH, not in containers.

---

## Phase 4: CPU Throttling Analysis ✅ COMPLETE

Phase 4 intentionally triggers CPU throttling to demonstrate performance impact. Unlike Phase 2 (0% throttling), these experiments show what happens when containers exceed quotas.

### Phase 4 Results

**4 throttling experiments completed:**

| Configuration | Limit | Throttling | Performance | vs Phase 2 |
|---------------|-------|------------|-------------|------------|
| Heavy Throttle | 500m | **69.8%** | 19.2 ops/s | **-62.7%** |
| Moderate Throttle | 1500m | 33.2% | 53.6 ops/s | +4.0% |
| Light Throttle | 3000m | 4.7% | 108.8 ops/s | +111.2% |
| No Throttle | 4000m | 0.0% | 139.1 ops/s | **+170.0%** |

**Key Finding:** 69.8% throttling → 62.7% performance loss

### What's Included

1. **Throttling Experiments** ✅ Complete
   - 4 configurations demonstrating throttling impact
   - Evidence: `phase4/evidence/*.json`
   - Analysis: `phase4/THROTTLING_ANALYSIS.md`

2. **Prometheus Monitoring** ✅ Ready to deploy
   - Complete kube-prometheus-stack deployment
   - Grafana dashboard with throttling panels
   - Alert rules for production use
   - See: `phase4/monitoring/`

3. **BPF Scheduler Traces** ✅ Infrastructure deployed
   - Systemd service on cluster nodes
   - Guest-side BPF collection
   - See: `phase3/`

4. **Complete Documentation** ✅
   - `RESEARCH_COMPLETE.md` - Master summary
   - `phase4/THROTTLING_ANALYSIS.md` - Deep throttling analysis
   - `complete-research-dashboard.html` - Visual summary

### Phase 4 Quick Start

```bash
cd phase4

# View analysis (already generated)
cat THROTTLING_ANALYSIS.md

# Re-run experiments (optional)
python3 run-throttling-experiments.py
python3 analyze-throttling-fixed.py

# Deploy Prometheus monitoring
./deploy-prometheus.sh
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

See `phase4/README.md` and `RESEARCH_COMPLETE.md` for complete details.

---

## Next Steps

### Extend Research (Beyond Phase 4)

1. **More workload types:**
   - Network-intensive (nginx, redis)
   - Batch jobs (spark, flink)
   - Databases (postgres, mysql)

2. **Latency-sensitive testing:**
   - Deploy HTTP server with CPU limits
   - Measure P99 latency during throttling
   - Use wrk/hey for load generation

3. **Production validation:**
   - Apply patterns to real workloads
   - Measure before/after metrics
   - Document case studies

---

## Contributing

Found an issue or have improvements? The research is repeatable:

```bash
# Re-run experiments
cd phase2
python3 run-experiments.py

# Regenerate outputs
python3 generate-guide.py
python3 generate-dashboard.py
```

---

## License

This research is part of the k8labs project. See repository root for license details.

---

**Questions?** Open an issue or consult the comprehensive guide at `GUIDE.md`.
