#!/usr/bin/env bash
# latency-loadgen.sh — Run the latency load generator on a pod's node and
# fetch the latency CSV back to the host.
#
# The host has no route to the pod CIDR, so HTTP load generation must run on
# the node that hosts the pod (mirrors start_load_generation's SSH pattern).
# This helper streams load-generator.sh to the node's bash, runs it there
# against the pod IP, and captures the per-request CSV rows it writes back
# into the local output file.
#
# Failure semantics: any failure — SSH error, unreachable target,
# generator error — leaves the output file absent (or partial) and exits
# non-zero so the caller can log a warning and continue; the run never
# hard-fails because of load generation.
#
# Usage:
#   latency-loadgen.sh <node-ip> <target-url> <rate> <duration> <endpoints> <output-file>
#
# Example:
#   latency-loadgen.sh 192.168.124.26 http://10.0.0.5:8080 50 120 \
#       "users:30,orders:30,search:20,reports:20" ./latency.csv

set -Eeuo pipefail

NODE_IP="${1:?latency-loadgen.sh: missing node-ip}"
TARGET_URL="${2:?latency-loadgen.sh: missing target-url}"
RATE="${3:?latency-loadgen.sh: missing rate}"
DURATION="${4:?latency-loadgen.sh: missing duration}"
ENDPOINTS="${5:?latency-loadgen.sh: missing endpoints}"
OUTPUT_FILE="${6:?latency-loadgen.sh: missing output-file}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
GENERATOR="${SCRIPT_DIR}/load-generator.sh"

if [[ ! -f "$GENERATOR" ]]; then
    echo "error: load-generator.sh not found at: ${GENERATOR}" >&2
    exit 1
fi

TARGET_URL="${TARGET_URL%/}"
REMOTE_CSV="/tmp/latency-loadgen-$$.csv"
REMOTE_SUMMARY="/tmp/latency-loadgen-$$.summary"

# Stream the generator script (stdin -> remote bash -s) to the node. The
# generator's own stdout summary is redirected to a remote file so the CSV is
# the only stdout on the SSH stream; when the generator finishes, the CSV is
# streamed back to the local output file and the remote command exits with the
# generator's rc. The single-quoted remote strings intentionally use
# locally-expanded values (target/rate/duration/endpoints/paths) while $? is
# escaped so it evaluates on the node.
# shellcheck disable=SC2029
rc=0
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=5 -o BatchMode=yes \
    "root@${NODE_IP}" \
    "bash -s '${TARGET_URL}' --rate '${RATE}' --duration '${DURATION}' --endpoints '${ENDPOINTS}' --output '${REMOTE_CSV}' > '${REMOTE_SUMMARY}' 2>&1; rc=\$?; cat '${REMOTE_CSV}' 2>/dev/null; rm -f '${REMOTE_CSV}' '${REMOTE_SUMMARY}'; exit \$rc" \
    < "$GENERATOR" \
    > "$OUTPUT_FILE" || rc=$?

# Nothing captured (SSH failure, generator crash before writing) — drop the
# empty artifact and report failure so the caller warns and continues.
if [[ ! -s "$OUTPUT_FILE" ]]; then
    rm -f "$OUTPUT_FILE"
    echo "error: latency generation produced no output for ${TARGET_URL} (ssh rc=${rc})" >&2
    exit 1
fi

# A CSV with a header but zero data rows is not a usable generation result.
local_rows="$(tail -n +2 "$OUTPUT_FILE" 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
if [[ "$local_rows" -eq 0 ]]; then
    rm -f "$OUTPUT_FILE"
    echo "error: latency generator produced no data rows for ${TARGET_URL}" >&2
    exit 1
fi

# Propagate the generator's rc: non-zero (e.g. unreachable target with error
# rows recorded) still keeps the partial CSV for analysis, but signals the
# caller that generation degraded.
exit "$rc"
