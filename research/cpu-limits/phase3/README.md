# Phase 3: BPF Tracing Implementation

**Status**: Documented manual collection approach (automated privileged container approach blocked by rootless crun limitations)

## Objective

Collect low-level kernel scheduler traces using BPF to observe:
- CPU throttle events (`sched:sched_cfs_quota_throttled`)
- Scheduler context switches
- Runtime distribution
- Correlation with cgroup metrics

## Architecture Challenge: Guest vs. Host Tracing

Since the Kubernetes cluster runs on **cloud-hypervisor VMs** (guest kernels), BPF traces must run **inside the guest**, not on the bare-metal host:

- ✅ **Guest BPF** → sees container processes, pod scheduling (correct)
- ❌ **Host BPF** → sees only hypervisor/KVM processes (incorrect)

## Implementation Approaches

### Approach 1: Privileged DaemonSet (Blocked)

**Status**: ❌ Failed due to rootless crun limitation

Attempted to deploy a privileged DaemonSet with BPF tools, but hit:
```
Error: container create failed: mknotat `/dev/loop0`: Permission denied
```

Rootless crun cannot create device nodes required by privileged containers, even with `hostPID`, `hostNetwork`, and full capabilities.

**Files**:
- `bpf-collector-daemonset.yaml`
- `bpf-runner-pod.yaml`

### Approach 2: Manual Collection via SSH (Implemented)

**Status**: ✅ Working (manual process)

Since privileged containers fail, the working approach is:
1. Deploy workload to cluster
2. Identify which node it runs on
3. SSH into that node
4. Run bpftrace directly on the guest kernel
5. Collect output and correlate with cgroup metrics

**Tools**:
- `collect-guest-bpf.py` — generates step-by-step guide and shell commands
- `manual-bpf-trace.md` — comprehensive manual collection guide

**Example**:
```bash
# Generate collection commands
python3 research/cpu-limits/phase3/collect-guest-bpf.py

# The script outputs:
# 1. Pod info (UID, node, QoS)
# 2. SSH command to node
# 3. Shell commands to run bpftrace
# 4. SCP commands to retrieve results
```

### Approach 3: Systemd Service (Implemented - Rust)

**Status**: ✅ **Recommended approach for production**

High-performance systemd service written in Rust that runs on each node (outside containers):
1. Runs on bare guest kernel (no container runtime limitations)
2. Automatically discovers pods via cgroup filesystem
3. Collects BPF traces and cgroup metrics
4. Exposes Prometheus metrics on HTTP endpoint (port 9101)
5. Low overhead (~10MB RSS, <1% CPU)

**Implementation**: `systemd/rust/`

**Quick Start**:
```bash
cd systemd/rust
./build.sh
sudo ./install-rust.sh
curl http://localhost:9101/metrics
```

See [systemd/rust/README.md](systemd/rust/README.md) for full documentation.

## BPF Trace Scripts

### Throttle Events

Traces `sched:sched_cfs_quota_throttled` tracepoint:
```bpftrace
tracepoint:sched:sched_cfs_quota_throttled {
  printf("%s[%d] throttled for %d us\n",
    args->comm, args->pid, args->throttled_usec);
  @throttle_events++;
  @total_throttled_us += args->throttled_usec;
}
```

**Metrics captured**:
- Throttle event count
- Total throttled time (microseconds)
- Per-process throttle distribution

### Runtime Distribution

Traces `sched:sched_stat_runtime` tracepoint:
```bpftrace
tracepoint:sched:sched_stat_runtime {
  @runtime_us[args->comm] = hist(args->runtime / 1000);
}
```

**Metrics captured**:
- Runtime histogram per process
- Distribution of time slices

### Context Switches

Traces `sched:sched_switch` tracepoint:
```bpftrace
tracepoint:sched:sched_switch {
  if (args->prev_pid == $CONTAINER_PID || args->next_pid == $CONTAINER_PID) {
    printf("%d: %s[%d] -> %s[%d]\n",
      nsecs / 1000000,
      args->prev_comm, args->prev_pid,
      args->next_comm, args->next_pid);
    @switches++;
  }
}
```

**Metrics captured**:
- Context switch count
- Time between switches

## Correlation with Phase 2 Data

Phase 3 BPF traces complement Phase 2 cgroup metrics:

| Metric | Source | Resolution |
|--------|--------|------------|
| `cpu.stat` throttle count | cgroup | 1-10s intervals |
| BPF throttle events | kernel | microsecond precision |
| `cpu.stat` usage_usec | cgroup | cumulative |
| BPF runtime distribution | kernel | per-scheduling-slice |

BPF provides **event-level** granularity that cgroup metrics lack.

## Collection Workflow

### 1. Deploy Workload

```bash
kubectl apply -f research/cpu-limits/phase2/workloads/cpu-bound-burstable.yaml
kubectl wait --for=condition=ready pod/cpu-bound-burstable -n cpu-limits-research
```

### 2. Get Node Info

```bash
python3 research/cpu-limits/phase3/collect-guest-bpf.py

# Outputs:
#   Pod UID: 2dec945a-227a-48e3-850a-fcd3a498cbca
#   Node: k8labs-md-custom-4vcpu-qq498-zlz6p-6n655
#   Node IP: 192.168.124.3
#   Commands saved to: research/cpu-limits/phase3/bpf-traces/collect-cpu-bound-burstable.sh
```

### 3. SSH into Node

```bash
ssh fedora@192.168.124.3
```

### 4. Run BPF Trace

```bash
# Install bpftrace
sudo dnf install -y bpftrace

# Find container PID
CGROUP_PATH="/sys/fs/cgroup/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod2dec945a-227a-48e3-850a-fcd3a498cbca.slice"
CONTAINER_PID=$(cat ${CGROUP_PATH}/cgroup.procs | head -1)

# Run 30-second trace
sudo timeout 30s bpftrace -e '
tracepoint:sched:sched_cfs_quota_throttled {
  printf("%s[%d] throttled for %d us\n",
    args->comm, args->pid, args->throttled_usec);
  @throttle_events++;
  @total_throttled_us += args->throttled_usec;
}
END {
  printf("\nTotal throttle events: %d\n", @throttle_events);
  printf("Total throttled time: %d us\n", @total_throttled_us);
}
' > /tmp/bpf-trace-cpu-bound-burstable.txt
```

### 5. Copy Results

```bash
scp fedora@192.168.124.3:/tmp/bpf-trace-cpu-bound-burstable.txt research/cpu-limits/phase3/bpf-traces/
```

## Next Steps

1. **Manual Collection**: Use the generated commands to collect BPF traces for each workload type
2. **Analysis Script**: Create `analyze-bpf.py` to parse BPF output and correlate with Phase 2 metrics
3. **Visualization**: Add BPF trace timelines to the final report
4. **Automation**: Consider systemd service approach for production environments

## Files

```
research/cpu-limits/phase3/
├── README.md                     # This file
├── collect-guest-bpf.py          # Generate collection commands
├── manual-bpf-trace.md           # Step-by-step manual guide
├── bpf-traces/                   # Collected trace output
│   ├── collect-*.sh              # Generated shell scripts
│   ├── bpf-trace-*.txt           # BPF trace output
│   └── cgroup-stats-*.txt        # Parallel cgroup monitoring
├── bpf-collector-daemonset.yaml  # Failed privileged approach
└── bpf-runner-pod.yaml           # Failed privileged approach
```

## Limitations

- **Manual process**: Cannot automate SSH and sudo commands from Python
- **Node access**: Requires SSH access to Kubernetes nodes
- **Root required**: bpftrace needs root/sudo on nodes
- **Rootless crun**: Privileged containers fail with device node errors
- **Per-workload**: Must manually trace each experiment

## Alternative: eBPF CO-RE

For future work, consider using **libbpf + CO-RE (Compile Once, Run Everywhere)**:
- Compile BPF bytecode once
- Run on any kernel version without recompilation
- Can be embedded in a static binary
- Avoids bpftrace dependency

Example tools:
- `bpftool` for loading programs
- Custom C/Rust program with libbpf
- No Python/bpftrace needed on nodes

This would enable a single binary to be deployed as a DaemonSet (if privileged containers can be fixed) or as a systemd service.
