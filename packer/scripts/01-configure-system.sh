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
    # A local repository ships as plain RPMs; build its metadata first.
    case "${KERNEL_REPO_URL}" in
        file://*)
            dnf install -y createrepo_c >/dev/null
            REPO_PATH="${KERNEL_REPO_URL#file://}"
            createrepo_c --quiet "${REPO_PATH}"
            ;;
        *)
            ;;
    esac
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

# The research image must be reproducible: silently accepting a newer package
# changes the kernel BTF and can invalidate sched_ext scheduler binaries.
dnf_install_kernel "kernel-core-${KERNEL_VERSION}" \
    "kernel-modules-${KERNEL_VERSION}" \
    "kernel-modules-core-${KERNEL_VERSION}" \
    "kernel-modules-extra-${KERNEL_VERSION}"

# Resolve the installed version, including the architecture suffix. The value
# passed in may omit the architecture (the lock stores the version without it).
INSTALLED_KERNEL_VERSION=$(rpm -q --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' "kernel-core-${KERNEL_VERSION}" | head -1)
if [ -z "${INSTALLED_KERNEL_VERSION}" ]; then
    echo "ERROR: exact kernel ${KERNEL_VERSION} was not installed" >&2
    exit 1
fi
KERNEL_VERSION="${INSTALLED_KERNEL_VERSION}"

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
    bpftool \
    systemd-ukify \
    sbsigntools

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
    curl -fsSL -o "${clip_tmp}/clip.zip" "${FIXTURE_CLIP_URL}"
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
