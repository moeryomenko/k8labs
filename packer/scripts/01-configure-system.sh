#!/bin/sh
# shellcheck shell=sh # this is a POSIX sh script; declare the real shell so
#                     # ShellCheck applies the sh ruleset (repo .shellcheckrc
#                     # defaults to bash).
set -eux

# ---------------------------------------------------------------------------
# The Fedora Cloud Base image can boot with the root filesystem still
# read-only while systemd remounts it. dnf refuses to install into /usr in
# that state, so remount the root and /usr writable before touching packages.
# ---------------------------------------------------------------------------
if ! touch /usr/.k8labs-rw-test 2>/dev/null; then
    mount -o remount,rw / 2>/dev/null || true
    mount -o remount,rw /usr 2>/dev/null || true
fi
touch /usr/.k8labs-rw-test || {
    echo "ERROR: /usr is not writable; cannot bake packages" >&2
    exit 1
}
rm -f /usr/.k8labs-rw-test

# ---------------------------------------------------------------------------
# Kernel selection — install the stock Fedora kernel and the local-tree
# comparison kernel, then freeze them so derived images do not upgrade either.
# KERNEL_VERSION, CUSTOM_KERNEL_VERSION, and DEFAULT_KERNEL are passed through
# the Packer shell provisioner environment_vars.
# ---------------------------------------------------------------------------
KERNEL_VERSION="${KERNEL_VERSION:-7.3.0-65.fc46}"
CUSTOM_KERNEL_VERSION="${CUSTOM_KERNEL_VERSION:-7.3.0-k8labs}"
DEFAULT_KERNEL="${DEFAULT_KERNEL:-stock}"

echo "==> Baking with stock kernel ${KERNEL_VERSION}, custom kernel ${CUSTOM_KERNEL_VERSION}, default ${DEFAULT_KERNEL}"

# Both kernels come from local rpm-md repositories uploaded by base.pkr.hcl.
# A local repository ships as plain RPMs; build its metadata first.
prepare_kernel_repo() {
    repo_url=$1
    [ -n "${repo_url}" ] || return 0
    echo "==> Kernel repository: ${repo_url}"
    case "${repo_url}" in
        file://*)
            dnf install -y createrepo_c >/dev/null
            repo_path="${repo_url#file://}"
            # Packer may preserve the source directory name under the
            # destination. Flatten any nested RPMs before createrepo_c, which
            # only scans the directory it is given.
            find "${repo_path}" -mindepth 2 -name '*.rpm' \
                -exec cp -n {} "${repo_path}/" \;
            createrepo_c --quiet "${repo_path}"
            ;;
        *)
            ;;
    esac
}

# The ad-hoc repos carry no configured OpenPGP key, so signature checking is
# disabled for them. The versions are pinned by exact NVR.
dnf_install_from_repo() {
    repo_url=$1
    shift
    if [ -n "${repo_url}" ]; then
        dnf install -y --refresh \
            --repofrompath=k8labs-kernel,"${repo_url}" \
            --repo=k8labs-kernel \
            --setopt=k8labs-kernel.gpgcheck=0 \
            --setopt=k8labs-kernel.repo_gpgcheck=0 \
            "$@"
    else
        dnf --refresh install -y "$@"
    fi
}

prepare_kernel_repo "${KERNEL_REPO_URL:-}"
prepare_kernel_repo "${CUSTOM_KERNEL_REPO_URL:-}"

# Both kernels need BLS entries: grubby selects the default, and 04-uki.sh
# reuses each entry's command line for the signed UKI. Fedora's kernel-install
# defaults to bls, but make it explicit so a future base-image change cannot
# silently switch to the uki layout and leave no entry to read.
mkdir -p /etc/kernel
printf 'layout=bls\n' > /etc/kernel/install.conf

# The ESP is only ~100 MiB. kernel-install must keep the kernel and initramfs
# on the btrfs /boot; the ESP is reserved for the signed UKIs and boot manager.
# BOOT_ROOT is inherited by the RPM scriptlets; the install.conf key is not
# understood by this systemd build.
export BOOT_ROOT=/boot

# A host-only initramfs is enough for the VM's virtio/btrfs root and keeps the
# two UKIs small enough to fit.
mkdir -p /etc/dracut.conf.d
cat > /etc/dracut.conf.d/99-k8labs-hostonly.conf <<'EOF'
hostonly="yes"
compress="zstd"
omit_dracutmodules+=" network network-manager nfs cifs iscsi fcoe multipath dmraid "
EOF

# The research image must be reproducible: silently accepting a newer package
# changes the kernel BTF and can invalidate sched_ext scheduler binaries.
dnf_install_from_repo "${KERNEL_REPO_URL:-}" \
    "kernel-core-${KERNEL_VERSION}" \
    "kernel-modules-${KERNEL_VERSION}" \
    "kernel-modules-core-${KERNEL_VERSION}" \
    "kernel-modules-extra-${KERNEL_VERSION}"

STOCK_KERNEL_RELEASE=$(rpm -q --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' "kernel-core-${KERNEL_VERSION}" 2>/dev/null | head -1 || true)
if [ -z "${STOCK_KERNEL_RELEASE}" ]; then
    echo "ERROR: exact stock kernel ${KERNEL_VERSION} was not installed" >&2
    exit 1
fi

# The stock kernel-core RPM marks /boot/vmlinuz-<release> as a ghost file and
# leaves it to kernel-install. With BOOT_ROOT=/boot kernel-install writes the
# BLS tree under /boot; create the canonical image name the UKI step uses.
if [ ! -f "/boot/vmlinuz-${STOCK_KERNEL_RELEASE}" ]; then
    install -m 0755 "/lib/modules/${STOCK_KERNEL_RELEASE}/vmlinuz" \
        "/boot/vmlinuz-${STOCK_KERNEL_RELEASE}"
fi

dnf_install_from_repo "${CUSTOM_KERNEL_REPO_URL:-}" kernel-k8labs
CUSTOM_KERNEL_RELEASE=$(rpm -q --qf '%{VERSION}-%{RELEASE}\n' kernel-k8labs 2>/dev/null | head -1 || true)
if [ -z "${CUSTOM_KERNEL_RELEASE}" ]; then
    echo "ERROR: custom kernel kernel-k8labs was not installed" >&2
    exit 1
fi
if [ "${CUSTOM_KERNEL_RELEASE}" != "${CUSTOM_KERNEL_VERSION}" ]; then
    echo "ERROR: custom kernel release ${CUSTOM_KERNEL_RELEASE} != ${CUSTOM_KERNEL_VERSION}" >&2
    exit 1
fi

for release in "${STOCK_KERNEL_RELEASE}" "${CUSTOM_KERNEL_RELEASE}"; do
    if [ ! -f "/boot/vmlinuz-${release}" ]; then
        echo "ERROR: kernel image vmlinuz-${release} was not installed" >&2
        exit 1
    fi
done

case "${DEFAULT_KERNEL}" in
    stock) DEFAULT_KERNEL_RELEASE="${STOCK_KERNEL_RELEASE}" ;;
    custom) DEFAULT_KERNEL_RELEASE="${CUSTOM_KERNEL_RELEASE}" ;;
    *) echo "ERROR: DEFAULT_KERNEL must be stock or custom" >&2; exit 1 ;;
esac
# GRUB is a fallback path only; the firmware boots the signed systemd-boot
# UKI entry. grubby cannot always resolve the BLS entry in the Cloud image, so
# do not fail the bake on it. 04-uki.sh writes the authoritative loader.conf.
if ! grubby --set-default "/boot/vmlinuz-${DEFAULT_KERNEL_RELEASE}"; then
    echo "WARN: grubby could not select ${DEFAULT_KERNEL_RELEASE}; systemd-boot default is set by 04-uki.sh" >&2
fi

# Record both kernels and the default for the UKI step and post-boot checks.
cat > /etc/k8labs-kernels <<EOF
stock=${STOCK_KERNEL_RELEASE}
custom=${CUSTOM_KERNEL_RELEASE}
default=${DEFAULT_KERNEL}
EOF

{
    echo "Stock kernel: ${STOCK_KERNEL_RELEASE}"
    echo "Custom kernel: ${CUSTOM_KERNEL_RELEASE}"
    echo "Default kernel: ${DEFAULT_KERNEL_RELEASE}"
} > /etc/baked-kernel-version

echo "==> Kernels installed: stock ${STOCK_KERNEL_RELEASE}, custom ${CUSTOM_KERNEL_RELEASE}"

# kernel-devel supplies /usr/src/kernels/<ver>. Only install it when the stock
# kernel came from the base repositories: an ad-hoc kernel repository has no
# devel tree for this image's release. Run-queue metrics use the libbpf-tools
# CO-RE binaries, which read BTF and need no headers.
if [ -z "${KERNEL_REPO_URL:-}" ]; then
    if ! dnf --refresh install -y "kernel-devel-${KERNEL_VERSION}" kernel-headers; then
        echo "WARN: kernel-devel-${KERNEL_VERSION} unavailable; BCC tools will not compile"
    fi
else
    dnf --refresh install -y kernel-headers || true
fi

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
    bpftool \
    systemd-boot-unsigned \
    systemd-ukify \
    sbsigntools \
    softhsm \
    openssl-pkcs11

# ---------------------------------------------------------------------------
# Scheduler-research fixtures
# ---------------------------------------------------------------------------
# Baked onto the node because the workload Pods mount them read-only and the lab
# has no PVC and no SSH to the VMs. The load generators themselves run inside
# the observer container, so wrk2 is installed into FIXTURE_BIN_DIR, which the
# observer also mounts. Node and container are both Fedora 44, so a node-built
# binary runs in the container.
#
# Set FIXTURE_DIR empty to skip this section entirely (a bake that is not for
# the scheduler-research series).
FIXTURE_DIR="${FIXTURE_DIR:-/var/lib/linux-sched/fixtures}"
FIXTURE_BIN_DIR="${FIXTURE_BIN_DIR:-/var/lib/linux-sched/bin}"
FIXTURE_CLIP_URL="${FIXTURE_CLIP_URL:-https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4.zip}"
FIXTURE_CLIP_ZIP_SHA256="${FIXTURE_CLIP_ZIP_SHA256:-109e3ede8790bd633f374ca311d9cc61dce8d7f98f5b0797ca98199c9fbceedf}"
FIXTURE_CLIP_MP4_SHA256="${FIXTURE_CLIP_MP4_SHA256:-f78f39603e6774907f2faafabf26a667f4a6fc31769ec304a8a8f7c62d280508}"
WRK2_REPO_URL="${WRK2_REPO_URL:-https://github.com/giltene/wrk2}"
WRK2_COMMIT="${WRK2_COMMIT:-44a94c17d8e6a0bac8559b53da76848e430cb7a7}"
PGBENCH_SCALE="${PGBENCH_SCALE:-10}"

if [ -n "${FIXTURE_DIR}" ]; then
    mkdir -p "${FIXTURE_DIR}" "${FIXTURE_BIN_DIR}"

    # Build tools plus the PostgreSQL server used to take the snapshot. The
    # server package is bake-time only; the workload runs the pinned
    # postgres image, and the observer installs the client for pgbench.
    # zlib-devel is a hard requirement of wrk2's hdr_histogram, and the build
    # fails without it rather than degrading, so it is not optional.
    dnf install -y gcc make git openssl-devel zlib-devel curl unzip

    # Fedora packages wrk, which cannot hold a fixed offered rate (wrk2's -R).
    # The scenarios measure latency under a fixed rate, so the fork is built
    # here and pinned by commit.
    wrk2_tmp=$(mktemp -d)
    git clone --quiet --recursive "${WRK2_REPO_URL}" "${wrk2_tmp}/wrk2"
    git -C "${wrk2_tmp}/wrk2" checkout --quiet "${WRK2_COMMIT}"
    make -C "${wrk2_tmp}/wrk2" WITH_OPENSSL=1
    install -m 0755 "${wrk2_tmp}/wrk2/wrk" "${FIXTURE_BIN_DIR}/wrk2"

    # wrk2 loads its request builder with `require "wrk"`. The Makefile
    # byte-compiles src/wrk.lua into an object and links it, but this toolchain
    # writes an object it cannot read back:
    #
    #   ld.bfd: obj/bytecode.o: string table [3] is corrupt
    #
    # The linker still produces a binary, so the failure is silent: the module is
    # nil and every run panics with `attempt to index a nil value`, while
    # `--version` answers normally. Ship the Lua source beside the binary; the
    # observer puts it on Lua's module path.
    install -m 0644 "${wrk2_tmp}/wrk2/src/wrk.lua" "${FIXTURE_BIN_DIR}/wrk.lua"
    rm -rf "${wrk2_tmp}"

    # Verify the load generator actually *runs*. The previous check only asked
    # for `--version`, which passes on a binary that panics on every real
    # invocation — and that is exactly how a broken load generator was baked and
    # then discovered in the middle of a calibration sweep.
    #
    # Nothing listens on port 1, so a working wrk2 reports a connection failure;
    # only the Lua panic indicates a broken build.
    if LUA_PATH="${FIXTURE_BIN_DIR}/?.lua;;" "${FIXTURE_BIN_DIR}/wrk2" \
        -t1 -c1 -d1s -R10 --latency http://127.0.0.1:1/ 2>&1 | grep -q PANIC; then
        echo "ERROR: baked wrk2 panics instead of running" >&2
        exit 1
    fi

    # The FFmpeg scenario loops a licensed clip. Both the archive and the
    # extracted file are verified, so a truncated or substituted download
    # cannot reach a run.
    clip_tmp=$(mktemp -d)
    if [ -f /root/clip.zip ]; then
        cp /root/clip.zip "${clip_tmp}/clip.zip"
    else
        curl -fsSL -o "${clip_tmp}/clip.zip" "${FIXTURE_CLIP_URL}"
    fi
    echo "${FIXTURE_CLIP_ZIP_SHA256}  ${clip_tmp}/clip.zip" | sha256sum -c -
    unzip -j -o -q "${clip_tmp}/clip.zip" -d "${clip_tmp}"
    clip_mp4=$(find "${clip_tmp}" -maxdepth 1 -name '*.mp4' | head -1)
    if [ -z "${clip_mp4}" ]; then
        echo "ERROR: clip archive contained no .mp4" >&2
        exit 1
    fi
    echo "${FIXTURE_CLIP_MP4_SHA256}  ${clip_mp4}" | sha256sum -c -
    install -m 0644 "${clip_mp4}" "${FIXTURE_DIR}/input.mp4"
    rm -rf "${clip_tmp}"

    # The PostgreSQL snapshot and transaction script are deliberately NOT baked.
    # The workload initialises its own deterministic scale-2 database in the Pod
    # (`pgbench -i -s 2` with the built-in TPC-B script): the baked scale-10
    # snapshot was several times larger than a 5 GiB worker can hold beside the
    # instrumentation, and an arm using it was evicted for ephemeral-storage.
    # Basing the workload on the in-Pod initialisation removes ~96 MB from the
    # image and one cross-version dependency (the dump had to match the server).

    # The observer and the workload Pods mount these paths. A freshly baked
    # path does not carry the container file type, so label it where the
    # policy is enforcing. Best effort: on a permissive node this is a no-op.
    chcon -R -t container_file_t "${FIXTURE_DIR}" "${FIXTURE_BIN_DIR}" 2>/dev/null || true

    # Record what was baked, so a run can state its inputs.
    {
        echo "clip_url=${FIXTURE_CLIP_URL}"
        echo "clip_sha256=${FIXTURE_CLIP_MP4_SHA256}"
        echo "wrk2_commit=${WRK2_COMMIT}"
    } > "${FIXTURE_DIR}/BAKED"
fi
