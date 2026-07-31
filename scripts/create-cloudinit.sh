#!/usr/bin/env bash
# create-cloudinit.sh — Generate a FAT16 disk image with cloud-init NoCloud data
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"
DISK_SECTORS=8192
DISK_LABEL="CIDATA"

# ---- Cleanup ----
_TMPDIR=""
_cleanup() {
	local ec=$?
	[[ -n "${_TMPDIR}" && -d "${_TMPDIR}" ]] && rm -rf -- "${_TMPDIR}"
	trap - EXIT ERR
	exit "${ec}"
}
trap _cleanup EXIT
trap 'printf "[ERROR] %s: Failed at line %d\n" "${SCRIPT_NAME}" "${LINENO}" >&2' ERR

# ---- Parse arguments ----
USER_DATA=""; META_DATA=""; NETWORK_CONFIG=""; OUTPUT_FILE=""
while [[ $# -gt 0 ]]; do
	case "$1" in
		--user-data)        USER_DATA="${2:?--user-data requires a value}";        shift 2 ;;
		--user-data=*)      USER_DATA="${1#*=}";                                  shift   ;;
		--meta-data)        META_DATA="${2:?--meta-data requires a value}";        shift 2 ;;
		--meta-data=*)      META_DATA="${1#*=}";                                  shift   ;;
		--network-config)   NETWORK_CONFIG="${2:?--network-config requires a value}"; shift 2 ;;
		--network-config=*) NETWORK_CONFIG="${1#*=}";                              shift   ;;
		--output)           OUTPUT_FILE="${2:?--output requires a value}";         shift 2 ;;
		--output=*)         OUTPUT_FILE="${1#*=}";                                 shift   ;;
		-h|--help)          echo "Usage: ${SCRIPT_NAME} --user-data <file> --meta-data <file> [--network-config <file>] --output <file>"; exit 0 ;;
		*)                  echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
	esac
done

# ---- Validate ----
[[ -n "${USER_DATA}" ]]   || { echo "ERROR: --user-data is required" >&2; exit 1; }
[[ -n "${META_DATA}" ]]   || { echo "ERROR: --meta-data is required" >&2; exit 1; }
[[ -n "${OUTPUT_FILE}" ]] || { echo "ERROR: --output is required" >&2; exit 1; }
[[ -f "${USER_DATA}" ]]   || { echo "ERROR: user-data not found: ${USER_DATA}" >&2; exit 1; }
[[ -f "${META_DATA}" ]]   || { echo "ERROR: meta-data not found: ${META_DATA}" >&2; exit 1; }
if [[ -n "${NETWORK_CONFIG}" && ! -f "${NETWORK_CONFIG}" ]]; then
	echo "ERROR: network-config not found: ${NETWORK_CONFIG}" >&2
	exit 1
fi

# ---- Check dependencies ----
command -v mkdosfs &>/dev/null || { echo "ERROR: mkdosfs not found (install dosfstools)" >&2; exit 1; }
command -v mcopy   &>/dev/null || { echo "ERROR: mcopy not found (install mtools)" >&2; exit 1; }

# ---- Build disk image ----
_TMPDIR="$(mktemp -d)"
DISK_IMG="${_TMPDIR}/disk.img"
dd if=/dev/zero of="${DISK_IMG}" bs=512 count="${DISK_SECTORS}" status=none
mkdosfs -F 16 -s 1 -n "${DISK_LABEL}" "${DISK_IMG}" &>/dev/null

export MTOOLS_SKIP_CHECK=1
mcopy -i "${DISK_IMG}" "${USER_DATA}"      ::user-data
mcopy -i "${DISK_IMG}" "${META_DATA}"      ::meta-data
[[ -n "${NETWORK_CONFIG}" ]] && mcopy -i "${DISK_IMG}" "${NETWORK_CONFIG}" ::network-config

mv -- "${DISK_IMG}" "${OUTPUT_FILE}"
printf 'Created CIDATA disk: %s (4 MB)\n' "${OUTPUT_FILE}"
