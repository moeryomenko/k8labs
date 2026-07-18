#!/bin/sh
set -eux

# Enable IP forwarding for Kubernetes networking
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.bridge.bridge-nf-call-iptables=1

# Persist sysctl settings
cat > /etc/sysctl.d/99-kubernetes.conf <<EOF
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
EOF

# Load kernel modules needed for containers
modprobe overlay
modprobe br_netfilter

# Persist module loading
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
