# Phase 3: BPF Trace Collection — Deployment Guide

## Overview

This phase deploys BPF trace collectors on Kubernetes nodes to capture low-level scheduler events (context switches, throttling, wakeups, migrations) correlated with pod/container metrics.

**Two approaches available:**

1. **DaemonSet** (recommended for production clusters)
2. **Systemd service** (recommended for single-node lab clusters)

---

## Approach 1: DaemonSet (Multi-Node Clusters)

### Prerequisites

- Kubernetes cluster with kernel ≥ 5.8 (for BPF CO-RE)
- BCC tools installed on nodes (`bpftrace`, `funccount`, `execsnoop`)
- Nodes labeled: `bpf-trace=enabled`

### Quick Start

```bash
cd research/cpu-limits/phase3/daemonset

# Label nodes for BPF tracing
kubectl label nodes <node-name> bpf-trace=enabled

# Deploy the DaemonSet
kubectl apply -f bpf-tracer-daemonset.yaml

# Verify pods are running
kubectl get pods -n kube-system -l app=bpf-tracer

# Check logs
kubectl logs -n kube-system -l app=bpf-tracer --tail=50

# View metrics
kubectl port-forward -n kube-system <pod-name> 9100:9100
curl http://localhost:9100/metrics | grep bpf_
```

### Architecture

```
┌─────────────────────────────────────────┐
│ Kubernetes Node                         │
├─────────────────────────────────────────┤
│ DaemonSet Pod (privileged)              │
│ ├─ BPF Trace Collector                  │
│ │  ├─ Watch cgroup creation             │
│ │  ├─ Attach BPF probes per pod         │
│ │  └─ Parse events → Prometheus metrics │
│ ├─ Prometheus exporter (:9100/metrics)  │
│ └─ Volume mounts:                        │
│    ├─ /sys/fs/cgroup (read cgroup tree) │
│    ├─ /sys/kernel/debug (BPF)           │
│    └─ /var/run/k8s-bpf-traces (output)  │
└─────────────────────────────────────────┘
```

### Configuration

Edit `config.yaml` section in the DaemonSet manifest:

```yaml
data:
  config.yaml: |
    cgroup_root: /sys/fs/cgroup
    output_dir: /var/run/k8s-bpf-traces
    trace_interval: 10s
    metrics_port: 9100
    log_level: info
    bpf_programs:
      - sched:sched_switch
      - cpu:cpu_frequency
```

### Monitoring

Prometheus scrape config:

```yaml
- job_name: 'k8s-bpf-tracer'
  kubernetes_sd_configs:
    - role: pod
      namespaces:
        names: [kube-system]
  relabel_configs:
    - source_labels: [__meta_kubernetes_pod_label_app]
      regex: bpf-tracer
      action: keep
```

---

## Approach 2: Systemd Service (Single-Node Lab)

### Prerequisites

- Rust toolchain (1.75+)
- BCC tools installed
- Kubernetes node access (SSH or local)
- `systemd` init system

### Quick Start

```bash
cd research/cpu-limits/phase3/systemd/rust

# Build the Rust daemon
./build.sh

# Install to system (requires sudo)
sudo ./install.sh

# Verify service status
sudo systemctl status k8s-bpf-tracer

# View logs
sudo journalctl -u k8s-bpf-tracer -f

# Check metrics
curl http://localhost:9101/metrics | grep k8s_bpf
```

### Architecture

```
┌─────────────────────────────────────────┐
│ Kubernetes Node (systemd)               │
├─────────────────────────────────────────┤
│ k8s-bpf-tracer.service                  │
│ ├─ Rust daemon (async Tokio)            │
│ │  ├─ Poll /sys/fs/cgroup every 5s      │
│ │  ├─ Detect new pods → launch BPF      │
│ │  ├─ Parse BPF output → metrics        │
│ │  └─ Export Prometheus (:9101/metrics) │
│ ├─ BPF child processes per pod          │
│ │  ├─ bpftrace (sched_switch)           │
│ │  └─ funccount (throttle events)       │
│ └─ Output: /var/lib/k8s-bpf-tracer/     │
└─────────────────────────────────────────┘
```

### Configuration

Edit `/etc/k8s-bpf-tracer/config.toml`:

```toml
[tracer]
cgroup_root = "/sys/fs/cgroup"
output_dir = "/var/lib/k8s-bpf-tracer"
poll_interval = "5s"
trace_duration = "120s"

[metrics]
listen_addr = "0.0.0.0:9101"

[logging]
level = "info"
format = "json"
```

Restart after changes:

```bash
sudo systemctl restart k8s-bpf-tracer
```

### Uninstall

```bash
cd research/cpu-limits/phase3/systemd/rust
sudo ./uninstall.sh
```

---

## Comparison

| Aspect | DaemonSet | Systemd Service |
|--------|-----------|-----------------|
| **Deployment** | `kubectl apply` | `sudo ./install.sh` |
| **Scope** | Multi-node cluster | Single node |
| **Privileges** | Kubernetes RBAC + privileged pod | Root user |
| **Isolation** | Pod sandbox | Native systemd |
| **Monitoring** | Kubernetes service discovery | Static endpoint |
| **Lifecycle** | Managed by Kubernetes | Managed by systemd |
| **Best for** | Production clusters | Lab/dev single-node |

---

## Verification

After deploying either approach, verify BPF traces are being collected:

### Check Metrics

```bash
# DaemonSet
kubectl port-forward -n kube-system <pod> 9100:9100
curl http://localhost:9100/metrics | grep bpf_context_switch

# Systemd
curl http://localhost:9101/metrics | grep k8s_bpf_context_switch
```

Expected output:

```
k8s_bpf_context_switch_total{pod="cpu-bound-burstable",container="workload",qos="burstable"} 1523
k8s_bpf_throttle_count_total{pod="cpu-bound-burstable",container="workload",qos="burstable"} 42
```

### Check Trace Files

```bash
# DaemonSet
kubectl exec -n kube-system <pod> -- ls -lh /var/run/k8s-bpf-traces/

# Systemd
sudo ls -lh /var/lib/k8s-bpf-tracer/traces/
```

### Correlate with Phase 2

Compare BPF metrics with Phase 2 cgroup metrics:

```bash
cd research/cpu-limits/phase2
python3 analyze-evidence.py

# Look for:
# - High context_switch_total + high nr_throttled → CPU limit too low
# - Low context_switch_total + high cpu_usage → CPU-bound, no throttling
```

---

## Troubleshooting

### DaemonSet pod CrashLoopBackOff

```bash
kubectl logs -n kube-system <pod>

# Common issues:
# 1. BCC not installed on node → install bpftrace/bcc-tools
# 2. Kernel too old → upgrade to ≥5.8
# 3. Seccomp blocked → add seccomp annotations
```

### Systemd service fails to start

```bash
sudo journalctl -u k8s-bpf-tracer -n 50

# Common issues:
# 1. Missing /sys/fs/cgroup → check cgroup v2 mount
# 2. BPF programs fail → verify kernel CONFIG_BPF=y
# 3. Port 9101 in use → change metrics.listen_addr in config
```

### No BPF events captured

```bash
# Verify BPF programs can attach
sudo bpftrace -e 'tracepoint:sched:sched_switch { @[comm] = count(); }'

# Check cgroup paths
ls -la /sys/fs/cgroup/kubepods.slice/

# Verify pod detection
# DaemonSet: kubectl logs -n kube-system <pod> | grep "Detected pod"
# Systemd: sudo journalctl -u k8s-bpf-tracer | grep "Detected pod"
```

---

## Next Steps

1. **Run Phase 2 experiments** while Phase 3 is collecting traces
2. **Analyze correlations** between cgroup metrics and BPF events
3. **Identify throttling patterns** per workload type (CPU-bound, IO-bound, etc.)
4. **Tune CPU limits** based on observed scheduler behavior
5. **Document findings** in `research/cpu-limits/findings.md`

---

## References

- [BPF CO-RE](https://nakryiko.com/posts/bpf-portability-and-co-re/)
- [CFS Bandwidth Control](https://www.kernel.org/doc/html/latest/scheduler/sched-bwc.html)
- [Kubernetes Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
