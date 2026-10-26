# Phase 3 Quick Start Guide

Three approaches for collecting BPF traces from Kubernetes guest VMs. Choose based on your environment.

## Approach Comparison

| Approach | Status | Automation | Performance | Best For |
|----------|--------|------------|-------------|----------|
| 1. Privileged DaemonSet | ❌ Blocked | Full | High | (Blocked by rootless crun) |
| 2. Manual SSH | ✅ Working | Manual | High | One-off experiments |
| 3. Systemd Service (Rust) | ✅ **Recommended** | Full | Highest | Production, continuous monitoring |

---

## Approach 2: Manual Collection (Quick Experiments)

**Use when**: You need traces for a specific workload once.

### Steps

1. Deploy workload:
   ```bash
   kubectl apply -f research/cpu-limits/phase2/workloads/cpu-bound-burstable.yaml
   ```

2. Generate collection commands:
   ```bash
   python3 research/cpu-limits/phase3/collect-guest-bpf.py
   ```

3. Follow the output instructions to SSH into the node and run bpftrace.

**Pros**: No installation, works immediately  
**Cons**: Manual process, not automated

📖 Full guide: [manual-bpf-trace.md](manual-bpf-trace.md)

---

## Approach 3: Systemd Service (Production)

**Use when**: You want continuous monitoring with Prometheus integration.

### Installation (5 minutes)

```bash
# On your local machine
cd research/cpu-limits/phase3/systemd/rust

# Build
./build.sh

# Deploy to all nodes
for node in 192.168.124.2 192.168.124.3 192.168.124.4; do
  scp target/release/k8s-bpf-tracer fedora@$node:/tmp/
  scp install-rust.sh fedora@$node:/tmp/
  ssh fedora@$node "cd /tmp && sudo ./install-rust.sh"
done
```

### Verify

```bash
# Check service on a node
ssh fedora@192.168.124.2 "systemctl status k8s-bpf-tracer"

# Check metrics
curl http://192.168.124.2:9101/metrics | grep throttle
```

### Prometheus Integration

Add to Prometheus config:
```yaml
scrape_configs:
  - job_name: 'k8s-bpf-tracer'
    static_configs:
      - targets: ['192.168.124.2:9101', '192.168.124.3:9101', '192.168.124.4:9101']
    scrape_interval: 10s
```

**Pros**: Fully automated, Prometheus integration, low overhead  
**Cons**: Requires Rust build, one-time setup

📖 Full guide: [systemd/rust/README.md](systemd/rust/README.md)

---

## What Gets Collected

### BPF Traces
- `sched:sched_cfs_quota_throttled` events
- Per-event timestamps, PIDs, throttled duration
- Microsecond precision

### Cgroup Metrics
- `cpu.stat` fields (usage, throttling, bursts)
- 10-second intervals
- Correlated with BPF events by pod UID

---

## Analysis

After collecting traces, analyze them:

```bash
# Approach 2: Analyze manual traces
python3 research/cpu-limits/phase3/analyze-bpf-traces.py --compare

# Approach 3: Query Prometheus
# Throttle ratio by pod
k8s_container_throttle_ratio

# BPF vs cgroup discrepancy
k8s_container_bpf_throttle_events_total - k8s_container_throttled_periods_total
```

---

## Troubleshooting

### bpftrace not found
```bash
sudo dnf install bpftrace kernel-devel
```

### No pods discovered
```bash
# Check kubelet cgroup exists
ls -la /sys/fs/cgroup/kubepods.slice/
```

### BPF tracer fails
```bash
# Test bpftrace manually
sudo bpftrace -e 'tracepoint:sched:sched_cfs_quota_throttled { printf("ok\n"); }'
```

### Service won't start (Approach 3)
```bash
# Check logs
journalctl -u k8s-bpf-tracer -n 50
```

---

## Next Steps

1. **Phase 3 complete**: BPF traces + cgroup metrics collected
2. **Phase 4**: Correlate with Phase 2 evidence, generate final report
3. **Phase 5**: Write comprehensive guide for developers and admins
