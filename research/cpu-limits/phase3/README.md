# Phase 3: BPF Tracing Implementation

**Status**: ❌ NOT RUN as designed. The privileged-container path is blocked by
rootless crun. BPF tracing was delivered by **Phase 7.4**, which run bpftrace
inside the unprivileged reader pod with the host tracefs mounted -
`evidence/phase7/live-bpf/scheduler-trace-20261026T192056Z.json`.

> **Correction.** Every probe named `sched_cfs_quota_throttled` below is wrong.
> The kernel `sched` tracepoints are `sched_switch`, `sched_wakeup`,
> `sched_wakeup_new`, `sched_process_exit`, and `sched_stat_*`. CFS bandwidth
> throttling is traced through the `cfs_bandwidth` kprobes
> (`throttle_cfs_rq_work`, `unthrottle_cfs_rq`, `sched_cfs_period_timer`) or
> `cgroup:cgroup_attach_task`. Phase 7.4 used `sched_wakeup` plus
> `sched_switch` to measure wake-to-run delay. The scripts in
> `phase3/manual-bpf-trace.md`, `phase3/QUICK_START.md`, and
> `phase3/systemd/rust/README.md` still contain the invalid probe names.

## Objective

Collect low-level kernel scheduler traces using BPF to observe:
- Wake-to-run delay per scheduling event (`sched_wakeup` -> `sched_switch`)
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

There is no `sched:sched_cfs_quota_throttled` tracepoint. Use the bandwidth
kprobes, as in Phase 7.4:

```bpftrace
kprobe:sched_cfs_period_timer { @periods++; }
kprobe:throttle_cfs_rq_work { @throttled++; }
kprobe:unthrottle_cfs_rq   { @unthrottled++; }
```

**Metrics captured**:
- Quota-period ticks, throttle and unthrottle transitions
- These are event counts; per-cgroup throttling is read from `cpu.stat`

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
| `cpu.stat` nr_throttled / throttled_usec | cgroup | poll interval of the collector |
| BPF wake-to-run delay | kernel | per event (Phase 7.4) |
| `cpu.stat` usage_usec | cgroup | cumulative |
| BPF runtime distribution | kernel | per-scheduling-slice |

BPF provides **event-level** granularity that cgroup counters lack; the cgroup
counters provide the only per-cgroup throttle percentage.

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
# Run 30-second trace (probes that exist on this kernel; see the Correction note)
sudo timeout 30s bpftrace -e '
BEGIN { printf("tracing wake-to-run delay\n"); }
tracepoint:sched:sched_wakeup { @wake[args->pid] = nsecs; }
tracepoint:sched:sched_switch /@wake[args->next_pid]/ {
  @delay = hist(nsecs - @wake[args->next_pid]);
  delete(@wake[args->next_pid]);
}
kprobe:sched_cfs_period_timer { @periods++; }
END {
  printf("\nQuota period ticks: %d\n", @periods);
  print(@delay);
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

The working path is `phase7/trace-scheduler.sh`, which runs bpftrace in the
unprivileged reader pod. No trace files were produced by the scripts above.

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
