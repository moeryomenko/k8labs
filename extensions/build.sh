#!/bin/sh
#
# extensions/build.sh — Package sysext and confext extensions
#
# POSIX-compatible shell script for building systemd-sysext and
# systemd-confext extension images from source directory trees.
#
# Usage: build.sh [--dir] <type> <source-dir> [output-name]
#

set -u

SCRIPT_DIR=$(dirname "$0")
RELEASE_DIR="${SCRIPT_DIR}/release"

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<'USAGE_EOF'
Usage: build.sh [--dir] <type> <source-dir> [output-name]

Package a sysext or confext extension.

Arguments:
  --dir          Directory mode: validate + copy, no squashfs
  <type>         Extension type: sysext or confext
  <source-dir>   Path to extension source tree
  [output-name]  Name for output file (default: basename of source-dir)

Types:
  sysext   Must have usr/ directory and extension-release.d/ with .sysext files
  confext  Must have etc/ directory and extension-release.d/ with .confext files

Output:
  Default: squashfs image at release/<name>.raw (via mksquashfs or genisoimage)
  --dir:   directory tree copied to release/<name>/
USAGE_EOF
}

# ---------------------------------------------------------------------------
# Error helper
# ---------------------------------------------------------------------------
die() {
    echo "Error: $*" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
dir_mode=0

case "${1:-}" in
    --help)
        usage
        exit 0
        ;;
    --dir)
        dir_mode=1
        shift
        ;;
esac

if [ $# -lt 2 ]; then
    usage >&2
    exit 1
fi

type="$1"
source_dir="$2"
output_name="${3:-$(basename "$source_dir")}"

# ---------------------------------------------------------------------------
# Validate source directory exists
# ---------------------------------------------------------------------------
[ -d "$source_dir" ] || die "source directory not found: $source_dir"

# ---------------------------------------------------------------------------
# Validation by type
# ---------------------------------------------------------------------------
case "$type" in
    sysext)
        [ -d "${source_dir}/usr" ] || die "sysext must have usr/ directory"
        if [ ! -d "${source_dir}/extension-release.d" ]; then
            die "sysext must have extension-release.d/ directory"
        fi
        # Check at least one .sysext file exists
        found=0
        for f in "${source_dir}/extension-release.d/"*.sysext; do
            [ -f "$f" ] && found=1 && break
        done
        [ "$found" -eq 1 ] || die "sysext must have at least one .sysext file in extension-release.d/"
        ;;
    confext)
        [ -d "${source_dir}/etc" ] || die "confext must have etc/ directory"
        if [ ! -d "${source_dir}/extension-release.d" ]; then
            die "confext must have extension-release.d/ directory"
        fi
        # Check at least one .confext file exists
        found=0
        for f in "${source_dir}/extension-release.d/"*.confext; do
            [ -f "$f" ] && found=1 && break
        done
        [ "$found" -eq 1 ] || die "confext must have at least one .confext file in extension-release.d/"
        ;;
    *)
        die "invalid type '${type}' (must be sysext or confext)"
        ;;
esac

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
mkdir -p "$RELEASE_DIR"

if [ "$dir_mode" -eq 1 ]; then
    # Directory mode: copy tree
    target="${RELEASE_DIR}/${output_name}"
    rm -rf "$target"
    mkdir -p "$target"
    cp -R "${source_dir}/." "$target/"
else
    # Squashfs mode
    output_file="${RELEASE_DIR}/${output_name}.raw"

    if command -v mksquashfs >/dev/null 2>&1; then
        mksquashfs "$source_dir" "$output_file" -noappend -all-root
    elif command -v genisoimage >/dev/null 2>&1; then
        genisoimage -output "$output_file" "$source_dir"
    else
        echo "Warning: neither mksquashfs nor genisoimage found, falling back to directory mode" >&2
        target="${RELEASE_DIR}/${output_name}"
        rm -rf "$target"
        mkdir -p "$target"
        cp -R "${source_dir}/." "$target/"
    fi
fi
