# Phase 2: Workload Experiments

This phase executes controlled workloads to measure how Kubernetes CPU requests and limits affect cgroup v2 parameters and scheduler behavior.

## Experiment Design

### Workload Types

1. **CPU-bound** (`stress-ng --cpu`)
   - Pure computation (Ackermann function)
   - Expected to hit cpu.max limits
   - High user CPU time
   - Shows throttling impact on compute tasks

2. **I/O-bound** (`stress-ng --io`)
   - Disk I/O operations
   - Low CPU usage, high iowait
   - Demonstrates that CPU limits don't throttle I/O wait time
   - Tasks spend time in D state (uninterruptible sleep)

3. **Memory-bound** (`stress-ng --vm`)
   - Memory access patterns (flip method)
   - Moderate CPU for memory operations
   - Cache misses and memory bandwidth as bottleneck
   - Shows CPU limits don't account for memory latency

4. **Hybrid** (mix of CPU, I/O, and memory)
   - Realistic application behavior
   - Variable CPU usage patterns
   - Demonstrates interaction between workload phases and CPU limits

### QoS Classes

Each workload type is tested with three QoS classes:

1. **Guaranteed** (requests == limits)
   - `cpu.weight` derived from request
   - `cpu.max` enforces hard limit
   - Predictable behavior, isolated from other workloads

2. **Burstable** (requests < limits)
   - `cpu.weight` provides baseline share
   - `cpu.max` allows burst to limit
   - Can use less than request when idle, throttled at limit

3. **BestEffort** (no requests or limits)
   - `cpu.weight = 1` (lowest priority)
   - `cpu.max = max` (no hard limit)
   - Gets remaining CPU after Guaranteed and Burstable

### Contention Scenario

Multiple workloads running concurrently to demonstrate:
- CPU share distribution under contention
- Throttling behavior when total demand exceeds capacity
- QoS class prioritization by cgroup v2 CPU controller

## Evidence Collection

For each experiment, we collect:

### Pre-run Baseline
- Container cgroup parameters (cpu.weight, cpu.max)
- Container CPU usage (usage_usec, nr_throttled)
- Node aggregate CPU usage (/proc/stat)

### During Run
- BPF scheduler traces (runqlat, runqlen, sched_switch)
- Workload output and metrics
- Real-time cgroup stat updates

### Post-run Measurements
- Final cgroup parameters and usage
- Accumulated throttling (nr_throttled, throttled_usec)
- CPU pressure (PSI avg10/60/300)
- Workload logs and exit code

### BPF Traces

1. **runqlat** - Run queue latency histogram
   - Time tasks spend waiting in scheduler run queue
   - Shows scheduling latency impact of CPU limits

2. **runqlen** - Run queue length over time
   - Number of tasks waiting to run
   - Indicates scheduler contention

3. **sched_switch** - Context switch events
   - Task state transitions (R → D, D → R)
   - Shows when tasks are throttled or wake up

## Running Experiments

```bash
# Validate environment first
python3 research/cpu-limits/phase1/gate-environment.py

# Run all Phase 2 experiments
python3 research/cpu-limits/phase2/run-experiments.py

# Or run individual workloads
kubectl apply -f research/cpu-limits/phase2/workloads/cpu-bound-guaranteed.yaml
kubectl logs -n cpu-limits-research cpu-bound-guaranteed -f
```

## Expected Results

### CPU-bound Guaranteed (2 CPUs)
- Should use ~200000μs per 100000μs period
- Throttled when attempting >2 CPUs
- cpu.stat shows `nr_throttled > 0`

### CPU-bound Burstable (500m request, 2 CPU limit)
- Can use <500m without throttling when node idle
- Gets throttled at 2 CPU limit
- Under contention, gets share proportional to cpu.weight

### CPU-bound BestEffort
- Can consume all available CPU when node idle
- Gets lowest share under contention (cpu.weight = 1)
- No throttling (cpu.max = max)

### I/O-bound
- Low CPU usage regardless of limit
- Most time in iowait (not throttled)
- Shows CPU limits don't affect I/O wait time

### Memory-bound
- Moderate CPU usage
- Cache misses dominate performance, not CPU throttling
- Shows CPU limits don't account for memory access latency

### Hybrid
- Variable CPU usage across workload phases
- Demonstrates realistic application behavior
- CPU phase gets throttled, I/O phase does not

### Contention
- Guaranteed gets full quota
- Burstable gets share based on cpu.weight
- BestEffort gets remainder
- Total CPU usage ≤ 4 CPUs (node capacity)

## Analysis

Evidence files in `phase2/evidence/` will be analyzed in Phase 3 to:
- Correlate Kubernetes resources with cgroup parameters
- Measure throttling impact on different workload types
- Compare QoS classes under contention
- Generate visual representations of scheduler behavior
- Produce recommendations for setting requests/limits
