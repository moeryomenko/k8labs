# Custom Schedulers Guide: When and How to Use sched-ext (scx)

**Audience:** Kubernetes administrators considering custom schedulers  
**Objective:** Understand when to use scx and how to deploy it

---

## Table of Contents

1. [What is sched-ext (scx)?](#what-is-sched-ext-scx)
2. [When to Use Custom Schedulers](#when-to-use-custom-schedulers)
3. [Available scx Schedulers](#available-scx-schedulers)
4. [Installation and Setup](#installation-and-setup)
5. [Performance Comparison](#performance-comparison)
6. [Migration Strategy](#migration-strategy)
7. [Monitoring and Debugging](#monitoring-and-debugging)

---

## What is sched-ext (scx)?

### Overview

**sched-ext** is a Linux kernel feature (since 6.12) that allows implementing custom CPU schedulers as eBPF programs, without modifying the kernel.

**Key Features:**
- **BPF-based**: Write schedulers in restricted C (compiled to eBPF)
- **Safe**: Cannot crash the kernel (BPF verifier ensures safety)
- **Dynamic**: Load/unload schedulers without rebooting
- **Flexible**: Implement domain-specific scheduling policies

**Kernel Requirements:**
- Linux **6.12+**
- `CONFIG_SCHED_CLASS_EXT=y`

### How It Works

```
User Task
    ↓
CFS (default)  ←→  scx Scheduler (BPF)
    ↓                    ↓
Kernel Core Scheduler
    ↓
CPU Runqueue
```

**Fallback Safety:**
- If scx scheduler fails or is unloaded, tasks fall back to CFS
- No risk of system hang

---

## When to Use Custom Schedulers

### ✅ Good Use Cases

#### 1. Specialized Workloads
- **Gaming servers** - Minimize frame time variance
- **HPC clusters** - Gang scheduling for parallel jobs
- **Real-time systems** - Deterministic latency
- **Trading systems** - Ultra-low latency (< 10μs)

#### 2. Research and Experimentation
- Test new scheduling algorithms
- Benchmark against CFS
- Optimize for specific hardware

#### 3. Domain-Specific Optimization
- ML training - GPU-CPU co-scheduling
- Video encoding - Deadline-aware scheduling
- Databases - NUMA-aware placement

---

### ❌ When NOT to Use scx

#### 1. General-Purpose Kubernetes
- CFS works well for 95% of workloads
- scx adds complexity
- Limited production experience

#### 2. Multi-Tenant Clusters
- CFS provides proven fairness guarantees
- scx schedulers may have different fairness semantics
- Harder to debug issues

#### 3. Compliance-Sensitive Environments
- scx is relatively new (kernel 6.12+, Oct 2024)
- May not meet certification requirements
- CFS has decades of production hardening

---

## Available scx Schedulers

### 1. scx_simple

**Type:** Round-robin per-CPU  
**Complexity:** Low (reference implementation)

**Characteristics:**
- Simplest possible scheduler
- No load balancing
- Tasks stay on assigned CPU
- Useful for benchmarking overhead

**Use Case:** Testing, learning scx basics

**Command:**
```bash
sudo scx_simple
```

---

### 2. scx_rusty

**Type:** Load-balanced multi-domain  
**Complexity:** High

**Characteristics:**
- Rust-based implementation
- Hierarchical scheduling domains (like CFS)
- Dynamic load balancing
- NUMA-aware

**Use Case:** General-purpose replacement for CFS

**Command:**
```bash
sudo scx_rusty
```

**Parameters:**
```bash
# Limit to specific CPUs
sudo scx_rusty --cpumask 0-7

# Set scheduling domains
sudo scx_rusty --domains "0-3,4-7"
```

---

### 3. scx_lavd

**Type:** Latency-aware  
**Complexity:** Medium

**Characteristics:**
- Optimizes for low latency
- Tracks per-task latency metrics
- Prioritizes interactive workloads
- Good for desktop and latency-sensitive servers

**Use Case:** HTTP APIs, databases, real-time apps

**Command:**
```bash
sudo scx_lavd
```

**Expected improvement from our research:**
- Heavy throttle (69.8%, Phase 4) might see lower P99 latency
- Benefit unclear for already-low throttling scenarios

---

### 4. scx_nest

**Type:** Central scheduler with worker CPUs  
**Complexity:** Medium

**Characteristics:**
- One CPU makes scheduling decisions
- Other CPUs execute tasks
- Reduces synchronization overhead
- Good for moderate core counts (< 32)

**Use Case:** Mid-sized servers with mixed workloads

**Command:**
```bash
sudo scx_nest
```

---

## Installation and Setup

### Check Kernel Support

```bash
# Check kernel version
uname -r
# Must be 6.12+

# Check if sched-ext is enabled
grep CONFIG_SCHED_CLASS_EXT /boot/config-$(uname -r)
# Should output: CONFIG_SCHED_CLASS_EXT=y

# Check if sched-ext is available
ls /sys/kernel/sched_ext/
# Should show: enable, state, ...
```

---

### Install scx Tools

#### From Source (Recommended)

```bash
# Install dependencies
sudo apt install -y git build-essential clang llvm libelf-dev \
  libbpf-dev linux-headers-$(uname -r) rust-all

# Clone repository
git clone https://github.com/sched-ext/scx.git
cd scx

# Build
make

# Install
sudo make install

# Verify
which scx_simple scx_rusty scx_lavd
```

---

#### From Package (if available)

```bash
# Ubuntu 25.04+, Debian 13+
sudo apt install scx-schedulers

# Fedora 42+
sudo dnf install scx-schedulers
```

---

### Load a Scheduler

```bash
# Start scx_lavd (latency-aware)
sudo scx_lavd

# In another terminal, check status
cat /sys/kernel/sched_ext/state
# Output: enabled

# Stop (Ctrl+C)
# Tasks automatically fall back to CFS
```

---

### Run as Systemd Service

```ini
# /etc/systemd/system/scx-lavd.service
[Unit]
Description=sched-ext Latency-Aware Scheduler
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/bin/scx_lavd
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable scx-lavd
sudo systemctl start scx-lavd
```

---

## Performance Comparison

### Benchmark: Phase 4 Throttling Workload

**Hypothesis:** scx_lavd reduces latency under heavy throttling

**Method:**
1. Run Phase 4 heavy-throttle workload (69.8% throttling)
2. Test under CFS and scx_lavd
3. Compare bogo ops/sec and P99 latency (if latency test available)

**Expected Results:**

| Scheduler | Throttling % | Performance | P99 Latency |
|-----------|--------------|-------------|-------------|
| CFS | 69.8% | 19.2 ops/s | ~500ms (est) |
| scx_lavd | 69.8% | 20-22 ops/s | ~300ms (est) |

**Why it might help:**
- scx_lavd tracks latency metrics
- May schedule throttled tasks more aggressively when they wake up
- CFS treats all wakeups equally

**Why it might not:**
- Throttling is enforced by cgroup CPU controller, not scheduler
- scx cannot bypass quota limits
- Improvement limited to scheduling order, not CPU time available

---

### Experiment Script

```bash
#!/bin/bash
# Compare CFS vs scx_lavd

set -e

# 1. Run under CFS (default)
echo "Testing CFS..."
cd research/cpu-limits/phase4
python3 run-throttling-experiments.py --config heavy-throttle
mv evidence/cpu-bound-heavy-throttle-*.json evidence/cfs-result.json

# 2. Start scx_lavd
echo "Starting scx_lavd..."
sudo scx_lavd &
SCX_PID=$!
sleep 5

# Verify scx is active
if [ "$(cat /sys/kernel/sched_ext/state)" != "enabled" ]; then
  echo "Error: scx_lavd did not start"
  exit 1
fi

# 3. Run under scx_lavd
echo "Testing scx_lavd..."
python3 run-throttling-experiments.py --config heavy-throttle
mv evidence/cpu-bound-heavy-throttle-*.json evidence/scx-lavd-result.json

# 4. Stop scx_lavd
echo "Stopping scx_lavd..."
sudo kill $SCX_PID
wait $SCX_PID

# 5. Compare
echo "Comparison:"
echo "CFS:"
jq '.performance.bogo_ops_per_sec_real' evidence/cfs-result.json
echo "scx_lavd:"
jq '.performance.bogo_ops_per_sec_real' evidence/scx-lavd-result.json
```

---

## Migration Strategy

### Phase 1: Testing (Non-Production)

1. **Set up test cluster**
   - Separate from production
   - Match production hardware/kernel

2. **Test scx_simple first**
   ```bash
   sudo scx_simple
   # Run existing workloads
   # Verify no crashes
   ```

3. **Test target scheduler (e.g., scx_lavd)**
   ```bash
   sudo scx_lavd
   # Run representative workloads
   # Measure performance
   ```

4. **Run for 24-48 hours**
   - Monitor for stability
   - Check for memory leaks
   - Verify automatic fallback works

---

### Phase 2: Canary Deployment

1. **Select canary node pool**
   - Dedicate 1-2 nodes
   - Taint nodes to control placement

2. **Deploy scx via systemd**
   ```bash
   # On canary nodes
   sudo systemctl enable scx-lavd
   sudo systemctl start scx-lavd
   ```

3. **Route test traffic**
   ```yaml
   # Pod with toleration for canary nodes
   tolerations:
   - key: "scheduler"
     operator: "Equal"
     value: "scx"
     effect: "NoSchedule"
   ```

4. **Monitor metrics**
   - CPU utilization
   - Latency (P50/P95/P99)
   - Throttling rate
   - Context switches

5. **Compare to baseline**
   - Same workload on CFS nodes
   - A/B test for 1-2 weeks

---

### Phase 3: Gradual Rollout

1. **Expand to 10% of fleet**
2. **Monitor for 1 week**
3. **Expand to 50% of fleet**
4. **Monitor for 1 week**
5. **Full rollout or rollback**

---

### Rollback Plan

scx makes rollback trivial:

```bash
# Stop scx service
sudo systemctl stop scx-lavd

# Tasks automatically fall back to CFS
# No reboot required
```

---

## Monitoring and Debugging

### Check scx Status

```bash
# Is scx enabled?
cat /sys/kernel/sched_ext/state
# enabled | disabled | disabling

# Which scheduler?
cat /sys/kernel/sched_ext/name
# scx_lavd

# Any errors?
dmesg | grep -i sched_ext
```

---

### Monitor Performance

**Key metrics:**
```bash
# Context switches (should be similar to CFS)
vmstat 1 | awk '{print $12}'

# Load average (should be comparable)
uptime

# Per-task scheduling stats
cat /proc/<pid>/sched

# CPU utilization
mpstat -P ALL 1
```

**Prometheus queries:**
```promql
# CPU usage (should match CFS)
rate(node_cpu_seconds_total[5m])

# Throttling (scx cannot reduce this)
rate(container_cpu_cfs_throttled_seconds_total[5m])

# Context switches
rate(node_context_switches_total[5m])
```

---

### Debug Issues

**Scheduler hung or unresponsive:**
```bash
# scx should auto-disable and fall back to CFS
cat /sys/kernel/sched_ext/state
# Should show: disabled

# Check logs
dmesg | tail -50
# Look for: "sched_ext: BPF scheduler XXXX disabled (error)"
```

**Tasks not making progress:**
```bash
# Check if scx is still active
cat /sys/kernel/sched_ext/state

# Manual fallback (if needed)
sudo scx_state disable
```

**Performance regression:**
```bash
# Compare context switches
# More switches = more overhead
pidstat 1 | grep stress-ng

# Compare CPU utilization
# Lower utilization = wasted capacity
mpstat -P ALL 1

# Stop scx and retest
sudo systemctl stop scx-lavd
# Re-run workload
```

---

## Recommendations

### For Most Kubernetes Clusters

**Stick with CFS** unless:
- You have a specific, measurable problem CFS cannot solve
- You've tested scx thoroughly in non-production
- You have kernel 6.12+ available
- You can commit to monitoring and iterating

**CFS is proven:**
- 15+ years of production use
- Well-understood performance characteristics
- Extensive tuning documentation
- Stable across kernel versions

---

### When to Consider scx

1. **Latency-critical workloads**
   - P99 latency > 50ms under load
   - CFS tuning (see KERNEL_CONFIG.md) didn't help
   - Willing to invest in validation

2. **Specialized environments**
   - HPC, gaming, real-time
   - Homogeneous workloads
   - Custom hardware

3. **Research and optimization**
   - Proving scheduler impact
   - Academic research
   - Vendor differentiation

---

## References

- [sched-ext Project](https://github.com/sched-ext/scx)
- [Kernel Documentation](https://www.kernel.org/doc/html/latest/scheduler/sched-ext.html)
- [LWN Article: sched-ext](https://lwn.net/Articles/922405/)
- [Phoronix Benchmarks](https://www.phoronix.com/review/sched-ext-linux-612)

---

**Next:** See [PRODUCTION_PATTERNS.md](./PRODUCTION_PATTERNS.md) for practical Kubernetes deployment patterns.
