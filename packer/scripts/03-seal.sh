#!/bin/sh
set -eux

# Create directories for extension metadata and the baked extension payloads.
# /var/lib/extensions and /var/lib/confexts are created by
# 01-configure-system.sh before the file-provisioner uploads; re-creating them
# here is belt-and-braces so a sealed image always carries them.
mkdir -p /etc/extensions
mkdir -p /var/lib/extensions /var/lib/confexts

# Deferred sysext/confext merge: the baked images must NOT
# be merged at sysinit. Merging at sysinit renders /etc read-only before
# cloud-init runs, so cloud-init cannot write its NetworkManager connection
# profile and the NIC is never configured (no DHCP lease, wait-ssh fails).
# Fedora Cloud Base ships systemd-sysext/systemd-confext enabled in
# sysinit.target.wants, so explicitly disable them. Instead bake
# k8slab-merge.service, ordered after cloud-init has finished writing /etc,
# which refreshes (merges) the baked images on every boot.
#
# Ordering rationale (verified against Fedora 44 unit files):
#   * cloud-init.target has "After=multi-user.target" on Fedora 44. Ordering
#     k8slab-merge after cloud-init.target while also WantedBy=multi-user.target
#     (or ordering Before=multi-user.target) forms an ordering cycle
#     (multi-user.target -> k8slab-merge -> cloud-init.target -> multi-user.target)
#     and systemd SKIPS the unit entirely — observed twice:
#       - Before=multi-user.target variant (repair attempt 3, 20:44)
#       - After=cloud-init.target + Wants=cloud-init.target variant (20:58
#         bake; scratch boot 21:05: "Ordering cycle found, skipping
#         k8slab-merge.service", sysexts never merged).
#   * cloud-config.service is the LAST cloud-init stage that writes /etc:
#     init-local (NetworkManager profile) runs in cloud-init-local.service and
#     hostname/users/ssh-keys run in the config stage; both complete before
#     cloud-config.service finishes. cloud-final.service only runs runcmd
#     (resize-rootfs.sh, block-device work — no /etc writes).
#   * Therefore ordering After=cloud-config.service + Wants=cloud-config.service
#     guarantees cloud-init's /etc writes complete before the merge makes /etc
#     read-only, with NO ordering cycle. Before=multi-user.target is safe here
#     because cloud-config.service does not depend on multi-user.target.
systemctl disable systemd-sysext systemd-confext

cat > /usr/lib/systemd/system/k8slab-merge.service <<'EOF'
[Unit]
Description=Merge baked sysext and confext images after cloud-init
After=cloud-config.service
Wants=cloud-config.service
Before=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/systemd-sysext refresh
ExecStart=/usr/bin/systemd-confext refresh
ExecStart=/usr/bin/systemctl daemon-reload

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable k8slab-merge

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
