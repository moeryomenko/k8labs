#!/bin/sh
# shellcheck shell=sh # this is a POSIX sh script; declare the real shell so
#                     # ShellCheck applies the sh ruleset (repo .shellcheckrc
#                     # defaults to bash).
set -eux

# ---------------------------------------------------------------------------
# Kernel version pinning — freeze the kernel at the baked version so derived
# images do not accidentally upgrade it.  KERNEL_VERSION is passed through
# the Packer shell provisioner environment_vars.
# ---------------------------------------------------------------------------
KERNEL_VERSION="${KERNEL_VERSION:-7.1}"

echo "==> Baking with kernel target: ${KERNEL_VERSION}"

# Install the target kernel from the Fedora updates repo if available.
# The base ISO ships with the GA kernel (e.g. 6.19); we upgrade to the
# target version so the image is already running the desired kernel.
if dnf install -y "kernel-core-${KERNEL_VERSION}*" \
    "kernel-modules-${KERNEL_VERSION}*" \
    "kernel-modules-core-${KERNEL_VERSION}*" \
    2>/dev/null; then
    echo "==> Kernel ${KERNEL_VERSION} installed from updates"

    # Set the newly installed kernel as the default boot entry
    NEW_KERNEL=$(find /boot -maxdepth 1 -name "vmlinuz-${KERNEL_VERSION}*" \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    if [ -n "${NEW_KERNEL}" ]; then
        grubby --set-default "${NEW_KERNEL}" 2>/dev/null || true
    fi

    # Note: old kernel packages remain installed as boot fallback
else
    echo "Warning: kernel ${KERNEL_VERSION} not found in repos, using default" >&2
fi

# Record the installed kernel version for traceability
rpm -q kernel-core > /etc/baked-kernel-version 2>/dev/null || {
    echo "Warning: kernel-core not installed during bake" >&2
    echo "unknown" > /etc/baked-kernel-version
}
echo "Kernel target: ${KERNEL_VERSION}" >> /etc/baked-kernel-version

# Prevent kernel updates in images derived from this base
mkdir -p /etc/dnf/dnf.conf.d
cat > /etc/dnf/dnf.conf.d/kernel-pin.conf <<EOF
# Kernel frozen at the version baked into this image.
# Remove this file to allow kernel updates on derived images.
excludepkgs=kernel* kernel-core* kernel-modules* kernel-devel*
EOF

# Load kernel modules needed for containers and Kubernetes networking
modprobe overlay
modprobe br_netfilter || true

# Enable IP forwarding for Kubernetes networking
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.bridge.bridge-nf-call-iptables=1 || true

# Persist sysctl settings
cat > /etc/sysctl.d/99-kubernetes.conf <<EOF
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
EOF

# Persist module loading
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF

# ---------------------------------------------------------------------------
# sysext/confext payload destinations
# ---------------------------------------------------------------------------
# Directories that receive the baked extension images. The Packer file
# provisioners upload the .raw images here after this script runs, so the
# destinations must already exist.
mkdir -p /var/lib/extensions /var/lib/confexts

# ---------------------------------------------------------------------------
# Runtime prerequisites
# ---------------------------------------------------------------------------
# conmon is the container runtime monitor required by cri-o. parted/growpart
# are required by /usr/local/sbin/resize-rootfs.sh at first boot — without
# them the resize helper dies and the root disk is never grown (reviewer
# F-001). tar/rsync are intentionally NOT installed (Ansible is gone) and
# policy.json lives in the confext-containers image, not the base.
dnf install -y conmon parted growpart
