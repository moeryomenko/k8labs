# Kernel Scheduler Configuration Guide for Kubernetes Administrators

**Audience:** Kubernetes cluster administrators  
**Objective:** Configure Linux kernel scheduler for optimal container performance

---

## Table of Contents

1. [CFS Scheduler Overview](#cfs-scheduler-overview)
2. [CPU cgroup Controller](#cpu-cgroup-controller)
3. [CFS Parameters](#cfs-parameters)
4. [UP vs SMP Scheduling](#up-vs-smp-scheduling)
5. [CPU Isolation](#cpu-isolation)
6. [Production Tuning Examples](#production-tuning-examples)

---

## CFS Scheduler Overview

### What is CFS?

The **Completely Fair Scheduler (CFS)** is the default Linux process scheduler since kernel 2.6.23. It schedules tasks based on a red-black tree of virtual runtime, ensuring fair CPU distribution.

**Key Characteristics:**
- **Fair**: Each process gets proportional CPU time based on nice value/cgroup weight
- **Low latency**: Target latency of 6-48ms (configurable)
- **Scalable**: Handles thousands of tasks efficiently
- **Group scheduling**: Respects cgroup CPU limits and shares

### How Kubernetes Uses CFS

```
Kubernetes Pod
├── cpu.max (limit)        → CFS quota enforcement (throttling)
├── cpu.weight (request)   → CFS share-based priority
└── Tasks (processes)      → Scheduled by CFS
```

**Without limits:**
- Pods get CPU time proportional to their `cpu.weight`
- No throttling, can burst to 100% CPU

**With limits:**
- CFS enforces `cpu.max` (quota/period)
- Exceeding quota triggers throttling (process suspension)

---

## CPU cgroup Controller

### cgroup v2 CPU Controller Files

Located at `/sys/fs/cgroup/<cgroup>/`:

| File | Purpose | Example |
|------|---------|---------|
| `cpu.max` | Quota and period (limits) | `200000 100000` = 2 cores |
| `cpu.weight` | Relative share (requests) | `100` (default) to `10000` |
| `cpu.stat` | Usage and throttling stats | See below |
| `cpu.pressure` | PSI pressure metrics | Stall time |

### cpu.stat Fields

```bash
$ cat /sys/fs/cgroup/kubepods/cpu.stat

usage_usec 1234567890       # Total CPU time consumed (microseconds)
user_usec 1000000000        # User-space time
system_usec 234567890       # Kernel-space time
nr_periods 12345            # Number of enforcement periods elapsed
nr_throttled 678            # Number of periods where throttling occurred
throttled_usec 456789000    # Total time spent throttled (microseconds)
nr_bursts 0                 # Burst periods (if burst enabled)
burst_usec 0                # Time from burst allowance
```

### Kubernetes Resource Mapping

```yaml
resources:
  requests:
    cpu: "500m"      # → cpu.weight = 51 (500/1000 * 100 + 2)
  limits:
    cpu: "2000m"     # → cpu.max = "200000 100000"
```

**Weight calculation:**
```
cpu.weight = (requests_milliсores / 1000) * 100 + 2
```

**Quota calculation:**
```
quota = (limits_millicores / 1000) * period
period = 100ms (100000 microseconds)
```

---

## CFS Parameters

### Core CFS Sysctls

Located at `/proc/sys/kernel/sched_*`:

#### 1. sched_latency_ns

**Default:** `6000000` (6ms)  
**Range:** `100000` - `1000000000`

Target latency for all tasks to run once. CFS aims to schedule each task within this window.

**Higher values:**
- ✅ Better throughput (fewer context switches)
- ❌ Higher latency for individual tasks

**Lower values:**
- ✅ Lower latency (faster response)
- ❌ More context switches (overhead)

**Tuning:**
```bash
# For latency-sensitive workloads (HTTP, databases)
echo 3000000 > /proc/sys/kernel/sched_latency_ns  # 3ms

# For throughput-focused workloads (batch, ML)
echo 24000000 > /proc/sys/kernel/sched_latency_ns  # 24ms
```

---

#### 2. sched_min_granularity_ns

**Default:** `750000` (0.75ms)  
**Range:** `100000` - `100000000`

Minimum time a task runs before being preempted. Prevents excessive context switching.

**Relationship:**
```
min_granularity ≤ (latency_target / nr_running)
```

**Tuning:**
```bash
# For very low latency (trading, real-time)
echo 500000 > /proc/sys/kernel/sched_min_granularity_ns  # 0.5ms

# For batch workloads
echo 2000000 > /proc/sys/kernel/sched_min_granularity_ns  # 2ms
```

---

#### 3. sched_wakeup_granularity_ns

**Default:** `1000000` (1ms)  
**Range:** `100000` - `100000000`

Threshold for preempting a running task when a sleeping task wakes up.

**Lower values:**
- ✅ Better for interactive/latency-sensitive tasks
- ❌ More context switches

**Higher values:**
- ✅ Better for CPU-bound tasks
- ❌ Higher wakeup latency

**Tuning:**
```bash
# For latency-sensitive (wake up quickly)
echo 500000 > /proc/sys/kernel/sched_wakeup_granularity_ns  # 0.5ms

# For CPU-bound (reduce context switches)
echo 2000000 > /proc/sys/kernel/sched_wakeup_granularity_ns  # 2ms
```

---

#### 4. sched_cfs_bandwidth_slice_us

**Default:** `5000` (5ms)  
**Range:** `1000` - `100000`

Time slice for CFS bandwidth control (throttling). When a cgroup exceeds its quota, it's suspended for the remainder of the period. This controls how long each slice is.

**Smaller slices (1ms):**
- ✅ More responsive throttling
- ✅ Better for latency-sensitive workloads
- ❌ More overhead (frequent checks)

**Larger slices (10ms):**
- ✅ Less overhead
- ✅ Better for batch workloads
- ❌ Coarser throttling (longer delays)

**From our Phase 4 research:**
- Heavy throttle (69.8%) with 5ms slice = severe latency spikes
- Reducing slice to 1ms may reduce P99 latency (but more CPU overhead)

**Tuning:**
```bash
# For latency-sensitive (fine-grained throttling)
echo 1000 > /proc/sys/kernel/sched_cfs_bandwidth_slice_us  # 1ms

# For throughput (coarse-grained throttling)
echo 10000 > /proc/sys/kernel/sched_cfs_bandwidth_slice_us  # 10ms
```

---

### SMP Scheduling Parameters

#### 5. sched_migration_cost_ns

**Default:** `500000` (0.5ms)  
**Range:** `0` - `5000000`

Time a task must be running before CFS considers migrating it to another CPU.

**Higher values:**
- ✅ Less migration (better cache locality)
- ❌ Poorer load balancing

**Lower values:**
- ✅ Better load balancing
- ❌ More cache misses

**Tuning:**
```bash
# For CPU-bound (reduce migration)
echo 2000000 > /proc/sys/kernel/sched_migration_cost_ns  # 2ms

# For I/O-bound (allow migration)
echo 100000 > /proc/sys/kernel/sched_migration_cost_ns  # 0.1ms
```

---

#### 6. sched_nr_migrate

**Default:** `32`  
**Range:** `1` - `1024`

Maximum number of tasks to migrate in one load balancing operation.

**Higher values:**
- ✅ Faster load balancing
- ❌ More overhead per balance

**Lower values:**
- ✅ Less overhead
- ❌ Slower convergence

**Tuning:**
```bash
# For clusters with many small pods
echo 64 > /proc/sys/kernel/sched_nr_migrate

# For clusters with few large pods
echo 16 > /proc/sys/kernel/sched_nr_migrate
```

---

## UP vs SMP Scheduling

### Uniprocessor (UP) Build

**When:**
- Single-core VMs
- Embedded systems
- Testing environments

**Benefits:**
- No SMP overhead (simpler code paths)
- No cache line bouncing between CPUs
- Slightly faster context switches

**Kernel config:**
```
CONFIG_SMP=n
```

**Trade-offs:**
- Cannot use multiple CPUs (obviously)
- Rare in production Kubernetes clusters

---

### Symmetric Multiprocessing (SMP)

**Default for modern kernels**

**Features:**
- Load balancing across CPUs
- CPU affinity and migration
- NUMA awareness
- Per-CPU runqueues

**Scheduling Domains:**
- Organized hierarchically (core → package → node)
- Load balancing within domains
- Configurable via `/proc/sys/kernel/sched_domain/`

**Viewing scheduling domains:**
```bash
ls /proc/sys/kernel/sched_domain/cpu0/
# domain0  domain1  domain2  ...
```

---

## CPU Isolation

### Boot Parameter: isolcpus

Reserve CPUs for specific workloads, excluding them from the global scheduler.

**Syntax:**
```bash
isolcpus=<cpu-list>
```

**Example** (isolate CPUs 2-7):
```bash
# /etc/default/grub
GRUB_CMDLINE_LINUX="isolcpus=2-7"
```

**Effect:**
- Normal processes won't be scheduled on isolated CPUs
- Use `taskset` or cgroup `cpuset` to assign tasks

**Kubernetes usage:**
```yaml
# Guaranteed QoS + cpuset
resources:
  requests:
    cpu: "4"
  limits:
    cpu: "4"
# Kubernetes assigns isolated CPUs if available
```

---

### cgroup cpuset Controller

Pin processes to specific CPUs.

**Example:**
```bash
# Assign pod to CPUs 0-1
echo "0-1" > /sys/fs/cgroup/kubepods/pod-uuid/cpuset.cpus

# Assign to NUMA node 0
echo "0" > /sys/fs/cgroup/kubepods/pod-uuid/cpuset.mems
```

**Kubernetes CPU Manager:**
- Enabled with `--cpu-manager-policy=static`
- Allocates exclusive CPUs to Guaranteed pods
- Reduces context switching and cache misses

**Configuration:**
```yaml
# kubelet config
cpuManagerPolicy: static
cpuManagerReconcilePeriod: 10s
topologyManagerPolicy: best-effort
```

---

## Production Tuning Examples

### Scenario 1: Latency-Sensitive Microservices

**Workload:** HTTP APIs, databases, real-time systems

**Symptoms:**
- P99 latency spikes
- Inconsistent response times
- Throttling causing delays

**Tuning:**
```bash
# Reduce target latency
echo 3000000 > /proc/sys/kernel/sched_latency_ns

# Reduce granularity
echo 500000 > /proc/sys/kernel/sched_min_granularity_ns
echo 500000 > /proc/sys/kernel/sched_wakeup_granularity_ns

# Fine-grained throttling
echo 1000 > /proc/sys/kernel/sched_cfs_bandwidth_slice_us

# Prevent migration
echo 2000000 > /proc/sys/kernel/sched_migration_cost_ns
```

**Kubernetes:**
- Use `cpuManagerPolicy: static` for critical pods
- Set `isolcpus` for ultra-low latency
- Consider request-only (no limits) to avoid throttling

---

### Scenario 2: Batch/ML Workloads

**Workload:** Training, data processing, batch jobs

**Symptoms:**
- Low throughput
- Excessive context switches
- Underutilized CPUs

**Tuning:**
```bash
# Increase target latency (less preemption)
echo 24000000 > /proc/sys/kernel/sched_latency_ns

# Increase granularity (longer slices)
echo 2000000 > /proc/sys/kernel/sched_min_granularity_ns
echo 2000000 > /proc/sys/kernel/sched_wakeup_granularity_ns

# Coarse-grained throttling (if using limits)
echo 10000 > /proc/sys/kernel/sched_cfs_bandwidth_slice_us

# Allow migration for load balancing
echo 100000 > /proc/sys/kernel/sched_migration_cost_ns
```

**Kubernetes:**
- Use BestEffort or Burstable QoS
- Consider no CPU limits (let batch jobs use idle CPU)
- Use node taints for dedicated batch nodes

---

### Scenario 3: Multi-Tenant Clusters

**Workload:** Mixed (latency + batch + background)

**Symptoms:**
- Noisy neighbor problems
- Unpredictable performance
- Throttling

**Tuning:**
```bash
# Balanced settings
echo 6000000 > /proc/sys/kernel/sched_latency_ns
echo 750000 > /proc/sys/kernel/sched_min_granularity_ns
echo 1000000 > /proc/sys/kernel/sched_wakeup_granularity_ns
echo 5000 > /proc/sys/kernel/sched_cfs_bandwidth_slice_us
```

**Kubernetes:**
- **Enforce CPU limits** for isolation
- Use separate node pools for different tiers
- Monitor throttling per namespace
- Set ResourceQuotas

---

## Monitoring Scheduler Behavior

### View current settings:
```bash
cat /proc/sys/kernel/sched_latency_ns
cat /proc/sys/kernel/sched_min_granularity_ns
cat /proc/sys/kernel/sched_wakeup_granularity_ns
cat /proc/sys/kernel/sched_cfs_bandwidth_slice_us
```

### Monitor context switches:
```bash
# Node-level
vmstat 1
# cs column = context switches/sec

# Pod-level
cat /proc/<pid>/status | grep voluntary_ctxt_switches
```

### Monitor throttling:
```bash
# cgroup
cat /sys/fs/cgroup/kubepods/cpu.stat

# Prometheus
rate(container_cpu_cfs_throttled_seconds_total[5m]) > 0
```

---

## Making Changes Persistent

### Sysctl Configuration

```bash
# /etc/sysctl.d/99-kubernetes-scheduler.conf
kernel.sched_latency_ns = 3000000
kernel.sched_min_granularity_ns = 500000
kernel.sched_wakeup_granularity_ns = 500000
kernel.sched_cfs_bandwidth_slice_us = 1000
kernel.sched_migration_cost_ns = 2000000

# Apply
sysctl --system
```

### Boot Parameters

```bash
# /etc/default/grub
GRUB_CMDLINE_LINUX="isolcpus=2-7 nohz_full=2-7"

# Update grub
update-grub  # Debian/Ubuntu
grub2-mkconfig -o /boot/grub2/grub.cfg  # RHEL/CentOS
```

---

## References

- [CFS Scheduler Documentation](https://www.kernel.org/doc/html/latest/scheduler/sched-design-CFS.html)
- [cgroup v2 CPU Controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#cpu)
- [Linux Scheduler Parameters](https://www.kernel.org/doc/Documentation/scheduler/sched-design-CFS.txt)
- [CPU Isolation Documentation](https://www.kernel.org/doc/html/latest/admin-guide/kernel-parameters.html)

---

**Next:** See [CUSTOM_SCHEDULERS.md](./CUSTOM_SCHEDULERS.md) for scx (sched-ext) custom scheduler configuration.
