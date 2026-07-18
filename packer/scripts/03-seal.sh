#!/bin/sh
set -eux

# Create directories for extension metadata
mkdir -p /etc/extensions

# Ensure systemd-sysext and systemd-confext are available
systemctl enable systemd-sysext
systemctl enable systemd-confext

# Ensure qemu-guest-agent is running (service may be named differently per distro)
systemctl enable qemu-guest-agent 2>/dev/null || systemctl enable qemu-ga 2>/dev/null || true

# Remove machine-id so each VM gets unique ID on first boot
rm -f /etc/machine-id
touch /etc/machine-id
