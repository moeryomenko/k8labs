#!/usr/bin/env bash
# Build the k8labs comparison kernel from the local linux checkout.
#
# The build is out of tree so the user's linux checkout stays clean. It runs in
# a digest-pinned Fedora container because the lab host is Arch and does not
# carry pahole/rpmbuild. The resulting RPM is named kernel-k8labs so it can be
# installed beside Fedora's kernel-core/kernel-modules packages.
#
# Usage:
#   scripts/build-custom-kernel.sh
#   LINUX_SRC=/path/to/linux LINUX_COMMIT=<sha> scripts/build-custom-kernel.sh
#   scripts/build-custom-kernel.sh --package-only
#
# Outputs:
#   build/kernel-custom-7.3.0/          out-of-tree build
#   build/kernel-rpms-custom/            installable RPM
set -euo pipefail

ROOT=$(CDPATH=; cd -- "$(dirname -- "$0")/.." && pwd)
LINUX_SRC=${LINUX_SRC:-/home/eryoma/workspace/blog/linux}
LINUX_COMMIT=${LINUX_COMMIT:-$(git -C "${LINUX_SRC}" rev-parse HEAD)}
WORKTREE=${WORKTREE:-${ROOT}/build/linux-custom-src}
OUT=${OUT:-${ROOT}/build/kernel-custom-7.3.0}
STAGE=${STAGE:-${OUT}/stage}
RPMBUILD=${RPMBUILD:-${ROOT}/build/rpmbuild-custom}
RPMDIR=${RPMDIR:-${ROOT}/build/kernel-rpms-custom}
STOCK_KERNEL_CONFIG=${STOCK_KERNEL_CONFIG:-${ROOT}/build/kernel-rpms-stock/7.3.0-65.fc46/config-7.3.0-65.fc46}
KERNELRELEASE=${KERNELRELEASE:-7.3.0-k8labs}
JOBS=${JOBS:-$(nproc)}
BUILD_IMAGE=${BUILD_IMAGE:-registry.fedoraproject.org/fedora@sha256:10667e171df877cedd245b7065238143e09eaae7da4656d3e62eb7c094378011}
PACKAGE_ONLY=0

for arg in "$@"; do
    case "${arg}" in
        --inside-container) ;;
        --package-only) PACKAGE_ONLY=1 ;;
        *) echo "unknown argument: ${arg}" >&2; exit 2 ;;
    esac
done

inside_container=0
for arg in "$@"; do
    [[ "${arg}" = "--inside-container" ]] && inside_container=1
done

prepare_worktree() {
    if [[ -e "${WORKTREE}/.git" ]]; then
        current=$(git -C "${WORKTREE}" rev-parse HEAD)
        if [[ "${current}" != "${LINUX_COMMIT}" ]]; then
            git -C "${LINUX_SRC}" worktree remove --force "${WORKTREE}"
        fi
    fi

    if [[ ! -e "${WORKTREE}/.git" ]]; then
        git -C "${LINUX_SRC}" worktree add --detach "${WORKTREE}" "${LINUX_COMMIT}"
    fi

    current=$(git -C "${WORKTREE}" rev-parse HEAD)
    if [[ "${current}" != "${LINUX_COMMIT}" ]]; then
        echo "ERROR: worktree is at ${current}, expected ${LINUX_COMMIT}" >&2
        exit 1
    fi

    # The kernel's certs/Makefile reads the module-signing key from the source
    # tree. The key is gitignored, so copying it does not dirty the worktree.
    if [[ ! -f "${WORKTREE}/certs/signing_key.pem" ]]; then
        if [[ -f "${LINUX_SRC}/certs/signing_key.pem" ]]; then
            install -m 0600 "${LINUX_SRC}/certs/signing_key.pem" \
                "${WORKTREE}/certs/signing_key.pem"
        else
            mkdir -p "${WORKTREE}/certs"
            openssl req -new -x509 -nodes -days 3650 -newkey rsa:2048 \
                -keyout "${WORKTREE}/certs/signing_key.pem" \
                -out "${WORKTREE}/certs/signing_key.x509" \
                -subj "/CN=k8labs module signing/" >/dev/null 2>&1
        fi
    fi
}

prepare_config() {
    [[ -f "${STOCK_KERNEL_CONFIG}" ]] || {
        echo "ERROR: stock kernel config not found: ${STOCK_KERNEL_CONFIG}" >&2
        echo "Run: make stock-kernel" >&2
        exit 1
    }

    mkdir -p "${OUT}"
    cp "${STOCK_KERNEL_CONFIG}" "${OUT}/.config"
    cat "${ROOT}/configs/fedora-44-kernel.config-fragment" >> "${OUT}/.config"

    # Keep the generated module-signing key that already exists in the source
    # tree; the fragment's k8labs key path is not present in this checkout.
    # CONFIG_MODULE_SIG_KEY is resolved against the output tree, so copy the
    # key there before the build; otherwise the kernel generates a new one.
    sed -i 's|^CONFIG_MODULE_SIG_KEY=.*|CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"|' "${OUT}/.config"
    mkdir -p "${OUT}/certs"
    cp "${WORKTREE}/certs/signing_key.pem" "${OUT}/certs/signing_key.pem"
    sed -i 's|^CONFIG_LOCALVERSION=.*|CONFIG_LOCALVERSION="-k8labs"|' "${OUT}/.config"
    if grep -q '^CONFIG_LOCALVERSION_AUTO=' "${OUT}/.config"; then
        sed -i 's|^CONFIG_LOCALVERSION_AUTO=.*|# CONFIG_LOCALVERSION_AUTO is not set|' "${OUT}/.config"
    else
        echo '# CONFIG_LOCALVERSION_AUTO is not set' >> "${OUT}/.config"
    fi

    # The stock Fedora config builds every driver. A comparison worker only
    # needs the VM hardware, the container filesystems/network, and the
    # scheduler/cgroup/BPF feature set. Disable the bulk categories so the
    # build stays practical; the scheduler-relevant options below are kept
    # identical to the stock kernel (HZ=1000, PREEMPT_LAZY, cgroup v2, BTF).
    "${WORKTREE}/scripts/config" --file "${OUT}/.config" \
        -d DRM -d AGP -d FB -d SOUND -d USB_SUPPORT -d INFINIBAND \
        -d MEDIA_SUPPORT -d COMEDI -d STAGING -d ETHERNET -d WLAN -d WIRELESS \
        -d BT -d CAN -d HAMRADIO -d WWAN -d SCSI -d ATA -d NVME_CORE \
        -d BLK_DEV_NVME -d MMC -d MTD -d MD -d BLK_DEV_DM -d PHYLIB -d PPS \
        -d PTP_1588_CLOCK -d HWMON -d THERMAL -d WATCHDOG -d GPIO -d PINCTRL \
        -d I2C -d SPI -d RTC_CLASS -d LEDS_CLASS -d INPUT -d HID -d SERIO \
        -d FRAMEBUFFER_CONSOLE -d LOGO -d XFS_FS -d OCFS2_FS -d GFS2_FS \
        -d NFS_FS -d CIFS -d 9P_FS -d CEPH_FS -d NTFS3_FS -d ISO9660_FS \
        -d UDF_FS -d EROFS_FS \
        -d DEBUG_INFO_NONE \
        -e DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
        -e DEBUG_INFO_BTF \
        -e DEBUG_INFO_BTF_MODULES \
        -e SCHED_CLASS_EXT \
        -d MODULE_SIG_FORCE \
        -d MODULE_SIG_KEY_TYPE_MLDSA_87 \
        -e MODULE_SIG_KEY_TYPE_RSA

    make -C "${WORKTREE}" O="${OUT}" olddefconfig

    grep -q '^CONFIG_SCHED_CLASS_EXT=y$' "${OUT}/.config"
    grep -q '^CONFIG_DEBUG_INFO_BTF=y$' "${OUT}/.config"
    grep -q '^CONFIG_HZ=1000$' "${OUT}/.config"
    if grep -q '^CONFIG_DEBUG_INFO_NONE=y$' "${OUT}/.config"; then
        echo "ERROR: DEBUG_INFO_NONE survived the merge" >&2
        exit 1
    fi
}

build_kernel() {
    make -C "${WORKTREE}" O="${OUT}" -j"${JOBS}" bzImage modules
}

stage_kernel() {
    rm -rf "${STAGE}"
    mkdir -p "${STAGE}"
    make -C "${WORKTREE}" O="${OUT}" \
        INSTALL_MOD_PATH="${STAGE}" \
        INSTALL_PATH="${STAGE}/boot" \
        install modules_install
    # make install writes the unsuffixed names; the kernel-install layout and
    # the RPM file list use the release-suffixed names.
    install -m 0644 "${STAGE}/boot/vmlinuz" "${STAGE}/boot/vmlinuz-${KERNELRELEASE}"
    install -m 0644 "${OUT}/.config" "${STAGE}/boot/config-${KERNELRELEASE}"
    install -m 0644 "${OUT}/System.map" "${STAGE}/boot/System.map-${KERNELRELEASE}"
    rm -f "${STAGE}/boot/vmlinuz" "${STAGE}/boot/System.map"
    rm -f "${STAGE}/lib/modules/${KERNELRELEASE}/build" \
          "${STAGE}/lib/modules/${KERNELRELEASE}/source"
}

package_kernel() {
    rm -rf "${RPMBUILD}"
    mkdir -p "${RPMBUILD}"
    rpmbuild -bb "${ROOT}/packer/kernel-k8labs.spec" \
        --define "_topdir ${RPMBUILD}" \
        --define "kernelrelease ${KERNELRELEASE}" \
        --define "staging ${STAGE}" \
        --define "debug_package %{nil}" \
        --define "_build_id_links none"
    mkdir -p "${RPMDIR}"
    cp "${RPMBUILD}"/RPMS/x86_64/kernel-k8labs-*.rpm "${RPMDIR}/"
    rpm -qip "${RPMDIR}"/kernel-k8labs-*.rpm
    rpm -qlp "${RPMDIR}"/kernel-k8labs-*.rpm | head -20
}

if [[ "${inside_container}" -eq 0 ]]; then
    prepare_worktree
    inner_args="--inside-container"
    if [[ "${PACKAGE_ONLY}" -eq 1 ]]; then
        inner_args="${inner_args} --package-only"
    fi
    exec podman run --rm --name "k8labs-kbuild-$$" \
        -v "${ROOT}:${ROOT}" \
        -v "${LINUX_SRC}:${LINUX_SRC}:ro" \
        -w "${ROOT}" \
        "${BUILD_IMAGE}" \
        /bin/bash -c "dnf install -y --setopt=install_weak_deps=False \
            gcc gcc-c++ make flex bison bc elfutils-libelf-devel openssl openssl-devel \
            dwarves perl python3 rsync tar xz rpm-build cpio kmod git diffutils \
            findutils >/dev/null && \
            ${ROOT}/scripts/build-custom-kernel.sh ${inner_args}"
fi

if [[ "${PACKAGE_ONLY}" -eq 0 ]]; then
    prepare_config
    build_kernel
fi
stage_kernel
package_kernel
