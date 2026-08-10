#!/bin/sh
set -eux

# Create directories for extension metadata and the baked extension payloads.
# /var/lib/extensions and /var/lib/confexts are created by
# 01-configure-system.sh before the file-provisioner uploads; re-creating them
# here is belt-and-braces so a sealed image always carries them.
mkdir -p /etc/extensions
mkdir -p /var/lib/extensions /var/lib/confexts

# Ensure systemd-sysext and systemd-confext are available
systemctl enable systemd-sysext
systemctl enable systemd-confext

# Load the baked SELinux ebpf-fix policy module into the policy store. The .pp
# was uploaded to /root/ebpf-fix.pp by a Packer file provisioner that runs
# before this script; semodule makes the module persistent in the image
# (bake-time).
semodule -i /root/ebpf-fix.pp

# Make the baked first-boot resize helper executable. The file
# provisioner preserves the source mode, but the exec bit is load-bearing so
# it is asserted explicitly.
chmod +x /usr/local/sbin/resize-rootfs.sh

# Cloud-Hypervisor uses ACPI for graceful shutdown — no guest agent needed.
# Unlike the QEMU/libvirt variant, no qemu-guest-agent is installed or enabled.

# Remove machine-id so each VM gets unique ID on first boot
rm -f /etc/machine-id
touch /etc/machine-id
