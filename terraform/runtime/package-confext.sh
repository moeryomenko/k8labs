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
# the phase-B push step scps to /var/lib/confexts on each node. After
# packaging, any *.raw whose tree no longer exists under <trees-dir> (e.g. a
# removed node's z-kubelet image) is pruned, so the output dir converges to
# the current tree set; non-.raw files are never touched.
#
# Usage: package-confext.sh <trees-dir> <output-dir>
#
# Enablement symlinks: before packaging, each role tree gets its unit
# enablement symlinks created INSIDE the tree under
# etc/systemd/system/multi-user.target.wants/ (the exact links `systemctl
# enable` would write for these WantedBy=multi-user.target units). Shipping
# them inside the image means the systemd-confext merge itself enables the
# units — no post-boot write into the read-only merged /etc is ever needed
# (verified in the E2E replay; phase-B push/refresh contract).
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

# add_enablement_symlinks <tree> <name> — create the enablement symlinks that
# `systemctl enable` would write, INSIDE the confext tree, so the merge
# activates the units without any post-boot write into the read-only merged
# /etc. Every k8s unit's [Install] is WantedBy=multi-user.target,
# so enablement is exactly these symlinks; absolute targets mirror systemd's
# own /etc/systemd/system/<wants> links and resolve to the sysext-provided
# unit files on the merged node.
add_enablement_symlinks() {
    _tree=$1
    _name=$2
    case "${_name}" in
        z-etcd) _units="etcd.service" ;;
        z-kubernetes-cp) _units="kube-apiserver.service kube-controller-manager.service kube-scheduler.service" ;;
        z-kubelet-*) _units="crio.service kubelet.service" ;;
        *) return 0 ;;
    esac
    _wants="${_tree}/etc/systemd/system/multi-user.target.wants"
    mkdir -p "${_wants}"
    for _unit in ${_units}; do
        ln -sf "/usr/lib/systemd/system/${_unit}" "${_wants}/${_unit}"
    done
}

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
    add_enablement_symlinks "${tree}" "${name}"
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

# Reconcile the output dir with the current tree set: delete every *.raw
# whose tree directory is gone (a removed node's z-kubelet-<node> image or
# any other orphaned image). Non-.raw files are never touched, and images
# for current trees are kept (they were just rebuilt above).
pruned=0
for image in "${output_dir}"/*.raw; do
    # With no *.raw files the glob stays literal; skip it.
    if [ ! -f "${image}" ]; then
        continue
    fi
    name=$(basename "${image}" .raw)
    if [ ! -d "${trees_dir}/${name}" ]; then
        echo "package-confext: pruning stale image ${image}"
        rm -f "${image}"
        pruned=$((pruned + 1))
    fi
done
if [ "${pruned}" -gt 0 ]; then
    echo "package-confext: pruned ${pruned} stale image(s)"
fi

echo "package-confext: packaged ${packaged} confext image(s) into ${output_dir}"
