#!/bin/sh
#
# extensions/download-sysexts.sh — Download sysext binary artifacts
#
# Downloads the pre-built binaries for each sysext extension from upstream
# releases. Each sysext source tree has its binary directory populated so
# that extensions/build.sh can package them into squashfs images.
#
# Usage: download-sysexts.sh [--dry-run] [sysext-name ...]
#
#   --dry-run     Print what would be downloaded without downloading
#   sysext-name   Download only the specified sysext(s); default: all
#
# Supported sysexts: cri-o, crun, kubelet, cni, etcd, kubernetes-cp

set -u

SCRIPT_DIR="$(dirname "$0")"
SYSEXT_DIR="$(cd "${SCRIPT_DIR}/../sysext" && pwd)"

# ---------------------------------------------------------------------------
# Config: upstream versions and URLs
# Keep these in sync with extension-release.d/ metadata.
# ---------------------------------------------------------------------------
KUBELET_VERSION="v1.32.13"
CRIO_VERSION="v1.35.5"
CRUN_VERSION="1.28"
CNI_VERSION="v1.9.1"

KUBELET_URL="https://dl.k8s.io/${KUBELET_VERSION}/bin/linux/amd64/kubelet"
# cri-o releases are now distributed via Google Cloud Storage
CRIO_URL="https://storage.googleapis.com/cri-o/artifacts/cri-o.amd64.${CRIO_VERSION}.tar.gz"
CRUN_URL="https://github.com/containers/crun/releases/download/${CRUN_VERSION}/crun-${CRUN_VERSION}-linux-amd64"
CNI_URL="https://github.com/containernetworking/plugins/releases/download/${CNI_VERSION}/cni-plugins-linux-amd64-${CNI_VERSION}.tgz"

ETCD_VERSION="v3.5.17"
KUBERNETES_CP_VERSION="v1.32.13"

ETCD_URL="https://github.com/etcd-io/etcd/releases/download/${ETCD_VERSION}/etcd-${ETCD_VERSION}-linux-amd64.tar.gz"
KUBERNETES_CP_URL="https://dl.k8s.io/${KUBERNETES_CP_VERSION}/kubernetes-server-linux-amd64.tar.gz"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
usage() {
    cat <<'USAGE_EOF'
Usage: download-sysexts.sh [--dry-run] [--sequential] [sysext-name ...]

Download sysext binary artifacts from upstream releases.

Arguments:
  --dry-run       Print what would be downloaded without downloading
  --sequential    Download sysexts one at a time (default: parallel)
  sysext-name     Download only the specified sysext(s); default: all

Supported sysexts: cri-o, crun, kubelet, cni, etcd, kubernetes-cp
USAGE_EOF
}

die() {
    echo "Error: $*" >&2
    exit 1
}

info() {
    echo "==> $*"
}

# ---------------------------------------------------------------------------
# Cache check: skip download if all expected paths exist and are non-empty
# ---------------------------------------------------------------------------
check_cached() {
    desc="$1"
    shift
    for f in "$@"; do
        if [ ! -f "$f" ] || [ ! -s "$f" ]; then
            return 1  # missing or empty — need to download
        fi
    done
    echo "  ${desc}: already available, skipping download"
    return 0  # all exist — cache hit
}

dry_run=0
sequential=0

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        --sequential)
            sequential=1
            shift
            ;;
        *)
            break
            ;;
    esac
done

requested="$*"  # empty = all

# ---------------------------------------------------------------------------
# Download function
# ---------------------------------------------------------------------------
download_file() {
    url="$1"
    dest="$2"
    desc="$3"

    if [ "$dry_run" -eq 1 ]; then
        echo "  [DRY-RUN] would download: ${url}"
        echo "            to:           ${dest}"
        return 0
    fi

    mkdir -p "$(dirname "$dest")"

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$dest" "$url" || die "failed to download ${desc} from ${url}"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$dest" "$url" || die "failed to download ${desc} from ${url}"
    else
        die "neither curl nor wget found — cannot download ${desc}"
    fi

    chmod 755 "$dest"
    echo "  Downloaded ${desc}: ${dest}"
}

download_archive() {
    url="$1"
    tmpfile="$2"
    desc="$3"

    if [ "$dry_run" -eq 1 ]; then
        echo "  [DRY-RUN] would download archive: ${url}"
        echo "            to:                   ${tmpfile}"
        return 0
    fi

    mkdir -p "$(dirname "$tmpfile")"

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$tmpfile" "$url" || die "failed to download ${desc} archive from ${url}"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$tmpfile" "$url" || die "failed to download ${desc} archive from ${url}"
    else
        die "neither curl nor wget found — cannot download ${desc}"
    fi

    echo "  Downloaded ${desc} archive: ${tmpfile}"
}

# ---------------------------------------------------------------------------
# Sysext downloaders
# ---------------------------------------------------------------------------
download_kubelet() {
    target="${SYSEXT_DIR}/kubelet/usr/bin/kubelet"
    check_cached "kubelet ${KUBELET_VERSION}" "$target" && return 0
    download_file "$KUBELET_URL" "$target" "kubelet ${KUBELET_VERSION}"
}

download_crio() {
    target_dir="${SYSEXT_DIR}/cri-o/usr/bin"
    check_cached "cri-o ${CRIO_VERSION}" "${target_dir}/crio" "${target_dir}/crictl" "${target_dir}/pinns" && return 0

    tmpdir="${SYSEXT_DIR}/cri-o/.download.tmp"
    archive="${tmpdir}/crio.tar.gz"
    mkdir -p "$tmpdir"

    download_archive "$CRIO_URL" "$archive" "cri-o ${CRIO_VERSION}"

    if [ "$dry_run" -eq 0 ]; then
        target_dir="${SYSEXT_DIR}/cri-o/usr/bin"
        mkdir -p "$target_dir"

        # Extract only the binaries we need from the tarball
        tar -xzf "$archive" -C "$tmpdir" 2>/dev/null || die "failed to extract cri-o archive"

        # The tarball contains bin/ with the binaries
        # Note: crio-conmon and crio-conmonrs are distributed separately
        # from the containers/conmon and containers/conmon-rs projects.
        for bin in crio crictl pinns; do
            found=0
            for f in "${tmpdir}"/*/bin/"${bin}" "${tmpdir}"/bin/"${bin}"; do
                if [ -f "$f" ]; then
                    cp "$f" "${target_dir}/${bin}"
                    chmod 755 "${target_dir}/${bin}"
                    echo "  Extracted ${bin} from cri-o archive"
                    found=1
                    break
                fi
            done
            if [ "$found" -eq 0 ]; then
                echo "  Warning: ${bin} not found in cri-o archive" >&2
            fi
        done

        rm -rf "$tmpdir"
    fi
}

download_crun() {
    target="${SYSEXT_DIR}/crun/usr/bin/crun"
    check_cached "crun ${CRUN_VERSION}" "$target" && return 0
    download_file "$CRUN_URL" "$target" "crun ${CRUN_VERSION}"
}

download_cni() {
    target_dir="${SYSEXT_DIR}/cni/usr/lib/cni"
    # Check a representative plugin to gauge cache status
    check_cached "cni-plugins ${CNI_VERSION}" "${target_dir}/bridge" "${target_dir}/host-local" && return 0

    tmpdir="${SYSEXT_DIR}/cni/.download.tmp"
    archive="${tmpdir}/cni-plugins.tgz"
    mkdir -p "$tmpdir"

    download_archive "$CNI_URL" "$archive" "cni-plugins ${CNI_VERSION}"

    if [ "$dry_run" -eq 0 ]; then
        target_dir="${SYSEXT_DIR}/cni/usr/lib/cni"
        mkdir -p "$target_dir"

        # Extract all CNI plugins
        tar -xzf "$archive" -C "$target_dir" || die "failed to extract cni-plugins archive"
        chmod 755 "$target_dir"/*

        rm -rf "$tmpdir"
        echo "  Extracted CNI plugins to ${target_dir}"
    fi
}

download_etcd() {
    target_dir="${SYSEXT_DIR}/etcd/usr/bin"
    check_cached "etcd ${ETCD_VERSION}" "${target_dir}/etcd" "${target_dir}/etcdctl" && return 0

    tmpdir="${SYSEXT_DIR}/etcd/.download.tmp"
    archive="${tmpdir}/etcd.tar.gz"
    mkdir -p "$tmpdir"

    download_archive "$ETCD_URL" "$archive" "etcd ${ETCD_VERSION}"

    if [ "$dry_run" -eq 0 ]; then
        target_dir="${SYSEXT_DIR}/etcd/usr/bin"
        mkdir -p "$target_dir"

        tar -xzf "$archive" -C "$tmpdir" 2>/dev/null || die "failed to extract etcd archive"

        # The tarball contains a single directory: etcd-v3.5.17-linux-amd64/
        for bin in etcd etcdctl; do
            found=0
            for f in "${tmpdir}"/*/"${bin}" "${tmpdir}"/"${bin}"; do
                if [ -f "$f" ]; then
                    cp "$f" "${target_dir}/${bin}"
                    chmod 755 "${target_dir}/${bin}"
                    echo "  Extracted ${bin} from etcd archive"
                    found=1
                    break
                fi
            done
            if [ "$found" -eq 0 ]; then
                echo "  Warning: ${bin} not found in etcd archive" >&2
            fi
        done

        rm -rf "$tmpdir"
    fi
}

download_kubernetes_cp() {
    target_dir="${SYSEXT_DIR}/kubernetes-cp/usr/bin"
    check_cached "kubernetes-cp ${KUBERNETES_CP_VERSION}" "${target_dir}/kube-apiserver" "${target_dir}/kube-controller-manager" "${target_dir}/kube-scheduler" "${target_dir}/kubectl" && return 0

    tmpdir="${SYSEXT_DIR}/kubernetes-cp/.download.tmp"
    archive="${tmpdir}/kubernetes-server.tar.gz"
    mkdir -p "$tmpdir"

    download_archive "$KUBERNETES_CP_URL" "$archive" "kubernetes-server ${KUBERNETES_CP_VERSION}"

    if [ "$dry_run" -eq 0 ]; then
        target_dir="${SYSEXT_DIR}/kubernetes-cp/usr/bin"
        mkdir -p "$target_dir"

        tar -xzf "$archive" -C "$tmpdir" 2>/dev/null || die "failed to extract kubernetes-server archive"

        # The tarball contains kubernetes/server/bin/
        for bin in kube-apiserver kube-controller-manager kube-scheduler kubectl; do
            found=0
            for f in "${tmpdir}"/kubernetes/server/bin/"${bin}" "${tmpdir}"/server/bin/"${bin}" "${tmpdir}"/bin/"${bin}"; do
                if [ -f "$f" ]; then
                    cp "$f" "${target_dir}/${bin}"
                    chmod 755 "${target_dir}/${bin}"
                    echo "  Extracted ${bin} from kubernetes-server archive"
                    found=1
                    break
                fi
            done
            if [ "$found" -eq 0 ]; then
                echo "  Warning: ${bin} not found in kubernetes-server archive" >&2
            fi
        done

        rm -rf "$tmpdir"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    # Map of sysext name -> download function
    downloads="cri-o crun kubelet cni etcd kubernetes-cp"

    if [ -n "$requested" ]; then
        downloads="$requested"
    fi

    if [ "$sequential" -eq 1 ]; then
        # Sequential mode: one at a time
        for name in $downloads; do
            case "$name" in
                cri-o)    info "Downloading cri-o...";    download_crio ;;
                crun)     info "Downloading crun...";     download_crun ;;
                kubelet)  info "Downloading kubelet...";  download_kubelet ;;
                cni)      info "Downloading cni...";      download_cni ;;
                etcd)     info "Downloading etcd...";     download_etcd ;;
                kubernetes-cp) info "Downloading kubernetes-cp..."; download_kubernetes_cp ;;
                *)
                    echo "Warning: unknown sysext '${name}' — skipping" >&2
                    ;;
            esac
        done
    else
        # Parallel mode: launch downloads as background processes
        pids=""
        failures=0
        for name in $downloads; do
            # Check cache before spawning a process
            case "$name" in
                cri-o)
                    if check_cached "cri-o ${CRIO_VERSION}" \
                        "${SYSEXT_DIR}/cri-o/usr/bin/crio" \
                        "${SYSEXT_DIR}/cri-o/usr/bin/crictl" \
                        "${SYSEXT_DIR}/cri-o/usr/bin/pinns"
                    then
                        continue
                    fi
                    ;;
                crun)
                    if check_cached "crun ${CRUN_VERSION}" \
                        "${SYSEXT_DIR}/crun/usr/bin/crun"
                    then
                        continue
                    fi
                    ;;
                kubelet)
                    if check_cached "kubelet ${KUBELET_VERSION}" \
                        "${SYSEXT_DIR}/kubelet/usr/bin/kubelet"
                    then
                        continue
                    fi
                    ;;
                cni)
                    if check_cached "cni-plugins ${CNI_VERSION}" \
                        "${SYSEXT_DIR}/cni/usr/lib/cni/bridge" \
                        "${SYSEXT_DIR}/cni/usr/lib/cni/host-local"
                    then
                        continue
                    fi
                    ;;
                etcd)
                    if check_cached "etcd ${ETCD_VERSION}" \
                        "${SYSEXT_DIR}/etcd/usr/bin/etcd" \
                        "${SYSEXT_DIR}/etcd/usr/bin/etcdctl"
                    then
                        continue
                    fi
                    ;;
                kubernetes-cp)
                    if check_cached "kubernetes-cp ${KUBERNETES_CP_VERSION}" \
                        "${SYSEXT_DIR}/kubernetes-cp/usr/bin/kube-apiserver" \
                        "${SYSEXT_DIR}/kubernetes-cp/usr/bin/kube-controller-manager" \
                        "${SYSEXT_DIR}/kubernetes-cp/usr/bin/kube-scheduler" \
                        "${SYSEXT_DIR}/kubernetes-cp/usr/bin/kubectl"
                    then
                        continue
                    fi
                    ;;
                *)
                    echo "Warning: unknown sysext '${name}' — skipping" >&2
                    continue
                    ;;
            esac

            # Spawn download in a background subshell with per-sysext lock
            (
                set -e
                trap 'rm -rf "${SYSEXT_DIR}/${name}.lock"' EXIT
                mkdir "${SYSEXT_DIR}/${name}.lock" 2>/dev/null || return 0
                info "Downloading ${name}..."
                case "$name" in
                    cri-o)    download_crio ;;
                    crun)     download_crun ;;
                    kubelet)  download_kubelet ;;
                    cni)      download_cni ;;
                    etcd)     download_etcd ;;
                    kubernetes-cp) download_kubernetes_cp ;;
                esac
            ) &
            pids="$pids $!"
        done

        # Collect exit codes from all background jobs
        for pid in $pids; do
            [ -z "$pid" ] && continue
            wait "$pid" || failures=$((failures + 1))
        done
        [ "$failures" -eq 0 ] || die "$failures download(s) failed"
    fi

    info "Done."
}

main
