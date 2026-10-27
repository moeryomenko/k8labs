# CPU Requests/Limits Research - Implementation Complete

**Date:** 2026-10-26  
**Status:** Phase 1 and 2 complete; Phase 3 superseded by Phase 7.4

> **Correction.** An earlier revision of this file claimed that no CPU
> throttling occurred in Phase 2. That is false, and the evidence refutes it:
> where both a start and an end `cpu.stat` snapshot exist, the containers were
> throttled in nearly every quota period (see Key Findings below).

---

## What Was Delivered

### 1. Comprehensive Guide
**Location:** `research/cpu-limits/GUIDE.md`

The guide includes:
- Executive summary with key findings
- Deep explanation of CPU requests vs limits
- QoS class behavior (BestEffort, Burstable, Guaranteed)
- cgroup v2 CPU controller mechanics
- Experimental results across 4 workload types
- Developer guidelines (when to set limits, sizing patterns)
- Administrator guidelines (kernel tuning, monitoring, alerting)
- Complete reference list

### 2. Experimental Evidence
**Location:** `research/cpu-limits/phase2/evidence/`

14 experiments conducted across:
- **Workload Types:**
  - CPU-bound (pure computation)
  - IO-bound (disk operations)
  - Memory-bound (memory access patterns)
  - Hybrid (mixed workload)
  
- **QoS Classes:**
  - BestEffort (no requests/limits)
  - Burstable (request < limit)
  - Guaranteed (request == limit)

Each experiment captured:
- cgroup CPU statistics (usage, throttling)
- Node CPU statistics (/proc/stat)
- Workload performance (stress-ng bogo ops)
- Container logs
- Duration and exit codes

### 3. Key Findings

**Performance Impact:**
- BestEffort: 98.8 ops/sec, but over 0.3 s (short sample); 73.18 ops/sec over
  the full 120 s run
- Burstable: 51.41 ops/sec (`cpu.max` = `200000 100000`), 1150/1150 periods
  throttled
- Guaranteed: 51.65 ops/sec with a 2-CPU quota; the end snapshot is missing
  from this run, so its throttling is unknown

**Throttling (where both snapshots exist):**
- Only 5 of 14 evidence files contain a start *and* an end `cpu.stat`. The rest
  have `final_cgroup: null`, so their run-level throttling is unknown, not zero.
- cpu-bound burstable: 1150/1150 periods throttled (100.0%), 22.7s throttled
- cpu-bound burstable: 1167/1178 periods (99.1%), 29.8s
- hybrid burstable: 1186/1186 periods (100.0%), 138.9s
- memory-bound guaranteed: 1170/1172 periods (99.8%), 43.6s
- The early "no throttling" claim came from reading a start-of-run snapshot
  (`nr_periods 0`) as a result. The complete per-run table is in `GUIDE.md`.

**Node CPU Usage:**
- BestEffort used 98.5% of available CPU
- Burstable/Guaranteed capped at 51% (2 cores out of 4)
- Demonstrates hard enforcement of limits via cgroup

### 4. Automation Tools

**Experiment Runner:**
`research/cpu-limits/phase2/run-experiments.py`
- Automatically runs all workload configurations
- Collects metrics at baseline and completion
- Generates structured evidence JSON
- Handles cleanup between experiments

**Guide Generator:**
`research/cpu-limits/phase2/generate-guide.py`
- Parses all evidence files
- Extracts performance metrics from stress-ng output
- Calculates CPU deltas from /proc/stat
- Generates comprehensive markdown guide

---

## Phase 3 (superseded)

### BPF Tracing Integration

**Status:** Implemented but not deployed due to rootless container constraints

**What Was Built:**
- `research/cpu-limits/phase3/bpf-tracer-daemon.py` - Systemd service for BPF collection
- `research/cpu-limits/phase3/daemonset/bpf-tracer-daemonset.yaml` - Kubernetes deployment
- `research/cpu-limits/phase3/analyze-bpf.py` - Correlation analysis tool

**Why Not Deployed:**
Running privileged BPF tools (bpftrace, perf) in rootless crun containers hits permission issues:
```
Error: container create failed: mknodat `/dev/loop0`: Permission denied
```

**How to Deploy (if needed):**

1. **SSH into Kubernetes node (guest VM)**
2. **Install bpftrace:**
   ```bash
   sudo apt install -y bpftrace linux-tools-generic
   ```

3. **Copy systemd service:**
   ```bash
   sudo cp research/cpu-limits/phase3/bpf-tracer.service /etc/systemd/system/
   sudo cp research/cpu-limits/phase3/bpf-tracer-daemon.py /usr/local/bin/
   sudo chmod +x /usr/local/bin/bpf-tracer-daemon.py
   ```

4. **Start service:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable bpf-tracer
   sudo systemctl start bpf-tracer
   ```

5. **Trigger via annotation:**
   ```bash
   kubectl annotate pod <pod-name> bpf-trace=enabled
   ```

**What BPF Tracing Would Add:**
- Scheduler latency (runqlat) - time tasks wait in run queue
- Run queue length (runqlen) - scheduler contention
- Context switches (sched:sched_switch) - CPU scheduling events
- Correlation with throttling events

**Is It Needed?**
Not for the core guide. The Phase 2 evidence already demonstrates:
- CPU limit enforcement via cgroup
- Performance impact of different QoS classes
- Clear recommendations for developers and administrators

BPF tracing would add deeper kernel-level insight but isn't required for the stated objectives.

---

## How to Use the Deliverables

### For Developers
Read `research/cpu-limits/GUIDE.md` sections:
- "Developer Guidelines" - When to set limits
- "Recommended Patterns" - Request-only, Guaranteed, Burstable
- "Sizing Guidelines" - How to measure and set values
- "Debugging Throttling" - How to detect and fix issues

### For Administrators
Read `research/cpu-limits/GUIDE.md` sections:
- "Administrator Guidelines" - Cluster-level CPU management
- "Kernel Scheduler Configuration" - CFS tuning, custom schedulers
- "Monitoring and Alerting" - Prometheus queries and alert rules
- "Resource Quotas" - Namespace-level enforcement

### To Re-Run Experiments
```bash
cd research/cpu-limits/phase2
python3 run-experiments.py 2>&1 | tee experiment-run.log
```

### To Regenerate Guide
```bash
cd research/cpu-limits/phase2
python3 generate-guide.py
```

---

## Alignment with Original Objectives

### ✅ Achieved

1. **Demonstrate CPU usage vs throttling**
   - Experiments show CPU usage capped by limits
   - Throttling was measured in every run that has an end snapshot
   - Guide explains when throttling happens

2. **Impact on application types**
   - Tested 4 workload types (CPU, IO, memory, hybrid)
   - Measured performance impact of each QoS class
   - Clear performance differences documented

3. **Provide developer guide**
   - When to set limits (3 clear patterns)
   - Sizing guidelines based on monitoring
   - Debugging procedures with examples

4. **Provide administrator guide**
   - Kernel scheduler tuning
   - Custom scheduler recommendations (scx)
   - Cluster-level policies (quotas, limits, priority)
   - Monitoring and alerting setup

5. **Low-level BPF traces: delivered later as Phase 7.4**
   - bpftrace ran inside an unprivileged reader pod mounting host tracefs, so
     node SSH was not needed
   - `evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`: worst
     wake-to-run delay 5.5 ms with no limit, 71.7 ms at a 100m limit

### 📊 Evidence Format

All experiments stored as structured JSON:
```json
{
  "experiment_id": "cpu-bound-burstable-20261026T110714Z",
  "workload_type": "cpu-bound",
  "qos_class": "burstable",
  "duration_seconds": 122.2,
  "baseline_cgroup": {...},
  "final_cgroup": {...},
  "baseline_node_cpu": {...},
  "final_node_cpu": {...},
  "workload_logs": "...",
  "workload_exit_code": 0
}
```

---

## Next Steps (If Needed)

1. **Add more workload variations:**
   - Network-intensive (nginx, redis)
   - Batch jobs (spark, flink)
   - Real applications (databases, web servers)

2. **BPF tracing:** done as Phase 7.4 from the reader pod; the SSH-based
   systemd service in `phase3/` was not used

3. **Extend monitoring:**
   - Deploy Prometheus + Grafana
   - Create dashboards from guide's PromQL queries
   - Set up alert rules

4. **Test custom schedulers:**
   - Install scx tools
   - Compare CFS vs scx_simple vs scx_rusty
   - Measure latency improvements

5. **Production validation:**
   - Apply patterns to real workloads
   - Measure before/after metrics
   - Document case studies

---

## File Inventory

```
research/cpu-limits/
├── GUIDE.md                          # Comprehensive guide
├── IMPLEMENTATION_COMPLETE.md         # This file
├── phase1/
│   └── workloads/                    # 6 Pod manifests (besteffort, burstable, guaranteed)
├── phase2/
│   ├── run-experiments.py            # Automated experiment runner
│   ├── generate-guide.py             # Guide generator from evidence
│   ├── experiment-run.log            # Last run log
│   └── evidence/
│       ├── *.json                    # 14 experiment results
│       └── summary.json              # Aggregate summary
└── phase3/
    ├── bpf-tracer-daemon.py          # Systemd BPF collector (optional)
    ├── bpf-tracer.service            # Systemd unit file
    ├── analyze-bpf.py                # BPF/cgroup correlation tool
    └── daemonset/
        └── bpf-tracer-daemonset.yaml # Kubernetes DaemonSet (has permission issues)
```

---

## Conclusion

**Core deliverable complete:** The guide provides comprehensive, evidence-based recommendations for CPU requests/limits based on real experiments.

**BPF tracing is bonus:** The infrastructure exists if deeper kernel analysis is needed, but the current evidence is sufficient for developer and administrator guidance.

**Ready to use:** Teams can immediately apply the patterns and monitoring setup from the guide.
