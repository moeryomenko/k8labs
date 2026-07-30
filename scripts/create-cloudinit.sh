#!/usr/bin/env bash
# create-cloudinit.sh — Generate a FAT16 disk image with cloud-init NoCloud data
#
# Usage: ./scripts/create-cloudinit.sh --user-data <file> --meta-data <file> [--network-config <file>] --output <file>
#
# Creates a FAT16-formatted disk image (4 MB) with volume label "CIDATA"
# containing user-data, meta-data, and optionally network-config for
# Cloud-Hypervisor's cloud-init NoCloud datasource.
#
# Requires: mkdosfs (dosfstools), mcopy (mtools)
set -Eeuo pipefail
IFS=$'\n\t'

# ---- Configuration ----
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

# Disk geometry: 8192 sectors of 512 bytes = 4 MB
DISK_SECTORS=8192
DISK_LABEL="CIDATA"

# ---- Cleanup ----
_TMPDIR=""

_cleanup() {
	local exit_code=$?
	if [[ -n "${_TMPDIR}" && -d "${_TMPDIR}" ]]; then
		rm -rf -- "${_TMPDIR}"
	fi
	trap - EXIT ERR
	exit "${exit_code}"
}

_error_handler() {
	local line="${1}"
	local cmd="${2}"
	printf '[ERROR] %s: Command failed at line %d: %s\n' "${SCRIPT_NAME}" "${line}" "${cmd}" >&2
}

trap _cleanup EXIT
trap '_error_handler $LINENO "$BASH_COMMAND"' ERR

# ---- Usage ----
usage() {
	cat <<EOF
Usage: ${SCRIPT_NAME} [OPTIONS]

Generate a FAT16 disk image with cloud-init NoCloud datasource files.

Options:
  --user-data FILE       REQUIRED: cloud-init user-data file
  --meta-data FILE       REQUIRED: cloud-init meta-data file
  --network-config FILE  OPTIONAL: cloud-init network-config file
  --output FILE          REQUIRED: output disk image path (e.g., /tmp/cloudinit.img)
  -h, --help             Show this help and exit

Examples:
  ${SCRIPT_NAME} --user-data user-data --meta-data meta-data --output /tmp/cloudinit.img
  ${SCRIPT_NAME} --user-data user-data --meta-data meta-data --network-config network-config --output /tmp/cloudinit.img
EOF
	exit "${1:-0}"
}

# ---- Parse arguments ----
USER_DATA=""
META_DATA=""
NETWORK_CONFIG=""
OUTPUT_FILE=""

while [[ $# -gt 0 ]]; do
	case "$1" in
		--user-data)
			USER_DATA="${2:?--user-data requires a value}"
			shift 2
			;;
		--user-data=*)
			USER_DATA="${1#*=}"
			shift
			;;
		--meta-data)
			META_DATA="${2:?--meta-data requires a value}"
			shift 2
			;;
		--meta-data=*)
			META_DATA="${1#*=}"
			shift
			;;
		--network-config)
			NETWORK_CONFIG="${2:?--network-config requires a value}"
			shift 2
			;;
		--network-config=*)
			NETWORK_CONFIG="${1#*=}"
			shift
			;;
		--output)
			OUTPUT_FILE="${2:?--output requires a value}"
			shift 2
			;;
		--output=*)
			OUTPUT_FILE="${1#*=}"
			shift
			;;
		-h|--help)
			usage 0
			;;
		-*)
			printf 'ERROR: Unknown option: %s\n' "$1" >&2
			usage 1
			;;
		*)
			printf 'ERROR: Unexpected argument: %s\n' "$1" >&2
			usage 1
			;;
	esac
done

# ---- Validate required arguments ----
[[ -n "${USER_DATA}" ]] || {
	printf 'ERROR: --user-data is required\n' >&2
	usage 1
}

[[ -n "${META_DATA}" ]] || {
	printf 'ERROR: --meta-data is required\n' >&2
	usage 1
}

[[ -n "${OUTPUT_FILE}" ]] || {
	printf 'ERROR: --output is required\n' >&2
	usage 1
}

# ---- Validate source files ----
if [[ ! -f "${USER_DATA}" ]]; then
	printf 'ERROR: user-data file not found: %s\n' "${USER_DATA}" >&2
	exit 1
fi

if [[ ! -f "${META_DATA}" ]]; then
	printf 'ERROR: meta-data file not found: %s\n' "${META_DATA}" >&2
	exit 1
fi

if [[ -n "${NETWORK_CONFIG}" && ! -f "${NETWORK_CONFIG}" ]]; then
	printf 'ERROR: network-config file not found: %s\n' "${NETWORK_CONFIG}" >&2
	exit 1
fi

# ---- Check dependencies ----
check_deps() {
	local -a missing=()
	local cmd

	for cmd in "$@"; do
		if ! command -v "${cmd}" &>/dev/null; then
			missing+=("${cmd}")
		fi
	done

	if [[ ${#missing[@]} -gt 0 ]]; then
		printf 'ERROR: %s requires: %s\n' "${SCRIPT_NAME}" "${missing[*]}" >&2
		printf 'Install missing dependencies:\n' >&2
		for cmd in "${missing[@]}"; do
			printf '  - %s\n' "${cmd}" >&2
		done
		exit 1
	fi
}

check_deps mkdosfs mcopy

# ---- Build disk image ----
# Create temporary working directory
_TMPDIR=$(mktemp -d) || {
	printf 'FATAL: Cannot create temporary directory\n' >&2
	exit 1
}

# Create an empty disk image
DISK_IMG="${_TMPDIR}/disk.img"
if ! dd if=/dev/zero of="${DISK_IMG}" bs=512 count="${DISK_SECTORS}" status=none; then
	printf 'FATAL: Failed to create disk image\n' >&2
	exit 1
fi

# Format as FAT16 with CIDATA volume label.
# -s 1: 1 sector per cluster (minimum) to fit FAT16 on a 4 MB image.
if ! mkdosfs -F 16 -s 1 -n "${DISK_LABEL}" "${DISK_IMG}" &>/dev/null; then
	printf 'FATAL: mkdosfs failed\n' >&2
	exit 1
fi

# Copy cloud-init files to the disk image using mcopy
# MTOOLS_SKIP_CHECK avoids floppy-mode check for disk images
export MTOOLS_SKIP_CHECK=1

if ! mcopy -i "${DISK_IMG}" "${USER_DATA}" ::user-data; then
	printf 'FATAL: Failed to copy user-data to disk image\n' >&2
	exit 1
fi

if ! mcopy -i "${DISK_IMG}" "${META_DATA}" ::meta-data; then
	printf 'FATAL: Failed to copy meta-data to disk image\n' >&2
	exit 1
fi

if [[ -n "${NETWORK_CONFIG}" ]]; then
	if ! mcopy -i "${DISK_IMG}" "${NETWORK_CONFIG}" ::network-config; then
		printf 'FATAL: Failed to copy network-config to disk image\n' >&2
		exit 1
	fi
fi

# Move completed disk image to final location
mv -- "${DISK_IMG}" "${OUTPUT_FILE}"

# ---- Success summary ----
printf 'Created CIDATA disk: %s (4 MB)\n' "${OUTPUT_FILE}"
