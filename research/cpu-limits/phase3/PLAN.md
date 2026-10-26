# Phase 3: BPF Tracing and Real-Time Scheduler Analysis

**Status**: Planning  
**Dependencies**: Phase 2 complete  
**Estimated effort**: 3-4 hours

---

## Objectives

Capture low-level kernel scheduler events to visualize:

1. **Throttling events**: When and why containers get throttled
2. **Scheduling latency**: Time from runnable → running
3. **Context switches**: How often containers lose CPU
4. **CFS period behavior**: Distribution of CPU usage within periods

---

## Tools

### 1. bpftrace

One-liner scripts to capture specific events:

```bash
# Capture throttle events
sudo bpftrace -e '
kprobe:throttle_cfs_rq {
  @throttles[comm] = count();
  printf("%s throttled at %d\n", comm, nsecs);
}'

# Scheduling latency histogram
sudo bpftrace -e '
tracepoint:sched:sched_wakeup {
  @start[args->pid] = nsecs;
}

tracepoint:sched:sched_switch {
  $ns = @start[args->next_pid];
  if ($ns) {
    @latency_ms = hist((nsecs - $ns) / 1000000);
    delete(@start[args->next_pid]);
  }
}'
```

### 2. BCC (BPF Compiler Collection)

Pre-built tools:

- `runqlat`: Scheduling latency
- `cpudist`: CPU usage distribution
- `funclatency`: Function latency (e.g., `throttle_cfs_rq`)

Example:

```bash
sudo /usr/share/bcc/tools/runqlat -m -p $(pidof stress-ng)
sudo /usr/share/bcc/tools/cpudist -p $(pidof stress-ng) 10 1
```

### 3. perf

Traditional profiling:

```bash
# Record scheduling events
sudo perf record -e sched:sched_switch,sched:sched_stat_runtime \
  -p $(pidof stress-ng) sleep 60

# Generate report
sudo perf script > perf-trace.txt

# Flame graph
git clone https://github.com/brendangregg/FlameGraph
sudo perf script | FlameGraph/stackcollapse-perf.pl | \
  FlameGraph/flamegraph.pl > flamegraph.svg
```

---

## Implementation Plan

### Step 1: Install BPF Tools

```bash
# On Fedora/RHEL
sudo dnf install bpftrace bcc-tools kernel-devel

# On Ubuntu/Debian
sudo apt install bpftrace bpfcc-tools linux-headers-$(uname -r)
```

### Step 2: Create Tracing Scripts

Create `research/cpu-limits/phase3/bpf-traces/`:

1. **throttle-events.bt**: Capture throttle_cfs_rq events
2. **sched-latency.bt**: Measure scheduling latency
3. **cpu-distribution.bt**: CPU usage distribution within CFS periods
4. **context-switches.bt**: Count involuntary context switches

### Step 3: Integrate with Experiment Runner

Modify `run-experiments.py` to:

1. Start BPF trace in background before workload
2. Capture PID of stress-ng container process
3. Run workload
4. Stop BPF trace and save output
5. Parse BPF output into structured JSON

Example integration:

```python
def start_bpf_trace(pod_name: str, trace_script: str) -> subprocess.Popen:
    """Start BPF trace for pod's main process."""
    # Get container PID
    result = subprocess.run(
        [
            "kubectl", "exec", pod_name, "-n", NAMESPACE, "--",
            "sh", "-c", "echo $$"
        ],
        capture_output=True, text=True, check=True
    )
    container_pid = result.stdout.strip()
    
    # Find host PID (container PID is in container's PID namespace)
    # Need to map via /proc/<pid>/status or cgroup
    host_pid = get_host_pid_for_container(pod_name)
    
    # Start bpftrace
    proc = subprocess.Popen(
        ["sudo", "bpftrace", trace_script, str(host_pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    return proc


def stop_bpf_trace(proc: subprocess.Popen) -> str:
    """Stop BPF trace and return output."""
    proc.terminate()
    stdout, stderr = proc.communicate(timeout=5)
    return stdout.decode()
```

### Step 4: Visualization

Add BPF trace data to dashboard:

- Throttle event timeline
- Latency histograms
- CPU usage heatmaps

---

## Expected Outputs

For each experiment:

1. **throttle-events.json**: List of throttle events with timestamps
2. **sched-latency.json**: Scheduling latency histogram
3. **cpu-distribution.json**: CPU usage distribution per CFS period
4. **flamegraph.svg**: CPU profiling flame graph

Example `throttle-events.json`:

```json
{
  "experiment_id": "cpu-bound-burstable-20261026T120000Z",
  "events": [
    {
      "timestamp_ns": 1729933200123456789,
      "comm": "stress-ng-cpu",
      "pid": 12345,
      "duration_ns": 19876543
    },
    ...
  ],
  "summary": {
    "total_events": 1100,
    "total_throttled_ms": 23456,
    "avg_duration_ms": 19.8,
    "max_duration_ms": 45.2
  }
}
```

---

## Challenges

### 1. Container PID Mapping

Container PIDs are in a separate PID namespace. Need to map container PID → host PID.

**Solution**: Read `/sys/fs/cgroup/kubepods.slice/.../cgroup.procs` to find host PIDs.

### 2. Rootless Container Runtime

CRI-O runs rootless, so cgroup paths are under user slice:

```
/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/...
```

BPF tools need `sudo` but must target the correct cgroup.

**Solution**: Run BPF tools as root but filter by cgroup path.

### 3. Kernel Symbol Availability

Some kernel functions may not be available depending on kernel config.

**Solution**: Check available kprobes first:

```bash
sudo bpftrace -l 'kprobe:*throttle*'
```

---

## Success Criteria

Phase 3 is complete when:

1. ✅ BPF traces successfully capture throttle events for all QoS classes
2. ✅ Scheduling latency data shows correlation with throttling
3. ✅ Visualizations clearly demonstrate CFS period behavior
4. ✅ Raw BPF output is parsed into structured JSON
5. ✅ Dashboard includes BPF-derived metrics

---

## Timeline

- **Day 1**: Install tools, create trace scripts, test on single workload
- **Day 2**: Integrate with experiment runner, run full suite
- **Day 3**: Parse BPF output, generate visualizations
- **Day 4**: Write analysis and update README.md

---

## References

- [BPF Performance Tools](https://www.brendangregg.com/bpf-performance-tools-book.html)
- [bpftrace Programmer's Guide](https://github.com/iovisor/bpftrace/blob/master/docs/reference_guide.md)
- [Linux Scheduler Tracepoints](https://www.kernel.org/doc/html/latest/scheduler/sched-stats.html)
- [CFS Bandwidth Control](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)
