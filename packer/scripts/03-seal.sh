#!/bin/sh
# shellcheck shell=sh # POSIX sh script; declare the real shell so ShellCheck
#                     # applies the sh ruleset (repo .shellcheckrc defaults to
#                     # bash; [[ ]] is not POSIX).
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

# Load the baked SELinux policy modules into the policy store. The .pp files
# were uploaded to /root/ by Packer file provisioners that run before this
# script; semodule makes the modules persistent in the image.
# A module is only "loaded" once it appears in `semodule --list` (the active
# policy store). libsemanage's commit can be interrupted transiently (observed
# in bakes: the module lands in the tmp staging store and the policy binary
# carries its rules, but the active store listing is never finalized), so
# verify and retry while /etc is still writable (post-merge /etc is read-only
# and a later semodule can never commit).
install_selinux_module() {
    _pp="$1"
    _name="$2"
    for _attempt in 1 2 3; do
        semodule -i "${_pp}"
        _listed=$(semodule --list 2>/dev/null | grep -c "${_name}" || true)
        if [ "${_listed}" -ne 0 ]; then
            echo "SELinux module ${_name} installed (attempt ${_attempt})"
            return 0
        fi
        echo "WARNING: SELinux module ${_name} not committed to the policy store after semodule -i (attempt ${_attempt}/3); retrying" >&2
    done
    echo "ERROR: SELinux module ${_name} could not be committed to the policy store" >&2
    return 1
}

install_selinux_module /root/ebpf-fix.pp ebpf-fix
# k8slab-merge: allow init_t to execute the merged sysext binaries
# (unlabeled_t — squashfs strips SELinux xattrs) so crio/kubelet/etcd etc. do
# not fail with status=203/EXEC under SELinux Enforcing.
install_selinux_module /root/k8slab-merge.pp k8slab-merge
# k8slab-conmon: the k8s stack runs as init_t (no
# container-selinux type_transition is installed), so under Enforcing it needs
# the container-runtime permission catalog the base policy grants only to
# container_runtime_t: conmon exec, name_connect on unreserved/http ports,
# kmsg+syslog_read, container_file_t/iptables_var_run_t/ifconfig_var_run_t/
# container_runtime_tmpfs_t/container_var_run_t file/dir/sock/fifo creates,
# tmpfs+container_file_t+devpts filesystem relabel, fusefs_t+fusermount
# (fuse-overlayfs over btrfs), process setpgid. The E2E proved a node-local
# module covering exactly these rules makes crio/kubelet/etcd/apiserver/cm/
# scheduler and pods run under Enforcing; this module ships that coverage.
install_selinux_module /root/k8slab-conmon.pp k8slab-conmon

# CRI-O short-name cache label: cri-o resolves short image names by
# creating /var/cache/containers/short-name-aliases.conf.lock; with no fcontext
# the file is labeled var_t and init_t is denied create. Pre-create the dir in
# the image with the container_var_lib_t context (the same label container.fc
# uses) so the runtime can manage it (k8slab-conmon grants init_t manage).
mkdir -p /var/cache/containers
if command -v semanage >/dev/null 2>&1; then
    semanage fcontext -a -t container_var_lib_t '/var/cache/containers(/.*)?' 2>/dev/null || true
    restorecon -R /var/cache/containers 2>/dev/null || true
else
    chcon -R -t container_var_lib_t /var/cache/containers 2>/dev/null || true
fi

# Make the baked first-boot resize helper executable. The file
# provisioner preserves the source mode, but the exec bit is load-bearing so
# it is asserted explicitly.
chmod +x /usr/local/sbin/resize-rootfs.sh

# Cloud-Hypervisor uses ACPI for graceful shutdown — no guest agent needed.
# Unlike the QEMU/libvirt variant, no qemu-guest-agent is installed or enabled.

# Remove machine-id so each VM gets unique ID on first boot
rm -f /etc/machine-id
touch /etc/machine-id

# Flush all writes to disk before the packer plugin shuts the VM down. The
# cloud-hypervisor plugin force-deletes the VM after its ACPI shutdown timeout
# (the guest does not complete a clean poweroff), and unsynced pages are lost
# with the kill — observed losing the freshly committed SELinux module store
# entries even though semodule -i succeeded and semodule --list verified them
# during provisioning. sync ensures the semodule transaction is durable.
sync
