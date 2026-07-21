#!/bin/sh
set -eux

# Clean package manager cache
dnf clean all --releasever=44 || true
rm -rf /var/cache/dnf/*

# Zero free space for better qcow2 compression
dd if=/dev/zero of=/zero.fill bs=1M || true
rm -f /zero.fill

# Clean logs
find /var/log -type f -name '*.log' -exec sh -c ': > "$1"' _ {} \;
