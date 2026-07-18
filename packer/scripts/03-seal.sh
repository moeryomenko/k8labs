#!/bin/sh
set -eux

# Create directories for extension metadata
mkdir -p /etc/extensions

# Ensure systemd-sysext and systemd-confext are available
systemctl enable systemd-sysext
systemctl enable systemd-confext

# Ensure qemu-guest-agent is running
systemctl enable qemu-guest-agent

# Remove machine-id so each VM gets unique ID on first boot
rm -f /etc/machine-id
touch /etc/machine-id
