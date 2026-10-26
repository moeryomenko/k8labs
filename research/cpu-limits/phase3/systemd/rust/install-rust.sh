#!/bin/bash
# Install k8s-bpf-tracer Rust version as systemd service on Kubernetes nodes
#
# Usage:
#   # Build first
#   ./build.sh
#
#   # Install on current machine (node)
#   sudo ./install-rust.sh
#
#   # Install on remote nodes
#   for node in node1 node2 node3; do
#     scp target/release/k8s-bpf-tracer fedora@$node:/tmp/
#     ssh fedora@$node "sudo mv /tmp/k8s-bpf-tracer /usr/local/bin/ && sudo chmod +x /usr/local/bin/k8s-bpf-tracer"
#   done

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

error() {
    echo -e "${RED}[ERROR]${NC} $*"
    exit 1
}

# Check root
if [ "$EUID" -ne 0 ]; then
    error "Please run as root (sudo ./install-rust.sh)"
fi

# Check if binary exists
BINARY="target/release/k8s-bpf-tracer"
if [ ! -f "$BINARY" ]; then
    error "Binary not found: $BINARY. Run ./build.sh first"
fi

# Check bpftrace
info "Checking bpftrace..."
if ! command -v bpftrace &> /dev/null; then
    warn "bpftrace not found, installing..."
    dnf install -y bpftrace kernel-devel || error "Failed to install bpftrace"
fi

BPFTRACE_VERSION=$(bpftrace --version 2>&1 | head -1)
info "bpftrace found: $BPFTRACE_VERSION"

# Create directories
info "Creating directories..."
mkdir -p /usr/local/bin
mkdir -p /var/lib/k8s-bpf-tracer
mkdir -p /etc/systemd/system

# Install binary
info "Installing binary..."
cp "$BINARY" /usr/local/bin/k8s-bpf-tracer
chmod +x /usr/local/bin/k8s-bpf-tracer

# Verify binary
BIN_VERSION=$(/usr/local/bin/k8s-bpf-tracer --version 2>&1 || echo "unknown")
info "Installed version: $BIN_VERSION"

# Create systemd service
info "Creating systemd service..."
cat > /etc/systemd/system/k8s-bpf-tracer.service <<'EOF'
[Unit]
Description=Kubernetes Container BPF Tracer (Rust)
Documentation=https://github.com/k8labs/cpu-limits-research
After=network.target kubelet.service
Wants=kubelet.service

[Service]
Type=simple
ExecStart=/usr/local/bin/k8s-bpf-tracer --port 9101 --interval 10
Restart=always
RestartSec=10
User=root

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=k8s-bpf-tracer

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/k8s-bpf-tracer
PrivateTmp=true

# Resource limits
MemoryMax=256M
TasksMax=20

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd
info "Reloading systemd..."
systemctl daemon-reload

# Enable service
info "Enabling service..."
systemctl enable k8s-bpf-tracer.service

# Start service
info "Starting service..."
systemctl start k8s-bpf-tracer.service

# Wait for startup
sleep 3

# Check status
info "Checking service status..."
if systemctl is-active --quiet k8s-bpf-tracer.service; then
    info "Service is running"
    systemctl status k8s-bpf-tracer.service --no-pager | head -20
else
    error "Service failed to start. Check logs: journalctl -u k8s-bpf-tracer.service"
fi

# Check metrics endpoint
info "Checking metrics endpoint..."
sleep 2
if curl -s http://localhost:9101/health > /dev/null 2>&1; then
    info "Metrics endpoint is responding"
    echo ""
    info "Metrics available at: http://$(hostname -I | awk '{print $1}'):9101/metrics"
else
    warn "Metrics endpoint not responding yet (may still be starting)"
fi

echo ""
info "Installation complete!"
echo ""
echo "Binary: /usr/local/bin/k8s-bpf-tracer"
echo "Service: k8s-bpf-tracer.service"
echo "State: /var/lib/k8s-bpf-tracer"
echo ""
echo "Commands:"
echo "  Status:  systemctl status k8s-bpf-tracer"
echo "  Logs:    journalctl -u k8s-bpf-tracer -f"
echo "  Stop:    systemctl stop k8s-bpf-tracer"
echo "  Restart: systemctl restart k8s-bpf-tracer"
echo "  Metrics: curl http://localhost:9101/metrics"
echo "  Health:  curl http://localhost:9101/health"
