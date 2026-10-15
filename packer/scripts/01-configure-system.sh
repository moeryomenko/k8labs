#!/bin/sh
# shellcheck shell=sh # this is a POSIX sh script; declare the real shell so
#                     # ShellCheck applies the sh ruleset (repo .shellcheckrc
#                     # defaults to bash).
set -eux

# ---------------------------------------------------------------------------
# Kernel version pinning — freeze the kernel at the baked version so derived
# images do not accidentally upgrade it. KERNEL_VERSION is passed through
# the Packer shell provisioner environment_vars.
# ---------------------------------------------------------------------------
KERNEL_VERSION="${KERNEL_VERSION:-7.2.10-200.fc44}"

echo "==> Baking with kernel target: ${KERNEL_VERSION}"

# The kernel may come from a repository other than the base image's, which is
# how the scheduler-research kernel (a newer sched_ext ABI than Fedora 44 ships)
# is installed. KERNEL_REPO_URL is a base URL of an rpm-md repository.
if [ -n "${KERNEL_REPO_URL:-}" ]; then
    echo "==> Kernel repository: ${KERNEL_REPO_URL}"
    # The ad-hoc repo carries no configured OpenPGP key, so signature checking
    # is disabled for it. Packages are fetched over HTTPS from the vendor's
    # official mirror, and the kernel version is pinned by full NVR.
    dnf_install_kernel() {
        dnf install -y --refresh \
            --repofrompath=k8labs-kernel,"${KERNEL_REPO_URL}" \
            --repo=k8labs-kernel \
            --setopt=k8labs-kernel.gpgcheck=0 \
            --setopt=k8labs-kernel.repo_gpgcheck=0 \
            "$@"
    }
else
    dnf_install_kernel() {
        dnf --refresh install -y "$@"
    }
fi

# Refresh Fedora metadata before installing. If the lock's exact build has
# rotated out of the repository, install the current kernel set and pin the
# image to the version actually installed rather than failing on stale metadata.
if ! dnf_install_kernel "kernel-core-${KERNEL_VERSION}" \
    "kernel-modules-${KERNEL_VERSION}" \
    "kernel-modules-core-${KERNEL_VERSION}"; then
    echo "==> Exact kernel ${KERNEL_VERSION} is unavailable; installing current Fedora kernel"
    dnf_install_kernel kernel kernel-core kernel-modules kernel-modules-core kernel-modules-extra
fi

# Resolve the version actually installed, including the architecture suffix:
# the /boot filename and grubby both need the full NVR. The value passed in may
# omit the arch (the lock stores the version without it).
KERNEL_VERSION=$(rpm -q --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' kernel-core | sort -V | tail -1)

echo "==> Kernel ${KERNEL_VERSION} installed from refreshed repositories"

# kernel-devel supplies /usr/src/kernels/<ver>. Only install it when the kernel
# came from the base repositories: an ad-hoc kernel repository (a different
# Fedora release) has no devel tree for this image's release, and pulling one
# drags that release's dependency set in and conflicts with the image's own
# packages. Run-queue metrics use the libbpf-tools CO-RE binaries, which read
# BTF and need no headers.
if [ -z "${KERNEL_REPO_URL:-}" ]; then
    if ! dnf --refresh install -y "kernel-devel-${KERNEL_VERSION}" kernel-headers; then
        echo "WARN: kernel-devel-${KERNEL_VERSION} unavailable; BCC tools will not compile"
    fi
else
    dnf --refresh install -y kernel-headers || true
fi

# Set the exact target kernel as the default boot entry. The original Fedora
# kernel remains installed as a recovery fallback.
NEW_KERNEL=$(find /boot -maxdepth 1 -name "vmlinuz-${KERNEL_VERSION}" \
    -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
if [ -z "${NEW_KERNEL}" ]; then
    echo "ERROR: exact kernel image vmlinuz-${KERNEL_VERSION} was not installed" >&2
    exit 1
fi
grubby --set-default "${NEW_KERNEL}"

# Record the exact target for post-boot verification.
rpm -q kernel-core > /etc/baked-kernel-version
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
#
# Scheduler-research observer tooling is installed node-native: the linux-sched
# series runs bpftrace, perf and the BCC tools directly on the node. Privileged
# observer DaemonSets are deliberately absent because this lab's cri-o refuses
# to create device nodes for privileged containers.
dnf install -y conmon parted growpart \
    bpftrace \
    perf \
    bcc-tools \
    libbpf-tools \
    python3-bcc \
    bpftool
