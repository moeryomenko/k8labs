# Manual BPF Trace Collection on Kubernetes Nodes

This guide explains how to collect low-level BPF traces from container processes
running on Kubernetes nodes when privileged containers are not available (rootless crun).

## Prerequisites

- SSH access to Kubernetes nodes
- Root or sudo access on nodes
- bpftrace installed on nodes

## Steps

### 1. Identify Target Pod and Node

```bash
POD_NAME="cpu-bound-burstable"
NODE=$(kubectl get pod $POD_NAME -n cpu-limits-research -o jsonpath='{.spec.nodeName}')
echo "Pod $POD_NAME is running on node: $NODE"
```

### 2. Get Pod UID and QoS Class

```bash
POD_UID=$(kubectl get pod $POD_NAME -n cpu-limits-research -o jsonpath='{.metadata.uid}')
QOS=$(kubectl get pod $POD_NAME -n cpu-limits-research -o jsonpath='{.status.qosClass}' | tr '[:upper:]' '[:lower:]')

echo "Pod UID: $POD_UID"
echo "QoS class: $QOS"
```

### 3. SSH into the Node

```bash
# Assuming nodes are accessible via their internal IP
NODE_IP=$(kubectl get node $NODE -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
ssh fedora@$NODE_IP
```

Or use console access if SSH is not configured.

### 4. Install BPF Tools (if not already installed)

```bash
sudo dnf install -y bpftrace bcc-tools kernel-devel
```

### 5. Find Container Process

```bash
# Use pod UID and QoS class to locate cgroup
POD_UID="2dec945a-227a-48e3-850a-fcd3a498cbca"  # Replace with actual UID
QOS="burstable"  # besteffort, burstable, or guaranteed

# Find cgroup path
CGROUP_PATH="/sys/fs/cgroup/kubepods.slice/kubepods-${QOS}.slice/kubepods-${QOS}-pod${POD_UID}.slice"

# Get main container PID
CONTAINER_PID=$(cat ${CGROUP_PATH}/cgroup.procs | head -1)
echo "Container PID: $CONTAINER_PID"

# Verify
ps -p $CONTAINER_PID -o pid,comm,cmd
```

### 6. Run BPF Trace

#### CPU Throttle Events

```bash
sudo bpftrace -e '
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
'
```

#### Scheduler Context Switches

```bash
sudo bpftrace -e '
tracepoint:sched:sched_switch {
  if (args->prev_pid == '$CONTAINER_PID' || args->next_pid == '$CONTAINER_PID') {
    printf("%d: %s[%d] -> %s[%d]\n",
      nsecs / 1000000,
      args->prev_comm, args->prev_pid,
      args->next_comm, args->next_pid);
    @switches++;
  }
}

END {
  printf("\nTotal switches: %d\n", @switches);
}
'
```

#### CPU Time Distribution

```bash
sudo bpftrace -e '
tracepoint:sched:sched_process_exit /pid == '$CONTAINER_PID'/ {
  printf("Container process exited\n");
}

tracepoint:sched:sched_stat_runtime /args->pid == '$CONTAINER_PID'/ {
  @runtime_us = hist(args->runtime / 1000);
}

END {
  printf("\nRuntime distribution (us):\n");
  print(@runtime_us);
}
'
```

### 7. Collect cgroup Metrics Alongside BPF

Open another terminal on the same node:

```bash
# Monitor throttling stats
watch -n 1 "cat ${CGROUP_PATH}/cpu.stat"

# Or save to file
while true; do
  echo "$(date +%s) $(cat ${CGROUP_PATH}/cpu.stat)"
  sleep 1
done > /tmp/cgroup-stats.txt
```

### 8. Correlate with Kubernetes Metrics

From your local machine:

```bash
# Query Prometheus or metrics-server
kubectl top pod $POD_NAME -n cpu-limits-research --use-protocol-buffers

# Get pod events
kubectl get events -n cpu-limits-research --field-selector involvedObject.name=$POD_NAME
```

## Example: Full Trace Session

```bash
# Terminal 1: Start workload
kubectl apply -f research/cpu-limits/phase2/workloads/cpu-bound-burstable.yaml

# Terminal 2: Run BPF trace (on node)
ssh node-ip
POD_UID=$(kubectl get pod cpu-bound-burstable -n cpu-limits-research -o jsonpath='{.metadata.uid}')
CGROUP_PATH="/sys/fs/cgroup/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod${POD_UID}.slice"
CONTAINER_PID=$(cat ${CGROUP_PATH}/cgroup.procs | head -1)

sudo bpftrace -e 'tracepoint:sched:sched_cfs_quota_throttled { ... }'

# Terminal 3: Monitor cgroup (on node)
watch -n 1 "cat ${CGROUP_PATH}/cpu.stat"

# Terminal 4: Check pod status
kubectl get pod cpu-bound-burstable -n cpu-limits-research -w
```

## Workaround for Rootless Clusters

If you cannot get privileged containers working due to rootless crun limitations,
the manual SSH approach is the most reliable method for collecting guest-level BPF traces.

## Alternative: Host-Level Node Exporter

For automated collection, consider deploying a custom node-exporter that:
1. Runs as a systemd service on each node (outside containers)
2. Collects BPF traces periodically
3. Exposes metrics via HTTP endpoint
4. Gets scraped by Prometheus

This avoids container runtime limitations entirely.
