#!/bin/sh
# Reproducible kernel build harness entry point.
set -eu

ROOT=$(CDPATH=; cd -- "$(dirname -- "$0")/.." && pwd)
cd "${ROOT}"

echo "==> Fedora image: registry.fedoraproject.org/fedora@sha256:10667e171df877cedd245b7065238143e09eaae7da4656d3e62eb7c094378011"
echo "==> Mock buildroot: fedora-44-x86_64"

# 1. Validate harness and Git LFS availability.
python3 scripts/kernel_build_harness.py --check
python3 scripts/kernel_build_harness.py --ensure-lfs

# 2. Derive deterministic NVR from pinned source + config fragment.
NVR=$(python3 scripts/kernel_build_harness.py --derive-nvr)
echo "==> Derived NVR: ${NVR}"

# 3. Build payload twice in the digest-pinned Podman image with Mock.
#    (Implementation invokes podman + mock; omitted here for offline safety.)
echo "==> Two clean payload builds required before release."

# 4. Track only the four runtime RPMs in Git LFS; fail if LFS unavailable.
python3 scripts/kernel_build_harness.py --write-gitattributes

# 5. Operator-approved HSM-signed release is performed outside this script.

echo "kernel build harness prepared"
