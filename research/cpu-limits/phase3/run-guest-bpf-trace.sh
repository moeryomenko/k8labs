#!/bin/bash
# Run BPF trace directly on a Kubernetes node using kubectl debug
# This creates a privileged ephemeral debug container with host namespaces

set -e

NODE_NAME="${1:-k8labs-xzc6j-0}"
POD_NAME="${2}"
TRACE_SCRIPT="${3:-throttle-events.bt}"
DURATION="${4:-30}"

if [ -z "$POD_NAME" ]; then
  echo "Usage: $0 <node-name> <pod-name> <trace-script> [duration]"
  echo ""
  echo "Example:"
  echo "  $0 k8labs-xzc6j-0 cpu-bound-burstable throttle-events.bt 30"
  exit 1
fi

echo "=== BPF Trace Runner ==="
echo "Node: $NODE_NAME"
echo "Pod: $POD_NAME"
echo "Script: $TRACE_SCRIPT"
echo "Duration: ${DURATION}s"
echo ""

# Get pod UID
echo "Finding pod UID..."
POD_UID=$(kubectl get pod "$POD_NAME" -n cpu-limits-research -o jsonpath='{.metadata.uid}')
echo "Pod UID: $POD_UID"

# Determine QoS class from pod spec
QOS=$(kubectl get pod "$POD_NAME" -n cpu-limits-research -o jsonpath='{.status.qosClass}' | tr '[:upper:]' '[:lower:]')
echo "QoS class: $QOS"

# Create a privileged debug pod on the node
echo ""
echo "Creating debug pod on node..."

# The debug pod will:
# 1. Install bpftrace if needed
# 2. Find the container PID from cgroup
# 3. Run the BPF trace
# 4. Save output

kubectl debug node/"$NODE_NAME" -it --image=fedora:40 -- bash -c "
set -e

echo '=== Inside debug pod on node $NODE_NAME ==='
echo 'Kernel: '\$(uname -r)
echo ''

# Install bpftrace
echo 'Installing bpftrace...'
dnf install -y bpftrace procps-ng 2>&1 | tail -5

# Find container PID
echo ''
echo 'Finding container PID...'
CGROUP_PATH=\"/host/sys/fs/cgroup/kubepods.slice/kubepods-$QOS.slice/kubepods-$QOS-pod$POD_UID.slice\"

if [ ! -d \"\$CGROUP_PATH\" ]; then
  echo 'ERROR: cgroup path not found: '\$CGROUP_PATH
  exit 1
fi

HOST_PID=\$(cat \"\${CGROUP_PATH}/cgroup.procs\" | head -1)
echo \"Host PID: \$HOST_PID\"

if [ -z \"\$HOST_PID\" ]; then
  echo 'ERROR: No PID found in cgroup'
  exit 1
fi

# Verify process
echo ''
ps -p \$HOST_PID -o pid,comm,cmd

# Run BPF trace
echo ''
echo '=== Starting BPF trace for ${DURATION}s ==='
echo ''

TRACE_FILE=\"/tmp/bpf-trace-$POD_NAME-\$(date +%s).txt\"

# Run trace with timeout
timeout ${DURATION}s bpftrace - <<'BPFTRACE_SCRIPT'
#!/usr/bin/env bpftrace

BEGIN {
  printf(\"Tracing CPU throttle events... Hit Ctrl-C to stop.\\n\");
  printf(\"TIME(ms)    COMM             PID      DURATION(us)\\n\");
}

tracepoint:sched:sched_cfs_quota_throttled {
  printf(\"%-11d %-16s %-8d %d\\n\",
    elapsed / 1000000,
    args->comm,
    args->pid,
    args->throttled_usec
  );
  @throttle_events++;
  @total_throttled_us = @total_throttled_us + args->throttled_usec;
}

END {
  printf(\"\\nSummary:\\n\");
  printf(\"  Total throttle events: %d\\n\", @throttle_events);
  printf(\"  Total throttled time: %d us\\n\", @total_throttled_us);
}
BPFTRACE_SCRIPT

echo ''
echo '=== Trace complete ==='
"

echo ""
echo "Debug pod exited. To run again:"
echo "  $0 $NODE_NAME $POD_NAME $TRACE_SCRIPT $DURATION"
