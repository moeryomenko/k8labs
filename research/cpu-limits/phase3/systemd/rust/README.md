# k8s-bpf-tracer: Rust Implementation

High-performance systemd service for collecting CPU throttle events and cgroup metrics from Kubernetes containers using BPF tracing.

## Architecture

- **Language**: Rust (async/tokio)
- **BPF**: bpftrace for kernel tracing
- **Metrics**: Prometheus text format on HTTP endpoint
- **Deployment**: systemd service (outside container runtime)

## Features

- ✅ Discovers pods automatically via cgroup filesystem
- ✅ Attaches BPF throttle event tracer
- ✅ Collects cgroup cpu.stat metrics periodically
- ✅ Exposes Prometheus metrics on port 9101
- ✅ No privileged container required
- ✅ Low memory footprint (~10MB RSS)
- ✅ Async I/O for efficiency
- ✅ Automatic pod lifecycle tracking

## Metrics Exposed

### Cgroup Metrics (from cpu.stat)

- `k8s_container_cpu_usage_usec` - Total CPU usage in microseconds
- `k8s_container_cpu_user_usec` - User mode CPU time
- `k8s_container_cpu_system_usec` - System mode CPU time
- `k8s_container_throttled_periods_total` - Number of throttled periods
- `k8s_container_throttled_usec_total` - Total throttled time in microseconds
- `k8s_container_throttle_ratio` - Ratio of throttled time to total time (gauge)
- `k8s_container_burst_periods_total` - Number of CPU burst periods (if available)

### BPF Metrics (from kernel tracing)

- `k8s_container_bpf_throttle_events_total` - BPF-traced throttle events
- `k8s_container_bpf_throttled_usec_total` - Total BPF-traced throttled time

### Labels

All metrics include:
- `pod_uid` - Kubernetes pod UID
- `qos` - QoS class (besteffort, burstable, guaranteed)

## Building

### Prerequisites

- Rust 1.70+ (`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`)
- Linux kernel 4.9+ with BPF support

### Build

```bash
cd research/cpu-limits/phase3/systemd/rust
./build.sh
```

Binary will be at `target/release/k8s-bpf-tracer`.

**Binary size**: ~5MB (stripped, with LTO)

## Installation

### Single Node

```bash
# Build
./build.sh

# Install (requires root)
sudo ./install-rust.sh
```

### Multiple Nodes

```bash
# Build locally
./build.sh

# Deploy to all nodes
for node in 192.168.124.2 192.168.124.3 192.168.124.4; do
  echo "Installing on $node..."
  scp target/release/k8s-bpf-tracer fedora@$node:/tmp/
  ssh fedora@$node "sudo mv /tmp/k8s-bpf-tracer /usr/local/bin/ && \
                     sudo chmod +x /usr/local/bin/k8s-bpf-tracer"
  
  # Install systemd service
  ssh fedora@$node "sudo tee /etc/systemd/system/k8s-bpf-tracer.service" < k8s-bpf-tracer.service
  
  # Enable and start
  ssh fedora@$node "sudo systemctl daemon-reload && \
                     sudo systemctl enable --now k8s-bpf-tracer"
done
```

## Usage

### Service Management

```bash
# Status
systemctl status k8s-bpf-tracer

# Logs (follow)
journalctl -u k8s-bpf-tracer -f

# Restart
systemctl restart k8s-bpf-tracer

# Stop
systemctl stop k8s-bpf-tracer
```

### Metrics Endpoint

```bash
# Health check
curl http://localhost:9101/health

# Prometheus metrics
curl http://localhost:9101/metrics

# Watch live metrics
watch -n 1 "curl -s http://localhost:9101/metrics | grep throttle"
```

### Example Metrics Output

```
# HELP k8s_container_cpu_usage_usec Total CPU usage in microseconds
# TYPE k8s_container_cpu_usage_usec counter
k8s_container_cpu_usage_usec{pod_uid="2dec945a-227a-48e3-850a-fcd3a498cbca",qos="burstable"} 120456789

# HELP k8s_container_throttled_periods_total Number of throttled periods
# TYPE k8s_container_throttled_periods_total counter
k8s_container_throttled_periods_total{pod_uid="2dec945a-227a-48e3-850a-fcd3a498cbca",qos="burstable"} 245

# HELP k8s_container_throttled_usec_total Total throttled time in microseconds
# TYPE k8s_container_throttled_usec_total counter
k8s_container_throttled_usec_total{pod_uid="2dec945a-227a-48e3-850a-fcd3a498cbca",qos="burstable"} 48000000

# HELP k8s_container_throttle_ratio Ratio of throttled time to total time
# TYPE k8s_container_throttle_ratio gauge
k8s_container_throttle_ratio{pod_uid="2dec945a-227a-48e3-850a-fcd3a498cbca",qos="burstable"} 0.285714

# HELP k8s_container_bpf_throttle_events_total BPF-traced throttle events
# TYPE k8s_container_bpf_throttle_events_total counter
k8s_container_bpf_throttle_events_total{pod_uid="2dec945a-227a-48e3-850a-fcd3a498cbca",qos="burstable"} 247
```

## Prometheus Integration

Add this to your Prometheus configuration:

```yaml
scrape_configs:
  - job_name: 'k8s-bpf-tracer'
    static_configs:
      - targets:
          - '192.168.124.2:9101'
          - '192.168.124.3:9101'
          - '192.168.124.4:9101'
    scrape_interval: 10s
```

### Example PromQL Queries

```promql
# Throttle ratio by pod
k8s_container_throttle_ratio

# Throttled time rate (per second)
rate(k8s_container_throttled_usec_total[1m]) / 1000000

# BPF vs cgroup throttle event discrepancy
k8s_container_bpf_throttle_events_total - k8s_container_throttled_periods_total

# CPU usage rate (cores)
rate(k8s_container_cpu_usage_usec[1m]) / 1000000

# Pods being throttled (>10% throttle ratio)
k8s_container_throttle_ratio > 0.1
```

## Performance

**Resource usage** (measured on Fedora 40, 4-core node):

- Memory: ~8-12 MB RSS
- CPU: <1% (idle), ~2-3% (active collection)
- Disk: ~100 KB/day logs

**Overhead**:

- Pod discovery: negligible (filesystem scan every 5s)
- Cgroup metrics: negligible (read ~10 files every 10s)
- BPF tracing: <0.1% CPU overhead

## Comparison: Python vs Rust

| Metric | Python (original) | Rust |
|--------|-------------------|------|
| Binary size | N/A (script) | 5 MB |
| Memory (RSS) | ~50 MB | ~10 MB |
| Startup time | ~1s | <100ms |
| Dependencies | Python 3 | None (static) |
| Async I/O | asyncio | tokio |
| Type safety | Runtime | Compile-time |

## Troubleshooting

### Service fails to start

```bash
# Check logs
journalctl -u k8s-bpf-tracer -n 100

# Common issues:
# 1. bpftrace not installed
sudo dnf install bpftrace

# 2. Insufficient permissions
# Service must run as root

# 3. Port 9101 already in use
sudo lsof -i :9101
```

### No BPF events appearing

```bash
# Check if BPF tracing is working
sudo bpftrace -e 'tracepoint:sched:sched_cfs_quota_throttled { printf("event\n"); }'

# Deploy a workload that will throttle
kubectl apply -f research/cpu-limits/phase2/workloads/cpu-bound-burstable.yaml

# Check metrics
curl -s http://localhost:9101/metrics | grep bpf_throttle
```

### No pods discovered

```bash
# Check if kubelet cgroup exists
ls -la /sys/fs/cgroup/kubepods.slice/

# Check service logs for discovery
journalctl -u k8s-bpf-tracer -f | grep "Discovered pod"
```

## Development

### Run in development mode

```bash
# With debug logging
RUST_LOG=debug cargo run

# With custom port
cargo run -- --port 9102

# Without BPF (testing cgroup collection only)
cargo run -- --no-bpf
```

### Run tests

```bash
cargo test
```

### Check for updates

```bash
cargo outdated
cargo update
```

## Files

```
research/cpu-limits/phase3/systemd/rust/
├── Cargo.toml                  # Rust dependencies
├── src/
│   ├── main.rs                 # Main application and HTTP server
│   ├── bpf.rs                  # BPF tracing
│   ├── cgroup.rs               # Cgroup metrics reading
│   └── metrics.rs              # Metrics registry and Prometheus rendering
├── build.sh                    # Build script
├── install-rust.sh             # Installation script
└── README.md                   # This file
```

## Security

- Runs as `root` (required for BPF tracing)
- Systemd security hardening applied:
  - `NoNewPrivileges=true`
  - `ProtectSystem=strict`
  - `ProtectHome=true`
  - `PrivateTmp=true`
- No network access except HTTP server
- Read-only access to cgroup filesystem
- Memory limit: 256 MB

## License

Same as parent project (k8labs).
