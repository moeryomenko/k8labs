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
# Supported sysexts: cri-o, crun, kubelet, cni, etcd, kubernetes-cp, perfetto

set -u

SCRIPT_DIR="$(dirname "$0")"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
SYSEXT_DIR="$(cd "${SCRIPT_DIR}/../sysext" && pwd -P)"
MANIFEST_FILE="${REPO_ROOT}/extensions/manifest.yaml"
LOCK_FILE="${REPO_ROOT}/versions.lock.yaml"

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

Supported sysexts: cri-o, crun, kubelet, cni, etcd, kubernetes-cp, perfetto
USAGE_EOF
}

die() {
    echo "Error: $*" >&2
    exit 1
}

# Resolve each source through the immutable artifact lock and reject manifest
# drift before any cache check or download.
ARTIFACT_VALUES="$(python3 - "${MANIFEST_FILE}" "${LOCK_FILE}" "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(sys.argv[3]) / "scripts"))
from lock import load_lock

manifest = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
lock = load_lock(Path(sys.argv[2]))
entries = {item["name"]: item for item in manifest["sysexts"]}
artifacts = {
    item["id"]: item
    for item in lock["artifacts"] + lock["guest_upgrade_artifacts"]
}
required = ("kubelet", "cri-o", "crun", "cni", "etcd", "kubernetes-cp", "perfetto")
values = [manifest["settings"]["kernel_version"]]
for name in required:
    entry = entries.get(name)
    if not isinstance(entry, dict):
        raise SystemExit(f"extension manifest is missing sysext {name}")
    sources = entry.get("sources")
    if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
        raise SystemExit(f"{name} must have exactly one locked source")
    source = sources[0]
    artifact_id = source.get("artifact_id")
    artifact = artifacts.get(artifact_id)
    if artifact is None:
        raise SystemExit(f"{name} source references an unknown lock artifact: {artifact_id!r}")
    version = entry.get("version")
    if artifact.get("version") != version:
        raise SystemExit(f"{name} manifest version does not match artifact {artifact_id}")
    if source.get("url") != artifact.get("url"):
        raise SystemExit(f"{name} manifest URL does not match artifact {artifact_id}")
    values.extend((version, artifact["sha256"], artifact["url"]))
if any(not isinstance(value, str) or not value for value in values):
    raise SystemExit("extension manifest or artifact lock contains an empty value")
print("|".join(values))
PY
)" || die "unable to resolve locked extension artifacts from ${MANIFEST_FILE} and ${LOCK_FILE}"
previous_ifs=$IFS
IFS='|'
read -r KERNEL_VERSION \
    KUBELET_VERSION KUBELET_SHA256 KUBELET_URL \
    CRIO_VERSION CRIO_SHA256 CRIO_URL \
    CRUN_VERSION CRUN_SHA256 CRUN_URL \
    CNI_VERSION CNI_SHA256 CNI_URL \
    ETCD_VERSION ETCD_SHA256 ETCD_URL \
    KUBERNETES_CP_VERSION KUBERNETES_CP_SHA256 KUBERNETES_CP_URL \
    PERFETTO_VERSION PERFETTO_SHA256 PERFETTO_URL <<EOF
${ARTIFACT_VALUES}
EOF
IFS=$previous_ifs

info() {
    echo "==> $*"
}

# ---------------------------------------------------------------------------
# Cache check: require the lock digest and the current digest of every target.
# ---------------------------------------------------------------------------
sha256_file() {
    file="$1"
    output="$(sha256sum "$file" 2>/dev/null)" || return 1
    digest="${output%% *}"
    case "$digest" in
        *[!0-9a-f]*|'') return 1 ;;
    esac
    [ "${#digest}" -eq 64 ] || return 1
    printf '%s\n' "$digest"
}

marker_content() {
    version="$1"
    artifact_sha256="$2"
    shift 2
    printf 'version=%s\nartifact_sha256=%s\n' "$version" "$artifact_sha256"
    for file in "$@"; do
        [ -f "$file" ] && [ -s "$file" ] || return 1
        digest="$(sha256_file "$file")" || return 1
        printf 'file_sha256=%s  %s\n' "$digest" "$file"
    done
}

check_cached() {
    desc="$1"
    version="$2"
    artifact_sha256="$3"
    marker="$4"
    shift 4
    [ -f "$marker" ] || return 1
    expected="$(marker_content "$version" "$artifact_sha256" "$@")" || return 1
    if [ "$(cat "$marker")" != "$expected" ]; then
        return 1
    fi
    echo "  ${desc}: already available, skipping download"
    return 0
}

version_marker() {
    printf '%s\n' "${REPO_ROOT}/build/.sysext-download-cache/$1.version"
}

record_version() {
    name="$1"
    version="$2"
    artifact_sha256="$3"
    shift 3
    [ "$dry_run" -eq 0 ] || return 0
    marker="$(version_marker "$name")"
    mkdir -p "$(dirname "$marker")"
    temporary="$(mktemp "$(dirname "$marker")/.${name}.XXXXXX")" || die "unable to create cache marker for ${name}"
    if ! marker_content "$version" "$artifact_sha256" "$@" > "$temporary"; then
        rm -f -- "$temporary"
        die "unable to record verified files for ${name}"
    fi
    mv -f -- "$temporary" "$marker" || die "unable to publish cache marker for ${name}"
}

# ---------------------------------------------------------------------------
# is_elf_binary — Return 0 only if $1 exists, is non-empty, and starts with
# the ELF magic bytes (0x7f 'E'). Used for the perfetto cache check so a
# stale text wrapper (e.g. the get.perfetto.dev Python prebuilt-wrapper) is
# never treated as a cache hit.
# ---------------------------------------------------------------------------
is_elf_binary() {
    [ -f "$1" ] || return 1
    [ -s "$1" ] || return 1
    # ELF magic is 0x7f 'E' 'L' 'F' — the first three bytes hex-encode as
    # 7f454c. (head -c 2 alone yields only "7f45" and cannot match.)
    magic="$(head -c 3 "$1" | od -An -tx1 | tr -d ' \n')"
    [ "$magic" = "7f454c" ]
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
    expected_sha256="$4"

    if [ "$dry_run" -eq 1 ]; then
        echo "  [DRY-RUN] would download: ${url}"
        echo "            to:           ${dest}"
        echo "            sha256:       ${expected_sha256}"
        return 0
    fi

    mkdir -p "$(dirname "$dest")"
    temporary="$(mktemp "$(dirname "$dest")/.$(basename "$dest").XXXXXX")" || die "unable to create temporary file for ${desc}"

    if command -v curl >/dev/null 2>&1; then
        if ! curl -fsSL -o "$temporary" "$url"; then
            rm -f -- "$temporary"
            die "failed to download ${desc} from ${url}"
        fi
    elif command -v wget >/dev/null 2>&1; then
        if ! wget -q -O "$temporary" "$url"; then
            rm -f -- "$temporary"
            die "failed to download ${desc} from ${url}"
        fi
    else
        rm -f -- "$temporary"
        die "neither curl nor wget found — cannot download ${desc}"
    fi

    actual_sha256="$(sha256_file "$temporary")" || {
        rm -f -- "$temporary"
        die "failed to compute sha256 for ${desc}"
    }
    if [ "$actual_sha256" != "$expected_sha256" ]; then
        rm -f -- "$temporary"
        die "sha256 mismatch for ${desc}: expected ${expected_sha256}, got ${actual_sha256}"
    fi
    chmod 755 "$temporary" || {
        rm -f -- "$temporary"
        die "failed to set executable mode for ${desc}"
    }
    mv -f -- "$temporary" "$dest" || die "failed to install ${desc} at ${dest}"
    echo "  Verified ${desc} sha256: ${actual_sha256}"
    echo "  Downloaded ${desc}: ${dest}"
}

download_archive() {
    url="$1"
    tmpfile="$2"
    desc="$3"
    expected_sha256="$4"

    if [ "$dry_run" -eq 1 ]; then
        echo "  [DRY-RUN] would download archive: ${url}"
        echo "            to:                   ${tmpfile}"
        echo "            sha256:               ${expected_sha256}"
        return 0
    fi

    mkdir -p "$(dirname "$tmpfile")"
    temporary="$(mktemp "$(dirname "$tmpfile")/.$(basename "$tmpfile").XXXXXX")" || die "unable to create temporary archive for ${desc}"

    if command -v curl >/dev/null 2>&1; then
        if ! curl -fsSL -o "$temporary" "$url"; then
            rm -f -- "$temporary"
            die "failed to download ${desc} archive from ${url}"
        fi
    elif command -v wget >/dev/null 2>&1; then
        if ! wget -q -O "$temporary" "$url"; then
            rm -f -- "$temporary"
            die "failed to download ${desc} archive from ${url}"
        fi
    else
        rm -f -- "$temporary"
        die "neither curl nor wget found — cannot download ${desc}"
    fi

    actual_sha256="$(sha256_file "$temporary")" || {
        rm -f -- "$temporary"
        die "failed to compute sha256 for ${desc} archive"
    }
    if [ "$actual_sha256" != "$expected_sha256" ]; then
        rm -f -- "$temporary"
        die "sha256 mismatch for ${desc} archive: expected ${expected_sha256}, got ${actual_sha256}"
    fi
    mv -f -- "$temporary" "$tmpfile" || die "failed to install ${desc} archive at ${tmpfile}"
    echo "  Verified ${desc} archive sha256: ${actual_sha256}"
    echo "  Downloaded ${desc} archive: ${tmpfile}"
}

# ---------------------------------------------------------------------------
# Sysext downloaders
# ---------------------------------------------------------------------------
download_kubelet() {
    target="${SYSEXT_DIR}/kubelet/usr/bin/kubelet"
    check_cached "kubelet ${KUBELET_VERSION}" "$KUBELET_VERSION" "$KUBELET_SHA256" "$(version_marker kubelet)" "$target" && return 0
    download_file "$KUBELET_URL" "$target" "kubelet ${KUBELET_VERSION}" "$KUBELET_SHA256"
    record_version kubelet "$KUBELET_VERSION" "$KUBELET_SHA256" "$target"
}

download_crio() {
    target_dir="${SYSEXT_DIR}/cri-o/usr/bin"
    check_cached "cri-o ${CRIO_VERSION}" "$CRIO_VERSION" "$CRIO_SHA256" "$(version_marker cri-o)" "${target_dir}/crio" "${target_dir}/crictl" "${target_dir}/pinns" && return 0

    if [ "$dry_run" -eq 0 ]; then
        mkdir -p "${SYSEXT_DIR}/cri-o"
        tmpdir="$(mktemp -d "${SYSEXT_DIR}/cri-o/.download.XXXXXX")" || die "unable to create CRI-O extraction directory"
    else
        tmpdir="${SYSEXT_DIR}/cri-o/.download-dry-run"
    fi
    archive="${tmpdir}/crio.tar.gz"

    download_archive "$CRIO_URL" "$archive" "cri-o ${CRIO_VERSION}" "$CRIO_SHA256"

    if [ "$dry_run" -eq 0 ]; then
        target_dir="${SYSEXT_DIR}/cri-o/usr/bin"
        mkdir -p "$target_dir"
        extract_dir="${tmpdir}/extracted"
        mkdir -p "$extract_dir"

        # Extract only the binaries we need from the tarball
        tar -xzf "$archive" -C "$extract_dir" 2>/dev/null || die "failed to extract cri-o archive"

        # The tarball contains bin/ with the binaries
        # Note: crio-conmon and crio-conmonrs are distributed separately
        # from the containers/conmon and containers/conmon-rs projects.
        for bin in crio crictl pinns; do
            found=0
            for f in "${extract_dir}"/*/bin/"${bin}" "${extract_dir}"/bin/"${bin}"; do
                if [ -f "$f" ]; then
                    cp "$f" "${target_dir}/${bin}" || die "failed to install ${bin} from cri-o archive"
                    chmod 755 "${target_dir}/${bin}" || die "failed to set executable mode for ${bin}"
                    echo "  Extracted ${bin} from cri-o archive"
                    found=1
                    break
                fi
            done
            if [ "$found" -eq 0 ]; then
                die "required binary ${bin} not found in verified cri-o archive"
            fi
        done

        rm -rf "$tmpdir"
    fi
    record_version cri-o "$CRIO_VERSION" "$CRIO_SHA256" "${target_dir}/crio" "${target_dir}/crictl" "${target_dir}/pinns"
}

download_crun() {
    target="${SYSEXT_DIR}/crun/usr/bin/crun"
    check_cached "crun ${CRUN_VERSION}" "$CRUN_VERSION" "$CRUN_SHA256" "$(version_marker crun)" "$target" && return 0
    download_file "$CRUN_URL" "$target" "crun ${CRUN_VERSION}" "$CRUN_SHA256"
    record_version crun "$CRUN_VERSION" "$CRUN_SHA256" "$target"
}

download_cni() {
    target_dir="${SYSEXT_DIR}/cni/usr/lib/cni"
    check_cached "cni-plugins ${CNI_VERSION}" "$CNI_VERSION" "$CNI_SHA256" "$(version_marker cni)" "${target_dir}"/* && return 0

    if [ "$dry_run" -eq 0 ]; then
        mkdir -p "${SYSEXT_DIR}/cni"
        tmpdir="$(mktemp -d "${SYSEXT_DIR}/cni/.download.XXXXXX")" || die "unable to create CNI extraction directory"
    else
        tmpdir="${SYSEXT_DIR}/cni/.download-dry-run"
    fi
    archive="${tmpdir}/cni-plugins.tgz"

    download_archive "$CNI_URL" "$archive" "cni-plugins ${CNI_VERSION}" "$CNI_SHA256"

    if [ "$dry_run" -eq 0 ]; then
        target_dir="${SYSEXT_DIR}/cni/usr/lib/cni"
        mkdir -p "$target_dir"
        extract_dir="${tmpdir}/extracted"
        mkdir -p "$extract_dir"

        # Extract and validate before copying any plugins into the source tree.
        tar -xzf "$archive" -C "$extract_dir" || die "failed to extract cni-plugins archive"
        [ -f "${extract_dir}/bridge" ] || die "bridge plugin missing from verified CNI archive"
        [ -f "${extract_dir}/host-local" ] || die "host-local plugin missing from verified CNI archive"
        for plugin in "${extract_dir}"/*; do
            [ -f "$plugin" ] || continue
            plugin_name="$(basename "$plugin")"
            cp "$plugin" "${target_dir}/${plugin_name}" || die "failed to install CNI plugin ${plugin_name}"
            chmod 755 "${target_dir}/${plugin_name}" || die "failed to set executable mode for CNI plugin ${plugin_name}"
        done

        rm -rf "$tmpdir"
        echo "  Extracted CNI plugins to ${target_dir}"
    fi
    record_version cni "$CNI_VERSION" "$CNI_SHA256" "${target_dir}"/*
}

download_etcd() {
    target_dir="${SYSEXT_DIR}/etcd/usr/bin"
    check_cached "etcd ${ETCD_VERSION}" "$ETCD_VERSION" "$ETCD_SHA256" "$(version_marker etcd)" "${target_dir}/etcd" "${target_dir}/etcdctl" && return 0

    if [ "$dry_run" -eq 0 ]; then
        mkdir -p "${SYSEXT_DIR}/etcd"
        tmpdir="$(mktemp -d "${SYSEXT_DIR}/etcd/.download.XXXXXX")" || die "unable to create etcd extraction directory"
    else
        tmpdir="${SYSEXT_DIR}/etcd/.download-dry-run"
    fi
    archive="${tmpdir}/etcd.tar.gz"

    download_archive "$ETCD_URL" "$archive" "etcd ${ETCD_VERSION}" "$ETCD_SHA256"

    if [ "$dry_run" -eq 0 ]; then
        target_dir="${SYSEXT_DIR}/etcd/usr/bin"
        mkdir -p "$target_dir"
        extract_dir="${tmpdir}/extracted"
        mkdir -p "$extract_dir"

        tar -xzf "$archive" -C "$extract_dir" 2>/dev/null || die "failed to extract etcd archive"

        # The tarball contains a versioned directory with etcd and etcdctl.
        for bin in etcd etcdctl; do
            found=0
            for f in "${extract_dir}"/*/"${bin}" "${extract_dir}"/"${bin}"; do
                if [ -f "$f" ]; then
                    cp "$f" "${target_dir}/${bin}" || die "failed to install ${bin} from etcd archive"
                    chmod 755 "${target_dir}/${bin}" || die "failed to set executable mode for ${bin}"
                    echo "  Extracted ${bin} from etcd archive"
                    found=1
                    break
                fi
            done
            if [ "$found" -eq 0 ]; then
                die "required binary ${bin} not found in verified etcd archive"
            fi
        done

        rm -rf "$tmpdir"
    fi
    record_version etcd "$ETCD_VERSION" "$ETCD_SHA256" "${target_dir}/etcd" "${target_dir}/etcdctl"
}

download_perfetto() {
    target="${SYSEXT_DIR}/perfetto/usr/bin/tracebox"
    if is_elf_binary "$target" && check_cached "tracebox ${PERFETTO_VERSION}" "$PERFETTO_VERSION" "$PERFETTO_SHA256" "$(version_marker perfetto)" "$target"; then
        echo "  tracebox: already available, skipping download"
        return 0
    fi
    download_file "$PERFETTO_URL" "$target" "tracebox ${PERFETTO_VERSION}" "$PERFETTO_SHA256"

    if [ "$dry_run" -eq 0 ]; then
        is_elf_binary "$target" || die "tracebox download is not an ELF binary"
    fi
    record_version perfetto "$PERFETTO_VERSION" "$PERFETTO_SHA256" "$target"
}

download_kubernetes_cp() {
    target_dir="${SYSEXT_DIR}/kubernetes-cp/usr/bin"
    check_cached "kubernetes-cp ${KUBERNETES_CP_VERSION}" "$KUBERNETES_CP_VERSION" "$KUBERNETES_CP_SHA256" "$(version_marker kubernetes-cp)" "${target_dir}/kube-apiserver" "${target_dir}/kube-controller-manager" "${target_dir}/kube-scheduler" "${target_dir}/kubectl" && return 0

    if [ "$dry_run" -eq 0 ]; then
        mkdir -p "${SYSEXT_DIR}/kubernetes-cp"
        tmpdir="$(mktemp -d "${SYSEXT_DIR}/kubernetes-cp/.download.XXXXXX")" || die "unable to create Kubernetes extraction directory"
    else
        tmpdir="${SYSEXT_DIR}/kubernetes-cp/.download-dry-run"
    fi
    archive="${tmpdir}/kubernetes-server.tar.gz"

    download_archive "$KUBERNETES_CP_URL" "$archive" "kubernetes-server ${KUBERNETES_CP_VERSION}" "$KUBERNETES_CP_SHA256"

    if [ "$dry_run" -eq 0 ]; then
        target_dir="${SYSEXT_DIR}/kubernetes-cp/usr/bin"
        mkdir -p "$target_dir"
        extract_dir="${tmpdir}/extracted"
        mkdir -p "$extract_dir"

        tar -xzf "$archive" -C "$extract_dir" 2>/dev/null || die "failed to extract kubernetes-server archive"

        # The tarball contains kubernetes/server/bin/
        for bin in kube-apiserver kube-controller-manager kube-scheduler kubectl; do
            found=0
            for f in "${extract_dir}"/kubernetes/server/bin/"${bin}" "${extract_dir}"/server/bin/"${bin}" "${extract_dir}"/bin/"${bin}"; do
                if [ -f "$f" ]; then
                    cp "$f" "${target_dir}/${bin}" || die "failed to install ${bin} from Kubernetes server archive"
                    chmod 755 "${target_dir}/${bin}" || die "failed to set executable mode for ${bin}"
                    echo "  Extracted ${bin} from kubernetes-server archive"
                    found=1
                    break
                fi
            done
            if [ "$found" -eq 0 ]; then
                die "required binary ${bin} not found in verified Kubernetes server archive"
            fi
        done

        rm -rf "$tmpdir"
    fi
    record_version kubernetes-cp "$KUBERNETES_CP_VERSION" "$KUBERNETES_CP_SHA256" "${target_dir}/kube-apiserver" "${target_dir}/kube-controller-manager" "${target_dir}/kube-scheduler" "${target_dir}/kubectl"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required to verify locked guest artifacts"

    # Map of sysext name -> download function
    downloads="cri-o crun kubelet cni etcd kubernetes-cp perfetto"

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
                perfetto)  info "Downloading perfetto...";  download_perfetto ;;
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
                        "$CRIO_VERSION" "$CRIO_SHA256" "$(version_marker cri-o)" \
                        "${SYSEXT_DIR}/cri-o/usr/bin/crio" \
                        "${SYSEXT_DIR}/cri-o/usr/bin/crictl" \
                        "${SYSEXT_DIR}/cri-o/usr/bin/pinns"
                    then
                        continue
                    fi
                    ;;
                crun)
                    if check_cached "crun ${CRUN_VERSION}" \
                        "$CRUN_VERSION" "$CRUN_SHA256" "$(version_marker crun)" \
                        "${SYSEXT_DIR}/crun/usr/bin/crun"
                    then
                        continue
                    fi
                    ;;
                kubelet)
                    if check_cached "kubelet ${KUBELET_VERSION}" \
                        "$KUBELET_VERSION" "$KUBELET_SHA256" "$(version_marker kubelet)" \
                        "${SYSEXT_DIR}/kubelet/usr/bin/kubelet"
                    then
                        continue
                    fi
                    ;;
                cni)
                    if check_cached "cni-plugins ${CNI_VERSION}" \
                        "$CNI_VERSION" "$CNI_SHA256" "$(version_marker cni)" \
                        "${SYSEXT_DIR}/cni/usr/lib/cni"/*
                    then
                        continue
                    fi
                    ;;
                etcd)
                    if check_cached "etcd ${ETCD_VERSION}" \
                        "$ETCD_VERSION" "$ETCD_SHA256" "$(version_marker etcd)" \
                        "${SYSEXT_DIR}/etcd/usr/bin/etcd" \
                        "${SYSEXT_DIR}/etcd/usr/bin/etcdctl"
                    then
                        continue
                    fi
                    ;;
                kubernetes-cp)
                    if check_cached "kubernetes-cp ${KUBERNETES_CP_VERSION}" \
                        "$KUBERNETES_CP_VERSION" "$KUBERNETES_CP_SHA256" "$(version_marker kubernetes-cp)" \
                        "${SYSEXT_DIR}/kubernetes-cp/usr/bin/kube-apiserver" \
                        "${SYSEXT_DIR}/kubernetes-cp/usr/bin/kube-controller-manager" \
                        "${SYSEXT_DIR}/kubernetes-cp/usr/bin/kube-scheduler" \
                        "${SYSEXT_DIR}/kubernetes-cp/usr/bin/kubectl"
                    then
                        continue
                    fi
                    ;;
                perfetto)
                    if is_elf_binary "${SYSEXT_DIR}/perfetto/usr/bin/tracebox" &&
                        check_cached "tracebox ${PERFETTO_VERSION}" "$PERFETTO_VERSION" "$PERFETTO_SHA256" "$(version_marker perfetto)" \
                            "${SYSEXT_DIR}/perfetto/usr/bin/tracebox"
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
            # NOTE: trap set AFTER acquiring lock so losing processes
            # (which return 0 on mkdir failure) don't delete the winner's lock.
            (
                set -e
                mkdir "${SYSEXT_DIR}/${name}.lock" 2>/dev/null || return 0
                trap 'rm -rf "${SYSEXT_DIR}/${name}.lock"' EXIT
                info "Downloading ${name}..."
                case "$name" in
                    cri-o)    download_crio ;;
                    crun)     download_crun ;;
                    kubelet)  download_kubelet ;;
                    cni)      download_cni ;;
                    etcd)     download_etcd ;;
                    kubernetes-cp) download_kubernetes_cp ;;
                    perfetto)  download_perfetto ;;
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
