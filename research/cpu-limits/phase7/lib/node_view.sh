#!/bin/sh
# Run a command with the node's scheduler view mounted where the kernel and
# libbpf expect it.
#
#     node_view.sh COMMAND [ARG...]
#
# The pod's own /sys is private: /sys/kernel/debug is a container-local
# debugfs with no sched/ (CONFIG_SCHED_DEBUG is off on the node, so the CFS
# group mode and the preemption model live under the node's real debugfs),
# /sys/kernel/btf is a plain sysfs mount without vmlinux, and
# /sys/kernel/sched_ext does not exist at all. /sys is read-only in this
# container, so the pod's temporary /sys/kernel tmpfs is replaced by the
# node's own mounts, reached through the read-write /host mount.
#
# The mounts go into a *private mount namespace* created by unshare(1), so
# they disappear when COMMAND exits. Mounting them in the pod's own namespace
# does not work: /sys and the hostPath mount at /host sit in a propagation
# peer group shared with the node, so every bind mount replicates into every
# peer, and each additional run multiplied the pod's mount table. After a
# dozen runs it held 90000 entries and mount(2) began to fail with ENOSPC.
#
# Reading scheduler state does not need this wrapper: the same files are
# always reachable read-only at /host/sys/kernel/{sched_ext,debug}.
#
# Must run as root inside the container; requires SYS_ADMIN.
set -eu

if [ "$#" -eq 0 ]; then
    echo "usage: node_view.sh COMMAND [ARG...]" >&2
    exit 2
fi

exec unshare -m --propagation private sh -c '
set -eu
umount /sys/kernel 2>/dev/null || true
mkdir -p /sys/kernel/debug /sys/kernel/tracing /sys/kernel/btf /sys/kernel/sched_ext

mount --bind /host/sys/kernel/debug /sys/kernel/debug
mount --bind /host/sys/kernel/tracing /sys/kernel/tracing
mount --bind /host/sys/kernel/btf /sys/kernel/btf
mount --bind /host/sys/kernel/sched_ext /sys/kernel/sched_ext

# libbpf resolves the kernel Kconfig it needs as /boot/config-$(uname -r),
# and the pod has no /boot. The node boot directory is read-only there.
mkdir -p /boot
mount --bind /host/boot /boot

test -e /sys/kernel/debug/sched/cgroup_mode
test -e /sys/kernel/sched_ext/state
test -e /sys/kernel/btf/vmlinux
test -e "/boot/config-$(uname -r)"

# An unprivileged RLIMIT_MEMLOCK is 8 MiB here; the schedulers map several
# arenas and the loader fails with EPERM without this.
ulimit -l unlimited 2>/dev/null || true

exec "$@"
' sh "$@"
