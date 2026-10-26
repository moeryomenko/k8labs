#!/bin/bash
# Install k8s-bpf-tracer systemd service on Kubernetes nodes
#
# Usage:
#   # Install on current machine (node)
#   sudo ./install.sh
#
#   # Install on remote nodes
#   for node in node1 node2 node3; do
#     scp install.sh k8s-bpf-tracer.py bpf-tracer.service fedora@$node:/tmp/
#     ssh fedora@$node "cd /tmp && sudo ./install.sh"
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
    error "Please run as root (sudo ./install.sh)"
fi

# Check bpftrace
info "Checking bpftrace..."
if ! command -v bpftrace &> /dev/null; then
    warn "bpftrace not found, installing..."
    dnf install -y bpftrace kernel-devel || error "Failed to install bpftrace"
fi

BPFTRACE_VERSION=$(bpftrace --version 2>&1 | head -1)
info "bpftrace found: $BPFTRACE_VERSION"

# Check Python
info "Checking Python..."
if ! command -v python3 &> /dev/null; then
    error "Python 3 not found"
fi

PYTHON_VERSION=$(python3 --version)
info "Python found: $PYTHON_VERSION"

# Create directories
info "Creating directories..."
mkdir -p /usr/local/bin
mkdir -p /var/lib/k8s-bpf-tracer
mkdir -p /etc/systemd/system

# Install tracer script
info "Installing tracer script..."
cp k8s-bpf-tracer.py /usr/local/bin/k8s-bpf-tracer
chmod +x /usr/local/bin/k8s-bpf-tracer

# Install systemd service
info "Installing systemd service..."
cp bpf-tracer.service /etc/systemd/system/

# Reload systemd
info "Reloading systemd..."
systemctl daemon-reload

# Enable service
info "Enabling service..."
systemctl enable bpf-tracer.service

# Start service
info "Starting service..."
systemctl start bpf-tracer.service

# Wait for startup
sleep 2

# Check status
info "Checking service status..."
if systemctl is-active --quiet bpf-tracer.service; then
    info "Service is running"
    systemctl status bpf-tracer.service --no-pager | head -15
else
    error "Service failed to start. Check logs: journalctl -u bpf-tracer.service"
fi

# Check metrics endpoint
info "Checking metrics endpoint..."
if curl -s http://localhost:9101/health > /dev/null; then
    info "Metrics endpoint is responding"
    echo ""
    info "Metrics available at: http://$(hostname -I | awk '{print $1}'):9101/metrics"
else
    warn "Metrics endpoint not responding yet (may still be starting)"
fi

echo ""
info "Installation complete!"
echo ""
echo "Commands:"
echo "  Status:  systemctl status bpf-tracer"
echo "  Logs:    journalctl -u bpf-tracer -f"
echo "  Stop:    systemctl stop bpf-tracer"
echo "  Restart: systemctl restart bpf-tracer"
echo "  Metrics: curl http://localhost:9101/metrics"
