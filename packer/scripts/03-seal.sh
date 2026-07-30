#!/bin/sh
set -eux

# Create directories for extension metadata
mkdir -p /etc/extensions

# Ensure systemd-sysext and systemd-confext are available
systemctl enable systemd-sysext
systemctl enable systemd-confext

# Cloud-Hypervisor uses ACPI for graceful shutdown — no guest agent needed.
# Unlike the QEMU/libvirt variant, no qemu-guest-agent is installed or enabled.

# Remove machine-id so each VM gets unique ID on first boot
rm -f /etc/machine-id
touch /etc/machine-id
