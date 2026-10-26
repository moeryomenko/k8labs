# Manual BPF Tracing Guide

This guide describes how to run BPF traces manually on the host system to capture low-level scheduler behavior during CPU limits experiments.

## Prerequisites

Install BPF tools on the host (not in containers):

```bash
# Fedora/RHEL
sudo dnf install bpftrace bcc-tools kernel-devel

# Ubuntu/Debian  
sudo apt install bpftrace bpfcc-tools linux-headers-$(uname -r)
```

Verify installation:

```bash
sudo bpftrace --version
sudo bpftrace -l 'tracepoint:sched:*' | head
```

## Finding the Container PID

Before running BPF traces, you need the **host PID** of the workload process:

```bash
# 1. Get pod UID
POD_NAME="cpu-bound-burstable"
POD_UID=$(kubectl get pod $POD_NAME -n cpu-limits-research -o jsonpath='{.metadata.uid}')

# 2. Find cgroup.procs file
# Path depends on QoS class (besteffort, burstable, or guaranteed)
for QOS in besteffort burstable guaranteed; do
  CGROUP_PATH="/sys/fs/cgroup/kubepods.slice/kubepods-${QOS}.slice/kubepods-${QOS}-pod${POD_UID}.slice"
  if [ -d "$CGROUP_PATH" ]; then
    echo "Found cgroup: $CGROUP_PATH"
    # Get main container PID
    HOST_PID=$(cat ${CGROUP_PATH}/cgroup.procs | head -1)
    echo "Host PID: $HOST_PID"
    break
  fi
done
```

Or use the helper script:

```bash
python3 research/cpu-limits/phase3/bpf-collector-simple.py $POD_NAME test
```

## Running BPF Traces

All scripts are in `research/cpu-limits/phase3/bpf-traces/`.

### 1. Throttle Events

Captures CFS bandwidth control throttle/unthrottle events:

```bash
HOST_PID=12345  # Replace with actual PID

sudo bpftrace research/cpu-limits/phase3/bpf-traces/throttle-events.bt
```

**Expected output**:

```
TIME(ms)    COMM             PID      DURATION(us)
1234        stress-ng-cpu    12345    18765
1334        stress-ng-cpu    12345    19234
...

Summary:
  Total throttle events: 1200
  Total throttled time: 23456789 us
```

### 2. Scheduling Latency

Measures time from wakeup to running (run queue latency):

```bash
HOST_PID=12345

sudo bpftrace research/cpu-limits/phase3/bpf-traces/sched-latency.bt $HOST_PID
```

**Expected output**:

```
Scheduling Latency Distribution (microseconds):
[0]                  1234 |@@@@@@@@                                        |
[1]                  2345 |@@@@@@@@@@@@@@@@@@@@                            |
[2, 4)               3456 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@            |
[4, 8)               5678 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|
[8, 16)              2345 |@@@@@@@@@@@@@@@@@@@@                            |
...

Summary:
  Total scheduling events: 15000
  Average latency: 4 us
```

### 3. Context Switches

Counts voluntary (blocking) vs involuntary (preempted) context switches:

```bash
HOST_PID=12345

sudo bpftrace research/cpu-limits/phase3/bpf-traces/context-switches.bt $HOST_PID
```

**Expected output**:

```
Context Switch Summary:
  Voluntary switches (blocking): 234
  Involuntary switches (preempted): 12456
  Switches in (scheduled): 12690
  Total switches out: 12690
```

### 4. CPU Distribution

Shows CPU runtime distribution per scheduling event:

```bash
HOST_PID=12345

sudo bpftrace research/cpu-limits/phase3/bpf-traces/cpu-distribution.bt $HOST_PID
```

## Running During Experiment

To collect BPF traces during a workload run:

```bash
# Terminal 1: Start workload
kubectl apply -f research/cpu-limits/phase1/workloads/cpu-bound-burstable.yaml
kubectl wait --for=condition=ready pod/cpu-bound-burstable -n cpu-limits-research

# Terminal 2: Find PID and start traces
POD_UID=$(kubectl get pod cpu-bound-burstable -n cpu-limits-research -o jsonpath='{.metadata.uid}')
HOST_PID=$(cat /sys/fs/cgroup/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod${POD_UID}.slice/cgroup.procs | head -1)

# Run trace (Ctrl-C after workload completes)
sudo bpftrace research/cpu-limits/phase3/bpf-traces/throttle-events.bt > throttle-trace.txt

# Terminal 1: Wait for completion
kubectl logs -f cpu-bound-burstable -n cpu-limits-research
```

## Parsing Output

Save the BPF trace output to a file and parse it:

```python
import re

def parse_throttle_trace(filename):
    with open(filename) as f:
        content = f.read()
    
    events = []
    pattern = re.compile(r'(\d+)\s+(\S+)\s+(\d+)\s+(\d+)')
    for match in pattern.finditer(content):
        events.append({
            'timestamp_ms': int(match.group(1)),
            'comm': match.group(2),
            'pid': int(match.group(3)),
            'duration_us': int(match.group(4))
        })
    
    return events

events = parse_throttle_trace('throttle-trace.txt')
total_throttled_ms = sum(e['duration_us'] for e in events) / 1000
print(f"Total throttled: {total_throttled_ms:.1f} ms")
print(f"Events: {len(events)}")
```

## Automated Collection (TODO)

For fully automated collection, we need:

1. **Privileged DaemonSet** with BPF tools (blocked by rootless crun device node issue)
2. **Host-based collector** that runs outside Kubernetes
3. **Integration with run-experiments.py** to launch traces automatically

Current status: Manual execution required due to privilege/security constraints.

## Troubleshooting

### Permission Denied

BPF requires root:

```bash
sudo bpftrace ...
```

### Kernel Symbols Not Found

Check available kprobes:

```bash
sudo bpftrace -l 'kprobe:*throttle*'
```

If `throttle_cfs_rq` is not available, your kernel may not have it compiled in or exposed.

### No Output

1. Check PID is correct: `ps -p $HOST_PID`
2. Verify process is running: `cat /proc/$HOST_PID/status`
3. Check cgroup path: `cat /proc/$HOST_PID/cgroup`

### Container Exits Too Fast

BPF traces take a few seconds to attach. For short workloads (<10s), you may miss events. Use longer-running workloads (120s) for BPF tracing.

## References

- [bpftrace Reference Guide](https://github.com/iovisor/bpftrace/blob/master/docs/reference_guide.md)
- [Linux Scheduler Tracepoints](https://www.kernel.org/doc/html/latest/scheduler/sched-stats.html)
- [BPF Performance Tools Book](https://www.brendangregg.com/bpf-performance-tools-book.html)
