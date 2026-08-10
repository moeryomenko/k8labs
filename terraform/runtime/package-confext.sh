#!/bin/sh
# shellcheck disable=SC2292  # POSIX sh per AGENTS.md: [ ] not [[ ]]; rc forces shell=bash
#
# package-confext.sh — Package role-split runtime confext images
#
# For each source tree directory under <trees-dir> (rendered by confexts.tf
# into build/runtime/trees/), build <output-dir>/<name>.raw with
# `mksquashfs -noappend -all-root` — the same mechanics as
# extensions/build.sh. The tree directory name is the image name, so the
# z-etcd, z-kubernetes-cp and z-kubelet-<node> trees become the .raw files
# the phase-B push step scps to /var/lib/confexts on each node.
#
# Usage: package-confext.sh <trees-dir> <output-dir>
#
# Idempotent: -noappend rebuilds the image in place, so re-running after a
# content change replaces the .raw without needing a clean step. Each image
# is logged on stdout; any failure exits non-zero.
#
# POSIX sh only (AGENTS.md); no bashisms, no pipefail.
# =============================================================================

set -eu

usage() {
    echo "Usage: package-confext.sh <trees-dir> <output-dir>" >&2
}

die() {
    echo "package-confext: error: $*" >&2
    exit 1
}

trees_dir="${1:-}"
output_dir="${2:-}"

if [ -z "${trees_dir}" ]; then
    usage
    exit 1
fi
if [ -z "${output_dir}" ]; then
    usage
    exit 1
fi

if [ ! -d "${trees_dir}" ]; then
    die "trees directory not found: ${trees_dir}"
fi

if ! mkdir -p "${output_dir}"; then
    die "cannot create output directory: ${output_dir}"
fi

packaged=0
for tree in "${trees_dir}"/*; do
    if [ ! -d "${tree}" ]; then
        continue
    fi
    name=$(basename "${tree}")
    case "${name}" in
        ''|.) continue ;;
        *) : ;;
    esac
    # Confext images merge into /etc only; content elsewhere silently has no
    # effect, so refuse to package a tree that carries anything outside etc/.
    if [ ! -d "${tree}/etc" ]; then
        die "${name}: missing etc/ directory (confext trees carry only etc/ content)"
    fi
    if [ ! -d "${tree}/etc/extension-release.d" ]; then
        die "${name}: missing etc/extension-release.d/ metadata directory"
    fi
    output_file="${output_dir}/${name}.raw"
    echo "package-confext: packaging ${name} -> ${output_file}"
    if ! mksquashfs "${tree}" "${output_file}" -noappend -all-root; then
        die "${name}: mksquashfs failed"
    fi
    packaged=$((packaged + 1))
done

if [ "${packaged}" -eq 0 ]; then
    die "no confext trees found under ${trees_dir}"
fi

echo "package-confext: packaged ${packaged} confext image(s) into ${output_dir}"
