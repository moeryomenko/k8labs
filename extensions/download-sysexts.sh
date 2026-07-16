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
# Supported sysexts: cri-o, crun, kubelet, cni

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
CRIO_URL="https://github.com/cri-o/cri-o/releases/download/${CRIO_VERSION}/crio-${CRIO_VERSION}-linux-amd64.tar.gz"
CRUN_URL="https://github.com/containers/crun/releases/download/${CRUN_VERSION}/crun-${CRUN_VERSION}-linux-amd64"
CNI_URL="https://github.com/containernetworking/plugins/releases/download/${CNI_VERSION}/cni-plugins-linux-amd64-${CNI_VERSION}.tgz"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
usage() {
    cat <<'USAGE_EOF'
Usage: download-sysexts.sh [--dry-run] [sysext-name ...]

Download sysext binary artifacts from upstream releases.

Arguments:
  --dry-run     Print what would be downloaded without downloading
  sysext-name   Download only the specified sysext(s); default: all

Supported sysexts: cri-o, crun, kubelet, cni
USAGE_EOF
}

die() {
    echo "Error: $*" >&2
    exit 1
}

info() {
    echo "==> $*"
}

dry_run=0

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
case "${1:-}" in
    --help|-h)
        usage
        exit 0
        ;;
    --dry-run)
        dry_run=1
        shift
        ;;
esac

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
    download_file "$KUBELET_URL" "$target" "kubelet ${KUBELET_VERSION}"
}

download_crio() {
    tmpdir="${SYSEXT_DIR}/crio/.download.tmp"
    archive="${tmpdir}/crio.tar.gz"
    mkdir -p "$tmpdir"

    download_archive "$CRIO_URL" "$archive" "cri-o ${CRIO_VERSION}"

    if [ "$dry_run" -eq 0 ]; then
        target_dir="${SYSEXT_DIR}/crio/usr/bin"
        mkdir -p "$target_dir"

        # Extract only the binaries we need from the tarball
        tar -xzf "$archive" -C "$tmpdir" 2>/dev/null || die "failed to extract cri-o archive"

        # The tarball contains bin/ with the binaries
        for bin in crio crictl crio-conmon crio-conmonrs; do
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
    download_file "$CRUN_URL" "$target" "crun ${CRUN_VERSION}"
}

download_cni() {
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

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    # Map of sysext name -> download function
    downloads="cri-o crun kubelet cni"

    if [ -n "$requested" ]; then
        downloads="$requested"
    fi

    for name in $downloads; do
        case "$name" in
            cri-o)    info "Downloading cri-o...";    download_crio ;;
            crun)     info "Downloading crun...";     download_crun ;;
            kubelet)  info "Downloading kubelet...";  download_kubelet ;;
            cni)      info "Downloading cni...";      download_cni ;;
            *)
                echo "Warning: unknown sysext '${name}' — skipping" >&2
                ;;
        esac
    done

    info "Done."
}

main
